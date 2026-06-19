"""GET /dept/<slug> — per-dept detail (layer state + outputs + queue depths).
GET /dept/<slug>/management-view — CEO aggregation view (management depts only).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from console.services import (
    backup_history,
    dept_registry,
    github_reader,
    loop_history,
    markdown_render,
    whiteboard_series,
)
from console.services.gate_grouping import group_gates_by_kind

router = APIRouter()

# Firm-wide kanban lives on the Mac dashboard; the management dept page shows a
# compact snapshot of it. urllib (no `requests` dep — same as the /kanban route).
_DASHBOARD = "http://100.75.151.47:3847"
_KANBAN_COL_LABELS = {
    "needs_attention": "À traiter",
    "investigating": "En cours",
    "waiting": "En attente",
}


def _kanban_snapshot(limit: int = 6) -> "dict | None":
    """Compact cross-dept kanban view for the management cockpit: per-column
    counts + the first few `needs_attention` items. Returns None if the
    dashboard is unreachable (the page must still render)."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{_DASHBOARD}/api/inbox", timeout=4) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    cols = data.get("columns", {}) or {}
    counts = data.get("counts", {}) or {}
    attention = (cols.get("needs_attention") or [])[:limit]
    return {
        "counts": {k: counts.get(k, 0) for k in _KANBAN_COL_LABELS},
        "labels": _KANBAN_COL_LABELS,
        "attention": attention,
        "attention_total": len(cols.get("needs_attention") or []),
        "generated_at": data.get("generated_at", ""),
    }


@router.get("/dept/{slug}", response_class=HTMLResponse)
def dept_detail(slug: str, request: Request):
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Unknown dept: {slug}")
    dept_yaml = github_reader.load_dept_yaml(slug)
    gates = github_reader.list_pending_gates(slug)
    # Group gates by kind so /dept/<slug> mirrors / (msg 3030 — "shouldn't
    # they be grouped?"). Same helper as home.py, single source of truth.
    gate_groups = group_gates_by_kind(gates)
    # Back-compat: keep `missions` (slug list) for legacy templates / tests,
    # but enrich with full mission dicts (cadence, description, layer,
    # creates, gate_policy_id) so the UI can render the body of each
    # mission. Joris flag 2026-05-24 msg 3137.
    missions = github_reader.list_missions(slug)
    missions_full = github_reader.list_missions_full(slug)
    layers = []
    if isinstance(dept_yaml, dict):
        layers = dept_yaml.get("layers", {}).get("subscribed", []) or []
    # MANDATE.md verbatim — Joris reads this in the operating phase to
    # audit scope or onboard Jade (msg 3118).
    mandate_md = github_reader.load_mandate_md(slug)
    # Per-layer PROMPT.md — populated only for layers that have a file
    # on disk (graceful degradation when absent). Joris flag msg 3137.
    layer_prompts = {n: github_reader.load_layer_prompt_md(slug, n)
                     for n in layers}
    # Bucket missions by layer so the template can render ONE section
    # (Moments) with missions nested inside, rather than a duplicate
    # "Rendez-vous récurrents" section (Joris msg 3142).
    missions_by_layer = github_reader.group_missions_by_layer(missions_full)
    # Per-layer recent output (last-run timestamp + summary excerpt) for
    # activity tracking in the moments kanban (Joris msg 1071, 2026-05-28).
    layer_recent_outputs = {
        n: github_reader.load_recent_layer_output(slug, n)
        for n in layers
    }
    # Per-dept whiteboard — agent-surfaced KPIs/metrics for Joris
    # (Joris msg 1073, 2026-05-28).
    whiteboard = github_reader.load_whiteboard(slug)
    # Free-space whiteboard — a blank canvas card the dept manager fills with
    # any data representation it wants: tables, headings, rich text, embedded
    # chart images (whiteboard.md). Rendered as SANITIZED markdown→HTML so the
    # agent's markdown actually formats (Ben's allocation tables rendered as raw
    # text before — Joris 2026-06-19) without allowing script injection.
    # Original msg 1174, 2026-06-01.
    whiteboard_freeform = markdown_render.render_markdown_safe(
        github_reader.load_whiteboard_freeform(slug)
    )
    # KPI graphs — time series built from the dept's Layer-4 output history
    # (one datapoint per loop run). Joris msg 1163, 2026-06-01.
    whiteboard_graphs = whiteboard_series.load_whiteboard_series(slug)
    # Loop-run history — one entry per active day, with clickable outputs.
    # Joris msg 1168, 2026-06-01.
    loop_runs = loop_history.list_loop_runs(slug)
    # Decision timeline — all past and pending decisions surfaced in the
    # loop-history section (Joris 2026-06-09).
    decision_events = loop_history.list_decision_events(slug)
    # Safety-net (loop-backup) events — the twice-daily backup timer's verdict
    # per fire: loop alive → skip, or loop dead/parked → one backup tick.
    # Joris msg 1171, 2026-06-01.
    backup_events = backup_history.recent_backups(slug)
    latest_backup = backup_history.latest_backup(slug)
    # Compact firm-wide kanban snapshot — ONLY for the management dept (Tony).
    # The management cockpit should surface the cross-dept board (counts + the
    # few items needing attention); the full board lives at /kanban. Read-only.
    # (Joris 2026-06-17.)
    kanban_summary = None
    is_management = (
        isinstance(dept_yaml, dict)
        and (dept_yaml.get("hierarchy") or {}).get("level") == "management"
    )
    if is_management:
        kanban_summary = _kanban_snapshot()
    return request.app.state.templates.TemplateResponse(
        "dept_detail.html",
        {
            "request": request,
            "dept": d,
            "dept_yaml": dept_yaml,
            "gates": gates,
            "gate_groups": gate_groups,
            "missions": missions,
            "missions_full": missions_full,
            "missions_by_layer": missions_by_layer,
            "layers": layers,
            "layer_prompts": layer_prompts,
            "mandate_md": mandate_md,
            "layer_recent_outputs": layer_recent_outputs,
            "whiteboard": whiteboard,
            "whiteboard_freeform": whiteboard_freeform,
            "whiteboard_graphs": whiteboard_graphs,
            "loop_runs": loop_runs,
            "decision_events": decision_events,
            "backup_events": backup_events,
            "latest_backup": latest_backup,
            "kanban_summary": kanban_summary,
        },
    )


@router.get("/dept/{slug}/output", response_class=HTMLResponse)
def output_file(slug: str, f: str, request: Request):
    """View a single loop-run output file in-browser (Joris msg 1168).

    `f` is a repo-relative path that must live under outputs/ — the reader
    refuses anything that escapes it.
    """
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Unknown dept: {slug}")
    fileinfo = loop_history.read_output_file(slug, f)
    if fileinfo is None:
        raise HTTPException(
            status_code=404,
            detail=f"Fichier introuvable ou hors du dossier outputs/ : {f}",
        )
    return request.app.state.templates.TemplateResponse(
        "output_file.html",
        {"request": request, "dept": d, "file": fileinfo},
    )


@router.get("/dept/{slug}/management-view", response_class=HTMLResponse)
def management_view(slug: str, request: Request):
    """CEO cross-dept aggregation view.

    Only available for departments with level=management (per hierarchy.level
    in dept.yaml). Returns 404 with a French Bureau-de-Cadre message for ops
    depts. Returns a kanban-shaped view: one column per child x KPI status x
    pending gates x staleness.
    """
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Unknown dept: {slug}")

    dept_yaml = github_reader.load_dept_yaml(slug)
    dept_level = "ops"
    if isinstance(dept_yaml, dict):
        dept_level = (
            dept_yaml.get("hierarchy", {}).get("level")
            or dept_yaml.get("department", {}).get("level")
            or "ops"
        )

    if dept_level != "management":
        # Per spec: return 404 with a friendly French Bureau-de-Cadre message.
        raise HTTPException(
            status_code=404,
            detail=(
                f"Ce département ({slug}) n'est pas un département de management. "
                f"La vue agrégée n'est disponible que pour les départements de niveau "
                f"'management'. Ce département est de niveau '{dept_level}'."
            ),
        )

    aggregated = github_reader.load_management_exports(slug)
    return request.app.state.templates.TemplateResponse(
        "management_view.html",
        {
            "request": request,
            "dept": d,
            "dept_yaml": dept_yaml,
            "aggregated": aggregated,
            "children": aggregated.get("children", []),
            "total_open_gates": aggregated.get("total_open_gates", 0),
            "stale_children": aggregated.get("stale_children", []),
        },
    )
