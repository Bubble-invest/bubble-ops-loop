"""
test_dry_run_validates_layer4_three_outputs.py — UX-4

Per Notion v5 line 278, Layer 4 must emit 3 outputs:
  risk-brief.md + risk-kpis.yaml + management-export.yaml.
Plus its own 4-file output schema. management-export.yaml must validate
against schemas-draft/management-export.schema.yaml.
"""
from __future__ import annotations

import yaml

from skill_lib.dry_run import run_dry_run_full


def test_layer4_emits_three_named_outputs(tmp_dept_repo, stub_agent_context):
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    layer4_dir = result.artifacts_dir / "4"
    assert (layer4_dir / "risk-brief.md").is_file()
    assert (layer4_dir / "risk-kpis.yaml").is_file()
    assert (layer4_dir / "management-export.yaml").is_file()


def test_layer4_management_export_validates_schema(
    tmp_dept_repo, stub_agent_context, schema_validator
):
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    me_path = result.artifacts_dir / "4" / "management-export.yaml"
    me = yaml.safe_load(me_path.read_text(encoding="utf-8"))

    v = schema_validator("management-export")
    errors = list(v.iter_errors(me))
    assert errors == [], f"management-export violations: {[e.message for e in errors]}"
