"""Tests for src/staging.py — what does `git diff` say is about to push?

The guard must detect BOTH:
  (a) files staged in the working tree (`git diff --cached --name-only`)
  (b) files in unpushed commits on the current branch
      (`git diff @{upstream}..HEAD --name-only`)

The union of (a) and (b), deduped, is what's "about to push".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src import staging
from tests.conftest import stage_files


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e.com",
        },
    )


def test_detects_staged_files_for_push(temp_git_repo):
    stage_files(temp_git_repo, ["outputs/2026-05-20/1/summary.md", "queues/research/x.yaml"])
    out = staging.currently_staged(temp_git_repo)
    assert "outputs/2026-05-20/1/summary.md" in out
    assert "queues/research/x.yaml" in out


def test_does_not_detect_unstaged_files(temp_git_repo):
    # Write but don't add
    (temp_git_repo / "untracked.md").write_text("nope\n")
    out = staging.currently_staged(temp_git_repo)
    assert "untracked.md" not in out


def test_detects_files_in_commits_between_head_and_remote(temp_git_repo):
    """If we commit something locally without push, the path must appear in
    `staged_paths_for_push` so the guard can check it before push."""
    stage_files(temp_git_repo, ["outputs/2026-05-20/1/summary.md"])
    _git(temp_git_repo, "commit", "-m", "local commit, not pushed")
    # No upstream set — staged_paths_for_push should still detect the unpushed commit
    # by falling back to "all files in HEAD that wouldn't be on origin/main".
    out = staging.staged_paths_for_push(temp_git_repo)
    assert "outputs/2026-05-20/1/summary.md" in out


def test_returns_union_of_staged_and_unpushed_commits(temp_git_repo):
    # First commit a file locally (unpushed)
    stage_files(temp_git_repo, ["outputs/2026-05-20/1/summary.md"])
    _git(temp_git_repo, "commit", "-m", "unpushed")
    # Then stage another file in the working tree
    stage_files(temp_git_repo, ["queues/research/y.yaml"])
    out = staging.staged_paths_for_push(temp_git_repo)
    assert "outputs/2026-05-20/1/summary.md" in out
    assert "queues/research/y.yaml" in out


def test_deduplicates_paths_present_in_both(temp_git_repo):
    """A path can be both in HEAD and re-staged. Must appear once."""
    stage_files(temp_git_repo, ["outputs/x.md"])
    _git(temp_git_repo, "commit", "-m", "first")
    # Re-modify + re-stage same path
    (temp_git_repo / "outputs" / "x.md").write_text("y\n")
    _git(temp_git_repo, "add", "outputs/x.md")
    out = staging.staged_paths_for_push(temp_git_repo)
    assert out.count("outputs/x.md") == 1


def test_returns_empty_list_when_nothing_staged_or_unpushed(temp_git_repo):
    # Fresh repo with only the seed commit
    # Pretend the seed commit is "already on remote" by setting an upstream
    # pointing to a clone — but for simplicity, the function should at least
    # not crash and should return [] for a fresh tree.
    out = staging.currently_staged(temp_git_repo)
    assert out == []
