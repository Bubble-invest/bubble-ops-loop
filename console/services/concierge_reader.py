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
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional


# Known concierges. Kept explicit (small, stable set) rather than scanned,
# since concierge dirs are unprefixed and would be ambiguous to auto-detect.
CONCIERGES = ("morty", "claudette")

# Concierges have no dept.yaml (no layers, no onboarding — see module
# docstring), so there is nowhere to declare a per-dept `model:` field the
# way /dept/<slug> reads it from github_reader.load_agent_model_info(). This
# is a small, explicit, hardcoded map of the ACTUAL deployment fact per
# concierge (ground truth as of 2026-07-02, mapped by Rick):
#   - claudette: claude-agent-claudette.service ExecStart has NO --model
#     flag. Falls back to opus[1m] (its own settings.json + agent.md both
#     pin opus[1m]).
#   - morty: claude-agent-morty.service ExecStart runs run-deepseek-morty.sh
#     via a DeepSeek proxy — morty is NOT a Claude process at all.
# Update this map if a concierge's actual --model / backend changes.
_CONCIERGE_MODEL_INFO: dict = {
    "claudette": {"model": "opus[1m]", "runtime": "claude", "model_declared": False},
    "morty": {"model": "deepseek-v4-pro", "runtime": "deepseek", "model_declared": True},
}
_DEFAULT_CONCIERGE_MODEL_INFO = {
    "model": "opus[1m]", "runtime": "claude", "model_declared": False,
}


def concierge_model_info(name: str) -> dict:
    """Return {"model": str, "runtime": str, "model_declared": bool} for a
    concierge. Unknown names get the platform default — never raises."""
    return dict(_CONCIERGE_MODEL_INFO.get(name, _DEFAULT_CONCIERGE_MODEL_INFO))


@dataclass
class SessionTurn:
    """One human-readable turn from a session transcript.

    kind:
      - "message" → real prose the agent said (text content). ``text`` set.
      - "tool"    → a tool action. ``tool_name`` + ``detail`` set, ``text`` = "".
    Pure tool-result / empty turns are dropped upstream (mechanical noise).
    """
    ts: str
    role: str             # "assistant" | "user"
    kind: str = "message"  # "message" | "tool"
    text: str = ""         # prose (message kind)
    tool_name: str = ""    # tool kind
    detail: str = ""       # tool kind — a short useful detail (file/command/url)


@dataclass
class ConciergeSummary:
    name: str
    workspace: str
    session_jsonl: Optional[str] = None
    last_activity_iso: Optional[str] = None   # mtime of newest session file
    metadata: dict = field(default_factory=dict)


def _workspace(name: str, agents_root: str) -> str:
    return os.path.join(agents_root, name)


@dataclass
class ProjectCard:
    """One working project surfaced on a concierge's page ({{OPERATOR}} msg 1193).

    Read from <workspace>/workspace/projects/<slug>/STATUS.md — the same
    async status file the concierge keeps for the CEO."""
    slug: str
    title: str
    status_line: str        # one-line current state (the **État** line)
    url: str = ""           # first http(s) link in STATUS.md (live demo, etc.)
    has_status: bool = True


def _parse_status(path: str, slug: str) -> "ProjectCard":
    """Pull a title (first H1) + a one-line state from a STATUS.md."""
    title, state = slug, ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return ProjectCard(slug=slug, title=slug, status_line="", has_status=False)
    for ln in lines:
        s = ln.strip()
        if s.startswith("# ") and title == slug:
            title = s[2:].strip().removeprefix("STATUS —").removeprefix("STATUS -").strip() or slug
        # First bold **État…** line, or first bold line, is the state summary.
        if not state and s.startswith("**"):
            state = s.replace("*", "").strip()
    if not state:
        for ln in lines:
            s = ln.strip()
            if s and not s.startswith("#"):
                state = s
                break
    if len(state) > 160:
        state = state[:157] + "…"
    # First http(s) URL anywhere in the file → clickable link on the card
    # (e.g. a "**Démo :** https://…" line). Strip trailing markdown/punct.
    url = ""
    m = re.search(r"https?://[^\s<>)\]]+", "\n".join(lines))
    if m:
        url = m.group(0).rstrip(".,;)")
    return ProjectCard(slug=slug, title=title, status_line=state, url=url)


def list_projects(name: str, agents_root: str = "/home/claude/agents") -> List["ProjectCard"]:
    """Working projects for a concierge: each subdir of
    <workspace>/workspace/projects/ that has a STATUS.md. Newest first by
    STATUS.md mtime. [] if the concierge has no projects dir."""
    ws = _workspace(name, agents_root)
    proj_dir = os.path.join(ws, "workspace", "projects")
    if not os.path.isdir(proj_dir):
        return []
    cards: List[tuple] = []
    for entry in os.listdir(proj_dir):
        d = os.path.join(proj_dir, entry)
        status = os.path.join(d, "STATUS.md")
        if not (os.path.isdir(d) and os.path.isfile(status)):
            continue
        try:
            mtime = os.path.getmtime(status)
        except OSError:
            mtime = 0.0
        cards.append((mtime, _parse_status(status, entry)))
    cards.sort(key=lambda c: c[0], reverse=True)
    return [c for _, c in cards]


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


# Per-tool: pick the most informative single field for a compact detail line.
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


def _tool_detail(name: str, inp: Any) -> str:
    """A short, useful one-liner for a tool call (the file, command, url…)."""
    if not isinstance(inp, dict):
        return ""
    for key in _TOOL_DETAIL_KEYS.get(name, ()):
        v = inp.get(key)
        if v:
            s = str(v).strip().replace("\n", " ")
            # For file paths, the basename is what the operator cares about.
            if key.endswith("path") and "/" in s:
                s = s.rsplit("/", 1)[-1]
            return s[:120]
    # Fallback: first short string value, if any.
    for v in inp.values():
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\n", " ")[:120]
    return ""


def _classify(content: Any) -> list:
    """Turn a message 'content' into a list of (kind, payload) classified
    items. ``message`` → text str; ``tool`` → (name, detail). Pure
    tool_result items are dropped (mechanical noise)."""
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
        # tool_result → dropped (noise)
    return out


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
        ts = (obj.get("timestamp") or "")[:19]
        for kind, payload in _classify(msg.get("content")):
            if kind == "message":
                turns.append(SessionTurn(ts=ts, role=role,
                                         kind="message", text=payload))
            elif kind == "tool":
                name, detail = payload
                turns.append(SessionTurn(ts=ts, role=role, kind="tool",
                                         tool_name=name, detail=detail))
    return turns[-n:]
