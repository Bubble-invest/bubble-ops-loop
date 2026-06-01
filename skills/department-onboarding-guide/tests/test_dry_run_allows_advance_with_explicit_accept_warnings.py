"""
test_dry_run_allows_advance_with_explicit_accept_warnings.py — UX-4

Notion v5 line 946 — "ou acceptation explicite": a WARNING-only result
must be advanceable iff operator passes operator_accepts_warnings=True.
"""
from __future__ import annotations

import yaml

from skill_lib.dry_run import run_dry_run_full


def _write_brand_warning_dept(tmp_dept_repo):
    dept_draft = {
        "department": {
            "slug": "miranda",
            "level": "ops",
            "mandate": "Produire et publier du contenu social.",
            "status": "onboarding",
        },
        "gate_policies": {
            "social_post": {
                "kpi_guardrail_set": "miranda_content_kpis",
                "kpi_guardrails": {"brand_safety_breaches": 0},
            }
        },
    }
    (tmp_dept_repo / "dept.yaml.draft").write_text(
        yaml.safe_dump(dept_draft, sort_keys=False), encoding="utf-8"
    )


def test_warning_without_override_blocks(tmp_dept_repo, stub_agent_context):
    _write_brand_warning_dept(tmp_dept_repo)
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        operator_accepts_warnings=False,
        seed=42,
    )
    assert result.overall_status == "WARNING"
    assert result.can_advance_to_ready is False


def test_warning_with_override_advances(tmp_dept_repo, stub_agent_context):
    _write_brand_warning_dept(tmp_dept_repo)
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        operator_accepts_warnings=True,
        seed=42,
    )
    assert result.overall_status == "WARNING"
    assert result.can_advance_to_ready is True
