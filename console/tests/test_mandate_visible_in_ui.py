"""
test_mandate_visible_in_ui.py — MANDATE.md must be readable from the UI
in BOTH lifecycle phases.

Joris flag 2026-05-24 msg 3118:
  > I need to be able to read it in the UI at the current onboarding
  > phase, and in the operational phase after that when needed, as for
  > all agents.

Phase 1 (onboarding, status != Live): the dept's MANDATE.md must be
visible on `/agents/<slug>/onboarding`. Joris uses this page to read
what the agent has committed to during the éclosion.

Phase 2 (operating, status == Live): MANDATE.md remains the canonical
contract. It must be visible on `/dept/<slug>`. Joris reads it when
auditing the agent's scope or when introducing the agent to Jade.

Test pattern: monkey-patch github_reader.load_mandate_md to return a
sentinel, GET both pages, assert the sentinel content is present.
"""
from __future__ import annotations


_SENTINEL_MANDATE = (
    "# Mandat de Maya\n\n"
    "## La phrase\n\n"
    "> *« This is the sentinel mandate body for the test. »*\n\n"
    "## Ce que je dois produire\n\n"
    "Mes 4 livrables — A B C D — sentinel-marker-12345\n"
)


def test_load_mandate_md_helper_exists():
    """github_reader must expose a load_mandate_md(slug) helper that
    returns the MANDATE.md content as a string (or None if missing)."""
    from console.services import github_reader
    assert hasattr(github_reader, "load_mandate_md"), (
        "github_reader.load_mandate_md(slug) is the canonical helper for "
        "reading a dept's MANDATE.md. Add it as a sibling of "
        "load_dept_yaml_raw."
    )


def test_load_mandate_md_returns_none_for_unknown_slug(client):
    """No exception, no crash — returns None cleanly."""
    from console.services import github_reader
    result = github_reader.load_mandate_md("does-not-exist-zzz")
    assert result is None


def test_load_mandate_md_reads_real_file(tmp_path, monkeypatch):
    """When MANDATE.md exists at repo root, it's returned verbatim."""
    from console.services import github_reader
    fake_repo = tmp_path / "bubble-ops-fakeslug"
    fake_repo.mkdir()
    (fake_repo / "MANDATE.md").write_text(_SENTINEL_MANDATE, encoding="utf-8")
    # Patch the symbol in the module where it's *used*, not its source.
    monkeypatch.setattr(
        github_reader, "repo_path",
        lambda slug: fake_repo if slug == "fakeslug" else None,
    )
    out = github_reader.load_mandate_md("fakeslug")
    assert out == _SENTINEL_MANDATE


def test_onboarding_page_renders_mandate_section(client, monkeypatch):
    """GET /agents/<slug>/onboarding must include the MANDATE.md content
    in the rendered page (verbatim or as collapsed/expandable section)."""
    from console.services import github_reader
    monkeypatch.setattr(
        github_reader, "load_mandate_md",
        lambda slug: _SENTINEL_MANDATE if slug == "miranda" else None,
    )
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text
    # The unique sentinel marker must appear somewhere in the rendered HTML
    assert "sentinel-marker-12345" in body, (
        "MANDATE.md body not rendered on the onboarding page. The whole "
        "point is that Joris can read what the agent committed to."
    )


def test_dept_detail_page_renders_mandate_section(client, monkeypatch):
    """GET /dept/<slug> (operating phase) must also include the MANDATE.md
    content."""
    from console.services import github_reader
    monkeypatch.setattr(
        github_reader, "load_mandate_md",
        lambda slug: _SENTINEL_MANDATE if slug == "fixture" else None,
    )
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    assert "sentinel-marker-12345" in body, (
        "MANDATE.md body not rendered on the operating dept page. Joris "
        "must be able to read it any time, not just during éclosion."
    )


def test_onboarding_page_handles_missing_mandate_gracefully(client, monkeypatch):
    """No MANDATE.md yet (early éclosion) → page still renders, just
    without the mandate section content (or with a 'pas encore défini'
    placeholder)."""
    from console.services import github_reader
    monkeypatch.setattr(
        github_reader, "load_mandate_md", lambda slug: None,
    )
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    # No crash, page renders normally
    assert "Miranda" in r.text or "miranda" in r.text.lower()


def test_dept_detail_page_handles_missing_mandate_gracefully(client, monkeypatch):
    """Same for the operating page — gracefully degrades when MANDATE.md
    is missing (e.g. older fixture-style depts created before the
    mandate flow)."""
    from console.services import github_reader
    monkeypatch.setattr(
        github_reader, "load_mandate_md", lambda slug: None,
    )
    r = client.get("/dept/fixture")
    assert r.status_code == 200
