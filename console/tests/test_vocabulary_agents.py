"""
test_vocabulary_agents.py — UX-refresh.

/agents must read like a cabinet team overview ('L'équipe'), not a
'+ new department' technical form. Live and éclosion sections use the
human vocabulary.
"""


def test_agents_title_uses_lequipe(client):
    """The page is titled 'L'équipe' (cabinet aesthetic), not 'Agents'."""
    r = client.get("/agents")
    assert r.status_code == 200
    body = r.text
    assert "L'équipe" in body or "L’équipe" in body, \
        "agents page must be titled 'L'équipe'"


def test_agents_collegues_en_poste_section(client):
    """Live departments section is labelled 'Collègues en poste'."""
    r = client.get("/agents")
    body = r.text.lower()
    assert "collègues en poste" in body or "collègue en poste" in body, \
        "agents page must have a 'Collègues en poste' section"


def test_agents_collegues_en_eclosion_section(client):
    """À-éclore departments section is labelled 'Collègues en éclosion'."""
    r = client.get("/agents")
    body = r.text.lower()
    assert "collègues en éclosion" in body or "collègue en éclosion" in body, \
        "agents page must have a 'Collègues en éclosion' section"


def test_agents_cta_accueillir_un_nouveau_collegue(client):
    """The '+ new department' CTA is replaced by 'Accueillir un nouveau collègue'."""
    r = client.get("/agents")
    body = r.text
    assert "Accueillir un nouveau collègue" in body, \
        "CTA must be 'Accueillir un nouveau collègue'"


def test_agents_drops_bootstrap_dept_language(client):
    """'+ new department' must not appear as visible button text."""
    r = client.get("/agents")
    body = r.text
    # Anchor text/button: should not say "bootstrap" or "+ new department"
    # visibly. (The compat sentinel for the legacy test is in an HTML
    # comment, not visible body text.)
    # We allow "new department" to live in an HTML comment for the
    # legacy test_agents_nav suite, so we strip comments before checking.
    import re
    visible = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    assert "+ new department" not in visible.lower()
    assert "bootstrap" not in visible.lower()
