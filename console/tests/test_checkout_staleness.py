"""test_checkout_staleness.py — board #1299.

The console reads dept content straight off the on-disk checkout
(`repo_path(slug)`), no caching of its own. For a `host: vps` dept (e.g.
Maya) that checkout IS the dept's own live working tree — nothing re-clones
or auto-pulls it for the console. If the dept's own `/loop` tick (which
normally reconciles to origin on every wake) stalls for a stretch, the
checkout can sit days behind origin/main while the console keeps faithfully
rendering whatever is on disk (the exact incident: a stale `prospect_email`
draft body served on /dept/maya).

`github_reader.checkout_staleness()` is a READ-ONLY two-SHA comparison
(local `git rev-parse HEAD` vs. GitHub's `commits/<branch>` via `gh api`) —
it must never pull/fetch/reset the checkout, and it must degrade to `None`
(never raise, never claim "not stale") on any failure so the page always
renders.
"""
from __future__ import annotations

import subprocess

import pytest

from console.services import github_reader

# Captured BEFORE any test monkeypatches `console.services.github_reader.subprocess.run`
# (which patches the shared `subprocess` module object, not just that one import site).
# Fakes below must call THIS, never `subprocess.run` directly, or a "fall through to
# the real git call" branch recurses into itself forever.
_REAL_RUN = subprocess.run


def _init_git_repo(path, sha_env_override=None):
    """Create a minimal git repo at `path` with one commit; return its HEAD sha."""
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *args: _REAL_RUN(  # noqa: E731
        ["git", "-C", str(path), *args],
        check=True, capture_output=True, text=True,
    )
    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (path / "README.md").write_text("x", encoding="utf-8")
    run("add", "README.md")
    run("commit", "-q", "-m", "initial")
    head = _REAL_RUN(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return head


def _fake_gh_sha(sha: str, rc: int = 0):
    def fake_run(cmd, *a, **k):
        if cmd[0] == "gh":
            class R:
                returncode = rc
                stdout = sha + "\n" if rc == 0 else ""
                stderr = "" if rc == 0 else "boom"
            return R()
        # anything else (git) falls through to the REAL subprocess (never the
        # monkeypatched one — see _REAL_RUN comment above).
        return _REAL_RUN(cmd, *a, **k)
    return fake_run


def test_none_when_repo_not_on_disk(monkeypatch):
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: None)
    assert github_reader.checkout_staleness("nope") is None


def test_none_when_not_a_git_repo(tmp_path, monkeypatch):
    repo = tmp_path / "bubble-ops-plain"
    repo.mkdir()
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    assert github_reader.checkout_staleness("plain") is None


def test_none_when_gh_api_fails(tmp_path, monkeypatch):
    repo = tmp_path / "bubble-ops-maya"
    head = _init_git_repo(repo)
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    monkeypatch.setattr(
        "console.services.github_reader.subprocess.run",
        _fake_gh_sha("", rc=1),
    )
    assert github_reader.checkout_staleness("maya") is None


def test_none_when_gh_raises(tmp_path, monkeypatch):
    """Network hiccup / timeout must never propagate — fail to None."""
    repo = tmp_path / "bubble-ops-maya"
    _init_git_repo(repo)
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)

    def raising_run(cmd, *a, **k):
        if cmd[0] == "gh":
            raise subprocess.TimeoutExpired(cmd, 8)
        return _REAL_RUN(cmd, *a, **k)

    monkeypatch.setattr(
        "console.services.github_reader.subprocess.run", raising_run)
    assert github_reader.checkout_staleness("maya") is None


def test_none_when_gh_returns_null_sha(tmp_path, monkeypatch):
    """Review finding (#1299): `gh api ... --jq .sha` on an unexpected/empty
    API body (e.g. a nonexistent branch) can print a truthy `"null"` with
    rc=0 — that must NOT be treated as a real sha (it would register as a
    bogus "stale" the moment it's compared, since "null" != local_sha)."""
    repo = tmp_path / "bubble-ops-maya"
    _init_git_repo(repo)
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    monkeypatch.setattr(
        "console.services.github_reader.subprocess.run",
        _fake_gh_sha("null"),  # rc=0, non-empty, but not a real sha
    )
    assert github_reader.checkout_staleness("maya") is None


def test_not_stale_when_shas_match(tmp_path, monkeypatch):
    repo = tmp_path / "bubble-ops-maya"
    head = _init_git_repo(repo)
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    monkeypatch.setattr(
        "console.services.github_reader.subprocess.run",
        _fake_gh_sha(head),
    )
    result = github_reader.checkout_staleness("maya")
    assert result == {
        "stale": False, "local_sha": head, "remote_sha": head, "branch": "main",
    }


def test_stale_when_local_head_behind_remote(tmp_path, monkeypatch):
    """The board #1299 scenario: origin/main has moved on, the checkout hasn't."""
    repo = tmp_path / "bubble-ops-maya"
    _init_git_repo(repo)
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    newer_remote_sha = "f" * 40
    monkeypatch.setattr(
        "console.services.github_reader.subprocess.run",
        _fake_gh_sha(newer_remote_sha),
    )
    result = github_reader.checkout_staleness("maya")
    assert result is not None
    assert result["stale"] is True
    assert result["remote_sha"] == newer_remote_sha


def test_branch_defaults_to_checkouts_own_default_branch(tmp_path, monkeypatch):
    """Board #1299 review follow-up: don't hardcode "main" — a dept repo
    could have a different (or renamed) default branch. Resolve it from the
    checkout's own `origin` remote, same hazard `sync-local-dept-clones.sh`'s
    `resolve_canonical_branch` already guards against."""
    repo = tmp_path / "bubble-ops-maya"
    head = _init_git_repo(repo)
    _REAL_RUN(["git", "-C", str(repo), "branch", "-m", "trunk"], check=True)
    # Fake a bare "origin" remote pointing at this same repo so `remote show
    # origin` can report a HEAD branch without a real network fetch.
    _REAL_RUN(["git", "-C", str(repo), "remote", "add", "origin", str(repo)],
               check=True)
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)

    captured = {}

    def fake_run(cmd, *a, **k):
        if cmd[0] == "gh":
            captured["api_path"] = cmd[2]
            class R:
                returncode = 0
                stdout = head + "\n"
                stderr = ""
            return R()
        return _REAL_RUN(cmd, *a, **k)

    monkeypatch.setattr(
        "console.services.github_reader.subprocess.run", fake_run)
    result = github_reader.checkout_staleness("maya")
    assert result is not None
    assert result["branch"] == "trunk", (
        f"expected the resolved branch 'trunk', got {result['branch']!r}")
    assert "/commits/trunk" in captured["api_path"]


def test_never_pulls_or_mutates_the_checkout(tmp_path, monkeypatch):
    """Regression guard: must be pure read (rev-parse only) — never fetch/pull/reset."""
    repo = tmp_path / "bubble-ops-maya"
    head = _init_git_repo(repo)
    monkeypatch.setattr(github_reader, "repo_path", lambda slug: repo)
    seen_git_args = []

    def fake_run(cmd, *a, **k):
        if cmd[0] == "git":
            seen_git_args.append(cmd)
            return _REAL_RUN(cmd, *a, **k)
        return _fake_gh_sha(head)(cmd, *a, **k)

    monkeypatch.setattr(
        "console.services.github_reader.subprocess.run", fake_run)
    github_reader.checkout_staleness("maya")
    mutating = {"fetch", "pull", "reset", "clean", "checkout", "rebase"}
    for args in seen_git_args:
        assert not (mutating & set(args)), f"mutating git call: {args}"
