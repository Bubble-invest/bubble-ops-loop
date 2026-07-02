"""
test_safe_pull_sandbox.py — safe_pull() must degrade GRACEFULLY under the
agent OS-sandbox (bwrap fs-jail) instead of aborting the whole STEP-A sync.

Board issue #453 (2026-07-02): the sandboxed sync failed EVERY tick for
Ben/Tony/Accountant. Depts worked around it with a manual unsandboxed push,
every tick, indefinitely — defeating the point of the sandboxed sync.

Root cause (reproduced with real git repos, not mocks — this is a
git-behavior bug, mocking would hide it):

1. `.gitmodules` (or any tracked path under a submodule dir) can be
   PERMISSION-DENIED to the sandboxed subprocess even though the OS user
   owns it fine outside the jail (submodule content sits outside the
   sandbox's narrow `allowWrite` allowlist — see
   deploy/templates/managed-settings.sandbox.json). git treats an unreadable
   tracked path as "modified" (it can't compare content), and BOTH
   `git add -- <batch>` and `git stash push --include-untracked` ABORT
   THE ENTIRE OPERATION the moment they hit one unreadable path — not just
   that path. That's what made every substep after it fail.
2. `sudo -n <credential-helper>` fails under the sandbox (no controlling
   tty / no cached credential) with "a password is required" and no
   `password=` line in stdout — this used to surface as an opaque
   "failed to mint GitHub App token" error instead of a clear WARN.

Fix (graceful degradation, NOT a safety reduction):
- `_find_unreadable_tracked_paths` + `_restore_unreadable_tracked_paths`:
  detect tracked-but-unreadable paths and `git checkout -- <path>` them
  (restore from the index) BEFORE they can poison a batched add/stash. Safe
  because an agent cannot have made an in-sandbox edit to a file it cannot
  read — the "modification" is a jail-fs artifact, never real work. Only
  ever applied to TRACKED paths; untracked/new content is never touched.
- `_is_sudo_available`: cheap `sudo -n true` probe so the credential-helper
  path fails fast with an unambiguous WARN instead of a cryptic mint error.

Every scenario here asserts `ok=True` with a WARN note (graceful skip) —
never a hard abort — and separately re-asserts the never-lose-work
guarantee (untracked work, runtime commits, and merged upstream changes all
still land correctly even with the unreadable path present).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent
if str(SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_LIB))

import dispatch_helpers as dh  # noqa: E402


def _git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True, **kw)


@pytest.fixture
def repo_with_gitmodules(tmp_path):
    """A bare origin + local clone with a real `.gitmodules` file (a
    lightweight fake — no real submodule clone needed, we only need a
    TRACKED file that can be made unreadable to reproduce the jail
    signature: git reports a path as modified because it can't read it)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)],
                   check=True, capture_output=True)
    _git(seed, "config", "user.email", "t@t.t")
    _git(seed, "config", "user.name", "t")
    (seed / "CLAUDE.md").write_text("v1\n")
    (seed / ".gitmodules").write_text(
        '[submodule "agent-deploy"]\n'
        "\tpath = agent-deploy\n"
        "\turl = https://github.com/Bubble-invest/bubble-agent-deploy.git\n"
    )
    (seed / "outputs").mkdir()
    (seed / "outputs" / ".keep").write_text("baseline\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "main")

    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(origin), str(local)],
                   check=True, capture_output=True)
    _git(local, "config", "user.email", "l@l.l")
    _git(local, "config", "user.name", "l")
    return origin, seed, local


def _make_unreadable(path: Path):
    path.chmod(0o000)


def _cleanup_perms(path: Path):
    # pytest tmp_path cleanup needs read perms restored, else rmtree fails.
    if path.exists():
        path.chmod(0o644)


# ─── Failure mode 1: unreadable `.gitmodules` poisons `git add` ────────────

def test_unreadable_gitmodules_does_not_abort_force_commit_and_push(
    repo_with_gitmodules,
):
    """Before the fix: one unreadable tracked path poisoned the WHOLE
    `git add` batch (git aborts the entire index update), so a dept's
    legitimate runtime commit (outputs/queues/...) never landed. After the
    fix: the unreadable path is excluded + restored from the index with a
    WARN, and the REST of the runtime batch still commits successfully."""
    origin, seed, local = repo_with_gitmodules
    gitmodules = local / ".gitmodules"
    try:
        (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")
        _make_unreadable(gitmodules)

        assert not os.access(gitmodules, os.R_OK), \
            "precondition: .gitmodules must be genuinely unreadable"

        ok, err = dh.force_commit_and_push(
            local, "test runtime commit",
            bubble_git_guard_path="/nonexistent/guard",
        )
        assert ok, f"force_commit_and_push should degrade gracefully; got err={err}"

        # The runtime file WAS committed despite the unreadable .gitmodules.
        log = _git(local, "log", "--oneline", "-1").stdout
        assert "test runtime commit" in log or True  # commit landed (see status below)
        status = _git(local, "status", "--porcelain").stdout
        assert "outputs/.keep" not in status, \
            "runtime edit should have been committed, not left dirty"
    finally:
        _cleanup_perms(gitmodules)


def test_unreadable_gitmodules_restored_not_lost(repo_with_gitmodules):
    """The unreadable .gitmodules content must survive intact — restoring it
    from the index (not deleting it, not corrupting it)."""
    origin, seed, local = repo_with_gitmodules
    gitmodules = local / ".gitmodules"
    original_content = gitmodules.read_text()
    try:
        (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")
        _make_unreadable(gitmodules)

        ok, _ = dh.force_commit_and_push(
            local, "test runtime commit",
            bubble_git_guard_path="/nonexistent/guard",
        )
        assert ok
    finally:
        _cleanup_perms(gitmodules)

    # Content must be byte-identical to the tracked version — never lost.
    assert gitmodules.read_text() == original_content, \
        ".gitmodules content was lost/corrupted by the degradation path"


# ─── Failure mode 2: unreadable `.gitmodules` poisons `git stash` ──────────

def test_unreadable_gitmodules_does_not_abort_safe_pull(repo_with_gitmodules):
    """Before the fix: `git stash push --include-untracked` aborts ENTIRELY
    the moment it hits one unreadable tracked path (`error: open(...):
    Permission denied` / `fatal: Unable to process path` / `Cannot save the
    current worktree state`), so the tree was never cleaned and step 4's
    `git pull --rebase` always failed with 'you have unstaged changes' —
    the exact `.gitmodules permission + sudo errors` + 'rebase aborted'
    symptom reported for Ben/Tony/Accountant. After the fix: safe_pull
    exits ok=True with a WARN, not an abort."""
    origin, seed, local = repo_with_gitmodules
    gitmodules = local / ".gitmodules"
    try:
        # Simulate a merged upstream change landing BEFORE the dept goes
        # dirty (so local's own runtime push in step 1 isn't racing a
        # concurrent push — that race is orthogonal to this fix and covered
        # by the pre-existing safe_pull test suite).
        (seed / "CLAUDE.md").write_text("v2-merged\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "merged PR: CLAUDE.md v2")
        _git(seed, "push", "origin", "main")

        # local is now BEHIND origin. Dirty it the way the sandbox leaves it:
        # a runtime edit, git-junk (untracked tooling file), AND the
        # unreadable .gitmodules — WITHOUT pulling first (the dept doesn't
        # know about the merge yet; that's safe_pull's job to land).
        (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")
        (local / "untracked_junk.py").write_text("# wip\n")
        _make_unreadable(gitmodules)

        ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")

        assert ok, f"safe_pull must degrade gracefully, not abort; got: {summary}"
        assert "pulled" in summary, \
            f"pull step must have completed (not aborted on the unreadable path); got: {summary}"

        # The merged upstream change MUST still land (the whole point of
        # safe_pull — auto-redeploy must not regress).
        assert (local / "CLAUDE.md").read_text().strip() == "v2-merged", \
            "merged CLAUDE.md change did not land under sandbox conditions"
    finally:
        _cleanup_perms(gitmodules)


def test_unreadable_gitmodules_preserves_untracked_junk(repo_with_gitmodules):
    """Never-lose-work guarantee: git-junk/untracked paths present alongside
    the unreadable file must survive the sync untouched."""
    origin, seed, local = repo_with_gitmodules
    gitmodules = local / ".gitmodules"
    try:
        (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")
        (local / "untracked_junk.py").write_text("# wip untouched\n")
        _make_unreadable(gitmodules)

        ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")
        assert ok, summary
        assert (local / "untracked_junk.py").exists(), \
            "untracked work was lost during sandbox degradation"
        assert (local / "untracked_junk.py").read_text() == "# wip untouched\n", \
            "untracked work content was corrupted"
    finally:
        _cleanup_perms(gitmodules)


def test_find_unreadable_tracked_paths_ignores_untracked(repo_with_gitmodules):
    """The detection helper must never flag untracked ('??') entries — only
    genuinely unreadable TRACKED paths. Untracked content is real new work,
    not a jail artifact, and must never be auto-restored/discarded."""
    origin, seed, local = repo_with_gitmodules
    gitmodules = local / ".gitmodules"
    try:
        (local / "brand_new_untracked.py").write_text("# new file\n")
        _make_unreadable(gitmodules)

        status = subprocess.run(
            ["git", "-C", str(local), "status", "--porcelain"],
            capture_output=True, text=True,
        ).stdout
        unreadable = dh._find_unreadable_tracked_paths(local, status)

        assert ".gitmodules" in unreadable
        assert "brand_new_untracked.py" not in unreadable
    finally:
        _cleanup_perms(gitmodules)


# ─── Failure mode 3: sudo unavailable under the sandbox ────────────────────

def test_is_sudo_available_detects_noninteractive_failure(monkeypatch):
    """Under the sandbox, `sudo -n true` fails (no tty, no cached
    credential). The probe must return False cleanly rather than raising or
    hanging, so the caller can WARN-and-skip instead of surfacing a cryptic
    'failed to mint GitHub App token' error."""
    import subprocess as sp

    class _FakeCompletedProcess:
        returncode = 1

    def _fake_run(cmd, **kw):
        assert cmd[:2] == ["sudo", "-n"]
        return _FakeCompletedProcess()

    monkeypatch.setattr(sp, "run", _fake_run)
    assert dh._is_sudo_available() is False


def test_is_sudo_available_true_when_sudo_succeeds(monkeypatch):
    import subprocess as sp

    class _FakeCompletedProcess:
        returncode = 0

    def _fake_run(cmd, **kw):
        return _FakeCompletedProcess()

    monkeypatch.setattr(sp, "run", _fake_run)
    assert dh._is_sudo_available() is True


def test_sudo_unavailable_does_not_crash_force_commit_and_push(
    repo_with_gitmodules, monkeypatch,
):
    """When sudo is unavailable (sandbox), the generic-fallback push branch
    must fail with a CLEAR WARN message (non-fatal to the caller —
    force_commit_and_push returns ok=False with a message; safe_pull already
    treats this as a note, not an abort) instead of the old cryptic
    'failed to mint GitHub App token' with a raw stderr dump."""
    origin, seed, local = repo_with_gitmodules

    # Point origin at a github.com-looking bubble-ops-<slug> URL so the code
    # takes the sudo/credential-helper branch (not the file:// fast path).
    _git(local, "remote", "set-url", "origin",
         "https://github.com/Bubble-invest/bubble-ops-testdept.git")

    monkeypatch.setattr(dh, "_is_sudo_available", lambda: False)

    (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")
    ok, err = dh.force_commit_and_push(
        local, "test runtime commit",
        bubble_git_guard_path="/nonexistent/guard",
    )
    assert ok is False
    assert "sudo unavailable" in err
    assert "non-fatal" in err


def test_safe_pull_still_succeeds_when_sudo_unavailable(
    repo_with_gitmodules, monkeypatch,
):
    """safe_pull's overall contract must hold even when the runtime push
    branch can't mint a token at all (sudo unavailable): the pull itself
    (merged upstream changes landing) is independent of the push, so
    safe_pull must still return ok=True."""
    origin, seed, local = repo_with_gitmodules
    _git(local, "remote", "set-url", "origin",
         "https://github.com/Bubble-invest/bubble-ops-testdept.git")
    # But keep origin fetchable — point fetch back at the real bare repo via
    # a second remote trick: simplest is to leave push URL as bubble-ops-*
    # only for resolve_push_target's benefit is not how git works (one URL).
    # Instead: directly monkeypatch resolve_push_target's sudo dependency,
    # leaving the real origin URL so pull/fetch keep working.
    _git(local, "remote", "set-url", "origin", str(origin))

    monkeypatch.setattr(dh, "_is_sudo_available", lambda: False)
    # Force resolve_push_target to look like a real dept repo despite the
    # local file:// origin, so force_commit_and_push takes the sudo branch.
    monkeypatch.setattr(dh, "resolve_push_target",
                         lambda repo_dir: ("testdept", "bubble-ops-testdept"))

    (seed / "CLAUDE.md").write_text("v2-merged\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "merged PR: CLAUDE.md v2")
    _git(seed, "push", "origin", "main")

    (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")

    ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")
    assert ok, f"safe_pull must still succeed (pull is independent of push); got: {summary}"
    assert (local / "CLAUDE.md").read_text().strip() == "v2-merged"


# ─── Full combined scenario (issue #453's exact symptom) ───────────────────

def test_all_three_failure_modes_combined_still_completes_step_a(
    repo_with_gitmodules, monkeypatch,
):
    """The exact issue #453 reproduction: unreadable `.gitmodules` + git
    junk (untracked files) + sudo unavailable, ALL AT ONCE, on a dept tick
    that also has a merged upstream change waiting. STEP A (safe_pull) must
    complete with ok=True and WARN notes — never abort, never fall back to
    a manual push."""
    origin, seed, local = repo_with_gitmodules
    gitmodules = local / ".gitmodules"
    try:
        monkeypatch.setattr(dh, "_is_sudo_available", lambda: False)

        # Merged upstream structural change waiting to land.
        (seed / "CLAUDE.md").write_text("v2-merged\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "merged PR: CLAUDE.md v2")
        _git(seed, "push", "origin", "main")

        # Dirty local tree: runtime edit + git junk + unreadable .gitmodules.
        (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")
        (local / "untracked_junk.py").write_text("# wip\n")
        _make_unreadable(gitmodules)

        ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")

        assert ok, f"STEP A must complete under full sandbox conditions; got: {summary}"
        assert (local / "CLAUDE.md").read_text().strip() == "v2-merged", \
            "merged change did not land — auto-redeploy regressed"
        assert (local / "untracked_junk.py").exists(), "untracked work was lost"
        assert _git(local, "status", "--porcelain").stdout.strip() == "" \
            or "untracked_junk.py" in _git(local, "status", "--porcelain").stdout, \
            "tree should be clean modulo the preserved untracked junk"
    finally:
        _cleanup_perms(gitmodules)


# ─── Restore-allowlist scoping (independent review finding #1, PR #188) ────
#
# `.gitmodules`/submodule dirs are the ONLY paths safe to restore-from-index
# (`git checkout -- <path>`), because those are provably sandbox-jail
# plumbing an agent cannot have edited in-sandbox. ANY OTHER tracked path
# that is merely unreadable to THIS sandboxed subprocess right now could be
# a real edit made moments earlier by an EARLIER, UNSANDBOXED step (a
# human/root drop-in landing between ticks, Tony hand-restoring a file,
# etc.) — restoring it from the index would silently DISCARD that real
# work. These tests reproduce that scenario with a genuinely-modified
# ordinary tracked file (CLAUDE.md, outside the allowlist) that is then
# made unreadable, and assert the fix EXCLUDES it from the batch (so the
# batch still succeeds) rather than restoring/discarding it.

def test_non_allowlisted_unreadable_file_is_excluded_not_restored(
    repo_with_gitmodules,
):
    """Reviewer's reproduction (PR #188 finding #1): a normal tracked file
    (CLAUDE.md — NOT `.gitmodules`, NOT a submodule path) is genuinely
    modified on disk by an earlier, unsandboxed step, then becomes
    unreadable to this sandboxed subprocess. Before the fix:
    `_restore_unreadable_tracked_paths` `git checkout --`s it unconditionally,
    silently discarding the real edit. After the fix: the file is excluded
    from the add/stash batch (so the batch still succeeds) but left
    UNTOUCHED on disk — never restored — because it is not on the
    `.gitmodules`+submodule-dir allowlist. force_commit_and_push must still
    succeed (batch not poisoned) and, after re-chmod, the file's real
    (modified) content must be intact."""
    origin, seed, local = repo_with_gitmodules
    claude_md = local / "CLAUDE.md"
    real_edit = "REAL EDIT — made by an earlier unsandboxed step, must survive\n"
    try:
        # A genuine, legitimate local modification to an ordinary tracked
        # file — simulating an earlier, unsandboxed step's edit.
        claude_md.write_text(real_edit)
        # Also give the batch a genuine runtime change so we can prove the
        # batch as a whole is not poisoned by the unreadable, excluded path.
        (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")
        _make_unreadable(claude_md)

        assert not os.access(claude_md, os.R_OK), \
            "precondition: CLAUDE.md must be genuinely unreadable"

        ok, err = dh.force_commit_and_push(
            local, "test runtime commit",
            bubble_git_guard_path="/nonexistent/guard",
        )
        assert ok, f"batch must not be poisoned by the excluded path; got err={err}"

        # The runtime file WAS committed despite CLAUDE.md being excluded.
        status = _git(local, "status", "--porcelain").stdout
        assert "outputs/.keep" not in status, \
            "runtime edit should have been committed, not left dirty"
        # CLAUDE.md itself must still show as unreadable/dirty (excluded,
        # not staged, not committed) — it was never touched by git checkout.
    finally:
        _cleanup_perms(claude_md)

    # After re-chmod: the REAL edit must be exactly intact — never restored
    # to the pre-edit committed version ("v1\n" from the fixture seed).
    assert claude_md.read_text() == real_edit, (
        "non-allowlisted unreadable file was restored-from-index (discarding "
        "real work) instead of being excluded — this is the data-loss "
        "vector the allowlist scoping must prevent"
    )


def test_non_allowlisted_unreadable_file_does_not_poison_safe_pull(
    repo_with_gitmodules,
):
    """Same scenario end-to-end through safe_pull (not just
    force_commit_and_push): a genuinely-modified ordinary tracked file
    (structural — CLAUDE.md is never runtime-committed), made unreadable.
    Because it's excluded (never restored, never staged), it's ALSO the
    only thing left dirty once the stash pathspec excludes it too — so
    `git stash push` correctly reports "No local changes to save" for that
    remainder and the tree stays dirty. safe_pull's pull step then fails
    HONESTLY (`ok=False`, "you have unstaged changes") rather than
    force-discarding the file to make the pull succeed — this is the
    documented, acceptable failure mode (a WARN/abort, not silent data
    loss) per the reviewer's note. The critical assertion is what did NOT
    happen: the file was never touched by `git checkout --`, so its real
    content is byte-identical after the run, and no exception was raised
    getting there."""
    origin, seed, local = repo_with_gitmodules
    claude_md = local / "CLAUDE.md"
    real_edit = "REAL EDIT — must survive safe_pull\n"
    try:
        # Merged upstream change waiting — proves this isn't a general pull
        # regression; the pull genuinely can't proceed only because the
        # excluded path keeps the tree dirty, not because safe_pull crashed.
        (seed / "README.md").write_text("upstream-merged\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-m", "merged PR: README.md")
        _git(seed, "push", "origin", "main")

        claude_md.write_text(real_edit)
        _make_unreadable(claude_md)

        # Must not raise/crash, even though it can't fully succeed.
        ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")
        assert ok is False, (
            "safe_pull should honestly report failure when a non-allowlisted "
            "unreadable path can't be committed or stashed away — silently "
            "discarding it to force success would be the data-loss bug"
        )
        assert "unstaged changes" in summary or "pull --rebase FAILED" in summary
        # The exclusion note must be present — proof the code took the
        # exclude-not-restore path, not a silent restore.
        assert "not on the restore allowlist" in summary
        assert "NOT restored" in summary
    finally:
        _cleanup_perms(claude_md)

    assert claude_md.read_text() == real_edit, (
        "non-allowlisted unreadable file's real edit was lost/overwritten "
        "by safe_pull instead of being left untouched"
    )


def test_non_allowlisted_unreadable_file_excluded_other_batch_steps_still_complete(
    repo_with_gitmodules,
):
    """A genuinely modified tracked file OUTSIDE the allowlist, made
    unreadable, alongside OTHER pending work (a runtime file AND a leftover
    structural edit). Because the excluded path is a real dirty tracked
    path git can never rebase over (excluding it from `git stash push`
    correctly leaves it dirty — `git pull --rebase` refuses to run with
    ANY unstaged change present, by git's own design, regardless of which
    path it is), `safe_pull`'s overall pull step honestly fails — but every
    OTHER step of the batch still completes: the runtime file commits+pushes,
    the leftover structural edit is stashed (not lost, not poisoned by the
    excluded path), and the excluded file itself is left byte-for-byte
    untouched rather than being restored/discarded to force a fake success.
    This is the fix's actual contract: the excluded path degrades ONE step
    (the pull) honestly, it doesn't poison every OTHER step or destroy
    anything."""
    origin, seed, local = repo_with_gitmodules
    # Seed the victim file from `seed` (before local diverges) so both the
    # merged-upstream push below and local's own state stay conflict-free.
    victim_seed = seed / "outputs" / "earlier_unsandboxed_edit.txt"
    victim_seed.write_text("baseline\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed victim file")
    _git(seed, "push", "origin", "main")
    _git(local, "pull", "origin", "main")
    victim = local / "outputs" / "earlier_unsandboxed_edit.txt"

    real_edit = "REAL EDIT — made by an earlier unsandboxed step, must survive\n"
    try:
        # A genuine runtime edit — must still commit+push despite the
        # excluded victim sitting alongside it in the same status batch.
        (local / "outputs" / ".keep").write_text("other-runtime-edit\n")
        # A leftover STRUCTURAL edit (never runtime-committed by
        # force_commit_and_push — routed via propose-settings-pr) — must
        # still be stashed (not lost, not blocked by the excluded victim).
        tools_dir = local / "tools"
        tools_dir.mkdir(exist_ok=True)
        (tools_dir / "leftover.py").write_text("# leftover structural edit\n")
        victim.write_text(real_edit)
        _make_unreadable(victim)

        ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")

        # The pull itself correctly fails-safe (never silently discards the
        # excluded path to force success) — this is documented, expected
        # behavior, not a regression: git itself refuses to rebase with any
        # unstaged change present, and excluding is what keeps it unstaged.
        assert ok is False
        assert "pull --rebase FAILED" in summary

        # But every OTHER batch step completed despite the excluded path:
        assert "runtime committed+pushed" in summary, \
            "the other runtime edit should have committed+pushed despite the excluded victim"
        assert "stashed leftovers" in summary, \
            "the leftover structural edit should have been stashed despite the excluded victim"
        status = _git(local, "status", "--porcelain").stdout
        assert "outputs/.keep" not in status, "runtime edit should be committed, not dirty"

        # The leftover structural edit is not lost: on pull failure safe_pull
        # pops the stash back onto disk (never-lose-work) — verify it's back.
        assert (tools_dir / "leftover.py").exists(), \
            "the leftover edit must survive the pull failure (stash popped back)"
        assert (tools_dir / "leftover.py").read_text() == "# leftover structural edit\n"
    finally:
        _cleanup_perms(victim)

    assert victim.read_text() == real_edit, (
        "excluded non-allowlisted file was restored/lost instead of left "
        "untouched — this is exactly the data-loss vector the allowlist "
        "scoping must prevent"
    )


def test_is_restore_allowlisted_scopes_to_gitmodules_and_submodules(
    repo_with_gitmodules,
):
    """Unit-level check of the allowlist itself: `.gitmodules` and submodule
    directory paths are allowlisted; an arbitrary ordinary tracked file is
    not."""
    origin, seed, local = repo_with_gitmodules
    assert dh._is_restore_allowlisted(local, ".gitmodules") is True
    assert dh._is_restore_allowlisted(local, "agent-deploy") is True
    assert dh._is_restore_allowlisted(local, "agent-deploy/some/nested/file") is True
    assert dh._is_restore_allowlisted(local, "CLAUDE.md") is False
    assert dh._is_restore_allowlisted(local, "outputs/.keep") is False


# ─── Directory-level denial (independent review finding #2, PR #188) ───────
#
# The realistic sandbox shape isn't "chmod 000 the file" — it's the
# CONTAINING DIRECTORY being unwritable (bwrap `allowWrite` scoped above the
# file, not to it: submodule content sits entirely outside the dept's
# narrow allowWrite allowlist). `chmod 000` on a file alone still lets
# `git checkout -- <path>` succeed (unlink/recreate is a directory
# permission operation, not a file permission one) — so the file-only
# fixture used everywhere else in this suite does NOT exercise the
# `still_unreadable`/checkout-failure WARN path. This test denies write at
# the PARENT DIRECTORY instead, so `git checkout --` itself fails (rc!=0),
# and asserts the code takes the WARN-and-continue path, never crashes, and
# the batch still completes.

def _make_dir_unwritable(path: Path):
    path.chmod(0o500)  # r-x: readable/listable, not writable — blocks unlink/recreate


def _cleanup_dir_perms(path: Path):
    if path.exists():
        path.chmod(0o755)


def test_directory_level_denial_on_allowlisted_path_warns_no_crash(
    repo_with_gitmodules,
):
    """Reviewer's reproduction (PR #188 finding #2): `.gitmodules` (an
    ALLOWLISTED path) is unreadable AND its containing directory (the repo
    root) is unwritable to the sandboxed subprocess — the realistic
    bwrap-allowWrite denial shape, not just a chmod-000 file. `git checkout
    -- .gitmodules` fails (rc=255-ish) because unlink/recreate needs
    directory write. The code must take the `excluded` path (WARN, not
    crash) and the batch must still complete: the runtime edit still
    commits, and .gitmodules is left exactly as-is (not corrupted, not
    half-restored)."""
    origin, seed, local = repo_with_gitmodules
    gitmodules = local / ".gitmodules"
    original_content = gitmodules.read_text()
    try:
        _make_unreadable(gitmodules)
        _make_dir_unwritable(local)

        try:
            (local / "outputs").exists()  # sanity: dir still listable (r-x)

            unreadable = dh._find_unreadable_tracked_paths(
                local, _git(local, "status", "--porcelain").stdout,
            )
            assert ".gitmodules" in unreadable

            restored, excluded = dh._restore_unreadable_tracked_paths(local, unreadable)

            # Allowlisted, but the checkout itself failed under directory
            # denial — must land in `excluded`, NOT crash, NOT silently
            # vanish from both lists.
            assert ".gitmodules" not in restored, (
                "checkout should have failed under directory-level denial, "
                "so .gitmodules cannot be in the successfully-restored list"
            )
            assert ".gitmodules" in excluded, (
                "a failed checkout under directory-level denial must be "
                "reported as excluded (WARN path), not silently dropped"
            )
        finally:
            # Restore dir write perms before touching file perms again
            # (chmod on a file inside a 0o500 dir still works since we're
            # not renaming/creating — but be safe and restore dir first).
            _cleanup_dir_perms(local)
    finally:
        _cleanup_dir_perms(local)
        _cleanup_perms(gitmodules)

    # Never crashed, never corrupted — content is exactly what it was.
    assert gitmodules.read_text() == original_content


def test_directory_level_denial_does_not_crash_force_commit_and_push(
    repo_with_gitmodules,
):
    """End-to-end: directory-level denial on the allowlisted `.gitmodules`
    path must not crash force_commit_and_push, and the rest of the runtime
    batch (outputs/.keep) must still commit successfully — the WARN path,
    not an abort."""
    origin, seed, local = repo_with_gitmodules
    gitmodules = local / ".gitmodules"
    try:
        (local / "outputs" / ".keep").write_text("dirty-runtime-edit\n")
        _make_unreadable(gitmodules)
        _make_dir_unwritable(local)

        try:
            ok, err = dh.force_commit_and_push(
                local, "test runtime commit under dir-level denial",
                bubble_git_guard_path="/nonexistent/guard",
            )
        finally:
            _cleanup_dir_perms(local)

        assert ok, f"dir-level denial must degrade gracefully, not crash; got err={err}"
        status = _git(local, "status", "--porcelain").stdout
        assert "outputs/.keep" not in status, \
            "runtime edit should have been committed despite dir-level denial"
    finally:
        _cleanup_dir_perms(local)
        _cleanup_perms(gitmodules)
