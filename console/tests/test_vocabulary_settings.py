"""
test_vocabulary_settings.py — UX-refresh.

/settings/<slug> reads-only and uses prose to describe each gate policy
('Sur les décisions de type X, elle attend ton feu vert.'). No edit
buttons, no operator vocabulary.
"""


def test_settings_title_uses_reglages(client):
    r = client.get("/settings/fixture")
    assert r.status_code == 200
    body = r.text
    assert "Réglages de" in body, "settings title must be 'Réglages de [Nom]'"


def test_settings_uses_courrier_du_matin_subtitle(client):
    """The subtitle steers ajustements toward the morning-letter flow."""
    r = client.get("/settings/fixture")
    body = r.text
    assert "courrier du matin" in body.lower(), \
        "subtitle must mention 'courrier du matin' (settings_pr flow in human terms)"


def test_settings_uses_human_gate_mode_phrasing(client):
    """Gate policies are described in prose using the human phrasing for
    modes (e.g. 'Tu valides chaque fois')."""
    r = client.get("/settings/fixture")
    body = r.text
    # echo_action is manual_required in the fixture; the human translation:
    assert "Tu valides chaque fois" in body or \
           "tu valides chaque fois" in body.lower(), \
        "manual_required must be translated to 'Tu valides chaque fois'"


def test_settings_has_no_edit_buttons(client):
    r = client.get("/settings/fixture")
    body = r.text.lower()
    # No <button> elements; no 'edit' / 'save' / 'update' affordance text.
    assert "<button" not in body, \
        "settings is read-only — no buttons"
