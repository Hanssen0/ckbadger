#!/usr/bin/env python3
"""Verify CKBadger 0.8.3 docker-save config digest and remote manifest.

Standard-library helper invoked by `.github/workflows/package-v0.8.3.yml`:
- `config-digest`: hashes the actual Config regular member bytes selected by
  the single `manifest.json` entry for the expected RepoTag (no extraction, no
  guessed filenames, no reliance on `docker image inspect .Id`).
- `verify-manifest`: checks `config.digest` on a direct OCI/Docker v2 manifest
  or resolves a single `linux/amd64` child manifest from an index by digest.
- `is-not-found`: positively matches explicit manifest-not-found output for the
  exact remote tag so auth, transport, or unknown errors fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tarfile
from typing import Callable, Mapping, Sequence


SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONFIG_DIGEST_PATH_RE = re.compile(r"^(?:blobs/sha256/([0-9a-f]{64})|([0-9a-f]{64})\.json)$")

SINGLE_MANIFEST_TYPES = frozenset(
    {
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    }
)
INDEX_MANIFEST_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)


def _norm_tar_path(raw: str) -> str:
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise ValueError(f"invalid tar member path: {raw!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise ValueError(f"unsafe tar member path: {raw!r}")
    norm = posixpath.normpath("/".join(parts))
    if norm in ("", ".") or norm.startswith(("/", "..")):
        raise ValueError(f"unsafe normalized tar member path: {raw!r}")
    return norm


def archive_config_digest(archive_path: str | Path, repo_tag: str = "ckbadger:0.8.3") -> str:
    """Return `sha256:<hex>` of the selected image's Config regular member bytes."""
    try:
        tar_ctx = tarfile.open(archive_path, mode="r:")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f"cannot open tar archive {archive_path}: {exc}") from exc

    with tar_ctx as tar:
        members: dict[str, tarfile.TarInfo] = {}
        duplicates: set[str] = set()
        for member in tar.getmembers():
            if member.isdir():
                continue
            norm = _norm_tar_path(member.name)
            if norm in members:
                duplicates.add(norm)
            else:
                members[norm] = member

        def read_regular(raw_path: str, label: str) -> tuple[str, bytes]:
            norm = _norm_tar_path(raw_path)
            if norm in duplicates:
                raise ValueError(f"duplicate tar entry for {label}: {norm!r}")
            info = members.get(norm)
            if info is None:
                raise ValueError(f"missing {label} in archive: {norm!r}")
            if not info.isfile():
                raise ValueError(f"{label} must be a regular file: {norm!r}")
            stream = tar.extractfile(info)
            if stream is None:
                raise ValueError(f"cannot read {label} from archive: {norm!r}")
            return norm, stream.read()

        _, manifest_raw = read_regular("manifest.json", "manifest.json")
        try:
            manifest_doc = json.loads(manifest_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid manifest.json: {exc}") from exc

        if not isinstance(manifest_doc, list) or len(manifest_doc) != 1:
            raise ValueError("manifest.json must contain exactly one image entry")
        entry = manifest_doc[0]
        if not isinstance(entry, dict):
            raise ValueError("manifest.json entry must be an object")
        if entry.get("RepoTags") != [repo_tag]:
            raise ValueError(f"RepoTags must be [{repo_tag!r}], got {entry.get('RepoTags')!r}")

        raw_config = entry.get("Config")
        if not isinstance(raw_config, str) or not raw_config:
            raise ValueError(f"manifest.json missing valid Config path: {raw_config!r}")

        if "index.json" in members or "index.json" in duplicates:
            _, index_raw = read_regular("index.json", "index.json")
            try:
                index_doc = json.loads(index_raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid index.json: {exc}") from exc
            manifests = index_doc.get("manifests") if isinstance(index_doc, dict) else None
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise ValueError("index.json must contain exactly one descriptor")

        config_path, config_bytes = read_regular(raw_config, "image config")

    try:
        config_doc = json.loads(config_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid image config JSON at {config_path!r}: {exc}") from exc
    if not isinstance(config_doc, dict):
        raise ValueError(f"image config JSON at {config_path!r} must be an object")

    digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    path_match = CONFIG_DIGEST_PATH_RE.match(config_path)
    if path_match:
        declared = "sha256:" + (path_match.group(1) or path_match.group(2))
        if declared != digest:
            raise ValueError(
                f"config path {config_path!r} implies {declared}, actual bytes hash to {digest}"
            )
    return digest


def _is_attestation(desc: Mapping[str, object]) -> bool:
    annotations = desc.get("annotations")
    platform = desc.get("platform")
    return (
        isinstance(annotations, dict)
        and annotations.get("vnd.docker.reference.type") == "attestation-manifest"
        and isinstance(platform, dict)
        and platform.get("os") == "unknown"
        and platform.get("architecture") == "unknown"
    )


def _manifest_kind(doc: Mapping[str, object]) -> str:
    schema_version = doc.get("schemaVersion")
    if type(schema_version) is not int or schema_version != 2:
        raise ValueError(f"unsupported manifest schemaVersion: {schema_version!r}")

    media_type = doc.get("mediaType")
    if media_type is not None:
        if media_type in SINGLE_MANIFEST_TYPES:
            return "single"
        if media_type in INDEX_MANIFEST_TYPES:
            return "index"
        raise ValueError(f"unsupported manifest mediaType: {media_type!r}")

    if "config" in doc and "layers" in doc and "manifests" not in doc:
        return "single"
    if "manifests" in doc and "config" not in doc and "layers" not in doc:
        return "index"
    raise ValueError("ambiguous or unsupported manifest shape")


def verify_remote_manifest(
    raw_manifest: str,
    expected_config_digest: str,
    fetch_by_digest: Callable[[str], str],
    *,
    allow_index: bool = True,
) -> str:
    """Verify `config.digest` directly or via a single `linux/amd64` index child."""
    if not SHA256_RE.match(expected_config_digest):
        raise ValueError(f"invalid expected config digest: {expected_config_digest!r}")

    try:
        doc = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        raise ValueError(f"remote manifest is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError("remote manifest JSON must be an object")

    if _manifest_kind(doc) == "single":
        if "manifests" in doc:
            raise ValueError("single image manifest must not contain manifests list")
        config = doc.get("config")
        digest = config.get("digest") if isinstance(config, dict) else None
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            raise ValueError(f"remote manifest has invalid config.digest: {digest!r}")
        layers = doc.get("layers")
        if not isinstance(layers, list) or not layers:
            raise ValueError("remote manifest must contain a non-empty layers list")
        for layer in layers:
            layer_digest = layer.get("digest") if isinstance(layer, dict) else None
            if not isinstance(layer_digest, str) or not SHA256_RE.match(layer_digest):
                raise ValueError(f"remote manifest layer has invalid digest: {layer_digest!r}")
        if digest != expected_config_digest:
            raise ValueError(
                f"remote manifest config.digest {digest} != expected {expected_config_digest}"
            )
        return digest

    if not allow_index:
        raise ValueError("nested image index is not supported")
    if "config" in doc or "layers" in doc:
        raise ValueError("image index must not contain top-level config or layers")

    manifests = doc.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise ValueError("image index must contain a non-empty manifests list")

    matches: list[dict[str, object]] = []
    for desc in manifests:
        if not isinstance(desc, dict):
            raise ValueError("image index descriptor must be an object")
        digest = desc.get("digest")
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            raise ValueError(f"image index descriptor has invalid digest: {digest!r}")
        if _is_attestation(desc):
            continue
        platform = desc.get("platform")
        if not isinstance(platform, dict):
            raise ValueError(f"image index descriptor {digest} missing platform metadata")
        os_name, arch = platform.get("os"), platform.get("architecture")
        if not isinstance(os_name, str) or not os_name or not isinstance(arch, str) or not arch:
            raise ValueError(f"image index descriptor {digest} has incomplete platform metadata")
        if (os_name, arch) != ("linux", "amd64"):
            raise ValueError(f"image index contains unexpected platform {os_name}/{arch}")
        matches.append(desc)

    if len(matches) != 1:
        raise ValueError(f"image index must contain exactly 1 linux/amd64 manifest, found {len(matches)}")

    child_media_type = matches[0].get("mediaType")
    if child_media_type is not None and child_media_type not in SINGLE_MANIFEST_TYPES:
        raise ValueError(f"selected child descriptor has unsupported mediaType: {child_media_type!r}")

    child_digest = matches[0]["digest"]
    assert isinstance(child_digest, str)
    return verify_remote_manifest(
        fetch_by_digest(child_digest),
        expected_config_digest,
        fetch_by_digest,
        allow_index=False,
    )


def fetch_manifest_by_digest(repo: str, digest: str) -> str:
    ref = f"{repo}@{digest}"
    proc = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ValueError(f"failed to fetch child manifest {ref}: {detail}")
    return proc.stdout


def is_manifest_not_found(remote_tag: str, stderr: str) -> bool:
    """Return True only for an explicit single-line manifest-not-found for `remote_tag`."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return len(lines) == 1 and lines[0] in {
        f"ERROR: {remote_tag}: not found",
        f"ERROR: {remote_tag}: manifest unknown",
        f"ERROR: {remote_tag}: name unknown",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    cfg = sub.add_parser("config-digest")
    cfg.add_argument("archive")
    cfg.add_argument("--repo-tag", default="ckbadger:0.8.3")

    ver = sub.add_parser("verify-manifest")
    ver.add_argument("expected_config_digest")
    ver.add_argument("remote_repo")

    nf = sub.add_parser("is-not-found")
    nf.add_argument("remote_tag")

    args = parser.parse_args(argv)
    try:
        if args.command == "config-digest":
            print(archive_config_digest(args.archive, args.repo_tag))
            return 0
        if args.command == "verify-manifest":
            verify_remote_manifest(
                sys.stdin.read(),
                args.expected_config_digest,
                lambda digest: fetch_manifest_by_digest(args.remote_repo, digest),
            )
            return 0
        if args.command == "is-not-found":
            return 0 if is_manifest_not_found(args.remote_tag, sys.stdin.read()) else 1
        raise ValueError(f"unknown command: {args.command}")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
