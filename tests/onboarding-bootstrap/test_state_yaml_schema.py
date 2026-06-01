"""
Verify state.schema.yaml exists, is a valid Draft-07 schema, and that a
sample STATE.yaml validates against it.
"""
from __future__ import annotations

from pathlib import Path

import jsonschema
import yaml


def test_schema_file_exists(schemas_dir: Path) -> None:
    p = schemas_dir / "state.schema.yaml"
    assert p.exists(), f"missing schema: {p}"


def test_schema_is_valid_draft7(schemas_dir: Path) -> None:
    p = schemas_dir / "state.schema.yaml"
    schema = yaml.safe_load(p.read_text(encoding="utf-8"))
    jsonschema.Draft7Validator.check_schema(schema)


def test_sample_state_yaml_validates(schemas_dir: Path, sample_state_yaml: dict) -> None:
    p = schemas_dir / "state.schema.yaml"
    schema = yaml.safe_load(p.read_text(encoding="utf-8"))
    v = jsonschema.Draft7Validator(schema)
    errors = sorted(v.iter_errors(sample_state_yaml), key=lambda e: list(e.path))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]


def test_schema_rejects_unknown_status(schemas_dir: Path, sample_state_yaml: dict) -> None:
    p = schemas_dir / "state.schema.yaml"
    schema = yaml.safe_load(p.read_text(encoding="utf-8"))
    bad = dict(sample_state_yaml)
    bad["status"] = "NotARealStatus"
    v = jsonschema.Draft7Validator(schema)
    errors = list(v.iter_errors(bad))
    assert errors, "schema must reject status not in the 7-enum"


def test_schema_rejects_unknown_validated_step(schemas_dir: Path, sample_state_yaml: dict) -> None:
    p = schemas_dir / "state.schema.yaml"
    schema = yaml.safe_load(p.read_text(encoding="utf-8"))
    bad = dict(sample_state_yaml)
    bad["validated_steps"] = ["mandate", "not_a_step"]
    v = jsonschema.Draft7Validator(schema)
    errors = list(v.iter_errors(bad))
    assert errors, "schema must reject step-ids outside the 7-step enum"
