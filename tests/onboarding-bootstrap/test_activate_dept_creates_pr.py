"""
Verify activate-dept.sh, when all 6 work-steps are validated AND status is
Ready to activate, invokes `gh pr create` with the spec'd title.
"""
from __future__ import annotations

import json
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


def test_activate_calls_gh_pr_create(
    scripts_dir: Path, bootstrapped_repo: Path, mock_gh_bin: dict
) -> None:
    _force_ready(bootstrapped_repo / "onboarding" / "STATE.yaml")

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

    # Verify a `gh pr create` was recorded with the spec'd title.
    calls = [
        json.loads(l)
        for l in mock_gh_bin["calls_file"].read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    pr_creates = [c for c in calls if c["argv"][:2] == ["pr", "create"]]
    assert pr_creates, f"no `gh pr create` call recorded. all calls: {calls}"
    argv = pr_creates[-1]["argv"]
    # --title must include 'Activate SmokeTest department' (Notion v5 line 975).
    title_idx = argv.index("--title") if "--title" in argv else -1
    assert title_idx >= 0, f"no --title flag in: {argv}"
    title = argv[title_idx + 1]
    assert "Activate" in title and "SmokeTest" in title and "department" in title, \
        f"unexpected PR title: {title!r}"
