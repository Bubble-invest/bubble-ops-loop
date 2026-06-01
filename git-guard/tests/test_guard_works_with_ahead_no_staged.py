"""Regression: guard must not crash when invoked from a non-git directory
or in the [ahead N, behind 0] no-staged-files state.

Bug discovered 2026-05-20 when Joris's main agent tried to push a patched
CLAUDE.md via the guard from his home directory. The guard's
`staged_paths_for_push()` calls `git diff --cached --name-only -z`; when
git cannot find a repo (cwd is OUTSIDE any worktree, or `--git-dir` not
discoverable), git silently falls back to `--no-index` mode where the
`--cached` option does not exist:

    $ cd /tmp && git diff --cached --name-only -z
    error: unknown option `cached'
    usage: git diff --no-index [<options>] <path> <path>
    (exit 129)

The current code raises a CalledProcessError with this stderr buried in
it, killing the entire push flow even though the actual problem is "you
ran me from the wrong cwd".

What the user reported as the symptom: a push from a repo with a local
commit but no staged files failed. The root cause is identical — if the
guard's --repo-dir resolves to something git cannot interpret as a repo
(missing `.git/`, or cwd outside any worktree), the FIRST git command
fails with this misleading "no-index" error rather than a clear
"not a git repository" message.

Fix: validate we are inside a git repo BEFORE running any diff/log
command, and emit a clear error message if not. We use
`git rev-parse --is-inside-work-tree` which is the canonical guard
(exits 0 inside a repo, 128 outside).

Also covered: the [ahead N, behind 0] state with NO staged files —
i.e. a clean working tree but unpushed commits. This is the exact
shape of the loop's normal happy path (commits land on HEAD as the
loop ticks, then the guard is asked to push them). The fix must not
regress that path.
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
        staging.staged_paths_for_push(non_git_dir)

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

    out = staging.staged_paths_for_push(temp_git_repo)
    assert "outputs/2026-05-20/tick.md" in out, (
        f"the loop's ahead-N-no-staged happy path must detect unpushed-commit paths; got {out!r}"
    )


def test_staged_paths_for_push_handles_clean_tree_no_unpushed(temp_git_repo: Path):
    """A perfectly clean tree with NO unpushed commits returns []. No crash."""
    # Fixture is already in this state (seed commit pushed, nothing more)
    out = staging.staged_paths_for_push(temp_git_repo)
    assert out == [], f"clean state must return []; got {out!r}"


def test_currently_staged_rejects_non_git_directory(tmp_path: Path):
    """Same guard for the lower-level `currently_staged()` helper."""
    non_git_dir = tmp_path / "outside-any-repo"
    non_git_dir.mkdir()

    with pytest.raises((subprocess.CalledProcessError, RuntimeError)) as exc_info:
        staging.currently_staged(non_git_dir)

    msg = str(exc_info.value) + " " + (
        exc_info.value.stderr if hasattr(exc_info.value, "stderr") and exc_info.value.stderr else ""
    )
    assert "unknown option" not in msg.lower(), (
        "currently_staged() must also fail loud BEFORE git's --no-index fallback fires"
    )
