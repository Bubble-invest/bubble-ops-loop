"""Tests for #1080 — dispatch output-truth: stop trusting a `.last-run`
marker as "fired" when the mission's real output does not exist.

ROOT CAUSE (confirmed live, Ben dept, 2026-09-02, L1+L2+L4 all hit):
`materialize_due_missions_for_tick` stamps a shim-resolved mission's
per-mission marker (`outputs/<today>/missions/<id>/.last-run`) at DECISION
time — the SAME tick `build_dispatch_ctx(materialize=True)` runs merely to
decide what to dispatch, before the runtime has actually spawned (let alone
completed) any subagent for it. `_mission_last_fired` — the function that
reads this exact marker to decide whether a mission is "fired" for
`is_mission_due` — trusted any PRIOR-tick marker unconditionally. So a
session that materialized a mission and then died (or was never actually
dispatched) before producing its real deliverable was permanently read as
"already fired today" on every later tick — silently starving the mission
for the rest of the day, with no live session watching to catch it.

An output-evidence gate (`layer_output_present`, #749/#750 defect c)
already existed, but it lived ONLY inside
`_mission_last_fired_with_shim_fallback`'s OWN layer-marker fallback branch
— and that function checks `_mission_last_fired(ctx, mission)` FIRST and
returns immediately on any non-None value, so the direct per-mission-marker
path (used by the LIVE-LOOP's `select_due_missions` /
`_due_missions_for_layer` — the path that actually hit the live incident)
never went through the gate at all.

THE FIX: a new shared helper, `_layer_output_evidence_ok`, is now applied by
BOTH `_mission_last_fired` (the per-mission-marker path) and
`_mission_last_fired_with_shim_fallback` (the layer-marker fallback) — so
the two call sites can never diverge on what "fired" means. For L1/L4 (the
layers whose shim prompt is confirmed to write real STEP-3 output into
`outputs/<today>/<N>/` — see `layer_output_present`'s docstring), a
prior-tick marker is trusted as "fired" ONLY if that output also exists.
L2/L3 are deliberately left ungated (their real output lives in the vault /
a DB row, never in `outputs/<today>/{2,3}/` even on a fully healthy run) —
gating them would falsely force a needless re-run of a genuinely completed
mission every tick.

Interaction with other guards (verified, not just asserted):
  - #302 (never write a bare stub into queues/gates/) and the #1084/#1085
    accountant reconciliation fix both live entirely in
    `materialize_due_missions_for_tick`'s WRITE-side gate logic — this fix
    touches ONLY the READ-side (`_mission_last_fired` /
    `_mission_last_fired_with_shim_fallback`), so neither is touched.
  - #338/#965 (round_counter / L3 structural-defer gate) concern layer 3,
    which is NOT in `_LAYERS_WITH_OUTPUT_EVIDENCE` — the new gate is a no-op
    for L3 markers. The full `scripts/lib/tests/` suite (including
    test_965_l1_cycle_gate_starvation.py, updated in this same change to
    corroborate its L1/L4 markers with real output) stays green.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    _layer_output_evidence_ok,
    _mission_last_fired,
    _mission_last_fired_with_shim_fallback,
    build_dispatch_ctx,
    layer_output_present,
    materialize_due_missions_for_tick,
    read_last_run,
    select_due_missions,
    write_last_run,
)

# A Wednesday at 08:00 Paris (06:00 UTC in June/CEST) — past L1's 07:00 floor.
_DAY = datetime(2026, 6, 24, 6, 0, 0, tzinfo=timezone.utc)
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


def _mk_daily(mid: str, layer: int, time: str, **extra) -> dict:
    m = {
        "id": mid, "layer": layer, "cadence": "daily", "time": time,
        "output_queue": "queues/research/", "creates": [],
    }
    m.update(extra)
    return m


def _bare_ctx(today_dir: "Path | str", now: datetime, layer: int) -> dict:
    """Minimal ctx for _mission_last_fired as a pure function."""
    return {
        "today_dir": str(today_dir),
        "now_utc": now,
        f"layer_{layer}_last_run_today": None,
    }


# ===========================================================================
# 1. _layer_output_evidence_ok — unit tests on the shared gate
# ===========================================================================

def test_output_evidence_ok_true_for_l2_l3_regardless_of_output(tmp_path: Path):
    """L2/L3 are never gated — real output there lives outside outputs/<today>/."""
    assert _layer_output_evidence_ok(str(tmp_path), 2) is True
    assert _layer_output_evidence_ok(str(tmp_path), 3) is True


def test_output_evidence_ok_false_for_l1_l4_when_output_missing(tmp_path: Path):
    (tmp_path / "1").mkdir()
    (tmp_path / "4").mkdir()
    assert _layer_output_evidence_ok(str(tmp_path), 1) is False
    assert _layer_output_evidence_ok(str(tmp_path), 4) is False


def test_output_evidence_ok_true_for_l1_l4_when_output_present(tmp_path: Path):
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "situation_brief.md").write_text("ok")
    (tmp_path / "4").mkdir()
    (tmp_path / "4" / "risk-brief.md").write_text("ok")
    assert _layer_output_evidence_ok(str(tmp_path), 1) is True
    assert _layer_output_evidence_ok(str(tmp_path), 4) is True


def test_output_evidence_ok_fails_open_when_today_dir_absent():
    """No today_dir at all -> fail-open (True) — matches _mission_last_fired's
    own pre-existing fail-open when today_dir is missing from ctx entirely."""
    assert _layer_output_evidence_ok(None, 1) is True
    assert _layer_output_evidence_ok("", 4) is True


# ===========================================================================
# 2. _mission_last_fired — the direct per-mission-marker path (#1080's actual
#    fix target: this is what select_due_missions/_due_missions_for_layer
#    calls on the LIVE-LOOP path, unlike the floor path's shim-fallback).
# ===========================================================================

def test_l1_marker_without_output_is_not_trusted_as_fired(tmp_path: Path):
    """THE #1080 BUG, reproduced directly: a per-mission marker exists from a
    PRIOR tick (exactly what materialize_due_missions_for_tick stamps at
    DECISION time for a shim-resolved mission) but outputs/<today>/1/ has
    nothing besides the marker itself never got a chance to exist — the
    mission's real STEP-3 output is simply absent (died mid-dispatch, or
    never actually dispatched at all). Before the fix, this returned the
    marker unconditionally -> is_mission_due vetoed a re-dispatch -> silent
    starvation for the rest of the day. After the fix: not trusted -> None."""
    mid = "data_update"
    today_dir = _today_dir(tmp_path)
    prior_tick = _DAY - timedelta(hours=1)
    write_last_run(today_dir / "missions" / mid, prior_tick)
    # Sanity: no real output exists anywhere under outputs/<today>/1/.
    assert not (today_dir / "1").exists()

    ctx = _bare_ctx(today_dir, _DAY, layer=1)
    result = _mission_last_fired(ctx, {"id": mid, "layer": 1})

    assert result is None, (
        "a per-mission marker with NO corroborating L1 output must NOT be "
        "trusted as 'fired' — this is the exact false-marker bug #1080 fixes"
    )


def test_l1_marker_with_real_output_is_trusted_as_fired(tmp_path: Path):
    """Healthy-day counterpart: the mission genuinely completed — marker AND
    real output both exist. Must be trusted as fired (no needless re-run)."""
    mid = "data_update"
    today_dir = _today_dir(tmp_path)
    prior_tick = _DAY - timedelta(hours=1)
    write_last_run(today_dir / "missions" / mid, prior_tick)
    (today_dir / "1").mkdir(parents=True, exist_ok=True)
    (today_dir / "1" / "situation_brief.md").write_text("ok")

    ctx = _bare_ctx(today_dir, _DAY, layer=1)
    result = _mission_last_fired(ctx, {"id": mid, "layer": 1})

    assert result == prior_tick, (
        "a per-mission marker corroborated by real L1 output must be trusted "
        "as fired — the gate must not force a needless re-run of a healthy mission"
    )


def test_l4_marker_without_output_is_not_trusted_as_fired(tmp_path: Path):
    """Same defect, L4 (also hit live 2026-09-02)."""
    mid = "risk_control"
    today_dir = _today_dir(tmp_path)
    prior_tick = _DAY - timedelta(hours=1)
    write_last_run(today_dir / "missions" / mid, prior_tick)

    ctx = _bare_ctx(today_dir, _DAY, layer=4)
    result = _mission_last_fired(ctx, {"id": mid, "layer": 4})

    assert result is None


def test_l2_marker_without_output_still_trusted_as_fired(tmp_path: Path):
    """L2/L3 must NOT be gated — their real output never lands in
    outputs/<today>/{2,3}/ even on a fully successful run (vault note / DB
    row instead). Gating them would falsely re-run a genuinely healthy
    mission every single tick."""
    mid = "research"
    today_dir = _today_dir(tmp_path)
    prior_tick = _DAY - timedelta(hours=1)
    write_last_run(today_dir / "missions" / mid, prior_tick)
    assert not (today_dir / "2").exists()

    ctx = _bare_ctx(today_dir, _DAY, layer=2)
    result = _mission_last_fired(ctx, {"id": mid, "layer": 2})

    assert result == prior_tick, (
        "L2 must still be treated as fired from the marker alone — no "
        "output-evidence requirement there (pre-existing, unchanged rule)"
    )


def test_same_tick_marker_still_excluded_regardless_of_output(tmp_path: Path):
    """No regression: the pre-existing same-tick exclusion still applies
    BEFORE the new output-evidence gate is even reached — a marker stamped
    at exactly now_utc means 'became due this tick', not 'fired'."""
    mid = "data_update"
    today_dir = _today_dir(tmp_path)
    write_last_run(today_dir / "missions" / mid, _DAY)  # stamped THIS tick
    (today_dir / "1").mkdir(parents=True, exist_ok=True)
    (today_dir / "1" / "situation_brief.md").write_text("ok")  # even with output present

    ctx = _bare_ctx(today_dir, _DAY, layer=1)
    result = _mission_last_fired(ctx, {"id": mid, "layer": 1})

    assert result is None, (
        "a same-tick marker must still be treated as 'not yet dispatched', "
        "independent of the new output-evidence gate"
    )


# ===========================================================================
# 3. _mission_last_fired_with_shim_fallback — confirm the two call sites
#    (per-mission path + layer-marker fallback) now share the SAME gate via
#    _layer_output_evidence_ok (refactor, not just an additive check).
# ===========================================================================

def test_shim_fallback_still_gates_its_own_layer_marker_path(tmp_path: Path):
    """No regression on the pre-existing #749/#750 defect-c coverage: with NO
    per-mission marker at all, a shim-resolved mission falls back to the
    layer marker, which is STILL gated on output evidence for L4."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "layers" / "4").mkdir(parents=True)
    (repo / "layers" / "4" / "PROMPT.md").write_text("legacy L4 shim")
    today_dir = _today_dir(repo)
    prior_tick = _DAY - timedelta(hours=1)
    write_last_run(today_dir / "4", prior_tick)  # layer marker only, no output

    ctx = {"today_dir": str(today_dir), "now_utc": _DAY,
           "layer_4_last_run_today": prior_tick}
    mission = {"id": "risk_control", "layer": 4, "cadence": "daily", "time": "21:00"}

    result = _mission_last_fired_with_shim_fallback(repo, ctx, mission)
    assert result is None, (
        "the shim-fallback's own output-evidence gate (#749/#750 defect c) "
        "must still work after the #1080 refactor onto the shared helper"
    )


# ===========================================================================
# 4. End-to-end integration — reproduces the LIVE incident through the
#    actual dispatch pipeline (materialize_due_missions_for_tick +
#    build_dispatch_ctx + select_due_missions), not just the unit-level
#    helper. This is the closest fail-before/pass-after repro of the
#    reported bug: "build_dispatch_ctx(materialize=True), called merely to
#    DECIDE, stamped the marker; a later tick read it as fired though
#    nothing had run."
# ===========================================================================

def test_integration_shim_mission_survives_died_mid_dispatch_session(tmp_path: Path):
    """TICK 1: data_update (shim-resolved, L1) is due — build_dispatch_ctx's
    materialize side effect stamps its per-mission marker at decision time,
    exactly as it does live. Simulate the dispatched session then dying
    before producing any real output (the confirmed live failure mode).
    TICK 2 (a later tick, same day): select_due_missions must STILL return
    data_update as due — recovery, not silent starvation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "queues" / "inbox" / "decisions").mkdir(parents=True)
    data_update = _mk_daily("data_update", layer=1, time="07:00")
    _make_dept_yaml(repo, [data_update])

    # TICK 1 — 08:00 Paris: data_update becomes due; the live loop's decision
    # call (materialize=True, the default) stamps its per-mission marker as a
    # side effect of DECIDING, before any subagent has run.
    tick1 = _DAY
    ctx1 = build_dispatch_ctx(repo, now_utc=tick1)
    ctx1["_repo_dir"] = str(repo)
    due1 = select_due_missions(ctx1, [data_update])
    assert "data_update" in [m["id"] for m in due1], (
        "tick 1: data_update must be selected (never fired, time reached)"
    )
    today_dir = repo / "outputs" / _TODAY
    marker = read_last_run(today_dir / "missions" / "data_update")
    assert marker is not None, (
        "precondition: the materializer must have stamped the per-mission "
        "marker as a decision-time side effect (this IS the root cause)"
    )
    # Nothing besides the marker exists anywhere under outputs/<today>/1/ —
    # the dispatched session died before STEP 3 (or was never truly spawned).
    assert not (today_dir / "1").exists(), (
        "precondition: no real L1 output exists — the confirmed live failure mode"
    )

    # TICK 2 — 10:00 Paris, same day: a later tick re-decides.
    tick2 = _DAY + timedelta(hours=2)
    ctx2 = build_dispatch_ctx(repo, now_utc=tick2)
    ctx2["_repo_dir"] = str(repo)
    due2 = select_due_missions(ctx2, [data_update])

    assert "data_update" in [m["id"] for m in due2], (
        "THE #1080 FIX: data_update must be RE-SELECTED for recovery on "
        "tick 2 — before the fix, the decision-time marker from tick 1 was "
        "read as 'already fired today' and due2 came back [], silently "
        "starving the mission for the rest of the day with no live session "
        "watching to catch it (exactly the confirmed 2026-09-02 incident)."
    )


def test_integration_shim_mission_excluded_once_real_output_appears(tmp_path: Path):
    """Healthy-day counterpart to the test above: once data_update's real
    output actually appears (the session completed normally), a later tick
    must NOT re-select it — no needless re-run of a genuinely healthy
    mission."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "queues" / "research").mkdir(parents=True)
    (repo / "queues" / "inbox" / "decisions").mkdir(parents=True)
    data_update = _mk_daily("data_update", layer=1, time="07:00")
    _make_dept_yaml(repo, [data_update])

    tick1 = _DAY
    ctx1 = build_dispatch_ctx(repo, now_utc=tick1)
    ctx1["_repo_dir"] = str(repo)
    select_due_missions(ctx1, [data_update])

    # The dispatched session completes normally and writes its real artifact.
    today_dir = repo / "outputs" / _TODAY
    (today_dir / "1").mkdir(parents=True, exist_ok=True)
    (today_dir / "1" / "situation_brief.md").write_text("ok")

    tick2 = _DAY + timedelta(hours=2)
    ctx2 = build_dispatch_ctx(repo, now_utc=tick2)
    ctx2["_repo_dir"] = str(repo)
    due2 = select_due_missions(ctx2, [data_update])

    assert "data_update" not in [m["id"] for m in due2], (
        "data_update genuinely completed (marker + real output both present) "
        "— it must not be re-selected on a later tick the same day"
    )
