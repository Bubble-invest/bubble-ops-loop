"""
test_vocabulary_onboarding.py — UX-refresh.

/agents/<slug>/onboarding is the most-important page — the *carnet d'arrivée*
(leather-bound new-hire book). Vertical narrative, no 3-pane grid in the
human-facing copy. The 7 step names are written verbatim from the brief.
"""


def test_onboarding_uses_carnet_arrivee_header(client):
    """Header presents the colleague joining the team in human language."""
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text
    # "rejoint l'équipe" header subtitle
    assert "rejoint l'équipe" in body or "rejoint l’équipe" in body, \
        "onboarding must show 'rejoint l'équipe' header subtitle"


def test_onboarding_has_section_sa_mission(client):
    """Section A 'Sa mission' must exist."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    assert "Sa mission" in body, "missing section A 'Sa mission'"


def test_onboarding_has_seven_human_step_names(client):
    """Section B timeline shows the 7 human step names verbatim."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    expected_steps = [
        "Sa charte de mission",
        "Ce qu'elle fera chaque jour",
        "Ses 4 moments de la journée",
        "Ce qu'elle saura faire",
        "Les décisions qu'elle pourra prendre",
        "Sa répétition à blanc",
        "Sa cérémonie d'arrivée",
    ]
    for step in expected_steps:
        # Accept straight apostrophe, curly apostrophe, or HTML-escaped &#39;
        # (Jinja autoescape emits the entity by default).
        alt_curly = step.replace("'", "’")
        alt_html = step.replace("'", "&#39;")
        assert step in body or alt_curly in body or alt_html in body, \
            f"timeline step missing: {step!r}"


def test_onboarding_has_section_ce_quelle_a_appris(client):
    """Section C 'Ce que [Nom] a déjà appris' must exist."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    assert "déjà appris" in body, \
        "missing section C 'Ce que [Nom] a déjà appris'"


def test_onboarding_has_section_ce_qui_reste_a_faire(client):
    """Section D 'Ce qu'il reste à faire ensemble' must exist."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    assert "reste à faire" in body, \
        "missing section D 'Ce qu'il reste à faire ensemble'"


def test_onboarding_has_section_comment_parler(client):
    """Section E 'Comment parler à <Nom>' replaces the legacy claude-code
    terminal CTA (Phase G4 — the agent now lives on Morty via SystemD and
    is reachable only via Telegram)."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    assert "Comment parler à" in body, \
        "missing section E 'Comment parler à <Nom>' (Phase G4)"
    # The bot handle must be present.
    assert "@bubbleopsmiranda_bot" in body, \
        "missing dedicated bot handle"


def test_onboarding_drops_3_pane_visible_labels(client):
    """No human-facing 'Étapes onboarding (checklist)' / 'Chat avec l'agent'
    / 'Artifacts en construction' titles. They are replaced by narrative
    sections. (Legacy class/markers remain for backward-compat tests.)"""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    # Visible h2 titles from the old 3-pane layout are gone (case-sensitive).
    assert "Étapes onboarding (checklist)" not in body
    assert "Chat avec l'agent" not in body
    assert "Artifacts en construction" not in body
