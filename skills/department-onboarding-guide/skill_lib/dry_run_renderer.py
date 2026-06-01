"""
dry_run_renderer.py — UX-4

Render a DryRunResult as an HTMX-compatible HTML fragment matching the
Notion v5 lines 938-945 format:

    Dry run result:
    ✓ Layer 1 output valid
    ✓ Queue schema valid
    ✓ Layer 2 draft produced
    ✓ Gate produced
    ✓ Layer 3 dry-run execution valid
    ⚠ Missing brand safety test fixture

Output is a single HTML fragment (no <html>/<body> wrapper) so HTMX can
swap it directly into a target element.
"""
from __future__ import annotations

import html
from typing import Iterable

from .dry_run import Check, DryRunResult


_ICON = {
    "passed": "✓",
    "warning": "⚠",
    "failed": "✗",
}

_CLASS = {
    "passed": "dryrun-check dryrun-check--passed",
    "warning": "dryrun-check dryrun-check--warning",
    "failed": "dryrun-check dryrun-check--failed",
}


def _render_checks(checks: Iterable[Check]) -> str:
    rows = []
    for c in checks:
        icon = _ICON.get(c.status, "·")
        cls = _CLASS.get(c.status, "dryrun-check")
        rows.append(
            f'  <li class="{cls}"><span class="dryrun-icon">{icon}</span> '
            f'<span class="dryrun-step">{html.escape(c.step)}</span> '
            f'<span class="dryrun-msg">{html.escape(c.message)}</span></li>'
        )
    return "\n".join(rows)


def render_dry_run_html(result: DryRunResult) -> str:
    """Render `result` as an HTMX-swap-safe HTML fragment."""
    header = f'<h3 class="dryrun-heading">Dry run result:</h3>'
    badge = (
        f'<p class="dryrun-overall dryrun-overall--{result.overall_status.lower()}">'
        f'Overall: <strong>{html.escape(result.overall_status)}</strong> · '
        f'can_advance_to_ready=<strong>{str(result.can_advance_to_ready).lower()}</strong>'
        f'</p>'
    )
    list_html = _render_checks(result.checks)
    return (
        '<section class="dryrun-result" data-ts="'
        + html.escape(result.dry_run_ts) + '">\n'
        + header + "\n"
        + badge + "\n"
        + '<ul class="dryrun-checks">\n'
        + list_html + "\n"
        + '</ul>\n'
        + '</section>'
    )
