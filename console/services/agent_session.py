"""agent_session.py — read an agent's live session transcript.

Shared by the concierge pages and the dept pages. An agent's raw terminal
output is discarded (`script -qfc … /dev/null`), so the JSONL transcript
at ~/.claude/projects/-home-claude-agents-<dir>/ IS the real-time
activity stream.

The session DIR is not perfectly predictable from the slug: depts run
from a prefixed workdir (bubble-ops-<slug>) but the session dir can be
either the prefixed or unprefixed form depending on how the cwd resolved
(symlink). So we match by a set of candidate dir-suffixes and pick the
one with the newest transcript.
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class SessionTurn:
    """One classified turn from a session transcript.

    kind == "message" → prose (text). kind == "tool" → tool_name + detail.
    """
    ts: str
    role: str
    kind: str = "message"
    text: str = ""
    tool_name: str = ""
    detail: str = ""


_TOOL_DETAIL_KEYS = {
    "Bash": ("command",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "Read": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("pattern",),
    "Grep": ("pattern",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
}


def _tool_detail(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in _TOOL_DETAIL_KEYS.get(name, ()):
        v = inp.get(key)
        if v:
            s = str(v).strip().replace("\n", " ")
            if key.endswith("path") and "/" in s:
                s = s.rsplit("/", 1)[-1]
            return s[:120]
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\n", " ")[:120]
    return ""


def _classify(content) -> list:
    if isinstance(content, str):
        s = content.strip()
        return [("message", s)] if s else []
    if not isinstance(content, list):
        return []
    out: list = []
    for c in content:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            s = (c.get("text") or "").strip()
            if s:
                out.append(("message", s))
        elif t == "tool_use":
            name = c.get("name", "?")
            out.append(("tool", (name, _tool_detail(name, c.get("input")))))
    return out


def newest_session_file(dir_suffixes: List[str]) -> Optional[str]:
    """Return the newest *.jsonl across the candidate session dirs.

    ``dir_suffixes`` are the trailing dir names to try, e.g.
    ``["maya", "bubble-ops-maya"]``. Returns the single newest transcript
    across all of them, or None."""
    home = os.path.expanduser("~")
    files: List[str] = []
    for suffix in dir_suffixes:
        files.extend(glob.glob(
            os.path.join(home, ".claude", "projects",
                         f"-home-claude-agents-{suffix}", "*.jsonl")
        ))
    if not files:
        return None
    return max(files, key=lambda p: os.path.getmtime(p))


def read_session_turns(session_file: Optional[str], n: int = 30) -> List[SessionTurn]:
    """Return the last ``n`` classified turns from a transcript file."""
    if not session_file:
        return []
    try:
        with open(session_file, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    turns: List[SessionTurn] = []
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
        ts = (obj.get("timestamp") or "")[:19]
        for kind, payload in _classify(msg.get("content")):
            if kind == "message":
                turns.append(SessionTurn(ts=ts, role=role, kind="message", text=payload))
            else:
                name, detail = payload
                turns.append(SessionTurn(ts=ts, role=role, kind="tool",
                                         tool_name=name, detail=detail))
    return turns[-n:]


def newest_session_mtime_iso(dir_suffixes: List[str]) -> Optional[str]:
    """ISO-8601 UTC of the newest transcript across candidate dirs, or None."""
    import datetime as _dt
    f = newest_session_file(dir_suffixes)
    if not f:
        return None
    try:
        return (_dt.datetime.fromtimestamp(os.path.getmtime(f), _dt.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"))
    except OSError:
        return None
