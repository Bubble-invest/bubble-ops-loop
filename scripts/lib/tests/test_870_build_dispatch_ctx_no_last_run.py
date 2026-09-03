"""Regression test for board #870 — build_dispatch_ctx re-stamps a shim-
resolved mission's `.last-run` as a SIDE EFFECT of merely building/deciding
dispatch context, silently marking an unrun mission as "fired".

LIVE evidence (2026-08-01, Maya): `build_dispatch_ctx()` alone — no subagent
dispatched, no output directory created — wrote
`outputs/<today>/missions/draft_batch/.last-run`. `draft_batch` is an L2
shim-resolved mission (no dedicated `missions/draft_batch/PROMPT.md`) whose
`output_queue` is `queues/gates` — the #302 bare-stub-suppression branch in
`materialize_due_missions_for_tick` fires, and (pre-#870) stamped the
mission's per-mission `.last-run` unconditionally as its anti-fire-spin
guard. That phantom stamp then made `layer_2_mission_fired_today` True and
excluded `draft_batch` from `select_due_missions`' return for the rest of
the day, even though `draft_batch` never actually ran.

THE FIX (#870): `build_dispatch_ctx` / `materialize_due_missions_for_tick`
now NEVER write a file literally named `.last-run` — that filename is
reserved exclusively for a mission's own real executor (a dedicated
`missions/<id>/PROMPT.md`'s STEP 0, unaffected by this change). The
materializer's own "this mission was due and I looked at it this tick" proxy
stamp — still required so a shim-resolved mission with no other completion
signal does not fire-spin (#261/#277/#428/#432/#442/#454, all still green) —
now lands on a separately-named marker, `.last-materialized`
(`read_last_materialized`/`write_last_materialized`), never on `.last-run`.
Every read site that used to treat a per-mission `.last-run` as "handled
today" now unions BOTH markers via `_mission_handled_marker`, so every
existing dispatch/idempotence decision is UNCHANGED — only the on-disk
artifact identity is now honest, and matches the acceptance test suggested
on the board thread: "call build_dispatch_ctx() twice in a row [...] and
assert that neither outputs/<today>/missions/*/.last-run [...] was created."

SCOPE NOTE (see the PR description / board #870 for the full context): this
does NOT change LIVE dispatch behaviour for shim-resolved, no-real-executor
missions like `draft_batch`/`qualify` — `layer_2_mission_fired_today` (and
`select_due_missions`'s own per-mission gate) still treat a materialization
attempt as "handled" via the `.last-materialized` fallback, on purpose:
`draft_batch` has no dedicated prompt and `layers/2/PROMPT.md` is a reactive
queue-item-kind-dispatch shim, not a mission-materialization loop — flipping
this to "still due forever" would spawn a real subagent every eligible tick
against a mission its shim cannot actually execute (the #261/#277 fire-spin
class, on a live system, for a real reason this time). Giving
`draft_batch`/`qualify` their own dedicated `missions/<id>/PROMPT.md` (the
precedent set by #884 for `discovery`) is the follow-up that closes that
remaining gap; it is out of scope for this single-file dispatch_helpers.py
fix.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    build_dispatch_ctx,
    read_last_materialized,
    read_last_run,
)

# Wed 2026-07-08, 12:05 Paris (CEST, UTC+2) = 10:05 UTC — past L2's 12:00
# Paris floor, matching the live #870 evidence.
_NOW1 = datetime(2026, 7, 8, 10, 5, 0, tzinfo=timezone.utc)
_TODAY = _NOW1.strftime("%Y-%m-%d")

# The exact live shape reported in #870: an L2 shim-resolved (no dedicated
# PROMPT.md) proactive-batch mission whose real output is a hand-authored
# gate card, so materialize_due_missions_for_tick's #302 guard suppresses
# any bare stub for it.
_DRAFT_BATCH = {
    "id": "draft_batch",
    "layer": 2,
    "cadence": "daily",
    "time": "08:00",
    "output_queue": "queues/gates",
    "creates": ["dm_draft"],
}


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": [_DRAFT_BATCH]}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )
    return repo


def test_build_dispatch_ctx_never_writes_last_run_for_unrun_mission(tmp_path: Path):
    """FAIL-BEFORE / PASS-AFTER for #870.

    Two calls to build_dispatch_ctx (the live-loop default, materialize=True)
    ~30 minutes apart, with NO real dispatch of draft_batch's work in
    between (no gate card ever authored) — mirrors the exact board #870
    repro. Before the fix: `.last-run` is created on the FIRST call already.
    After the fix: `.last-run` is never created; only `.last-materialized`
    is, and it is stable across the second call (still no fire-spin)."""
    repo = _make_repo(tmp_path)
    mission_dir = repo / "outputs" / _TODAY / "missions" / "draft_batch"

    build_dispatch_ctx(repo, now_utc=_NOW1)

    assert read_last_run(mission_dir) is None, (
        "#870: build_dispatch_ctx must NEVER write .last-run for a mission "
        "whose real work never ran — draft_batch's gate card was never "
        "authored (queues/gates stayed empty), so .last-run must stay absent"
    )
    assert (repo / "queues" / "gates").exists() is False or not any(
        f for f in (repo / "queues" / "gates").iterdir()
        if f.is_file() and not f.name.startswith(".")
    ), "no bare gate stub should ever be created for draft_batch (#302, unaffected by #870)"
    first_materialized = read_last_materialized(mission_dir)
    assert first_materialized is not None, (
        "the materializer's own anti-fire-spin proxy stamp must still exist, "
        "just on .last-materialized instead of .last-run (#870)"
    )

    # A second build_dispatch_ctx call, later the same tick-day, with STILL no
    # real dispatch in between — the exact "re-stamps on EVERY call" repro
    # from the original board report.
    now2 = _NOW1 + timedelta(minutes=30)
    build_dispatch_ctx(repo, now_utc=now2)

    assert read_last_run(mission_dir) is None, (
        "#870: a second build_dispatch_ctx call must still never write "
        ".last-run for draft_batch"
    )
    # Idempotent: the materialization marker is not being churned every call
    # (no need for it to be re-stamped once already materialized today).
    assert read_last_materialized(mission_dir) == first_materialized


def test_readonly_gate_probe_still_writes_neither_marker(tmp_path: Path):
    """No regression on #454: a read-only gate-check call
    (materialize=False) must still write NEITHER `.last-run` NOR the new
    `.last-materialized` marker."""
    repo = _make_repo(tmp_path)
    mission_dir = repo / "outputs" / _TODAY / "missions" / "draft_batch"

    build_dispatch_ctx(repo, now_utc=_NOW1, materialize=False)

    assert read_last_run(mission_dir) is None
    assert read_last_materialized(mission_dir) is None
