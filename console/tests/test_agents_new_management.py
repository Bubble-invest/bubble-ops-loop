"""
test_agents_new_management.py — POST /agents/new now accepts level + children
form fields and passes them through to bootstrap-dept.sh.

Wave 3 Step 0: bridge what Team A built in scaffold.py (CLI flags
--level=ops|management --children=...) to what the console exposes
in the HTML form.

Contract (after this step):
  GET  /agents/new     → form has 6 fields:
                          slug, display_name, owner, telegram_bot_token,
                          level (radio: ops|management, default=ops),
                          children (text, comma-separated, optional)
  POST /agents/new     → same as today PLUS:
                          - validates level in {ops, management}
                          - if level=management AND children empty → 400
                          - if level=ops AND children non-empty → 400
                          - passes --level=X (always) + --children=X (when
                            level=management AND children non-empty)
                            through to bootstrap-dept.sh
                          - rest of the flow (eclosure_launcher.launch)
                            unchanged
"""
from __future__ import annotations

import json


# ─── Form (GET) ────────────────────────────────────────────────────────────

def test_form_has_level_radio_with_ops_default(client):
    """GET /agents/new exposes a level radio button defaulting to ops."""
    r = client.get("/agents/new")
    assert r.status_code == 200
    body = r.text.lower()
    assert 'name="level"' in body
    # both options present
    assert 'value="ops"' in body
    assert 'value="management"' in body
    # ops is the default (checked attribute on the ops option)
    # Accept any of: checked, checked="checked", checked='checked'
    assert ('value="ops"' in body and 'checked' in body), (
        "ops should be the default radio selection"
    )


def test_form_has_children_field(client):
    """GET /agents/new exposes a children field (comma-separated)."""
    r = client.get("/agents/new")
    assert r.status_code == 200
    body = r.text.lower()
    assert 'name="children"' in body


def test_level_radios_use_radio_stack_layout(client):
    """The level radio cluster must use the .radio-stack wrapper so the
    labels don't wrap mid-word on narrow viewports.

    Regression: 2026-05-24 Joris msg 3046 — the inline display:inline-flex
    layout combined with .cabinet-field input { width: 100% } pushed the
    radio dot to full-row width and broke 'Collègue opérationnel' /
    'Manager d'une équipe' into vertical word-stacks on iPhone."""
    r = client.get("/agents/new")
    assert r.status_code == 200
    body = r.text
    # Both options must be present
    assert "Collègue opérationnel" in body
    assert "Manager d'une équipe" in body
    # The cluster must live inside a .radio-stack wrapper
    assert 'class="radio-stack"' in body, (
        "Level radios must be wrapped in <div class=\"radio-stack\">. "
        "Inline display:inline-flex layout is forbidden — it breaks on mobile."
    )
    # The old buggy inline style must NOT reappear
    assert 'display:inline-flex' not in body, (
        "Inline display:inline-flex on the level radios was the source of "
        "the msg 3046 layout bug. Use .radio-stack instead."
    )


# ─── POST validation ──────────────────────────────────────────────────────

def test_post_with_invalid_level_returns_400(client, mock_bootstrap):
    """level must be ops|management — anything else is rejected."""
    r = client.post(
        "/agents/new",
        data={
            "slug": "newdept",
            "display_name": "NewDept",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "principal",   # not a valid scaffold level
            "children": "",
        },
    )
    assert r.status_code == 400
    assert mock_bootstrap.exists() is False


def test_post_management_without_children_returns_400(client, mock_bootstrap):
    """A management dept MUST list at least one child."""
    r = client.post(
        "/agents/new",
        data={
            "slug": "tony",
            "display_name": "Tony",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "management",
            "children": "",
        },
    )
    assert r.status_code == 400
    assert mock_bootstrap.exists() is False


def test_post_ops_with_children_returns_400(client, mock_bootstrap):
    """An ops dept must NOT list children — that field is management-only."""
    r = client.post(
        "/agents/new",
        data={
            "slug": "newleaf",
            "display_name": "NewLeaf",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "ops",
            "children": "ben,maya",
        },
    )
    assert r.status_code == 400
    assert mock_bootstrap.exists() is False


def test_post_children_with_invalid_slug_chars_returns_400(client, mock_bootstrap):
    """Children slugs must each match ^[a-z][a-z0-9-]+$ (same rule as the dept slug)."""
    r = client.post(
        "/agents/new",
        data={
            "slug": "tony",
            "display_name": "Tony",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "management",
            "children": "BEN,Maya!",   # uppercase + special chars
        },
    )
    assert r.status_code == 400
    assert mock_bootstrap.exists() is False


# ─── POST happy paths ──────────────────────────────────────────────────────

def test_post_ops_stores_level_ops_for_callback(client, monkeypatch):
    """Ops happy path: POST stores level=ops in pending éclosure state.
    Wave-3 Step 0b: bootstrap fires at callback time, not POST time.
    The level is plumbed through to launch() when the callback consumes
    the state, so we assert the stored params contain level=ops."""
    import re
    from console.routes import agents as agents_mod
    r = client.post(
        "/agents/new",
        data={
            "slug": "newleaf",
            "display_name": "NewLeaf",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "ops",
            "children": "",
        },
    )
    assert r.status_code in (200, 202)
    state = re.search(r"state=([a-f0-9]+)", r.text).group(1)
    # Inspect the pending éclosure store directly (white-box)
    assert state in agents_mod._pending_eclosures
    stored = agents_mod._pending_eclosures[state]
    assert stored["level"] == "ops"
    assert stored["children_list"] == []


def test_post_management_stores_children_for_callback(client, monkeypatch):
    """Management happy path: POST stores level=management + parsed children
    list in pending éclosure state (consumed at callback time)."""
    import re
    from console.routes import agents as agents_mod
    r = client.post(
        "/agents/new",
        data={
            "slug": "tony",
            "display_name": "Tony",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "management",
            "children": "fixture,ben",
        },
    )
    assert r.status_code in (200, 202)
    state = re.search(r"state=([a-f0-9]+)", r.text).group(1)
    stored = agents_mod._pending_eclosures[state]
    assert stored["level"] == "management"
    assert stored["children_list"] == ["fixture", "ben"]


def test_post_management_whitespace_in_children_is_normalized(client, monkeypatch):
    """`  fixture , ben  ` should normalize to ['fixture', 'ben'] in the
    stored pending éclosure (whitespace stripped, empty entries dropped)."""
    import re
    from console.routes import agents as agents_mod
    r = client.post(
        "/agents/new",
        data={
            "slug": "tony",
            "display_name": "Tony",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            "level": "management",
            "children": "  fixture , ben  ",
        },
    )
    assert r.status_code in (200, 202)
    state = re.search(r"state=([a-f0-9]+)", r.text).group(1)
    stored = agents_mod._pending_eclosures[state]
    assert stored["children_list"] == ["fixture", "ben"]


# ─── Backward compat: old form (no level/children) defaults to ops ────────

def test_post_without_level_defaults_to_ops(client, monkeypatch):
    """Submitting the form without an explicit level should default to ops
    so callers that don't know about the new fields keep working.
    Wave-3 Step 0b: now asserted via the pending éclosure state."""
    import re
    from console.routes import agents as agents_mod
    r = client.post(
        "/agents/new",
        data={
            "slug": "newleaf",
            "display_name": "NewLeaf",
            "owner": "joris",
            "telegram_bot_token": "12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
            # no level field
            # no children field
        },
    )
    assert r.status_code in (200, 202)
    state = re.search(r"state=([a-f0-9]+)", r.text).group(1)
    stored = agents_mod._pending_eclosures[state]
    assert stored["level"] == "ops"
    assert stored["children_list"] == []
