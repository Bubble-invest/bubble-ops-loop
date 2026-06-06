"""
test_safe_pull.py — safe_pull() must land MERGED upstream changes even when the
local working tree is DIRTY, without losing the agent's work.

{{OPERATOR}} msg 3979 (2026-06-06): "build an auto redeploy" — Option A. The loop's
`git pull --quiet --rebase` failed on a dirty tree ("cannot pull with rebase")
so merged structural PRs (CLAUDE.md, skills) never reached the box. safe_pull
commits runtime → stashes leftovers → pulls → restores, so merge == live.

These use REAL git temp repos (origin + local clone) — the bug is a git-behavior
bug, mocking it would hide it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_helpers as dh  # noqa: E402


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def origin_and_local(tmp_path, monkeypatch):
    """A bare origin + a working clone, with one commit on main."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)],
                   check=True, capture_output=True)
    _git(seed, "config", "user.email", "t@t.t")
    _git(seed, "config", "user.name", "t")
    (seed / "CLAUDE.md").write_text("v1\n")
    (seed / "outputs").mkdir()
    (seed / "outputs" / ".keep").write_text("")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "main")

    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(origin), str(local)],
                   check=True, capture_output=True)
    _git(local, "config", "user.email", "l@l.l")
    _git(local, "config", "user.name", "l")

    # Simulate a MERGED upstream change to a structural file (CLAUDE.md).
    _git(seed, "pull", "origin", "main")
    (seed / "CLAUDE.md").write_text("v2-merged\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "merged PR: CLAUDE.md v2")
    _git(seed, "push", "origin", "main")

    # Neutralize the runtime push (no broker in tests): make
    # force_commit_and_push a local-commit-only no-network op by pointing the
    # guard/cred paths nowhere → it falls to the bare `git push origin main`,
    # which works against our local bare origin.
    return origin, local


def test_safe_pull_lands_merged_change_despite_dirty_tree(origin_and_local):
    origin, local = origin_and_local
    # Dirty the local tree the way Tony's was: an UNSTAGED change to a TRACKED
    # file (the real blocker — git refuses `pull --rebase` on unstaged tracked
    # changes) + an untracked tooling file. Use a runtime file (outputs/*) so
    # force_commit_and_push will actually commit it (CLAUDE.md is structural and
    # would be stashed instead — also fine, but this exercises the commit path).
    (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")  # tracked, modified
    (local / "untracked_tool.py").write_text("# wip\n")
    # Sanity: a plain rebase pull WOULD fail here (unstaged tracked change).
    plain = subprocess.run(
        ["git", "-C", str(local), "pull", "--rebase", "origin", "main"],
        capture_output=True, text=True)
    assert plain.returncode != 0, "precondition: dirty tracked file should block plain pull"

    ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")

    assert ok, f"safe_pull should succeed; got: {summary}"
    # The merged upstream change MUST now be on disk (auto-redeploy worked).
    assert (local / "CLAUDE.md").read_text().strip() == "v2-merged", \
        "merged CLAUDE.md change did not land"
    # The agent's untracked file must NOT have been destroyed.
    assert (local / "untracked_tool.py").exists(), "untracked work was lost"


def test_safe_pull_noop_clean_tree_still_pulls(origin_and_local):
    origin, local = origin_and_local
    ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")
    assert ok, summary
    assert (local / "CLAUDE.md").read_text().strip() == "v2-merged"
