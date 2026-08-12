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
    """Assemble the full Living Portfolio Report dataset on demand."""
    cached = _cached(slug)
    if cached is not None:
        return cached

    root = repo_path(slug)
    if root is None:
        return {}

    # A: structural data from vault_to_graph
    base = _run_vault_to_graph(root)

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

    # Harden the client render contract: thesis_book.html iterates these list
    # fields UNGUARDED (`DATA.<field>.forEach(...)`), so a missing/empty key
    # throws "Cannot read properties of undefined (reading 'forEach')" and blanks
    # the ENTIRE page (found live 2026-08-11: `macro` absent when vault/macro is
    # empty). Guarantee every iterated field is at least [].
    for _k in ("macro", "themes", "nodes", "clusters", "sectors"):
        base.setdefault(_k, [])

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
