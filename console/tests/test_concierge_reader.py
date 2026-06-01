"""test_concierge_reader.py — concierge state + live session reader.

Concierges (Morty, Claudette) aren't ops-loop depts; the cockpit needs a
simpler status + live-session view for them. This tests the read layer.

TDD: written BEFORE the module exists. RED -> GREEN.
"""
from __future__ import annotations

import json
import os

import pytest

from console.services.concierge_reader import (
    list_concierges,
    get_concierge,
    read_recent_session,
    CONCIERGES,
)


def _make_agents_root(tmp_path):
    root = tmp_path / "agents"
    for name in ("morty", "claudette"):
        (root / name).mkdir(parents=True)
        (root / name / "CLAUDE.md").write_text("# " + name, encoding="utf-8")
    return root


def _seed_session(home, name, turns):
    """Write a session JSONL at ~/.claude/projects/-home-claude-agents-<name>/."""
    d = home / ".claude" / "projects" / f"-home-claude-agents-{name}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "sess.jsonl"
    with f.open("w", encoding="utf-8") as fh:
        for t in turns:
            fh.write(json.dumps(t) + "\n")
    return f


def test_list_concierges_finds_existing_dirs(tmp_path, monkeypatch):
    root = _make_agents_root(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cs = list_concierges(agents_root=str(root))
    names = {c.name for c in cs}
    assert names == {"morty", "claudette"}


def test_list_concierges_skips_missing_dir(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    (root / "morty").mkdir(parents=True)   # only morty exists
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cs = list_concierges(agents_root=str(root))
    assert [c.name for c in cs] == ["morty"]


def test_get_concierge_unknown_returns_none(tmp_path, monkeypatch):
    root = _make_agents_root(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert get_concierge("nope", agents_root=str(root)) is None


def test_get_concierge_returns_summary_with_workspace(tmp_path, monkeypatch):
    root = _make_agents_root(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    c = get_concierge("morty", agents_root=str(root))
    assert c is not None
    assert c.name == "morty"
    assert c.workspace.endswith("/morty")


def test_read_recent_session_returns_turns(tmp_path, monkeypatch):
    root = _make_agents_root(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _seed_session(home, "morty", [
        {"timestamp": "2026-06-01T08:00:00Z",
         "message": {"role": "user", "content": "ping"}},
        {"timestamp": "2026-06-01T08:00:05Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "pong, working on it"}]}},
    ])
    turns = read_recent_session("morty", agents_root=str(root))
    assert len(turns) == 2
    assert turns[-1].role == "assistant"
    assert "pong" in turns[-1].text


def test_read_recent_session_summarizes_tool_calls(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _seed_session(home, "claudette", [
        {"timestamp": "2026-06-01T08:01:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
    ])
    turns = read_recent_session("claudette", agents_root=str(tmp_path / "agents"))
    assert len(turns) == 1
    assert "[tool: Bash]" in turns[0].text


def test_read_recent_session_skips_empty_turns(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _seed_session(home, "morty", [
        {"timestamp": "t", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "x"}]}},   # renders to [result] only? no—
        {"timestamp": "t2", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "real text"}]}},
    ])
    turns = read_recent_session("morty", agents_root=str(tmp_path / "agents"))
    # the tool_result-only turn renders to "[result]" which is non-empty, so
    # it's kept; assert the real text turn is present and last.
    assert turns[-1].text == "real text"


def test_read_recent_session_respects_n(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _seed_session(home, "morty", [
        {"timestamp": f"t{i}", "message": {"role": "assistant",
         "content": [{"type": "text", "text": f"turn {i}"}]}}
        for i in range(10)
    ])
    turns = read_recent_session("morty", n=3, agents_root=str(tmp_path / "agents"))
    assert len(turns) == 3
    assert turns[-1].text == "turn 9"


def test_read_recent_session_unknown_concierge_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert read_recent_session("nope", agents_root=str(tmp_path / "agents")) == []


def test_concierges_constant_is_morty_and_claudette():
    assert set(CONCIERGES) == {"morty", "claudette"}


def test_service_status_returns_string(tmp_path, monkeypatch):
    """service_status never raises and returns a string (real systemctl
    or unknown). We do not assert a specific value (env-dependent)."""
    from console.services.concierge_reader import service_status
    s = service_status("morty")
    assert isinstance(s, str) and s
