"""Credential isolation and safe Telegram failure diagnostics (#1598)."""

import io
import json
import os
import urllib.error

import pytest

from scripts.lib import notify


def test_inherited_credentials_are_removed(tmp_path):
    assert not any(name in os.environ for name in (
        "TELEGRAM_STATE_DIR", "TELEGRAM_BOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"
    ))
    assert os.environ["HOME"] == str(tmp_path)


@pytest.mark.parametrize("failure", ["http", "transport", "api"])
@pytest.mark.parametrize("recipient", ["123", "123,456"])
def test_telegram_errors_do_not_echo_credentials(monkeypatch, tmp_path, failure, recipient):
    token = "123456:FAKEFAKE-secret-for-regression"
    other_token = "ghs_FAKE-regression"
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={token}\n")
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(tmp_path))
    echoed = token + " " + other_token

    def opener(request, timeout):
        if failure == "http":
            raise urllib.error.HTTPError(
                request.full_url, 401, echoed, {}, io.BytesIO(echoed.encode())
            )
        if failure == "transport":
            raise urllib.error.URLError(request.full_url + echoed)
        return io.BytesIO(json.dumps({"ok": False, "description": echoed}).encode())

    receipt = notify.TelegramBackend({}, _opener=opener).send(
        notify.NotificationPayload("test", "hello"), recipient
    )
    assert receipt.success is False
    assert receipt.error
    assert token not in repr(receipt)
    assert other_token not in repr(receipt)


def test_state_dir_invalid_encoding_is_missing_credential(monkeypatch, tmp_path):
    (tmp_path / ".env").write_bytes(b"TELEGRAM_BOT_TOKEN=\xff")
    monkeypatch.setenv("TELEGRAM_STATE_DIR", str(tmp_path))
    with pytest.raises(notify.MissingCredentialError):
        notify.TelegramBackend({})._read_token()


@pytest.mark.parametrize("lookup", ["channel", "account", "backend"])
def test_config_errors_do_not_echo_values(lookup):
    token = "ghs_FAKE-config-value"
    with pytest.raises(ValueError) as caught:
        if lookup == "channel":
            notify.resolve_recipients("operator", [token], {})
        elif lookup == "account":
            notify.resolve_recipients(token, [notify.CHANNEL_EMAIL], {})
        else:
            notify._get_backend(notify.CHANNEL_EMAIL, {"email": {"backend": token}})
    assert token not in str(caught.value)


@pytest.mark.parametrize("failure", ["mime", "smtp"])
def test_email_errors_do_not_echo_credentials(monkeypatch, failure):
    token = "ghs_FAKE-email-error"
    monkeypatch.setenv("SMTP_USER", "test@example.invalid")
    monkeypatch.setenv("SMTP_PASSWORD", token)
    backend = notify.SMTPEmailBackend({})

    def fail(*args, **kwargs):
        raise ValueError(token)

    if failure == "mime":
        monkeypatch.setattr(backend, "_build_message", fail)
    else:
        monkeypatch.setattr(notify.smtplib, "SMTP", fail)
    receipt = backend.send(notify.NotificationPayload("test", "hello"), "test@example.invalid")
    assert receipt.success is False
    assert token not in repr(receipt)
    assert "ValueError" in receipt.error


def test_request_construction_error_does_not_echo_token(monkeypatch):
    token = "ghs_FAKE-request-error"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)

    def fail(*args, **kwargs):
        raise ValueError(token)

    monkeypatch.setattr(notify.urllib.request, "Request", fail)
    receipt = notify.TelegramBackend({}).send(notify.NotificationPayload("test", "hello"), "123")
    assert receipt.success is False
    assert token not in repr(receipt)
