"""
test_727_value_chains.py — value-chain sector maps on /dept/<slug>
(board #727, Part 2 of #725).

Mirrors test_364_risk_cluster_table.py's fixture style: a temp READ_FROM_DISK
root shaped like bubble-ops-<slug>/vault/value-chains/. Covers the service
module directly (present + absent vault subdir, empty dir, malformed/frontmatter
files, escaping) and the /dept/<slug> route rendering the section.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from console import settings
from console.services import value_chains


@pytest.fixture
def disk_root(monkeypatch, tmp_path):
    """Point the services' disk-mode reader at a temp root (mirrors
    test_364_risk_cluster_table.py's fixture — settings.READ_FROM_DISK is
    captured at import time, so the module attribute must be patched)."""
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(tmp_path))
    return tmp_path


def _build_repo(root: Path, slug: str = "ben") -> Path:
    repo = root / f"bubble-ops-{slug}"
    repo.mkdir(parents=True)
    return repo


# ─── Service-level tests ────────────────────────────────────────────────

def test_unavailable_when_no_repo(disk_root):
    data = value_chains.load_value_chains("nonexistent")
    assert data.available is False
    assert data.sectors == []
    assert data.overview_html is None


def test_unavailable_when_no_vault_dir(disk_root):
    _build_repo(disk_root, "ben")
    data = value_chains.load_value_chains("ben")
    assert data.available is False


def test_unavailable_when_value_chains_dir_missing(disk_root):
    repo = _build_repo(disk_root, "ben")
    (repo / "vault").mkdir()
    data = value_chains.load_value_chains("ben")
    assert data.available is False


def test_unavailable_when_value_chains_dir_empty(disk_root):
    repo = _build_repo(disk_root, "ben")
    (repo / "vault" / "value-chains").mkdir(parents=True)
    data = value_chains.load_value_chains("ben")
    assert data.available is False


def test_loads_index_and_sectors(disk_root):
    repo = _build_repo(disk_root, "ben")
    vc = repo / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "_index.md").write_text(
        "# Value chains — overview\n\nTags: own, watch, early, ran, private.\n",
        encoding="utf-8",
    )
    (vc / "technology.md").write_text(
        "# Technology\n\n- AAPL — own\n- MSFT — watch\n",
        encoding="utf-8",
    )
    (vc / "energy.md").write_text(
        "---\ntitle: Energy value chain\n---\n\n# Energy\n\n- XOM — ran\n",
        encoding="utf-8",
    )
    data = value_chains.load_value_chains("ben")
    assert data.available is True
    assert data.overview_html is not None
    assert "Value chains" in str(data.overview_html)
    assert data.sector_count == 2
    titles = sorted(s.title for s in data.sectors)
    assert titles == ["Energy", "Technology"]
    # frontmatter is stripped, not rendered as stray markdown
    energy = next(s for s in data.sectors if s.slug == "energy")
    assert "title: Energy value chain" not in str(energy.html)


def test_index_only_file_ignored_as_sector(disk_root):
    """_index.md must never appear in the sectors list itself."""
    repo = _build_repo(disk_root, "ben")
    vc = repo / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "_index.md").write_text("# Overview\n", encoding="utf-8")
    data = value_chains.load_value_chains("ben")
    assert data.available is True
    assert data.sectors == []
    assert all(s.slug != "_index" for s in data.sectors)


def test_sector_without_h1_falls_back_to_filename(disk_root):
    repo = _build_repo(disk_root, "ben")
    vc = repo / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "consumer-staples.md").write_text("- WMT — own\n", encoding="utf-8")
    data = value_chains.load_value_chains("ben")
    assert data.available is True
    assert data.sectors[0].title == "Consumer Staples"


def test_untrusted_content_is_sanitized(disk_root):
    """Vault markdown is agent/human-authored but rendered via the SAME
    sanitizer as the whiteboard — a <script> tag must never survive."""
    repo = _build_repo(disk_root, "ben")
    vc = repo / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "tech.md").write_text(
        "# Tech\n\n<script>alert('xss')</script>\n\nAAPL — own\n",
        encoding="utf-8",
    )
    data = value_chains.load_value_chains("ben")
    assert "<script>" not in str(data.sectors[0].html)


def test_tag_legend_present_and_matches_the_nine_known_tags(disk_root):
    """Legend must cover the FULL union of tags used across Ben's live
    per-sector deep-dive files, not just the 5 named in _index.md's summary
    (#727 review finding — financials/industrials/information-technology use
    `excluded-own` as an 8th tag; energy.md uses `wrong-sector` instead)."""
    repo = _build_repo(disk_root, "ben")
    vc = repo / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "_index.md").write_text("# Overview\n", encoding="utf-8")
    data = value_chains.load_value_chains("ben")
    codes = {t["code"] for t in data.tag_legend}
    assert codes == {
        "own", "watch", "early", "ran", "private",
        "broken", "arb", "excluded-own", "wrong-sector",
    }


# ─── Route-level tests (/dept/<slug>) ──────────────────────────────────

def test_dept_detail_omits_section_when_no_value_chains(client):
    """The 'fixture' dept (conftest.fixture_root) has no vault/ at all — the
    section must not render, and the page must still 200 (graceful, not a
    crash on the missing dir)."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "dept-value-chains-heading" not in r.text


def test_dept_detail_renders_compact_value_chains_summary(client, fixture_root):
    """Card #1065: the FULL value-chain render (overview, sector bodies, tag
    legend) moved off /dept/<slug> — the main page keeps only a compact
    one-line summary + link. Card #1079: that link now points at the
    /dept/<slug>/portfolio 3rd tab (Ben's live Value-Chain Maps panel, #1078)
    since the #1065 dedicated /dept/<slug>/value-chains sub-page (a
    console-rendered snapshot of the same vault data) was redundant with it
    and has been removed. Writing vault/value-chains/ under the 'fixture'
    dept's on-disk repo (same READ_FROM_DISK root the app/client fixtures
    already point at) must surface that compact summary, but NOT the sector
    bodies or legend."""
    vc = fixture_root / "bubble-ops-fixture" / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "_index.md").write_text(
        "# Value chains — overview\n\nEleven GICS sectors.\n", encoding="utf-8",
    )
    (vc / "technology.md").write_text(
        "# Technology\n\n- AAPL — own\n- MSFT — watch\n", encoding="utf-8",
    )

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    # section heading + compact summary + link to the portfolio tab are present
    assert "dept-value-chains-heading" in body
    assert "/dept/fixture/portfolio" in body
    assert "/dept/fixture/value-chains" not in body
    assert "1 carte GICS" in body
    # the FULL render (sector title/body, overview text, tag legend) must NOT
    # ship inline on the main page any more (that's the whole point of #1065)
    assert "Technology" not in body
    assert "Eleven GICS sectors" not in body
    for label in ("Own", "Watch", "Early", "Ran", "Private"):
        assert label not in body


# ─── Route-level tests (/dept/<slug>/value-chains) — card #1065 ────────
# The dedicated sub-page route was removed by card #1079: it was a
# console-rendered snapshot of the same vault/value-chains/ data that had
# become redundant with Ben's live Value-Chain Maps panel embedded as the
# /dept/<slug>/portfolio 3rd tab (#1078). See test_1078_* / thesis_book tests
# for coverage of that panel, and the 404-on-removed-route check below.

def test_value_chains_subpage_route_removed(client):
    """The old /dept/<slug>/value-chains sub-page (#1065) is gone — the
    value-chain view now lives solely at /dept/<slug>/portfolio's 3rd tab
    (#1078). This must 404 (route removed), not resolve to stale content."""
    r = client.get("/dept/fixture/value-chains")
    assert r.status_code == 404
