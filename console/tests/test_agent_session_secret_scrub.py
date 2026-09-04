"""test_agent_session_secret_scrub.py — board #1116 (cockpit audit).

console/services/agent_session.py's `_tool_detail` renders raw
Bash.command / WebFetch.url (etc.) from an agent's OWN transcript to
EVERY cockpit viewer, unredacted — an agent routinely types
`export TOKEN=...` / `curl -H "Authorization: Bearer ..."` into a Bash
call, and pre-fix that landed verbatim in the session feed. `_scrub_secrets`
now redacts anything secret-shaped before `_tool_detail` (and therefore
`read_session_turns`/`SessionTurn.detail`) ever returns it.
"""
from __future__ import annotations

import json

from console.services.agent_session import (
    _scrub_secrets,
    _tool_detail,
    read_session_turns,
)


# ─── _scrub_secrets — unit level ────────────────────────────────────────────


def test_redacts_env_export_token():
    out = _scrub_secrets("export X_TOKEN=abc123supersecretvalue")
    assert "abc123supersecretvalue" not in out
    assert "[redacted]" in out


def test_redacts_compound_env_var_name_token():
    """The classic case: TOKEN isn't \\b-bounded inside TELEGRAM_BOT_TOKEN
    (underscore is a word char) — must still redact the VALUE."""
    out = _scrub_secrets("export TELEGRAM_BOT_TOKEN=8350575119:AAHreal-looking-suffix")
    assert "8350575119:AAHreal-looking-suffix" not in out
    assert "[redacted]" in out


def test_redacts_authorization_bearer_header():
    out = _scrub_secrets('curl -H "Authorization: Bearer sk-ant-oat01-abcdefghijklmnop"')
    assert "sk-ant-oat01-abcdefghijklmnop" not in out
    assert "Bearer" not in out or "[redacted]" in out
    assert "[redacted]" in out


def test_redacts_bearer_value_fully_not_just_first_word():
    """Regression guard: a naive `Bearer[=: ]+\\S+`-only match on the
    KEYWORD pattern would consume only 'Bearer' and leave the real token
    exposed right after it. The dedicated Bearer pattern must run first."""
    out = _scrub_secrets("Authorization: Bearer ghp_ABCDEFGHIJ0123456789abcdefghij")
    assert "ghp_ABCDEFGHIJ0123456789abcdefghij" not in out


def test_redacts_github_pat():
    out = _scrub_secrets("git clone https://github_pat_11ABCDEFG0123456789_realvaluehere@github.com/x/y")
    assert "github_pat_11ABCDEFG0123456789_realvaluehere" not in out


def test_redacts_openrouter_key():
    out = _scrub_secrets("OPENROUTER_API_KEY=sk-or-v1-0123456789abcdef0123456789abcdef")
    assert "sk-or-v1-0123456789abcdef0123456789abcdef" not in out


def test_redacts_age_secret_key():
    out = _scrub_secrets("cat /etc/age/key.txt # AGE-SECRET-KEY-1QWERTYUIOPASDFGHJKLZXCVBNM123456")
    assert "AGE-SECRET-KEY-1QWERTYUIOPASDFGHJKLZXCVBNM123456" not in out


def test_redacts_password_assignment():
    out = _scrub_secrets("mysql -u root --password=hunter2reallysecretpw")
    assert "hunter2reallysecretpw" not in out


def test_leaves_ordinary_command_untouched():
    out = _scrub_secrets("ls -la /home/claude/scripts")
    assert out == "ls -la /home/claude/scripts"


def test_leaves_ordinary_url_untouched():
    out = _scrub_secrets("https://api.github.com/repos/Bubble-invest/bubble-ops-loop/pulls")
    assert "[redacted]" not in out


def test_empty_and_none_safe():
    assert _scrub_secrets("") == ""
    assert _scrub_secrets(None) is None


# ─── _tool_detail — the actual render path ──────────────────────────────────


def test_tool_detail_scrubs_bash_command():
    detail = _tool_detail("Bash", {"command": "export GITHUB_TOKEN=ghp_realtoken1234567890123456 && git push"})
    assert "ghp_realtoken1234567890123456" not in detail
    assert "[redacted]" in detail


def test_tool_detail_scrubs_webfetch_url_with_query_token():
    detail = _tool_detail("WebFetch", {"url": "https://api.example.com/data?access_token=verysecretvalue123"})
    assert "verysecretvalue123" not in detail


def test_tool_detail_file_path_unaffected_by_scrub():
    """A file path is not secret-shaped — must render exactly as before
    (basename-only), not accidentally mangled by the scrubber."""
    detail = _tool_detail("Read", {"file_path": "/etc/bubble/secrets.sops.env"})
    assert detail == "secrets.sops.env"


def test_tool_detail_fallback_branch_also_scrubbed():
    """Tools not in _TOOL_DETAIL_KEYS fall through to the generic
    'first string value' branch — that must be scrubbed too."""
    detail = _tool_detail("SomeOtherTool", {"note": "using API_KEY=abcdefghijklmnopqrstuvwx now"})
    assert "abcdefghijklmnopqrstuvwx" not in detail


# ─── read_session_turns — end-to-end through the real transcript path ──────


def _seed(home, suffix, turns, fname="s.jsonl"):
    d = home / ".claude" / "projects" / f"-home-claude-agents-{suffix}"
    d.mkdir(parents=True, exist_ok=True)
    f = d / fname
    with f.open("w", encoding="utf-8") as fh:
        for t in turns:
            fh.write(json.dumps(t) + "\n")
    return f


def test_session_feed_never_leaks_secret_from_bash_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = _seed(tmp_path, "bubble-ops-ben", [
        {"timestamp": "2026-09-04T08:00:00Z", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": 'curl -H "Authorization: Bearer sk-ant-oat01-LIVEVALUEDONOTLEAK123" https://x'}},
        ]}},
    ])
    turns = read_session_turns(str(f))
    assert len(turns) == 1
    assert "sk-ant-oat01-LIVEVALUEDONOTLEAK123" not in turns[0].detail
