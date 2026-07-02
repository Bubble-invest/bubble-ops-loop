"""
test_kanban_card_decide.py — POST /kanban/card/<n>/decide (board #427).

Joris records a decision (approve/reject/defer) on one of Rick's own
`needs:human` board cards. The decision is written ONTO the GitHub board issue
(label + comment + close, or un-queue for defer) — record-only; Rick acts on
his next loop tick. No auto-trigger.

These tests mock the HTTP layer (`_board_api` + `_read_board_token`) so NO real
GitHub call happens. We assert each action issues the right REST operations,
bad actions 400, and the no-token / API-error paths fail safe (never 500).
"""
from __future__ import annotations

import urllib.error

import pytest


def _kanban_module():
    """The kanban route module the LIVE app uses.

    The `app` fixture deletes + re-imports every `console.*` module so env vars
    are picked up fresh, so we must resolve the module AFTER the app is built
    (and patch THAT instance) — patching a module imported at test-collection
    time would target a stale copy the running app no longer uses."""
    from console.routes import kanban as _kanban
    return _kanban


@pytest.fixture
def captured_board(client, monkeypatch):
    """Capture every _board_api call as (method, path, payload) and force a token
    to be present, so the decide route exercises the full write path offline.

    Depends on `client` so the app (and its fresh `console.routes.kanban`) exists
    before we patch — see _kanban_module()."""
    _kanban = _kanban_module()
    calls: list[tuple] = []

    def fake_board_api(method, path, token, payload=None):
        calls.append((method, path, payload))
        # Mimic the real GitHub responses well enough for the route: label/comment
        # POSTs and the PATCH all return a (here irrelevant) object; we return {}.
        return {}

    monkeypatch.setattr(_kanban, "_read_board_token", lambda: "fake-token")
    monkeypatch.setattr(_kanban, "_board_api", fake_board_api)
    return calls


def test_approve_labels_comments_and_closes(client, captured_board):
    """approve → ensure+add decision:approved label, post an approval comment,
    PATCH the issue closed."""
    r = client.post("/kanban/card/427/decide",
                    data={"action": "approve", "comment": "go"})
    assert r.status_code == 200
    methods_paths = [(m, p) for (m, p, _payload) in captured_board]

    # Label created (idempotent) + applied
    assert ("POST", "/labels") in methods_paths
    assert ("POST", "/issues/427/labels") in methods_paths
    # A comment was posted carrying the approval text + the operator's note
    comment_calls = [pl for (m, p, pl) in captured_board
                     if p == "/issues/427/comments"]
    assert comment_calls, "no comment posted on approve"
    assert "Approved by Joris via cockpit" in comment_calls[0]["body"]
    assert "go" in comment_calls[0]["body"]
    # Issue closed
    assert ("PATCH", "/issues/427") in methods_paths
    patch_payload = [pl for (m, p, pl) in captured_board if m == "PATCH"][0]
    assert patch_payload == {"state": "closed"}


def test_reject_labels_comments_and_closes(client, captured_board):
    """reject → decision:rejected label, rejection comment, close."""
    r = client.post("/kanban/card/500/decide", data={"action": "reject"})
    assert r.status_code == 200
    methods_paths = [(m, p) for (m, p, _payload) in captured_board]
    assert ("POST", "/issues/500/labels") in methods_paths
    label_payload = [pl for (m, p, pl) in captured_board
                     if p == "/issues/500/labels"][0]
    assert label_payload == {"labels": ["decision:rejected"]}
    comment_calls = [pl for (m, p, pl) in captured_board
                     if p == "/issues/500/comments"]
    assert "Rejected by Joris via cockpit" in comment_calls[0]["body"]
    assert ("PATCH", "/issues/500") in methods_paths


def test_defer_removes_needs_human_and_does_not_close(client, captured_board):
    """defer → DELETE the needs:human label, comment, and DO NOT close (the card
    returns to Rick's queue)."""
    r = client.post("/kanban/card/600/decide", data={"action": "defer"})
    assert r.status_code == 200
    methods_paths = [(m, p) for (m, p, _payload) in captured_board]
    assert ("DELETE", "/issues/600/labels/needs:human") in methods_paths
    comment_calls = [pl for (m, p, pl) in captured_board
                     if p == "/issues/600/comments"]
    assert "Deferred by Joris" in comment_calls[0]["body"]
    # MUST NOT close on defer
    assert not any(m == "PATCH" for (m, p, _pl) in captured_board), \
        "defer must not close the issue"


def test_clarify_removes_needs_human_marker_comment_no_close(client, captured_board):
    """clarify (board #483) → DELETE needs:human, post the EXACT «🔍 Pas clair»
    marker comment, NO close, and NO other label change (unlike approve/reject).
    Same board mechanics as defer but a distinct, byte-exact marker."""
    from console.routes import kanban as _kanban

    r = client.post("/kanban/card/700/decide", data={"action": "clarify"})
    assert r.status_code == 200
    methods_paths = [(m, p) for (m, p, _payload) in captured_board]

    # needs:human removed
    assert ("DELETE", "/issues/700/labels/needs:human") in methods_paths
    # The marker comment is posted EXACTLY (loop-detection contract)
    comment_calls = [pl for (m, p, pl) in captured_board
                     if p == "/issues/700/comments"]
    assert comment_calls, "no marker comment posted on clarify"
    assert comment_calls[0]["body"] == _kanban._CLARIFY_MARKER
    # Byte-exact prefix Rick's loop keys on
    assert comment_calls[0]["body"].startswith("🔍 Pas clair")
    # MUST NOT close, and MUST NOT add/apply any label (no decision:* / labels POST)
    assert not any(m == "PATCH" for (m, p, _pl) in captured_board), \
        "clarify must not close the issue"
    assert not any(p == "/issues/700/labels" for (m, p, _pl) in captured_board), \
        "clarify must not apply any label"
    assert not any(p == "/labels" for (m, p, _pl) in captured_board), \
        "clarify must not create any label"


def test_decide_from_detail_with_return_to_sends_hx_redirect(client, captured_board):
    """A decide carrying an allowlisted return_to (the detail-page case) → the
    decision is still applied AND the response carries HX-Redirect back to that
    path, with no inline partial body (board #483 follow-up)."""
    r = client.post("/kanban/card/427/decide",
                    data={"action": "approve", "return_to": "/kanban"})
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/kanban"
    assert r.text == ""  # HX-Redirect, not the inline confirmation partial
    # The decision was still applied (label + comment + close)
    methods_paths = [(m, p) for (m, p, _payload) in captured_board]
    assert ("POST", "/issues/427/labels") in methods_paths
    assert ("PATCH", "/issues/427") in methods_paths


def test_decide_return_to_root_allowed(client, captured_board):
    """`/` is on the allowlist → HX-Redirect to /."""
    r = client.post("/kanban/card/427/decide",
                    data={"action": "defer", "return_to": "/"})
    assert r.status_code == 200
    assert r.headers.get("HX-Redirect") == "/"


def test_decide_garbage_return_to_no_redirect_inline_partial(client, captured_board):
    """A return_to NOT on the allowlist (open-redirect attempt / arbitrary path)
    is treated as absent → NO HX-Redirect, the normal inline partial is returned
    and the decision is still applied."""
    for bad in ("https://evil.example/phish", "/settings", "//evil.example",
                "/kanban/card/427", "javascript:alert(1)"):
        captured_board.clear()
        r = client.post("/kanban/card/427/decide",
                        data={"action": "approve", "return_to": bad})
        assert r.status_code == 200, bad
        assert "HX-Redirect" not in r.headers, bad
        # Inline confirmation partial rendered instead
        assert "Rick" in r.text, bad
        # Decision still applied
        assert any(p == "/issues/427/labels"
                   for (m, p, _pl) in captured_board), bad


def test_decide_from_surface_no_return_to_unchanged_inline(client, captured_board):
    """A decide from the home/kanban card surface sends NO return_to → unchanged
    behavior: no HX-Redirect, the inline confirmation partial swaps as before."""
    r = client.post("/kanban/card/427/decide", data={"action": "approve"})
    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers
    assert "Rick" in r.text  # inline partial


def test_decide_failed_with_return_to_still_error_partial_no_redirect(client, monkeypatch):
    """Even with a valid return_to, a FAILED decide must surface the error partial
    (no HX-Redirect) — the redirect only fires on success."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_read_board_token", lambda: None)  # 503 path
    r = client.post("/kanban/card/427/decide",
                    data={"action": "approve", "return_to": "/kanban"})
    assert r.status_code == 503
    assert "HX-Redirect" not in r.headers
    assert "non enregistrée" in r.text


def test_bad_action_returns_400(client, captured_board):
    """An action outside {approve,reject,defer} → 400, no board writes."""
    r = client.post("/kanban/card/427/decide", data={"action": "nuke"})
    assert r.status_code == 400
    assert captured_board == [], "no board write should happen on a bad action"


def test_no_token_fails_safe_not_500(client, monkeypatch):
    """No board token (dev/CI / refresh gap) → graceful 503 partial, never a
    crash, and no board write attempted."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_read_board_token", lambda: None)
    called = []
    monkeypatch.setattr(_kanban, "_board_api",
                        lambda *a, **k: called.append(a))
    r = client.post("/kanban/card/427/decide", data={"action": "approve"})
    assert r.status_code == 503
    assert "non enregistrée" in r.text
    assert called == [], "no board write should happen without a token"


def test_github_api_error_renders_partial_not_500(client, monkeypatch):
    """A non-2xx from GitHub → an error partial (502), not a 500."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_read_board_token", lambda: "fake-token")

    def boom(method, path, token, payload=None):
        raise urllib.error.HTTPError(path, 403, "Forbidden", {}, None)

    monkeypatch.setattr(_kanban, "_board_api", boom)
    r = client.post("/kanban/card/427/decide", data={"action": "approve"})
    assert r.status_code == 502
    assert "GitHub" in r.text
