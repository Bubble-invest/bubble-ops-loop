"""
test_dry_run_validates_layer1_output_schema.py — UX-4

Validates that Layer 1's synthesized queue item validates against
schemas-draft/queue-item.schema.yaml.
"""
from __future__ import annotations

import yaml

from skill_lib.dry_run import run_dry_run_full


def test_layer1_synthesized_queue_item_validates_schema(
    tmp_dept_repo, stub_agent_context, schema_validator
):
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    qi_path = result.artifacts_dir / "1" / "synthesized-queue-item.yaml"
    assert qi_path.is_file(), "Layer 1 must materialize the queue item"
    qi = yaml.safe_load(qi_path.read_text(encoding="utf-8"))

    v = schema_validator("queue-item")
    errors = list(v.iter_errors(qi))
    assert errors == [], f"queue-item schema violations: {[e.message for e in errors]}"

    # And the Check list must report this validation as passed.
    queue_checks = [c for c in result.checks if "queue" in c.step.lower()]
    assert any(c.status == "passed" for c in queue_checks)
