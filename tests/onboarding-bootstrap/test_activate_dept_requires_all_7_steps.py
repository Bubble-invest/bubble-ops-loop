"""
Verify activate-dept.sh refuses to open the activation PR unless STATE.yaml
shows all 6 onboarding work-steps validated AND status is `Ready to activate`
(the 7th step "activation" is what THIS script performs).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


REQUIRED = ["mandate", "missions", "layers", "skills_tools", "gates_kpis", "dry_run"]


def _run_activate(scripts_dir: Path, slug: str, repo_dir: Path,
                  extra_env: dict | None = None,
                  expect_fail: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    res = subprocess.run(
        [
            "bash", str(scripts_dir / "activate-dept.sh"),
            f"--slug={slug}", f"--repo-dir={repo_dir}",
        ],
        env=env, capture_output=True, text=True,
    )
    if not expect_fail and res.returncode != 0:
        raise AssertionError(
            f"activate-dept.sh failed (exit={res.returncode}):\n"
            f"STDOUT: {res.stdout}\nSTDERR: {res.stderr}"
        )
    return res


def test_activate_refuses_when_no_steps_validated(
    scripts_dir: Path, bootstrapped_repo: Path
) -> None:
    """Brand-new repo right after bootstrap: zero steps validated -> refuse."""
    res = _run_activate(scripts_dir, "smoke-test", bootstrapped_repo, expect_fail=True)
    assert res.returncode != 0
    combined = (res.stdout + res.stderr).lower()
    assert "not validated" in combined or "missing" in combined or "incomplete" in combined, \
        f"expected refusal message: {res.stdout + res.stderr!r}"


def test_activate_refuses_with_partial_steps(
    scripts_dir: Path, bootstrapped_repo: Path
) -> None:
    """Only some steps validated -> refuse."""
    state_path = bootstrapped_repo / "onboarding" / "STATE.yaml"
    doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    doc["validated_steps"] = ["mandate", "missions"]  # only 2 of 6
    doc["status"] = "Drafting"
    state_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    res = _run_activate(scripts_dir, "smoke-test", bootstrapped_repo, expect_fail=True)
    assert res.returncode != 0
    combined = (res.stdout + res.stderr).lower()
    # Must enumerate the missing steps.
    assert "layers" in combined or "skills_tools" in combined or "missing" in combined, \
        f"expected refusal mentioning missing steps: {res.stdout + res.stderr!r}"
