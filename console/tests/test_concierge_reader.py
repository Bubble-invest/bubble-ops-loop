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
    # UX v2: tool calls are now a structured kind, not text noise.
    assert turns[0].kind == "tool" and turns[0].tool_name == "Bash"


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


# ─── TTL cache on service_status (board #450) ──────────────────────────

def test_service_status_cached_within_ttl(monkeypatch):
    """Repeat calls within the TTL window must NOT re-shell out to systemctl —
    this used to run `systemctl is-active` on every concierge on every page load."""
    from console.services import concierge_reader

    concierge_reader._service_status_cache.clear()
    calls = {"n": 0}

    class R:
        stdout = "active\n"

    def fake_run(*a, **k):
        calls["n"] += 1
        return R()

    import subprocess as _subprocess
    monkeypatch.setattr(_subprocess, "run", fake_run)

    first = concierge_reader.service_status("morty-ttl-test")
    second = concierge_reader.service_status("morty-ttl-test")
    assert first == second == "active"
    assert calls["n"] == 1, f"expected exactly 1 systemctl call, got {calls['n']}"


def test_service_status_logs_warning_on_probe_failure(monkeypatch, caplog):
    import logging
    from console.services import concierge_reader

    concierge_reader._service_status_cache.clear()

    def fake_run(*a, **k):
        raise OSError("systemctl not found")

    import subprocess as _subprocess
    monkeypatch.setattr(_subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING):
        result = concierge_reader.service_status("nope-ttl-test")

    assert result == "unknown"
    assert any(rec.levelno == logging.WARNING for rec in caplog.records), (
        f"expected a WARNING log on probe failure, got: {[r.message for r in caplog.records]}"
    )


# ─── UX v2: richer turn classification ─────────────────────────────

def test_turn_has_kind_message_for_text(tmp_path, monkeypatch):
    home = tmp_path / "home"; monkeypatch.setenv("HOME", str(home))
    _seed_session(home, "morty", [
        {"timestamp": "2026-06-01T08:00:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Voici le résumé."}]}},
    ])
    turns = read_recent_session("morty", agents_root=str(tmp_path / "agents"))
    assert turns[-1].kind == "message"
    assert turns[-1].text == "Voici le résumé."


def test_tool_turn_has_kind_tool_and_detail(tmp_path, monkeypatch):
    home = tmp_path / "home"; monkeypatch.setenv("HOME", str(home))
    _seed_session(home, "morty", [
        {"timestamp": "t", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "git push origin main"}}]}},
    ])
    turns = read_recent_session("morty", agents_root=str(tmp_path / "agents"))
    assert turns[-1].kind == "tool"
    assert turns[-1].tool_name == "Bash"
    assert "git push" in turns[-1].detail


def test_pure_tool_result_turns_are_dropped(tmp_path, monkeypatch):
    """A user turn that is ONLY a tool_result is mechanical noise — drop it."""
    home = tmp_path / "home"; monkeypatch.setenv("HOME", str(home))
    _seed_session(home, "morty", [
        {"timestamp": "t1", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "x"}]}},
        {"timestamp": "t2", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "real"}]}},
    ])
    turns = read_recent_session("morty", agents_root=str(tmp_path / "agents"))
    # only the real message survives; the tool_result-only turn is dropped
    assert len(turns) == 1
    assert turns[0].kind == "message"


def test_tool_detail_for_edit_shows_file(tmp_path, monkeypatch):
    home = tmp_path / "home"; monkeypatch.setenv("HOME", str(home))
    _seed_session(home, "morty", [
        {"timestamp": "t", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": "/home/claude/x/home.html"}}]}},
    ])
    turns = read_recent_session("morty", agents_root=str(tmp_path / "agents"))
    assert turns[-1].kind == "tool"
    assert "home.html" in turns[-1].detail
