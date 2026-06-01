"""GET /dept/<slug>/session — live session feed fragment for a department.

Same live-session view as the concierge pages, for dept agents. Added as
its OWN route file (not folded into routes/dept.py) so the dept detail
page only needs a small HTMX include, and this evolves independently.

The session DIR for a dept can be the prefixed (bubble-ops-<slug>) or the
unprefixed (<slug>) form depending on how the agent's cwd resolved, so we
try both via agent_session.newest_session_file.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from console.services import agent_session, dept_registry

router = APIRouter()


@router.get("/dept/{slug}/session", response_class=HTMLResponse)
def dept_session_fragment(slug: str, request: Request):
    if dept_registry.get_department(slug) is None:
        raise HTTPException(status_code=404, detail=f"Unknown dept: {slug}")
    sess = agent_session.newest_session_file([slug, f"bubble-ops-{slug}"])
    turns = agent_session.read_session_turns(sess, n=30)
    return request.app.state.templates.TemplateResponse(
        "partials/_session_feed.html",
        {"request": request, "name": slug, "turns": turns},
    )
