"""
test_home_kpis_live.py — Item E3 polish.

The "Cette semaine en bref" KPI tiles must reflect live data:
  - Décisions en attente = sum of pending gates across all depts
  - Collègues en poste   = count of live departments
  - Arrivées en préparation = count of a-éclore departments

These must NOT be hardcoded literals (smoke saw 2·1·2 baseline, but the
values must move when the fixture changes).
"""
from __future__ import annotations

from pathlib import Path

import yaml


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "kpi-depts"
    root.mkdir()
    return root


def _make_live_dept(root: Path, slug: str, display: str, gates: int = 0) -> Path:
    repo = root / f"bubble-ops-{slug}"
    repo.mkdir()
    (repo / "dept.yaml").write_text(
        yaml.safe_dump({
            "department": {"slug": slug, "level": "ops", "mandate": "x"},
            "layers": {"subscribed": [1, 2, 3, 4]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (repo / "onboarding").mkdir()
    (repo / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump({
            "schema_version": 1, "slug": slug, "display_name": display,
            "owner": "joris", "created_at": "2026-05-15T10:00:00Z",
            "status": "Live",
            "validated_steps": ["mandate", "missions", "layers",
                                "skills_tools", "gates_kpis", "dry_run"],
            "last_updated_at": "2026-05-19T10:00:00Z",
            "commits": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    if gates > 0:
        (repo / "queues" / "gates").mkdir(parents=True)
        for i in range(gates):
            (repo / "queues" / "gates" / f"g-{slug}-{i}.yaml").write_text(
                yaml.safe_dump({
                    "id": f"g-{slug}-{i}", "kind": f"kind_{i}",
                    "risk_level": "low",
                    "current_mode": "manual_required",
                }, sort_keys=False),
                encoding="utf-8",
            )
    return repo


def _build_client(monkeypatch, root: Path):
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app  # noqa: WPS433
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})
    return c


def _extract_kpi_values(body: str) -> list[str]:
    """Extract every <div class="kpi-value">N</div> integer."""
    import re
    return re.findall(
        r'<div class="kpi-value">\s*(\d+)\s*</div>',
        body,
    )


def test_kpis_reflect_live_fixture_3_depts_0_eclore_7_gates(monkeypatch, tmp_path):
    """3 live depts, 0 in éclosion, gates total = 2+3+2 = 7 → KPI tiles
    show 7 (décisions en attente), 3 (collègues en poste), 0 (arrivées)."""
    root = _make_root(tmp_path)
    _make_live_dept(root, "alpha", "Alpha", gates=2)
    _make_live_dept(root, "beta",  "Beta",  gates=3)
    _make_live_dept(root, "gamma", "Gamma", gates=2)

    c = _build_client(monkeypatch, root)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text

    values = _extract_kpi_values(body)
    # Order on the page: Décisions en attente, Collègues en poste, Arrivées
    assert values == ["7", "3", "0"], (
        f"expected KPI tiles to read 7·3·0, got {values}"
    )


def test_kpis_count_changes_when_fixture_changes(monkeypatch, tmp_path):
    """If we add a 4th live dept with no gates, decisions stays the same,
    collègues bumps to 4, arrivées stays 0 — proves the values are NOT
    hardcoded literals."""
    root = _make_root(tmp_path)
    _make_live_dept(root, "alpha", "Alpha", gates=1)
    _make_live_dept(root, "beta",  "Beta",  gates=1)
    _make_live_dept(root, "gamma", "Gamma", gates=0)
    _make_live_dept(root, "delta", "Delta", gates=0)

    c = _build_client(monkeypatch, root)
    body = c.get("/").text
    values = _extract_kpi_values(body)
    assert values == ["2", "4", "0"], (
        f"expected KPI tiles to read 2·4·0, got {values}"
    )
