"""Strict read-only operator-intents mirror validation shared by consumers."""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path


MAX_AGE_SECONDS = 3600
METADATA = {".mirror-commit", ".mirror-manifest", ".mirror-synced-at"}


class MirrorValidationError(ValueError):
    """The selected mirror is absent, stale, writable, or internally inconsistent."""


def platform_default() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/Application Support/Bubble/operator-intents")
    return Path("/opt/bubble-operator-intents")


def validate_mirror(mirror_root: Path, *, now: datetime | None = None) -> Path:
    """Return the canonical release only after all immutable-mirror checks pass."""
    if not mirror_root.is_symlink():
        raise MirrorValidationError(f"mirror is not a stable symlink: {mirror_root}")
    link_info = mirror_root.lstat()
    if link_info.st_uid != 0 or link_info.st_gid != 0:
        raise MirrorValidationError(f"mirror symlink is not root-owned: {mirror_root}")
    try:
        release = mirror_root.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise MirrorValidationError(f"mirror symlink cannot be resolved: {exc}") from exc
    if not release.is_dir() or release.is_symlink():
        raise MirrorValidationError(f"mirror release is not a directory: {release}")
    release_info = release.lstat()
    if release_info.st_uid != 0 or release_info.st_gid != 0:
        raise MirrorValidationError("mirror release is not root-owned")
    if stat.S_IMODE(release_info.st_mode) != 0o555:
        raise MirrorValidationError("mirror release is not mode 0555")
    if release.parent.stat().st_uid != 0 or release.parent.stat().st_gid != 0:
        raise MirrorValidationError("mirror release parent is not root-owned")
    if stat.S_IMODE(release.parent.stat().st_mode) != 0o555:
        raise MirrorValidationError("mirror release parent is not mode 0555")

    manifest = release / ".mirror-manifest"
    commit_file = release / ".mirror-commit"
    synced_file = release / ".mirror-synced-at"
    intent_dir = release / "operator-intents"
    for required in (manifest, commit_file, synced_file, intent_dir):
        if not required.exists() or required.is_symlink():
            raise MirrorValidationError(f"required mirror object missing: {required}")
    commit = commit_file.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or release.name != commit:
        raise MirrorValidationError("release basename is not its 40-hex mirror commit")
    try:
        synced = datetime.fromisoformat(
            synced_file.read_text(encoding="utf-8").strip().replace("Z", "+00:00")
        )
    except (OSError, ValueError) as exc:
        raise MirrorValidationError(f"mirror sync timestamp is invalid: {exc}") from exc
    if synced.tzinfo is None:
        raise MirrorValidationError("mirror sync timestamp has no timezone")
    checked_at = now or datetime.now(timezone.utc)
    age = (checked_at.astimezone(timezone.utc) - synced.astimezone(timezone.utc)).total_seconds()
    if age < -300 or age > MAX_AGE_SECONDS:
        raise MirrorValidationError(f"mirror is stale (age={int(age)}s)")

    actual: list[str] = []
    for path in sorted(release.rglob("*"), key=lambda p: p.relative_to(release).as_posix()):
        relative = path.relative_to(release).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise MirrorValidationError(f"mirror content contains symlink: {relative}")
        if path.name == ".git":
            raise MirrorValidationError(f"mirror content contains .git: {relative}")
        if info.st_uid != 0 or info.st_gid != 0:
            raise MirrorValidationError(f"mirror object is not root-owned: {relative}")
        if stat.S_ISDIR(info.st_mode):
            if stat.S_IMODE(info.st_mode) != 0o555:
                raise MirrorValidationError(f"mirror directory is not 0555: {relative}")
            continue
        if not stat.S_ISREG(info.st_mode):
            raise MirrorValidationError(f"unsupported mirror object: {relative}")
        if stat.S_IMODE(info.st_mode) != 0o444:
            raise MirrorValidationError(f"mirror file is not 0444: {relative}")
        if path.name in METADATA and path.parent != release:
            raise MirrorValidationError(f"nested reserved metadata name: {relative}")
        if path not in {manifest, synced_file}:
            actual.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{relative}")
    expected = manifest.read_text(encoding="utf-8").splitlines()
    if actual != expected:
        raise MirrorValidationError("mirror manifest mismatch")
    return release


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the immutable operator-intents mirror.")
    parser.add_argument("mirror", nargs="?", type=Path, default=platform_default())
    parser.add_argument("--mode", choices=("compile", "synthesis", "pruning", "skillsmith"))
    args = parser.parse_args(argv)
    if args.mode == "skillsmith":
        return 0
    try:
        release = validate_mirror(args.mirror)
    except MirrorValidationError as exc:
        print(f"readonly-intents-mirror: FAIL: {exc}", file=sys.stderr)
        return 2
    print(f"readonly-intents-mirror: PASS release={release}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
