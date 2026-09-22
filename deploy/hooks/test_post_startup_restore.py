"""Tests for post-startup-restore.py (cross-session handoff re-injection, #1195)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "post-startup-restore.py"


def _run(home: Path, event: str = '{"session_id":"x","source":"startup"}'):
    env = dict(os.environ, HOME=str(home))
    p = subprocess.run([sys.executable, str(HOOK)], input=event,
                       capture_output=True, text=True, env=env, timeout=15)
    return p.returncode, p.stdout.strip()


def _write_handoff(home: Path, body: str, age_h: float = 0.0):
    d = home / ".claude" / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "latest.md"
    f.write_text(body, encoding="utf-8")
    if age_h:
        old = time.time() - age_h * 3600
        os.utime(f, (old, old))
    return f


def test_fresh_handoff_is_injected(tmp_path):
    _write_handoff(tmp_path, "## Working state\n- goal: X\n- next: Y")
    rc, out = _run(tmp_path)
    assert rc == 0
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "goal: X" in ctx and "daily session-rotation" in ctx
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_missing_handoff_emits_nothing(tmp_path):
    rc, out = _run(tmp_path)
    assert rc == 0 and out == ""


def test_stale_handoff_not_injected(tmp_path):
    _write_handoff(tmp_path, "old stuff", age_h=48.0)  # > MAX_AGE_HOURS (36h)
    rc, out = _run(tmp_path)
    assert rc == 0 and out == ""


def test_empty_handoff_emits_nothing(tmp_path):
    _write_handoff(tmp_path, "   \n  ")
    rc, out = _run(tmp_path)
    assert rc == 0 and out == ""


def test_malformed_event_still_tolerant(tmp_path):
    _write_handoff(tmp_path, "recover this")
    rc, out = _run(tmp_path, event="not-json")
    assert rc == 0
    assert "recover this" in out  # tolerant: still restores despite a bad event
