"""
Verify bootstrap-dept.sh produces the exact tree spec'd in Notion v5 lines
751-762 + UX-2 component-A spec.
"""
from __future__ import annotations

from pathlib import Path

import yaml


REQUIRED_PATHS = [
    "README.md",
    ".gitignore",
    "dept.yaml.draft",
    "onboarding/STATE.yaml",
    "onboarding/1-mandate/README.md",
    "onboarding/2-missions",
    "onboarding/3-layers",
    "onboarding/4-skills-tools",
    "onboarding/5-gates-kpis",
    "onboarding/6-dry-run",
    "onboarding/7-activation",
    "missions/.gitkeep",
    "layers/1/.gitkeep",
    "layers/2/.gitkeep",
    "layers/3/.gitkeep",
    "layers/4/.gitkeep",
    "subagents/.gitkeep",
    "skills/.gitkeep",
    "tools/.gitkeep",
    "policies/.gitkeep",
    "tests/fixtures/.gitkeep",
    "tests/run.sh",
    "queues/research/.gitkeep",
    "queues/gates/.gitkeep",
    "queues/management/.gitkeep",
    "queues/improvements/.gitkeep",
    "inbox/decisions/.gitkeep",
    "outputs/onboarding/.gitkeep",
    ".claude/settings.json",
]


def test_full_tree_exists(bootstrapped_repo: Path) -> None:
    """Every required path enumerated in the spec exists after bootstrap."""
    missing = [p for p in REQUIRED_PATHS if not (bootstrapped_repo / p).exists()]
    assert not missing, f"Missing paths after bootstrap: {missing}"


def test_dept_yaml_draft_has_onboarding_status(bootstrapped_repo: Path) -> None:
    """The dept.yaml.draft must declare status=onboarding (Notion v5 line 818)."""
    draft = bootstrapped_repo / "dept.yaml.draft"
    doc = yaml.safe_load(draft.read_text(encoding="utf-8"))
    assert doc["department"]["status"] == "onboarding"
    assert doc["department"]["slug"] == "smoke-test"


def test_tests_run_sh_is_executable_and_exits_0(bootstrapped_repo: Path) -> None:
    """tests/run.sh stub exists, is executable, and exits 0 with the
    'no tests yet' message (per spec)."""
    import os
    import subprocess

    path = bootstrapped_repo / "tests" / "run.sh"
    assert path.exists()
    assert os.access(path, os.X_OK), "tests/run.sh must be executable"
    res = subprocess.run(["bash", str(path)], capture_output=True, text=True)
    assert res.returncode == 0
    assert "no tests yet" in (res.stdout + res.stderr).lower()
