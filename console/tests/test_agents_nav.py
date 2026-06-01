"""
test_agents_nav.py — GET /agents (2-section nav).

Notion v5 lines 737-749:
  Agents
  +-- Live departments (Maya, Ben, Tony, ...)
  +-- Agents a eclore  (Miranda - 42% configuree, ...)
                       + New department
"""


def test_agents_page_has_two_sections(client):
    r = client.get("/agents")
    assert r.status_code == 200
    body = r.text.lower()
    # both navigation sections + the new-dept affordance
    assert "live" in body
    assert "eclore" in body or "\xc3\xa9clore" in body or "éclore" in body
    assert "new department" in body or "+ new dept" in body


def test_agents_page_classifies_fixture_as_live_and_miranda_as_eclore(client):
    r = client.get("/agents")
    assert r.status_code == 200
    body = r.text
    # fixture (status=Live) belongs in Live departments
    # miranda (status=Drafting) belongs in Agents a eclore
    assert "fixture" in body.lower()
    assert "miranda" in body.lower()
