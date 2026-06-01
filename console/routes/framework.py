"""GET /framework — a simple flowchart of how the organisation works.

{{OPERATOR}} msg 1183 (2026-06-01): one easy-to-read picture of the framework —
concierges, departments, layers — drawn from the live codebase + the
Notion "bubble-ops-loop — Architecture finale simplifiée" page.

The shape is the canonical hierarchy from that page:
    Principal ({{OPERATOR}} · {{OPERATOR_2}})
      ↓ directives (PR)        ↑ exports + risk KPIs
    Management department (Tony)
      ↓
    Ops departments (Maya, CGP, Ben, …)
and, beside the loop, the reactive Concierges (Morty · Claudette).
Every department runs the SAME 4-moment OODA loop (the layers).

Department boxes are filled from the live registry so the chart always
reflects who actually exists; the structure itself is the fixed framework.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from console.services import concierge_reader, dept_registry, github_reader

router = APIRouter()

# The 4 layers (OODA "moments") every department runs each day. Mirrors
# loop_history.MOMENT_NAMES + the Notion architecture page.
LAYERS = [
    {"num": 1, "name": "Le matin", "ooda": "Data", "what": "rafraîchit les données, lit les directives"},
    {"num": 2, "name": "La recherche", "ooda": "Research", "what": "analyse, prépare les décisions"},
    {"num": 3, "name": "L'exécution", "ooda": "Exec", "what": "agit sur ce qui est validé"},
    {"num": 4, "name": "Le débrief du soir", "ooda": "Risk", "what": "audit, risques, améliorations"},
]


def _level(slug: str) -> str:
    """Hierarchy level of a dept from its dept.yaml (management|ops)."""
    y = github_reader.load_dept_yaml(slug)
    if isinstance(y, dict):
        lvl = (y.get("hierarchy", {}) or {}).get("level") \
            or (y.get("department", {}) or {}).get("level")
        if lvl == "management":
            return "management"
    return "ops"


@router.get("/framework", response_class=HTMLResponse)
def framework(request: Request):
    live = dept_registry.live_departments()
    management = [d for d in live if _level(d.slug) == "management"]
    ops = [d for d in live if _level(d.slug) != "management"]
    concierges = [concierge_reader.get_concierge(n) for n in concierge_reader.CONCIERGES]
    concierges = [c for c in concierges if c is not None]
    return request.app.state.templates.TemplateResponse(
        "framework.html",
        {
            "request": request,
            "management": management,
            "ops": ops,
            "concierges": concierges,
            "layers": LAYERS,
        },
    )
