#!/usr/bin/env bash
set -euo pipefail

readonly source_url=https://github.com/Hanssen0/ckbadger
readonly image=ckbadger:0.8.3
readonly tarball=ckbadger-v0.8.3-linux-amd64.tar
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ $# = 1 ]] || fail "Usage: $0 OUTPUT_DIRECTORY"
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] ||
    fail 'Use a Linux x86_64 build host (Ubuntu 24.04 recommended).'
for dependency in git rustup node pnpm docker python3 sha256sum; do
    command -v "$dependency" >/dev/null || fail "Missing dependency: $dependency"
done
[[ $(node -p 'process.versions.node.split(".")[0]') == 24 ]] || fail 'Use Node.js 24.'
[[ $(pnpm --version) == 9.15.0 ]] || fail 'Use pnpm 9.15.0.'
mkdir -p -- "$1"
output="$(cd -- "$1" && pwd -P)"
[[ ! -e "$output/$tarball" && ! -e "$output/$tarball.sha256" ]] ||
    fail 'Remove or move the previous archive and checksum first.'

# Use only this checkout's committed tree. Detach HEAD so the CLI's build.rs
# keeps the real commit ID without adding a branch name to --version.
revision="$(git -C "$repo" rev-parse --verify HEAD)"
scratch="$(mktemp -d)"
source="$scratch/source"
cleanup() {
    if [[ -f "$source/.git" ]]; then
        git -C "$repo" worktree remove --force "$source" || true
    fi
    rm -rf -- "$scratch"
}
trap cleanup EXIT
git -C "$repo" -c core.hooksPath=/dev/null worktree add --quiet --detach "$source" "$revision"
recipe_dir="$source/packaging/ckbadger-v0.8.3"
short_revision="$(git -C "$source" rev-parse --short=12 HEAD)"

(
    cd -- "$source/frontend"
    pnpm install --frozen-lockfile
    pnpm build
)
# The 0.8.3 branch does not track Cargo.lock.
(
    cd -- "$source"
    CARGO_TARGET_DIR="$scratch/target" rustup run stable cargo build --release -j 2 -p ckbadger
)

context="$scratch/docker"
mkdir -- "$context"
install -m 0755 "$scratch/target/release/ckbadger" "$context/ckbadger"
docker build --pull --platform linux/amd64 --file "$recipe_dir/Dockerfile" --tag "$image" \
    --label "org.opencontainers.image.source=$source_url" \
    --label "org.opencontainers.image.revision=$revision" \
    --label 'org.opencontainers.image.version=0.8.3' "$context"

# Check the actual packaged executable, not just the image tag or OCI labels.
actual="$(docker run --rm --network none "$image" --version)"
expected="ckbadger 0.8.3@$short_revision"
[[ $actual == "$expected" ]] || fail "Expected $expected, got: $actual"
printf '%s\n' "$actual"

# Run the checked-out revision's loopback frontend and container shutdown
# fixtures; neither needs a node or chain data.
launcher="$scratch/run-packaged-frontend"
cat > "$launcher" <<'SH'
#!/bin/sh
set -eu
exec docker run --rm --init --network host --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$PWD,dst=$PWD" ckbadger:0.8.3 "$@"
SH
chmod +x "$launcher"
python3 "$source/scripts/test_frontend_release.py" --binary "$launcher"
bash "$recipe_dir/test_docker_shutdown.sh" "$image"

(
    cd -- "$scratch"
    docker save --output "$tarball" "$image"
    sha256sum "$tarball" > "$tarball.sha256"
    sha256sum --check "$tarball.sha256"
)
mv -- "$scratch/$tarball" "$scratch/$tarball.sha256" "$output/"
printf 'Image archive: %s/%s\n' "$output" "$tarball"
