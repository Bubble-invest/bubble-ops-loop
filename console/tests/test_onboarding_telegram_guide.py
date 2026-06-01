"""
Phase G4 — onboarding page must replace the "claude code" terminal CTA
with a "Comment lui parler" Telegram guide.

Spec: msg 2720, 2026-05-21.
"""
from __future__ import annotations

import re


def test_onboarding_page_contains_bot_handle(client):
    """The page must explicitly name the dedicated bot @bubbleops<slug>_bot."""
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text
    # miranda has no dashes -> handle is @bubbleopsmiranda_bot
    assert "@bubbleopsmiranda_bot" in body, \
        "onboarding page must contain the dedicated bot handle"


def test_onboarding_page_contains_3_step_telegram_instructions(client):
    """The page must contain 3 numbered steps explaining how to message her."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text.lower()
    # Look for a numbered list (1./2./3.) inside the "comment lui parler" section
    # or for the keywords ouvre / cherche / /start in that order.
    assert "ouvre telegram" in body or "ouvre l'application telegram" in body, \
        "instruction 'Ouvre Telegram' missing"
    assert "cherche" in body and "bubbleops" in body, \
        "instruction to search for the bot handle missing"
    assert "/start" in body, "instruction to type /start missing"


def test_onboarding_page_no_longer_contains_claude_code_terminal_cta(client):
    """The old `cd /tmp/ && claude code` line must be gone."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    # The legacy command should NOT appear in the page anymore.
    assert "cd /tmp/" not in body, \
        "old 'cd /tmp/...' terminal CTA must be removed"
    # 'claude code' literal as a command line should also be gone.
    # (We tolerate the words 'claude code' if used in prose context, but the
    # exact code-block invocation must not exist.)
    assert "&& claude code" not in body, \
        "old '... && claude code' command must be removed"


def test_onboarding_page_contains_telegram_deep_link(client):
    """The page must contain a deep-link to the bot conversation."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    # Either tg://resolve?domain=... or https://t.me/...
    has_link = (
        "t.me/bubbleopsmiranda_bot" in body
        or "tg://resolve?domain=bubbleopsmiranda_bot" in body
    )
    assert has_link, \
        "onboarding page must contain a Telegram deep-link to the bot"
