"""Shared pytest fixtures for bubble-git-guard tests.

Notion v4 doctrine (line 725): paths are enforced LOCALLY by this guard
because GitHub's `contents:write` is not path-scoped. The broker
(Step 3b) handles repo+permission, this guard handles path.

Fixtures provided:
  - temp_git_repo:        a tmp git working tree with optional staged files
  - fixture_policy_yaml:  the canonical bubble-ops-fixture policy YAML
  - mock_broker_binary:   a fake `bubble-token-broker` script that returns a
                          canned ghs_MOCK<hex> token (or fails)
  - mock_failed_broker_binary: same shape but exits non-zero
  - mock_broker_env:      monkeypatches PATH so the fake broker is picked up
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional

import pytest
import yaml

# Make src/ (this package) importable
GUARD_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GUARD_ROOT))

# Make the token-broker's src/ importable so we can reuse Policy.
BROKER_ROOT = GUARD_ROOT.parent / "token-broker"
sys.path.insert(0, str(BROKER_ROOT))


# --- Git repo fixture -----------------------------------------------------


def _git(repo: Path, *args: str, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Run a git command in `repo`, raising on non-zero. Quiet by default."""
    base_env = os.environ.copy()
    base_env.setdefault("GIT_AUTHOR_NAME", "test")
    base_env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    base_env.setdefault("GIT_COMMITTER_NAME", "test")
    base_env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    if env:
        base_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=base_env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def temp_git_repo(tmp_path: Path) -> Path:
    """Create an initialized git repo with a configured upstream (bare remote).

    Layout:
      tmp_path/remote.git   <- bare remote (the "GitHub" stand-in)
      tmp_path/repo         <- working tree, branch 'main' tracks origin/main

    The seed commit is pushed to the remote, so `@{upstream}..HEAD` correctly
    returns ONLY the work the test added on top — exactly what production looks
    like on Morty when the loop runs and pushes its outputs.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    # Seed commit
    (repo / ".gitkeep").write_text("init\n")
    _git(repo, "add", ".gitkeep")
    _git(repo, "commit", "-m", "init")
    # Wire up the remote and push the seed so HEAD matches origin/main exactly.
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo


def stage_files(repo: Path, paths: Iterable[str], content: str = "x\n") -> List[str]:
    """Helper: write+stage each path in the repo. Returns the list of staged paths."""
    out: List[str] = []
    for p in paths:
        full = repo / p
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        _git(repo, "add", p)
        out.append(p)
    return out


# Expose as fixture for tests that want a callable
@pytest.fixture
def stage(temp_git_repo):
    def _stage(paths: Iterable[str], content: str = "x\n") -> List[str]:
        return stage_files(temp_git_repo, paths, content)

    return _stage


# --- Policy fixture -------------------------------------------------------


@pytest.fixture
def fixture_policy_yaml(tmp_path: Path) -> Path:
    """Canonical bubble-ops-fixture policy (matches deploy/policies/fixture-policy.yaml)."""
    data = {
        "github_access": {
            "actor": "ops-loop-fixture",
            "own_repo": "bubble-ops-fixture",
            "read": ["bubble-ops-fixture", "bubble-shared-wiki"],
            "write": [
                {
                    "repo": "bubble-ops-fixture",
                    "allowed_paths": ["outputs/**", "queues/**", "inbox/**"],
                    "mode": "direct_runtime_commit",
                }
            ],
            "pull_requests": {"can_open_to": []},
        }
    }
    path = tmp_path / "fixture-policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def tony_policy_yaml(tmp_path: Path) -> Path:
    """Tony policy (can open priority PRs to children, target queues/management/**)."""
    data = {
        "github_access": {
            "actor": "ops-loop-tony",
            "own_repo": "bubble-ops-tony",
            "read": ["bubble-ops-tony"],
            "write": [
                {
                    "repo": "bubble-ops-tony",
                    "allowed_paths": ["outputs/**", "queues/**", "inbox/**"],
                    "mode": "direct_runtime_commit",
                }
            ],
            "pull_requests": {
                "can_open_to": ["bubble-ops-fixture"],
                "target_paths": ["queues/management/**"],
            },
        }
    }
    path = tmp_path / "tony-policy.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# --- Mock broker binary (subprocess-faithful) -----------------------------


_OK_BROKER_BODY = """#!/usr/bin/env python3
import sys, os
# Record the call for the test to inspect
log = os.environ.get("MOCK_BROKER_CALL_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(repr(sys.argv) + "\\n")
# Emit a fake token (40 hex chars after ghs_MOCK)
sys.stdout.write("ghs_MOCK" + ("a" * 40))
sys.exit(0)
"""

_FAIL_BROKER_BODY = """#!/usr/bin/env python3
import sys, os
log = os.environ.get("MOCK_BROKER_CALL_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(repr(sys.argv) + "\\n")
sys.stderr.write("DENIED: simulated broker failure\\n")
sys.exit(2)
"""


def _write_mock(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def mock_broker_binary(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "mock-bin-ok"
    bin_dir.mkdir()
    p = bin_dir / "bubble-token-broker"
    _write_mock(p, _OK_BROKER_BODY)
    return p


@pytest.fixture
def mock_failed_broker_binary(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "mock-bin-fail"
    bin_dir.mkdir()
    p = bin_dir / "bubble-token-broker"
    _write_mock(p, _FAIL_BROKER_BODY)
    return p


@pytest.fixture
def broker_call_log(tmp_path: Path, monkeypatch) -> Path:
    """A file the mock broker appends to whenever it's invoked."""
    log = tmp_path / "broker-calls.log"
    monkeypatch.setenv("MOCK_BROKER_CALL_LOG", str(log))
    return log


# --- Mock git push --------------------------------------------------------


@pytest.fixture
def mock_git_push(monkeypatch):
    """Monkeypatch subprocess.run in guard.py to short-circuit `git push`.

    Returns a list of (args, env_subset) tuples for assertion. By default
    the mock returns a CompletedProcess(returncode=0). Tests can mutate
    `mock_git_push.returncode` to simulate push failure.
    """
    from src import guard as guard_module

    calls: list = []
    state = {"returncode": 0, "stderr": ""}

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        # Only intercept `git push` invocations originating from the guard.
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "git" and "push" in cmd:
            # Redact env: only record whether AUTHORIZATION header includes ghs_
            env = kwargs.get("env") or {}
            calls.append((list(cmd), {k: v for k, v in env.items() if k.startswith("GIT_") or k == "GITHUB_TOKEN"}))
            return subprocess.CompletedProcess(
                args=cmd, returncode=state["returncode"], stdout="", stderr=state["stderr"]
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(guard_module.subprocess, "run", fake_run)
    fake_run.calls = calls  # type: ignore[attr-defined]
    fake_run.state = state  # type: ignore[attr-defined]
    return fake_run
