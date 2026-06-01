"""
Verify validate-step.sh commits + appends the commit SHA to STATE.yaml when
the step's artifact is valid.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def _run_validate(scripts_dir: Path, slug: str, step: str, repo_dir: Path,
                  validated_by: str = "joris", expect_fail: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    res = subprocess.run(
        [
            "bash", str(scripts_dir / "validate-step.sh"),
            f"--slug={slug}", f"--step={step}", f"--repo-dir={repo_dir}",
            f"--validated-by={validated_by}",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if not expect_fail and res.returncode != 0:
        raise AssertionError(
            f"validate-step.sh failed (exit={res.returncode}):\n"
            f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
        )
    return res


def test_validate_step_mandate_appends_commit(
    scripts_dir: Path, bootstrapped_repo: Path
) -> None:
    """After running validate-step --step=mandate, STATE.yaml gains a `mandate`
    entry in validated_steps + a commits[] row with the new SHA."""
    # Pre: bootstrapped_repo already has dept.yaml.draft with status=onboarding.
    # The mandate artifact at this point IS dept.yaml.draft.
    _run_validate(scripts_dir, "smoke-test", "mandate", bootstrapped_repo)

    state = yaml.safe_load((bootstrapped_repo / "onboarding" / "STATE.yaml").read_text())
    assert "mandate" in state["validated_steps"]
    assert any(c["step"] == "mandate" for c in state["commits"])

    # The commit recorded should match the last commit on the branch.
    last_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(bootstrapped_repo),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    recorded = [c for c in state["commits"] if c["step"] == "mandate"][0]
    assert last_sha.startswith(recorded["commit_sha"]) or recorded["commit_sha"] == last_sha, (
        f"recorded SHA {recorded['commit_sha']!r} does not match HEAD {last_sha!r}"
    )


def test_validate_step_commit_message_convention(
    scripts_dir: Path, bootstrapped_repo: Path
) -> None:
    """The commit message must follow `onboarding: <step> validated`."""
    _run_validate(scripts_dir, "smoke-test", "mandate", bootstrapped_repo)
    msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(bootstrapped_repo),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert msg == "onboarding: mandate validated", f"unexpected commit subject: {msg!r}"
