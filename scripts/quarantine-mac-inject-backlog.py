#!/usr/bin/env python3
"""Hold an unknown Mac maintenance-inject backlog without reading or replaying it."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path


SLUG = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")


def fail(message: str) -> "None":
    raise ValueError(message)


def open_state(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        fail("state directory must be absolute without traversal")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            fail("state directory must be owned and not writable by peers")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(directory: int, name: str) -> tuple[bytes, os.stat_result]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
        ):
            fail("inject backlog is not one safe owned regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def quarantine(state: Path, slug: str, expected_sha: str, expected_lines: int, apply: bool) -> dict:
    if not SLUG.fullmatch(slug) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        fail("invalid slug or expected hash")
    state_fd = open_state(state)
    held_fd = None
    try:
        body, before = read_regular(state_fd, "inject")
        digest = hashlib.sha256(body).hexdigest()
        lines = len(body.splitlines())
        if digest != expected_sha or lines != expected_lines:
            fail("inject backlog changed; refusing stale quarantine plan")
        result = {"bytes": len(body), "lines": lines, "sha256": digest, "applied": False}
        if not apply:
            return result
        try:
            os.mkdir(".held-maintenance-inject", 0o700, dir_fd=state_fd)
        except FileExistsError:
            pass
        held_fd = os.open(
            ".held-maintenance-inject",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=state_fd,
        )
        held_info = os.fstat(held_fd)
        if held_info.st_uid != os.geteuid() or stat.S_IMODE(held_info.st_mode) != 0o700:
            fail("unsafe held-backlog directory")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        held_name = f"inject-{slug}-{stamp}-{digest[:12]}.held"
        try:
            os.stat(held_name, dir_fd=held_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            fail("held backlog destination already exists")
        os.rename("inject", held_name, src_dir_fd=state_fd, dst_dir_fd=held_fd)
        try:
            new_fd = os.open(
                "inject",
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o600,
                dir_fd=state_fd,
            )
            os.fchmod(new_fd, 0o600)
            os.fsync(new_fd)
            os.close(new_fd)
        except BaseException:
            # Do not overwrite a queue another writer created in the small
            # post-rename window. The original backlog remains held exactly;
            # restore it only when the live name is still absent.
            try:
                os.stat("inject", dir_fd=state_fd, follow_symlinks=False)
            except FileNotFoundError:
                os.rename(held_name, "inject", src_dir_fd=held_fd, dst_dir_fd=state_fd)
            raise
        held_body, held_stat = read_regular(held_fd, held_name)
        if held_stat.st_ino != before.st_ino or hashlib.sha256(held_body).hexdigest() != expected_sha:
            fresh, _ = read_regular(state_fd, "inject")
            if fresh == b"":
                os.unlink("inject", dir_fd=state_fd)
                os.rename(held_name, "inject", src_dir_fd=held_fd, dst_dir_fd=state_fd)
            fail("backlog changed during quarantine; restored when safe")
        os.fsync(held_fd)
        os.fsync(state_fd)
        result["applied"] = True
        result["held_name"] = held_name
        return result
    finally:
        if held_fd is not None:
            os.close(held_fd)
        os.close(state_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-lines", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = quarantine(
            Path(args.state_dir), args.slug, args.expected_sha256,
            args.expected_lines, args.apply,
        )
    except (OSError, ValueError) as exc:
        print(f"quarantine-mac-inject-backlog: {exc}", file=os.sys.stderr)
        return 1
    print(
        f"inject backlog metadata: bytes={result['bytes']} lines={result['lines']} "
        f"sha256={result['sha256']}"
    )
    if result["applied"]:
        print(f"held without replay: {result['held_name']}")
    else:
        print("dry-run: backlog unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
