"""Tests for build_dispatch_ctx — the queue-scanner that feeds decide_dispatch.

Root-cause fix 2026-06-01 ({{OPERATOR}} msg 3588): the /loop was calling
decide_dispatch with a placeholder ctx, so has_research_items / has_inbox_decisions
were never set → L2/L3 never fired → work piled up. build_dispatch_ctx scans the
repo's queues so the dispatch tree actually sees pending work.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import json

from scripts.lib.dispatch_helpers import (
    build_dispatch_ctx,
    decide_dispatch,
    increment_round_counter,
    write_l1_baseline,
    write_last_run,
    write_last_mgmt_scan,
    _scan_mgmt_notes,
    _load_consumed_ids,
    _drainable_kinds_for_queue,
)


def _mk_repo(tmp_path: Path) -> Path:
    (tmp_path / "queues" / "research").mkdir(parents=True)
    (tmp_path / "queues" / "inbox" / "decisions").mkdir(parents=True)
    # .gitkeep present (must be ignored)
    (tmp_path / "queues" / "research" / ".gitkeep").write_text("")
    (tmp_path / "queues" / "inbox" / "decisions" / ".gitkeep").write_text("")
    return tmp_path


NOON = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # 14:00 Paris (summer)
# Paris-local min-time anchors (June = CEST = UTC+2). decide_dispatch gates each
# layer on a Paris-local minimum: L1>=07:00, L2>=12:00, L3>=16:00, L4>=19:00.
AFTER_L2 = datetime(2026, 6, 1, 10, 30, tzinfo=timezone.utc)  # 12:30 Paris (>=L2)
AFTER_L3 = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)  # 16:30 Paris (>=L3)
AFTER_L4 = datetime(2026, 6, 1, 17, 30, tzinfo=timezone.utc)  # 19:30 Paris (>=L4)


def _fire(repo, layer, when):
    """Mark layer N as having fired today (writes its .last-run)."""
    today = when.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / str(layer), when)


def test_empty_queues_give_false_flags(tmp_path: Path):
    repo = _mk_repo(tmp_path)
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert ctx["has_research_items"] is False
    assert ctx["has_inbox_decisions"] is False
    assert ctx["layer_4_last_run_today"] is None
    assert ctx["round_counter"] == {}
    # Empty queues AND L1 not yet run today -> the daily floor fires L1.
    # (Canonical semantics, {{OPERATOR}} 2026-06-01: L1 runs at least once per day even
    # with empty queues — there are always emails + the Notion logbook to review.)
    assert ctx["layer_1_last_run_today"] is None
    assert decide_dispatch(ctx) == "layer_1"


def test_empty_queues_after_l1_ran_is_heartbeat(tmp_path: Path):
    # Same empty repo, but L1 HAS run today -> nothing to do -> heartbeat.
    repo = _mk_repo(tmp_path)
    today = NOON.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / "1", NOON)
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert ctx["layer_1_last_run_today"] is not None
    assert decide_dispatch(ctx) == "heartbeat"


def test_research_item_triggers_layer_2(tmp_path: Path):
    repo = _mk_repo(tmp_path)
    (repo / "queues" / "research" / "warming_task-x.yaml").write_text("kind: warming_task\n")
    _fire(repo, 1, NOON)  # morning floor already done so L2 wins over L1 idle path
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert ctx["has_research_items"] is True
    assert decide_dispatch(ctx) == "layer_2"


def test_inbox_decision_triggers_layer_3(tmp_path: Path):
    repo = _mk_repo(tmp_path)
    (repo / "queues" / "inbox" / "decisions" / "d-1.yaml").write_text("decision: send\n")
    # L3 has a 16:00 Paris min-time; L1 morning floor must already be satisfied
    # so it doesn't win the fall-through. Use AFTER_L3 and pre-fire L1.
    _fire(repo, 1, AFTER_L3)
    ctx = build_dispatch_ctx(repo, now_utc=AFTER_L3)
    assert ctx["has_inbox_decisions"] is True
    assert decide_dispatch(ctx) == "layer_3"


def test_dotfiles_and_processed_subdir_are_ignored(tmp_path: Path):
    repo = _mk_repo(tmp_path)
    # a .processed subdir full of consumed items must NOT count as pending
    proc = repo / "queues" / "research" / ".processed"
    proc.mkdir()
    (proc / "old.yaml").write_text("kind: warming_task\n")
    (repo / "queues" / "research" / ".hidden.yaml").write_text("x: 1\n")
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert ctx["has_research_items"] is False


def test_l4_last_run_and_round_counter_read(tmp_path: Path):
    repo = _mk_repo(tmp_path)
    today = NOON.strftime("%Y-%m-%d")
    (repo / "outputs" / today / "4").mkdir(parents=True)
    write_last_run(repo / "outputs" / today / "4", NOON)
    increment_round_counter(repo / "outputs" / today, layer=2)
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert ctx["layer_4_last_run_today"] is not None
    assert ctx["round_counter"].get("2") == 1


def test_missing_queues_dir_is_safe(tmp_path: Path):
    # repo with no queues/ at all -> all False, no crash
    ctx = build_dispatch_ctx(tmp_path, now_utc=NOON)
    assert ctx["has_research_items"] is False
    assert ctx["has_inbox_decisions"] is False


def test_default_repo_dir_is_cwd(tmp_path: Path, monkeypatch):
    repo = _mk_repo(tmp_path)
    (repo / "queues" / "research" / "item.yaml").write_text("kind: x\n")
    monkeypatch.chdir(repo)
    ctx = build_dispatch_ctx(now_utc=NOON)  # no repo_dir -> cwd
    assert ctx["has_research_items"] is True


def test_full_day_cycles_all_layers(tmp_path: Path):
    """The regression guard: a full day with work queued must cycle
    L2 -> L3 -> L1, and L4 in its window. Proves the loop is no longer blind.

    This is the exact failure Maya hit: only L1 ever fired. With
    build_dispatch_ctx wired in, each queue state routes to the right layer.
    """
    repo = _mk_repo(tmp_path)
    today = NOON.strftime("%Y-%m-%d")

    # 0) Morning (>=07:00 Paris), nothing fired yet -> L1 floor first.
    MORNING = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)  # 08:00 Paris
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=MORNING)) == "layer_1"
    _fire(repo, 1, MORNING)

    # 1) After 12:00 Paris with a research item -> L2 (L1 floor already done).
    r = repo / "queues" / "research" / "warming-1.yaml"
    r.write_text("kind: warming_task\n")
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=AFTER_L2)) == "layer_2"
    _fire(repo, 2, AFTER_L2)

    # 2) After 16:00 Paris, research consumed, an inbox decision waits -> L3.
    r.unlink()
    (repo / "queues" / "inbox" / "decisions" / "d-1.yaml").write_text("decision: send\n")
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=AFTER_L3)) == "layer_3"
    _fire(repo, 3, AFTER_L3)

    # 3) After 19:00 Paris with L1+L2+L3 all fired today and L4 not yet run ->
    #    L4 (the aggregator) is now eligible and takes priority.
    (repo / "queues" / "inbox" / "decisions" / "d-1.yaml").unlink()
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=AFTER_L4)) == "layer_4"
    _fire(repo, 4, AFTER_L4)

    # 4) Same evening, L4 already ran, queues empty, but L2/L3/L4 each did a
    #    fresh round since L1's morning fire -> L1 re-consolidation gate fires.
    (repo / "outputs" / today).mkdir(parents=True, exist_ok=True)
    for layer in (2, 3, 4):
        increment_round_counter(repo / "outputs" / today, layer=layer)
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=AFTER_L4)) == "layer_1"


# ── build_dispatch_ctx must inject the Layer-1 daily-floor signals ──────────
#
# REAL INCIDENT — Tony's VPS session ebb03972, 2026-06-02 morning. Tony OBSERVED
# this bug live in production and self-diagnosed it verbatim: build_dispatch_ctx
# injects neither `layer_1_last_run_today` nor `layer_1_baseline_counter`, so
# decide_dispatch always saw l1_last=None and returned "layer_1" on EVERY hourly
# tick even though L1 had already run that morning (08:19) and the queues were
# empty. Tony's round_counter that day was {"1": 1}. Tony self-mitigated (chose
# heartbeat by hand every tick), so no flood of re-runs — but it was a real bug
# plus manual-override toil on every tick. These tests are the executable proof
# the fix resolves that observed glitch: red against the old build_dispatch_ctx
# (which omits the two keys), green once it injects them.

def test_build_ctx_injects_layer_1_last_run_today(tmp_path: Path):
    """build_dispatch_ctx must read outputs/<today>/1/.last-run and expose it
    as layer_1_last_run_today — the key decide_dispatch's daily floor reads."""
    repo = _mk_repo(tmp_path)
    today = NOON.strftime("%Y-%m-%d")
    write_last_run(repo / "outputs" / today / "1", NOON)
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert "layer_1_last_run_today" in ctx
    assert ctx["layer_1_last_run_today"] is not None


def test_build_ctx_layer_1_last_run_none_when_not_run(tmp_path: Path):
    """No L1 .last-run today -> key present but None (the genuine daily-floor)."""
    repo = _mk_repo(tmp_path)
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert "layer_1_last_run_today" in ctx
    assert ctx["layer_1_last_run_today"] is None


def test_build_ctx_injects_layer_1_baseline_counter(tmp_path: Path):
    """build_dispatch_ctx must expose the L1 cycle baseline so the C.0 cycle
    gate measures other layers' progress from L1's last fire, not day start."""
    repo = _mk_repo(tmp_path)
    today = NOON.strftime("%Y-%m-%d")
    increment_round_counter(repo / "outputs" / today, layer=2)
    write_l1_baseline(repo / "outputs" / today)
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert "layer_1_baseline_counter" in ctx
    assert ctx["layer_1_baseline_counter"] == {"2": 1}


def test_incident_ebb03972_l1_already_ran_empty_queues_is_heartbeat(tmp_path: Path):
    """REGRESSION — Tony's live 2026-06-02 incident (VPS session ebb03972).

    Scenario reproduced exactly: Layer 1 has ALREADY run today (its .last-run is
    stamped, round_counter {"1": 1}), the research + decisions queues are empty,
    and we are OUTSIDE the 22:00-22:30 UTC Layer-4 window. The correct decision
    is "heartbeat" — there is nothing to do this tick.

    Against the BROKEN build_dispatch_ctx (which never injects
    layer_1_last_run_today) this returns "layer_1" on every tick, exactly the
    glitch Tony observed and had to override by hand each hour. After the fix it
    returns "heartbeat".
    """
    repo = _mk_repo(tmp_path)
    today = NOON.strftime("%Y-%m-%d")

    # L1 already ran this morning (Tony's was 08:19; NOON here is later, same day,
    # still outside the L4 window).
    write_last_run(repo / "outputs" / today / "1", NOON)
    # round_counter today = {"1": 1} — L1 ran once, nothing else has.
    increment_round_counter(repo / "outputs" / today, layer=1)
    # No baseline of other-layer progress since L1 fired (the cycle gate must NOT
    # trip: L2/L3/L4 have not advanced).

    ctx = build_dispatch_ctx(repo, now_utc=NOON)

    # The ctx the bug omitted must now be present and truthful.
    assert ctx["layer_1_last_run_today"] is not None, (
        "build_dispatch_ctx must inject layer_1_last_run_today — its absence IS "
        "the ebb03972 bug"
    )
    assert ctx["round_counter"].get("1") == 1

    # The decision the loop actually makes this tick.
    assert decide_dispatch(ctx) == "heartbeat", (
        "L1 already ran today + empty queues + outside L4 window must be a "
        "heartbeat, not another layer_1 (Tony incident ebb03972, 2026-06-02)"
    )


# ── Layer-3 path bug + morning-floor eligibility (Ben CRITICAL, 2026-06-11) ──
#
# REAL INCIDENT — Ben's L3 "never fires from the live loop". build_dispatch_ctx
# scanned `queues/inbox/decisions/`, but the cockpit approve-click writes to the
# dept TOP-LEVEL `inbox/decisions/` (console github_reader.write_decision), and
# that is the ONLY path that exists on disk for every dept. So has_inbox_decisions
# was permanently False -> C.3 never fired from /loop -> approved trades stranded
# until the once-daily backup floor cron forced an L3 tick (root of the recurring
# DTLA "missed its window", 2026-06-04..09). The fix reads the real top-level path
# (and keeps the old one as a fallback), and lets an APPROVED decision fire L3
# from L1's morning floor so the trade reaches the executor at its market's open
# instead of waiting for the fixed 16:00 gate.


def _mk_repo_real_inbox(tmp_path: Path) -> Path:
    """Like _mk_repo but ALSO creates the real top-level inbox/decisions/ —
    the path the cockpit actually writes to (console github_reader)."""
    repo = _mk_repo(tmp_path)
    (repo / "inbox" / "decisions").mkdir(parents=True)
    (repo / "inbox" / "decisions" / ".gitkeep").write_text("")
    return repo


def test_build_ctx_reads_top_level_inbox_decisions(tmp_path: Path):
    """A decision in the REAL `inbox/decisions/` must set has_inbox_decisions.

    Red against the old build_dispatch_ctx (which only scanned
    `queues/inbox/decisions/`), green after the fix. This is the exact byte that
    kept Ben's Layer 3 dark."""
    repo = _mk_repo_real_inbox(tmp_path)
    (repo / "inbox" / "decisions" / "gate-42.yaml").write_text("decision: approve\n")
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert ctx["has_inbox_decisions"] is True, (
        "decision in top-level inbox/decisions/ must be seen — its absence IS "
        "the Ben L3-never-fires bug (2026-06-11)"
    )


def test_top_level_decision_fires_layer_3_from_morning_floor(tmp_path: Path):
    """An approved decision must fire L3 from L1's morning floor (>=07:00 Paris),
    not wait for the old 16:00 gate — so the trade hits its market's open.

    MORNING is 08:00 Paris (after L1 floor, well before the old 16:00 L3 gate).
    L1 has already fired this morning so it does not win the fall-through."""
    repo = _mk_repo_real_inbox(tmp_path)
    MORNING = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)  # 08:00 Paris
    _fire(repo, 1, MORNING)  # morning floor satisfied
    (repo / "inbox" / "decisions" / "gate-7.yaml").write_text("decision: approve\n")
    ctx = build_dispatch_ctx(repo, now_utc=MORNING)
    assert ctx["has_inbox_decisions"] is True
    assert decide_dispatch(ctx) == "layer_3", (
        "an approved decision must reach L3 from the morning floor, not wait for "
        "16:00 Paris (Ben L3 timing fix, 2026-06-11)"
    )


def test_approved_decision_outranks_research_queue(tmp_path: Path):
    """C.3 (L3) still ranks ABOVE C.2 (L2): with BOTH a research item and an
    approved decision waiting after the morning floor, L3 wins."""
    repo = _mk_repo_real_inbox(tmp_path)
    MORNING = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)  # 08:00 Paris
    _fire(repo, 1, MORNING)
    (repo / "queues" / "research" / "warming-1.yaml").write_text("kind: warming_task\n")
    (repo / "inbox" / "decisions" / "gate-9.yaml").write_text("decision: approve\n")
    ctx = build_dispatch_ctx(repo, now_utc=MORNING)
    assert ctx["has_research_items"] is True
    assert ctx["has_inbox_decisions"] is True
    assert decide_dispatch(ctx) == "layer_3"


def test_incident_ebb03972_refires_only_after_a_full_cycle(tmp_path: Path):
    """Companion to the ebb03972 regression: once L1 has run, it re-fires only
    after L2/L3/L4 EACH complete a round since L1's baseline — not before."""
    repo = _mk_repo(tmp_path)
    today = NOON.strftime("%Y-%m-%d")

    write_last_run(repo / "outputs" / today / "1", NOON)
    increment_round_counter(repo / "outputs" / today, layer=1)
    write_l1_baseline(repo / "outputs" / today)  # baseline at L1's fire

    # Partial progress (only L2 advanced) -> still a heartbeat.
    increment_round_counter(repo / "outputs" / today, layer=2)
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=NOON)) == "heartbeat"

    # Full cycle (L3 and L4 also advance) -> L1 re-fires.
    increment_round_counter(repo / "outputs" / today, layer=3)
    increment_round_counter(repo / "outputs" / today, layer=4)
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=NOON)) == "layer_1"
# ── L4 L2-prerequisite relaxation (Proposal 2, 2026-06-16) ──
#
# PR #62 fixed the L3 half: L4 fires when (l3_fired or not has_decisions).
# Proposal 2 fixes the symmetric L2 half: on no-research days, L2 never
# fires so L4 was still blocked. The fix relaxes the L2 prerequisite
# identically: (l2_fired or not has_research).
#
# These tests are RED against the pre-fix C.1 gate
# (l1_fired and l2_fired and ...), GREEN after the fix.

def test_l4_fires_on_no_research_day_without_l2(tmp_path: Path):
    """L4 fires at 19:00 when no research items exist, even if L2 didn't fire.

    Pre-fix: L4 required l2_fired=True regardless of whether there was research
    work. On quiet days with no research items, L2 never fires → L4 blocked
    forever → no end-of-day debrief. The fix relaxes the gate symmetrically:
    (l2_fired or not has_research).
    """
    repo = _mk_repo(tmp_path)
    # AFTER_L4 = 19:30 Paris (>= L4 min-time 19:00)
    _fire(repo, 1, AFTER_L4)  # L1 fired today (morning floor)
    # L2 NOT fired (no .last-run, no round_counter)
    # No research items (queues/research/ empty)
    # No inbox decisions
    # L4 NOT yet fired
    ctx = build_dispatch_ctx(repo, now_utc=AFTER_L4)
    assert ctx["has_research_items"] is False
    assert ctx["has_inbox_decisions"] is False
    assert decide_dispatch(ctx) == "layer_4", (
        "L4 must fire on a no-research day with only L1 having run — "
        "without the L2 relaxation, this was a heartbeat (the aggregator "
        "never ran on quiet days)"
    )


def test_l4_blocked_when_research_pending_and_l2_not_fired(tmp_path: Path):
    """L4 is still blocked when research IS pending and L2 hasn't processed it.

    Symmetry check: the relaxation must not fire L4 when there is actual
    research work sitting in the queue. In that case, C.2 (L2) fires first
    to process the research, and L4 waits until L2 has completed its round.
    """
    repo = _mk_repo(tmp_path)
    # Place a research item in the queue (L2 has work to do)
    (repo / "queues" / "research" / "warming-1.yaml").write_text("kind: warming_task\n")
    _fire(repo, 1, AFTER_L4)  # L1 fired today
    # L2 NOT fired
    # No inbox decisions
    ctx = build_dispatch_ctx(repo, now_utc=AFTER_L4)
    assert ctx["has_research_items"] is True
    result = decide_dispatch(ctx)
    assert result != "layer_4", (
        "L4 must NOT fire when research is pending and L2 hasn't fired — "
        "L2 must process the research first"
    )
    assert result == "layer_2", (
        "with pending research at L4 min-time, C.2 should fire L2 to "
        "process the backlog before L4 can aggregate"
    )


# ── Materialize due missions into queue items ──

import yaml


def _mk_dept_yaml(repo: Path, missions: list[dict]) -> None:
    """Write a minimal dept.yaml with the given recurring_missions."""
    dept = {"recurring_missions": missions}
    (repo / "dept.yaml").write_text(
        yaml.dump(dept, allow_unicode=True, default_flow_style=False)
    )


def _mk_mission(mission_id: str, layer: int = 1, **kwargs) -> dict:
    """Build a minimal recurring mission dict for testing."""
    base = {
        "id": mission_id,
        "layer": layer,
        "cadence": kwargs.pop("cadence", "daily"),
        "time": kwargs.pop("time", "07:00"),
        "description": f"Test mission {mission_id}",
        "output_queue": kwargs.pop("output_queue", "queues/research/"),
        "creates": kwargs.pop("creates", ["test_task"]),
    }
    base.update(kwargs)
    return base


# 08:30 Paris = 06:30 UTC in June (CEST, UTC+2) — well after the 07:00 floor.
MORNING_AFTER_FLOOR = datetime(2026, 6, 16, 6, 30, tzinfo=timezone.utc)


def test_materialize_daily_mission_creates_queue_item(tmp_path: Path):
    """A daily mission whose time has passed today creates a queue item."""
    repo = _mk_repo(tmp_path)
    _mk_dept_yaml(repo, [
        _mk_mission("morning_sync", time="07:00"),
    ])

    # Yesterday's .last-run so the mission is due today.
    today_str = MORNING_AFTER_FLOOR.strftime("%Y-%m-%d")
    yesterday = MORNING_AFTER_FLOOR.replace(day=MORNING_AFTER_FLOOR.day - 1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    (repo / "outputs" / yesterday_str / "missions" / "morning_sync").mkdir(parents=True)
    write_last_run(
        repo / "outputs" / yesterday_str / "missions" / "morning_sync",
        yesterday,
    )

    # No existing queue items for this mission.
    ctx = build_dispatch_ctx(repo, now_utc=MORNING_AFTER_FLOOR)

    # A queue item should have been created
    research_dir = repo / "queues" / "research"
    items = sorted(
        p for p in research_dir.glob("*.yaml")
        if not p.name.startswith(".")
    )
    assert len(items) >= 1, (
        "materializer must create at least one queue item in queues/research/"
    )
    item_data = yaml.safe_load(items[0].read_text(encoding="utf-8"))
    assert item_data["mission_id"] == "morning_sync"
    assert item_data["kind"] == "test_task"
    assert item_data["created_by"] == "materialize_due_missions"


def test_materialize_idempotent(tmp_path: Path):
    """Same mission does not get double-queued on a second call."""
    repo = _mk_repo(tmp_path)
    _mk_dept_yaml(repo, [
        _mk_mission("morning_sync", time="07:00"),
    ])

    # Yesterday's .last-run → due today.
    yesterday = MORNING_AFTER_FLOOR.replace(day=MORNING_AFTER_FLOOR.day - 1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")
    (repo / "outputs" / yesterday_str / "missions" / "morning_sync").mkdir(parents=True)
    write_last_run(
        repo / "outputs" / yesterday_str / "missions" / "morning_sync",
        yesterday,
    )

    # Pre-seed the queue with an existing item for this mission.
    existing_item = {
        "id": "test_task-morning_sync-20260616-063000",
        "mission_id": "morning_sync",
        "kind": "test_task",
        "created_at": MORNING_AFTER_FLOOR.isoformat(),
        "created_by": "manual",
    }
    (repo / "queues" / "research" / "existing.yaml").write_text(
        yaml.dump(existing_item, allow_unicode=True, default_flow_style=False)
    )

    # First call: should NOT create a duplicate.
    ctx = build_dispatch_ctx(repo, now_utc=MORNING_AFTER_FLOOR)

    research_dir = repo / "queues" / "research"
    items = sorted(
        p for p in research_dir.glob("*.yaml")
        if not p.name.startswith(".")
    )
    assert len(items) == 1, (
        f"materializer must NOT create duplicate queue items; "
        f"expected 1, got {len(items)}: {[p.name for p in items]}"
    )


def test_materialize_respects_per_mission_last_run_today(tmp_path: Path):
    """If a mission already fired today (.last-run exists for today), skip it."""
    repo = _mk_repo(tmp_path)
    _mk_dept_yaml(repo, [
        _mk_mission("morning_sync", time="07:00"),
    ])

    # Today's .last-run exists — mission already fired.
    today_str = MORNING_AFTER_FLOOR.strftime("%Y-%m-%d")
    (repo / "outputs" / today_str / "missions" / "morning_sync").mkdir(parents=True)
    write_last_run(
        repo / "outputs" / today_str / "missions" / "morning_sync",
        MORNING_AFTER_FLOOR,
    )

    ctx = build_dispatch_ctx(repo, now_utc=MORNING_AFTER_FLOOR)

    research_dir = repo / "queues" / "research"
    items = sorted(
        p for p in research_dir.glob("*.yaml")
        if not p.name.startswith(".")
    )
    assert len(items) == 0, (
        f"mission already fired today must not re-materialize; "
        f"got {len(items)} items: {[p.name for p in items]}"
    )


# ── Issue #176 — management note heartbeat coverage ──────────────────────────
#
# PROBLEM (2026-06-15, {{OPERATOR}} catch): inbound management notes in
# queues/management/ are only read inside a real layer's STEP 0-ter.  A note
# arriving during a heartbeat-only stretch sits unread until the next L1/L2/L3
# natural fire — up to many hours on an overnight quiet stretch.
#
# FIX (issue #176, PART 2): the dispatcher reads queues/management/ on EVERY
# tick (including heartbeat ticks) via _scan_mgmt_notes() and exposes
# `has_unconsumed_mgmt_notes` in the ctx.  decide_dispatch's new C.mgmt branch
# routes to "layer_1" when unconsumed notes exist and the morning floor
# (>=07:00 Paris) has been reached — so a directive arriving in a quiet
# afternoon is consumed within one tick (up to ~1h), not deferred until the
# next natural L1 floor or full cycle gate.
#
# The marker file `queues/management/.last-mgmt-scan` is written by L1 after
# its STEP 0-ter; notes with created_at strictly after the marker are "new".
# Absent marker → any note is new (safe default for fresh depts).

# 08:30 Paris = 06:30 UTC in June (CEST, UTC+2) — after the 07:00 floor.
_MGMT_MORNING = datetime(2026, 6, 20, 6, 30, tzinfo=timezone.utc)
# 16:30 Paris = 14:30 UTC in June — past L2 floor (12:00) but before L4 (19:00).
# Used for quiet-stretch tests: no research items, no inbox decisions, no L4 window.
_MGMT_AFTERNOON = datetime(2026, 6, 20, 14, 30, tzinfo=timezone.utc)
# 04:00 Paris = 02:00 UTC in June — before the 07:00 morning floor.
_MGMT_PREDAWN = datetime(2026, 6, 20, 2, 0, tzinfo=timezone.utc)


def _write_mgmt_note(repo: Path, name: str, created_at: datetime) -> None:
    """Write a minimal inbound management note into queues/management/."""
    mgmt = repo / "queues" / "management"
    mgmt.mkdir(parents=True, exist_ok=True)
    (mgmt / name).write_text(
        f"id: {name.replace('.yaml', '')}\n"
        f"kind: management_note\n"
        f"created_at: '{created_at.isoformat()}'\n"
        f"created_by: tony\n"
        f"title: Test directive\n"
        f"detail: Some instructions.\n"
        f"status: open\n",
        encoding="utf-8",
    )


# ── _scan_mgmt_notes unit tests ──────────────────────────────────────────────

def test_scan_mgmt_notes_no_dir_returns_false(tmp_path: Path):
    """Missing queues/management/ → False (no notes)."""
    assert _scan_mgmt_notes(tmp_path, since=None) is False


def test_scan_mgmt_notes_no_marker_any_note_is_new(tmp_path: Path):
    """With no .last-mgmt-scan marker, any note is treated as new."""
    _write_mgmt_note(tmp_path, "tony-directive-20260620.yaml", _MGMT_MORNING)
    assert _scan_mgmt_notes(tmp_path, since=None) is True


def test_scan_mgmt_notes_note_after_marker_is_new(tmp_path: Path):
    """A note with created_at after the marker → new."""
    earlier = datetime(2026, 6, 20, 5, 0, tzinfo=timezone.utc)
    later = datetime(2026, 6, 20, 7, 0, tzinfo=timezone.utc)
    _write_mgmt_note(tmp_path, "tony-directive-20260620.yaml", later)
    # marker was written at `earlier` → note (created at `later`) is new
    assert _scan_mgmt_notes(tmp_path, since=earlier) is True


def test_scan_mgmt_notes_note_before_marker_is_seen(tmp_path: Path):
    """A note with created_at before (or equal to) the marker → already seen."""
    earlier = datetime(2026, 6, 20, 5, 0, tzinfo=timezone.utc)
    later = datetime(2026, 6, 20, 7, 0, tzinfo=timezone.utc)
    _write_mgmt_note(tmp_path, "tony-directive-20260620.yaml", earlier)
    # marker was written at `later` → note (created at `earlier`) is already seen
    assert _scan_mgmt_notes(tmp_path, since=later) is False


def test_scan_mgmt_notes_dotfiles_ignored(tmp_path: Path):
    """Dotfiles (.consumed.json, .gitkeep, .last-mgmt-scan) are ignored."""
    mgmt = tmp_path / "queues" / "management"
    mgmt.mkdir(parents=True)
    (mgmt / ".gitkeep").write_text("")
    (mgmt / ".consumed.json").write_text("{}")
    (mgmt / ".last-mgmt-scan").write_text(datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc).isoformat())
    assert _scan_mgmt_notes(tmp_path, since=None) is False


def test_scan_mgmt_notes_unparseable_created_at_is_fail_open(tmp_path: Path):
    """A note with an unparseable created_at is treated as unconsumed (fail-open)."""
    mgmt = tmp_path / "queues" / "management"
    mgmt.mkdir(parents=True)
    (mgmt / "bad-note.yaml").write_text("id: bad\ncreated_at: not-a-date\n")
    since = datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)
    assert _scan_mgmt_notes(tmp_path, since=since) is True


def test_scan_mgmt_notes_missing_created_at_is_fail_open(tmp_path: Path):
    """A note with no created_at field is treated as unconsumed (fail-open)."""
    mgmt = tmp_path / "queues" / "management"
    mgmt.mkdir(parents=True)
    (mgmt / "legacy-note.yaml").write_text("id: x\nkind: management_note\n")
    since = datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)
    assert _scan_mgmt_notes(tmp_path, since=since) is True


# ── build_dispatch_ctx integration tests ─────────────────────────────────────

def test_build_ctx_exposes_has_unconsumed_mgmt_notes_key(tmp_path: Path):
    """build_dispatch_ctx must always expose has_unconsumed_mgmt_notes."""
    repo = _mk_repo(tmp_path)
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert "has_unconsumed_mgmt_notes" in ctx


def test_build_ctx_no_mgmt_notes_is_false(tmp_path: Path):
    """Empty queues/management/ → has_unconsumed_mgmt_notes is False."""
    repo = _mk_repo(tmp_path)
    (repo / "queues" / "management").mkdir(parents=True)
    ctx = build_dispatch_ctx(repo, now_utc=_MGMT_MORNING)
    assert ctx["has_unconsumed_mgmt_notes"] is False


def test_build_ctx_new_mgmt_note_no_marker_is_true(tmp_path: Path):
    """A note with no .last-mgmt-scan marker → has_unconsumed_mgmt_notes True."""
    repo = _mk_repo(tmp_path)
    _write_mgmt_note(repo, "tony-test-20260620.yaml", _MGMT_MORNING)
    ctx = build_dispatch_ctx(repo, now_utc=_MGMT_AFTERNOON)
    assert ctx["has_unconsumed_mgmt_notes"] is True


def test_build_ctx_note_after_marker_is_true(tmp_path: Path):
    """A note with created_at after .last-mgmt-scan → has_unconsumed_mgmt_notes True."""
    repo = _mk_repo(tmp_path)
    scan_time = datetime(2026, 6, 20, 5, 0, tzinfo=timezone.utc)
    note_time = datetime(2026, 6, 20, 7, 0, tzinfo=timezone.utc)
    write_last_mgmt_scan(repo, scan_time)
    _write_mgmt_note(repo, "tony-late-note-20260620.yaml", note_time)
    ctx = build_dispatch_ctx(repo, now_utc=_MGMT_AFTERNOON)
    assert ctx["has_unconsumed_mgmt_notes"] is True


def test_build_ctx_note_before_marker_is_false(tmp_path: Path):
    """A note with created_at before .last-mgmt-scan → has_unconsumed_mgmt_notes False."""
    repo = _mk_repo(tmp_path)
    note_time = datetime(2026, 6, 20, 5, 0, tzinfo=timezone.utc)
    scan_time = datetime(2026, 6, 20, 7, 0, tzinfo=timezone.utc)
    write_last_mgmt_scan(repo, scan_time)
    _write_mgmt_note(repo, "tony-old-note-20260620.yaml", note_time)
    ctx = build_dispatch_ctx(repo, now_utc=_MGMT_AFTERNOON)
    assert ctx["has_unconsumed_mgmt_notes"] is False


# ── decide_dispatch C.mgmt branch tests ──────────────────────────────────────

def test_decide_dispatch_unconsumed_mgmt_note_fires_l1_in_afternoon(tmp_path: Path):
    """C.mgmt: unconsumed note + time>=07:00 Paris → layer_1, even if L1 already
    ran this morning (issue #176 — directive arriving in afternoon quiet stretch
    must not wait until tomorrow's floor).
    """
    repo = _mk_repo(tmp_path)
    # L1 already fired this morning (morning floor satisfied).
    _fire(repo, 1, _MGMT_MORNING)
    # A new management note arrived after L1 ran → no .last-mgmt-scan stamp yet.
    _write_mgmt_note(repo, "tony-afternoon-20260620.yaml", _MGMT_AFTERNOON)
    ctx = build_dispatch_ctx(repo, now_utc=_MGMT_AFTERNOON)
    assert ctx["has_unconsumed_mgmt_notes"] is True
    result = decide_dispatch(ctx)
    assert result == "layer_1", (
        "a new management note arriving in a quiet afternoon must route to "
        "layer_1 (C.mgmt branch) so it is consumed this tick, not deferred"
    )


def test_decide_dispatch_mgmt_note_not_fired_before_morning_floor(tmp_path: Path):
    """C.mgmt is gated at >=07:00 Paris: a note in a pre-dawn tick must not
    fire L1 (the morning floor hasn't opened yet).
    """
    repo = _mk_repo(tmp_path)
    _write_mgmt_note(repo, "tony-predawn-20260620.yaml", _MGMT_PREDAWN)
    ctx = build_dispatch_ctx(repo, now_utc=_MGMT_PREDAWN)
    assert ctx["has_unconsumed_mgmt_notes"] is True
    result = decide_dispatch(ctx)
    # Before 07:00 Paris no layer fires → heartbeat
    assert result == "heartbeat", (
        "C.mgmt must NOT fire before the 07:00 Paris morning floor"
    )


def test_decide_dispatch_mgmt_note_outranked_by_inbox_decisions(tmp_path: Path):
    """C.3 (inbox decisions) outranks C.mgmt: a real trade to execute takes
    priority over reading a management note.
    """
    repo = _mk_repo_real_inbox(tmp_path)
    _fire(repo, 1, _MGMT_MORNING)
    _write_mgmt_note(repo, "tony-note-20260620.yaml", _MGMT_MORNING)
    (repo / "inbox" / "decisions" / "gate-42.yaml").write_text("decision: approve\n")
    ctx = build_dispatch_ctx(repo, now_utc=_MGMT_AFTERNOON)
    assert ctx["has_unconsumed_mgmt_notes"] is True
    assert ctx["has_inbox_decisions"] is True
    result = decide_dispatch(ctx)
    assert result == "layer_3", (
        "inbox decisions (C.3) must outrank management notes (C.mgmt)"
    )


def test_decide_dispatch_mgmt_note_outranked_by_research_items(tmp_path: Path):
    """C.2 (research queue) outranks C.mgmt: pending research work takes
    priority over reading a management note.
    """
    repo = _mk_repo(tmp_path)
    _fire(repo, 1, _MGMT_MORNING)
    _write_mgmt_note(repo, "tony-note-20260620.yaml", _MGMT_MORNING)
    (repo / "queues" / "research" / "warming-1.yaml").write_text("kind: warming_task\n")
    ctx = build_dispatch_ctx(repo, now_utc=_MGMT_AFTERNOON)
    assert ctx["has_unconsumed_mgmt_notes"] is True
    assert ctx["has_research_items"] is True
    result = decide_dispatch(ctx)
    assert result == "layer_2", (
        "research items (C.2) must outrank management notes (C.mgmt)"
    )


def test_decide_dispatch_mgmt_note_seen_after_l1_scan_is_heartbeat(tmp_path: Path):
    """After L1 writes .last-mgmt-scan and there are no newer notes,
    has_unconsumed_mgmt_notes is False → back to heartbeat on the next tick.
    """
    repo = _mk_repo(tmp_path)
    _fire(repo, 1, _MGMT_MORNING)
    note_time = datetime(2026, 6, 20, 7, 0, tzinfo=timezone.utc)
    _write_mgmt_note(repo, "tony-note-20260620.yaml", note_time)
    # L1 ran and stamped .last-mgmt-scan after the note
    scan_time = datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc)
    write_last_mgmt_scan(repo, scan_time)
    ctx = build_dispatch_ctx(repo, now_utc=_MGMT_AFTERNOON)
    assert ctx["has_unconsumed_mgmt_notes"] is False
    result = decide_dispatch(ctx)
    assert result == "heartbeat", (
        "once .last-mgmt-scan is stamped after the note, the dispatcher "
        "must stop routing to L1 and fall through to heartbeat"
    )


# ── Issue #204 — research_item starvation fix ────────────────────────────────
#
# ROOT CAUSE: `has_research_items` was computed using
# `_drainable_kinds_for_layer(repo, 2)`, which returns L2's OWN creates[]
# (e.g. `{investment_case, proposal}` for Ben). Items in `queues/research/`
# are PRODUCED by L1 (kind: `research_item`) and CONSUMED by L2. Because
# `research_item ∉ L2-creates-set`, `_queue_has_items` returned False →
# `has_research_items=False` → L2 never fired. Ben had 8 stuck items (all
# kind: research_item) as live proof (2026-06-20).
#
# FIX: use `_drainable_kinds_for_queue(repo, "queues/research/")` which
# returns the union of creates[] of ALL missions whose output_queue is
# "queues/research/" — the producer-side view. This correctly includes
# `research_item` (from L1's data_update mission) without knowing which layer
# consumes the queue.
#
# ANTI-FIRE-SPIN guarantee (issue #61797): a kind present in NO mission's
# creates[] is still an orphan (not produced by any mission) → excluded →
# cannot pin the dispatcher in a fire-spin loop.

# Reuse the AFTER_L2 timestamp (12:30 Paris — past L2 min-time 12:00).
_I204_AFTER_L2 = datetime(2026, 6, 20, 10, 30, tzinfo=timezone.utc)  # 12:30 Paris


def _mk_ben_dept_yaml(repo: Path) -> None:
    """Write a minimal dept.yaml that mimics Ben's real mission structure:
    - data_update (L1, output_queue=queues/research/, creates=[situation_brief, research_item])
    - research    (L2, output_queue=queues/gates/,    creates=[investment_case, proposal])
    This is the schema that triggered issue #204.
    """
    dept = {
        "recurring_missions": [
            {
                "id": "data_update",
                "layer": 1,
                "cadence": "daily",
                "time": "07:30",
                "output_queue": "queues/research/",
                "creates": ["situation_brief", "research_item"],
            },
            {
                "id": "research",
                "layer": 2,
                "cadence": "daily",
                "time": "18:00",
                "output_queue": "queues/gates/",
                "creates": ["investment_case", "proposal"],
            },
        ]
    }
    (repo / "dept.yaml").write_text(
        yaml.dump(dept, allow_unicode=True, default_flow_style=False)
    )


def test_issue_204_research_item_in_queue_triggers_layer_2(tmp_path: Path):
    """Starvation-fix: research_item in queues/research/ must set
    has_research_items=True and dispatch to layer_2.

    This is the EXACT failure mode from issue #204: Ben had 8 stuck
    research_item files in queues/research/, all with kind: research_item
    (produced by L1's data_update mission). The old code checked L2's own
    creates[] ({investment_case, proposal}), found no match, returned False
    → L2 starved. The fix uses the producer-side view (creates[] of missions
    that write to queues/research/) which correctly includes research_item.

    Red against the pre-fix build_dispatch_ctx, green after.
    """
    repo = _mk_repo(tmp_path)
    _mk_ben_dept_yaml(repo)

    # Write a research_item identical to the stuck items on VPS
    (repo / "queues" / "research" / "research_item-data_update-20260620-054639.yaml").write_text(
        yaml.dump({
            "id": "research_item-data_update-20260620-054639",
            "kind": "research_item",
            "mission_id": "data_update",
            "created_at": "2026-06-20T05:46:39Z",
            "created_by": "materialize_due_missions",
        }, allow_unicode=True, default_flow_style=False)
    )

    # L1 already ran so the morning floor does not steal the dispatch.
    _fire(repo, 1, _I204_AFTER_L2)
    ctx = build_dispatch_ctx(repo, now_utc=_I204_AFTER_L2)

    assert ctx["has_research_items"] is True, (
        "research_item in queues/research/ must be seen as drainable — "
        "its absence IS the issue #204 starvation bug"
    )
    assert decide_dispatch(ctx) == "layer_2", (
        "a research_item in queues/research/ after L2's min-time must "
        "dispatch to layer_2, not heartbeat (issue #204)"
    )


def test_issue_204_orphan_kind_still_excluded(tmp_path: Path):
    """Anti-fire-spin regression (issue #61797): a kind that no mission
    produces must NOT count as drainable, so it cannot pin the dispatcher.

    We drop a YAML with kind='orphan_mystery_kind' into queues/research/.
    That kind appears in NO mission's creates[], so it is an orphan.
    has_research_items must remain False — the orphan kind is excluded.

    This proves the fix cannot reintroduce the fire-spin that drove the
    2026-06-16 deaf-restart storm.
    """
    repo = _mk_repo(tmp_path)
    _mk_ben_dept_yaml(repo)

    # Pre-stamp all missions' .last-run for today so materialize_due_missions_for_tick
    # skips them — we want to test only the orphan quarantine, not materialisation.
    today_str = _I204_AFTER_L2.strftime("%Y-%m-%d")
    for mission_id in ("data_update", "research"):
        write_last_run(
            repo / "outputs" / today_str / "missions" / mission_id,
            _I204_AFTER_L2,
        )

    # Drop an orphan kind that no mission creates
    (repo / "queues" / "research" / "orphan-20260620.yaml").write_text(
        yaml.dump({
            "id": "orphan-20260620",
            "kind": "orphan_mystery_kind",  # not in any mission's creates[]
            "created_at": "2026-06-20T05:00:00Z",
        }, allow_unicode=True, default_flow_style=False)
    )

    # L1 already ran so the morning floor does not steal the dispatch.
    _fire(repo, 1, _I204_AFTER_L2)
    ctx = build_dispatch_ctx(repo, now_utc=_I204_AFTER_L2)

    assert ctx["has_research_items"] is False, (
        "a kind not produced by any mission must NOT count as drainable — "
        "orphan kinds must still be excluded to prevent fire-spin (#61797)"
    )
    # With no real drainable items, L1 daily floor already satisfied, and
    # no inbox decisions, the result must be heartbeat (not layer_2).
    assert decide_dispatch(ctx) == "heartbeat", (
        "orphan kind in queues/research/ must NOT cause layer_2 dispatch — "
        "the fire-spin guard (#61797) must remain intact after the #204 fix"
    )


# ── Issue #198 — Fix 1: consumed check inside _scan_mgmt_notes ───────────────
#
# BUG: a management note with an unparseable or missing created_at hits the
# fail-open path in _scan_mgmt_notes and returns True every tick until the
# note is physically removed — even after L1 already acted on it. This causes
# wasteful L1 re-triggers on every daytime tick.
#
# FIX: load .consumed.json INSIDE _scan_mgmt_notes BEFORE attempting to parse
# created_at. If a note's `id` appears in the consumed set, skip it
# unconditionally — a bad/missing timestamp on an already-consumed note must
# not cause re-triggers. The fail-open logic is preserved for notes that have
# NOT been consumed yet (better to fire L1 once than to silently miss a live note).
#
# EVALUATION (from issue #198): "A malformed note fires L1 at most once (not
# every tick)."

def test_consumed_note_bad_created_at_no_retrigger(tmp_path: Path):
    """Fix #198 — consumed note with bad/missing created_at must NOT re-trigger L1.

    Scenario (the pre-fix bug):
      1. A management note lands with an unparseable created_at (or none at all).
      2. L1 fires, consumes the note, and records its id in .consumed.json.
      3. On the NEXT tick, _scan_mgmt_notes still returns True (fail-open on the
         bad timestamp) → C.mgmt fires L1 AGAIN → and again → and again, every
         tick until the file is removed.

    After the fix: the consumed check runs BEFORE the timestamp parse, so an
    already-consumed note is skipped — even if created_at is garbage. L1 fires
    at most once for this note (the first tick, before it was consumed).
    """
    mgmt = tmp_path / "queues" / "management"
    mgmt.mkdir(parents=True)

    # A note with a completely unparseable created_at field.
    (mgmt / "bad-ts-note.yaml").write_text(
        "id: bad-ts-note-001\n"
        "kind: management_note\n"
        "created_at: definitely-not-a-date\n"  # unparseable → old code: fail-open re-trigger
        "created_by: tony\n"
        "title: Test directive with bad timestamp\n",
        encoding="utf-8",
    )

    since = datetime(2026, 6, 20, 5, 0, tzinfo=timezone.utc)

    # BEFORE consuming: fail-open → returns True (note must fire L1 at least once).
    assert _scan_mgmt_notes(tmp_path, since=since) is True, (
        "a note with unparseable created_at must be treated as unconsumed "
        "(fail-open) before it appears in .consumed.json"
    )

    # L1 fires, acts on the note, and records it in .consumed.json.
    consumed = {"bad-ts-note-001": {"consumed_at": "2026-06-20T06:00:00+00:00"}}
    (mgmt / ".consumed.json").write_text(json.dumps(consumed), encoding="utf-8")

    # AFTER consuming: even though created_at is garbage, the consumed check
    # runs first → the note is skipped → returns False (no re-trigger).
    assert _scan_mgmt_notes(tmp_path, since=since) is False, (
        "a note already in .consumed.json must NOT re-trigger L1, even if "
        "its created_at is unparseable — fix #198: consumed check before "
        "created_at parse"
    )


def test_consumed_note_missing_created_at_no_retrigger(tmp_path: Path):
    """Fix #198 variant: consumed note with a completely absent created_at
    field must also not re-trigger once consumed.

    This is the 'missing created_at' counterpart to the 'bad created_at' case.
    Both paths (missing and unparseable) hit the fail-open branch; both must
    be short-circuited by the consumed check.
    """
    mgmt = tmp_path / "queues" / "management"
    mgmt.mkdir(parents=True)

    # Note with NO created_at field at all.
    (mgmt / "no-ts-note.yaml").write_text(
        "id: no-ts-note-002\n"
        "kind: management_note\n"
        "created_by: tony\n"
        "title: Directive without timestamp\n",
        encoding="utf-8",
    )

    since = datetime(2026, 6, 20, 5, 0, tzinfo=timezone.utc)

    # Pre-consume: fail-open fires.
    assert _scan_mgmt_notes(tmp_path, since=since) is True

    # Post-consume: consumed check silences the re-trigger.
    consumed = {"no-ts-note-002": {"consumed_at": "2026-06-20T06:30:00+00:00"}}
    (mgmt / ".consumed.json").write_text(json.dumps(consumed), encoding="utf-8")

    assert _scan_mgmt_notes(tmp_path, since=since) is False, (
        "a note already consumed (in .consumed.json) with no created_at must "
        "not re-trigger L1 — fix #198"
    )


def test_unconsumed_note_bad_created_at_still_triggers(tmp_path: Path):
    """Fix #198 preservation: fail-open is still active for NON-consumed notes.

    A note with a bad created_at that has NOT been consumed must still return
    True (fail-open). The fix only short-circuits notes that appear in
    .consumed.json; it must not suppress unconsumed malformed notes.
    """
    mgmt = tmp_path / "queues" / "management"
    mgmt.mkdir(parents=True)

    (mgmt / "bad-ts-live.yaml").write_text(
        "id: bad-ts-live-003\n"
        "kind: management_note\n"
        "created_at: not-a-date\n"
        "created_by: tony\n"
        "title: Live directive with bad timestamp\n",
        encoding="utf-8",
    )

    since = datetime(2026, 6, 20, 5, 0, tzinfo=timezone.utc)

    # .consumed.json exists but does NOT include this note's id.
    other_consumed = {"some-other-note-999": {"consumed_at": "2026-06-20T05:00:00+00:00"}}
    (mgmt / ".consumed.json").write_text(json.dumps(other_consumed), encoding="utf-8")

    # Must still return True — fail-open is preserved for unconsumed notes.
    assert _scan_mgmt_notes(tmp_path, since=since) is True, (
        "a note with bad created_at that is NOT in .consumed.json must still "
        "trigger L1 (fail-open preserved for unconsumed notes)"
    )


# ── Issue #198 — Fix 2: C.1 (L4) outranks C.mgmt ────────────────────────────
#
# The priority tree in decide_dispatch is: C.1 (L4) > C.3 (L3) > C.2 (L2) >
# C.mgmt (L1 for mgmt notes) > C.0 (L1 floor). This was verified manually in
# the #92 review but no test pinned it. A management note must NOT block L4 from
# firing when L4's prerequisites are met — the end-of-day debrief outranks a
# management note that arrived during the day.
#
# EVALUATION (from issue #198): "a test pins C.1 > C.mgmt."

def test_c1_l4_outranks_c_mgmt_management_note(tmp_path: Path):
    """C.1 (Layer 4) outranks C.mgmt: when L4 conditions are met AND an
    unconsumed management note exists, decide_dispatch must return 'layer_4',
    not 'layer_1'.

    Fleet-wide blast radius: if C.mgmt could block C.1, any management note
    arriving after 19:00 Paris would defer the end-of-day debrief indefinitely
    (until the note is consumed), potentially skipping the risk aggregation that
    guards overnight positions. This test pins the priority order.
    """
    repo = _mk_repo_real_inbox(tmp_path)

    # L4 window: 19:30 Paris = 17:30 UTC in June (CEST, UTC+2).
    AFTER_L4 = datetime(2026, 6, 20, 17, 30, tzinfo=timezone.utc)

    # L1 already ran today (L4 prerequisite).
    _fire(repo, 1, AFTER_L4.replace(hour=6))  # morning
    # L2 and L3 fired today (L4 prerequisites — no research items, no decisions).
    _fire(repo, 2, AFTER_L4.replace(hour=10))
    _fire(repo, 3, AFTER_L4.replace(hour=14))
    # L4 has NOT fired yet.

    # An unconsumed management note exists (C.mgmt condition met).
    _write_mgmt_note(repo, "tony-note-at-l4-window.yaml", AFTER_L4.replace(hour=16))

    ctx = build_dispatch_ctx(repo, now_utc=AFTER_L4)

    # Both conditions must be visible in the ctx.
    assert ctx["has_unconsumed_mgmt_notes"] is True, (
        "management note must be visible to confirm C.mgmt condition is met"
    )
    assert ctx["layer_4_last_run_today"] is None, (
        "L4 must not have fired yet — C.1 condition requires l4_fired=False"
    )

    result = decide_dispatch(ctx)
    assert result == "layer_4", (
        "C.1 (Layer 4 end-of-day debrief) must outrank C.mgmt (management note) "
        "when L4 prerequisites are satisfied — a management note must NOT block "
        "the risk aggregator (issue #198, fleet-wide blast radius)"
    )


# ── Issue #214 — is_mission_due list-day integration test ────────────────────
#
# Fleet-wide crash: a dept.yaml with a weekly mission whose `day` is a LIST
# (e.g. day: [tuesday, friday]) caused build_dispatch_ctx() → materialize_due_
# missions_for_tick() → is_mission_due() to crash with AttributeError: 'list'
# object has no attribute 'lower' on EVERY tick.
#
# This integration test runs build_dispatch_ctx against a real dept.yaml fixture
# containing such a mission and asserts it does NOT crash and returns a valid ctx.

def test_build_dispatch_ctx_does_not_crash_on_list_day_mission(tmp_path: Path):
    """INTEGRATION — fix #214: build_dispatch_ctx must not crash when dept.yaml
    contains a weekly mission with day as a list.

    This reproduces the fleet-wide crash verbatim: content's newsletter_redaction
    mission had day:['tuesday','friday'], which caused AttributeError on every
    tick of build_dispatch_ctx → materialize_due_missions_for_tick →
    is_mission_due (the weekly branch called .lower() on the list).

    Asserts:
      - No AttributeError or any other exception is raised.
      - The returned ctx is a dict (valid ctx shape).
      - The function completes and returns all mandatory keys.
    """
    repo = _mk_repo(tmp_path)

    # Fixture dept.yaml with the exact list-day pattern from issue #214.
    list_day_dept = {
        "recurring_missions": [
            {
                "id": "newsletter_redaction",
                "layer": 1,
                "cadence": "weekly",
                "time": "07:00",
                "day": ["tuesday", "friday"],  # <-- the crashing shape
                "description": "Bi-weekly newsletter redaction.",
                "output_queue": "queues/research/",
                "creates": ["newsletter_draft"],
            },
        ]
    }
    (tmp_path / "dept.yaml").write_text(
        yaml.dump(list_day_dept, allow_unicode=True, default_flow_style=False)
    )

    # Must NOT raise — this is the crash guard.
    ctx = build_dispatch_ctx(tmp_path, now_utc=NOON)

    # Returned value is a valid ctx dict with the mandatory keys.
    assert isinstance(ctx, dict), "build_dispatch_ctx must return a dict"
    assert "now_utc" in ctx
    assert "today" in ctx
    assert "has_research_items" in ctx
    assert "has_inbox_decisions" in ctx
    # No crash = fix confirmed. The specific dispatch decision is secondary.
    assert "layer_1_last_run_today" in ctx
