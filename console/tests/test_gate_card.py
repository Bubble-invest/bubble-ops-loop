"""
test_gate_card.py — GET /gate/<dept>/<id>.

Notion v5 line 1018: "/gate/<dept>/<id> -> carte de decision
(approve / reject / modify ; POST ecrit inbox/decisions/<id>.yaml + commit)".
"""


def test_gate_card_renders_yaml_and_four_actions(client):
    """Gate page shows the YAML payload + 4 action buttons."""
    r = client.get("/gate/fixture/echo-1")
    assert r.status_code == 200
    body = r.text.lower()
    assert "echo-1" in body
    for action in ("approve", "reject", "modify", "defer"):
        assert action in body, f"action button '{action}' missing"
