"""Tests for #1235 — a dedicated-prompt mission's own output evidence must
corroborate its `.last-run` marker, not just the shared layer directory.

ROOT CAUSE (confirmed live, Ben's dept, 2026-09-11): `_layer_output_evidence_ok`
(#1080) only ever checked `outputs/<today>/<N>/` — the SHARED layer directory a
LEGACY-SHIM mission's real output lands in. A DEDICATED-PROMPT mission (one
with its own `missions/<id>/PROMPT.md`, e.g. Ben's `weekly_review`) writes its
real deliverables into its OWN `outputs/<today>/missions/<id>/` directory
instead — confirmed live: `weekly_kpi_review.md`/`.yaml` land there, never in
`outputs/<today>/4/`. So `weekly_review` genuinely completed at 15:02Z
(`.last-run` AND real output both present, just in its own mission dir), yet
a later 19:02Z tick's `select_due_missions_for_forced_layer(..., 4)` still
re-listed it as due: `_layer_output_evidence_ok` checked the shared `4/` dir
(empty — nothing else had run in L4 yet that tick), found no evidence, and
`_mission_last_fired` returned None even though the mission's own `.last-run`
was genuine and corroborated.

THE FIX: `_layer_output_evidence_ok` gained an optional `mission_id` — when
given, it ALSO accepts real output found under the mission's own
`outputs/<today>/missions/<id>/` directory as evidence, in addition to (never
instead of) the shared layer directory. `_mission_last_fired` now passes the
mission's own id through. This can only ADD evidence a caller would otherwise
miss (a shim mission never populates `missions/<id>/` at all — the check is a
pure no-op there), so #1080's original "shared dir empty -> died mid-dispatch"
protection for shim missions is unchanged (see test_1080_dispatch_output_truth.py
and test_518_floor_mission_granular.py, both still green after this change).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from scripts.lib.dispatch_helpers import (
    _any_mission_fired_today_for_layer,
    _layer_output_evidence_ok,
    _mission_last_fired,
    _mission_output_present,
    _record_dispatched_trigger_ids,
    build_dispatch_ctx,
    select_due_missions,
    select_due_missions_for_forced_layer,
    write_last_materialized,
    write_last_run,
)

# Mirrors the real incident: 2026-09-11 is a Friday.
AT_15_02_PARIS = datetime(2026, 9, 11, 13, 2, tzinfo=timezone.utc)   # 15:02 Paris (CEST, +2)
AT_19_02_PARIS = datetime(2026, 9, 11, 17, 2, tzinfo=timezone.utc)   # 19:02 Paris


def _mk_repo(tmp_path: Path) -> Path:
    (tmp_path / "queues" / "research").mkdir(parents=True)
    (tmp_path / "queues" / "inbox" / "decisions").mkdir(parents=True)
    return tmp_path


def _fire_prereqs(repo: Path, when: datetime) -> None:
    today = when.strftime("%Y-%m-%d")
    for n in (1, 2, 3):
        write_last_run(repo / "outputs" / today / str(n), when)


def _write_dept_yaml(repo: Path, missions: list[dict]) -> None:
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )


def _weekly_review(time: str = "15:00", day: str = "friday") -> dict:
    return {
        "id": "weekly_review", "layer": 4, "cadence": "weekly",
        "time": time, "day": day,
        "output_queue": "queues/research/", "creates": [],
    }


def _risk_control(time: str = "19:00") -> dict:
    return {
        "id": "risk_control", "layer": 4, "cadence": "daily", "time": time,
        "output_queue": "queues/research/", "creates": [],
    }


# ===========================================================================
# 1. _layer_output_evidence_ok — unit tests on the new mission_id branch
# ===========================================================================

def test_output_evidence_ok_true_when_only_the_mission_dir_has_output(tmp_path: Path):
    """The shared layer dir is EMPTY (not even created) — no OTHER mission has
    run in this layer yet today — but the mission's OWN dir has real output.
    Must be accepted: this is exactly `weekly_review`'s confirmed live shape."""
    (tmp_path / "missions" / "weekly_review").mkdir(parents=True)
    (tmp_path / "missions" / "weekly_review" / "weekly_kpi_review.md").write_text("ok")
    assert not (tmp_path / "4").exists()

    assert _layer_output_evidence_ok(str(tmp_path), 4, "weekly_review") is True


def test_output_evidence_ok_still_false_with_no_mission_id_and_empty_shared_dir(tmp_path: Path):
    """No regression: omitting mission_id (the #1080 call sites this doesn't
    touch) preserves the original shared-dir-only behavior."""
    (tmp_path / "missions" / "weekly_review").mkdir(parents=True)
    (tmp_path / "missions" / "weekly_review" / "weekly_kpi_review.md").write_text("ok")
    assert _layer_output_evidence_ok(str(tmp_path), 4) is False


def test_output_evidence_ok_false_when_mission_dir_has_only_the_marker(tmp_path: Path):
    """Died-mid-dispatch, dedicated-mission flavor: the mission's own dir has
    ONLY its `.last-run` (STEP 0 stamped, real work never happened) and the
    shared layer dir is untouched. Must still be rejected — #1080's
    protection must hold for dedicated missions too, not just shim ones."""
    write_last_run(tmp_path / "missions" / "weekly_review", AT_15_02_PARIS)
    assert _layer_output_evidence_ok(str(tmp_path), 4, "weekly_review") is False


def test_output_evidence_ok_true_when_shared_dir_has_output_even_without_mission_dir(tmp_path: Path):
    """The pre-existing shim path is unaffected: shared-dir evidence alone is
    still sufficient even when a mission_id is passed and its own dir is empty."""
    (tmp_path / "4").mkdir()
    (tmp_path / "4" / "risk-brief.md").write_text("ok")
    assert _layer_output_evidence_ok(str(tmp_path), 4, "risk_control") is True


# ===========================================================================
# 2. _mission_last_fired — direct per-mission-marker path
# ===========================================================================

def test_dedicated_mission_marker_with_own_dir_output_is_trusted_as_fired(tmp_path: Path):
    today_dir = tmp_path / "outputs" / "2026-09-11"
    prior_tick = AT_15_02_PARIS
    write_last_run(today_dir / "missions" / "weekly_review", prior_tick)
    (today_dir / "missions" / "weekly_review" / "weekly_kpi_review.md").write_text("ok")

    ctx = {"today_dir": str(today_dir), "now_utc": AT_19_02_PARIS,
           "layer_4_last_run_today": None}
    result = _mission_last_fired(ctx, {"id": "weekly_review", "layer": 4})

    assert result == prior_tick, (
        "a dedicated mission's own marker, corroborated by real output in ITS "
        "OWN mission dir, must be trusted as fired even when the shared layer "
        "dir has nothing in it yet — the exact #1235 incident"
    )


def test_dedicated_mission_marker_without_any_output_still_not_trusted(tmp_path: Path):
    """Recovery case preserved: marker exists, but NEITHER the mission's own
    dir NOR the shared layer dir has real output — died mid-dispatch. Must
    still return None so the mission is re-selected for recovery."""
    today_dir = tmp_path / "outputs" / "2026-09-11"
    prior_tick = AT_15_02_PARIS
    write_last_run(today_dir / "missions" / "weekly_review", prior_tick)

    ctx = {"today_dir": str(today_dir), "now_utc": AT_19_02_PARIS,
           "layer_4_last_run_today": None}
    result = _mission_last_fired(ctx, {"id": "weekly_review", "layer": 4})

    assert result is None


# ===========================================================================
# 3. End-to-end integration — the exact confirmed live incident, through both
#    dispatch entry points (the floor's forced-layer selector AND the
#    live-loop's select_due_missions).
# ===========================================================================

def test_floor_does_not_relist_a_completed_dedicated_mission_alongside_a_later_one(tmp_path: Path):
    """THE #1235 REPRO: weekly_review (dedicated prompt) completes at 15:02
    Paris, writing its real deliverable into ITS OWN mission dir. At a LATER
    19:02 Paris floor tick (risk_control's own slot, still same L4 layer),
    forced-L4 must return ONLY risk_control — NOT re-list weekly_review."""
    repo = _mk_repo(tmp_path)
    (repo / "missions" / "weekly_review").mkdir(parents=True)
    (repo / "missions" / "weekly_review" / "PROMPT.md").write_text("dedicated prompt")
    _write_dept_yaml(repo, [_weekly_review(), _risk_control()])
    _fire_prereqs(repo, AT_15_02_PARIS)

    write_last_run(repo / "outputs" / "2026-09-11" / "missions" / "weekly_review", AT_15_02_PARIS)
    (repo / "outputs" / "2026-09-11" / "missions" / "weekly_review" / "weekly_kpi_review.md").write_text(
        "ok"
    )

    due = select_due_missions_for_forced_layer(repo, 4, now_utc=AT_19_02_PARIS)
    ids = [m["id"] for m in due]

    assert ids == ["risk_control"], (
        f"weekly_review already completed (marker + real output in its own "
        f"mission dir) and must not re-list; risk_control's own slot has "
        f"opened and it must still dispatch. got {ids}"
    )


def test_live_loop_does_not_relist_a_completed_dedicated_mission(tmp_path: Path):
    """Same incident through the LIVE-LOOP entry point (select_due_missions /
    _due_missions_for_layer), which is the direct caller of
    `_mission_last_fired` (no shim-fallback wrapper involved)."""
    repo = _mk_repo(tmp_path)
    (repo / "missions" / "weekly_review").mkdir(parents=True)
    (repo / "missions" / "weekly_review" / "PROMPT.md").write_text("dedicated prompt")
    weekly_review = _weekly_review()
    _write_dept_yaml(repo, [weekly_review])

    today_dir = repo / "outputs" / "2026-09-11"
    write_last_run(today_dir / "missions" / "weekly_review", AT_15_02_PARIS)
    (today_dir / "missions" / "weekly_review" / "weekly_kpi_review.md").write_text("ok")

    ctx = build_dispatch_ctx(repo, now_utc=AT_19_02_PARIS)
    ctx["_repo_dir"] = str(repo)
    due = select_due_missions(ctx, [weekly_review])

    assert due == [], (
        "select_due_missions must not re-select an already-completed "
        "dedicated-prompt mission just because the shared layer dir is bare"
    )


def test_any_mission_fired_today_recognizes_a_dedicated_mission_own_dir_output(tmp_path: Path):
    """The SAME root bug also lived in `_any_mission_fired_today_for_layer`
    (the '#432 defect A' fallback signal, ctx['layer_N_mission_fired_today']):
    it applied `_layer_output_evidence_ok` ONCE for the whole layer, before
    even knowing which mission's marker it was about to trust — so a
    dedicated-prompt mission's own-dir output could never satisfy it either.
    Fixed by moving the check inside the per-mission loop with `mid`."""
    repo = _mk_repo(tmp_path)
    (repo / "missions" / "weekly_review").mkdir(parents=True)
    _write_dept_yaml(repo, [_weekly_review()])

    today_dir = repo / "outputs" / "2026-09-11"
    prior_tick = AT_15_02_PARIS
    write_last_run(today_dir / "missions" / "weekly_review", prior_tick)
    (today_dir / "missions" / "weekly_review" / "weekly_kpi_review.md").write_text("ok")
    assert not (today_dir / "4").exists()  # shared layer dir has nothing

    assert _any_mission_fired_today_for_layer(
        repo, today_dir, 4, now_utc=AT_19_02_PARIS
    ) is True, (
        "a dedicated mission's own marker + own-dir output must count as "
        "'this layer has fired today', even with the shared dir empty"
    )


def test_any_mission_fired_today_still_requires_output_somewhere(tmp_path: Path):
    """No regression: a marker with NO real output anywhere (own dir OR
    shared dir) must still NOT count as fired — #1080's died-mid-dispatch
    protection, now checked per-mission instead of once for the layer."""
    repo = _mk_repo(tmp_path)
    (repo / "missions" / "weekly_review").mkdir(parents=True)
    _write_dept_yaml(repo, [_weekly_review()])

    today_dir = repo / "outputs" / "2026-09-11"
    write_last_run(today_dir / "missions" / "weekly_review", AT_15_02_PARIS)

    assert _any_mission_fired_today_for_layer(
        repo, today_dir, 4, now_utc=AT_19_02_PARIS
    ) is False


def test_floor_still_recovers_a_dedicated_mission_that_died_mid_dispatch(tmp_path: Path):
    """No inverted-failure regression: a dedicated mission whose STEP 0 marker
    was stamped but which produced NO real output anywhere (own dir empty,
    shared dir empty) must still be selected for recovery — the #1080
    guarantee must hold for dedicated missions exactly as it does for shim
    ones."""
    repo = _mk_repo(tmp_path)
    (repo / "missions" / "weekly_review").mkdir(parents=True)
    (repo / "missions" / "weekly_review" / "PROMPT.md").write_text("dedicated prompt")
    _write_dept_yaml(repo, [_weekly_review()])
    _fire_prereqs(repo, AT_15_02_PARIS)

    write_last_run(repo / "outputs" / "2026-09-11" / "missions" / "weekly_review", AT_15_02_PARIS)
    # No real output anywhere — died mid-dispatch.

    due = select_due_missions_for_forced_layer(repo, 4, now_utc=AT_19_02_PARIS)
    assert [m["id"] for m in due] == ["weekly_review"], (
        "a dedicated mission with a marker but NO real output anywhere must "
        "still be recovered, not permanently starved"
    )


# ===========================================================================
# 4. CRITICAL inverted-failure catch (independent-reviewer finding,
#    pre-merge): `dispatched-items/` — dispatch's OWN bookkeeping written
#    into a mission's own dir — must NEVER count as real output evidence.
# ===========================================================================
#
# `_record_dispatched_trigger_ids` writes `outputs/<today>/missions/<id>/
# dispatched-items/<trigger-id>` UNCONDITIONALLY at materialize/decision time
# for ANY event-cadence mission with a pending trigger — before any subagent
# has run, let alone produced a deliverable. Naively reusing
# `layer_output_present` (which accepts ANY non-dotfile entry) on a
# mission's own dir would treat this bookkeeping directory as "real output":
# a died-mid-dispatch event mission (trigger consumed + `.last-materialized`
# stamped, subagent then died before writing `.last-run` or any real
# deliverable) would be wrongly read as FIRED and permanently starved of
# dispatch — the exact "worse than the bug" inverted failure this whole gate
# exists to prevent. Fixed via `_mission_output_present`, which additionally
# excludes `_MISSION_DIR_BOOKKEEPING_NAMES`.

def test_mission_output_present_excludes_dispatched_items_bookkeeping(tmp_path: Path):
    """Unit test on the helper directly: a mission dir containing ONLY the
    dispatched-items bookkeeping dir (no real deliverable) must read as
    'no output present'."""
    mission_dir = tmp_path / "missions" / "publish_on_approval"
    mission_dir.mkdir(parents=True)
    (mission_dir / "dispatched-items").mkdir()
    (mission_dir / "dispatched-items" / "trigger-xyz").write_text("2026-09-11T15:00:00+00:00")

    assert _mission_output_present(mission_dir) is False, (
        "dispatched-items/ is dispatch's own trigger-consumption ledger, "
        "not a deliverable — its presence alone must not count as output"
    )


def test_mission_output_present_true_with_a_real_deliverable_alongside_bookkeeping(tmp_path: Path):
    """Healthy-day counterpart: once a REAL deliverable also exists (in
    addition to the bookkeeping dir), it must be recognized as output."""
    mission_dir = tmp_path / "missions" / "publish_on_approval"
    mission_dir.mkdir(parents=True)
    (mission_dir / "dispatched-items").mkdir()
    (mission_dir / "dispatched-items" / "trigger-xyz").write_text("2026-09-11T15:00:00+00:00")
    (mission_dir / "publication.md").write_text("ok")

    assert _mission_output_present(mission_dir) is True


def test_died_mid_dispatch_event_mission_with_only_dispatched_items_is_not_trusted_as_fired(
    tmp_path: Path,
):
    """THE REPRO: an event-cadence L4 mission's trigger is consumed by the
    materializer (dispatched-items/ + .last-materialized written) at tick 1,
    then the subagent dies before writing .last-run or any real output. A
    later tick's `_mission_last_fired` must NOT trust the bookkeeping alone
    as evidence — the mission must still be recoverable, not permanently
    starved."""
    today_dir = tmp_path / "outputs" / "2026-09-11"
    mid = "publish_on_approval"
    tick1 = AT_15_02_PARIS

    _record_dispatched_trigger_ids(today_dir, mid, {"trigger-xyz"}, now_utc=tick1)
    write_last_materialized(today_dir / "missions" / mid, when=tick1)
    # Deliberately no .last-run and no real deliverable — died mid-dispatch.

    ctx = {"today_dir": str(today_dir), "now_utc": AT_19_02_PARIS,
           "layer_4_last_run_today": None}
    result = _mission_last_fired(ctx, {"id": mid, "layer": 4})

    assert result is None, (
        "dispatched-items/ bookkeeping alone must never be trusted as proof "
        "of real output — a died-mid-dispatch event mission must remain "
        "recoverable, not silently starved forever"
    )


def test_any_mission_fired_today_also_excludes_dispatched_items_bookkeeping(tmp_path: Path):
    """Same repro through `_any_mission_fired_today_for_layer` (the second
    call site the independent review confirmed shares this gate)."""
    repo = _mk_repo(tmp_path)
    mid = "publish_on_approval"
    _write_dept_yaml(repo, [{
        "id": mid, "layer": 4, "cadence": "event",
        "output_queue": "queues/research/", "creates": [],
    }])
    today_dir = repo / "outputs" / "2026-09-11"
    tick1 = AT_15_02_PARIS

    _record_dispatched_trigger_ids(today_dir, mid, {"trigger-xyz"}, now_utc=tick1)
    write_last_materialized(today_dir / "missions" / mid, when=tick1)

    assert _any_mission_fired_today_for_layer(
        repo, today_dir, 4, now_utc=AT_19_02_PARIS
    ) is False, (
        "dispatched-items/ bookkeeping alone must not make this fallback "
        "signal report the layer as having genuinely fired"
    )
