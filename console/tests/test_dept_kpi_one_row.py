"""
test_dept_kpi_one_row.py — #534 (Joris): the dept page shows only ONE row of
KPI stat-tiles and ONE row of "Évolution des métriques" sparkline graphs by
default; everything past the first desktop row tucks behind a "+N de plus"
collapse (the loop-runs-overflow idiom).

Rows chosen from the desktop (1280) column count of each grid:
  - #dept-kpi-grid  repeat(auto-fit, minmax(148px,1fr)) → 5 tiles / row
  - .kpi-chart-grid repeat(auto-fit, minmax(240px,1fr)) → 3 charts / row

Guards asserted here:
  - > one row → first-row items visible, rest inside a <details>, all still
    in the DOM (id + values preserved).
  - ≤ one row → all items rendered with NO empty <details> ("+0" never shown).
"""
from __future__ import annotations

from pathlib import Path

import yaml

KPI_ROW = 5      # visible KPI stat-tiles (one desktop row)
GRAPH_ROW = 3    # visible sparkline charts (one desktop row)


def _write_whiteboard(root: Path, n_kpis: int, slug: str = "fixture") -> None:
    repo = root / f"bubble-ops-{slug}"
    kpis = [
        {"label": f"Indicateur {i}", "value": str(100 + i),
         "trend": "up" if i % 2 else "stable"}
        for i in range(n_kpis)
    ]
    (repo / "whiteboard.yaml").write_text(
        yaml.safe_dump({"title": "Tableau", "kpis": kpis}, sort_keys=False),
        encoding="utf-8",
    )


def _write_export(root: Path, day: str, top_kpis: dict, slug: str = "fixture") -> None:
    d = root / f"bubble-ops-{slug}" / "outputs" / day / "4"
    d.mkdir(parents=True, exist_ok=True)
    (d / "management-export.yaml").write_text(
        yaml.safe_dump({"dept": slug, "date": day, "top_kpis": top_kpis},
                       sort_keys=False),
        encoding="utf-8",
    )


# ─── KPI stat-tiles ─────────────────────────────────────────────────────────

def test_kpi_tiles_many_collapse_past_first_row(client, fixture_root):
    """14 KPIs → 5 visible tiles + a <details> holding the other 9; every
    KPI value still exists in the DOM, and the #dept-kpi-grid id survives."""
    n = 14
    _write_whiteboard(fixture_root, n)

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text

    # The grid id is preserved (tests + tooling depend on it).
    assert 'id="dept-kpi-grid"' in body
    # An overflow collapse exists with the natural French summary.
    assert 'class="loop-runs-overflow kpi-overflow"' in body
    extra = n - KPI_ROW
    assert f"{extra} autres indicateurs" in body
    assert f"voir tous ({n})" in body

    # ALL 14 KPI tiles still exist in the DOM (collapsed ones live in <details>).
    assert body.count('class="kpi-value"') == n
    # Every value string is present (nothing dropped).
    for i in range(n):
        assert str(100 + i) in body


def test_kpi_tiles_one_row_no_collapse(client, fixture_root):
    """Exactly one row (5 KPIs) → all shown, NO <details> (no empty "+0")."""
    n = KPI_ROW
    _write_whiteboard(fixture_root, n)

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text

    assert 'id="dept-kpi-grid"' in body
    assert body.count('class="kpi-value"') == n
    # No overflow collapse rendered for a single row.
    assert "kpi-overflow" not in body


def test_kpi_tiles_under_one_row_no_collapse(client, fixture_root):
    """Fewer than one row (3 KPIs) → all shown, no collapse."""
    n = 3
    _write_whiteboard(fixture_root, n)

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    assert body.count('class="kpi-value"') == n
    assert "kpi-overflow" not in body


# ─── Sparkline graphs ───────────────────────────────────────────────────────

def _many_kpi_export(n: int, offset: int) -> dict:
    # numeric leaves that MOVE (so each becomes a plottable series with a chart)
    return {f"metric_{i}": (10 + i) * offset for i in range(n)}


def test_graphs_many_collapse_past_first_row(client, fixture_root):
    """>3 graph series → 3 visible charts + a <details> holding the rest.
    Every sparkline SVG stays in the DOM."""
    n = 7
    _write_export(fixture_root, "2026-05-30", _many_kpi_export(n, 1))
    _write_export(fixture_root, "2026-05-31", _many_kpi_export(n, 2))

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text

    assert "Évolution des métriques" in body
    assert 'class="loop-runs-overflow kpi-overflow"' in body
    extra = n - GRAPH_ROW
    assert f"{extra} autres graphiques" in body
    assert f"voir tous ({n})" in body
    # All n charts rendered (visible + collapsed). One polyline per chart —
    # a stable per-chart marker (the svg class matches twice via the trend
    # modifier, so count that instead).
    assert body.count('class="kpi-chart-line"') == n


def test_graphs_one_row_no_collapse(client, fixture_root):
    """Exactly one row (3 graphs) → all shown, NO collapse."""
    n = GRAPH_ROW
    _write_export(fixture_root, "2026-05-30", _many_kpi_export(n, 1))
    _write_export(fixture_root, "2026-05-31", _many_kpi_export(n, 2))

    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    assert "Évolution des métriques" in body
    assert body.count('class="kpi-chart-line"') == n
    assert "kpi-overflow" not in body


def test_graphs_empty_state_preserved(client, fixture_root):
    """No L4 history → the graphs empty-state still renders, no collapse."""
    # No whiteboard.yaml, no exports.
    r = client.get("/dept/fixture")
    assert r.status_code == 200
    body = r.text
    assert "kpi-overflow" not in body
    # The friendly empty-state message is preserved.
    assert "Les graphiques apparaîtront" in body
