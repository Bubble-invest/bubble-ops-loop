"""
org_framework.py — the organisation framework data for the cockpit.

Joris msg 1183 → 1188 (2026-06-01): a simple flowchart of how the org works
(concierges, departments, layers), shown INSIDE the Carnet de bord page (not
a separate page). This service builds the data; the partial
partials/_org_framework.html draws it.

Shape from the Notion "bubble-ops-loop — Architecture finale simplifiée" page:
    Principal (Joris·Jade) → Management dept → Ops depts,  + Concierges beside.
Every department runs the same 4-moment OODA day (the layers).
"""
from __future__ import annotations

from typing import Any, Dict

from console.services import concierge_reader, dept_registry, github_reader

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


def build() -> Dict[str, Any]:
    """Return {management, ops, concierges, layers} for the framework chart.

    Department boxes are filled live from the registry so the chart always
    reflects who actually exists; the structure itself is the fixed framework.
    """
    live = dept_registry.live_departments()
    management = [d for d in live if _level(d.slug) == "management"]
    ops = [d for d in live if _level(d.slug) != "management"]
    concierges = [concierge_reader.get_concierge(n) for n in concierge_reader.CONCIERGES]
    concierges = [c for c in concierges if c is not None]
    return {
        "management": management,
        "ops": ops,
        "concierges": concierges,
        "layers": LAYERS,
    }
