"""
test_carnet_framework.py — the org-framework flowchart on the Carnet de bord.

Joris msg 1183 → 1188 (2026-06-01): a simple flowchart of how the org works
(concierges, departments, layers) — shown INSIDE the Carnet de bord (/health)
page, not on a separate page.
"""
from __future__ import annotations

from console.services import org_framework


# ─── Service ─────────────────────────────────────────────────────────────

def test_build_returns_four_keys(client):
    fw = org_framework.build()
    assert set(fw) == {"management", "ops", "concierges", "layers"}
    assert len(fw["layers"]) == 4


# ─── On the Carnet de bord page ──────────────────────────────────────────

def test_carnet_shows_hierarchy(client):
    r = client.get("/health")
    body = r.text
    assert r.status_code == 200
    assert "La hiérarchie" in body
    assert "Principal" in body
    assert "Joris" in body and "Jade" in body
    assert "management" in body.lower() and "ops" in body.lower()


def test_carnet_lists_live_departments_in_chart(client):
    """The fixture dept (Live) appears as a chart node linked to its page."""
    r = client.get("/health")
    assert "Fixture" in r.text
    assert "/dept/fixture" in r.text


def test_carnet_shows_four_moments(client):
    r = client.get("/health")
    body = r.text
    # "L'exécution" renders with an escaped apostrophe — match the stem.
    for name in ["Le matin", "La recherche", "exécution", "Le débrief du soir"]:
        assert name in body, f"missing layer: {name}"


def test_carnet_mentions_concierges(client):
    r = client.get("/health")
    assert "concierge" in r.text.lower()


def test_carnet_describes_two_rails(client):
    r = client.get("/health")
    body = r.text.lower()
    assert "moteur" in body and "filet de sécurité" in body


def test_no_separate_framework_page(client):
    """The standalone /framework page was removed — it lives on /health now."""
    r = client.get("/framework")
    assert r.status_code == 404
    # And no nav tab points to it.
    home = client.get("/")
    assert 'href="/framework"' not in home.text


def test_carnet_still_shows_live_activity(client):
    """Regression: the live-activity section + footer stay on the page."""
    r = client.get("/health")
    body = r.text
    assert "Activité en direct" in body
    assert "en direct" in body.lower() or "source vivante" in body.lower()
