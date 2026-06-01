"""
test_layer_1_fire_after_rounds.py — schema test for the optional
`layer_1.fire_after_rounds` top-level field on dept.yaml.

Context (Joris msg 3129, 2026-05-24): STEP C.0 (Layer-1 dispatch) has an
idle gate threshold N, configurable per dept. We add a new optional
top-level `layer_1:` block whose only field today is `fire_after_rounds:
<int>` (default 1).

Contract:
  - The field is OPTIONAL — back-compat: pre-existing dept.yaml files
    that don't declare `layer_1` continue to validate.
  - When declared, `fire_after_rounds` must be a positive integer.
  - Bogus values (negative, float, string) fail validation.
  - The block has additionalProperties: false (no surprises).

Dependencies: jsonschema, pyyaml.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

try:
    from jsonschema import Draft7Validator
except ImportError:
    pytest.skip("jsonschema not installed", allow_module_level=True)

# ---------------------------------------------------------------------------
# Path surgery
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SCHEMAS_DRAFT = HERE.parent
PROJECT_ROOT = SCHEMAS_DRAFT.parent
SCRIPTS_LIB = PROJECT_ROOT / "scripts" / "lib"
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
for p in (str(SKILL_ROOT), str(SCRIPTS_LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

import scaffold  # noqa: E402

_SCHEMA_PATH = SCHEMAS_DRAFT / "dept.schema.yaml"


def _load_schema() -> dict:
    return yaml.safe_load(_SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dept_schema():
    spec = _load_schema()
    Draft7Validator.check_schema(spec)
    return spec


@pytest.fixture()
def baseline_ops_dept() -> dict:
    """Render a fresh ops dept.yaml.draft (already schema-valid) as
    baseline. Tests mutate copies of this."""
    rendered = scaffold.render_dept_yaml_draft(
        slug="maya", display_name="Maya", owner="joris", level="ops"
    )
    return yaml.safe_load(rendered)


# ---------------------------------------------------------------------------
# Back-compat: omitting layer_1 is fine.
# ---------------------------------------------------------------------------

def test_dept_yaml_without_layer_1_field_validates(dept_schema, baseline_ops_dept):
    """A dept.yaml that does NOT declare a `layer_1:` block must still
    pass validation. This is the back-compat contract — no existing
    dept.yaml should break when we add the optional field."""
    instance = copy.deepcopy(baseline_ops_dept)
    instance.pop("layer_1", None)
    errors = sorted(Draft7Validator(dept_schema).iter_errors(instance),
                    key=lambda e: list(e.path))
    assert not errors, (
        "Adding the optional layer_1 field must not break existing dept.yaml. "
        "Got errors: " + "\n".join(f"[{'.'.join(str(p) for p in e.path)}]: {e.message}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Valid declarations
# ---------------------------------------------------------------------------

def test_dept_yaml_with_layer_1_fire_after_rounds_int_validates(dept_schema, baseline_ops_dept):
    instance = copy.deepcopy(baseline_ops_dept)
    instance["layer_1"] = {"fire_after_rounds": 1}
    errors = sorted(Draft7Validator(dept_schema).iter_errors(instance),
                    key=lambda e: list(e.path))
    assert not errors, "\n".join(f"[{'.'.join(str(p) for p in e.path)}]: {e.message}" for e in errors)


def test_dept_yaml_with_layer_1_fire_after_rounds_3_validates(dept_schema, baseline_ops_dept):
    """Joris-configurable threshold — N=3 is a perfectly valid choice."""
    instance = copy.deepcopy(baseline_ops_dept)
    instance["layer_1"] = {"fire_after_rounds": 3}
    errors = sorted(Draft7Validator(dept_schema).iter_errors(instance),
                    key=lambda e: list(e.path))
    assert not errors, "\n".join(f"[{'.'.join(str(p) for p in e.path)}]: {e.message}" for e in errors)


def test_dept_yaml_with_empty_layer_1_block_validates(dept_schema, baseline_ops_dept):
    """An empty `layer_1: {}` block is allowed — the field itself is
    optional, so any sub-field is too."""
    instance = copy.deepcopy(baseline_ops_dept)
    instance["layer_1"] = {}
    errors = sorted(Draft7Validator(dept_schema).iter_errors(instance),
                    key=lambda e: list(e.path))
    assert not errors, "\n".join(f"[{'.'.join(str(p) for p in e.path)}]: {e.message}" for e in errors)


# ---------------------------------------------------------------------------
# Invalid declarations
# ---------------------------------------------------------------------------

def test_dept_yaml_with_negative_fire_after_rounds_fails(dept_schema, baseline_ops_dept):
    instance = copy.deepcopy(baseline_ops_dept)
    instance["layer_1"] = {"fire_after_rounds": -1}
    errors = list(Draft7Validator(dept_schema).iter_errors(instance))
    assert errors, "fire_after_rounds = -1 should fail (minimum >= 1)"


def test_dept_yaml_with_zero_fire_after_rounds_fails(dept_schema, baseline_ops_dept):
    """N=0 would mean 'fire immediately every tick' which defeats the
    idle-gate purpose. Disallow it."""
    instance = copy.deepcopy(baseline_ops_dept)
    instance["layer_1"] = {"fire_after_rounds": 0}
    errors = list(Draft7Validator(dept_schema).iter_errors(instance))
    assert errors, "fire_after_rounds = 0 should fail (minimum >= 1)"


def test_dept_yaml_with_string_fire_after_rounds_fails(dept_schema, baseline_ops_dept):
    instance = copy.deepcopy(baseline_ops_dept)
    instance["layer_1"] = {"fire_after_rounds": "1"}
    errors = list(Draft7Validator(dept_schema).iter_errors(instance))
    assert errors, "fire_after_rounds = \"1\" (string) should fail (integer required)"


def test_dept_yaml_with_unknown_layer_1_subfield_fails(dept_schema, baseline_ops_dept):
    """The layer_1 block must have additionalProperties: false."""
    instance = copy.deepcopy(baseline_ops_dept)
    instance["layer_1"] = {"fire_after_rounds": 1, "bogus_field": True}
    errors = list(Draft7Validator(dept_schema).iter_errors(instance))
    assert errors, "layer_1.bogus_field must be rejected (additionalProperties: false)"
