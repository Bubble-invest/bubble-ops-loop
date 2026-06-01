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
    # with nothing queued and gate not satisfied -> heartbeat
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
