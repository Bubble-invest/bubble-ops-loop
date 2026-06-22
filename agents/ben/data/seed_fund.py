#!/usr/bin/env python3
"""
seed_fund.py — build a SYNTHETIC example fund database for the Ben example agent.

Everything here is FAKE: fictional tickers (ACME, GLOBEX, INITECH, …), fictional
prices, fictional NAV. No real company, no real position, no real money. The point
is to let the agent run L1→L2 out-of-the-box in a demo.

Run:  python3 agents/ben/data/seed_fund.py
Creates:  agents/ben/data/fund.sqlite  (overwrites if present)

The schema is a minimal subset of what a portfolio agent needs: positions,
decisions, trades, kpi_snapshots, research_items. Extend it for your own use.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fund.sqlite")

SCHEMA = """
CREATE TABLE positions (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    sleeve TEXT NOT NULL,          -- etf_backbone | single_stock | crypto
    qty REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    last_price REAL NOT NULL,
    market_value REAL NOT NULL,
    weight_pct REAL NOT NULL,
    opened_at TEXT NOT NULL
);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,            -- propose_buy | propose_sell | escalation | thesis_update
    ticker TEXT,
    status TEXT NOT NULL,          -- proposed | approved | rejected | executed | deferred
    risk_level TEXT,
    gate_policy_id TEXT,
    reasoning TEXT,
    idempotency_key TEXT,
    linked_trade_id INTEGER
);

CREATE TABLE trades (
    id INTEGER PRIMARY KEY,
    executed_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    fill_price REAL NOT NULL,
    broker TEXT NOT NULL,
    decision_id INTEGER
);

CREATE TABLE kpi_snapshots (
    id INTEGER PRIMARY KEY,
    as_of TEXT NOT NULL,
    nav REAL NOT NULL,
    cash REAL NOT NULL,
    total_return_itd_pct REAL,
    sharpe_itd REAL,
    current_drawdown_pct REAL
);

CREATE TABLE research_items (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    ticker TEXT,
    source TEXT,
    reason TEXT,
    priority TEXT,
    risk_level TEXT,
    status TEXT NOT NULL DEFAULT 'open'   -- open | processed
);
"""

# --- Synthetic book: fictional tickers, fictional everything ---------------- #
TODAY = date.today().isoformat()

POSITIONS = [
    # ticker, sleeve, qty, avg_entry, last, mv, weight_pct
    ("ACME",    "single_stock",  100, 95.00, 102.00, 10_200.0, 14.8),
    ("GLOBEX",  "single_stock",   40, 210.00, 215.00, 8_600.0, 12.5),
    ("INITECH", "etf_backbone",  150, 60.00,  63.50,  9_525.0, 13.8),
    ("HOOLI",   "etf_backbone",  200, 48.00,  47.20,  9_440.0, 13.7),
    ("UMBRELLA","crypto",          5, 1100.0, 1180.0,  5_900.0,  8.6),
]
CASH = 25_135.0

DECISIONS = [
    (TODAY, "propose_buy", "STARKIND", "proposed", "low_risk_trade", "trade_proposal",
     "EXAMPLE proposal: starter position in STARKIND on the synthetic infra theme. "
     "Awaiting human approval (proposals-only).", "ORDER-EXAMPLE-STARKIND-buy", None),
]

KPIS = [(TODAY, 68_800.0, CASH, 4.2, 0.91, -1.3)]

RESEARCH = [
    (TODAY, "ACME",    "price_trigger",     "ACME +7% on synthetic earnings beat — re-grade conviction", "high",   "research_only"),
    (TODAY, "STARKIND","watchlist_momentum","STARKIND fresh 3-month high on volume — tactical re-look",   "medium", "research_only"),
]


def main() -> None:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    con.executemany(
        "INSERT INTO positions (ticker,sleeve,qty,avg_entry_price,last_price,market_value,weight_pct,opened_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(t, s, q, ae, lp, mv, w, TODAY) for (t, s, q, ae, lp, mv, w) in POSITIONS],
    )
    con.executemany(
        "INSERT INTO decisions (created_at,kind,ticker,status,risk_level,gate_policy_id,reasoning,idempotency_key,linked_trade_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)", DECISIONS,
    )
    con.executemany(
        "INSERT INTO kpi_snapshots (as_of,nav,cash,total_return_itd_pct,sharpe_itd,current_drawdown_pct) "
        "VALUES (?,?,?,?,?,?)", KPIS,
    )
    con.executemany(
        "INSERT INTO research_items (created_at,ticker,source,reason,priority,risk_level) "
        "VALUES (?,?,?,?,?,?)", RESEARCH,
    )
    con.commit()

    nav = CASH + sum(p[5] for p in POSITIONS)
    print(f"[seed_fund] wrote {DB_PATH}")
    print(f"[seed_fund] synthetic NAV ${nav:,.0f} | {len(POSITIONS)} positions | "
          f"{len(DECISIONS)} open proposal | {len(RESEARCH)} research items")
    print("[seed_fund] ALL DATA IS FICTIONAL — fake tickers, fake prices, no real money.")
    con.close()


if __name__ == "__main__":
    main()
