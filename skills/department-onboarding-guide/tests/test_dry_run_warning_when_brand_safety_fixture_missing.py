"""
test_dry_run_warning_when_brand_safety_fixture_missing.py — UX-4

Notion v5 line 944 — the canonical example WARNING is "Missing brand
safety test fixture". The simulator must surface a warning Check when the
dept declares a brand_safety-shaped policy but tests/brand_safety.yaml
(or any *brand_safety*.yaml fixture) is missing.
"""
from __future__ import annotations

import yaml

from skill_lib.dry_run import run_dry_run_full


def test_brand_safety_missing_emits_warning(tmp_dept_repo, stub_agent_context):
    # Write a dept.yaml.draft that declares a brand_safety guardrail set,
    # WITHOUT providing the corresponding test fixture.
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

    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    brand_warnings = [c for c in result.checks if "brand_safety" in c.step]
    assert any(c.status == "warning" for c in brand_warnings), (
        f"expected brand_safety warning, got checks: {[(c.step, c.status) for c in result.checks]}"
    )
    assert result.overall_status == "WARNING"
