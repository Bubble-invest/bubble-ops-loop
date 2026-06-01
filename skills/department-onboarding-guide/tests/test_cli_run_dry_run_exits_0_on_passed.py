"""
test_cli_run_dry_run_exits_0_on_passed.py — UX-4

scripts/run-dry-run.sh exit-code contract:
  0 = PASSED, or WARNING + --accept-warnings
  1 = FAILED, or WARNING without --accept-warnings
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

SCRIPT = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "scripts" / "run-dry-run.sh"
)


@pytest.mark.skipif(not SCRIPT.exists(), reason="CLI not generated yet")
def test_cli_exits_0_on_passed(tmp_dept_repo, stub_agent_context):
    # Write a clean dept (no brand_safety => no warning).
    dept_draft = {
        "department": {"slug": "alpha", "level": "ops",
                       "mandate": "minimal alpha dept.", "status": "onboarding"},
    }
    (tmp_dept_repo / "dept.yaml.draft").write_text(
        yaml.safe_dump(dept_draft, sort_keys=False), encoding="utf-8"
    )
    ctx = stub_agent_context("step6_dry_run")
    fake_qi_path = tmp_dept_repo / "fake-qi.yaml"
    fake_qi_path.write_text(yaml.safe_dump(ctx["fake_queue_item"]), encoding="utf-8")

    env = os.environ.copy()
    proc = subprocess.run(
        [
            "bash", str(SCRIPT),
            f"--dept-root={tmp_dept_repo}",
            f"--fake-queue-item={fake_qi_path}",
            "--seed=42",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, (
        f"expected 0, got {proc.returncode}; stderr={proc.stderr[:500]}"
    )
