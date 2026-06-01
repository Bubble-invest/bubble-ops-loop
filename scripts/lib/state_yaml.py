"""
state_yaml.py — CRUD helpers for onboarding/STATE.yaml.

The file format is defined by `schemas-draft/state.schema.yaml`. Functions
here keep STATE.yaml in sync with the 7-status state machine declared in the
UX-1 skill at `skills/department-onboarding-guide/skill_lib/state_machine.py`.

Per Notion v5 lines 793-801, status transitions are strict-linear except for
explicit operator-driven resets.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import yaml


# Mapping from a validated step-id to the status the dept should be in AFTER
# that step is validated. Derived from SKILL.md "## The 7 steps" table:
#   Step 1 (mandate)       -> status becomes `Configuring`
#   Step 2 (missions)      -> status becomes `Drafting`
#   Step 3 (layers)        -> status stays  `Drafting` (mid-step, no SM move)
#   Step 4 (skills_tools)  -> status becomes `Needs validation`
#   Step 5 (gates_kpis)    -> status becomes `Dry run`
#   Step 6 (dry_run)       -> status becomes `Ready to activate`
# Step 7 (activation) is NOT a "validated step" - the status flips
# `Ready to activate` -> `Live` via activate-dept.sh.
STEP_TO_POST_STATUS = {
    "mandate": "Configuring",
    "missions": "Drafting",
    "layers": "Drafting",
    "skills_tools": "Needs validation",
    "gates_kpis": "Dry run",
    "dry_run": "Ready to activate",
}

ALL_STEPS: List[str] = list(STEP_TO_POST_STATUS.keys())

STATUSES_IN_ORDER: List[str] = [
    "Idea",
    "Configuring",
    "Drafting",
    "Needs validation",
    "Dry run",
    "Ready to activate",
    "Live",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_state(
    path: Path,
    slug: str,
    display_name: str,
    owner: str,
    created_at: Optional[str] = None,
) -> None:
    """
    Create an empty STATE.yaml at `path` for a freshly-bootstrapped dept.
    status starts at `Idea`; validated_steps is empty.

    Idempotent: if the file already exists, this function REFUSES to overwrite
    (raises FileExistsError). Callers that want to reset must delete first.
    """
    if path.exists():
        raise FileExistsError(f"STATE.yaml already exists: {path}")
    doc = {
        "schema_version": 1,
        "slug": slug,
        "display_name": display_name,
        "owner": owner,
        "created_at": created_at or _now_iso(),
        "status": "Idea",
        "validated_steps": [],
        "last_updated_at": created_at or _now_iso(),
        "commits": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def load_state(path: Path) -> dict:
    """Read STATE.yaml. Raises FileNotFoundError or yaml.YAMLError on bad input."""
    if not path.exists():
        raise FileNotFoundError(f"STATE.yaml not found: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def save_state(path: Path, doc: dict) -> None:
    """Atomically write STATE.yaml (write tmp then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def record_validated_step(
    path: Path,
    step: str,
    commit_sha: str,
    validated_at: Optional[str] = None,
    validated_by: Optional[str] = None,
) -> dict:
    """
    Append a validated step + its commit to STATE.yaml and advance status.

    Idempotent: if `step` is already in validated_steps, returns the doc as-is
    without duplicating the entry. (No new commits[] row in that case either.)

    Returns the updated doc.
    """
    if step not in ALL_STEPS:
        raise ValueError(
            f"Unknown step {step!r}; must be one of {ALL_STEPS}"
        )

    doc = load_state(path)
    now = validated_at or _now_iso()

    # Idempotency guard.
    if step in doc.get("validated_steps", []):
        return doc

    doc.setdefault("validated_steps", []).append(step)

    commit_row = {
        "step": step,
        "commit_sha": commit_sha,
        "validated_at": now,
    }
    if validated_by:
        commit_row["validated_by"] = validated_by
    doc.setdefault("commits", []).append(commit_row)

    # Advance status. Only move forward; if the new status is "below" current,
    # we keep the current (this is paranoid - shouldn't happen with the strict
    # linear flow, but protects against operator-driven re-validations).
    new_status = STEP_TO_POST_STATUS[step]
    cur = doc.get("status", "Idea")
    if cur not in STATUSES_IN_ORDER:
        raise ValueError(f"STATE.yaml has unknown status {cur!r}")
    if STATUSES_IN_ORDER.index(new_status) > STATUSES_IN_ORDER.index(cur):
        doc["status"] = new_status

    doc["last_updated_at"] = now
    if validated_by:
        doc["last_validated_by"] = validated_by

    save_state(path, doc)
    return doc


def missing_steps(doc: dict) -> List[str]:
    """Return the subset of ALL_STEPS not yet in validated_steps."""
    validated = set(doc.get("validated_steps", []))
    return [s for s in ALL_STEPS if s not in validated]


def is_ready_to_activate(doc: dict) -> bool:
    """True iff all 6 work-steps validated AND status == Ready to activate."""
    return (
        not missing_steps(doc)
        and doc.get("status") == "Ready to activate"
    )


def mark_activated(
    path: Path,
    pr_number: int,
    pr_url: str,
    systemd_unit_path: str,
    activated_at: Optional[str] = None,
) -> dict:
    """UX-5: write the post-activation block to STATE.yaml.

    Sets:
      - status -> "Live"
      - activated_at -> ISO8601 UTC
      - activation_pr -> {number, url}
      - systemd_unit -> path

    Idempotent: if status is already "Live" AND activation_pr.number
    matches AND systemd_unit matches, this is a no-op (preserves the
    existing activated_at timestamp).
    """
    doc = load_state(path)
    cur_pr = doc.get("activation_pr") or {}
    if (
        doc.get("status") == "Live"
        and cur_pr.get("number") == pr_number
        and cur_pr.get("url") == pr_url
        and doc.get("systemd_unit") == systemd_unit_path
    ):
        return doc  # idempotent no-op

    doc["status"] = "Live"
    doc["activated_at"] = activated_at or _now_iso()
    doc["activation_pr"] = {"number": int(pr_number), "url": pr_url}
    doc["systemd_unit"] = systemd_unit_path
    doc["last_updated_at"] = doc["activated_at"]
    save_state(path, doc)
    return doc
