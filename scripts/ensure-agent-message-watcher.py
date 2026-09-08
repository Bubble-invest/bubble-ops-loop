#!/usr/bin/env python3
"""Reapply an already-configured fixed-peer watcher after plugin cache refresh."""
from __future__ import annotations

import argparse
import grp
import importlib.util
import json
import os
import re
import pwd
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional


BEGIN = "// BUBBLE-AGENT-MESSAGE-WATCHER-v1"
END = "// END-BUBBLE-AGENT-MESSAGE-WATCHER-v1"
ANCHOR = "await mcp.connect(new StdioServerTransport())"
NAMES = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")


def owned_file(path: Path, *, allow_protected_root: bool = False) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid() and not (allow_protected_root and info.st_uid == 0)
            or info.st_nlink != 1
            or info.st_mode & 0o002
        ):
            raise ValueError(f"unsafe owned file: {path.name}")
        if info.st_mode & 0o020:
            # Plugin managers commonly extract server.ts as 0664. Accept that
            # only for the tenant's isolated primary group: no supplementary
            # member and no second passwd account may share the gid. Protected
            # root-owned packages remain forbidden from group-write entirely.
            if info.st_uid == 0 or info.st_gid != os.getegid():
                raise ValueError(f"unsafe owned file: {path.name}")
            username = pwd.getpwuid(os.geteuid()).pw_name
            group = grp.getgrgid(info.st_gid)
            primary_users = [p.pw_name for p in pwd.getpwall() if p.pw_gid == info.st_gid]
            if any(name != username for name in group.gr_mem + primary_users):
                raise ValueError(f"unsafe shared group file: {path.name}")
        if info.st_size > 128 * 1024:
            raise ValueError(f"oversized file: {path.name}")
        chunks = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def load_json(path: Path) -> dict:
    try:
        value = json.loads(owned_file(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid object: {path.name}")
    return value


def load_installer(path: Path):
    # Linux installs the reviewed package root-owned in /usr/local/lib and the
    # isolated tenant executes it after privilege drop. macOS keeps the package
    # user-owned. Both forms must be immutable to other accounts.
    owned_file(path, allow_protected_root=True)
    spec = importlib.util.spec_from_file_location("fixed_peer_watcher_installer", path)
    if not spec or not spec.loader:
        raise ValueError("cannot load canonical watcher installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "TEMPLATE") or not hasattr(module, "MARKER"):
        raise ValueError("invalid canonical watcher installer")
    return module


def expected_route(sender_config: dict, config_dir: Path, chat_id: str) -> dict:
    config_info = config_dir.lstat()
    if (
        not config_dir.is_dir()
        or config_dir.is_symlink()
        or config_info.st_uid != os.geteuid()
        or config_info.st_mode & 0o077
    ):
        raise ValueError("unsafe peer configuration directory")
    if set(sender_config) != {
        "self", "peer", "host", "user", "identity_file", "known_hosts_file"
    }:
        raise ValueError("invalid existing sender config schema")
    recipient = sender_config["self"]
    sender = sender_config["peer"]
    if not all(isinstance(v, str) for v in sender_config.values()):
        raise ValueError("invalid existing sender config values")
    if not NAMES.fullmatch(sender) or not NAMES.fullmatch(recipient) or sender == recipient:
        raise ValueError("invalid existing fixed identities")
    inbox = config_dir / "inbox"
    info = inbox.lstat()
    if (
        not inbox.is_dir()
        or inbox.is_symlink()
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("unsafe existing peer inbox")
    if not re.fullmatch(r"-?[0-9]+", chat_id):
        raise ValueError("invalid existing channel id")
    return {
        "inbox": str(inbox),
        "sender": sender,
        "recipient": recipient,
        "chat_id": chat_id,
    }


def route_from_server(source: str) -> Optional[dict]:
    if BEGIN not in source and END not in source:
        return None
    if source.count(BEGIN) != 1 or source.count(END) != 1:
        raise ValueError("malformed existing peer watcher markers")
    block = source[source.index(BEGIN): source.index(END) + len(END)]
    match = re.search(r"^\s*const route = (\{[^\n]+\})\s*$", block, re.MULTILINE)
    if not match:
        raise ValueError("existing peer watcher route missing")
    try:
        route = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError("existing peer watcher route invalid") from exc
    if not isinstance(route, dict) or set(route) != {"inbox", "sender", "recipient", "chat_id"}:
        raise ValueError("existing peer watcher route schema invalid")
    return route


def write_descriptor(path: Path, route: dict) -> None:
    directory = path.parent
    info = directory.lstat()
    if (
        not directory.is_dir()
        or directory.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("unsafe peer configuration directory")
    temp = f".{path.name}.{os.getpid()}.tmp"
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        try:
            body = (json.dumps(route, sort_keys=True, separators=(",", ":")) + "\n").encode()
            os.write(fd, body)
            os.fchmod(fd, 0o600)
            os.fsync(fd)
        finally:
            os.close(fd)
        # Publish only if the durable name is still absent. Never overwrite a
        # descriptor another process created after our initial read.
        os.link(
            temp,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temp, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temp, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def ensure(server: Path, sender_path: Path, descriptor_path: Path, installer: Path, dry_run: bool) -> str:
    sender_exists = sender_path.exists()
    installer_exists = installer.exists()
    if not sender_exists:
        return "skipped"
    if not installer_exists:
        raise ValueError("sender config exists but canonical watcher installer is missing")

    sender_config = load_json(sender_path)
    source = owned_file(server).decode()
    current = route_from_server(source)
    descriptor = load_json(descriptor_path) if descriptor_path.exists() else None
    if current is not None:
        route = expected_route(sender_config, sender_path.parent, str(current.get("chat_id", "")))
        if current != route:
            raise ValueError("existing peer watcher conflicts with sender config")
        if descriptor is not None and descriptor != route:
            raise ValueError("durable peer watcher descriptor differs")
    else:
        if descriptor is None:
            raise ValueError("peer watcher missing and no durable descriptor exists")
        route = expected_route(sender_config, sender_path.parent, str(descriptor.get("chat_id", "")))
        if descriptor != route:
            raise ValueError("invalid durable peer watcher descriptor")
        if source.count(ANCHOR) != 1:
            raise ValueError("plugin cache lacks one MCP connect anchor")

    canonical = load_installer(installer)
    patch = canonical.TEMPLATE.replace("__ROUTE__", json.dumps(route, ensure_ascii=True))
    if current is not None and patch.strip() not in source:
        raise ValueError("existing peer watcher differs from canonical installer")
    if dry_run:
        return "would-persist" if descriptor is None else ("verified" if current else "would-restore")

    if descriptor is None:
        write_descriptor(descriptor_path, route)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(installer),
            "--server", str(server),
            "--inbox", route["inbox"],
            "--sender", route["sender"],
            "--recipient", route["recipient"],
            "--chat-id", route["chat_id"],
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError("canonical peer watcher installer failed")
    return "verified" if current is not None else "restored"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, type=Path)
    parser.add_argument("--sender-config", required=True, type=Path)
    parser.add_argument("--watcher-config", required=True, type=Path)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        outcome = ensure(
            args.server, args.sender_config, args.watcher_config, args.installer, args.dry_run
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ensure-agent-message-watcher: {exc}", file=sys.stderr)
        return 1
    print(f"peer-watcher: {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
