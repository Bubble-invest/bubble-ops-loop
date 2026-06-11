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

from scripts.lib.dispatch_helpers import (
    build_dispatch_ctx,
    decide_dispatch,
    increment_round_counter,
    write_l1_baseline,
    write_last_run,
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
