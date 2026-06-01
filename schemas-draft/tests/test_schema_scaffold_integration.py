"""
test_schema_scaffold_integration.py — cross-team integration test.

Renders a management dept.yaml.draft via scripts/lib/scaffold.py (Team A's
output), then validates it against schemas-draft/dept.schema.yaml.

This is the 'rendered scaffold output must be schema-valid' contract test.
If Team A changes the rendered shape, this test catches the drift immediately.

Covers GAP-H schema fix: read_paths must now be accepted by the schema.

Dependencies: jsonschema, pyyaml (same as the validate_all.py harness).

Run from the schemas-draft/tests/ directory or project root:
  pytest schemas-draft/tests/test_schema_scaffold_integration.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

try:
    import jsonschema
    from jsonschema import Draft7Validator
except ImportError:
    pytest.skip("jsonschema not installed", allow_module_level=True)

# ---------------------------------------------------------------------------
# Path surgery — import scaffold from scripts/lib/
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SCHEMAS_DRAFT = HERE.parent                      # schemas-draft/
PROJECT_ROOT = SCHEMAS_DRAFT.parent              # bubble-ops-loop/
SCRIPTS_LIB = PROJECT_ROOT / "scripts" / "lib"
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"

for p in (str(SKILL_ROOT), str(SCRIPTS_LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

import scaffold  # noqa: E402

# ---------------------------------------------------------------------------
# Load the dept schema once for the module
# ---------------------------------------------------------------------------
_SCHEMA_PATH = SCHEMAS_DRAFT / "dept.schema.yaml"


def _load_schema() -> dict:
    with _SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

MANAGEMENT_CHILDREN = ["ben", "maya", "miranda", "eliot"]

EXPECTED_READ_PATHS = [
    "outputs/*/4/risk-kpis.yaml",
    "outputs/*/4/risk-brief.md",
    "outputs/*/management-export.yaml",
    "queues/gates/**",
    "queues/improvements/**",
]


@pytest.fixture(scope="module")
def dept_schema():
    """Load and meta-validate the dept schema once per test module."""
    spec = _load_schema()
    Draft7Validator.check_schema(spec)
    return spec


@pytest.fixture()
def management_dept_yaml_draft(tmp_path):
    """Scaffold a management dept and return the parsed dept.yaml.draft content."""
    root = tmp_path / "bubble-ops-tony"
    root.mkdir()
    scaffold.scaffold(
        root=root,
        slug="tony",
        display_name="Tony",
        owner="joris",
        level="management",
        children=MANAGEMENT_CHILDREN,
    )
    draft_path = root / "dept.yaml.draft"
    assert draft_path.exists(), f"scaffold did not produce dept.yaml.draft at {draft_path}"
    return yaml.safe_load(draft_path.read_text(encoding="utf-8"))


def test_scaffold_management_dept_yaml_validates_against_schema(
    dept_schema, management_dept_yaml_draft
):
    """
    CROSS-TEAM INTEGRATION: Team A's scaffold output must validate cleanly
    against the dept schema.

    This is the GAP-H contract: after the schema accepts read_paths, the
    rendered dept.yaml.draft must be schema-valid with no additionalProperties
    errors.
    """
    validator = Draft7Validator(dept_schema)
    errors = sorted(validator.iter_errors(management_dept_yaml_draft), key=lambda e: list(e.path))

    if errors:
        msg = "\n".join(
            f"  [{'.'.join(str(p) for p in e.path)}]: {e.message}"
            for e in errors
        )
        pytest.fail(
            f"Scaffold-rendered management dept.yaml.draft failed schema validation:\n{msg}"
        )


def test_scaffold_management_draft_has_read_paths(management_dept_yaml_draft):
    """
    Scaffold must emit read_paths in hierarchy.visibility — and it must match
    the 5 canonical Notion paths.
    """
    visibility = management_dept_yaml_draft["hierarchy"]["visibility"]
    read_paths = visibility.get("read_paths")
    assert read_paths is not None, (
        "Scaffold must emit hierarchy.visibility.read_paths for management depts"
    )
    assert isinstance(read_paths, list), (
        f"read_paths must be a list, got {type(read_paths)}"
    )
    assert sorted(read_paths) == sorted(EXPECTED_READ_PATHS), (
        f"read_paths mismatch.\nExpected: {sorted(EXPECTED_READ_PATHS)}\n"
        f"Got:      {sorted(read_paths)}"
    )


def test_scaffold_management_draft_boolean_flags(management_dept_yaml_draft):
    """
    Scaffold must emit the four boolean visibility flags with correct defaults
    per Notion §"Scope dept.yaml pour un département management".
    """
    vis = management_dept_yaml_draft["hierarchy"]["visibility"]
    assert vis["read_risk_kpis"] is True, "read_risk_kpis default must be true"
    assert vis["read_risk_briefs"] is True, "read_risk_briefs default must be true"
    assert vis["read_raw_artifacts"] is False, "read_raw_artifacts must be false (Notion §1.1 security rule)"
    assert vis["read_secrets"] is False, "read_secrets must be false (hardcoded const)"


def test_scaffold_management_draft_read_outputs_are_slugs(management_dept_yaml_draft):
    """
    read_outputs must be a list of plain slug strings, one per child dept.
    """
    vis = management_dept_yaml_draft["hierarchy"]["visibility"]
    read_outputs = vis["read_outputs"]
    assert isinstance(read_outputs, list), f"read_outputs must be a list, got {type(read_outputs)}"
    for item in read_outputs:
        assert isinstance(item, str), (
            f"read_outputs items must be strings (slugs), got {type(item)}: {item!r}"
        )
    assert sorted(read_outputs) == sorted(MANAGEMENT_CHILDREN), (
        f"read_outputs mismatch. Expected {MANAGEMENT_CHILDREN}, got {read_outputs}"
    )


def test_example_management_tony_still_validates(dept_schema):
    """
    The canonical reference example (examples/dept-management-tony.yaml) must
    still validate cleanly after the schema patch.

    If this test fails: either the example diverged from the schema, or the
    schema patch introduced a regression. Do NOT silently update the example
    — flag the divergence.
    """
    example_path = SCHEMAS_DRAFT / "examples" / "dept-management-tony.yaml"
    assert example_path.exists(), f"Reference example not found: {example_path}"

    instance = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    validator = Draft7Validator(dept_schema)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))

    if errors:
        msg = "\n".join(
            f"  [{'.'.join(str(p) for p in e.path)}]: {e.message}"
            for e in errors
        )
        pytest.fail(
            f"Reference example dept-management-tony.yaml failed validation after schema patch.\n"
            f"DIVERGENCE DETECTED — do not silently fix the example; investigate the root cause.\n"
            f"Errors:\n{msg}"
        )
