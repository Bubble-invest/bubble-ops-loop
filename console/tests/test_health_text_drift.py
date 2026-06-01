"""
test_health_text_drift.py — Bug A4.

The /health page used to advertise "v1 stub returns 'never' until UX-5 wires
the live journal." but UX-5 shipped as the activation flow + Morty deploy,
NOT the journal reader. The journal-wiring is still pending and not on the
immediate roadmap.

The page must:
  - NOT promise UX-5 will fix this (stale)
  - Communicate that live-journal wiring is pending (post-MVP)
"""


def test_health_does_not_advertise_ux5_eta(client):
    """Stale UX-5 promise must be removed."""
    r = client.get("/health")
    assert r.status_code == 200
    low = r.text.lower()
    assert "until ux-5" not in low, \
        "stale: /health must not claim UX-5 will wire the journal"


def test_health_says_journal_wiring_pending(client):
    """Footer must communicate the wiring is pending (post-MVP)."""
    r = client.get("/health")
    assert r.status_code == 200
    low = r.text.lower()
    assert "pending" in low, \
        "/health must say live-journal wiring is pending"
    assert "post-mvp" in low, \
        "/health must flag the wiring as post-MVP"


def test_health_still_lists_dept_layers(client):
    """Regression guard."""
    r = client.get("/health")
    low = r.text.lower()
    assert "fixture" in low
    assert "layer 1" in low
