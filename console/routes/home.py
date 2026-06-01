"""GET / — cabinet d'éclosion home (décisions awaiting + équipe + KPIs)."""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from console.services import dept_registry, github_reader
from console.services.gate_grouping import group_gates_by_kind

router = APIRouter()


# Back-compat alias for tests that import the private helper.
# Canonical home lives in console.services.gate_grouping — also used by
# dept.py so / and /dept/<slug> apply identical grouping rules (msg 3030).
def _group_gates_by_kind(gates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return group_gates_by_kind(gates)


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    # Exclude anciens (Retired/Cancelled) — they have their own section on
    # /agents and their gates are stale by definition. Regression caught
    # 2026-05-24 msg 3041: after retiring fixture, its 9 stale gates kept
    # surfacing on home and fixture itself was listed as "en éclosion".
    depts = [d for d in dept_registry.list_departments() if not d.is_ancien]
    columns = []
    for d in depts:
        gates = github_reader.list_pending_gates(d.slug)
        columns.append({
            "dept": d,
            "gates": gates,
            "gate_count": len(gates),
            "gate_groups": _group_gates_by_kind(gates),
        })
    total_gates = sum(c["gate_count"] for c in columns)
    return request.app.state.templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "columns": columns,
            "total_gates": total_gates,
            "live_count": len([c for c in columns if c["dept"].is_live]),
            "eclore_count": len([c for c in columns if not c["dept"].is_live]),
        },
    )
