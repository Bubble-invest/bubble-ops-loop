"""
bubble-ops-console — FastAPI app entry point.

Single binary serving the 7 routes described in Notion v5 lines 1004-1041.
Auth: bearer token via `Authorization: Bearer <CONSOLE_BEARER_TOKEN>`.
Default bind: 127.0.0.1:8642 (Tailscale-tunneled by the operator).

Run locally:
    uvicorn console.main:app --reload --host 127.0.0.1 --port 8642

See deploy/README.md for the Tailscale exposure recipe.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import RedirectResponse

from console import settings
from console.routes import (
    agents, auth, concierge, costs, dept, dept_session, gate, health, home,
    kanban, onboarding, thesis_book,
)
from console.routes import settings as settings_route
from console.services import sessions

_log = logging.getLogger("console.main")


def create_app() -> FastAPI:
    app = FastAPI(
        title="bubble-ops-console",
        version="0.1.0-ux3",
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    templates = Jinja2Templates(directory=str(settings.TEMPLATES_DIR))
    # Expose humanize_kind helper to all templates (Item E1 polish, msg 2709).
    from console.services.humanize import (  # noqa: WPS433
        capitalize_fr, format_gate_age, gate_age_is_stale, gate_channel,
        gate_human_title, humanize_cadence, humanize_channel,
        humanize_future_modes, humanize_kind, humanize_mode, humanize_risk,
        humanize_substep, shadow_autonomy_label,
    )
    templates.env.globals["humanize_kind"] = humanize_kind
    templates.env.globals["humanize_risk"] = humanize_risk
    templates.env.globals["humanize_mode"] = humanize_mode
    templates.env.globals["humanize_future_modes"] = humanize_future_modes
    templates.env.globals["humanize_substep"] = humanize_substep
    templates.env.globals["humanize_cadence"] = humanize_cadence
    templates.env.globals["shadow_autonomy_label"] = shadow_autonomy_label
    templates.env.globals["capitalize_fr"] = capitalize_fr
    templates.env.globals["format_gate_age"] = format_gate_age
    templates.env.globals["gate_age_is_stale"] = gate_age_is_stale
    templates.env.globals["gate_channel"] = gate_channel
    templates.env.globals["gate_human_title"] = gate_human_title
    templates.env.globals["humanize_channel"] = humanize_channel
    # Auto cache-buster for /static/style.css: tie the ?v= to the file's mtime
    # so every CSS change busts the browser cache automatically. Without this,
    # a hand-bumped ?v= string goes stale and deployed CSS fixes look "reverted"
    # until users manually hard-refresh (bit us on the 4-moments fix, 2026-08-12).
    def _css_version() -> str:
        try:
            return str(int((settings.STATIC_DIR / "style.css").stat().st_mtime))
        except OSError:
            return "0"
    templates.env.globals["css_version"] = _css_version
    # Expose dept_registry + sidebar_agents for navigation ({{OPERATOR}} 2026-06-09)
    from console.services import dept_registry  # noqa: WPS433
    templates.env.globals["dept_registry"] = dept_registry
    templates.env.globals["sidebar_agents"] = dept_registry.sidebar_agents  # called fresh each render
    templates.env.filters["humanize_kind"] = humanize_kind
    templates.env.filters["humanize_risk"] = humanize_risk
    templates.env.filters["humanize_mode"] = humanize_mode
    templates.env.filters["humanize_future_modes"] = humanize_future_modes
    templates.env.filters["humanize_substep"] = humanize_substep
    templates.env.filters["humanize_cadence"] = humanize_cadence
    templates.env.filters["capitalize_fr"] = capitalize_fr
    app.state.templates = templates

    if settings.STATIC_DIR.exists():
        app.mount("/static",
                  StaticFiles(directory=str(settings.STATIC_DIR)),
                  name="static")

    # --- auth middleware -----------------------------------------------
    # Credential carriers, in priority order:
    #   1. `Authorization: Bearer <token>`   — API / curl / CI (kept).
    #   2. `console_session` cookie           — the login-page opaque session
    #                                           (sliding; board #997 option C).
    #   3. `?token=<token>` query param       — first-hit bootstrap: upgraded
    #                                           to MINT a session + set the
    #                                           opaque cookie (no longer stores
    #                                           the raw bearer in a cookie).
    #   4. legacy `console_token` cookie == bearer — back-compat during cutover
    #                                           so browsers still holding the
    #                                           old raw-bearer cookie aren't
    #                                           kicked out the moment this ships.
    # `request.state.user` is set to the acting identity (a login username, or
    # "bearer" for header/legacy access) so decision handlers can attribute
    # approve/reject to the person (per-user login → per-name audit).
    # Unauthenticated browser (HTML) requests → redirect to /login; htmx →
    # 401 + HX-Redirect; API → 401 JSON.
    LEGACY_COOKIE = "console_token"
    SESSION_COOKIE = settings.SESSION_COOKIE
    _PUBLIC_PREFIXES = ("/static/",)
    _PUBLIC_EXACT = {
        "/health-noauth",       # tailscale liveness ping (see route below)
        "/agents/setup-callback",  # GitHub App redirect — auth'd by one-shot state
        "/login", "/login/link", "/logout",  # you can't be logged in to log in
    }

    def _needs_login(request: Request) -> Response:
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        login_url = f"/login?next={quote(nxt, safe='')}"
        if request.headers.get("hx-request", "").lower() == "true":
            return Response(status_code=401, headers={"HX-Redirect": login_url})
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url=login_url, status_code=303)
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    def _session_cookie(sid: str):
        return dict(key=SESSION_COOKIE, value=sid, httponly=True, secure=True,
                    samesite="lax", max_age=settings.SESSION_IDLE_SECONDS, path="/")

    @app.middleware("http")
    async def auth_mw(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if path in _PUBLIC_EXACT or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        token = settings.BEARER_TOKEN

        # 1. Authorization: Bearer header (API / CI). A present-but-wrong header
        #    is rejected outright (no fall-through to cookies).
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            if token and header.split(" ", 1)[1].strip() == token:
                request.state.user = "bearer"
                return await call_next(request)
            return JSONResponse({"detail": "Invalid bearer token"}, status_code=401)

        # 2. Opaque session cookie (the login-page path).
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            user = sessions.validate_and_touch(sid)
            if user is not None:
                request.state.user = user
                resp = await call_next(request)
                resp.set_cookie(**_session_cookie(sid))  # slide the browser cookie too
                return resp

        # 3. ?token= bootstrap → mint a session, set the opaque cookie, clean URL.
        qp_token = request.query_params.get("token")
        if qp_token and token and qp_token.strip() == token:
            new_sid = sessions.create_session("bearer-bootstrap")
            qp = {k: v for k, v in request.query_params.items() if k != "token"}
            qs = "&".join(f"{k}={v}" for k, v in qp.items())
            clean_url = request.url.path + (f"?{qs}" if qs else "")
            resp = RedirectResponse(url=clean_url, status_code=303)
            resp.set_cookie(**_session_cookie(new_sid))
            return resp

        # 4. Legacy raw-bearer cookie (back-compat during cutover).
        legacy = request.cookies.get(LEGACY_COOKIE)
        if legacy and token and legacy == token:
            request.state.user = "bearer"
            return await call_next(request)

        # Unauthenticated.
        if not token and not sessions.login_configured():
            return JSONResponse(
                {"detail": "No auth configured on server (set CONSOLE_BEARER_TOKEN "
                           "or CONSOLE_LOGIN_USERS)"},
                status_code=503,
            )
        return _needs_login(request)

    # --- routes --------------------------------------------------------
    app.include_router(auth.router)
    app.include_router(home.router)
    app.include_router(dept.router)
    app.include_router(gate.router)
    app.include_router(settings_route.router)
    app.include_router(health.router)
    app.include_router(costs.router)
    app.include_router(agents.router)
    app.include_router(onboarding.router)
    app.include_router(concierge.router)
    app.include_router(dept_session.router)
    app.include_router(kanban.router)
    app.include_router(thesis_book.router)

    # unauthenticated liveness probe (tailscale only — see middleware)
    @app.get("/health-noauth")
    def health_noauth() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
