"""Repository validation and clean-worktree push regressions.

The actor repository is now used only to resolve the immutable source SHA,
but a wrong ``--repo-dir`` must still fail with a clear repository/worktree
diagnostic. A clean working tree with committed, unpushed changes remains the
normal happy path; a source identical to the destination produces no paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src import staging


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


def test_staged_paths_for_push_rejects_non_git_directory(tmp_path: Path):
    """Calling staged_paths_for_push from a non-git dir must NOT raise the
    confusing 'unknown option `cached`' error from git's --no-index fallback.

    It should raise a clear, named exception (subprocess.CalledProcessError
    or RuntimeError) with a message mentioning 'git repository' or 'work tree'
    so the caller knows what went wrong.

    Before the fix: raises CalledProcessError with stderr containing
    "unknown option \\`cached'" and "git diff --no-index". After fix: raises
    with a clear "not a git repository" / "not inside a work tree" message.
    """
    # tmp_path is a fresh directory with no `.git/` — git cannot find a repo.
    non_git_dir = tmp_path / "not-a-repo"
    non_git_dir.mkdir()

    with pytest.raises((subprocess.CalledProcessError, RuntimeError)) as exc_info:
        staging.staged_paths_for_push(
            non_git_dir, destination_url=str(tmp_path / "remote.git")
        )

    msg = str(exc_info.value) + " " + (
        exc_info.value.stderr if hasattr(exc_info.value, "stderr") and exc_info.value.stderr else ""
    )
    # The fix means we get a CLEAR error (mentioning git repository / work tree),
    # NOT the misleading "unknown option `cached`" --no-index fallback error.
    assert "unknown option" not in msg.lower(), (
        "Bug regression: the misleading 'unknown option `cached`' error from "
        "git's --no-index fallback is still bubbling up. The fix should catch "
        "this BEFORE the diff command runs."
    )
    # Positive: error must mention git/repository so caller knows what's wrong.
    assert (
        "git" in msg.lower()
        and ("repository" in msg.lower() or "work tree" in msg.lower() or "worktree" in msg.lower())
    ), f"error message should mention git repository/work tree; got: {msg!r}"


def test_staged_paths_for_push_handles_ahead_n_no_staged(temp_git_repo: Path):
    """The [ahead N, behind 0] state with NO staged files is the loop's happy path.

    Sequence:
      1. temp_git_repo fixture: seed commit already pushed to bare remote.
      2. Add 1 unpushed commit on top.
      3. Working tree clean (nothing staged in the index).
      4. staged_paths_for_push must return ['outputs/<date>/foo.md'] — i.e.
         it sees the unpushed-commit paths via `@{upstream}..HEAD`.

    Before the fix: identical to test_detects_files_in_commits_between_head_and_remote
    (already in test_staging_detection.py), but explicitly named to mirror
    the bug report shape. The guard must NEVER return [] for this state
    or it would let the unpushed commit slip past the path-policy check.
    """
    # Commit something locally without staging anything new in the index
    outputs_dir = temp_git_repo / "outputs" / "2026-05-20"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "tick.md").write_text("loop tick\n")
    _git(temp_git_repo, "add", "outputs/2026-05-20/tick.md")
    _git(temp_git_repo, "commit", "-m", "loop tick")

    # Sanity: working tree is clean, index is empty, but there's 1 unpushed commit
    status = _git(temp_git_repo, "status", "--porcelain=v1", "--branch").stdout
    assert "[ahead 1]" in status, f"setup wrong, expected [ahead 1] in: {status!r}"
    assert "outputs/" not in status.split("\n", 1)[-1], (
        f"setup wrong, working tree should be clean: {status!r}"
    )

    out = staging.staged_paths_for_push(
        temp_git_repo, destination_url=str(temp_git_repo.parent / "remote.git")
    )
    assert "outputs/2026-05-20/tick.md" in out, (
        f"the loop's ahead-N-no-staged happy path must detect unpushed-commit paths; got {out!r}"
    )


def test_staged_paths_for_push_handles_clean_tree_no_unpushed(temp_git_repo: Path):
    """A perfectly clean tree with NO unpushed commits returns []. No crash."""
    # Fixture is already in this state (seed commit pushed, nothing more)
    out = staging.staged_paths_for_push(
        temp_git_repo, destination_url=str(temp_git_repo.parent / "remote.git")
    )
    assert out == [], f"clean state must return []; got {out!r}"
