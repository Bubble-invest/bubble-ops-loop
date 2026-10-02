"""#1329 — sector weights must use pushed graph-data, like the header NAV."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest


@pytest.fixture
def sector_repo(tmp_path, monkeypatch):
    from console.services import thesis_book as tb

    repo = tmp_path / "agents" / "fixture"
    (repo / "db").mkdir(parents=True)
    with sqlite3.connect(repo / "db" / "fund.sqlite") as con:
        con.execute("CREATE TABLE positions (market_value REAL)")
        con.execute("CREATE TABLE kpi_snapshots (id INTEGER, nav REAL)")
        assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    outdir = repo / "outputs" / date.today().isoformat()
    outdir.mkdir(parents=True)
    monkeypatch.setenv("CANONICAL_AGENTS_ROOT", str(repo.parent))
    monkeypatch.setattr(tb, "_cached", lambda slug: None)
    return tb, repo, outdir / "graph-data.json"


@pytest.mark.parametrize("live_weight", [0.0, 1.0])
def test_sector_weights_prefer_pushed_graph_data(sector_repo, monkeypatch, live_weight):
    tb, repo, graph_path = sector_repo
    pushed = {
        "portfolio_overview": {"nav": 100000.0},
        "sectors": [
            {"name": "Industrials", "weight_pct": 6.67, "acwi_weight_pct": 10.5},
            {"name": "Financials", "weight_pct": 4.13, "acwi_weight_pct": 16.0},
        ],
        "acwi_sector_weights": {"Industrials": 10.5, "Financials": 16.0},
    }
    graph_path.write_text(json.dumps(pushed), encoding="utf-8")
    # The producer is owned by Ben's repo; simulate its stale/empty-DB output.
    live = {
        "sectors": [{"name": s["name"], "weight_pct": live_weight,
                     "acwi_weight_pct": 9.0} for s in pushed["sectors"]],
        "acwi_sector_weights": {"Industrials": 9.0, "Financials": 15.0},
    }
    monkeypatch.setattr(tb, "_run_vault_to_graph", lambda root: live)

    assert tb.runtime_repo_path("fixture") == repo
    data = tb.build_thesis_data("fixture")

    assert data["sectors"] == pushed["sectors"]
    assert all(s["weight_pct"] > 0 for s in data["sectors"])
    assert data["acwi_sector_weights"] == pushed["acwi_sector_weights"]
    assert data["nav"] == pushed["portfolio_overview"]["nav"]


@pytest.mark.parametrize("pushed", [None, {},
    {"sectors": [], "acwi_sector_weights": {}},
    {"sectors": {}, "acwi_sector_weights": []},
])
def test_sector_weights_keep_live_fallback(sector_repo, monkeypatch, pushed):
    tb, _, graph_path = sector_repo
    if pushed is not None:
        graph_path.write_text(json.dumps(pushed), encoding="utf-8")
    live = {
        "sectors": [{"name": "Industrials", "weight_pct": 2.5,
                     "acwi_weight_pct": 10.5}],
        "acwi_sector_weights": {"Industrials": 10.5},
    }
    monkeypatch.setattr(tb, "_run_vault_to_graph", lambda root: live)

    expected_sectors = live["sectors"]
    expected_benchmark = live["acwi_sector_weights"]
    data = tb.build_thesis_data("fixture")

    assert data["sectors"] == expected_sectors
    assert data["acwi_sector_weights"] == expected_benchmark
