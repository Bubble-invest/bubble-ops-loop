"""Tests for #1407 — `decide_dispatch`'s Layer-4 gate silently strands the
evening debrief when L1 did not fire today.

THE BUG: `decide_dispatch()`'s C.1 branch required
`l1_fired and (l2_fired or not has_research) and (l3_fired or not has_decisions)
and not l4_fired` before Layer 4 (the evening CEO debrief / risk_control
export) was eligible. `l1_fired` was intended to SEQUENCE the aggregator
after L1's same-day situation_brief.md exists — but it was a HARD block, and
L1's own daily-floor catch-up (`decide_dispatch`'s C.0 branch, `not
l1_fired`) is the LOWEST-priority branch in the function. On a day where L1
never fires (dept down/quiet all morning), the FIRST tick that reaches the
evening window falls through C.1 (blocked on l1_fired) all the way to C.0
and dispatches Layer 1 instead — consuming that tick. If that is the day's
only remaining tick (self-paced /loop wake-arming treats a quiet evening as
"all done, sleep until tomorrow 08:03 Paris" per scaffold.py), Layer 4 never
gets a look-in that day: no risk-brief.md / management-export.yaml, nothing
for Tony to read.

THE FIX: L4's own eligibility (both in `decide_dispatch`'s C.1 branch and its
mirror `_layer_eligible_from_signals`, layer==4) no longer requires
`l1_fired`. L1's independent daily-floor guarantee (C.0) is UNCHANGED — it
still fires L1 at least once/day on whichever tick isn't otherwise claimed;
this fix only stops L4 from hard-blocking on that separate guarantee. When
L1 DID fire today (the ordinary case), `l1_fired` was already True, so this
is a no-op for that path — see `test_l4_still_fires_normally_when_l1_fired`
below for the explicit non-regression proof.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    build_dispatch_ctx,
    decide_dispatch,
    write_last_run,
)

# 19:30 Paris (CEST, UTC+2) = 17:30 UTC — past L4's 19:00 Paris floor, same
# Paris calendar day as the UTC date used for `today_dir`.
_NOW = datetime(2026, 9, 22, 17, 30, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")


def _mk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "queues" / "inbox" / "decisions").mkdir(parents=True)
    # No dept.yaml — exercises decide_dispatch's phase-string contract
    # directly, same shape as test_1085_l4_export_gate.py.
    return repo


def test_l4_fires_when_l1_did_not_fire_today(tmp_path: Path):
    """THE FIX: a quiet/no-research, no-decisions evening with L1 having
    NEVER fired today must still open the L4 debrief — previously this was
    hard-blocked on `l1_fired` and fell through to a Layer-1 catch-up
    instead, stranding L4 for the rest of the day."""
    repo = _mk_repo(tmp_path)
    # Deliberately NOT writing outputs/<today>/1/ at all — L1 genuinely never
    # ran today (dept down/quiet all morning).

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)

    assert ctx.get("layer_1_last_run_today") is None, (
        "test premise: L1 has not fired today by any signal"
    )
    assert decide_dispatch(ctx) == "layer_4", (
        "REGRESSION #1407: the evening debrief must not be hard-gated on "
        "L1 having fired today — a genuinely-missed L1 must not strand L4 "
        "for the whole day"
    )


def test_l4_still_fires_normally_when_l1_fired(tmp_path: Path):
    """NO REGRESSION: the ordinary path — L1 fired this morning, no
    research/decisions pending — must still open L4 exactly as before this
    fix (l1_fired was already True, so dropping the requirement is a no-op
    here)."""
    repo = _mk_repo(tmp_path)
    write_last_run(repo / "outputs" / _TODAY / "1", when=_NOW)

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)

    assert ctx.get("layer_1_last_run_today") is not None
    assert decide_dispatch(ctx) == "layer_4"


def test_l4_still_waits_for_l3_when_decisions_pending_and_l1_absent(tmp_path: Path):
    """NO REGRESSION: dropping the l1_fired leg must not touch the l3_fired
    leg. With approved decisions pending and L3 not yet run, decide_dispatch
    must still route to layer_3 (never skip ahead to a premature L4),
    regardless of whether L1 fired today."""
    import yaml

    repo = _mk_repo(tmp_path)
    (repo / "inbox" / "decisions").mkdir(parents=True)
    (repo / "inbox" / "decisions" / "approved-1.yaml").write_text(
        yaml.dump({"id": "approved-1", "kind": "trade", "status": "approved"},
                  allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    # L1 never fired today (same as the strand scenario); L3 also never ran.

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)

    assert ctx["has_inbox_decisions"] is True
    assert decide_dispatch(ctx) == "layer_3", (
        "an approved decision with L3 not-yet-run must still outrank L4 "
        "(C.3), independent of L1's fired-today state"
    )
