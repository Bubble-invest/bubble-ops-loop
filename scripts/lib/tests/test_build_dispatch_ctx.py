"""Tests for build_dispatch_ctx — the queue-scanner that feeds decide_dispatch.

Root-cause fix 2026-06-01 (Joris msg 3588): the /loop was calling
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


NOON = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # outside L4 window


def test_empty_queues_give_false_flags(tmp_path: Path):
    repo = _mk_repo(tmp_path)
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert ctx["has_research_items"] is False
    assert ctx["has_inbox_decisions"] is False
    assert ctx["layer_4_last_run_today"] is None
    assert ctx["round_counter"] == {}
    # Empty queues AND L1 not yet run today -> the daily floor fires L1.
    # (Canonical semantics, Joris 2026-06-01: L1 runs at least once per day even
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
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert ctx["has_research_items"] is True
    assert decide_dispatch(ctx) == "layer_2"


def test_inbox_decision_triggers_layer_3(tmp_path: Path):
    repo = _mk_repo(tmp_path)
    (repo / "queues" / "inbox" / "decisions" / "d-1.yaml").write_text("decision: send\n")
    ctx = build_dispatch_ctx(repo, now_utc=NOON)
    assert ctx["has_inbox_decisions"] is True
    # no research items -> L2 not chosen, L3 is
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

    # 1) research item present -> L2
    r = repo / "queues" / "research" / "warming-1.yaml"
    r.write_text("kind: warming_task\n")
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=NOON)) == "layer_2"

    # 2) L2 consumed the research item (moved to .processed), now an inbox
    #    decision exists -> L3
    r.unlink()
    (repo / "queues" / "inbox" / "decisions" / "d-1.yaml").write_text("decision: send\n")
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=NOON)) == "layer_3"

    # 3) L3 consumed the decision; queues empty but L2/L3/L4 each did a round
    #    today -> L1 idle gate satisfied -> L1
    (repo / "queues" / "inbox" / "decisions" / "d-1.yaml").unlink()
    (repo / "outputs" / today).mkdir(parents=True, exist_ok=True)
    for layer in (2, 3, 4):
        increment_round_counter(repo / "outputs" / today, layer=layer)
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=NOON)) == "layer_1"

    # 4) inside the L4 window with L4 not yet run today -> L4 takes priority
    in_window = datetime(2026, 6, 1, 22, 10, tzinfo=timezone.utc)
    # fresh day dir for the window check (no L4 .last-run yet)
    assert decide_dispatch(build_dispatch_ctx(repo, now_utc=in_window)) == "layer_4"


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
