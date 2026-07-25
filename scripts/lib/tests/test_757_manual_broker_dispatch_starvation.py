"""Tests for #757 — decide_dispatch/select_due_missions starvation from an
approved MANUAL-broker decision that never gets archived out of
`inbox/decisions/`.

THE BUG (verified live-shaped, reproduced here): `decide_dispatch()` returns
`phase="layer_3"` on EVERY tick whenever `has_inbox_decisions` is True —
regardless of whether L3 actually has anything LEFT to do. A MANUAL-broker
reco (e.g. Boursorama — no execution API, awaits a human placing the trade by
hand) sits in `inbox/decisions/` all day (nothing auto-archives it), so
`has_inbox_decisions` stays True even after L3's daily `execution` mission has
already run once today (its own per-mission `.last-run` marker blocks
re-selection for the rest of the day). Because `_LAYER_PRIORITY` puts L3 above
L1/L2/L4, L3 SHADOWS the lower layers every tick — but
`select_due_missions()` returns `[]` on that phase (nothing new for L3 to do),
so time-due L2 research / L4 weekly_review never get selected. The loop
degrades to manual-fire-only until the human places the manual trade.

OPTION CHOSEN: (b) — fallthrough to the next-lower eligible layer when the
top layer's `due` list is empty. Verified during investigation: this repo's
decision-item data model (`inbox/decisions/*.yaml`, read by `_queue_has_items`
/ `_present_trigger_ids` in dispatch_helpers.py) carries NO broker / manual-
execution-mode field reachable at `has_inbox_decisions` time — decisions are
identified only by filename presence + an optional `kind` key. So option (a)
(exclude manual-broker decisions from the L3 `has_decisions` predicate) is not
implementable without a schema addition; the card explicitly directs falling
back to (b) in that case.

RED before the fix: `test_manual_broker_decision_no_longer_starves_L2` fails
(`due` came back `[]` while L2's research queue was genuinely time-due).
GREEN after: `select_due_missions` walks `_LAYER_PRIORITY` and returns the
first NON-EMPTY eligible layer's due list.

NO REGRESSION for auto-executable brokers: `test_auto_executable_decision_still_produces_layer_3`
asserts an approved alpaca/saxo/crypto-shaped decision, with L3's `execution`
mission still due today, DOES select layer_3 exactly as before — the
fallthrough only engages when the top layer's own due list is empty.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    build_dispatch_ctx,
    decide_dispatch,
    select_due_missions,
    write_last_run,
)

# Friday 2026-07-24, 13:00 Paris (11:00 UTC, CEST) — past L1 (07:00) and
# L2 (12:00) floors, before L3's legacy 16:00 gate (irrelevant here: C.3 is
# gated at L1's 07:00 floor, see decide_dispatch docstring) and L4 (19:00).
_NOW = datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")


def _ben_shaped_missions() -> list[dict]:
    """A dept.yaml recurring_missions list shaped like agents/ben/dept.yaml:
    data_update (L1) -> research (L2, consumer of queues/research/) ->
    execution (L3, daily producer, no input_queue — mirrors the real Ben dept:
    L3's mission gate is `has_inbox_decisions`, not an input-queue check) ->
    weekly_review (L4, weekly)."""
    return [
        {
            "id": "data_update",
            "layer": 1,
            "cadence": "daily",
            "time": "07:00",
            "output_queue": "queues/research/",
            "creates": ["research_item"],
        },
        {
            "id": "research",
            "layer": 2,
            "cadence": "daily",
            "time": "12:00",
            "output_queue": "queues/gates/",
            "input_queue": "queues/research/",
            "creates": ["investment_case"],
        },
        {
            "id": "execution",
            "layer": 3,
            "cadence": "daily",
            "time": "07:00",
            "output_queue": "queues/management/",
            "creates": ["executed_trade"],
        },
        {
            "id": "weekly_review",
            "layer": 4,
            "cadence": "weekly",
            "day": "friday",
            "time": "18:00",
            "output_queue": "queues/management/",
            "creates": ["weekly_kpi_review"],
        },
    ]


def _mk_repo(tmp_path: Path, missions: list[dict]) -> Path:
    (tmp_path / "queues" / "research").mkdir(parents=True)
    (tmp_path / "queues" / "gates").mkdir(parents=True)
    (tmp_path / "inbox" / "decisions").mkdir(parents=True)
    (tmp_path / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )
    return tmp_path


def _drop_decision(repo: Path, decision_id: str, *, broker: str, status: str = "approved") -> None:
    (repo / "inbox" / "decisions" / f"{decision_id}.yaml").write_text(
        yaml.dump({
            "id": decision_id,
            "broker": broker,
            "status": status,
        }, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def _drop_research_item(repo: Path, item_id: str) -> None:
    (repo / "queues" / "research" / f"{item_id}.yaml").write_text(
        yaml.dump({"id": item_id, "kind": "research_item"},
                  allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )


def test_manual_broker_decision_no_longer_starves_L2(tmp_path: Path):
    """THE FIX: an approved MANUAL-broker decision sitting in inbox/decisions/
    (never archived — awaits a human placing the trade) must NOT prevent a
    time-due L2 research item from being dispatched.

    Setup: L3's `execution` mission already fired once today (simulates the
    mission having already surfaced/processed the manual reco this morning —
    there's nothing NEW for it to do, it's just waiting on the human). The
    manual decision is still present (nothing auto-archives a manual-awaiting
    -human item), so has_inbox_decisions stays True -> L3 stays "eligible"
    every tick under the OLD code. Meanwhile L2's research queue has a fresh,
    time-due item.

    RED (pre-fix): select_due_missions picks eligible_layer=3 (has_decisions
    pins it), finds execution's per-mission marker already stamped today ->
    due=[] -> L2's research item never selected.
    GREEN (post-fix): L3's due list is empty -> fallthrough to L2 -> research
    mission returned.
    """
    missions = _ben_shaped_missions()
    repo = _mk_repo(tmp_path, missions)

    # Manual-broker (Boursorama) approved decision — awaits human placement,
    # sits in inbox/decisions/ indefinitely.
    _drop_decision(repo, "manual-boursorama-1", broker="boursorama")

    # L3's execution mission already ran once today (per-mission marker from
    # an earlier tick this morning) — nothing NEW for L3 to do this tick.
    write_last_run(
        repo / "outputs" / _TODAY / "missions" / "execution",
        datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc),
    )

    # A fresh, time-due L2 research item.
    _drop_research_item(repo, "ri-fresh-1")

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)
    ctx["_repo_dir"] = str(repo)

    # Preconditions.
    assert ctx["has_inbox_decisions"] is True, (
        "precondition: the manual decision must be visible to has_inbox_decisions"
    )
    assert ctx["has_research_items"] is True, (
        "precondition: L2's research queue must have a fresh item"
    )
    assert decide_dispatch(ctx) == "layer_3", (
        "precondition: decide_dispatch's phase string is UNCHANGED by this fix "
        "— has_inbox_decisions=True still pins the phase string to layer_3. "
        "The fix lives in select_due_missions, not decide_dispatch."
    )

    due = select_due_missions(ctx, missions)
    ids = [m["id"] for m in due]

    assert "execution" not in ids, (
        "execution must NOT be re-selected — it already fired today "
        "(per-mission idempotence marker), there's nothing new for it to do"
    )
    assert "research" in ids, (
        "REGRESSION #757: a time-due L2 research mission must be dispatched "
        "via fallthrough when L3 (pinned eligible by a manual-broker decision "
        "that never gets archived) has no due mission of its own. Before the "
        "fix, select_due_missions stopped at L3, found due=[], and returned "
        "[] — starving L2 (and L1/L4) for the rest of the day."
    )


def test_manual_broker_decision_falls_through_two_levels_to_l1_mgmt_note(tmp_path: Path):
    """Demonstrates the fallthrough walking MULTIPLE levels down — not just
    one — when both L3 and L2 have nothing due: L3 (manual decision, nothing
    new) -> L2 (no research items, genuinely ineligible) -> L1 (an
    unconsumed management note makes it eligible via C.mgmt, issue #176).

    This is the same starvation shape as the primary test, but proves the
    walk doesn't stop after trying just the second-highest layer."""
    missions = _ben_shaped_missions()
    # A second L1 mission that does NOT feed queues/research/ (so it can't
    # flip has_research_items True) — an L1 producer that is genuinely due
    # this tick, to prove the walk actually reaches L1's own due list (not
    # just L1's eligibility signal).
    missions = missions + [{
        "id": "morning_note_triage",
        "layer": 1,
        "cadence": "daily",
        "time": "07:00",
        "output_queue": "queues/management/",
        "creates": ["situation_note"],
    }]
    repo = _mk_repo(tmp_path, missions)

    _drop_decision(repo, "manual-boursorama-2", broker="boursorama")
    write_last_run(
        repo / "outputs" / _TODAY / "missions" / "execution",
        datetime(2026, 7, 24, 6, 0, tzinfo=timezone.utc),
    )
    # No research items -> L2 genuinely ineligible (has_research_items=False).
    # data_update (L1's only producer into queues/research/) must also be
    # marked already-fired today, else build_dispatch_ctx's materializer would
    # re-produce a fresh research_item this tick (it's a due daily mission)
    # and has_research_items would flip True — defeating this test's "L2 has
    # nothing due" precondition. morning_note_triage is deliberately left
    # UN-fired so it is the mission the fallthrough should surface at L1.
    write_last_run(
        repo / "outputs" / _TODAY / "missions" / "data_update",
        datetime(2026, 7, 24, 5, 30, tzinfo=timezone.utc),
    )
    # L1 already fired this morning (so C.0's "not yet run today" branch does
    # NOT also make L1 eligible — we want ONLY the mgmt-note signal, C.mgmt,
    # to make L1 eligible, isolating the fallthrough-to-L1-via-mgmt-notes path).
    write_last_run(repo / "outputs" / _TODAY / "1", datetime(2026, 7, 24, 5, 30, tzinfo=timezone.utc))

    # An unconsumed management note arrived after L1's morning scan — makes L1
    # eligible via C.mgmt (issue #176) regardless of the cycle-gate state.
    mgmt_dir = repo / "queues" / "management"
    mgmt_dir.mkdir(parents=True, exist_ok=True)
    (mgmt_dir / "note-1.yaml").write_text(
        yaml.dump({"id": "note-1", "created_at": _NOW.isoformat()},
                  allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)
    ctx["_repo_dir"] = str(repo)

    assert ctx["has_inbox_decisions"] is True
    assert ctx["has_research_items"] is False
    assert ctx["has_unconsumed_mgmt_notes"] is True, (
        "precondition: the fresh management note must be visible"
    )
    assert decide_dispatch(ctx) == "layer_3", (
        "precondition: the phase string is still pinned to layer_3 by the "
        "stale manual decision (has_decisions=True outranks C.mgmt) — proving "
        "the OLD single-phase model would starve the mgmt note indefinitely too"
    )

    due = select_due_missions(ctx, missions)
    ids = [m["id"] for m in due]
    assert "morning_note_triage" in ids, (
        "REGRESSION #757: fallthrough must walk past an empty-due L3 (manual "
        "decision, nothing new) AND an ineligible/empty L2 (no research items) "
        "down to L1, which is eligible via the unconsumed management note "
        "(C.mgmt, issue #176) — not just try ONE layer down and give up."
    )


def test_auto_executable_decision_still_produces_layer_3(tmp_path: Path):
    """NO REGRESSION: an approved auto-executable decision (alpaca/saxo/crypto
    -shaped, execution mission NOT yet run today) must still select layer_3
    exactly as before the fix — the fallthrough must never skip a layer that
    genuinely has due work."""
    missions = _ben_shaped_missions()
    repo = _mk_repo(tmp_path, missions)

    _drop_decision(repo, "auto-alpaca-1", broker="alpaca")
    # execution has NOT fired today — it is genuinely due.

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)
    ctx["_repo_dir"] = str(repo)

    assert ctx["has_inbox_decisions"] is True
    assert decide_dispatch(ctx) == "layer_3"

    due = select_due_missions(ctx, missions)
    ids = [m["id"] for m in due]
    assert ids == ["execution"], (
        "an approved auto-executable decision with a genuinely-due execution "
        "mission must still produce exactly ['execution'] on layer_3 — "
        "behaviour for auto-executable brokers must be byte-identical to "
        "before this fix"
    )


def test_no_fallthrough_when_top_layer_has_due_work_and_lower_layers_also_eligible(tmp_path: Path):
    """NO REGRESSION: when the top eligible layer DOES have due work, lower
    eligible layers must NOT also be dispatched this tick (select_due_missions
    returns exactly ONE layer's missions per tick, as documented)."""
    missions = _ben_shaped_missions()
    repo = _mk_repo(tmp_path, missions)

    _drop_decision(repo, "auto-saxo-1", broker="saxo")
    _drop_research_item(repo, "ri-also-due")  # L2 also has due work

    ctx = build_dispatch_ctx(repo, now_utc=_NOW)
    ctx["_repo_dir"] = str(repo)

    due = select_due_missions(ctx, missions)
    layers = {m["layer"] for m in due}
    assert layers == {3}, (
        f"expected only layer 3 missions when L3 has genuine due work, got "
        f"layers {layers} — the fallthrough must not fire when the top "
        f"layer already satisfied the tick"
    )
