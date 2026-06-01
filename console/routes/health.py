"""GET /health — last successful run per (layer x dept), red if stale > 2x."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from console.services import dept_registry, morty_reader

router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
def health(request: Request):
    depts = dept_registry.list_departments()
    slugs = [d.slug for d in depts]
    rows = morty_reader.per_dept_layer_heartbeats(slugs)
    # group rows by dept for the table
    by_dept = {}
    for r in rows:
        by_dept.setdefault(r.dept, []).append(r)
    return request.app.state.templates.TemplateResponse(
        "health.html",
        {
            "request": request,
            "depts": depts,
            "by_dept": by_dept,
            "any_stale": any(r.is_stale for r in rows),
        },
    )
