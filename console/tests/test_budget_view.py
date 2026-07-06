"""
test_budget_view.py — per-dept / per-agent BUDGET vs SPEND (board #524d).

Covers:
  · cost_tracker.mission_budget_total  — Σ budget_usd over recurring_missions[],
    None when nothing carries a budget (graceful "budget non défini"). Still
    used elsewhere (kept for back-compat); NO LONGER the home page's
    denominator — see #550 below.
  · cost_tracker.budget_status         — green/amber/red level + pct + over-budget.
  · cost_tracker.spent_by_dept         — roll per-agent report up to dept slug.
  · home._dept_budgets                 — home Coûts rows, fixed #550: now
    compares week spend to settings.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT (a
    per-dept-slug WEEKLY envelope), not mission_budget_total (a ONE-mission-
    cycle budget) — the old code produced nonsense like tony $185.92/$8=2324%.
  · /costs                             — by-agent budget column + fleet total +
                                         graceful degradation with no budget.
"""
from __future__ import annotations

from pathlib import Path

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


# ── home._dept_budgets: no-envelope + over-envelope cases (fixed #550) ───
# Board #550: the home page divided a dept's WEEK total spend by
# mission_budget_total (Σ budget_usd over ONE daily mission cycle) — e.g.
# tony $185.92 spent / $8 mission budget = 2324%. The fix looks up
# settings.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT by dept SLUG instead — a
# real WEEKLY envelope, same shape as /costs' per-agent fix (#546).
class _Dept:
    def __init__(self, slug, name, live=True):
        self.slug = slug
        self.display_name = name
        self._live = live

    @property
    def is_live(self):
        return self._live


def test_dept_budgets_no_envelope_graceful(monkeypatch):
    from console.routes import home

    # Patch on the SAME module objects home actually references — after another
    # test reimports console.* these may differ from this file's top-level
    # `cost_tracker`, so patch via home.cost_tracker to be reimport-robust.
    monkeypatch.setattr(home.cost_tracker, "build_report",
                        lambda refresh=False: {"agents": {"alpha": {"week": {"cost": 5.0}}}})
    # dept slug NOT in the envelope map → budget None → "budget non défini"
    monkeypatch.setattr(home.settings, "OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT", {})
    cols = [{"dept": _Dept("alpha", "Alpha")}]
    rows = home._dept_budgets(cols)
    assert len(rows) == 1
    assert rows[0]["slug"] == "alpha"
    assert rows[0]["defined"] is False
    assert rows[0]["spent"] == 5.0
    assert rows[0]["pct"] is None


def test_dept_budgets_sane_pct_not_old_2324_nonsense(monkeypatch):
    """The original #550 bug: tony showed 2324% ($185.92 week-spend / $8
    ONE-DAY mission budget). With the per-dept-slug WEEKLY envelope, the same
    kind of spend now yields a SANE percentage (spent 186 vs envelope 220 →
    84.5%, not 2324%)."""
    from console.routes import home

    monkeypatch.setattr(home.cost_tracker, "build_report",
                        lambda refresh=False: {"agents": {"tony": {"week": {"cost": 186.0}}}})
    monkeypatch.setattr(home.settings, "OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT", {"tony": 220})
    cols = [{"dept": _Dept("tony", "Tony")}]
    rows = home._dept_budgets(cols)
    assert rows[0]["defined"] is True
    # 84.5% falls in the 80-100 "warn" (amber) band — sane, not the red/over
    # 2324% the old mission-budget denominator produced.
    assert rows[0]["level"] == "warn"
    assert rows[0]["pct"] == 84.5
    assert rows[0]["budget"] == 220.0
    assert rows[0]["pct"] != 2324.0


def test_dept_budgets_over_envelope_case(monkeypatch):
    from console.routes import home

    monkeypatch.setattr(home.cost_tracker, "build_report",
                        lambda refresh=False: {"agents": {"beta": {"week": {"cost": 30.0}}}})
    monkeypatch.setattr(home.settings, "OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT", {"beta": 20})
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
    monkeypatch.setattr(home.cost_tracker, "build_report", _boom)
    rows = home._dept_budgets([{"dept": _Dept("x", "X")}])
    assert rows == []  # never raises → home page never 500s


def test_dept_budgets_skips_non_live(monkeypatch):
    from console.routes import home

    monkeypatch.setattr(home.cost_tracker, "build_report",
                        lambda refresh=False: {"agents": {}})
    monkeypatch.setattr(home.settings, "OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT", {})
    cols = [{"dept": _Dept("live1", "L", live=True)},
            {"dept": _Dept("eclore1", "E", live=False)}]
    rows = home._dept_budgets(cols)
    assert [r["slug"] for r in rows] == ["live1"]


# ── settings.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT + its JSON override ───
def test_operating_envelope_by_dept_json_override_merges_over_defaults(monkeypatch):
    """OPERATING_ENVELOPE_BY_DEPT_JSON merges OVER the defaults: an existing
    key is replaced, a new key is added, defaults not mentioned are untouched."""
    monkeypatch.setenv("OPERATING_ENVELOPE_BY_DEPT_JSON",
                       '{"tony": 999, "newdept": 42}')
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console import settings
    assert settings.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT["tony"] == 999.0      # overridden
    assert settings.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT["newdept"] == 42.0    # added
    assert settings.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT["maya"] == 100        # untouched default


def test_operating_envelope_by_dept_json_malformed_falls_back_to_defaults(monkeypatch):
    """A malformed OPERATING_ENVELOPE_BY_DEPT_JSON (bad JSON, or not an object)
    must degrade to the defaults, never crash the console on boot."""
    monkeypatch.setenv("OPERATING_ENVELOPE_BY_DEPT_JSON", "not json{{{")
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console import settings
    assert settings.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT["tony"] == 220

    monkeypatch.setenv("OPERATING_ENVELOPE_BY_DEPT_JSON", "[1, 2, 3]")
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console import settings as settings2
    assert settings2.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT["tony"] == 220


# ── /costs page: by-agent SPEND-vs-OPERATING-ENVELOPE + fleet + degradation ──
# Fixed 2026-07-05 (Joris's screenshot: tony 1368%, fleet 1562%). Root cause was
# dividing a WEEK of total agent session-spend by ONE mission-cycle's
# budget_usd. /costs now compares week spend to a per-agent WEEKLY OPERATING
# ENVELOPE (settings.OPERATING_ENVELOPE_WEEKLY_USD) — NOT the dept.yaml mission
# budget (that stays on the home page's per-dept "Coûts" section, unchanged).
def _write_session(proj: Path, dirname: str, model: str, u: dict) -> None:
    import json
    d = proj / dirname
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant", "model": model, "usage": u}}) + "\n",
        encoding="utf-8",
    )


def _build_costs_client(monkeypatch, tmp_path, *, agent_dirname="bubble-ops-ben",
                        model="claude-sonnet-4-6",
                        usage=None, envelope_json=None):
    """Build a /costs TestClient whose cost report attributes ONE session to an
    agent (default dir → classifies to 'ben', which IS in the default envelope
    map at $90/week). Optionally sets OPERATING_ENVELOPE_JSON to override the
    envelope map from the env (no dept.yaml / READ_FROM_DISK involved anymore —
    the /costs denominator no longer reads mission budgets at all).

    We set HOME to a temp tree so the freshly-imported cost_tracker resolves its
    PROJECTS_DIR/CACHE from THERE (patching the old module reference is lost when
    console.* is re-imported below).
    """
    if usage is None:
        usage = {"input_tokens": 1_000_000, "output_tokens": 0,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    home = tmp_path / "home"
    proj = home / ".claude" / "projects"
    proj.mkdir(parents=True)
    _write_session(proj, f"-home-claude-agents-{agent_dirname}", model, usage)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "test-token-xyz")
    monkeypatch.delenv("READ_FROM_DISK", raising=False)
    if envelope_json is not None:
        monkeypatch.setenv("OPERATING_ENVELOPE_JSON", envelope_json)
    else:
        monkeypatch.delenv("OPERATING_ENVELOPE_JSON", raising=False)
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app
    from fastapi.testclient import TestClient
    c = TestClient(create_app())
    c.headers.update({"Authorization": "Bearer test-token-xyz"})
    return c


def test_costs_page_agent_not_in_envelope_map_degrades(monkeypatch, tmp_path):
    """An agent dir that classifies to a name NOT in the envelope map (e.g. a
    brand-new/unmapped dept) must still render the column + fleet-total line,
    both saying 'non définie' — graceful, same as the old missing-budget path."""
    c = _build_costs_client(monkeypatch, tmp_path,
                             agent_dirname="bubble-ops-brandnewdept")
    r = c.get("/costs")
    assert r.status_code == 200
    body = r.text
    assert "Enveloppe&nbsp;(hebdo)" in body       # the renamed column header
    assert "non définie" in body                  # graceful: no envelope mapped
    assert "budget-bar-fill--" not in body         # no bar for the undefined agent


def test_costs_page_shows_sane_pct_not_old_1368_nonsense(monkeypatch, tmp_path):
    """The original bug: tony showed 1368% because $109 week-spend was divided
    by an $8 ONE-DAY mission budget. With the operating-envelope fix, a
    realistic spend now yields a SANE percentage. 1M input sonnet tokens =
    $3.00 real cost; 'ben' envelope default is $90 → 3.33%, well under 100%."""
    c = _build_costs_client(monkeypatch, tmp_path, agent_dirname="bubble-ops-ben")
    r = c.get("/costs")
    assert r.status_code == 200
    body = r.text
    assert "budget-pct--ok" in body
    assert "1368" not in body
    assert "budget-pct--over" not in body
    assert "Enveloppe opérationnelle cette semaine" in body   # fleet-total copy


def test_costs_page_envelope_over_100_still_shows_over_level(monkeypatch, tmp_path):
    """A spend that genuinely exceeds ITS OWN weekly envelope still renders the
    red 'over' level — the fix changes the denominator, not the thresholds."""
    # 1M input tokens on sonnet = $3.00 real cost; override ben's envelope to
    # $1 so $3.00 spend is a genuine 300% over-envelope case.
    c = _build_costs_client(monkeypatch, tmp_path, agent_dirname="bubble-ops-ben",
                             envelope_json='{"ben": 1}')
    r = c.get("/costs")
    assert r.status_code == 200
    body = r.text
    assert "budget-bar-fill--over" in body
    assert "budget-pct--over" in body


def test_costs_page_fleet_row_sums_envelopes(monkeypatch, tmp_path):
    """The fleet '__fleet__' total sums the envelopes of agents that HAVE one
    defined (skips agents without an envelope) — verified directly against the
    route helper rather than scraping HTML, since the fleet is a single number."""
    monkeypatch.delenv("OPERATING_ENVELOPE_JSON", raising=False)
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.routes.costs import _agent_budgets
    report = {"agents": {
        "ben": {"week": {"cost": 10.0}},          # envelope 90 (default)
        "maya": {"week": {"cost": 20.0}},         # envelope 130 (default)
        "brandnewdept": {"week": {"cost": 5.0}},  # no envelope → excluded from fleet budget
    }}
    out = _agent_budgets(report)
    fleet = out["__fleet__"]
    assert fleet["defined"] is True
    assert fleet["budget"] == 220.0     # 90 + 130, brandnewdept excluded
    assert fleet["spent"] == 35.0       # 10 + 20 + 5, ALL spend counted


def test_operating_envelope_json_override_merges_over_defaults(monkeypatch):
    """OPERATING_ENVELOPE_JSON merges OVER the defaults: an existing key is
    replaced, a new key is added, defaults not mentioned are untouched."""
    monkeypatch.setenv("OPERATING_ENVELOPE_JSON",
                       '{"ben": 999, "newagent": 42}')
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console import settings
    assert settings.OPERATING_ENVELOPE_WEEKLY_USD["ben"] == 999.0        # overridden
    assert settings.OPERATING_ENVELOPE_WEEKLY_USD["newagent"] == 42.0    # added
    assert settings.OPERATING_ENVELOPE_WEEKLY_USD["maya"] == 130         # untouched default


def test_operating_envelope_json_malformed_falls_back_to_defaults(monkeypatch):
    """A malformed OPERATING_ENVELOPE_JSON (bad JSON, or not an object) must
    degrade to the defaults, never crash the console on boot."""
    monkeypatch.setenv("OPERATING_ENVELOPE_JSON", "not json{{{")
    import sys
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console import settings
    assert settings.OPERATING_ENVELOPE_WEEKLY_USD["ben"] == 90

    monkeypatch.setenv("OPERATING_ENVELOPE_JSON", "[1, 2, 3]")
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console import settings as settings2
    assert settings2.OPERATING_ENVELOPE_WEEKLY_USD["ben"] == 90
