"""
Verify the bootstrap creates and checks out the `onboarding/<slug>` branch
(Notion v5 line 964).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def test_branch_named_onboarding_slug(bootstrapped_repo: Path) -> None:
    """HEAD is on `onboarding/smoke-test`."""
    head = _git_out(bootstrapped_repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert head == "onboarding/smoke-test", f"unexpected branch: {head}"


def test_remote_origin_set(bootstrapped_repo: Path) -> None:
    """Origin remote is configured (otherwise the push step wouldn't work)."""
    out = _git_out(bootstrapped_repo, "remote", "-v")
    assert "origin" in out, f"no origin in: {out!r}"
