import datetime as dt
import json
import os
from pathlib import Path

import pytest

from scripts.lib.loop_backup import (
    _MISSION_ID_RE,
    DueMissionConfigError,
    claim_due_missions,
    due_mission_plan,
    due_period,
    read_due_watermarks,
    release_due_claims,
    write_due_success,
)


NOW = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def friday_weekly_dept(tmp_path: Path) -> Path:
    """Synthetic #1696 shape: morning L1 done, weekly uses a layer shim.

    Last Friday only materialized at 18:11:05 CEST; there is no weekly
    completion. No client data or dependency on the copied evidence files.
    """
    import yaml

    missions = [
        {"id": "daily_ledger_cash_sync", "layer": 1, "cadence": "daily", "time": "07:00"},
        {"id": "weekly_timesheet_collection", "layer": 1, "cadence": "weekly",
         "day": "friday", "time": "18:00", "output_queue": "queues/research/",
         "creates": ["timesheet_collection"]},
    ]
    (tmp_path / "dept.yaml").write_text(yaml.safe_dump({"recurring_missions": missions}))
    prompt = tmp_path / "layers/1/PROMPT.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("synthetic layer prompt\n")
    prior = dt.datetime(2026, 9, 25, 16, 11, 5, tzinfo=dt.timezone.utc)
    marker = tmp_path / "outputs/2026-09-25/missions/weekly_timesheet_collection/.last-materialized"
    marker.parent.mkdir(parents=True)
    marker.write_text(prior.isoformat())
    os.utime(marker, (prior.timestamp(), prior.timestamp()))
    today = tmp_path / "outputs/2026-10-02"
    (today / "1").mkdir(parents=True)
    (today / "1/summary.md").write_text("synthetic morning output\n")
    (today / "dispatch.json").write_text(json.dumps({
        "daily_ledger_cash_sync": {
            "dispatched_at": "2026-10-02T07:16:02+00:00",
            "completed_at": "2026-10-02T08:16:57+00:00",
            "materialized_at": "2026-10-02T07:16:02+00:00",
            "artifacts": ["outputs/2026-10-02/1/summary.md"],
        },
    }))
    return tmp_path


@pytest.mark.parametrize("instant,due,reason", [
    ("2026-10-02T16:07:00+00:00", True, "due"),  # Friday 18:07 CEST
    ("2026-10-01T16:07:00+00:00", False, "wrong_day"),
    ("2026-10-02T15:59:00+00:00", False, "not_yet_time"),
])
def test_1696_weekly_plan_local_day_time_and_previous_materialization(
    friday_weekly_dept: Path, capsys, instant, due, reason
):
    from scripts.due_missions import command_plan, parser

    epoch = int(dt.datetime.fromisoformat(instant).timestamp())
    args = parser().parse_args([
        "plan", "--dept-dir", str(friday_weekly_dept), "--now-epoch", str(epoch),
        "--format", "json",
    ])
    before = {p.relative_to(friday_weekly_dept): p.read_bytes()
              for p in friday_weekly_dept.rglob("*") if p.is_file()}
    assert command_plan(args) == 0
    output = json.loads(capsys.readouterr().out)
    decisions = {m["id"]: m for m in output["missions"]}
    assert set(decisions) == {"daily_ledger_cash_sync", "weekly_timesheet_collection"}
    assert decisions["weekly_timesheet_collection"] == {
        "id": "weekly_timesheet_collection", "due": due, "reason": reason,
    }
    assert ("weekly_timesheet_collection" in {m["id"] for m in output["due"]}) == due
    assert before == {p.relative_to(friday_weekly_dept): p.read_bytes()
                      for p in friday_weekly_dept.rglob("*") if p.is_file()}


def test_1696_same_week_completion_suppresses_then_next_friday_is_due(
    friday_weekly_dept: Path, capsys
):
    import yaml
    from scripts.due_missions import command_plan, command_wake_prompt, parser, _wake_prompt_recurring
    from scripts.lib.dispatch_helpers import commit_dispatch

    data = yaml.safe_load((friday_weekly_dept / "dept.yaml").read_text())
    weekly = data["recurring_missions"][1]
    now = dt.datetime(2026, 10, 2, 16, 7, tzinfo=dt.timezone.utc)
    args = parser().parse_args([
        "wake-prompt", "--dept-dir", str(friday_weekly_dept), "--now-epoch", str(int(now.timestamp())),
    ])
    assert command_wake_prompt(args) == 0
    # Pin the existing renderer: only the selected mission data changes.
    assert capsys.readouterr().out.rstrip("\n") == _wake_prompt_recurring(
        [weekly], friday_weekly_dept.resolve(), "the dept's"
    )
    artifact = friday_weekly_dept / "outputs/2026-10-02/1/summary.md"
    assert commit_dispatch(friday_weekly_dept, weekly, dispatched_at=now,
                           completed_at=now + dt.timedelta(minutes=1),
                           artifacts=[artifact], materialize_outputs=False)
    for later, due in [(now + dt.timedelta(minutes=3), False), (now + dt.timedelta(days=7), True)]:
        args = parser().parse_args([
            "plan", "--dept-dir", str(friday_weekly_dept), "--format", "json",
            "--now-epoch", str(int(later.timestamp())),
        ])
        assert command_plan(args) == 0
        decision = next(m for m in json.loads(capsys.readouterr().out)["missions"]
                        if m["id"] == weekly["id"])
        assert decision == {"id": weekly["id"], "due": due,
                            "reason": "due" if due else "already_completed_this_period"}


def test_1696_daily_shim_still_obeys_layer_cycle_gate(friday_weekly_dept: Path, capsys):
    import yaml
    from scripts.due_missions import command_plan, parser

    path = friday_weekly_dept / "dept.yaml"
    data = yaml.safe_load(path.read_text())
    data["recurring_missions"][1]["cadence"] = "daily"
    path.write_text(yaml.safe_dump(data))
    args = parser().parse_args([
        "plan", "--dept-dir", str(friday_weekly_dept), "--format", "json",
        "--now-epoch", str(int(dt.datetime(2026, 10, 2, 16, 7, tzinfo=dt.timezone.utc).timestamp())),
    ])
    assert command_plan(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["due"] == []
    assert output["missions"][1]["reason"] == "layer_closed"


def test_1696_weekly_catchup_does_not_admit_other_daily_shims(friday_weekly_dept: Path):
    import yaml
    from scripts.due_missions import _plan_for_wake_recurring

    path = friday_weekly_dept / "dept.yaml"
    data = yaml.safe_load(path.read_text())
    data["recurring_missions"].append({
        "id": "late_daily", "layer": 1, "cadence": "daily", "time": "18:00",
    })
    path.write_text(yaml.safe_dump(data))
    now = dt.datetime(2026, 10, 2, 16, 7, tzinfo=dt.timezone.utc)
    assert [m["id"] for m in _plan_for_wake_recurring(
        friday_weekly_dept, data, int(now.timestamp())
    )] == ["weekly_timesheet_collection"]


def test_1696_weekly_consumer_keeps_input_and_layer_gates(friday_weekly_dept: Path, capsys):
    import yaml
    from scripts.due_missions import command_plan, parser

    path = friday_weekly_dept / "dept.yaml"
    data = yaml.safe_load(path.read_text())
    data["recurring_missions"][1]["input_queue"] = "queues/research/"
    path.write_text(yaml.safe_dump(data))
    args = parser().parse_args([
        "plan", "--dept-dir", str(friday_weekly_dept), "--format", "json",
        "--now-epoch", str(int(dt.datetime(2026, 10, 2, 16, 7, tzinfo=dt.timezone.utc).timestamp())),
    ])
    assert command_plan(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["due"] == []
    assert result["missions"][1]["reason"] == "input_not_ready"


def test_plan_json_explains_due_dispatch_scope_completion_and_pending(tmp_path: Path, capsys):
    import yaml
    from scripts.due_missions import command_plan, parser

    data = manifest(
        mission("completed", "weekly", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
        mission("pending", "daily", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
        mission("board", "continuous", {"policy": "every_tick"}),
        mission("planned", "weekly", None, status="planned"),
        mission("old_pending", "weekly", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
    )
    data["recurring_missions"].append(mission("unscoped", "monthly", None))
    data["layers"] = {"subscribed": [1]}
    (tmp_path / "dept.yaml").write_text(yaml.safe_dump(data))
    for mid in ("completed", "pending", "board", "old_pending"):
        path = tmp_path / f"missions/{mid}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text("synthetic mission\n")
    path = tmp_path / "layers/1/PROMPT.md"
    path.parent.mkdir(parents=True)
    path.write_text("synthetic layer\n")
    state = tmp_path / data["loop"]["due_dispatch"]["watermark"]
    state.parent.mkdir(parents=True)
    pending = {"period": "2026-09-13", "claim_id": "synthetic", "expires_at_epoch": int(NOW.timestamp()) + 3600}
    state.write_text(json.dumps({"version": 1, "missions": {
        "completed": {"last_success_period": "2026-W37"},
        "pending": {"pending": pending},
        "old_pending": {"pending": {**pending, "period": "2026-W36"}},
    }}))
    args = parser().parse_args([
        "plan", "--dept-dir", str(tmp_path), "--format", "json",
        "--now-epoch", str(int(NOW.timestamp())),
    ])
    assert command_plan(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert {m["id"]: (m["due"], m["reason"]) for m in result["missions"]} == {
        "completed": (False, "already_completed_this_period"),
        "pending": (False, "pending_lease"),
        "board": (True, "due"),
        "planned": (False, "not_live"),
        "old_pending": (True, "due"),
        "unscoped": (False, "out_of_scope"),
    }


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
    # #1317: only a mission whose status is exactly "live" is dispatchable, so
    # the default here is "live" (these fixtures assert on dispatched work).
    # Pass status="planned" (or omit-via-None) to exercise the skip filter.
    item = {
        "id": mission_id,
        "layer": 1,
        "cadence": cadence,
        "due": due,
        "mission_file": f"missions/{mission_id}.md",
    }
    if status is not None:
        item["status"] = status
    return item


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


def test_pending_claim_suppresses_duplicate_until_expiry(tmp_path: Path):
    data = manifest(
        mission("board", "continuous", {"policy": "every_tick"}),
        mission("weekly_scan", "weekly", {"policy": "calendar_period", "timezone": "Europe/Paris"}),
    )
    path = tmp_path / "monitoring" / "due.json"
    first, claims = claim_due_missions(path, data, NOW, 21600)
    second, second_claims = claim_due_missions(path, data, NOW + dt.timedelta(seconds=901), 21600)
    after_expiry, retry_claims = claim_due_missions(
        path, data, NOW + dt.timedelta(seconds=21601), 21600
    )

    assert [item["id"] for item in first] == ["board", "weekly_scan"]
    assert set(claims) == {"weekly_scan"}
    assert [item["id"] for item in second] == ["board"]
    assert second_claims == {}
    assert [item["id"] for item in after_expiry] == ["board", "weekly_scan"]
    assert set(retry_claims) == {"weekly_scan"}
    completed = write_due_success(path, "weekly_scan", "2026-W37", NOW + dt.timedelta(seconds=21602))
    assert "pending" not in completed["missions"]["weekly_scan"]
    assert completed["missions"]["weekly_scan"]["last_success_period"] == "2026-W37"


def test_release_removes_only_the_callers_claim(tmp_path: Path):
    data = manifest(
        mission("weekly_scan", "weekly", {"policy": "calendar_period", "timezone": "Europe/Paris"})
    )
    path = tmp_path / "monitoring" / "due.json"
    _, claims = claim_due_missions(path, data, NOW, 21600)
    release_due_claims(path, {"weekly_scan": "not-the-owner"})
    assert due_mission_plan(data, read_due_watermarks(path), NOW) == []
    release_due_claims(path, claims)
    assert [item["id"] for item in due_mission_plan(data, read_due_watermarks(path), NOW)] == [
        "weekly_scan"
    ]


def test_delayed_old_period_completion_preserves_new_period_claim(tmp_path: Path):
    data = manifest(
        mission("weekly_scan", "weekly", {"policy": "calendar_period", "timezone": "Europe/Paris"})
    )
    path = tmp_path / "monitoring" / "due.json"
    claim_due_missions(path, data, NOW, 21600)
    next_week = NOW + dt.timedelta(days=7)
    claim_due_missions(path, data, next_week, 21600)

    state = write_due_success(path, "weekly_scan", "2026-W37", next_week + dt.timedelta(seconds=1))
    assert state["missions"]["weekly_scan"]["last_success_period"] == "2026-W37"
    assert state["missions"]["weekly_scan"]["pending"]["period"] == "2026-W38"
    assert due_mission_plan(data, state, next_week) == []


# ── #1317: per-mission status filter (only status: live dispatches/claims) ─────

WEEKLY_DUE = {"policy": "calendar_period", "timezone": "Europe/Paris"}
EMPTY = {"version": 1, "missions": {}}


def test_planned_mission_is_neither_due_nor_claimed(tmp_path: Path):
    # The exact production shape: two live missions, one planned. Only the live
    # ones plan; the planned one is skipped AND never leased (claim path).
    data = manifest(
        mission("board", "continuous", {"policy": "every_tick"}),
        mission("live_weekly", "weekly", WEEKLY_DUE),
        mission("planned_weekly", "weekly", WEEKLY_DUE, status="planned"),
    )
    skipped: list = []
    plan = due_mission_plan(data, EMPTY, NOW, skipped=skipped)
    assert [item["id"] for item in plan] == ["board", "live_weekly"]
    assert [s["id"] for s in skipped] == ["planned_weekly"]

    path = tmp_path / "monitoring" / "due.json"
    claim_skipped: list = []
    cplan, claims = claim_due_missions(path, data, NOW, 21600, skipped=claim_skipped)
    assert [item["id"] for item in cplan] == ["board", "live_weekly"]
    assert set(claims) == {"live_weekly"}  # continuous 'board' is never leased
    assert "planned_weekly" not in claims
    assert [s["id"] for s in claim_skipped] == ["planned_weekly"]
    # A planned mission must have NO pending lease persisted against it.
    state = read_due_watermarks(path)
    assert "planned_weekly" not in state["missions"]


def test_missing_or_unknown_status_is_not_live_fail_closed():
    # Fail-closed: a missing status, or any value other than exactly "live",
    # is treated as NOT live and skipped — we never dispatch work we can't
    # confirm is live. Both are surfaced in `skipped` so they stay visible.
    data = manifest(
        mission("has_status", "weekly", WEEKLY_DUE),
        mission("no_status", "weekly", WEEKLY_DUE, status=None),
        mission("weird_status", "weekly", WEEKLY_DUE, status="active"),
    )
    skipped: list = []
    plan = due_mission_plan(data, EMPTY, NOW, skipped=skipped)
    assert [item["id"] for item in plan] == ["has_status"]
    assert {s["id"] for s in skipped} == {"no_status", "weird_status"}
    assert {s["id"]: s["status"] for s in skipped} == {
        "no_status": None,
        "weird_status": "active",
    }


def test_status_match_is_exactly_live_case_and_whitespace_sensitive():
    # "Live" and "live " are NOT "live": a mistyped status fails closed (skip)
    # and is surfaced loudly, rather than silently dispatching or — worse —
    # being assumed live.
    data = manifest(
        mission("capitalised", "weekly", WEEKLY_DUE, status="Live"),
        mission("trailing_space", "weekly", WEEKLY_DUE, status="live "),
    )
    skipped: list = []
    assert due_mission_plan(data, EMPTY, NOW, skipped=skipped) == []
    assert {s["id"] for s in skipped} == {"capitalised", "trailing_space"}


def test_flipping_planned_to_live_redispatches_and_is_claimable(tmp_path: Path):
    # Pure filter: the ONLY change from not-dispatched to dispatched is the
    # status value. Same mission, same rule.
    planned = manifest(mission("m", "weekly", WEEKLY_DUE, status="planned"))
    assert due_mission_plan(planned, EMPTY, NOW) == []

    live = manifest(mission("m", "weekly", WEEKLY_DUE, status="live"))
    assert [item["id"] for item in due_mission_plan(live, EMPTY, NOW)] == ["m"]

    path = tmp_path / "monitoring" / "due.json"
    plan, claims = claim_due_missions(path, live, NOW, 21600)
    assert [item["id"] for item in plan] == ["m"]
    assert set(claims) == {"m"}


def test_planned_mission_with_incomplete_rule_never_crashes_live_dispatch():
    # The inverted-failure guard: a not-yet-built planned mission whose due rule
    # is incomplete (would raise "missing due rule" if validated) must be
    # skipped BEFORE validation so it can never take the live missions down.
    data = manifest(
        mission("live_board", "continuous", {"policy": "every_tick"}),
        {
            "id": "planned_unbuilt",
            "layer": 1,
            "cadence": "weekly",
            "status": "planned",
            "mission_file": "missions/planned_unbuilt.md",
            # deliberately NO "due" rule — an unbuilt mission
        },
    )
    skipped: list = []
    plan = due_mission_plan(data, EMPTY, NOW, skipped=skipped)
    assert [item["id"] for item in plan] == ["live_board"]
    assert [s["id"] for s in skipped] == ["planned_unbuilt"]


def test_live_pending_lease_not_redispatched_alongside_planned(tmp_path: Path):
    # Dedupe not regressed: a live mission with a genuine in-flight lease is
    # still suppressed until expiry, even while a planned sibling is skipped.
    data = manifest(
        mission("board", "continuous", {"policy": "every_tick"}),
        mission("live_weekly", "weekly", WEEKLY_DUE),
        mission("planned_weekly", "weekly", WEEKLY_DUE, status="planned"),
    )
    path = tmp_path / "monitoring" / "due.json"
    first, claims = claim_due_missions(path, data, NOW, 21600)
    assert [item["id"] for item in first] == ["board", "live_weekly"]
    assert set(claims) == {"live_weekly"}

    second, second_claims = claim_due_missions(
        path, data, NOW + dt.timedelta(seconds=901), 21600
    )
    assert [item["id"] for item in second] == ["board"]  # live_weekly held by lease
    assert second_claims == {}

    after_expiry, retry = claim_due_missions(
        path, data, NOW + dt.timedelta(seconds=21601), 21600
    )
    assert [item["id"] for item in after_expiry] == ["board", "live_weekly"]
    assert set(retry) == {"live_weekly"}
    # The planned mission never appears and is never leased across the cycle.
    assert "planned_weekly" not in read_due_watermarks(path)["missions"]


def test_skip_notice_is_a_single_stderr_line(capsys):
    from scripts.due_missions import _emit_skip_notice

    _emit_skip_notice(
        [{"id": "a", "status": "planned"}, {"id": "b", "status": None}]
    )
    err = capsys.readouterr().err
    assert err.count("\n") == 1  # ONE line, not one per mission
    assert "skipped 2 non-live" in err
    assert "a" in err and "b" in err

    _emit_skip_notice([])  # nothing skipped → no notice at all
    assert capsys.readouterr().err == ""


def test_completion_command_is_pinned_to_sys_executable_not_bare_python3():
    # Board #1330: a hardcoded "python3" in the generated completion command
    # resolves non-deterministically on a Mac with more than one python3 on
    # PATH — one has pyyaml, the other doesn't, and landing on the wrong one
    # crashes `due_missions.py complete` on `import yaml` (silently, from the
    # tick's point of view). sys.executable is the interpreter that is
    # ACTUALLY running this process right now (so it already has pyyaml, by
    # construction) and is an absolute path — no further PATH lookup, so it
    # can never flip between invocations.
    import sys

    from scripts.due_missions import _prompt

    plan = [
        {
            "id": "weekly_scan",
            "cadence": "weekly",
            "period": "2026-W37",
            "layers": [4],
            "mission_file": "missions/weekly-scan.md",
        }
    ]
    prompt = _prompt(plan, Path("/tmp/dept"))
    assert f"COMPLETE weekly_scan => {sys.executable} " in prompt
    assert "=> python3 " not in prompt
    assert sys.executable.startswith("/")  # never a bare, PATH-resolved name


# ── wake-prompt (#1484: the self-wake CronCreate prompt is GENERATED, never
# agent-authored free text — see #1483's audit of Ben's uncited "be
# cost-conscious" drift) ──────────────────────────────────────────────────────


@pytest.mark.parametrize("schema", ["mac", "recurring"])
@pytest.mark.parametrize("wake", [False, True], ids=["tick", "wake"])
@pytest.mark.parametrize("layers", [(), (1,), (2,), (3,), (4,), (2, 3, 4), (4, 1, 2), (1, 1)])
def test_room_check_clause_only_for_plans_containing_layer_one(schema, wake, layers):
    from scripts import due_missions

    plan = [
        {
            "id": f"mission_{index}",
            "cadence": "daily",
            # Mac missions can attach several layers; L1 need not be first.
            **({"period": "2026-09-13", "layers": [4, layer]} if schema == "mac"
               else {"layer": layer}),
            "mission_file": f"missions/mission_{index}.md",
        }
        for index, layer in enumerate(layers)
    ]
    name = "_wake_prompt" if wake else "_prompt"
    if schema == "recurring":
        name += "_recurring"
    prompt = getattr(due_missions, name)(plan, Path("/tmp/dept"), "Maya's")
    assert prompt.count(due_missions.ROOM_CHECK_CLAUSE) == (1 if 1 in layers else 0)
    assert prompt.count("ROOM CHECK") == (1 if 1 in layers else 0)
    assert prompt == getattr(due_missions, name)(plan, Path("/tmp/dept"), "Maya's")


def test_room_check_clause_is_byte_identical_across_depts_and_prompt_variants():
    from scripts import due_missions

    mission = {"id": "morning", "cadence": "daily", "mission_file": "missions/morning.md"}
    for slug, label in (("rnd", "Rick's"), ("maya", "Maya's"), ("ben", "Ben's")):
        for name in ("_prompt", "_wake_prompt", "_prompt_recurring", "_wake_prompt_recurring"):
            fields = {"layer": 1} if name.endswith("_recurring") else {
                "period": "2026-09-13", "layers": [1],
            }
            plan = [{**mission, **fields}]
            prompt = getattr(due_missions, name)(plan, Path("/tmp") / slug, label)
            start = prompt.index(" ROOM CHECK")
            clause = prompt[start:start + len(due_missions.ROOM_CHECK_CLAUSE)]
            assert clause.encode("utf-8") == due_missions.ROOM_CHECK_CLAUSE.encode("utf-8")
            assert prompt.count(due_missions.ROOM_CHECK_CLAUSE) == 1


def test_idle_envelopes_never_include_room_check():
    from scripts import due_missions

    notes = "morning(daily@07:30)"
    prompts = [due_missions._idle_prompt("Maya's", notes)]
    for renderer in (due_missions._wake_idle_prompt, due_missions._wake_idle_prompt_recurring):
        prompts.append(renderer(Path("/tmp/dept"), "Maya's", notes))
    for prompt in prompts:
        assert "DUE_MISSIONS=[]" in prompt
        assert due_missions.ROOM_CHECK_CLAUSE not in prompt
        assert "ROOM CHECK" not in prompt


def test_dept_label_reads_department_display_name_or_slug_with_generic_fallback():
    from scripts.due_missions import _dept_label

    assert _dept_label({"department": {"display_name": "Rick", "slug": "rnd"}}) == "Rick's"
    assert _dept_label({"department": {"slug": "maya"}}) == "maya's"
    assert _dept_label({}) == "the dept's"
    assert _dept_label({"department": "not-a-mapping"}) == "the dept's"


def test_wake_prompt_reuses_the_exact_floor_envelope_then_staleness_then_footer():
    from scripts.due_missions import _prompt, _staleness_clause, _wake_prompt

    plan = [
        {
            "id": "weekly_scan",
            "cadence": "weekly",
            "period": "2026-W37",
            "layers": [4],
            "mission_file": "missions/weekly-scan.md",
        }
    ]
    dept_dir = Path("/tmp/dept")
    floor_prompt = _prompt(plan, dept_dir, "maya's")
    wake = _wake_prompt(plan, dept_dir, "maya's")
    # The wake prompt is the SAME envelope the floor/backup tick already
    # renders, verbatim, then the staleness re-check clause, then the fixed
    # footer — not a re-derivation of any of the three.
    assert wake.startswith(floor_prompt)
    assert wake == floor_prompt + _staleness_clause(dept_dir) + require_wake_prompt_footer()


def test_wake_prompt_still_emits_full_envelope_with_no_due_missions():
    """`_wake_prompt` itself is a pure renderer with no gate — it is legal to
    call it with an empty plan (e.g. from a test, or a future caller with its
    own reason to). The FAIL-CLOSED refusal on an empty plan lives in the CLI
    (`command_wake_prompt`), tested separately below; this only pins that the
    renderer's own output stays well-formed either way."""
    from scripts.due_missions import _wake_prompt

    wake = _wake_prompt([], Path("/tmp/dept"), "the dept's")
    assert "DUE_MISSIONS=[]" in wake
    assert "Resume the dept's OODA loop and run one full tick now." in wake
    assert wake.endswith(require_wake_prompt_footer())


def test_wake_prompt_carries_the_handoff_pointer_and_citation_rule():
    from scripts.due_missions import _wake_prompt

    wake = _wake_prompt([], Path("/tmp/dept"), "the dept's")
    assert "WORKING_MEMORY/HANDOFF.md" in wake
    assert "must cite a msg id / tg id / dated source" in wake
    assert "never carry forward an unsourced operator-intent claim" in wake


def test_wake_prompt_carries_the_staleness_re_check_instruction():
    """#1484 PR review: the prompt is rendered at ARM time but fires hours
    later, so a mission that becomes due overnight is invisible to the
    DUE_MISSIONS list baked in at arm time. The fixed fix: instruct the agent
    to re-run the SAME generator at fire time and trust that fresh output."""
    import sys

    from scripts.due_missions import _wake_prompt

    dept_dir = Path("/tmp/dept")
    wake = _wake_prompt([], dept_dir, "the dept's")
    assert "STALENESS" in wake
    assert "ARMED" in wake and "FIRES" in wake
    assert f"{sys.executable} " in wake  # #1330: pinned interpreter, not bare python3
    assert "wake-prompt --dept-dir" in wake
    assert "authoritative DUE_MISSIONS for this tick" in wake


def test_wake_prompt_is_never_a_bare_slash_command_and_forbids_free_text():
    from scripts.due_missions import _wake_prompt

    wake = _wake_prompt([], Path("/tmp/dept"), "the dept's")
    assert not wake.lstrip().startswith("/")
    # The prompt itself states there is no free-text slot for the agent to
    # fill in — the citation/no-improvisation contract this card is about.
    assert "never compose, paraphrase, edit, or append your own wording" in wake
    assert "pass this exact text to CronCreate verbatim" in wake


def test_wake_prompt_is_a_pure_deterministic_function_of_its_inputs():
    from scripts.due_missions import _wake_prompt

    plan = [
        {
            "id": "board",
            "cadence": "continuous",
            "period": "continuous",
            "layers": [1, 2, 3, 4],
            "mission_file": "missions/board.md",
        }
    ]
    first = _wake_prompt(plan, Path("/tmp/dept"), "Rick's")
    second = _wake_prompt(plan, Path("/tmp/dept"), "Rick's")
    assert first == second  # same inputs -> byte-identical output, always


def require_wake_prompt_footer() -> str:
    from scripts.due_missions import WAKE_PROMPT_FOOTER

    return WAKE_PROMPT_FOOTER


def _mission_centric_dept_yaml(dept_dir: Path, slug: str, display_name: str) -> None:
    """A minimal, VALID Mac-style due_dispatch manifest with one always-due
    continuous mission — the happy-path fixture shared by the CLI tests
    below."""
    import yaml

    (dept_dir / "missions").mkdir(parents=True)
    (dept_dir / "layers" / "1").mkdir(parents=True)
    (dept_dir / "layers" / "1" / "PROMPT.md").write_text("# layer 1\n", encoding="utf-8")
    (dept_dir / "missions" / "board.md").write_text("# board\n", encoding="utf-8")
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": slug, "display_name": display_name},
                "loop": {
                    "due_dispatch": {
                        "mission_ids": ["board"],
                        "watermark": "monitoring/due.json",
                        "pending_lease_seconds": 21600,
                    }
                },
                "layers": {"subscribed": [1]},
                "recurring_missions": [
                    {
                        "id": "board",
                        "layer": 1,
                        "status": "live",
                        "cadence": "continuous",
                        "due": {"policy": "every_tick"},
                        "mission_file": "missions/board.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_command_wake_prompt_cli_end_to_end(tmp_path: Path, capsys):
    from scripts.due_missions import command_wake_prompt, parser

    dept_dir = tmp_path / "maya"
    _mission_centric_dept_yaml(dept_dir, "maya", "Maya")
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789300800"]
    )
    rc = command_wake_prompt(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Resume Maya's OODA loop and run one full tick now." in out
    assert "DUE_MISSIONS=[board{cadence=continuous" in out
    assert "WORKING_MEMORY/HANDOFF.md" in out
    assert "STALENESS" in out
    # Same invocation, same clock -> byte-identical stdout (determinism holds
    # through the full CLI path, not just the pure helper functions above).
    rc2 = command_wake_prompt(args)
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert out2 == out


# ── FAIL-CLOSED (#1484 PR review) ───────────────────────────────────────────
#
# Repro that triggered this: `due_missions.py wake-prompt --dept-dir
# /srv/agents/ben` (a LIVE VPS dept.yaml, `recurring_missions:
# [{id,layer,cadence,time,...}]`, no `loop.due_dispatch` at all) printed a
# well-formed `DUE_MISSIONS=[]` — indistinguishable from "genuinely nothing
# due" — exactly the failure #1483 exists to prevent. `wake-prompt` must now
# refuse (raise, non-zero exit via `main()`, EMPTY stdout) whenever the
# manifest's schema is not understood or is structurally broken.
#
# #1513 narrowed this: once the schema IS understood and the manifest IS
# valid, a genuinely empty plan (nothing due right now) no longer refuses —
# it emits a machine-generated `DUE_MISSIONS=[]` idle envelope instead (exit
# 0). See `test_command_wake_prompt_emits_idle_envelope_*` below. The
# refusal above still stands for an unrecognized/malformed schema.


def _recurring_missions_dept_yaml(dept_dir: Path, slug: str = "ben", display_name: str = "Ben") -> None:
    """The exact Ben repro shape (#1487): `recurring_missions` uses the VPS
    layer/cadence/time schema, with NO `loop.due_dispatch` block at all — one
    Layer-1 `daily@07:30` mission, no prior `.last-run` (so it is due as soon
    as the floor's own 07:00 Paris minimum and the mission's own 07:30 are
    both reached)."""
    import yaml

    (dept_dir / "missions" / "data_update").mkdir(parents=True)
    (dept_dir / "missions" / "data_update" / "PROMPT.md").write_text(
        "# data_update\n", encoding="utf-8"
    )
    (dept_dir / "layers" / "1").mkdir(parents=True)
    (dept_dir / "layers" / "1" / "PROMPT.md").write_text("# layer 1\n", encoding="utf-8")
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": slug, "display_name": display_name},
                "layers": {"subscribed": [1, 2, 3, 4]},
                "recurring_missions": [
                    {
                        "id": "data_update",
                        "layer": 1,
                        "cadence": "daily",
                        "time": "07:30",
                        "description": "Layer 1 (Observe). Sync positions/cash/P&L.",
                        "output_queue": "queues/research/",
                        "creates": ["situation_brief"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


# ── recurring_missions / VPS schema (#1487) ─────────────────────────────────
#
# #1484 shipped `wake-prompt` for the Mac `loop.due_dispatch` schema only,
# and deliberately FAILED CLOSED for the VPS `recurring_missions` schema
# (ben/tony/maya, plus content/accountant — see board #1487) because no
# selector reused it yet. #1487 closes that: `select_due_missions` +
# `build_dispatch_ctx(materialize=False)` (the SAME primitive the live
# /loop's STEP C already calls every tick) now back a second `wake-prompt`
# branch for this schema. These tests replace the old
# "always fails closed for recurring_missions" pair (that invariant no
# longer holds by design) with: succeeds when something is due, still fails
# closed when nothing is due right now, and a THIRD, narrower fail-closed
# case for a manifest with neither schema at all.


def test_command_wake_prompt_succeeds_on_vps_recurring_missions_schema_when_due(
    tmp_path: Path, capsys
):
    from scripts.due_missions import command_wake_prompt, parser

    dept_dir = tmp_path / "ben"
    dept_dir.mkdir()
    _recurring_missions_dept_yaml(dept_dir)
    # 2026-09-13T12:00:00Z == 14:00 Paris (CEST) — past both the mission's own
    # 07:30 and Layer 1's 07:00 floor; no prior .last-run anywhere, so C.0
    # ("morning floor not yet run today") is the only eligible branch.
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789300800"]
    )
    rc = command_wake_prompt(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Resume Ben's OODA loop and run one full tick now." in out
    assert "DUE_MISSIONS=[data_update{cadence=daily,layer=1,file=" in out
    assert "COMPLETE data_update =>" in out
    # The VPS completion path is commit_dispatch, never the Mac watermark CLI.
    assert "scripts.lib.dispatch_helpers.commit_dispatch" in out
    assert "due_missions.py complete" not in out
    assert "STALENESS" in out
    assert "WORKING_MEMORY/HANDOFF.md" in out
    # Still an authoritative WHAT-is-due list, not a replacement for HOW the
    # tick executes (Chesterton's fence, card #1487 item 4).
    assert "decide_dispatch" in out


def test_command_wake_prompt_emits_idle_envelope_on_vps_recurring_missions_schema_when_nothing_due(
    tmp_path: Path, capsys
):
    """#1513: the manifest IS valid (VPS/recurring_missions schema) and
    `select_due_missions` genuinely has nothing due right now (a true
    heartbeat tick — the exact board #1513 repro: boot at 05:3xZ, before any
    mission's own time floor). This must no longer raise — it must emit the
    machine-generated `DUE_MISSIONS=[]` idle envelope, exit 0, so the caller
    never falls back to free, self-composed tick text on its most common
    tick."""
    from scripts.due_missions import command_wake_prompt, parser

    dept_dir = tmp_path / "ben"
    dept_dir.mkdir()
    _recurring_missions_dept_yaml(dept_dir)
    # 2026-09-13T04:00:00Z == 06:00 Paris — before Layer 1's own 07:00 floor
    # (and every other layer's, with no queues populated either), so nothing
    # is eligible: a genuine heartbeat tick.
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789272000"]
    )
    rc = command_wake_prompt(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Resume Ben's OODA loop and run one full tick now." in out
    assert "DUE_MISSIONS=[]." in out
    assert "not a refusal and not an error" in out
    # Still fully machine-generated: same guardrail + staleness + footer text
    # as the non-empty envelope, never a free-text slot.
    assert "never self-merge mission/mandate/loop/agent-def changes" in out
    assert "STALENESS" in out
    assert "WORKING_MEMORY/HANDOFF.md" in out
    # Best-effort, data-only context: names the known configured mission and
    # its cadence, never a fabricated next-fire timestamp.
    assert "data_update(daily@07:30)" in out
    assert "do not infer or state an exact next-due time" in out


# ── #1513 PR-review follow-up: structural validation gate for recurring_
# missions ────────────────────────────────────────────────────────────────
#
# Independent review flagged that `_plan_for_wake_recurring` did no
# structural validation of `recurring_missions` entries: `select_due_
# missions`/`_mission_cadence_due` are permissive BY DESIGN for a mission
# that can never become due (out-of-range `layer`, unrecognized `cadence`,
# missing `time`/`day`) — it is just silently never selected, never an
# exception. Pre-#1513 that silence didn't matter (wake-prompt refused on
# ANY empty plan regardless of why). #1513 turned "empty plan" into a
# trusted, printed `DUE_MISSIONS=[]` — so a broken entry now silently
# produces a confident-looking idle envelope instead of a refusal, exactly
# the #1487 "well-formed but wrong" failure this whole family of cards
# exists to catch. These tests pin the fix: `_validate_recurring_missions`
# (called unconditionally inside `_plan_for_wake_recurring`) must still
# refuse for each of these shapes, and the idle envelope must still fire
# for an all-valid, nothing-due manifest.


def _recurring_missions_dept_yaml_custom(dept_dir: Path, missions: list) -> None:
    """Like `_recurring_missions_dept_yaml` but with a caller-supplied
    `recurring_missions` list, for exercising `_validate_recurring_missions`
    against specific broken shapes. Creates the same Layer-1 PROMPT.md the
    default fixture uses (mission-file existence is validated too), plus a
    per-mission `missions/<id>/PROMPT.md` for every mission whose id is a
    safe snake_case string (so the mission-file-existence check is never
    what trips a test aimed at a DIFFERENT structural problem)."""
    import yaml

    (dept_dir / "layers" / "1").mkdir(parents=True, exist_ok=True)
    (dept_dir / "layers" / "1" / "PROMPT.md").write_text("# layer 1\n", encoding="utf-8")
    for mission in missions:
        mission_id = mission.get("id")
        if isinstance(mission_id, str) and _MISSION_ID_RE.fullmatch(mission_id):
            (dept_dir / "missions" / mission_id).mkdir(parents=True, exist_ok=True)
            (dept_dir / "missions" / mission_id / "PROMPT.md").write_text(
                f"# {mission_id}\n", encoding="utf-8"
            )
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": "ben", "display_name": "Ben"},
                "layers": {"subscribed": [1, 2, 3, 4]},
                "recurring_missions": missions,
            }
        ),
        encoding="utf-8",
    )


def test_command_wake_prompt_refuses_on_out_of_range_layer(tmp_path: Path, capsys):
    """`layer: 99` — the exact PR-review repro. `select_due_missions` would
    silently never select this mission (no layer 1..4 walk ever matches 99),
    turning it into a confident `DUE_MISSIONS=[]` pre-fix. Must now refuse."""
    from scripts.due_missions import DueMissionConfigError, command_wake_prompt, parser

    dept_dir = tmp_path / "ben"
    dept_dir.mkdir()
    _recurring_missions_dept_yaml_custom(
        dept_dir,
        [
            {
                "id": "ghost_layer_mission",
                "layer": 99,
                "cadence": "daily",
                "time": "07:30",
            }
        ],
    )
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789272000"]
    )
    with pytest.raises(DueMissionConfigError, match="layer must be an int in 1..4"):
        command_wake_prompt(args)
    assert capsys.readouterr().out == ""


def test_command_wake_prompt_refuses_on_unrecognized_cadence(tmp_path: Path, capsys):
    """A typo'd/unsupported cadence string — `_mission_cadence_due` would
    silently fail closed ("Unknown cadence string") forever, never raising.
    The idle envelope must not paper over that."""
    from scripts.due_missions import DueMissionConfigError, command_wake_prompt, parser

    dept_dir = tmp_path / "ben"
    dept_dir.mkdir()
    _recurring_missions_dept_yaml_custom(
        dept_dir,
        [{"id": "typo_cadence_mission", "layer": 1, "cadence": "dailyy"}],
    )
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789272000"]
    )
    with pytest.raises(DueMissionConfigError, match="unrecognized cadence"):
        command_wake_prompt(args)
    assert capsys.readouterr().out == ""


def test_command_wake_prompt_refuses_on_missing_id(tmp_path: Path, capsys):
    """An entry with no `id` at all — sorting/formatting would silently
    coerce it to `""` (e.g. `m.get('id', '')` elsewhere in the selector)
    rather than raising. Must refuse instead."""
    from scripts.due_missions import DueMissionConfigError, command_wake_prompt, parser

    dept_dir = tmp_path / "ben"
    dept_dir.mkdir()
    _recurring_missions_dept_yaml_custom(
        dept_dir,
        [{"layer": 1, "cadence": "daily", "time": "07:30"}],
    )
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789272000"]
    )
    with pytest.raises(DueMissionConfigError, match="id must be a safe snake_case string"):
        command_wake_prompt(args)
    assert capsys.readouterr().out == ""


def test_command_wake_prompt_refuses_on_daily_missing_time(tmp_path: Path, capsys):
    """`cadence: daily` with no `time:` — `_mission_cadence_due`'s daily
    branch returns False forever without it (silently). Must refuse."""
    from scripts.due_missions import DueMissionConfigError, command_wake_prompt, parser

    dept_dir = tmp_path / "ben"
    dept_dir.mkdir()
    _recurring_missions_dept_yaml_custom(
        dept_dir,
        [{"id": "no_time_mission", "layer": 1, "cadence": "daily"}],
    )
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789272000"]
    )
    with pytest.raises(DueMissionConfigError, match="requires a time"):
        command_wake_prompt(args)
    assert capsys.readouterr().out == ""


def test_command_wake_prompt_emits_idle_envelope_when_all_recurring_missions_valid_but_not_due(
    tmp_path: Path, capsys
):
    """Sibling of the earlier idle-envelope test, but explicitly exercising
    the NEW validation gate: two structurally valid missions (different
    layers, different recognized cadences, including a `cron:` entry),
    neither due yet. Must still emit the idle envelope (exit 0), and the
    hint must name only these validated missions."""
    from scripts.due_missions import command_wake_prompt, parser

    dept_dir = tmp_path / "ben"
    dept_dir.mkdir()
    _recurring_missions_dept_yaml_custom(
        dept_dir,
        [
            {"id": "quiet_daily", "layer": 1, "cadence": "daily", "time": "07:30"},
            {"id": "quiet_quarterly", "layer": 4, "cadence": "cron:0 8 1 1,4,7,10 *"},
        ],
    )
    # 2026-09-13T04:00:00Z == 06:00 Paris — before the daily mission's own
    # 07:30 floor, and nowhere near the quarterly cron's next fire.
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789272000"]
    )
    rc = command_wake_prompt(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DUE_MISSIONS=[]." in out
    assert "quiet_daily(daily@07:30)" in out
    assert "quiet_quarterly(cron:0 8 1 1,4,7,10 *)" in out


def test_command_wake_prompt_fails_closed_on_unrecognized_manifest_schema(tmp_path: Path, capsys):
    """Neither `loop.due_dispatch` (Mac) nor a non-empty `recurring_missions`
    (VPS/content/accountant, #1487) is present — the narrowed catch-all that
    replaces #1484's original "any recurring_missions manifest fails closed"
    refusal now that the VPS schema itself is understood."""
    import yaml

    from scripts.due_missions import DueMissionConfigError, command_wake_prompt, parser

    dept_dir = tmp_path / "mystery"
    dept_dir.mkdir()
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump({"department": {"slug": "mystery"}}), encoding="utf-8"
    )
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789300800"]
    )
    with pytest.raises(DueMissionConfigError, match="this manifest has neither"):
        command_wake_prompt(args)
    assert capsys.readouterr().out == ""


def test_command_wake_prompt_emits_idle_envelope_when_nothing_is_currently_due(tmp_path: Path, capsys):
    """#1513: schema IS understood (loop.due_dispatch present) and the
    manifest IS valid, but the single scoped mission's own watermark already
    covers this period — plan resolves to empty. This is the Mac
    `loop.due_dispatch` twin of the VPS idle-envelope test above: must emit
    the machine-generated `DUE_MISSIONS=[]` idle envelope (exit 0), not
    refuse — a purely calendar-cadence dept legitimately has quiet moments."""
    import yaml

    from scripts.due_missions import (
        command_complete,
        command_wake_prompt,
        parser,
    )

    dept_dir = tmp_path / "quiet"
    dept_dir.mkdir()
    (dept_dir / "missions").mkdir()
    (dept_dir / "missions" / "weekly.md").write_text("# weekly\n", encoding="utf-8")
    (dept_dir / "layers" / "4").mkdir(parents=True)
    (dept_dir / "layers" / "4" / "PROMPT.md").write_text("# layer 4\n", encoding="utf-8")
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": "quiet"},
                "loop": {
                    "due_dispatch": {
                        "mission_ids": ["weekly_only"],
                        "watermark": "monitoring/due.json",
                        "pending_lease_seconds": 21600,
                    }
                },
                "layers": {"subscribed": [4]},
                "recurring_missions": [
                    {
                        "id": "weekly_only",
                        "layer": 4,
                        "status": "live",
                        "cadence": "weekly",
                        "due": {"policy": "calendar_period", "timezone": "Europe/Paris"},
                        "mission_file": "missions/weekly.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    now_epoch = 1789300800  # 2026-09-13T12:00:00Z -> 2026-W37
    complete_args = parser().parse_args(
        [
            "complete",
            "--dept-dir",
            str(dept_dir),
            "--mission",
            "weekly_only",
            "--period",
            "2026-W37",
            "--now-epoch",
            str(now_epoch),
        ]
    )
    assert command_complete(complete_args) == 0
    capsys.readouterr()  # discard the "completed ..." line

    wake_args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", str(now_epoch)]
    )
    rc = command_wake_prompt(wake_args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Resume quiet's OODA loop and run one full tick now." in out
    assert "DUE_MISSIONS=[]." in out
    assert "not a refusal and not an error" in out
    assert "never self-merge mission/mandate/loop/agent-def changes" in out
    assert "STALENESS" in out
    assert "WORKING_MEMORY/HANDOFF.md" in out
    assert "weekly_only(weekly)" in out


def test_command_wake_prompt_still_fails_closed_on_broken_due_dispatch_manifest(
    tmp_path: Path, capsys
):
    """#1513: only the valid-but-empty case is relaxed. A structurally
    broken `loop.due_dispatch` manifest (here: a scoped mission_id with no
    matching `recurring_missions` entry at all) must still refuse — exit
    non-zero, empty stdout — exactly as before #1513."""
    import yaml

    from scripts.due_missions import DueMissionConfigError, command_wake_prompt, parser

    dept_dir = tmp_path / "broken"
    dept_dir.mkdir()
    (dept_dir / "layers" / "4").mkdir(parents=True)
    (dept_dir / "layers" / "4" / "PROMPT.md").write_text("# layer 4\n", encoding="utf-8")
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": "broken"},
                "loop": {
                    "due_dispatch": {
                        "mission_ids": ["ghost_mission"],
                        "watermark": "monitoring/due.json",
                        "pending_lease_seconds": 21600,
                    }
                },
                "layers": {"subscribed": [4]},
                "recurring_missions": [],
            }
        ),
        encoding="utf-8",
    )
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789300800"]
    )
    with pytest.raises(DueMissionConfigError, match="scoped mission is missing"):
        command_wake_prompt(args)
    assert capsys.readouterr().out == ""


def test_wake_prompt_cli_subprocess_succeeds_on_vps_recurring_missions_schema(tmp_path: Path):
    """End-to-end via a real subprocess (mirrors production's exact
    `due_missions.py wake-prompt --dept-dir` invocation) — the VPS
    `recurring_missions` schema now succeeds when something is due: exit 0,
    non-empty stdout carrying the DUE_MISSIONS envelope, nothing on stderr."""
    import subprocess
    import sys

    dept_dir = tmp_path / "ben"
    dept_dir.mkdir()
    _recurring_missions_dept_yaml(dept_dir)
    script = Path(__file__).resolve().parents[2] / "due_missions.py"
    proc = subprocess.run(
        [
            sys.executable, str(script), "wake-prompt",
            "--dept-dir", str(dept_dir), "--now-epoch", "1789300800",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "DUE_MISSIONS=[data_update{cadence=daily,layer=1,file=" in proc.stdout
    assert proc.stderr == ""


def test_wake_prompt_cli_subprocess_fails_closed_with_empty_stdout_on_unrecognized_schema(
    tmp_path: Path,
):
    """End-to-end via a real subprocess (mirrors the exact reviewer repro
    command) — non-zero exit AND byte-empty stdout, not just an empty-ish
    string, and the reason lands on stderr, never stdout. Uses a manifest
    with NEITHER schema (the narrowed catch-all — see the module note above
    `_recurring_missions_dept_yaml`); the VPS `recurring_missions` schema
    itself is no longer a universal-refusal case as of #1487."""
    import subprocess
    import sys

    import yaml

    dept_dir = tmp_path / "mystery"
    dept_dir.mkdir()
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump({"department": {"slug": "mystery"}}), encoding="utf-8"
    )
    script = Path(__file__).resolve().parents[2] / "due_missions.py"
    proc = subprocess.run(
        [sys.executable, str(script), "wake-prompt", "--dept-dir", str(dept_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "this manifest has neither" in proc.stderr
