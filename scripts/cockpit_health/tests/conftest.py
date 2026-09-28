"""Credential isolation for cockpit_health tests (mirrors scripts/lib/tests/
conftest.py, #1598 — never let a test accidentally read a real Telegram/GH
credential from the environment)."""

import pytest


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    for name in ("TELEGRAM_STATE_DIR", "TELEGRAM_BOT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
