"""Board #1551 — `_push_repo`'s commit must never depend on ambient git
identity (local OR global).

Root cause: `dispatch_directives.py --remote-delivery` (ISOLATED FLOOR MODE,
#606) commits into `_clone_remote_repo`'s fresh, throwaway `git clone` —
a directory `bootstrap-dept.sh` never touches, so it starts with NO local
git identity. Since #1120 (per-agent uid isolation) the uid this relay runs
as (agent-tony) has no GLOBAL git user.name/user.email configured either, so
`git commit` failed outright: "git commit failed: Author identity unknown"
(FAIL deliver accountant-20260926-01 -> accountant).

This test reproduces the "no identity anywhere" environment directly (repo
with no local identity, HOME pointing at an empty dir so no global
~/.gitconfig, and no GIT_AUTHOR_*/GIT_COMMITTER_* env leaking in from the
test runner) and asserts `_push_repo`'s commit still succeeds, with the
expected (fleet-standard, `bootstrap-dept.sh`-matching) bot author.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dispatch_directives as dd  # noqa: E402


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
    )


def _make_repo_no_identity(root: Path, name: str) -> Path:
    """A repo with NO local git identity configured — mirrors a fresh
    `_clone_remote_repo` throwaway clone, never touched by bootstrap-dept.sh."""
    repo = root / name
    repo.mkdir(parents=True)
    r = _git(repo, "init", "-q")
    assert r.returncode == 0, r.stderr
    # Deliberately do NOT set user.name/user.email here (the whole point).
    (repo / "README.md").write_text("x")
    return repo


def test_commit_succeeds_with_no_identity_anywhere(tmp_path, monkeypatch):
    # No global ~/.gitconfig: point HOME at a brand-new, empty dir.
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    monkeypatch.setenv("HOME", str(empty_home))
    # Belt-and-braces: strip any GIT_AUTHOR_*/GIT_COMMITTER_* env and system
    # config the test process itself might be running under, so the repro is
    # genuinely "no identity anywhere", not just "no HOME identity".
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Token minting is expected to fail in this offline test; skip production
    # backoff delays because this case only verifies the commit identity.
    monkeypatch.setattr(dd, "_mint_token", lambda *_args: None)
    monkeypatch.setattr(dd, "_sleep_before_transport_retry", lambda *_args: None)

    repo = _make_repo_no_identity(tmp_path, "bubble-ops-accountant")

    # NOTE: whether a bare, identity-less `git commit` here fails outright
    # ("Author identity unknown", board #1551's exact VPS failure) or falls
    # back to a guessed identity with just a warning depends on the local
    # git build/OS account (e.g. GECOS full name) — not portable enough to
    # assert as a sanity check. What matters, and IS portable, is that
    # `_push_repo` never relies on that guess either way: it sets identity
    # explicitly, so its commit always lands with the exact expected author.

    # Exercise the actual delivery path: _push_repo's commit must
    # succeed anyway, because it sets identity explicitly per invocation.
    ok, detail = dd._push_repo(
        repo_dir=repo,
        repo_name="bubble-ops-accountant",
        message="queues/management: deliver directive-20260926-01",
        dry_run=False,
        paths=["README.md"],
    )
    # ok may still end up False downstream (no real GitHub credential
    # helper/token available in a test env — _mint_token fails safe and
    # _push_repo reports that as its own, separate failure) but the COMMIT
    # itself — the thing #1551 broke — must have landed.
    if not ok:
        assert "could not mint token" in detail or "push rejected" in detail, (
            f"_push_repo failed for an unexpected (non-token/push) reason: {detail!r}"
        )

    log = _git(repo, "log", "-1", "--format=%an\t%ae\t%s")
    assert log.returncode == 0, log.stderr
    author_name, author_email, subject = log.stdout.strip().split("\t", 2)
    assert author_name == dd._BOT_NAME
    assert author_email == dd._BOT_EMAIL
    assert subject == "queues/management: deliver directive-20260926-01"


def test_push_repo_never_relies_on_ambient_identity_config():
    """The `-c user.name=/-c user.email=` flags must be present on the
    commit invocation itself (not merely "set somewhere") — a regression
    that silently drops them would reintroduce #1551 even if some other
    part of the fleet happens to configure identity in the meantime."""
    import inspect
    src = inspect.getsource(dd._push_repo)
    assert "user.name=" in src
    assert "user.email=" in src
    assert dd._BOT_NAME and dd._BOT_EMAIL
