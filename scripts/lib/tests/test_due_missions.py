import datetime as dt
from pathlib import Path

import pytest

from scripts.lib.loop_backup import (
    DueMissionConfigError,
    claim_due_missions,
    due_mission_plan,
    due_period,
    read_due_watermarks,
    release_due_claims,
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


def test_dept_label_reads_department_display_name_or_slug_with_generic_fallback():
    from scripts.due_missions import _dept_label

    assert _dept_label({"department": {"display_name": "Rick", "slug": "rnd"}}) == "Rick's"
    assert _dept_label({"department": {"slug": "maya"}}) == "maya's"
    assert _dept_label({}) == "the dept's"
    assert _dept_label({"department": "not-a-mapping"}) == "the dept's"


def test_wake_prompt_reuses_the_exact_floor_envelope_and_is_never_empty_due_missions():
    from scripts.due_missions import _prompt, _wake_prompt

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
    # renders, verbatim, plus the fixed footer appended — not a re-derivation.
    assert wake.startswith(floor_prompt)
    assert wake == floor_prompt + require_wake_prompt_footer()


def test_wake_prompt_still_emits_full_envelope_with_no_due_missions():
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


def test_command_wake_prompt_cli_end_to_end(tmp_path: Path, capsys):
    import yaml

    from scripts.due_missions import command_wake_prompt, parser

    dept_dir = tmp_path / "maya"
    (dept_dir / "missions").mkdir(parents=True)
    (dept_dir / "layers" / "1").mkdir(parents=True)
    (dept_dir / "layers" / "1" / "PROMPT.md").write_text("# layer 1\n", encoding="utf-8")
    (dept_dir / "missions" / "board.md").write_text("# board\n", encoding="utf-8")
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": "maya", "display_name": "Maya"},
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
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789300800"]
    )
    rc = command_wake_prompt(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Resume Maya's OODA loop and run one full tick now." in out
    assert "DUE_MISSIONS=[board{cadence=continuous" in out
    assert "WORKING_MEMORY/HANDOFF.md" in out
    # Same invocation, same clock -> byte-identical stdout (determinism holds
    # through the full CLI path, not just the pure helper functions above).
    rc2 = command_wake_prompt(args)
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert out2 == out


def test_command_wake_prompt_cli_legacy_manifest_without_due_dispatch(tmp_path: Path, capsys):
    import yaml

    from scripts.due_missions import command_wake_prompt, parser

    dept_dir = tmp_path / "legacy"
    dept_dir.mkdir()
    (dept_dir / "dept.yaml").write_text(
        yaml.safe_dump({"department": {"slug": "legacy"}, "recurring_missions": []}),
        encoding="utf-8",
    )
    args = parser().parse_args(
        ["wake-prompt", "--dept-dir", str(dept_dir), "--now-epoch", "1789300800"]
    )
    rc = command_wake_prompt(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "DUE_MISSIONS=[]" in out
    assert "Resume legacy's OODA loop and run one full tick now." in out
