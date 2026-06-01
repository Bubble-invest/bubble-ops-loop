"""
test_artifact_recurring_mission.py — Refonte #2 of 3, Deliverable B.

Pins the behavior of the per-mission semantic tester
(`test_recurring_mission`) used by the Step 2 runner to gate every
APPROVE_SUBSTEP before writing the mission to disk.

Notion v5 line 846 ("Test mission") — the agent must simulate one
mission tick, generate a fake queue item from `creates[]`, and verify
it conforms to queue-item.schema.yaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from skill_lib.artifact_tests import test_artifact
from skill_lib.artifact_tests.recurring_mission import (
    test_recurring_mission,
    simulate_queue_item_from_mission,
)


def _canonical_mission() -> dict:
    return {
        "id": "content_signal_scan",
        "layer": 1,
        "cadence": "daily",
        "time": "06:00",
        "description": "Scanner les signaux de contenu (wiki + LinkedIn + notes).",
        "output_queue": "queues/research/",
        "creates": ["content_idea_task"],
    }


def _ctx(tmp_path: Path) -> dict:
    return {
        "dept_root": tmp_path,
        "dept_yaml_draft_path": tmp_path / "dept.yaml.draft",
    }


def test_passes_on_canonical_fixture(tmp_path):
    r = test_recurring_mission(_canonical_mission(), _ctx(tmp_path))
    assert r.passed, r.summary_md
    # "validée" (FR) — passes both "valid" and "validée" checks.
    assert "valid" in r.summary_md.lower() or "validée" in r.summary_md.lower()


def test_dispatcher_routes_to_recurring_mission(tmp_path):
    r = test_artifact("recurring_mission", _canonical_mission(), _ctx(tmp_path))
    assert r.passed


def test_fails_on_bad_cadence_pattern(tmp_path):
    m = _canonical_mission()
    m["cadence"] = "everynight"
    r = test_recurring_mission(m, _ctx(tmp_path))
    assert r.passed is False
    assert any("cadence" in i.lower() for i in r.issues)


def test_fails_on_layer_out_of_range(tmp_path):
    m = _canonical_mission()
    m["layer"] = 5
    r = test_recurring_mission(m, _ctx(tmp_path))
    assert r.passed is False
    assert any("layer" in i.lower() for i in r.issues)


def test_fails_on_empty_creates(tmp_path):
    m = _canonical_mission()
    m["creates"] = []
    r = test_recurring_mission(m, _ctx(tmp_path))
    assert r.passed is False
    assert any("crée" in i.lower() or "creates" in i.lower() for i in r.issues)


def test_fails_on_duplicate_id(tmp_path):
    # Seed an existing mission file with the same id.
    missions_dir = tmp_path / "missions"
    missions_dir.mkdir(parents=True)
    other = _canonical_mission()
    (missions_dir / f"{other['id']}.yaml").write_text(
        yaml.safe_dump(other, sort_keys=False), encoding="utf-8")
    # Now try to register a DIFFERENT mission with the same id.
    new = _canonical_mission()
    new["description"] = "Another mission with the same id."
    r = test_recurring_mission(new, _ctx(tmp_path))
    assert r.passed is False
    assert any("doublon" in i.lower() or "duplicate" in i.lower() or "unique" in i.lower()
               for i in r.issues)


def test_simulated_queue_item_is_schema_valid(tmp_path):
    item = simulate_queue_item_from_mission(_canonical_mission())
    # Fields the schema requires.
    for f in ("id", "kind", "source_layer", "target_layer", "priority",
              "created_at", "payload"):
        assert f in item


def test_simulated_queue_item_validates_against_schema(tmp_path):
    import jsonschema
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    schema_path = project_root / "schemas-draft" / "queue-item.schema.yaml"
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    item = simulate_queue_item_from_mission(_canonical_mission())
    jsonschema.Draft7Validator(schema).validate(item)


def test_passes_on_weekly_with_day_and_time(tmp_path):
    m = {
        "id": "weekly_review",
        "layer": 1,
        "cadence": "weekly",
        "day": "monday",
        "time": "09:00",
        "description": "Récap hebdomadaire.",
        "output_queue": "queues/research/",
        "creates": ["perf_review_task"],
    }
    r = test_recurring_mission(m, _ctx(tmp_path))
    assert r.passed, r.summary_md


def test_summary_md_uses_bureau_de_cadre_french(tmp_path):
    r = test_recurring_mission(_canonical_mission(), _ctx(tmp_path))
    # FR + concrete: mention the mission id and an action verb.
    assert "content_signal_scan" in r.summary_md
    # Should not leak English boilerplate.
    assert "Validation passed" not in r.summary_md


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
