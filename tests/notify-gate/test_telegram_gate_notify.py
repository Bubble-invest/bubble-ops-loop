"""Step 10 — Telegram gate notification primitive.

TDD spec for `tools/notify-gate/notify.py` (lives in the fixture repo).

Doctrine sources:
  - Notion v4 lines 376-381: 4 kinds of gates (Layer-2 decision /
    Layer-3 exec_retry / Layer-4 mandate_breach / modify) — each must
    ping Telegram with the gate context + 4 actions.
  - Notion v4 line 213: gate fields including `requires_human` and
    `actions: [approve, reject, modify, defer]`.
  - MVP-ROADMAP Step 10 deliverables: a notification PRIMITIVE
    (not a daemon), invoked at the end of a Layer 2 tick.

Contract under test:

    python3 notify.py --gate-path queues/gates/<id>.yaml
                      --repo bubble-ops-fixture
                      [--commit-sha <sha>]
                      [--repo-root <abs path>]

  Exit codes:
    0 on success (Telegram returned ok=true)
    1 on Telegram API failure (5xx / network / non-ok response)
    2 on input error (missing file / bad YAML / missing required fields)

  Side effects:
    - POST https://api.telegram.org/bot<TOKEN>/sendMessage
    - JSON payload: { chat_id: "6532205130", text: <msg>, parse_mode: "Markdown",
                      disable_web_page_preview: false }
    - stdout: JSON {notification_sent: bool, message_id?: int, error?: str}
    - stderr: human-readable log line (token REDACTED)

  Env vars read:
    FIXTURE_TELEGRAM_BOT_TOKEN (preferred per Step 10 brief)
       fallback: TELEGRAM_BOT_TOKEN (what's actually deployed in
       /run/claude-agent-fixture/env today)
    NOTIFY_GATE_CHAT_ID (default: "6532205130" = Joris)
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Allow importing the impl module directly (set in conftest)
try:
    import notify  # noqa: F401
    HAS_IMPL = True
except ImportError:
    HAS_IMPL = False


# Skip everything cleanly if the impl module doesn't exist yet (RED state).
# Once notify.py lands, all tests become live.
pytestmark = pytest.mark.skipif(
    not HAS_IMPL,
    reason="tools/notify-gate/notify.py not implemented yet (RED phase)"
)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

SAMPLE_GATE_YAML = """\
id: gate-notif-test-002
kind: research_decision
source_item: research-notif-test-002
layer: 2
gate_policy_id: echo_action
created_at: 2026-05-20T19:15:00Z
status: awaiting_human_approval
summary: "Step 10 notify primitive smoke test — verifies Telegram ping fires."
evidence:
  - outputs/2026-05-20/2/research/research-notif-test-002.md
  - outputs/2026-05-20/2/summary.md
risk: low
recommended_action: approve
"""


@pytest.fixture
def gate_file(tmp_path):
    """Materialise a sample gate-item YAML at queues/gates/<id>.yaml."""
    gate_dir = tmp_path / "queues" / "gates"
    gate_dir.mkdir(parents=True)
    gate_path = gate_dir / "gate-notif-test-002.yaml"
    gate_path.write_text(SAMPLE_GATE_YAML)
    return tmp_path, gate_path


@pytest.fixture
def fake_token_env(monkeypatch):
    """Set the bot token env var to a recognisable fake."""
    monkeypatch.setenv("FIXTURE_TELEGRAM_BOT_TOKEN", "FAKE_TEST_TOKEN_DO_NOT_LEAK")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    yield "FAKE_TEST_TOKEN_DO_NOT_LEAK"


@pytest.fixture
def mock_telegram_ok():
    """Patch the HTTP POST so it succeeds without hitting the network."""
    with patch.object(notify, "_post_to_telegram") as mock_post:
        mock_post.return_value = (
            True,
            {"ok": True, "result": {"message_id": 42, "chat": {"id": 6532205130}}},
            None,
        )
        yield mock_post


@pytest.fixture
def mock_telegram_5xx():
    with patch.object(notify, "_post_to_telegram") as mock_post:
        mock_post.return_value = (
            False,
            None,
            "Telegram API returned HTTP 502",
        )
        yield mock_post


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_notify_gate_sends_telegram_with_required_fields(
    gate_file, fake_token_env, mock_telegram_ok
):
    """The Telegram message body must include gate_id, kind, risk_level,
    and a clickable GitHub blob URL pointing at the gate file."""
    repo_root, gate_path = gate_file
    rel = gate_path.relative_to(repo_root)

    result = notify.notify_gate(
        gate_path=str(rel),
        repo="bubble-ops-fixture",
        commit_sha="abc123def456",
        repo_root=str(repo_root),
    )

    assert result["notification_sent"] is True, result
    assert mock_telegram_ok.called

    # Inspect the call args — the message body is the 2nd positional arg
    # (or the 'text' key in the payload, depending on impl shape).
    call_args, call_kwargs = mock_telegram_ok.call_args
    # Impl contract: _post_to_telegram(token, chat_id, text, parse_mode)
    # OR _post_to_telegram(payload_dict, token=...) — accept either.
    text = None
    for a in call_args:
        if isinstance(a, str) and "gate-notif-test-002" in a:
            text = a
            break
        if isinstance(a, dict) and "text" in a:
            text = a["text"]
            break
    for v in call_kwargs.values():
        if isinstance(v, str) and "gate-notif-test-002" in v:
            text = v
        elif isinstance(v, dict) and "text" in v:
            text = v["text"]
    assert text is not None, f"Couldn't find text in call: {call_args} {call_kwargs}"

    # Required fields. The impl escapes underscores for Telegram Markdown
    # (e.g. `research_decision` → `research\_decision`), so check on a
    # de-escaped copy of the text.
    text_unescaped = text.replace(r"\_", "_").replace(r"\*", "*")
    assert "gate-notif-test-002" in text_unescaped, "gate id missing"
    assert "research_decision" in text_unescaped, "kind missing"
    assert "low" in text_unescaped.lower(), "risk_level missing"
    # GitHub blob URL
    assert "github.com/vdk888/bubble-ops-fixture" in text \
        or "github.com" in text and "bubble-ops-fixture" in text
    assert "abc123def456" in text or "queues/gates/gate-notif-test-002.yaml" in text


def test_notify_gate_includes_4_actions(gate_file, fake_token_env, mock_telegram_ok):
    """Message must contain the 4 actions Joris can copy-paste back:
    approve / reject / modify / defer."""
    repo_root, gate_path = gate_file
    rel = gate_path.relative_to(repo_root)

    notify.notify_gate(
        gate_path=str(rel),
        repo="bubble-ops-fixture",
        repo_root=str(repo_root),
    )

    call_args, call_kwargs = mock_telegram_ok.call_args
    text = None
    for a in list(call_args) + list(call_kwargs.values()):
        if isinstance(a, str) and "approve" in a.lower():
            text = a
            break
        if isinstance(a, dict) and "text" in a and "approve" in a["text"].lower():
            text = a["text"]
            break
    assert text is not None, "No text containing 'approve' found in call"

    text_lower = text.lower()
    assert "approve" in text_lower, "missing 'approve' action"
    assert "reject" in text_lower, "missing 'reject' action"
    assert "modify" in text_lower, "missing 'modify' action"
    assert "defer" in text_lower, "missing 'defer' action"
    # The gate id should be quoted alongside each action so Joris can
    # copy-paste a single line back.
    assert text_lower.count("gate-notif-test-002") >= 1


def test_notify_gate_uses_fixture_bot_not_morty(
    gate_file, monkeypatch, mock_telegram_ok
):
    """Token must come from FIXTURE_TELEGRAM_BOT_TOKEN (preferred) or
    TELEGRAM_BOT_TOKEN (fallback). It must NEVER read a global / morty
    token name like TELEGRAM_REPORTER_TOKEN or BOT_TOKEN_MAIN."""
    monkeypatch.setenv("FIXTURE_TELEGRAM_BOT_TOKEN", "FIXTURE_TOKEN_SENTINEL")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "WRONG_TOKEN_DO_NOT_USE")
    monkeypatch.setenv("TELEGRAM_REPORTER_TOKEN", "ALSO_WRONG")
    monkeypatch.setenv("BOT_TOKEN_MAIN", "ALSO_WRONG_2")

    repo_root, gate_path = gate_file
    rel = gate_path.relative_to(repo_root)

    notify.notify_gate(
        gate_path=str(rel),
        repo="bubble-ops-fixture",
        repo_root=str(repo_root),
    )

    # The first positional arg is the token (per our contract).
    call_args, call_kwargs = mock_telegram_ok.call_args
    token_seen = None
    for a in call_args:
        if isinstance(a, str) and "TOKEN" in a:
            token_seen = a
            break
    for v in call_kwargs.values():
        if isinstance(v, str) and "TOKEN" in v:
            token_seen = v
            break
    assert token_seen == "FIXTURE_TOKEN_SENTINEL", \
        f"Expected FIXTURE_TELEGRAM_BOT_TOKEN, got: {token_seen!r}"


def test_notify_gate_falls_back_to_telegram_bot_token(
    gate_file, monkeypatch, mock_telegram_ok
):
    """When FIXTURE_TELEGRAM_BOT_TOKEN is unset, fall back to
    TELEGRAM_BOT_TOKEN (which is what's actually deployed in
    /run/claude-agent-fixture/env today)."""
    monkeypatch.delenv("FIXTURE_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FALLBACK_TOKEN_OK")

    repo_root, gate_path = gate_file
    rel = gate_path.relative_to(repo_root)

    result = notify.notify_gate(
        gate_path=str(rel),
        repo="bubble-ops-fixture",
        repo_root=str(repo_root),
    )

    assert result["notification_sent"] is True
    call_args, call_kwargs = mock_telegram_ok.call_args
    token_seen = None
    for a in list(call_args) + list(call_kwargs.values()):
        if isinstance(a, str) and "TOKEN" in a:
            token_seen = a
            break
    assert token_seen == "FALLBACK_TOKEN_OK"


def test_notify_gate_handles_telegram_api_failure_gracefully(
    gate_file, fake_token_env, mock_telegram_5xx, capsys
):
    """If Bot API returns 5xx, return non-zero exit code (via return dict),
    log a structured error to stderr, but do NOT raise."""
    repo_root, gate_path = gate_file
    rel = gate_path.relative_to(repo_root)

    result = notify.notify_gate(
        gate_path=str(rel),
        repo="bubble-ops-fixture",
        repo_root=str(repo_root),
    )

    assert result["notification_sent"] is False
    assert "error" in result
    assert "502" in result["error"] or "Telegram" in result["error"]

    captured = capsys.readouterr()
    # Error should be visible in stderr
    assert "error" in captured.err.lower() or "fail" in captured.err.lower()


def test_notify_gate_redacts_token_in_logs(gate_file, monkeypatch, mock_telegram_5xx, capsys):
    """Even on failure, the actual token value MUST NOT appear in stderr
    or stdout — anywhere it would be written (logs, traceback, structured
    output). Use a recognisable sentinel so any leak is unambiguous."""
    SECRET = "SUPER_SECRET_TOKEN_VALUE_42_DO_NOT_LEAK_ME"
    monkeypatch.setenv("FIXTURE_TELEGRAM_BOT_TOKEN", SECRET)

    repo_root, gate_path = gate_file
    rel = gate_path.relative_to(repo_root)

    result = notify.notify_gate(
        gate_path=str(rel),
        repo="bubble-ops-fixture",
        repo_root=str(repo_root),
    )

    captured = capsys.readouterr()
    assert SECRET not in captured.out, "TOKEN LEAKED in stdout!"
    assert SECRET not in captured.err, "TOKEN LEAKED in stderr!"
    assert SECRET not in json.dumps(result), "TOKEN LEAKED in return dict!"


def test_notify_gate_rejects_missing_file(tmp_path, fake_token_env):
    """If the gate file does not exist, return an input-error dict."""
    result = notify.notify_gate(
        gate_path="queues/gates/does-not-exist.yaml",
        repo="bubble-ops-fixture",
        repo_root=str(tmp_path),
    )
    assert result["notification_sent"] is False
    assert "error" in result
    assert "not found" in result["error"].lower() or "does not exist" in result["error"].lower()


def test_notify_gate_builds_correct_github_blob_url(
    gate_file, fake_token_env, mock_telegram_ok
):
    """With commit_sha: github.com/vdk888/<repo>/blob/<sha>/<path>.
    Without commit_sha: github.com/vdk888/<repo>/blob/main/<path>."""
    repo_root, gate_path = gate_file
    rel = gate_path.relative_to(repo_root)

    # With commit_sha
    notify.notify_gate(
        gate_path=str(rel),
        repo="bubble-ops-fixture",
        commit_sha="deadbeef" * 5,
        repo_root=str(repo_root),
    )
    text = _extract_text(mock_telegram_ok)
    assert "deadbeef" in text, f"commit_sha missing from URL: {text}"
    assert "queues/gates/gate-notif-test-002.yaml" in text

    # Without commit_sha — defaults to main
    mock_telegram_ok.reset_mock()
    notify.notify_gate(
        gate_path=str(rel),
        repo="bubble-ops-fixture",
        repo_root=str(repo_root),
    )
    text2 = _extract_text(mock_telegram_ok)
    assert "/blob/main/" in text2 or "/main/" in text2


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _extract_text(mock_obj):
    """Pull the 'text' (or any str arg containing a gate id) from the
    mock call args."""
    call_args, call_kwargs = mock_obj.call_args
    for a in call_args:
        if isinstance(a, str) and ("gate-" in a or "approve" in a.lower()):
            return a
        if isinstance(a, dict) and "text" in a:
            return a["text"]
    for v in call_kwargs.values():
        if isinstance(v, str) and ("gate-" in v or "approve" in v.lower()):
            return v
        if isinstance(v, dict) and "text" in v:
            return v["text"]
    return ""
