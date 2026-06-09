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

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from console import settings
from console.routes import (
    agents, concierge, costs, dept, dept_session, gate, health, home, onboarding,
)
from console.routes import settings as settings_route

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
        capitalize_fr, humanize_cadence, humanize_future_modes,
        humanize_kind, humanize_mode, humanize_risk, humanize_substep,
        shadow_autonomy_label,
    )
    templates.env.globals["humanize_kind"] = humanize_kind
    templates.env.globals["humanize_risk"] = humanize_risk
    templates.env.globals["humanize_mode"] = humanize_mode
    templates.env.globals["humanize_future_modes"] = humanize_future_modes
    templates.env.globals["humanize_substep"] = humanize_substep
    templates.env.globals["humanize_cadence"] = humanize_cadence
    templates.env.globals["shadow_autonomy_label"] = shadow_autonomy_label
    templates.env.globals["capitalize_fr"] = capitalize_fr
    # Expose dept_registry + sidebar_agents for navigation (Joris 2026-06-09)
    from console.services import dept_registry  # noqa: WPS433
    templates.env.globals["dept_registry"] = dept_registry
    templates.env.globals["sidebar_agents"] = dept_registry.sidebar_agents()
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

    # --- bearer auth middleware ----------------------------------------
    # 3 accepted credential carriers, in priority order:
    #   1. `Authorization: Bearer <token>`        (curl / tests / CI)
    #   2. `?token=<token>` query param           (first-time browser visit)
    #   3. `console_token` HttpOnly cookie        (subsequent browser nav)
    # On a valid query-param hit we redirect to the same URL without the
    # token (so it doesn't sit in browser history) and set the cookie.
    COOKIE_NAME = "console_token"

    @app.middleware("http")
    async def bearer_auth(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # /health-noauth is intentionally unauthenticated for tailscale ping.
        if request.url.path == "/health-noauth":
            return await call_next(request)
        # /static/* is public — browsers load CSS/fonts/images on follow-up
        # requests without the bearer header, so 401 here breaks the page
        # render. The static dir contains only design assets, never operator
        # data. Caught during the Bureau-de-Cadre UX smoke (msg 2700, 2026-05-21).
        if request.url.path.startswith("/static/"):
            return await call_next(request)
        # /agents/setup-callback is GitHub's redirect target after the
        # operator authorizes the bubble-ops-bot App. GitHub cannot
        # supply our bearer token — the auth on this endpoint is the
        # one-shot `state` query param (24-byte random, server-issued,
        # popped from _pending_eclosures on first hit). Bypass the
        # bearer gate so GitHub's redirect can reach us.
        # Caught 2026-05-24 (msg 3089): operator hit 401 here right
        # after granting Bubble-invest org access to the App.
        if request.url.path == "/agents/setup-callback":
            return await call_next(request)

        token = settings.BEARER_TOKEN
        if not token:
            return JSONResponse(
                {"detail": "CONSOLE_BEARER_TOKEN not set on server"},
                status_code=503,
            )

        # Try Authorization header first
        header = request.headers.get("authorization", "")
        supplied = None
        if header.lower().startswith("bearer "):
            supplied = header.split(" ", 1)[1].strip()

        # Then try ?token= query param (first browser hit)
        set_cookie_and_redirect = False
        if supplied is None:
            qp_token = request.query_params.get("token")
            if qp_token:
                supplied = qp_token.strip()
                set_cookie_and_redirect = True

        # Finally try cookie (subsequent navigation)
        if supplied is None:
            supplied = request.cookies.get(COOKIE_NAME)

        if not supplied:
            return JSONResponse({"detail": "Missing bearer token"},
                                status_code=401)
        if supplied != token:
            return JSONResponse({"detail": "Invalid bearer token"},
                                status_code=401)

        # If the token came from the query param, set a cookie and redirect
        # to the same URL stripped of the token (clean URL, no token in history).
        if set_cookie_and_redirect:
            from starlette.responses import RedirectResponse
            qp = dict(request.query_params)
            qp.pop("token", None)
            qs = "&".join(f"{k}={v}" for k, v in qp.items())
            clean_url = request.url.path + (f"?{qs}" if qs else "")
            resp = RedirectResponse(url=clean_url, status_code=303)
            resp.set_cookie(
                key=COOKIE_NAME,
                value=token,
                httponly=True,
                secure=True,           # tailnet uses TLS, no clearnet exposure
                samesite="lax",
                max_age=60 * 60 * 24 * 30,   # 30 days
                path="/",
            )
            return resp

        return await call_next(request)

    # --- routes --------------------------------------------------------
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

    # unauthenticated liveness probe (tailscale only — see middleware)
    @app.get("/health-noauth")
    def health_noauth() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app


app = create_app()
