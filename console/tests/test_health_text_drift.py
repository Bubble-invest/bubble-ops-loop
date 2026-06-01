"""
test_health_text_drift.py — Bug A4, updated 2026-06-01 (Joris msg 1180).

The /health page WAS a stub fed by placeholder data; its footer promised the
"live journal wiring" was still pending (post-MVP). That promise is now
itself the stale text: morty_reader reads the real on-disk loop traces live
(the console runs on the box — no SSH/journal needed). The page must:
  - NOT promise UX-5 will wire it (never true)
  - NOT still claim the live source is "pending" / "post-MVP" (it's wired)
  - state the data is read live from the real loop traces
"""


def test_health_does_not_advertise_ux5_eta(client):
    """Stale UX-5 promise must be removed."""
    r = client.get("/health")
    assert r.status_code == 200
    low = r.text.lower()
    assert "until ux-5" not in low, \
        "stale: /health must not claim UX-5 will wire the journal"


def test_health_no_longer_claims_pending(client):
    """The live source is wired — the page must not say it's pending/post-MVP."""
    r = client.get("/health")
    assert r.status_code == 200
    low = r.text.lower()
    assert "pending" not in low, \
        "/health is wired to live data — must not say 'pending'"
    assert "post-mvp" not in low, \
        "/health is wired to live data — must not flag it 'post-MVP'"


def test_health_states_it_reads_live(client):
    """Footer must communicate the data is read live from real loop traces."""
    r = client.get("/health")
    low = r.text.lower()
    assert "en direct" in low or "source vivante" in low, \
        "/health must say its data is read live from the real loop traces"


def test_health_still_lists_dept_layers(client):
    """Regression guard."""
    r = client.get("/health")
    low = r.text.lower()
    assert "fixture" in low
    assert "layer 1" in low
