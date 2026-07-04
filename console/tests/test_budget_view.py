"""
test_budget_view.py — per-dept / per-agent BUDGET vs SPEND (board #524d).

Covers:
  · cost_tracker.mission_budget_total  — Σ budget_usd over recurring_missions[],
    None when nothing carries a budget (graceful "budget non défini").
  · cost_tracker.budget_status         — green/amber/red level + pct + over-budget.
  · cost_tracker.spent_by_dept         — roll per-agent report up to dept slug.
  · home._dept_budgets                 — home Coûts rows incl no-budget + >100%.
  · /costs                             — by-agent budget column + fleet total +
                                         graceful degradation with no budget.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from console.services import cost_tracker


# ── mission_budget_total ────────────────────────────────────────────────
def test_mission_budget_total_sums_recurring_missions():
    doc = {"recurring_missions": [
        {"id": "m1", "budget_usd": 10},
        {"id": "m2", "budget_usd": 5.5},
        {"id": "m3"},  # no budget → skipped
    ]}
    assert cost_tracker.mission_budget_total(doc) == 15.5


def test_mission_budget_total_across_layers_and_missions_keys():
    doc = {
        "layers": [{"layer": 1, "budget_usd": 2}],
        "missions": [{"id": "x", "budget_usd": 3}],
    }
    assert cost_tracker.mission_budget_total(doc) == 5.0


def test_mission_budget_total_none_when_no_budget_anywhere():
    """No budget_usd anywhere → None (so the UI shows 'budget non défini',
    NOT a misleading $0 with a full/empty bar)."""
    doc = {"recurring_missions": [{"id": "m1"}, {"id": "m2"}]}
    assert cost_tracker.mission_budget_total(doc) is None


def test_mission_budget_total_malformed_is_none():
    assert cost_tracker.mission_budget_total(None) is None
    assert cost_tracker.mission_budget_total("not a dict") is None
    assert cost_tracker.mission_budget_total({}) is None


def test_mission_budget_total_ignores_bool_budget():
    """A YAML `budget_usd: true` must not be counted as 1.0."""
    doc = {"recurring_missions": [{"id": "m", "budget_usd": True}]}
    assert cost_tracker.mission_budget_total(doc) is None


# ── budget_status thresholds ─────────────────────────────────────────────
def test_budget_status_green_under_80():
    s = cost_tracker.budget_status(50, 100)
    assert s["defined"] is True
    assert s["level"] == "ok"
    assert s["pct"] == 50.0


def test_budget_status_amber_80_to_100():
    assert cost_tracker.budget_status(90, 100)["level"] == "warn"
    assert cost_tracker.budget_status(100, 100)["level"] == "warn"


def test_budget_status_red_over_100():
    s = cost_tracker.budget_status(150, 100)
    assert s["level"] == "over"
    assert s["pct"] == 150.0


def test_budget_status_undefined_no_divzero():
    s = cost_tracker.budget_status(42, None)
    assert s["defined"] is False
    assert s["pct"] is None
    assert s["budget"] is None
    assert s["spent"] == 42.0
    # zero budget must not divide-by-zero either
    assert cost_tracker.budget_status(10, 0)["defined"] is False


# ── spent_by_dept roll-up ────────────────────────────────────────────────
def test_spent_by_dept_maps_agent_keys_to_slugs():
    report = {"agents": {
        "ben": {"week": {"cost": 3.0}},
        "miranda (jade-mac)": {"week": {"cost": 2.0}},  # → content slug via alias+suffix strip
        "wiki-compile": {"week": {"cost": 1.0}},         # -p cron, no dept
    }}
    m = cost_tracker.spent_by_dept(report, span="week")
    assert m["ben"] == 3.0
    assert m["content"] == 2.0
    assert m["wiki-compile"] == 1.0  # kept in map but simply won't match a dept


def test_spent_by_dept_empty_report():
    assert cost_tracker.spent_by_dept({}, "week") == {}
    assert cost_tracker.spent_by_dept({"agents": None}, "week") == {}


# ── home._dept_budgets: no-budget + over-budget cases ────────────────────
class _Dept:
    def __init__(self, slug, name, live=True):
        self.slug = slug
        self.display_name = name
        self._live = live

    @property
    def is_live(self):
        return self._live


def test_dept_budgets_no_budget_graceful(monkeypatch):
    from console.routes import home

    monkeypatch.setattr(cost_tracker, "build_report",
                        lambda refresh=False: {"agents": {"alpha": {"week": {"cost": 5.0}}}})
    # dept.yaml with NO budget_usd → budget None → "budget non défini"
    monkeypatch.setattr(home.github_reader, "load_dept_yaml",
                        lambda slug: {"recurring_missions": [{"id": "m"}]})
    cols = [{"dept": _Dept("alpha", "Alpha")}]
    rows = home._dept_budgets(cols)
    assert len(rows) == 1
    assert rows[0]["slug"] == "alpha"
    assert rows[0]["defined"] is False
    assert rows[0]["spent"] == 5.0
    assert rows[0]["pct"] is None


def test_dept_budgets_over_budget_case(monkeypatch):
    from console.routes import home

    monkeypatch.setattr(cost_tracker, "build_report",
                        lambda refresh=False: {"agents": {"beta": {"week": {"cost": 30.0}}}})
    monkeypatch.setattr(home.github_reader, "load_dept_yaml",
                        lambda slug: {"recurring_missions": [{"id": "m", "budget_usd": 20}]})
    cols = [{"dept": _Dept("beta", "Beta")}]
    rows = home._dept_budgets(cols)
    assert rows[0]["defined"] is True
    assert rows[0]["level"] == "over"
    assert rows[0]["pct"] == 150.0
    assert rows[0]["budget"] == 20.0


def test_dept_budgets_degrades_to_empty_on_report_failure(monkeypatch):
    from console.routes import home

    def _boom(refresh=False):
        raise RuntimeError("cost scan blew up")
    monkeypatch.setattr(cost_tracker, "build_report", _boom)
    rows = home._dept_budgets([{"dept": _Dept("x", "X")}])
    assert rows == []  # never raises → home page never 500s


def test_dept_budgets_skips_non_live(monkeypatch):
    from console.routes import home

    monkeypatch.setattr(cost_tracker, "build_report",
                        lambda refresh=False: {"agents": {}})
    monkeypatch.setattr(home.github_reader, "load_dept_yaml", lambda slug: None)
    cols = [{"dept": _Dept("live1", "L", live=True)},
            {"dept": _Dept("eclore1", "E", live=False)}]
    rows = home._dept_budgets(cols)
    assert [r["slug"] for r in rows] == ["live1"]


# ── /costs page: by-agent budget column + fleet total + degradation ──────
def _write_session(proj: Path, dirname: str, model: str, u: dict) -> None:
    import json
    d = proj / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant", "model": model, "usage": u}}) + "\n",
        encoding="utf-8",
    )


def _build_costs_client(monkeypatch, tmp_path, budget_usd):
    """Build a /costs TestClient whose cost report attributes ONE session to the
    'fixture' dept, with dept.yaml carrying `budget_usd` (None → no budget key).

    We set HOME to a temp tree so the freshly-imported cost_tracker resolves its
    PROJECTS_DIR/CACHE from THERE (patching the old module reference is lost when
    console.* is re-imported below), and READ_FROM_DISK for the dept.yaml.
    """
    home = tmp_path / "home"
    proj = home / ".claude" / "projects"
    proj.mkdir(parents=True)
    _write_session(proj, "-home-claude-agents-bubble-ops-fixture",
                   "claude-sonnet-4-6",
                   {"input_tokens": 1_000_000, "output_tokens": 0,
                    "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    root = tmp_path / "depts"
    (root / "bubble-ops-fixture").mkdir(parents=True)
    mission = {"id": "m1"}
    if budget_usd is not None:
        mission["budget_usd"] = budget_usd
    (root / "bubble-ops-fixture" / "dept.yaml").write_text(
        yaml.safe_dump({"department": {"slug": "fixture", "level": "ops"},
                        "recurring_missions": [mission]}, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.setenv("READ_FROM_DISK", str(root))
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})
    return c


def test_costs_page_shows_budget_column_and_degrades(monkeypatch, tmp_path):
    """With NO budget_usd in the fixture dept.yaml, /costs must still render the
    Budget column + fleet-total line, both saying 'non défini' — graceful."""
    c = _build_costs_client(monkeypatch, tmp_path, budget_usd=None)
    r = c.get("/costs")
    assert r.status_code == 200
    body = r.text
    assert "Budget&nbsp;(7j)" in body            # the new column header
    assert "non défini" in body                  # graceful: no budget_usd set
    assert "Budget cette semaine" in body        # the fleet-total line
    # the agent row exists but has no bar (undefined budget)
    assert "budget-bar-fill--" not in body


def test_costs_page_shows_populated_budget_bar(monkeypatch, tmp_path):
    """With a budget_usd SET on the fixture dept.yaml, /costs renders a real
    spent-vs-budget % (over-budget here → red 'over' level) + a fleet total.
    1M input sonnet tokens = $3 real cost; budget $1 → 300% over."""
    c = _build_costs_client(monkeypatch, tmp_path, budget_usd=1)
    r = c.get("/costs")
    assert r.status_code == 200
    body = r.text
    assert "budget-bar-fill--over" in body        # red bar (spent ≫ budget)
    assert "budget-pct--over" in body
    assert "de budget" in body                     # fleet-total copy
