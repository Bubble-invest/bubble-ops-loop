"""
test_safe_pull.py — safe_pull() must land MERGED upstream changes even when the
local working tree is DIRTY, without losing the agent's work.

Joris msg 3979 (2026-06-06): "build an auto redeploy" — Option A. The loop's
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


def test_safe_pull_preserves_outputs_when_upstream_merge_deletes_them(
    tmp_path, monkeypatch,
):
    """
    Regression test for Maya's 2026-06-15 data-loss bug.

    Reproduced mechanism (NOT a rebase-conflict — a fast-forward overwrite):

    1. Subagent writes output files, safe_pull commits + pushes them to
       origin/main (commit A with outputs/2026-06-15/4/*).
    2. A human merges a PR whose branch was forked BEFORE commit A.
       The merge commit B on origin/main effectively DELETES the output
       files because the PR branch never had them.
    3. Next tick: safe_pull runs.  The tree is clean (outputs already
       committed), so step 1 is a no-op.  Step 3's ``git pull --rebase``
       fast-forwards to B — and the output files are GONE from the
       working tree because B deleted them.
    4. The output files are not in any branch, dangling commit, or stash.
       Unrecoverable without an external copy (Notion logbook).

    This test recreates the exact sequence with real git repos.
    """
    # ── Build a bare origin + seed clone ──
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True, capture_output=True,
    )
    seed = tmp_path / "seed"
    subprocess.run(
        ["git", "clone", str(origin), str(seed)],
        check=True, capture_output=True,
    )
    _git(seed, "config", "user.email", "t@t.t")
    _git(seed, "config", "user.name", "t")
    (seed / "CLAUDE.md").write_text("v1\n")
    (seed / "outputs").mkdir()
    (seed / "outputs" / ".keep").write_text("baseline\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "main")

    # ── Clone the dept ──
    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", str(origin), str(local)],
        check=True, capture_output=True,
    )
    _git(local, "config", "user.email", "l@l.l")
    _git(local, "config", "user.name", "l")

    # ── Phase 1: Subagent writes outputs → safe_pull commits + pushes ──
    today_dir = local / "outputs" / "2026-06-15" / "4"
    today_dir.mkdir(parents=True, exist_ok=True)
    (today_dir / "risk-brief.md").write_text("# Risk Brief\nContent here\n")
    (today_dir / "risk-kpis.yaml").write_text("kpi: 42\n")
    (today_dir / "management-export.yaml").write_text("export: data\n")
    (today_dir / "summary.md").write_text("summary\n")
    (today_dir / ".last-run").write_text("2026-06-15T19:43:00Z\n")
    # Also dirty a tracked runtime file
    (local / "outputs" / ".keep").write_text("local-outputs-v1\n")

    # First safe_pull call: this commits + pushes the outputs
    ok, summary = dh.safe_pull(
        local, bubble_git_guard_path="/nonexistent/guard",
    )
    assert ok, f"first safe_pull should succeed; got: {summary}"
    # Verify the outputs were pushed (they exist in origin)
    # Pull origin into seed to check
    _git(seed, "pull", "origin", "main")
    assert (seed / "outputs" / "2026-06-15" / "4" / "risk-brief.md").exists(), \
        "outputs should be on origin after first push"

    # ── Phase 2: A human merges a PR that deletes the outputs ──
    # The PR branch was forked before Phase 1, so it never had the output
    # files.  When merged, it effectively deletes them from main.
    import shutil
    shutil.rmtree(seed / "outputs" / "2026-06-15", ignore_errors=True)
    (seed / "CLAUDE.md").write_text("v2-merged-from-PR\n")
    # Also update the tracked runtime file
    (seed / "outputs" / ".keep").write_text("pr-version\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "merged PR: composer-fix (overwrites outputs/)")
    _git(seed, "push", "origin", "main")

    # ── Phase 3: Next tick's safe_pull — this is where data loss happens ──
    # The local tree is clean (outputs already committed in Phase 1).
    # safe_pull step 1: no-op (nothing to commit).
    # safe_pull step 3: git pull --rebase → FAST-FORWARD to the merge
    # commit that DELETED the output files.
    ok, summary = dh.safe_pull(
        local, bubble_git_guard_path="/nonexistent/guard",
    )

    assert ok, f"second safe_pull should succeed; got: {summary}"

    # The merged upstream change MUST land (auto-redeploy).
    assert (local / "CLAUDE.md").read_text().strip() == "v2-merged-from-PR", \
        "merged CLAUDE.md change did not land"

    # ═══ THE REGRESSION ASSERTIONS ═══
    # Every subagent output file MUST survive the sync.
    assert (today_dir / "risk-brief.md").exists(), \
        "L4 output risk-brief.md was DESTROYED by safe_pull fast-forward"
    assert (today_dir / "risk-kpis.yaml").exists(), \
        "L4 output risk-kpis.yaml was DESTROYED by safe_pull fast-forward"
    assert (today_dir / "management-export.yaml").exists(), \
        "L4 output management-export.yaml was DESTROYED by safe_pull fast-forward"
    assert (today_dir / "summary.md").exists(), \
        "L4 output summary.md was DESTROYED by safe_pull fast-forward"
    assert (today_dir / ".last-run").exists(), \
        "L4 output .last-run was DESTROYED by safe_pull fast-forward"

    # Content integrity: the restored files must have the same content.
    assert (today_dir / "risk-brief.md").read_text() == "# Risk Brief\nContent here\n", \
        "L4 output risk-brief.md content was corrupted"
    assert (today_dir / "risk-kpis.yaml").read_text() == "kpi: 42\n", \
        "L4 output risk-kpis.yaml content was corrupted"
    assert (today_dir / "management-export.yaml").read_text() == "export: data\n", \
        "L4 output management-export.yaml content was corrupted"
    assert (today_dir / "summary.md").read_text() == "summary\n", \
        "L4 output summary.md content was corrupted"
    assert (today_dir / ".last-run").read_text() == "2026-06-15T19:43:00Z\n", \
        "L4 output .last-run content was corrupted"
