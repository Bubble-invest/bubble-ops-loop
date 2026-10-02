"""GET /dept/<slug>/session — privacy-safe activity fragment.

The historical URL is preserved for HTMX/deep-link compatibility, but board
#1673 forbids rendering session prose or tool arguments.  This route reads only
archive mtime plus output-file metadata and links to the existing evidence
viewer; it never opens transcript content.
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from console.services import agent_session, dept_registry, loop_history

router = APIRouter()
_NON_DELIVERABLE_OUTPUTS = {".last-run", "logs.jsonl", "round_counter.json"}


@router.get("/dept/{slug}/session", response_class=HTMLResponse)
def dept_session_fragment(slug: str, request: Request):
    dept = dept_registry.get_department(slug)
    if dept is None:
        raise HTTPException(status_code=404, detail=f"Unknown dept: {slug}")

    deliverables = []
    for run in loop_history.list_loop_runs(slug):
        for layer in run.layers:
            for output in layer.files:
                if output.name in _NON_DELIVERABLE_OUTPUTS:
                    continue
                deliverables.append({
                    "label": f"L{layer.num} · {output.name}",
                    "url": (
                        f"/dept/{slug}/output?f="
                        f"{quote(output.rel_path, safe='')}"
                    ),
                })
                if len(deliverables) >= 4:
                    break
            if len(deliverables) >= 4:
                break
        if deliverables:
            break

    return request.app.state.templates.TemplateResponse(
        "partials/_activity_status.html",
        {
            "request": request,
            "person_name": dept.display_name,
            "status_label": "En poste" if dept.is_live else dept.status,
            "last_activity_iso": agent_session.newest_session_mtime_iso(
                [slug, f"bubble-ops-{slug}"]
            ),
            "deliverables": deliverables,
            "evidence_url": f"/dept/{slug}#dept-history-heading",
            "evidence_label": "Voir l’historique et toutes les preuves",
        },
    )
