"""
test_home_empty_team_cta.py — when the team is empty, the L'équipe
empty-state card must surface a "Hire a new colleague" CTA linking to
/agents/new (the éclosion form).

Joris flagged msg 3044 (2026-05-24) on a fresh home: "Ok so add a link
in here to hire a new colleague (redirect to already existing)". The
empty-state already said "Commence par accueillir ton premier collègue."
but had no actionable link — the operator had to navigate to /agents
first to find the "+ Accueillir un nouveau collègue" button. One-click
is one click too many when the empty-state is itself the prompt.
"""
from __future__ import annotations

from pathlib import Path


def _build_client_empty_team(monkeypatch, tmp_path: Path):
    """Build a TestClient pointing at an empty disk root — no depts at all
    so home renders the empty-team empty-state."""
    root = tmp_path / "depts"
    root.mkdir()
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})
    return c


def test_empty_team_section_links_to_agents_new(monkeypatch, tmp_path):
    """When the L'équipe section is empty, the empty-state card MUST
    include a CTA link to /agents/new so the operator can hire the
    first colleague in one click."""
    c = _build_client_empty_team(monkeypatch, tmp_path)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text

    # The existing empty-state phrasing must still be present (regression guard).
    assert "Commence par accueillir ton premier collègue" in body

    # AND the CTA link to /agents/new must be there (this is what's new).
    assert 'href="/agents/new"' in body, (
        "Empty-team empty-state must include a CTA link to /agents/new. "
        "Joris msg 3044 — operator should not need to navigate to /agents "
        "first to find the éclosion button."
    )


def test_empty_team_cta_uses_action_oriented_copy(monkeypatch, tmp_path):
    """The CTA label must be action-oriented (e.g. 'Accueillir ton premier
    collègue', mirroring the /agents page's '+ Accueillir un nouveau
    collègue'). No bare 'click here' or 'go to'.
    """
    c = _build_client_empty_team(monkeypatch, tmp_path)
    r = c.get("/")
    body = r.text
    # Pull a small window around the CTA anchor to assert on its inner text.
    anchor_idx = body.find('href="/agents/new"')
    assert anchor_idx != -1, "CTA link missing — see other test for details"
    window = body[max(0, anchor_idx - 50): anchor_idx + 300]
    assert ("Accueillir" in window or "accueillir" in window), (
        f"CTA label should use 'accueillir' (the canonical éclosion verb). "
        f"Got window around the anchor:\n{window}"
    )
