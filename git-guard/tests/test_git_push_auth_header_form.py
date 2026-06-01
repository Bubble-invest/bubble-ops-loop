"""Regression: `git push` uses HTTP Basic auth (NOT Bearer) for github.com.

Empirically discovered 2026-05-20 during Step 7 deployment on Morty:

  - `Authorization: Bearer ghs_xxx` works for api.github.com (REST API).
  - The same header is REJECTED with HTTP/2 401 by github.com (git smart-HTTP
    endpoint that serves /info/refs and /git-receive-pack).
  - The endpoint requires HTTP Basic with `x-access-token` as the username
    and the installation token as the password.

This test locks in the corrected auth-header form so a future refactor
cannot silently regress back to Bearer (which would fail closed in
production, but with a misleading "401 / could not read Username" error).
"""

from __future__ import annotations

import base64

from src.guard import Guard
from src.policy_loader import load_policy
from tests.conftest import stage_files


def test_git_push_uses_http_basic_with_x_access_token(
    fixture_policy_yaml,
    temp_git_repo,
    mock_broker_binary,
    mock_git_push,
):
    """The `-c http.extraheader=...` arg must use HTTP Basic, NOT Bearer."""
    stage_files(temp_git_repo, ["outputs/2026-05-20/1/summary.md"])
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy, broker_cmd=[str(mock_broker_binary)])
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert rc == 0
    assert len(mock_git_push.calls) == 1, "expected exactly one git push"
    cmd, _env = mock_git_push.calls[0]

    # Locate the `-c http.extraheader=...` arg.
    header_args = [
        a for a in cmd
        if isinstance(a, str) and a.startswith("http.extraheader=")
    ]
    assert len(header_args) == 1, f"missing http.extraheader -c arg in {cmd!r}"
    header_value = header_args[0].split("=", 1)[1]

    # MUST be Basic, MUST NOT be Bearer.
    assert header_value.lower().startswith("authorization: basic "), (
        f"git push auth header must be HTTP Basic for github.com, got: "
        f"{header_value!r}"
    )
    assert "bearer" not in header_value.lower(), (
        "Bearer is rejected by github.com git smart-HTTP (use Basic instead)"
    )

    # The base64 payload must decode to `x-access-token:<the token>`.
    b64 = header_value.split(" ", 2)[2]
    decoded = base64.b64decode(b64).decode("ascii")
    assert decoded.startswith("x-access-token:"), (
        f"username must be 'x-access-token' for GitHub App install tokens, "
        f"got payload starting: {decoded[:20]!r}"
    )
    token = decoded.split(":", 1)[1]
    assert token.startswith("ghs_"), (
        f"password must be the ghs_ installation token, got: {token[:6]!r}..."
    )


def test_git_askpass_is_not_dev_null(
    fixture_policy_yaml,
    temp_git_repo,
    mock_broker_binary,
    mock_git_push,
):
    """GIT_ASKPASS=/dev/null is a bug: /dev/null is a char device, not exec'able.

    Linux refuses to exec it, which would surface as "fatal: cannot exec
    '/dev/null': Permission denied" — masking the real auth-form bug. We
    use /bin/true (or any executable that exits 0 silently) instead.
    """
    stage_files(temp_git_repo, ["outputs/2026-05-20/1/summary.md"])
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy, broker_cmd=[str(mock_broker_binary)])
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert rc == 0
    _cmd, env = mock_git_push.calls[0]
    assert env.get("GIT_ASKPASS") != "/dev/null", (
        "GIT_ASKPASS=/dev/null cannot be exec'd on Linux; use /bin/true"
    )
    assert env.get("GIT_TERMINAL_PROMPT") == "0", (
        "GIT_TERMINAL_PROMPT must be '0' to suppress all interactive prompts"
    )
