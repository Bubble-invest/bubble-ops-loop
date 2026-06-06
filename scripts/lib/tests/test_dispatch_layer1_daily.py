"""Layer 1 fires at least once a day, or once the other layers complete a cycle.

Joris flag 2026-06-01 (refined): "at least once per day, or whenever all other
layers have fired once."

decide_dispatch C.0 fires L1 when EITHER:
  (a) L1 has not run today  → daily floor, independent of dept exports
      (emails + Notion logbook are reason enough), OR
  (b) L2, L3, L4 have EACH completed >= fire_after_rounds rounds since L1's last
      fire (measured against layer_1_baseline_counter) → fresh cycle.
It stays lowest priority — L4 window, research, decisions win first on a tick.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pathlib import Path

from scripts.lib.dispatch_helpers import (
    decide_dispatch,
    increment_round_counter,
    read_l1_baseline,
    write_l1_baseline,
)

# A time outside the 22:00-22:30 UTC L4 window.
_MORNING = datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc)
_L4_WINDOW = datetime(2026, 6, 1, 17, 10, 0, tzinfo=timezone.utc)
_RAN = datetime(2026, 6, 1, 5, 0, 0, tzinfo=timezone.utc)


# ── (a) daily floor ─────────────────────────────────────────────────────────

def test_layer1_fires_when_not_run_today_even_with_no_exports():
    out = decide_dispatch({
        "now_utc": _MORNING,
        "has_research_items": False,
        "has_inbox_decisions": False,
        "layer_1_last_run_today": None,
    })
    assert out == "layer_1"


def test_daily_floor_ignores_empty_counter():
    # No rounds at all, but L1 hasn't run today → daily floor fires it.
    out = decide_dispatch({
        "now_utc": _MORNING,
        "round_counter": {},
        "layer_1_last_run_today": None,
    })
    assert out == "layer_1"


# ── (b) cycle gate after L1 already ran ─────────────────────────────────────

def test_no_refire_when_no_new_rounds():
    # Ran today, no other layer progressed since → heartbeat.
    out = decide_dispatch({
        "now_utc": _MORNING,
        "layer_1_last_run_today": _RAN,
        "round_counter": {"2": 1, "3": 1, "4": 1},
        "layer_1_baseline_counter": {"2": 1, "3": 1, "4": 1},
    })
    assert out == "heartbeat"


def test_refire_when_full_cycle_since_last_fire():
    # Each of L2/L3/L4 advanced >=1 beyond the baseline → re-fire.
    out = decide_dispatch({
        "now_utc": _MORNING,
        "layer_1_last_run_today": _RAN,
        "round_counter": {"2": 2, "3": 2, "4": 2},
        "layer_1_baseline_counter": {"2": 1, "3": 1, "4": 1},
    })
    assert out == "layer_1"


def test_no_refire_on_partial_cycle():
    # Only L2 and L3 advanced; L4 has not → not a full cycle → heartbeat.
    out = decide_dispatch({
        "now_utc": _MORNING,
        "layer_1_last_run_today": _RAN,
        "round_counter": {"2": 2, "3": 2, "4": 1},
        "layer_1_baseline_counter": {"2": 1, "3": 1, "4": 1},
    })
    assert out == "heartbeat"


def test_first_cycle_from_zero_baseline():
    # L1 ran early (daily floor) with no baseline recorded yet ({} => zeros);
    # once all three layers fire once, the cycle completes → re-fire.
    out = decide_dispatch({
        "now_utc": _MORNING,
        "layer_1_last_run_today": _RAN,
        "round_counter": {"2": 1, "3": 1, "4": 1},
        "layer_1_baseline_counter": {},
    })
    assert out == "layer_1"


def test_fire_after_rounds_threshold_respected():
    # With threshold 2, a single round each is not yet a full cycle.
    base = {
        "now_utc": _MORNING,
        "layer_1_last_run_today": _RAN,
        "layer_1_baseline_counter": {"2": 0, "3": 0, "4": 0},
        "fire_after_rounds": 2,
    }
    assert decide_dispatch({**base, "round_counter": {"2": 1, "3": 1, "4": 1}}) == "heartbeat"
    assert decide_dispatch({**base, "round_counter": {"2": 2, "3": 2, "4": 2}}) == "layer_1"


# ── priority: other branches still win over L1 ──────────────────────────────

def test_l4_window_wins_over_layer1():
    out = decide_dispatch({
        "now_utc": _L4_WINDOW,
        "layer_4_last_run_today": None,
        "layer_1_last_run_today": None,
    })
    assert out == "layer_4"


def test_research_wins_over_layer1():
    out = decide_dispatch({
        "now_utc": _MORNING,
        "has_research_items": True,
        "layer_1_last_run_today": None,
    })
    assert out == "layer_2"


def test_decisions_win_over_layer1():
    out = decide_dispatch({
        "now_utc": _MORNING,
        "has_inbox_decisions": True,
        "layer_1_last_run_today": None,
    })
    assert out == "layer_3"


# ── baseline snapshot round-trip ────────────────────────────────────────────

def test_l1_baseline_missing_is_empty(tmp_path):
    assert read_l1_baseline(tmp_path) == {}


def test_l1_baseline_snapshots_current_counter(tmp_path):
    increment_round_counter(tmp_path, layer=2)
    increment_round_counter(tmp_path, layer=3)
    snap = write_l1_baseline(tmp_path)
    assert snap == {"2": 1, "3": 1}
    assert read_l1_baseline(tmp_path) == {"2": 1, "3": 1}


def test_l1_baseline_explicit_counts(tmp_path):
    write_l1_baseline(tmp_path, {"2": 5, "3": 4, "4": 4})
    assert read_l1_baseline(tmp_path) == {"2": 5, "3": 4, "4": 4}
