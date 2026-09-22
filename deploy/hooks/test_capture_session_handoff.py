"""Tests for capture-session-handoff.py (daily-rotation handoff writer, #1195)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "capture-session-handoff.py"


def _run(home: Path):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                          text=True, env=env, timeout=15)


def _transcript(home: Path, lines):
    d = home / ".claude" / "projects" / "-srv-agents-x"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "s1.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    return f


def _note(home: Path, body: str):
    d = home / ".claude" / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    (d / "note.md").write_text(body, encoding="utf-8")


def test_deterministic_extract_writes_latest(tmp_path):
    _transcript(tmp_path, [
        {"message": {"role": "user", "content": "work on the fund rebalance"}},
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text": "Rebalanced; queued a trim proposal."},
            {"type": "tool_use", "input": {"file_path": "/srv/agents/x/WORKING_MEMORY.md"}}]}},
    ])
    p = _run(tmp_path)
    assert p.returncode == 0
    latest = (tmp_path / ".claude" / "handoff" / "latest.md").read_text()
    assert "fund rebalance" in latest and "trim proposal" in latest
    assert "WORKING_MEMORY.md" in latest


def test_agent_note_is_folded_and_consumed(tmp_path):
    _transcript(tmp_path, [{"message": {"role": "user", "content": "hi"}}])
    _note(tmp_path, "## My state\n- mid-way through X\n- next: Y")
    p = _run(tmp_path)
    assert p.returncode == 0
    latest = (tmp_path / ".claude" / "handoff" / "latest.md").read_text()
    assert "mid-way through X" in latest and "handoff note (written just now)" in latest
    assert not (tmp_path / ".claude" / "handoff" / "note.md").exists()  # consumed


def test_failsafe_no_content_exits_nonzero(tmp_path):
    # no transcript, no note -> must NOT write latest.md, exit 1 (rotation skipped)
    p = _run(tmp_path)
    assert p.returncode == 1
    assert not (tmp_path / ".claude" / "handoff" / "latest.md").exists()


def test_failsafe_only_tool_noise_exits_nonzero(tmp_path):
    # transcript with no real user ask / assistant note -> fail-safe
    _transcript(tmp_path, [{"message": {"role": "user", "content": "<tool_result> stuff"}}])
    p = _run(tmp_path)
    assert p.returncode == 1


def test_stale_note_ignored_but_extract_still_ok(tmp_path):
    _transcript(tmp_path, [{"message": {"role": "user", "content": "real ask here"}}])
    _note(tmp_path, "stale note")
    nf = tmp_path / ".claude" / "handoff" / "note.md"
    old = time.time() - 3600  # 1h > default 1800s
    os.utime(nf, (old, old))
    p = _run(tmp_path)
    assert p.returncode == 0
    latest = (tmp_path / ".claude" / "handoff" / "latest.md").read_text()
    assert "real ask here" in latest and "stale note" not in latest
