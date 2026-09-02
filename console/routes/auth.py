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

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from console import settings
from console.services import sessions

router = APIRouter()


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
    if sessions.verify_login(username.strip(), password):
        sid = sessions.create_session(username.strip() or "operator")
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
    # Generic failure (no username/password distinction — no enumeration hint).
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


@router.get("/login/link")
def login_link(request: Request):
    """Passwordless one-time login (board #997). The operator opens a link
    `/login/link?t=<token>` (sent privately over Telegram); a valid, unused,
    unexpired token mints a session as its user and drops the token. Lets Rick
    enroll Joris/Jade with nothing to type — works on Mac and iPhone alike."""
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
