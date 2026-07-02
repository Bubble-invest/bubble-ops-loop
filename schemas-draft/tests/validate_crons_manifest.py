#!/usr/bin/env python3
"""
validate_crons_manifest.py — schema validator for config/crons.yaml fixtures.

Kept SEPARATE from validate_all.py deliberately: crons-manifest.schema.yaml
is a different concept from the v3 dept.yaml family (recurring_missions =
Layer 1-4 pipeline work; crons.yaml = session-level CronCreate wakes — see
crons-manifest.schema.yaml's own description). Folding it into
validate_all.py's PREFIX_TO_SCHEMA / EXPECTED_SCHEMA_COUNT=7 harness would
conflate the two; this script is the crons-manifest twin of that harness,
same PASS/FAIL contract, over its own examples/negative directories:
  - schemas-draft/crons-manifest-examples/*.yaml   (expect PASS)
  - schemas-draft/crons-manifest-negative/*.yaml   (expect FAIL)

Dependencies: pip3 install jsonschema pyyaml --break-system-packages

Invocation:
  python3 schemas-draft/tests/validate_crons_manifest.py
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # schemas-draft/
SCHEMA_PATH = ROOT / "crons-manifest.schema.yaml"
EXAMPLES_DIR = ROOT / "crons-manifest-examples"
NEGATIVE_DIR = ROOT / "crons-manifest-negative"


def load_yaml(path: Path) -> object:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main() -> int:
    if not SCHEMA_PATH.is_file():
        print(f"[FATAL] schema not found: {SCHEMA_PATH}")
        return 2
    spec = load_yaml(SCHEMA_PATH)
    try:
        Draft7Validator.check_schema(spec)
    except jsonschema.SchemaError as exc:
        print(f"[FATAL] schema is malformed: {exc.message}")
        return 2
    validator = Draft7Validator(spec)

    if not EXAMPLES_DIR.is_dir():
        print(f"[FATAL] examples dir not found: {EXAMPLES_DIR}")
        return 1
    if not NEGATIVE_DIR.is_dir():
        print(f"[FATAL] negative dir not found: {NEGATIVE_DIR}")
        return 1

    all_ok = True

    example_files = sorted(EXAMPLES_DIR.glob("*.yaml"))
    print(f"Validating {len(example_files)} positive examples (expect PASS):")
    for path in example_files:
        instance = load_yaml(path)
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        if not errors:
            print(f"  [PASS] {path.name}")
        else:
            msgs = "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
            print(f"  [FAIL] {path.name} — expected PASS but got errors: {msgs}")
            all_ok = False
    print()

    negative_files = sorted(NEGATIVE_DIR.glob("*.yaml"))
    print(f"Validating {len(negative_files)} negative fixtures (expect FAIL):")
    for path in negative_files:
        instance = load_yaml(path)
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            print(f"  [PASS] {path.name} (rejected as expected: {list(first.path)}: {first.message})")
        else:
            print(f"  [FAIL] {path.name} — expected FAIL but instance validated")
            all_ok = False
    print()

    if all_ok:
        print(f"OK — {len(example_files)} positive + {len(negative_files)} negative checks all matched expectations.")
        return 0
    print("FAILED — see [FAIL] lines above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
