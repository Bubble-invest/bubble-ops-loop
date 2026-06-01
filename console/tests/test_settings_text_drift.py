"""
test_settings_text_drift.py — Bug A3.

The /settings/<slug> page used to advertise "Knob editing arrives in UX-4"
but UX-4 shipped as the dry-run mechanism, not the knob editor. Per UX-3
design decision ({{OPERATOR}} sign-off — "/settings read-only en v1, édition =
settings_pr branch, deferred"), knob editing is deferred indefinitely.

The page must:
  - NOT advertise "arrives in UX-4" (stale)
  - State that editing is deferred and point at the settings_pr flow per
    Notion v5 §lifecycle line 700.
"""


def test_settings_does_not_advertise_ux4_eta(client):
    """Stale UX-4 promise must be removed."""
    r = client.get("/settings/fixture")
    assert r.status_code == 200
    low = r.text.lower()
    assert "arrives in ux-4" not in low, \
        "stale: '/settings/<slug>' must not promise knob editing in UX-4"
    assert "knob editing arrives in ux-4" not in low


def test_settings_mentions_deferred_and_settings_pr(client):
    """Page must communicate the deferred-indefinitely + settings_pr flow."""
    r = client.get("/settings/fixture")
    assert r.status_code == 200
    low = r.text.lower()
    assert "deferred" in low, \
        "page must say knob editing is deferred"
    assert "settings_pr" in low, \
        "page must point at the settings_pr branch flow per Notion v5 §lifecycle"


def test_settings_still_renders_existing_gate_policies(client):
    """Regression guard: existing gate-policy rendering must still work."""
    r = client.get("/settings/fixture")
    body = r.text.lower()
    assert "echo_action" in body
    assert "manual_required" in body
