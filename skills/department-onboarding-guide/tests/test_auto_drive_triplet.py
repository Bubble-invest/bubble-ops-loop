"""
Sprint H+I Fix 3 — Notion `[Approve / Edit / Ask agent to refine]` triplet.

Notion v5 lines 826-828 mandate that every step expose 3 operator actions:
    [Approve mandate] [Edit] [Ask agent to refine]

These tests anchor the triplet API in auto_drive:
  - get_followup_prompt(step, choice)     — 2nd-turn FR prompt that
    surfaces the triplet to the operator.
  - record_approval(state, step)
  - record_edit_request(state, step, operator_text)
  - record_refine_request(state, step, reason)
All 3 record_* helpers append to STATE.yaml::step_interactions[].
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


def _write_state(path: Path, *, status: str = "Idea", validated: list = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": 1,
        "slug": "smoke",
        "display_name": "Smoke",
        "owner": "joris",
        "created_at": "2026-05-21T00:00:00Z",
        "status": status,
        "validated_steps": validated or [],
        "last_updated_at": "2026-05-21T00:00:00Z",
        "commits": [],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# get_followup_prompt
# ---------------------------------------------------------------------------

def test_followup_prompt_surfaces_triplet_for_mandate_option_1() -> None:
    from skill_lib.auto_drive import get_followup_prompt
    prompt = get_followup_prompt("mandate", "1")
    low = prompt.lower()
    # The triplet must appear textually so the operator can pick.
    assert "approuves" in low or "approve" in low, \
        f"followup must surface 'approve' action; got: {prompt}"
    assert "édite" in low or "editer" in low or "edit" in low, \
        f"followup must surface 'edit' action; got: {prompt}"
    assert "refine" in low or "raffine" in low, \
        f"followup must surface 'refine' action; got: {prompt}"


def test_followup_prompt_returns_french_bureau_de_cadre_voice() -> None:
    from skill_lib.auto_drive import get_followup_prompt
    prompt = get_followup_prompt("mandate", "2")
    # Must use tu/toi (French BdC voice), not "vous".
    assert "tu " in prompt.lower() or " tu" in prompt.lower(), \
        f"followup must tutoyer Joris; got: {prompt}"


def test_followup_prompt_falls_back_for_unknown_choice() -> None:
    """A choice the agent didn't predict (e.g. '4') still produces a
    sensible followup that surfaces the triplet."""
    from skill_lib.auto_drive import get_followup_prompt
    prompt = get_followup_prompt("mandate", "9")
    low = prompt.lower()
    assert "approuve" in low or "approve" in low
    assert "édite" in low or "edit" in low
    assert "refine" in low or "raffine" in low


def test_followup_prompt_rejects_unknown_step() -> None:
    from skill_lib.auto_drive import get_followup_prompt
    with pytest.raises(ValueError):
        get_followup_prompt("not_a_step", "1")


# ---------------------------------------------------------------------------
# record_approval / record_edit_request / record_refine_request
# ---------------------------------------------------------------------------

def test_record_approval_appends_step_interaction(tmp_path: Path) -> None:
    from skill_lib.auto_drive import record_approval
    state = _write_state(tmp_path / "STATE.yaml")
    record_approval(state, "mandate")

    doc = _load(state)
    interactions = doc.get("step_interactions", [])
    assert len(interactions) == 1
    entry = interactions[0]
    assert entry["step"] == "mandate"
    assert entry["action"] == "approve"
    assert "ts" in entry
    # last_updated_at must be bumped.
    assert doc["last_updated_at"] == entry["ts"]


def test_record_edit_request_persists_operator_text(tmp_path: Path) -> None:
    from skill_lib.auto_drive import record_edit_request
    state = _write_state(tmp_path / "STATE.yaml")
    edit = "Je préfère un mandat plus resserré, juste sur le content social"
    record_edit_request(state, "mandate", edit)

    interactions = _load(state)["step_interactions"]
    assert len(interactions) == 1
    assert interactions[0]["action"] == "edit"
    assert interactions[0]["operator_text"] == edit


def test_record_refine_request_persists_reason(tmp_path: Path) -> None:
    from skill_lib.auto_drive import record_refine_request
    state = _write_state(tmp_path / "STATE.yaml")
    reason = "trop long, simplifie en une phrase"
    record_refine_request(state, "mandate", reason)

    interactions = _load(state)["step_interactions"]
    assert len(interactions) == 1
    assert interactions[0]["action"] == "refine"
    assert interactions[0]["operator_text"] == reason


def test_record_actions_append_in_order(tmp_path: Path) -> None:
    """Multiple actions on the same step accumulate as separate entries
    in order (audit trail, not last-write-wins)."""
    from skill_lib.auto_drive import (
        record_refine_request,
        record_edit_request,
        record_approval,
    )
    state = _write_state(tmp_path / "STATE.yaml")
    record_refine_request(state, "mandate", "trop large")
    record_edit_request(state, "mandate", "ma version réécrite")
    record_approval(state, "mandate")

    interactions = _load(state)["step_interactions"]
    actions = [e["action"] for e in interactions]
    assert actions == ["refine", "edit", "approve"]


def test_record_edit_request_rejects_empty_text(tmp_path: Path) -> None:
    from skill_lib.auto_drive import record_edit_request
    state = _write_state(tmp_path / "STATE.yaml")
    with pytest.raises(ValueError):
        record_edit_request(state, "mandate", "")


def test_record_refine_request_rejects_empty_reason(tmp_path: Path) -> None:
    from skill_lib.auto_drive import record_refine_request
    state = _write_state(tmp_path / "STATE.yaml")
    with pytest.raises(ValueError):
        record_refine_request(state, "mandate", "")


def test_record_action_rejects_unknown_step(tmp_path: Path) -> None:
    from skill_lib.auto_drive import record_approval
    state = _write_state(tmp_path / "STATE.yaml")
    with pytest.raises(ValueError):
        record_approval(state, "not_a_step")


# ---------------------------------------------------------------------------
# schema acceptance of step_interactions
# ---------------------------------------------------------------------------

def test_state_schema_accepts_step_interactions(tmp_path: Path) -> None:
    """The state.schema.yaml must accept the new optional
    step_interactions field. Existing fixtures (no step_interactions)
    must still validate (additive change)."""
    import json
    import jsonschema

    schema_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "schemas-draft" / "state.schema.yaml"
    )
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))

    base_state = {
        "schema_version": 1,
        "slug": "smoke",
        "display_name": "Smoke",
        "owner": "joris",
        "created_at": "2026-05-21T00:00:00Z",
        "status": "Configuring",
        "validated_steps": ["mandate"],
        "last_updated_at": "2026-05-21T00:00:00Z",
        "commits": [
            {
                "step": "mandate",
                "commit_sha": "abc1234",
                "validated_at": "2026-05-21T00:00:00Z",
            }
        ],
        "step_interactions": [
            {
                "step": "mandate",
                "action": "approve",
                "ts": "2026-05-21T00:00:00Z",
            },
            {
                "step": "mandate",
                "action": "edit",
                "ts": "2026-05-21T00:01:00Z",
                "operator_text": "version réécrite",
            },
        ],
    }
    # Must validate without raising.
    jsonschema.validate(base_state, schema)


def test_state_schema_still_accepts_state_without_step_interactions() -> None:
    """Additive change: existing STATE.yaml without step_interactions still valid."""
    import jsonschema

    schema_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "schemas-draft" / "state.schema.yaml"
    )
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))

    base_state = {
        "schema_version": 1,
        "slug": "smoke",
        "display_name": "Smoke",
        "owner": "joris",
        "created_at": "2026-05-21T00:00:00Z",
        "status": "Idea",
        "validated_steps": [],
        "last_updated_at": "2026-05-21T00:00:00Z",
        "commits": [],
    }
    jsonschema.validate(base_state, schema)


def test_state_schema_rejects_unknown_action() -> None:
    """An action outside {approve, edit, refine} must be rejected
    (poka-yoke against typos in agent code)."""
    import jsonschema

    schema_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "schemas-draft" / "state.schema.yaml"
    )
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))

    bad_state = {
        "schema_version": 1,
        "slug": "smoke",
        "display_name": "Smoke",
        "owner": "joris",
        "created_at": "2026-05-21T00:00:00Z",
        "status": "Idea",
        "validated_steps": [],
        "last_updated_at": "2026-05-21T00:00:00Z",
        "commits": [],
        "step_interactions": [
            {
                "step": "mandate",
                "action": "yolo",  # invalid
                "ts": "2026-05-21T00:00:00Z",
            }
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad_state, schema)


# ---------------------------------------------------------------------------
# SKILL.md documents the 3 actions
# ---------------------------------------------------------------------------

def test_skill_md_documents_triplet() -> None:
    skill_md = (
        Path(__file__).resolve().parent.parent / "SKILL.md"
    ).read_text(encoding="utf-8").lower()
    # Document each of the 3 actions.
    assert "approve" in skill_md, "SKILL.md must document the approve action"
    assert "edit" in skill_md, "SKILL.md must document the edit action"
    assert "refine" in skill_md, "SKILL.md must document the refine action"
