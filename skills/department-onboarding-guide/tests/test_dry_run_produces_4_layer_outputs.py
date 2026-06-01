"""
test_dry_run_produces_4_layer_outputs.py — UX-4

Verify that the enhanced `run_dry_run_full` simulator writes the canonical
4-file output schema (summary.md, artifacts/, logs.jsonl, .last-run) for
each of the 4 layers under outputs/dry-run/<ts>/<layer>/.
Notion v5 lines 56-105 (output schema standard) + lines 925-946 (dry-run).
"""
from __future__ import annotations

from skill_lib.dry_run import run_dry_run_full


def test_full_dry_run_writes_4_layer_subdirs(tmp_dept_repo, stub_agent_context):
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    base = result.artifacts_dir
    for layer in (1, 2, 3, 4):
        assert (base / str(layer)).is_dir(), f"missing layer {layer} dir"
        assert (base / str(layer) / "summary.md").is_file(), f"missing layer {layer} summary.md"
        assert (base / str(layer) / "logs.jsonl").is_file(), f"missing layer {layer} logs.jsonl"
        assert (base / str(layer) / ".last-run").is_file(), f"missing layer {layer} .last-run"
        assert (base / str(layer) / "artifacts").is_dir(), f"missing layer {layer} artifacts/"


def test_full_dry_run_marks_artifacts_as_fake(tmp_dept_repo, stub_agent_context):
    """Every YAML/MD artifact must carry the standardized 'DRY-RUN ARTIFACT' marker."""
    ctx = stub_agent_context("step6_dry_run")
    result = run_dry_run_full(
        dept_root=tmp_dept_repo,
        fake_queue_item=ctx["fake_queue_item"],
        seed=42,
    )
    base = result.artifacts_dir
    summary_l1 = (base / "1" / "summary.md").read_text(encoding="utf-8")
    assert "DRY-RUN ARTIFACT" in summary_l1
