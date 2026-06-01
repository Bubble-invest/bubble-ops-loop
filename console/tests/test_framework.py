"""
test_framework.py — GET /framework, the org-framework flowchart.

Joris msg 1183 (2026-06-01): one simple picture of how the organisation
works — concierges, departments, layers — from the live codebase + the
Notion architecture page.
"""
from __future__ import annotations


def test_framework_page_renders(client):
    r = client.get("/framework")
    assert r.status_code == 200


def test_framework_shows_three_tiers(client):
    """Principal → Management → Ops hierarchy is present."""
    r = client.get("/framework")
    body = r.text
    assert "Principal" in body
    assert "Joris" in body and "Jade" in body
    assert "management" in body.lower()
    assert "ops" in body.lower()


def test_framework_lists_live_departments(client):
    """The fixture dept (Live) appears as a node, linked to its page."""
    r = client.get("/framework")
    assert "Fixture" in r.text
    assert "/dept/fixture" in r.text


def test_framework_shows_four_layers(client):
    """All four daily moments are drawn."""
    r = client.get("/framework")
    body = r.text
    # "L'exécution" renders with an HTML-escaped apostrophe — match the
    # apostrophe-free stem so the test is escaping-agnostic.
    for name in ["Le matin", "La recherche", "exécution", "Le débrief du soir"]:
        assert name in body, f"missing layer: {name}"


def test_framework_mentions_concierges(client):
    """Concierges are shown beside the loop as reactive assistants."""
    r = client.get("/framework")
    body = r.text.lower()
    assert "concierge" in body


def test_framework_in_nav(client):
    """Reachable from the top nav ('Le cadre')."""
    r = client.get("/")
    assert 'href="/framework"' in r.text
    assert "Le cadre" in r.text


def test_framework_describes_two_rails(client):
    """The engine + safety-net framing is present (ties back to loop-backup)."""
    r = client.get("/framework")
    body = r.text.lower()
    assert "moteur" in body
    assert "filet de sécurité" in body
