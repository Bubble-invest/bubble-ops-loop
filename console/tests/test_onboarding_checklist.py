"""
test_onboarding_checklist.py — left pane reflects STATE.yaml::validated_steps.

Per Notion v5 lines 782-792, each step has an explicit status icon:
  - validated  -> check
  - in progress -> half-circle
  - pending    -> empty-circle

We mirror that via classes / data attributes so the template doesn't depend
on unicode glyphs (mobile-safe).
"""


def test_checklist_marks_validated_steps_for_miranda(client):
    """
    miranda has validated_steps=[mandate, missions, layers] in fixtures.
    The HTML must distinguish those 3 from the remaining 3 (skills_tools,
    gates_kpis, dry_run) and the 7th (activation).
    """
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text
    # validated steps tagged with data-status="validated"
    assert 'data-step="mandate"' in body
    assert 'data-step="missions"' in body
    assert 'data-step="layers"' in body
    assert 'data-step="skills_tools"' in body  # still pending
    # status markers present
    assert 'data-status="validated"' in body
    assert 'data-status="pending"' in body
