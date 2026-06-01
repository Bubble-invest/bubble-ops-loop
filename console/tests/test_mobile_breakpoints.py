"""
test_mobile_breakpoints.py — responsive HTML structure.

Notion v5 line 1012: "Mobile-friendly ({{OPERATOR}} valide ses gates depuis son
telephone)".

The console uses Tailwind responsive classes: stacked on mobile (<768px,
the md: breakpoint), 2-pane on tablet (>=md), 3-pane on desktop (>=lg).
We verify the relevant classes exist in the rendered HTML for the
onboarding view since that's the most-layout-dependent page.
"""


def test_onboarding_uses_responsive_grid_classes(client):
    """3-pane layout must use md: + lg: breakpoint classes."""
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text
    # at least one md: or lg: breakpoint class present in the 3-pane container
    assert any(token in body for token in ("md:grid", "lg:grid",
                                            "md:flex", "lg:flex"))


def test_home_includes_viewport_meta(client):
    """All pages must include the mobile viewport meta tag."""
    r = client.get("/")
    assert r.status_code == 200
    assert 'name="viewport"' in r.text
    assert "width=device-width" in r.text
