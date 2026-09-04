"""Tests for #1085 — Layer-4 evening debrief permanently gated shut on a
supervised-booking day with pending approved-but-unexecuted holds
(Géraldine/accountant, no export since 2026-06-24).

THE BUG: `decide_dispatch()`'s C.1 branch requires
`l1_fired and (l2_fired or not has_research) and (l3_fired or not has_decisions)
and not l4_fired` before Layer 4 is eligible. Since board #17 made booking
permanently human-supervised for this dept, Layer 3's STEP 0bis guard-rail
check blocks EVERY tick, forever (see `layer_templates.py::_L3`'s "if a
guard-rail blocks -> ABORT, do not stamp `.last-run`" contract) — so
`l3_fired` never becomes True. Meanwhile the ~28 approved decisions sit in
`inbox/decisions/` forever (nothing auto-archives an item awaiting a human's
hand), so `has_inbox_decisions` never becomes False. `(l3_fired or not
has_decisions)` is therefore False forever -> Layer 4 (the evening
`management-export.yaml` + `risk-brief.md` Tony reads) never fires.

INVESTIGATION FINDING (rejecting #757's option (a)): `has_inbox_decisions`
is NOT wrongly counting unapproved holds — every item that reaches
`inbox/decisions/` is, by construction, an ALREADY-APPROVED decision (see
`build_dispatch_ctx`'s docstring: the cockpit approve-click is what writes
there). So narrowing it to "approved-only" is a no-op for this bug; the gap
is that L3 had no way to say "I looked, and structurally there is nothing
*I* can do about this today" without also claiming — falsely — that it
might still execute later today (which would wrongly justify blocking L4).

THE FIX (option (b)): `write_l3_human_deferred()` — a new, distinctly-named
entry point (mechanically identical to `write_last_run`) that the L3
prompt's STRUCTURAL human-supervised-defer branch calls INSTEAD of leaving
`.last-run` unwritten. A TRANSIENT guard-rail block (kill-switch,
quiet-hours, quota) still uses the no-stamp ABORT path, so it keeps
retrying the same day and correctly keeps L4 waiting — proven by
`test_l4_does_not_fire_when_l3_has_not_run_and_decisions_are_fresh` below.
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
    write_last_run,
    write_l3_human_deferred,
)

# 2026-09-03, 22:00 UTC = 00:00 Paris the NEXT day in CEST (UTC+2)... avoid
# midnight edge cases: use 20:00 UTC = 22:00 Paris (CEST), well past L4's
# 19:00 Paris floor, same Paris calendar day as the UTC date used for
# `today_dir` (build_dispatch_ctx keys `today` off UTC, matching production).
_NOW = datetime(2026, 9, 3, 20, 0, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "inbox" / "decisions").mkdir(parents=True)
    (repo / "queues" / "research").mkdir(parents=True)
    # No dept.yaml — mirrors the manual-execution decision shape exactly
    # (booking decisions are NOT modeled as a recurring_mission; they are
    # ad-hoc cockpit-approved gate cards, per #757's investigation).
    return repo


def _drop_approved_decision(repo: Path, decision_id: str) -> None:
    (repo / "inbox" / "decisions" / f"{decision_id}.yaml").write_text(
        yaml.dump({"id": decision_id, "kind": "booking", "status": "approved"},
                  allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def test_l4_fires_on_supervised_booking_day_with_pending_holds(tmp_path: Path):
    """THE FIX, scenario (i): L1 ran today, no research backlog, ~28 (here: 2)
    approved decisions are STILL sitting unarchived in inbox/decisions/
    (awaiting a human to book by hand), and L3 recorded a STRUCTURAL
    human-supervised defer via `write_l3_human_deferred` (NOT a real
    execution — the decisions remain pending). L4 must still fire."""
    repo = _mk_repo(tmp_path)
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)
    _drop_approved_decision(repo, "hold-boursorama-1")
    _drop_approved_decision(repo, "hold-boursorama-2")
    # L3 ran this tick's (or an earlier tick's) check, confirmed nothing here
    # is autonomously executable, and stamped via the new structural-defer path.
    write_l3_human_deferred(repo / "outputs" / _TODAY / "3", when=_NOW,
                             reason="board #17 — booking is human-supervised")

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)

    assert ctx["has_inbox_decisions"] is True, (
        "precondition: the two pending holds must still be visible — they "
        "are NOT archived by the defer (a human still has to book them)"
    )
    assert decide_dispatch(ctx) == "layer_4", (
        "REGRESSION #1085: a supervised-booking day with pending holds must "
        "still open the evening debrief (management-export.yaml + "
        "risk-brief.md, 'the export Tony reads') once L3 has structurally "
        "deferred — waiting forever for an execution that will never "
        "happen autonomously is the bug (no export since 2026-06-24)."
    )


def test_l4_does_not_fire_when_l3_has_not_run_and_decisions_are_fresh(tmp_path: Path):
    """NO PREMATURE FIRE, scenario (ii): an approved trade that genuinely
    still needs L3 to execute it (autonomous dept, no structural block) must
    NOT let L4 fire just because it's past 19:00 Paris — L3 has not run
    (transient guard-rail / not yet dispatched this tick), so decide_dispatch
    must keep routing to layer_3, not skip ahead to layer_4."""
    repo = _mk_repo(tmp_path)
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)
    _drop_approved_decision(repo, "auto-alpaca-1")
    # Deliberately NOT calling write_l3_human_deferred / write_last_run for
    # layer 3 — L3 has not run today at all (e.g. it's about to be
    # dispatched this very tick, or a transient guard-rail is still active).

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)

    assert ctx["has_inbox_decisions"] is True
    assert decide_dispatch(ctx) == "layer_3", (
        "an approved decision with L3 genuinely not-yet-run must keep "
        "routing to layer_3 (C.3 outranks the L4 gate) — L4 must not fire "
        "prematurely while there is still a real chance of autonomous "
        "execution today"
    )


def test_l4_fires_normally_once_l3_actually_executes(tmp_path: Path):
    """NO REGRESSION: the ORDINARY path — L3 genuinely executes (archives the
    decision to `.processed/` and stamps its OWN `.last-run` via the plain
    STEP 1 `write_last_run`, exactly as `layer_templates.py::_L3` documents
    for a successful run) — must still open L4 exactly as before this fix.
    `write_l3_human_deferred` is not involved; a plain `write_last_run` is
    functionally sufficient (both write the same marker), proving the new
    function is additive, not a behavioural fork."""
    repo = _mk_repo(tmp_path)
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)
    # Decision executed and archived — inbox/decisions/ is now empty.
    write_last_run(repo / "outputs" / _TODAY / "3", when=_NOW)

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)

    assert ctx["has_inbox_decisions"] is False
    assert decide_dispatch(ctx) == "layer_4"


def test_write_l3_human_deferred_commits_to_dispatch_ledger(tmp_path: Path):
    """A structural defer is a terminal ledger outcome, not a marker."""
    from scripts.lib.dispatch_helpers import read_dispatch_ledger, read_last_run

    layer3_dir = tmp_path / "outputs" / _TODAY / "3"
    write_l3_human_deferred(layer3_dir, when=_NOW)
    assert read_last_run(layer3_dir) is None
    entry = read_dispatch_ledger(layer3_dir.parent)["__ad_hoc_l3_human_defer__"]
    assert entry["completed_at"] == _NOW.isoformat()
