#!/usr/bin/env python3
"""
validate_all.py — schema validator for bubble-ops-loop Step 0 contracts (v3).

Loads the 6 YAML JSON-schemas under schemas-draft/, then:
  - For every file in examples/, asserts it validates against the matching schema
    (PASS expected). Exit 1 on unexpected FAIL.
  - For every file in tests/negative/, asserts it does NOT validate against the
    matching schema (FAIL expected). Exit 1 on unexpected PASS.

Prints one line per check. Exit 0 iff every assertion matches expectations.

Notion v3 changes vs v1 harness:
  - 6 schemas instead of 5 (NEW: recurring-mission.schema.yaml)
  - new prefix mapping: recurring-mission-*  -> recurring-mission.schema.yaml

Dependencies:
  pip3 install jsonschema pyyaml --break-system-packages
  (or use bubble-vps-platform's venv at
   ~/claude-workspaces/Rick_RnD/projects/bubble-vps-platform/.venv/)

Invocation:
  python3 tests/validate_all.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
    import jsonschema
    from jsonschema import Draft7Validator
except ImportError as exc:
    print(f"[FATAL] missing dep: {exc}. Install via:")
    print("  pip3 install jsonschema pyyaml --break-system-packages")
    sys.exit(2)


# Resolve paths relative to this file so the runner is invocation-safe.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # schemas-draft/

# Map example/negative file prefixes to the schema filename that should govern them.
# Order matters: longer/more-specific prefixes MUST come before shorter ones
# (e.g. "recurring-mission-" before any hypothetical "recurring-").
PREFIX_TO_SCHEMA: list[tuple[str, str]] = [
    ("recurring-mission-", "recurring-mission.schema.yaml"),
    ("management-export-", "management-export.schema.yaml"),
    ("queue-item-", "queue-item.schema.yaml"),
    ("gate-item-", "gate-item.schema.yaml"),
    ("directive-", "directive.schema.yaml"),
    ("dept-", "dept.schema.yaml"),
    ("state-", "state.schema.yaml"),
]


def schema_for(filename: str) -> str | None:
    """Pick the schema whose prefix matches the file's basename."""
    for prefix, schema_name in PREFIX_TO_SCHEMA:
        if filename.startswith(prefix):
            return schema_name
    return None


def load_yaml(path: Path) -> object:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_schemas() -> dict[str, dict]:
    schemas: dict[str, dict] = {}
    for schema_path in sorted(ROOT.glob("*.schema.yaml")):
        try:
            spec = load_yaml(schema_path)
        except yaml.YAMLError as exc:
            print(f"[FATAL] cannot parse schema {schema_path.name}: {exc}")
            sys.exit(2)
        if not isinstance(spec, dict):
            print(f"[FATAL] schema {schema_path.name} is not a mapping")
            sys.exit(2)
        # Sanity: must be a Draft-07 schema and meta-validate.
        try:
            Draft7Validator.check_schema(spec)
        except jsonschema.SchemaError as exc:
            print(f"[FATAL] schema {schema_path.name} is malformed: {exc.message}")
            sys.exit(2)
        schemas[schema_path.name] = spec
    return schemas


def validate_file(
    instance_path: Path,
    schemas: dict[str, dict],
    *,
    expect_pass: bool,
) -> bool:
    """Return True if outcome matches expectation, False otherwise. Print one line."""
    schema_name = schema_for(instance_path.name)
    if schema_name is None:
        print(f"[FAIL] {instance_path.relative_to(ROOT)}: no schema mapping for prefix")
        return False
    if schema_name not in schemas:
        print(
            f"[FAIL] {instance_path.relative_to(ROOT)}: "
            f"schema {schema_name} not found in {ROOT}"
        )
        return False

    try:
        instance = load_yaml(instance_path)
    except yaml.YAMLError as exc:
        print(f"[FAIL] {instance_path.relative_to(ROOT)}: invalid YAML — {exc}")
        return False

    validator = Draft7Validator(schemas[schema_name])
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))

    if expect_pass:
        if not errors:
            print(f"[PASS] {instance_path.relative_to(ROOT)} against {schema_name}")
            return True
        msgs = "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
        print(
            f"[FAIL] {instance_path.relative_to(ROOT)} against {schema_name} — "
            f"expected PASS but got errors: {msgs}"
        )
        return False
    else:
        if errors:
            first = errors[0]
            print(
                f"[PASS] {instance_path.relative_to(ROOT)} against {schema_name} "
                f"(rejected as expected: {list(first.path)}: {first.message})"
            )
            return True
        print(
            f"[FAIL] {instance_path.relative_to(ROOT)} against {schema_name} — "
            f"expected FAIL but instance validated"
        )
        return False


# 8th schema (board #461): crons-manifest.schema.yaml — a deliberately
# SEPARATE manifest concept from the v3 dept.yaml family this harness
# otherwise governs (session-level durable CronCreate wakes, not
# Layer 1-4 recurring_missions). It is NOT wired into PREFIX_TO_SCHEMA /
# examples/ / tests/negative/ above — it has its own examples/negative dirs
# and its own validator (schemas-draft/tests/validate_crons_manifest.py) so
# it can evolve independently of the v3 dept.schema.yaml contract. This
# count only asserts load_schemas() still finds exactly the schemas we
# expect to exist in this directory.
EXPECTED_SCHEMA_COUNT = 8


def main() -> int:
    schemas = load_schemas()
    if len(schemas) != EXPECTED_SCHEMA_COUNT:
        print(
            f"[FATAL] expected {EXPECTED_SCHEMA_COUNT} schemas in {ROOT}, "
            f"found {len(schemas)}: {sorted(schemas.keys())}"
        )
        return 1

    print(f"Loaded {len(schemas)} schemas from {ROOT}:")
    for name in sorted(schemas):
        print(f"  - {name}")
    print()

    examples_dir = ROOT / "examples"
    negative_dir = HERE / "negative"
    # tests/positive/ holds targeted positive fixtures that complement the
    # broader examples/ set. Both directories are treated identically:
    # every file MUST validate cleanly.
    positive_dir = HERE / "positive"

    if not examples_dir.is_dir():
        print(f"[FATAL] examples directory not found: {examples_dir}")
        return 1
    if not negative_dir.is_dir():
        print(f"[FATAL] negative directory not found: {negative_dir}")
        return 1

    all_ok = True
    example_files = sorted(p for p in examples_dir.glob("*.yaml") if p.is_file())
    positive_files = (
        sorted(p for p in positive_dir.glob("*.yaml") if p.is_file())
        if positive_dir.is_dir()
        else []
    )
    negative_files = sorted(p for p in negative_dir.glob("*.yaml") if p.is_file())

    total_positive = len(example_files) + len(positive_files)

    print(f"Validating {len(example_files)} positive examples (expect PASS):")
    for path in example_files:
        ok = validate_file(path, schemas, expect_pass=True)
        all_ok = all_ok and ok
    print()

    if positive_files:
        print(f"Validating {len(positive_files)} tests/positive fixtures (expect PASS):")
        for path in positive_files:
            ok = validate_file(path, schemas, expect_pass=True)
            all_ok = all_ok and ok
        print()

    print(f"Validating {len(negative_files)} negative examples (expect FAIL):")
    for path in negative_files:
        ok = validate_file(path, schemas, expect_pass=False)
        all_ok = all_ok and ok
    print()

    if all_ok:
        print(
            f"OK — {total_positive} positive + {len(negative_files)} negative "
            f"checks all matched expectations."
        )
        return 0
    print("FAILED — at least one check did not match expectations. See [FAIL] lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
