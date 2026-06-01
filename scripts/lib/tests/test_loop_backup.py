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

import pytest

from scripts.lib.loop_backup import backup_decision


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
