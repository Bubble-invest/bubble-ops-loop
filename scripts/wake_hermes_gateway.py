#!/usr/bin/env python3
"""Wake one live Hermes gateway session through its persisted /loop API.

The gateway control socket proves the profile is live.  A one-shot LoopManager
row is then consumed by the gateway's own idle watcher and injected through its
already-connected platform adapter.  This process never starts an agent/model.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys
from typing import Any, Callable

import yaml


def _profile_home_chat(profile_home: Path) -> str:
    cfg = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8"))
    telegram = ((cfg or {}).get("platforms") or {}).get("telegram") or {}
    return str(telegram.get("home_chat_id") or telegram.get("home") or "").strip()


def wake_gateway_loop(
    profile_home: Path,
    hermes_root: Path,
    prompt: str,
    *,
    control_query: "Callable[[Path, str], dict[str, Any] | None] | None" = None,
    session_db_factory: "Callable[[], Any] | None" = None,
    loop_factory: "Callable[[str], Any] | None" = None,
) -> int:
    profile_home = profile_home.expanduser()
    try:
        info = profile_home.lstat()
    except OSError:
        print("Hermes profile unavailable", file=sys.stderr)
        return 2
    if profile_home.is_symlink() or not stat.S_ISDIR(info.st_mode):
        print("unsafe Hermes profile", file=sys.stderr)
        return 2
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        print("Hermes profile is not private to this service user", file=sys.stderr)
        return 2
    if not prompt.strip() or len(prompt.encode("utf-8")) > 64 * 1024:
        print("invalid Hermes wake prompt", file=sys.stderr)
        return 2

    os.environ["HERMES_HOME"] = str(profile_home)
    if control_query is None or session_db_factory is None or loop_factory is None:
        sys.path.insert(0, str(hermes_root))
        from gateway.control_socket import query_gateway_control
        from hermes_cli.loops import LoopManager
        from hermes_state import SessionDB

        control_query = query_gateway_control
        session_db_factory = SessionDB
        loop_factory = LoopManager

    identity = control_query(profile_home, "identify") or {}
    runtime = control_query(profile_home, "status") or {}
    identity_home_raw = str(identity.get("hermes_home") or "")
    identity_home = Path(identity_home_raw)
    if (
        not identity.get("pid")
        or not identity_home_raw
        or identity_home.resolve(strict=False) != profile_home.resolve(strict=False)
        or runtime.get("gateway_state") != "running"
        or (
            runtime.get("answering_pid") is not None
            and runtime.get("answering_pid") != identity.get("pid")
        )
    ):
        print("Hermes gateway control socket is not live", file=sys.stderr)
        return 3

    home_chat = _profile_home_chat(profile_home)
    if not home_chat:
        print("Hermes Telegram home chat is not configured", file=sys.stderr)
        return 3
    rows = session_db_factory().list_sessions_rich(
        source="telegram",
        limit=100,
        order_by_last_active=True,
        compact_rows=True,
    )
    matches = [
        row for row in rows
        if str(row.get("chat_id") or "") == home_chat
        and row.get("session_key")
        and not row.get("ended_at")
    ]
    if len(matches) != 1:
        print("Hermes home-chat session is missing or ambiguous", file=sys.stderr)
        return 3

    row = matches[0]
    manager = loop_factory(str(row["id"]))
    state = manager.state
    if state is not None and state.status == "paused":
        print("Hermes loop is operator-paused", file=sys.stderr)
        return 4
    if state is not None and state.awaiting_response:
        print("Hermes loop wake already running", file=sys.stderr)
        return 4
    if state is not None and state.status == "active":
        if manager.resume() is None:
            print("Hermes loop re-arm failed", file=sys.stderr)
            return 4
        print("Hermes gateway loop re-armed")
        return 0

    route = {
        "platform": "telegram",
        "chat_id": str(row.get("chat_id") or ""),
        "chat_type": str(row.get("chat_type") or ""),
        "thread_id": str(row.get("thread_id") or ""),
        "user_id": str(row.get("user_id") or ""),
        "user_name": str(row.get("user_name") or ""),
    }
    route = {key: value for key, value in route.items() if value}
    manager.set(prompt.strip(), times=1, route=route)
    print("Hermes gateway one-shot loop armed")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-home", required=True, type=Path)
    parser.add_argument(
        "--hermes-root", type=Path, default=Path("/opt/hermes/hermes-agent")
    )
    args = parser.parse_args(argv)
    return wake_gateway_loop(args.profile_home, args.hermes_root, sys.stdin.read())


if __name__ == "__main__":
    raise SystemExit(main())
