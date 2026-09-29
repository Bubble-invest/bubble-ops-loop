"""Tests for path computation inside the guard-owned bare repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

from src import staging
from tests.conftest import stage_files


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
        },
    )


def _remote(repo: Path) -> str:
    return str(repo.parent / "remote.git")


def test_detects_files_in_unpushed_commit(temp_git_repo):
    stage_files(temp_git_repo, ["outputs/summary.md", "queues/research/x.yaml"])
    _git(temp_git_repo, "commit", "-m", "local commit")
    out = staging.staged_paths_for_push(
        temp_git_repo, destination_url=_remote(temp_git_repo)
    )
    assert set(out) == {"outputs/summary.md", "queues/research/x.yaml"}


def test_uncommitted_index_and_worktree_are_not_push_inputs(temp_git_repo):
    stage_files(temp_git_repo, ["outputs/staged-only.md"])
    (temp_git_repo / "untracked.md").write_text("not pushed\n")
    out = staging.staged_paths_for_push(
        temp_git_repo, destination_url=_remote(temp_git_repo)
    )
    assert out == []


def test_deduplicates_paths_changed_by_multiple_commits(temp_git_repo):
    stage_files(temp_git_repo, ["outputs/x.md"], "one\n")
    _git(temp_git_repo, "commit", "-m", "first")
    stage_files(temp_git_repo, ["outputs/x.md"], "two\n")
    _git(temp_git_repo, "commit", "-m", "second")
    out = staging.staged_paths_for_push(
        temp_git_repo, destination_url=_remote(temp_git_repo)
    )
    assert out == ["outputs/x.md"]


def test_returns_empty_when_source_matches_destination(temp_git_repo):
    out = staging.staged_paths_for_push(
        temp_git_repo, destination_url=_remote(temp_git_repo)
    )
    assert out == []
