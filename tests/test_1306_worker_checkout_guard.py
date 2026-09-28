"""Worker preflight rejects managed checkouts without changing their state."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "worker-checkout-guard.py"


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("worker_checkout_guard", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", [
    "/home/claude/bubble-ops-loop", "/home/claude/bubble-ops-loop/scripts",
    "/opt/bubble-ops-loop", "/srv/agents", "/srv/agents/rnd/repo",
])
def test_live_paths_refused(guard, path):
    assert guard.is_managed_checkout(Path(path))


@pytest.mark.parametrize("path", [
    "/home/claude/worktrees/issue-1306", "/tmp/codex-worker.123/repo",
    "/home/claude/bubble-ops-loop-copy", "/srv/agents-copy",
])
def test_isolated_paths_allowed(guard, path):
    assert not guard.is_managed_checkout(Path(path))


def test_symlink_and_parent_traversal(guard, tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (live / "sub").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(live, target_is_directory=True)
    guard.MANAGED_ROOTS = (live,)
    assert guard.is_managed_checkout(alias / "sub" / "..")
    assert not guard.is_managed_checkout(live / ".." / "isolated")


def test_cli_refuses_before_mutation():
    proc = subprocess.run([sys.executable, str(GUARD), "/srv/agents/example"],
                          capture_output=True, text=True)
    assert proc.returncode == 2
    assert "worktree" in proc.stderr


def test_wrapper_rejects_checkout_scratch_without_touching_staged_work(tmp_path):
    live = tmp_path / "shared"
    live.mkdir()
    subprocess.run(["git", "init", "-q", str(live)], check=True)
    (live / "staged.txt").write_text("preserve me\n")
    subprocess.run(["git", "-C", str(live), "add", "staged.txt"], check=True)
    before = (live / ".git" / "index").read_bytes()
    alias = tmp_path / "alias"
    alias.symlink_to(live, target_is_directory=True)
    env = dict(os.environ, CODEX_WORKER_TMPDIR=str(alias),
               CODEX_WORKER_CODEX_BIN="true")
    proc = subprocess.run(["bash", str(ROOT / "scripts/codex-worker-vps.sh"),
                           "--repo", str(live)], input="task", env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 2
    assert "checkout" in proc.stderr
    assert (live / ".git" / "index").read_bytes() == before
    assert (live / "staged.txt").read_text() == "preserve me\n"
    assert not list(live.glob("codex-worker.*"))


def test_wrapper_still_launches_in_private_clone(tmp_path):
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    (source / "file").write_text("fixture")
    subprocess.run(["git", "-C", str(source), "add", "file"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    probe = tmp_path / "probe"
    stub = tmp_path / "codex"
    stub.write_text('#!/bin/sh\npwd > "$PROBE"\ncat > "$PROBE.task"\n')
    stub.chmod(0o700)
    env = dict(os.environ, CODEX_WORKER_TMPDIR=str(scratch),
               CODEX_WORKER_CODEX_BIN=str(stub), PROBE=str(probe))
    proc = subprocess.run(["bash", str(ROOT / "scripts/codex-worker-vps.sh"),
                           "--repo", str(source), "--ref", "main"], input="bounded task", env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert Path(probe.read_text().strip()).parent.parent == scratch.resolve()
    assert Path(str(probe) + ".task").read_text() == "bounded task"
    assert not list(scratch.iterdir())
