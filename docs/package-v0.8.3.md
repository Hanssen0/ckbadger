# Package the CKBadger 0.8.3 branches

Every push to `0.8.3` or `hotfix/0.8.3` in `Hanssen0/ckbadger` triggers packaging, even
when the push changes only documentation. The build uses the exact tip
commit from that push, not the branch's current tip when the job starts.
A push containing several commits publishes one image for its tip commit.
Distinct SHA-12 tags use independent publish concurrency groups, and pushes for the same tag are serialized.

After the Linux amd64 image passes its version, embedded frontend, and
Docker shutdown tests, the workflow publishes it to
`ghcr.io/hanssen0/ckbadger:sha-<first-12-hex-of-commit-sha>`. It does not move
`latest` or `v0.8.3` or change the `main` branch. PRs into either release branch build
and test the proposed merge commit but never publish. Branch deletions
do not build or publish an image.

## Before merging

- If GitHub shows **Awaiting approval**, review the workflow changes, then
  choose **Approve workflows to run** on the PR. Wait for the packaging
  check to pass.
- Make sure the GHCR package gives `Hanssen0/ckbadger` **Write** access under
  **Package settings → Manage Actions access** (inherited access also works).
  Publishing uses the workflow's short-lived token, not a personal token.

After merging, wait for **Publish the tested push SHA to GHCR** to succeed.
Copy the first 12 characters of the commit SHA from the push run (or copy the image reference from
its summary):

```sh
sha="FIRST_12_HEX"  # Replace with the first 12 characters of the push run's SHA.
image="ghcr.io/hanssen0/ckbadger:sha-$sha"
docker pull "$image"
docker run --rm --network none "$image" --version
```

The binary reports `ckbadger 0.8.3@<abbreviated-commit-sha>`. The publish
job only uses the checksum-verified archive from the same push run and
verifies the image's platform, OCI source and revision, and remote image
configuration digest. If the SHA tag already exists, a matching digest is
reused; a different digest or unreadable tag fails without overwriting it.
Do not publish to the same SHA tag from outside this workflow.

## Artifact and local fallback

Download **ckbadger-v0.8.3-linux-amd64-attempt-N** from a successful PR or
push run (N is the workflow attempt; seven-day retention). Unzip it and run:

```sh
sha256sum -c ckbadger-v0.8.3-linux-amd64.tar.sha256
docker load -i ckbadger-v0.8.3-linux-amd64.tar
docker run --rm --network none ckbadger:0.8.3 --version
```

Locally, check out the commit you want to package in a Git checkout. The
script builds the committed files, recipe, and test fixtures from a clean
detached worktree at that commit, not from staged or uncommitted edits.
Use Ubuntu 24.04 x86_64 with Docker, Git, Python 3, Node.js 24, pnpm 9.15.0,
and stable Rust via rustup:

```sh
sudo apt-get install -y build-essential libclang-dev libssl-dev zlib1g-dev
bash scripts/package-v0.8.3.sh ./package-v0.8.3
```

The archive and checksum are written only after the tests pass. These
release branches do not track a Cargo lockfile, and the image uses an Ubuntu
base tag, so rebuilds of the same commit are not guaranteed to be
byte-identical.

Loading the image does not change a running deployment or migrate its
database. Do not mount layout-8 RocksDB stores into a `0.8.3` build that
does not support them. Use a separate CKBadger workdir with fresh stores
or a verified, compatible completed snapshot. Packaging alone does not
validate a testnet re-index or recover an incomplete bulk build.
