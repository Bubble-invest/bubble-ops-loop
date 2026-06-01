"""test_agent_session.py — shared live-session reader (depts + concierges)."""
from __future__ import annotations

import json

import pytest

from console.services.agent_session import (
    newest_session_file,
    read_session_turns,
    newest_session_mtime_iso,
    SessionTurn,
)


def _seed(home, suffix, turns, fname="s.jsonl"):
    d = home / ".claude" / "projects" / f"-home-claude-agents-{suffix}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / fname
    with f.open("w", encoding="utf-8") as fh:
        for t in turns:
            fh.write(json.dumps(t) + "\n")
    return f


def test_newest_session_picks_across_candidate_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # prefixed dir is older, unprefixed is newer → newest wins
    import os, time
    f_old = _seed(tmp_path, "bubble-ops-maya", [{"message": {"role": "user", "content": "a"}}])
    f_new = _seed(tmp_path, "maya", [{"message": {"role": "user", "content": "b"}}], fname="n.jsonl")
    os.utime(f_old, (1, 1))
    os.utime(f_new, (10_000_000_000, 10_000_000_000))
    got = newest_session_file(["maya", "bubble-ops-maya"])
    assert got == str(f_new)


def test_newest_session_none_when_no_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert newest_session_file(["nope", "bubble-ops-nope"]) is None


def test_read_turns_classifies_message_and_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = _seed(tmp_path, "bubble-ops-tony", [
        {"timestamp": "2026-06-01T08:00:00Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}},
        {"timestamp": "2026-06-01T08:00:01Z",
         "message": {"role": "assistant", "content": [
             {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]}},
    ])
    turns = read_session_turns(str(f))
    assert turns[0].kind == "message" and turns[0].text == "hello"
    assert turns[1].kind == "tool" and turns[1].tool_name == "Bash"
    assert "ls -la" in turns[1].detail


def test_read_turns_drops_tool_results(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = _seed(tmp_path, "bubble-ops-cgp", [
        {"timestamp": "t", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "x"}]}},
        {"timestamp": "t2", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "kept"}]}},
    ])
    turns = read_session_turns(str(f))
    assert len(turns) == 1 and turns[0].text == "kept"


def test_read_turns_respects_n(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = _seed(tmp_path, "bubble-ops-tony", [
        {"timestamp": f"t{i}", "message": {"role": "assistant",
         "content": [{"type": "text", "text": f"m{i}"}]}} for i in range(8)
    ])
    turns = read_session_turns(str(f), n=2)
    assert [t.text for t in turns] == ["m6", "m7"]


def test_read_turns_none_file_empty():
    assert read_session_turns(None) == []


def test_mtime_iso_present(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _seed(tmp_path, "bubble-ops-tony", [{"message": {"role": "user", "content": "x"}}])
    iso = newest_session_mtime_iso(["tony", "bubble-ops-tony"])
    assert iso and iso.endswith("Z")
