"""#1209 — every cockpit NAV headline must read the ONE canonical, audited,
frozen morning mark from the PUSHED graph-data.json (mirrors the latest verified
kpi_snapshots row), NOT the live un-audited consolidated_nav that drifts on every
refresh. Covers the resolver + the Dashboard and Risk-KPIs (management-view)
headlines.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path


def _write_graph_data(repo: Path, day: str, payload: dict) -> None:
    outdir = repo / "outputs" / day
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "graph-data.json").write_text(json.dumps(payload), encoding="utf-8")


# ── resolver ────────────────────────────────────────────────────────────────
def test_canonical_nav_reads_pushed_graph_data(tmp_path, monkeypatch):
    from console.services import canonical_nav as cn
    repo = tmp_path / "bubble-ops-fixture"
    today = datetime.date.today().isoformat()
    _write_graph_data(repo, today, {
        "nav": 266108.36,
        "since_rebase_pct": 7.03,
        "generated_at": today,
        "portfolio_overview": {"nav": 266108.36, "since_rebase_pct": 7.03,
                               "nav_date": today},
    })
    monkeypatch.setattr(cn, "runtime_repo_path", lambda s: repo)
    out = cn.canonical_nav("fixture")
    assert out["nav"] == 266108.36
    assert out["since_rebase_pct"] == 7.03
    assert out["as_of"] == today
    assert out["is_stale"] is False
    assert out["source"] == "graph-data.json"


def test_canonical_nav_falls_back_to_portfolio_overview_fields(tmp_path, monkeypatch):
    """Top-level nav/since_rebase absent → read them from portfolio_overview."""
    from console.services import canonical_nav as cn
    repo = tmp_path / "bubble-ops-fixture"
    today = datetime.date.today().isoformat()
    _write_graph_data(repo, today, {
        "portfolio_overview": {"nav": 123456.0, "since_rebase_pct": -2.5,
                               "nav_date": today},
    })
    monkeypatch.setattr(cn, "runtime_repo_path", lambda s: repo)
    out = cn.canonical_nav("fixture")
    assert out["nav"] == 123456.0
    assert out["since_rebase_pct"] == -2.5
    assert out["as_of"] == today


def test_canonical_nav_is_stale_when_as_of_before_today(tmp_path, monkeypatch):
    from console.services import canonical_nav as cn
    repo = tmp_path / "bubble-ops-fixture"
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    # File lives under today's dir (so the 7-day scan finds it), but the audited
    # mark inside is yesterday's → stale.
    _write_graph_data(repo, today, {
        "nav": 266108.36, "since_rebase_pct": 7.03,
        "portfolio_overview": {"nav": 266108.36, "since_rebase_pct": 7.03,
                               "nav_date": yesterday},
    })
    monkeypatch.setattr(cn, "runtime_repo_path", lambda s: repo)
    out = cn.canonical_nav("fixture")
    assert out["as_of"] == yesterday
    assert out["is_stale"] is True


def test_canonical_nav_not_stale_when_as_of_today(tmp_path, monkeypatch):
    from console.services import canonical_nav as cn
    repo = tmp_path / "bubble-ops-fixture"
    today = datetime.date.today().isoformat()
    _write_graph_data(repo, today, {
        "nav": 1.0, "portfolio_overview": {"nav": 1.0, "nav_date": today}})
    monkeypatch.setattr(cn, "runtime_repo_path", lambda s: repo)
    assert cn.canonical_nav("fixture")["is_stale"] is False


def test_canonical_nav_graceful_when_no_graph_data(tmp_path, monkeypatch):
    """Repo exists but no outputs/ → all-None empty result, never raises."""
    from console.services import canonical_nav as cn
    repo = tmp_path / "bubble-ops-fixture"
    repo.mkdir(parents=True)
    monkeypatch.setattr(cn, "runtime_repo_path", lambda s: repo)
    out = cn.canonical_nav("fixture")
    assert out == {"nav": None, "since_rebase_pct": None, "as_of": None,
                   "is_stale": False, "source": "graph-data.json"}


def test_canonical_nav_graceful_when_no_repo(monkeypatch):
    from console.services import canonical_nav as cn
    monkeypatch.setattr(cn, "runtime_repo_path", lambda s: None)
    out = cn.canonical_nav("nope")
    assert out["nav"] is None and out["is_stale"] is False


# ── Dashboard route render ───────────────────────────────────────────────────
def test_dashboard_headline_uses_graph_data_nav_not_consolidated(client, monkeypatch):
    """The Dashboard (/dept/<slug>) headline NAV must equal the pushed
    graph-data.json nav, and NOT surface the live consolidated_nav_usd as the
    big number."""
    from console.routes import dept as dept_route
    canonical = {"nav": 266108.36, "since_rebase_pct": 7.03,
                 "as_of": datetime.date.today().isoformat(), "is_stale": False,
                 "source": "graph-data.json"}
    monkeypatch.setattr(dept_route.canonical_nav_service, "canonical_nav",
                        lambda slug: canonical)
    # A whiteboard carrying the live consolidated_nav_usd as a KPI tile — it must
    # NOT become the headline.
    monkeypatch.setattr(dept_route.github_reader, "load_whiteboard",
                        lambda slug: {"title": "Tableau", "updated_at": "x",
                                      "kpis": [{"label": "NAV live",
                                                "value": "$999,999"}]})
    resp = client.get("/dept/fixture")
    assert resp.status_code == 200
    html = resp.text
    # The audited headline number is present…
    assert "266,108" in html
    # …inside the canonical nav-headline block.
    assert "nav-headline" in html


def test_dashboard_headline_shows_stale_banner(client, monkeypatch):
    from console.routes import dept as dept_route
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    monkeypatch.setattr(dept_route.canonical_nav_service, "canonical_nav",
                        lambda slug: {"nav": 100.0, "since_rebase_pct": 1.0,
                                      "as_of": yesterday, "is_stale": True,
                                      "source": "graph-data.json"})
    resp = client.get("/dept/fixture")
    assert resp.status_code == 200
    assert "pf-stale" in resp.text
    assert yesterday in resp.text


def test_dashboard_no_headline_when_no_graph_data(client, monkeypatch):
    """A dept with no graph-data.json → resolver empty → no nav-headline block
    (page unchanged for non-fund depts)."""
    from console.routes import dept as dept_route
    monkeypatch.setattr(dept_route.canonical_nav_service, "canonical_nav",
                        lambda slug: {"nav": None, "since_rebase_pct": None,
                                      "as_of": None, "is_stale": False,
                                      "source": "graph-data.json"})
    resp = client.get("/dept/fixture")
    assert resp.status_code == 200
    assert 'class="nav-headline"' not in resp.text


# ── Risk-KPIs / management-view route render ────────────────────────────────
def test_management_view_headline_uses_graph_data_nav(client, monkeypatch):
    """The management view's per-child NAV headline must equal the pushed
    graph-data.json nav, and the stale consolidated_nav_usd_true in risk_kpis is
    relabelled as the live tick, not the headline."""
    from console.routes import dept as dept_route
    monkeypatch.setattr(dept_route.canonical_nav_service, "canonical_nav",
                        lambda slug: {"nav": 266108.36, "since_rebase_pct": 7.03,
                                      "as_of": datetime.date.today().isoformat(),
                                      "is_stale": False,
                                      "source": "graph-data.json"})
    monkeypatch.setattr(dept_route.github_reader, "load_dept_yaml",
                        lambda slug: {"hierarchy": {"level": "management"}})
    monkeypatch.setattr(dept_route.github_reader, "load_management_exports",
                        lambda slug: {"children": [{
                            "slug": "ben", "staleness_days": 0,
                            "pending_gates": [],
                            "risk_kpis": {"consolidated_nav_usd_true": 999999,
                                          "cash_pct": 12.0}}],
                            "total_open_gates": 0, "stale_children": []})
    resp = client.get("/dept/fixture/management-view")
    assert resp.status_code == 200
    html = resp.text
    assert "266,108" in html                 # audited headline present
    assert "nav-headline" in html
    assert "nav-live-tick" in html           # consolidated_* relabelled
    assert "cash_pct" in html                # other risk KPIs preserved
