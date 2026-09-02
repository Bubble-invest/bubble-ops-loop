"""test_auth_login.py — the /login page + opaque sliding session (board #997 C).

Covers: pbkdf2 hashing, per-user vs shared password verification, the SQLite
session store (create/validate/slide/revoke/expire), and the auth middleware's
new paths (session cookie, unauth HTML→/login vs API→401, ?token bootstrap
upgrade, legacy raw-bearer cookie back-compat during cutover).

Session-cookie round-trips use an explicit ``Cookie`` header rather than the
TestClient jar: the cookie is ``secure=True`` (tailnet is TLS-only) and the test
transport is plain http, so httpx would not resend it on its own.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from console import settings
from console.services import sessions


def _hash(pw: str) -> str:
    return sessions.hash_password(pw)


# ---- password hashing ------------------------------------------------

def test_password_hash_roundtrip():
    h = sessions.hash_password("s3cret-pw")
    assert h.startswith("pbkdf2_sha256$")
    assert sessions.verify_password("s3cret-pw", h)
    assert not sessions.verify_password("wrong-pw", h)
    # malformed encodings never raise, just fail closed
    assert not sessions.verify_password("s3cret-pw", "garbage")
    assert not sessions.verify_password("s3cret-pw", "")


# ---- credential verification -----------------------------------------

def test_verify_login_per_user(monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_USERS_JSON",
                        json.dumps({"joris": _hash("alpha"), "jade": _hash("beta")}))
    monkeypatch.setattr(settings, "LOGIN_PASSWORD_HASH", "")
    assert sessions.verify_login("joris", "alpha")
    assert sessions.verify_login("jade", "beta")
    assert not sessions.verify_login("joris", "beta")     # right user, wrong pw
    assert not sessions.verify_login("ghost", "alpha")    # unknown user
    assert sessions.login_configured()


def test_verify_login_shared_fallback(monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_USERS_JSON", "")
    monkeypatch.setattr(settings, "LOGIN_PASSWORD_HASH", _hash("shared-pw"))
    assert sessions.verify_login("anyone", "shared-pw")   # username ignored
    assert not sessions.verify_login("anyone", "nope")


def test_verify_login_none_configured(monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_USERS_JSON", "")
    monkeypatch.setattr(settings, "LOGIN_PASSWORD_HASH", "")
    assert not sessions.verify_login("joris", "whatever")
    assert not sessions.login_configured()


def test_verify_login_malformed_users_json_degrades(monkeypatch):
    monkeypatch.setattr(settings, "LOGIN_USERS_JSON", "{not valid json")
    monkeypatch.setattr(settings, "LOGIN_PASSWORD_HASH", "")
    assert not sessions.verify_login("joris", "alpha")   # no crash, just fails


# ---- opaque session store --------------------------------------------

@pytest.fixture
def tmp_session_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SESSION_DB_PATH", tmp_path / "sessions.db")
    return tmp_path


def test_session_lifecycle(tmp_session_db):
    sid = sessions.create_session("joris")
    assert isinstance(sid, str) and len(sid) > 20
    assert sessions.validate_and_touch(sid) == "joris"    # valid → username
    sessions.revoke(sid)
    assert sessions.validate_and_touch(sid) is None       # revoked
    assert sessions.validate_and_touch("nonexistent") is None
    assert sessions.validate_and_touch("") is None


def test_session_expiry(monkeypatch, tmp_session_db):
    monkeypatch.setattr(settings, "SESSION_IDLE_SECONDS", -1)  # expire on creation
    sid = sessions.create_session("jade")
    assert sessions.validate_and_touch(sid) is None


def test_login_token_single_use(tmp_session_db):
    tok = sessions.create_login_token("joris")
    assert sessions.consume_login_token(tok) == "joris"   # first use → username
    assert sessions.consume_login_token(tok) is None       # single-use → gone
    assert sessions.consume_login_token("nope") is None
    assert sessions.consume_login_token("") is None


def test_login_token_expiry(tmp_session_db):
    tok = sessions.create_login_token("jade", ttl_seconds=-1)  # already expired
    assert sessions.consume_login_token(tok) is None


def test_session_absolute_cap(monkeypatch, tmp_session_db):
    # idle window huge, absolute cap already exceeded → still expires
    monkeypatch.setattr(settings, "SESSION_IDLE_SECONDS", 10_000)
    monkeypatch.setattr(settings, "SESSION_ABSOLUTE_SECONDS", -1)
    sid = sessions.create_session("joris")
    # validate slides expires to min(now+idle, created+absolute) = created-1 < now
    assert sessions.validate_and_touch(sid) is None


# ---- middleware + routes ---------------------------------------------
# NOTE: the `app` fixture purges + re-imports console.* to re-read env, so a
# top-level `from console import settings` binding is STALE for these tests. We
# grab the LIVE settings/sessions from sys.modules (the ones the running app
# uses) and monkeypatch those — see login_ctx.

@pytest.fixture
def login_ctx(monkeypatch, tmp_path, app):
    """TestClient (no auth header) + per-user login + temp session DB, wired
    onto the LIVE console modules the app actually imports."""
    import sys
    from types import SimpleNamespace
    live_settings = sys.modules["console.settings"]
    live_sessions = sys.modules["console.services.sessions"]
    monkeypatch.setattr(
        live_settings, "LOGIN_USERS_JSON",
        json.dumps({"joris": live_sessions.hash_password("alpha"),
                    "jade": live_sessions.hash_password("beta")}))
    monkeypatch.setattr(live_settings, "LOGIN_PASSWORD_HASH", "")
    monkeypatch.setattr(live_settings, "SESSION_DB_PATH", tmp_path / "s.db")
    return SimpleNamespace(client=TestClient(app),
                           settings=live_settings, sessions=live_sessions)


def test_login_page_renders(login_ctx):
    r = login_ctx.client.get("/login")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert 'autocomplete="current-password"' in r.text     # Keychain autofill
    assert 'autocomplete="username"' in r.text


def test_unauth_html_redirects_to_login(login_ctx):
    r = login_ctx.client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")


def test_unauth_api_returns_401(login_ctx):
    r = login_ctx.client.get("/", headers={"Accept": "application/json"}, follow_redirects=False)
    assert r.status_code == 401


def test_unauth_htmx_gets_hx_redirect(login_ctx):
    r = login_ctx.client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers.get("HX-Redirect", "").startswith("/login")


def test_login_grants_session_access(login_ctx):
    s = login_ctx.settings
    r = login_ctx.client.post("/login",
                              data={"username": "joris", "password": "alpha", "next": "/"},
                              follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    sess = r.cookies.get(s.SESSION_COOKIE)
    assert sess and sess != s.BEARER_TOKEN                  # opaque id, NOT the bearer
    # authenticated for a protected page (explicit Cookie header — secure cookie)
    r2 = login_ctx.client.get("/", headers={"Accept": "text/html",
                                            "Cookie": f"{s.SESSION_COOKIE}={sess}"})
    assert r2.status_code == 200


def test_bad_login_401_no_cookie(login_ctx):
    r = login_ctx.client.post("/login",
                              data={"username": "joris", "password": "WRONG"},
                              follow_redirects=False)
    assert r.status_code == 401
    assert login_ctx.settings.SESSION_COOKIE not in r.cookies


def test_logout_revokes_session(login_ctx):
    s = login_ctx.settings
    r = login_ctx.client.post("/login", data={"username": "jade", "password": "beta"},
                              follow_redirects=False)
    sess = r.cookies.get(s.SESSION_COOKIE)
    lo = login_ctx.client.get("/logout", headers={"Cookie": f"{s.SESSION_COOKIE}={sess}"},
                              follow_redirects=False)
    assert lo.status_code == 303
    assert lo.headers["location"] == "/login"
    assert login_ctx.sessions.validate_and_touch(sess) is None   # revoked


def test_token_bootstrap_mints_opaque_session(login_ctx):
    """?token=<bearer> bootstraps a browser: mints a session, sets the opaque
    cookie (NOT the raw bearer), and strips the token from the URL."""
    s = login_ctx.settings
    r = login_ctx.client.get(f"/?token={s.BEARER_TOKEN}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"                    # token stripped
    sess = r.cookies.get(s.SESSION_COOKIE)
    assert sess and sess != s.BEARER_TOKEN


def test_legacy_bearer_cookie_still_accepted(login_ctx):
    """Cutover back-compat: a browser still holding the old raw-bearer
    `console_token` cookie is not kicked out."""
    s = login_ctx.settings
    r = login_ctx.client.get("/", headers={"Accept": "text/html",
                                           "Cookie": f"console_token={s.BEARER_TOKEN}"},
                             follow_redirects=False)
    assert r.status_code == 200


def test_bearer_header_still_works(login_ctx):
    s = login_ctx.settings
    r = login_ctx.client.get("/", headers={"Authorization": f"Bearer {s.BEARER_TOKEN}"})
    assert r.status_code == 200


def test_login_link_grants_session(login_ctx):
    """A valid one-time link logs the user in (passwordless) and attributes the
    session to them."""
    tok = login_ctx.sessions.create_login_token("jade")
    r = login_ctx.client.get(f"/login/link?t={tok}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    sess = r.cookies.get(login_ctx.settings.SESSION_COOKIE)
    assert sess and login_ctx.sessions.validate_and_touch(sess) == "jade"


def test_login_link_single_use_then_401(login_ctx):
    tok = login_ctx.sessions.create_login_token("joris")
    first = login_ctx.client.get(f"/login/link?t={tok}", follow_redirects=False)
    assert first.status_code == 303
    # reusing the same link fails (single-use)
    again = login_ctx.client.get(f"/login/link?t={tok}", follow_redirects=False)
    assert again.status_code == 401


def test_login_link_bad_token_401(login_ctx):
    r = login_ctx.client.get("/login/link?t=not-a-real-token", follow_redirects=False)
    assert r.status_code == 401


def test_login_open_redirect_blocked(login_ctx):
    """A crafted ?next can't turn login into an open redirect."""
    r = login_ctx.client.post("/login",
                              data={"username": "joris", "password": "alpha",
                                    "next": "//evil.example.com"},
                              follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"                    # coerced back to /
