"""
test_dry_run_validates_gate_item_schema.py — UX-4

Layer 2 must produce a fake gate that validates against
schemas-draft/gate-item.schema.yaml.
"""
from __future__ import annotations

import yaml

from skill_lib.dry_run import run_dry_run_full


def test_layer2_synthesized_gate_item_validates_schema(
    tmp_dept_repo, stub_agent_context, schema_validator
):
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    # The gate is named gate-fake-<id>.yaml under layer 2.
    gate_dir = result.artifacts_dir / "2"
    gate_files = list(gate_dir.glob("gate-fake-*.yaml"))
    assert gate_files, "Layer 2 must emit at least one fake gate file"
    gate = yaml.safe_load(gate_files[0].read_text(encoding="utf-8"))

    v = schema_validator("gate-item")
    errors = list(v.iter_errors(gate))
    assert errors == [], f"gate-item schema violations: {[e.message for e in errors]}"

    gate_checks = [c for c in result.checks if "gate" in c.step.lower()]
    assert any(c.status == "passed" for c in gate_checks)
