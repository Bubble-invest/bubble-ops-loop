"""
test_whiteboard_graphs.py — KPI time-series graphs on /dept/<slug>.

Joris msg 1163 (2026-06-01): each dept page should show graphs of its
KPIs/metrics, populated by Layer 4 at each loop run. Layer 4 already writes
outputs/<date>/4/{management-export,risk-kpis}.yaml every run, so the series
is built by reading that history — no new write path.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from console import settings
from console.services import whiteboard_series


@pytest.fixture
def disk_root(monkeypatch, tmp_path):
    """Point the services' disk-mode reader at a temp root.

    settings.READ_FROM_DISK is captured at import time, so setting the env
    var isn't enough — patch the module attribute the helpers actually read.
    """
    monkeypatch.setattr(settings, "READ_FROM_DISK", str(tmp_path))
    return tmp_path


# ─── Helpers ──────────────────────────────────────────────────────────────

def _write_export(repo: Path, day: str, top_kpis: dict) -> None:
    """Write a management-export.yaml for one date under the dept's L4 dir."""
    d = repo / "outputs" / day / "4"
    d.mkdir(parents=True, exist_ok=True)
    (d / "management-export.yaml").write_text(
        yaml.safe_dump({
            "dept": repo.name.removeprefix("bubble-ops-"),
            "date": day,
            "status": "clean",
            "last_successful_layer": 4,
            "open_gates": 0,
            "open_exceptions": 0,
            "top_kpis": top_kpis,
        }, sort_keys=False),
        encoding="utf-8",
    )


def _build_repo(root: Path, slug: str = "demo") -> Path:
    repo = root / f"bubble-ops-{slug}"
    (repo / "outputs").mkdir(parents=True)
    return repo


# ─── Service-level tests ────────────────────────────────────────────────────

def test_series_built_from_management_export_history(disk_root):
    tmp_path = disk_root
    repo = _build_repo(tmp_path)
    _write_export(repo, "2026-05-29", {"ops_health_score": 80, "escalations_open": 2})
    _write_export(repo, "2026-05-30", {"ops_health_score": 85, "escalations_open": 1})
    _write_export(repo, "2026-05-31", {"ops_health_score": 90, "escalations_open": 0})

    series = whiteboard_series.load_whiteboard_series("demo")
    by_key = {s.key: s for s in series}

    assert "ops_health_score" in by_key
    health = by_key["ops_health_score"]
    assert [v for _, v in health.points] == [80.0, 85.0, 90.0]
    assert health.trend == "up"           # 80 → 90
    assert health.last_display == "90"    # whole number, no trailing .0
    assert health.has_chart
    assert health.polyline                # geometry computed

    esc = by_key["escalations_open"]
    assert esc.trend == "down"            # 2 → 0


def test_series_skips_booleans_and_strings(disk_root):
    tmp_path = disk_root
    repo = _build_repo(tmp_path)
    _write_export(repo, "2026-05-30", {"ops_health_score": 70, "dry_run": True})
    _write_export(repo, "2026-05-31", {"ops_health_score": 72, "dry_run": True})

    keys = {s.key for s in whiteboard_series.load_whiteboard_series("demo")}
    assert "ops_health_score" in keys
    assert "dry_run" not in keys          # boolean flag must not be plotted


def test_series_accepts_kpis_snapshot_block(disk_root):
    """Maya's L4 emits `kpis_snapshot` (not `top_kpis`) — must be picked up
    as the curated source rather than falling back to risk-kpis."""
    tmp_path = disk_root
    repo = _build_repo(tmp_path)
    for day, drafts in (("2026-05-30", 35), ("2026-05-31", 30)):
        d = repo / "outputs" / day / "4"
        d.mkdir(parents=True)
        (d / "management-export.yaml").write_text(
            yaml.safe_dump({
                "dept": "demo", "date": day, "status": "warning",
                "kpis_snapshot": {
                    "drafts_pending": drafts,
                    "validation_latency_p50_hours": None,  # null → skipped
                },
            }, sort_keys=False),
            encoding="utf-8",
        )
        # a risk-kpis with many leaves that must NOT be used (snapshot wins)
        (d / "risk-kpis.yaml").write_text(
            yaml.safe_dump({"volumes": {"a": 1, "b": 2, "c": 3}}), encoding="utf-8")

    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("demo")}
    assert set(series) == {"drafts_pending"}          # snapshot used, null dropped
    assert series["drafts_pending"].trend == "down"   # 35 → 30
    assert series["drafts_pending"].label == "Drafts en attente"


def test_series_accepts_curated_block_nested_under_export(disk_root):
    """Ben (and any dept that wraps its export under a top-level `export:` key)
    declares its curated KPIs at `export.top_kpis`. The loader must look inside
    `export:` for the curated block, not only at the doc root — otherwise it
    silently falls back to flattening every risk-kpis leaf (the 12-chart clutter
    Joris flagged 2026-06-19)."""
    tmp_path = disk_root
    repo = _build_repo(tmp_path)
    for day, nav in (("2026-05-30", 238_000), ("2026-05-31", 239_300)):
        d = repo / "outputs" / day / "4"
        d.mkdir(parents=True)
        (d / "management-export.yaml").write_text(
            yaml.safe_dump({
                "export": {                       # everything nested under export:
                    "dept": "demo", "date": day,
                    "top_kpis": {
                        "consolidated_nav_usd": nav,
                        "cash_pct": 36.2,
                    },
                    "nav_summary": {"unused": 999},  # other blocks ignored
                },
            }, sort_keys=False),
            encoding="utf-8",
        )
        # many risk-kpis leaves that must NOT be used (curated block wins)
        (d / "risk-kpis.yaml").write_text(
            yaml.safe_dump({"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}), encoding="utf-8")

    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("demo")}
    assert set(series) == {"consolidated_nav_usd", "cash_pct"}  # only the curated few
    assert series["consolidated_nav_usd"].trend == "up"         # 238k → 239.3k


def test_series_capped_to_max(disk_root):
    """A risk-kpis fallback with many leaves is capped, keeping the movers."""
    tmp_path = disk_root
    repo = _build_repo(tmp_path)
    # Build 20 KPIs across 2 days; only a few actually move.
    for i, day in enumerate(("2026-05-30", "2026-05-31")):
        d = repo / "outputs" / day / "4"
        d.mkdir(parents=True)
        block = {f"flat_{n}": 5 for n in range(20)}        # 20 static
        block["mover_a"] = i * 100                          # moves a lot
        block["mover_b"] = i * 50                           # moves some
        (d / "risk-kpis.yaml").write_text(
            yaml.safe_dump({"k": block}), encoding="utf-8")

    series = whiteboard_series.load_whiteboard_series("demo")
    assert len(series) <= 12
    keys = {s.key for s in series}
    assert "k.mover_a" in keys and "k.mover_b" in keys     # movers kept


def test_series_falls_back_to_risk_kpis(disk_root):
    """No management-export → flatten risk-kpis.yaml numeric leaves."""
    tmp_path = disk_root
    repo = _build_repo(tmp_path)
    for day, val in (("2026-05-30", 3), ("2026-05-31", 5)):
        d = repo / "outputs" / day / "4"
        d.mkdir(parents=True)
        (d / "risk-kpis.yaml").write_text(
            yaml.safe_dump({
                "date": day, "dept": "demo",
                "gates": {"open_total": val, "opened_today": 1},
            }, sort_keys=False),
            encoding="utf-8",
        )
    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("demo")}
    assert "gates.open_total" in series
    assert [v for _, v in series["gates.open_total"].points] == [3.0, 5.0]


def test_ignores_dry_run_and_non_date_dirs(disk_root):
    tmp_path = disk_root
    repo = _build_repo(tmp_path)
    # a real dated point
    _write_export(repo, "2026-05-31", {"ops_health_score": 88})
    # a dry-run dir that must be ignored
    dr = repo / "outputs" / "dry-run" / "2026-05-20T00-00-00Z" / "4"
    dr.mkdir(parents=True)
    (dr / "management-export.yaml").write_text(
        yaml.safe_dump({"top_kpis": {"ops_health_score": 1}}), encoding="utf-8")

    series = whiteboard_series.load_whiteboard_series("demo")
    health = next(s for s in series if s.key == "ops_health_score")
    assert [v for _, v in health.points] == [88.0]   # dry-run excluded
    assert not health.has_chart                       # single real point


def test_ben_kpis_synthesised_from_nav_perf_sleeve(disk_root):
    """Ben's management-export.yaml has no top_kpis block — the cockpit must
    synthesise NAV, performance, and sleeve-allocation series from his existing
    nav_summary / performance / sleeve_allocation_pct_nav sub-dicts.  (#139)
    Gate is now slug=="ben" (not field presence) per #199."""
    tmp_path = disk_root
    repo = _build_repo(tmp_path, slug="ben")
    # day, nav, cash, dd, use_perf_vs_bench_key, sharpe, etf, ss, crypto
    days = [
        # 2026-06-13 uses `performance_vs_benchmark` (weekly-review format),
        # no current_drawdown — mirrors real Ben L4 weekly export structure.
        ("2026-06-13", 237_400, 37.6, None,  True,  0.99, 66.7, 3.0,  4.6),
        ("2026-06-16", 239_271, 36.2, -0.5,  False, 0.99, 58.9, 3.07, 4.4),
        ("2026-06-17", 239_352, 36.2,  0.0,  False, 1.00, 58.9, 3.06, 4.4),
        ("2026-06-19", 238_435, 26.2, -2.7,  False, 0.60, 67.0, 2.3,  4.7),
    ]
    for day, nav, cash, dd, use_pvb, sharpe, etf, ss, crypto in days:
        d = repo / "outputs" / day / "4"
        d.mkdir(parents=True, exist_ok=True)
        perf_block = {"sharpe_itd": sharpe, "risk_status": "clean"}
        if dd is not None:
            perf_block["current_drawdown_pct"] = dd
        perf_key = "performance_vs_benchmark" if use_pvb else "performance"
        export_data: dict = {
            "dept": "ben", "date": day, "generated_by": "layer4",
            "nav_summary": {
                "consolidated_nav_usd": nav,
                "cash_pct": cash,
                "note": "ignore this string",
            },
            perf_key: perf_block,
            "sleeve_allocation_pct_nav": {
                "etf_backbone": etf,
                "single_stock": ss,
                "crypto_true": crypto,
                "limits_4_status": "no_breach",  # string — must be skipped
            },
        }
        (d / "management-export.yaml").write_text(
            yaml.safe_dump({"export": export_data}, sort_keys=False),
            encoding="utf-8",
        )

    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("ben")}

    # NAV history must be present across all 4 dates.
    assert "nav_usd" in series
    nav_s = series["nav_usd"]
    assert nav_s.label == "NAV (USD)"
    assert len(nav_s.points) == 4
    assert nav_s.has_chart
    # Trend: started 237400, ended 238435 → up.
    assert nav_s.trend == "up"

    # Sleeve allocation must be present.
    assert "sleeve_etf_pct" in series
    assert "sleeve_single_stock_pct" in series
    assert "sleeve_crypto_pct" in series
    assert series["sleeve_etf_pct"].label == "ETF backbone (% NAV)"

    # Strings must never appear as series.
    for s in series.values():
        assert isinstance(s.last_value, (int, float)) or s.last_value is None
    # Boolean/string guard: no "limits_4_status", no "risk_status", no "note"
    bad_keys = {"limits_4_status", "risk_status", "note"}
    assert not bad_keys.intersection(series.keys())

    # Cash % must be a series across all 4 dates.
    assert "cash_pct" in series
    assert series["cash_pct"].label == "Cash (%)"
    assert len(series["cash_pct"].points) == 4

    # Drawdown only appears on dates where the performance block has it
    # (06-16, 06-17, 06-19 = 3 points).
    assert "drawdown_pct" in series
    assert len(series["drawdown_pct"].points) == 3


def test_ben_kpis_sleeve_field_drift(disk_root):
    """Older Ben exports use `single_stock_3a` / `crypto_3b` naming.
    The synthesiser must pick those up via its alias list.
    Gate is now slug=="ben" per #199."""
    tmp_path = disk_root
    repo = _build_repo(tmp_path, slug="ben")
    for day, nav in (("2026-06-13", 237_400), ("2026-06-14", 238_000)):
        d = repo / "outputs" / day / "4"
        d.mkdir(parents=True)
        (d / "management-export.yaml").write_text(
            yaml.safe_dump({
                "export": {
                    "dept": "ben", "date": day,
                    "nav_summary": {"consolidated_nav_usd_true": nav, "cash_pct": 37.0},
                    "performance_vs_benchmark": {"sharpe_itd": 0.99},
                    "sleeve_allocation_pct_nav": {
                        "etf_backbone": 66.7,
                        "single_stock_3a": 3.0,  # old field name
                        "crypto_3b": 4.6,         # old field name
                    },
                },
            }, sort_keys=False),
            encoding="utf-8",
        )

    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("ben")}
    assert "sleeve_single_stock_pct" in series   # alias resolved
    assert "sleeve_crypto_pct" in series          # alias resolved
    assert "nav_usd" in series                    # consolidated_nav_usd_true picked up


def test_ben_synth_gate_is_slug_not_field_presence(disk_root):
    """#199 — the Ben KPI synthesiser must ONLY fire for dept slug 'ben'.

    A future dept that emits a nav_summary block (e.g. a second fund dept or
    any dept that happens to use that field name) must NOT get Ben's
    fund-office synthesiser applied.  Previously the gate was `isinstance(
    scope.get('nav_summary'), dict)` — a naming convention, not a contract.
    After #199 the gate is `dept_slug == 'ben'`.

    This test creates a non-ben dept with a nav_summary block and asserts:
    - the synthesised keys (nav_usd, cash_pct, ...) are ABSENT,
    - the dept falls through to the risk-kpis fallback instead.
    """
    tmp_path = disk_root
    # dept slug is "other-fund" — has nav_summary just like Ben, but is not Ben
    repo = _build_repo(tmp_path, slug="other-fund")
    for day, nav in (("2026-06-18", 500_000), ("2026-06-19", 501_000)):
        d = repo / "outputs" / day / "4"
        d.mkdir(parents=True)
        (d / "management-export.yaml").write_text(
            yaml.safe_dump({
                "export": {
                    "dept": "other-fund", "date": day,
                    # nav_summary present — would have triggered Ben synth pre-#199
                    "nav_summary": {
                        "consolidated_nav_usd": nav,
                        "cash_pct": 20.0,
                    },
                    "performance": {"sharpe_itd": 1.2},
                },
            }, sort_keys=False),
            encoding="utf-8",
        )
        # Provide a risk-kpis fallback so we can confirm the dept falls through
        (d / "risk-kpis.yaml").write_text(
            yaml.safe_dump({"ops_score": 90 + (1 if day == "2026-06-19" else 0)}),
            encoding="utf-8",
        )

    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("other-fund")}

    # Ben's synthesised keys must NOT appear for a non-ben dept.
    ben_synth_keys = {"nav_usd", "cash_pct", "return_itd_pct", "sleeve_etf_pct",
                      "sleeve_single_stock_pct", "sleeve_crypto_pct"}
    assert not ben_synth_keys.intersection(series), (
        f"Ben synthesiser wrongly fired for 'other-fund': "
        f"{ben_synth_keys.intersection(series)}"
    )
    # The dept should have fallen through to its risk-kpis fallback instead.
    assert "ops_score" in series


def test_no_history_returns_empty(disk_root):
    tmp_path = disk_root
    _build_repo(tmp_path)
    assert whiteboard_series.load_whiteboard_series("demo") == []


def test_unknown_dept_returns_empty(disk_root):
    tmp_path = disk_root
    assert whiteboard_series.load_whiteboard_series("nope") == []


# ─── Route-level test ───────────────────────────────────────────────────────

def test_dept_page_renders_graphs(client, fixture_root):
    """/dept/fixture must render the graphs section once L4 history exists."""
    repo = fixture_root / "bubble-ops-fixture"
    _write_export(repo, "2026-05-30", {"ops_health_score": 75})
    _write_export(repo, "2026-05-31", {"ops_health_score": 82})

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    assert "Évolution des métriques" in body          # graphs heading
    assert "kpi-chart-svg" in body                     # an SVG chart rendered
    assert "<polyline" in body                         # the line itself
    assert "Score de santé ops" in body                # humanized KPI label


def test_dept_page_graphs_empty_state(client):
    """No L4 history → friendly empty state, never a crash."""
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    # Section may be present (if whiteboard.yaml exists) or absent; either way
    # the page must render and not contain a stray broken chart.
    assert "kpi-chart-svg" not in r.text


# ─── Option A: current-keys restriction (Bug A #180) ────────────────────────

def _write_ben_legacy_export(repo: Path, day: str, nav: float, sleeve_etf: float) -> None:
    """Write a Ben-style legacy management-export (no top_kpis, uses nav_summary
    + sleeve_allocation_pct_nav) as existed for dates 06-13→06-19."""
    d = repo / "outputs" / day / "4"
    d.mkdir(parents=True, exist_ok=True)
    (d / "management-export.yaml").write_text(
        yaml.safe_dump({
            "export": {
                "dept": "ben", "date": day,
                "nav_summary": {"consolidated_nav_usd": nav, "cash_pct": 36.0},
                "performance": {"sharpe_itd": 0.99},
                "sleeve_allocation_pct_nav": {
                    "etf_backbone": sleeve_etf,
                    "single_stock": 3.0,
                    "crypto_true": 4.5,
                },
            },
        }, sort_keys=False),
        encoding="utf-8",
    )


def _write_ben_curated_export(repo: Path, day: str, top_kpis: dict) -> None:
    """Write a Ben-style curated management-export (with top_kpis) as emitted
    from 06-20 onward (lean 13 KPI set)."""
    d = repo / "outputs" / day / "4"
    d.mkdir(parents=True, exist_ok=True)
    (d / "management-export.yaml").write_text(
        yaml.safe_dump({
            "export": {
                "dept": "ben", "date": day,
                "top_kpis": top_kpis,
                # legacy blocks still present but must be ignored once top_kpis exists
                "nav_summary": {"consolidated_nav_usd": 999_999, "cash_pct": 0.0},
                "sleeve_allocation_pct_nav": {"etf_backbone": 0.0},
            },
        }, sort_keys=False),
        encoding="utf-8",
    )


def test_ben_graph_series_restricted_to_current_curated_keys(disk_root):
    """Bug A (#180) — Option A fix.

    Scenario: 06-13→06-19 have legacy Ben exports (nav_summary + sleeve_*,
    no top_kpis).  06-20 has a curated top_kpis block with lean KPIs.

    Before the fix: the pivot unioned ALL keys over all dates, so
    sleeve_etf_pct / sleeve_single_stock_pct / sleeve_crypto_pct from the
    legacy synthesiser appeared alongside the lean KPIs — exactly the bug.

    After the fix (Option A): the series is restricted to the keys in the
    MOST RECENT curated block (06-20's top_kpis).  Legacy sleeve_* keys are
    dropped.  The lean KPIs are present; nav_usd/cash_pct from the legacy
    synthesiser are dropped because they're not in the current curated set.
    """
    tmp_path = disk_root
    repo = _build_repo(tmp_path, slug="ben")

    # Legacy dates (06-13 → 06-19): only nav_summary + sleeve_* in export
    _write_ben_legacy_export(repo, "2026-06-13", nav=237_400, sleeve_etf=66.7)
    _write_ben_legacy_export(repo, "2026-06-16", nav=239_271, sleeve_etf=58.9)
    _write_ben_legacy_export(repo, "2026-06-19", nav=238_435, sleeve_etf=67.0)

    # Curated date (06-20): lean 13 KPIs via top_kpis block (numeric subset)
    lean_kpis = {
        "ops_health_score": 95,
        "open_gates": 0,
        "nav_total_usd": 238_435,
    }
    _write_ben_curated_export(repo, "2026-06-20", top_kpis=lean_kpis)

    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("ben")}

    # The lean curated KPIs must be present.
    assert "ops_health_score" in series, "lean curated key missing"
    assert "open_gates" in series, "lean curated key missing"
    assert "nav_total_usd" in series, "lean curated key missing"

    # Legacy sleeve_* series must NOT appear (they're not in today's top_kpis).
    legacy_keys = {"sleeve_etf_pct", "sleeve_single_stock_pct", "sleeve_crypto_pct"}
    assert not legacy_keys.intersection(series), (
        f"Legacy sleeve keys leaked into graph: {legacy_keys.intersection(series)}"
    )

    # Legacy synthesiser keys not in the lean set must also be absent.
    assert "nav_usd" not in series, (
        "nav_usd (legacy synthesiser) leaked — should be excluded by current-key filter"
    )
    assert "cash_pct" not in series, (
        "cash_pct (legacy synthesiser) leaked — should be excluded by current-key filter"
    )

    # The curated KPIs may be sparse (only 1 point from 06-20 if not in legacy).
    # That's fine — sparse lines are acceptable per Option A spec.
    assert len(series["ops_health_score"].points) == 1   # only 06-20 has this key
    assert not series["ops_health_score"].has_chart      # single point, no line yet


def test_current_keys_restriction_does_not_break_depts_without_curated_block(disk_root):
    """Option A fall-through: if NO date has a curated block, all keys pass
    through as before — existing behavior for depts that never curated."""
    tmp_path = disk_root
    repo = _build_repo(tmp_path, slug="ben")

    # Only legacy dates, no top_kpis ever
    _write_ben_legacy_export(repo, "2026-06-13", nav=237_400, sleeve_etf=66.7)
    _write_ben_legacy_export(repo, "2026-06-16", nav=239_271, sleeve_etf=58.9)

    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("ben")}

    # Legacy synthesiser keys MUST still appear (no curated anchor → no filter).
    assert "nav_usd" in series, "nav_usd missing: no-curated fallback broken"
    assert "sleeve_etf_pct" in series, "sleeve_etf_pct missing: no-curated fallback broken"
    assert len(series["nav_usd"].points) == 2
