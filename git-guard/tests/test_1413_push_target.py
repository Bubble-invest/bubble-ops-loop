"""The checked changeset must match the source and destination passed to push."""

import pytest

from src.guard import Guard
from src.policy_loader import load_policy
from src.staging import staged_paths_for_push
from tests.conftest import _git, stage_files


@pytest.fixture
def diverged_repo(temp_git_repo):
    repo = temp_git_repo
    _git(repo, "branch", "onboarding/ben")
    stage_files(repo, ["requirements.txt"], "weasyprint==66.0\n")
    _git(repo, "commit", "-m", "existing main dependency")
    _git(repo, "push", "origin", "main", "onboarding/ben")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/onboarding/ben")
    _git(repo, "branch", "--set-upstream-to=origin/onboarding/ben")
    stage_files(repo, ["outputs/heartbeat.log"])
    _git(repo, "commit", "-m", "runtime heartbeat")
    assert not _git(repo, "status", "--porcelain").stdout
    return repo


@pytest.mark.parametrize("ref", ["HEAD", "main", "refs/heads/main", "HEAD:main", "HEAD:refs/heads/main"])
def test_heartbeat_push_ignores_divergent_default_and_upstream(
    diverged_repo, fixture_policy_yaml, ref, monkeypatch, capsys
):
    # This override must not supersede the destination being authorized.
    monkeypatch.setenv("BUBBLE_GUARD_DIFF_BASE", "origin/HEAD")
    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(diverged_repo, "fixture", "runtime_write_own", "bubble-ops-fixture",
                      ref=ref, dry_run=True) == 0
    output = capsys.readouterr().err
    assert "outputs/heartbeat.log" in output
    assert "requirements.txt" not in output


def test_structural_commit_cannot_be_hidden_by_upstream_or_override(
    temp_git_repo, fixture_policy_yaml, mock_broker_binary, broker_call_log, mock_git_push, monkeypatch
):
    stage_files(temp_git_repo, ["MANDATE.md"])
    _git(temp_git_repo, "commit", "-m", "unreviewed structural change")
    _git(temp_git_repo, "update-ref", "refs/remotes/origin/onboarding/ben", "HEAD")
    _git(temp_git_repo, "branch", "--set-upstream-to=origin/onboarding/ben")
    monkeypatch.setenv("BUBBLE_GUARD_DIFF_BASE", "HEAD")
    stage_files(temp_git_repo, ["outputs/heartbeat.log"])
    guard = Guard(load_policy(fixture_policy_yaml), broker_cmd=[str(mock_broker_binary)])
    assert guard.push(temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture") == 1
    assert not broker_call_log.exists()
    assert not mock_git_push.calls


def test_explicit_source_is_checked_instead_of_checkout(temp_git_repo):
    _git(temp_git_repo, "checkout", "-b", "other")
    stage_files(temp_git_repo, ["MANDATE.md"])
    _git(temp_git_repo, "commit", "-m", "structural source")
    _git(temp_git_repo, "checkout", "main")
    assert staged_paths_for_push(temp_git_repo, ref="other:main") == ["MANDATE.md"]


def test_selected_remote_and_destination_are_checked(temp_git_repo):
    _git(temp_git_repo, "remote", "add", "backup", "unused-offline")
    _git(temp_git_repo, "update-ref", "refs/remotes/backup/release", "HEAD")
    stage_files(temp_git_repo, ["outputs/heartbeat.log"])
    _git(temp_git_repo, "commit", "-m", "runtime")
    _git(temp_git_repo, "update-ref", "refs/remotes/origin/release", "HEAD")
    assert staged_paths_for_push(temp_git_repo, remote="backup", ref="HEAD:release") == ["outputs/heartbeat.log"]


def test_missing_destination_keeps_inclusive_history_fallback(diverged_repo):
    paths = staged_paths_for_push(diverged_repo, ref="HEAD:new-branch")
    assert {".gitkeep", "requirements.txt", "outputs/heartbeat.log"} <= set(paths)


def test_staged_structural_change_still_denied(diverged_repo, fixture_policy_yaml):
    stage_files(diverged_repo, ["MANDATE.md"])
    guard = Guard(load_policy(fixture_policy_yaml))
    assert guard.push(diverged_repo, "fixture", "runtime_write_own", "bubble-ops-fixture",
                      dry_run=True) == 1


@pytest.mark.parametrize("ref", [":main", "HEAD:", "HEAD:refs/tags/v1", "refs/heads/*:refs/heads/*", "missing:main", "--all", "HEAD:HEAD"])
def test_unsupported_ref_fails_before_mint(
    temp_git_repo, fixture_policy_yaml, mock_broker_binary, broker_call_log, mock_git_push, ref
):
    stage_files(temp_git_repo, ["outputs/heartbeat.log"])
    guard = Guard(load_policy(fixture_policy_yaml), broker_cmd=[str(mock_broker_binary)])
    assert guard.push(temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture", ref=ref) == 1
    assert not broker_call_log.exists()
    assert not mock_git_push.calls
