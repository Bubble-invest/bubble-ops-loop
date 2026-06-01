"""Failure-mode tests: missing policy, broker not in PATH, push network error, etc."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.cli import main as cli_main
from src.guard import Guard
from src.policy_loader import load_policy
from tests.conftest import stage_files


def test_policy_file_missing(temp_git_repo, tmp_path, capsys):
    """A non-existent policy path must exit 1 with a clear error."""
    rc = cli_main(
        [
            "push",
            "--dept", "fixture",
            "--action", "runtime_write_own",
            "--repo", "bubble-ops-fixture",
            "--repo-dir", str(temp_git_repo),
            "--policy", str(tmp_path / "does-not-exist.yaml"),
            "--audit-log", str(tmp_path / "audit.jsonl"),
            "--dry-run",
        ]
    )
    assert rc != 0
    captured = capsys.readouterr()
    assert "policy" in captured.err.lower()


def test_broker_not_in_path(
    fixture_policy_yaml, temp_git_repo, mock_git_push, tmp_path
):
    """If the broker binary cannot be found, fail-closed with a clear error."""
    stage_files(temp_git_repo, ["outputs/x.md"])
    policy = load_policy(fixture_policy_yaml)
    g = Guard(
        policy=policy,
        broker_cmd=["/nonexistent/path/to/bubble-token-broker"],
        audit_log_path=tmp_path / "audit.jsonl",
    )
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert rc != 0
    # Audit should still log failure
    audit_file = tmp_path / "audit.jsonl"
    if audit_file.exists():
        lines = [json.loads(l) for l in audit_file.read_text().splitlines() if l.strip()]
        # Some failure-mode entry must exist
        assert any(ln.get("status") in {"denied", "push_failed", "mint_failed"} for ln in lines)
    # No git push happened
    assert mock_git_push.calls == []


def test_network_error_during_push(
    fixture_policy_yaml,
    temp_git_repo,
    mock_broker_binary,
    mock_git_push,
    tmp_path,
):
    """Simulated git push failure (network error) → exit 1, audit records it."""
    stage_files(temp_git_repo, ["outputs/x.md"])
    mock_git_push.state["returncode"] = 128
    mock_git_push.state["stderr"] = "fatal: unable to access 'https://github.com/...': could not resolve host"
    audit = tmp_path / "audit.jsonl"
    policy = load_policy(fixture_policy_yaml)
    g = Guard(
        policy=policy, broker_cmd=[str(mock_broker_binary)], audit_log_path=audit
    )
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert rc != 0
    lines = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    assert any(ln.get("status") == "push_failed" for ln in lines)


def test_malformed_policy_yaml(temp_git_repo, tmp_path, capsys):
    """A YAML parse error in the policy file must fail-closed clearly."""
    bad = tmp_path / "broken.yaml"
    bad.write_text("github_access:\n  actor: x\n  write: [: malformed\n")
    rc = cli_main(
        [
            "push",
            "--dept", "fixture",
            "--action", "runtime_write_own",
            "--repo", "bubble-ops-fixture",
            "--repo-dir", str(temp_git_repo),
            "--policy", str(bad),
            "--audit-log", str(tmp_path / "audit.jsonl"),
            "--dry-run",
        ]
    )
    assert rc != 0


def test_unknown_action_fails_closed(
    fixture_policy_yaml, temp_git_repo, mock_broker_binary, broker_call_log, mock_git_push, tmp_path
):
    """An action class not in {runtime_read, runtime_write_own, open_priority_pr, settings_pr}
    must be rejected before any broker call."""
    stage_files(temp_git_repo, ["outputs/x.md"])
    policy = load_policy(fixture_policy_yaml)
    g = Guard(
        policy=policy, broker_cmd=[str(mock_broker_binary)], audit_log_path=tmp_path / "a.jsonl"
    )
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="totally_made_up",
        repo="bubble-ops-fixture",
    )
    assert rc != 0
    assert not broker_call_log.exists() or broker_call_log.read_text() == ""
    assert mock_git_push.calls == []
