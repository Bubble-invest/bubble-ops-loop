"""
test_lifecycle_status_extension.py — Sprint Lifecycle Deliverable C.

The 7-status enum (Notion v5 lines 794-801) is extended with 2 lifecycle
terminals:
  - Cancelled  (used by cancel-eclosion for pre-Live abandonment)
  - Retired    (used by retire-dept for post-Live decommission)

Tests:
  1. STATE.yaml with status=Cancelled + cancelled_at validates.
  2. STATE.yaml with status=Retired + retired_at + retired_reason validates.
  3. dept.yaml::department.status enum still accepts 'retired' (existing v3.1
     value — sanity check we didn't break it).
  4. dept.yaml::department.status now accepts 'cancelled' too.
  5. validate_all.py exits 0 (covers the new positive examples we add).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

try:
    import yaml
    from jsonschema import Draft7Validator
except ImportError as exc:
    pytest.fail(f"missing dep: {exc}")


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # schemas-draft/
STATE_SCHEMA = ROOT / "state.schema.yaml"
DEPT_SCHEMA = ROOT / "dept.schema.yaml"


def _schema(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# STATE.yaml — Cancelled positive case
# ---------------------------------------------------------------------------

def test_state_yaml_cancelled_status_validates():
    """A STATE.yaml documenting a cancelled eclosure must validate."""
    instance = {
        "schema_version": 1,
        "slug": "smoke",
        "display_name": "Smoke",
        "owner": "operator",
        "created_at": "2026-05-21T10:00:00Z",
        "status": "Cancelled",
        "validated_steps": ["mandate"],
        "last_updated_at": "2026-05-21T10:30:00Z",
        "cancelled_at": "2026-05-21T10:30:00Z",
        "commits": [],
    }
    validator = Draft7Validator(_schema(STATE_SCHEMA))
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, (
        "Cancelled STATE.yaml should validate; got: "
        + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
    )


# ---------------------------------------------------------------------------
# STATE.yaml — Retired positive case
# ---------------------------------------------------------------------------

def test_state_yaml_retired_status_validates():
    """A STATE.yaml documenting a retired Live dept must validate."""
    instance = {
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "operator",
        "created_at": "2026-05-21T10:00:00Z",
        "status": "Retired",
        "validated_steps": ["mandate", "missions", "layers",
                            "skills_tools", "gates_kpis", "dry_run"],
        "last_updated_at": "2026-05-21T11:00:00Z",
        "retired_at": "2026-05-21T11:00:00Z",
        "retired_reason": "Maya v2 supersedes this dept",
        "commits": [],
    }
    validator = Draft7Validator(_schema(STATE_SCHEMA))
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, (
        "Retired STATE.yaml should validate; got: "
        + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
    )


# ---------------------------------------------------------------------------
# Existing 7 statuses still accepted (regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    "Idea", "Configuring", "Drafting", "Needs validation",
    "Dry run", "Ready to activate", "Live",
])
def test_existing_seven_statuses_still_accepted(status: str):
    """The original 7 Notion-v5 statuses (lines 794-801) MUST still validate
    after the lifecycle extension."""
    instance = {
        "schema_version": 1,
        "slug": "smoke",
        "display_name": "Smoke",
        "owner": "operator",
        "created_at": "2026-05-21T10:00:00Z",
        "status": status,
        "validated_steps": [],
        "last_updated_at": "2026-05-21T10:00:00Z",
        "commits": [],
    }
    validator = Draft7Validator(_schema(STATE_SCHEMA))
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, (
        f"Existing status {status!r} regressed; got: "
        + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
    )


# ---------------------------------------------------------------------------
# dept.yaml::department.status — new 'cancelled' value
# ---------------------------------------------------------------------------

def test_dept_yaml_status_cancelled_validates():
    """dept.yaml::department.status MUST now accept 'cancelled' (mirror of
    STATE.yaml's Cancelled terminal)."""
    schema = _schema(DEPT_SCHEMA)
    dept_status_enum = (
        schema["properties"]["department"]["properties"]["status"]["enum"]
    )
    assert "cancelled" in dept_status_enum, (
        f"dept.yaml::department.status enum missing 'cancelled': "
        f"{dept_status_enum}"
    )


def test_dept_yaml_status_retired_still_accepted():
    """'retired' was added in v3.1 and MUST still be in the enum."""
    schema = _schema(DEPT_SCHEMA)
    dept_status_enum = (
        schema["properties"]["department"]["properties"]["status"]["enum"]
    )
    assert "retired" in dept_status_enum, dept_status_enum


# ---------------------------------------------------------------------------
# validate_all.py end-to-end (covers the new positive examples we ship).
# ---------------------------------------------------------------------------

def test_validate_all_passes_with_new_examples():
    """Run schemas-draft/tests/validate_all.py — it must exit 0 after the
    new positive examples (state-cancelled, state-retired) are added."""
    script = HERE / "validate_all.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"validate_all.py exited {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# The two new positive examples exist on disk and validate.
# ---------------------------------------------------------------------------

def test_state_cancelled_example_exists():
    p = ROOT / "examples" / "state-cancelled.yaml"
    assert p.is_file(), f"Missing positive example: {p}"
    instance = yaml.safe_load(p.read_text(encoding="utf-8"))
    validator = Draft7Validator(_schema(STATE_SCHEMA))
    errors = list(validator.iter_errors(instance))
    assert not errors, [
        f"{list(e.path)}: {e.message}" for e in errors
    ]


def test_state_retired_example_exists():
    p = ROOT / "examples" / "state-retired.yaml"
    assert p.is_file(), f"Missing positive example: {p}"
    instance = yaml.safe_load(p.read_text(encoding="utf-8"))
    validator = Draft7Validator(_schema(STATE_SCHEMA))
    errors = list(validator.iter_errors(instance))
    assert not errors, [
        f"{list(e.path)}: {e.message}" for e in errors
    ]
