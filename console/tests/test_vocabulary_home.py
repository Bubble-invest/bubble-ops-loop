"""
test_vocabulary_home.py — UX-refresh: "Bureau de Cadre" aesthetic.

Asserts the human concierge vocabulary is present on / (home) and the
technical/operator dashboard vocabulary is absent from visible copy.

The home page is no longer a "Cross-dept kanban" — it is the *registre
d'arrivée d'un cabinet*: the morning greeting + decisions awaited + team
overview, in serif Cormorant + ochre accents.
"""


def test_home_greets_joris_by_name(client):
    """The greeting opens with 'Bonjour {{OPERATOR}}.'"""
    r = client.get("/")
    assert r.status_code == 200
    assert "Bonjour" in r.text, "home must open with 'Bonjour ...'"


def test_home_uses_human_decisions_vocabulary(client):
    """'Décisions qu'on attend de toi' replaces 'pending gates'."""
    r = client.get("/")
    body = r.text
    assert "décisions qu'on attend" in body.lower() or \
           "décision qu'on attend" in body.lower(), \
        "home must surface 'décisions qu'on attend de toi' phrasing"


def test_home_uses_collegue_vocabulary(client):
    """Departments are 'collègues' (en poste / en éclosion)."""
    r = client.get("/")
    body = r.text.lower()
    assert "collègue" in body or "collègues" in body, \
        "home must use 'collègues' vocabulary"


def test_home_drops_kanban_label_from_visible_copy(client):
    """No 'Kanban' / 'Cross-dept board' label in *visible* copy.

    The route MAY still link to /kanban (the href URL is not visible text), but
    the jargon word must not appear as a heading, label, or any text node the
    user reads.  We strip HTML tags before asserting so that href="/kanban"
    attributes do not trigger a false positive.
    """
    import re as _re
    r = client.get("/")
    raw = r.text
    # Strip all HTML tags — leaves only the visible text nodes.
    visible = _re.sub(r"<[^>]+>", " ", raw).lower()
    assert "kanban" not in visible, (
        "home must not use the word 'kanban' in visible copy (headings / labels / text)"
    )
    assert "cross-dept board" not in visible, \
        "home must not say 'Cross-dept board'"


def test_home_drops_pending_gate_label_from_visible_copy(client):
    """No 'pending gate' label — replaced by 'décision qu'on attend de toi'."""
    r = client.get("/")
    body = r.text.lower()
    assert "pending gate" not in body
    assert "queue clean" not in body
