"""Composition/regression test for board #1059 — dispatch
materialize_due_missions writing false markers every tick (L2 mis-pin +
phantom .last-run).

The marker half of #1059 is canonicalized in #870 (merged): the materializer
now writes ONLY `.last-materialized` (its own anti-fire-spin proxy), never
`.last-run` — the honest completion marker reserved for a mission's real
executor. The phantom-queue half (a drainable-kind `situation_brief` item
materialized into queues/research falsely PINNING L2) is closed by the #175
`input_kinds` allow-list: a kind no downstream layer consumes is never
materialized into the input queue in the first place.

This module LOCKS both, so this cluster's other changes (mission_scaffold /
mission_doctor / the #1022 marker-contract work) cannot silently regress the
#346/#344 (`.last-materialized` split + output-truth) guarantees they compose
with.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    materialize_due_missions,
    materialize_due_missions_for_tick,
    read_last_materialized,
    read_last_run,
)

# Wed 2026-09-02, 12:05 Paris (CEST) = 10:05 UTC — past L2's 12:00 floor.
_NOW = datetime(2026, 9, 2, 10, 5, 0, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")


def _make_repo(tmp_path: Path, missions: list) -> Path:
    repo = tmp_path / "ben"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )
    return repo


# --- marker half: never a false .last-run (composes with #870) --------------

def test_materialize_stamps_last_materialized_not_last_run(tmp_path: Path):
    """A due, shim-resolved L2 mission that produces a real queue item gets
    the `.last-materialized` proxy stamp — NEVER `.last-run` (which would
    falsely claim the mission's work ran, the #1059/#870 symptom)."""
    mission = {
        "id": "research_scan", "layer": 2, "cadence": "daily", "time": "08:00",
        "output_queue": "queues/research/", "creates": ["research_item"],
    }
    repo = _make_repo(tmp_path, [mission])
    today_dir = repo / "outputs" / _TODAY

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    mdir = today_dir / "missions" / "research_scan"
    assert read_last_run(mdir) is None, (
        "#1059/#870: materialize must NEVER write .last-run for a mission it "
        "only queued (did not execute)"
    )
    assert read_last_materialized(mdir) is not None, (
        "the anti-fire-spin proxy stamp must still land on .last-materialized"
    )
    # And a real queue item was created (the mission is genuinely due).
    assert created and created[0]["kind"] == "research_item"


# --- phantom-queue half: a non-consumed L1 output never pins L2 (#175) -------

def test_l1_self_output_kind_not_materialized_into_research_queue(tmp_path: Path):
    """#1059 symptom (2): an L1 mission that also declares `situation_brief`
    (its own morning-brief artifact) in creates[] must NOT get a
    `situation_brief-*.yaml` phantom materialized into queues/research, where
    it would falsely PIN L2 as due. The #175 input_kinds allow-list — active
    as soon as ANY mission declares input_kinds — suppresses a kind no
    downstream layer consumes."""
    data_update = {
        "id": "data_update", "layer": 1, "cadence": "daily", "time": "07:00",
        "output_queue": "queues/research/",
        "creates": ["situation_brief", "research_item"],
    }
    # An L2 consumer declares it eats research_item (and NOT situation_brief),
    # which turns on the #175 gate for the whole dept.
    research = {
        "id": "research_scan", "layer": 2, "cadence": "daily", "time": "08:00",
        "output_queue": "queues/gates/", "creates": ["investment_case"],
        "input_kinds": ["research_item"],
    }
    repo = _make_repo(tmp_path, [data_update, research])

    due = materialize_due_missions(
        [data_update, research], now=_NOW, last_fired_per_mission={}
    )
    kinds = {d["kind"] for d in due}
    assert "research_item" in kinds, "the genuinely-consumed L1 output must materialize"
    assert "situation_brief" not in kinds, (
        "#1059: situation_brief is an L1 self-output no layer consumes — it "
        "must not be materialized into queues/research to pin L2"
    )

    # End-to-end through the tick: no situation_brief-*.yaml file on disk.
    materialize_due_missions_for_tick(repo, repo / "outputs" / _TODAY, _NOW)
    research_q = repo / "queues" / "research"
    situation_files = [
        p.name for p in research_q.glob("situation_brief-*.yaml")
    ] if research_q.is_dir() else []
    assert situation_files == [], (
        f"no phantom situation_brief queue item may be written, got {situation_files}"
    )
