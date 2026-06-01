"""--dry-run prints the plan without minting a token or pushing.

Useful for operators ("would this push be allowed?") and for the loop's
self-test mode.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.cli import main as cli_main
from tests.conftest import stage_files


def test_dry_run_prints_plan_no_side_effect(
    fixture_policy_yaml,
    temp_git_repo,
    mock_broker_binary,
    broker_call_log,
    mock_git_push,
    tmp_path,
    capsys,
    monkeypatch,
):
    stage_files(
        temp_git_repo,
        ["outputs/2026-05-20/1/summary.md", "queues/research/x.yaml"],
    )
    audit = tmp_path / "audit.jsonl"
    # Point CLI at our mock broker via PATH
    monkeypatch.setenv("PATH", str(mock_broker_binary.parent) + ":" + monkeypatch.delenv("PATH", raising=False) if False else f"{mock_broker_binary.parent}:/usr/bin:/bin")
    rc = cli_main(
        [
            "push",
            "--dept", "fixture",
            "--action", "runtime_write_own",
            "--repo", "bubble-ops-fixture",
            "--repo-dir", str(temp_git_repo),
            "--policy", str(fixture_policy_yaml),
            "--audit-log", str(audit),
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0, f"dry-run should exit 0; stderr={captured.err}"
    # Plan should mention dry-run and the staged paths
    full_output = captured.out + captured.err
    assert "dry-run" in full_output.lower() or "would" in full_output.lower()
    assert "outputs/2026-05-20/1/summary.md" in full_output
    # No broker call, no push
    assert not broker_call_log.exists() or broker_call_log.read_text() == ""
    assert mock_git_push.calls == []
    # Audit should record `would_allow`
    if audit.exists():
        lines = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
        assert any(ln.get("status") == "would_allow" for ln in lines)


def test_dry_run_denial_exits_nonzero(
    fixture_policy_yaml,
    temp_git_repo,
    mock_broker_binary,
    broker_call_log,
    mock_git_push,
    tmp_path,
    capsys,
):
    stage_files(temp_git_repo, ["MANDATE.md"])
    audit = tmp_path / "audit.jsonl"
    rc = cli_main(
        [
            "push",
            "--dept", "fixture",
            "--action", "runtime_write_own",
            "--repo", "bubble-ops-fixture",
            "--repo-dir", str(temp_git_repo),
            "--policy", str(fixture_policy_yaml),
            "--audit-log", str(audit),
            "--dry-run",
        ]
    )
    assert rc != 0
    captured = capsys.readouterr()
    assert "MANDATE.md" in captured.err or "denied" in captured.err.lower()
    assert mock_git_push.calls == []
    assert not broker_call_log.exists() or broker_call_log.read_text() == ""


def test_help_shows_examples(capsys):
    """--help must show usage examples (acceptance criterion)."""
    import pytest
    with pytest.raises(SystemExit):
        cli_main(["--help"])
    out = capsys.readouterr().out
    # Acceptance: at least 3 example lines mentioning bubble-git-guard
    example_count = out.lower().count("bubble-git-guard")
    assert example_count >= 3, f"expected ≥3 example lines, got {example_count}:\n{out}"
