"""Tests for #1489 — shared selector honors `cadence: cron:<expr>` + `active_hours`.

Context (fleet-loop-audit.md gaps #1 and #5, outputs/2026-09-24):
`dispatch_helpers.is_mission_due()` returned False, unconditionally, for any
`cron:`-prefixed cadence ("escape hatch; NOT evaluated here — out of scope
for STEP C.0" — a gap present since the squashed "Initial commit"
73d4824, never later narrowed for a deliberate reason; it was simply never
built). Two live, compliance-critical Géraldine (accountant, jade-m5)
missions therefore could NEVER be selected by the shared selector, on any
host: `quarterly_vat_audit` (`cron:0 8 1 1,4,7,10 *`) and
`annual_statutory_calendar` (`cron:0 8 1 * *`) — VAT-filing / statutory-
deadline missions that depended entirely on the live agent noticing and
firing them from memory.

Also gap #5: content's (Miranda, jade-m1) `draft_substack_note` declares
`active_hours: '09:03-21:00'` in dept.yaml, but `is_mission_due()` never
read it anywhere — the declared quiet-hours window was decorative.

Coverage:
  1. `_cron_prev_fire_paris` — the small self-contained 5-field cron matcher
     (no croniter dependency added — scripts/requirements.txt pins only
     PyYAML, board #1330).
  2. `is_mission_due()` cron branch, using Géraldine's REAL cadences: not
     due before the day's scheduled time; due once the fire moment passes;
     caught up ONCE after a simulated "Mac asleep" multi-day gap, never
     repeated; due again once the NEXT period's slot passes; fails closed
     on a malformed expression.
  3. `active_hours` gate, using Miranda's REAL window: inside vs outside,
     boundary-inclusive, a malformed window fails OPEN, and a mission
     WITHOUT `active_hours` is completely unaffected (regression guard for
     gap #5's "second silently-unenforced field" framing).
  4. Regression: unchanged daily/weekly behavior through the new
     `is_mission_due` -> `_mission_cadence_due` wrapper split.
  5. Both selectors (`select_due_missions`, `select_due_missions_for_forced_
     layer`) pick up a due cron mission, and — the actual production bug,
     not just the pure-function contract — correctly do NOT re-select it
     once it has fired, using a REAL per-mission `.last-run` marker written
     to a DIFFERENT day-dir than the one the selector is later called with.
     `_mission_last_fired` is scoped to `ctx['today_dir']`; without the
     cross-day watermark scan (`_mission_last_fired_cron`) a naive read
     would forget the fire the instant the Paris-local day rolls over, and
     these missions would re-fire on every tick for the rest of the
     quarter/month.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    _cron_prev_fire_paris,
    is_mission_due,
    select_due_missions,
    select_due_missions_for_forced_layer,
    write_last_run,
)

_PARIS = ZoneInfo("Europe/Paris")

# Géraldine's REAL dept.yaml cadences (accountant, jade-m5 — #1489).
QUARTERLY_VAT_CRON = "0 8 1 1,4,7,10 *"
ANNUAL_STATUTORY_CRON = "0 8 1 * *"  # fires the 1st of EVERY month, per the
                                     # actual declared expression (dom=1,
                                     # month=*) — not literally annual; this
                                     # file tests the expression as declared.

QUARTERLY_MISSION = {"id": "quarterly_vat_audit", "layer": 3,
                      "cadence": f"cron:{QUARTERLY_VAT_CRON}"}
ANNUAL_MISSION = {"id": "annual_statutory_calendar", "layer": 1,
                   "cadence": f"cron:{ANNUAL_STATUTORY_CRON}"}

# Miranda's REAL active_hours window (content, jade-m1 — #1489 gap #5).
ACTIVE_HOURS_MISSION = {"id": "draft_substack_note", "layer": 2,
                         "cadence": "every_3h", "active_hours": "09:03-21:00"}


# ---------------------------------------------------------------------------
# UTC <-> Paris anchors used below (Paris CET = UTC+1 in Jan/Feb/Dec,
# CEST = UTC+2 Apr-Oct — DST 2026 runs 2026-03-29 to 2026-10-25).
# ---------------------------------------------------------------------------
JAN1_0730_UTC = datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc)   # 07:30 Paris
JAN1_0800_UTC = datetime(2026, 1, 1, 7, 0, tzinfo=timezone.utc)    # 08:00 Paris
JAN1_0900_UTC = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)    # 09:00 Paris
JAN1_0915_UTC = datetime(2026, 1, 1, 8, 15, tzinfo=timezone.utc)   # 09:15 Paris
JAN5_0900_UTC = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)    # 09:00 Paris
JAN15_0900_UTC = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)  # 09:00 Paris
JAN20_0900_UTC = datetime(2026, 1, 20, 8, 0, tzinfo=timezone.utc)  # 09:00 Paris
FEB2_0900_UTC = datetime(2026, 2, 2, 8, 0, tzinfo=timezone.utc)    # 09:00 Paris
APR1_0800_UTC = datetime(2026, 4, 1, 6, 0, tzinfo=timezone.utc)    # 08:00 Paris (CEST)
APR1_0900_UTC = datetime(2026, 4, 1, 7, 0, tzinfo=timezone.utc)    # 09:00 Paris (CEST)
OCT1_2025_0800_UTC = datetime(2025, 10, 1, 6, 0, tzinfo=timezone.utc)  # 08:00 Paris (CEST)
DEC15_2025_NOON_UTC = datetime(2025, 12, 15, 11, 0, tzinfo=timezone.utc)  # 12:00 Paris


def _paris(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=_PARIS)


# ---------------------------------------------------------------------------
# 1. _cron_prev_fire_paris — the pure matcher
# ---------------------------------------------------------------------------

def test_cron_matcher_finds_previous_quarter_before_this_quarters_slot():
    # Dec 15 2025 — after Q4's Oct 1 08:00 slot, before Q1 2026's Jan 1 slot.
    prev = _cron_prev_fire_paris(QUARTERLY_VAT_CRON, _paris(2025, 12, 15, 12, 0))
    assert prev == _paris(2025, 10, 1, 8, 0)


def test_cron_matcher_at_exact_slot_returns_that_slot():
    prev = _cron_prev_fire_paris(QUARTERLY_VAT_CRON, _paris(2026, 1, 1, 8, 0))
    assert prev == _paris(2026, 1, 1, 8, 0)


def test_cron_matcher_before_slot_same_day_returns_previous_period():
    prev = _cron_prev_fire_paris(QUARTERLY_VAT_CRON, _paris(2026, 1, 1, 7, 59))
    assert prev == _paris(2025, 10, 1, 8, 0)


def test_cron_matcher_monthly_expression_every_month():
    # annual_statutory_calendar's REAL expression (dom=1, month=*) — fires
    # the 1st of every month, not literally annual.
    prev = _cron_prev_fire_paris(ANNUAL_STATUTORY_CRON, _paris(2026, 2, 15, 9, 0))
    assert prev == _paris(2026, 2, 1, 8, 0)


def test_cron_matcher_malformed_expression_returns_none():
    assert _cron_prev_fire_paris("not a cron expr", _paris(2026, 1, 1, 9, 0)) is None
    assert _cron_prev_fire_paris("0 8 1 1,4,7,10", _paris(2026, 1, 1, 9, 0)) is None  # 4 fields
    assert _cron_prev_fire_paris("60 8 1 1 *", _paris(2026, 1, 1, 9, 0)) is None  # minute out of range


# ---------------------------------------------------------------------------
# 2. is_mission_due() — cron branch
# ---------------------------------------------------------------------------

def test_quarterly_cron_not_due_before_scheduled_time_same_day():
    # On the fire day, before 08:00 Paris — last-known watermark is the
    # PRIOR period's fire (already accounted for), so today's slot hasn't
    # opened yet.
    assert is_mission_due(
        QUARTERLY_MISSION, now=JAN1_0730_UTC, last_fired=OCT1_2025_0800_UTC,
    ) is False


def test_quarterly_cron_due_once_scheduled_time_reached():
    assert is_mission_due(
        QUARTERLY_MISSION, now=JAN1_0800_UTC, last_fired=OCT1_2025_0800_UTC,
    ) is True
    assert is_mission_due(
        QUARTERLY_MISSION, now=JAN1_0900_UTC, last_fired=OCT1_2025_0800_UTC,
    ) is True


def test_quarterly_cron_never_fired_is_due_once_first_slot_in_scope_passes():
    # Brand new mission (last_fired=None) — mirrors daily/weekly's own
    # last_fired=None semantics (due once the time-of-day gate opens).
    assert is_mission_due(QUARTERLY_MISSION, now=JAN1_0900_UTC, last_fired=None) is True


def test_quarterly_cron_not_due_again_same_period_after_firing():
    # Fired for real (whenever the executor actually completed — here late,
    # at 09:15, not exactly 08:00) -> not due again later the same day/period.
    assert is_mission_due(
        QUARTERLY_MISSION, now=JAN1_0915_UTC, last_fired=JAN1_0915_UTC,
    ) is False
    assert is_mission_due(
        QUARTERLY_MISSION, now=JAN15_0900_UTC, last_fired=JAN1_0915_UTC,
    ) is False


def test_quarterly_cron_catchup_after_missed_fire_is_caught_up_once():
    # "Mac asleep" through the whole Jan 1 fire day — last-known watermark
    # is still the PRIOR period (Oct 1); now is several days later. Must
    # still be due (catch-up), exactly once.
    assert is_mission_due(
        QUARTERLY_MISSION, now=JAN5_0900_UTC, last_fired=OCT1_2025_0800_UTC,
    ) is True
    # Once the catch-up actually runs (marker now reflects the real, late
    # completion time) it must NOT fire again for the rest of the quarter.
    assert is_mission_due(
        QUARTERLY_MISSION, now=JAN15_0900_UTC, last_fired=JAN5_0900_UTC,
    ) is False
    assert is_mission_due(
        QUARTERLY_MISSION, now=JAN20_0900_UTC, last_fired=JAN5_0900_UTC,
    ) is False


def test_quarterly_cron_due_again_next_period():
    assert is_mission_due(
        QUARTERLY_MISSION, now=APR1_0800_UTC, last_fired=JAN5_0900_UTC,
    ) is True
    assert is_mission_due(
        QUARTERLY_MISSION, now=APR1_0900_UTC, last_fired=JAN5_0900_UTC,
    ) is True


def test_monthly_cron_expression_due_and_caught_up_like_quarterly():
    # annual_statutory_calendar's real (monthly) expression, same idempotence
    # shape: due on Feb 1 after 08:00; not due mid-month; catches up once if
    # missed; due again on the next month's slot.
    assert is_mission_due(ANNUAL_MISSION, now=FEB2_0900_UTC, last_fired=JAN1_0915_UTC) is True
    assert is_mission_due(ANNUAL_MISSION, now=JAN20_0900_UTC, last_fired=JAN1_0915_UTC) is False


def test_cron_malformed_expression_fails_closed_never_due():
    bad_mission = {"id": "broken", "cadence": "cron:not a valid expr"}
    assert is_mission_due(bad_mission, now=JAN1_0900_UTC, last_fired=None) is False
    assert is_mission_due(bad_mission, now=JAN1_0900_UTC, last_fired=OCT1_2025_0800_UTC) is False


# ---------------------------------------------------------------------------
# 3. active_hours gate (#1489 gap #5)
# ---------------------------------------------------------------------------

def _utc(y, mo, d, h_paris, mi_paris, *, offset_h):
    """Build a UTC datetime for a given Paris-local wall time + known offset."""
    return datetime(y, mo, d, h_paris - offset_h, mi_paris, tzinfo=timezone.utc)


# June = CEST = UTC+2.
JUNE_0900_UTC = _utc(2026, 6, 15, 9, 0, offset_h=2)    # 09:00 Paris — BEFORE window (09:03)
JUNE_0903_UTC = _utc(2026, 6, 15, 9, 3, offset_h=2)    # 09:03 Paris — window start, inclusive
JUNE_1200_UTC = _utc(2026, 6, 15, 12, 0, offset_h=2)   # 12:00 Paris — inside
JUNE_2100_UTC = _utc(2026, 6, 15, 21, 0, offset_h=2)   # 21:00 Paris — window end, inclusive
JUNE_2101_UTC = _utc(2026, 6, 15, 21, 1, offset_h=2)   # 21:01 Paris — just after
JUNE_0300_UTC = _utc(2026, 6, 15, 3, 0, offset_h=2)    # 03:00 Paris — well outside


def test_active_hours_inside_window_is_due():
    assert is_mission_due(ACTIVE_HOURS_MISSION, now=JUNE_1200_UTC, last_fired=None) is True


def test_active_hours_before_window_not_due():
    assert is_mission_due(ACTIVE_HOURS_MISSION, now=JUNE_0900_UTC, last_fired=None) is False


def test_active_hours_after_window_not_due():
    assert is_mission_due(ACTIVE_HOURS_MISSION, now=JUNE_2101_UTC, last_fired=None) is False
    assert is_mission_due(ACTIVE_HOURS_MISSION, now=JUNE_0300_UTC, last_fired=None) is False


def test_active_hours_boundaries_are_inclusive():
    assert is_mission_due(ACTIVE_HOURS_MISSION, now=JUNE_0903_UTC, last_fired=None) is True
    assert is_mission_due(ACTIVE_HOURS_MISSION, now=JUNE_2100_UTC, last_fired=None) is True


def test_active_hours_does_not_override_cadence_gate():
    # Inside the window, but the every_3h cadence itself isn't due yet
    # (fired 1h ago) — active_hours can only NARROW, never widen, "due".
    one_hour_before = JUNE_1200_UTC.replace(hour=JUNE_1200_UTC.hour - 1)
    assert is_mission_due(
        ACTIVE_HOURS_MISSION, now=JUNE_1200_UTC, last_fired=one_hour_before,
    ) is False


def test_active_hours_malformed_window_fails_open():
    broken = {"id": "x", "cadence": "every_3h", "active_hours": "not-a-window"}
    # Any time of day — a malformed field must never silently starve.
    assert is_mission_due(broken, now=JUNE_0300_UTC, last_fired=None) is True


def test_mission_without_active_hours_is_unaffected():
    # Same cadence, no active_hours field at all — every_3h behavior is
    # exactly what it was pre-#1489, at any hour.
    control = {"id": "draft_x", "cadence": "every_3h"}
    assert is_mission_due(control, now=JUNE_0300_UTC, last_fired=None) is True
    assert is_mission_due(control, now=JUNE_2101_UTC, last_fired=None) is True


# ---------------------------------------------------------------------------
# 4. Regression — unchanged daily/weekly behavior
# ---------------------------------------------------------------------------

DAILY_MISSION = {"id": "session_handoff", "cadence": "daily", "time": "18:00"}
WEEKLY_MISSION = {"id": "draft_linkedin", "cadence": "weekly",
                   "day": ["monday", "thursday"], "time": "11:35"}


def test_daily_regression_unchanged():
    before = _utc(2026, 6, 15, 17, 0, offset_h=2)   # 17:00 Paris — before 18:00
    at_time = _utc(2026, 6, 15, 18, 0, offset_h=2)  # 18:00 Paris
    later_same_day = _utc(2026, 6, 15, 20, 0, offset_h=2)
    next_day = _utc(2026, 6, 16, 18, 0, offset_h=2)

    assert is_mission_due(DAILY_MISSION, now=before, last_fired=None) is False
    assert is_mission_due(DAILY_MISSION, now=at_time, last_fired=None) is True
    assert is_mission_due(DAILY_MISSION, now=later_same_day, last_fired=at_time) is False
    assert is_mission_due(DAILY_MISSION, now=next_day, last_fired=at_time) is True


def test_weekly_regression_unchanged():
    # 2026-06-15 Monday, 2026-06-16 Tuesday, 2026-06-18 Thursday (CEST, +2).
    monday = _utc(2026, 6, 15, 11, 35, offset_h=2)
    tuesday = _utc(2026, 6, 16, 11, 35, offset_h=2)
    thursday = _utc(2026, 6, 18, 11, 35, offset_h=2)

    assert is_mission_due(WEEKLY_MISSION, now=monday, last_fired=None) is True
    assert is_mission_due(WEEKLY_MISSION, now=tuesday, last_fired=monday) is False
    assert is_mission_due(WEEKLY_MISSION, now=thursday, last_fired=monday) is True


# ---------------------------------------------------------------------------
# 5. Both selectors pick up a due cron mission (+ cross-day watermark)
# ---------------------------------------------------------------------------

def _mk_repo(tmp_path: Path) -> Path:
    (tmp_path / "queues" / "research").mkdir(parents=True)
    (tmp_path / "queues" / "inbox" / "decisions").mkdir(parents=True)
    return tmp_path


def _write_dept_yaml(repo: Path, missions: list[dict]) -> None:
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )


def _stamp_mission_lastrun(repo: Path, mid: str, when: datetime) -> None:
    """Stamp a mission's per-mission `.last-run` AND a dummy real-deliverable
    file next to it. L1/L4 missions are additionally gated by #1080's
    output-truth check (`_layer_output_evidence_ok` — only a `.last-run`
    marker with NO corroborating output is treated as "died mid-dispatch");
    L2/L3 ignore the extra file, so writing it unconditionally is harmless.
    """
    today = when.astimezone(_PARIS).strftime("%Y-%m-%d")
    mission_dir = repo / "outputs" / today / "missions" / mid
    write_last_run(mission_dir, when)
    (mission_dir / "report.md").write_text("stub deliverable\n", encoding="utf-8")


# -- select_due_missions_for_forced_layer (VPS/Mac floor path) --------------

def test_forced_layer_cron_not_due_before_fire_time_same_day(tmp_path):
    repo = _mk_repo(tmp_path)
    _write_dept_yaml(repo, [QUARTERLY_MISSION])
    _stamp_mission_lastrun(repo, "quarterly_vat_audit", OCT1_2025_0800_UTC)
    due = select_due_missions_for_forced_layer(repo, 3, now_utc=JAN1_0730_UTC)
    assert due == []


def test_forced_layer_cron_fires_once_across_day_rollover(tmp_path):
    """The actual production bug: a marker written on the fire day must
    still veto re-selection on a LATER day (different outputs/<today>/ dir).
    """
    repo = _mk_repo(tmp_path)
    _write_dept_yaml(repo, [QUARTERLY_MISSION])

    # Before any watermark at all: nothing fired for Q1 yet, past Q4's slot
    # -> due (fresh-mission catch-up, mirrors daily/weekly last_fired=None).
    due = select_due_missions_for_forced_layer(repo, 3, now_utc=JAN1_0900_UTC)
    assert [m["id"] for m in due] == ["quarterly_vat_audit"]

    # It actually ran (late — 09:15 Jan 1), stamped under outputs/2026-01-01/.
    _stamp_mission_lastrun(repo, "quarterly_vat_audit", JAN1_0915_UTC)

    # A floor tick TWO WEEKS later reads a DIFFERENT outputs/<today>/ dir.
    # Pre-#1489-fix, _mission_last_fired only checked THAT day's dir, found
    # nothing, and would wrongly re-select the mission every tick.
    due_later = select_due_missions_for_forced_layer(repo, 3, now_utc=JAN15_0900_UTC)
    assert due_later == []
    due_even_later = select_due_missions_for_forced_layer(repo, 3, now_utc=JAN20_0900_UTC)
    assert due_even_later == []

    # Next quarter's slot (Apr 1) -> due again.
    due_next_period = select_due_missions_for_forced_layer(repo, 3, now_utc=APR1_0900_UTC)
    assert [m["id"] for m in due_next_period] == ["quarterly_vat_audit"]


def test_forced_layer_no_dept_yaml_returns_empty(tmp_path):
    repo = _mk_repo(tmp_path)
    assert select_due_missions_for_forced_layer(repo, 3, now_utc=JAN1_0900_UTC) == []


# -- select_due_missions (live-loop path) ------------------------------------

def _bare_ctx(now: datetime, repo_dir: "str | None" = None, **overrides) -> dict:
    base = {
        "now_utc": now,
        "today": now.astimezone(_PARIS).strftime("%Y-%m-%d"),
        "today_dir": "/nonexistent/scratch",
        "_repo_dir": repo_dir,
        "has_research_items": False,
        "has_inbox_decisions": False,
        "has_unconsumed_mgmt_notes": False,
        "layer_1_last_run_today": None,
        "layer_2_last_run_today": None,
        "layer_3_last_run_today": None,
        "layer_4_last_run_today": None,
        "round_counter": {},
        "layer_1_baseline_counter": {},
        "fire_after_rounds": 1,
    }
    base.update(overrides)
    return base


def test_select_due_missions_picks_up_cron_mission(tmp_path):
    repo = _mk_repo(tmp_path)
    ctx = _bare_ctx(JAN1_0900_UTC, repo_dir=str(repo))
    due = select_due_missions(ctx, [ANNUAL_MISSION])
    assert [m["id"] for m in due] == ["annual_statutory_calendar"]


def test_select_due_missions_cron_fires_once_across_day_rollover(tmp_path):
    repo = _mk_repo(tmp_path)
    _stamp_mission_lastrun(repo, "annual_statutory_calendar", JAN1_0915_UTC)

    # Same (monthly) period, later day -> not re-selected.
    ctx_mid_month = _bare_ctx(JAN20_0900_UTC, repo_dir=str(repo))
    assert select_due_missions(ctx_mid_month, [ANNUAL_MISSION]) == []

    # Next month's slot -> due again.
    ctx_next_month = _bare_ctx(FEB2_0900_UTC, repo_dir=str(repo))
    due = select_due_missions(ctx_next_month, [ANNUAL_MISSION])
    assert [m["id"] for m in due] == ["annual_statutory_calendar"]
