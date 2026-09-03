"""
test_720_safepull_mtime_wedge.py — board card #720.

DEFECT: `safe_pull()` (scripts/lib/dispatch_helpers.py) is meant to be
dirty-tree-proof, but a specific wedge slips past it: a live process bumps a
TRACKED file's mtime with its CONTENT unchanged. `git status`/`git diff
HEAD` correctly report the tree clean, yet `git pull --rebase` (safe_pull's
step 4) aborts with "Your local changes ... would be overwritten" or
"Entry '<path>' not uptodate. Cannot merge." — and even `git reset --hard`
/ `git update-index --really-refresh`, the usual unwedge incantations, hit
the same signature. Left alone this permanently wedges the dept's sync.

FIX: a TIGHTLY-GUARDED fallback (`_recover_mtime_wedge`, wired into
safe_pull's step 4 via `_parse_wedge_stuck_paths`) that only ever rewrites a
stuck path from origin when it has PROVEN doing so discards nothing real:
content == HEAD or == origin for every stuck path, AND HEAD carries no
commit origin doesn't already have (merge-base-confirmed — a repo-wide
guard, since the remedy is a repo-wide `git reset --hard`). Any path that
fails either check → REFUSE, touch nothing.

This suite uses REAL git temp repos throughout (same style as
test_safe_pull.py) — only the single `git pull --rebase origin main`
subprocess call is intercepted, to deterministically inject the exact wedge
error text git is known to emit for this condition (reproducing the actual
git-internal race non-flakily is not practical in CI; the injected text is
the literal signature `_parse_wedge_stuck_paths` must recognize). Every
other git operation — the stash, the fetch, the guard's content/ancestor
checks, the rm+reset recovery itself, and the retried pull — runs against a
real repository, so the recovery (or refusal) is exercised for real.
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
def origin_and_local(tmp_path):
    """A bare origin + a working clone, with one committed tracked file."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(origin), str(seed)],
                   check=True, capture_output=True)
    _git(seed, "config", "user.email", "t@t.t")
    _git(seed, "config", "user.name", "t")
    (seed / "wedged.txt").write_text("stable-content\n")
    (seed / "CLAUDE.md").write_text("v1\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "origin", "main")

    local = tmp_path / "local"
    subprocess.run(["git", "clone", str(origin), str(local)],
                   check=True, capture_output=True)
    _git(local, "config", "user.email", "l@l.l")
    _git(local, "config", "user.name", "l")

    return origin, seed, local


def _make_pull_wedge_injector(monkeypatch, repo_dir: Path, stderr: str):
    """Monkeypatch subprocess.run so the SINGLE `git -C <repo_dir> pull
    --quiet --rebase origin main` invocation returns a synthetic failed
    CompletedProcess carrying `stderr` (the injected wedge signature) — the
    first time it is called. Every other subprocess.run call (including any
    RETRY of that same pull command later, and every other git op safe_pull
    or the recovery fallback makes) passes straight through to the real
    subprocess.run, so the rest of the flow is exercised for real."""
    real_run = subprocess.run
    state = {"intercepted": False}

    def _fake_run(cmd, *args, **kwargs):
        # Match on command SHAPE, not the repo_dir string: safe_pull resolves
        # repo_dir via Path(...).resolve() internally, which can differ
        # textually from the fixture's path (e.g. macOS /tmp -> /private/tmp
        # symlink resolution) even though it's the same directory.
        is_pull = (
            not state["intercepted"]
            and len(cmd) >= 3
            and cmd[0] == "git"
            and cmd[1] == "-C"
            and cmd[3:] == ["pull", "--quiet", "--rebase", "origin", "main"]
        )
        if is_pull:
            state["intercepted"] = True
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr=stderr,
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)


# ─── (a) recovery case: reproduces the wedge, asserts RECOVERY ─────────────

def test_mtime_wedge_recovers_when_content_provably_safe(
    origin_and_local, monkeypatch,
):
    """The wedge signature: `wedged.txt`'s content on disk is byte-identical
    to HEAD (a `touch`-only mtime bump — the real #720 reproduction; content
    is untouched, `git diff HEAD -- wedged.txt` is empty, `git status` is
    clean), yet the injected pull failure carries the exact 'not uptodate'
    text. The guard must recover: rm + reset --hard origin/main, restoring
    wedged.txt clean, and safe_pull must still succeed end-to-end (including
    landing an unrelated merged upstream change)."""
    origin, seed, local = origin_and_local

    # The actual #720 mechanism: bump mtime only, content unchanged.
    target = local / "wedged.txt"
    original_mtime = target.stat().st_mtime
    target.touch()
    assert target.stat().st_mtime >= original_mtime
    assert target.read_text() == "stable-content\n"
    # Precondition asserted exactly as the card describes: clean per git.
    status = _git(local, "status", "--porcelain")
    assert status.stdout.strip() == "", "precondition: status must be clean"
    diff_head = _git(local, "diff", "HEAD", "--", "wedged.txt")
    assert diff_head.stdout == "", "precondition: diff HEAD must be empty"

    # A genuine, unrelated merged upstream change the pull must still land.
    (seed / "CLAUDE.md").write_text("v2-merged\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "merged PR: CLAUDE.md v2")
    _git(seed, "push", "origin", "main")

    _make_pull_wedge_injector(
        monkeypatch, local,
        stderr=(
            "error: Entry 'wedged.txt' not uptodate. Cannot merge.\n"
        ),
    )

    ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")

    assert ok, f"safe_pull should recover and succeed; got: {summary}"
    assert "mtime-wedge fallback engaged" in summary, summary
    assert "wedged.txt" in summary, summary
    # The wedge is cleared — file still has its stable content.
    assert (local / "wedged.txt").read_text() == "stable-content\n"
    # The unrelated merged upstream change landed too (sync actually completed).
    assert (local / "CLAUDE.md").read_text().strip() == "v2-merged"


def test_mtime_wedge_recovers_via_overwritten_by_checkout_signature(
    origin_and_local, monkeypatch,
):
    """Same recovery, but exercising the OTHER git message form safe_pull's
    `pull --rebase` step is actually more likely to emit (a rebase replays
    via checkout internally): 'local changes ... would be overwritten by
    checkout'. `_parse_wedge_stuck_paths` must recognize this form too."""
    origin, seed, local = origin_and_local
    target = local / "wedged.txt"
    target.touch()

    _make_pull_wedge_injector(
        monkeypatch, local,
        stderr=(
            "error: Your local changes to the following files would be "
            "overwritten by checkout:\n"
            "\twedged.txt\n"
            "Please commit your changes or stash them before you switch "
            "branches.\nAborting\n"
        ),
    )

    ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")

    assert ok, f"safe_pull should recover and succeed; got: {summary}"
    assert "mtime-wedge fallback engaged" in summary, summary
    assert (local / "wedged.txt").read_text() == "stable-content\n"


# ─── (b) refusal case: genuine local/unpushed content → NO rm/reset ────────

def test_mtime_wedge_refuses_when_content_genuinely_diverges(
    origin_and_local,
):
    """Directly exercises `_recover_mtime_wedge`'s content guard (unit
    boundary, not the full safe_pull flow): if disk content genuinely
    diverges from BOTH HEAD and origin, `git status`/`git diff HEAD` would
    already show it dirty — meaning safe_pull's OWN step 1
    (force_commit_and_push) commits+pushes it before step 4 ever runs, so
    this exact path can never legitimately reach the fallback through the
    normal safe_pull flow. That's precisely why the fallback re-verifies
    content itself rather than trusting the caller's stuck-path list: if it
    is EVER handed a path whose content isn't provably == HEAD or == origin
    (a stale list, a race, a caller bug), it must REFUSE — no rm, no reset
    --hard, no HEAD move — never destroy real content it can't prove is
    safe."""
    origin, seed, local = origin_and_local

    real_local_edit = "REAL UNCOMMITTED LOCAL EDIT — must never be lost\n"
    (local / "wedged.txt").write_text(real_local_edit)
    head_before = _git(local, "rev-parse", "HEAD").stdout.strip()

    ok, msg = dh._recover_mtime_wedge(local, ["wedged.txt"], default_branch="main")

    assert not ok, f"guard must REFUSE, not recover; got ok=True, msg: {msg}"
    assert "REFUSED mtime-wedge fallback" in msg, msg
    assert "wedged.txt" in msg, msg
    # The guard must not have touched anything: HEAD unmoved, content intact.
    assert _git(local, "rev-parse", "HEAD").stdout.strip() == head_before, \
        "REFUSED fallback must never move HEAD"
    assert (local / "wedged.txt").read_text() == real_local_edit, \
        "REFUSED fallback must never rm/overwrite the real local edit — DATA LOSS"


def test_mtime_wedge_refuses_when_head_has_unpushed_commit(
    origin_and_local, monkeypatch,
):
    """`wedged.txt` itself is clean (content == HEAD, matching the wedge
    precondition), but HEAD carries an UNPUSHED local commit on a DIFFERENT
    file (e.g. step 1's runtime push failed a moment earlier this same
    tick). A path-scoped-only check would wrongly allow the repo-wide
    `reset --hard origin/main` here and silently discard that unrelated
    commit — the guard's repo-wide merge-base check must catch this and
    REFUSE instead."""
    origin, seed, local = origin_and_local

    # A genuine unpushed local commit on an unrelated file.
    (local / "unpushed_runtime.txt").write_text("important unpushed work\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-m", "local commit never pushed (simulated push failure)")
    head_before = _git(local, "rev-parse", "HEAD").stdout.strip()

    # wedged.txt itself is untouched/clean — only its mtime is bumped.
    (local / "wedged.txt").touch()

    _make_pull_wedge_injector(
        monkeypatch, local,
        stderr=(
            "error: Entry 'wedged.txt' not uptodate. Cannot merge.\n"
        ),
    )

    ok, summary = dh.safe_pull(local, bubble_git_guard_path="/nonexistent/guard")

    assert not ok, (
        f"safe_pull must REFUSE (unrelated unpushed commit on HEAD), not "
        f"silently reset past it; got ok=True, summary: {summary}"
    )
    assert "REFUSED mtime-wedge fallback" in summary, summary
    assert "unpushed commit" in summary, summary
    # Nothing touched: HEAD unmoved, the unpushed commit's file still there.
    assert _git(local, "rev-parse", "HEAD").stdout.strip() == head_before, \
        "REFUSED fallback must never move HEAD (would discard the unpushed commit)"
    assert (local / "unpushed_runtime.txt").read_text() == "important unpushed work\n", \
        "REFUSED fallback must never destroy an unrelated unpushed commit — DATA LOSS"


def test_parse_wedge_stuck_paths_ignores_unrelated_failures():
    """A generic pull failure (no wedge signature) must yield [] — this is
    the switch that keeps the fallback from ever engaging on an ordinary
    pull error (network, real conflict, auth, etc.)."""
    assert dh._parse_wedge_stuck_paths(
        "fatal: unable to access 'https://...': Could not resolve host"
    ) == []
    assert dh._parse_wedge_stuck_paths(
        "CONFLICT (content): Merge conflict in foo.txt\n"
        "error: could not apply abc123... some commit message"
    ) == []
    assert dh._parse_wedge_stuck_paths(None) == []
    assert dh._parse_wedge_stuck_paths("") == []
