"""Tests for #432 — two coupled silent-skip bugs in dispatch_helpers.py.

Both defects are the same class as #375/#428: a premature "done" marker
written at DECISION time (before/without the real work being dispatched)
silently kills a mission for the rest of the day. Found LIVE on
content/Miranda 2026-06-30: L4 daily debrief silently killed, feedback_digest
stale 13 days.

DEFECT A — `_layer_fired_today(ctx, layer)` only consults the LAYER-level
`.last-run` marker (outputs/<today>/<N>/.last-run) and round_counter. After
the per-mission migration, a layer can run entirely via its per-mission path
(e.g. content_daily_rotation, a shim-resolved L1 mission) — which stamps
`outputs/<today>/missions/<id>/.last-run` but NEVER the layer-level marker
(that's only written by the loop runner / a layer's STEP 0, which the
per-mission path bypasses). So `_layer_fired_today(ctx, 1)` reports False all
day even though L1 plainly ran → the L4 gate
(`l1_fired AND (l2_fired or not has_research) AND (l3_fired or not has_decisions)`)
never opens → L4 (daily debrief) never dispatches.

Fix: `_layer_fired_today` gets a THIRD fallback — if any per-mission marker
for a mission belonging to that layer was stamped today, treat the layer as
fired. `build_dispatch_ctx` computes this per-layer "any per-mission ran
today" set from dept.yaml's recurring_missions + today_dir and injects it
into ctx as `layer_N_mission_fired_today` (boolean) for N in 1..4.

DEFECT B — in `materialize_due_missions_for_tick`, the NON-EVENT (daily)
pre-stamp branch stamps a due daily mission's per-mission marker
unconditionally (gated only by `_mission_authors_own_marker`), with NO
highest-eligible-layer gate — unlike the EVENT branch (fixed by #375-v3),
which only stamps when `_highest_eligible_layer_from_signals(...) ==
layer_n`. So a daily shim mission that is `is_mission_due=True` but whose
layer is NOT this tick's highest-eligible layer gets stamped-as-done WITHOUT
ever being dispatched -> silent skip for the rest of the day.

Fix: gate the daily pre-stamp the same way: compute `highest_eligible` once
(reusing the same signal extraction the event branch already computes) and
only stamp a daily due mission when `int(m.get("layer", 0)) == highest_eligible`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    _layer_fired_today,
    build_dispatch_ctx,
    decide_dispatch,
    materialize_due_missions_for_tick,
    read_last_run,
    write_last_run,
)

# A day at 19:30 Paris (17:30 UTC summer / CEST) — past the L4 19:00 floor.
_DAY = datetime(2026, 6, 30, 17, 30, 0, tzinfo=timezone.utc)
_TODAY = _DAY.strftime("%Y-%m-%d")


def _make_dept_yaml(repo: Path, missions: list[dict]) -> None:
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )


def _today_dir(repo: Path, now: datetime = _DAY) -> Path:
    td = repo / "outputs" / now.strftime("%Y-%m-%d")
    td.mkdir(parents=True, exist_ok=True)
    return td


# ===========================================================================
# DEFECT A — _layer_fired_today must fall back to per-mission markers
# ===========================================================================

def test_layer_fired_today_falls_back_to_per_mission_marker():
    """A ctx where L1's per-mission marker exists today but the layer-level
    1/.last-run does NOT, and round_counter is 0 -> _layer_fired_today(ctx, 1)
    must be True (currently False: only layer-marker / round_counter checked)."""
    ctx = {
        "layer_1_last_run_today": None,
        "round_counter": {},
        "layer_1_mission_fired_today": True,  # new ctx key the fix adds
    }
    assert _layer_fired_today(ctx, 1) is True, (
        "a per-mission marker for a layer-1 mission stamped today must count "
        "as the layer having fired, even with no layer-level marker and "
        "round_counter empty"
    )


def test_layer_fired_today_still_false_with_no_signal_at_all():
    """Sanity / no-regression: with no layer marker, no round_counter, and no
    per-mission fallback signal, the layer reports as NOT fired."""
    ctx = {
        "layer_1_last_run_today": None,
        "round_counter": {},
        "layer_1_mission_fired_today": False,
    }
    assert _layer_fired_today(ctx, 1) is False


def test_layer_fired_today_layer_marker_path_unbroken():
    """No regression: the original layer-marker path still works even if the
    new fallback key is absent from ctx entirely."""
    ctx = {
        "layer_1_last_run_today": _DAY,
        "round_counter": {},
    }
    assert _layer_fired_today(ctx, 1) is True


def test_layer_fired_today_round_counter_path_unbroken():
    """No regression: the original round_counter fallback still works."""
    ctx = {
        "layer_1_last_run_today": None,
        "round_counter": {"1": 2},
    }
    assert _layer_fired_today(ctx, 1) is True


def test_build_dispatch_ctx_injects_per_mission_fallback_for_layer(tmp_path: Path):
    """build_dispatch_ctx must populate the per-layer per-mission-fired signal
    by scanning today_dir/missions/<id>/.last-run for missions whose
    dept.yaml layer == N — reproducing the LIVE bug: content_daily_rotation
    (L1) ran via its per-mission path only (no outputs/<today>/1/.last-run)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "queues" / "inbox" / "decisions").mkdir(parents=True)
    _make_dept_yaml(repo, [
        {
            "id": "content_daily_rotation",
            "layer": 1,
            "cadence": "daily",
            "time": "07:00",
            "output_queue": "queues/research/",
            "creates": [],
        }
    ])
    today_dir = _today_dir(repo)
    # Per-mission marker stamped (mirrors materialize_due_missions_for_tick
    # having already run earlier today) but NO layer-level marker.
    write_last_run(today_dir / "missions" / "content_daily_rotation", _DAY - timedelta(hours=10))
    assert not (today_dir / "1" / ".last-run").exists()

    ctx = build_dispatch_ctx(repo, now_utc=_DAY)

    assert ctx.get("layer_1_last_run_today") is None, (
        "test premise: the layer-level marker is genuinely absent"
    )
    assert _layer_fired_today(ctx, 1) is True, (
        "build_dispatch_ctx must surface the per-mission marker as a "
        "layer-fired fallback signal so _layer_fired_today(ctx, 1) is True"
    )


def test_l4_gate_opens_via_per_mission_fallback_integration(tmp_path: Path):
    """Integration: with L1 fired ONLY via its per-mission marker (no layer-1
    marker), L2/L3 satisfied (no research/decisions pending => vacuously
    satisfied), and time >= 19:00 Paris, decide_dispatch must open the L4
    gate (currently stuck on 'heartbeat' because l1_fired is wrongly False)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "queues" / "inbox" / "decisions").mkdir(parents=True)
    _make_dept_yaml(repo, [
        {
            "id": "content_daily_rotation",
            "layer": 1,
            "cadence": "daily",
            "time": "07:00",
            "output_queue": "queues/research/",
            "creates": [],
        }
    ])
    today_dir = _today_dir(repo)
    write_last_run(today_dir / "missions" / "content_daily_rotation", _DAY - timedelta(hours=10))

    ctx = build_dispatch_ctx(repo, now_utc=_DAY)
    decision = decide_dispatch(ctx)

    assert decision == "layer_4", (
        f"expected the L4 gate to open via the per-mission fallback, got {decision!r}"
    )


# ===========================================================================
# DEFECT B — daily pre-stamp branch must gate on highest-eligible layer
# ===========================================================================

def test_daily_prestamp_not_stamped_when_layer_not_highest_eligible(tmp_path: Path):
    """A daily shim L4 mission that is is_mission_due=True but whose layer is
    NOT this tick's highest-eligible layer must NOT be pre-stamped.

    Reproduces the LIVE bug: synthesizing_content_feedback (L4 daily shim,
    no dedicated PROMPT.md) stamped at 19:33 with zero work, because L1 was
    the eligible layer this tick (not yet fired today), not L4.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_dept_yaml(repo, [
        {
            "id": "synthesizing_content_feedback",
            "layer": 4,
            "cadence": "daily",
            "time": "19:00",
            "output_queue": "queues/research/",
            "creates": [],
        }
    ])
    # NO dedicated PROMPT.md -> shim-resolved, _mission_authors_own_marker=False.
    today_dir = _today_dir(repo)
    # L1 has NOT fired today -> the highest-eligible layer this tick is L1
    # (C.0 in decide_dispatch), NOT L4 -- even though L4's own time floor
    # (19:00) has been reached and is_mission_due is True for the L4 mission.

    materialize_due_missions_for_tick(repo, today_dir, _DAY)

    marker = read_last_run(today_dir / "missions" / "synthesizing_content_feedback")
    assert marker is None, (
        "a daily mission must NOT be pre-stamped when its layer is not the "
        "highest-eligible layer this tick -- it was never actually dispatched"
    )


def test_daily_prestamp_still_stamped_when_layer_is_highest_eligible(tmp_path: Path):
    """Positive / no-fire-spin case: when the daily shim mission's layer IS
    the highest-eligible layer this tick, it DOES get stamped (idempotence
    preserved — otherwise it would re-materialize / re-dispatch every tick)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_dept_yaml(repo, [
        {
            "id": "synthesizing_content_feedback",
            "layer": 4,
            "cadence": "daily",
            "time": "19:00",
            "output_queue": "queues/research/",
            "creates": [],
        }
    ])
    today_dir = _today_dir(repo)
    # Make L1/L2/L3 already fired today so L4 IS the highest-eligible layer
    # at 19:30 Paris (mirrors decide_dispatch's C.1 gate).
    write_last_run(today_dir / "1", _DAY - timedelta(hours=12))
    write_last_run(today_dir / "2", _DAY - timedelta(hours=8))
    write_last_run(today_dir / "3", _DAY - timedelta(hours=4))

    materialize_due_missions_for_tick(repo, today_dir, _DAY)

    marker = read_last_run(today_dir / "missions" / "synthesizing_content_feedback")
    assert marker is not None, (
        "when the mission's layer IS the highest-eligible layer this tick, it "
        "is actually being dispatched -> it MUST be stamped (anti fire-spin)"
    )


def test_daily_prestamp_dedicated_prompt_mission_still_never_prestamped(tmp_path: Path):
    """No regression: a dedicated-prompt daily mission is still never
    pre-stamped by the materializer, regardless of highest-eligible layer
    (that guard, #428, is independent of and additive to the #432 gate)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_dept_yaml(repo, [
        {
            "id": "feedback_digest",
            "layer": 4,
            "cadence": "daily",
            "time": "19:00",
            "output_queue": "queues/research/",
            "creates": ["feedback_digest_report"],
        }
    ])
    pdir = repo / "missions" / "feedback_digest"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "PROMPT.md").write_text("# dedicated prompt\n", encoding="utf-8")
    today_dir = _today_dir(repo)
    # Make L4 the highest-eligible layer this tick.
    write_last_run(today_dir / "1", _DAY - timedelta(hours=12))
    write_last_run(today_dir / "2", _DAY - timedelta(hours=8))
    write_last_run(today_dir / "3", _DAY - timedelta(hours=4))

    materialize_due_missions_for_tick(repo, today_dir, _DAY)

    marker = read_last_run(today_dir / "missions" / "feedback_digest")
    assert marker is None, (
        "dedicated-prompt missions are never pre-stamped by the materializer "
        "(#428), independent of the #432 highest-eligible gate"
    )
