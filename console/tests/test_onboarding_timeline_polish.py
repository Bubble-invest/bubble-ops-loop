"""
test_onboarding_timeline_polish.py — Item E4 polish.

The onboarding timeline puces (bullets) must read with confidence:
  - validated   → filled ochre + ✓ inside (state: complété, with confidence)
  - in_progress → filled ochre + halo ring (state: 'là où on en est')
  - pending     → hollow outline, no fill (state: 'à venir')

These are CSS state expressed via a `data-status` attribute on the <li>
(or a BEM modifier class). The CSS rules in console/static/style.css
must (1) place the checkmark on validated, (2) add a halo ring on
in_progress, (3) leave the bullet hollow on pending.

We assert markup + CSS rule presence (test_static_no_auth pattern: the
test reads style.css directly; static is unauthenticated anyway since
the bearer-no-auth fix).
"""
from __future__ import annotations

import re
from pathlib import Path

STYLE_CSS = (
    Path(__file__).parents[1] / "static" / "style.css"
)


def test_validated_etape_carries_data_status_validated(client):
    """Markup: each <li class='etape' ...> for a validated step must carry
    data-status='validated' so CSS can target it."""
    r = client.get("/agents/miranda/onboarding")
    assert r.status_code == 200
    body = r.text
    # The miranda fixture has mandate/missions/layers validated.
    # Look for the literal substring on the timeline li.
    # The <li class="etape"> attributes include data-step=mandate +
    # data-status=validated.
    assert re.search(
        r'<li class="etape"\s+data-step="mandate"\s+'
        r'data-step-num="1"\s+data-status="validated"',
        body,
    ), "validated step <li> missing data-status='validated' attribute"


def test_current_etape_carries_data_status_in_progress(client):
    """Markup: the in-progress step must carry data-status='in_progress'."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    # Miranda is Drafting → step 4 (skills_tools) becomes in_progress
    assert re.search(
        r'<li class="etape"\s+data-step="skills_tools"\s+'
        r'data-step-num="4"\s+data-status="in_progress"',
        body,
    ), "in_progress step <li> missing data-status='in_progress' attribute"


def test_pending_etape_carries_data_status_pending(client):
    """Markup: a future step must carry data-status='pending'."""
    r = client.get("/agents/miranda/onboarding")
    body = r.text
    # Step 7 (activation) is pending for miranda (Drafting status)
    assert re.search(
        r'<li class="etape"\s+data-step="activation"\s+'
        r'data-step-num="7"\s+data-status="pending"',
        body,
    ), "pending step <li> missing data-status='pending' attribute"


def test_css_validated_puce_has_checkmark():
    """CSS rule: .etape[data-status='validated']::before must contain a
    checkmark glyph (either via `content` or a chained ::after rule)."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    # Look for the checkmark on validated rule. Accept either:
    #   .etape[data-status="validated"]::after { content: "✓"; ... }
    # or .etape[data-status="validated"]::before { content: "✓"; ... }
    has_check = bool(re.search(
        r'\.etape\[data-status="validated"\](?:::before|::after)\s*\{[^}]*content:\s*"✓"',
        css,
    ))
    assert has_check, \
        "CSS missing checkmark on .etape[data-status='validated'] ::before/::after"


def test_css_in_progress_puce_has_halo():
    """CSS rule: .etape[data-status='in_progress']::before must have an
    extra ring (halo). We accept either box-shadow with a ring OR an
    outline rule, on top of the existing ochre fill."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    # Extract the .etape[data-status="in_progress"]::before block
    m = re.search(
        r'\.etape\[data-status="in_progress"\]::before\s*\{([^}]*)\}',
        css,
    )
    assert m, "missing .etape[data-status='in_progress']::before rule"
    block = m.group(1)
    has_halo = ("box-shadow" in block) or ("outline" in block)
    assert has_halo, (
        "in_progress puce must have a halo (box-shadow or outline ring), "
        f"found: {block!r}"
    )


def test_css_pending_puce_is_hollow():
    """CSS rule: .etape[data-status='pending']::before (or default) must
    leave the puce hollow — background should be papier-tone (paper, not
    ochre) — i.e. NOT filled with ochre."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    # The default `.etape::before` block is the pending shape — confirm
    # it uses paper background. Find the first .etape::before block.
    m = re.search(
        r'(?<!\])\.etape::before\s*\{([^}]*)\}',
        css,
    )
    assert m, "missing default .etape::before rule"
    block = m.group(1)
    # The default block must have background: var(--paper) (hollow)
    assert "background: var(--paper)" in block, (
        "default .etape::before must use background: var(--paper) (hollow)"
    )
