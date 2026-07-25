"""
test_364_risk_cluster_table.py — cluster rollup + bet drill-down on
/dept/<slug> (board #364, PR-B — option (c): rollup that expands to bets).

Rick's research on #364 found vault/investment-cases/*.md frontmatter is NOT
uniform across the live ben repo: some files carry `ticker: XXX` (singular),
some `tickers: [A, B]` (list), some `positions_affected: [A, B]` (list). This
suite covers all three, plus the empty-state paths (no clusters at all, and
a cluster whose members have no matching case file), plus escaping of
vault-sourced text.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from console import settings
from console.services import risk_clusters


@pytest.fixture
def disk_root(monkeypatch, tmp_path):
    """Point the services' disk-mode reader at a temp root (mirrors
    test_nav_chart.py's fixture — settings.READ_FROM_DISK is captured at
    import time, so the module attribute must be patched)."""
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(tmp_path))
    return tmp_path


def _build_repo(root: Path, slug: str = "ben") -> Path:
    repo = root / f"bubble-ops-{slug}"
    (repo / "vault" / "clusters").mkdir(parents=True)
    (repo / "vault" / "investment-cases").mkdir(parents=True)
    return repo


def _write_cluster(
    repo: Path, cluster_id: str, name: str, members: list[str],
    nav_weight_pct: float, mv_usd: float,
) -> None:
    fm = {
        "title": f"Cluster — {name}",
        "type": "cluster_analysis",
        "cluster_id": cluster_id,
        "members": members,
        "nav_weight_pct": nav_weight_pct,
        "mv_usd": mv_usd,
    }
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# Cluster — " + name + "\n\nNarrative.\n"
    (repo / "vault" / "clusters" / f"{cluster_id}.md").write_text(text, encoding="utf-8")


def _write_summary(repo: Path) -> None:
    """_summary.md must never be treated as a cluster memo."""
    (repo / "vault" / "clusters" / "_summary.md").write_text(
        "---\ntitle: Cluster Analysis — Cross-Cluster Summary\ntype: cluster_summary\n---\n\n# Summary\n",
        encoding="utf-8",
    )
    (repo / "vault" / "clusters" / "_config.yaml").write_text("clusters: {}\n", encoding="utf-8")


def _write_case_singular_ticker(repo: Path, fname: str, ticker: str, h1: str,
                                 status: str = "watch", conviction: str = "2/5") -> None:
    fm = {"ticker": ticker, "status": status, "conviction": conviction, "sleeve": "§3a single-stock"}
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# " + h1 + "\n\nBody.\n"
    (repo / "vault" / "investment-cases" / fname).write_text(text, encoding="utf-8")


def _write_case_tickers_list(repo: Path, fname: str, tickers: list[str], h1: str,
                              status: str = "active") -> None:
    fm = {"kind": "investment_case", "status": status, "tickers": tickers}
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# " + h1 + "\n\nBody.\n"
    (repo / "vault" / "investment-cases" / fname).write_text(text, encoding="utf-8")


def _write_case_positions_affected(repo: Path, fname: str, positions: list[str], h1: str,
                                    status: str = "closed") -> None:
    fm = {"type": "investment-case", "status": status, "positions_affected": positions}
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# " + h1 + "\n\nBody.\n"
    (repo / "vault" / "investment-cases" / fname).write_text(text, encoding="utf-8")


# ─── Service-level tests ────────────────────────────────────────────────

def test_empty_when_no_repo(disk_root):
    table = risk_clusters.load_risk_clusters("nonexistent")
    assert table.clusters == []
    assert not table.has_clusters


def test_empty_when_no_vault_dir(disk_root):
    repo = disk_root / "bubble-ops-ben"
    repo.mkdir()
    table = risk_clusters.load_risk_clusters("ben")
    assert not table.has_clusters


def test_empty_when_no_cluster_memos(disk_root):
    repo = _build_repo(disk_root)
    _write_summary(repo)  # only summary/config, no real cluster memo
    table = risk_clusters.load_risk_clusters("ben")
    assert not table.has_clusters


def test_summary_and_config_are_skipped_as_cluster_rows(disk_root):
    repo = _build_repo(disk_root)
    _write_summary(repo)
    _write_cluster(repo, "us_tech", "US Tech Concentration Pack", ["QQQ", "GOOGL"], 5.58, 12775.29)
    table = risk_clusters.load_risk_clusters("ben")
    ids = [c.cluster_id for c in table.clusters]
    assert "us_tech" in ids
    assert "_summary" not in ids
    assert len(table.clusters) == 1


def test_cluster_parses_frontmatter_fields(disk_root):
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH", "ROBO", "URA"], 3.12, 7150.0)
    table = risk_clusters.load_risk_clusters("ben")
    assert table.has_clusters
    c = table.clusters[0]
    assert c.cluster_id == "ai_capex"
    assert c.name == "AI-Capex Cluster"
    assert c.members == ["SMH", "ROBO", "URA"]
    assert c.weight_display == "3.12%"
    assert c.mv_display == "$7,150"


def test_bets_matched_via_singular_ticker_field(disk_root):
    """`ticker: XXX` (singular string) — e.g. 005380.KS.md, *-idea-*.md."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "single_stock", "Single Stock", ["SU.PA"], 1.0, 1000.0)
    _write_case_singular_ticker(
        repo, "idea-schneider.md", "SU.PA",
        "SU.PA (Schneider Electric) — quality industrial compounder",
    )
    table = risk_clusters.load_risk_clusters("ben")
    c = table.clusters[0]
    assert c.bet_count == 1
    assert c.bets[0].ticker == "SU.PA"
    assert "Schneider Electric" in c.bets[0].thesis
    assert c.bets[0].conviction == "2/5"


def test_bets_matched_via_tickers_list_field(disk_root):
    """`tickers: [A, B, C]` — most dated investment-case memos."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH", "URA", "ROBO"], 3.12, 7150.0)
    _write_case_tickers_list(
        repo, "2026-06-06-monday-bounce-trim.md", ["SMH", "URA", "ROBO"],
        "Monday-Bounce Trim — AI-Capex Cluster Conditional De-risk",
    )
    table = risk_clusters.load_risk_clusters("ben")
    c = table.clusters[0]
    # one case file referencing 3 members -> 3 bets (one per member ticker)
    assert c.bet_count == 3
    tickers = {b.ticker for b in c.bets}
    assert tickers == {"SMH", "URA", "ROBO"}


def test_bets_matched_via_positions_affected_field(disk_root):
    """`positions_affected: [A, B]` — the third convention in active use."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "commodities", "Commodity / Inflation Complex", ["PDBC", "IAUM"], 3.35, 8000.0)
    _write_case_positions_affected(
        repo, "2026-06-17-commodities-gold-regime-refresh.md", ["PDBC", "IAUM"],
        "Commodities/Gold Regime Refresh",
    )
    table = risk_clusters.load_risk_clusters("ben")
    c = table.clusters[0]
    assert c.bet_count == 2
    assert {b.ticker for b in c.bets} == {"PDBC", "IAUM"}


def test_cluster_with_no_matching_cases_has_empty_bets_not_crash(disk_root):
    repo = _build_repo(disk_root)
    _write_cluster(repo, "defense", "Defense Thematic Cluster", ["ITA"], 0.0, 0.0)
    # No investment-case file for ITA at all.
    table = risk_clusters.load_risk_clusters("ben")
    assert table.has_clusters
    c = table.clusters[0]
    assert c.bet_count == 0
    assert c.unmatched_members == ["ITA"]


def test_clusters_sorted_by_weight_descending(disk_root):
    repo = _build_repo(disk_root)
    _write_cluster(repo, "small", "Small Cluster", ["A"], 1.0, 100.0)
    _write_cluster(repo, "big", "Big Cluster", ["B"], 9.0, 900.0)
    table = risk_clusters.load_risk_clusters("ben")
    assert [c.cluster_id for c in table.clusters] == ["big", "small"]


def test_case_with_no_ticker_fields_is_ignored(disk_root):
    """A process_note with `tickers: []` (or no ticker field at all) must not
    crash the parser and must not attach to any cluster."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH"], 3.12, 7150.0)
    fm = {"kind": "process_note", "status": "closed", "tickers": []}
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# Process note\n\nBody.\n"
    (repo / "vault" / "investment-cases" / "process-note.md").write_text(text, encoding="utf-8")
    table = risk_clusters.load_risk_clusters("ben")
    c = table.clusters[0]
    assert c.bet_count == 0


def test_malformed_frontmatter_does_not_crash(disk_root):
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH"], 3.12, 7150.0)
    (repo / "vault" / "investment-cases" / "broken.md").write_text(
        "---\nticker: [unterminated\n---\nbody\n", encoding="utf-8",
    )
    table = risk_clusters.load_risk_clusters("ben")
    assert table.has_clusters  # doesn't crash the whole page


# ─── Route-level tests ───────────────────────────────────────────────────

def test_route_renders_200_with_cluster_table(client, fixture_root):
    """/dept/fixture renders 200 with the cluster table markup when the
    fixture dept has a vault/clusters/ dir. `fixture_root` (conftest.py) is
    the SAME on-disk root the `app`/`client` fixtures point READ_FROM_DISK
    at, and it already creates bubble-ops-fixture/ — we just add vault/
    files into it, same access pattern as every other service."""
    repo = fixture_root / "bubble-ops-fixture"
    (repo / "vault" / "clusters").mkdir(parents=True)
    (repo / "vault" / "investment-cases").mkdir(parents=True)
    _write_cluster_into(repo, "ai_capex", "AI-Capex Cluster", ["SMH"], 3.12, 7150.0)
    _write_case_into(repo, "case1.md", ["SMH"], "SMH — AI-capex core holding")

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    assert "Clusters de risque" in body
    assert "AI-Capex Cluster" in body
    assert "SMH" in body


def test_route_empty_state_no_clusters(client, fixture_root):
    """The fixture dept has no vault/clusters/ dir at all (conftest.py's
    fixture_root doesn't create one) — must render cleanly (no crash, no
    visible empty shell) since the section only renders when has_clusters."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    # No crash; the risk-cluster-card section must not appear at all.
    assert "risk-cluster-card" not in r.text


def test_route_cluster_with_no_bets_shows_graceful_empty(client, fixture_root):
    repo = fixture_root / "bubble-ops-fixture"
    (repo / "vault" / "clusters").mkdir(parents=True)
    (repo / "vault" / "investment-cases").mkdir(parents=True)
    _write_cluster_into(repo, "defense", "Defense Thematic Cluster", ["ITA"], 0.5, 500.0)
    # no matching case file for ITA

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "Defense Thematic Cluster" in r.text
    assert "Aucun dossier de thèse" in r.text


def test_route_escapes_vault_sourced_text(client, fixture_root):
    """A cluster name / thesis containing HTML must render escaped, never
    raw — Jinja2 autoescape, no |safe on any vault-sourced field."""
    repo = fixture_root / "bubble-ops-fixture"
    (repo / "vault" / "clusters").mkdir(parents=True)
    (repo / "vault" / "investment-cases").mkdir(parents=True)
    _write_cluster_into(repo, "xss_cluster", "<script>alert(1)</script>", ["ZZZ"], 1.0, 100.0)
    _write_case_into(repo, "xss_case.md", ["ZZZ"], "<img src=x onerror=alert(2)>")

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "<img src=x onerror=alert(2)>" not in r.text
    # escaped form must be present somewhere (proves the text rendered, not silently dropped)
    assert "&lt;script&gt;" in r.text or "&lt;img" in r.text


# ─── local helpers reused by the route tests (avoid name clash with the
#     service-level _write_cluster/_write_case_* which build a *-prefixed
#     "ben" slug dir; these write straight into a caller-supplied repo) ────

def _write_cluster_into(repo: Path, cluster_id: str, name: str, members: list[str],
                         nav_weight_pct: float, mv_usd: float) -> None:
    fm = {
        "title": f"Cluster — {name}",
        "cluster_id": cluster_id,
        "members": members,
        "nav_weight_pct": nav_weight_pct,
        "mv_usd": mv_usd,
    }
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# Cluster — " + name + "\n\nNarrative.\n"
    (repo / "vault" / "clusters" / f"{cluster_id}.md").write_text(text, encoding="utf-8")


def _write_case_into(repo: Path, fname: str, tickers: list[str], h1: str) -> None:
    fm = {"tickers": tickers, "status": "active", "conviction": "3/5"}
    text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n\n# " + h1 + "\n\nBody.\n"
    (repo / "vault" / "investment-cases" / fname).write_text(text, encoding="utf-8")
