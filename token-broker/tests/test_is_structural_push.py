"""Tests for deploy/is-structural-push.py — the mission-file-lock detector.

These build REAL temp git repos (local bare "remote" + clone) and exercise the
exact decision the root credential helper makes: exit 0 (structural -> read-only
token) vs exit 1 (non-structural / undetectable -> write token).

Run: python3 -m pytest token-broker/tests/test_is_structural_push.py -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # bubble-ops-loop/
SCRIPT = REPO_ROOT / "token-broker" / "deploy" / "is-structural-push.py"
POLICY_PY = REPO_ROOT / "token-broker" / "src" / "policy.py"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(cwd),  # isolate from user gitconfig
        "PATH": __import__("os").environ.get("PATH", ""),
    }
    return subprocess.run(["git", *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, check=True)


def _run_detector(repo_dir: Path) -> int:
    """Return the detector's exit code (0=structural, 1=not)."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-dir", str(repo_dir),
         "--policy-py", str(POLICY_PY), "--verbose"],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode


@pytest.fixture
def repo_with_upstream(tmp_path: Path):
    """A clone of a local bare remote, with an initial pushed commit.

    Yields (clone_dir). The clone has an upstream so @{upstream}..HEAD works.
    """
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(bare))
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init")
    _git(clone, "remote", "add", "origin", str(bare))
    # initial commit on a non-structural file, push so upstream is set
    (clone / "README.md").write_text("hi\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "init")
    _git(clone, "branch", "-M", "main")
    _git(clone, "push", "-u", "origin", "main")
    return clone


def test_non_structural_staged_returns_write(repo_with_upstream: Path):
    """outputs/** staged but not committed -> not structural -> exit 1 (write)."""
    repo = repo_with_upstream
    p = repo / "outputs" / "2026-06-01" / "1"
    p.mkdir(parents=True)
    (p / "summary.md").write_text("ok\n")
    _git(repo, "add", "-A")
    assert _run_detector(repo) == 1


def test_structural_staged_returns_readonly(repo_with_upstream: Path):
    """layers/1/PROMPT.md staged -> structural -> exit 0 (read-only)."""
    repo = repo_with_upstream
    p = repo / "layers" / "1"
    p.mkdir(parents=True)
    (p / "PROMPT.md").write_text("the IPO topic\n")
    _git(repo, "add", "-A")
    assert _run_detector(repo) == 0


def test_structural_committed_not_pushed_returns_readonly(repo_with_upstream: Path):
    """Commit a mission file (the incident: commit then plain push). Caught via
    @{upstream}..HEAD -> exit 0."""
    repo = repo_with_upstream
    (repo / "dept.yaml").write_text("department:\n  slug: x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "edit mission")
    assert _run_detector(repo) == 0


def test_hide_structural_behind_innocuous_commit_returns_readonly(repo_with_upstream: Path):
    """Commit structural in commit 1, innocuous in commit 2, push both.
    Diff is @{upstream}..HEAD so BOTH are seen -> exit 0."""
    repo = repo_with_upstream
    (repo / "missions" / "x.yaml").parent.mkdir(parents=True)
    (repo / "missions" / "x.yaml").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "sneaky mission")
    (repo / "outputs").mkdir()
    (repo / "outputs" / "z.md").write_text("b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "innocuous")
    assert _run_detector(repo) == 0


def test_mixed_staged_returns_readonly(repo_with_upstream: Path):
    """A structural + a non-structural staged together -> ANY structural -> 0."""
    repo = repo_with_upstream
    (repo / "outputs").mkdir()
    (repo / "outputs" / "a.md").write_text("a\n")
    (repo / "skills").mkdir()
    (repo / "skills" / "s.md").write_text("s\n")
    _git(repo, "add", "-A")
    assert _run_detector(repo) == 0


def test_structural_deletion_returns_readonly(repo_with_upstream: Path):
    """Deleting a mission file is also a mutation -> structural -> 0."""
    repo = repo_with_upstream
    (repo / "tools").mkdir()
    (repo / "tools" / "t.py").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add tool")
    _git(repo, "push")  # now tools/t.py is on upstream
    (repo / "tools" / "t.py").unlink()
    _git(repo, "add", "-A")
    assert _run_detector(repo) == 0


def test_nothing_staged_returns_write(repo_with_upstream: Path):
    """Clean tree, nothing to push -> nothing structural -> exit 1 (write)."""
    assert _run_detector(repo_with_upstream) == 1


def test_not_a_repo_returns_write(tmp_path: Path):
    """cwd not a git repo (e.g. clone from parent dir) -> can't detect -> exit 1."""
    d = tmp_path / "plain"
    d.mkdir()
    assert _run_detector(d) == 1


def test_fresh_branch_no_upstream_structural_returns_readonly(tmp_path: Path):
    """Brand-new repo, no upstream, first commit is a mission file.
    Fail-CLOSED for detection (whole-branch scan) -> exit 0."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    _git(repo, "init")
    (repo / "layers" / "1").mkdir(parents=True)
    (repo / "layers" / "1" / "PROMPT.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "first")
    assert _run_detector(repo) == 0


def test_fresh_branch_no_upstream_nonstructural_returns_write(tmp_path: Path):
    """Brand-new repo, no upstream, first commit is non-structural -> exit 1."""
    repo = tmp_path / "fresh2"
    repo.mkdir()
    _git(repo, "init")
    (repo / "outputs").mkdir()
    (repo / "outputs" / "a.md").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "first")
    assert _run_detector(repo) == 1


def test_working_memory_file_is_not_structural(repo_with_upstream: Path):
    """WORKING_MEMORY.md (Part A) must be writable -> NOT structural -> exit 1.
    This is the escape valve: the agent CAN push transient topics here."""
    repo = repo_with_upstream
    (repo / "WORKING_MEMORY.md").write_text("- IPO Watch: SpaceX, Anthropic\n")
    _git(repo, "add", "-A")
    assert _run_detector(repo) == 1
