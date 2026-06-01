"""GET /settings/<slug> — per-dept knobs (cadence, gate policies, etc)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from console.services import dept_registry, github_reader

router = APIRouter()


@router.get("/settings/{slug}", response_class=HTMLResponse)
def settings_page(slug: str, request: Request):
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    dept_yaml = github_reader.load_dept_yaml(slug)
    gate_policies = {}
    if isinstance(dept_yaml, dict):
        gate_policies = dept_yaml.get("gate_policies", {}) or {}
    return request.app.state.templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "dept": d,
            "dept_yaml": dept_yaml,
            "gate_policies": gate_policies,
        },
    )
