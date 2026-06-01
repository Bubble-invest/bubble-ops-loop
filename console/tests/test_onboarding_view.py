"""
test_onboarding_view.py — GET /agents/<slug>/onboarding (3-pane view).

Notion v5 lines 765-778:
  Header: Miranda - Agent a eclore - 42% pret
  +---------------------+------------------------+----------------+
  | Etapes onboarding   | Chat avec l'agent      | Artifacts      |
  +---------------------+------------------------+----------------+
"""


def test_onboarding_view_renders_three_panes(client):
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text.lower()
    # three pane markers — checklist + chat + artifacts
    assert "checklist" in body or "étapes" in body or "etapes" in body
    assert "chat" in body
    assert "artifacts" in body or "artefacts" in body


def test_onboarding_view_404_for_unknown_dept(client):
    r = client.get("/agents/nonexistent/onboarding")
    assert r.status_code == 404
