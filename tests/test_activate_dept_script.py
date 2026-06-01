"""
test_activate_dept_script.py — UX-5 task 3.

Tests the `scripts/activate-dept.sh --slug <kebab> --dry-run` flow.
Verifies:
  - exits 2 with blocker list when can_activate() is False
  - exits 0 with PR body printed to stdout when can_activate() is True

NEVER opens a real PR (uses --dry-run for the GREEN path). The non-dry
path is exercised by skills/.../test_activation_pr.py with subprocess
mocked.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "activate-dept.sh"


def _state(**over) -> dict:
    base = {
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "joris",
        "created_at": "2026-05-19T10:00:00Z",
        "status": "Ready to activate",
        "validated_steps": [
            "mandate", "missions", "layers", "skills_tools",
            "gates_kpis", "dry_run",
        ],
        "last_updated_at": "2026-05-20T10:00:00Z",
        "commits": [
            {"step": "dry_run", "commit_sha": "abcdef0",
             "validated_at": "2026-05-20T09:00:00Z"},
        ],
    }
    base.update(over)
    return base


def _dept() -> dict:
    return {
        "department": {
            "slug": "miranda", "level": "ops",
            "mandate": "Produce content with verifiable quality KPIs.",
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
        "recurring_missions": [
            {"id": "scan", "layer": 1, "cadence": "every_2h",
             "active_hours": "08:00-22:00",
             "creates": ["content_idea_task"],
             "output_queue": "queues/research/",
             "input_sources": ["wiki"],
             "description": "Scan signals."},
        ],
        "skills": {"layer_1": ["a"], "layer_2": ["b"],
                   "layer_3": ["c"], "layer_4": ["d"]},
        "tools": ["t"],
        "gate_policies": {
            "social_post": {
                "current_mode": "manual_required",
                "eligible_future_modes": [],
                "authorization_band": "low_risk",
                "kpi_guardrail_set": "miranda_kpis",
            }
        },
        "hierarchy": {
            "level": "ops", "parent": None, "children": [],
            "visibility": {
                "read_outputs": [], "read_risk_kpis": False,
                "read_risk_briefs": False, "read_raw_artifacts": False,
                "read_secrets": False,
            },
            "directive_policy": {
                "can_open_priority_prs": False,
                "target_queue": "queues/management/",
                "requires_human_gate_for": [],
            },
        },
        "optional_domain_ledger": None,
    }


def _make_repo(tmp_path: Path, state_over=None, dept=None) -> Path:
    repo = tmp_path / "bubble-ops-miranda"
    repo.mkdir()
    (repo / "onboarding").mkdir()
    state_doc = _state(**(state_over or {}))
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(state_doc, sort_keys=False), encoding="utf-8")
    (repo / "dept.yaml").write_text(
        yaml.safe_dump(dept if dept is not None else _dept(),
                       sort_keys=False), encoding="utf-8")
    for sub in ("outputs", "queues/research", "queues/management",
                "inbox/decisions", "missions", "tests"):
        (repo / sub).mkdir(parents=True, exist_ok=True)
    return repo


def test_dry_run_succeeds_and_prints_pr_body(tmp_path):
    repo = _make_repo(tmp_path)
    res = subprocess.run(
        [str(SCRIPT), "--slug=miranda",
         f"--repo-dir={repo}", "--dry-run"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, \
        f"expected 0, got {res.returncode}\nstdout={res.stdout}\nstderr={res.stderr}"
    # The PR body should be on stdout. Vocabulary refresh (msg 2702/2708,
    # 2026-05-21): old English headings now appear as their humanized French
    # equivalents (Bureau de Cadre, see activation_pr.py).
    assert "## Sa mission" in res.stdout
    assert "## Ce qu'elle fera chaque jour" in res.stdout
    assert "## Ce qu'il faut vérifier avant la cérémonie" in res.stdout
    assert "miranda" in res.stdout.lower()


def test_dry_run_fails_when_not_ready(tmp_path):
    repo = _make_repo(tmp_path, state_over={"status": "Drafting"})
    res = subprocess.run(
        [str(SCRIPT), "--slug=miranda",
         f"--repo-dir={repo}", "--dry-run"],
        capture_output=True, text=True,
    )
    assert res.returncode == 2, (
        f"expected 2, got {res.returncode}\n{res.stdout}\n{res.stderr}")
    err = res.stderr + res.stdout
    assert "Ready to activate" in err


def test_dry_run_fails_when_missing_step(tmp_path):
    repo = _make_repo(tmp_path, state_over={
        "validated_steps": ["mandate", "missions", "layers"],
        "status": "Drafting",
    })
    res = subprocess.run(
        [str(SCRIPT), "--slug=miranda",
         f"--repo-dir={repo}", "--dry-run"],
        capture_output=True, text=True,
    )
    assert res.returncode == 2
    assert "skills_tools" in (res.stderr + res.stdout) or \
           "dry_run" in (res.stderr + res.stdout)


def test_script_help_documents_dry_run():
    res = subprocess.run(
        [str(SCRIPT), "--help"],
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    assert "--dry-run" in res.stdout
