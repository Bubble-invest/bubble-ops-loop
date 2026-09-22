"""
test_1452_guard_abs_repo_dir.py — regression for board #1452.

Bug (root-caused during the 2026-09-22 L4 debrief): `force_commit_and_push`
committed fine but its guarded push ALWAYS failed in production with:

    DENIED: git error: not inside a git work tree: .
    fatal: not a git repository

because it passed a RELATIVE `--repo-dir` straight through to the
`bubble-git-guard` subprocess. Unlike the `git -C <repo_dir>` calls in this
same function (which resolve a relative path against the CURRENT process
cwd every time git runs), the separate `bubble-git-guard` binary is not
guaranteed to see the same cwd, so a relative `--repo-dir` silently pointed
at the wrong tree — even though the earlier `git status`/`add`/`commit`
calls (also relative, via `-C`) had just succeeded against the right one.

Fix: `force_commit_and_push` now resolves `repo_dir` to an ABSOLUTE path
(`Path(repo_dir).resolve()`) as its very first step, before any subprocess
call — including the guard invocation. That's a no-op when the caller's
cwd already IS the repo root (Chesterton's fence: `Path('.').resolve()`
from the repo root == the repo root), so existing callers that happen to
run from the repo root see identical behavior.

These tests prove:
  1. the guard is invoked with an ABSOLUTE `--repo-dir` even when the
     process cwd at call time is NOT the repo root and the caller passes a
     relative path (the exact shape of the production bug — the operator's
     workaround comment gave `--repo-dir /srv/agents/maya`, i.e. absolute);
  2. a well-formed push (guard doctrine path, dirty tree) still returns
     ok=True in that scenario — the fix doesn't just change the argument,
     it makes the guarded push actually usable from a non-repo-root cwd;
  3. the literal reported bug shape — `repo_dir='.'`, cwd already at the
     repo root — resolves to that same absolute repo root (Chesterton's
     fence: behavior unchanged for the caller shape that "presumably
     worked").
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent
for p in (str(SCRIPTS_LIB),):
    if p not in sys.path:
        sys.path.insert(0, p)

import dispatch_helpers as dh  # noqa: E402


def _arm_guard_doctrine_path(monkeypatch):
    """Make force_commit_and_push take its guarded (bubble-git-guard) push
    branch: dept slug resolvable + guard binary present + per-dept policy
    file present. Mirrors test_dispatch_retry_and_push.py's helper of the
    same name (kept local here so this regression file has no cross-test
    import dependency)."""
    monkeypatch.setattr(dh, "resolve_push_target",
                        lambda repo_dir: ("tony", "bubble-ops-tony"))
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/local/bin/bubble-git-guard")
    _real_exists = Path.exists

    def _fake_exists(self):
        if str(self).endswith("tony-policy.yaml"):
            return True
        return _real_exists(self)

    monkeypatch.setattr(Path, "exists", _fake_exists)


def _fake_run_factory(calls):
    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=" M outputs/2026-09-22/4/risk-brief.md\n",
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return fake_run


def _guard_repo_dir_arg(calls):
    guard_calls = [c for c in calls if "bubble-git-guard" in " ".join(c)]
    assert guard_calls, calls
    call = guard_calls[0]
    idx = call.index("--repo-dir")
    return call[idx + 1]


def test_guard_gets_absolute_repo_dir_when_cwd_is_not_repo_root(
    tmp_path, monkeypatch,
):
    """Reproduces the production shape: the process cwd is some OTHER
    directory (not the repo root), and the caller passes a relative
    repo_dir (relative to that cwd). Pre-fix, the guard would receive that
    same relative string verbatim and fail with 'not inside a git work
    tree'. Post-fix, the guard must receive the fully-resolved absolute
    path — and the push must still succeed."""
    _arm_guard_doctrine_path(monkeypatch)

    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    repo_root = workdir / "myrepo"
    repo_root.mkdir()

    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(calls))
    monkeypatch.chdir(workdir)

    # cwd (workdir) != repo_root: this is the "process cwd is NOT the repo
    # root" case, with a relative repo_dir resolved against that cwd.
    assert Path.cwd() != repo_root
    ok, err = dh.force_commit_and_push(repo_dir="myrepo", message="test")

    assert ok is True, err
    passed = _guard_repo_dir_arg(calls)
    assert Path(passed).is_absolute(), (passed, calls)
    assert Path(passed) == repo_root.resolve()
    # Never the raw relative token the bug used to pass through.
    assert passed != "myrepo"
    assert passed != "."


def test_guard_still_gets_absolute_repo_dir_for_relative_dot_from_repo_root(
    tmp_path, monkeypatch,
):
    """Chesterton's fence: the exact literal shape from the bug report
    (`repo_dir='.'`) when the caller's cwd already IS the repo root must
    keep working — and now also resolves to an absolute path rather than
    the bare '.' that triggered 'not inside a git work tree: .'."""
    _arm_guard_doctrine_path(monkeypatch)

    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()

    calls = []
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(calls))
    monkeypatch.chdir(repo_root)

    ok, err = dh.force_commit_and_push(repo_dir=".", message="test")

    assert ok is True, err
    passed = _guard_repo_dir_arg(calls)
    assert Path(passed).is_absolute(), (passed, calls)
    assert Path(passed) == repo_root.resolve()
    assert passed != "."
