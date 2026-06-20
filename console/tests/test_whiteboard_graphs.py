"""
test_whiteboard_graphs.py — KPI time-series graphs on /dept/<slug>.

{{OPERATOR}} msg 1163 (2026-06-01): each dept page should show graphs of its
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
    {{OPERATOR}} flagged 2026-06-19)."""
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
    nav_summary / performance / sleeve_allocation_pct_nav sub-dicts.  (#139)"""
    tmp_path = disk_root
    repo = _build_repo(tmp_path)
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
            "dept": "demo", "date": day, "generated_by": "layer4",
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

    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("demo")}

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
    The synthesiser must pick those up via its alias list."""
    tmp_path = disk_root
    repo = _build_repo(tmp_path)
    for day, nav in (("2026-06-13", 237_400), ("2026-06-14", 238_000)):
        d = repo / "outputs" / day / "4"
        d.mkdir(parents=True)
        (d / "management-export.yaml").write_text(
            yaml.safe_dump({
                "export": {
                    "dept": "demo", "date": day,
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

    series = {s.key: s for s in whiteboard_series.load_whiteboard_series("demo")}
    assert "sleeve_single_stock_pct" in series   # alias resolved
    assert "sleeve_crypto_pct" in series          # alias resolved
    assert "nav_usd" in series                    # consolidated_nav_usd_true picked up


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
