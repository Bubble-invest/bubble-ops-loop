"""
Verify the initial commit message follows the UX-2 spec:
  'bootstrap: <DisplayName> dept (onboarding/<slug>) - empty skeleton ready for step 1'
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def test_initial_commit_message(bootstrapped_repo: Path) -> None:
    out = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(bootstrapped_repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert out.startswith("bootstrap:"), f"unexpected commit subject: {out!r}"
    assert "SmokeTest" in out, f"display name missing: {out!r}"
    assert "onboarding/smoke-test" in out, f"branch missing: {out!r}"
    assert "step 1" in out.lower(), f"step-1 pointer missing: {out!r}"


def test_initial_commit_includes_skeleton_files(bootstrapped_repo: Path) -> None:
    """The initial commit must include the dept.yaml.draft and STATE.yaml."""
    files = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=str(bootstrapped_repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()
    assert "dept.yaml.draft" in files
    assert "onboarding/STATE.yaml" in files
    assert ".claude/settings.json" in files
