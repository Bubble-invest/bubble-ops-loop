"""
test_cli_run_dry_run_exits_1_on_failed.py — UX-4

Verify the CLI exits non-zero when the dry-run produces FAILED status
(broken fixture) or WARNING without --accept-warnings.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "scripts" / "run-dry-run.sh"
)


@pytest.mark.skipif(not SCRIPT.exists(), reason="CLI not generated yet")
def test_cli_exits_1_on_failed(tmp_dept_repo):
    """Broken fixture (missing required priority field) -> FAILED -> exit 1."""
    fake_qi_path = tmp_dept_repo / "fake-qi.yaml"
    bad = {
        "id": "bad-fixture-001",
        "kind": "research",
        "source_layer": 1,
        "target_layer": 2,
        # intentionally missing priority + created_at + payload
    }
    fake_qi_path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    # Tell run-dry-run.sh to use THIS interpreter (dev venv or CI
    # runner) instead of whatever bare `python3` PATH resolves to.
    env = os.environ.copy()
    env["PYTHON"] = sys.executable
    proc = subprocess.run(
        [
            "bash", str(SCRIPT),
            f"--dept-root={tmp_dept_repo}",
            f"--fake-queue-item={fake_qi_path}",
            "--seed=42",
            "--accept-warnings",  # even with this, FAILED still blocks
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 1, (
        f"expected 1, got {proc.returncode}; stdout={proc.stdout[:300]}"
    )


@pytest.mark.skipif(not SCRIPT.exists(), reason="CLI not generated yet")
def test_cli_exits_1_on_warning_without_override(tmp_dept_repo, stub_agent_context):
    dept_draft = {
        "department": {"slug": "miranda", "level": "ops",
                       "mandate": "content.", "status": "onboarding"},
        "gate_policies": {"social_post": {
            "kpi_guardrail_set": "miranda_content_kpis",
            "kpi_guardrails": {"brand_safety_breaches": 0},
        }},
    }
    (tmp_dept_repo / "dept.yaml.draft").write_text(
        yaml.safe_dump(dept_draft, sort_keys=False), encoding="utf-8"
    )
    ctx = stub_agent_context("step6_dry_run")
    fake_qi_path = tmp_dept_repo / "fake-qi.yaml"
    fake_qi_path.write_text(yaml.safe_dump(ctx["fake_queue_item"]), encoding="utf-8")

    # Tell run-dry-run.sh to use THIS interpreter (dev venv or CI
    # runner) instead of whatever bare `python3` PATH resolves to.
    env = os.environ.copy()
    env["PYTHON"] = sys.executable
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
    assert proc.returncode == 1, (
        f"expected 1 (WARNING without --accept-warnings), got {proc.returncode}"
    )
