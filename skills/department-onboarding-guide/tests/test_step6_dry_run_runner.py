"""
test_step6_dry_run_runner.py — Refonte #1 of 3, Deliverable E.

Pins the conversational behavior of `DryRunRunner` (the per-step
runner for Notion v5 Step 6, Tests / dry-run, lines 925-946).

The runner:
  - on start() runs the full dry-run simulator and humanizes the raw
    report (via humanize_dry_run_report from Deliverable D)
  - emits a FR Bureau-de-Cadre Telegram prompt
  - parses the operator's free text (valide / modifie / raffine / fixed)
  - obeys Notion line 946 — never advances past FAILED; advances past
    WARNING only if the operator explicitly accepts.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.dry_run import DryRunRunner


# ----- helpers -----


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "joris",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Dry run",
        "validated_steps": [
            "mandate", "missions", "layers", "skills_tools", "gates_kpis",
        ],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "level": "ops",
            "mandate": "Produire, planifier et auditer du contenu.",
            "owner": "joris",
        },
    }, sort_keys=False), encoding="utf-8")
    return draft


def _green_dry_run_dict() -> dict:
    return {
        "overall_status": "PASSED",
        "can_advance_to_ready": True,
        "checks": [
            {"step": "layer_1_output_schema", "scope": "/x/1",
             "status": "passed", "message": "ok"},
            {"step": "layer_2_gate_item_schema", "scope": "/x/2",
             "status": "passed", "message": "ok"},
            {"step": "layer_3_execution_valid", "scope": "/x/3",
             "status": "passed", "message": "ok"},
            {"step": "layer_4_three_outputs", "scope": "/x/4",
             "status": "passed", "message": "ok"},
        ],
    }


def _warning_dry_run_dict() -> dict:
    raw = _green_dry_run_dict()
    raw["overall_status"] = "WARNING"
    raw["can_advance_to_ready"] = False
    raw["checks"].append({
        "step": "layer_4_brand_safety_fixture",
        "scope": "/x/tests/brand_safety.yaml",
        "status": "warning",
        "message": "Missing brand safety test fixture",
    })
    return raw


def _failed_dry_run_dict() -> dict:
    raw = _green_dry_run_dict()
    raw["overall_status"] = "FAILED"
    raw["can_advance_to_ready"] = False
    raw["checks"][2]["status"] = "failed"
    raw["checks"][2]["message"] = "exec crashed"
    return raw


class _FakeResult:
    """Pseudo DryRunResult with the .to_dict() the runner calls."""

    def __init__(self, payload: dict):
        self._payload = payload

    def to_dict(self) -> dict:
        return self._payload

    @property
    def can_advance_to_ready(self) -> bool:
        return bool(self._payload.get("can_advance_to_ready", False))

    @property
    def overall_status(self) -> str:
        return self._payload.get("overall_status", "UNKNOWN")


# ----- tests -----


def test_green_report_makes_runner_done_immediately(tmp_path):
    """A PASSED + can_advance dry-run → operator just needs to confirm."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("dry_run")
    fake = _FakeResult(_green_dry_run_dict())
    with patch("skill_lib.step_runners.dry_run._invoke_dry_run",
               return_value=fake):
        runner.start(state, draft)
    p = runner.next_prompt()
    assert p is not None
    assert "répétition" in p.lower()
    # All-green: passed but waits for operator OK to flip to done.
    assert runner.is_done() is False
    action = runner.on_answer("valide")
    assert action == Action.DONE
    assert runner.is_done() is True


def test_warning_report_blocks_until_operator_accepts(tmp_path):
    """WARNING → operator must say `valide` or `accepte` to advance."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("dry_run")
    fake = _FakeResult(_warning_dry_run_dict())
    with patch("skill_lib.step_runners.dry_run._invoke_dry_run",
               return_value=fake):
        runner.start(state, draft)
    p = runner.next_prompt()
    assert p is not None
    # The warning must be surfaced
    assert "⚠" in p or "warning" in p.lower() or "avertissement" in p.lower()
    assert "brand safety" in p.lower() or "brand_safety" in p.lower()
    assert runner.is_done() is False
    # An ambiguous answer keeps us waiting
    action = runner.on_answer("euh, je sais pas")
    assert action == Action.CONTINUE
    assert runner.is_done() is False
    # Explicit acceptance → done
    action = runner.on_answer("valide")
    assert action == Action.DONE
    assert runner.is_done() is True


def test_failed_report_never_advances(tmp_path):
    """FAILED → operator cannot validate; runner stays not-done."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("dry_run")
    fake = _FakeResult(_failed_dry_run_dict())
    with patch("skill_lib.step_runners.dry_run._invoke_dry_run",
               return_value=fake):
        runner.start(state, draft)
    # Even "valide" cannot lift a FAILED.
    action = runner.on_answer("valide")
    assert action != Action.DONE
    assert runner.is_done() is False
    # Operator can request a refine on a specific layer
    action = runner.on_answer("raffine layer 3")
    assert action == Action.REFINE
    assert runner.is_done() is False


def test_failed_report_summary_mentions_failing_layer(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("dry_run")
    fake = _FakeResult(_failed_dry_run_dict())
    with patch("skill_lib.step_runners.dry_run._invoke_dry_run",
               return_value=fake):
        runner.start(state, draft)
    p = runner.next_prompt()
    assert p is not None
    # Layer 3 = L'exécution
    assert "exécution" in p.lower() or "execution" in p.lower() or "l'exécution" in p


def test_edit_intent_lets_operator_provide_fixture_inline(tmp_path):
    """Operator can answer 'modifie' to record an edit request."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("dry_run")
    fake = _FakeResult(_warning_dry_run_dict())
    with patch("skill_lib.step_runners.dry_run._invoke_dry_run",
               return_value=fake):
        runner.start(state, draft)
    action = runner.on_answer("modifie")
    assert action == Action.EDIT
    # Still not done — edit kicks the conversation back to the operator
    assert runner.is_done() is False


def test_runner_records_progress_in_state_yaml(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("dry_run")
    fake = _FakeResult(_green_dry_run_dict())
    with patch("skill_lib.step_runners.dry_run._invoke_dry_run",
               return_value=fake):
        runner.start(state, draft)
    doc = yaml.safe_load(state.read_text(encoding="utf-8"))
    assert "step_progress" in doc
    assert "dry_run" in doc["step_progress"]
    progress = doc["step_progress"]["dry_run"]
    assert progress["current_status"] in ("awaiting_validation", "validated")


def test_runner_is_dryrunrunner_class(tmp_path):
    runner = get_runner("dry_run")
    assert isinstance(runner, DryRunRunner)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
