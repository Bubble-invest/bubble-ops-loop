"""Tests for #1316 — a stale-claim detector, plus inert-lease purge for
missions the dispatcher now skips (status != live).

CONFIRMED LIVE INCIDENT (Rick's own dept, 2026-09-14): 6 of 8 missions were
claimed in the SAME SECOND at 23:14:18Z and held PENDING for ~4 hours with no
completion, while the dispatcher delivered two OTHER missions nine times in
ninety minutes without ever mentioning the six. "Claimed" was standing in for
"running" — a pending lease correctly prevents duplicate delivery while work
MAY be in flight, but nothing distinguished "claimed and being worked" from
"claimed and never delivered" until the lease finally expired on its own.

THE FIX (two independent, additive pieces — neither changes WHICH missions
get dispatched or WHEN):

  1. `due_mission_plan(..., stale=[])`: a live mission's own unexpired pending
     lease, held for more than `_STALE_CLAIM_FRACTION` (0.5) of its own lease
     window with no completion, is appended to `stale` instead of being
     silently `continue`d past. `claim_due_missions` threads this through.
     `due_missions.py`'s `_emit_stale_notice` prints ONE stderr line so it
     surfaces in the tick's own output — never silently waited out.

  2. `_purge_inert_leases` (called from `claim_due_missions`, the one
     write-capable call site): a mission `due_mission_plan` reports as
     `skipped` (non-live) has any pending lease it might still be holding
     removed. Per Rick's own follow-up note on #1316: once #1317 made the
     status filter run BEFORE the lease check, a lease on a now-non-live
     mission can never complete (nothing dispatches it) NOR self-clear
     (#1317's filter skips it before the release logic ever runs) — it just
     sits in the watermark file forever. Pure hygiene: the #1317 filter
     already makes such a lease irrelevant to dispatch; this only stops it
     from accumulating as cruft.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from scripts.lib.loop_backup import (
    _STALE_CLAIM_FRACTION,
    claim_due_missions,
    due_mission_plan,
    read_due_watermarks,
)

# 16:00 UTC on a Monday, well clear of the Paris-local midnight/week boundary
# so subtracting up to a full lease window never crosses into a different
# ISO week (which would change `period` and invalidate the "same claim,
# later tick" test setup below).
NOW = dt.datetime(2026, 9, 14, 16, 0, 0, tzinfo=dt.timezone.utc)
WEEKLY_DUE = {"policy": "calendar_period", "timezone": "Europe/Paris"}
LEASE_SECONDS = 4 * 3600  # 4h, matches the confirmed incident's own ~4h lease


def manifest(*missions):
    return {
        "loop": {
            "due_dispatch": {
                "mission_ids": [mission["id"] for mission in missions],
                "watermark": "monitoring/due-mission-watermarks.json",
            }
        },
        "recurring_missions": list(missions),
    }


def mission(mission_id, cadence, due, status="live"):
    item = {
        "id": mission_id, "layer": 1, "cadence": cadence, "due": due,
        "mission_file": f"missions/{mission_id}.md",
    }
    if status is not None:
        item["status"] = status
    return item


def _pending_watermark(mission_id: str, period: str, claimed_at: dt.datetime,
                        lease_seconds: int) -> dict:
    return {
        "version": 1,
        "missions": {
            mission_id: {
                "pending": {
                    "period": period,
                    "claim_id": "abc123",
                    "claimed_at": claimed_at.astimezone(dt.timezone.utc).isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "expires_at_epoch": int(claimed_at.timestamp()) + lease_seconds,
                }
            }
        },
    }


# ===========================================================================
# 1. Stale-claim detector — due_mission_plan(stale=...)
# ===========================================================================

def test_claim_under_half_its_lease_is_not_yet_stale():
    data = manifest(mission("security_guard", "weekly", WEEKLY_DUE))
    claimed_at = NOW - dt.timedelta(seconds=int(LEASE_SECONDS * 0.4))  # 40% elapsed
    watermarks = _pending_watermark("security_guard", "2026-W38", claimed_at, LEASE_SECONDS)

    stale: list = []
    plan = due_mission_plan(data, watermarks, NOW, stale=stale)

    assert plan == [], "the lease is still held and unexpired — mission stays excluded"
    assert stale == [], "40% of the lease elapsed — below the 50% staleness threshold"


def test_claim_past_half_its_lease_with_no_completion_is_surfaced_as_stale():
    """THE #1316 REPRO: a live mission's claim held well past half its lease,
    no completion recorded — exactly the confirmed incident shape (6
    missions, 4h into their lease, no delivery)."""
    data = manifest(mission("security_guard", "weekly", WEEKLY_DUE))
    claimed_at = NOW - dt.timedelta(seconds=int(LEASE_SECONDS * 0.75))  # 75% elapsed
    watermarks = _pending_watermark("security_guard", "2026-W38", claimed_at, LEASE_SECONDS)

    stale: list = []
    plan = due_mission_plan(data, watermarks, NOW, stale=stale)

    assert plan == [], (
        "the lease is not yet EXPIRED — the mission must still not be "
        "re-dispatched (no duplicate delivery); staleness is surfaced "
        "ALONGSIDE that, not instead of it"
    )
    assert [s["id"] for s in stale] == ["security_guard"]
    assert stale[0]["age_fraction"] == pytest.approx(0.75, abs=0.02)


def test_stale_detection_is_opt_in_and_never_raises_on_malformed_claimed_at():
    """Omitting `stale` (existing callers) changes nothing. A malformed/missing
    `claimed_at` must never raise — it's an observability signal, and the
    inverted failure (crashing the plan over a cosmetic field) is worse than
    just not reporting staleness for that one entry."""
    data = manifest(mission("m", "weekly", WEEKLY_DUE))
    claimed_at = NOW - dt.timedelta(seconds=int(LEASE_SECONDS * 0.9))
    watermarks = _pending_watermark("m", "2026-W38", claimed_at, LEASE_SECONDS)

    assert due_mission_plan(data, watermarks, NOW) == []  # no `stale` kwarg — no crash

    watermarks["missions"]["m"]["pending"]["claimed_at"] = "not-a-timestamp"
    stale: list = []
    assert due_mission_plan(data, watermarks, NOW, stale=stale) == []
    assert stale == [], "an unparseable claimed_at must not be reported as stale, and must not raise"


def test_claim_due_missions_surfaces_stale_through_the_write_path(tmp_path: Path):
    """The claim path (the one that actually holds the write lock) threads
    `stale` through identically to the read-only plan path."""
    data = manifest(mission("security_guard", "weekly", WEEKLY_DUE))
    path = tmp_path / "monitoring" / "due.json"

    # First claim at NOW - creates the pending lease.
    claim_due_missions(path, data, NOW - dt.timedelta(seconds=int(LEASE_SECONDS * 0.9)), LEASE_SECONDS)

    stale: list = []
    plan, claims = claim_due_missions(path, data, NOW, LEASE_SECONDS, stale=stale)

    assert plan == []
    assert claims == {}
    assert [s["id"] for s in stale] == ["security_guard"]


def test_a_planned_mission_never_reported_stale_it_is_skipped_first():
    """Non-live missions never reach the pending-lease check at all (#1317's
    filter runs first) — even if they somehow carried a stale-shaped pending
    lease, they must appear in `skipped`, never `stale`."""
    data = manifest(mission("planned_weekly", "weekly", WEEKLY_DUE, status="planned"))
    claimed_at = NOW - dt.timedelta(seconds=int(LEASE_SECONDS * 0.9))
    watermarks = _pending_watermark("planned_weekly", "2026-W38", claimed_at, LEASE_SECONDS)

    skipped: list = []
    stale: list = []
    plan = due_mission_plan(data, watermarks, NOW, skipped=skipped, stale=stale)

    assert plan == []
    assert [s["id"] for s in skipped] == ["planned_weekly"]
    assert stale == []


def test_stale_notice_is_a_single_stderr_line(capsys):
    from scripts.due_missions import _emit_stale_notice

    _emit_stale_notice([
        {"id": "security_guard", "period": "2026-W38", "claimed_at": "2026-09-13T23:14:18Z",
         "expires_at_epoch": 0, "age_fraction": 0.9},
        {"id": "fleet_hygiene", "period": "2026-W38", "claimed_at": "2026-09-13T23:14:18Z",
         "expires_at_epoch": 0, "age_fraction": 0.75},
    ])
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "2 stale claim" in err
    assert "security_guard" in err and "fleet_hygiene" in err

    _emit_stale_notice([])
    assert capsys.readouterr().err == ""


# ===========================================================================
# 2. Inert-lease purge for missions the dispatcher now skips (status != live)
# ===========================================================================

def test_purges_a_lease_left_behind_when_a_mission_flips_to_planned(tmp_path: Path):
    """The exact scenario Rick flagged as a #1316 follow-up: a mission is
    claimed while `status: live`, then flipped to `status: planned` (e.g. a
    dept realizing the mission isn't actually built yet, mirroring #1317's
    own root cause) BEFORE it ever completes. #1317 already stops it from
    being re-claimed or dispatched; #1316 additionally clears the now-inert
    lease instead of leaving it in the watermark file forever."""
    path = tmp_path / "monitoring" / "due.json"
    live = manifest(mission("follow_through", "weekly", WEEKLY_DUE))
    claim_due_missions(path, live, NOW, LEASE_SECONDS)
    before = read_due_watermarks(path)
    assert "pending" in before["missions"]["follow_through"]

    # The mission is now discovered to be unbuilt and flipped to planned.
    now_planned = manifest(mission("follow_through", "weekly", WEEKLY_DUE, status="planned"))
    skipped: list = []
    plan, claims = claim_due_missions(
        path, now_planned, NOW + dt.timedelta(minutes=5), LEASE_SECONDS, skipped=skipped
    )

    assert plan == []
    assert claims == {}
    assert [s["id"] for s in skipped] == ["follow_through"]
    after = read_due_watermarks(path)
    assert "follow_through" not in after["missions"], (
        "the inert lease for a now-non-live mission must be purged, not left "
        "as permanent cruft in the watermark file"
    )


def test_purge_does_not_touch_a_live_missions_genuine_in_flight_lease(tmp_path: Path):
    """No overreach: purging is scoped exactly to missions `due_mission_plan`
    reports as skipped this tick — a live mission's own unexpired lease must
    survive untouched (dedupe is not regressed)."""
    path = tmp_path / "monitoring" / "due.json"
    data = manifest(
        mission("live_weekly", "weekly", WEEKLY_DUE),
        mission("planned_weekly", "weekly", WEEKLY_DUE, status="planned"),
    )
    claim_due_missions(path, data, NOW, LEASE_SECONDS)
    plan, claims = claim_due_missions(path, data, NOW + dt.timedelta(seconds=901), LEASE_SECONDS)

    assert plan == []  # live_weekly still held by its own genuine lease
    assert claims == {}
    state = read_due_watermarks(path)
    assert "pending" in state["missions"]["live_weekly"], (
        "a live mission's own in-flight lease must not be purged"
    )
    assert "planned_weekly" not in state["missions"]


def test_purge_is_a_no_op_when_nothing_is_skipped(tmp_path: Path):
    """Baseline: an all-live manifest with no skipped missions never triggers
    an extra write purely for the purge check."""
    path = tmp_path / "monitoring" / "due.json"
    data = manifest(mission("live_weekly", "weekly", WEEKLY_DUE))
    plan, claims = claim_due_missions(path, data, NOW, LEASE_SECONDS)
    assert set(claims) == {"live_weekly"}
    state = read_due_watermarks(path)
    assert "pending" in state["missions"]["live_weekly"]
