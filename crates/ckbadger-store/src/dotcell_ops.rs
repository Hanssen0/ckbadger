//! Read operations for `.cell` (DotCell) names.
//!
//! Ownership is stored as the chain stores it — a 20-byte lock-hash prefix —
//! so the owner index and the per-owner counter are both keyed by those 20
//! bytes. Resolving a prefix to an address is a read-time concern
//! (`resolve_lock_hash_prefix`), never a write-time guess.

use std::collections::BTreeSet;

use rocksdb::IteratorMode;

use crate::keys;
use crate::store::CkbadgerStore;
use crate::types::{DotCellNamespace, DotCellRingRoot, IdentityExtra, DOTCELL_SENTINEL_COLLECTION};

impl CkbadgerStore {
    /// The `.cell` name ids owned by one 20-byte owner prefix, in ascending id
    /// order. `cursor` is exclusive: pass the last id of the previous page.
    pub fn list_dotcell_names_by_owner20(
        &self,
        owner20: &[u8; 20],
        cursor: Option<[u8; 20]>,
        limit: usize,
    ) -> anyhow::Result<Vec<[u8; 20]>> {
        let prefix = keys::encode_dotcell_name_by_owner_prefix(owner20);
        let mut out = Vec::new();
        for item in self.prefix_iterator_cf(self.cf_dotcell_name_by_owner(), &prefix) {
            let (key, _) = item.map_err(|e| {
                anyhow::anyhow!(
                    "failed to iterate dotcell_name_by_owner: owner20=0x{}, {}",
                    hex::encode(owner20),
                    e
                )
            })?;
            if !key.starts_with(&prefix) {
                break;
            }
            if key.len() != keys::DOTCELL_NAME_BY_OWNER_KEY_SIZE {
                anyhow::bail!(
                    "dotcell_name_by_owner key is not {} bytes: len={}, key=0x{}",
                    keys::DOTCELL_NAME_BY_OWNER_KEY_SIZE,
                    key.len(),
                    hex::encode(&key)
                );
            }
            let (_, id) = keys::decode_dotcell_name_by_owner_key(&key);
            if let Some(cursor) = cursor {
                if id <= cursor {
                    continue;
                }
            }
            out.push(id);
            if out.len() >= limit {
                break;
            }
        }
        Ok(out)
    }

    /// How many `.cell` names one 20-byte owner prefix holds live, from the
    /// per-owner counter in `CF_STATS_IDENTITY`.
    pub fn get_dotcell_owner20_count(
        &self,
        collection_id: &[u8],
        owner20: &[u8; 20],
    ) -> anyhow::Result<i64> {
        let key = keys::encode_identity_owner20_key(collection_id, owner20);
        match self.get_cf(self.cf_stats_identity(), &key)? {
            Some(value) => {
                if value.len() != 8 {
                    anyhow::bail!(
                        "invalid identity owner value length: expected 8, got {} (owner20=0x{})",
                        value.len(),
                        hex::encode(owner20)
                    );
                }
                Ok(i64::from_le_bytes(value[..8].try_into().expect("8 bytes")))
            }
            None => Ok(0),
        }
    }

    /// The ring root recorded for one namespace, if the root cell has been
    /// indexed yet.
    pub fn get_dotcell_ring(
        &self,
        namespace_args: &[u8; 20],
    ) -> anyhow::Result<Option<DotCellRingRoot>> {
        match self.get_cf(self.cf_dotcell_ring(), namespace_args)? {
            Some(value) => Ok(Some(bincode::deserialize(&value).map_err(|e| {
                anyhow::anyhow!(
                    "failed to deserialize DotCellRingRoot: namespace=0x{}, error={}",
                    hex::encode(namespace_args),
                    e
                )
            })?)),
            None => Ok(None),
        }
    }

    /// Every namespace's ring root. A network has exactly one; more than one
    /// would make bare 20-byte ids collide on identical labels, which the
    /// write path refuses.
    pub fn list_dotcell_rings(&self) -> anyhow::Result<Vec<([u8; 20], DotCellRingRoot)>> {
        let mut out = Vec::new();
        for item in self.iterator_cf(self.cf_dotcell_ring(), IteratorMode::Start) {
            let (key, value) =
                item.map_err(|e| anyhow::anyhow!("failed to iterate dotcell_ring: {}", e))?;
            if key.len() != 20 {
                anyhow::bail!(
                    "dotcell_ring key is not 20 bytes: len={}, key=0x{}",
                    key.len(),
                    hex::encode(&key)
                );
            }
            let namespace: [u8; 20] = key[..20].try_into().expect("20 bytes");
            let root: DotCellRingRoot = bincode::deserialize(&value).map_err(|e| {
                anyhow::anyhow!(
                    "failed to deserialize DotCellRingRoot: namespace=0x{}, error={}",
                    hex::encode(namespace),
                    e
                )
            })?;
            out.push((namespace, root));
        }
        Ok(out)
    }

    /// The persisted recognized `.cell` v3 namespace row, if written.
    pub fn get_dotcell_namespace(&self) -> anyhow::Result<Option<DotCellNamespace>> {
        let key = keys::encode_dotcell_namespace_key();
        match self.get_cf(self.cf_stats_spore(), &key)? {
            Some(value) => {
                if value.len() != 20 {
                    anyhow::bail!(
                        "invalid DotCellNamespace value length: expected 20, got {}",
                        value.len()
                    );
                }
                Ok(Some(bincode::deserialize(&value).map_err(|e| {
                    anyhow::anyhow!("failed to deserialize DotCellNamespace: error={}", e)
                })?))
            }
            None => Ok(None),
        }
    }

    /// Every recognized `.cell` v3 namespace present in this store.
    ///
    /// Reads the persisted `DotCellNamespace` row when present and always
    /// checks legacy/committed `CF_DOTCELL_RING` rows. When the persisted row
    /// is absent (old DBs), also scans decoded `.cell` identities in
    /// `CF_IDENTITY_DATA` via `CF_IDENTITY_BY_COLLECTION` so preexisting
    /// name-only history enforces the namespace without a migration or reset.
    /// Never reads opaque or unindexed `.cell` markers.
    pub fn list_known_dotcell_namespaces(&self) -> anyhow::Result<Vec<DotCellNamespace>> {
        let mut namespaces: BTreeSet<[u8; 20]> = BTreeSet::new();

        let persisted = self.get_dotcell_namespace()?;
        if let Some(ns) = persisted {
            namespaces.insert(ns.namespace_args);
        }

        for (namespace_args, _root) in self.list_dotcell_rings()? {
            namespaces.insert(namespace_args);
        }

        if persisted.is_none() {
            const CHUNK_SIZE: usize = 512;
            let mut cursor: Option<Vec<u8>> = None;
            loop {
                let ids = self.list_identity_ids_by_collection(
                    &DOTCELL_SENTINEL_COLLECTION,
                    cursor.as_deref(),
                    CHUNK_SIZE,
                )?;
                if ids.is_empty() {
                    break;
                }
                let fetched_len = ids.len();
                let next_cursor = ids.last().cloned();
                for (id, entry) in ids.iter().zip(self.get_identities(&ids)?) {
                    let Some(entry) = entry else {
                        anyhow::bail!(
                            "missing identity in .cell collection: 0x{}",
                            hex::encode(id)
                        );
                    };
                    let IdentityExtra::DotCell { namespace_args, .. } = entry.extra else {
                        anyhow::bail!(
                            "unexpected identity in .cell collection: 0x{}",
                            hex::encode(id)
                        );
                    };
                    namespaces.insert(namespace_args);
                }
                if fetched_len < CHUNK_SIZE {
                    break;
                }
                cursor = next_cursor;
            }
        }

        Ok(namespaces
            .into_iter()
            .map(|namespace_args| DotCellNamespace { namespace_args })
            .collect())
    }

    /// Resolve the single recognized `.cell` v3 namespace for this store, if
    /// any, failing loudly if contradictory namespaces exist.
    pub fn resolve_dotcell_namespace(&self) -> anyhow::Result<Option<DotCellNamespace>> {
        let namespaces = self.list_known_dotcell_namespaces()?;
        match namespaces.as_slice() {
            [] => Ok(None),
            [only] => Ok(Some(*only)),
            many => anyhow::bail!(
                "multiple .cell namespaces present in store: {}",
                many.iter()
                    .map(|ns| format!("0x{}", hex::encode(ns.namespace_args)))
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use crate::batch::StoreBatch;
    use crate::keys;
    use crate::store::CkbadgerStore;
    use crate::types::{DotCellRingRoot, DOTCELL_SENTINEL_COLLECTION};
    use tempfile::TempDir;

    fn id(byte: u8) -> [u8; 20] {
        [byte; 20]
    }

    #[test]
    fn list_dotcell_names_by_owner20_scans_prefix_in_id_order() {
        let dir = TempDir::new().unwrap();
        let store = CkbadgerStore::open_test_unified(dir.path()).unwrap();
        let owner_a = id(0xAA);
        let owner_b = id(0xBB);

        let mut batch = StoreBatch::new(&store);
        for name in [id(0x30), id(0x10), id(0x20)] {
            batch.put_dotcell_name_by_owner(&owner_a, &name);
        }
        batch.put_dotcell_name_by_owner(&owner_b, &id(0x99));
        batch.commit().unwrap();

        assert_eq!(
            store
                .list_dotcell_names_by_owner20(&owner_a, None, 10)
                .unwrap(),
            vec![id(0x10), id(0x20), id(0x30)]
        );
        assert_eq!(
            store
                .list_dotcell_names_by_owner20(&owner_b, None, 10)
                .unwrap(),
            vec![id(0x99)]
        );
        assert_eq!(
            store
                .list_dotcell_names_by_owner20(&owner_a, Some(id(0x10)), 10)
                .unwrap(),
            vec![id(0x20), id(0x30)],
            "the cursor is exclusive"
        );
        assert_eq!(
            store
                .list_dotcell_names_by_owner20(&owner_a, None, 2)
                .unwrap()
                .len(),
            2
        );
        assert!(store
            .list_dotcell_names_by_owner20(&id(0xCC), None, 10)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn deleting_an_owner_row_removes_it_from_the_listing() {
        let dir = TempDir::new().unwrap();
        let store = CkbadgerStore::open_test_unified(dir.path()).unwrap();
        let owner = id(0xAA);

        let mut batch = StoreBatch::new(&store);
        batch.put_dotcell_name_by_owner(&owner, &id(0x10));
        batch.put_dotcell_name_by_owner(&owner, &id(0x20));
        batch.commit().unwrap();

        let mut batch = StoreBatch::new(&store);
        batch.delete_dotcell_name_by_owner(&owner, &id(0x10));
        batch.commit().unwrap();

        assert_eq!(
            store
                .list_dotcell_names_by_owner20(&owner, None, 10)
                .unwrap(),
            vec![id(0x20)]
        );
    }

    #[test]
    fn dotcell_ring_roundtrip_and_namespace_guard_read() {
        let dir = TempDir::new().unwrap();
        let store = CkbadgerStore::open_test_unified(dir.path()).unwrap();
        let namespace = id(0x42);
        let root = DotCellRingRoot {
            root_tx_hash: vec![0x77; 32],
            root_output_index: 0,
            first_id: id(0x11),
            created_at_block: 20_515_882,
        };

        let mut batch = StoreBatch::new(&store);
        batch.put_dotcell_ring(&namespace, &root);
        batch.commit().unwrap();

        assert_eq!(
            store.get_dotcell_ring(&namespace).unwrap(),
            Some(root.clone())
        );
        assert_eq!(store.get_dotcell_ring(&id(0x43)).unwrap(), None);
        assert_eq!(store.list_dotcell_rings().unwrap(), vec![(namespace, root)]);
    }

    #[test]
    fn owner20_counts_read_back_through_the_explicit_20_byte_key() {
        let dir = TempDir::new().unwrap();
        let store = CkbadgerStore::open_test_unified(dir.path()).unwrap();
        let owner = id(0x5A);

        let mut batch = StoreBatch::new(&store);
        batch.put_identity_owner20_count(&DOTCELL_SENTINEL_COLLECTION, &owner, 3);
        batch.commit().unwrap();

        assert_eq!(
            store
                .get_dotcell_owner20_count(&DOTCELL_SENTINEL_COLLECTION, &owner)
                .unwrap(),
            3
        );
        assert_eq!(
            store
                .get_dotcell_owner20_count(&DOTCELL_SENTINEL_COLLECTION, &id(0x5B))
                .unwrap(),
            0
        );
        // The generic listing hands back the 32-byte owner segment; a dotcell
        // caller decodes the 20 bytes the chain actually gave it.
        let rows = store
            .list_identity_owner_counts(&DOTCELL_SENTINEL_COLLECTION)
            .unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(keys::decode_identity_owner20(&rows[0].0), owner);
        assert_eq!(rows[0].1, 3);
    }

    #[test]
    fn dotcell_namespace_roundtrip_and_legacy_fallback_resolution() {
        use crate::types::{DotCellNamespace, IdentityEntry, IdentityExtra, IdentityStandard};

        let dir = TempDir::new().unwrap();
        let store = CkbadgerStore::open_test_unified(dir.path()).unwrap();
        let namespace = id(0x42);

        assert_eq!(store.get_dotcell_namespace().unwrap(), None);
        assert_eq!(store.resolve_dotcell_namespace().unwrap(), None);

        // An opaque/unindexed marker must never establish a namespace.
        let mut batch = StoreBatch::new(&store);
        batch.put_stats(&keys::encode_dotcell_unsupported_key(&[0x11; 32], 0), &[]);
        batch.commit().unwrap();
        assert_eq!(store.get_dotcell_namespace().unwrap(), None);
        assert_eq!(store.resolve_dotcell_namespace().unwrap(), None);

        // Preexisting name-only history (old DB without 0x31 or ring row)
        // resolves the namespace regardless of holding creation blocks (which
        // may reset on takeover).
        let mut batch = StoreBatch::new(&store);
        for (name_byte, block) in [(0x10u8, 520i64), (0x20u8, 500i64)] {
            let name_id = id(name_byte);
            batch.put_identity(
                &name_id,
                &IdentityEntry {
                    standard: IdentityStandard::DotCell,
                    owner_lock_hash: None,
                    name: Some(format!("n{name_byte}.cell")),
                    is_live: name_byte == 0x10,
                    created_at_block: block,
                    created_at_tx: vec![name_byte; 32],
                    extra: IdentityExtra::DotCell {
                        label: format!("n{name_byte}"),
                        namespace_args: namespace,
                        layout_version: 3,
                        expired_at: 1_900_000_000,
                        owner_hash20: id(0xAA),
                        manager_hash20: id(0xAA),
                        next_id: [0u8; 20],
                        records_hash: [0u8; 32],
                        records: vec![],
                        parent_id: None,
                    },
                },
            );
            batch.put_identity_by_collection(&DOTCELL_SENTINEL_COLLECTION, &name_id);
        }
        batch.commit().unwrap();

        let expected = DotCellNamespace {
            namespace_args: namespace,
        };
        assert_eq!(store.get_dotcell_namespace().unwrap(), None);
        assert_eq!(store.resolve_dotcell_namespace().unwrap(), Some(expected));

        // Adding a legacy ring row resolves the same single namespace.
        let mut batch = StoreBatch::new(&store);
        batch.put_dotcell_ring(
            &namespace,
            &DotCellRingRoot {
                root_tx_hash: vec![0x77; 32],
                root_output_index: 0,
                first_id: id(0x10),
                created_at_block: 530,
            },
        );
        batch.commit().unwrap();
        assert_eq!(store.resolve_dotcell_namespace().unwrap(), Some(expected));

        // Explicit persisted namespace round-trips cleanly as 20 raw bytes.
        let mut batch = StoreBatch::new(&store);
        batch.put_dotcell_namespace(&expected);
        batch.commit().unwrap();
        assert_eq!(
            store
                .get_cf(
                    store.cf_stats_spore(),
                    &keys::encode_dotcell_namespace_key()
                )
                .unwrap()
                .unwrap()
                .len(),
            20
        );
        assert_eq!(store.get_dotcell_namespace().unwrap(), Some(expected));
        assert_eq!(store.resolve_dotcell_namespace().unwrap(), Some(expected));
    }

    #[test]
    fn legacy_dotcell_membership_and_corrupt_namespace_rows_preserve_errors() {
        use crate::types::{IdentityEntry, IdentityExtra, IdentityStandard};

        // Missing identity record in collection.
        {
            let dir = TempDir::new().unwrap();
            let store = CkbadgerStore::open_test_unified(dir.path()).unwrap();
            let mut batch = StoreBatch::new(&store);
            batch.put_identity_by_collection(&DOTCELL_SENTINEL_COLLECTION, &id(0x42));
            batch.commit().unwrap();
            assert!(store
                .resolve_dotcell_namespace()
                .unwrap_err()
                .to_string()
                .contains("missing identity"));
        }

        // Unexpected IdentityExtra variant in .cell collection.
        {
            let dir = TempDir::new().unwrap();
            let store = CkbadgerStore::open_test_unified(dir.path()).unwrap();
            let mut batch = StoreBatch::new(&store);
            batch.put_identity(
                &id(0x43),
                &IdentityEntry {
                    standard: IdentityStandard::DidCkb,
                    owner_lock_hash: Some(vec![0x11; 32]),
                    name: None,
                    is_live: true,
                    created_at_block: 10,
                    created_at_tx: vec![0x22; 32],
                    extra: IdentityExtra::DidCkb,
                },
            );
            batch.put_identity_by_collection(&DOTCELL_SENTINEL_COLLECTION, &id(0x43));
            batch.commit().unwrap();
            assert!(store
                .resolve_dotcell_namespace()
                .unwrap_err()
                .to_string()
                .contains("unexpected identity"));
        }

        // Corrupt persisted 0x31 row length fails loudly.
        {
            let dir = TempDir::new().unwrap();
            let store = CkbadgerStore::open_test_unified(dir.path()).unwrap();
            let mut batch = StoreBatch::new(&store);
            batch.put_stats(&keys::encode_dotcell_namespace_key(), &[0xAA; 28]);
            batch.commit().unwrap();
            assert!(store
                .get_dotcell_namespace()
                .unwrap_err()
                .to_string()
                .contains("invalid DotCellNamespace value length"));
        }

        // Contradictory persisted namespace and ring namespace fails loudly.
        {
            let dir = TempDir::new().unwrap();
            let store = CkbadgerStore::open_test_unified(dir.path()).unwrap();
            let mut batch = StoreBatch::new(&store);
            batch.put_dotcell_namespace(&crate::types::DotCellNamespace {
                namespace_args: id(0x01),
            });
            batch.put_dotcell_ring(
                &id(0x02),
                &DotCellRingRoot {
                    root_tx_hash: vec![0x77; 32],
                    root_output_index: 0,
                    first_id: id(0x10),
                    created_at_block: 10,
                },
            );
            batch.commit().unwrap();
            assert!(store
                .resolve_dotcell_namespace()
                .unwrap_err()
                .to_string()
                .contains("multiple .cell namespaces"));
        }
    }
}
