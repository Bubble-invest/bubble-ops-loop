"""concierge_reader.py — read concierge (Morty, Claudette) state for the cockpit.

Concierges are NOT ops-loop departments: they run as `claude-agent-<name>`
(tenant bubble-internal), use UNPREFIXED workspace dirs (agents/<name>),
and have no dept.yaml / layers / queues / gates / heartbeat. They are
reactive assistants. So their cockpit page is a simpler "status + live
session activity" view, distinct from the dept gate/mission view.

This module is the read layer:
  - list_concierges()                  -> [ConciergeSummary]
  - get_concierge(name)                -> ConciergeSummary | None
  - read_recent_session(name, n=20)    -> [SessionTurn]  (the live view)

The "live session view" reads the most recent session transcript JSONL
(continuously appended by the running agent) and returns the last N
human-readable turns. The raw PTY is discarded to /dev/null, so the JSONL
IS the real-time activity stream.
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional


# Known concierges. Kept explicit (small, stable set) rather than scanned,
# since concierge dirs are unprefixed and would be ambiguous to auto-detect.
CONCIERGES = ("morty", "claudette")


@dataclass
class SessionTurn:
    """One human-readable turn from a session transcript."""
    ts: str
    role: str            # "assistant" | "user"
    text: str            # rendered text (tool calls summarized)


@dataclass
class ConciergeSummary:
    name: str
    workspace: str
    session_jsonl: Optional[str] = None
    last_activity_iso: Optional[str] = None   # mtime of newest session file
    metadata: dict = field(default_factory=dict)


def _workspace(name: str, agents_root: str) -> str:
    return os.path.join(agents_root, name)


def list_concierges(agents_root: str = "/home/claude/agents") -> List[ConciergeSummary]:
    out: List[ConciergeSummary] = []
    for name in CONCIERGES:
        ws = _workspace(name, agents_root)
        if not os.path.isdir(ws):
            continue
        out.append(_summary(name, ws))
    return out


def get_concierge(
    name: str, agents_root: str = "/home/claude/agents"
) -> Optional[ConciergeSummary]:
    if name not in CONCIERGES:
        return None
    ws = _workspace(name, agents_root)
    if not os.path.isdir(ws):
        return None
    return _summary(name, ws)


def _session_glob(name: str) -> str:
    # ~/.claude/projects/-home-claude-agents-<name>/*.jsonl
    home = os.path.expanduser("~")
    return os.path.join(
        home, ".claude", "projects", f"-home-claude-agents-{name}", "*.jsonl"
    )


def _newest_session(name: str) -> Optional[str]:
    files = glob.glob(_session_glob(name))
    if not files:
        return None
    return max(files, key=lambda p: os.path.getmtime(p))


def service_status(name: str) -> str:
    """Return systemd ActiveState for the concierge's claude-agent unit.

    One of "active" / "inactive" / "failed" / "unknown". Read-only, no
    sudo needed (`systemctl is-active` works for any user). Never raises."""
    import subprocess
    try:
        r = subprocess.run(
            ["systemctl", "is-active", f"claude-agent-{name}"],
            capture_output=True, text=True, timeout=5,
        )
        out = (r.stdout or "").strip()
        return out or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def _summary(name: str, ws: str) -> ConciergeSummary:
    import datetime as _dt
    sess = _newest_session(name)
    last_iso: Optional[str] = None
    if sess:
        try:
            last_iso = (
                _dt.datetime.fromtimestamp(os.path.getmtime(sess), _dt.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except OSError:
            last_iso = None
    return ConciergeSummary(
        name=name, workspace=ws, session_jsonl=sess, last_activity_iso=last_iso,
        metadata={"service_status": service_status(name)},
    )


def _render_content(content: Any) -> str:
    """Turn a message 'content' field into a short human-readable string.

    Tool calls are summarized as ``[tool: name]``; tool results as
    ``[result]``; text is passed through (trimmed)."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            parts.append((c.get("text") or "").strip())
        elif t == "tool_use":
            parts.append(f"[tool: {c.get('name', '?')}]")
        elif t == "tool_result":
            parts.append("[result]")
    return " ".join(p for p in parts if p).strip()


def read_recent_session(
    name: str, n: int = 20, agents_root: str = "/home/claude/agents"
) -> List[SessionTurn]:
    """Return the last ``n`` human-readable turns from the newest session.

    Empty list if the concierge is unknown or has no session yet. Skips
    turns that render to empty text (pure tool-result noise)."""
    if name not in CONCIERGES:
        return []
    sess = _newest_session(name)
    if not sess:
        return []
    turns: List[SessionTurn] = []
    try:
        with open(sess, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role not in ("assistant", "user"):
            continue
        text = _render_content(msg.get("content"))
        if not text:
            continue
        ts = (obj.get("timestamp") or "")[:19]
        turns.append(SessionTurn(ts=ts, role=role, text=text))
    return turns[-n:]
