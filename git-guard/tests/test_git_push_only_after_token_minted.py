"""`git push` MUST NOT be invoked if the broker mint failed.

Fail-closed: a broker error must surface as a guard failure, never as a
silent fallback to some other token source (PAT, env GITHUB_TOKEN, etc.).
"""

from __future__ import annotations

from src.guard import Guard
from src.policy_loader import load_policy
from tests.conftest import stage_files


def test_git_push_not_called_when_broker_fails(
    fixture_policy_yaml,
    temp_git_repo,
    mock_failed_broker_binary,
    broker_call_log,
    mock_git_push,
):
    stage_files(temp_git_repo, ["outputs/2026-05-20/1/summary.md"])
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy, broker_cmd=[str(mock_failed_broker_binary)])
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert rc != 0, "broker failure must propagate as non-zero exit"
    # Broker was called (it must be, to know it failed)…
    assert broker_call_log.exists() and broker_call_log.read_text() != ""
    # …but git push must NEVER have been invoked.
    assert mock_git_push.calls == [], (
        f"git push must not run after broker failure; calls={mock_git_push.calls}"
    )


def test_guard_does_not_fall_back_to_env_github_token(
    fixture_policy_yaml,
    temp_git_repo,
    mock_failed_broker_binary,
    broker_call_log,
    mock_git_push,
    monkeypatch,
):
    """Even if GITHUB_TOKEN is set in the environment, the guard must NOT use it."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_LEAKED_PAT_xxxxxxxxxxxxxxxxxxxx")
    stage_files(temp_git_repo, ["outputs/2026-05-20/1/summary.md"])
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy, broker_cmd=[str(mock_failed_broker_binary)])
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert rc != 0
    assert mock_git_push.calls == [], "must not fall back to env GITHUB_TOKEN"
