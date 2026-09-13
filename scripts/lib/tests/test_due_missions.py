import datetime as dt
from pathlib import Path

import pytest

from scripts.lib.loop_backup import (
    DueMissionConfigError,
    due_mission_plan,
    due_period,
    read_due_watermarks,
    write_due_success,
)


NOW = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)


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


def mission(mission_id, cadence, due):
    return {
        "id": mission_id,
        "layer": 1,
        "cadence": cadence,
        "due": due,
        "mission_file": f"missions/{mission_id}.md",
    }


def test_calendar_periods_use_the_declared_timezone():
    # 22:30 UTC is already the next calendar day in Paris in September.
    boundary = dt.datetime(2026, 9, 13, 22, 30, tzinfo=dt.timezone.utc)
    assert due_period("daily", boundary, "Europe/Paris") == "2026-09-14"
    assert due_period("weekly", NOW, "Europe/Paris") == "2026-W37"
    assert due_period("monthly", NOW, "Europe/Paris") == "2026-09"
    assert due_period("continuous", NOW) == "continuous"


def test_periodic_missions_catch_up_after_sleep_and_continuous_stays_due():
    data = manifest(
        mission("board", "continuous", {"policy": "every_tick"}),
        mission("daily_scan", "daily", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
        mission("weekly_scan", "weekly", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
        mission("monthly_scan", "monthly", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
    )
    stale = {
        "version": 1,
        "missions": {
            "board": {"last_success_period": "continuous"},
            "daily_scan": {"last_success_period": "2026-09-01"},
            "weekly_scan": {"last_success_period": "2026-W20"},
            "monthly_scan": {"last_success_period": "2026-08"},
        },
    }
    plan = due_mission_plan(data, stale, NOW)
    assert [item["id"] for item in plan] == ["board", "daily_scan", "weekly_scan", "monthly_scan"]


def test_same_period_success_suppresses_periodic_but_not_continuous():
    data = manifest(
        mission("board", "continuous", {"policy": "every_tick"}),
        mission("daily_scan", "daily", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
        mission("weekly_scan", "weekly", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
        mission("monthly_scan", "monthly", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
    )
    current = {
        "version": 1,
        "missions": {
            "board": {"last_success_period": "continuous"},
            "daily_scan": {"last_success_period": "2026-09-13"},
            "weekly_scan": {"last_success_period": "2026-W37"},
            "monthly_scan": {"last_success_period": "2026-09"},
        },
    }
    assert [item["id"] for item in due_mission_plan(data, current, NOW)] == ["board"]


def test_next_calendar_period_makes_a_successful_mission_due_again():
    data = manifest(
        mission("weekly_scan", "weekly", {"policy": "calendar_period", "timezone": "Europe/Paris"})
    )
    current = {
        "version": 1,
        "missions": {"weekly_scan": {"last_success_period": "2026-W37"}},
    }
    next_week = NOW + dt.timedelta(days=7)
    assert due_mission_plan(data, current, NOW) == []
    assert [item["id"] for item in due_mission_plan(data, current, next_week)] == ["weekly_scan"]


@pytest.mark.parametrize(
    "data,error",
    [
        (
            {
                "loop": {"due_dispatch": {"mission_ids": ["missing"], "watermark": "state.json"}},
                "recurring_missions": [],
            },
            "scoped mission is missing",
        ),
        (manifest(mission("no_rule", "weekly", None)), "missing due rule"),
        (
            manifest(mission("bad", "hourly", {"policy": "calendar_period", "timezone": "UTC"})),
            "unsupported cadence",
        ),
        (
            manifest(mission("bad", "weekly", {"policy": "every_tick"})),
            "requires due.policy=calendar_period",
        ),
    ],
)
def test_invalid_or_missing_scoped_rules_fail_closed(data, error):
    with pytest.raises(DueMissionConfigError, match=error):
        due_mission_plan(data, {"version": 1, "missions": {}}, NOW)


def test_explicit_success_write_is_atomic_and_idempotent(tmp_path: Path):
    path = tmp_path / "monitoring" / "due.json"
    first = write_due_success(path, "weekly_scan", "2026-W37", NOW)
    second = write_due_success(path, "weekly_scan", "2026-W37", NOW + dt.timedelta(minutes=1))
    assert first["missions"]["weekly_scan"]["last_success_period"] == "2026-W37"
    assert second["missions"]["weekly_scan"]["last_success_period"] == "2026-W37"
    assert read_due_watermarks(path) == second
    assert path.stat().st_mode & 0o777 == 0o600
