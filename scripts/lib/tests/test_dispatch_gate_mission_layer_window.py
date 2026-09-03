"""Regression test — accountant L1→L2 trigger silent-skip (Géraldine, Sep 2026).

THE BUG (live, 2026-08-12 → 2026-09-02): Géraldine's L1 read the books every
morning (Dougs/Qonto ledger + supplier invoices staged into queues/research/)
but the L2 categorisation mission never ran for ~3 weeks, so nothing was gated.

ROOT CAUSE: the accountant's L2 mission `daily_categorisation_reconciliation`
is SHIM-resolved (no dedicated `missions/<id>/PROMPT.md`), targets
`queues/gates/`, and carries cadence `time: '08:00'` while the Layer-2 minimum
fire time is 12:00 Paris. On the MORNING L1 tick (08:00–12:00 Paris) the
materializer sees the mission as due-by-cadence, materializes it, hits the
`queues/gates` bare-stub suppression branch (#302), and — for a non
dedicated-prompt mission — stamped its per-mission `.last-run` UNCONDITIONALLY.
That marked the mission "fired today" BEFORE Layer 2 was ever time-eligible, so
when Layer 2 finally opened at 12:00 Paris `is_mission_due()` vetoed it inside
`select_due_missions()` → nothing dispatched → read-but-uncategorised.

Note `decide_dispatch()` STILL returns "layer_2" the whole time (the raw rows
make `has_research_items` True), which is exactly why the failure was silent:
the phase looked right, but the mission-centric selector returned [].

THE FIX: the fire-spin stamp in the gates-suppression branch is now gated on
`_time_reached(now_paris, layer)` — it only fires once the mission's LAYER
window has opened. It still stamps during the eligible window (preserving the
#302 fire-spin guard) but never before it.

This test reproduces the exact morning→afternoon sequence and asserts the
afternoon tick dispatches the L2 categorisation mission (not an empty/heartbeat
result). It also asserts the morning tick does NOT pre-stamp the marker, and
that a later same-day tick does not re-fire (idempotence preserved).

UPSTREAMED (#1084/#1085, 2026-09-03): this fix originated as a local,
unvendored patch on Géraldine's live M5 copy of `dispatch_helpers.py` (found
during the #1084 re-vendor sweep, which correctly declined to clobber it).
This test file is ported byte-for-byte from that live copy into canonical so
a future re-vendor carries it fleet-wide instead of re-losing it.
"""
from __future__ import annotations

import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    build_dispatch_ctx,
    decide_dispatch,
    select_due_missions,
    read_last_run,
    read_last_materialized,
)

# Accountant-shaped dept: two L1 producers → queues/research/, one L2 gate
# mission (SHIM-resolved: no missions/<id>/PROMPT.md) whose cadence time (08:00)
# precedes the L2 layer floor (12:00 Paris). This is the exact live shape.
_MISSIONS = [
    {
        "id": "daily_ledger_cash_sync",
        "layer": 1,
        "cadence": "daily",
        "time": "07:00",
        "output_queue": "queues/research/",
        "creates": ["ledger_sync"],
    },
    {
        "id": "daily_invoice_receipt_intake",
        "layer": 1,
        "cadence": "daily",
        "time": "07:00",
        "output_queue": "queues/research/",
        "creates": ["invoice_intake"],
    },
    {
        "id": "daily_categorisation_reconciliation",
        "layer": 2,
        "cadence": "daily",
        "time": "08:00",          # EARLIER than the 12:00 Paris L2 floor
        "output_queue": "queues/gates/",
        "creates": ["categorisation_review"],
    },
]

# Paris is CEST (UTC+2) in September.
_MORNING = datetime(2026, 9, 5, 6, 30, tzinfo=timezone.utc)    # 08:30 Paris (< 12:00 L2 floor)
_AFTERNOON = datetime(2026, 9, 5, 12, 30, tzinfo=timezone.utc)  # 14:30 Paris (>= 12:00 L2 floor)
_LATER = datetime(2026, 9, 5, 13, 30, tzinfo=timezone.utc)      # 15:30 Paris (same day)

_L2_ID = "daily_categorisation_reconciliation"


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": _MISSIONS}, allow_unicode=True),
        encoding="utf-8",
    )
    # A raw ledger row staged by L1's reader skill — NO `kind` field, exactly
    # like the live dougs-pending-*/qonto-* rows. It keeps has_research_items True.
    (repo / "queues" / "research" / "dougs-pending-001.yaml").write_text(
        "source: dougs\namount: 47.70\nvendor: Hippopotamus\n", encoding="utf-8"
    )
    return repo


def _l2_marker(repo: Path, now: datetime):
    """#870: daily_categorisation_reconciliation is shim-resolved (no
    dedicated PROMPT.md), so its fire-spin guard stamp lands on
    .last-materialized, never .last-run (nothing has actually categorised
    anything — see _mission_handled_marker)."""
    today_dir = repo / "outputs" / now.strftime("%Y-%m-%d")
    assert read_last_run(today_dir / "missions" / _L2_ID) is None, (
        "materialize_due_missions_for_tick must never write .last-run for a "
        "mission it did not actually run (#870)"
    )
    return read_last_materialized(today_dir / "missions" / _L2_ID)


def test_afternoon_tick_dispatches_l2_categorisation(tmp_path: Path):
    """The core regression: after the morning L1 tick, the afternoon tick must
    DISPATCH the L2 categorisation mission — not silently resolve to nothing."""
    repo = _make_repo(tmp_path)

    # --- Morning L1 tick (live path: materialize=True) -------------------
    ctx_am = build_dispatch_ctx(repo, now_utc=_MORNING, materialize=True)
    # L2 is not the eligible phase yet (before its 12:00 floor).
    assert decide_dispatch(ctx_am) == "layer_1"
    # BUG GUARD: the L2 gate mission must NOT be pre-stamped in the morning.
    assert _l2_marker(repo, _MORNING) is None, (
        "L2 gate mission was stamped fired-today during the morning L1 tick, "
        "before its 12:00 layer floor — this is the silent-skip bug."
    )

    # --- Afternoon L2 tick (live path) -----------------------------------
    ctx_pm = build_dispatch_ctx(repo, now_utc=_AFTERNOON, materialize=True)
    assert decide_dispatch(ctx_pm) == "layer_2"
    due = [m["id"] for m in select_due_missions(ctx_pm, _MISSIONS)]
    assert _L2_ID in due, (
        "L2 categorisation mission was NOT dispatched in the afternoon — the "
        "premature morning stamp vetoed it (the 3-week read-but-uncategorised "
        f"bug). select_due_missions returned {due!r}"
    )
    # It IS stamped now (fire-spin guard active once the window is open).
    assert _l2_marker(repo, _AFTERNOON) is not None


def test_no_refire_same_day(tmp_path: Path):
    """Idempotence: once L2 has been dispatched (and stamped) at 14:30, a later
    same-day tick must not re-select it (the #302 fire-spin guard still holds)."""
    repo = _make_repo(tmp_path)
    build_dispatch_ctx(repo, now_utc=_MORNING, materialize=True)
    ctx_pm = build_dispatch_ctx(repo, now_utc=_AFTERNOON, materialize=True)
    assert _L2_ID in [m["id"] for m in select_due_missions(ctx_pm, _MISSIONS)]

    ctx_later = build_dispatch_ctx(repo, now_utc=_LATER, materialize=True)
    later = [m["id"] for m in select_due_missions(ctx_later, _MISSIONS)]
    assert _L2_ID not in later, (
        f"L2 re-fired the same day (fire-spin) — select returned {later!r}"
    )
