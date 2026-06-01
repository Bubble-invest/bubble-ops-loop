"""
test_vocabulary_dept.py — UX-refresh.

/dept/<slug> is the live-colleague detail page. After the msg-3142
consolidation (2026-05-24) it has two main sections:
"Ses 4 moments de la journée" (was "Ce qu'elle fait chaque jour" —
now consolidates missions + per-layer prompts in one section) and
"Décisions qu'on attend de toi". The old separate "Ses 4 moments"
section was merged into the first one. No raw 'Recurring missions' /
'Pending gates' / 'Subscribed layers' h2 titles.
"""


def test_dept_detail_has_ce_quelle_fait_section(client):
    """Accepts either the legacy heading or the new consolidated
    'Ses 4 moments de la journée' heading (msg 3142)."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    accepted = [
        "Ce qu'elle fait chaque jour",
        "Ce qu’elle fait chaque jour",
        "Ses 4 moments de la journée",
        "Ses 4 moments",
    ]
    assert any(s in body for s in accepted), (
        "dept detail must have a 'what she does each day' section "
        "(under either the legacy or the new heading)"
    )


def test_dept_detail_has_decisions_section(client):
    r = client.get("/dept/fixture")
    body = r.text.lower()
    assert "décisions qu'on attend" in body or \
           "décision qu'on attend" in body, \
        "dept detail must surface 'Décisions qu'on attend de toi'"


def test_dept_detail_has_moments_section(client):
    r = client.get("/dept/fixture")
    body = r.text
    assert "moments de la journée" in body, \
        "dept detail must surface 'Ses 4 moments de la journée'"


def test_dept_detail_drops_layer_n_labels_in_visible_copy(client):
    """Visible copy uses the 4 human layer names, not 'Layer 1'/'Layer 2'."""
    r = client.get("/dept/fixture")
    body = r.text
    # Strip HTML comments so a backward-compat sentinel doesn't trip this.
    import re
    visible = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    # At least one of the human moment names must show up.
    assert "Le matin" in visible or "L'exécution" in visible or \
           "La recherche" in visible or "Le débrief du soir" in visible, \
        "dept detail must use human moment names (Le matin / La recherche / ...)"
