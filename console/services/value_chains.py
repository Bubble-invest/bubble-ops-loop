"""
value_chains.py — count-only read of Ben's GICS value-chain sector maps for
the /dept/<slug> compact summary (board #1082, trimming #727/Part 2 of #725).

Ben's `vault/value-chains/` holds an `_index.md` overview plus one Markdown
file per GICS sector (~11 sector maps, ~500 names tagged own/watch/early/ran/
private — see #727/#725). This module used to also render that vault to HTML
(overview + per-sector bodies + a tag legend) for a dedicated /dept/<slug>/
value-chains sub-page (#1065). That sub-page was removed in #1079 once Ben's
live Value-Chain Maps panel became the embedded 3rd tab on /dept/<slug>/
portfolio (#1078, rendered separately by
console.routes.thesis_book._latest_maps_html straight from
outputs/<date>/value-chain-maps-panel.html — NOT this module). That left the
main /dept/<slug> page's compact summary (dept_detail.html section "02b") as
the ONLY consumer of this module, and it only ever read `.available` /
`.sector_count` — never the rendered HTML. So as of #1082 this module does
the cheap thing the one remaining consumer actually needs: check the dir
exists and count sector files, no markdown parsing/sanitizing/rendering of
all 11 maps on every main-page load.

Graceful degradation is unchanged: any dept without a `vault/value-chains/`
dir (every dept but Ben today) gets `ValueChainData(available=False)` — the
template omits the whole section, never a crash or an empty shell (mirrors
risk_clusters.py's `has_clusters` gate).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from console.services.dept_registry import repo_path

_log = logging.getLogger(__name__)

# Sector map files to skip when counting "per-sector" maps.
_INDEX_FILENAME = "_index.md"


@dataclass(frozen=True)
class ValueChainData:
    """Everything /dept/<slug>'s compact value-chains summary needs, or the
    graceful empty state when the vault subdir isn't present. Count-only —
    see module docstring for why the full HTML render (overview/sectors/tag
    legend) was dropped in #1082; nothing reads it any more."""
    available: bool = False
    sector_count: int = 0


def load_value_chains(slug: str) -> ValueChainData:
    """Return the value-chains COUNT for a dept's compact summary. Empty/
    unavailable (graceful) for any dept without vault/value-chains/ — only
    Ben has this vault subdir today.

    Cheap by construction: this only stats the directory listing to decide
    availability and count sector files by filename — it never reads or
    parses file contents, and never touches markdown rendering (#1082;
    contrast with the pre-#1082 version, which rendered all ~11 sector maps
    to sanitized HTML plus the overview on every call, for a number nothing
    but this same summary read)."""
    root = repo_path(slug)
    if root is None:
        return ValueChainData()

    vc_dir = root / "vault" / "value-chains"
    if not vc_dir.exists() or not vc_dir.is_dir():
        return ValueChainData()

    has_index = False
    sector_count = 0
    for p in vc_dir.iterdir():
        if not p.is_file() or p.suffix != ".md":
            continue
        if p.name == _INDEX_FILENAME:
            # Non-empty check mirrors the pre-#1082 behavior of only treating
            # a readable/non-empty _index.md as "there's an overview" —
            # cheap (a stat), not a read.
            try:
                has_index = p.stat().st_size > 0
            except OSError as exc:
                _log.warning("value_chains: failed stat-ing %s: %s", p, exc)
            continue
        if p.name.startswith((".", "_")):
            continue
        sector_count += 1

    if not has_index and sector_count == 0:
        # Dir exists but is empty (or holds only an empty/unreadable
        # _index.md) — still "not available" from the page's point of view.
        return ValueChainData()

    return ValueChainData(available=True, sector_count=sector_count)
