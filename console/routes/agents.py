"""
GET  /agents                          — three-section nav (live + a eclore + anciens)
GET  /agents/new                      — form to bootstrap a new department
POST /agents/new                      — invokes scripts/bootstrap-dept.sh
POST /agents/<slug>/activate          — UX-5: dry-run activation PR preview
                                        (HTMX fragment). With ?confirm=1, opens
                                        the real PR via the token broker.
POST /agents/<slug>/cancel-eclosion   — Sprint Lifecycle Deliverable D: abandon
                                        a pre-Live eclosure. Returns HTMX
                                        fragment with BotFather operator
                                        instructions.
POST /agents/<slug>/retire            — Sprint Lifecycle Deliverable D: retire a
                                        Live dept with dignity. Returns HTMX
                                        fragment with farewell preview.
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from console import settings
from console.services import concierge_reader, dept_registry, eclosure_launcher

# Make scripts/lib importable so we can call cancel_eclosion + retire_dept
# directly from the route handlers (production wiring). Tests mock
# subprocess inside those libs.
_PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_LIB = _PROJ_ROOT / "scripts" / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

router = APIRouter()

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]+$")

# Per-slug event broker for SSE streaming. Each éclosure run creates a
# Queue, pushes events as the launcher emits them, and the SSE handler
# drains the queue to the browser. Multiple concurrent éclosures get
# their own queues. Cleaned up after `done` is dispatched.
_eclosure_queues: Dict[str, "queue.Queue[Dict[str, Any]]"] = {}
_eclosure_lock = threading.Lock()


def _get_or_create_queue(slug: str) -> "queue.Queue[Dict[str, Any]]":
    with _eclosure_lock:
        q = _eclosure_queues.get(slug)
        if q is None:
            q = queue.Queue()
            _eclosure_queues[slug] = q
        return q


def _drop_queue(slug: str) -> None:
    with _eclosure_lock:
        _eclosure_queues.pop(slug, None)


# Pending éclosures: state token (24-byte hex) -> dict of POST form params
# + creation timestamp. The operator POSTs /agents/new, we store the params,
# return a "go to GitHub" link with the state in the query. After the operator
# grants App access, GitHub redirects to /agents/setup-callback?state=<…>
# and we resume the chain. One-shot: state is dropped after first callback.
_pending_eclosures: Dict[str, Dict[str, Any]] = {}
_pending_lock = threading.Lock()
_PENDING_TTL_SECONDS = 24 * 60 * 60  # 24h — humans pace through GitHub auth slowly

# The bubble-ops-bot App installation ID for the operator's account.
# This is the well-known ID of the App's installation on the vdk888 user
# (confirmed via /app/installations on 2026-05-23). It's not a secret —
# operator can see it in github.com/settings/installations URLs.
# Override via env var for tests or future multi-tenant.
BUBBLE_OPS_BOT_INSTALLATION_ID = int(
    os.environ.get("BUBBLE_OPS_BOT_INSTALLATION_ID", "134075326")
)


def _store_pending(state: str, params: Dict[str, Any]) -> None:
    with _pending_lock:
        _pending_eclosures[state] = {**params, "_created_at": time.time()}


def _pop_pending(state: str) -> Optional[Dict[str, Any]]:
    """One-shot lookup. Returns the stored params (without _created_at) or
    None if the state is unknown OR expired."""
    with _pending_lock:
        entry = _pending_eclosures.pop(state, None)
    if entry is None:
        return None
    created = entry.pop("_created_at", 0)
    if (time.time() - created) > _PENDING_TTL_SECONDS:
        return None  # expired — already removed from the dict
    return entry


@router.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    live = dept_registry.live_departments()
    eclore = dept_registry.agents_a_eclore()
    anciens = dept_registry.anciens_collegues()
    concierges = concierge_reader.list_concierges()
    return request.app.state.templates.TemplateResponse(
        "agents.html",
        {
            "request": request,
            "live": live,
            "eclore": eclore,
            "anciens": anciens,
            "concierges": concierges,
        },
    )


@router.get("/agents/new", response_class=HTMLResponse)
def new_dept_form(request: Request):
    return request.app.state.templates.TemplateResponse(
        "agents_new.html",
        {"request": request},
    )


@router.post("/agents/new", response_class=HTMLResponse)
def new_dept_submit(
    request: Request,
    slug: str = Form(...),
    display_name: str = Form(...),
    owner: str = Form(...),
    telegram_bot_token: str = Form(...),
    level: str = Form("ops"),
    children: str = Form(""),
):
    """Full éclosure on a single click:
      1. Validate inputs (incl. Telegram bot token shape + level + children).
      2. Run bootstrap-dept.sh (scaffold + GitHub repo + clone).
      3. Hand off to eclosure_launcher.launch() in a background thread
         that emits progress events to the per-slug queue.
      4. Return the HTMX result fragment immediately with an SSE pointer
         (`/agents/<slug>/eclosure-stream`) so the browser can watch
         progress in real time.

    Wave-3 Step 0 (2026-05-23): added `level` and `children` form fields
    so management depts (Tony, future principal-aggregators) can be
    éclos through the console. See Team A's scaffold for the underlying
    --level=ops|management and --children=<comma,sep> CLI surface.
    """
    if not SLUG_RE.match(slug):
        raise HTTPException(400, f"Invalid slug: {slug!r} (need ^[a-z][a-z0-9-]+$)")
    if not display_name.strip():
        raise HTTPException(400, "display_name required")
    if not owner.strip():
        raise HTTPException(400, "owner required")
    if not eclosure_launcher.is_valid_telegram_bot_token(telegram_bot_token):
        raise HTTPException(
            400,
            "telegram_bot_token has invalid shape "
            "(expected <8-11 digits>:<30+ chars of A-Za-z0-9_->)",
        )

    # Level + children validation (Wave-3 Step 0)
    level = (level or "ops").strip().lower()
    if level not in {"ops", "management"}:
        raise HTTPException(
            400, f"Invalid level: {level!r} (must be 'ops' or 'management')",
        )

    # Normalize children: split on comma, strip whitespace, drop empties.
    children_list = [c.strip() for c in (children or "").split(",") if c.strip()]
    if level == "management" and not children_list:
        raise HTTPException(
            400,
            "a management department must list at least one child "
            "(children=ben,maya,... — comma-separated)",
        )
    if level == "ops" and children_list:
        raise HTTPException(
            400,
            "an ops department cannot list children; "
            "remove the children field or set level=management",
        )
    # Each child slug must satisfy the same kebab-case rule as the dept slug.
    for c in children_list:
        if not SLUG_RE.match(c):
            raise HTTPException(
                400,
                f"Invalid child slug: {c!r} (need ^[a-z][a-z0-9-]+$)",
            )

    # Wave-3 Step 0b refactor (2026-05-23 evening) — DEFER bootstrap to the
    # GitHub App "Setup URL" callback flow. Reasoning: the broker's PAT
    # cannot CREATE new repos under vdk888 (fine-grained scope), but the
    # bubble-ops-bot GitHub App has `Administration: write` since Joris
    # configured it. So we let the operator drive the App authorization
    # via the standard GitHub redirect dance:
    #
    #   1. Store the form params in _pending_eclosures keyed by a fresh
    #      state token.
    #   2. Render an HTMX fragment with a "Continue on GitHub" link to
    #      https://github.com/apps/bubble-ops-bot/installations/<INST_ID>?state=<…>
    #   3. Operator clicks → grants App access to the new repo → GitHub
    #      redirects to our /agents/setup-callback?installation_id=…&
    #      setup_action=update&state=<…>.
    #   4. Callback pops the state, kicks off the éclosure chain (which
    #      uses the App installation token to CREATE the repo + push).
    state = secrets.token_hex(24)
    _store_pending(state, {
        "slug": slug,
        "display_name": display_name,
        "owner": owner,
        "telegram_bot_token": telegram_bot_token,
        "level": level,
        "children_list": children_list,
    })

    # GitHub fires "Redirect on update" ONLY on actual config changes.
    # We must drive the operator through the "add repositories" flow so
    # GitHub knows to redirect back to our Setup URL with installation_id
    # + setup_action + state as query params. Two URL shapes work:
    #   - /apps/<name>/installations/new   → forces install flow, but a
    #     user with the App already installed gets "already installed"
    #     and a Configure button that goes to /installations/<id>.
    #   - /apps/<name>/installations/<id>  → opens the existing install
    #     in CONFIGURE mode. The redirect only fires if the operator
    #     clicks "Save" after a real change (add/remove a repo).
    # Bug caught 2026-05-24 msg 3067: we used /<id> and the operator
    # opened+closed without changing anything → GitHub silently kept the
    # state token in the URL bar but never called our callback.
    # /installations/new is more reliable because GitHub treats the
    # add-repo step as a config change and ALWAYS redirects.
    install_url = (
        f"https://github.com/apps/bubble-ops-bot/installations/new"
        f"?state={state}"
    )

    return request.app.state.templates.TemplateResponse(
        "agents_new_result.html",
        {
            "request": request,
            "slug": slug,
            "display_name": display_name,
            "ok": True,
            "output": "",
            "stream_url": None,           # not yet — fired by callback
            "github_app": None,
            "install_url": install_url,   # NEW: link the operator clicks next
            "state": state,
        },
    )


@router.get("/agents/setup-callback", response_class=HTMLResponse)
def setup_callback(
    request: Request,
    state: str = "",
    installation_id: str = "",
    setup_action: str = "",
):
    """GitHub redirects here after the operator grants bubble-ops-bot access
    to the new dept's repo. We look up the pending éclosure by state,
    kick off the chain, and redirect the browser to the SSE result page.

    Per the GitHub Apps docs, the `installation_id` parameter is operator-
    spoofable, so we do NOT trust it for auth. Trust comes from the
    `state` token (24-byte random, one-shot, server-generated).
    """
    if not state:
        raise HTTPException(400, "Missing state parameter")
    if not installation_id:
        raise HTTPException(400, "Missing installation_id parameter")

    params = _pop_pending(state)
    if params is None:
        raise HTTPException(
            400,
            "Unknown or expired state. Did the éclosure session time out (>15 min)? "
            "Go back to /agents/new and start again.",
        )

    slug = params["slug"]
    telegram_bot_token = params["telegram_bot_token"]
    # Parse installation_id into an int (GitHub sends it as string)
    try:
        inst_id_int = int(installation_id)
    except ValueError:
        raise HTTPException(400, f"installation_id must be an integer, got {installation_id!r}")

    # Kick off the éclosure chain in a background thread (same pattern as
    # the old POST flow).
    q = _get_or_create_queue(slug)

    def _runner() -> None:
        try:
            eclosure_launcher.launch(
                slug=slug,
                telegram_bot_token=telegram_bot_token,
                installation_id=inst_id_int,
                level=params.get("level", "ops"),
                children_list=params.get("children_list", []),
                display_name=params.get("display_name", slug),
                owner=params.get("owner", "joris"),
                on_progress=q.put,
            )
        except Exception as exc:  # noqa: BLE001
            q.put({"kind": "error", "slug": slug, "message": str(exc)[:200]})
        finally:
            q.put({"kind": "_terminate"})

    threading.Thread(target=_runner, daemon=True).start()

    # Render the result page (reuses the existing SSE-progress template).
    return request.app.state.templates.TemplateResponse(
        "agents_new_result.html",
        {
            "request": request,
            "slug": slug,
            "display_name": params.get("display_name", slug),
            "ok": True,
            "output": "",
            "stream_url": f"/agents/{slug}/eclosure-stream",
            "github_app": None,
            "install_url": None,
            "state": None,
        },
    )


@router.get("/agents/{slug}/eclosure-stream")
def eclosure_stream(slug: str, request: Request) -> StreamingResponse:
    """Server-Sent Events stream of éclosure progress.

    Subscribes to the per-slug queue created by POST /agents/new and
    relays events as SSE messages until the launcher emits a
    `_terminate` sentinel (or 120s pass with no events).
    """
    if not SLUG_RE.match(slug):
        raise HTTPException(400, f"Invalid slug: {slug!r}")

    q = _get_or_create_queue(slug)

    def event_iter():
        # Prime with a heartbeat so the connection is unambiguously open.
        yield "event: connected\ndata: {\"slug\":\"" + slug + "\"}\n\n"
        idle_seconds = 0
        max_idle = 120  # close after 2 min of silence
        while True:
            try:
                ev = q.get(timeout=2.0)
            except queue.Empty:
                idle_seconds += 2
                if idle_seconds >= max_idle:
                    yield "event: timeout\ndata: {}\n\n"
                    break
                # heartbeat to keep proxies awake
                yield ": ping\n\n"
                continue
            idle_seconds = 0
            if ev.get("kind") == "_terminate":
                yield "event: close\ndata: {}\n\n"
                _drop_queue(slug)
                break
            payload = json.dumps(ev, ensure_ascii=False)
            yield f"event: progress\ndata: {payload}\n\n"

    return StreamingResponse(
        event_iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# UX-5: POST /agents/<slug>/activate
# ---------------------------------------------------------------------------


ACTIVATE_SCRIPT = settings.SCRIPTS_DIR / "activate-dept.sh"


@router.post("/agents/{slug}/activate", response_class=HTMLResponse)
def activate_dept(
    request: Request,
    slug: str,
    confirm: int = 0,
):
    """Run activate-dept.sh and return its output as an HTMX fragment.

    Default (confirm=0): runs with --dry-run, returns the rendered PR
    body for the operator to preview.

    confirm=1: opens the real PR via the broker. Same 3-pane page can
    then refresh.

    Returns:
      200  preview rendered, contains PR body + confirm button
      404  unknown dept slug
      409  dept exists but is not in `Ready to activate` status
      500  underlying script failed for some other reason
    """
    if not SLUG_RE.match(slug):
        raise HTTPException(400, f"Invalid slug: {slug!r}")
    dept = dept_registry.get_department(slug)
    if dept is None:
        raise HTTPException(404, f"Unknown dept: {slug!r}")
    if dept.status != "Ready to activate":
        raise HTTPException(
            409,
            f"Dept {slug!r} status is {dept.status!r}; "
            f"must be 'Ready to activate' first.",
        )

    repo = dept_registry.repo_path(slug)
    if repo is None:
        raise HTTPException(500, f"Dept {slug!r} repo not found on disk")

    cmd = [
        "bash", str(ACTIVATE_SCRIPT),
        f"--slug={slug}",
        f"--repo-dir={repo}",
    ]
    if not confirm:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(500, f"activate-dept.sh not found: {exc}")

    if result.returncode == 2:
        # can_activate blocked. Surface blockers.
        raise HTTPException(
            409,
            "activate-dept blocked:\n" + (result.stderr or result.stdout),
        )
    if result.returncode != 0:
        raise HTTPException(
            500,
            f"activate-dept.sh exited {result.returncode}: "
            f"{(result.stderr or result.stdout)[:500]}",
        )

    return request.app.state.templates.TemplateResponse(
        "partials/activation_preview.html",
        {
            "request": request,
            "slug": slug,
            "display_name": dept.display_name,
            "pr_body": result.stdout,
            "is_preview": not bool(confirm),
        },
    )


# ---------------------------------------------------------------------------
# Sprint Lifecycle Deliverable D
# ---------------------------------------------------------------------------


@router.post("/agents/{slug}/cancel-eclosion", response_class=HTMLResponse)
def cancel_eclosion_route(request: Request, slug: str):
    """Cancel an in-flight eclosure (pre-Live only).

    Returns:
      200  HTMX fragment confirming cancellation + BotFather instructions
      404  unknown slug
      409  dept is Live (operator must use retire-dept instead)
      500  underlying lib raised
    """
    if not SLUG_RE.match(slug):
        raise HTTPException(400, f"Invalid slug: {slug!r}")
    dept = dept_registry.get_department(slug)
    if dept is None:
        raise HTTPException(404, f"Unknown dept: {slug!r}")

    repo = dept_registry.repo_path(slug)
    if repo is None:
        raise HTTPException(500, f"Dept {slug!r} repo not found on disk")

    # Direct in-process invocation — we get a structured result back
    # without shelling out (cleaner than parsing the script's stdout).
    import cancel_eclosion as _ce  # type: ignore

    try:
        result = _ce.cancel_eclosion(slug=slug, repo_dir=repo)
    except Exception as exc:  # noqa: BLE001 — surface anything cleanly
        raise HTTPException(500, f"cancel_eclosion raised: {exc}")

    if result["status"] == "blocked":
        # 409 — operator must use a different verb (retire-dept).
        raise HTTPException(
            409,
            "cancel-eclosion blocked: " + " | ".join(result["reasons"]),
        )

    return request.app.state.templates.TemplateResponse(
        "partials/cancel_eclosion_fragment.html",
        {
            "request": request,
            "slug": slug,
            "display_name": dept.display_name,
            "operator_instructions": result["operator_instructions"],
        },
    )


@router.post("/agents/{slug}/retire", response_class=HTMLResponse)
def retire_route(request: Request, slug: str,
                 reason: str = Form("Decommissioned")):
    """Retire a Live dept with dignity.

    Returns:
      200  HTMX fragment with the farewell preview
      404  unknown slug
      409  dept is not Live (operator must use cancel-eclosion instead)
      500  underlying lib raised
    """
    if not SLUG_RE.match(slug):
        raise HTTPException(400, f"Invalid slug: {slug!r}")
    dept = dept_registry.get_department(slug)
    if dept is None:
        raise HTTPException(404, f"Unknown dept: {slug!r}")

    repo = dept_registry.repo_path(slug)
    if repo is None:
        raise HTTPException(500, f"Dept {slug!r} repo not found on disk")

    import retire_dept as _rd  # type: ignore

    try:
        result = _rd.retire_dept(slug=slug, repo_dir=repo, reason=reason)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"retire_dept raised: {exc}")

    if result["status"] == "blocked":
        raise HTTPException(
            409,
            "retire-dept blocked: " + " | ".join(result["reasons"]),
        )

    return request.app.state.templates.TemplateResponse(
        "partials/retire_dept_fragment.html",
        {
            "request": request,
            "slug": slug,
            "display_name": dept.display_name,
            "final_telegram_msg": result["final_telegram_msg"],
        },
    )
