#!/usr/bin/env python3
"""Offline unit tests for packaging/ckbadger-v0.8.3/publish_verify.py."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_verify as pv  # noqa: E402


LOCAL_TAG = "ckbadger:0.8.3"
REMOTE_TAG = "ghcr.io/hanssen0/ckbadger:sha-aaaaaaaaaaaa"


def make_config_bytes(seed: str = "v0.8.3") -> tuple[bytes, str]:
    raw = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "config": {"Labels": {"org.opencontainers.image.version": seed}},
            "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "1" * 64]},
        },
        sort_keys=True,
    ).encode("utf-8")
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()


def write_tar(
    path: Path,
    entries: list[tuple[str, bytes]],
    *,
    symlinks: list[tuple[str, str]] | None = None,
) -> None:
    with tarfile.open(path, mode="w:") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for link_name, target in symlinks or []:
            info = tarfile.TarInfo(name=link_name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)


class ArchiveConfigDigestTests(unittest.TestCase):
    def test_classic_and_oci_archives_hash_actual_config_bytes_distinct_from_manifest_id(
        self,
    ) -> None:
        config_bytes, config_digest = make_config_bytes()
        config_hex = config_digest.split(":", 1)[1]
        distinct_manifest_digest = "sha256:" + "9" * 64
        self.assertNotEqual(distinct_manifest_digest, config_digest)

        with tempfile.TemporaryDirectory() as tmp:
            # Classic docker-save layout: <config_hex>.json
            classic = Path(tmp) / "classic.tar"
            write_tar(
                classic,
                [
                    (
                        "manifest.json",
                        json.dumps(
                            [{"Config": f"{config_hex}.json", "RepoTags": [LOCAL_TAG], "Layers": []}]
                        ).encode("utf-8"),
                    ),
                    (f"{config_hex}.json", config_bytes),
                ],
            )
            self.assertEqual(pv.archive_config_digest(classic, LOCAL_TAG), config_digest)

            # Containerd/OCI docker-save layout: blobs/sha256/<hex> + index.json
            # carrying a distinct top-level manifest digest (what containerd .Id reports).
            oci = Path(tmp) / "oci.tar"
            write_tar(
                oci,
                [
                    ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
                    (
                        "index.json",
                        json.dumps(
                            {
                                "schemaVersion": 2,
                                "mediaType": "application/vnd.oci.image.index.v1+json",
                                "manifests": [
                                    {
                                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                                        "digest": distinct_manifest_digest,
                                        "size": 123,
                                    }
                                ],
                            }
                        ).encode("utf-8"),
                    ),
                    (
                        "manifest.json",
                        json.dumps(
                            [
                                {
                                    "Config": f"blobs/sha256/{config_hex}",
                                    "RepoTags": [LOCAL_TAG],
                                    "Layers": [],
                                }
                            ]
                        ).encode("utf-8"),
                    ),
                    (f"blobs/sha256/{config_hex}", config_bytes),
                ],
            )
            self.assertEqual(pv.archive_config_digest(oci, LOCAL_TAG), config_digest)

            # Arbitrary relative config path (no guessed filenames).
            custom = Path(tmp) / "custom.tar"
            write_tar(
                custom,
                [
                    (
                        "./manifest.json",
                        json.dumps(
                            [
                                {
                                    "Config": "./configs/image.json",
                                    "RepoTags": [LOCAL_TAG],
                                    "Layers": [],
                                }
                            ]
                        ).encode("utf-8"),
                    ),
                    ("./configs/image.json", config_bytes),
                ],
            )
            self.assertEqual(pv.archive_config_digest(custom, LOCAL_TAG), config_digest)

    def test_rejects_archive_ambiguity_and_unexpected_state(self) -> None:
        config_bytes, config_digest = make_config_bytes()
        config_hex = config_digest.split(":", 1)[1]
        wrong_hex = "f" * 64

        with tempfile.TemporaryDirectory() as tmp:
            # Filename digest disagrees with actual config bytes.
            bad_name = Path(tmp) / "bad-name.tar"
            write_tar(
                bad_name,
                [
                    (
                        "manifest.json",
                        json.dumps(
                            [
                                {
                                    "Config": f"blobs/sha256/{wrong_hex}",
                                    "RepoTags": [LOCAL_TAG],
                                    "Layers": [],
                                }
                            ]
                        ).encode("utf-8"),
                    ),
                    (f"blobs/sha256/{wrong_hex}", config_bytes),
                ],
            )
            with self.assertRaisesRegex(ValueError, "actual bytes hash"):
                pv.archive_config_digest(bad_name, LOCAL_TAG)

            # Multiple images in manifest.json.
            multi = Path(tmp) / "multi.tar"
            write_tar(
                multi,
                [
                    (
                        "manifest.json",
                        json.dumps(
                            [
                                {"Config": f"{config_hex}.json", "RepoTags": [LOCAL_TAG]},
                                {"Config": f"{config_hex}.json", "RepoTags": ["other:1"]},
                            ]
                        ).encode("utf-8"),
                    ),
                    (f"{config_hex}.json", config_bytes),
                ],
            )
            with self.assertRaisesRegex(ValueError, "exactly one image"):
                pv.archive_config_digest(multi, LOCAL_TAG)

            # Extra or wrong RepoTags.
            bad_tags = Path(tmp) / "bad-tags.tar"
            write_tar(
                bad_tags,
                [
                    (
                        "manifest.json",
                        json.dumps(
                            [{"Config": f"{config_hex}.json", "RepoTags": [LOCAL_TAG, "ckbadger:latest"]}]
                        ).encode("utf-8"),
                    ),
                    (f"{config_hex}.json", config_bytes),
                ],
            )
            with self.assertRaisesRegex(ValueError, "RepoTags"):
                pv.archive_config_digest(bad_tags, LOCAL_TAG)

            # Multi-image index.json.
            multi_index = Path(tmp) / "multi-index.tar"
            write_tar(
                multi_index,
                [
                    (
                        "index.json",
                        json.dumps(
                            {
                                "schemaVersion": 2,
                                "manifests": [
                                    {"digest": "sha256:" + "3" * 64, "size": 10},
                                    {"digest": "sha256:" + "4" * 64, "size": 10},
                                ],
                            }
                        ).encode("utf-8"),
                    ),
                    (
                        "manifest.json",
                        json.dumps([{"Config": f"blobs/sha256/{config_hex}", "RepoTags": [LOCAL_TAG]}]).encode(
                            "utf-8"
                        ),
                    ),
                    (f"blobs/sha256/{config_hex}", config_bytes),
                ],
            )
            with self.assertRaisesRegex(ValueError, "index.json"):
                pv.archive_config_digest(multi_index, LOCAL_TAG)

            # Duplicate config tar member.
            dup = Path(tmp) / "dup.tar"
            write_tar(
                dup,
                [
                    (
                        "manifest.json",
                        json.dumps([{"Config": f"{config_hex}.json", "RepoTags": [LOCAL_TAG]}]).encode("utf-8"),
                    ),
                    (f"{config_hex}.json", config_bytes),
                    (f"./{config_hex}.json", config_bytes),
                ],
            )
            with self.assertRaisesRegex(ValueError, "duplicate tar entry"):
                pv.archive_config_digest(dup, LOCAL_TAG)

            # Symlink config tar member.
            sym = Path(tmp) / "sym.tar"
            write_tar(
                sym,
                [
                    (
                        "manifest.json",
                        json.dumps([{"Config": f"{config_hex}.json", "RepoTags": [LOCAL_TAG]}]).encode("utf-8"),
                    )
                ],
                symlinks=[(f"{config_hex}.json", "other.json")],
            )
            with self.assertRaisesRegex(ValueError, "regular file"):
                pv.archive_config_digest(sym, LOCAL_TAG)


class VerifyRemoteManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = "sha256:" + "a" * 64
        self.other = "sha256:" + "b" * 64
        self.child = "sha256:" + "c" * 64

    def _single(self, config_digest: str, media_type: str | None) -> str:
        doc: dict[str, object] = {
            "schemaVersion": 2,
            "config": {"digest": config_digest, "size": 512},
            "layers": [{"digest": "sha256:" + "1" * 64, "size": 1024}],
        }
        if media_type is not None:
            doc["mediaType"] = media_type
        return json.dumps(doc)

    def test_verifies_direct_manifest_and_single_amd64_index_child(self) -> None:
        for media_type in (
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            None,
        ):
            self.assertEqual(
                pv.verify_remote_manifest(
                    self._single(self.expected, media_type),
                    self.expected,
                    lambda _d: self.fail("should not fetch child for direct manifest"),
                ),
                self.expected,
            )

        index = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": self.child,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    },
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + "d" * 64,
                        "platform": {"os": "unknown", "architecture": "unknown"},
                        "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
                    },
                ],
            }
        )
        fetched: list[str] = []

        def fetch(digest: str) -> str:
            fetched.append(digest)
            return self._single(self.expected, "application/vnd.oci.image.manifest.v1+json")

        self.assertEqual(pv.verify_remote_manifest(index, self.expected, fetch), self.expected)
        self.assertEqual(fetched, [self.child])

    def test_rejects_wrong_digest_ambiguous_index_nested_index_and_unknown_type(self) -> None:
        # Wrong config digest.
        with self.assertRaisesRegex(ValueError, "!="):
            pv.verify_remote_manifest(
                self._single(self.other, "application/vnd.docker.distribution.manifest.v2+json"),
                self.expected,
                lambda _d: self.fail("unexpected child fetch"),
            )

        # Two linux/amd64 entries (ambiguous).
        ambiguous = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": self.child,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    },
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + "e" * 64,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    },
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "exactly 1"):
            pv.verify_remote_manifest(
                ambiguous, self.expected, lambda _d: self.fail("must not fetch ambiguous index")
            )

        # Extra non-amd64 platform.
        multi_arch = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                        "digest": self.child,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    },
                    {
                        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                        "digest": "sha256:" + "e" * 64,
                        "platform": {"os": "linux", "architecture": "arm64"},
                    },
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "unexpected platform"):
            pv.verify_remote_manifest(
                multi_arch, self.expected, lambda _d: self.fail("must not fetch multi-arch index")
            )

        # Nested index.
        single_index = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": self.child,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "nested image index"):
            pv.verify_remote_manifest(single_index, self.expected, lambda _d: single_index)

        # Unsupported mediaType.
        unsupported = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.example.unknown+json",
                "config": {"digest": self.expected},
                "layers": [{"digest": "sha256:" + "1" * 64}],
            }
        )
        with self.assertRaisesRegex(ValueError, "unsupported manifest mediaType"):
            pv.verify_remote_manifest(
                unsupported, self.expected, lambda _d: self.fail("unexpected child fetch")
            )

        # Missing schemaVersion even with declared mediaType.
        no_schema = json.dumps(
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": self.expected},
                "layers": [{"digest": "sha256:" + "1" * 64}],
            }
        )
        with self.assertRaisesRegex(ValueError, "schemaVersion"):
            pv.verify_remote_manifest(
                no_schema, self.expected, lambda _d: self.fail("unexpected child fetch")
            )

        # Declared direct manifest missing layers array.
        no_layers = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": self.expected},
            }
        )
        with self.assertRaisesRegex(ValueError, "layers"):
            pv.verify_remote_manifest(
                no_layers, self.expected, lambda _d: self.fail("unexpected child fetch")
            )

        # Declared index with contradictory layers field.
        index_with_layers = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "layers": [{"digest": "sha256:" + "1" * 64}],
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": self.child,
                        "platform": {"os": "linux", "architecture": "amd64"},
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "config or layers"):
            pv.verify_remote_manifest(
                index_with_layers, self.expected, lambda _d: self.fail("must not fetch contradictory index")
            )


class IsManifestNotFoundTests(unittest.TestCase):
    def test_accepts_only_explicit_not_found_for_exact_tag(self) -> None:
        for line in (
            f"ERROR: {REMOTE_TAG}: not found\n",
            f"ERROR: {REMOTE_TAG}: manifest unknown\n",
            f"ERROR: {REMOTE_TAG}: name unknown\n",
        ):
            self.assertTrue(pv.is_manifest_not_found(REMOTE_TAG, line), line)

        for err in (
            "",
            "ERROR: unauthorized: authentication required\n",
            "ERROR: denied: permission_denied\n",
            "ERROR: dial tcp 127.0.0.1:5000: connect: connection refused\n",
            "ERROR: 503 Service Unavailable: manifest unknown\n",
            "ERROR: ghcr.io/hanssen0/ckbadger:sha-bbbbbbbbbbbb: not found\n",
            f"WARNING: something\nERROR: {REMOTE_TAG}: not found\n",
        ):
            self.assertFalse(pv.is_manifest_not_found(REMOTE_TAG, err), err)


if __name__ == "__main__":
    unittest.main()
