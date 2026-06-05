"""GET /health — the Carnet de bord: real per-dept loop activity.

Reads live on-disk loop traces (heartbeat + per-layer .last-run) via
morty_reader — no longer a stub (Joris msg 1180, 2026-06-01)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from console.services import dataflow, dept_registry, morty_reader, org_framework

router = APIRouter()


@router.get("/health/dataflow/{slug}.json")
def health_dataflow(slug: str) -> JSONResponse:
    """Per-dept data-flow (grounded in dept.yaml): which repos/vault/db/broker/
    queue/external sources each layer reads + writes. Backs the click-through
    'data-flow' view on the /health flowchart."""
    return JSONResponse(dataflow.dept_dataflow(slug))


@router.get("/health/graph.json")
def health_graph() -> JSONResponse:
    """Live org graph (nodes/edges/rails) for the React Flow chart on /health.

    Same auth as the whole console (global bearer middleware). The /health
    page fetches this on load and re-fetches to refresh status colours
    without a full page reload."""
    return JSONResponse(org_framework.build_graph())


@router.get("/health/layer/{slug}/{layer}.json")
def health_layer_detail(slug: str, layer: int) -> JSONResponse:
    """Click-through detail for one dept×layer (the 'log visuel' panel):
    last-run, the summary.md snippet, and the artifact filenames produced.
    Backs the clickable 1-2-3-4 badges on the /health flowchart."""
    if layer not in (1, 2, 3, 4):
        return JSONResponse({"detail": "layer must be 1-4"}, status_code=400)
    return JSONResponse(morty_reader.layer_output_detail(slug, layer))


@router.get("/health", response_class=HTMLResponse)
def health(request: Request):
    depts = dept_registry.list_departments()
    slugs = [d.slug for d in depts]
    rows = morty_reader.per_dept_layer_heartbeats(slugs)
    pulse = morty_reader.loop_pulse(slugs)
    # group rows by dept for the activity table (section 1)
    by_dept = {}
    for r in rows:
        by_dept.setdefault(r.dept, []).append(r)
    # Sections 2-4 (hierarchy / 4-moment loop / rails) are now the single
    # interactive React Flow graph, fed live by GET /health/graph.json.
    # The 4 layer names are still passed for the activity-table headers.
    return request.app.state.templates.TemplateResponse(
        "health.html",
        {
            "request": request,
            "depts": depts,
            "by_dept": by_dept,
            "pulse": pulse,
            "any_stale": any(r.is_stale for r in rows),
            "layers": org_framework.LAYERS,
        },
    )
