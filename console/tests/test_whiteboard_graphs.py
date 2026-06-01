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
