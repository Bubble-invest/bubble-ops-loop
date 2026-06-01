"""Detect what would actually be pushed by `git push`.

The guard needs to know the COMPLETE set of paths that will land on the
remote — both files staged in the working tree AND files in commits that
haven't been pushed yet. If we only looked at `git diff --cached`, the
attacker could `git commit` a structural file FIRST, then call the guard
with only innocuous staged files, and the structural file would slip
through on push.

Two git commands cover the picture:
  (a) `git diff --cached --name-only` — staged in the index, not yet committed
  (b) `git diff @{upstream}..HEAD --name-only` — committed locally, not yet pushed
      (if no upstream is configured, we fall back to `git log HEAD --name-only`
      collecting every changed path on the current branch — fail-LOUD-and-CLOSED
      rather than missing a path).

Notion v4 §"GitHub access model" line 725: paths are enforced LOCALLY.
This module is half of that enforcement.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List


def _run_git(repo_dir: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """Run a git command in `repo_dir`. Returns the CompletedProcess.

    Does NOT raise on non-zero — callers inspect returncode. We want graceful
    handling of "no upstream configured" (exit 128) for fresh repos.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_inside_work_tree(repo_dir: Path) -> None:
    """Fail loud if `repo_dir` is not inside a git work tree.

    Why this exists: when `repo_dir` does NOT live inside any git repo
    (e.g. the caller passed a wrong --repo-dir, or the cwd is on a path
    where no `.git/` ancestor exists), `git diff --cached --name-only`
    silently falls back to `--no-index` mode where the `--cached` flag
    is unrecognized. The resulting error message is:

        error: unknown option `cached'
        usage: git diff --no-index [<options>] <path> <path>

    That stderr is then buried inside a CalledProcessError, masking the
    actual root cause ("you're not in a git repo"). This guard short-
    circuits with a clear message BEFORE any diff/log command runs.

    Bug discovered 2026-05-20 when the main agent ran the guard from a
    non-repo cwd to push a CLAUDE.md patch (see Step-11 robustness
    report). The original CalledProcessError did surface — but its
    message ("unknown option `cached'") is so misleading that future
    callers will waste time hunting for a code bug instead of fixing
    their cwd.
    """
    proc = _run_git(repo_dir, "rev-parse", "--is-inside-work-tree")
    # git emits "true\n" on stdout when inside, exits 128 otherwise
    if proc.returncode != 0 or proc.stdout.strip() != "true":
        # Use CalledProcessError so existing callers that catch it keep
        # working, but craft a clear message instead of letting the
        # downstream --no-index fallback poison the diagnostics.
        clear_stderr = (
            f"not inside a git work tree: {repo_dir!s}\n"
            f"(git rev-parse --is-inside-work-tree exited {proc.returncode}, "
            f"stdout={proc.stdout.strip()!r}, "
            f"stderr={proc.stderr.strip()!r})"
        )
        raise subprocess.CalledProcessError(
            proc.returncode or 128,
            ["git", "rev-parse", "--is-inside-work-tree"],
            proc.stdout,
            clear_stderr,
        )


def currently_staged(repo_dir: Path) -> List[str]:
    """Return the list of paths currently staged in the index.

    Equivalent to `git diff --cached --name-only -z`. Returns [] if nothing
    is staged. Raises CalledProcessError if `repo_dir` isn't a git repo
    (with a CLEAR error message — see `_assert_inside_work_tree` for the
    backstory on the bug this fixes).
    """
    _assert_inside_work_tree(repo_dir)
    proc = _run_git(repo_dir, "diff", "--cached", "--name-only", "-z")
    if proc.returncode != 0:
        # Not a git repo, or some other hard error
        raise subprocess.CalledProcessError(
            proc.returncode, ["git", "diff", "--cached"], proc.stdout, proc.stderr
        )
    if not proc.stdout:
        return []
    return [p for p in proc.stdout.split("\x00") if p]


def _unpushed_commit_paths(repo_dir: Path) -> List[str]:
    """Return paths touched by commits on the current branch that aren't on
    the configured upstream yet.

    Strategy:
      1. Try `git diff @{upstream}..HEAD --name-only -z`.
      2. If that fails (no upstream configured), fall back to
         `git log --name-only --pretty=format: HEAD` to cover the entire
         branch history — fail-CLOSED by being inclusive.
    """
    # Attempt 1: upstream-aware diff
    proc = _run_git(repo_dir, "diff", "@{upstream}..HEAD", "--name-only", "-z")
    if proc.returncode == 0:
        return [p for p in proc.stdout.split("\x00") if p]

    # Attempt 2: no upstream — collect everything on this branch
    proc = _run_git(repo_dir, "log", "--name-only", "--pretty=format:", "HEAD")
    if proc.returncode != 0:
        # Genuine git failure — be loud, don't pretend the set is empty
        raise subprocess.CalledProcessError(
            proc.returncode, ["git", "log"], proc.stdout, proc.stderr
        )
    paths = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return paths


def staged_paths_for_push(repo_dir: Path) -> List[str]:
    """Return the deduped union of staged-in-index + unpushed-commit paths.

    This is the SET-TO-CHECK before invoking the broker. If any element of
    this set is denied by policy, the entire push is denied (atomicity).
    """
    staged = currently_staged(repo_dir)
    unpushed = _unpushed_commit_paths(repo_dir)
    seen: set = set()
    out: List[str] = []
    for p in list(staged) + list(unpushed):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out
