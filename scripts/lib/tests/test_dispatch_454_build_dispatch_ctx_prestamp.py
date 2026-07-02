"""Tests for #454 — `build_dispatch_ctx` pre-stamps a mission's `.last-run`
marker as a SIDE EFFECT of a READ-ONLY gate check, silently vetoing the real
dispatch a few seconds later. Same failure family as #428/#375/#432: "a
context-builder/dispatch-decider must NEVER write a run-marker as a
side-effect."

LIVE evidence (board #454): Ben's `data_update` (a shim-resolved L1 mission,
no dedicated `missions/data_update/PROMPT.md`) gets its per-mission marker
`outputs/<today>/missions/data_update/.last-run` pre-stamped by
`materialize_due_missions_for_tick` — which `build_dispatch_ctx` calls
UNCONDITIONALLY at the top, before returning ctx. `build_dispatch_ctx` is
called not only by the real dispatch decision but ALSO by
`scripts/loop-backup.sh`'s FORCE_LAYER pre-check (a read-only "is this layer
eligible yet" gate, used to decide whether to wake the live session) — see
loop-backup.sh's Python snippet around `layer_ok = ...`. That backup-cron
gate check runs `build_dispatch_ctx(workdir, now_utc=...)` ~9s before waking
the live session (`inject_live_loop`). Its call materializes + stamps
`data_update`'s marker. When the live session starts 9s later and calls
`build_dispatch_ctx` again for the REAL dispatch decision, the marker it
just wrote is now a PRIOR-tick stamp (`stamped < now_utc`) — the same-tick
exclusion in `_mission_last_fired` / `_any_mission_fired_today_for_layer`
does NOT protect against a marker stamped by a DIFFERENT, earlier
`build_dispatch_ctx` call. `layer_1_mission_fired_today` becomes True,
`l1_fired` becomes True, and `decide_dispatch` falls through to
"heartbeat" — L1/data_update never dispatches, even though nothing has
actually run. Ben was hand-archiving the marker every tick to un-stick it.

Fix: `build_dispatch_ctx` gets a `materialize: bool = True` parameter. A
caller that only wants READ-ONLY ctx signals (e.g. loop-backup.sh's
eligibility gate, or any other pre-check) passes `materialize=False` so
`materialize_due_missions_for_tick` (the ONLY mutating step in
`build_dispatch_ctx`) is skipped — zero side effects, ctx still reflects
whatever real state exists on disk. The REAL dispatch decision call site
(the live `/loop` — scaffold.py's documented `ctx = build_dispatch_ctx('.')`
call) keeps the default `materialize=True` so due missions still get
queued/stamped exactly as before (#428/#432 behaviour unchanged).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    build_dispatch_ctx,
    decide_dispatch,
    select_due_missions,
    read_last_run,
)

# Friday 2026-07-03, 08:00 Paris (CEST, UTC+2) = 06:00 UTC — past L1's 07:00
# Paris floor, well before L2 (12:00)/L3 (16:00)/L4 (19:00).
_NOW1 = datetime(2026, 7, 3, 6, 0, 0, tzinfo=timezone.utc)
_TODAY = _NOW1.strftime("%Y-%m-%d")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    missions = [
        {
            "id": "data_update",
            "layer": 1,
            "cadence": "daily",
            "time": "07:00",
            "output_queue": "queues/research/",
            "creates": ["situation_brief", "research_item"],
        },
    ]
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )
    return repo


def _missions(repo: Path) -> list[dict]:
    return yaml.safe_load((repo / "dept.yaml").read_text())["recurring_missions"]


# ---------------------------------------------------------------------------
# 1. Repro: two build_dispatch_ctx calls ~9s apart (mirrors loop-backup.sh's
#    read-only gate check, immediately followed by the live session's real
#    dispatch call) — the SECOND call must still see data_update as due.
# ---------------------------------------------------------------------------

def test_gate_check_call_must_not_veto_the_real_dispatch_9s_later(tmp_path):
    repo = _make_repo(tmp_path)
    missions = _missions(repo)

    # TICK A — a read-only pre-check (loop-backup.sh's FORCE_LAYER gate),
    # called BEFORE the live session and NOT followed by any real dispatch
    # of data_update (the backup script only inspects _layer_fired_today
    # signals from this ctx — it never calls select_due_missions/spawns a
    # subagent from this call). Crucially, it must leave NO trace on disk.
    build_dispatch_ctx(repo, now_utc=_NOW1, materialize=False)
    assert read_last_run(repo / "outputs" / _TODAY / "missions" / "data_update") is None, (
        "sanity: a read-only gate-check call must not stamp any marker"
    )

    # TICK B — 9 seconds later, the live session wakes and calls
    # build_dispatch_ctx for the REAL dispatch decision.
    now2 = _NOW1 + timedelta(seconds=9)
    ctx_real = build_dispatch_ctx(repo, now_utc=now2)
    phase = decide_dispatch(ctx_real)
    due = select_due_missions(ctx_real, missions)

    assert phase == "layer_1", (
        f"expected layer_1 to be selected as the eligible phase, got {phase!r} "
        "— the read-only gate check must not have vetoed L1 eligibility"
    )
    assert [m["id"] for m in due] == ["data_update"], (
        "data_update must be dispatched on the real tick — the earlier "
        "read-only gate-check call pre-stamped its marker as a side effect "
        "and silently vetoed the real run (#454)"
    )


def test_readonly_ctx_build_writes_no_marker_at_all(tmp_path):
    """A build_dispatch_ctx(..., materialize=False) call must not create or
    modify ANY `.last-run` marker on disk — it is a pure read."""
    repo = _make_repo(tmp_path)
    missions_dir = repo / "outputs" / _TODAY / "missions"

    build_dispatch_ctx(repo, now_utc=_NOW1, materialize=False)

    assert not missions_dir.exists() or not any(missions_dir.rglob(".last-run")), (
        "materialize=False must not write any per-mission .last-run marker "
        "(build_dispatch_ctx is a context BUILDER, not a dispatch decider — "
        "it must never write a run-marker as a side effect, #428-class)"
    )
    assert read_last_run(repo / "outputs" / _TODAY / "missions" / "data_update") is None


# ---------------------------------------------------------------------------
# 2. Regression guard: the DEFAULT (materialize=True) behaviour — used by the
#    real /loop dispatch decision — must be UNCHANGED. data_update still gets
#    materialized + dispatched on the tick it becomes due (no regression of
#    #428/#432 fire-spin protections).
# ---------------------------------------------------------------------------

def test_default_materialize_true_still_dispatches_on_first_due_tick(tmp_path):
    repo = _make_repo(tmp_path)
    missions = _missions(repo)

    ctx = build_dispatch_ctx(repo, now_utc=_NOW1)  # materialize defaults True
    phase = decide_dispatch(ctx)
    due = select_due_missions(ctx, missions)

    assert phase == "layer_1"
    assert [m["id"] for m in due] == ["data_update"]
    # The per-mission marker IS stamped this tick (anti-fire-spin, #261/#277) —
    # materialize=True still writes it, same-tick-excluded so it doesn't
    # cannibalize the tick it became due on (unchanged from #428/#432).
    marker = read_last_run(repo / "outputs" / _TODAY / "missions" / "data_update")
    assert marker == _NOW1


def test_default_materialize_true_second_tick_same_process_still_vetoes_as_before(tmp_path):
    """Two materialize=True calls (the OLD/default behaviour) 9s apart still
    reproduce the veto — this is expected: the fix is that the GATE CHECK
    caller must opt into materialize=False, not that materialize=True stops
    being idempotent. This test pins the pre-existing (correct, by-design)
    idempotence semantics so the fix doesn't accidentally weaken them."""
    repo = _make_repo(tmp_path)
    missions = _missions(repo)

    build_dispatch_ctx(repo, now_utc=_NOW1)  # materialize=True: stamps for real
    now2 = _NOW1 + timedelta(seconds=9)
    ctx2 = build_dispatch_ctx(repo, now_utc=now2)
    due2 = select_due_missions(ctx2, missions)

    assert due2 == [], (
        "a genuine materialize=True dispatch tick correctly marks the "
        "mission as fired for subsequent ticks — this is NOT the bug; the "
        "bug is a read-only gate check using materialize=True implicitly"
    )
