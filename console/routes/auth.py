"""/login + /logout — the cockpit's password login (board #997 option C).

The global auth middleware in ``main.py`` bypasses these paths (you can't be
logged in to log in). On success we mint an opaque server-side session and set
the ``console_session`` cookie; the middleware admits subsequent requests by
that cookie (sliding), never by the raw bearer.

The form is a plain full-page POST with standard ``autocomplete`` attributes so
Safari/Chrome offer to save the credential to the macOS Keychain, which iCloud
Keychain then syncs to iPhone — the operator's "auto on open, Mac + iPhone" ask.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from console import settings
from console.services import sessions

router = APIRouter()
_log = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """Best-effort client IP for the login throttle's per-(user, ip) key.

    board #1116: deliberately does NOT trust X-Forwarded-For/X-Real-IP — the
    console binds directly (Tailscale-only, no reverse proxy in front of it;
    see settings.BIND_HOST's docstring), so those headers are attacker-
    settable with nothing to strip them. request.client.host is what the
    ASGI server itself observed the TCP peer to be — not spoofable from the
    request body/headers.
    """
    return request.client.host if request.client else "unknown"


def safe_next(raw: str | None) -> str:
    """Only allow a same-site absolute path as the post-login redirect.

    Rejects protocol-relative (``//host``), scheme (``https://evil``), and
    anything not starting with a single ``/`` — so a crafted ``?next=`` can't
    turn login into an open redirect.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    # Already authenticated → bounce straight through.
    sid = request.cookies.get(settings.SESSION_COOKIE)
    if sid and sessions.validate_and_touch(sid):
        return RedirectResponse(safe_next(request.query_params.get("next")), status_code=303)
    return request.app.state.templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "next": safe_next(request.query_params.get("next")),
            "error": None,
            "login_configured": sessions.login_configured(),
        },
    )


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    nxt = safe_next(next)
    uname = username.strip()
    ip = _client_ip(request)

    def _reject() -> HTMLResponse:
        # Generic failure (no username/password distinction, and — board
        # #1116 — no throttled-vs-wrong-password distinction either: SAME
        # template, status code, and error message either way, so an
        # attacker probing the throttle can't use the response to tell
        # "still guessing" apart from "now being rate-limited".
        return request.app.state.templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "next": nxt,
                "error": "Identifiants invalides.",
                "login_configured": sessions.login_configured(),
            },
            status_code=401,
        )

    # board #1116: per-(username, client-IP) exponential backoff (5 free
    # fails, then doubling, capped at 5min — see sessions.py). Checked
    # BEFORE verify_login so a throttled flood never pays for (or triggers)
    # another full-cost 600k-iter pbkdf2 verify.
    if sessions.login_retry_after_seconds(uname, ip) > 0:
        _log.warning("login throttled user=%s ip=%s", uname, ip)
        return _reject()

    if sessions.verify_login(uname, password):
        sessions.clear_login_failures(uname, ip)
        sid = sessions.create_session(uname or "operator")
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(
            key=settings.SESSION_COOKIE,
            value=sid,
            httponly=True,
            secure=True,          # tailnet is TLS-only, never clearnet
            samesite="lax",
            max_age=settings.SESSION_IDLE_SECONDS,
            path="/",
        )
        return resp

    sessions.record_login_failure(uname, ip)
    _log.warning("login failed user=%s ip=%s", uname, ip)
    return _reject()


@router.get("/login/link")
def login_link(request: Request):
    """Passwordless one-time login (board #997). The operator opens a link
    `/login/link?t=<token>` (sent privately over Telegram); a valid, unused,
    unexpired token mints a session as its user and drops the token. Lets Rick
    enroll Joris/Jade with nothing to type — works on Mac and iPhone alike.

    Residual caveat (#1073 review finding 1): this MUST stay a plain GET —
    Telegram links have to be click-openable, no JS/POST round-trip — which
    means a link-preview bot or prefetching proxy that GETs the URL ahead of
    the human still burns the single-use token (`consume_login_token` is
    atomic, so at most one opener wins; the human just loses the race). Not
    fixable without breaking "click to log in"; mitigated by (a) redacting
    the token out of the access log so it isn't a *second* leak vector and
    (b) a short 15min TTL so a lost race is cheap to recover from (mint a new
    link) rather than a standing exposure.
    """
    user = sessions.consume_login_token(request.query_params.get("t", ""))
    if user is None:
        return request.app.state.templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "next": "/",
                "error": "Lien invalide, déjà utilisé ou expiré. Demande un nouveau lien.",
                "login_configured": sessions.login_configured(),
            },
            status_code=401,
        )
    sid = sessions.create_session(user)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        key=settings.SESSION_COOKIE,
        value=sid,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.SESSION_IDLE_SECONDS,
        path="/",
    )
    return resp


@router.get("/logout")
@router.post("/logout")
def logout(request: Request):
    sid = request.cookies.get(settings.SESSION_COOKIE)
    if sid:
        sessions.revoke(sid)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(settings.SESSION_COOKIE, path="/")
    return resp
