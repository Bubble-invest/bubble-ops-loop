"""The token broker MUST NOT be invoked if any staged path is denied.

This is the most important "fail-closed" invariant: if the guard mints a
token for a deny case, the deny is meaningless (attacker has the token
even if push fails downstream).
"""

from __future__ import annotations

from pathlib import Path

from src.guard import Guard
from src.policy_loader import load_policy
from tests.conftest import stage_files


def test_broker_not_called_when_path_denied(
    fixture_policy_yaml, temp_git_repo, mock_broker_binary, broker_call_log, mock_git_push
):
    """Stage a denied path, run guard.push(), assert mock broker log is empty."""
    stage_files(temp_git_repo, ["MANDATE.md"])  # denied for runtime_write_own
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy, broker_cmd=[str(mock_broker_binary)])
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert rc != 0, "denied push must return non-zero exit code"
    assert not broker_call_log.exists() or broker_call_log.read_text() == "", (
        f"broker MUST NOT be invoked for a denied push; log content: "
        f"{broker_call_log.read_text() if broker_call_log.exists() else '<no file>'}"
    )
    # And `git push` must not have been called either
    assert mock_git_push.calls == []


def test_broker_called_when_all_paths_allowed(
    fixture_policy_yaml, temp_git_repo, mock_broker_binary, broker_call_log, mock_git_push
):
    """Sanity: allowed paths DO trigger broker mint + push."""
    stage_files(
        temp_git_repo,
        ["outputs/2026-05-20/1/summary.md", "queues/research/x.yaml"],
    )
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy, broker_cmd=[str(mock_broker_binary)])
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert rc == 0, "allowed push should succeed (exit 0)"
    assert broker_call_log.exists() and broker_call_log.read_text() != ""
    assert mock_git_push.calls != []
