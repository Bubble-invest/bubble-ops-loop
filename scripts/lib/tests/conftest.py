"""Credential isolation for the shared script tests."""

import pytest


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    """Never read operator credentials or Telegram state during tests (#1598)."""
    for name in ("TELEGRAM_STATE_DIR", "TELEGRAM_BOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
