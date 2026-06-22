"""markdown_render.py — render dept-authored Markdown to SAFE HTML.

WHY
---
The free-space whiteboard (`whiteboard.md`) lets a dept agent surface ANY data
representation it wants — tables, headings, rich text, embedded chart images.
Until now the cockpit rendered it as `{{ whiteboard_freeform }}` (Jinja
auto-escaped) → the agent's markdown showed as raw plain text (Ben's allocation
tables rendered literally). This module turns that markdown into HTML so the
board renders as intended.

SECURITY (this is the trust boundary)
-------------------------------------
The content is AGENT-authored, not human-authored, so we treat it as untrusted
and SANITIZE the rendered HTML against a strict allowlist with nh3 (Rust
ammonia — the maintained bleach successor). Allowed: structural + table +
formatting tags, links, and images. Stripped: <script>, <style>, event handlers
(onclick…), <iframe>/<object>, and any tag/attr not on the allowlist. This keeps
the whiteboard "an artifact the agent fills with any data rep" WITHOUT giving an
agent script execution in {{OPERATOR}}'s cockpit ({{OPERATOR}} 2026-06-19: keep it secure →
markdown+sanitize, NOT sandboxed JS).

Images are allowed so an agent can embed a chart it generated (e.g. the
auth-gated /gate/<slug>/chart route, or a saved PNG); we constrain `src` schemes
to http(s) and same-origin relative paths (no `data:`/`javascript:`).
"""
from __future__ import annotations

import markdown as _md
import nh3
from markupsafe import Markup

# Tags an agent may use to present data. No script/style/iframe/object/form.
_ALLOWED_TAGS = {
    # structure / text
    "p", "br", "hr", "div", "span", "blockquote", "pre", "code",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "u", "s", "del", "ins", "mark", "small", "sub", "sup",
    # lists
    "ul", "ol", "li", "dl", "dt", "dd",
    # tables (the common case — Ben's allocation board)
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col",
    # links + media
    "a", "img", "figure", "figcaption",
}

_ALLOWED_ATTRS = {
    # NOTE: do NOT list "rel" here — nh3 manages the rel attribute itself via the
    # link_rel arg below (it raises if both are set). target kept for new-tab links.
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title", "width", "height", "loading"},
    "td": {"align", "colspan", "rowspan"},
    "th": {"align", "colspan", "rowspan", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
    # allow a class hook on a few block tags for cockpit styling, no inline style
    "div": {"class"}, "span": {"class"}, "table": {"class"}, "code": {"class"},
}

# url schemes nh3 will keep on href/src; everything else (javascript:, data:) dropped
_ALLOWED_SCHEMES = {"http", "https", "mailto"}

# markdown extensions: tables (Ben's board), fenced code, sane lists, no raw HTML
_MD_EXTENSIONS = ["tables", "fenced_code", "sane_lists", "nl2br"]


def render_markdown_safe(text: str | None) -> Markup | None:
    """Render dept markdown → sanitized HTML, wrapped Markup so Jinja won't
    re-escape it. Returns None for empty/whitespace input.

    Fail-safe: on ANY rendering error, fall back to escaped plain text (never
    raise into the request, never emit unsanitized HTML)."""
    if not text or not text.strip():
        return None
    try:
        raw_html = _md.markdown(
            text,
            extensions=_MD_EXTENSIONS,
            output_format="html",
        )
        clean = nh3.clean(
            raw_html,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRS,
            url_schemes=_ALLOWED_SCHEMES,
            link_rel="noopener noreferrer",
        )
        return Markup(clean)
    except Exception:
        # escaped plain text — visible, never executable
        return Markup(nh3.clean(text, tags=set(), attributes={}))
