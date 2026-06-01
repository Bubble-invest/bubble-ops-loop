"""
state_yaml_reader.py — read onboarding/STATE.yaml from a bubble-ops-<slug>
repo (on-disk in test/dev mode; via `gh api` in prod follow-up).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml


def read_state_for_repo(repo_dir: Path) -> Optional[dict]:
    """Return the parsed STATE.yaml dict, or None if missing."""
    p = repo_dir / "onboarding" / "STATE.yaml"
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None


# All 6 work-steps per state.schema.yaml (the 7th step "activation" is the
# status flip Ready-to-activate -> Live, not a validated_steps entry).
ALL_STEPS = ["mandate", "missions", "layers", "skills_tools", "gates_kpis",
             "dry_run"]


def step_status(state: dict, step: str) -> str:
    """
    Return one of {"validated", "in_progress", "pending"} for a given step.
    A step is "in_progress" iff it's the FIRST not-yet-validated step.
    """
    validated = state.get("validated_steps", []) if state else []
    if step in validated:
        return "validated"
    # the first un-validated step is the one in progress (per Notion v5
    # "Drafting" mid-step status); rest are "pending"
    for s in ALL_STEPS:
        if s not in validated:
            return "in_progress" if s == step else "pending"
    return "pending"
