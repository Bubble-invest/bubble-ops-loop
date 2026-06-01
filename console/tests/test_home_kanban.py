"""
test_home_kanban.py — / cross-dept kanban.

Notion v5 line 1014: "/ -> cross-dept board (kanban: par dept x par layer x
{gates pending, tasks in-flight, last run})".
"""


def test_home_renders_kanban_with_live_dept_column(client):
    """Home must list the live 'fixture' dept and show its pending gate."""
    r = client.get("/")
    assert r.status_code == 200
    body = r.text.lower()
    # both the dept and the gate id must appear
    assert "fixture" in body
    assert "echo-1" in body
