"""
test_mobile_responsive_css.py — QA-Spirit Blocker C.

At 375 px the home page H1 cut mid-word, the topnav clipped, the
collegue-row CTA truncated, and the role line had no ellipsis. The
console must be triage-able from {{OPERATOR}}'s phone (Notion v5 line 1012:
"Mobile-friendly — {{OPERATOR}} valide ses gates depuis son téléphone").

This test pins down the CSS contract that fixes the symptoms:
  - a 720 px tablet breakpoint that wraps the topbar and stacks
    .collegue-row columns
  - a 480 px phone breakpoint that single-columns grids and shrinks
    the hero
  - hero H1 declared with `clamp()` + `word-break: break-word` so it
    cannot be truncated mid-word
  - no `display: none` on operator-facing content (mobile must restack,
    never hide info)
"""
from __future__ import annotations

import re
from pathlib import Path

CSS_PATH = (
    Path(__file__).resolve().parent.parent / "static" / "style.css"
)


def _read_css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def test_mobile_breakpoint_720px_block_exists() -> None:
    """A tablet-and-down breakpoint at <=720px is required."""
    css = _read_css()
    assert "@media (max-width: 720px)" in css, (
        "missing @media (max-width: 720px) block — tablet/large-phone"
    )


def test_mobile_breakpoint_480px_block_exists() -> None:
    """A phone-and-down breakpoint at <=480px is required."""
    css = _read_css()
    assert "@media (max-width: 480px)" in css, (
        "missing @media (max-width: 480px) block — phone"
    )


def test_hero_title_uses_clamp_and_word_break() -> None:
    """Hero H1 must resize fluidly AND never truncate mid-word."""
    css = _read_css()
    # find the .page-hero-title rule body (anywhere in the file)
    pat = re.compile(
        r"\.page-hero-title\s*\{[^}]*\}", re.MULTILINE
    )
    blocks = pat.findall(css)
    assert blocks, ".page-hero-title rule missing entirely"
    joined = "\n".join(blocks)
    assert "clamp(" in joined, (
        ".page-hero-title must use clamp() for fluid sizing on mobile"
    )
    assert "word-break" in joined or "overflow-wrap" in joined or "hyphens" in joined, (
        ".page-hero-title must declare word-break / overflow-wrap / hyphens"
    )


def test_mobile_has_at_least_five_class_rules() -> None:
    """The mobile blocks must restack at least 5 distinct surfaces."""
    css = _read_css()
    # extract content of both mobile @media blocks
    pat = re.compile(
        r"@media\s*\(max-width:\s*(?:720|480)px\)\s*\{(?P<body>(?:[^{}]|\{[^}]*\})*)\}",
        re.MULTILINE,
    )
    blocks = pat.findall(css)
    assert blocks, "no mobile @media block content found"
    inner = "\n".join(blocks)
    # count class-selector rules inside the @media bodies
    class_rules = re.findall(r"\.[a-zA-Z][a-zA-Z0-9_-]*\s*\{", inner)
    assert len(class_rules) >= 5, (
        f"mobile blocks contain only {len(class_rules)} class rules "
        "(need >=5 to restack hero / topbar / collegue / decision / "
        "kpi grids)"
    )


def test_mobile_does_not_hide_operator_content() -> None:
    """`display: none` is forbidden inside mobile blocks — info must
    restack, not disappear ({{OPERATOR}} must see the same content as on desktop,
    just laid out vertically)."""
    css = _read_css()
    pat = re.compile(
        r"@media\s*\(max-width:\s*(?:720|480)px\)\s*\{(?P<body>(?:[^{}]|\{[^}]*\})*)\}",
        re.MULTILINE,
    )
    blocks = pat.findall(css)
    inner = "\n".join(blocks)
    # `display:none` on aria-hidden or pseudo-decorations would be OK
    # in principle, but the simpler safer contract is: no display:none
    # in mobile blocks at all. Restack instead.
    assert "display: none" not in inner and "display:none" not in inner, (
        "display:none found in mobile block — info must restack, "
        "not be hidden on phone"
    )


def test_collegue_row_restacks_on_mobile() -> None:
    """`.collegue-row` (the live-team list row) must restack to column
    on mobile so the CTA, role, and state don't compete for width."""
    css = _read_css()
    # extract the 720px block specifically
    pat = re.compile(
        r"@media\s*\(max-width:\s*720px\)\s*\{(?P<body>(?:[^{}]|\{[^}]*\})*)\}",
        re.MULTILINE,
    )
    m = pat.search(css)
    assert m, "720px mobile block missing"
    body = m.group("body")
    assert ".collegue-row" in body, (
        ".collegue-row must be redeclared in the 720px block to restack"
    )
    # `flex-direction: column` is the expected restacking method
    # (current desktop style uses `align-items: center` row layout).
    assert "flex-direction: column" in body or "flex-direction:column" in body, (
        ".collegue-row must restack to column on mobile"
    )


def test_nav_restacks_on_mobile() -> None:
    """The nav (CSS-only hamburger sidebar — `.topbar`/`.topnav` were
    removed as dead CSS in #449, confirmed zero references in base.html,
    which uses `.sidebar`/`.app-shell` instead) must restyle so it doesn't
    clip on narrow viewports: the toggle becomes visible and the sidebar
    itself goes off-canvas, both keyed off the same breakpoint family."""
    css = _read_css()
    pat = re.compile(
        r"@media\s*\(max-width:\s*768px\)\s*\{(?P<body>(?:[^{}]|\{[^}]*\})*)\}",
        re.MULTILINE,
    )
    m = pat.search(css)
    assert m, "768px mobile sidebar block missing"
    body = m.group("body")
    assert ".sidebar-toggle-label" in body, (
        "sidebar hamburger toggle must be shown at the mobile breakpoint"
    )
    assert "transform" in body, (
        "sidebar must go off-canvas (transform) on mobile so it doesn't clip content"
    )


def test_grids_collapse_to_single_column_on_phone() -> None:
    """At 480px the multi-column grids (kpi, decisions) must collapse
    to a single column so CTAs/labels don't truncate."""
    css = _read_css()
    pat = re.compile(
        r"@media\s*\(max-width:\s*480px\)\s*\{(?P<body>(?:[^{}]|\{[^}]*\})*)\}",
        re.MULTILINE,
    )
    m = pat.search(css)
    assert m, "480px mobile block missing"
    body = m.group("body")
    assert "1fr" in body, (
        "480px block must collapse at least one grid to a single 1fr column"
    )
