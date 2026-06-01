"""GET /health — the Carnet de bord: real per-dept loop activity.

Reads live on-disk loop traces (heartbeat + per-layer .last-run) via
morty_reader — no longer a stub ({{OPERATOR}} msg 1180, 2026-06-01)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from console.services import dept_registry, morty_reader, org_framework

router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
def health(request: Request):
    depts = dept_registry.list_departments()
    slugs = [d.slug for d in depts]
    rows = morty_reader.per_dept_layer_heartbeats(slugs)
    pulse = morty_reader.loop_pulse(slugs)
    # group rows by dept for the table
    by_dept = {}
    for r in rows:
        by_dept.setdefault(r.dept, []).append(r)
    # The org-framework flowchart lives on this same page ({{OPERATOR}} msg 1188:
    # in the Carnet de bord, not a separate page).
    framework = org_framework.build()
    return request.app.state.templates.TemplateResponse(
        "health.html",
        {
            "request": request,
            "depts": depts,
            "by_dept": by_dept,
            "pulse": pulse,
            "any_stale": any(r.is_stale for r in rows),
            "management": framework["management"],
            "ops": framework["ops"],
            "concierges": framework["concierges"],
            "layers": framework["layers"],
        },
    )
