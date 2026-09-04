"""Tests for #1085 — the MISSING CALLER of `write_l3_human_deferred` on the
ad-hoc-inbox-decision path (Géraldine/accountant, no export since 2026-08-27
per #1094).

THE FULL DEADLOCK CHAIN (confirmed in the card comment, and re-verified
here): a dept whose booking decisions are ad-hoc `inbox/decisions/` items
with NO recurring L3 mission in `dept.yaml::recurring_missions` hits:

  1. `decide_dispatch(ctx)` resolves C.3 -> `"layer_3"` (an approved decision
     is waiting: `has_inbox_decisions=True`, L1's 07:00 floor reached).
  2. `select_due_missions(ctx, missions)` walks layer 3's own due-list and
     finds it EMPTY — there is no layer-3 mission that could ever be "due".
  3. The /loop therefore spawns NOTHING for layer 3 this tick (a heartbeat,
     in effect) — the L3 subagent, whose STEP 0bis would call
     `write_l3_human_deferred`, never runs.
  4. `outputs/<today>/3/.last-run` is never stamped -> `l3_fired` stays False
     forever (the ad-hoc decision is never archived, so `has_inbox_decisions`
     never flips False either) -> L4's C.1 prerequisite
     `(l3_fired or not has_decisions)` can never be satisfied -> L4
     (`management-export.yaml` + `risk-brief.md`, "the export Tony reads")
     never fires.

`write_l3_human_deferred` (#335/#965) is the correct primitive for "L3
looked, and structurally there is nothing it can do today" — see
`test_1085_l4_export_gate.py` for decide_dispatch's C.1 behaviour once it
HAS been called. This file tests the missing piece: `maybe_defer_ad_hoc_l3`,
the caller that stamps it automatically on this exact starved path.

WIRED IN TWO PLACES (code-level fix, not template-only — see the PR
discussion on #1085): (1) `build_dispatch_ctx`'s `materialize=True` branch in
`dispatch_helpers.py` — this is what makes the fix reach every dept
automatically the instant its vendored `dispatch_helpers.py` is refreshed,
with NO CLAUDE.md edit needed (an already-onboarded dept's live protocol
text was baked at onboarding time and does not pick up a template-only
change); and (2) the /loop's documented STEP C prose right after
`select_due_missions` in `scripts/lib/scaffold.py` (belt-and-suspenders +
visibility for future depts).

Most tests below build `ctx` with `build_dispatch_ctx(..., materialize=False)`
and then call `maybe_defer_ad_hoc_l3` directly — this isolates the FUNCTION's
own decision logic from `build_dispatch_ctx`'s new automatic call, so a test
doesn't end up invoking the defer twice in one "tick" (once inside
`build_dispatch_ctx`, once manually) and miscounting `round_counter`. The
`materialize=True` INTEGRATION behaviour (the code-level wiring itself, and
the #454 read-only-probe invariant that `materialize=False` must never
stamp) is covered explicitly by
`test_build_dispatch_ctx_materialize_true_stamps_l3_and_unblocks_l4` and
`test_build_dispatch_ctx_materialize_false_does_not_stamp` at the bottom of
this file.
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
    decide_dispatch,
    select_due_missions,
    maybe_defer_ad_hoc_l3,
    read_dispatch_ledger,
    read_last_run,
    read_round_counter,
    write_last_run,
)

# Thursday 2026-09-03, 20:00 UTC = 22:00 Paris (CEST, UTC+2) — well past every
# layer's Paris-local minimum fire time (L1 07:00 .. L4 19:00), same Paris
# calendar day as the UTC date `build_dispatch_ctx` keys `today_dir` off of.
_NOW = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")

# No recurring L3 mission anywhere in this list — the exact ad-hoc-inbox-
# decision shape from the card (accountant DOES have `daily_risk_audit`, an
# L4 mission; L3 is ad-hoc-only).
_NO_L3_MISSIONS = [
    {
        "id": "daily_risk_audit",
        "layer": 4,
        "cadence": "daily",
        "time": "21:30",
        "output_queue": "queues/management/",
        "creates": ["risk_audit"],
    },
]


def _mk_repo(tmp_path: Path, missions: list[dict]) -> Path:
    repo = tmp_path / "repo"
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "inbox" / "decisions").mkdir(parents=True)
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )
    return repo


def _drop_approved_decision(repo: Path, decision_id: str) -> None:
    (repo / "inbox" / "decisions" / f"{decision_id}.yaml").write_text(
        yaml.dump({"id": decision_id, "kind": "booking", "status": "approved"},
                  allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _drop_research_item(repo: Path, item_id: str) -> None:
    (repo / "queues" / "research" / f"{item_id}.yaml").write_text(
        yaml.dump({"id": item_id, "kind": "research_item"},
                  allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def test_reproduces_the_deadlock_before_the_fix(tmp_path: Path):
    """Pin the exact starved state the card describes: C.3 resolves, but the
    due-set for layer 3 is empty because no recurring L3 mission exists."""
    repo = _mk_repo(tmp_path, _NO_L3_MISSIONS)
    _drop_approved_decision(repo, "hold-boursorama-1")
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)  # L1 already ran

    # materialize=False: a pure read of decide_dispatch/select_due_missions'
    # own selection logic, undisturbed by build_dispatch_ctx's own
    # maybe_defer_ad_hoc_l3 call (which only runs in the materialize=True
    # branch) — this is what "before the fix" looked like structurally.
    ctx = build_dispatch_ctx(repo, now_utc=_NOW, materialize=False)
    assert ctx["has_inbox_decisions"] is True
    phase = decide_dispatch(ctx)
    assert phase == "layer_3", "C.3 must resolve while an approved decision is pending"

    due = select_due_missions(ctx, _NO_L3_MISSIONS)
    assert due == [], (
        "the due-set for layer 3 must be empty — there is no recurring L3 "
        "mission at all for this dept (the ad-hoc-inbox-decision case)"
    )
    assert read_last_run(repo / "outputs" / _TODAY / "3") is None, (
        "BEFORE the fix: nothing ever stamps L3's .last-run on this path"
    )


def test_maybe_defer_ad_hoc_l3_unblocks_l4(tmp_path: Path):
    """THE FIX: once `select_due_missions` comes back empty on layer_3 with
    no recurring L3 mission declared, `maybe_defer_ad_hoc_l3` stamps L3
    handled-today — and a fresh ctx built afterward shows L4 (the dept's real
    `daily_risk_audit` mission) becomes eligible, exactly as
    `write_l3_human_deferred`'s existing STEP-0bis caller already achieves
    when it DOES run (see test_1085_l4_export_gate.py)."""
    repo = _mk_repo(tmp_path, _NO_L3_MISSIONS)
    _drop_approved_decision(repo, "hold-boursorama-1")
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)

    # Isolated unit-test of the function: materialize=False so
    # build_dispatch_ctx itself does not ALSO call maybe_defer_ad_hoc_l3
    # (that integration path is covered separately below).
    ctx = build_dispatch_ctx(repo, now_utc=_NOW, materialize=False)
    phase = decide_dispatch(ctx)
    due = select_due_missions(ctx, _NO_L3_MISSIONS)
    assert phase == "layer_3" and due == []

    wrote = maybe_defer_ad_hoc_l3(ctx, _NO_L3_MISSIONS, phase=phase)
    assert wrote is True, "must stamp on the genuinely-starved ad-hoc case"

    ledger = read_dispatch_ledger(repo / "outputs" / _TODAY)
    assert ledger["__ad_hoc_l3_human_defer__"]["completed_at"] == _NOW.isoformat()
    assert read_round_counter(repo / "outputs" / _TODAY).get("3") == 1, (
        "#965: a structural defer must also advance round_counter[3], or "
        "L1's cycle gate can never be satisfied again today"
    )

    # Rebuild ctx from disk (a fresh tick, real materialize=True path this
    # time) — L4 must now be eligible: L1 fired, no research backlog
    # (vacuously satisfied), L3 now fired (via the defer), L4 not yet run.
    # The marker is already on disk, so build_dispatch_ctx's own internal
    # maybe_defer_ad_hoc_l3 call is a no-op here (guard 3) — no double-stamp.
    ctx2 = build_dispatch_ctx(repo, now_utc=_NOW)
    assert ctx2["round_counter"]["3"] == 1
    assert decide_dispatch(ctx2) == "layer_4", (
        "L4 (daily_risk_audit / management-export.yaml) must become eligible "
        "once L3 is marked handled-today — this is the #1094 symptom (no "
        "export) this fix closes"
    )


def test_maybe_defer_ad_hoc_l3_is_idempotent_no_fire_spin(tmp_path: Path):
    """#302/#965 GUARD: with a research backlog keeping L4 gated shut for an
    unrelated reason (so `decide_dispatch` keeps resolving to `"layer_3"`
    tick after tick, since C.3 carries no `not l3_fired` gate), a SECOND tick
    must NOT re-stamp `.last-run` or re-increment `round_counter` — the exact
    re-fire-loop class #302/#965 guard against."""
    repo = _mk_repo(tmp_path, _NO_L3_MISSIONS)
    _drop_approved_decision(repo, "hold-boursorama-1")
    _drop_research_item(repo, "unrelated-research-item")  # keeps L4 gated (l2 not fired)
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)

    # Isolated unit-test of the function's own idempotence, decoupled from
    # build_dispatch_ctx's automatic call (materialize=False on both ticks).
    ctx1 = build_dispatch_ctx(repo, now_utc=_NOW, materialize=False)
    phase1 = decide_dispatch(ctx1)
    assert phase1 == "layer_3"
    assert ctx1["has_research_items"] is True

    wrote1 = maybe_defer_ad_hoc_l3(ctx1, _NO_L3_MISSIONS, phase=phase1)
    assert wrote1 is True
    stamped_at = read_dispatch_ledger(repo / "outputs" / _TODAY)[
        "__ad_hoc_l3_human_defer__"
    ]["completed_at"]
    assert stamped_at == _NOW.isoformat()
    assert read_round_counter(repo / "outputs" / _TODAY).get("3") == 1

    # Tick 2, same Paris day, later in the day: decide_dispatch still resolves
    # "layer_3" (has_decisions is still True — the ad-hoc item is never
    # archived — and the research backlog still blocks L4's C.1 regardless
    # of L3's status).
    later = _NOW.replace(hour=21, minute=30)
    ctx2 = build_dispatch_ctx(repo, now_utc=later, materialize=False)
    phase2 = decide_dispatch(ctx2)
    assert phase2 == "layer_3", "L3 keeps shadowing while has_decisions stays True (no not-l3_fired gate in C.3)"

    wrote2 = maybe_defer_ad_hoc_l3(ctx2, _NO_L3_MISSIONS, phase=phase2)
    assert wrote2 is False, "must be a no-op — L3 already handled today (#302/#965 fire-spin guard)"

    # Nothing was touched a second time.
    assert read_dispatch_ledger(repo / "outputs" / _TODAY)[
        "__ad_hoc_l3_human_defer__"
    ]["completed_at"] == stamped_at
    assert read_round_counter(repo / "outputs" / _TODAY).get("3") == 1, (
        "round_counter[3] must NOT be incremented a second time this day"
    )


def test_maybe_defer_ad_hoc_l3_leaves_a_real_pending_l3_mission_alone(tmp_path: Path):
    """NO FALSE POSITIVE: a dept WITH a real recurring L3 mission (e.g. Ben's
    `execution`) that simply is not due YET this tick (its own `time:` gate
    not reached) must NOT be deferred — it may still genuinely fire later
    today. The signal is "no recurring L3 mission AT ALL", never "this
    tick's due-list happens to be empty"."""
    missions_with_real_l3 = _NO_L3_MISSIONS + [
        {
            "id": "execution",
            "layer": 3,
            "cadence": "daily",
            "time": "23:00",  # later than _NOW's Paris time (22:00) -> not due yet
            "output_queue": "queues/management/",
            "creates": ["executed_trade"],
        },
    ]
    repo = _mk_repo(tmp_path, missions_with_real_l3)
    _drop_approved_decision(repo, "auto-alpaca-1")
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)
    phase = decide_dispatch(ctx)
    assert phase == "layer_3"
    due = select_due_missions(ctx, missions_with_real_l3)
    assert due == [], "execution's own time gate (23:00) has not been reached yet"

    wrote = maybe_defer_ad_hoc_l3(ctx, missions_with_real_l3, phase=phase)
    assert wrote is False, (
        "a real recurring L3 mission exists for this dept — it may still "
        "fire later today; must NOT be falsely deferred"
    )
    assert read_last_run(repo / "outputs" / _TODAY / "3") is None


def test_maybe_defer_ad_hoc_l3_noop_when_phase_is_not_layer_3(tmp_path: Path):
    """Sanity: with no inbox decisions at all, decide_dispatch never resolves
    layer_3, and the function must be a strict no-op (nothing to defer)."""
    repo = _mk_repo(tmp_path, _NO_L3_MISSIONS)
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)
    phase = decide_dispatch(ctx)
    assert phase != "layer_3"

    wrote = maybe_defer_ad_hoc_l3(ctx, _NO_L3_MISSIONS, phase=phase)
    assert wrote is False
    assert read_last_run(repo / "outputs" / _TODAY / "3") is None


def test_build_dispatch_ctx_is_pure_before_explicit_l3_defer(tmp_path: Path):
    """#1117: even the former materialize path cannot commit during DECIDE."""
    repo = _mk_repo(tmp_path, _NO_L3_MISSIONS)
    _drop_approved_decision(repo, "hold-boursorama-1")
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)

    assert read_last_run(repo / "outputs" / _TODAY / "3") is None, "precondition"

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)
    phase = decide_dispatch(ctx)
    assert phase == "layer_3"
    assert read_dispatch_ledger(repo / "outputs" / _TODAY) == {}
    assert maybe_defer_ad_hoc_l3(ctx, _NO_L3_MISSIONS, phase=phase)
    assert read_round_counter(repo / "outputs" / _TODAY).get("3") == 1

    # A FRESH tick now sees L3 as fired and L4 becomes eligible — the actual
    # #1094 symptom (no management-export.yaml) this closes.
    ctx2 = build_dispatch_ctx(repo, now_utc=_NOW)
    assert decide_dispatch(ctx2) == "layer_4"


def test_build_dispatch_ctx_materialize_false_does_not_stamp(tmp_path: Path):
    """#454 READ-ONLY-PROBE INVARIANT (the same one `reconcile_gate_dir`
    already respects, right above this call site): a `materialize=False`
    caller — e.g. `loop-backup.sh`'s FORCE_LAYER pre-wake eligibility probe —
    must NEVER stamp a marker as a side effect of just checking state. The
    new `maybe_defer_ad_hoc_l3` call sits in the SAME `if materialize:`
    branch, so a probe call must leave L3's `.last-run` untouched."""
    repo = _mk_repo(tmp_path, _NO_L3_MISSIONS)
    _drop_approved_decision(repo, "hold-boursorama-1")
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)

    ctx = build_dispatch_ctx(repo, now_utc=_NOW, materialize=False)
    assert decide_dispatch(ctx) == "layer_3"

    assert read_last_run(repo / "outputs" / _TODAY / "3") is None, (
        "a materialize=False probe must NEVER write outputs/<today>/3/.last-run "
        "(or bump round_counter) as a side effect — #454"
    )
    assert read_round_counter(repo / "outputs" / _TODAY).get("3", 0) == 0

    # Calling the probe repeatedly changes nothing either.
    build_dispatch_ctx(repo, now_utc=_NOW, materialize=False)
    build_dispatch_ctx(repo, now_utc=_NOW, materialize=False)
    assert read_last_run(repo / "outputs" / _TODAY / "3") is None
