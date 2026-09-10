"""canonical_nav.py — the ONE NAV headline every cockpit panel must read.

Board #1209: the cockpit showed 4 different NAVs because each panel read a
different source (pushed graph-data, a live un-audited broker-sum in
whiteboard.yaml, a stale risk-kpis figure, …). Ben (the fund dept, the data
owner) ruled:

  1. The canonical NAV is the frozen AUDITED morning mark from the latest
     VERIFIED kpi_snapshots row (nav, total_return_since_rebase, snapshot_at).
  2. The DEPLOYED console cannot read the gitignored DB (that is the #1207 root
     cause), so every panel must read the canonical figure from the PUSHED
     outputs/<date>/graph-data.json, which mirrors that row exactly:
        top-level  `nav` + `since_rebase_pct`
        portfolio_overview.nav + .since_rebase_pct + .nav_date
        generated_at
     graph-data.json is THE ONE pushed source every headline reads.
  3. The live consolidated_nav (consolidated_nav_usd / _true) is NOT a headline
     — it is an intraday tick that drifts on every refresh.
  4. Every NAV headline carries an as-of stamp and shows a stale indicator when
     the as-of date is older than today.

This module is that single resolver. It reuses thesis_book's existing 7-day
date-scan (`_load_latest_graph_data`) rather than duplicating it, so the header
NAV that the Thesis Book already shows (also from the pushed graph-data) and the
NAV the Dashboard + Risk-KPIs panels now show cannot diverge.

Degrades to an all-None result (never raises) for any dept with no
graph-data.json on disk — the templates then fall back to their existing
empty-state behavior, so depts other than Ben are unaffected.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from console.services.dept_registry import runtime_repo_path
from console.services.thesis_book import _load_latest_graph_data

#: The shape every caller can rely on, even on the no-data path.
_EMPTY: Dict[str, Any] = {
    "nav": None,
    "since_rebase_pct": None,
    "as_of": None,
    "is_stale": False,
    "source": "graph-data.json",
}


def _to_float(value: Any) -> Optional[float]:
    """Coerce a JSON scalar to float, or None (never raise)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def canonical_nav(slug: str) -> Dict[str, Any]:
    """Resolve the canonical (frozen, audited) NAV headline for `slug` from the
    latest pushed graph-data.json.

    Returns a dict of exactly:
        {"nav": float|None, "since_rebase_pct": float|None,
         "as_of": "YYYY-MM-DD"|None, "is_stale": bool,
         "source": "graph-data.json"}

    `nav` / `since_rebase_pct` prefer the top-level graph-data fields (which
    mirror the audited kpi_snapshots row) and fall back to the
    `portfolio_overview` copies. `as_of` is `portfolio_overview.nav_date` when
    present, else the (date-truncated) top-level `generated_at`. `is_stale` is
    True when `as_of` is strictly before today.

    Never raises: any dept without a graph-data.json on disk (or a malformed
    one) yields the all-None result, so a template can guard on `nav is None`
    to fall back to its prior behavior.
    """
    root = runtime_repo_path(slug)
    if root is None:
        return dict(_EMPTY)

    data = _load_latest_graph_data(root)
    if not isinstance(data, dict) or not data:
        return dict(_EMPTY)

    po = data.get("portfolio_overview")
    if not isinstance(po, dict):
        po = {}

    nav = _to_float(data.get("nav"))
    if nav is None:
        nav = _to_float(po.get("nav"))

    since = _to_float(data.get("since_rebase_pct"))
    if since is None:
        since = _to_float(po.get("since_rebase_pct"))

    as_of_raw = po.get("nav_date") or data.get("generated_at")
    as_of: Optional[str] = None
    if isinstance(as_of_raw, str) and as_of_raw.strip():
        as_of = as_of_raw.strip()[:10]

    is_stale = bool(as_of) and as_of < date.today().isoformat()

    return {
        "nav": nav,
        "since_rebase_pct": since,
        "as_of": as_of,
        "is_stale": is_stale,
        "source": "graph-data.json",
    }
