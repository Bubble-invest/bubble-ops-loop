"""Regressions for hostile repository hooks and repository-routing env.

The guarded actor controls its checkout, including ``.git/hooks`` and local
Git config.  No Git subprocess spawned by the guard may execute that code or
be redirected to a different repository/object/index namespace by inherited
environment variables.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.guard import Guard
from src.policy_loader import load_policy
from src.staging import hardened_git_command, hardened_git_env, staged_paths_for_push
from tests.conftest import stage_files


_REPOSITORY_ROUTING_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
)


def _allowed_real_push(
    temp_git_repo: Path,
    fixture_policy_yaml: Path,
    mock_broker_binary: Path,
    tmp_path: Path,
) -> int:
    stage_files(temp_git_repo, ["outputs/hook-regression.txt"], "allowed\n")
    subprocess.run(
        ["git", "commit", "-m", "allowed hook regression"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    guard = Guard(
        load_policy(fixture_policy_yaml),
        broker_cmd=[str(mock_broker_binary)],
        audit_log_path=tmp_path / "audit.jsonl",
    )
    return guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
    )


def test_repo_local_pre_push_hook_cannot_read_minted_token(
    temp_git_repo,
    fixture_policy_yaml,
    mock_broker_binary,
    tmp_path,
):
    """Round-4 PoC: the default .git/hooks/pre-push must never execute."""
    exfil = tmp_path / "default-hooks-env.txt"
    hook = temp_git_repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\nenv > {exfil}\n")
    hook.chmod(0o755)

    assert _allowed_real_push(
        temp_git_repo,
        fixture_policy_yaml,
        mock_broker_binary,
        tmp_path,
    ) == 0
    assert not exfil.exists(), "repo-local pre-push hook executed with token env"


def test_core_hookspath_pre_push_hook_cannot_read_minted_token(
    temp_git_repo,
    fixture_policy_yaml,
    mock_broker_binary,
    tmp_path,
):
    """Round-4 PoC variant: local core.hooksPath must be overridden."""
    hooks = tmp_path / "attacker-hooks"
    hooks.mkdir()
    exfil = tmp_path / "configured-hooks-env.txt"
    hook = hooks / "pre-push"
    hook.write_text(f"#!/bin/sh\nenv > {exfil}\n")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "config", "core.hooksPath", str(hooks)],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
        text=True,
    )

    assert _allowed_real_push(
        temp_git_repo,
        fixture_policy_yaml,
        mock_broker_binary,
        tmp_path,
    ) == 0
    assert not exfil.exists(), "core.hooksPath pre-push hook executed with token env"


def test_fetch_cannot_run_reference_transaction_hook(
    temp_git_repo,
    fixture_policy_yaml,
    mock_broker_binary,
    tmp_path,
):
    """Fetching the verified base must not execute another client-side hook."""
    stage_files(temp_git_repo, ["outputs/ref-hook.txt"], "allowed\n")
    subprocess.run(
        ["git", "commit", "-m", "allowed reference hook regression"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    marker = tmp_path / "reference-transaction-ran"
    hook = temp_git_repo / ".git" / "hooks" / "reference-transaction"
    hook.write_text(f"#!/bin/sh\necho \"$1\" >> {marker}\ncat >/dev/null\n")
    hook.chmod(0o755)

    # Control: this Git build does invoke the hook for a normal fetch ref
    # update, so absence below proves the guard's override rather than a
    # platform where the hook is inert.
    subprocess.run(
        ["git", "fetch", "--force", "origin", "main:refs/hook-control/base"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert marker.exists()
    marker.unlink()

    guard = Guard(
        load_policy(fixture_policy_yaml),
        broker_cmd=[str(mock_broker_binary)],
        audit_log_path=tmp_path / "audit.jsonl",
    )
    assert guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
    ) == 0
    assert not marker.exists(), "guard fetch executed reference-transaction hook"


def test_every_hardened_git_command_disables_all_hook_lookup():
    cmd = hardened_git_command("rev-parse", "HEAD")
    assert "core.hooksPath=/dev/null" in cmd


def test_final_push_also_bypasses_pre_push_explicitly(
    temp_git_repo,
    fixture_policy_yaml,
    mock_broker_binary,
    mock_git_push,
):
    stage_files(temp_git_repo, ["outputs/no-verify.txt"], "allowed\n")
    subprocess.run(
        ["git", "commit", "-m", "allowed no-verify regression"],
        cwd=temp_git_repo, check=True, capture_output=True, text=True,
    )
    guard = Guard(
        load_policy(fixture_policy_yaml),
        broker_cmd=[str(mock_broker_binary)],
    )

    assert guard.push(
        temp_git_repo,
        "fixture",
        "runtime_write_own",
        "bubble-ops-fixture",
    ) == 0
    cmd, _env = mock_git_push.calls[0]
    assert "--no-verify" in cmd
    assert "core.hooksPath=/dev/null" in cmd


@pytest.mark.parametrize("name", _REPOSITORY_ROUTING_ENV)
def test_hardened_git_env_scrubs_repository_routing_variables(name):
    env = hardened_git_env({name: "/attacker/controlled", "PATH": "/bin"})
    assert name not in env


def test_repo_dir_nested_inside_another_worktree_fails_closed(temp_git_repo):
    """A valid ancestor repository is not the repository named by --repo-dir."""
    nested = temp_git_repo / "nested-not-a-repository"
    nested.mkdir()

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        staged_paths_for_push(
            nested, destination_url=str(temp_git_repo.parent / "remote.git")
        )

    assert "outside requested repo dir" in (exc_info.value.stderr or "")
