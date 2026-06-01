"""
test_step6_dry_run.py — Step 6 (Tests / dry-run).

Notion v5 lines 925-946. Simulator takes a fake queue item and synthesizes
all 4 layer outputs in fixture-only mode (no real APIs).
"""
from __future__ import annotations

import pytest

from skill_lib.dry_run import run_dry_run, DryRunStatus


def test_dry_run_fixture_produces_full_round_trip(stub_agent_context, tmp_dept_repo):
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        layer_checks={  # all 4 green
            "layer_1": "passed",
            "layer_2": "passed",
            "layer_3": "passed",
            "layer_4": "passed",
        },
    )
    assert set(result["outputs"].keys()) == {"layer_1", "layer_2", "layer_3", "layer_4"}
    for layer_key in ("layer_1", "layer_2", "layer_3", "layer_4"):
        # Each output is a non-empty dict with at least a `kind` field.
        out = result["outputs"][layer_key]
        assert isinstance(out, dict)
        assert "kind" in out
    assert result["overall_status"] == DryRunStatus.PASSED


def test_dry_run_blocks_activation_if_not_all_green(stub_agent_context, tmp_dept_repo):
    """If any of the 4 layers fails its dry-run check, status must be FAILED
    and can_advance_to_ready must be False."""
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        layer_checks={
            "layer_1": "passed",
            "layer_2": "passed",
            "layer_3": "failed",  # Layer 3 fails
            "layer_4": "passed",
        },
    )
    assert result["overall_status"] == DryRunStatus.FAILED
    assert result["can_advance_to_ready"] is False


def test_dry_run_allows_explicit_override_for_yellow_items(stub_agent_context, tmp_dept_repo):
    """Notion v5 line 946 — operator can explicitly accept warnings and proceed."""
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        layer_checks={
            "layer_1": "passed",
            "layer_2": "warning",  # yellow item
            "layer_3": "passed",
            "layer_4": "passed",
        },
        operator_accepts_warnings=True,
    )
    assert result["overall_status"] == DryRunStatus.WARNING
    assert result["can_advance_to_ready"] is True
    # And without the override, can_advance_to_ready is False.
    result2 = run_dry_run(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        layer_checks={
            "layer_1": "passed",
            "layer_2": "warning",
            "layer_3": "passed",
            "layer_4": "passed",
        },
        operator_accepts_warnings=False,
    )
    assert result2["can_advance_to_ready"] is False
