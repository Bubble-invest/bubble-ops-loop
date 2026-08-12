"""
thesis_book.py — Live data assembly for the Living Portfolio Report.

Produces the full graph-data dict on demand by merging:
  A. vault_to_graph output (nodes, themes, clusters, sectors)
  B. Live portfolio state from fund.sqlite + today's outputs

Cached for 60s (vault files don't change intra-minute).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from console.services.dept_registry import repo_path

_log = logging.getLogger(__name__)

_cache: Dict[str, Any] = {}
_CACHE_TTL = 60


# ── The render contract ─────────────────────────────────────────────────────
# console/templates/thesis_book.html assembles tab 1 of /dept/<slug>/portfolio
# LIVE, from this payload, inside ONE IIFE — so a single missing key that the
# template dereferences used to blank the WHOLE tab (the 2026-08-11/12 outage:
# `m.exposure_pct_nav.toFixed(1)` on a key `vault_to_graph.py` has never
# emitted).
#
# WHY THIS CONSTANT EXISTS: the template was copied from a hand-authored
# artifact whose data was richer than what the producer actually emits, and the
# producer (bubble-ops-ben/tools/vault_to_graph.py) is owned by another repo we
# cannot pin. Every previous fix patched that day's symptom — e.g.
# `base.setdefault("macro", [])` let an EMPTY macro list survive, and the moment
# macro became populated the page died on the next unguarded field. So the fix
# has to be structural on BOTH sides:
#
#   1. this module fills a safe default for every key the template reads and
#      LOGS LOUDLY which ones the producer omitted (below), and
#   2. the template routes every field through a nullable formatter and
#      isolates each render step (see the render-contract comment there).
#
# If you add a field to the template, add it here. If a key shows up in the
# "producer omitted" warning below, that is the producer drifting — fix it in
# bubble-ops-ben, don't delete it from this list.
#
# Fence: console/tests/test_thesis_book_total_renderer.py

#: Top-level keys the template dereferences, mapped to a safe default.
#: `None` renders as an em-dash; `[]`/`{}` render as an empty section.
REQUIRED_THESIS_KEYS: Dict[str, Any] = {
    "generated_at": None,       # header + footer date
    "nav": None,                # header NAV
    "since_rebase_pct": None,   # header return
    "vs_acwi_pct": None,        # header "vs ACWI" clause (optional in practice)
    "node_count": None,         # footer
    "theme_count": None,        # footer
    "nodes": [],                # iterated: NODE_BY_ID, sector view, footer
    "themes": [],               # iterated: theme cards, search index
    "macro": [],                # iterated: macro grid, theme grouping
    "clusters": [],             # iterated (reserved)
    "sectors": [],              # iterated: sector view
    "acwi_sector_weights": {},  # sector view benchmark column
    "global_macro": {},         # narrative + key_signals
    "portfolio_overview": {},   # overview card
}

#: Per-item keys read off each `macro[]` entry.
#: `themes_driven` is the template's name for what the producer emits as
#: `themes` — normalized below so an upstream rename cannot silently empty the
#: macro→theme grouping (it did, for months: every theme fell into
#: "Cross-Cutting & Idiosyncratic").
MACRO_ITEM_KEYS: Dict[str, Any] = {
    "id": None,
    "title": None,
    "subtitle": None,
    "indicators": [],
    "themes_driven": [],
    "exposure_usd": None,       # NOT emitted by vault_to_graph today
    "exposure_pct_nav": None,   # NOT emitted — this is what caused the outage
    "return_wtd": None,         # NOT emitted
    "research_note": None,      # NOT emitted
}

#: Per-item keys read off each `themes[]` entry.
THEME_ITEM_KEYS: Dict[str, Any] = {
    "id": None,
    "name": None,
    "ticker_count": None,
    "tickers": [],
    "last_verified": None,
    "review_by": None,
    "held_tickers": [],         # NOT emitted by vault_to_graph today
    "exposure_usd": None,       # NOT emitted
    "exposure_pct_nav": None,   # NOT emitted — latent twin of the outage line
    "theme_return_wtd": None,   # NOT emitted
    "watchlist_return_avg": None,  # NOT emitted
}

#: Keys the producer is currently known not to emit. They are still filled with
#: safe defaults, but they are NOT re-reported on every request — only a change
#: in the missing set is worth a log line.
_KNOWN_ABSENT = {
    # genuinely optional at top level — the template already renders these as a
    # dropped clause, so their absence is not a contract breach
    "top": {"vs_acwi_pct"},
    "macro": {"exposure_usd", "exposure_pct_nav", "return_wtd", "research_note"},
    "themes": {"held_tickers", "exposure_usd", "exposure_pct_nav",
               "theme_return_wtd", "watchlist_return_avg"},
}

#: slug -> last logged (top_missing, macro_missing, theme_missing) signature.
_last_contract_signature: Dict[str, tuple] = {}


def _fill_items(items: Any, spec: Dict[str, Any]) -> tuple:
    """Fill `spec` defaults into every dict in `items`. Returns
    (normalized_list, set_of_keys_that_were_missing_from_at_least_one_item)."""
    missing: set = set()
    out: List[dict] = []
    if not isinstance(items, list):
        return [], set(spec)
    for it in items:
        if not isinstance(it, dict):
            continue
        for key, default in spec.items():
            if it.get(key) is None:
                missing.add(key)
                it[key] = [] if isinstance(default, list) else default
        out.append(it)
    return out, missing


def normalize_thesis_data(base: Any, slug: str = "?") -> dict:
    """Make `base` satisfy the documented render contract, and log loudly when
    the producer omitted something.

    This is the server half of "the consumer is total": the template still
    guards every field itself, but a payload that leaves this function is
    already shaped so the template's guards never have to fire on a key we
    know about — and anything it DID have to invent shows up in the log
    instead of as a blank tab nobody notices until Joris opens the page.
    """
    if not isinstance(base, dict):
        base = {}

    top_missing = set()
    for key, default in REQUIRED_THESIS_KEYS.items():
        if base.get(key) is None:
            top_missing.add(key)
            base[key] = [] if isinstance(default, list) else (
                {} if isinstance(default, dict) else default)
        elif isinstance(default, list) and not isinstance(base[key], list):
            top_missing.add(key)
            base[key] = []
        elif isinstance(default, dict) and not isinstance(base[key], dict):
            top_missing.add(key)
            base[key] = {}

    # The producer calls a macro's theme list `themes`; the template reads
    # `themes_driven`. Bridge it BEFORE filling defaults, so the grouping keeps
    # working across the rename either way.
    for m in base["macro"]:
        if isinstance(m, dict) and not m.get("themes_driven") and isinstance(
                m.get("themes"), list):
            m["themes_driven"] = m["themes"]

    base["macro"], macro_missing = _fill_items(base["macro"], MACRO_ITEM_KEYS)
    base["themes"], theme_missing = _fill_items(base["themes"], THEME_ITEM_KEYS)

    signature = (tuple(sorted(top_missing)), tuple(sorted(macro_missing)),
                 tuple(sorted(theme_missing)))
    if signature != _last_contract_signature.get(slug):
        _last_contract_signature[slug] = signature
        novel_top = top_missing - _KNOWN_ABSENT["top"]
        novel_macro = macro_missing - _KNOWN_ABSENT["macro"]
        novel_theme = theme_missing - _KNOWN_ABSENT["themes"]
        if novel_top or novel_macro or novel_theme:
            _log.warning(
                "thesis_book[%s]: producer payload does NOT satisfy the render "
                "contract — filled safe defaults. missing top-level=%s "
                "macro[]=%s themes[]=%s. See REQUIRED_THESIS_KEYS in "
                "console/services/thesis_book.py.",
                slug, sorted(novel_top), sorted(novel_macro),
                sorted(novel_theme),
            )
        elif macro_missing or theme_missing:
            _log.info(
                "thesis_book[%s]: known-absent producer fields defaulted "
                "(macro[]=%s themes[]=%s) — page renders them as '—'.",
                slug, sorted(macro_missing), sorted(theme_missing),
            )
    return base


def _cached(slug: str) -> Optional[dict]:
    entry = _cache.get(slug)
    if entry and (time.monotonic() - entry["at"]) < _CACHE_TTL:
        return entry["data"]
    return None


def _set_cache(slug: str, data: dict) -> None:
    _cache[slug] = {"data": data, "at": time.monotonic()}


def _today() -> str:
    return date.today().isoformat()


def _run_vault_to_graph(root: Path) -> dict:
    """Call vault_to_graph.py and capture the JSON output."""
    venv_py = root / ".venv" / "bin" / "python3"
    script = root / "tools" / "vault_to_graph.py"
    if not script.exists():
        return {}
    try:
        result = subprocess.run(
            [str(venv_py), str(script), "--out", "/dev/stdout"],
            capture_output=True, text=True, cwd=str(root), timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        _log.warning("vault_to_graph failed (rc=%d): %s", result.returncode,
                      result.stderr[:300])
    except Exception as exc:
        _log.warning("vault_to_graph error: %s", exc)
    return {}


def _open_db(db_path: Path) -> Optional[sqlite3.Connection]:
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error as exc:
        _log.warning("thesis_book: DB open failed %s: %s", db_path, exc)
        return None


def _build_portfolio_overview(db_path: Path, root: Path) -> dict:
    """Assemble portfolio_overview from kpi_snapshots + positions + decisions."""
    con = _open_db(db_path)
    if con is None:
        return {}
    try:
        # Latest 2 KPI snapshots for day-change calc
        rows = con.execute(
            "SELECT * FROM kpi_snapshots ORDER BY id DESC LIMIT 2"
        ).fetchall()
        if not rows:
            return {}
        latest = dict(rows[0])
        prev = dict(rows[1]) if len(rows) > 1 else {}

        nav = latest.get("nav", 0)
        prev_nav = prev.get("nav", nav)
        day_change = nav - prev_nav
        day_pct = (day_change / prev_nav * 100) if prev_nav else 0

        po: Dict[str, Any] = {
            "nav": nav,
            "nav_date": latest.get("snapshot_at", "")[:10],
            "since_rebase_pct": latest.get("total_return_since_rebase", 0),
            "day_change_usd": round(day_change),
            "day_change_pct": round(day_pct, 2),
            "mtd_pct": latest.get("total_return_mtd", 0),
            "ytd_pct": latest.get("total_return_ytd", 0),
            "drawdown_pct": latest.get("current_drawdown", 0),
            "max_drawdown_itd_pct": latest.get("max_drawdown_itd", 0),
            "sharpe_90d": latest.get("sharpe_90d", 0),
            "sortino_90d": latest.get("sortino_90d", 0),
            "vs_acwi_pct": latest.get("excess_return_since_rebase_acwi", 0),
            "acwi_return_pct": latest.get("acwi_return_itd", 0),
        }

        # Broker breakdown
        brokers: List[dict] = []
        max_snap = con.execute(
            "SELECT MAX(snapshot_at) FROM positions"
        ).fetchone()[0]
        if max_snap:
            broker_rows = con.execute(
                "SELECT broker, count(*) as cnt, sum(market_value) as mv "
                "FROM positions WHERE snapshot_at=? GROUP BY broker",
                (max_snap,),
            ).fetchall()
            for br in broker_rows:
                brokers.append({
                    "name": (dict(br).get("broker") or "unknown").title(),
                    "nav_usd": round(dict(br).get("mv") or 0),
                    "positions": dict(br).get("cnt", 0),
                    "status": "live",
                })

        # Boursorama
        try:
            brow = con.execute(
                "SELECT snapshot_at, sum(valuation_cents)/100.0 as nav_eur, "
                "count(*) as cnt FROM bourso_holdings "
                "WHERE snapshot_at=(SELECT MAX(snapshot_at) FROM bourso_holdings)"
            ).fetchone()
            if brow and dict(brow).get("nav_eur"):
                bd = dict(brow)
                stale_days = (date.today() - date.fromisoformat(
                    bd["snapshot_at"][:10])).days
                brokers.append({
                    "name": "Boursorama",
                    "nav_eur": round(bd["nav_eur"]),
                    "positions": bd["cnt"],
                    "status": f"{stale_days}d stale" if stale_days > 1 else "live",
                })
        except Exception:
            pass

        # Crypto.com
        try:
            crow = con.execute(
                "SELECT sum(valuation_usd) as nav_usd, count(*) as cnt "
                "FROM cryptocom_holdings "
                "WHERE snapshot_at=(SELECT MAX(snapshot_at) FROM cryptocom_holdings) "
                "AND is_dust=0"
            ).fetchone()
            if crow and dict(crow).get("nav_usd"):
                cd = dict(crow)
                brokers.append({
                    "name": "Crypto.com",
                    "nav_usd": round(cd["nav_usd"]),
                    "positions": cd["cnt"],
                    "status": "live",
                })
        except Exception:
            pass

        po["brokers"] = brokers

        # Pending decisions
        dec_rows = con.execute(
            "SELECT id, symbol, action, status, broker "
            "FROM decisions WHERE status IN ('proposed','approved') "
            "ORDER BY id DESC"
        ).fetchall()
        po["pending_decisions"] = [
            {"id": f"dec{dict(r)['id']}", "ticker": dict(r).get("symbol", ""),
             "action": dict(r).get("action", ""), "status": dict(r).get("status", "")}
            for r in dec_rows
        ]

        # Hedge overlay — latest executed hedge
        try:
            hedge = con.execute(
                "SELECT * FROM decisions WHERE kind='hedge' AND status='executed' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if hedge:
                hd = dict(hedge)
                po["hedge_overlay"] = {
                    "instrument": hd.get("symbol", ""),
                    "status": "live",
                }
        except Exception:
            pass

        return po
    except Exception as exc:
        _log.warning("thesis_book: portfolio_overview failed: %s", exc)
        return {}
    finally:
        con.close()


def _load_latest_graph_data(root: Path) -> dict:
    """Load the most recent graph-data.json from outputs/ as a fallback
    for macro/global_macro data that's hard to reconstruct live."""
    today = _today()
    for day_offset in range(7):
        d = date.today()
        d = date(d.year, d.month, d.day)
        from datetime import timedelta
        check = (d - timedelta(days=day_offset)).isoformat()
        path = root / "outputs" / check / "graph-data.json"
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                continue
    return {}


def build_thesis_data(slug: str) -> dict:
    """Assemble the full Living Portfolio Report dataset on demand.

    The returned dict always satisfies REQUIRED_THESIS_KEYS (see the render
    contract at the top of this module) — including on the no-repo path, which
    used to return a bare `{}` and hand the template a payload with no `nodes`
    at all.
    """
    cached = _cached(slug)
    if cached is not None:
        return cached

    root = repo_path(slug)
    if root is None:
        return normalize_thesis_data({}, slug)

    # A: structural data from vault_to_graph. It is an out-of-repo producer —
    # do not assume it even returned an object.
    base = _run_vault_to_graph(root)
    if not isinstance(base, dict):
        _log.warning("thesis_book[%s]: vault_to_graph returned %s, not an "
                     "object — falling back to defaults", slug, type(base).__name__)
        base = {}

    # B: live portfolio state
    db_path = root / "db" / "fund.sqlite"
    po = _build_portfolio_overview(db_path, root)
    if po:
        base["portfolio_overview"] = po
        if po.get("nav"):
            base["nav"] = po["nav"]
        if po.get("since_rebase_pct") is not None:
            base["since_rebase_pct"] = po["since_rebase_pct"]

    # Macro/global_macro: fall back to latest on-disk graph-data
    fallback = _load_latest_graph_data(root)
    if not isinstance(fallback, dict):
        fallback = {}
    for key in ("macro", "global_macro", "vs_acwi_pct", "acwi_return_pct"):
        if key not in base or not base.get(key):
            if key in fallback and fallback[key]:
                base[key] = fallback[key]

    # Merge top_movers from fallback if not in PO
    if "portfolio_overview" in base:
        for extra_key in ("top_movers", "hedge_overlay"):
            if extra_key not in base["portfolio_overview"]:
                fb_po = fallback.get("portfolio_overview", {})
                if extra_key in fb_po:
                    base["portfolio_overview"][extra_key] = fb_po[extra_key]

    # Satisfy the documented render contract (and shout about anything the
    # producer left out). This SUPERSEDES the old
    # `for _k in (...): base.setdefault(_k, [])` guard, which only covered the
    # five iterated lists — the 2026-08-12 outage was a *scalar* field
    # (`macro[].exposure_pct_nav`) inside a list that had been successfully
    # defaulted, which is exactly the gap a key-by-key contract closes.
    base = normalize_thesis_data(base, slug)

    _set_cache(slug, base)
    return base


def chart_path(slug: str, name: str) -> Optional[Path]:
    """Return the filesystem path for a named chart PNG, or None."""
    root = repo_path(slug)
    if root is None:
        return None
    today = _today()
    path = root / "outputs" / today / "charts" / name
    if path.exists() and path.suffix in (".png", ".jpg", ".svg"):
        return path
    # Try yesterday
    from datetime import timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    path = root / "outputs" / yesterday / "charts" / name
    if path.exists() and path.suffix in (".png", ".jpg", ".svg"):
        return path
    return None
