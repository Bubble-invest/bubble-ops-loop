"""
test_new_department_form.py — POST /agents/new triggers bootstrap-dept.sh.

Notion v5 lines 747-749:
  Agents a eclore
  +-- ...
  +-- + New department

The "+ New department" button posts a form (slug, display_name, owner)
and the console shells out to scripts/bootstrap-dept.sh.
"""


def test_get_agents_exposes_new_department_form(client):
    """GET /agents/new returns a form with slug/display_name/owner inputs."""
    r = client.get("/agents/new")
    assert r.status_code == 200
    body = r.text.lower()
    assert 'name="slug"' in body
    assert 'name="display_name"' in body
    assert 'name="owner"' in body


def test_post_new_department_returns_github_install_link(client, monkeypatch):
    """POST /agents/new with a valid form returns an HTMX fragment with the
    GitHub App "continue" link (rather than running bootstrap directly).

    Updated 2026-05-23 (Wave-3 Step 0b): bootstrap is now deferred to the
    setup-callback endpoint that fires after the operator grants App
    access to the new repo on github.com. POST stores the form params
    keyed by a one-shot state token and returns the link to GitHub.
    """
    import re
    r = client.post(
        "/agents/new",
        data={
            "slug": "newdept",
            "display_name": "NewDept",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
        },
    )
    assert r.status_code in (200, 202)
    body = r.text
    assert "github.com/apps/bubble-ops-bot/installations" in body
    assert re.search(r"state=[a-f0-9]+", body) is not None
    # Slug is visible in the result page
    assert "newdept" in body
