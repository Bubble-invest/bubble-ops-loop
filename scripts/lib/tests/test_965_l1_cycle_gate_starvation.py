"""Tests for #965 — a structurally-blocked Layer 3 decision permanently
starves Layer 1's re-fire (the daily OODA cycle silently stalls). Same class
as #432/#1085 (a subtle, state-dependent gate that silently kills a layer for
the rest of the day) — but for L1's CYCLE-GATE re-fire, not L4's C.1 gate.

THE BUG: `decide_dispatch`/`select_due_missions`'s C.0 branch (b) re-fires
Layer 1 once L2, L3 and L4 have EACH completed >= `fire_after_rounds` since
L1's last fire (`_layer_eligible_from_signals`, layer==1 — the "cycle gate").
"Completed a round" is measured via `round_counter`, which is bumped ONLY by
`increment_round_counter` — the documented LAST action of a layer's OWN
successful PROMPT.md run (see `skills/department-onboarding-guide/SKILL.md`).

#1085 (2026-09-03, same day) gave Layer 3 a STRUCTURAL human-supervised-defer
signal, `write_l3_human_deferred()`, for decisions that can NEVER be executed
autonomously (e.g. board #17 human-supervised booking) — but that function
only stamped `.last-run` (fixing L4's `l3_fired` gate), not `round_counter`.
So a structurally-blocked L3 NEVER advances `round_counter["3"]` -> L1's cycle
gate can never be satisfied again for the rest of the day -> Layer 1 becomes
flatly INELIGIBLE (not merely "eligible with an empty due list", which
`select_due_missions`'s existing #757 fallthrough already handles — an
INELIGIBLE layer is skipped outright by the `_LAYER_PRIORITY` walk, so no
amount of fallthrough can rescue it) -> a genuinely-due SECONDARY L1 mission
(e.g. an hourly "pure report" mission, unrelated to L3's block) is silently
starved, even in the EVENING after L2 and L4 have both genuinely completed
their own rounds for the day.

THE FIX: `write_l3_human_deferred()` now ALSO calls
`increment_round_counter(..., layer=3)` — a structural defer means L3's
"round" is exactly as complete as it will ever be today, so it should count
for L1's cycle gate exactly like a real completed round does. See that
function's updated docstring in `dispatch_helpers.py`.

RED before the fix (verified during investigation by temporarily reverting
just the `increment_round_counter` call): the EVENING tick's `due` came back
`[]` even though L2 and L4 had both genuinely completed a fresh round and an
hourly L1 mission was genuinely due — L1's cycle gate is permanently stuck
requiring `round_counter["3"]` to advance, which only a real L3 completion or
a structural defer can do.
GREEN after: `write_l3_human_deferred` advances `round_counter["3"]`, L1's
cycle gate opens once L2 and L4 also complete their rounds, and
`select_due_missions` returns the due L1 mission.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    build_dispatch_ctx,
    commit_dispatch,
    decide_dispatch,
    event_trigger_ids_for_dispatch,
    select_due_missions,
    write_last_run,
    write_l3_human_deferred,
    increment_round_counter,
    write_l1_baseline,
    read_round_counter,
    read_last_run,
    read_dispatch_ledger,
)

_TODAY = "2026-09-03"


def _mk_repo(tmp_path: Path, missions: list[dict]) -> Path:
    (tmp_path / "queues" / "research").mkdir(parents=True)
    (tmp_path / "queues" / "gates").mkdir(parents=True)
    (tmp_path / "queues" / "trades").mkdir(parents=True)
    (tmp_path / "inbox" / "decisions").mkdir(parents=True)
    (tmp_path / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )
    return tmp_path


def _drop_decision(repo: Path, decision_id: str) -> None:
    (repo / "inbox" / "decisions" / f"{decision_id}.yaml").write_text(
        yaml.dump({"id": decision_id, "kind": "booking", "status": "approved"},
                  allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _missions() -> list[dict]:
    """A dept.yaml shape with a SECONDARY, unrelated L1 mission (`news_scan`,
    hourly) alongside the primary L1/L2/L3/L4 missions. `news_scan` uses
    `creates: []` (a "pure report" mission, matching ben's `market_wrapup`
    pattern documented in `materialize_due_missions_for_tick`) so its own
    idempotence runs purely through the per-mission `.last-run` marker, not
    through a queue-item dedup check — isolating the cycle-gate behaviour
    this test is about from the unrelated queue-drain mechanics.

    Deliberately does NOT reuse `queues/management/` for any mission's
    `output_queue` (that directory is separately scanned for inbound
    management notes; reusing it would falsely flip
    `has_unconsumed_mgmt_notes`, masking the cycle-gate signal this test
    exercises)."""
    return [
        {
            "id": "data_update", "layer": 1, "cadence": "daily", "time": "07:00",
            "output_queue": "queues/research/", "creates": ["research_item"],
        },
        {
            "id": "news_scan", "layer": 1, "cadence": "hourly",
            "output_queue": "queues/trades/", "creates": [],
        },
        {
            "id": "research", "layer": 2, "cadence": "daily", "time": "12:00",
            "output_queue": "queues/gates/", "input_queue": "queues/research/",
            "creates": ["investment_case"],
        },
        {
            "id": "execution", "layer": 3, "cadence": "event",
            "input_queue": "inbox/decisions", "output_queue": "queues/trades/",
            "creates": ["executed_trade"],
        },
        {
            "id": "risk_control", "layer": 4, "cadence": "daily", "time": "19:00",
            "output_queue": "queues/trades/", "creates": ["risk_report"],
        },
        {
            "id": "weekly_review", "layer": 4, "cadence": "weekly", "day": "friday",
            "time": "18:00", "output_queue": "queues/trades/",
            "creates": ["weekly_kpi_review"],
        },
    ]


def _run_full_ooda_day(repo: Path, missions: list[dict], today_dir: Path) -> dict:
    """Drive a realistic sequence of ticks through ONE UTC day (2026-09-03,
    a Thursday — so `weekly_review` never fires) and return the FINAL
    evening ctx + due list. Mirrors how the live /loop actually operates:
    each tick calls `build_dispatch_ctx` (which scans recurring
    missions) then `select_due_missions`; a mission's OWN layer-completion
    state is advanced by `commit_dispatch` only after a successful return."""
    # Tick 0 — 07:00 Paris: L1's morning floor fires (data_update + news_scan).
    now0 = datetime(2026, 9, 3, 5, 0, tzinfo=timezone.utc)
    ctx0 = build_dispatch_ctx(repo, now_utc=now0)
    ctx0["_repo_dir"] = str(repo)
    due0 = select_due_missions(ctx0, missions)
    write_last_run(today_dir / "1", when=now0)
    # #1080 output-truth: a layer marker alone is no longer sufficient
    # evidence of a genuine L1 fire (L1 is output-evidence-gated) — write the
    # real STEP-3 artifact too, matching what a genuinely-completed L1 run
    # produces.
    (today_dir / "1").mkdir(parents=True, exist_ok=True)
    (today_dir / "1" / "situation_brief.md").write_text("ok")
    for mission in due0:
        commit_dispatch(
            repo, mission, dispatched_at=now0, completed_at=now0,
            artifacts=[today_dir / "1" / "situation_brief.md"],
        )

    # An approved decision arrives that can NEVER execute autonomously
    # (board-#17-shaped human-supervised booking).
    _drop_decision(repo, "hold-boursorama-1")

    # Tick 1 — 09:00 Paris: L3 is dispatched for the blocked decision and,
    # per its STEP 0bis contract, structurally defers.
    now1 = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    ctx1 = build_dispatch_ctx(repo, now_utc=now1)
    ctx1["_repo_dir"] = str(repo)
    due1 = select_due_missions(ctx1, missions)
    for mission in due1:
        trigger_ids = event_trigger_ids_for_dispatch(ctx1, mission)
        commit_dispatch(
            repo,
            mission,
            dispatched_at=now1,
            completed_at=now1,
            artifacts=[],
            dispatched_trigger_ids=trigger_ids,
            materialize_outputs=False,
        )

    # Tick 2 — 12:00 Paris: L2's research consumer genuinely fires and
    # completes (consumes the item data_update produced at tick 0).
    now2 = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    ctx2 = build_dispatch_ctx(repo, now_utc=now2)
    ctx2["_repo_dir"] = str(repo)
    due2 = select_due_missions(ctx2, missions)
    if "research" in [m["id"] for m in due2]:
        research = next(m for m in due2 if m["id"] == "research")
        commit_dispatch(repo, research, dispatched_at=now2, completed_at=now2,
                        artifacts=[], materialize_outputs=False)
        for f in (repo / "queues" / "research").glob("*.yaml"):
            f.unlink()

    # Tick 3 — 19:00 Paris: L4's daily primary (`risk_control`) genuinely
    # fires and completes.
    now3 = datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc)
    ctx3 = build_dispatch_ctx(repo, now_utc=now3)
    ctx3["_repo_dir"] = str(repo)
    due3 = select_due_missions(ctx3, missions)
    if "risk_control" in [m["id"] for m in due3]:
        # #1080 output-truth: L4 is output-evidence-gated too — write the real
        # STEP-3 artifact alongside the marker, or a later tick would (per the
        # new invariant, correctly) treat risk_control as died-mid-dispatch
        # and re-select it, out-ranking L1 in _LAYER_PRIORITY and masking the
        # exact starvation this test isolates.
        (today_dir / "4").mkdir(parents=True, exist_ok=True)
        (today_dir / "4" / "risk-brief.md").write_text("ok")
        risk = next(m for m in due3 if m["id"] == "risk_control")
        commit_dispatch(repo, risk, dispatched_at=now3, completed_at=now3,
                        artifacts=[today_dir / "4" / "risk-brief.md"])

    # Tick 4 — 19:30 Paris: the tick under test.
    now4 = datetime(2026, 9, 3, 17, 30, tzinfo=timezone.utc)
    ctx4 = build_dispatch_ctx(repo, now_utc=now4)
    ctx4["_repo_dir"] = str(repo)
    due4 = select_due_missions(ctx4, missions)

    return {"ctx": ctx4, "due": due4, "now": now4}


def test_hourly_l1_mission_starved_by_structurally_blocked_l3(tmp_path: Path):
    """THE BUG (pre-fix) / THE FIX (post-fix): once L1's morning floor, L2's
    research round, and L4's daily primary have ALL genuinely completed for
    the day, and L3 has structurally deferred an unexecutable approved
    decision, a genuinely-due, UNRELATED hourly L1 mission (`news_scan`) must
    still be dispatched via L1's cycle-gate re-fire — not silently swallowed
    because L3's structural defer never advanced `round_counter["3"]`."""
    missions = _missions()
    repo = _mk_repo(tmp_path, missions)
    today_dir = repo / "outputs" / _TODAY

    result = _run_full_ooda_day(repo, missions, today_dir)
    ctx, due = result["ctx"], result["due"]
    ids = [m["id"] for m in due]

    assert ctx["has_inbox_decisions"] is True, (
        "precondition: the structurally-deferred decision is still pending "
        "(a human still has to book it by hand) — has_inbox_decisions stays True"
    )
    assert decide_dispatch(ctx) == "layer_3", (
        "precondition: decide_dispatch's phase string is still pinned to "
        "layer_3 by has_inbox_decisions — the fix does not touch decide_dispatch"
    )
    assert read_round_counter(today_dir).get("3") == 1, (
        "precondition: COMMIT must advance round_counter['3'] for the "
        "validated structural-defer return"
    )

    assert "news_scan" in ids, (
        "REGRESSION #965: a genuinely-due, unrelated hourly L1 mission must "
        "not be starved by a structurally-blocked L3 decision, even after "
        "L2 and L4 have both genuinely completed their own rounds for the "
        "day. Before the fix, write_l3_human_deferred never advanced "
        "round_counter['3'], so L1's cycle gate "
        "(_layer_eligible_from_signals, layer==1) could never be satisfied "
        "again today -> L1 was skipped as INELIGIBLE by select_due_missions' "
        "layer walk (not merely 'eligible with an empty due list', which "
        "the #757 fallthrough already handles) -> due == []."
    )


def test_transient_guard_rail_block_does_not_advance_round_counter(tmp_path: Path):
    """NO REGRESSION: a TRANSIENT guard-rail ABORT (kill-switch, quiet-hours,
    quota — the STEP 0bis path that deliberately does NOT call
    `write_l3_human_deferred`, per its own contract) must NOT advance
    `round_counter['3']` — L3 must keep being retried the same day, and L1's
    cycle gate must correctly stay closed (there is still a real chance L3
    executes later today)."""
    missions = _missions()
    repo = _mk_repo(tmp_path, missions)
    today_dir = repo / "outputs" / _TODAY

    now0 = datetime(2026, 9, 3, 5, 0, tzinfo=timezone.utc)
    write_last_run(today_dir / "1", when=now0)
    increment_round_counter(today_dir, layer=1)
    write_l1_baseline(today_dir)

    _drop_decision(repo, "auto-alpaca-1")
    # Deliberately NOT calling write_l3_human_deferred / write_last_run for
    # layer 3 at all — a transient guard-rail block leaves NO marker, per the
    # canonical STEP 0bis contract (see layer_templates.py::_L3).

    assert read_round_counter(today_dir) == {"1": 1}, (
        "a transient block must never touch round_counter['3'] — only a "
        "STRUCTURAL defer (write_l3_human_deferred) or a real completed run "
        "(increment_round_counter) may do so"
    )


def test_l4_fires_normally_and_no_regression_on_ordinary_l3_completion(tmp_path: Path):
    """NO REGRESSION: the ORDINARY path — L3 genuinely executes (a plain
    `write_last_run` + `increment_round_counter(layer=3)`, no structural
    defer involved) — must still open both L4's gate (#1085, unaffected)
    and, once L2/L4 also complete, L1's cycle gate exactly as before."""
    missions = _missions()
    repo = _mk_repo(tmp_path, missions)
    today_dir = repo / "outputs" / _TODAY

    now0 = datetime(2026, 9, 3, 5, 0, tzinfo=timezone.utc)
    write_last_run(today_dir / "1", when=now0)
    # data_update already ran this morning too — else the materializer would
    # auto-produce a fresh research_item at the evening tick below (it is
    # still is_mission_due otherwise), which would legitimately give L2 due
    # work this tick and outrank L1 — not what this test is isolating.
    write_last_run(today_dir / "missions" / "data_update", when=now0)
    # #1080 output-truth: L1 is output-evidence-gated — a marker alone (layer
    # or per-mission) is no longer sufficient proof data_update genuinely
    # produced its brief.
    (today_dir / "1").mkdir(parents=True, exist_ok=True)
    (today_dir / "1" / "situation_brief.md").write_text("ok")
    increment_round_counter(today_dir, layer=1)
    write_l1_baseline(today_dir)

    # L3 genuinely executes and completes its round the ordinary way.
    now1 = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    write_last_run(today_dir / "3", when=now1)
    increment_round_counter(today_dir, layer=3)
    increment_round_counter(today_dir, layer=2)
    # L4's daily primary (risk_control) also genuinely completes today —
    # stamp its OWN per-mission marker too, else it would still be due at
    # now_evening and correctly outrank L1 this tick (L4 > L1 priority),
    # which is not what this test is isolating.
    now_l4 = datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc)
    write_last_run(today_dir / "missions" / "risk_control", when=now_l4)
    # #1080 output-truth: L4 is output-evidence-gated too.
    (today_dir / "4").mkdir(parents=True, exist_ok=True)
    (today_dir / "4" / "risk-brief.md").write_text("ok")
    increment_round_counter(today_dir, layer=4)

    now_evening = datetime(2026, 9, 3, 17, 30, tzinfo=timezone.utc)
    ctx = build_dispatch_ctx(repo, now_utc=now_evening)
    ctx["_repo_dir"] = str(repo)

    due = select_due_missions(ctx, missions)
    ids = [m["id"] for m in due]
    assert "news_scan" in ids, (
        "L1's cycle gate must open once L2/L3/L4 have each completed a "
        "fresh round the ordinary way — no regression from the #965 fix"
    )


def test_write_l3_human_deferred_advances_round_counter_directly(tmp_path: Path):
    """Direct unit check on the fixed function: round_counter['3'] goes from
    absent to 1 after a single `write_l3_human_deferred` call, alongside the
    pre-existing `.last-run` stamp (unchanged #1085 behaviour)."""
    layer3_dir = tmp_path / "outputs" / _TODAY / "3"
    when = datetime(2026, 9, 3, 7, 0, tzinfo=timezone.utc)
    write_l3_human_deferred(layer3_dir, when=when)

    assert read_last_run(layer3_dir) is None
    assert read_dispatch_ledger(layer3_dir.parent)[
        "__ad_hoc_l3_human_defer__"
    ]["completed_at"] == when.isoformat()
    assert read_round_counter(layer3_dir.parent) == {"3": 1}
