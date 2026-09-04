"""test_auth_login.py — the /login page + opaque sliding session (board #997 C).

Covers: pbkdf2 hashing, per-user vs shared password verification, the SQLite
session store (create/validate/slide/revoke/expire), and the auth middleware's
new paths (session cookie, unauth HTML→/login vs API→401, deprecated ?token
rejection, legacy raw-bearer cookie back-compat during cutover).

Session-cookie round-trips use an explicit ``Cookie`` header rather than the
TestClient jar: the cookie is ``secure=True`` (tailnet is TLS-only) and the test
transport is plain http, so httpx would not resend it on its own.
"""
from __future__ import annotations

import json
import logging
import threading

import pytest
from fastapi.testclient import TestClient

from console import main as console_main
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


def test_login_token_default_ttl_is_short(tmp_session_db):
    """#1073 review finding 1: default TTL shrunk 24h -> 15min to bound the
    prefetch/leak exposure window."""
    import time
    before = time.time()
    tok = sessions.create_login_token("joris")  # default ttl_seconds
    with sessions._connect() as conn:  # noqa: SLF001 (test-only introspection)
        row = conn.execute(
            "SELECT expires_at FROM login_tokens WHERE token=?", (tok,)
        ).fetchone()
    assert row is not None
    ttl = row[0] - before
    assert 0 < ttl <= 900 + 5  # ~15 minutes, allow a hair of test slop


def test_consume_login_token_race_exactly_one_wins(tmp_session_db):
    """#1073 review finding 2: the SELECT-then-UPDATE TOCTOU is fixed by a
    single atomic conditional UPDATE. Simulate two concurrent openers of the
    same one-time link (e.g. a prefetching proxy racing the real click) and
    assert exactly one of them claims the token."""
    tok = sessions.create_login_token("joris")
    results: list[str | None] = [None, None]
    barrier = threading.Barrier(2)

    def worker(i: int) -> None:
        barrier.wait()  # maximize the chance both threads overlap in the DB call
        results[i] = sessions.consume_login_token(tok)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert winners == ["joris"]
    assert len(losers) == 1
    # the token is now burned for good, even for a fresh third caller
    assert sessions.consume_login_token(tok) is None


# ---- uvicorn access-log token redaction (#1073 review finding 1) -----

def _fake_access_record(full_path: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:0", "GET", full_path, "1.1", 303),
        exc_info=None,
    )


def test_access_log_filter_redacts_login_link_token():
    f = console_main._RedactTokenAccessFilter()
    record = _fake_access_record("/login/link?t=SUPERSECRETTOKEN")
    assert f.filter(record) is True
    assert "SUPERSECRETTOKEN" not in record.args[2]
    assert "redacted" in record.args[2]


def test_access_log_filter_redacts_bootstrap_token():
    f = console_main._RedactTokenAccessFilter()
    record = _fake_access_record("/kanban?token=abc123&next=/home")
    assert f.filter(record) is True
    assert "abc123" not in record.args[2]
    assert "next=/home" in record.args[2]  # only the token param is touched


def test_access_log_filter_leaves_untokened_paths_alone():
    f = console_main._RedactTokenAccessFilter()
    record = _fake_access_record("/kanban?status=open")
    assert f.filter(record) is True
    assert record.args[2] == "/kanban?status=open"


def test_access_log_redaction_installed_on_create_app(app):
    """`app` fixture calls create_app() — the filter should be attached to
    uvicorn's real access logger, exactly once even across repeated rebuilds.

    NOTE: the `app` fixture purges + re-imports console.* (see the module note
    above `login_ctx`), so we grab the LIVE `console.main` from sys.modules
    rather than this file's top-level import — otherwise the isinstance()
    check compares against a stale class object from a different module
    instance and always fails.
    """
    import sys
    live_main = sys.modules["console.main"]
    access_logger = logging.getLogger("uvicorn.access")
    matching = [f for f in access_logger.filters
                if isinstance(f, live_main._RedactTokenAccessFilter)]
    assert len(matching) == 1


# ---- app/root-logger token redaction (#1086 hardening #1) ------------
# `_RedactTokenAccessFilter` above only ever sees `uvicorn.access`'s fixed
# 5-arg record shape. `_RedactTokenLogFilter` is the generic companion for
# any OTHER logger (a traceback, or an app-level `logger.warning(...)` that
# happens to include `request.url`) — see its docstring in console/main.py.

def _fake_plain_record(msg, args=None):
    return logging.LogRecord(
        name="console.some_service", level=logging.WARNING, pathname=__file__,
        lineno=1, msg=msg, args=args, exc_info=None,
    )


def test_app_log_filter_redacts_fstring_message():
    """No separate %-args — the token is already baked into the message
    string, e.g. `_log.warning(f"unhandled error for {request.url}")`."""
    f = console_main._RedactTokenLogFilter()
    record = _fake_plain_record(
        "unhandled error for /login/link?t=SUPERSECRETTOKEN"
    )
    assert f.filter(record) is True
    assert "SUPERSECRETTOKEN" not in record.msg
    assert "<redacted>" in record.msg


def test_app_log_filter_redacts_percent_style_args():
    f = console_main._RedactTokenLogFilter()
    record = _fake_plain_record(
        "failed for %s", args=("/kanban?token=abc123&next=/home",)
    )
    assert f.filter(record) is True
    assert "abc123" not in record.args[0]
    assert "<redacted>" in record.args[0]
    assert "next=/home" in record.args[0]  # only the token param is touched


def test_app_log_filter_leaves_untokened_alone():
    f = console_main._RedactTokenLogFilter()
    record = _fake_plain_record(
        "plain message, nothing to see for %s", args=("/kanban?status=open",)
    )
    assert f.filter(record) is True
    assert record.msg == "plain message, nothing to see for %s"
    assert record.args[0] == "/kanban?status=open"


def test_app_log_redaction_installed_on_create_app(app):
    """Companion to `test_access_log_redaction_installed_on_create_app`: the
    generic app-logger filter must land on the root logger, this module's own
    `_log`, and `logging.lastResort` — see `_install_app_log_redaction`'s
    docstring in console/main.py for why all three matter (a `Logger`'s own
    `.filters` are only consulted for records it *originates*, and most
    app-level loggers here have no handler anywhere in their ancestry, so
    they fall through to `lastResort`).
    """
    import sys
    live_main = sys.modules["console.main"]
    targets = [logging.getLogger(), live_main._log]
    if logging.lastResort is not None:
        targets.append(logging.lastResort)
    for target in targets:
        matching = [f for f in target.filters
                    if isinstance(f, live_main._RedactTokenLogFilter)]
        assert len(matching) == 1, f"{target!r} has {len(matching)} matching filters"


# ---- pinned-uvicorn (0.47.0) access-log smoke test (#1086 hardening #2) -
# The unit tests above fabricate the LogRecord by hand, encoding OUR
# assumption about uvicorn's access-log record shape (`record.args[2]` is
# the full request path+query — see `_RedactTokenAccessFilter`'s docstring).
# That assumption is exactly the kind of thing a uvicorn version bump could
# silently break (reordering args, moving the query string into
# `record.__dict__` instead, changing the message format string, ...)
# WITHOUT any test above going red — they'd keep passing against a shape
# uvicorn no longer produces. This test instead drives the REAL, PINNED
# uvicorn end-to-end: a live server on a real socket, exercising uvicorn's
# own `access_logger.info(...)` call inside
# `uvicorn.protocols.http.h11_impl` — not a hand-built LogRecord.
try:
    import uvicorn as _uvicorn_probe
except ImportError:  # pragma: no cover
    _uvicorn_probe = None

# Keep in lockstep with the `uvicorn==` pin in console/requirements.txt.
_PINNED_UVICORN_VERSION = "0.47.0"


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_uvicorn_server(tmp_path, monkeypatch):
    """Boot the real app under the real, pinned uvicorn on a real socket —
    TestClient never goes through uvicorn's HTTP protocol implementation at
    all (httpx calls the ASGI app directly), so it can't exercise the
    `uvicorn.access` code path this test needs to prove."""
    if _uvicorn_probe is None:
        pytest.skip("uvicorn not installed")

    import socket
    import sys
    import threading
    import time

    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "irrelevant-for-health-noauth")
    monkeypatch.setenv("READ_FROM_DISK", str(tmp_path))

    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app  # noqa: WPS433
    live_app = create_app()

    port = _free_port()
    config = _uvicorn_probe.Config(live_app, host="127.0.0.1", port=port,
                                    log_level="info")
    server = _uvicorn_probe.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        pytest.fail("live_uvicorn_server did not start in time")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


def test_pinned_uvicorn_access_log_end_to_end_redaction(live_uvicorn_server):
    """Fails red if a uvicorn upgrade changes the access-log record
    shape/format string in a way that would let `?t=`/`?token=` reach the
    journal again without any other test noticing.
    """
    assert _uvicorn_probe.__version__ == _PINNED_UVICORN_VERSION, (
        f"installed uvicorn is {_uvicorn_probe.__version__}, pinned is "
        f"{_PINNED_UVICORN_VERSION} (console/requirements.txt) — the two "
        "have drifted apart. Re-verify _RedactTokenAccessFilter against the "
        "new version's record.args shape, then update BOTH the pin and this "
        "constant together."
    )

    import io
    import time
    import urllib.request

    from uvicorn.logging import AccessFormatter

    access_logger = logging.getLogger("uvicorn.access")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(AccessFormatter(use_colors=False))
    # Attached AFTER the server thread has started: uvicorn's own
    # `Config.configure_logging()` (dictConfig) runs at server startup and
    # replaces `uvicorn.access`'s HANDLERS (though not its filters — see
    # `_install_access_log_redaction`'s docstring) — a handler attached
    # before that point would be wiped.
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)
    try:
        secret = "SUPERSECRETTOKEN_e2e_12345"  # pragma: allowlist secret
        with urllib.request.urlopen(
            f"{live_uvicorn_server}/health-noauth?token={secret}", timeout=5
        ) as resp:
            assert resp.status == 200

        # The access-log line is written synchronously while sending the
        # response, but give the background server thread a brief grace
        # window against scheduling jitter.
        deadline = time.time() + 2
        output = ""
        while time.time() < deadline:
            output = buf.getvalue()
            if "/health-noauth" in output:
                break
            time.sleep(0.05)

        assert "/health-noauth" in output, f"no access-log line captured: {output!r}"
        assert secret not in output
        assert "token=<redacted>" in output
    finally:
        access_logger.removeHandler(handler)


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


def test_token_bootstrap_is_deprecated_and_cannot_authenticate(login_ctx):
    """A bearer in the query string no longer authenticates the browser."""
    s = login_ctx.settings
    r = login_ctx.client.get(
        f"/?token={s.BEARER_TOKEN}&view=open",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?next=")
    assert s.BEARER_TOKEN not in r.headers["location"]
    assert "view%3Dopen" in r.headers["location"]
    assert s.SESSION_COOKIE not in r.cookies


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
