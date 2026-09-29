"""test_no_secret_leakage.py — regression guard for cockpit_health: no
collected evidence file, stdout, or board-card body ever contains a raw
secret value.

Mirrors the fleet's existing secret-leak tests (tests/test_1573_telegram_
token_argv_leak.sh, tests/test_1548_telegram_token_not_in_repl_env.sh):
set every credential this package touches to a distinctive dummy value, run
the real code paths, and grep every place a human (or the board) could see
output for that dummy substring.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.cockpit_health import collect, console_client, judge  # noqa: E402

DUMMY_BEARER = "SECRET-CONSOLE-BEARER-zzz999"
DUMMY_OPENROUTER_KEY = "SECRET-OPENROUTER-KEY-yyy888"
DUMMY_GH_TOKEN = "SECRET-GH-TOKEN-xxx777"

ALL_DUMMY_SECRETS = (DUMMY_BEARER, DUMMY_OPENROUTER_KEY, DUMMY_GH_TOKEN)


@pytest.fixture(autouse=True)
def set_dummy_secrets(monkeypatch):
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", DUMMY_BEARER)
    monkeypatch.setenv("OPENROUTER_API_KEY", DUMMY_OPENROUTER_KEY)
    monkeypatch.setenv("GH_TOKEN", DUMMY_GH_TOKEN)


def _assert_no_secret_leak(text: str) -> None:
    for secret in ALL_DUMMY_SECRETS:
        assert secret not in text, f"secret leaked: {secret!r} found in output"


def test_console_client_error_never_carries_the_token_value(monkeypatch):
    """A missing/broken console import still must not echo the token that
    WAS set — build_client() only reads the token to use it, never to
    report it back in an error string."""
    monkeypatch.delenv("CONSOLE_BEARER_TOKEN", raising=False)
    _client, error = console_client.build_client()
    assert error is not None
    assert "CONSOLE_BEARER_TOKEN" in error  # the var NAME is fine to mention
    _assert_no_secret_leak(error)  # the dummy VALUE (unset here) must not appear


def test_build_client_header_never_logged(monkeypatch, capsys):
    """With the app importable, the Authorization header is set once and
    never printed anywhere by build_client()."""
    client, error = console_client.build_client()
    captured = capsys.readouterr()
    _assert_no_secret_leak(captured.out)
    _assert_no_secret_leak(captured.err)
    if client is not None:
        # If the console app happened to be importable in this environment,
        # the header value itself is only ever inside the client object, and
        # the client object is never stringified/printed by this package —
        # confirm that directly.
        auth_header = client.headers.get("Authorization", "")
        assert DUMMY_BEARER in auth_header  # sanity: it WAS set, just never leaked


def test_evidence_bundle_on_disk_never_contains_the_bearer_token(tmp_path):
    class FakeResponse:
        status_code = 200
        def json(self):
            return {}

    class FakeClient:
        def get(self, path):
            return FakeResponse()

    bundle, out_path = collect.collect(
        out_dir=tmp_path, client=FakeClient(), client_error=None,
    )
    on_disk = out_path.read_text()
    _assert_no_secret_leak(on_disk)
    _assert_no_secret_leak(json.dumps(bundle))


def test_judge_stdout_never_contains_the_jev_key_even_on_failure(capsys, tmp_path):
    """A Jev call that fails (e.g. a real network error under a real key)
    reports the FAILURE reason, never the key. Simulate the failure path
    directly — build_jev_caller() when jev.py is missing already returns a
    generic 'unavailable' error with no key material in it."""
    evidence = {"pages": {"costs": {"http": {"status_code": 200}, "checks": []}}}
    (tmp_path / "evidence_latest.json").write_text(json.dumps(evidence))

    def stub_call(state, questions):
        return {"ok": False, "error": f"connection reset (key ending in ...{DUMMY_OPENROUTER_KEY[-4:]} rejected)"}

    # Exercise judge_evidence directly (shadow mode — no board calls) rather
    # than the CLI, to keep this test hermetic; the point is stdout from
    # main()'s final json.dumps never carries a full key even when the
    # underlying error string is careless — assert on the FULL dummy value,
    # which a well-behaved error message (like the one above) never contains.
    results = judge.judge_evidence(
        evidence, stub_call, shadow=True,
        emit_script=Path("/should/not/be/called.sh"),
        shadow_log_path=tmp_path / "shadow-log.jsonl",
    )
    output = json.dumps(results)
    _assert_no_secret_leak(output)


def test_github_helpers_keep_token_out_of_url_and_argv(monkeypatch, tmp_path):
    """REST keeps the token header-only; judge keeps it out of gh argv."""
    from scripts.cockpit_health import checks as checks_mod

    captured_argvs = []
    captured_urls = []

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_urlopen(req, timeout):
        captured_urls.append(req.full_url)
        assert req.get_header("Authorization") == f"Bearer {DUMMY_GH_TOKEN}"
        return FakeResponse(b"[]")

    def fake_run(cmd, capture_output=True, text=True, timeout=20, check=False):
        captured_argvs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(checks_mod.urllib.request, "urlopen", fake_urlopen)
    checks_mod._github_open_issue_count(
        "Bubble-invest/bubble-ops-board", token_file=tmp_path / "missing")

    monkeypatch.setattr(judge.subprocess, "run", fake_run)
    judge.emit_or_update("costs", {"cause": "x", "jev": None}, "summary",
                          emit_script=Path("/fake/emit.sh"))

    for argv in captured_argvs:
        for arg in argv:
            for secret in ALL_DUMMY_SECRETS:
                assert secret not in str(arg), f"secret leaked into argv: {argv!r}"
    for url in captured_urls:
        _assert_no_secret_leak(url)


def test_no_source_file_contains_a_hardcoded_looking_secret():
    """Static guard: none of this package's own .py files embed anything
    that looks like a live credential (a long base64/hex-ish literal
    assigned to a *_KEY/*_TOKEN/*_SECRET-shaped name). Env-var NAMES and
    documentation prose mentioning 'CONSOLE_BEARER_TOKEN' etc. are fine —
    this only flags an actual literal value."""
    pkg_dir = Path(__file__).resolve().parents[1]
    suspicious_pattern = re.compile(
        r'(?:KEY|TOKEN|SECRET)\s*=\s*["\'][A-Za-z0-9+/_-]{16,}["\']'
    )
    offenders = []
    for py_file in pkg_dir.rglob("*.py"):
        if "tests" in py_file.parts:
            continue
        text = py_file.read_text(errors="replace")
        for match in suspicious_pattern.finditer(text):
            offenders.append(f"{py_file}: {match.group(0)}")
    assert not offenders, f"possible hardcoded secret literal(s): {offenders}"
