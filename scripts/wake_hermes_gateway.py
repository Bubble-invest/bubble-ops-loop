#!/usr/bin/env python3
"""Wake one live Hermes session through its persisted /loop scheduler.

The gateway control socket only proves that the exact profile is live. A
validated active LoopManager row is re-armed, or a one-shot rescue row is
created for the exact Telegram session route. The gateway's idle watcher later
consumes that persisted state through its normal platform adapter. This process
never starts an agent/model and queue acceptance is not execution proof.
"""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any, Callable, Optional

import yaml


def _profile_home_chat(profile_home: Path) -> str:
    cfg = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8"))
    telegram = ((cfg or {}).get("platforms") or {}).get("telegram") or {}
    return str(telegram.get("home_chat_id") or telegram.get("home") or "").strip()


def _session_route(row: dict[str, Any]) -> dict[str, str]:
    """Build the exact gateway route for the selected Telegram session."""
    route = {
        "platform": "telegram",
        "chat_id": str(row.get("chat_id") or ""),
        "chat_type": str(row.get("chat_type") or ""),
        "thread_id": str(row.get("thread_id") or ""),
        "user_id": str(row.get("user_id") or ""),
        "user_name": str(row.get("user_name") or ""),
    }
    return {key: value for key, value in route.items() if value}


def _normalized_route(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if item is not None and str(item)
    }


def _verify_persisted_wake(
    manager: Any,
    *,
    route: dict[str, str],
    prompt: str,
    require_one_shot: bool,
    previous_next_due_at: Optional[float] = None,
) -> bool:
    """Re-read persisted loop state and prove the intended wake is armed.

    LoopManager's save methods deliberately swallow persistence errors, so a
    successful ``set``/``resume`` return is not sufficient. ``refresh`` is the
    cross-process DB read and must show the exact route/prompt plus a near-term
    due time before this helper can return zero.
    """
    try:
        manager.refresh()
        state = manager.state
        due = float(getattr(state, "next_due_at", 0.0) or 0.0)
    except Exception:
        return False
    if state is None or getattr(state, "status", None) != "active":
        return False
    if bool(getattr(state, "awaiting_response", False)):
        return False
    if _normalized_route(getattr(state, "route", None)) != route:
        return False
    if str(getattr(state, "prompt", "")) != prompt:
        return False
    if require_one_shot and int(getattr(state, "times", 0) or 0) != 1:
        return False
    if not math.isfinite(due) or due <= 0 or due > time.time() + 10.0:
        return False
    if previous_next_due_at is not None and due == previous_next_due_at:
        # resume() always re-arms relative to now. Equality means its in-memory
        # mutation was lost (the swallowed-write failure this readback catches).
        return False
    return True


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
    route = _session_route(row)
    manager = loop_factory(str(row["id"]))
    state = manager.state
    if state is not None and getattr(state, "status", None) == "paused":
        print("Hermes loop is operator-paused", file=sys.stderr)
        return 4
    if state is not None and bool(getattr(state, "awaiting_response", False)):
        print("Hermes loop wake already running", file=sys.stderr)
        return 4

    existing_route = _normalized_route(getattr(state, "route", None))
    existing_prompt = str(getattr(state, "prompt", ""))
    if (
        state is not None
        and getattr(state, "status", None) == "active"
        and existing_route == route
        and existing_prompt.strip()
    ):
        try:
            previous_due = float(getattr(state, "next_due_at", 0.0) or 0.0)
            resumed = manager.resume()
        except Exception:
            resumed = None
        if resumed is None or not _verify_persisted_wake(
            manager,
            route=route,
            prompt=existing_prompt,
            require_one_shot=False,
            previous_next_due_at=previous_due,
        ):
            print("Hermes loop re-arm failed", file=sys.stderr)
            return 4
        print("Hermes active loop validated and re-armed")
        return 0

    rescue_prompt = prompt.strip()
    try:
        created = manager.set(rescue_prompt, times=1, route=route)
    except Exception:
        created = None
    if created is None or not _verify_persisted_wake(
        manager,
        route=route,
        prompt=rescue_prompt,
        require_one_shot=True,
    ):
        print("Hermes one-shot rescue persistence failed", file=sys.stderr)
        return 4
    print("Hermes one-shot rescue validated and armed")
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
