"""Tests for next_pending_mission_time_today (board #508, 2026-07-03).

BUG FIXED: wake-arming was LAYER-granular, not mission-granular. The live
/loop's protocol arms "tomorrow morning" once every LAYER has fired at least
once today (e.g. Ben's L4 `risk_control`@21:00 fires -> loop declares L4 done
-> arms tomorrow 08:03). A SAME-LAYER mission with a LATER `time:` (e.g. Ben's
`market_wrapup`@22:30, also L4) is then never re-checked and silently skipped
every day it doesn't happen to coincide with a live tick.

FIX: next_pending_mission_time_today(missions, now, last_run_lookup) is a pure
helper the loop's wake-arming step calls BEFORE arming the next-morning
one-shot: if any mission's cadence resolves to "due later today" (daily/weekly
whose `time:` is still ahead of `now`, not yet fired today/this-week), return
that mission's next Paris-local fire datetime TODAY so the loop can arm an
INTERIM one-shot wake instead of jumping straight to tomorrow morning.

Test coverage:
  1. daily@22:30, now=21:05, not fired today -> returns today's 22:30 (Paris).
  2. Same mission, already fired today (last_run stamped after 22:30 today)
     -> returns None (nothing pending later today for it).
  3. Multiple pending missions today -> returns the EARLIEST upcoming time
     (so the loop arms the closest useful wake, not the latest).
  4. A daily mission whose time has ALREADY PASSED today (and not yet fired)
     is NOT "later today" (it's overdue NOW, not later) -> excluded from this
     helper's result (the live tick / catch-up path handles overdue, not
     interim-wake arming).
  5. Weekly mission: due today later in the day, right weekday -> included;
     wrong weekday -> excluded.
  6. hourly / every_Nh / every_Nm / event cadences do not have a fixed `time:`
     "later today" slot -> excluded (helper only handles daily/weekly time-of-day
     missions — the ones actually starved by the layer-granular bug).
  7. No missions pending later today -> returns None.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.lib.dispatch_helpers import next_pending_mission_time_today


def _last_run_lookup(fired: "dict[str, datetime]"):
    def _lookup(mission_id: str):
        return fired.get(mission_id)
    return _lookup


def test_daily_mission_pending_later_today_returns_its_time():
    # 2026-06-23 21:05 Paris (CEST, UTC+2) -> 19:05 UTC.
    now = datetime(2026, 6, 23, 19, 5, tzinfo=timezone.utc)
    missions = [
        {"id": "market_wrapup", "layer": 4, "cadence": "daily", "time": "22:30"},
    ]
    result = next_pending_mission_time_today(missions, now=now, last_run_lookup=_last_run_lookup({}))
    assert result is not None
    # 22:30 Paris today == 20:30 UTC same day.
    assert result.astimezone(timezone.utc) == datetime(2026, 6, 23, 20, 30, tzinfo=timezone.utc)


def test_daily_mission_already_fired_today_returns_none():
    now = datetime(2026, 6, 23, 21, 0, tzinfo=timezone.utc)  # 23:00 Paris
    missions = [
        {"id": "market_wrapup", "layer": 4, "cadence": "daily", "time": "22:30"},
    ]
    fired = {"market_wrapup": datetime(2026, 6, 23, 20, 35, tzinfo=timezone.utc)}  # fired 22:35 Paris today
    result = next_pending_mission_time_today(missions, now=now, last_run_lookup=_last_run_lookup(fired))
    assert result is None


def test_multiple_pending_missions_returns_earliest():
    now = datetime(2026, 6, 23, 17, 30, tzinfo=timezone.utc)  # 19:30 Paris
    missions = [
        {"id": "market_wrapup", "layer": 4, "cadence": "daily", "time": "22:30"},
        {"id": "risk_control", "layer": 4, "cadence": "daily", "time": "21:00"},
    ]
    result = next_pending_mission_time_today(missions, now=now, last_run_lookup=_last_run_lookup({}))
    assert result is not None
    # earliest pending = risk_control @ 21:00 Paris = 19:00 UTC
    assert result.astimezone(timezone.utc) == datetime(2026, 6, 23, 19, 0, tzinfo=timezone.utc)


def test_overdue_mission_not_yet_fired_is_not_later_today():
    # now is AFTER the mission's time and it hasn't fired -> that's "overdue
    # now", not "pending later today". This helper is only for interim-wake
    # arming (a future slot), not the live-tick catch-up path.
    now = datetime(2026, 6, 23, 21, 0, tzinfo=timezone.utc)  # 23:00 Paris, after 22:30
    missions = [
        {"id": "market_wrapup", "layer": 4, "cadence": "daily", "time": "22:30"},
    ]
    result = next_pending_mission_time_today(missions, now=now, last_run_lookup=_last_run_lookup({}))
    assert result is None


def test_weekly_mission_right_weekday_pending_later_today():
    # 2026-06-23 is a Tuesday.
    now = datetime(2026, 6, 23, 15, 0, tzinfo=timezone.utc)  # 17:00 Paris Tuesday
    missions = [
        {"id": "newsletter_redaction", "layer": 2, "cadence": "weekly",
         "day": "tuesday", "time": "18:03"},
    ]
    result = next_pending_mission_time_today(missions, now=now, last_run_lookup=_last_run_lookup({}))
    assert result is not None
    assert result.astimezone(timezone.utc) == datetime(2026, 6, 23, 16, 3, tzinfo=timezone.utc)


def test_weekly_mission_wrong_weekday_excluded():
    now = datetime(2026, 6, 23, 15, 0, tzinfo=timezone.utc)  # Tuesday
    missions = [
        {"id": "newsletter_redaction", "layer": 2, "cadence": "weekly",
         "day": "friday", "time": "18:03"},
    ]
    result = next_pending_mission_time_today(missions, now=now, last_run_lookup=_last_run_lookup({}))
    assert result is None


def test_non_time_of_day_cadences_excluded():
    now = datetime(2026, 6, 23, 15, 0, tzinfo=timezone.utc)
    missions = [
        {"id": "hourly_check", "layer": 1, "cadence": "hourly"},
        {"id": "every_2h_check", "layer": 1, "cadence": "every_2h"},
        {"id": "every_30m_check", "layer": 1, "cadence": "every_30m"},
        {"id": "event_mission", "layer": 3, "cadence": "event"},
    ]
    result = next_pending_mission_time_today(missions, now=now, last_run_lookup=_last_run_lookup({}))
    assert result is None


def test_no_missions_pending_returns_none():
    now = datetime(2026, 6, 23, 15, 0, tzinfo=timezone.utc)
    missions: list = []
    result = next_pending_mission_time_today(missions, now=now, last_run_lookup=_last_run_lookup({}))
    assert result is None
