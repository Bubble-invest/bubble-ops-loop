"""Tests for select_due_missions + resolve_mission_prompt (issue #261, 2026-06-23).

CORE BUG FIXED: the dispatch was PHASE-centric. decide_dispatch() returns ONE phase
string. The runtime loaded ONE layers/<N>/PROMPT.md and spawned ONE subagent. Every
secondary mission in dept.yaml::recurring_missions[] was ORPHANED — ben's
`market_wrapup`, content's `newsletter_redaction`, etc. never fired (0 runs ever).

FIX: select_due_missions(ctx, missions) returns ALL due missions on the highest-priority
eligible phase, so the runtime can spawn one subagent per mission. resolve_mission_prompt
provides the prompt path per mission (per-mission file or legacy layer fallback).

Design note on ctx construction in these tests
-----------------------------------------------
`select_due_missions` is a PURE function of the ctx dict — it reads no filesystem state
except through `ctx['today_dir']` (per-mission .last-run). Most tests use a *manually
constructed* ctx so `build_dispatch_ctx`'s side effect (calling
`materialize_due_missions_for_tick`, which stamps per-mission `.last-run` files) does not
interfere with the cadence-due check. The REGRESSION tests use `build_dispatch_ctx` to
verify the full pipeline.

Test coverage mandated by the brief:
  1. multi-mission phase: 2 due missions on the same layer → BOTH returned.
  2. cadence-not-due mission excluded; daily-already-fired-today excluded;
     weekly-wrong-day excluded.
  3. consumer mission with empty input queue excluded; producer without queue included.
  4. L4 prerequisite gate honored (L4 missions absent until L1/L2/L3 all fired).
  5. legacy shim: no missions/<id>/PROMPT.md → resolve_mission_prompt returns layer prompt.
  6. REGRESSION: decide_dispatch(ctx) returns SAME value as before; equals the
     highest-priority phase among select_due_missions results.
"""
from __future__ import annotations

import yaml
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.lib.dispatch_helpers import (
    build_dispatch_ctx,
    decide_dispatch,
    is_mission_due,
    resolve_mission_prompt,
    select_due_missions,
    write_last_run,
    increment_round_counter,
    write_l1_baseline,
)

# ── Time anchors (June = CEST = UTC+2) ──────────────────────────────────────
# Layer min-times (Paris-local): L1>=07:00, L2>=12:00, L3>=16:00, L4>=19:00.
# UTC equivalents in June: L1>=05:00, L2>=10:00, L3>=14:00, L4>=17:00.

MORNING = datetime(2026, 6, 23, 6, 0, tzinfo=timezone.utc)    # 08:00 Paris (>=L1)
AFTER_L2 = datetime(2026, 6, 23, 10, 30, tzinfo=timezone.utc)  # 12:30 Paris (>=L2)
AFTER_L3 = datetime(2026, 6, 23, 14, 30, tzinfo=timezone.utc)  # 16:30 Paris (>=L3)
AFTER_L4 = datetime(2026, 6, 23, 17, 30, tzinfo=timezone.utc)  # 19:30 Paris (>=L4)
PREDAWN = datetime(2026, 6, 23, 3, 0, tzinfo=timezone.utc)     # 05:00 Paris (<L1)


# ── ctx builders ────────────────────────────────────────────────────────────

def _bare_ctx(now: datetime, **overrides) -> dict:
    """Build a minimal ctx dict WITHOUT calling build_dispatch_ctx.

    Use this for testing select_due_missions as a pure function, without
    triggering materialize_due_missions_for_tick (which would stamp per-mission
    .last-run files and alter the cadence-due check results).
    """
    base = {
        "now_utc": now,
        "today": now.strftime("%Y-%m-%d"),
        "today_dir": "/nonexistent/scratch",   # per-mission markers won't exist
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


def _mk_repo(tmp_path: Path) -> Path:
    """Minimal repo skeleton for integration tests that need the filesystem."""
    (tmp_path / "queues" / "research").mkdir(parents=True)
    (tmp_path / "queues" / "inbox" / "decisions").mkdir(parents=True)
    (tmp_path / "queues" / "research" / ".gitkeep").write_text("")
    (tmp_path / "queues" / "inbox" / "decisions" / ".gitkeep").write_text("")
    return tmp_path


def _write_dept_yaml(repo: Path, missions: list[dict]) -> None:
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )


def _fire(repo: Path, layer: int, when: datetime) -> None:
    """Stamp the layer .last-run marker (marks layer as fired today)."""
    today = when.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / str(layer), when)


def _full_ctx(repo: Path, now: datetime) -> dict:
    """Build ctx via build_dispatch_ctx and inject _repo_dir for consumer checks."""
    ctx = build_dispatch_ctx(repo, now_utc=now)
    ctx["_repo_dir"] = str(repo)
    return ctx


def _stamp_mission_lastrun(repo: Path, mid: str, when: datetime) -> None:
    """Write per-mission .last-run to simulate mission already fired today."""
    today = when.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / "missions" / mid, when)


def _mk_daily(mid: str, layer: int = 1, *, time: str = "07:00",
              output_queue: str = "queues/research/",
              creates: list | None = None, **extra) -> dict:
    m = {
        "id": mid,
        "layer": layer,
        "cadence": "daily",
        "time": time,
        "output_queue": output_queue,
        "creates": creates if creates is not None else [],
    }
    m.update(extra)
    return m


def _mk_weekly(mid: str, layer: int = 1, *, time: str = "07:00", day: str = "monday",
               output_queue: str = "queues/research/",
               creates: list | None = None) -> dict:
    return {
        "id": mid,
        "layer": layer,
        "cadence": "weekly",
        "time": time,
        "day": day,
        "output_queue": output_queue,
        "creates": creates if creates is not None else [],
    }


# ── 1. Multi-mission phase: 2 due missions → BOTH returned ──────────────────

def test_two_due_missions_on_same_layer_both_returned():
    """CORE BUG (#261): when 2 missions on L1 are due, select_due_missions
    returns BOTH, not just the primary one.

    Uses a bare ctx (no materialization) to isolate select_due_missions as a
    pure function. Both missions have creates:[] so they represent the
    orphan-category: missions that generate reports/summaries rather than
    queue items.
    """
    m1 = _mk_daily("morning_sync", layer=1, time="07:00")
    m2 = _mk_daily("market_wrapup", layer=1, time="07:00")
    # L1 not yet fired, morning floor reached → L1 eligible.
    ctx = _bare_ctx(MORNING, layer_1_last_run_today=None)
    due = select_due_missions(ctx, [m1, m2])

    assert len(due) == 2, (
        f"select_due_missions must return BOTH missions on the same due layer, "
        f"got {len(due)}: {[m['id'] for m in due]}\n"
        f"This IS the orphan-mission bug (#261): secondary missions never ran."
    )
    ids = {m["id"] for m in due}
    assert "morning_sync" in ids
    assert "market_wrapup" in ids


def test_two_due_missions_returned_sorted_by_id():
    """Due missions are sorted by id for determinism (same phase → stable order)."""
    m_z = _mk_daily("zzz_last", layer=1, time="07:00")
    m_a = _mk_daily("aaa_first", layer=1, time="07:00")
    ctx = _bare_ctx(MORNING)
    due = select_due_missions(ctx, [m_z, m_a])
    assert len(due) == 2
    assert due[0]["id"] == "aaa_first"
    assert due[1]["id"] == "zzz_last"


def test_missions_on_different_layers_only_highest_priority_returned():
    """Only missions from the HIGHEST-priority eligible phase are returned.

    At MORNING (L1 eligible, L2 not eligible yet): an L2 mission must NOT appear
    even if its cadence says it's due, because L2's time floor (12:00 Paris)
    hasn't been reached.
    """
    m_l1 = _mk_daily("morning_sync", layer=1, time="07:00")
    m_l2 = _mk_daily("research", layer=2, time="12:00")
    ctx = _bare_ctx(MORNING, layer_1_last_run_today=None)
    due = select_due_missions(ctx, [m_l1, m_l2])
    ids = [m["id"] for m in due]
    assert "morning_sync" in ids
    assert "research" not in ids, (
        "L2 mission must not appear at MORNING — L2 floor (12:00 Paris) not reached"
    )


# ── 2. Exclusion conditions ──────────────────────────────────────────────────

def test_cadence_not_yet_due_excluded():
    """A daily mission whose time has not yet been reached is excluded."""
    m = _mk_daily("night_mission", layer=1, time="23:00")  # 23:00 Paris
    ctx = _bare_ctx(MORNING)  # 08:00 Paris — before 23:00
    due = select_due_missions(ctx, [m])
    assert len(due) == 0, "mission not yet due (time not reached) must be excluded"


def test_daily_already_fired_today_excluded(tmp_path: Path):
    """A daily mission already fired today (per-mission .last-run stamped today) is excluded.

    Uses a real repo so write_last_run can stamp the per-mission marker and
    today_dir can be resolved properly.
    """
    repo = _mk_repo(tmp_path)
    m = _mk_daily("morning_sync", layer=1, time="07:00")
    _write_dept_yaml(repo, [m])

    # Stamp per-mission .last-run at a PRIOR tick today (07:30 Paris = 05:30 UTC),
    # earlier than the current tick (MORNING = 08:00 Paris). A marker from a prior
    # tick today means the mission already ran → must be excluded. (A marker equal
    # to now_utc would be the materializer's same-tick stamp and is treated as
    # "not yet dispatched this tick" — see _mission_last_fired.)
    prior_tick = datetime(2026, 6, 23, 5, 30, tzinfo=timezone.utc)  # 07:30 Paris
    _stamp_mission_lastrun(repo, "morning_sync", prior_tick)
    today_dir = str(repo / "outputs" / MORNING.strftime("%Y-%m-%d"))

    ctx = _bare_ctx(MORNING, today_dir=today_dir)
    due = select_due_missions(ctx, [m])
    assert len(due) == 0, (
        "mission with a prior-tick per-mission .last-run today must NOT be "
        "re-selected (daily already-fired-today gate)"
    )


def test_weekly_wrong_day_excluded():
    """A weekly mission whose `day` is not today must be excluded.

    2026-06-23 is a Tuesday. A friday mission must be excluded.
    """
    m = _mk_weekly("friday_brief", layer=1, time="07:00", day="friday")
    ctx = _bare_ctx(MORNING)  # Tuesday 2026-06-23
    due = select_due_missions(ctx, [m])
    assert len(due) == 0, "weekly mission on a different day must be excluded"


def test_weekly_correct_day_included():
    """A weekly mission on the CORRECT day AND after time is included.

    2026-06-23 is a Tuesday. A tuesday mission after 07:00 Paris is due.
    """
    m = _mk_weekly("tuesday_brief", layer=1, time="07:00", day="tuesday")
    ctx = _bare_ctx(MORNING)  # Tuesday 2026-06-23
    due = select_due_missions(ctx, [m])
    assert len(due) == 1, (
        "weekly mission on the correct day (Tuesday 2026-06-23) must be selected"
    )
    assert due[0]["id"] == "tuesday_brief"


def test_mixed_due_and_not_due():
    """One due and one not-due mission on the same layer → only due one returned."""
    m_due = _mk_daily("morning_sync", layer=1, time="07:00")  # 07:00 Paris, MORNING=08:00
    m_not_due = _mk_daily("night_mission", layer=1, time="23:00")  # 23:00 Paris, too late
    ctx = _bare_ctx(MORNING)
    due = select_due_missions(ctx, [m_due, m_not_due])
    assert len(due) == 1
    assert due[0]["id"] == "morning_sync"


# ── 3. Consumer mission input-queue gate ─────────────────────────────────────

def test_consumer_mission_with_empty_queue_excluded(tmp_path: Path):
    """A consumer mission (has `input_queue`) with an empty input queue is excluded.

    This mirrors the fire-spin fix (#61797): a consumer mission must not be
    dispatched when its input queue has no items to drain.
    """
    repo = _mk_repo(tmp_path)
    consumer = {
        "id": "research_consumer",
        "layer": 2,
        "cadence": "daily",
        "time": "12:00",
        "output_queue": "queues/gates/",
        "input_queue": "queues/research/",
        "creates": ["investment_case"],
    }
    _write_dept_yaml(repo, [consumer])

    # queues/research/ is EMPTY — no items to drain.
    # Build ctx manually with L2 eligible: has_research_items=False but time reached.
    ctx = _bare_ctx(
        AFTER_L2,
        # L2 is eligible only when has_research_items is True OR when L1 fired
        # but here we're testing the consumer gate specifically, so simulate
        # L2 as eligible by overriding the research flag.
        has_research_items=True,  # pretend the phase was chosen
        layer_1_last_run_today=MORNING,
        _repo_dir=str(repo),
    )
    due = select_due_missions(ctx, [consumer])
    assert len(due) == 0, (
        "consumer mission with empty input_queue must be excluded "
        "(prevents fire-spin on an empty queue)"
    )


def test_producer_mission_without_input_queue_included(tmp_path: Path):
    """A producer mission (no `input_queue`) is included regardless of queue state.

    Producer missions WRITE TO queues; they never need items in a queue first.
    """
    repo = _mk_repo(tmp_path)
    producer = _mk_daily(
        "data_update", layer=1, time="07:00",
        output_queue="queues/research/",
        creates=["research_item"],
    )
    # queues/research/ is empty — irrelevant for a producer.
    ctx = _bare_ctx(MORNING, _repo_dir=str(repo))
    due = select_due_missions(ctx, [producer])
    assert len(due) == 1, (
        "producer mission (no input_queue) must be included regardless "
        "of queue state — it generates new work"
    )
    assert due[0]["id"] == "data_update"


def test_consumer_mission_with_non_empty_queue_included(tmp_path: Path):
    """A consumer mission whose input_queue has items IS included."""
    repo = _mk_repo(tmp_path)
    dept_data = {
        "recurring_missions": [
            {
                "id": "data_update",
                "layer": 1,
                "cadence": "daily",
                "time": "07:30",
                "output_queue": "queues/research/",
                "creates": ["research_item"],
            },
            {
                "id": "research_consumer",
                "layer": 2,
                "cadence": "daily",
                "time": "12:00",
                "output_queue": "queues/gates/",
                "input_queue": "queues/research/",
                "creates": ["investment_case"],
            },
        ]
    }
    _write_dept_yaml(repo, dept_data["recurring_missions"])

    # Drop a research_item into the input queue.
    (repo / "queues" / "research" / "ri-001.yaml").write_text(
        yaml.dump({
            "id": "ri-001",
            "kind": "research_item",
            "mission_id": "data_update",
            "created_at": AFTER_L2.isoformat(),
        }, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    # L2 eligible: has_research_items=True, L1 fired, L2 time reached.
    ctx = _bare_ctx(
        AFTER_L2,
        has_research_items=True,
        layer_1_last_run_today=MORNING,
        _repo_dir=str(repo),
    )

    consumer = dept_data["recurring_missions"][1]
    due = select_due_missions(ctx, dept_data["recurring_missions"])
    consumer_ids = [m["id"] for m in due]
    assert "research_consumer" in consumer_ids, (
        "consumer mission with a non-empty input_queue must be selected"
    )


# ── 4. L4 prerequisite gate ──────────────────────────────────────────────────

def test_l4_missions_absent_when_l1_not_fired():
    """L4 missions must not appear if L1 has not fired today."""
    l4_mission = _mk_daily("debrief", layer=4, time="19:00")
    # L1 NOT fired today → L4 prerequisite fails.
    ctx = _bare_ctx(AFTER_L4, layer_1_last_run_today=None)
    due = select_due_missions(ctx, [l4_mission])
    assert len(due) == 0, (
        "L4 missions must not appear unless L1 has fired today "
        "(L4 prerequisite gate)"
    )


def test_l4_missions_absent_when_already_fired(tmp_path: Path):
    """Per-mission idempotence (#277): an L4 mission with its own per-mission
    marker from a prior tick today must NOT be re-selected (fire-spin guard).

    Under the new per-mission model, idempotence is provided by the per-mission
    .last-run marker (same as L1-3), NOT by a once-per-day layer-wide cap.
    The layer_4_last_run_today flag alone no longer blocks individual L4 missions;
    only a per-mission marker stamped at a prior tick does.
    """
    repo = _mk_repo(tmp_path)
    l4_mission = _mk_daily("debrief", layer=4, time="19:00")
    _write_dept_yaml(repo, [l4_mission])

    # Stamp the per-mission marker at a prior tick: 19:30 Paris = 17:30 UTC.
    # AFTER_L4 = 17:30 UTC (19:30 Paris), so prior_tick must be strictly earlier.
    # Use 19:00 Paris = 17:00 UTC as the initial fire time, and check at 20:30 Paris.
    prior_tick = datetime(2026, 6, 23, 17, 0, tzinfo=timezone.utc)   # 19:00 Paris
    check_tick = datetime(2026, 6, 23, 18, 30, tzinfo=timezone.utc)  # 20:30 Paris
    _stamp_mission_lastrun(repo, "debrief", prior_tick)

    # Build a ctx where L4 window is open (L1/L2/L3 fired, time >= 19:00 Paris).
    today_dir = str(repo / "outputs" / check_tick.strftime("%Y-%m-%d"))
    ctx = _bare_ctx(
        check_tick,
        today_dir=today_dir,
        layer_1_last_run_today=MORNING,
        layer_2_last_run_today=AFTER_L2,
        layer_3_last_run_today=AFTER_L3,
        layer_4_last_run_today=prior_tick,   # layer marker also set (as it would be in prod)
    )
    due = select_due_missions(ctx, [l4_mission])
    assert len(due) == 0, (
        "L4 mission with a per-mission .last-run from a prior tick today must "
        "NOT be re-selected — per-mission idempotence prevents fire-spin (#277)"
    )


def test_l4_missions_appear_when_prerequisites_met():
    """L4 missions appear when L1+L2+L3 fired today and L4 not yet run."""
    l4_mission = _mk_daily("debrief", layer=4, time="19:00")
    ctx = _bare_ctx(
        AFTER_L4,
        layer_1_last_run_today=MORNING,
        layer_2_last_run_today=AFTER_L2,
        layer_3_last_run_today=AFTER_L3,
        layer_4_last_run_today=None,  # not yet run
    )
    due = select_due_missions(ctx, [l4_mission])
    assert len(due) == 1, (
        "L4 missions must appear when L1+L2+L3 fired today and L4 not yet run"
    )
    assert due[0]["id"] == "debrief"


def test_l4_no_research_quiet_day_prerequisites_met():
    """L4 fires on a quiet day (no research, L2 not fired) with L1+L3 fired."""
    l4_mission = _mk_daily("debrief", layer=4, time="19:00")
    # No research items, L2 not fired (quiet day — L4 gate relaxed: l2_fired or not has_research).
    ctx = _bare_ctx(
        AFTER_L4,
        has_research_items=False,
        has_inbox_decisions=False,
        layer_1_last_run_today=MORNING,
        layer_2_last_run_today=None,   # L2 didn't fire (no research work)
        layer_3_last_run_today=None,   # L3 didn't fire (no decisions)
        layer_4_last_run_today=None,
    )
    due = select_due_missions(ctx, [l4_mission])
    assert len(due) == 1, (
        "L4 mission must appear on a quiet day (no research, no decisions, "
        "L2/L3 skipped) — L4 gate relaxes when there is no work to drain"
    )


def test_l4_multiple_missions_all_returned():
    """Multiple L4 missions all appear when prerequisites met (multi-mission fix)."""
    l4_a = _mk_daily("risk_audit", layer=4, time="19:00")
    l4_b = _mk_daily("performance_report", layer=4, time="19:00")
    ctx = _bare_ctx(
        AFTER_L4,
        layer_1_last_run_today=MORNING,
        layer_2_last_run_today=AFTER_L2,
        layer_3_last_run_today=AFTER_L3,
        layer_4_last_run_today=None,
    )
    due = select_due_missions(ctx, [l4_a, l4_b])
    assert len(due) == 2, (
        "All due L4 missions must be returned — the orphan fix applies to L4 too"
    )


# ── 5. Legacy shim: resolve_mission_prompt ───────────────────────────────────

def test_resolve_mission_prompt_per_mission_file_wins(tmp_path: Path):
    """When missions/<id>/PROMPT.md exists, it is returned."""
    repo = tmp_path
    mid = "morning_sync"
    prompt_path = repo / "missions" / mid / "PROMPT.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("# Per-mission prompt\n", encoding="utf-8")

    mission = {"id": mid, "layer": 1}
    result = resolve_mission_prompt(repo, mission)
    assert result == prompt_path, (
        "per-mission prompt must take precedence over the layer fallback"
    )


def test_resolve_mission_prompt_falls_back_to_layer(tmp_path: Path):
    """When no missions/<id>/PROMPT.md exists, fall back to layers/<N>/PROMPT.md.

    This is the zero-regression shim: depts without per-mission prompts keep
    running their existing layer prompt via the primary mission.
    """
    repo = tmp_path
    layer_prompt = repo / "layers" / "1" / "PROMPT.md"
    layer_prompt.parent.mkdir(parents=True)
    layer_prompt.write_text("# Layer 1 monolithic prompt\n", encoding="utf-8")

    mission = {"id": "morning_sync", "layer": 1}
    result = resolve_mission_prompt(repo, mission)
    assert result == layer_prompt, (
        "when no per-mission prompt exists, resolve_mission_prompt must "
        "fall back to layers/<N>/PROMPT.md (legacy zero-regression shim)"
    )


def test_resolve_mission_prompt_layer_fallback_no_id(tmp_path: Path):
    """A mission with no `id` falls back to the layer prompt."""
    repo = tmp_path
    layer_prompt = repo / "layers" / "2" / "PROMPT.md"
    layer_prompt.parent.mkdir(parents=True)
    layer_prompt.write_text("# Layer 2 prompt\n", encoding="utf-8")

    mission = {"layer": 2}  # no id
    result = resolve_mission_prompt(repo, mission)
    assert result == layer_prompt


def test_resolve_mission_prompt_per_mission_beats_layer(tmp_path: Path):
    """Even when layers/<N>/PROMPT.md exists, per-mission wins."""
    repo = tmp_path
    mid = "morning_sync"
    per_mission = repo / "missions" / mid / "PROMPT.md"
    per_mission.parent.mkdir(parents=True)
    per_mission.write_text("# Per-mission\n", encoding="utf-8")

    layer_prompt = repo / "layers" / "1" / "PROMPT.md"
    layer_prompt.parent.mkdir(parents=True)
    layer_prompt.write_text("# Layer\n", encoding="utf-8")

    mission = {"id": mid, "layer": 1}
    result = resolve_mission_prompt(repo, mission)
    assert result == per_mission, "per-mission must beat layer prompt when both exist"


# ── 6. REGRESSION: decide_dispatch unchanged ─────────────────────────────────

def test_decide_dispatch_unchanged_l1_morning_floor():
    """REGRESSION: decide_dispatch returns 'layer_1' when L1 not fired.
    select_due_missions must NOT mutate ctx; decide_dispatch output is identical.
    """
    m = _mk_daily("morning_sync", layer=1, time="07:00")
    ctx = _bare_ctx(MORNING, layer_1_last_run_today=None)
    phase_before = decide_dispatch(ctx)
    assert phase_before == "layer_1"

    # Call select_due_missions — must not mutate ctx.
    due = select_due_missions(ctx, [m])
    phase_after = decide_dispatch(ctx)  # must be identical
    assert phase_after == phase_before, (
        "decide_dispatch must return the SAME value after select_due_missions "
        "is called (select_due_missions must not mutate ctx)"
    )
    assert phase_after == "layer_1"

    # The due missions must match the phase.
    assert len(due) == 1
    assert due[0]["layer"] == 1, (
        "due missions must be on layer 1, matching decide_dispatch's 'layer_1' result"
    )


def test_decide_dispatch_regression_heartbeat(tmp_path: Path):
    """After L1 ran + queues empty + outside L4 window → heartbeat.

    This is the Tony ebb03972 regression guard. Uses build_dispatch_ctx for the
    full pipeline, then verifies select_due_missions also returns [] on a
    heartbeat tick.
    """
    repo = _mk_repo(tmp_path)
    today = MORNING.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / "1", MORNING)
    increment_round_counter(repo / "outputs" / today, layer=1)

    # Mission already stamped today (simulating mission ran this morning).
    m = _mk_daily("morning_sync", layer=1, time="07:00")
    _stamp_mission_lastrun(repo, "morning_sync", MORNING)

    ctx = _full_ctx(repo, MORNING)
    phase = decide_dispatch(ctx)
    assert phase == "heartbeat", (
        "L1 already ran + empty queues + outside L4 window must be heartbeat "
        "(Tony incident ebb03972 regression guard)"
    )
    due = select_due_missions(ctx, [m])
    assert len(due) == 0, (
        "select_due_missions must return [] on a heartbeat tick — "
        "no eligible phase means no due missions"
    )


def test_decide_dispatch_layer_2_regression(tmp_path: Path):
    """Research items + L1 fired + L2 time → decide_dispatch returns 'layer_2'.

    select_due_missions must return the L2 mission(s) with matching layer.
    Uses a bare ctx to decouple from materialization side effects.
    """
    repo = _mk_repo(tmp_path)
    dept = [
        {
            "id": "data_update",
            "layer": 1,
            "cadence": "daily",
            "time": "07:30",
            "output_queue": "queues/research/",
            "creates": ["research_item"],
        },
        {
            "id": "research",
            "layer": 2,
            "cadence": "daily",
            "time": "12:00",
            "output_queue": "queues/gates/",
            "creates": ["investment_case"],
        },
    ]

    # Build a ctx where L2 is the eligible phase (has_research_items=True, L1 fired).
    ctx = _bare_ctx(
        AFTER_L2,
        has_research_items=True,
        layer_1_last_run_today=MORNING,
        _repo_dir=str(repo),
    )
    phase = decide_dispatch(ctx)
    assert phase == "layer_2", (
        "research items + L1 fired + L2 min-time → decide_dispatch must return 'layer_2'"
    )

    due = select_due_missions(ctx, dept)
    assert len(due) > 0, "select_due_missions must return at least one L2 mission"
    assert all(m["layer"] == 2 for m in due), (
        "all returned missions must be on layer_2 (matching decide_dispatch)"
    )
    assert due[0]["id"] == "research"


# ── Pre-dawn: no phase eligible, empty result ─────────────────────────────────

def test_predawn_no_missions_due():
    """Before 07:00 Paris, no layer is eligible → select_due_missions returns []."""
    m = _mk_daily("morning_sync", layer=1, time="07:00")
    ctx = _bare_ctx(PREDAWN)  # 05:00 Paris, before all layer floors
    phase = decide_dispatch(ctx)
    assert phase == "heartbeat", "pre-dawn must be heartbeat"
    due = select_due_missions(ctx, [m])
    assert len(due) == 0, "pre-dawn tick must yield no due missions"


# ── L1 cycle gate: select_due_missions does not weaken it ───────────────────

def test_l1_cycle_gate_not_weakened(tmp_path: Path):
    """L1 must not re-fire until a full cycle (L2+L3+L4 each advance since baseline).

    Verifies that select_due_missions does not return L1 missions prematurely,
    preserving the cycle gate that prevents the bug that made L1 re-fire every
    tick (the ebb03972-family of issues #235/#237).
    """
    repo = _mk_repo(tmp_path)
    m = _mk_daily("morning_sync", layer=1, time="07:00")
    today = MORNING.strftime("%Y-%m-%d")

    # L1 already ran; capture baseline at that moment.
    write_last_run(repo / "outputs" / today / "1", MORNING)
    increment_round_counter(repo / "outputs" / today, layer=1)
    write_l1_baseline(repo / "outputs" / today)

    # Only L2 has advanced since L1's baseline — cycle NOT complete.
    increment_round_counter(repo / "outputs" / today, layer=2)

    # Stamp the per-mission marker so it appears "already ran today".
    _stamp_mission_lastrun(repo, "morning_sync", MORNING)

    ctx = _full_ctx(repo, MORNING)
    phase = decide_dispatch(ctx)
    assert phase == "heartbeat", (
        "L1 must NOT re-fire when only L2 advanced since baseline "
        "(cycle gate must require L2+L3+L4 each)"
    )
    due = select_due_missions(ctx, [m])
    assert len(due) == 0, (
        "select_due_missions must respect the L1 cycle gate — "
        "no L1 missions until the full cycle completes"
    )


def test_l1_cycle_gate_fires_after_full_cycle(tmp_path: Path):
    """L1 re-fires after L2+L3+L4 each completed a round since L1's baseline.

    Verifies the cycle gate is PRESERVED (not weakened) by select_due_missions.
    This is the positive case of the cycle gate test.
    """
    repo = _mk_repo(tmp_path)
    m = _mk_daily("morning_sync", layer=1, time="07:00")
    today = MORNING.strftime("%Y-%m-%d")

    # L1 ran, baseline captured.
    write_last_run(repo / "outputs" / today / "1", MORNING)
    increment_round_counter(repo / "outputs" / today, layer=1)
    write_l1_baseline(repo / "outputs" / today)

    # Full cycle: L2, L3, L4 each advanced.
    for layer in (2, 3, 4):
        increment_round_counter(repo / "outputs" / today, layer=layer)

    # Per-mission marker is from morning (today's date, before 08:00) — for daily,
    # last_fired=morning is still same Paris day → NOT re-due via per-mission marker.
    # But the LAYER level says L1 is eligible for cycle gate re-fire.
    # The ctx via build_dispatch_ctx has layer_1_last_run_today set.
    ctx = _full_ctx(repo, MORNING)
    phase = decide_dispatch(ctx)
    assert phase == "layer_1", (
        "L1 must re-fire after a full cycle — cycle gate re-fires L1 once "
        "L2+L3+L4 each complete a round since L1's baseline"
    )

    # For select_due_missions to return the mission, the per-mission marker must
    # not block it — since the LAYER itself is now eligible, missions without
    # a per-mission marker (or with a marker from a prior tick) must fire.
    # morning_sync has no per-mission marker at all here (we didn't stamp it).
    due = select_due_missions(ctx, [m])
    assert len(due) == 1, (
        "select_due_missions must return L1 missions when the cycle gate fires "
        "— the cycle re-fire must include all missions on L1"
    )


# ── Anti-fire-spin: creates:[] report-mission, full pipeline (#261 / #235-#237) ──
#
# THE DEPLOY BLOCKER the reviewer caught: a `creates: []` report-mission (e.g.
# ben's market_wrapup) never produces queue-item descriptors, so before FIX 2 it
# never entered the materializer's create-loop → its per-mission .last-run was
# never stamped → _mission_last_fired returned None forever → select_due_missions
# re-selected it on EVERY tick = fire-spin (the #235/#237 class we must NOT
# reintroduce).
#
# FIX 2 stamps the per-mission marker for EVERY due layer-1..3 mission (regardless
# of creates[]) at the top of build_dispatch_ctx. FIX in _mission_last_fired makes
# a same-tick marker (== now_utc) NOT veto this-tick selection, so:
#   tick 1 → mission SELECTED (and marker stamped this tick)
#   tick 2 → mission EXCLUDED (marker now < now_utc → is_mission_due vetoes)
# These tests use the FULL pipeline (build_dispatch_ctx, not a bare ctx) so they
# exercise the materializer's stamping behaviour, making the guard contractual.

def test_fire_spin_guard_creates_empty_mission_full_pipeline(tmp_path: Path):
    """FULL-PIPELINE fire-spin guard for a creates:[] report-mission.

    tick 1: mission selected, marker stamped by materializer.
    tick 2 (later same day): mission EXCLUDED (must NOT re-select = no fire-spin).
    """
    repo = _mk_repo(tmp_path)
    # A pure report-writer mission: is_mission_due() True but creates:[].
    m = _mk_daily("market_wrapup", layer=1, time="07:00",
                  output_queue="queues/research/", creates=[])
    _write_dept_yaml(repo, [m])

    today = MORNING.strftime("%Y-%m-%d")
    marker = repo / "outputs" / today / "missions" / "market_wrapup" / ".last-run"
    assert not marker.exists(), "precondition: no per-mission marker before tick 1"

    # ── TICK 1 ── (08:00 Paris) — mission is due, must be selected.
    ctx1 = _full_ctx(repo, MORNING)
    due1 = select_due_missions(ctx1, [m])
    ids1 = [x["id"] for x in due1]
    assert "market_wrapup" in ids1, (
        "TICK 1: a due creates:[] report-mission MUST be selected for dispatch"
    )

    # FIX 2: the materializer stamped the per-mission marker (no <N> path) this tick.
    assert marker.exists(), (
        "FIX 2: materializer must stamp outputs/<today>/missions/<id>/.last-run "
        "even for a creates:[] mission, so it cannot fire-spin"
    )

    # ── TICK 2 ── (09:00 Paris, same day) — must NOT be re-selected.
    LATER = datetime(2026, 6, 23, 7, 0, tzinfo=timezone.utc)  # 09:00 Paris
    ctx2 = _full_ctx(repo, LATER)
    due2 = select_due_missions(ctx2, [m])
    ids2 = [x["id"] for x in due2]
    assert "market_wrapup" not in ids2, (
        "TICK 2 FIRE-SPIN GUARD: the mission must be GONE — re-selecting it every "
        "tick is the #235/#237-class fire-spin we must not reintroduce"
    )
    assert due2 == [], (
        "TICK 2: select_due_missions must return EXACTLY [] (mission excluded, "
        "no other mission to run)"
    )


def test_fire_spin_guard_prior_tick_marker_excludes(tmp_path: Path):
    """Once the per-mission marker at the correct no-<N> path exists from a PRIOR
    tick, the mission is excluded — proving the marker path is the one
    _mission_last_fired actually reads.

    This is the explicit "correct path" assertion: we write the marker by hand at
    outputs/<today>/missions/<id>/.last-run (NO layer <N>) at an EARLIER time, and
    confirm select_due_missions excludes the mission.
    """
    repo = _mk_repo(tmp_path)
    m = _mk_daily("market_wrapup", layer=1, time="07:00",
                  output_queue="queues/research/", creates=[])
    _write_dept_yaml(repo, [m])

    today = MORNING.strftime("%Y-%m-%d")
    # Marker at the CORRECT no-<N> path, stamped at a PRIOR tick (07:30 Paris).
    prior = datetime(2026, 6, 23, 5, 30, tzinfo=timezone.utc)  # 07:30 Paris
    correct_path = repo / "outputs" / today / "missions" / "market_wrapup"
    write_last_run(correct_path, prior)

    ctx = _full_ctx(repo, MORNING)  # 08:00 Paris, after the prior stamp
    due = select_due_missions(ctx, [m])
    assert "market_wrapup" not in [x["id"] for x in due], (
        "a per-mission marker at outputs/<today>/missions/<id>/.last-run from a "
        "prior tick must exclude the mission (no-<N> path is the one read)"
    )


def test_fire_spin_guard_wrong_layered_path_does_not_exclude(tmp_path: Path):
    """A marker at the WRONG layered path (outputs/<today>/<N>/missions/<id>/) must
    NOT be read by _mission_last_fired — proving the no-<N> path is load-bearing.

    This is the negative control for the path mismatch FIX 1 fixed in the scaffold
    prose: if a subagent stamped the layered path, the mission would still be
    selected (fire-spin). We assert select_due_missions ignores the layered marker.
    Note: build_dispatch_ctx's own materializer will ALSO stamp the correct no-<N>
    path this tick, but a same-tick marker does not veto this-tick selection, so
    the mission is still selected on this first tick — exactly what we want to
    show: only the no-<N> path (from a PRIOR tick) gates dispatch.
    """
    repo = _mk_repo(tmp_path)
    m = _mk_daily("market_wrapup", layer=1, time="07:00",
                  output_queue="queues/research/", creates=[])
    _write_dept_yaml(repo, [m])

    today = MORNING.strftime("%Y-%m-%d")
    # WRONG layered path, stamped at a prior tick — must be IGNORED.
    prior = datetime(2026, 6, 23, 5, 30, tzinfo=timezone.utc)
    wrong_path = repo / "outputs" / today / "1" / "missions" / "market_wrapup"
    write_last_run(wrong_path, prior)

    ctx = _full_ctx(repo, MORNING)
    due = select_due_missions(ctx, [m])
    assert "market_wrapup" in [x["id"] for x in due], (
        "a marker at the WRONG layered path outputs/<today>/<N>/missions/<id>/ "
        "must NOT gate dispatch — only the no-<N> path is read (FIX 1 / FIX 2)"
    )


# ── Anti-fire-spin for L4 missions — per-mission idempotence (#277) ───────────
#
# Fix #277: L4 is now guarded by per-mission markers (same as L1-3), NOT by the
# once-per-day layer-wide cap.  materialize_due_missions_for_tick now stamps the
# per-mission .last-run for EVERY due L4 mission (no queue items are created —
# only the anti-fire-spin marker).
#
# The old test made the L4 layer-cap contractual.  After #277 the contract is:
#   • tick 1: L4 mission selected; materializer stamps per-mission marker at now_utc
#             (same-tick semantics → _mission_last_fired returns None → mission fires)
#   • tick 2: per-mission marker < now_utc → is_mission_due returns False → excluded
#
# decide_dispatch STILL uses `not l4_fired` for its own phase-string output (unchanged).
# select_due_missions uses _mission_layer_eligible which no longer carries the cap,
# and relies on per-mission markers for idempotence.

def test_fire_spin_guard_l4_creates_empty_mission_layer_cap(tmp_path: Path):
    """FULL-PIPELINE anti-fire-spin for a REAL L4 creates:[] mission.

    Per-mission idempotence (#277): L4's fire-spin guard is now the per-mission
    .last-run marker (stamped by materialize_due_missions_for_tick), NOT the
    once-per-day layer-wide cap.

    tick 1: L4 mission selected; materializer stamps per-mission marker at now_utc.
            (same-tick marker → _mission_last_fired returns None → mission fires)
    tick 2 (later same day): per-mission marker < now_utc → mission EXCLUDED (no fire-spin).
    """
    repo = _mk_repo(tmp_path)
    # A REAL L4 report-mission: creates:[] and layer=4.
    m = _mk_daily("market_wrapup", layer=4, time="19:00",
                  output_queue="queues/research/", creates=[])
    _write_dept_yaml(repo, [m])

    today = AFTER_L4.strftime("%Y-%m-%d")
    # L4 prerequisites: L1, L2, L3 LAYER markers stamped today.
    for n in (1, 2, 3):
        write_last_run(repo / "outputs" / today / str(n), AFTER_L4)

    per_mission_marker = repo / "outputs" / today / "missions" / "market_wrapup" / ".last-run"
    assert not per_mission_marker.exists(), "precondition: no per-mission marker before tick 1"

    # ── TICK 1 ── (19:30 Paris) — L4 eligible, mission must be selected.
    ctx1 = _full_ctx(repo, AFTER_L4)
    assert decide_dispatch(ctx1) == "layer_4", (
        "TICK 1: L1/L2/L3 fired + time>=19:00 + L4 not run → phase must be layer_4"
    )
    due1 = select_due_missions(ctx1, [m])
    assert "market_wrapup" in [x["id"] for x in due1], (
        "TICK 1: the due L4 creates:[] mission MUST be selected for dispatch"
    )
    # FIX #277: materializer NOW stamps the per-mission marker for L4 missions.
    assert per_mission_marker.exists(), (
        "FIX #277: materialize_due_missions_for_tick must stamp "
        "outputs/<today>/missions/<id>/.last-run for L4 missions too, "
        "providing per-mission idempotence"
    )

    # ── TICK 2 ── (20:30 Paris, same day) — mission marker from prior tick → excluded.
    LATER_L4 = datetime(2026, 6, 23, 18, 30, tzinfo=timezone.utc)  # 20:30 Paris
    ctx2 = _full_ctx(repo, LATER_L4)
    due2 = select_due_missions(ctx2, [m])
    assert due2 == [], (
        "TICK 2 FIRE-SPIN GUARD (per-mission): per-mission marker from tick 1 "
        "is < now_utc → is_mission_due returns False → select_due_missions "
        "returns EXACTLY [] — per-mission idempotence prevents L4 fire-spin"
    )


# ── Three-L4-mission scenario: risk_control + market_wrapup + weekly_review ───
#
# This is the key correctness test for #277.  With the layer-wide cap removed,
# three L4 missions at different times must each fire exactly once per day/week:
#   • risk_control   — daily 21:00 Paris, uses legacy layers/4/PROMPT.md shim
#                      (no per-mission PROMPT.md → resolve_mission_prompt returns
#                      the layer shim, but the per-mission .last-run marker is
#                      still stamped by the materializer)
#   • market_wrapup  — daily 22:30 Paris, per-mission prompt
#   • weekly_review  — weekly Friday 17:00 Paris (Tuesday 2026-06-23 → NOT due today)
#
# Scenario time: 22:31 Paris (UTC+2 in June → 20:31 UTC).  At this point:
#   • risk_control has already fired (per-mission marker from 21:00)
#   • market_wrapup is now due (time 22:30 reached, no prior marker)
#   • weekly_review is not due (wrong day — Tuesday not Friday)
#
# Expected: select_due_missions returns [market_wrapup] ONLY.

AT_22_31_UTC = datetime(2026, 6, 23, 20, 31, tzinfo=timezone.utc)  # 22:31 Paris
AT_21_00_UTC = datetime(2026, 6, 23, 19, 0, tzinfo=timezone.utc)   # 21:00 Paris


def test_three_l4_missions_only_market_wrapup_due_at_2231(tmp_path: Path):
    """Three-L4-mission scenario: at 22:31 after risk_control fired at 21:00,
    select_due_missions returns [market_wrapup] only — not risk_control again,
    not weekly_review (wrong day).

    This is the PRIMARY correctness test for fix #277.
    """
    repo = _mk_repo(tmp_path)

    risk_control = _mk_daily("risk_control", layer=4, time="21:00",
                             output_queue="queues/research/", creates=[])
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30",
                              output_queue="queues/research/", creates=[])
    weekly_review = _mk_weekly("weekly_review", layer=4, time="17:00", day="friday",
                               output_queue="queues/research/")
    _write_dept_yaml(repo, [risk_control, market_wrapup, weekly_review])

    today = AT_22_31_UTC.strftime("%Y-%m-%d")

    # L1/L2/L3 prerequisites fired today.
    for n in (1, 2, 3):
        write_last_run(repo / "outputs" / today / str(n), MORNING)

    # risk_control fired at 21:00 Paris — per-mission marker from prior tick.
    _stamp_mission_lastrun(repo, "risk_control", AT_21_00_UTC)

    # risk_control also stamps the LAYER marker (as the primary shim does in prod).
    write_last_run(repo / "outputs" / today / "4", AT_21_00_UTC)

    # Build ctx at 22:31 Paris.
    ctx = _full_ctx(repo, AT_22_31_UTC)

    due = select_due_missions(ctx, [risk_control, market_wrapup, weekly_review])
    ids = [m["id"] for m in due]

    assert "market_wrapup" in ids, (
        "market_wrapup (daily 22:30 Paris) must be selected at 22:31 — "
        "it has not fired today and its time is reached"
    )
    assert "risk_control" not in ids, (
        "risk_control must NOT re-fire — it has a per-mission marker from 21:00 "
        "earlier today (same Paris day → is_mission_due returns False)"
    )
    assert "weekly_review" not in ids, (
        "weekly_review (Friday) must NOT appear — today is Tuesday 2026-06-23"
    )
    assert len(due) == 1, (
        f"exactly ONE mission (market_wrapup) must be due at 22:31; got {ids}"
    )


def test_risk_control_does_not_refire_same_day(tmp_path: Path):
    """risk_control fires once at 21:00 Paris and must not re-fire the same day.

    This test verifies that the per-mission marker (stamped by the materializer
    at 21:00 tick) correctly gates risk_control on subsequent ticks — proving
    that removing the layer-wide cap does NOT reintroduce a risk_control fire-spin.
    """
    repo = _mk_repo(tmp_path)
    risk_control = _mk_daily("risk_control", layer=4, time="21:00",
                             output_queue="queues/research/", creates=[])
    _write_dept_yaml(repo, [risk_control])

    today = AT_22_31_UTC.strftime("%Y-%m-%d")
    for n in (1, 2, 3):
        write_last_run(repo / "outputs" / today / str(n), MORNING)

    # risk_control per-mission marker stamped at 21:00 (prior tick).
    _stamp_mission_lastrun(repo, "risk_control", AT_21_00_UTC)

    # Check at 22:31 — well after 21:00 but still same Paris day.
    ctx = _full_ctx(repo, AT_22_31_UTC)
    due = select_due_missions(ctx, [risk_control])
    assert due == [], (
        "risk_control must NOT re-fire at 22:31 — per-mission marker from 21:00 "
        "same Paris day → is_mission_due daily gate returns False"
    )


def test_market_wrapup_fires_once_then_excluded(tmp_path: Path):
    """market_wrapup fires at 22:30 then is excluded on the next tick.

    tick 1 (22:31 Paris): market_wrapup selected; materializer stamps per-mission marker.
    tick 2 (23:00 Paris): per-mission marker < now_utc → excluded (no fire-spin).
    """
    repo = _mk_repo(tmp_path)
    market_wrapup = _mk_daily("market_wrapup", layer=4, time="22:30",
                              output_queue="queues/research/", creates=[])
    _write_dept_yaml(repo, [market_wrapup])

    today = AT_22_31_UTC.strftime("%Y-%m-%d")
    for n in (1, 2, 3):
        write_last_run(repo / "outputs" / today / str(n), MORNING)

    per_mission_marker = (repo / "outputs" / today / "missions"
                          / "market_wrapup" / ".last-run")
    assert not per_mission_marker.exists(), "precondition: no marker before tick 1"

    # ── TICK 1 ── market_wrapup is due; materializer stamps the per-mission marker.
    ctx1 = _full_ctx(repo, AT_22_31_UTC)
    due1 = select_due_missions(ctx1, [market_wrapup])
    assert "market_wrapup" in [m["id"] for m in due1], (
        "market_wrapup must be selected at 22:31 Paris (time 22:30 reached, never fired)"
    )
    assert per_mission_marker.exists(), (
        "materializer must stamp per-mission marker for market_wrapup on tick 1"
    )

    # ── TICK 2 ── (23:00 Paris) — per-mission marker from prior tick → excluded.
    AT_23_00_UTC = datetime(2026, 6, 23, 21, 0, tzinfo=timezone.utc)  # 23:00 Paris
    ctx2 = _full_ctx(repo, AT_23_00_UTC)
    due2 = select_due_missions(ctx2, [market_wrapup])
    assert due2 == [], (
        "market_wrapup must be EXCLUDED on tick 2 — per-mission marker from tick 1 "
        "is < now_utc → fire-spin guard active"
    )
