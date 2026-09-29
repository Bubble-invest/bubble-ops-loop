"""Round-6 regressions: actor Git config is never a routing boundary."""

from __future__ import annotations

import http.server
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from src import staging
from src.guard import Guard, github_repo_url
from src.policy_loader import load_policy
from tests.conftest import commit_staged, stage_files


class _Capture(http.server.BaseHTTPRequestHandler):
    requests = []

    def _record(self):
        type(self).requests.append(
            (self.command, self.path, self.headers.get("Authorization"))
        )
        self.send_response(500)
        self.end_headers()

    do_GET = _record
    do_POST = _record

    def log_message(self, *_args):
        pass


@pytest.fixture
def capture_server():
    _Capture.requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/exfil.git", _Capture
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "variant",
    [
        "pushurl",
        "instead_of",
        "http_proxy",
        "include_path",
        "credential_helper",
        "receivepack",
        "ssh_command",
    ],
)
def test_actor_config_cannot_route_token_bearing_push(
    variant,
    temp_git_repo,
    fixture_policy_yaml,
    mock_broker_binary,
    tmp_path,
    capture_server,
):
    attacker_url, handler = capture_server
    marker = tmp_path / f"{variant}-executed"
    helper = tmp_path / "attacker-helper.sh"
    helper.write_text(f"#!/bin/sh\nenv > {marker}\nexit 1\n")
    helper.chmod(0o755)
    real_remote = str(temp_git_repo.parent / "remote.git")

    if variant == "pushurl":
        _git(temp_git_repo, "config", "remote.origin.pushurl", attacker_url)
    elif variant == "instead_of":
        _git(temp_git_repo, "config", f"url.{attacker_url}.insteadOf", real_remote)
    elif variant == "http_proxy":
        _git(temp_git_repo, "config", "http.proxy", attacker_url)
    elif variant == "include_path":
        included = tmp_path / "attacker.inc"
        included.write_text(
            f"[remote \"origin\"]\n\tpushurl = {attacker_url}\n"
            f"[credential]\n\thelper = !{helper}\n"
        )
        _git(temp_git_repo, "config", "include.path", str(included))
    elif variant == "credential_helper":
        _git(temp_git_repo, "config", "credential.helper", f"!{helper}")
    elif variant == "receivepack":
        _git(temp_git_repo, "config", "remote.origin.receivepack", str(helper))
    elif variant == "ssh_command":
        _git(temp_git_repo, "config", "core.sshCommand", str(helper))

    stage_files(temp_git_repo, [f"outputs/{variant}.txt"], "allowed\n")
    pushed_sha = commit_staged(temp_git_repo, f"actor config variant {variant}")
    guard = Guard(
        load_policy(fixture_policy_yaml),
        broker_cmd=[str(mock_broker_binary)],
        audit_log_path=tmp_path / "audit.jsonl",
    )
    assert guard.push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture"
    ) == 0

    remote_sha = subprocess.run(
        ["git", "ls-remote", real_remote, "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.split()[0]
    assert remote_sha == pushed_sha
    assert handler.requests == []
    assert not marker.exists()


def test_only_source_resolution_runs_in_actor_checkout(
    temp_git_repo, fixture_policy_yaml, tmp_path, monkeypatch
):
    stage_files(temp_git_repo, ["outputs/isolated.txt"])
    commit_staged(temp_git_repo)
    actor = str(temp_git_repo.resolve())
    records = []
    original_run = staging.subprocess.run

    def recording_run(cmd, *args, **kwargs):
        cwd = str(Path(kwargs.get("cwd", ".")).resolve())
        root_mode = None
        if "bubble-git-guard-" in cwd and Path(cwd).name == "repo.git":
            root_mode = stat.S_IMODE(Path(cwd).parent.stat().st_mode)
        records.append((list(cmd), cwd, root_mode))
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(staging.subprocess, "run", recording_run)
    guard = Guard(
        load_policy(fixture_policy_yaml),
        audit_log_path=tmp_path / "audit.jsonl",
    )
    assert guard.push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture",
        dry_run=True,
    ) == 0

    actor_commands = [cmd for cmd, cwd, _mode in records if cwd == actor]
    assert actor_commands
    forbidden = {"fetch", "ls-remote", "diff", "ls-tree", "push"}
    assert all(not forbidden.intersection(cmd) for cmd in actor_commands)

    guard_cwds = {
        Path(cwd) for _cmd, cwd, _mode in records
        if "bubble-git-guard-" in cwd and Path(cwd).name == "repo.git"
    }
    assert guard_cwds
    assert all(
        mode == 0o700 for _cmd, cwd, mode in records
        if "bubble-git-guard-" in cwd and Path(cwd).name == "repo.git"
    )
    for repo in guard_cwds:
        assert not repo.parent.exists(), "temporary guard repository was not removed"


def test_destination_url_is_literal_and_repo_name_is_validated():
    assert github_repo_url("bubble-ops-fixture") == (
        "https://github.com/Bubble-invest/bubble-ops-fixture.git"
    )
    for invalid in ("../evil", "org/repo", "repo?x=1", ""):
        with pytest.raises(ValueError):
            github_repo_url(invalid)


def test_private_remote_retry_uses_separate_read_token_in_isolated_repo(
    temp_git_repo,
    fixture_policy_yaml,
    mock_broker_binary,
    broker_call_log,
    tmp_path,
    monkeypatch,
):
    stage_files(temp_git_repo, ["outputs/private.txt"])
    commit_staged(temp_git_repo)
    original = staging.GuardRepository.remote_sha
    seen_tokens = []

    def require_auth(self, destination_url, destination, *, token=None):
        seen_tokens.append(token)
        if token is None:
            raise subprocess.CalledProcessError(
                128, ["git", "ls-remote"], stderr="fatal: could not read Username"
            )
        return original(
            self, destination_url, destination, token=token
        )

    monkeypatch.setattr(staging.GuardRepository, "remote_sha", require_auth)
    guard = Guard(
        load_policy(fixture_policy_yaml),
        broker_cmd=[str(mock_broker_binary)],
        audit_log_path=tmp_path / "audit.jsonl",
    )
    assert guard.push(
        temp_git_repo, "fixture", "runtime_write_own", "bubble-ops-fixture"
    ) == 0
    assert seen_tokens[0] is None
    assert seen_tokens[1] and seen_tokens[1].startswith("ghs_")
    broker_calls = broker_call_log.read_text()
    assert "runtime_read" in broker_calls
    assert "runtime_write_own" in broker_calls


def test_hardened_env_drops_all_actor_git_and_proxy_routing():
    hostile = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.http://attacker/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
        "GIT_SSH_COMMAND": "/tmp/attacker",
        "HTTPS_PROXY": "http://attacker",
        "http_proxy": "http://attacker",
    }
    env = staging.hardened_git_env(hostile, home=Path("/guard/home"))
    assert not any(key.startswith("GIT_") for key in env if key not in {
        "GIT_NO_REPLACE_OBJECTS", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
        "GIT_ATTR_NOSYSTEM", "GIT_TERMINAL_PROMPT", "GIT_ASKPASS",
    })
    assert "HTTPS_PROXY" not in env and "http_proxy" not in env


def test_settings_pr_read_token_mint_uses_settings_pr_action(
    temp_git_repo,
    fixture_policy_yaml,
    mock_broker_binary,
    broker_call_log,
    tmp_path,
    monkeypatch,
):
    """#543 regression (Ben, 2026-09-29): the settings_pr path mints through a
    root wrapper pinned to settings_pr only, which refuses runtime_read. The
    pre-push read token must therefore be minted with settings_pr there."""
    stage_files(temp_git_repo, ["dept.yaml"])
    commit_staged(temp_git_repo)
    original = staging.GuardRepository.remote_sha

    def require_auth(self, destination_url, destination, *, token=None):
        if token is None:
            raise subprocess.CalledProcessError(
                128, ["git", "ls-remote"], stderr="fatal: could not read Username"
            )
        return original(self, destination_url, destination, token=token)

    monkeypatch.setattr(staging.GuardRepository, "remote_sha", require_auth)
    guard = Guard(
        load_policy(fixture_policy_yaml),
        broker_cmd=[str(mock_broker_binary)],
        audit_log_path=tmp_path / "audit.jsonl",
    )
    assert guard.push(
        temp_git_repo, "fixture", "settings_pr", "bubble-ops-fixture"
    ) == 0
    broker_calls = broker_call_log.read_text()
    assert "runtime_read" not in broker_calls
    assert "settings_pr" in broker_calls
