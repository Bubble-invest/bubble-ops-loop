"""test_1548_telegram_token_state_dir_fallback.py — board #1548 follow-up.

Board #1548 deliberately keeps TELEGRAM_BOT_TOKEN OUT of the claude REPL env
on the Mac local-loop wrapper (every Agent-tool subagent + Bash subshell
inherits that env — the exact leak the card reports). The wrapper instead
writes the token straight into ``$TELEGRAM_STATE_DIR/.env`` (0600), exactly
where the telegram MCP plugin (server.ts) already reads it from.

``scripts/lib/notify.py``'s ``TelegramBackend`` is a DIFFERENT consumer of the
same token — it is invoked as a plain Bash-tool subprocess from WITHIN a
running dept session (``tools/notify_layer.py`` at CLAUDE.md STEP F), so it
used to rely on TELEGRAM_BOT_TOKEN being present in that subshell's inherited
env. Without a fallback, board #1548's fix would silently break every dept's
STEP F "layer fired" Telegram ping on the Mac. This locks in the fix: the
backend now falls back to reading ``$TELEGRAM_STATE_DIR/.env`` the same way
the plugin does, when the env var itself isn't set.

Covers:
  T1  env var present -> used directly (state-dir fallback not even touched).
  T2  env var ABSENT, TELEGRAM_STATE_DIR unset -> still MissingCredentialError
      (no fallback possible; unchanged pre-#1548 behavior).
  T3  env var ABSENT, TELEGRAM_STATE_DIR set but no .env there -> still
      MissingCredentialError (fail loud, never silently swallow).
  T4  env var ABSENT, a real .env at $TELEGRAM_STATE_DIR/.env -> the token is
      read from there and a send succeeds.
  T5  the state-dir .env's OTHER lines (comments, unrelated keys) don't
      confuse the parser; only the TELEGRAM_BOT_TOKEN= line is used.
  T6  env var present WINS over a stale value sitting in the state-dir .env
      (mirrors server.ts's "real env wins" contract — never override a
      legitimately-set env var with a fallback file).
"""

from __future__ import annotations

import pytest

from scripts.lib import notify


CONFIG: dict = {}


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _CapturingOpener:
    def __init__(self):
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append({"url": request.full_url})
        return _FakeResponse(b'{"ok": true, "result": {"message_id": 1}}')


def _backend(opener):
    return notify.TelegramBackend(CONFIG, _opener=opener)


def test_env_var_present_used_directly(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN:fromenv")
    monkeypatch.delenv("TELEGRAM_STATE_DIR", raising=False)
    backend = _backend(_CapturingOpener())
    assert backend._read_token() == "TESTTOKEN:fromenv"


def test_missing_everywhere_raises_clear_error(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_STATE_DIR", raising=False)
    backend = _backend(_CapturingOpener())
    with pytest.raises(notify.MissingCredentialError, match="TELEGRAM_BOT_TOKEN"):
        backend._read_token()


def test_state_dir_set_but_no_env_file_still_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(tmp_path / "does-not-exist"))
    backend = _backend(_CapturingOpener())
    with pytest.raises(notify.MissingCredentialError):
        backend._read_token()


def test_state_dir_env_file_fallback_used(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    state_dir = tmp_path / "telegram-state"
    state_dir.mkdir()
    (state_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=FROM-STATE-DIR-1548\n")
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(state_dir))
    backend = _backend(_CapturingOpener())
    assert backend._read_token() == "FROM-STATE-DIR-1548"


def test_state_dir_env_file_other_lines_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    state_dir = tmp_path / "telegram-state"
    state_dir.mkdir()
    (state_dir / ".env").write_text(
        "OTHER_KEY=keepme\n"
        "# a comment line\n"
        "TELEGRAM_BOT_TOKEN=FROM-STATE-DIR-WITH-EXTRAS\n"
    )
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(state_dir))
    backend = _backend(_CapturingOpener())
    assert backend._read_token() == "FROM-STATE-DIR-WITH-EXTRAS"


def test_env_var_wins_over_state_dir_file(monkeypatch, tmp_path):
    """Mirrors server.ts's "real env wins" contract: a live env var must never
    be silently overridden by a (possibly stale) state-dir .env value."""
    state_dir = tmp_path / "telegram-state"
    state_dir.mkdir()
    (state_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=STALE-FILE-VALUE\n")
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "LIVE-ENV-VALUE")
    backend = _backend(_CapturingOpener())
    assert backend._read_token() == "LIVE-ENV-VALUE"


def test_send_succeeds_end_to_end_via_state_dir_fallback(monkeypatch, tmp_path):
    """Full send() path (not just _read_token()) works off the fallback —
    proves tools/notify_layer.py's STEP F ping keeps working after board
    #1548 removes TELEGRAM_BOT_TOKEN from the claude REPL env."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    state_dir = tmp_path / "telegram-state"
    state_dir.mkdir()
    (state_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=FROM-STATE-DIR-1548\n")
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(state_dir))

    opener = _CapturingOpener()
    backend = _backend(opener)
    payload = notify.NotificationPayload(
        subject="test", markdown_body="hello", attachments=(), priority="normal", metadata={}
    )
    receipt = backend.send(payload, recipient="123456")
    assert receipt.success is True
    assert len(opener.calls) == 1
    assert "FROM-STATE-DIR-1548" in opener.calls[0]["url"]
