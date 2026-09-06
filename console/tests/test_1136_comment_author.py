"""
test_1136_comment_author.py — cockpit comments show the HUMAN author (#1136).

Cockpit-posted board comments (decisions + replies) are written with the board
bot's token, so GitHub attributes them to `bubble-ops-bot`. Each carries a
machine marker naming the human who acted via the cockpit gate. The cockpit
card-detail thread must show that human (with an initial avatar) instead of the
bot — while leaving the marker text byte-intact (rnd_loop greps it).

Covers the pure name-derivation seam AND the rendered detail page. GitHub I/O is
monkeypatched — no real network call.
"""
from __future__ import annotations

from console.services.cockpit_comment_author import (
    annotate_comment_authors,
    cockpit_human_name,
)


def _kanban_module():
    from console.routes import kanban as _kanban
    return _kanban


def _issue(number: int, comments: list) -> dict:
    return {
        "number": number,
        "title": f"Carte test #{number}",
        "body": "## Job\nUne question pour Joris.",
        "labels": [{"name": "dept:rnd"}],
        "html_url": f"https://github.com/Bubble-invest/bubble-ops-board/issues/{number}",
        "url": "",
        "updated_at": "2026-07-02T10:00:00Z",
        "updatedAt": "2026-07-02T10:00:00Z",
        "created_at": "2026-07-01T09:00:00Z",
        "comments_list": comments,
    }


# ── Pure name derivation ─────────────────────────────────────────────────────

def test_cockpit_human_name_matches_each_marker():
    assert cockpit_human_name("✅ Approved by Joris via cockpit") == "Joris"
    assert cockpit_human_name("❌ Rejected by Jade via cockpit\n\nune note") == "Jade"
    assert cockpit_human_name("⏳ Deferred by Joris — back to Rick's queue") == "Joris"
    assert cockpit_human_name("📝 Note de Jade : fais-en une v2") == "Jade"


def test_cockpit_human_name_ignores_non_markers():
    # A genuine human/agent comment with no marker prefix → no name recovered.
    assert cockpit_human_name("Vas-y, approuvé de mon côté.") is None
    assert cockpit_human_name("🔍 Pas clair — peux-tu préciser ?") is None  # no name
    assert cockpit_human_name("") is None
    assert cockpit_human_name(None) is None


def test_cockpit_human_name_only_at_start():
    # The marker must be a PREFIX — a quote of it mid-body can't spoof a name.
    assert cockpit_human_name("je cite : ✅ Approved by Mallory via cockpit") is None


def test_annotate_sets_display_fields_without_mutating_source():
    src = [
        {"user": {"login": "bubble-ops-bot"}, "created_at": "2026-07-02T11:00:00Z",
         "body": "✅ Approved by Joris via cockpit"},
        {"user": {"login": "vdk888"}, "created_at": "2026-07-02T12:00:00Z",
         "body": "commentaire humain direct sur GitHub"},
    ]
    out = annotate_comment_authors(src)
    # cockpit marker → human name + flag + initial
    assert out[0]["display_author"] == "Joris"
    assert out[0]["display_via_cockpit"] is True
    assert out[0]["display_initial"] == "J"
    # plain GitHub comment → keeps its login, not flagged
    assert out[1]["display_author"] == "vdk888"
    assert out[1]["display_via_cockpit"] is False
    # source dicts untouched
    assert "display_author" not in src[0]


def test_annotate_handles_missing_user_and_body():
    out = annotate_comment_authors([{}])
    assert out[0]["display_author"] == "inconnu"
    assert out[0]["display_via_cockpit"] is False


# ── Rendered detail page ─────────────────────────────────────────────────────

def test_detail_page_shows_human_not_bot_and_keeps_marker(client, monkeypatch):
    """A bot-authored `✅ Approved by Joris via cockpit` comment renders as
    'Joris' in the thread; the raw marker text stays present (rnd_loop needs it),
    and the bot login is not shown as the author."""
    _kanban = _kanban_module()
    comments = [
        {"user": {"login": "bubble-ops-bot"}, "created_at": "2026-07-02T11:00:00Z",
         "body": "✅ Approved by Joris via cockpit"},
    ]
    monkeypatch.setattr(
        _kanban, "_fetch_single_issue",
        lambda n: (_issue(n, comments), None),
    )
    r = client.get("/kanban/card/1136")
    assert r.status_code == 200
    # Human name shown in the byline
    assert "Joris" in r.text
    # Marker text preserved in the rendered comment body (display-only change)
    assert "via cockpit" in r.text
    # The bot login is NOT presented as the comment author byline
    assert "<strong>bubble-ops-bot</strong>" not in r.text
