"""
GET /agents/<slug>/onboarding — 3-pane onboarding view.
GET /agents/<slug>/onboarding/timeline — HTMX-polled timeline fragment (G3).
GET /agents/<slug>/onboarding/artifacts-fragment — HTMX artifacts fragment (G3).
GET /agents/<slug>/onboarding/heartbeat-fragment — HTMX latest-commit fragment (G3).

Per Notion v5 lines 763-781:
  +---------------+----------------+----------------+
  | Etapes        | Chat avec     | Artifacts      |
  | onboarding    | l'agent        | en construction|
  +---------------+----------------+----------------+
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from console.services import dept_registry, github_reader, state_yaml_reader

# Sprint Maya-blocker Fix 2 (2026-05-21): map step_id → STATE.yaml
# `step_progress.<step_id>` key. The id strings match between the
# console checklist and the eclosure runner's STATE.yaml writes
# (cf. skills/department-onboarding-guide/skill_lib/step_runners/*.py).

router = APIRouter()

# (step_num_in_ui, internal_step_id, display_label)
STEP_DEFS = [
    (1, "mandate", "Mandate"),
    (2, "missions", "Recurring missions"),
    (3, "layers", "Layer mapping"),
    (4, "skills_tools", "Skills & tools"),
    (5, "gates_kpis", "Gates & KPIs"),
    (6, "dry_run", "Dry run"),
    (7, "activation", "Activation"),
]

# Files we surface in the "what she has written" census beyond what
# list_artifacts already covers (Phase G — auto-driving agents drop
# MANDATE.md and similar markers at the repo root).
EXTRA_ROOT_FILES = ["MANDATE.md", "CLAUDE.md", "README.md", "MORNING_BRIEF.md"]


def _build_checklist(d, raw_state: dict | None = None) -> list:
    """Build the per-step checklist. Sprint Maya-blocker Fix 2 (2026-05-21):
    when `raw_state` is supplied, also attach the per-step `current_substep`
    so the template can humanize it ("En ce moment : …")."""
    state = {"validated_steps": d.validated_steps, "status": d.status}
    step_progress = (raw_state or {}).get("step_progress") or {}
    checklist = []
    for num, step_id, label in STEP_DEFS:
        if step_id == "activation":
            status = "validated" if d.status == "Live" else (
                "in_progress" if d.status == "Ready to activate" else "pending")
        else:
            status = state_yaml_reader.step_status(state, step_id)
        substep = None
        if status == "in_progress":
            entry = step_progress.get(step_id) or {}
            cs = entry.get("current_substep")
            if isinstance(cs, dict) and cs.get("type"):
                substep = cs
        checklist.append({
            "num": num, "step_id": step_id, "label": label, "status": status,
            "current_substep": substep,
        })
    return checklist


def _scan_extra_files(slug: str) -> List[str]:
    """Phase G — list root-level markers (MANDATE.md, etc.) that the
    eclosing agent drops alongside dept.yaml.draft. These don't fit in
    the existing list_artifacts categories."""
    root = dept_registry.repo_path(slug)
    if root is None:
        return []
    out = []
    for name in EXTRA_ROOT_FILES:
        if (root / name).exists():
            out.append(name)
    return out


def _read_latest_commit(slug: str) -> tuple[str | None, str | None]:
    """Return (latest_commit_message, relative_age) for the dept repo.
    Returns (None, None) if not a git repo or no commits yet."""
    root = dept_registry.repo_path(slug)
    if root is None or not (root / ".git").exists():
        return None, None
    try:
        msg = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(root), capture_output=True, text=True, timeout=2,
        )
        age = subprocess.run(
            ["git", "log", "-1", "--format=%ar"],
            cwd=str(root), capture_output=True, text=True, timeout=2,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None
    if msg.returncode != 0:
        return None, None
    return msg.stdout.strip() or None, age.stdout.strip() or None


@router.get("/agents/{slug}/onboarding", response_class=HTMLResponse)
def onboarding_view(slug: str, request: Request):
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(404, f"Unknown dept: {slug}")

    repo = dept_registry.repo_path(slug)
    raw_state = state_yaml_reader.read_state_for_repo(repo) if repo else None
    checklist = _build_checklist(d, raw_state)

    # Right pane: artifacts + dept.yaml.draft preview.
    artifacts = github_reader.list_artifacts(slug)
    dept_yaml_raw = github_reader.load_dept_yaml_raw(slug)
    # MANDATE.md verbatim — once Step 1 is validated, {{OPERATOR}} can read the
    # full mandate from this page (msg 3118).
    mandate_md = github_reader.load_mandate_md(slug)

    # Full mission list (id, layer, cadence, description, creates, ...)
    # and per-subscribed-layer PROMPT.md — {{OPERATOR}} reads these mid-éclosion
    # to validate the agent's recurring tasks + day-moment prompts.
    # {{OPERATOR}} flag 2026-05-24 msg 3137.
    missions_full = github_reader.list_missions_full(slug)
    dept_yaml = github_reader.load_dept_yaml(slug)
    layers: list[int] = []
    if isinstance(dept_yaml, dict):
        layers = dept_yaml.get("layers", {}).get("subscribed", []) or []
    layer_prompts = {n: github_reader.load_layer_prompt_md(slug, n)
                     for n in layers}
    # Bucket missions by layer (msg 3142) — consolidates the duplicate
    # "Rendez-vous récurrents" + "Moments" sections into a single
    # Moments section with missions nested inside.
    missions_by_layer = github_reader.group_missions_by_layer(missions_full)

    # Middle pane: chat log for the in-progress step (or last validated step).
    current_step = next(
        (item for item in checklist if item["status"] == "in_progress"),
        next((item for item in reversed(checklist)
              if item["status"] == "validated"), checklist[0]),
    )
    chat_log = github_reader.read_chat_log(
        slug, current_step["num"], current_step["step_id"])

    return request.app.state.templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "dept": d,
            "checklist": checklist,
            "current_step": current_step,
            "chat_log": chat_log,
            "artifacts": artifacts,
            "dept_yaml_raw": dept_yaml_raw,
            "mandate_md": mandate_md,
            "missions_full": missions_full,
            "missions_by_layer": missions_by_layer,
            "layers": layers,
            "layer_prompts": layer_prompts,
        },
    )


@router.get("/agents/{slug}/onboarding/timeline", response_class=HTMLResponse)
def onboarding_timeline_fragment(slug: str, request: Request):
    """Phase G3 — HTMX-polled timeline fragment.

    Reads STATE.yaml fresh on every request (no caching) so polling reflects
    live state. Returns just the <ol> list of step bullets; no <html>/<body>.

    Sprint Maya-blocker Fix 2 (2026-05-21): also threads the per-step
    `current_substep` so the timeline can humanize the in-flight sub-phase
    ("En ce moment : elle te demande comment nommer …").
    """
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    repo = dept_registry.repo_path(slug)
    raw_state = state_yaml_reader.read_state_for_repo(repo) if repo else None
    checklist = _build_checklist(d, raw_state)
    return request.app.state.templates.TemplateResponse(
        "partials/onboarding_timeline.html",
        {"request": request, "dept": d, "checklist": checklist},
    )


@router.get(
    "/agents/{slug}/onboarding/artifacts-fragment",
    response_class=HTMLResponse,
)
def onboarding_artifacts_fragment(slug: str, request: Request):
    """Phase G3 — HTMX-polled artifacts pane fragment.

    Lists STATE.yaml-validated steps as prose + the actual files on disk.
    """
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    artifacts = github_reader.list_artifacts(slug)
    extra_files = _scan_extra_files(slug)
    return request.app.state.templates.TemplateResponse(
        "partials/onboarding_artifacts.html",
        {
            "request": request,
            "dept": d,
            "artifacts": artifacts,
            "extra_files": extra_files,
        },
    )


@router.get(
    "/agents/{slug}/onboarding/heartbeat-fragment",
    response_class=HTMLResponse,
)
def onboarding_heartbeat_fragment(slug: str, request: Request):
    """Phase G3 — HTMX-polled 'dernier signe de vie' fragment.

    Shows the latest commit message + relative age, read via `git log`.
    """
    d = dept_registry.get_department(slug)
    if d is None:
        raise HTTPException(404, f"Unknown dept: {slug}")
    msg, age = _read_latest_commit(slug)
    return request.app.state.templates.TemplateResponse(
        "partials/onboarding_heartbeat.html",
        {
            "request": request,
            "dept": d,
            "last_commit_msg": msg,
            "last_commit_age": age,
        },
    )
