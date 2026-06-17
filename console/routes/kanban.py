"""Kanban board — reads from the Mac dashboard API and renders all cards."""
import json
import urllib.request
import urllib.error

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

DASHBOARD = "http://{{INTERNAL_IP}}:3847"
router = APIRouter()


@router.get("/kanban", response_class=HTMLResponse)
async def kanban_board(request: Request):
    """Full kanban board — all active cards from the dashboard API."""
    columns = []
    generated_at = ""
    error = None
    try:
        req = urllib.request.Request(f"{DASHBOARD}/api/inbox")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        columns = data.get("columns", [])
        generated_at = data.get("generated_at", "")
    except urllib.error.HTTPError as e:
        error = f"Dashboard returned HTTP {e.code}"
    except Exception as e:
        error = f"Cannot reach dashboard: {e}"

    return request.app.state.templates.TemplateResponse("kanban.html", {
        "request": request,
        "columns": columns,
        "error": error,
        "generated_at": generated_at,
    })
