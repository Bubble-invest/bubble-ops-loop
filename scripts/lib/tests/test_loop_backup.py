"""test_loop_backup.py — backup-execution decision logic.

Context ({{OPERATOR}} 2026-06-01): a per-dept BACKUP runner fires twice a day
(morning + afternoon). For each dept it must decide: is the persistent
/loop alive (recent heartbeat) → SKIP, or dead/parked (stale heartbeat)
→ run ONE backup dispatch tick. This module is the pure decision; the
bash wrapper does the `claude -p` + flock.

`backup_decision(latest_heartbeat_epoch, now_epoch, stale_after_sec)`
returns:
    {"action": "skip",  "reason": "loop alive (heartbeat fresh)"}      # fresh
    {"action": "run",   "reason": "loop stale (… )"}                    # stale
    {"action": "run",   "reason": "no heartbeat found"}                # never ticked

Rules:
  - latest_heartbeat_epoch is None  -> run (no evidence the loop ever ran)
  - age = now - latest;  age <= stale_after_sec -> skip (alive)
  - age >  stale_after_sec          -> run (dead/parked → back it up)
  - a heartbeat in the FUTURE (clock skew) is treated as fresh -> skip

TDD: written BEFORE the function exists. RED -> GREEN.
"""
from __future__ import annotations

import json

import pytest

from scripts.lib.loop_backup import (
    append_event,
    backup_decision,
    format_event,
    latest_per_dept,
    read_events,
)


HOUR = 3600
STALE = 90 * 60  # 90 minutes, {{OPERATOR}}-approved threshold


def test_fresh_heartbeat_skips():
    now = 1_000_000
    hb = now - 10 * 60  # 10 min ago
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "skip"


def test_stale_heartbeat_runs():
    now = 1_000_000
    hb = now - 120 * 60  # 2h ago > 90m
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "run"


def test_exactly_at_threshold_is_skip():
    """age == threshold → still considered alive (boundary inclusive)."""
    now = 1_000_000
    hb = now - STALE
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "skip"


def test_one_second_past_threshold_runs():
    now = 1_000_000
    hb = now - (STALE + 1)
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "run"


def test_no_heartbeat_runs():
    d = backup_decision(None, 1_000_000, STALE)
    assert d["action"] == "run"
    assert "no heartbeat" in d["reason"].lower()


def test_future_heartbeat_treated_as_fresh():
    """Clock skew: heartbeat slightly in the future must not trigger a run."""
    now = 1_000_000
    hb = now + 30  # 30s in the future
    d = backup_decision(hb, now, STALE)
    assert d["action"] == "skip"


def test_reason_is_present_and_descriptive():
    now = 1_000_000
    run = backup_decision(now - 3 * HOUR, now, STALE)
    skip = backup_decision(now - 60, now, STALE)
    assert run["reason"] and "stale" in run["reason"].lower()
    assert skip["reason"] and "fresh" in skip["reason"].lower()


def test_threshold_is_respected_per_call():
    """A caller can pass a tighter/looser threshold."""
    now = 1_000_000
    hb = now - 30 * 60  # 30 min ago
    assert backup_decision(hb, now, 20 * 60)["action"] == "run"   # 20m thresh → stale
    assert backup_decision(hb, now, 60 * 60)["action"] == "skip"  # 60m thresh → fresh


# ─── Event log ({{OPERATOR}} msg 1171) ──────────────────────────────────────────
#
# Every fire appends one JSON line per dept so the cockpit can surface the
# safety-net result in the front end. Reader/writer share this module.


def test_format_event_minimal():
    ev = format_event("maya", "skip", "loop alive", ts="2026-06-01T12:00:00Z")
    assert ev == {
        "ts": "2026-06-01T12:00:00Z",
        "slug": "maya",
        "action": "skip",
        "reason": "loop alive",
    }


def test_format_event_run_carries_age_and_exit():
    ev = format_event("tony", "run", "loop stale", age_sec=9000, exit_code=0,
                      ts="2026-06-01T12:00:01Z")
    assert ev["action"] == "run"
    assert ev["age_sec"] == 9000
    assert ev["exit"] == 0


def test_append_then_read_roundtrip(tmp_path):
    path = str(tmp_path / "state" / "loop-backup.jsonl")  # parent created on write
    append_event(path, format_event("maya", "skip", "alive", ts="2026-06-01T08:00:00Z"))
    append_event(path, format_event("tony", "run", "stale", age_sec=9000,
                                    exit_code=0, ts="2026-06-01T08:00:01Z"))
    events = read_events(path)
    assert [e["slug"] for e in events] == ["maya", "tony"]      # chronological
    assert events[1]["exit"] == 0


def test_read_events_missing_file_is_empty():
    assert read_events("/no/such/path.jsonl") == []


def test_read_events_skips_blank_and_garbage_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        json.dumps({"ts": "t1", "slug": "maya", "action": "skip", "reason": "x"}) + "\n"
        "\n"                                   # blank
        "not json at all\n"                    # garbage — must be skipped
        "[1,2,3]\n"                            # valid json but not a dict — skipped
        + json.dumps({"ts": "t2", "slug": "tony", "action": "run", "reason": "y"}) + "\n",
        encoding="utf-8",
    )
    events = read_events(str(path))
    assert [e["slug"] for e in events] == ["maya", "tony"]


def test_read_events_filter_by_slug_and_limit(tmp_path):
    path = tmp_path / "log.jsonl"
    for i in range(5):
        append_event(str(path), format_event("maya", "skip", f"r{i}", ts=f"t{i}"))
        append_event(str(path), format_event("tony", "skip", f"r{i}", ts=f"t{i}"))
    maya = read_events(str(path), slug="maya")
    assert len(maya) == 5 and all(e["slug"] == "maya" for e in maya)
    last2 = read_events(str(path), slug="maya", limit=2)
    assert [e["reason"] for e in last2] == ["r3", "r4"]


def test_latest_per_dept_keeps_most_recent():
    events = [
        {"ts": "t1", "slug": "maya", "action": "run", "reason": "a"},
        {"ts": "t2", "slug": "maya", "action": "skip", "reason": "b"},
        {"ts": "t3", "slug": "tony", "action": "skip", "reason": "c"},
    ]
    latest = latest_per_dept(events)
    assert latest["maya"]["reason"] == "b"     # second maya event wins
    assert latest["tony"]["reason"] == "c"
