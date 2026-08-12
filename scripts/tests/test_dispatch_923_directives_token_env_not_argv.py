"""
test_dispatch_923_directives_token_env_not_argv.py — board #923 (same class
as #921/PR#310): dispatch_directives.py's `_push_repo` must never place the
GitHub App push token in subprocess argv. It must travel via the shared
`dispatch_helpers._env_with_bearer_auth_header` env mechanism instead,
exactly like PR#310's fix for scripts/lib/dispatch_helpers.py's
force_commit_and_push.

Vulnerability that was fixed (scripts/dispatch_directives.py, `_push_repo`):
the push command used to be built as

    url = f"https://x-access-token:{token}@github.com/{_GH_ORG}/{repo_name}.git"
    push = _run(["git", "-C", str(repo_dir), "push", url, "HEAD:main"])

— the token sat in the URL, i.e. in argv[N], for a subprocess call whose
command is exactly what lands in a bash-command transcript / `ps`. The fix
keeps the remote URL clean and instead reuses dispatch_helpers's
`_env_with_bearer_auth_header` (imported from scripts/lib, NOT
re-implemented) to set `http.https://github.com/.extraHeader` via the
`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` env triad,
passed through `_run`'s new `env=` kwarg — never part of argv. The push argv
also carries `-c credential.helper=` (empty — not a secret, fine in argv),
matching the live-validated #310 generic-fallback push in
dispatch_helpers.py byte-for-byte in shape: same env mechanism + same
argv-level credential-helper neutralization flag.

Construction-only test (no real git remote / no live broker token): asserts
the COMMAND LIST and ENV DICT handed to subprocess.run, not an actual
authenticated push. A live before/after push validation is still required
before deploy (see the PR description) — this suite cannot substitute for
that.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dispatch_directives as dd  # noqa: E402


FAKE_TOKEN = "ghs_ThisIsATotallyFakeTestTokenNotReal1234567890"


def test_push_repo_token_not_in_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(dd, "_mint_token", lambda repo_name: FAKE_TOKEN)

    calls = []  # (cmd, kwargs) pairs, exactly as handed to subprocess.run

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        if "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M outputs/thing.md\n", stderr="")
        # add / commit / push all succeed.
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(dd.subprocess, "run", fake_run)

    ok, detail = dd._push_repo(repo_dir=tmp_path, repo_name="bubble-ops-maya",
                                message="test commit", dry_run=False)
    assert ok is True, detail

    push_calls = [(cmd, kw) for cmd, kw in calls if "push" in cmd]
    assert push_calls, calls
    push_cmd, push_kw = push_calls[-1]

    # 1. The token must not appear anywhere in argv.
    argv_joined = " ".join(push_cmd)
    assert FAKE_TOKEN not in argv_joined, push_cmd
    # 2. No credential-in-URL pattern at all.
    assert "x-access-token:" not in argv_joined, push_cmd
    assert "@github.com" not in argv_joined, push_cmd
    # 3. The remote URL is the clean, credential-free form.
    assert "https://github.com/Bubble-invest/bubble-ops-maya.git" in push_cmd, push_cmd
    # 4. credential.helper="" neutralization is preserved (matches #310's
    #    generic-fallback push argv shape exactly — reviewer nit).
    assert "credential.helper=" in push_cmd, push_cmd
    assert push_cmd == [
        "git", "-C", str(tmp_path), "-c", "credential.helper=",
        "push", "https://github.com/Bubble-invest/bubble-ops-maya.git", "HEAD:main",
    ], push_cmd

    # 5. The token DOES travel — via env, not argv.
    push_env = push_kw.get("env")
    assert push_env is not None, "push must pass env= carrying the auth header"
    git_config_values = [v for k, v in push_env.items() if k.startswith("GIT_CONFIG_VALUE_")]
    assert git_config_values, push_env

    import base64
    def _b64_decode_basic(header_value: str) -> str:
        b64 = header_value.split("Authorization: Basic ", 1)[1]
        return base64.b64decode(b64).decode()

    assert any(FAKE_TOKEN in _b64_decode_basic(v) for v in git_config_values), push_env


def test_push_repo_no_mint_no_push(tmp_path, monkeypatch):
    """Unrelated to #921/#923 directly, but guards the surrounding logic
    this fix sits inside: if minting fails, _push_repo must fail closed
    (no push attempted) — unchanged behaviour."""
    monkeypatch.setattr(dd, "_mint_token", lambda repo_name: None)

    calls = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        if "status" in cmd and "--porcelain" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M outputs/thing.md\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(dd.subprocess, "run", fake_run)

    ok, detail = dd._push_repo(repo_dir=tmp_path, repo_name="bubble-ops-maya",
                                message="test commit", dry_run=False)
    assert ok is False
    assert "could not mint token" in detail
    assert not any("push" in cmd for cmd, _ in calls)
