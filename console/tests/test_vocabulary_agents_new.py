"""
test_vocabulary_agents_new.py — UX-refresh.

/agents/new is the 'Accueillir un nouveau collègue' form: 3 fields with
human labels, ochre primary submit 'Commencer l'éclosion'.
"""


def test_new_agent_title_is_accueillir(client):
    r = client.get("/agents/new")
    assert r.status_code == 200
    body = r.text
    assert "Accueillir un nouveau collègue" in body, \
        "form title must be 'Accueillir un nouveau collègue'"


def test_new_agent_field_son_prenom(client):
    r = client.get("/agents/new")
    body = r.text
    # Field label for display_name = "Son prénom"
    assert "Son prénom" in body, "missing label 'Son prénom' for display_name"


def test_new_agent_field_appelle_techniquement(client):
    r = client.get("/agents/new")
    body = r.text
    assert "Comment on l'appelle techniquement" in body or \
           "Comment on l’appelle techniquement" in body, \
        "missing label for slug field"


def test_new_agent_field_parrain(client):
    r = client.get("/agents/new")
    body = r.text
    assert "parrain" in body.lower(), \
        "missing 'parrain' label for owner field"


def test_new_agent_submit_button_is_commencer_eclosion(client):
    r = client.get("/agents/new")
    body = r.text
    assert "Commencer l'éclosion" in body or \
           "Commencer l’éclosion" in body, \
        "submit button must be 'Commencer l'éclosion'"


def test_new_agent_drops_bootstrap_dept_script_path(client):
    """No mention of bootstrap-dept.sh or technical paths in human copy."""
    r = client.get("/agents/new")
    body = r.text.lower()
    assert "bootstrap-dept.sh" not in body
    assert "shells out" not in body
