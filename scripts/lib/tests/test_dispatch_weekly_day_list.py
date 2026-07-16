"""
test_dispatch_weekly_day_list.py — regression tests for the weekly day-list
"once per ISO week" veto bug (Miranda draft_linkedin, 2x/week).

Context: `day:` on a weekly mission may be a single string ("monday") or a
list (["monday", "thursday"]) — dispatch_helpers.is_mission_due() already
normalises both shapes for the weekday-membership check (fix #214, covered
in test_loop_dispatch_layer1.py). But the SEPARATE "already fired" veto used
same-ISO-year+week equality, so a day:[monday, thursday] mission fired on
Monday and was then vetoed on Thursday — same ISO week, veto tripped even
though Thursday is a distinct listed day the mission never actually ran on.
The list-day feature was therefore inert: a 2-day weekly fired once/week,
identical to a 1-day weekly.

The fix replaces the ISO-week veto with a same-Paris-date veto. This is
provably equivalent for the single-day case (the weekday-membership check
already excludes every other day of the week, so a same-date veto and a
same-week veto both cap it at once/week) and additionally correct for the
list case (fires once per listed day, not once per week).
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.lib.dispatch_helpers import is_mission_due

# 2026-06-15 Monday, 2026-06-16 Tuesday, 2026-06-18 Thursday, 2026-06-19
# Friday — all ISO week 25, matching the fixture dates already used in
# test_loop_dispatch_layer1.py's list-day (#214) tests.
_MONDAY_0800 = datetime(2026, 6, 15, 6, 0, tzinfo=timezone.utc)   # 08:00 Paris
_MONDAY_2200 = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)  # 22:00 Paris
_THURSDAY_0800 = datetime(2026, 6, 18, 6, 0, tzinfo=timezone.utc)  # 08:00 Paris
_FRIDAY_0800 = datetime(2026, 6, 19, 6, 0, tzinfo=timezone.utc)   # 08:00 Paris

_LIST_MISSION = {
    "id": "draft_linkedin",
    "cadence": "weekly",
    "time": "07:00",
    "day": ["monday", "thursday"],
}


def test_day_list_fires_on_first_listed_day():
    """A day:[monday, thursday] mission fires on Monday when never fired before."""
    assert is_mission_due(_LIST_MISSION, now=_MONDAY_0800, last_fired=None) is True


def test_day_list_fires_again_on_second_listed_day_same_week():
    """THE BUG: after firing Monday, the mission must ALSO fire Thursday —
    same ISO week, but a distinct listed day. The old same-ISO-week veto
    incorrectly vetoed this; the same-Paris-date veto does not.
    """
    assert is_mission_due(
        _LIST_MISSION, now=_THURSDAY_0800, last_fired=_MONDAY_0800
    ) is True, (
        "day:[monday, thursday] must fire on Thursday even though it already "
        "fired Monday in the same ISO week — the list-day feature was inert "
        "under the old same-ISO-week veto"
    )


def test_day_list_does_not_refire_twice_same_day():
    """A re-check later the same Monday (after already firing that morning)
    must NOT fire again."""
    assert is_mission_due(
        _LIST_MISSION, now=_MONDAY_2200, last_fired=_MONDAY_0800
    ) is False


def test_single_day_weekly_still_fires_once_not_again_later_that_week():
    """Regression: a single-day weekly (day:'friday') must still fire at
    most once per week — the same-Paris-date veto relies on the existing
    weekday-membership check to exclude every other day of the week, so
    re-checking on Friday afternoon (same date, already fired that morning)
    must not re-fire, and there is no other day that week it could fire on.
    """
    mission = {"id": "m", "cadence": "weekly", "time": "07:00", "day": "friday"}
    fired_friday_morning = _FRIDAY_0800
    later_friday_same_day = datetime(2026, 6, 19, 20, 0, tzinfo=timezone.utc)  # 22:00 Paris
    assert is_mission_due(
        mission, now=later_friday_same_day, last_fired=fired_friday_morning
    ) is False


def test_invalid_or_empty_day_returns_false():
    """An empty day list still returns False regardless of last_fired."""
    mission = {"id": "m", "cadence": "weekly", "time": "07:00", "day": []}
    assert is_mission_due(mission, now=_MONDAY_0800, last_fired=None) is False
    assert is_mission_due(mission, now=_MONDAY_0800, last_fired=_THURSDAY_0800) is False
