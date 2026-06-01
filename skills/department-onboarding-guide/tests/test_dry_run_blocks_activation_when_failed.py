"""
test_dry_run_blocks_activation_when_failed.py — UX-4

If any schema validation fails, overall_status MUST be FAILED and
can_advance_to_ready MUST be False, even with operator_accepts_warnings=True.
Notion v5 line 946: agent ne passe pas live sans dry run all-green.
"""
from __future__ import annotations

from skill_lib.dry_run import run_dry_run_full


def test_failed_schema_blocks_advance(tmp_dept_repo, stub_agent_context):
    # Inject a deliberately broken fake queue item (missing required `priority`).
    bad = {
        "id": "bad-fixture-001",
        "kind": "research",
        "source_layer": 1,
        "target_layer": 2,
        # missing priority + created_at + payload
    }
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=bad,
        operator_accepts_warnings=True,  # even with override, FAILED stays blocked
        seed=42,
    )
    assert result.overall_status == "FAILED"
    assert result.can_advance_to_ready is False
