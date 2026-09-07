"""Regression for board #1158: harness root files must not poison runtime pushes."""

from __future__ import annotations

import shutil
import subprocess

import scripts.lib.dispatch_helpers as dh


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_force_commit_skips_untracked_harness_files_but_commits_runtime(
    tmp_path, monkeypatch
):
    """Use real git status/add/commit while faking only auth and remote push."""
    repo = tmp_path / "dept"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test Agent")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("baseline\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "baseline")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://github.com/Bubble-invest/bubble-ops-ben.git",
    )

    runtime = repo / "outputs" / "heartbeat.jsonl"
    runtime.parent.mkdir()
    runtime.write_text('{"status":"ok"}\n')
    (repo / "AGENTS.md").write_text("local harness instructions\n")
    (repo / "HARNESS_HANDOFF.md").write_text("local continuity state\n")

    real_run = subprocess.run
    push_calls = []

    def selective_run(cmd, *args, **kwargs):
        command = list(cmd)
        if command[:3] == [
            "sudo",
            "-n",
            "/usr/local/bin/bubble-gh-credential-helper.sh",
        ]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="username=x-access-token\npassword=ghs_Fake1158Token\n",
                stderr="",
            )
        if "push" in command and any(
            "github.com/Bubble-invest/bubble-ops-ben.git" in arg
            for arg in command
        ):
            push_calls.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, *args, **kwargs)

    # Isolate the harness classification from structural policy behavior and
    # force the generic credential path, whose final network operation is fake.
    monkeypatch.setattr(dh, "_resolve_is_structural", lambda: (lambda path: False))
    monkeypatch.setattr(
        dh, "resolve_push_target", lambda repo_dir: ("ben", "bubble-ops-ben")
    )
    monkeypatch.setattr(dh, "_is_sudo_available", lambda: True)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(subprocess, "run", selective_run)

    ok, error = dh.force_commit_and_push(
        repo_dir=repo,
        message="loop: auto-commit runtime state before sync",
        bubble_git_guard_path=str(tmp_path / "missing-guard"),
    )

    assert ok is True, error
    assert push_calls, "the legitimate runtime commit must still reach push"
    committed = _git(repo, "show", "--pretty=format:", "--name-only", "HEAD")
    assert committed.stdout.splitlines() == ["outputs/heartbeat.jsonl"]
    status = _git(repo, "status", "--porcelain").stdout.splitlines()
    assert status == ["?? AGENTS.md", "?? HARNESS_HANDOFF.md"]


def test_harness_skip_is_exact_and_untracked_only():
    assert dh._is_untracked_harness_root_artifact("??", "AGENTS.md") is True
    assert (
        dh._is_untracked_harness_root_artifact("??", "HARNESS_HANDOFF.md")
        is True
    )
    assert dh._is_untracked_harness_root_artifact(" M", "AGENTS.md") is False
    assert dh._is_untracked_harness_root_artifact("??", "docs/AGENTS.md") is False
    assert dh._is_untracked_harness_root_artifact("??", "WORKING_MEMORY.md") is False
