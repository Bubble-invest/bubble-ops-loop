"""Tests for session_handoff_incomplete_today (board #1469, 2026-09-23).

BUG FIXED: the "all layers done -> arm tomorrow 08:03" wake-arming branch had
no code-level guard against the fleet-standard `session_handoff` mission
(board #1195) still being incomplete for today. `session_handoff` is
deliberately defined WITHOUT a `time:` field (it runs LAST in the day, after
every other L4 mission), so it falls through BOTH existing safety nets that
are explicitly scoped to `time:`-bearing missions:
  - next_pending_mission_time_today (#508)
  - the live-tick catch-up fallback (#757)
On 2026-09-22, ben's 20:32Z tick saw L4 already "fired" (risk_control had run)
and self-armed straight to tomorrow 06:03 without ever running
session_handoff — no HANDOFF.md was written, and the ~05:30Z rotation SKIPPED
ben outright (board #1469).

FIX: session_handoff_incomplete_today(missions, now, last_run_lookup) is a
pure helper the loop's wake-arming step calls BEFORE arming the next-morning
one-shot: if the dept defines a `session_handoff` mission and it has not
completed for today's Paris-local period, the "arm tomorrow" branch must not
be taken this tick.

Test coverage:
  1. No session_handoff mission defined -> False (dept unaffected, behaves
     exactly as before).
  2. session_handoff defined, never fired -> True (incomplete).
  3. session_handoff fired earlier TODAY (Paris) -> False (complete).
  4. session_handoff fired YESTERDAY (Paris), not yet today -> True
     (incomplete for today's period, even though it "fired" in the past).
  5. session_handoff fired late last night, checked just after Paris
     midnight -> True (new Paris day, new period, not yet fired).
  6. session_handoff with a non-"daily" cadence -> False (not the
     fleet-standard shape; do not guess at intent).
  7. Other missions in the list are ignored — only the "session_handoff" id
     is consulted.
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.lib.dispatch_helpers import session_handoff_incomplete_today


def _last_run_lookup(fired: "dict[str, datetime]"):
    def _lookup(mission_id: str):
        return fired.get(mission_id)
    return _lookup


def test_no_session_handoff_mission_returns_false():
    # 2026-09-22 20:32 UTC (22:32 Paris, CEST) — ben's exact tick time.
    now = datetime(2026, 9, 22, 20, 32, tzinfo=timezone.utc)
    missions = [
        {"id": "risk_control", "layer": 4, "cadence": "daily", "time": "21:00"},
        {"id": "market_wrapup", "layer": 4, "cadence": "daily", "time": "22:30"},
    ]
    result = session_handoff_incomplete_today(
        missions, now=now, last_run_lookup=_last_run_lookup({})
    )
    assert result is False


def test_session_handoff_never_fired_is_incomplete():
    now = datetime(2026, 9, 22, 20, 32, tzinfo=timezone.utc)
    missions = [
        {"id": "session_handoff", "layer": 4, "cadence": "daily"},
    ]
    result = session_handoff_incomplete_today(
        missions, now=now, last_run_lookup=_last_run_lookup({})
    )
    assert result is True


def test_session_handoff_fired_earlier_today_is_complete():
    # now = 23:05 Paris; fired at 22:50 Paris same day.
    now = datetime(2026, 9, 22, 21, 5, tzinfo=timezone.utc)
    missions = [
        {"id": "session_handoff", "layer": 4, "cadence": "daily"},
    ]
    fired = {"session_handoff": datetime(2026, 9, 22, 20, 50, tzinfo=timezone.utc)}
    result = session_handoff_incomplete_today(
        missions, now=now, last_run_lookup=_last_run_lookup(fired)
    )
    assert result is False


def test_session_handoff_fired_yesterday_is_incomplete_today():
    # now = 2026-09-23 20:32 UTC; last fire was 2026-09-22 (yesterday, Paris).
    now = datetime(2026, 9, 23, 20, 32, tzinfo=timezone.utc)
    missions = [
        {"id": "session_handoff", "layer": 4, "cadence": "daily"},
    ]
    fired = {"session_handoff": datetime(2026, 9, 22, 20, 50, tzinfo=timezone.utc)}
    result = session_handoff_incomplete_today(
        missions, now=now, last_run_lookup=_last_run_lookup(fired)
    )
    assert result is True


def test_session_handoff_fired_late_last_night_incomplete_after_paris_midnight():
    # Fired 2026-09-22 23:50 Paris (21:50 UTC). Checked 2026-09-23 00:10 Paris
    # (22:10 UTC on the 22nd, CEST) — a new Paris calendar day has begun, so
    # today's period has not fired yet.
    fired_at = datetime(2026, 9, 22, 21, 50, tzinfo=timezone.utc)
    now = datetime(2026, 9, 22, 22, 10, tzinfo=timezone.utc)
    missions = [
        {"id": "session_handoff", "layer": 4, "cadence": "daily"},
    ]
    result = session_handoff_incomplete_today(
        missions, now=now, last_run_lookup=_last_run_lookup({"session_handoff": fired_at})
    )
    assert result is True


def test_non_daily_cadence_returns_false():
    now = datetime(2026, 9, 22, 20, 32, tzinfo=timezone.utc)
    missions = [
        {"id": "session_handoff", "layer": 4, "cadence": "weekly", "day": "monday"},
    ]
    result = session_handoff_incomplete_today(
        missions, now=now, last_run_lookup=_last_run_lookup({})
    )
    assert result is False


def test_only_session_handoff_id_is_consulted():
    now = datetime(2026, 9, 22, 20, 32, tzinfo=timezone.utc)
    missions = [
        {"id": "market_wrapup", "layer": 4, "cadence": "daily", "time": "22:30"},
        {"id": "session_handoff", "layer": 4, "cadence": "daily"},
    ]
    # market_wrapup fired today; session_handoff never did.
    fired = {"market_wrapup": datetime(2026, 9, 22, 19, 0, tzinfo=timezone.utc)}
    result = session_handoff_incomplete_today(
        missions, now=now, last_run_lookup=_last_run_lookup(fired)
    )
    assert result is True
