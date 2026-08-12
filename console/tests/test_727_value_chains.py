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


def test_dept_detail_renders_value_chains_section(client, fixture_root):
    """Writing vault/value-chains/ under the 'fixture' dept's on-disk repo
    (same READ_FROM_DISK root the app/client fixtures already point at) must
    surface the overview + sector maps + tag legend on /dept/fixture."""
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
    assert "dept-value-chains-heading" in body
    assert "Technology" in body
    assert "Value chains" in body
    # tag legend labels
    for label in ("Own", "Watch", "Early", "Ran", "Private"):
        assert label in body
