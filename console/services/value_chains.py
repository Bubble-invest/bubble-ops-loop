"""
value_chains.py — read-only render of Ben's GICS value-chain sector maps on
/dept/ben (board #727, Part 2 of #725).

Ben's `vault/value-chains/` holds an `_index.md` overview plus one Markdown
file per GICS sector (~11 sector maps, ~500 names tagged own/watch/early/ran/
private — see #727/#725). This module reads those files directly, using the
SAME repo_path(slug) -> vault_dir access pattern as risk_clusters.py /
thesis_book.py (read-only, no recompute, no new dependency) and the SAME
markdown-render helper the whiteboard/thesis-book panes already use
(markdown_render.render_markdown_safe — agent-authored markdown is untrusted,
so it's sanitized to a strict HTML allowlist, never raw).

Graceful degradation: any dept without a `vault/value-chains/` dir (every dept
but Ben today) gets `ValueChainData(available=False)` — the template omits the
whole section, never a crash or an empty shell (mirrors risk_clusters.py's
`has_clusters` gate).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from markupsafe import Markup

from console.services.dept_registry import repo_path
from console.services.markdown_render import render_markdown_safe

_log = logging.getLogger(__name__)

# Sector map files to skip when listing "per-sector" maps.
_INDEX_FILENAME = "_index.md"

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
# Minimal frontmatter stripper (a sector map may or may not open with a
# `---`-delimited YAML block) — this module only ever needs the BODY text to
# render, unlike risk_clusters.py which reads specific frontmatter fields, so
# a full parse isn't warranted here; just don't let a raw `---\nkey: val\n---`
# block render as a stray markdown rule + prose in the output.
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)

#: Static tag legend — the union of every tag actually used inline across
#: Ben's live vault/value-chains/*.md sector maps (verified against
#: /home/claude/agents/bubble-ops-ben/vault/value-chains/ during #727 review;
#: the card's own summary only named the first 5). The vault does not (yet)
#: ship a machine-readable legend of its own, so this mirrors the vault's own
#: emoji + tag vocabulary rather than scraping one from content. If Ben's
#: vault ever adds a legend file, prefer reading it over this constant.
#: `excluded-own` and `wrong-sector` are both included as variants seen
#: across different sector files (financials/industrials/information-
#: technology use `excluded-own` as an 8th tag; energy.md uses
#: `wrong-sector` instead).
TAG_LEGEND: List[dict] = [
    {"code": "early", "label": "🟢 Early", "desc": "Early-stage, watchlist-adjacent."},
    {"code": "watch", "label": "🔵 Watch", "desc": "Watching."},
    {"code": "own", "label": "🟡 Own", "desc": "We hold."},
    {"code": "ran", "label": "🔴 Ran", "desc": "Ran — exited, a past position."},
    {"code": "private", "label": "⚪ Private", "desc": "Private, not investable."},
    {"code": "broken", "label": "⚫ Broken", "desc": "Cheap for a structural reason, not an opportunity."},
    {"code": "arb", "label": "🔶 Arb", "desc": "Deal pending, not an entry."},
    {"code": "excluded-own", "label": "🚫 Excluded-own", "desc": "Held via another sleeve — excluded here to avoid double-counting."},
    {"code": "wrong-sector", "label": "⛔ Wrong-sector", "desc": "Mapped here in error — belongs to another GICS sector."},
]


@dataclass(frozen=True)
class SectorMap:
    """One rendered `vault/value-chains/<slug>.md` sector map."""
    slug: str                     # filename stem, e.g. "technology"
    title: str                    # sector map's own H1, or a humanized filename
    html: Optional[Markup] = None  # sanitized markdown->HTML body; None if empty


@dataclass(frozen=True)
class ValueChainData:
    """Everything /dept/ben's value-chains section needs to render, or the
    graceful empty state when the vault subdir isn't present."""
    available: bool = False
    overview_html: Optional[Markup] = None
    sectors: List[SectorMap] = field(default_factory=list)
    tag_legend: List[dict] = field(default_factory=lambda: list(TAG_LEGEND))

    @property
    def sector_count(self) -> int:
        return len(self.sectors)


def _strip_frontmatter(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    return text[m.end():] if m else text


def _first_h1(text: str) -> str:
    m = _H1_RE.search(text)
    return m.group(1).strip() if m else ""


def _title_from_filename(stem: str) -> str:
    return (stem.replace("_", " ").replace("-", " ").strip() or stem).title()


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("value_chains: failed reading %s: %s", path, exc)
        return None


def load_value_chains(slug: str) -> ValueChainData:
    """Return the value-chains render data for a dept. Empty/unavailable
    (graceful) for any dept without vault/value-chains/ — only Ben has this
    vault subdir today."""
    root = repo_path(slug)
    if root is None:
        return ValueChainData()

    vc_dir = root / "vault" / "value-chains"
    if not vc_dir.exists() or not vc_dir.is_dir():
        return ValueChainData()

    overview_html: Optional[Markup] = None
    index_text = _read_text(vc_dir / _INDEX_FILENAME)
    if index_text:
        overview_html = render_markdown_safe(_strip_frontmatter(index_text))

    sectors: List[SectorMap] = []
    for p in sorted(vc_dir.glob("*.md")):
        if p.name == _INDEX_FILENAME or p.name.startswith((".", "_")):
            continue
        text = _read_text(p)
        if text is None:
            continue
        body = _strip_frontmatter(text)
        title = _first_h1(body) or _title_from_filename(p.stem)
        html = render_markdown_safe(body)
        sectors.append(SectorMap(slug=p.stem, title=title, html=html))

    if overview_html is None and not sectors:
        # Dir exists but is empty (or unreadable) — still "not available" from
        # the page's point of view; nothing to render.
        return ValueChainData()

    return ValueChainData(
        available=True,
        overview_html=overview_html,
        sectors=sectors,
    )
