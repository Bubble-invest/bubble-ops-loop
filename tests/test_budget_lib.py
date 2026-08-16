"""test_budget_lib.py — board #958: budget.py `consumed()` must be WINDOWED to
the current period (Paris-local day/week), not a lifetime sum.

Bug (#958): `consumed(item_id)` used to sum EVERY ledger row ever recorded for
an id, across all time. A per-run/per-day/per-week budget cap compared against
a ~2-week cumulative total drifts every mission permanently into
'tighten'/'over budget'. Only caller is the CLI `budget.py consumed <id>`
(invoked by the dept agent per scaffold.py STEP B.6 steer) — no programmatic
caller, so this is a pure library-level regression suite.

Fix: `consumed(item_id, window="day"|"week"|"all", *, now=None)`. The
day/week boundary is evaluated on the Europe/Paris calendar (matching
`dispatch_helpers.is_mission_due`'s own day/week semantics) — NOT a naive
`ts[:10] == today` UTC-string compare, which drifts a row into the wrong
bucket for up to ~2h around Paris midnight (CET/CEST offset). See
`test_paris_midnight_boundary_*` below for the case that actually exercises
that drift.

Run: python3 -m pytest tests/test_budget_lib.py -q
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.lib import budget


@pytest.fixture(autouse=True)
def _tmp_ledger(tmp_path, monkeypatch):
    """Point the module-level LEDGER at a tmp file for every test in this
    file (never chdir, never touch a real/live ledger)."""
    monkeypatch.setattr(budget, "LEDGER", tmp_path / "budget-ledger.jsonl")
    return budget.LEDGER


def _write_row(ledger_path, item_id: str, usd: float, ts: str) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": item_id, "usd": usd, "ts": ts}) + "\n")


# ---------------------------------------------------------------------------
# day / week / all windowing across 3 distinct Paris days
# ---------------------------------------------------------------------------
#
# now = 2026-06-10T10:00:00+00:00 UTC -> Paris 12:00 on Wed 2026-06-10 (CEST, UTC+2).
# Row A: Paris day 2026-06-10 (today)                          -> in "day" + "week"
# Row B: Paris day 2026-06-08 (Monday, same ISO week as A)     -> in "week" only
# Row C: Paris day 2026-06-01 (Monday, PRIOR ISO week)         -> in neither

_NOW = datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc)  # Paris: Wed 2026-06-10 12:00 CEST
_ROW_A_TS = "2026-06-10T07:00:00+00:00"   # Paris 2026-06-10 09:00 -> today
_ROW_B_TS = "2026-06-08T08:00:00+00:00"   # Paris 2026-06-08 10:00 -> this week, not today
_ROW_C_TS = "2026-06-01T08:00:00+00:00"   # Paris 2026-06-01 10:00 -> prior week


def test_day_window_is_today_only(_tmp_ledger):
    _write_row(_tmp_ledger, "958test", 1.0, _ROW_A_TS)
    _write_row(_tmp_ledger, "958test", 2.0, _ROW_B_TS)
    _write_row(_tmp_ledger, "958test", 4.0, _ROW_C_TS)
    assert budget.consumed("958test", "day", now=_NOW) == 1.0


def test_week_window_is_this_paris_iso_week(_tmp_ledger):
    _write_row(_tmp_ledger, "958test", 1.0, _ROW_A_TS)
    _write_row(_tmp_ledger, "958test", 2.0, _ROW_B_TS)
    _write_row(_tmp_ledger, "958test", 4.0, _ROW_C_TS)
    assert budget.consumed("958test", "week", now=_NOW) == 3.0  # A + B


def test_all_window_is_the_legacy_full_total_regression(_tmp_ledger):
    _write_row(_tmp_ledger, "958test", 1.0, _ROW_A_TS)
    _write_row(_tmp_ledger, "958test", 2.0, _ROW_B_TS)
    _write_row(_tmp_ledger, "958test", 4.0, _ROW_C_TS)
    assert budget.consumed("958test", "all", now=_NOW) == 7.0  # A + B + C


def test_default_window_is_day(_tmp_ledger):
    """CLI default / no explicit window arg -> "day", not the old all-time sum."""
    _write_row(_tmp_ledger, "958test", 1.0, _ROW_A_TS)
    _write_row(_tmp_ledger, "958test", 4.0, _ROW_C_TS)
    assert budget.consumed("958test", now=_NOW) == 1.0


# ---------------------------------------------------------------------------
# The boundary case: a row just before vs just after Paris-midnight must land
# in DIFFERENT day buckets. This is the test that FAILS under a naive
# `ts[:10] == today_utc` implementation (see module docstring / #958 plan).
# ---------------------------------------------------------------------------
#
# "now" = Paris 2026-01-15 10:00 (CET, UTC+1) -> now_utc = 2026-01-15T09:00:00+00:00.
# Row X: ts = 2026-01-14T22:59:00+00:00 UTC -> Paris 2026-01-14T23:59 -> Paris day 01-14 (NOT today)
# Row Y: ts = 2026-01-14T23:01:00+00:00 UTC -> Paris 2026-01-15T00:01 -> Paris day 01-15 (TODAY)
#
# Both X and Y have the SAME utc calendar date (2026-01-14) — a naive
# `ts[:10] == now_utc.date()` compare (now_utc.date() == "2026-01-15") would
# exclude BOTH, incorrectly dropping Y's spend from "today". The Paris-aware
# implementation must count Y and only Y.

_BOUNDARY_NOW = datetime(2026, 1, 15, 9, 0, 0, tzinfo=timezone.utc)  # Paris 2026-01-15 10:00 CET
_ROW_X_TS = "2026-01-14T22:59:00+00:00"  # Paris 2026-01-14 23:59 -> yesterday
_ROW_Y_TS = "2026-01-14T23:01:00+00:00"  # Paris 2026-01-15 00:01 -> today


def test_paris_midnight_boundary_before_midnight_excluded(_tmp_ledger):
    _write_row(_tmp_ledger, "boundary", 5.0, _ROW_X_TS)
    assert budget.consumed("boundary", "day", now=_BOUNDARY_NOW) == 0.0


def test_paris_midnight_boundary_after_midnight_included(_tmp_ledger):
    _write_row(_tmp_ledger, "boundary", 5.0, _ROW_Y_TS)
    assert budget.consumed("boundary", "day", now=_BOUNDARY_NOW) == 5.0


def test_paris_midnight_boundary_both_rows_sorted_into_different_buckets(_tmp_ledger):
    """The row that would be wrongly dropped by a naive UTC-string compare
    (same UTC calendar date as X, but a different Paris day) is the one that
    must count. This is the direct regression check for the #958 bug."""
    _write_row(_tmp_ledger, "boundary", 5.0, _ROW_X_TS)
    _write_row(_tmp_ledger, "boundary", 7.0, _ROW_Y_TS)
    assert budget.consumed("boundary", "day", now=_BOUNDARY_NOW) == 7.0
    assert budget.consumed("boundary", "all", now=_BOUNDARY_NOW) == 12.0


# ---------------------------------------------------------------------------
# Malformed / missing ts: skipped from windowed sums, no crash. "all" keeps
# legacy behaviour (never inspected ts at all) so it still sums the row.
# ---------------------------------------------------------------------------

def test_missing_ts_is_skipped_in_day_window_not_crashed(_tmp_ledger):
    _tmp_ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(_tmp_ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "noTs", "usd": 3.0}) + "\n")  # no "ts" key at all
    assert budget.consumed("noTs", "day", now=_NOW) == 0.0
    assert budget.consumed("noTs", "week", now=_NOW) == 0.0


def test_unparseable_ts_is_skipped_in_windowed_sums_not_crashed(_tmp_ledger):
    _write_row(_tmp_ledger, "badTs", 3.0, "not-a-timestamp")
    assert budget.consumed("badTs", "day", now=_NOW) == 0.0
    assert budget.consumed("badTs", "week", now=_NOW) == 0.0


def test_malformed_ts_still_counted_in_all_window_legacy_regression(_tmp_ledger):
    """`all` never looks at ts (matches the pre-#958 behaviour exactly) so a
    malformed/missing ts row is still summed in that mode — this is the
    backward-compat guarantee for the ~55 live ledger rows."""
    _tmp_ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(_tmp_ledger, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "badTs2", "usd": 3.0}) + "\n")  # no ts
    _write_row(_tmp_ledger, "badTs2", 2.0, "also-not-a-timestamp")
    assert budget.consumed("badTs2", "all", now=_NOW) == 5.0


def test_malformed_json_line_still_skipped(_tmp_ledger):
    """Unchanged pre-existing behaviour: a line that isn't even valid JSON is
    silently skipped in every window."""
    _tmp_ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(_tmp_ledger, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    _write_row(_tmp_ledger, "corrupt", 1.0, _ROW_A_TS)
    assert budget.consumed("corrupt", "day", now=_NOW) == 1.0
    assert budget.consumed("corrupt", "all", now=_NOW) == 1.0


# ---------------------------------------------------------------------------
# No ledger file yet -> 0.0 in every window (unchanged behaviour).
# ---------------------------------------------------------------------------

def test_no_ledger_file_returns_zero_in_every_window(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "LEDGER", tmp_path / "does-not-exist.jsonl")
    assert budget.consumed("anything", "day", now=_NOW) == 0.0
    assert budget.consumed("anything", "week", now=_NOW) == 0.0
    assert budget.consumed("anything", "all", now=_NOW) == 0.0


# ---------------------------------------------------------------------------
# record() is unchanged: still writes {id, usd, ts}.
# ---------------------------------------------------------------------------

def test_record_still_writes_id_usd_ts(_tmp_ledger):
    budget.record("958", 0.42)
    lines = _tmp_ledger.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert set(row.keys()) == {"id", "usd", "ts"}
    assert row["id"] == "958"
    assert row["usd"] == 0.42
    # ts round-trips as a real ISO-8601 timestamp
    datetime.fromisoformat(row["ts"])


def test_record_then_consumed_day_round_trip(_tmp_ledger):
    """record() (using real now()) is immediately visible to consumed(...,
    "day") with no injected `now` — the everyday non-test call path."""
    budget.record("roundtrip", 1.5)
    assert budget.consumed("roundtrip", "day") == 1.5
    assert budget.consumed("roundtrip") == 1.5  # default window is "day"


# ---------------------------------------------------------------------------
# CLI surface: --window flag, default day, output format unchanged.
# ---------------------------------------------------------------------------

def test_cli_consumed_default_window_is_day(_tmp_ledger, capsys):
    _write_row(_tmp_ledger, "cli1", 1.0, _ROW_A_TS)
    _write_row(_tmp_ledger, "cli1", 4.0, _ROW_C_TS)
    rc = budget.main(["consumed", "cli1"])
    assert rc == 0
    # Real "now" is used (no --now flag on the CLI) so we can't assert the
    # exact value here without freezing time; assert the OUTPUT FORMAT
    # instead (unchanged 4-decimal print), which is what STEP B.6 parses.
    out = capsys.readouterr().out.strip()
    float(out)  # parses cleanly
    assert "." in out and len(out.split(".")[-1]) == 4


def test_cli_consumed_window_all_matches_lib_call(_tmp_ledger, capsys):
    _write_row(_tmp_ledger, "cli2", 1.0, _ROW_A_TS)
    _write_row(_tmp_ledger, "cli2", 2.0, _ROW_B_TS)
    _write_row(_tmp_ledger, "cli2", 4.0, _ROW_C_TS)
    rc = budget.main(["consumed", "cli2", "--window", "all"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "7.0000"


def test_cli_rejects_unknown_window(_tmp_ledger):
    with pytest.raises(SystemExit):
        budget.main(["consumed", "cli3", "--window", "month"])
