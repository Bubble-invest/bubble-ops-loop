"""
test_dry_run_renderer_html_matches_notion_format.py — UX-4

Notion v5 lines 938-945:
  Dry run result:
  ✓ Layer 1 output valid
  ✓ Queue schema valid
  ✓ Layer 2 draft produced
  ✓ Gate produced
  ✓ Layer 3 dry-run execution valid
  ⚠ Missing brand safety test fixture

The HTML renderer must:
  - emit ✓ for passed checks
  - emit ⚠ for warnings
  - emit ✗ for failures (extension - not in Notion but symmetric)
  - be HTMX-swap-safe (single fragment, no <html>/<body> wrapper)
"""
from __future__ import annotations

from skill_lib.dry_run import run_dry_run_full
from skill_lib.dry_run_renderer import render_dry_run_html


def test_renderer_emits_check_icons(tmp_dept_repo, stub_agent_context):
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    html = render_dry_run_html(result)
    # No top-level wrappers — HTMX swap-safe.
    assert "<html" not in html.lower()
    assert "<body" not in html.lower()
    # Must contain at least one check icon.
    assert ("✓" in html or "&check;" in html), "must emit at least one pass marker"
    # Per Notion v5, the heading text appears verbatim.
    assert "Dry run result" in html


def test_renderer_emits_warning_marker(tmp_dept_repo, stub_agent_context):
    import yaml as _yaml

    dept_draft = {
        "department": {"slug": "miranda", "level": "ops",
                       "mandate": "social content.", "status": "onboarding"},
        "gate_policies": {"social_post": {
            "kpi_guardrail_set": "miranda_content_kpis",
            "kpi_guardrails": {"brand_safety_breaches": 0},
        }},
    }
    (tmp_dept_repo / "dept.yaml.draft").write_text(
        _yaml.safe_dump(dept_draft, sort_keys=False), encoding="utf-8"
    )
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    html = render_dry_run_html(result)
    assert ("⚠" in html or "&#9888;" in html or "warning" in html.lower())
