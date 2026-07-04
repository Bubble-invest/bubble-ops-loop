"""GET /costs — per-agent / per-job token & cost panel ({{OPERATOR}} msg 3994/4003).

Surfaces what each agent + each `claude -p` cron is spending (today + last 7d),
with a per-model breakdown. Backed by services.cost_tracker, which scans the
session JSONLs and prices tokens by model. Estimate for trend/relative cost, not
billing. Same global bearer auth as the rest of the console.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from console.services import cost_tracker, github_reader

router = APIRouter()


def _agent_budgets(report: dict) -> dict:
    """Per-agent budget-vs-spend for /costs (board #524d).

    For each agent in the report, its budget = Σ budget_usd across its dept's
    recurring_missions[] (same read-only source as the home Coûts section);
    spent = its week real-$ cost. Returns {agent_key: budget_status_dict} plus
    a fleet-total under key "__fleet__": {spent, budget, pct, level, defined}.

    An agent whose base name doesn't map to a dept with a dept.yaml, or whose
    dept has no budget_usd, gets a defined=False row (rendered "—"). Fully
    guarded: any lookup failure degrades that agent to no-budget, never a 500.
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
        base = cost_tracker.agent_key_base(key)
        slug = cost_tracker._AGENT_KEY_TO_SLUG_ALIAS.get(base, base)
        budget = None
        try:
            dept_yaml = github_reader.load_dept_yaml(slug)
            budget = cost_tracker.mission_budget_total(dept_yaml)
        except Exception:  # noqa: BLE001 — a bad/missing dept.yaml → no budget, not a 500
            budget = None
        out[key] = cost_tracker.budget_status(spent, budget)
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
    return request.app.state.templates.TemplateResponse(
        "costs.html",
        {"request": request, "report": report, "agent_budgets": agent_budgets},
    )
