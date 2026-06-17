"""Kanban board — reads from the Mac dashboard API and renders all cards."""
import requests
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from console.templates import templates

DASHBOARD = "http://{{INTERNAL_IP}}:3847"
router = APIRouter()

@router.get("/kanban", response_class=HTMLResponse)
async def kanban_board(request: Request):
    """Full kanban board — all active cards from the dashboard API."""
    columns = []
    error = None
    try:
        r = requests.get(f"{DASHBOARD}/api/inbox", timeout=5)
        if r.status_code == 200:
            data = r.json()
            columns = data.get("columns", [])
        else:
            error = f"Dashboard returned HTTP {r.status_code}"
    except Exception as e:
        error = f"Cannot reach dashboard: {e}"

    return templates.TemplateResponse("kanban.html", {
        "request": request,
        "columns": columns,
        "error": error,
        "generated_at": data.get("generated_at", "") if columns else "",
    })
