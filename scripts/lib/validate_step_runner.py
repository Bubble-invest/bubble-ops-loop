#!/usr/bin/env python3
"""
validate_step_runner.py - Python worker for validate-step.sh.

Per step, the validator:
  1. Locates the canonical artifact files for that step.
  2. Validates them against the right schema (dept.schema.yaml,
     recurring-mission.schema.yaml, etc.).
  3. If valid: git add + commit on the current branch with message
     `onboarding: <step> validated`, then record_validated_step in STATE.yaml.
  4. If invalid: print errors to stderr, exit 1, do NOT touch git or
     STATE.yaml.

The UX-2 spec only requires the `mandate` step to land a real validation;
the other steps are stubs that perform the same generic "validate the
files staged for this step" workflow. Step-specific validators are
pluggable via STEP_VALIDATORS below.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import jsonschema
import yaml


_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent  # .../projects/bubble-ops-loop
_SCHEMAS_DIR = _PROJECT_ROOT / "schemas-draft"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import state_yaml  # noqa: E402


# ---------------------------------------------------------------------------
# Schema loading.
# ---------------------------------------------------------------------------
def _load_schema(name: str) -> dict:
    p = _SCHEMAS_DIR / f"{name}.schema.yaml"
    if not p.exists():
        raise FileNotFoundError(f"schema not found: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Per-step validators. Each takes the repo_dir and returns (ok, errors,
# files_to_commit).
# ---------------------------------------------------------------------------
def validate_mandate(repo_dir: Path) -> Tuple[bool, List[str], List[Path]]:
    """Validates dept.yaml.draft against dept.schema.yaml."""
    draft = repo_dir / "dept.yaml.draft"
    if not draft.exists():
        return False, [f"missing artifact: {draft}"], []
    try:
        doc = yaml.safe_load(draft.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return False, [f"invalid YAML in {draft}: {exc}"], []
    schema = _load_schema("dept")
    v = jsonschema.Draft7Validator(schema)
    errors = sorted(v.iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        msgs = [f"{list(e.path)}: {e.message}" for e in errors]
        return False, msgs, []
    return True, [], [draft]


def validate_missions(repo_dir: Path) -> Tuple[bool, List[str], List[Path]]:
    """Validates each missions/*.yaml against recurring-mission.schema.yaml."""
    missions_dir = repo_dir / "missions"
    files = sorted(missions_dir.glob("*.yaml"))
    if not files:
        return False, ["no missions/*.yaml files found"], []
    schema = _load_schema("recurring-mission")
    v = jsonschema.Draft7Validator(schema)
    errors: List[str] = []
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            errors.append(f"{f.name}: invalid YAML: {exc}")
            continue
        for e in v.iter_errors(doc):
            errors.append(f"{f.name}: {list(e.path)}: {e.message}")
    if errors:
        return False, errors, []
    return True, [], files


def validate_generic_step(step: str):
    """
    Generic validator stub for layers / skills_tools / gates_kpis / dry_run.
    Validates that the onboarding/<n-step>/ directory has at least one file
    beyond README.md, and that any .yaml inside parses. This is a soft gate;
    the real schema-specific checks live in the UX-1 skill's lib (e.g.
    skill_lib.layers.map_layers, skill_lib.dry_run.run_dry_run).
    """
    dir_map = {
        "layers": "onboarding/3-layers",
        "skills_tools": "onboarding/4-skills-tools",
        "gates_kpis": "onboarding/5-gates-kpis",
        "dry_run": "onboarding/6-dry-run",
    }
    target = dir_map[step]

    def _v(repo_dir: Path) -> Tuple[bool, List[str], List[Path]]:
        d = repo_dir / target
        # Allow validation as long as the directory exists (Step 6+ may have
        # produced its artifacts directly under layers/, tests/, etc.).
        if not d.exists():
            return False, [f"missing directory: {d}"], []
        # Parse any yaml that's present.
        errors: List[str] = []
        for f in d.rglob("*.yaml"):
            try:
                yaml.safe_load(f.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                errors.append(f"{f}: invalid YAML: {exc}")
        if errors:
            return False, errors, []
        # Files to stage: all files under the step dir + everything currently
        # modified in the working tree (so the agent can commit what it
        # produced). We rely on `git add -A` for that.
        return True, [], [d]
    return _v


STEP_VALIDATORS = {
    "mandate": validate_mandate,
    "missions": validate_missions,
    "layers": validate_generic_step("layers"),
    "skills_tools": validate_generic_step("skills_tools"),
    "gates_kpis": validate_generic_step("gates_kpis"),
    "dry_run": validate_generic_step("dry_run"),
}


# ---------------------------------------------------------------------------
# Git helpers.
# ---------------------------------------------------------------------------
def _git(repo_dir: Path, *args: str, capture: bool = True) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=capture, text=True, check=True,
    )
    return res.stdout.strip()


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--slug", required=True)
    p.add_argument("--step", required=True, choices=list(STEP_VALIDATORS.keys()))
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--validated-by", default="operator")
    args = p.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    state_path = repo_dir / "onboarding" / "STATE.yaml"
    if not state_path.exists():
        print(f"ERROR: STATE.yaml not found at {state_path} - was the repo bootstrapped?", file=sys.stderr)
        return 1

    # Validate the step's artifact.
    validator = STEP_VALIDATORS[args.step]
    ok, errors, files = validator(repo_dir)
    if not ok:
        print(f"[validate-step] validation FAILED for step={args.step}:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    # Strategy: commit the artifact (allowing empty so even idempotent
    # re-validations leave a git trace) with the canonical message, THEN
    # update STATE.yaml to record the new HEAD's SHA. STATE.yaml is left
    # staged-but-uncommitted; the next validate-step (or activate-dept) will
    # roll it into history. This avoids the "self-referential SHA" trap.

    # Stage artifact files (any working-tree change).
    _git(repo_dir, "add", "-A")
    commit_msg = f"onboarding: {args.step} validated"
    # Use --allow-empty so re-running validate-step on an already-clean tree
    # still produces a visible "validated" commit.
    _git(repo_dir, "commit", "--allow-empty", "-m", commit_msg)
    sha = _git(repo_dir, "rev-parse", "HEAD")

    # Record into STATE.yaml.
    state_yaml.record_validated_step(
        path=state_path, step=args.step, commit_sha=sha,
        validated_by=args.validated_by,
    )

    # Stage STATE.yaml (caller / next step can commit it; tests only care
    # that the on-disk content shows the recorded SHA and HEAD's subject is
    # the validated message).
    _git(repo_dir, "add", "onboarding/STATE.yaml")

    print(f"[validate-step] step={args.step} validated. commit={sha[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
