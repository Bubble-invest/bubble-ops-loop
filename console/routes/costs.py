"""GET /costs — per-agent / per-job token & cost panel ({{OPERATOR}} msg 3994/4003).

Surfaces what each agent + each `claude -p` cron is spending (today + last 7d),
with a per-model breakdown. Backed by services.cost_tracker, which scans the
session JSONLs and prices tokens by model. Estimate for trend/relative cost, not
billing. Same global bearer auth as the rest of the console.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from console import settings
from console.services import cost_tracker, dept_registry, github_reader

router = APIRouter()


def _agent_budgets(report: dict) -> dict:
    """Per-agent SPEND-vs-OPERATING-ENVELOPE for /costs (board #524d, fixed
    2026-07-05 — see settings.OPERATING_ENVELOPE_WEEKLY_USD docstring for why).

    spent = the agent's week real-$ cost (its WHOLE session spend — interactive
    + operating + dev, not just missions). budget = the agent's weekly
    operating envelope from settings.OPERATING_ENVELOPE_WEEKLY_USD, looked up
    by the FULL report agent-key (e.g. "tony (local)") since the envelope is
    per-agent-session, not per-dept. Returns {agent_key: budget_status_dict}
    plus a fleet-total under key "__fleet__": {spent, budget, pct, level,
    defined} (fleet budget = Σ envelopes of agents that have one defined).

    This is DELIBERATELY NOT the dept.yaml mission_budget_total lookup (that
    stays on the home page's per-dept "Coûts" section — a mission-cycle
    budget, not a whole-session envelope). An agent key not in the envelope
    map gets a defined=False row (rendered "—"). Fully guarded: any lookup
    failure degrades that agent to no-envelope, never a 500.
    """
    agents = report.get("agents") if isinstance(report, dict) else None
    out: dict = {}
    fleet_spent = 0.0
    fleet_budget = 0.0
    any_budget = False
    if not isinstance(agents, dict):
        return {"__fleet__": cost_tracker.budget_status(0.0, None)}

    for key, a in agents.items():
        try:
            spent = float(a.get("week", {}).get("cost", 0.0))
        except (AttributeError, TypeError, ValueError):
            spent = 0.0
        budget = settings.OPERATING_ENVELOPE_WEEKLY_USD.get(key)
        out[key] = cost_tracker.budget_status(spent, budget)
        fleet_spent += spent
        if budget is not None and budget > 0:
            fleet_budget += budget
            any_budget = True

    out["__fleet__"] = cost_tracker.budget_status(
        fleet_spent, fleet_budget if any_budget else None
    )
    return out


def _dept_budgets(report: dict) -> dict:
    """Per-dept SPEND-vs-ENVELOPE for /costs (board #466, child of #404).

    budget = the dept's WEEKLY envelope, preferring the operator-set
    `department.budget_weekly_usd` field in the dept's OWN dept.yaml
    (cost_tracker.dept_weekly_envelope — new, board #466) and falling back
    to settings.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT (the pre-existing
    central-dict envelope, board #550) when the dept.yaml field is absent —
    so nothing regresses for depts that haven't set the new field yet.

    spent = the dept's rolled-up week real-$ spend (cost_tracker.spent_by_dept
    — UNCHANGED attribution logic, just consumed here). Returns
    {dept_slug: budget_status_dict} plus a fleet-total under "__fleet__"
    (Σ envelopes of depts that have one defined, from EITHER source).

    Only live, non-concierge depts are included (concierges have no
    dept.yaml to carry the field, and a-eclore/ancien depts aren't
    operationally spending against an envelope yet). Fully guarded: any
    lookup failure for one dept degrades that dept to no-envelope, never a
    500 for the whole page.
    """
    try:
        depts = [
            d for d in dept_registry.list_departments()
            if d.is_live and d.slug not in dept_registry.KNOWN_CONCIERGE_SLUGS
        ]
        spent_map = cost_tracker.spent_by_dept(report, span="week")
    except Exception:  # noqa: BLE001 — dept envelope overlay must never 500 /costs
        return {"__fleet__": cost_tracker.budget_status(0.0, None)}

    out: dict = {}
    fleet_spent = 0.0
    fleet_budget = 0.0
    any_budget = False
    for d in depts:
        try:
            dept_yaml = github_reader.load_dept_yaml(d.slug)
            budget = cost_tracker.dept_weekly_envelope(dept_yaml)
            if budget is None:
                budget = settings.OPERATING_ENVELOPE_WEEKLY_USD_BY_DEPT.get(d.slug)
            spent = float(spent_map.get(d.slug, 0.0))
        except Exception:  # noqa: BLE001 — one dept's failure never sinks the rest
            budget = None
            spent = float(spent_map.get(d.slug, 0.0)) if isinstance(spent_map, dict) else 0.0
        out[d.slug] = {"display_name": d.display_name, **cost_tracker.budget_status(spent, budget)}
        fleet_spent += spent
        if budget is not None and budget > 0:
            fleet_budget += budget
            any_budget = True

    out["__fleet__"] = cost_tracker.budget_status(
        fleet_spent, fleet_budget if any_budget else None
    )
    return out


@router.get("/costs.json")
def costs_json(refresh: bool = False) -> JSONResponse:
    """Raw cost report (per-agent + totals, today/7d, per-model)."""
    return JSONResponse(cost_tracker.build_report(refresh=refresh))


@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request) -> HTMLResponse:
    """The cost panel page."""
    report = cost_tracker.build_report(refresh=False)
    try:
        agent_budgets = _agent_budgets(report)
    except Exception:  # noqa: BLE001 — budget overlay must never 500 /costs
        agent_budgets = {"__fleet__": cost_tracker.budget_status(0.0, None)}
    try:
        dept_budgets = _dept_budgets(report)
    except Exception:  # noqa: BLE001 — dept envelope overlay must never 500 /costs
        dept_budgets = {"__fleet__": cost_tracker.budget_status(0.0, None)}
    return request.app.state.templates.TemplateResponse(
        "costs.html",
        {
            "request": request,
            "report": report,
            "agent_budgets": agent_budgets,
            "dept_budgets": dept_budgets,
        },
    )
