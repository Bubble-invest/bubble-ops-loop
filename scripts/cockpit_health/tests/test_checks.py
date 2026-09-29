"""test_checks.py — unit tests for scripts/cockpit_health/checks.py.

Every check imports `console.*` lazily, INSIDE the function body — so these
tests stub `sys.modules["console...."]` with fake objects rather than needing
a real FastAPI/console install. Each fake module is removed in a fixture
teardown so tests never leak state into each other.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
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


class _FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_github_open_issue_count_uses_token_file_and_rest(tmp_path, monkeypatch):
    token_file = tmp_path / "board-token"
    token_file.write_text("secret-value")
    seen = {}

    def fake_urlopen(req, timeout):
        seen["authorization"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        return _FakeHTTPResponse(json.dumps([
            {"number": 1}, {"number": 2},
            {"number": 3, "pull_request": {}},
        ]).encode())

    monkeypatch.setattr(checks.urllib.request, "urlopen", fake_urlopen)
    count, err = checks._github_open_issue_count(
        "Bubble-invest/bubble-ops-board", token_file=token_file)
    assert count == 2
    assert err is None
    assert seen["authorization"] == "Bearer secret-value"
    assert "state=open" in seen["url"]


def test_github_open_issue_count_degrades_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    count, err = checks._github_open_issue_count(
        "Bubble-invest/bubble-ops-board", token_file=tmp_path / "missing")
    assert count is None
    assert "token" in err


def test_github_open_issue_count_never_raises_on_rest_failure(tmp_path, monkeypatch):
    token_file = tmp_path / "board-token"
    token_file.write_text("secret-value")
    monkeypatch.setattr(checks.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")))
    count, err = checks._github_open_issue_count(
        "Bubble-invest/bubble-ops-board", token_file=token_file)
    assert count is None
    assert "timed out" in err


def test_recent_transcript_facts_counts_direct_homes_and_mac_mirrors(tmp_path):
    now = 2_000_000.0
    homes = tmp_path / "home"
    mirrors = tmp_path / "projects"
    direct = homes / "agent-ben" / ".claude" / "projects" / "-srv-agents-ben" / "a.jsonl"
    mac = mirrors / "_mac-jade" / "-Users-jade-claude-workspaces-bubble-ops-content" / "b.jsonl"
    old = homes / "agent-maya" / ".claude" / "projects" / "old.jsonl"
    for path in (direct, mac, old):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    os.utime(direct, (now - 60, now - 60))
    os.utime(mac, (now - 120, now - 120))
    os.utime(old, (now - 25 * 3600, now - 25 * 3600))

    facts = checks._recent_transcript_facts(
        now, agent_home_root=homes, mirror_root=mirrors)

    assert facts["total_recent_transcripts"] == 2
    assert facts["agents"]["ben"]["sources"] == ["agent_home"]
    assert facts["agents"]["content"]["sources"] == ["mac_mirror"]
    assert "maya" not in facts["agents"]


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

def test_costs_freshness_ok_when_recent_transcript_has_served_session(monkeypatch):
    served = {
        "agents": {"ben": {
            "today": {"runs": 1, "cost": 1.0},
            "week": {"runs": 2, "cost": 10.0},
        }},
        "totals": {
            "today": {"runs": 1, "cost": 1.0},
            "week": {"runs": 2, "cost": 10.0},
        },
    }
    monkeypatch.setattr(checks, "_recent_transcript_facts", lambda now: {
        "window_hours": 24.0, "total_recent_transcripts": 1,
        "agents": {"ben": {"recent_transcript_count": 1}},
    })
    client = FakeClient({"/costs.json": FakeResponse(200, served)})
    result = checks.check_costs_freshness(client, checks.Ctx())
    assert result["hard_inconsistency"] is False
    assert result["consistent"] is True


def test_costs_freshness_hard_when_recent_transcript_has_no_served_session(monkeypatch):
    served = {"agents": {}, "totals": {
        "today": {"runs": 0, "cost": 0.0},
        "week": {"runs": 0, "cost": 0.0},
    }}
    monkeypatch.setattr(checks, "_recent_transcript_facts", lambda now: {
        "window_hours": 24.0, "total_recent_transcripts": 3,
        "agents": {"ben": {"recent_transcript_count": 3}},
    })
    client = FakeClient({"/costs.json": FakeResponse(200, served)})
    result = checks.check_costs_freshness(client, checks.Ctx())
    assert result["hard_inconsistency"] is True
    assert "3 transcript" in result["reason"]
    assert "0 sessions" in result["reason"]


def test_costs_freshness_hard_on_non_200(monkeypatch):
    monkeypatch.setattr(checks, "_recent_transcript_facts", lambda now: {
        "window_hours": 24.0, "total_recent_transcripts": 0, "agents": {},
    })
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
    monkeypatch.setattr(checks, "_github_open_issue_count", lambda repo: (10, None))
    client = FakeClient({"/kanban": FakeResponse(200)})
    result = checks.check_kanban_open_count(client, checks.Ctx())
    assert result["hard_inconsistency"] is False


def test_kanban_open_count_hard_on_large_drift(stub_module, monkeypatch):
    stub_module("console.routes.kanban", _fetch_issues=lambda: ([{}] * 2, None))
    monkeypatch.setattr(checks, "_github_open_issue_count", lambda repo: (40, None))
    client = FakeClient({"/kanban": FakeResponse(200)})
    result = checks.check_kanban_open_count(client, checks.Ctx())
    assert result["hard_inconsistency"] is True


def test_kanban_open_count_hard_on_served_fetch_error(stub_module, monkeypatch):
    stub_module("console.routes.kanban", _fetch_issues=lambda: ([], "no board token"))
    monkeypatch.setattr(checks, "_github_open_issue_count", lambda repo: (5, None))
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


def _write_fund_snapshot_dates(root: Path, nav_day: str, exposure_day: str) -> None:
    """Minimal synthetic fund DB: dates only, never real holdings data."""
    db_dir = root / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_dir / "fund.sqlite")
    try:
        con.executescript(
            """
            CREATE TABLE kpi_snapshots (snapshot_at TEXT NOT NULL);
            CREATE TABLE positions (snapshot_at TEXT NOT NULL);
            """
        )
        con.execute("INSERT INTO kpi_snapshots VALUES (?)", (nav_day + "T06:00:00Z",))
        con.execute("INSERT INTO positions VALUES (?)", (exposure_day + "T06:01:00Z",))
        con.commit()
    finally:
        con.close()


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
    root.mkdir()  # no NAV artifacts or fund database
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
    _write_fund_snapshot_dates(root, nav_day=today, exposure_day=today)
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


def test_nav_freshness_resolves_live_ben_via_canonical_runtime_path(stub_module, tmp_path):
    root = tmp_path / "srv" / "agents" / "ben"
    root.mkdir(parents=True)
    today = date.today().isoformat()
    (root / "outputs" / today).mkdir(parents=True)
    (root / "outputs" / today / "graph-data.json").write_text("{}")
    _write_fund_snapshot_dates(root, nav_day=today, exposure_day=today)
    resolved = []
    canonical_calls = []
    stub_module(
        "console.services.dept_registry",
        live_departments=lambda: [FakeDept("ben")],
        runtime_repo_path=lambda slug: resolved.append(slug) or root,
    )
    stub_module(
        "console.services.canonical_nav",
        canonical_nav=lambda slug: canonical_calls.append(slug) or {
            "nav": 200000.0, "since_rebase_pct": 2.0, "as_of": today,
            "is_stale": False, "source": "graph-data.json",
        },
    )

    result = checks.check_nav_freshness(None, checks.Ctx())[0]

    assert resolved == ["ben"]
    assert canonical_calls == ["ben"]
    assert result["source"]["runtime_repo_path"] == str(root)


def test_nav_freshness_records_unreadable_live_dept(stub_module, tmp_path, monkeypatch):
    root = tmp_path / "srv" / "agents" / "claudette"
    root.mkdir(parents=True)
    stub_module(
        "console.services.dept_registry",
        live_departments=lambda: [FakeDept("claudette")],
        runtime_repo_path=lambda slug: root,
    )
    stub_module(
        "console.services.canonical_nav",
        canonical_nav=lambda slug: pytest.fail("unreadable repo must not reach NAV resolver"),
    )
    monkeypatch.setattr(checks.os, "access", lambda path, mode: False)

    result = checks.check_nav_freshness(None, checks.Ctx())[0]

    assert result["id"] == "nav_freshness_claudette"
    assert result["consistent"] is None
    assert result["hard_inconsistency"] is False
    assert "not readable" in result["reason"]


def test_nav_freshness_hard_when_exposure_predates_latest_nav(stub_module, tmp_path):
    """#1614: a page rebuilt today from yesterday's positions is visibly hard."""
    root = tmp_path / "ben"
    root.mkdir()
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (root / "outputs" / today).mkdir(parents=True)
    (root / "outputs" / today / "graph-data.json").write_text("{}")
    _write_fund_snapshot_dates(root, nav_day=today, exposure_day=yesterday)

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

    result = checks.check_nav_freshness(client, checks.Ctx())[0]

    assert result["hard_inconsistency"] is True
    assert result["consistent"] is False
    assert result["observed"]["exposure_as_of"] == yesterday
    assert result["source"]["latest_nav_snapshot_date"] == today
    assert "does not match latest NAV snapshot" in result["reason"]


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
