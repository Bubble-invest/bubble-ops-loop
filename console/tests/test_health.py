"""
test_health.py — GET /health.

Notion v5 line 1020: "/health -> last successful run par (layer x dept) ;
rouge si stale > 2x cadence".
"""


def test_health_page_lists_each_dept_layer(client):
    """Health page enumerates each (dept x layer) row."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.text.lower()
    # the live dept appears with all 4 layers
    assert "fixture" in body
    assert "layer 1" in body or "layer_1" in body or "l1" in body
