"""GET /concierge/<name>            — concierge detail and deliverables.
GET /concierge/<name>/session     — privacy-safe activity metadata fragment.

Concierges (Morty, Claudette) are reactive assistants, not ops-loop
departments, so they get a simpler page than /dept/<slug>: service
status, activity timestamp, deliverables and evidence links.  Board #1673
forbids rendering transcript prose or tool arguments in the cockpit.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from console.services import concierge_reader

router = APIRouter()


def _activity_context(c, projects: list) -> dict:
    status = c.metadata.get("service_status", "unknown")
    return {
        "person_name": c.name.capitalize(),
        "status_label": "En service" if status == "active" else status,
        "last_activity_iso": c.last_activity_iso,
        "deliverables": [
            {
                "label": project.title,
                "url": project.url or f"/concierge/{c.name}#concierge-projects-heading",
            }
            for project in projects[:4]
        ],
        "evidence_url": f"/concierge/{c.name}#concierge-projects-heading",
        "evidence_label": "Voir tous les livrables et liens de preuve",
    }


@router.get("/concierge/{name}", response_class=HTMLResponse)
def concierge_detail(name: str, request: Request):
    c = concierge_reader.get_concierge(name)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Unknown concierge: {name}")
    # Working projects from <workspace>/workspace/projects/*/STATUS.md
    # ({{OPERATOR}} msg 1193 — show what the concierge is building).
    projects = concierge_reader.list_projects(name)
    # Deployment fact: which model/runtime this concierge's agent process
    # actually runs (hardcoded — concierges have no dept.yaml to read it
    # from). Card 2026-07-02.
    agent_model_info = concierge_reader.concierge_model_info(name)
    return request.app.state.templates.TemplateResponse(
        "concierge_detail.html",
        {
            "request": request,
            "concierge": c,
            "projects": projects,
            "status": c.metadata.get("service_status", "unknown"),
            "agent_model_info": agent_model_info,
            **_activity_context(c, projects),
        },
    )


@router.get("/concierge/{name}/session", response_class=HTMLResponse)
def concierge_session_fragment(name: str, request: Request):
    """HTMX fragment with metadata only; transcript bytes are never opened."""
    c = concierge_reader.get_concierge(name)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Unknown concierge: {name}")
    projects = concierge_reader.list_projects(name)
    return request.app.state.templates.TemplateResponse(
        "partials/concierge_session.html",
        {"request": request, **_activity_context(c, projects)},
    )
