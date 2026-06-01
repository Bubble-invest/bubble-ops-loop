"""
GET  /gate/<dept>/kind/<kind>   — BATCH view: all pending gates of one kind,
                                  each with an inline action form (triage many
                                  at once; deciding one swaps just that card).
GET  /gate/<dept>/<id>          — decision card with 4 actions (single gate)
POST /gate/<dept>/<id>/decide   — writes inbox/decisions/<id>.yaml
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from console.services import dept_registry, github_reader
from console.services.humanize import humanize_kind

router = APIRouter()

ALLOWED_ACTIONS = {"approve", "reject", "modify", "defer"}


# IMPORTANT: this MUST be declared before /gate/{slug}/{gate_id} — otherwise
# FastAPI would match "kind" as a gate_id. Specific routes before catch-all.
@router.get("/gate/{slug}/kind/{kind}", response_class=HTMLResponse)
def gate_batch(slug: str, kind: str, request: Request):
    """List every pending gate of `kind` for the dept, each with an inline
    decision form. Fixes the two triage pains (2026-06-01): see all at once,
    and act-then-advance in place instead of being stranded on gate #1."""
    if dept_registry.get_department(slug) is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    gates = [g for g in github_reader.list_pending_gates(slug)
             if (g.get("kind") or "decision") == kind]
    return request.app.state.templates.TemplateResponse(
        "gate_batch.html",
        {
            "request": request,
            "slug": slug,
            "kind": kind,
            "kind_label": humanize_kind(kind),
            "gates": gates,
            "count": len(gates),
            "actions": sorted(ALLOWED_ACTIONS),
        },
    )


@router.get("/gate/{slug}/{gate_id}", response_class=HTMLResponse)
def gate_card(slug: str, gate_id: str, request: Request):
    if dept_registry.get_department(slug) is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    gate = github_reader.load_gate(slug, gate_id)
    raw = github_reader.load_gate_raw(slug, gate_id)
    if gate is None or raw is None:
        raise HTTPException(404, f"Gate not found: {gate_id}")
    return request.app.state.templates.TemplateResponse(
        "gate_card.html",
        {
            "request": request,
            "slug": slug,
            "gate_id": gate_id,
            "gate": gate,
            "gate_raw": raw,
            "actions": sorted(ALLOWED_ACTIONS),
        },
    )


@router.post("/gate/{slug}/{gate_id}/decide", response_class=HTMLResponse)
def gate_decide(
    slug: str, gate_id: str, request: Request,
    action: str = Form(...), comment: str = Form(""),
):
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(400, f"Invalid action: {action}")
    if dept_registry.get_department(slug) is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    if github_reader.load_gate(slug, gate_id) is None:
        raise HTTPException(404, f"Gate not found: {gate_id}")
    decision = {
        "gate_id": gate_id,
        "action": action,
        "comment": comment or "",
        "decided_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decided_by": "joris",  # single-operator console
    }
    out_path = github_reader.write_gate_decision(slug, gate_id, decision)
    return request.app.state.templates.TemplateResponse(
        "partials/gate_decision_ok.html",
        {
            "request": request,
            "slug": slug,
            "gate_id": gate_id,
            "action": action,
            "out_path": str(out_path),
        },
    )
