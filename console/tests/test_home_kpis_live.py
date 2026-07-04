"""
test_home_kpis_live.py — home de-dup (#524d) + live-data counts.

BEFORE #524d, "Cette semaine en bref" carried a three-tile kpi-grid
(id="dept-kpi-grid") repeating Décisions en attente / Collègues en poste /
Arrivées en préparation — the SAME numbers already shown in the hero counter
and the per-dept stat-tiles in section 02. #524d removed that duplicate grid.

This test now guards the de-dup + that the surviving figures are LIVE:
  · the redundant id="dept-kpi-grid" is GONE from home;
  · the pending-decisions count still reflects real gate totals (hero counter);
  · the count moves when the fixture changes (not a hardcoded literal).
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
            "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
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


def test_home_dropped_duplicate_kpi_grid(monkeypatch, tmp_path):
    """#524d: the redundant id="dept-kpi-grid" tiles were removed from home
    (they repeated the hero counter + the per-dept stat-tiles)."""
    root = _make_root(tmp_path)
    _make_live_dept(root, "alpha", "Alpha", gates=2)
    _make_live_dept(root, "beta",  "Beta",  gates=3)

    c = _build_client(monkeypatch, root)
    body = c.get("/").text
    assert 'id="dept-kpi-grid"' not in body, (
        "the duplicate 'Cette semaine en bref' kpi-grid must be gone (#524d de-dup)"
    )
    # The removed tile labels must not reappear as their own count-tiles.
    assert "Arrivées en préparation" not in body


def test_home_pending_count_reflects_live_gates(monkeypatch, tmp_path):
    """The pending-decisions figure in the hero counter reflects the real gate
    total (2 + 3 = 5 gates, 0 board cards in this fixture)."""
    root = _make_root(tmp_path)
    _make_live_dept(root, "alpha", "Alpha", gates=2)
    _make_live_dept(root, "beta",  "Beta",  gates=3)

    c = _build_client(monkeypatch, root)
    body = c.get("/").text
    # hero counter: "<b class="ochre-num">5</b> décisions qu'on attend"
    import re
    m = re.search(r'ochre-num">\s*(\d+)\s*</b>\s*\n?\s*décision', body)
    assert m, "hero decisions counter not found"
    assert m.group(1) == "5", f"expected 5 pending decisions, got {m.group(1)}"


def test_home_pending_count_moves_with_fixture(monkeypatch, tmp_path):
    """Proves the hero count is live, not a hardcoded literal: 1+1 gates → 2."""
    root = _make_root(tmp_path)
    _make_live_dept(root, "alpha", "Alpha", gates=1)
    _make_live_dept(root, "beta",  "Beta",  gates=1)

    c = _build_client(monkeypatch, root)
    body = c.get("/").text
    import re
    m = re.search(r'ochre-num">\s*(\d+)\s*</b>\s*\n?\s*décision', body)
    assert m and m.group(1) == "2", (
        f"expected 2 pending decisions, got {m.group(1) if m else None}"
    )
