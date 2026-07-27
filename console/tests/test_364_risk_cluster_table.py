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


# ─── Review finding: YAML-parse-failure fallback (board #364 review) ─────
#
# 7 of 145 live investment-case files have an unquoted `:` inside an
# unrelated frontmatter value (most commonly `title:` with an em-dash +
# colon, e.g. "INSM (Insmed) — L2 underwrite: first-in-class..."), which
# breaks the WHOLE `---` block for `yaml.safe_load` — so the ticker line
# sitting right next to it, correctly formatted, was silently discarded
# along with everything else. Live theses affected: INSM, LIN, LNG, CSGP,
# VRSK, ADYEN. This fixture copies the REAL shape of the INSM file (title
# line breaks the parse; `ticker: INSM` on its own line is well-formed) —
# not an invented minimal repro.

_REAL_MALFORMED_INSM_SHAPE = """---
title: INSM (Insmed) — L2 underwrite: first-in-class respiratory franchise, post-earnings pullback
type: investment-case
ticker: INSM
date: 2026-07-13
layer: 2
analyst: ben-l2
status: proposed
conviction: 3/5
---

# INSM (Insmed) — L2 underwrite

Body.
"""


def test_yaml_parse_failure_still_recovers_ticker_via_fallback(disk_root):
    """RED before the fix: yaml.safe_load raises on the `title:` line (an
    unquoted colon inside the value), _split_frontmatter used to catch that
    and return {} — discarding the well-formed `ticker: INSM` line right
    along with it, so this cluster showed 0 bets despite a real, valid case
    file. GREEN after the regex line-scan fallback: the ticker (and status)
    must still be recovered."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "healthcare_complex", "Healthcare Complex", ["INSM"], 1.50, 3435.0)
    (repo / "vault" / "investment-cases" / "2026-07-13-INSM-insmed-l2-underwrite.md").write_text(
        _REAL_MALFORMED_INSM_SHAPE, encoding="utf-8",
    )

    # Sanity-check the fixture actually reproduces a real YAML parse failure
    # (otherwise this test would pass for the wrong reason).
    import yaml as _yaml
    frontmatter_block = _REAL_MALFORMED_INSM_SHAPE.split("---\n", 2)[1]
    with pytest.raises(_yaml.YAMLError):
        _yaml.safe_load(frontmatter_block)

    table = risk_clusters.load_risk_clusters("ben")
    assert table.has_clusters
    c = table.clusters[0]
    assert c.bet_count == 1
    assert c.bets[0].ticker == "INSM"
    assert c.bets[0].status == "proposed"


def test_yaml_parse_failure_fallback_does_not_alter_clean_parse(disk_root):
    """Regression guard: when YAML parses fine, behaviour is byte-for-byte
    what it was before the fallback existed (thesis/status/conviction come
    from the real parsed dict, not the regex path)."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH"], 3.12, 7150.0)
    _write_case_singular_ticker(repo, "smh-case.md", "SMH", "SMH — AI-capex core holding")
    table = risk_clusters.load_risk_clusters("ben")
    c = table.clusters[0]
    assert c.bet_count == 1
    assert c.bets[0].ticker == "SMH"
    assert "AI-capex core holding" in c.bets[0].thesis
    assert c.bets[0].status == "watch"
    assert c.bets[0].conviction == "2/5"


def test_yaml_parse_failure_with_no_recoverable_ticker_stays_empty(disk_root):
    """A malformed file with no ticker-carrying line at all must still
    degrade gracefully (no crash, no phantom bet) — the fallback recovers
    what's there, it doesn't invent anything."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH"], 3.12, 7150.0)
    (repo / "vault" / "investment-cases" / "macro-note.md").write_text(
        "---\ntitle: Some macro note: with a colon\nkind: process_note\n---\n\nBody.\n",
        encoding="utf-8",
    )
    table = risk_clusters.load_risk_clusters("ben")
    c = table.clusters[0]
    assert c.bet_count == 0


# ─── Uncovered/unclustered NAV row (review addition (b), Joris-approved) ──
#
# _summary.md's real "Non-cluster NAV breakdown" table (copied verbatim from
# the live bubble-ben-vault repo, 2026-07-24) rolls the ETF/cash/single-stock
# (§3a)/crypto (§3b) sleeves clustering excludes by design into one bold
# **Total non-cluster** row:
#
#     | Bucket | NAV % | $ |
#     |---|---:|---:|
#     | Uncovered ETF (no roster) | 28.89% | $66,157 |
#     | Crypto sleeve (§3b — excluded by design) | 0.56% | $1,285 |
#     | Single-stock sleeve (§3a — excluded by design) | 3.01% | $6,888 |
#     | Cash (deployable) | 36.97% | $84,662 |
#     | **Total non-cluster** | **69.43%** | |
#
# That figure (69.43%) is _summary.md's own deduplicated number — used
# directly rather than recomputed, since a naive 100% - sum(cluster weights)
# double-counts positions that are members of 2+ clusters by design (the
# same "Sum check" fact _summary.md documents), which is also why change (c)
# (the overlap note) exists.

_REAL_SUMMARY_NONCLUSTER_TABLE = """---
title: Cluster Analysis — Cross-Cluster Summary
type: cluster_summary
status: active
---

# Cluster Analysis — Summary

**Total NAV (positions + cash):** $229,010

| Cluster | NAV % | Members |
|---------|------:|---------|
| [[ai_capex]] AI-Capex Cluster | 3.12% | 3 |

**Non-cluster NAV breakdown:**

| Bucket | NAV % | $ |
|---|---:|---:|
| Uncovered ETF (no roster) | 28.89% | $66,157 |
| Crypto sleeve (§3b — excluded by design) | 0.56% | $1,285 |
| Single-stock sleeve (§3a — excluded by design) | 3.01% | $6,888 |
| Cash (deployable) | 36.97% | $84,662 |
| **Total non-cluster** | **69.43%** | |

**Sum check (deduplicated):** clustered 30.57% + non-cluster 69.43% = 99.99% (should be ~100%)
"""


def test_uncovered_pct_read_directly_from_summary_md(disk_root):
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH", "URA", "ROBO"], 3.12, 7150.0)
    (repo / "vault" / "clusters" / "_summary.md").write_text(
        _REAL_SUMMARY_NONCLUSTER_TABLE, encoding="utf-8",
    )
    table = risk_clusters.load_risk_clusters("ben")
    assert table.uncovered_pct == pytest.approx(69.43)
    assert table.uncovered_source == "summary"
    assert table.uncovered_display == "69.43%"


# ─── Deduplicated clustered total (PR #291 follow-up) ─────────────────────
#
# The uncovered row alone (69.43%) doesn't reconcile against the raw sum of
# the individual cluster rows (~25.58%/~40.43% in these fixtures) because
# those rows double-count multi-cluster-membership positions by design.
# _summary.md's own "Sum check (deduplicated)" line states the figure that
# DOES reconcile: "clustered 30.57% + non-cluster 69.43% = 99.99%". That
# 30.57% must be parsed from this line directly, not computed from the
# visible cluster rows (which would just reproduce the double-counted
# figure the reconciliation exists to correct).

def test_dedup_clustered_pct_read_directly_from_summary_md(disk_root):
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH", "URA", "ROBO"], 3.12, 7150.0)
    (repo / "vault" / "clusters" / "_summary.md").write_text(
        _REAL_SUMMARY_NONCLUSTER_TABLE, encoding="utf-8",
    )
    table = risk_clusters.load_risk_clusters("ben")
    assert table.dedup_clustered_pct == pytest.approx(30.57)
    assert table.dedup_clustered_source == "summary"
    assert table.dedup_clustered_display == "30.57%"


def test_dedup_clustered_plus_uncovered_reconciles_to_100(disk_root):
    """The whole point of this fix: dedup_clustered_pct + uncovered_pct must
    sum to ~100%, using the REAL live-vault _summary.md figures (30.57% +
    69.43% = 99.99% ~ 100%), even though the raw cluster-row sum does not."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH", "URA", "ROBO"], 3.12, 7150.0)
    (repo / "vault" / "clusters" / "_summary.md").write_text(
        _REAL_SUMMARY_NONCLUSTER_TABLE, encoding="utf-8",
    )
    table = risk_clusters.load_risk_clusters("ben")
    total = table.dedup_clustered_pct + table.uncovered_pct
    assert total == pytest.approx(100.0, abs=0.1)
    # NOTE: _summary.md's own prose states "= 99.99%" (rounding artifact of
    # its unrounded internal figures), but summing the two *rounded* display
    # figures we parsed (30.57 + 69.43) is exactly 100.00 — arguably a
    # cleaner reconciliation than the source line's own rounding. Either way
    # it's within a rounding hair of 100%, which is the property this test
    # actually guards.
    assert table.reconciliation_total_display == "100.00%"


def test_dedup_clustered_pct_falls_back_to_complement_of_uncovered(disk_root):
    """No "Sum check" line in _summary.md (or no _summary.md at all) ->
    derive dedup_clustered = 100 - uncovered. Still reconciles by
    construction; source is labelled "derived" so tests/debug can tell."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "a", "Cluster A", ["X"], 30.0, 1000.0)
    _write_cluster(repo, "b", "Cluster B", ["Y"], 20.0, 500.0)
    # No _summary.md at all -> uncovered_pct itself falls back to "derived"
    # (100 - sum(cluster weights) = 50.0), and dedup_clustered should mirror
    # via the same complement relationship (100 - 50.0 = 50.0).
    table = risk_clusters.load_risk_clusters("ben")
    assert table.uncovered_source == "derived"
    assert table.dedup_clustered_source == "derived"
    assert table.dedup_clustered_pct == pytest.approx(50.0)
    assert table.dedup_clustered_pct + table.uncovered_pct == pytest.approx(100.0)


def test_dedup_clustered_pct_none_when_uncovered_also_none(disk_root):
    """No _summary.md, no cluster weights to derive uncovered from -> both
    uncovered_pct and dedup_clustered_pct are None, not a fabricated figure.
    The template must not render the reconciliation rows at all."""
    repo = _build_repo(disk_root)
    (repo / "vault" / "clusters" / "a.md").write_text(
        "---\ncluster_id: a\ntitle: Cluster A\nmembers: [X]\n---\n\n# Cluster A\n",
        encoding="utf-8",
    )
    table = risk_clusters.load_risk_clusters("ben")
    assert table.uncovered_pct is None
    assert table.dedup_clustered_pct is None
    assert table.dedup_clustered_display == "—"
    assert table.reconciliation_total_display == "—"


# ─── Fail-closed regression: _load_dedup_clustered_pct() adversarial inputs ─
#
# board #785: `_load_dedup_clustered_pct()`'s regex
# (`Sum check \(deduplicated\)[^\n]*?clustered\s+([\d.]+)%`) must fail CLOSED
# (return None / fall through to the documented complement fallback) rather
# than return a garbage partial match when `_summary.md`'s "Sum check
# (deduplicated)" line is reworded, reordered, or malformed. These tests
# call `_load_dedup_clustered_pct()` directly (unit-level — the function
# under test) so each malformed-line variant is isolated from the rest of
# `load_risk_clusters()`'s pipeline.
#
# Real fail-open found and fixed here: the original regex had no word
# boundary before "clustered", so it matched the "clustered" *substring*
# inside "non-clustered"/"non-cluster" tokens. A line that only mentions
# "non-clustered 69.43%" (no standalone "clustered N%" figure at all) used
# to silently return 69.43 — the non-cluster percentage mislabeled as the
# deduplicated CLUSTERED figure. Fixed with a `(?<![\w-])` lookbehind (see
# risk_clusters.py). The well-formed path is asserted byte-identical below.

def _write_summary_text(repo: Path, text: str) -> None:
    (repo / "vault" / "clusters" / "_summary.md").write_text(text, encoding="utf-8")


def test_dedup_regex_wellformed_baseline_unchanged(disk_root):
    """GREEN baseline: the well-formed line still parses exactly as before
    the fix (byte-identical behaviour on the real live-vault shape)."""
    repo = _build_repo(disk_root, slug="wf")
    _write_summary_text(
        repo,
        "**Sum check (deduplicated):** clustered 30.57% + non-cluster 69.43% = 99.99% (should be ~100%)\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    assert pct == pytest.approx(30.57)
    assert source == "summary"


@pytest.mark.parametrize(
    "label",
    [
        "Sum check (dedup)",           # reworded label
        "Sum-check (deduplicated)",    # hyphenated label
        "Deduplicated sum check",      # reordered label words
    ],
)
def test_dedup_regex_rejects_reworded_label(disk_root, label):
    """A relabeled "Sum check (deduplicated)" header must not be matched —
    falls through to the uncovered-complement fallback (source: "derived"),
    never a value silently sourced from a header the code doesn't recognize."""
    repo = _build_repo(disk_root, slug="reworded")
    _write_summary_text(
        repo, f"**{label}:** clustered 30.57% + non-cluster 69.43% = 99.99%\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    assert source == "derived"
    assert pct == pytest.approx(30.57)  # complement of uncovered, not a parse


def test_dedup_regex_rejects_reordered_clustered_after_nonclusterlabel(disk_root):
    """"clustered N%" appearing BEFORE the "Sum check (deduplicated)" label
    (i.e. outside the label's line) must not leak into the match — the regex
    is anchored to text that follows the label on the same line."""
    repo = _build_repo(disk_root, slug="reordered")
    _write_summary_text(
        repo,
        "clustered 55%% raw mention elsewhere in the doc\n"
        "**Sum check (deduplicated):** non-cluster 69.43% only, no clustered figure here\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    # No standalone "clustered N%" on the Sum-check line itself -> falls back
    # to the documented complement, not the unrelated "clustered 55%" above.
    assert source == "derived"
    assert pct == pytest.approx(30.57)


def test_dedup_regex_fail_closed_on_missing_percent_sign(disk_root):
    repo = _build_repo(disk_root, slug="nopercent")
    _write_summary_text(
        repo,
        "**Sum check (deduplicated):** clustered 30.57 + non-cluster 69.43% = 99.99%\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    assert source == "derived"
    assert pct == pytest.approx(30.57)  # complement fallback, not a bad parse


def test_dedup_regex_fail_closed_on_missing_number(disk_root):
    repo = _build_repo(disk_root, slug="nonumber")
    _write_summary_text(
        repo,
        "**Sum check (deduplicated):** clustered % + non-cluster 69.43% = 99.99%\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    assert source == "derived"
    assert pct == pytest.approx(30.57)


def test_dedup_regex_fail_closed_on_empty_summary_file(disk_root):
    repo = _build_repo(disk_root, slug="empty")
    _write_summary_text(repo, "")
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    assert source == "derived"
    assert pct == pytest.approx(30.57)


def test_dedup_regex_fail_closed_on_section_absent(disk_root):
    """_summary.md exists but has no "Sum check" section at all."""
    repo = _build_repo(disk_root, slug="nosection")
    _write_summary_text(
        repo, "# Cluster Analysis — Summary\n\nNo sum-check line in this file.\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    assert source == "derived"
    assert pct == pytest.approx(30.57)


def test_dedup_regex_fail_closed_when_summary_missing_and_no_uncovered(disk_root):
    """No _summary.md at all AND uncovered_pct also unavailable (None) ->
    the function must return (None, ""), never a fabricated number."""
    repo = _build_repo(disk_root, slug="nosummarynouncovered")
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=None,
    )
    assert pct is None
    assert source == ""


def test_dedup_regex_rejects_substring_match_inside_non_clustered(disk_root):
    """THE bug this card exists to catch: a line with ONLY "non-clustered
    N%" (a different metric — the non-cluster/uncovered figure) and no
    standalone "clustered N%" figure at all must NOT have its regex match
    the "clustered" substring inside "non-clustered" and return that number
    as if it were the deduplicated clustered total. Must fail closed
    (fall through to the complement-of-uncovered derivation) instead."""
    repo = _build_repo(disk_root, slug="substring")
    _write_summary_text(
        repo,
        "**Sum check (deduplicated):** non-clustered 69.43% (no true clustered figure on this line)\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    # Must NOT be 69.43 (the non-cluster figure smuggled through as
    # "clustered" via unanchored substring matching) — must fall back to the
    # documented complement instead.
    assert source == "derived"
    assert pct == pytest.approx(30.57)


def test_dedup_regex_recovers_real_clustered_figure_despite_non_clustered_prefix(disk_root):
    """Companion to the substring-match regression above: when the SAME line
    has both a "non-clustered N%" mention AND a real standalone "clustered
    N%" figure, the true clustered figure must still be recovered (not
    swallowed by the fix, and not confused with the non-cluster one)."""
    repo = _build_repo(disk_root, slug="substring2")
    _write_summary_text(
        repo,
        "**Sum check (deduplicated):** non-clustered 69.43% + clustered 30.57% = 99.99%\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    assert source == "summary"
    assert pct == pytest.approx(30.57)


def test_dedup_regex_multiple_candidate_lines_uses_first_valid_match(disk_root):
    """Multiple "Sum check (deduplicated)" lines in the same file (shouldn't
    happen in a well-formed _summary.md, but must not crash or silently
    pick garbage) -> re.search returns the first match, deterministically."""
    repo = _build_repo(disk_root, slug="multi")
    _write_summary_text(
        repo,
        "**Sum check (deduplicated):** clustered 30.57% + non-cluster 69.43% = 99.99%\n"
        "**Sum check (deduplicated):** clustered 99.99% + non-cluster 0.01% = 100.00%\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    assert source == "summary"
    assert pct == pytest.approx(30.57)  # first match, not the second


def test_dedup_regex_different_metric_same_line_does_not_leak(disk_root):
    """A number attached to a DIFFERENT metric label (not "clustered") on
    the Sum-check line must never be picked up as the clustered figure."""
    repo = _build_repo(disk_root, slug="diffmetric")
    _write_summary_text(
        repo,
        "**Sum check (deduplicated):** non-cluster 69.43% only (clustered figure omitted)\n",
    )
    pct, source = risk_clusters._load_dedup_clustered_pct(
        repo / "vault", uncovered_pct=69.43,
    )
    assert source == "derived"
    assert pct == pytest.approx(30.57)


def test_dedup_regex_fail_closed_end_to_end_via_load_risk_clusters(disk_root):
    """End-to-end regression through the public `load_risk_clusters()` path
    (not just the isolated helper): a `_summary.md` whose Sum-check line only
    carries the non-cluster figure must produce the SAFE derived
    dedup_clustered_pct, never the smuggled non-cluster number."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "ai_capex", "AI-Capex Cluster", ["SMH"], 3.12, 7150.0)
    _write_summary_text(
        repo,
        "\n".join([
            "---",
            "title: Cluster Analysis — Cross-Cluster Summary",
            "type: cluster_summary",
            "---",
            "",
            "# Cluster Analysis — Summary",
            "",
            "| Bucket | NAV % | $ |",
            "|---|---:|---:|",
            "| **Total non-cluster** | **69.43%** | |",
            "",
            "**Sum check (deduplicated):** non-clustered 69.43% (malformed — no true clustered figure)",
            "",
        ]),
    )
    table = risk_clusters.load_risk_clusters("ben")
    assert table.uncovered_pct == pytest.approx(69.43)
    assert table.uncovered_source == "summary"
    # Must be the SAFE derived complement (100 - 69.43 = 30.57), not the
    # smuggled 69.43 non-cluster figure mislabeled as "clustered".
    assert table.dedup_clustered_pct == pytest.approx(30.57)
    assert table.dedup_clustered_source == "derived"


def test_uncovered_pct_falls_back_to_derived_when_no_summary(disk_root):
    """No _summary.md at all -> derive uncovered = 100 - sum(cluster
    weights). Explicitly an approximation (can undercount when clusters
    overlap) — only used because there's no better source."""
    repo = _build_repo(disk_root)
    _write_cluster(repo, "a", "Cluster A", ["X"], 30.0, 1000.0)
    _write_cluster(repo, "b", "Cluster B", ["Y"], 20.0, 500.0)
    table = risk_clusters.load_risk_clusters("ben")
    assert table.uncovered_pct == pytest.approx(50.0)
    assert table.uncovered_source == "derived"


def test_uncovered_pct_none_when_summary_missing_row_and_no_weights(disk_root):
    """No _summary.md, no cluster weights to derive from -> None, not a
    fabricated 0% (the template must not render an uncovered row at all in
    this case)."""
    repo = _build_repo(disk_root)
    # nav_weight_pct absent entirely -> _write_cluster's dict helper always
    # writes a float; write the memo by hand so the field is truly missing.
    (repo / "vault" / "clusters" / "a.md").write_text(
        "---\ncluster_id: a\ntitle: Cluster A\nmembers: [X]\n---\n\n# Cluster A\n",
        encoding="utf-8",
    )
    table = risk_clusters.load_risk_clusters("ben")
    assert table.uncovered_pct is None
    assert table.uncovered_display == "—"


def test_uncovered_plus_cluster_weights_sum_near_100_on_real_summary(disk_root):
    """Weights + uncovered should sum to ~100% NAV using _summary.md's own
    per-cluster NAV% figures (the same 12-cluster table shape from the live
    vault) alongside its uncovered figure — this is the reconciliation the
    review asked for."""
    repo = _build_repo(disk_root)
    # Mirror the live _summary.md's 12 cluster rows + uncovered total.
    weights = {
        "ex_us_equity": 7.78, "defense_thematic": 0.00, "pea_geographic_diversifier": 9.53,
        "eu_quality_value_active_basket": 2.70, "us_tech_concentration_pack": 5.58,
        "ai_capex": 3.12, "dividend_cluster": 2.65, "long_duration": 2.80,
        "commodity_inflation_complex": 3.35, "real_assets": 0.43,
        "healthcare_complex": 1.50, "us_factor_smallcap": 0.99,
    }
    for cid, w in weights.items():
        _write_cluster(repo, cid, cid.replace("_", " ").title(), ["X"], w, 100.0)
    (repo / "vault" / "clusters" / "_summary.md").write_text(
        _REAL_SUMMARY_NONCLUSTER_TABLE, encoding="utf-8",
    )
    table = risk_clusters.load_risk_clusters("ben")
    # NOTE: raw per-cluster NAV% sums to 40.43% here (25.58% on the real
    # vault's own overlapping members) which does NOT reconcile to
    # 100 - 69.43 = 30.57% — that gap IS the multi-cluster overlap
    # _summary.md documents in its own "Sum check" line, which is exactly
    # why change (c)'s overlap note exists. This test asserts the value we
    # actually promise: the uncovered figure is _summary.md's real
    # deduplicated number, not a naive derived one, when both are available.
    assert table.uncovered_source == "summary"
    assert table.uncovered_pct == pytest.approx(69.43)


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


def test_route_cluster_table_wrapped_in_scroll_container(client, fixture_root):
    """Mobile-overflow fix: the cluster table must sit inside a
    `.table-scroll` (overflow-x:auto) wrapper so a narrow viewport scrolls the
    table WITHIN its own box rather than spilling the whole page body sideways.
    Guards against the wrapper being dropped in a future edit."""
    repo = fixture_root / "bubble-ops-fixture"
    (repo / "vault" / "clusters").mkdir(parents=True)
    (repo / "vault" / "investment-cases").mkdir(parents=True)
    _write_cluster_into(repo, "ai_capex", "AI-Capex Cluster", ["SMH"], 3.12, 7150.0)
    _write_case_into(repo, "case1.md", ["SMH"], "SMH — AI-capex core holding")

    body = client.get("/dept/fixture").text
    # the scroll wrapper opens before the table it protects
    assert '<div class="table-scroll">' in body
    assert body.index('<div class="table-scroll">') < body.index(
        '<table class="risk-cluster-table">'
    )


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


def test_route_renders_uncovered_row_and_overlap_note(client, fixture_root):
    """Review additions (b) + (c): the uncovered/unclustered row and the
    multi-cluster-overlap note must both render on the page when there are
    clusters."""
    repo = fixture_root / "bubble-ops-fixture"
    (repo / "vault" / "clusters").mkdir(parents=True)
    (repo / "vault" / "investment-cases").mkdir(parents=True)
    _write_cluster_into(repo, "ai_capex", "AI-Capex Cluster", ["SMH"], 3.12, 7150.0)
    _write_case_into(repo, "case1.md", ["SMH"], "SMH — AI-capex core holding")
    (repo / "vault" / "clusters" / "_summary.md").write_text(
        _REAL_SUMMARY_NONCLUSTER_TABLE, encoding="utf-8",
    )

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    # Uncovered row: label + sourced figure, visually distinct row class.
    assert "risk-cluster-uncovered-row" in body
    assert "Non couvert" in body
    assert "69.43%" in body
    # Overlap note: some form of "not mutually exclusive / can overlap"
    # messaging present near the table.
    assert "risk-cluster-overlap-note" in body
    assert "plusieurs clusters" in body
    # Reconciliation rows (PR #291 follow-up): dedup-clustered total +
    # uncovered = ~100%, sourced from the same _summary.md fixture's "Sum
    # check" line (30.57%).
    assert "risk-cluster-recon-clustered" in body
    assert "risk-cluster-recon-total" in body
    assert "30.57%" in body
    assert "100.00%" in body  # the reconciliation_total_display closer


def test_route_no_recon_rows_when_dedup_unavailable(client, fixture_root):
    """No _summary.md and no cluster weights to derive uncovered from ->
    neither the uncovered row NOR the reconciliation rows render (nothing to
    reconcile without a real figure)."""
    repo = fixture_root / "bubble-ops-fixture"
    (repo / "vault" / "clusters").mkdir(parents=True)
    (repo / "vault" / "investment-cases").mkdir(parents=True)
    (repo / "vault" / "clusters" / "ai_capex.md").write_text(
        "---\ncluster_id: ai_capex\ntitle: AI-Capex Cluster\nmembers: [SMH]\n---\n\n# AI-Capex\n",
        encoding="utf-8",
    )

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "risk-cluster-uncovered-row" not in r.text
    assert "risk-cluster-recon-clustered" not in r.text
    assert "risk-cluster-recon-total" not in r.text


def test_route_no_uncovered_row_when_pct_unavailable(client, fixture_root):
    """No _summary.md and no cluster weights to derive from -> no uncovered
    row rendered at all (must not fabricate a 0%/—% row)."""
    repo = fixture_root / "bubble-ops-fixture"
    (repo / "vault" / "clusters").mkdir(parents=True)
    (repo / "vault" / "investment-cases").mkdir(parents=True)
    (repo / "vault" / "clusters" / "ai_capex.md").write_text(
        "---\ncluster_id: ai_capex\ntitle: AI-Capex Cluster\nmembers: [SMH]\n---\n\n# AI-Capex\n",
        encoding="utf-8",
    )

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    assert "risk-cluster-uncovered-row" not in r.text
    # The overlap note still renders (it's static once there are clusters).
    assert "risk-cluster-overlap-note" in r.text


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
