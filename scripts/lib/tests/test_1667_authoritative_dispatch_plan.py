"""Characterize the migration seam for board #1667.

`build_dispatch_plan` is a read-only compatibility adapter: callers get one
mission-centric authoritative phase while the historical `decide_dispatch`
phase remains visible during the mixed-consumer rollout.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scripts.lib.dispatch_helpers import (
    _mission_handled_marker,
    build_dispatch_plan,
    maybe_defer_ad_hoc_l3,
    read_dispatch_ledger,
    read_round_counter,
    write_last_materialized,
    write_last_run,
)


_AFTER_L2 = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)  # 13:00 Paris


def _ctx(**overrides) -> dict:
    base = {
        "now_utc": _AFTER_L2,
        "today": "2026-10-01",
        "today_dir": "/nonexistent/outputs/2026-10-01",
        "has_research_items": True,
        "has_inbox_decisions": False,
        "has_unconsumed_mgmt_notes": False,
        "layer_1_last_run_today": _AFTER_L2,
        "layer_2_last_run_today": None,
        "layer_3_last_run_today": None,
        "layer_4_last_run_today": None,
        "round_counter": {},
        "layer_1_baseline_counter": {},
        "fire_after_rounds": 1,
    }
    base.update(overrides)
    return base


def _daily(mid: str, layer: int, at: str) -> dict:
    return {
        "id": mid,
        "layer": layer,
        "cadence": "daily",
        "time": at,
        "output_queue": "queues/research/",
        "creates": [],
    }


def test_plan_exposes_selected_missions_as_authoritative_phase():
    plan = build_dispatch_plan(
        _ctx(),
        [_daily("research_b", 2, "12:00"), _daily("research_a", 2, "12:00")],
    )

    assert plan["phase"] == "layer_2"
    assert [mission["id"] for mission in plan["missions"]] == [
        "research_a",
        "research_b",
    ]
    assert plan["legacy_phase"] == "layer_2"


def test_plan_phase_follows_fallthrough_missions_not_legacy_phase():
    plan = build_dispatch_plan(
        _ctx(
            has_inbox_decisions=True,
            layer_1_last_run_today=_AFTER_L2,
        ),
        [_daily("research", 2, "12:00")],
    )

    assert plan["legacy_phase"] == "layer_3"
    assert plan["phase"] == "layer_2"
    assert [mission["id"] for mission in plan["missions"]] == ["research"]


def test_plan_preserves_terminal_l3_defer_during_l2_fallthrough(tmp_path: Path):
    """Mission dispatch follows L2 while the legacy L3 signal still commits
    the required structural human defer through the real terminal helper."""
    today_dir = tmp_path / "outputs" / "2026-10-01"
    ctx = _ctx(
        today_dir=str(today_dir),
        has_inbox_decisions=True,
        layer_1_last_run_today=_AFTER_L2,
    )
    missions = [_daily("research", 2, "12:00")]

    plan = build_dispatch_plan(ctx, missions)

    assert plan["phase"] == "layer_2"
    assert [mission["id"] for mission in plan["missions"]] == ["research"]
    assert plan["legacy_phase"] == "layer_3"
    assert maybe_defer_ad_hoc_l3(
        ctx,
        missions,
        phase=plan["ad_hoc_l3_defer_phase"],
    )
    assert read_dispatch_ledger(today_dir)["__ad_hoc_l3_human_defer__"][
        "completed_at"
    ] == _AFTER_L2.isoformat()
    assert read_round_counter(today_dir)["3"] == 1


def test_plan_reports_heartbeat_when_legacy_phase_has_no_due_mission():
    plan = build_dispatch_plan(_ctx(), [])

    assert plan == {
        "phase": "heartbeat",
        "missions": [],
        "legacy_phase": "layer_2",
        "ad_hoc_l3_defer_phase": "layer_2",
    }


def test_plan_is_read_only(tmp_path: Path):
    repo = tmp_path / "dept"
    repo.mkdir()
    today_dir = repo / "outputs" / "2026-10-01"
    ctx = _ctx(_repo_dir=str(repo), today_dir=str(today_dir))

    before = sorted(path.relative_to(repo) for path in repo.rglob("*"))
    build_dispatch_plan(ctx, [_daily("research", 2, "12:00")])
    after = sorted(path.relative_to(repo) for path in repo.rglob("*"))

    assert after == before


def test_ledger_completion_precedes_legacy_markers(tmp_path: Path):
    today_dir = tmp_path / "outputs" / "2026-10-01"
    mission_dir = today_dir / "missions" / "research"
    legacy_run = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    legacy_materialized = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    committed = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

    write_last_run(mission_dir, legacy_run)
    write_last_materialized(mission_dir, legacy_materialized)
    (today_dir / "dispatch.json").write_text(
        '{"research":{"completed_at":"2026-10-01T10:00:00+00:00"}}\n',
        encoding="utf-8",
    )

    assert _mission_handled_marker(today_dir, "research") == committed


def test_legacy_markers_remain_ordered_fallbacks_without_ledger(tmp_path: Path):
    today_dir = tmp_path / "outputs" / "2026-10-01"
    real_dir = today_dir / "missions" / "real"
    proxy_dir = today_dir / "missions" / "proxy"
    real = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
    proxy = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    write_last_run(real_dir, real)
    write_last_materialized(real_dir, proxy)
    write_last_materialized(proxy_dir, proxy)

    assert _mission_handled_marker(today_dir, "real") == real
    assert _mission_handled_marker(today_dir, "proxy") == proxy
