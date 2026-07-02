"""GET /concierge/<name>            — concierge detail (status + live session).
GET /concierge/<name>/session     — HTMX fragment, the live session feed
                                     (auto-refreshes every few seconds).

Concierges (Morty, Claudette) are reactive assistants, not ops-loop
departments, so they get a simpler page than /dept/<slug>: service
status + a live-tailing view of their session transcript.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from console.services import concierge_reader

router = APIRouter()


@router.get("/concierge/{name}", response_class=HTMLResponse)
def concierge_detail(name: str, request: Request):
    c = concierge_reader.get_concierge(name)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Unknown concierge: {name}")
    turns = concierge_reader.read_recent_session(name, n=30)
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
            "turns": turns,
            "projects": projects,
            "status": c.metadata.get("service_status", "unknown"),
            "agent_model_info": agent_model_info,
        },
    )


@router.get("/concierge/{name}/session", response_class=HTMLResponse)
def concierge_session_fragment(name: str, request: Request):
    """HTMX-polled fragment: just the live session feed, re-rendered."""
    if concierge_reader.get_concierge(name) is None:
        raise HTTPException(status_code=404, detail=f"Unknown concierge: {name}")
    turns = concierge_reader.read_recent_session(name, n=30)
    return request.app.state.templates.TemplateResponse(
        "partials/concierge_session.html",
        {"request": request, "name": name, "turns": turns},
    )
