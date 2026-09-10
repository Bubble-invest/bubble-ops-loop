"""#1207 — Thesis Book header NAV must come from the PUSHED graph-data.json, not
an unpushed/stale fund.sqlite (which read $0 while the pushed data said $266k)."""
from __future__ import annotations
import datetime, json
from pathlib import Path


def test_thesis_header_nav_prefers_pushed_graph_data_over_zero_db(tmp_path, monkeypatch):
    from console.services import thesis_book as tb
    repo = tmp_path / "bubble-ops-fixture"
    today = datetime.date.today().isoformat()
    outdir = repo / "outputs" / today
    outdir.mkdir(parents=True)
    (outdir / "graph-data.json").write_text(json.dumps({
        "nav": 266108.36,
        "portfolio_overview": {"nav": 266108.36, "since_rebase_pct": 7.03,
                               "nav_date": today},
    }), encoding="utf-8")
    monkeypatch.setattr(tb, "_cached", lambda s: None)
    monkeypatch.setattr(tb, "runtime_repo_path", lambda s: repo)
    monkeypatch.setattr(tb, "_run_vault_to_graph", lambda root: {})
    # simulate the UNPUSHED / stale DB → zero NAV overview
    monkeypatch.setattr(tb, "_build_portfolio_overview", lambda db, root: {"nav": 0})
    data = tb.build_thesis_data("fixture")
    po = data.get("portfolio_overview") or {}
    assert float(po.get("nav") or 0) == 266108.36, (
        f"Thesis header must use the pushed graph-data NAV, not the $0 DB NAV; got {po.get('nav')}")
    assert float(data.get("nav") or 0) == 266108.36


def test_thesis_header_uses_db_when_pushed_absent(tmp_path, monkeypatch):
    """No pushed graph-data → fall back to the DB overview (behavior preserved)."""
    from console.services import thesis_book as tb
    repo = tmp_path / "bubble-ops-fixture"
    (repo / "outputs").mkdir(parents=True)
    monkeypatch.setattr(tb, "_cached", lambda s: None)
    monkeypatch.setattr(tb, "runtime_repo_path", lambda s: repo)
    monkeypatch.setattr(tb, "_run_vault_to_graph", lambda root: {})
    monkeypatch.setattr(tb, "_build_portfolio_overview", lambda db, root: {"nav": 266108.36})
    data = tb.build_thesis_data("fixture")
    assert float((data.get("portfolio_overview") or {}).get("nav") or 0) == 266108.36
