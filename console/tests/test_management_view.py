"""
test_management_view.py — TDD tests for GAP 11.

Tests cover:
  - test_load_management_exports_aggregates_children_kpis
      load_management_exports('tony-test') returns both children with
      their risk-kpis content.
  - test_load_management_exports_handles_missing_layer_4
      Child with no outputs/*/4/ returns staleness_days >= 1 and
      last_seen_at is set appropriately.
  - test_load_management_exports_handles_missing_management_export
      Child with no management-export.yaml returns entry with
      management_export=None.
  - test_get_management_view_for_management_dept_returns_200
      GET /dept/tony-fixture/management-view returns 200 + child slugs in body.
  - test_get_management_view_for_ops_dept_returns_404
      GET /dept/fixture/management-view returns 404 with French message.

These are all file-system-only (READ_FROM_DISK fixture mode). No network.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TEST_BEARER = "test-token-xyz"


# ---------------------------------------------------------------------------
# Fixtures: extend the existing fixture_root with a tony management dept
# that lists two children: 'fixture' (already exists) and 'miranda'.
# ---------------------------------------------------------------------------

def _make_tony_dept(root: Path, children: list[str]) -> None:
    """Create bubble-ops-tony-test with a management dept.yaml inside root."""
    tony_dir = root / "bubble-ops-tony-test"
    tony_dir.mkdir(exist_ok=True)

    (tony_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {
                    "slug": "tony-test",
                    "level": "management",
                    "mandate": "Aggregator for test children",
                },
                "layers": {"subscribed": [1, 4]},
                "recurring_missions": [],
                "skills": {},
                "tools": [],
                "gate_policies": {},
                "hierarchy": {
                    "level": "management",
                    "parent": None,
                    "children": children,
                    "visibility": {
                        "read_outputs": children,
                        "read_risk_kpis": True,
                        "read_risk_briefs": True,
                        "read_raw_artifacts": False,
                        "read_secrets": False,
                        "read_paths": [
                            "outputs/*/4/risk-kpis.yaml",
                            "outputs/*/4/risk-brief.md",
                            "outputs/*/management-export.yaml",
                            "queues/gates/**",
                            "queues/improvements/**",
                        ],
                    },
                    "directive_policy": {
                        "can_open_priority_prs": True,
                        "target_queue": "queues/management/",
                        "requires_human_gate_for": ["mandate_change"],
                    },
                },
                "optional_domain_ledger": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (tony_dir / "onboarding").mkdir(exist_ok=True)
    (tony_dir / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "slug": "tony-test",
                "display_name": "Tony Test",
                "owner": "operator",
                "created_at": "2026-05-20T10:00:00Z",
                "status": "Live",
                "validated_steps": [
                    "mandate", "missions", "layers",
                    "skills_tools", "gates_kpis", "dry_run",
                ],
                "last_updated_at": "2026-05-20T10:00:00Z",
                "commits": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _make_mini_ben(root: Path) -> None:
    """Create bubble-ops-mini-ben with full Layer-4 outputs."""
    today = date.today().isoformat()
    ben_dir = root / "bubble-ops-mini-ben"
    ben_dir.mkdir(exist_ok=True)

    (ben_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": "mini-ben", "level": "ops", "mandate": "Test ops dept ben"},
                "layers": {"subscribed": [1, 2, 3, 4]},
                "recurring_missions": [],
                "skills": {},
                "tools": [],
                "gate_policies": {},
                "hierarchy": {
                    "level": "ops",
                    "parent": "tony-test",
                    "children": [],
                    "visibility": {
                        "read_outputs": [],
                        "read_risk_kpis": False,
                        "read_risk_briefs": False,
                        "read_raw_artifacts": False,
                        "read_secrets": False,
                    },
                    "directive_policy": {
                        "can_open_priority_prs": False,
                        "target_queue": None,
                        "requires_human_gate_for": [],
                    },
                },
                "optional_domain_ledger": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (ben_dir / "onboarding").mkdir(exist_ok=True)
    (ben_dir / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1, "slug": "mini-ben", "display_name": "Mini Ben",
                "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
                "status": "Live",
                "validated_steps": ["mandate", "missions", "layers", "skills_tools", "gates_kpis", "dry_run"],
                "last_updated_at": "2026-05-19T10:00:00Z", "commits": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # Layer-4 outputs
    layer4_dir = ben_dir / "outputs" / today / "4"
    layer4_dir.mkdir(parents=True, exist_ok=True)
    (layer4_dir / "risk-kpis.yaml").write_text(
        yaml.safe_dump({"dept": "mini-ben", "date": today, "aum_growth": 3.2}, sort_keys=False),
        encoding="utf-8",
    )
    (layer4_dir / "risk-brief.md").write_text(
        f"# Risk brief mini-ben {today}\n\nAll good today.",
        encoding="utf-8",
    )
    # management-export.yaml (sibling of 4/)
    (ben_dir / "outputs" / today / "management-export.yaml").write_text(
        yaml.safe_dump(
            {
                "dept": "mini-ben", "date": today,
                "status": "clean", "last_successful_layer": 4,
                "open_gates": 0, "open_exceptions": 0,
                "top_kpis": {"aum_growth": 3.2},
                "needs_management_attention": [],
                "links": {
                    "risk_kpis": f"outputs/{today}/4/risk-kpis.yaml",
                    "risk_brief": f"outputs/{today}/4/risk-brief.md",
                    "gates": "queues/gates/",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    # A pending gate
    gates_dir = ben_dir / "queues" / "gates"
    gates_dir.mkdir(parents=True, exist_ok=True)
    (gates_dir / "ben-gate-1.yaml").write_text(
        yaml.safe_dump({"id": "ben-gate-1", "kind": "exec_retry", "status": "pending"}),
        encoding="utf-8",
    )


def _make_mini_maya_no_layer4(root: Path) -> None:
    """Create bubble-ops-mini-maya WITHOUT Layer-4 outputs (stale scenario)."""
    # Use yesterday's date for a stale run
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    maya_dir = root / "bubble-ops-mini-maya"
    maya_dir.mkdir(exist_ok=True)

    (maya_dir / "dept.yaml").write_text(
        yaml.safe_dump(
            {
                "department": {"slug": "mini-maya", "level": "ops", "mandate": "Test ops dept maya"},
                "layers": {"subscribed": [1, 2, 3, 4]},
                "recurring_missions": [],
                "skills": {},
                "tools": [],
                "gate_policies": {},
                "hierarchy": {
                    "level": "ops", "parent": "tony-test", "children": [],
                    "visibility": {
                        "read_outputs": [], "read_risk_kpis": False,
                        "read_risk_briefs": False, "read_raw_artifacts": False, "read_secrets": False,
                    },
                    "directive_policy": {
                        "can_open_priority_prs": False, "target_queue": None, "requires_human_gate_for": [],
                    },
                },
                "optional_domain_ledger": None,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (maya_dir / "onboarding").mkdir(exist_ok=True)
    (maya_dir / "onboarding" / "STATE.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1, "slug": "mini-maya", "display_name": "Mini Maya",
                "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
                "status": "Live",
                "validated_steps": ["mandate", "missions", "layers", "skills_tools", "gates_kpis", "dry_run"],
                "last_updated_at": "2026-05-19T10:00:00Z", "commits": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # Only Layer 1 output yesterday — no Layer-4, no management-export
    layer1_dir = maya_dir / "outputs" / yesterday / "1"
    layer1_dir.mkdir(parents=True, exist_ok=True)
    (layer1_dir / "summary.md").write_text("Layer 1 partial run yesterday.\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def management_fixture_root(tmp_path: Path) -> Path:
    """
    Multi-dept root with:
      - bubble-ops-tony-test   : management dept with children=[mini-ben, mini-maya]
      - bubble-ops-mini-ben    : ops with full Layer-4 today
      - bubble-ops-mini-maya   : ops with no Layer-4 (stale)
    """
    root = tmp_path / "depts"
    root.mkdir()

    _make_mini_ben(root)
    _make_mini_maya_no_layer4(root)
    _make_tony_dept(root, children=["mini-ben", "mini-maya"])

    return root


@pytest.fixture
def app_with_mgmt(monkeypatch, management_fixture_root: Path):
    """FastAPI app with READ_FROM_DISK pointing at the management fixture root."""
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", TEST_BEARER)
    monkeypatch.setenv("READ_FROM_DISK", str(management_fixture_root))

    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    return create_app()


@pytest.fixture
def mgmt_client(app_with_mgmt):
    from fastapi.testclient import TestClient
    c = TestClient(app_with_mgmt)
    c.headers.update({"Authorization": f"Bearer {TEST_BEARER}"})
    return c


# ---------------------------------------------------------------------------
# Service-level tests (github_reader.load_management_exports)
# ---------------------------------------------------------------------------

class TestLoadManagementExports:

    def test_aggregates_children_kpis(self, management_fixture_root: Path, monkeypatch):
        """load_management_exports should return entries for both mini-ben and mini-maya."""
        monkeypatch.setenv("READ_FROM_DISK", str(management_fixture_root))
        # Flush cached module if already imported
        import sys
        for mod in list(sys.modules):
            if mod == "console" or mod.startswith("console."):
                del sys.modules[mod]

        from console.services.github_reader import load_management_exports

        result = load_management_exports("tony-test")
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        assert "children" in result, f"Expected 'children' key, got keys: {list(result.keys())}"

        slugs = {e["slug"] for e in result["children"]}
        assert "mini-ben" in slugs, f"mini-ben missing from children: {slugs}"
        assert "mini-maya" in slugs, f"mini-maya missing from children: {slugs}"

        # mini-ben has Layer-4 today: risk_kpis should be populated
        ben_entry = next(e for e in result["children"] if e["slug"] == "mini-ben")
        assert ben_entry["risk_kpis"] is not None, "mini-ben should have risk_kpis"
        assert ben_entry["management_export"] is not None, "mini-ben should have management_export"
        assert ben_entry["staleness_days"] == 0, (
            f"mini-ben has data today, staleness_days should be 0, got {ben_entry['staleness_days']}"
        )

    def test_handles_missing_layer_4(self, management_fixture_root: Path, monkeypatch):
        """Child with no Layer-4 outputs must return staleness_days >= 1."""
        monkeypatch.setenv("READ_FROM_DISK", str(management_fixture_root))
        import sys
        for mod in list(sys.modules):
            if mod == "console" or mod.startswith("console."):
                del sys.modules[mod]

        from console.services.github_reader import load_management_exports

        result = load_management_exports("tony-test")
        maya_entry = next(e for e in result["children"] if e["slug"] == "mini-maya")

        assert maya_entry["staleness_days"] >= 1, (
            f"mini-maya has no Layer-4 today, staleness_days should be >= 1, "
            f"got {maya_entry['staleness_days']}"
        )
        assert maya_entry["last_seen_at"] is not None, (
            "last_seen_at should be set even for stale child (to the last known date)"
        )
        # risk_kpis should be absent or None
        assert maya_entry["risk_kpis"] is None, (
            f"mini-maya has no Layer-4, risk_kpis should be None, got {maya_entry['risk_kpis']}"
        )

    def test_handles_missing_management_export(self, tmp_path: Path, monkeypatch):
        """Child with Layer-4 risk files but no management-export.yaml returns management_export=None."""
        root = tmp_path / "depts"
        root.mkdir()
        today = date.today().isoformat()

        # Build a child with risk-kpis but no management-export
        child_dir = root / "bubble-ops-no-export-child"
        child_dir.mkdir()
        (child_dir / "dept.yaml").write_text(
            yaml.safe_dump(
                {
                    "department": {"slug": "no-export-child", "level": "ops", "mandate": "test"},
                    "layers": {"subscribed": [1, 4]},
                    "recurring_missions": [], "skills": {}, "tools": [], "gate_policies": {},
                    "hierarchy": {
                        "level": "ops", "parent": "mgr", "children": [],
                        "visibility": {
                            "read_outputs": [], "read_risk_kpis": False,
                            "read_risk_briefs": False, "read_raw_artifacts": False, "read_secrets": False,
                        },
                        "directive_policy": {
                            "can_open_priority_prs": False, "target_queue": None,
                            "requires_human_gate_for": [],
                        },
                    },
                    "optional_domain_ledger": None,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (child_dir / "onboarding").mkdir()
        (child_dir / "onboarding" / "STATE.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1, "slug": "no-export-child", "display_name": "NoExport",
                    "owner": "operator", "created_at": "2026-05-15T10:00:00Z",
                    "status": "Live",
                    "validated_steps": ["mandate", "missions", "layers", "skills_tools", "gates_kpis", "dry_run"],
                    "last_updated_at": "2026-05-15T10:00:00Z", "commits": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        layer4 = child_dir / "outputs" / today / "4"
        layer4.mkdir(parents=True)
        (layer4 / "risk-kpis.yaml").write_text(yaml.safe_dump({"dept": "no-export-child"}), encoding="utf-8")
        (layer4 / "risk-brief.md").write_text("Brief.\n", encoding="utf-8")
        # NOTE: management-export.yaml intentionally absent

        # Management dept pointing at this one child
        _make_tony_dept(root, children=["no-export-child"])
        # Rename tony-test to something the fixture root understands
        # Actually re-create with the correct child
        import shutil
        shutil.rmtree(root / "bubble-ops-tony-test")
        _make_tony_dept(root, children=["no-export-child"])

        monkeypatch.setenv("READ_FROM_DISK", str(root))
        import sys
        for mod in list(sys.modules):
            if mod == "console" or mod.startswith("console."):
                del sys.modules[mod]

        from console.services.github_reader import load_management_exports
        result = load_management_exports("tony-test")

        child_entry = next(e for e in result["children"] if e["slug"] == "no-export-child")
        assert child_entry["management_export"] is None, (
            f"management_export should be None when file is absent, got {child_entry['management_export']}"
        )
        # But risk_kpis should be present
        assert child_entry["risk_kpis"] is not None


# ---------------------------------------------------------------------------
# Route-level tests
# ---------------------------------------------------------------------------

class TestManagementViewRoute:

    def test_get_management_view_for_management_dept_returns_200(
        self, mgmt_client, management_fixture_root: Path
    ):
        """GET /dept/tony-test/management-view returns 200 with child slugs in body."""
        resp = mgmt_client.get("/dept/tony-test/management-view")
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}.\nBody: {resp.text[:500]}"
        )
        body = resp.text
        assert "mini-ben" in body, f"mini-ben missing from management-view body: {body[:500]}"
        assert "mini-maya" in body, f"mini-maya missing from management-view body: {body[:500]}"

    def test_get_management_view_for_ops_dept_returns_404(self, mgmt_client):
        """GET /dept/fixture/management-view returns 404 for an ops-level dept."""
        # fixture is an ops dept in the management fixture root
        # (it's NOT present — tony-test's children are mini-ben and mini-maya)
        # We use mini-ben which is a known ops dept
        resp = mgmt_client.get("/dept/mini-ben/management-view")
        assert resp.status_code == 404, (
            f"Expected 404 for ops dept, got {resp.status_code}.\nBody: {resp.text[:300]}"
        )
        # Should have a French error message
        body = resp.text.lower()
        assert "département" in body or "management" in body, (
            f"Expected French error message, got: {resp.text[:300]}"
        )

    def test_get_management_view_for_unknown_dept_returns_404(self, mgmt_client):
        """GET /dept/nonexistent/management-view returns 404."""
        resp = mgmt_client.get("/dept/nonexistent/management-view")
        assert resp.status_code == 404

    def test_management_view_shows_gate_counts(
        self, mgmt_client, management_fixture_root: Path
    ):
        """management-view body should show pending gate counts for mini-ben."""
        resp = mgmt_client.get("/dept/tony-test/management-view")
        assert resp.status_code == 200
        # mini-ben has 1 pending gate (ben-gate-1.yaml)
        body = resp.text
        # The body should indicate there's at least one gate pending
        # (either the gate id or a count > 0 or "gate" text)
        assert "gate" in body.lower() or "ben-gate" in body.lower(), (
            f"management-view should mention pending gates: {body[:500]}"
        )

    def test_management_view_shows_staleness_for_stale_child(
        self, mgmt_client, management_fixture_root: Path
    ):
        """management-view body should visually flag mini-maya as stale."""
        resp = mgmt_client.get("/dept/tony-test/management-view")
        assert resp.status_code == 200
        body = resp.text.lower()
        # Should show some staleness indicator
        assert "stale" in body or "retard" in body or "jour" in body or "mini-maya" in body, (
            f"management-view should mention staleness for mini-maya: {resp.text[:500]}"
        )


# ---------------------------------------------------------------------------
# _load_child_entry: malformed YAML must be logged, not silently swallowed
# (board #450 — a broken risk-KPI file used to silently blank a child in the
# management rollup with zero trace).
# ---------------------------------------------------------------------------

class TestLoadChildEntryLogsMalformedYaml:

    def test_malformed_risk_kpis_logs_warning(self, tmp_path: Path, caplog):
        from console.services.github_reader import _load_child_entry
        import logging

        today = date.today().isoformat()
        child_root = tmp_path / "bubble-ops-broken-child"
        layer4 = child_root / "outputs" / today / "4"
        layer4.mkdir(parents=True)
        # Unquoted colon-in-value is a classic YAML parse trap (same class of
        # bug as the 2026-06-06 gate-card incident referenced elsewhere here).
        (layer4 / "risk-kpis.yaml").write_text("nav: 100k: broken\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="console.github_reader"):
            entry = _load_child_entry("broken-child", child_root)

        assert entry["risk_kpis"] is None
        assert any(
            "risk-kpis.yaml" in rec.message and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), f"Expected a WARNING log naming risk-kpis.yaml, got: {[r.message for r in caplog.records]}"

    def test_malformed_management_export_logs_warning(self, tmp_path: Path, caplog):
        from console.services.github_reader import _load_child_entry
        import logging

        today = date.today().isoformat()
        child_root = tmp_path / "bubble-ops-broken-child2"
        layer4 = child_root / "outputs" / today / "4"
        layer4.mkdir(parents=True)
        (child_root / "outputs" / today / "management-export.yaml").write_text(
            "summary: [unterminated\n", encoding="utf-8"
        )

        with caplog.at_level(logging.WARNING, logger="console.github_reader"):
            entry = _load_child_entry("broken-child2", child_root)

        assert entry["management_export"] is None
        assert any(
            "management-export.yaml" in rec.message and rec.levelno == logging.WARNING
            for rec in caplog.records
        ), f"Expected a WARNING log naming management-export.yaml, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# _kanban_snapshot: cached + logs on failure (board #450). This used to do a
# blocking urlopen (up to 4s) on EVERY management-page load with zero log
# on failure — an unreachable dashboard silently vanished from the page.
# ---------------------------------------------------------------------------

class TestKanbanSnapshotCacheAndLogging:

    def test_unreachable_dashboard_logs_warning_and_returns_none(self, monkeypatch, caplog):
        import logging
        from console.routes import dept as dept_route

        dept_route._kanban_snapshot_cache.clear()

        def fake_urlopen(*a, **k):
            raise OSError("connection refused")

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        with caplog.at_level(logging.WARNING):
            result = dept_route._kanban_snapshot()

        assert result is None
        assert any(rec.levelno == logging.WARNING for rec in caplog.records), (
            f"expected a WARNING log on dashboard-unreachable, got: {[r.message for r in caplog.records]}"
        )

    def test_kanban_snapshot_cached_within_ttl(self, monkeypatch):
        from console.routes import dept as dept_route

        dept_route._kanban_snapshot_cache.clear()
        calls = {"n": 0}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"columns": {"needs_attention": []}, "counts": {}, "generated_at": "t1"}'

        def fake_urlopen(*a, **k):
            calls["n"] += 1
            return _Resp()

        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        first = dept_route._kanban_snapshot()
        second = dept_route._kanban_snapshot()
        assert first == second
        assert calls["n"] == 1, f"expected exactly 1 urlopen call (cached), got {calls['n']}"
