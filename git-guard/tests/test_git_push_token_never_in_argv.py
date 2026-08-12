"""#923 (final argv-leak site of this class, after #921/#311): the git-guard
push token must NEVER appear in `git push`'s argv/cmdline.

Prior behavior (the vulnerability): `_run_git_push` built the command as

    cmd = ["git", "-c", f"http.extraheader=Authorization: Basic {b64}",
           "push", remote, ref]

— `-c ...` is a literal argv element, so `basic_b64` (a base64 encoding of
`x-access-token:<token>`, i.e. the token itself, trivially reversible) landed
in `/proc/<pid>/cmdline` for the lifetime of the subprocess. Any local reader
of process listings (or a bash-command transcript capturing the invoked
argv) could recover the live installation token.

The fix moves the header value into the subprocess `env=` instead, via the
`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_<n>`/`GIT_CONFIG_VALUE_<n>` triad — the
same fleet-standard mechanism proven (and live-validated end-to-end against
a real broker token) in scripts/lib/dispatch_helpers.py's
`_env_with_bearer_auth_header` (board #921, PR #310/#311). `env=` is not
part of argv and is not recorded in `/proc/<pid>/cmdline`.

These tests assert the CONSTRUCTED argv/env only (via the `mock_git_push`
fixture, which intercepts `subprocess.run` before a real git process is
spawned) — they cannot exercise a real network push. A live push validation
against github.com is still required before deploy (see the PR).
"""

from __future__ import annotations

import base64

from src.guard import Guard
from src.policy_loader import load_policy
from tests.conftest import stage_files

# A syntactically-plausible but fake installation token — never a real
# credential. Chosen to be distinctive enough that an accidental argv leak
# would be unambiguous in a failing assertion message.
_FAKE_TOKEN_PREFIX = "ghs_MOCK"


def _run_guard_push(fixture_policy_yaml, temp_git_repo, mock_broker_binary, mock_git_push):
    stage_files(temp_git_repo, ["outputs/2026-05-20/1/summary.md"])
    policy = load_policy(fixture_policy_yaml)
    g = Guard(policy=policy, broker_cmd=[str(mock_broker_binary)])
    rc = g.push(
        repo_dir=temp_git_repo,
        dept="fixture",
        action="runtime_write_own",
        repo="bubble-ops-fixture",
    )
    assert rc == 0, "guard push must still succeed on an allowed path"
    assert len(mock_git_push.calls) == 1
    return mock_git_push.calls[0]


def test_token_and_basic_header_absent_from_argv(
    fixture_policy_yaml, temp_git_repo, mock_broker_binary, mock_git_push
):
    """No form of the token (raw or base64-Basic-encoded) may appear in cmd."""
    cmd, env = _run_guard_push(
        fixture_policy_yaml, temp_git_repo, mock_broker_binary, mock_git_push
    )

    # Recover what the token/base64 payload actually were from env (the
    # ONLY place they're allowed to live) so we can assert their absence
    # from argv specifically.
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    header_values = [
        env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(count)
        if env.get(f"GIT_CONFIG_KEY_{i}") == "http.extraheader"
    ]
    assert len(header_values) == 1
    header_value = header_values[0]
    b64_payload = header_value.split(" ", 2)[2]
    decoded_token = base64.b64decode(b64_payload).decode("ascii").split(":", 1)[1]
    assert decoded_token.startswith(_FAKE_TOKEN_PREFIX), (
        "sanity: mock broker token shape changed, update this test's assumptions"
    )

    argv_joined = " ".join(str(a) for a in cmd)

    # The literal token must not appear anywhere in argv.
    assert decoded_token not in argv_joined, (
        f"token leaked into git push argv: {cmd!r}"
    )
    # The base64 Basic-auth payload (trivially reversible to the token) must
    # not appear anywhere in argv either.
    assert b64_payload not in argv_joined, (
        f"base64 auth payload leaked into git push argv: {cmd!r}"
    )
    # No arg may look like an inline `-c http.extraheader=...` override —
    # that was the exact pre-#923 vulnerability shape.
    assert not any(
        isinstance(a, str) and a.lower().startswith("http.extraheader=")
        for a in cmd
    ), f"http.extraheader must not be passed via -c/argv anymore: {cmd!r}"
    assert not any(
        isinstance(a, str) and "authorization" in a.lower()
        for a in cmd
    ), f"no argv element may contain an Authorization header: {cmd!r}"


def test_credential_helper_neutralized_via_argv_flag(
    fixture_policy_yaml, temp_git_repo, mock_broker_binary, mock_git_push
):
    """`-c credential.helper=` (empty, not a secret) must still be present.

    This neutralizes any ambient credential-helper chain so the explicit
    extraHeader (now carried via env) is unambiguously what git uses to
    authenticate — matching the fleet-standard pattern from #310/#311.
    """
    cmd, _env = _run_guard_push(
        fixture_policy_yaml, temp_git_repo, mock_broker_binary, mock_git_push
    )
    assert "-c" in cmd and "credential.helper=" in cmd, (
        f"expected -c credential.helper= in argv, got: {cmd!r}"
    )
    # It must be the empty-string form specifically (disables the helper),
    # not some other credential.helper value.
    idx = cmd.index("credential.helper=")
    assert cmd[idx - 1] == "-c"


def test_remote_and_ref_still_passed_positionally(
    fixture_policy_yaml, temp_git_repo, mock_broker_binary, mock_git_push
):
    """Guard behavior unchanged: push still targets the right remote/ref.

    `git push <remote> <ref>` — remote defaults to "origin" and ref is the
    branch being pushed. This must be byte-identical to pre-#923 behavior;
    only the credential transport changed.
    """
    cmd, _env = _run_guard_push(
        fixture_policy_yaml, temp_git_repo, mock_broker_binary, mock_git_push
    )
    assert cmd[0] == "git"
    assert cmd[-2] == "origin", f"remote must still be positional argv, got: {cmd!r}"
    assert cmd[-1], f"ref must still be positional argv, got: {cmd!r}"


def test_git_askpass_and_terminal_prompt_still_scrubbed(
    fixture_policy_yaml, temp_git_repo, mock_broker_binary, mock_git_push
):
    """Guard behavior unchanged: askpass/terminal-prompt hardening is intact."""
    _cmd, env = _run_guard_push(
        fixture_policy_yaml, temp_git_repo, mock_broker_binary, mock_git_push
    )
    assert env.get("GIT_ASKPASS") == "/bin/true"
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert "GITHUB_TOKEN" not in env, "GITHUB_TOKEN must never be propagated"
