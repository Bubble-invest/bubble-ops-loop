"""
Verify activate-dept.sh promotes dept.yaml.draft -> dept.yaml and flips
status from onboarding to live (Notion v5 lines 947-952).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def _force_ready(state_path: Path) -> None:
    doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    doc["validated_steps"] = [
        "mandate", "missions", "layers", "skills_tools", "gates_kpis", "dry_run",
    ]
    doc["status"] = "Ready to activate"
    state_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_activate_renames_draft_to_dept_yaml(
    scripts_dir: Path, bootstrapped_repo: Path, mock_gh_bin: dict
) -> None:
    _force_ready(bootstrapped_repo / "onboarding" / "STATE.yaml")

    assert (bootstrapped_repo / "dept.yaml.draft").exists()
    assert not (bootstrapped_repo / "dept.yaml").exists()

    env = os.environ.copy()
    env["PATH"] = f"{mock_gh_bin['bin_dir']}:{env['PATH']}"
    res = subprocess.run(
        [
            "bash", str(scripts_dir / "activate-dept.sh"),
            "--slug=smoke-test", f"--repo-dir={bootstrapped_repo}",
        ],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, (
        f"activate-dept.sh failed: stdout={res.stdout!r} stderr={res.stderr!r}"
    )

    assert (bootstrapped_repo / "dept.yaml").exists(), "draft was not promoted"
    assert not (bootstrapped_repo / "dept.yaml.draft").exists(), \
        "draft file still present after activation"


def test_activate_flips_status_to_live(
    scripts_dir: Path, bootstrapped_repo: Path, mock_gh_bin: dict
) -> None:
    _force_ready(bootstrapped_repo / "onboarding" / "STATE.yaml")

    env = os.environ.copy()
    env["PATH"] = f"{mock_gh_bin['bin_dir']}:{env['PATH']}"
    subprocess.run(
        [
            "bash", str(scripts_dir / "activate-dept.sh"),
            "--slug=smoke-test", f"--repo-dir={bootstrapped_repo}",
        ],
        env=env, check=True, capture_output=True, text=True,
    )
    doc = yaml.safe_load((bootstrapped_repo / "dept.yaml").read_text(encoding="utf-8"))
    assert doc["department"]["status"] == "live"
