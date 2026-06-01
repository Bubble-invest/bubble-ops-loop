"""
test_setup_callback.py — POST /agents/new now defers GitHub work to the
operator-driven GitHub App "add repository" flow.

New contract (Wave-3 Step 0b, 2026-05-23 evening):

  POST /agents/new
    1. Validate fields (existing)
    2. Generate a 24-byte hex `state` token
    3. Store pending éclosion params in memory keyed by state
       (slug, display_name, owner, telegram_bot_token, level, children)
    4. Return an HTMX fragment with a link to:
       https://github.com/apps/bubble-ops-bot/installations/<INSTALLATION_ID>?state=<state>

  GET /agents/setup-callback?installation_id=<id>&setup_action=update&state=<state>
    1. Look up pending éclosion by state
    2. If state missing or unknown → 400
    3. If state expired (>15 min old) → 400 with operator-friendly message
    4. Kick off the éclosure chain in a background thread (same as before)
    5. Return an HTMX fragment / redirect to the existing SSE result page
       (so the browser can stream progress as before)
    6. Drop the state from the pending map (one-shot use)
"""
from __future__ import annotations

import re


# ─── POST /agents/new — returns "click here" link, not direct éclosion ────

def test_post_returns_github_app_install_link(client, monkeypatch):
    """POST /agents/new returns a link to the GitHub App installation
    config page so the operator can grant access to the new repo."""
    # Stub the launcher — should NOT be called yet on POST
    from console.services import eclosure_launcher
    launch_calls = []
    monkeypatch.setattr(
        eclosure_launcher, "launch",
        lambda **kw: launch_calls.append(kw) or {"ok": True, "github_app": {"ok": True}},
    )

    r = client.post(
        "/agents/new",
        data={
            "slug": "tony",
            "display_name": "Tony",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "management",
            "children": "fixture",
        },
    )
    assert r.status_code in (200, 202)
    body = r.text
    # The response must contain a link to the bubble-ops-bot installation page
    assert "github.com/apps/bubble-ops-bot/installations" in body
    # And a state= param (the éclosion key)
    assert "state=" in body
    # The launcher must NOT have been called yet — it fires only after the callback
    assert launch_calls == []


def test_post_state_token_is_random_per_request(client, monkeypatch):
    """Two consecutive POSTs produce different state tokens (basic entropy check)."""
    from console.services import eclosure_launcher
    monkeypatch.setattr(eclosure_launcher, "launch", lambda **kw: {"ok": True})
    base = {
        "slug": "x", "display_name": "X", "owner": "joris",
        "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
        "level": "ops",
    }
    r1 = client.post("/agents/new", data={**base, "slug": "first"})
    r2 = client.post("/agents/new", data={**base, "slug": "second"})
    s1 = re.search(r"state=([a-f0-9]+)", r1.text).group(1)
    s2 = re.search(r"state=([a-f0-9]+)", r2.text).group(1)
    assert s1 != s2
    assert len(s1) >= 32 and len(s2) >= 32   # at least 16 bytes hex


# ─── GET /agents/setup-callback ────────────────────────────────────────────

def test_callback_with_unknown_state_returns_400(client):
    """Callback with a state that doesn't match any pending éclosion → 400."""
    r = client.get(
        "/agents/setup-callback",
        params={
            "installation_id": "134075326",
            "setup_action": "update",
            "state": "this-state-does-not-exist-and-never-will",
        },
    )
    assert r.status_code == 400
    assert "state" in r.text.lower() or "unknown" in r.text.lower()


def test_callback_with_missing_state_returns_400(client):
    """Callback with no state param → 400."""
    r = client.get(
        "/agents/setup-callback",
        params={"installation_id": "1", "setup_action": "update"},
    )
    assert r.status_code == 400


def test_callback_with_missing_installation_id_returns_400(client):
    """Callback with no installation_id → 400 (the App-flow guarantees this is set)."""
    r = client.get(
        "/agents/setup-callback",
        params={"setup_action": "update", "state": "anything"},
    )
    assert r.status_code == 400


def test_callback_consumes_state_and_kicks_off_launcher(client, mock_bootstrap, monkeypatch):
    """Happy path: POST stores state; callback finds it + kicks off the
    éclosure chain with the right slug, telegram token, installation_id."""
    from console.services import eclosure_launcher
    launch_calls = []

    def fake_launch(**kw):
        launch_calls.append(kw)
        return {"ok": True, "github_app": {"ok": True, "installation_id": int(kw.get("installation_id", 0))}}

    monkeypatch.setattr(eclosure_launcher, "launch", fake_launch)

    # Step 1: POST /agents/new
    post = client.post(
        "/agents/new",
        data={
            "slug": "tonycb",
            "display_name": "Tony Callback",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "management",
            "children": "fixture",
        },
    )
    assert post.status_code in (200, 202)
    state = re.search(r"state=([a-f0-9]+)", post.text).group(1)

    # Step 2: GitHub redirects back to the callback URL
    cb = client.get(
        "/agents/setup-callback",
        params={
            "installation_id": "134075326",
            "setup_action": "update",
            "state": state,
        },
        follow_redirects=False,
    )
    # Callback may return 200 (HTMX fragment) or 303 (redirect to SSE page)
    assert cb.status_code in (200, 303), f"unexpected callback status: {cb.status_code}, body: {cb.text[:200]}"

    # Launcher must have been called with the right kwargs from the stored state
    assert len(launch_calls) == 1
    call = launch_calls[0]
    assert call["slug"] == "tonycb"
    # Token was stored at POST time and replayed at callback time
    assert call["telegram_bot_token"] == "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa"


def test_callback_one_shot_state_cannot_be_replayed(client, mock_bootstrap, monkeypatch):
    """A used state token cannot be used again (replay protection)."""
    from console.services import eclosure_launcher
    monkeypatch.setattr(eclosure_launcher, "launch", lambda **kw: {"ok": True, "github_app": {"ok": True}})

    post = client.post(
        "/agents/new",
        data={
            "slug": "tonyreplay",
            "display_name": "Tony Replay",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "ops",
        },
    )
    state = re.search(r"state=([a-f0-9]+)", post.text).group(1)

    # First callback succeeds
    cb1 = client.get(
        "/agents/setup-callback",
        params={"installation_id": "134075326", "setup_action": "update", "state": state},
        follow_redirects=False,
    )
    assert cb1.status_code in (200, 303)

    # Second callback with same state must fail (one-shot)
    cb2 = client.get(
        "/agents/setup-callback",
        params={"installation_id": "134075326", "setup_action": "update", "state": state},
        follow_redirects=False,
    )
    assert cb2.status_code == 400
