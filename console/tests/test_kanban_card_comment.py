"""
test_kanban_card_comment.py — card DETAIL answer UX (board #482).

Two surfaces:
  1. GET /kanban/card/<n> detail page renders the decision buttons (only for
     needs:human cards) + the inline «Répondre» comment form (always).
  2. POST /kanban/card/<n>/comment posts the operator's reply verbatim as a
     GitHub issue comment via the board token, then re-renders the comment
     thread inline. Empty body → 400; no token → 503; GitHub error → 502; all
     fail SAFE (never a 500) and re-render the block with a French message.

All GitHub I/O is monkeypatched (`_board_api`, `_read_board_token`,
`_fetch_single_issue`) so NO real network call happens.
"""
from __future__ import annotations

import urllib.error

import pytest


def _kanban_module():
    """Resolve the kanban route module the LIVE app uses (see
    test_kanban_card_decide for why this must happen after the app is built)."""
    from console.routes import kanban as _kanban
    return _kanban


def _issue(number: int, *, needs_human: bool, comments: list | None = None) -> dict:
    """A raw GitHub REST issue dict shaped like _fetch_single_issue returns."""
    labels = [{"name": "dept:rnd"}, {"name": "status:in-progress"}]
    if needs_human:
        labels.append({"name": "needs:human"})
    return {
        "number": number,
        "title": f"Carte test #{number}",
        "body": "## Job\nUne question pour Joris.",
        "labels": labels,
        "html_url": f"https://github.com/Bubble-invest/bubble-ops-board/issues/{number}",
        "url": "",
        "updated_at": "2026-07-02T10:00:00Z",
        "updatedAt": "2026-07-02T10:00:00Z",
        "created_at": "2026-07-01T09:00:00Z",
        "comments_list": comments if comments is not None else [],
    }


# ── Detail page rendering ───────────────────────────────────────────────────

def test_detail_needs_human_shows_buttons_and_form(client, monkeypatch):
    """A needs:human card's detail page shows decide buttons AND the reply form."""
    _kanban = _kanban_module()
    monkeypatch.setattr(
        _kanban, "_fetch_single_issue",
        lambda n: (_issue(n, needs_human=True), None),
    )
    r = client.get("/kanban/card/482")
    assert r.status_code == 200
    # Decision affordance — all FOUR actions (clarify added board #483)
    assert 'value="approve"' in r.text
    assert 'value="reject"' in r.text
    assert 'value="defer"' in r.text
    assert 'value="clarify"' in r.text
    assert "Pas clair" in r.text  # the clarify button's French label
    assert "/kanban/card/482/decide" in r.text
    # Reply form
    assert "/kanban/card/482/comment" in r.text
    assert "Répondre" in r.text


def test_detail_non_needs_human_shows_form_only(client, monkeypatch):
    """A card WITHOUT needs:human shows the reply form but NO decide buttons."""
    _kanban = _kanban_module()
    monkeypatch.setattr(
        _kanban, "_fetch_single_issue",
        lambda n: (_issue(n, needs_human=False), None),
    )
    r = client.get("/kanban/card/300")
    assert r.status_code == 200
    # Comment form present
    assert "/kanban/card/300/comment" in r.text
    assert "Répondre" in r.text
    # No decide buttons / decide endpoint
    assert "/kanban/card/300/decide" not in r.text
    assert 'value="approve"' not in r.text


# ── POST /comment ───────────────────────────────────────────────────────────

@pytest.fixture
def captured_comment(client, monkeypatch):
    """Force a token and capture _board_api calls; stub the post-write re-fetch so
    the re-rendered thread contains the new comment."""
    _kanban = _kanban_module()
    calls: list[tuple] = []

    def fake_board_api(method, path, token, payload=None):
        calls.append((method, path, payload))
        return {}

    monkeypatch.setattr(_kanban, "_read_board_token", lambda: "fake-token")
    monkeypatch.setattr(_kanban, "_board_api", fake_board_api)
    # After the POST, the route re-fetches the issue's comments — return the
    # comment that was just "posted" so the thread re-render shows it.
    def fake_fetch(n):
        posted = [pl for (m, p, pl) in calls if p == f"/issues/{n}/comments"]
        body = posted[0]["body"] if posted else ""
        return _issue(n, needs_human=True, comments=[
            {"user": {"login": "bubble-board-bot"},
             "created_at": "2026-07-02T11:00:00Z", "body": body},
        ]), None
    monkeypatch.setattr(_kanban, "_fetch_single_issue", fake_fetch)
    return calls


def test_comment_posts_verbatim_and_renders(client, captured_comment):
    """A non-empty body → one comments POST carrying the verbatim text, 200, and
    the new comment appears in the re-rendered thread."""
    r = client.post("/kanban/card/482/comment",
                    data={"body": "Vas-y, approuvé de mon côté."})
    assert r.status_code == 200
    comment_posts = [pl for (m, p, pl) in captured_comment
                     if p == "/issues/482/comments"]
    assert comment_posts, "no comment POSTed"
    # Verbatim — no prefix added to the operator's words
    assert comment_posts[0]["body"] == "Vas-y, approuvé de mon côté."
    # New comment rendered in the swapped thread
    assert "Vas-y, approuvé de mon côté." in r.text
    # Only ONE board write (no label mutation from the comment endpoint)
    assert all(p == "/issues/482/comments" for (m, p, _pl) in captured_comment)


# A comment body carrying an XSS payload. The template renders comment bodies as
# TEXT (Jinja autoescape) for a later client-side markdown pass — the raw tags
# must never reach the response HTML, or a `| safe` slip would ship stored XSS.
_XSS_PAYLOAD = "<script>alert(1)</script><img src=x onerror=alert(1)>"


def test_comment_body_is_escaped_on_detail_and_reply(client, captured_comment,
                                                      monkeypatch):
    """XSS regression fence (card #482): a malicious comment body is HTML-escaped
    on BOTH the GET detail page and the POST /comment re-render — raw <script> /
    onerror tags absent, escaped `&lt;script&gt;` present."""
    _kanban = _kanban_module()

    # 1. GET detail page — a stored comment carrying the payload.
    monkeypatch.setattr(
        _kanban, "_fetch_single_issue",
        lambda n: (_issue(n, needs_human=True, comments=[
            {"user": {"login": "attacker"},
             "created_at": "2026-07-02T11:00:00Z", "body": _XSS_PAYLOAD},
        ]), None),
    )
    r_get = client.get("/kanban/card/482")
    assert r_get.status_code == 200
    # No LIVE tags: the raw <script> and the <img …> opener must not survive as
    # HTML (the escaped `&lt;img …&gt;` inert form is fine — its delimiters are
    # neutralised, so an onerror attribute inside it can never fire).
    assert "<script>alert(1)</script>" not in r_get.text
    assert "<img src=x onerror=alert(1)>" not in r_get.text
    assert "&lt;script&gt;" in r_get.text
    assert "&lt;img" in r_get.text

    # 2. POST /comment — the operator's own reply is echoed back escaped in the
    #    re-rendered thread (captured_comment stubs the write + re-fetch).
    r_post = client.post("/kanban/card/482/comment",
                         data={"body": _XSS_PAYLOAD})
    assert r_post.status_code == 200
    assert "<script>alert(1)</script>" not in r_post.text
    assert "<img src=x onerror=alert(1)>" not in r_post.text
    assert "&lt;script&gt;" in r_post.text
    assert "&lt;img" in r_post.text


def test_comment_empty_body_returns_400(client, captured_comment):
    """Empty body → 400, a visible French message, and NO board write."""
    r = client.post("/kanban/card/482/comment", data={"body": "   "})
    assert r.status_code == 400
    assert "vide" in r.text.lower()
    assert captured_comment == [], "no board write on an empty comment"


def test_comment_too_long_returns_400(client, captured_comment):
    """A body over the char cap → 400, French message, no board write."""
    r = client.post("/kanban/card/482/comment",
                    data={"body": "x" * 70000})
    assert r.status_code == 400
    assert "trop long" in r.text.lower()
    assert captured_comment == []


def test_comment_no_token_fails_safe_503(client, monkeypatch):
    """No board token → 503 partial, never a crash, no board write."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_read_board_token", lambda: None)
    called = []
    monkeypatch.setattr(_kanban, "_board_api",
                        lambda *a, **k: called.append(a))
    r = client.post("/kanban/card/482/comment", data={"body": "salut"})
    assert r.status_code == 503
    assert "non enregistré" in r.text
    assert called == []


def test_comment_github_error_renders_partial_not_500(client, monkeypatch):
    """A non-2xx from GitHub → a 502 error partial, not a 500."""
    _kanban = _kanban_module()
    monkeypatch.setattr(_kanban, "_read_board_token", lambda: "fake-token")

    def boom(method, path, token, payload=None):
        raise urllib.error.HTTPError(path, 403, "Forbidden", {}, None)

    monkeypatch.setattr(_kanban, "_board_api", boom)
    r = client.post("/kanban/card/482/comment", data={"body": "salut"})
    assert r.status_code == 502
    assert "GitHub" in r.text


def test_comment_invalidates_cache(client, captured_comment, monkeypatch):
    """A successful comment resets the board cache timestamp so board-derived
    views refresh on their next load (mirrors the decide endpoint)."""
    _kanban = _kanban_module()
    # Prime the cache as if it were freshly populated.
    _kanban._cache_ts = 9e9
    _kanban._cache_data = [{"number": 1}]
    r = client.post("/kanban/card/482/comment", data={"body": "réponse"})
    assert r.status_code == 200
    assert _kanban._cache_ts == 0.0, "cache not invalidated after comment"
