"""
test_727_value_chains.py — value-chain sector-map COUNT on /dept/<slug>
(board #727, Part 2 of #725; trimmed to count-only by #1082).

Mirrors test_364_risk_cluster_table.py's fixture style: a temp READ_FROM_DISK
root shaped like bubble-ops-<slug>/vault/value-chains/. Covers the service
module directly (present + absent vault subdir, empty dir, counting) and the
/dept/<slug> route rendering the compact summary.

Card #1082: the FULL render this module used to do (overview HTML, per-sector
HTML, tag legend — added for #727, exposed on a dedicated sub-page by #1065)
lost its last consumer when #1079 removed that sub-page in favor of Ben's live
Value-Chain Maps panel (#1078, served by
console.routes.thesis_book._latest_maps_html from a pre-rendered
outputs/<date>/value-chain-maps-panel.html — a wholly separate code path).
The only thing left reading this module was /dept/<slug>'s compact one-line
summary, which only ever used `.available` / `.sector_count` — so
load_value_chains() was trimmed to compute exactly that, cheaply (a directory
listing, no markdown parsing/sanitizing of ~11 sector maps per page load).
The tests that used to cover the dropped rendering (overview_html content,
per-sector HTML, frontmatter stripping, H1/title extraction, XSS sanitization,
the tag legend) are gone with the feature they covered; see git history on
this file (pre-#1082) if that rendering is ever revived.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from console import settings
from console.services import markdown_render, value_chains


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
    assert data.sector_count == 0


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


def test_unavailable_when_only_empty_index(disk_root):
    """An _index.md that exists but is empty still counts as "nothing here"
    (mirrors the pre-#1082 behavior of only an overview with content making
    the section available) — cheap stat-based check, not a content read."""
    repo = _build_repo(disk_root, "ben")
    vc = repo / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "_index.md").write_text("", encoding="utf-8")
    data = value_chains.load_value_chains("ben")
    assert data.available is False


def test_loads_available_with_sector_count(disk_root):
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
    assert data.sector_count == 2


def test_index_only_dir_is_available_with_zero_sectors(disk_root):
    """_index.md alone (no per-sector maps yet) is "available" with count 0
    — an overview page, no sector cards. Also confirms _index.md itself is
    never counted as a sector."""
    repo = _build_repo(disk_root, "ben")
    vc = repo / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "_index.md").write_text("# Overview\n", encoding="utf-8")
    data = value_chains.load_value_chains("ben")
    assert data.available is True
    assert data.sector_count == 0


def test_dotfiles_and_underscore_files_not_counted_as_sectors(disk_root):
    repo = _build_repo(disk_root, "ben")
    vc = repo / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "technology.md").write_text("- AAPL — own\n", encoding="utf-8")
    (vc / ".hidden.md").write_text("noise\n", encoding="utf-8")
    (vc / "_draft.md").write_text("noise\n", encoding="utf-8")
    (vc / "readme.txt").write_text("not markdown\n", encoding="utf-8")
    data = value_chains.load_value_chains("ben")
    assert data.available is True
    assert data.sector_count == 1


def test_load_value_chains_never_reads_file_contents(disk_root, monkeypatch):
    """#1082: the count-only path must not open/parse sector or index files —
    only stat the directory listing. A file whose content would blow up any
    reader (binary garbage under a .md name) must not raise."""
    repo = _build_repo(disk_root, "ben")
    vc = repo / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "technology.md").write_bytes(b"\xff\xfe\x00\xff not valid utf-8")
    (vc / "_index.md").write_text("# Overview\n", encoding="utf-8")
    data = value_chains.load_value_chains("ben")
    assert data.available is True
    assert data.sector_count == 1


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
    /dept/<slug>/portfolio#maps 3rd tab (Ben's live Value-Chain Maps panel,
    #1078) since the #1065 dedicated /dept/<slug>/value-chains sub-page (a
    console-rendered snapshot of the same vault data) was redundant with it
    and has been removed."""
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
    # section heading + compact summary + deep link to the maps tab present
    assert "dept-value-chains-heading" in body
    assert "/dept/fixture/portfolio#maps" in body
    assert "/dept/fixture/value-chains" not in body
    assert "1 carte GICS" in body
    # the FULL render (sector title/body, overview text) must NOT ship
    # inline on the main page any more (that's the whole point of #1065)
    assert "Technology" not in body
    assert "Eleven GICS sectors" not in body


def test_main_dept_page_never_invokes_markdown_render(client, fixture_root, monkeypatch):
    """Card #1082: /dept/<slug>'s compact summary must not parse any of the
    ~11 sector maps to HTML — only count them. Sabotage the sanitizing
    markdown renderer used to build overview/sector HTML pre-#1082: if the
    main-page request path still touched it, this would blow up the request
    instead of quietly rendering the compact count."""
    def _boom(*_args, **_kwargs):
        raise AssertionError(
            "load_value_chains must not render markdown on the main /dept "
            "page path (card #1082) — the compact summary only needs "
            "available + sector_count."
        )

    monkeypatch.setattr(markdown_render, "render_markdown_safe", _boom)

    vc = fixture_root / "bubble-ops-fixture" / "vault" / "value-chains"
    vc.mkdir(parents=True)
    (vc / "_index.md").write_text("# Overview\n", encoding="utf-8")
    for sector in (
        "communication-services", "consumer-discretionary", "consumer-staples",
        "energy", "financials", "health-care", "industrials",
        "information-technology", "materials", "real-estate", "utilities",
    ):
        (vc / f"{sector}.md").write_text(f"# {sector}\n\n- TICK — own\n", encoding="utf-8")

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "11 carte" in r.text


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


def test_portfolio_page_has_maps_hash_deep_link_handler(client):
    """Card #1079 follow-up: /dept/<slug>/portfolio's tab bar does not honour
    a URL hash by default (the pf-tab JS just defaults to Thesis Book on
    load), which would make the main page's new '/portfolio#maps' link land
    on the wrong tab — undermining the "ONE entry point" this consolidation
    is for. thesis_book.html's tab-nav IIFE must read location.hash on load
    and open the matching pane (id="pane-<hash>") via the same show() used by
    the tab click handlers. This is a client-side behavior a server-rendered
    TestClient response can't execute, so this test only asserts the handler
    is present in the shipped markup — not that a specific hash resolves in
    a browser. Unconditional (not gated on Ben's maps_html), so it renders
    for every dept regardless of whether the 3rd tab itself is present."""
    r = client.get("/dept/fixture/portfolio")
    assert r.status_code == 200
    body = r.text
    assert 'location.hash' in body
    assert 'document.getElementById("pane-"+h)' in body
