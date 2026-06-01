"""Audit JSONL: every guard invocation MUST produce a line, with the right schema.

Schema (per Notion v4 lines 605-614 + Step 3c spec):
  {
    "ts":                  ISO8601 UTC,
    "actor":               "ops-loop-<dept>",
    "dept":                str,
    "repo":                str,
    "action":              str,
    "status":              "allowed" | "denied" | "would_allow" | "pushed" | "push_failed",
    "paths_count":         int,
    "denied_paths":        list[str] (only when status=denied),
    "reasons":             list[str] (only when status=denied),
    "token_ttl_minutes":   int (only when status in {allowed, pushed})
  }
"""

from __future__ import annotations

import json
from pathlib import Path

from src.guard import Guard
from src.policy_loader import load_policy
from tests.conftest import stage_files


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_audit_line_for_pushed_has_paths_count_and_token_ttl(
    fixture_policy_yaml,
    temp_git_repo,
    mock_broker_binary,
    broker_call_log,
    mock_git_push,
    tmp_path,
):
    stage_files(temp_git_repo, ["outputs/x.md", "queues/y.yaml"])
    audit = tmp_path / "audit.jsonl"
    policy = load_policy(fixture_policy_yaml)
    g = Guard(
        policy=policy, broker_cmd=[str(mock_broker_binary)], audit_log_path=audit
    )
    g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    lines = _read_jsonl(audit)
    assert len(lines) >= 1
    # The LAST line should be status=pushed
    pushed = [ln for ln in lines if ln.get("status") == "pushed"]
    assert pushed, f"no pushed line found in {lines}"
    e = pushed[-1]
    assert e["dept"] == "fixture"
    assert e["repo"] == "bubble-ops-fixture"
    assert e["action"] == "runtime_write_own"
    assert e["actor"] == "ops-loop-fixture"
    assert e["paths_count"] == 2
    assert "token_ttl_minutes" in e
    assert isinstance(e["token_ttl_minutes"], int)


def test_audit_line_for_deny_has_denied_paths_listed(
    fixture_policy_yaml,
    temp_git_repo,
    mock_broker_binary,
    mock_git_push,
    tmp_path,
):
    stage_files(temp_git_repo, ["outputs/x.md", "MANDATE.md"])
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
    lines = _read_jsonl(audit)
    denied = [ln for ln in lines if ln.get("status") == "denied"]
    assert denied, f"expected a denied audit line, got {lines}"
    e = denied[-1]
    assert e["dept"] == "fixture"
    assert e["repo"] == "bubble-ops-fixture"
    assert e["actor"] == "ops-loop-fixture"
    assert "MANDATE.md" in e.get("denied_paths", [])
    assert isinstance(e.get("reasons", []), list)
    assert len(e["reasons"]) > 0


def test_audit_line_for_push_failed_when_git_returns_nonzero(
    fixture_policy_yaml,
    temp_git_repo,
    mock_broker_binary,
    mock_git_push,
    tmp_path,
):
    stage_files(temp_git_repo, ["outputs/x.md"])
    mock_git_push.state["returncode"] = 1
    mock_git_push.state["stderr"] = "fatal: simulated remote rejection"
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
    lines = _read_jsonl(audit)
    failed = [ln for ln in lines if ln.get("status") == "push_failed"]
    assert failed, f"expected push_failed line, got {lines}"
