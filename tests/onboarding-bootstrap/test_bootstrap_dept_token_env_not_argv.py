"""
test_bootstrap_dept_token_env_not_argv.py — board #923 (same class as
#921/PR#310): bootstrap-dept.sh's push step must never place GH_TOKEN in
argv (or in the remote URL written to `.git/config`). It must travel via a
`GIT_CONFIG_*` extraHeader env-var triad instead, exactly like PR#310's
`_env_with_bearer_auth_header` fix for scripts/lib/dispatch_helpers.py.

Vulnerability that was fixed (scripts/bootstrap-dept.sh, Step 6 push): the
push command used to be built by rewriting the remote URL to
`https://x-access-token:<GH_TOKEN>@github.com/...` via
`git remote set-url origin <url>` — the token sat in argv for that command
(visible in `ps`/bash-command transcripts) AND, transiently, on disk in
`.git/config` between the set-url and the revert. The fix keeps the remote
URL untouched and instead passes `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_<n>`/
`GIT_CONFIG_VALUE_<n>` as env for just the `git push` invocations (bash
literal-name prefix-assignment — env only, never argv), setting
`http.https://github.com/.extraHeader: Authorization: Basic <b64>` and
neutralizing `credential.helper`.

This is a real (not mocked) end-to-end run of bootstrap-dept.sh against a
local bare-repo remote (no network, no real GitHub token) with a `git` shim
on PATH ahead of the real git binary that logs every invocation's argv (not
env) to a file — the same "assert the token substring is absent from the
constructed argv" spirit as
scripts/lib/tests/test_dispatch_921_token_env_not_argv.py, but exercised
live through the shell script rather than by mocking subprocess.run.

A live push against the REAL github.com still needs before/after validation
per the PR caveat — this test proves the token never touches argv, not that
the resulting Authorization header authenticates against GitHub.
"""
from __future__ import annotations

import os
import shutil
import textwrap

import pytest

FAKE_TOKEN = "ghs_ThisIsATotallyFakeTestTokenNotReal1234567890"


@pytest.fixture
def git_argv_logger(tmp_path):
    """Prepend a `git` shim to PATH that appends every invocation's argv
    (space-joined, NOT env) to a log file, then execs the real git. Lets the
    test assert on exactly what bootstrap-dept.sh hands `git` as arguments."""
    real_git = shutil.which("git")
    assert real_git, "git must be on PATH to run this test"
    bin_dir = tmp_path / "git-shim-bin"
    bin_dir.mkdir()
    log_file = tmp_path / "git_argv.log"
    shim = bin_dir / "git"
    shim.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "{log_file}"
            exec "{real_git}" "$@"
            """
        )
    )
    shim.chmod(0o755)
    return {"bin_dir": bin_dir, "log_file": log_file}


def test_push_with_fake_token_never_hits_git_argv(run_bootstrap, mock_gh_bin, git_argv_logger):
    # git-shim-bin must come BEFORE mock_gh_bin's bin_dir (and the rest of
    # PATH) so every `git` call — including the ones inside
    # bootstrap-dept.sh's push step — is logged.
    patched_path = f"{git_argv_logger['bin_dir']}:{mock_gh_bin['bin_dir']}:{os.environ['PATH']}"

    res = run_bootstrap(
        slug="tokentest",
        display_name="TokenTest",
        owner="operator",
        extra_env={"GH_TOKEN": FAKE_TOKEN, "PATH": patched_path},
    )
    assert res.returncode == 0, f"bootstrap failed:\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"

    log_lines = git_argv_logger["log_file"].read_text().splitlines()
    assert log_lines, "expected at least one git invocation to be logged"

    # 1. The token must not appear anywhere in any logged argv line.
    for line in log_lines:
        assert FAKE_TOKEN not in line, f"token leaked into git argv: {line!r}"
        # 2. No credential-in-URL pattern at all.
        assert "x-access-token:" not in line, f"credential-in-URL leaked into git argv: {line!r}"

    # Sanity: the push step actually ran (otherwise the assertions above are
    # vacuous — nothing was exercised). bootstrap-dept.sh always calls git as
    # `git -C "$CLONE_DIR" push ...`, so "push" is a later token, not argv[0].
    push_lines = [l for l in log_lines if " push " in f" {l} "]
    assert push_lines, f"no `git ... push ...` invocation was logged: {log_lines}"
    assert len(push_lines) == 2, f"expected 2 pushes (branch + main), got: {push_lines}"
