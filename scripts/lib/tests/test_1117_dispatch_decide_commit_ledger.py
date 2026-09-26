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


# ---------------------------------------------------------------------------
# Board #1542: intra-day cadences (every_Nh / every_Nm / hourly / cron:) must
# advance `completed_at` on a genuine 2nd+ same-day completion, while a true
# replay of the SAME dispatch stays idempotent. daily/weekly/legacy-ledger
# behavior must not change.
# ---------------------------------------------------------------------------

def _intraday_mission(cadence: str) -> dict:
    return {
        "id": "draft_substack_note",
        "layer": 2,
        "cadence": cadence,
        "output_queue": "queues/drafts/",
        "creates": [],
    }


def _seed_research_item(repo: Path) -> None:
    """Layer 2's eligibility gate (`has_research`) needs a non-empty
    `queues/research/` — content's real `draft_substack_note` runs alongside
    a steady research feed, so this mirrors that instead of poking ctx
    internals directly.
    """
    research = repo / "queues" / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "item.yaml").write_text(
        yaml.safe_dump({"id": "item", "kind": "research_item"}), encoding="utf-8"
    )


# Layer 2's time floor is 12:00 Paris (`_LAYER_MIN_TIME`), so intra-day-cadence
# tests use an afternoon base time instead of the module's `NOW` (08:05 Paris,
# used by the layer-1 `daily` fixtures above).
_L2_BASE = datetime(2026, 9, 4, 10, 5, tzinfo=timezone.utc)  # 12:05 Paris


def test_every_3h_second_completion_advances_ledger_and_is_not_reselected(tmp_path: Path):
    """THE BUG (#1542): an every_3h mission completed twice in one Paris day
    must advance `completed_at` to the 2nd completion — not keep re-selecting
    the mission on every subsequent tick because the ledger is stuck on the
    1st completion.
    """
    repo, missions = _repo(tmp_path, _intraday_mission("every_3h"))
    mission = missions[0]
    _seed_research_item(repo)

    first_dispatch = _L2_BASE
    first_completed_at = first_dispatch + timedelta(seconds=5)
    assert _due(repo, missions, first_dispatch) == ["draft_substack_note"]
    assert commit_dispatch(
        repo, mission,
        dispatched_at=first_dispatch,
        completed_at=first_completed_at,
    )
    # Not due again immediately after the 1st completion.
    assert _due(repo, missions, first_dispatch + timedelta(hours=1)) == []

    # 3h after the 1st COMPLETION (not the 1st dispatch): cadence math
    # (elapsed since the 1st completion) says due — this is the mission's
    # legitimate 2nd run of the day.
    second_dispatch = first_completed_at + timedelta(hours=3)
    assert _due(repo, missions, second_dispatch) == ["draft_substack_note"]
    second_completed_at = second_dispatch + timedelta(seconds=5)
    assert commit_dispatch(
        repo, mission,
        dispatched_at=second_dispatch,
        completed_at=second_completed_at,
    ), "a genuinely new same-day completion must be let through (return True)"

    ledger = read_dispatch_ledger(repo / "outputs" / TODAY)
    assert ledger["draft_substack_note"]["completed_at"] == second_completed_at.isoformat()

    # THE REGRESSION this closes: shortly after the 2nd completion, the old
    # code left `completed_at` at the 1st completion, so elapsed-time was
    # already >= 3h and the mission re-fired on every subsequent tick.
    assert _due(repo, missions, second_dispatch + timedelta(minutes=5)) == [], (
        "must not re-select right after the 2nd completion — the ledger has "
        "to measure elapsed time from the LATEST completion, not the first"
    )
    # Still not due just before the 3h mark from the 2nd completion.
    assert _due(
        repo, missions, second_completed_at + timedelta(hours=3) - timedelta(seconds=1)
    ) == []
    # Due again exactly 3h after the 2nd completion (not the 1st).
    assert _due(repo, missions, second_completed_at + timedelta(hours=3)) == [
        "draft_substack_note"
    ]


def test_every_3h_duplicate_commit_of_same_run_stays_noop(tmp_path: Path):
    """A retried/replayed commit of the SAME dispatch (identical
    `dispatched_at`) must remain idempotent — it must not double-count the
    round counter or otherwise advance state, even for an intra-day cadence.
    """
    repo, missions = _repo(tmp_path, _intraday_mission("every_3h"))
    mission = missions[0]

    dispatched_at = NOW
    completed_at = NOW + timedelta(seconds=5)
    assert commit_dispatch(
        repo, mission, dispatched_at=dispatched_at, completed_at=completed_at
    )
    round_counter_path = repo / "outputs" / TODAY / "round_counter.json"
    first_round_counter = json.loads(round_counter_path.read_text())

    # Same dispatch, replayed (e.g. a watchdog re-kick re-committing the same
    # worker result) — must return False and leave the ledger untouched.
    assert not commit_dispatch(
        repo, mission, dispatched_at=dispatched_at, completed_at=completed_at
    )
    ledger = read_dispatch_ledger(repo / "outputs" / TODAY)
    assert ledger["draft_substack_note"]["completed_at"] == completed_at.isoformat()
    assert json.loads(round_counter_path.read_text()) == first_round_counter


def test_every_30m_case_advances_across_two_completions(tmp_path: Path):
    """every_Nm behaves the same way as every_Nh (minute granularity)."""
    repo, missions = _repo(tmp_path, _intraday_mission("every_30m"))
    mission = missions[0]
    _seed_research_item(repo)

    first_dispatch = _L2_BASE
    first_completed_at = first_dispatch + timedelta(seconds=5)
    assert commit_dispatch(
        repo, mission,
        dispatched_at=first_dispatch,
        completed_at=first_completed_at,
    )
    assert _due(repo, missions, first_dispatch + timedelta(minutes=10)) == []

    second_dispatch = first_completed_at + timedelta(minutes=30)
    assert _due(repo, missions, second_dispatch) == ["draft_substack_note"]
    second_completed_at = second_dispatch + timedelta(seconds=5)
    assert commit_dispatch(
        repo, mission, dispatched_at=second_dispatch, completed_at=second_completed_at
    )

    ledger = read_dispatch_ledger(repo / "outputs" / TODAY)
    assert ledger["draft_substack_note"]["completed_at"] == second_completed_at.isoformat()
    assert _due(repo, missions, second_dispatch + timedelta(minutes=10)) == []
    assert _due(repo, missions, second_completed_at + timedelta(minutes=30)) == [
        "draft_substack_note"
    ]


def test_daily_cadence_second_same_day_commit_still_blocked(tmp_path: Path):
    """REGRESSION: daily cadence keeps its once-per-Paris-day idempotence —
    a 2nd same-day commit (even with a later `dispatched_at`, unlike a true
    replay) must still return False and must not advance `completed_at`.
    """
    repo, missions = _repo(tmp_path, _mission(cadence="daily"))
    mission = missions[0]

    # Layer 1's output-evidence gate requires a real artifact under
    # outputs/<today>/1/ before treating the mission as genuinely done
    # (crash-recovery retry, unrelated to #1542) — supply one so this test
    # exercises the cadence idempotence guard, not that separate gate.
    artifact = repo / "outputs" / TODAY / "1" / "summary.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("completed\n", encoding="utf-8")

    first_dispatch = NOW
    first_completed_at = first_dispatch + timedelta(seconds=5)
    assert commit_dispatch(
        repo, mission,
        dispatched_at=first_dispatch,
        completed_at=first_completed_at,
        artifacts=[artifact],
    )

    later_same_day = first_dispatch + timedelta(hours=2)
    assert not commit_dispatch(
        repo, mission,
        dispatched_at=later_same_day,
        completed_at=later_same_day + timedelta(seconds=5),
    ), "daily cadence must stay idempotent for a 2nd same-day completion"

    ledger = read_dispatch_ledger(repo / "outputs" / TODAY)
    assert ledger["data_update"]["completed_at"] == first_completed_at.isoformat()


def test_weekly_cadence_second_same_day_commit_still_blocked(tmp_path: Path):
    """REGRESSION: weekly cadence is unaffected by #1542 — still once per
    Paris-local listed day, even with a later `dispatched_at`.
    """
    friday_0800 = datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)  # 08:00 Paris, Friday
    mission = {
        "id": "weekly_digest",
        "layer": 2,
        "cadence": "weekly",
        "time": "07:00",
        "day": "friday",
        "output_queue": "queues/drafts/",
        "creates": [],
    }
    repo, missions = _repo(tmp_path, mission)

    assert commit_dispatch(
        repo, mission,
        dispatched_at=friday_0800,
        completed_at=friday_0800 + timedelta(seconds=5),
    )
    later_same_day = friday_0800 + timedelta(hours=4)
    assert not commit_dispatch(
        repo, mission,
        dispatched_at=later_same_day,
        completed_at=later_same_day + timedelta(seconds=5),
    ), "weekly cadence must stay idempotent for a 2nd same-day completion"


def test_legacy_ledger_entry_without_dispatched_at_still_loads_and_is_conservative(
    tmp_path: Path,
):
    """Old/hand-written dispatch.json entries are not guaranteed to carry a
    parseable `dispatched_at` (e.g. a pre-#1542 or malformed entry). Reading
    it must never crash, and — since there's no reliable evidence this is a
    NEW dispatch — the intra-day advance path must fail closed (stay
    idempotent), exactly like the pre-#1542 behavior.
    """
    repo, missions = _repo(tmp_path, _intraday_mission("every_3h"))
    mission = missions[0]
    today_dir = repo / "outputs" / TODAY
    today_dir.mkdir(parents=True)
    (today_dir / "dispatch.json").write_text(
        json.dumps({
            "draft_substack_note": {
                "materialized_at": NOW.isoformat(),
                "completed_at": NOW.isoformat(),
                "artifacts": [],
                # `dispatched_at` deliberately absent — legacy/malformed shape.
            }
        }),
        encoding="utf-8",
    )

    # Old-format ledger loads without raising.
    ledger = read_dispatch_ledger(today_dir)
    assert ledger["draft_substack_note"]["completed_at"] == NOW.isoformat()

    later = NOW + timedelta(hours=3)
    assert not commit_dispatch(
        repo, mission, dispatched_at=later, completed_at=later + timedelta(seconds=5)
    ), "missing dispatched_at evidence must fail closed to idempotent, not crash"
    ledger_after = read_dispatch_ledger(today_dir)
    assert ledger_after["draft_substack_note"]["completed_at"] == NOW.isoformat()
