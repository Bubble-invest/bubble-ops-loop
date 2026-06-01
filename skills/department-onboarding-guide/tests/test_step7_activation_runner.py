"""
test_step7_activation_runner.py — Refonte #1 of 3, Deliverable F.

Pins the conversational behavior of `ActivationRunner` (Step 7 per
Notion v5 lines 947-1003). The runner must:

  - Build the activation PR body via build_activation_pr_body() and
    verify it via test_activation_pr_body() before showing anything to
    the operator (Refonte invariant: the legacy English body must
    NEVER reach Joris).
  - Surface the humanized body on Telegram + ask for approval.
  - On `approuve`: invoke `scripts/activate-dept.sh` and flip the dept
    status to `live`.
  - On `édite` / `raffine`: record the request and stay not-done.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.activation import ActivationRunner


# ----- helpers -----


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "joris",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Ready to activate",
        "validated_steps": [
            "mandate", "missions", "layers",
            "skills_tools", "gates_kpis", "dry_run",
        ],
        "last_updated_at": "2026-05-21T09:00:00Z",
        "commits": [
            {"step": "dry_run", "commit_sha": "abcdef0",
             "validated_at": "2026-05-21T08:50:00Z"},
        ],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "level": "ops",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "owner": "joris",
            "status": "onboarding",
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
        "recurring_missions": [],
        "skills": {"layer_1": ["content-signal-scanner"]},
        "tools": [],
        "gate_policies": {},
    }, sort_keys=False), encoding="utf-8")
    # Also seed the promoted dept.yaml (some checks read it).
    promoted = tmp_path / "dept.yaml"
    promoted.write_text(draft.read_text(encoding="utf-8"), encoding="utf-8")
    return draft


# ----- tests -----


def test_prompt_surfaces_humanized_pr_body(tmp_path):
    """The first prompt must include the humanized French h1 + sections."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)
    p = runner.next_prompt()
    assert p is not None
    # Humanized h1 (any of the 3 variants)
    assert "Lettre d'arrivée" in p or "Cérémonie d'arrivée" in p or "Bienvenue" in p
    # 6 humanized sections per Refonte/D contract
    for section in [
        "## Sa mission", "## Ce qu'elle fera chaque jour",
        "## Ses 4 moments de la journée", "## Les décisions qu'elle prend",
        "## Sa répétition à blanc",
        "## Ce qu'il faut vérifier avant la cérémonie",
    ]:
        assert section in p
    # An explicit approval ask
    assert "approuv" in p.lower() or "envoy" in p.lower()


def test_legacy_english_body_blocks_runner(tmp_path):
    """If the PR body fails verification, runner shows an error prompt."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    with patch(
        "skill_lib.step_runners.activation._build_pr_body",
        return_value="# Activate Miranda department\n\nLegacy body.",
    ):
        runner.start(state, draft)
    p = runner.next_prompt()
    assert p is not None
    # The error prompt names the problem
    assert "à corriger" in p.lower() or "manquant" in p.lower() or "humanis" in p.lower()
    # Runner refuses to activate
    assert runner.is_done() is False


def test_approve_runs_activation_script_and_flips_status(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)
    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=0,
    ) as mock_activate:
        action = runner.on_answer("approuve")
    assert action == Action.DONE
    mock_activate.assert_called_once()
    assert runner.is_done() is True
    # dept.yaml status must have been flipped (the runner reads back
    # from disk; we simulate the script's side-effect ourselves here
    # because we patched it out).
    promoted = tmp_path / "dept.yaml"
    if promoted.exists():
        doc = yaml.safe_load(promoted.read_text(encoding="utf-8"))
        # The runner's own _flip handler sets it
        assert doc["department"]["status"] == "live"


def test_edit_records_request_does_not_activate(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)
    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=0,
    ) as mock_activate:
        action = runner.on_answer("édite: le 1er paragraphe est trop long")
    assert action == Action.EDIT
    mock_activate.assert_not_called()
    assert runner.is_done() is False


def test_refine_records_request_does_not_activate(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)
    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=0,
    ) as mock_activate:
        action = runner.on_answer("raffine la checklist")
    assert action == Action.REFINE
    mock_activate.assert_not_called()
    assert runner.is_done() is False


def test_approve_fails_when_script_returns_nonzero(tmp_path):
    """If activate-dept.sh fails, runner stays not-done."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("activation")
    runner.start(state, draft)
    with patch(
        "skill_lib.step_runners.activation._run_activation_script",
        return_value=2,  # can_activate gate refused
    ):
        action = runner.on_answer("approuve")
    assert action != Action.DONE
    assert runner.is_done() is False


def test_runner_is_activationrunner_class(tmp_path):
    runner = get_runner("activation")
    assert isinstance(runner, ActivationRunner)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
