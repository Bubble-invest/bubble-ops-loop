"""RED/GREEN fixture for board #1123.

dispatch_directives.py writes an untracked directive file into a CHILD
dept's live repo, unlocked, then calls `_push_repo` (status -> add -> commit
-> push). If the child's OWN live/backup tick independently runs
`safe_pull`'s `git stash push --include-untracked` in the write->status
window, the untracked file gets swept into a stash: `_push_repo` then sees a
CLEAN tree and reports (True, "nothing to commit") -- dispatch() treats that
as delivered and marks the source directive `dispatched` in the manager's
repo, even though the file is now orphaned in the child's stash, not
committed anywhere.

This test does NOT mock `_push_repo` (unlike test_dispatch_directives.py's
`world` fixture) -- it exercises the REAL git status/add/commit path (with
the network push itself monkeypatched away, since that needs a GitHub App
token) and injects the race by literally running `git stash -u` on the
child repo between dispatch()'s file-write and its call into `_push_repo`.

Guards under test (board #1123):
  1. LOCK: dispatch() must take the child's own ops-loop-<slug>.tick.lock
     for the write+push. If a "live session" (this test) is already holding
     it, dispatch() must SKIP the directive for this tick rather than write
     unlocked into a tree that session can stash from under it.
  2. VERIFY: even if the write+push race happens outside the lock window
     (defense in depth / lock not yet wired in prod), dispatch() must not
     trust `_push_repo`'s "nothing to commit" without confirming via
     `git show HEAD:<path>` that the file actually landed.
"""
import fcntl
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dispatch_directives as dd  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                           capture_output=True, text=True)


def _make_repo(root: Path, slug: str) -> Path:
    repo = root / f"bubble-ops-{slug}"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _drop(tony, did, **fields):
    p = tony / dd._OUTBOUND_REL / f"directive-{did}.yaml"
    base = {"directive_id": did, "target_dept": "maya",
            "approved_by": "operator", "status": "approved",
            "body": "Prioritise the Charlie-Finance segment this week."}
    base.update(fields)
    p.write_text(yaml.safe_dump(base))
    return p


@pytest.fixture()
def world(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    tony = _make_repo(root, "tony")
    maya = _make_repo(root, "maya")
    (tony / dd._OUTBOUND_REL).mkdir(parents=True)
    lock_dir = tmp_path / "lock"
    monkeypatch.setenv("BUBBLE_BACKUP_LOCK_DIR", str(lock_dir))

    # Never actually mint a token / hit the network: the push itself always
    # "succeeds" once we've committed locally -- exactly like the real
    # network push would if it got that far. This isolates the test to the
    # status/add/commit race + the lock/verify guards, which is what #1123
    # is actually about.
    monkeypatch.setattr(dd, "_mint_token", lambda repo_name: "ghs_fake")

    def fake_run_for_push(cmd, cwd=None, stdin=None, env=None):
        if "push" in cmd:
            class R:  # noqa: N801 - tiny stand-in for CompletedProcess
                returncode = 0
                stdout = ""
                stderr = ""
            return R()
        return subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, input=stdin, env=env,
            capture_output=True, text=True,
        )

    monkeypatch.setattr(dd, "_run", fake_run_for_push)
    return root, tony, maya, lock_dir


def _hold_child_lock(lock_dir: Path, slug: str):
    """Simulate the child's OWN live/backup tick already holding its lock
    (as loop-backup.sh's run_backup_tick / the live --channels session would
    via the SAME LOCK_DIR/ops-loop-<slug>.tick.lock path)."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"ops-loop-{slug}.tick.lock"
    fh = open(lock_path, "a+")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh  # caller must keep this open for the duration of the race


def test_race_directive_not_lost_when_child_stashes_concurrently(world, monkeypatch):
    """Core #1123 repro: simulate the child's safe_pull stashing the
    untracked directive file in the write->push window. Before the fix this
    made _push_repo report (True, 'nothing to commit') and dispatch() marked
    the directive 'dispatched' with the file orphaned in the child's stash.
    After the fix, dispatch() must either (a) skip via the lock and retry
    next tick, or (b) detect via git-show verification that the file never
    landed and mark it FAILED -- either way it must NOT report the directive
    delivered while the file is not actually committed in the child repo."""
    root, tony, maya, lock_dir = world
    _drop(tony, "d1")

    orig_push_repo = dd._push_repo

    def racy_push_repo(repo_dir, repo_name, message, dry_run, paths=None):
        # Between dispatch()'s write and its call into _push_repo, the
        # child's OWN live session runs safe_pull's autostash -- sweeping
        # our just-written untracked file out of the working tree. Only the
        # CHILD repo (maya) has an independent live session racing it in
        # real life -- the manager (tony) push is us, not a race partner.
        if Path(repo_dir) == maya:
            subprocess.run(
                ["git", "-C", str(repo_dir), "stash", "push", "--include-untracked",
                 "-m", "safe_pull: pre-rebase autostash"],
                capture_output=True, text=True,
            )
        # Forward `paths` only if the caller (dispatch()) actually passed
        # one -- lets this wrapper work unmodified against BOTH the pre-fix
        # _push_repo (no `paths` param at all, always did a blanket `add
        # -A`) and the post-fix one (requires explicit `paths`).
        if paths is None:
            return orig_push_repo(repo_dir, repo_name, message, dry_run)
        return orig_push_repo(repo_dir, repo_name, message, dry_run, paths=paths)

    monkeypatch.setattr(dd, "_push_repo", racy_push_repo)

    rc = dd.dispatch(root, "tony", dry_run=False)
    assert rc == 0  # dispatch() itself is never fatal

    dest = maya / dd._INBOX_REL / "directive-d1.yaml"
    src = yaml.safe_load((tony / dd._OUTBOUND_REL / "directive-d1.yaml").read_text())

    landed_in_head = _git(
        maya, "show", "HEAD:" + str(dest.relative_to(maya)), check=False,
    ).returncode == 0

    if src["status"] == "dispatched":
        # If we claim delivery, the file MUST actually be committed.
        assert landed_in_head, (
            "board #1123 DATA LOSS: directive marked 'dispatched' but the "
            "file never landed in the child repo's HEAD (orphaned in its "
            "stash) -- CEO directive silently lost."
        )
    else:
        # Otherwise it must still be 'approved' (retryable next tick), NOT
        # silently dropped in some other status.
        assert src["status"] == "approved"


def test_lock_held_by_child_skips_and_retries(world):
    """If the child's tick lock is already held (its own live/backup tick is
    mid-run), dispatch() must SKIP that directive this tick (idempotent --
    still 'approved') rather than write into its tree unlocked."""
    root, tony, maya, lock_dir = world
    _drop(tony, "d2")

    held = _hold_child_lock(lock_dir, "maya")
    try:
        rc = dd.dispatch(root, "tony", dry_run=False)
        assert rc == 0
        dest = maya / dd._INBOX_REL / "directive-d2.yaml"
        assert not dest.exists(), (
            "board #1123: dispatch() wrote into maya's repo while maya's own "
            "tick lock was held -- the cross-repo write is not lock-guarded."
        )
        src = yaml.safe_load((tony / dd._OUTBOUND_REL / "directive-d2.yaml").read_text())
        assert src["status"] == "approved", "must remain retryable, not silently dropped"
    finally:
        held.close()

    # Lock released -> a subsequent tick delivers normally.
    rc = dd.dispatch(root, "tony", dry_run=False)
    assert rc == 0
    dest = maya / dd._INBOX_REL / "directive-d2.yaml"
    assert dest.exists(), "directive should deliver once the child's lock is free"
