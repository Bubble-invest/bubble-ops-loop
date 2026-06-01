"""
test_settings_get.py — GET /settings/<slug>.

Notion v5 line 1019: "/settings/<slug> -> edite les knobs de dept.yaml
(cadence, gate policy, goals par layer)".
"""


def test_settings_page_shows_gate_policy_modes(client):
    """Settings shows the current_mode for fixture's echo_action policy."""
    r = client.get("/settings/fixture")
    assert r.status_code == 200
    body = r.text.lower()
    assert "echo_action" in body
    assert "manual_required" in body
