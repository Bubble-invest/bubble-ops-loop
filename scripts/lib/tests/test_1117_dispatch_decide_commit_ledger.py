"""Architecture card #1117: dispatch DECIDE is pure; COMMIT records truth."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    build_dispatch_ctx,
    commit_dispatch,
    decide_dispatch,
    event_trigger_ids_for_dispatch,
    read_dispatch_ledger,
    select_due_missions,
    select_due_missions_for_forced_layer,
    write_last_run,
)


NOW = datetime(2026, 9, 4, 6, 5, tzinfo=timezone.utc)  # 08:05 Paris
TODAY = "2026-09-04"


def _mission(*, cadence: str = "daily", output_queue: str = "queues/research/") -> dict:
    return {
        "id": "data_update",
        "layer": 1,
        "cadence": cadence,
        "time": "07:00",
        "output_queue": output_queue,
        "creates": ["research_item"],
    }


def _repo(tmp_path: Path, mission: dict | None = None) -> tuple[Path, list[dict]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    missions = [mission or _mission()]
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({"recurring_missions": missions}), encoding="utf-8"
    )
    return repo, missions


def _due(repo: Path, missions: list[dict], when: datetime) -> list[str]:
    ctx = build_dispatch_ctx(repo, now_utc=when)
    return [m["id"] for m in select_due_missions(ctx, missions)]


def test_repeated_decide_same_tick_is_read_only_and_does_not_starve(tmp_path: Path):
    repo, missions = _repo(tmp_path)

    first = _due(repo, missions, NOW)
    second = _due(repo, missions, NOW)

    assert first == second == ["data_update"]
    assert not (repo / "outputs").exists()
    assert not (repo / "queues").exists()


def test_watchdog_rekick_retries_uncommitted_then_stops_after_commit(tmp_path: Path):
    repo, missions = _repo(tmp_path)

    assert _due(repo, missions, NOW) == ["data_update"]
    # Simulate the first runtime dying before its worker returns: no COMMIT.
    rekick = NOW + timedelta(minutes=5)
    assert _due(repo, missions, rekick) == ["data_update"]

    artifact = repo / "outputs" / TODAY / "1" / "summary.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("completed\n", encoding="utf-8")
    assert commit_dispatch(
        repo,
        missions[0],
        dispatched_at=rekick,
        completed_at=rekick + timedelta(seconds=20),
        artifacts=[artifact],
    )

    # No wall-clock equality exception: a ctx rebuilt at the exact completion
    # timestamp sees a real completion and cannot double-fire.
    assert _due(repo, missions, rekick + timedelta(seconds=20)) == []
    assert _due(repo, missions, rekick + timedelta(minutes=5)) == []
    ledger = read_dispatch_ledger(repo / "outputs" / TODAY)
    assert set(ledger) == {"data_update"}
    assert set(ledger["data_update"]) == {
        "materialized_at", "dispatched_at", "completed_at", "artifacts"
    }
    assert ledger["data_update"]["artifacts"] == [
        "outputs/2026-09-04/1/summary.md",
        "queues/research/research_item-data_update-20260904-061020.yaml",
    ]
    assert json.loads(
        (repo / "outputs" / TODAY / "round_counter.json").read_text()
    ) == {"1": 1}


def test_failed_worker_cannot_advance_round_or_baseline(tmp_path: Path):
    repo, missions = _repo(tmp_path)

    assert _due(repo, missions, NOW) == ["data_update"]
    assert not (repo / "outputs").exists()

    artifact = repo / "outputs" / TODAY / "1" / "summary.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("completed\n", encoding="utf-8")
    assert commit_dispatch(
        repo, missions[0], dispatched_at=NOW, completed_at=NOW, artifacts=[artifact]
    )
    assert (repo / "outputs" / TODAY / ".l1-baseline.json").is_file()

    # A replay is a no-op across the ledger and the cycle counter.
    assert not commit_dispatch(
        repo, missions[0], dispatched_at=NOW, completed_at=NOW, artifacts=[artifact]
    )
    assert json.loads(
        (repo / "outputs" / TODAY / "round_counter.json").read_text()
    ) == {"1": 1}


def test_floor_probe_is_read_only_and_cannot_veto_live_dispatch(tmp_path: Path):
    repo, missions = _repo(tmp_path)

    assert [m["id"] for m in select_due_missions_for_forced_layer(repo, 1, now_utc=NOW)] == [
        "data_update"
    ]
    assert not (repo / "outputs").exists()
    assert _due(repo, missions, NOW + timedelta(seconds=9)) == ["data_update"]


def test_legacy_markers_remain_readable_during_rollout(tmp_path: Path):
    repo, missions = _repo(tmp_path)
    marker = repo / "outputs" / TODAY / "missions" / "data_update" / ".last-run"
    marker.parent.mkdir(parents=True)
    marker.write_text((NOW - timedelta(minutes=1)).isoformat(), encoding="utf-8")
    layer_dir = repo / "outputs" / TODAY / "1"
    layer_dir.mkdir(parents=True)
    (layer_dir / "summary.md").write_text("legacy completion\n", encoding="utf-8")

    assert _due(repo, missions, NOW) == []
    assert read_dispatch_ledger(repo / "outputs" / TODAY) == {}


def test_output_evidence_gate_still_retries_empty_l1_completion(tmp_path: Path):
    repo, missions = _repo(tmp_path)

    assert commit_dispatch(
        repo,
        missions[0],
        dispatched_at=NOW,
        completed_at=NOW + timedelta(seconds=10),
        artifacts=[],
        materialize_outputs=False,
    )
    assert _due(repo, missions, NOW + timedelta(minutes=1)) == ["data_update"]


def test_bare_gate_stub_remains_suppressed_on_commit(tmp_path: Path):
    repo, missions = _repo(tmp_path, _mission(output_queue="queues/gates"))
    artifact = repo / "outputs" / TODAY / "1" / "summary.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("completed\n", encoding="utf-8")

    commit_dispatch(
        repo,
        missions[0],
        dispatched_at=NOW,
        completed_at=NOW + timedelta(seconds=5),
        artifacts=[artifact],
    )

    assert not (repo / "queues" / "gates").exists()
    raw = json.loads((repo / "outputs" / TODAY / "dispatch.json").read_text())
    assert raw["data_update"]["completed_at"].endswith("+00:00")


def _event_mission() -> dict:
    return {
        "id": "execution",
        "layer": 3,
        "cadence": "event",
        "input_queue": "inbox/decisions",
        "output_queue": "queues/trades/",
        "creates": [],
    }


def _write_decision(repo: Path, decision_id: str, when: datetime) -> Path:
    path = repo / "inbox" / "decisions" / f"{decision_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "id": decision_id,
                "kind": "trade",
                "status": "approved",
                "created_at": when.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    return path


def _archive_processed(decision: Path, when: datetime) -> None:
    data = yaml.safe_load(decision.read_text(encoding="utf-8"))
    data["executed_at"] = when.isoformat()
    decision.write_text(yaml.safe_dump(data), encoding="utf-8")
    processed = decision.parent / ".processed" / decision.name
    processed.parent.mkdir(parents=True, exist_ok=True)
    decision.replace(processed)


def test_event_commit_records_only_trigger_dispatched_this_round(tmp_path: Path):
    """Two pending trades: processing A must not silently starve trade B."""
    when = datetime(2026, 9, 4, 14, 30, tzinfo=timezone.utc)  # 16:30 Paris
    mission = _event_mission()
    repo, missions = _repo(tmp_path, mission)
    trade_a = _write_decision(repo, "trade-A", when)
    _write_decision(repo, "trade-B", when)

    ctx = build_dispatch_ctx(repo, now_utc=when)
    assert [m["id"] for m in select_due_missions(ctx, missions)] == ["execution"]

    # DECIDE hands exactly one stable trigger identity to this worker.  The
    # worker processes and archives only that item while trade-B stays queued.
    dispatched_ids = event_trigger_ids_for_dispatch(ctx, mission)
    assert dispatched_ids == ["trade-A"]
    _archive_processed(trade_a, when + timedelta(seconds=10))
    assert commit_dispatch(
        repo,
        mission,
        dispatched_at=when,
        completed_at=when + timedelta(seconds=20),
        dispatched_trigger_ids=dispatched_ids,
        materialize_outputs=False,
    )

    ledger = read_dispatch_ledger(repo / "outputs" / TODAY)
    trigger_evidence = {
        item for item in ledger["execution"]["artifacts"]
        if item.startswith("trigger:")
    }
    assert trigger_evidence == {"trigger:trade-A"}

    # The exact starvation regression: trade-B was never claimed by A's
    # commit, so the event mission remains due on the next tick.
    next_tick = when + timedelta(minutes=5)
    assert _due(repo, missions, next_tick) == ["execution"]


def test_l4_sees_processed_l3_item_when_parent_crashes_before_commit(tmp_path: Path):
    """An archived trade is L3-fired evidence even without dispatch.json."""
    when = datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc)  # 22:00 Paris
    mission = _event_mission()
    repo, _ = _repo(tmp_path, mission)
    write_last_run(repo / "outputs" / TODAY / "1", when=when)
    trade_a = _write_decision(repo, "trade-A", when)
    _write_decision(repo, "trade-B", when)

    before = build_dispatch_ctx(repo, now_utc=when)
    assert decide_dispatch(before) == "layer_3"

    # The worker executed+archived A, then the parent died before COMMIT.
    _archive_processed(trade_a, when + timedelta(seconds=10))
    assert read_dispatch_ledger(repo / "outputs" / TODAY) == {}

    after = build_dispatch_ctx(repo, now_utc=when + timedelta(minutes=1))
    assert after["has_inbox_decisions"] is True  # trade-B is still real work
    assert after["layer_3_mission_fired_today"] is True
    assert decide_dispatch(after) == "layer_4"
