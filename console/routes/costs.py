"""GET /costs — per-agent / per-job token & cost panel (Joris msg 3994/4003).

Surfaces what each agent + each `claude -p` cron is spending (today + last 7d),
with a per-model breakdown. Backed by services.cost_tracker, which scans the
session JSONLs and prices tokens by model. Estimate for trend/relative cost, not
billing. Same global bearer auth as the rest of the console.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from console.services import cost_tracker

router = APIRouter()


@router.get("/costs.json")
def costs_json(refresh: bool = False) -> JSONResponse:
    """Raw cost report (per-agent + totals, today/7d, per-model)."""
    return JSONResponse(cost_tracker.build_report(refresh=refresh))


@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request) -> HTMLResponse:
    """The cost panel page."""
    report = cost_tracker.build_report(refresh=False)
    return request.app.state.templates.TemplateResponse(
        "costs.html", {"request": request, "report": report}
    )
