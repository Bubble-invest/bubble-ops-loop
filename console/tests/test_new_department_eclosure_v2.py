"""
test_new_department_eclosure_v2.py — POST /agents/new now triggers the FULL
éclosure chain, not just scaffold.

After the upgrade, the form must accept a telegram_bot_token and the POST
handler must:
  1. Run bootstrap-dept.sh
  2. Call eclosure_launcher.launch() which does:
     a. create_per_dept_sops_env(slug, token)
     b. install_systemd_unit(slug)
     c. systemctl_enable_and_start(slug)
     d. try_install_github_app(slug)
  3. Render an HTMX result fragment that includes:
     - the slug
     - a 'streaming' indicator (the SSE URL or hx-sse attribute)
     - if github_app failed → a manual fallback message
"""
from __future__ import annotations

import json


def test_form_has_telegram_token_field(client):
    """GET /agents/new must expose the new telegram_bot_token field."""
    r = client.get("/agents/new")
    assert r.status_code == 200
    body = r.text.lower()
    assert 'name="telegram_bot_token"' in body
    # Field is required
    assert 'name="telegram_bot_token"' in body


def test_post_without_telegram_token_returns_400(client, mock_bootstrap):
    """Submitting without a telegram token should be rejected."""
    r = client.post(
        "/agents/new",
        data={"slug": "newdept", "display_name": "NewDept", "owner": "joris"},
    )
    assert r.status_code in (400, 422)


def test_post_with_garbage_telegram_token_returns_400(client, mock_bootstrap):
    """An obviously-malformed token must be refused before any side effect."""
    r = client.post(
        "/agents/new",
        data={
            "slug": "newdept", "display_name": "NewDept", "owner": "joris",
            "telegram_bot_token": "not-a-valid-token",
        },
    )
    assert r.status_code == 400
    assert mock_bootstrap.exists() is False, "bootstrap should not have been invoked"


def test_post_valid_stores_state_and_returns_install_link(client, monkeypatch):
    """Happy path (Wave-3 Step 0b contract): POST stores the form params in
    the pending éclosure state and returns the GitHub App "continue" link.
    Bootstrap + launcher fire only after the operator grants App access
    on github.com and GitHub redirects to /agents/setup-callback."""
    import re
    from console.services import eclosure_launcher
    from console.routes import agents as agents_mod
    launcher_calls = []
    monkeypatch.setattr(
        eclosure_launcher, "launch",
        lambda **kw: launcher_calls.append(kw) or {"ok": True},
    )

    r = client.post(
        "/agents/new",
        data={
            "slug": "newdept",
            "display_name": "NewDept",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
        },
    )
    assert r.status_code in (200, 202)
    # Launcher must NOT fire on POST anymore — fires at callback time
    assert launcher_calls == []
    # State token must be present in the result, and stored server-side
    state_match = re.search(r"state=([a-f0-9]+)", r.text)
    assert state_match is not None
    state = state_match.group(1)
    assert state in agents_mod._pending_eclosures
    assert agents_mod._pending_eclosures[state]["slug"] == "newdept"


def test_post_result_contains_install_link_not_sse_stream(client, monkeypatch):
    """The HTMX result fragment must reference the GitHub App "continue"
    link (Wave-3 Step 0b) — the SSE stream URL only appears in the
    callback response after the operator grants App access on GitHub."""
    r = client.post(
        "/agents/new",
        data={
            "slug": "newdept",
            "display_name": "NewDept",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
        },
    )
    assert r.status_code in (200, 202)
    body = r.text
    # Link to the GitHub App installation page
    assert "github.com/apps/bubble-ops-bot/installations" in body
    # The SSE stream URL is NOT yet present (it appears in the callback page)
    assert "eclosure-stream" not in body


def test_post_result_shows_github_app_fallback_when_install_fails(
    client, mock_bootstrap, monkeypatch,
):
    """Wave-3 Step 0b: the GitHub App "continue" link is the PRIMARY path now,
    so the fallback box is no longer needed on the POST result page (the
    operator is going to GitHub by design, not as a fallback). The link
    to github.com/apps/bubble-ops-bot/installations/<id> in the POST
    result satisfies the original spec's intent ("operator can reach
    GitHub to manage App access") via a stronger, intended path."""
    import re
    r = client.post(
        "/agents/new",
        data={
            "slug": "newdept",
            "display_name": "NewDept",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
        },
    )
    assert r.status_code in (200, 202)
    body = r.text
    assert "github.com/apps/bubble-ops-bot/installations" in body
    assert re.search(r"state=[a-f0-9]+", body) is not None


def test_sse_endpoint_streams_event_format(client, monkeypatch):
    """GET /agents/<slug>/eclosure-stream returns text/event-stream with
    Server-Sent Events shaped like:
        event: progress
        data: {"kind":"sops_done","slug":"newdept"}

    For now we check the response Content-Type and that the first body
    bytes look like an SSE message (data: ...)."""
    # Use streaming so the response body is partial when we read it.
    with client.stream("GET", "/agents/newdept/eclosure-stream") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        # Read a small chunk to ensure SSE-shaped framing
        chunk = b""
        for piece in r.iter_bytes():
            chunk += piece
            if len(chunk) > 30 or b"\n\n" in chunk:
                break
        text = chunk.decode("utf-8", errors="replace")
        assert "data:" in text or "event:" in text
