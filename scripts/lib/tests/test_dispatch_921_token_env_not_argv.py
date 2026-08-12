"""
test_dispatch_921_token_env_not_argv.py — board #921: the GitHub App push
token must NEVER appear in subprocess argv (which lands verbatim in
bash-command transcripts / process listings). It must travel via the
subprocess `env=` kwarg instead (git's `GIT_CONFIG_*` extraHeader
mechanism), with auth behaviour otherwise unchanged.

Vulnerability that was fixed (scripts/lib/dispatch_helpers.py,
force_commit_and_push's "generic fallback" branch): the push command used
to be built as

    ["git", "-C", repo_dir, "-c", "credential.helper=", "push",
     f"https://x-access-token:{token}@github.com/Bubble-invest/{repo_name}.git",
     branch]

— the token sat in the URL, i.e. in argv[N], for a subprocess.run() call
whose command is exactly what gets logged in a transcript. The fix keeps
the remote URL clean and instead sets `http.https://github.com/.extraHeader`
via the `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>`
environment-variable triad (git >= 2.31), passed through subprocess.run's
`env=` kwarg — never part of argv.

These are construction-only tests (no real git remote / no live broker
token available in this environment): they assert the COMMAND LIST and the
ENV DICT that would be handed to subprocess.run, not an actual authenticated
push. A live before/after push validation is still required before wide
deploy (see the PR description) — this suite cannot substitute for that.
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


FAKE_TOKEN = "ghs_ThisIsATotallyFakeTestTokenNotReal1234567890"


# ── _env_with_bearer_auth_header (pure function) ────────────────────────

def test_env_with_bearer_auth_header_sets_git_config_triad():
    env = dh._env_with_bearer_auth_header({}, FAKE_TOKEN)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    # The token is present in the header VALUE (env, not argv) — decode and
    # confirm it round-trips to the expected basic-auth string.
    import base64
    b64 = env["GIT_CONFIG_VALUE_0"].split("Authorization: Basic ", 1)[1]
    decoded = base64.b64decode(b64).decode()
    assert decoded == f"x-access-token:{FAKE_TOKEN}"


def test_env_with_bearer_auth_header_appends_not_clobbers_existing_git_config():
    base_env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.someOther.config",
        "GIT_CONFIG_VALUE_0": "unrelated-value",
    }
    env = dh._env_with_bearer_auth_header(base_env, FAKE_TOKEN)
    # Existing entry preserved untouched.
    assert env["GIT_CONFIG_KEY_0"] == "http.someOther.config"
    assert env["GIT_CONFIG_VALUE_0"] == "unrelated-value"
    # New entry appended at the next free index.
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_KEY_1"] == "http.https://github.com/.extraHeader"
    assert FAKE_TOKEN not in env["GIT_CONFIG_KEY_1"]
    assert "Authorization: Basic " in env["GIT_CONFIG_VALUE_1"]


def test_env_with_bearer_auth_header_preserves_rest_of_base_env():
    env = dh._env_with_bearer_auth_header({"PATH": "/usr/bin", "HOME": "/root"}, FAKE_TOKEN)
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/root"


# ── force_commit_and_push generic-fallback branch: token never in argv ──

def _arm_generic_fallback_path(monkeypatch, repo_name="bubble-ops-maya"):
    """Force force_commit_and_push into the 'generic fallback' (credential
    -helper mint + push) branch: bubble-git-guard absent/unusable, but
    resolve_push_target still yields a repo_name, and sudo is reported
    available so the credential-helper mint is attempted."""
    import shutil
    monkeypatch.setattr(dh, "resolve_push_target",
                        lambda repo_dir: ("maya", repo_name))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(dh, "_is_sudo_available", lambda: True)


def test_generic_fallback_push_token_not_in_argv(tmp_path, monkeypatch):
    """The constructed push command list must contain NO token substring
    anywhere, and no `x-access-token:<...>@` URL — the token must instead
    show up only in the env dict passed to subprocess.run."""
    _arm_generic_fallback_path(monkeypatch)
    calls = []  # (cmd, kwargs) pairs

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        cmd_str = " ".join(cmd)
        if "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=" M outputs/thing.md\n", stderr="",
            )
        if "remote" in cmd and "get-url" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/Bubble-invest/bubble-ops-maya.git\n",
                stderr="",
            )
        if "sudo" in cmd and "bubble-gh-credential-helper.sh" in cmd_str:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"password={FAKE_TOKEN}\n", stderr="",
            )
        if "push" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, err = dh.force_commit_and_push(repo_dir=tmp_path, message="test")
    assert ok is True, err

    push_calls = [
        (cmd, kw) for cmd, kw in calls
        if "push" in cmd and "sudo" not in cmd
    ]
    assert push_calls, calls
    push_cmd, push_kw = push_calls[-1]

    # 1. The token must not appear anywhere in argv.
    argv_joined = " ".join(push_cmd)
    assert FAKE_TOKEN not in argv_joined, push_cmd
    # 2. No credential-in-URL pattern at all (x-access-token:<...>@).
    assert "x-access-token:" not in argv_joined, push_cmd
    assert "@github.com" not in argv_joined, push_cmd
    # 3. The remote URL is the clean, credential-free form.
    assert "https://github.com/Bubble-invest/bubble-ops-maya.git" in push_cmd, push_cmd
    # 4. credential.helper="" neutralization is preserved.
    assert "credential.helper=" in push_cmd, push_cmd

    # 5. The token DOES travel — via env, not argv.
    push_env = push_kw.get("env")
    assert push_env is not None, "push must pass env= carrying the auth header"
    git_config_values = [
        v for k, v in push_env.items() if k.startswith("GIT_CONFIG_VALUE_")
    ]
    assert any(FAKE_TOKEN in _b64_decode_basic(v) for v in git_config_values), push_env


def _b64_decode_basic(header_value: str) -> str:
    import base64
    b64 = header_value.split("Authorization: Basic ", 1)[1]
    return base64.b64decode(b64).decode()


def test_generic_fallback_push_fails_closed_when_token_not_ghs_prefixed(tmp_path, monkeypatch):
    """Unrelated to #921 directly, but guards the surrounding logic this fix
    sits inside: a malformed/missing token must still abort before any push
    is attempted (unchanged behaviour)."""
    _arm_generic_fallback_path(monkeypatch)

    def fake_run(cmd, **kw):
        if "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=" M outputs/thing.md\n", stderr="",
            )
        if "remote" in cmd and "get-url" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/Bubble-invest/bubble-ops-maya.git\n",
                stderr="",
            )
        if "sudo" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="password=not-a-real-token\n", stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, err = dh.force_commit_and_push(repo_dir=tmp_path, message="test")
    assert ok is False
    assert "failed to mint" in (err or "").lower()
