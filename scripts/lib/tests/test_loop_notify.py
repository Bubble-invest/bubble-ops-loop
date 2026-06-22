"""test_loop_notify.py — WS3 per-layer-fire notify helpers.

Covers:
  * notify_layer_fired (L1/L4 IMMEDIATE): URL, chat_id, parse_mode, message
    shape ``🔁 <dept> · L<N> fired — <first line>``; summary-missing fallback.
  * notify_layers_batched (L2/L3 BATCHED): ONE line ``🔁 <dept> · L2 ×3, L3 ×1``;
    layers with 0 fires omitted; all-zero → no send (returns None).
  * the RED→GREEN-anchoring batched-vs-immediate distinction (an immediate ping
    must NOT carry batched "×n" syntax; a batched ping must NOT say "fired").
  * missing TELEGRAM_BOT_TOKEN → clear error surfaced as a non-fatal failed
    receipt (mirrors Maya's pattern).
  * graceful email degradation: no SMTP creds → Telegram still delivers,
    email returns a failed-but-non-fatal receipt (per-channel isolation in the
    promoted notify.deliver).

No live HTTP — a fake opener captures every request.
"""

from __future__ import annotations

import json

import pytest

from scripts.lib import loop_notify
from scripts.lib import notify


# ─── Fakes ─────────────────────────────────────────────────────────────────


class _FakeResponse:
    """Minimal context-manager mimicking urllib's response object."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _CapturingOpener:
    """urllib-like opener that records each Request and returns a canned ok."""

    def __init__(self, ok_result_message_id: int = 4242):
        self.calls = []
        self._mid = ok_result_message_id

    def __call__(self, req, timeout=None):
        # Record the URL, method, headers, and decoded JSON body.
        data = req.data.decode("utf-8") if req.data else ""
        self.calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "headers": dict(req.header_items()),
                "body": json.loads(data) if data else {},
                "timeout": timeout,
            }
        )
        resp = {"ok": True, "result": {"message_id": self._mid}}
        return _FakeResponse(json.dumps(resp).encode("utf-8"), status=200)


# ─── Fixtures ──────────────────────────────────────────────────────────────


CONFIG = {
    "accounts": {
        "Operator": {"telegram_chat_id": "111222333", "email": "operator@example.com"},
        "Operator2": {"telegram_chat_id": "999888777", "email": "operator2@example.com"},
    },
    "dept_label": "maya",
}


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN:abc")
    yield


# ─── notify_layer_fired (immediate) ────────────────────────────────────────


def test_layer_fired_url_chatid_and_message_shape(tmp_path, token_env):
    summary = tmp_path / "summary.md"
    summary.write_text("# L1 brief: 3 prospects queued for review\nmore detail\n")
    opener = _CapturingOpener()

    receipt = loop_notify.notify_layer_fired(
        "maya", 1, summary, config=CONFIG, opener=opener
    )

    assert receipt.success is True
    assert len(opener.calls) == 1
    call = opener.calls[0]
    # URL uses the env token + sendMessage
    assert call["url"] == "https://api.telegram.org/botTESTTOKEN:abc/sendMessage"
    assert call["method"] == "POST"
    # chat_id resolved from config (NOT hardcoded)
    assert call["body"]["chat_id"] == "111222333"
    assert call["body"]["parse_mode"] == "MarkdownV2"
    # Message shape: 🔁 maya · L1 fired — <first line, heading stripped>
    text = call["body"]["text"]
    assert "🔁" in text
    assert "maya" in text
    assert "L1 fired" in text
    assert "3 prospects queued for review" in text


def test_layer_fired_accepts_L_prefixed_layer(tmp_path, token_env):
    opener = _CapturingOpener()
    loop_notify.notify_layer_fired("tony", "L4", None, config=CONFIG, opener=opener)
    text = opener.calls[0]["body"]["text"]
    assert "L4 fired" in text
    # no double-L (e.g. "LL4")
    assert "LL4" not in text


def test_layer_fired_missing_summary_omits_tail(tmp_path, token_env):
    opener = _CapturingOpener()
    loop_notify.notify_layer_fired(
        "cgp", 4, tmp_path / "nope.md", config=CONFIG, opener=opener
    )
    text = opener.calls[0]["body"]["text"]
    assert "L4 fired" in text
    # No "—" tail when there's no summary content.
    assert "—" not in text


def test_layer_fired_missing_token_is_clear_nonfatal(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    opener = _CapturingOpener()
    receipt = loop_notify.notify_layer_fired(
        "maya", 1, None, config=CONFIG, opener=opener
    )
    # No HTTP attempted, receipt is failed-but-non-fatal with a clear message.
    assert opener.calls == []
    assert receipt.success is False
    assert "TELEGRAM_BOT_TOKEN" in (receipt.error or "")


def test_layer_fired_multi_account_fans_out(tmp_path, token_env):
    opener = _CapturingOpener()
    loop_notify.notify_layer_fired(
        "maya", 1, None, config=CONFIG, account=["Operator", "Operator2"], opener=opener
    )
    chat_ids = {c["body"]["chat_id"] for c in opener.calls}
    assert chat_ids == {"111222333", "999888777"}


# ─── notify_layers_batched (batched) ───────────────────────────────────────


def test_batched_single_line_shape(token_env):
    opener = _CapturingOpener()
    receipt = loop_notify.notify_layers_batched(
        "maya", {"2": 3, "3": 1}, config=CONFIG, opener=opener
    )
    assert receipt.success is True
    # ONE message for the whole tick (single chat_id).
    assert len(opener.calls) == 1
    text = opener.calls[0]["body"]["text"]
    assert "🔁" in text
    assert "maya" in text
    assert "L2 ×3" in text
    assert "L3 ×1" in text


def test_batched_omits_zero_count_layers(token_env):
    opener = _CapturingOpener()
    loop_notify.notify_layers_batched(
        "maya", {"2": 0, "3": 2}, config=CONFIG, opener=opener
    )
    text = opener.calls[0]["body"]["text"]
    assert "L3 ×2" in text
    assert "L2" not in text


def test_batched_all_zero_sends_nothing(token_env):
    opener = _CapturingOpener()
    receipt = loop_notify.notify_layers_batched(
        "maya", {"2": 0, "3": 0}, config=CONFIG, opener=opener
    )
    assert receipt is None
    assert opener.calls == []


def test_format_batched_line_ascending_order():
    # pure formatter — no env / no HTTP
    line = loop_notify.format_batched_line("cgp", {"3": 1, "2": 4})
    assert line == "🔁 cgp · L2 ×4, L3 ×1"


# ─── batched vs immediate distinction (the red→green anchor) ───────────────


def test_immediate_vs_batched_are_distinct_shapes(token_env):
    """An IMMEDIATE L1/L4 ping says 'L<N> fired' and never uses the batched
    '×n' coalescing syntax; a BATCHED L2/L3 ping uses '×n' and never says
    'fired'. This is the core verbosity contract from the plan."""
    op_imm = _CapturingOpener()
    loop_notify.notify_layer_fired("maya", 1, None, config=CONFIG, opener=op_imm)
    imm_text = op_imm.calls[0]["body"]["text"]

    op_bat = _CapturingOpener()
    loop_notify.notify_layers_batched(
        "maya", {"2": 2, "3": 1}, config=CONFIG, opener=op_bat
    )
    bat_text = op_bat.calls[0]["body"]["text"]

    assert "fired" in imm_text and "×" not in imm_text
    assert "×" in bat_text and "fired" not in bat_text


# ─── graceful email degradation via the promoted notify.deliver ────────────


def test_email_degrades_no_creds_telegram_still_sent(monkeypatch):
    """No SMTP creds → email receipt failed-but-non-fatal; Telegram still
    delivered. Exercises the promoted module's per-channel failure isolation."""
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TESTTOKEN:abc")
    # Disable any inherited dry-run so we hit the real cred check path.
    monkeypatch.delenv("MAYA_DRY_RUN", raising=False)

    opener = _CapturingOpener()
    tg_backend = notify.TelegramBackend(CONFIG, _opener=opener)
    payload = notify.NotificationPayload(
        subject="[Test] both channels", markdown_body="body"
    )
    receipts = notify.deliver(
        payload,
        [notify.CHANNEL_EMAIL, notify.CHANNEL_TELEGRAM_ALERT],
        account_used="Operator",
        config=CONFIG,
        _backends={notify.CHANNEL_TELEGRAM_ALERT: tg_backend},
    )
    by_ch = {r.channel: r for r in receipts}
    # Email failed (no creds) but did NOT raise / abort the other channel.
    assert by_ch[notify.CHANNEL_EMAIL].success is False
    assert "SMTP" in (by_ch[notify.CHANNEL_EMAIL].error or "").upper() or "CRED" in (
        by_ch[notify.CHANNEL_EMAIL].error or ""
    ).upper()
    # Telegram still went out.
    assert by_ch[notify.CHANNEL_TELEGRAM_ALERT].success is True
    assert len(opener.calls) == 1
    assert opener.calls[0]["body"]["chat_id"] == "111222333"


def test_dept_agnostic_no_maya_hardcoded_in_alert():
    """notify_alert label comes from config, not a hardcoded 'Maya'."""
    # Build the alert payload path: ensure subject uses dept_label from config.
    # We call via a stubbed backend to avoid network.
    captured = {}

    class _StubBackend:
        name = notify.CHANNEL_TELEGRAM_ALERT

        def send(self, payload, recipient):
            captured["subject"] = payload.subject
            return notify.DeliveryReceipt(
                channel=self.name,
                recipient=recipient,
                success=True,
                delivered_at="2026-06-02T00:00:00+02:00",
            )

    cfg = {
        "dept_label": "tony",
        "accounts": {
            "_system": {"telegram_chat_id": "555", "email": "ops@example.com"},
        },
        # Restrict the alert to the telegram channel only so the test exercises
        # the dept-label path without needing a live SMTP backend.
        "notifications": {"_alert_default": {"channels": [notify.CHANNEL_TELEGRAM_ALERT]}},
    }
    notify.notify_alert(
        "kill switch tripped",
        severity="critical",
        config=cfg,
        _backends={notify.CHANNEL_TELEGRAM_ALERT: _StubBackend()},
    )
    assert "tony" in captured["subject"]
    assert "Maya" not in captured["subject"]


# ─── cockpit link in layer-fired pings ({{OPERATOR}} msg 3985, 2026-06-06) ──────────


def test_layer_fired_includes_cockpit_link(tmp_path, token_env):
    """Every layer-fired ping must carry the dept cockpit link so {{OPERATOR}} can
    open the work directly from Telegram."""
    summary = tmp_path / "summary.md"
    summary.write_text("# L4 risk brief done\n")
    opener = _CapturingOpener()
    loop_notify.notify_layer_fired("ben", 4, summary, config=CONFIG, opener=opener)
    text = opener.calls[0]["body"]["text"]
    # the /dept/ben path must be present (escaping of . and - is fine for MDV2)
    assert "/dept/ben" in text
    assert "8443" in text


def test_batched_includes_cockpit_link(token_env):
    opener = _CapturingOpener()
    loop_notify.notify_layers_batched("maya", {"2": 3, "3": 1}, config=CONFIG, opener=opener)
    text = opener.calls[0]["body"]["text"]
    assert "/dept/maya" in text
    # still carries the batched counts
    assert "L2" in text and "L3" in text


def test_cockpit_base_url_env_override(tmp_path, token_env, monkeypatch):
    import importlib
    monkeypatch.setenv("BUBBLE_COCKPIT_BASE_URL", "https://example.test:9999/")
    importlib.reload(loop_notify)
    try:
        assert loop_notify._cockpit_link("tony") == "https://example.test:9999/dept/tony"
    finally:
        monkeypatch.delenv("BUBBLE_COCKPIT_BASE_URL", raising=False)
        importlib.reload(loop_notify)


# ─── test prefix + artifact gate ({{OPERATOR}} msg 4022, 2026-06-07) ────────────────


def test_layer_fired_test_flag_prefixes_marker(tmp_path, token_env):
    """A verification ping (test=True) must be visibly marked so it can't be
    mistaken for a real layer-fire (the 2026-06-07 false-L4-ping incident)."""
    opener = _CapturingOpener()
    loop_notify.notify_layer_fired("ben", 4, None, config=CONFIG, opener=opener, test=True)
    text = opener.calls[0]["body"]["text"]
    assert "TEST" in text
    assert "\U0001F9EA" in text  # 🧪
    assert "L4 fired" in text


def test_layer_fired_no_test_flag_has_no_marker(tmp_path, token_env):
    summary = tmp_path / "s.md"
    summary.write_text("# real L4 export done\n")
    opener = _CapturingOpener()
    loop_notify.notify_layer_fired("ben", 4, summary, config=CONFIG, opener=opener)
    text = opener.calls[0]["body"]["text"]
    assert "TEST" not in text and "\U0001F9EA" not in text
