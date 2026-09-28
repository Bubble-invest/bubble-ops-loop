"""test_checks.py — unit tests for scripts/cockpit_health/checks.py.

Every check imports `console.*` lazily, INSIDE the function body — so these
tests stub `sys.modules["console...."]` with fake objects rather than needing
a real FastAPI/console install. Each fake module is removed in a fixture
teardown so tests never leak state into each other.
"""
from __future__ import annotations

import subprocess
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.cockpit_health import checks  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────

def test_ctx_uses_injected_clock():
    fixed = 1_700_000_000.0
    ctx = checks.Ctx(now_fn=lambda: fixed)
    assert ctx.now_epoch() == fixed
    assert ctx.today_iso() == datetime.fromtimestamp(fixed, tz=timezone.utc).date().isoformat()


def test_days_between():
    assert checks._days_between("2026-09-20", "2026-09-28") == 8
    assert checks._days_between(None, "2026-09-28") is None
    assert checks._days_between("not-a-date", "2026-09-28") is None


def test_scan_latest_graph_data_day_finds_newest(tmp_path):
    root = tmp_path
    today = date.today()
    old_day = (today - timedelta(days=10)).isoformat()
    (root / "outputs" / old_day).mkdir(parents=True)
    (root / "outputs" / old_day / "graph-data.json").write_text("{}")
    found = checks._scan_latest_graph_data_day(root, max_days=30)
    assert found == old_day


def test_scan_latest_graph_data_day_respects_window(tmp_path):
    root = tmp_path
    today = date.today()
    old_day = (today - timedelta(days=10)).isoformat()
    (root / "outputs" / old_day).mkdir(parents=True)
    (root / "outputs" / old_day / "graph-data.json").write_text("{}")
    # A 7-day window (canonical_nav's own) must NOT find a 10-day-old file.
    assert checks._scan_latest_graph_data_day(root, max_days=7) is None


def test_newest_mtime_picks_the_max(tmp_path):
    root = tmp_path
    (root / "outputs" / "2026-09-20").mkdir(parents=True)
    old_file = root / "outputs" / "2026-09-20" / "heartbeat.log"
    old_file.write_text("x")
    (root / "outputs" / "2026-09-28").mkdir(parents=True)
    new_file = root / "outputs" / "2026-09-28" / "heartbeat.log"
    new_file.write_text("y")
    import os, time
    os.utime(old_file, (1_000, 1_000))
    os.utime(new_file, (2_000_000, 2_000_000))
    newest = checks._newest_mtime(root, ["outputs/*/heartbeat.log"])
    assert newest == 2_000_000


def test_newest_mtime_none_when_nothing_matches(tmp_path):
    assert checks._newest_mtime(tmp_path, ["outputs/*/heartbeat.log"]) is None


def test_sum_today_cost_reads_totals_today():
    report = {"totals": {"today": {"cost": 12.34}, "week": {"cost": 99.0}}}
    assert checks._sum_today_cost(report) == 12.34


def test_sum_today_cost_degrades_on_bad_shape():
    assert checks._sum_today_cost({}) == 0.0
    assert checks._sum_today_cost(None) == 0.0
    assert checks._sum_today_cost({"totals": {"today": {"cost": "not-a-number"}}}) == 0.0


def test_gh_open_issue_count_parses_json(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(cmd, 0, stdout='[{"number":1},{"number":2}]', stderr="")
    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    count, err = checks._gh_open_issue_count("Bubble-invest/bubble-ops-board")
    assert count == 2
    assert err is None


def test_gh_open_issue_count_degrades_on_failure(monkeypatch):
    def fake_run(cmd, capture_output, text, timeout, check):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not authenticated")
    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    count, err = checks._gh_open_issue_count("Bubble-invest/bubble-ops-board")
    assert count is None
    assert "not authenticated" in err


def test_gh_open_issue_count_never_raises_on_exec_failure(monkeypatch):
    def fake_run(*a, **k):
        raise FileNotFoundError("gh: command not found")
    monkeypatch.setattr(checks.subprocess, "run", fake_run)
    count, err = checks._gh_open_issue_count("Bubble-invest/bubble-ops-board")
    assert count is None
    assert "gh" in err


# ─────────────────────────────────────────────────────────────────────────
# Fakes for client + console.* modules
# ─────────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}

    def json(self):
        return self._json_body


class FakeClient:
    """Minimal stand-in for the FastAPI TestClient — a dict of path -> FakeResponse."""

    def __init__(self, routes):
        self._routes = routes
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        if path not in self._routes:
            raise AssertionError(f"FakeClient: unexpected GET {path}")
        return self._routes[path]


@pytest.fixture
def stub_module(monkeypatch):
    """stub_module("console.services.cost_tracker", build_report=fn) installs
    a fake module into sys.modules and tears it down after the test."""
    installed = []

    def _install(dotted_name, **attrs):
        mod = types.ModuleType(dotted_name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[dotted_name] = mod
        installed.append(dotted_name)
        # Make sure a parent package lookup (e.g. `from console.services import
        # cost_tracker`) also finds it as an attribute of the parent package.
        parent_name, _, child = dotted_name.rpartition(".")
        if parent_name:
            parent = sys.modules.get(parent_name) or _install(parent_name)
            setattr(parent, child, mod)
        return mod

    yield _install
    for name in installed:
        sys.modules.pop(name, None)


# ─────────────────────────────────────────────────────────────────────────
# check_costs_freshness
# ─────────────────────────────────────────────────────────────────────────

def test_costs_freshness_ok_when_served_matches_fresh(stub_module):
    served = {"totals": {"today": {"cost": 10.0}}}
    fresh = {"totals": {"today": {"cost": 10.0}}}
    stub_module("console.services.cost_tracker", build_report=lambda refresh: fresh)
    client = FakeClient({"/costs.json": FakeResponse(200, served)})
    result = checks.check_costs_freshness(client, checks.Ctx())
    assert result["hard_inconsistency"] is False
    assert result["consistent"] is True


def test_costs_freshness_hard_when_served_zero_but_fresh_has_spend(stub_module):
    served = {"totals": {"today": {"cost": 0.0}}}
    fresh = {"totals": {"today": {"cost": 42.0}}}
    stub_module("console.services.cost_tracker", build_report=lambda refresh: fresh)
    client = FakeClient({"/costs.json": FakeResponse(200, served)})
    result = checks.check_costs_freshness(client, checks.Ctx())
    assert result["hard_inconsistency"] is True
    assert "$0" in result["reason"] or "0" in result["reason"]


def test_costs_freshness_hard_on_non_200(stub_module):
    stub_module("console.services.cost_tracker", build_report=lambda refresh: {})
    client = FakeClient({"/costs.json": FakeResponse(500, {})})
    result = checks.check_costs_freshness(client, checks.Ctx())
    assert result["hard_inconsistency"] is True


def test_costs_freshness_skips_without_client():
    result = checks.check_costs_freshness(None, checks.Ctx())
    assert result["consistent"] is None
    assert result["hard_inconsistency"] is False


# ─────────────────────────────────────────────────────────────────────────
# check_kanban_open_count
# ─────────────────────────────────────────────────────────────────────────

def test_kanban_open_count_consistent_within_tolerance(stub_module, monkeypatch):
    stub_module("console.routes.kanban", _fetch_issues=lambda: ([{}] * 10, None))
    monkeypatch.setattr(checks, "_gh_open_issue_count", lambda repo: (10, None))
    client = FakeClient({"/kanban": FakeResponse(200)})
    result = checks.check_kanban_open_count(client, checks.Ctx())
    assert result["hard_inconsistency"] is False


def test_kanban_open_count_hard_on_large_drift(stub_module, monkeypatch):
    stub_module("console.routes.kanban", _fetch_issues=lambda: ([{}] * 2, None))
    monkeypatch.setattr(checks, "_gh_open_issue_count", lambda repo: (40, None))
    client = FakeClient({"/kanban": FakeResponse(200)})
    result = checks.check_kanban_open_count(client, checks.Ctx())
    assert result["hard_inconsistency"] is True


def test_kanban_open_count_hard_on_served_fetch_error(stub_module, monkeypatch):
    stub_module("console.routes.kanban", _fetch_issues=lambda: ([], "no board token"))
    monkeypatch.setattr(checks, "_gh_open_issue_count", lambda repo: (5, None))
    client = FakeClient({"/kanban": FakeResponse(200)})
    result = checks.check_kanban_open_count(client, checks.Ctx())
    assert result["hard_inconsistency"] is True
    assert "no board token" in result["reason"]


# ─────────────────────────────────────────────────────────────────────────
# check_nav_freshness
# ─────────────────────────────────────────────────────────────────────────

class FakeDept:
    def __init__(self, slug):
        self.slug = slug


def test_nav_freshness_hard_when_stale_beyond_threshold(stub_module, tmp_path):
    root = tmp_path / "ben"
    root.mkdir()
    stale_day = (date.today() - timedelta(days=5)).isoformat()
    (root / "outputs" / stale_day).mkdir(parents=True)
    (root / "outputs" / stale_day / "graph-data.json").write_text("{}")

    stub_module(
        "console.services.dept_registry",
        live_departments=lambda: [FakeDept("ben")],
        runtime_repo_path=lambda slug: root if slug == "ben" else None,
    )
    stub_module(
        "console.services.canonical_nav",
        canonical_nav=lambda slug: {
            "nav": 100000.0, "since_rebase_pct": 1.0, "as_of": stale_day,
            "is_stale": True, "source": "graph-data.json",
        },
    )
    client = FakeClient({
        "/dept/ben": FakeResponse(200),
        "/dept/ben/portfolio": FakeResponse(200),
    })
    results = checks.check_nav_freshness(client, checks.Ctx())
    assert len(results) == 1
    assert results[0]["hard_inconsistency"] is True
    assert "5d old" in results[0]["reason"] or "5d" in results[0]["reason"]


def test_nav_freshness_hard_when_empty_but_stale_data_exists(stub_module, tmp_path):
    root = tmp_path / "ben"
    root.mkdir()
    old_day = (date.today() - timedelta(days=20)).isoformat()
    (root / "outputs" / old_day).mkdir(parents=True)
    (root / "outputs" / old_day / "graph-data.json").write_text("{}")

    stub_module(
        "console.services.dept_registry",
        live_departments=lambda: [FakeDept("ben")],
        runtime_repo_path=lambda slug: root,
    )
    stub_module(
        "console.services.canonical_nav",
        canonical_nav=lambda slug: {
            "nav": None, "since_rebase_pct": None, "as_of": None,
            "is_stale": False, "source": "graph-data.json",
        },
    )
    results = checks.check_nav_freshness(None, checks.Ctx())
    assert len(results) == 1
    assert results[0]["hard_inconsistency"] is True
    assert "empty state" in results[0]["reason"]


def test_nav_freshness_skips_non_fund_depts(stub_module, tmp_path):
    root = tmp_path / "maya"
    root.mkdir()  # no outputs/ dir at all
    stub_module(
        "console.services.dept_registry",
        live_departments=lambda: [FakeDept("maya")],
        runtime_repo_path=lambda slug: root,
    )
    stub_module(
        "console.services.canonical_nav",
        canonical_nav=lambda slug: {
            "nav": None, "since_rebase_pct": None, "as_of": None,
            "is_stale": False, "source": "graph-data.json",
        },
    )
    results = checks.check_nav_freshness(None, checks.Ctx())
    assert results == []


def test_nav_freshness_fresh_is_consistent(stub_module, tmp_path):
    root = tmp_path / "ben"
    root.mkdir()
    today = date.today().isoformat()
    (root / "outputs" / today).mkdir(parents=True)
    (root / "outputs" / today / "graph-data.json").write_text("{}")
    stub_module(
        "console.services.dept_registry",
        live_departments=lambda: [FakeDept("ben")],
        runtime_repo_path=lambda slug: root,
    )
    stub_module(
        "console.services.canonical_nav",
        canonical_nav=lambda slug: {
            "nav": 200000.0, "since_rebase_pct": 2.0, "as_of": today,
            "is_stale": False, "source": "graph-data.json",
        },
    )
    client = FakeClient({
        "/dept/ben": FakeResponse(200),
        "/dept/ben/portfolio": FakeResponse(200),
    })
    results = checks.check_nav_freshness(client, checks.Ctx())
    assert len(results) == 1
    assert results[0]["hard_inconsistency"] is False
    assert results[0]["consistent"] is True


# ─────────────────────────────────────────────────────────────────────────
# check_dept_heartbeat_age
# ─────────────────────────────────────────────────────────────────────────

def test_dept_heartbeat_age_hard_when_stale_but_claimed_alive(stub_module, tmp_path):
    root = tmp_path / "ben"
    (root / "outputs" / "2026-09-01").mkdir(parents=True)
    hb = root / "outputs" / "2026-09-01" / "heartbeat.log"
    hb.write_text("x")
    import os
    very_old = 1_000_000  # epoch seconds, long ago
    os.utime(hb, (very_old, very_old))

    stub_module(
        "console.services.dept_registry",
        live_departments=lambda: [FakeDept("ben")],
        repo_path=lambda slug: root,
    )
    now = 1_000_000 + checks._DEPT_STALE_HOURS_THRESHOLD * 3600 + 10_000
    client = FakeClient({
        "/health/graph.json": FakeResponse(200, {
            "nodes": [{"id": "dept:ben", "pulse": {"alive": True, "age_human": "old"}}]
        }),
    })
    result = checks.check_dept_heartbeat_age(client, checks.Ctx(now_fn=lambda: now))
    assert result["hard_inconsistency"] is True
    assert "ben" in result["reason"]


def test_dept_heartbeat_age_consistent_when_claimed_dead(stub_module, tmp_path):
    root = tmp_path / "ben"
    (root / "outputs" / "2026-09-01").mkdir(parents=True)
    hb = root / "outputs" / "2026-09-01" / "heartbeat.log"
    hb.write_text("x")
    import os
    very_old = 1_000_000
    os.utime(hb, (very_old, very_old))

    stub_module(
        "console.services.dept_registry",
        live_departments=lambda: [FakeDept("ben")],
        repo_path=lambda slug: root,
    )
    now = 1_000_000 + checks._DEPT_STALE_HOURS_THRESHOLD * 3600 + 10_000
    client = FakeClient({
        "/health/graph.json": FakeResponse(200, {
            "nodes": [{"id": "dept:ben", "pulse": {"alive": False, "age_human": "old"}}]
        }),
    })
    result = checks.check_dept_heartbeat_age(client, checks.Ctx(now_fn=lambda: now))
    # Console already agrees it's dead — no inconsistency, nothing to flag.
    assert result["hard_inconsistency"] is False
