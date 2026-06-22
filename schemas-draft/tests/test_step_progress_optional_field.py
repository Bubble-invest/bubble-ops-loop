"""
test_step_progress_optional_field.py — schema test for the new
`step_progress` optional field on STATE.yaml.

Refonte #1 of 3, Deliverable A — drives the conversational sub-iteration
tracking that step_runners/ rely on. The field must be:
  - OPTIONAL (existing STATE.yaml fixtures keep validating)
  - structurally CONSTRAINED (we want to catch typos at write time)

Test matrix:
  1. Existing positive fixture (state-mid-onboarding.yaml) keeps validating.
  2. A STATE.yaml WITH step_progress (well-formed) validates.
  3. A STATE.yaml WITH step_progress containing an unknown current_status
     fails.
  4. A STATE.yaml WITH step_progress whose sub_artifacts_validated isn't a
     list fails.
  5. A STATE.yaml WITH step_progress containing extra unknown top-level
     properties (typo guard) fails.
  6. A STATE.yaml WITH step_progress.current_substep that is neither
     null nor a well-formed object fails.

Notion north star: lines 803-829 (Step 1 Mandate) — the 6 clarifications
need per-substep tracking, which is exactly what step_progress encodes.
"""
from __future__ import annotations

from pathlib import Path

import jsonschema
import pytest
import yaml

# Paths.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # schemas-draft/
EXAMPLES_DIR = ROOT / "examples"


def _load_state_schema() -> dict:
    return yaml.safe_load(
        (ROOT / "state.schema.yaml").read_text(encoding="utf-8")
    )


def _base_state() -> dict:
    """A minimally-valid STATE.yaml doc (all required fields)."""
    return {
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-20T19:30:00Z",
        "status": "Drafting",
        "validated_steps": ["mandate"],
        "last_updated_at": "2026-05-20T19:45:00Z",
        "commits": [
            {
                "step": "mandate",
                "commit_sha": "abc1234",
                "validated_at": "2026-05-20T19:33:00Z",
            },
        ],
    }


def _validator() -> jsonschema.Draft7Validator:
    schema = _load_state_schema()
    return jsonschema.Draft7Validator(schema)


# ---------- POSITIVE 1: existing fixture still validates ----------

def test_existing_mid_onboarding_fixture_still_validates():
    """Back-compat: pre-existing STATE.yaml without step_progress is valid."""
    fixture = yaml.safe_load(
        (EXAMPLES_DIR / "state-mid-onboarding.yaml").read_text(encoding="utf-8")
    )
    errors = list(_validator().iter_errors(fixture))
    assert not errors, [e.message for e in errors]


# ---------- POSITIVE 2: well-formed step_progress validates ----------

def test_state_with_well_formed_step_progress_validates():
    """A STATE.yaml carrying a complete step_progress block validates."""
    doc = _base_state()
    doc["step_progress"] = {
        "missions": {
            "sub_artifacts_validated": [
                {
                    "id": "echo_heartbeat",
                    "type": "recurring_mission",
                    "validated_at": "2026-05-20T20:00:00Z",
                },
            ],
            "current_substep": {
                "type": "recurring_mission",
                "draft_payload": {
                    "id": "scan_signals",
                    "cadence": "daily",
                },
            },
            "current_status": "drafting",
        },
    }
    errors = list(_validator().iter_errors(doc))
    assert not errors, [e.message for e in errors]


def test_state_with_step_progress_null_current_substep_validates():
    """current_substep MAY be null (drafting hasn't started yet)."""
    doc = _base_state()
    doc["step_progress"] = {
        "mandate": {
            "sub_artifacts_validated": [],
            "current_substep": None,
            "current_status": "drafting",
        },
    }
    errors = list(_validator().iter_errors(doc))
    assert not errors, [e.message for e in errors]


# ---------- NEGATIVE 1: unknown current_status ----------

def test_state_with_unknown_current_status_fails():
    doc = _base_state()
    doc["step_progress"] = {
        "mandate": {
            "sub_artifacts_validated": [],
            "current_substep": None,
            "current_status": "in_progress",  # not in enum
        },
    }
    errors = list(_validator().iter_errors(doc))
    assert errors, "expected validation error on unknown current_status"
    msgs = " ".join(e.message for e in errors)
    assert "in_progress" in msgs or "enum" in msgs.lower()


# ---------- NEGATIVE 2: sub_artifacts_validated not a list ----------

def test_state_with_bad_sub_artifacts_shape_fails():
    doc = _base_state()
    doc["step_progress"] = {
        "missions": {
            "sub_artifacts_validated": "should be a list",  # type error
            "current_substep": None,
            "current_status": "drafting",
        },
    }
    errors = list(_validator().iter_errors(doc))
    assert errors, "expected validation error on bad sub_artifacts_validated type"


# ---------- NEGATIVE 3: typo guard on sub-step object ----------

def test_state_with_extra_property_in_step_progress_substep_fails():
    """additionalProperties: false on the sub-step shape catches typos."""
    doc = _base_state()
    doc["step_progress"] = {
        "mandate": {
            "sub_artifacts_validated": [],
            "current_substep": None,
            "current_status": "drafting",
            "typo_field": "boom",  # not allowed
        },
    }
    errors = list(_validator().iter_errors(doc))
    assert errors, "expected validation error on extra property in step_progress[step]"


# ---------- NEGATIVE 4: current_substep wrong shape ----------

def test_state_with_bad_current_substep_shape_fails():
    """current_substep must be null OR an object with type + draft_payload."""
    doc = _base_state()
    doc["step_progress"] = {
        "missions": {
            "sub_artifacts_validated": [],
            "current_substep": "not-an-object",  # neither null nor object
            "current_status": "drafting",
        },
    }
    errors = list(_validator().iter_errors(doc))
    assert errors, "expected validation error on bad current_substep type"


# ---------- NEGATIVE 5: sub_artifact entry missing required keys ----------

def test_state_with_sub_artifact_missing_keys_fails():
    doc = _base_state()
    doc["step_progress"] = {
        "missions": {
            "sub_artifacts_validated": [
                {"id": "echo_heartbeat"},  # missing type + validated_at
            ],
            "current_substep": None,
            "current_status": "validated",
        },
    }
    errors = list(_validator().iter_errors(doc))
    assert errors, "expected error on sub_artifact missing required fields"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
