"""test_login_throttle.py — board #1116 (cockpit audit): /login had NO
failure throttle/lockout/failure-audit. Unlimited attempts, each a 600k-iter
PBKDF2 verify on a single sync worker, is both a brute-force surface and a
CPU-amplification DoS surface.

Fix: console/services/sessions.py adds a per-(username, client-IP) failure
counter (login_failures table) backing an exponential backoff (5 free fails,
then doubling, capped at 5 min) — checked BEFORE verify_login in
console/routes/auth.py's login_submit, so a throttled flood never pays for
another full-cost pbkdf2 verify. A `_log.warning("login failed user=%s
ip=%s")` line covers both the throttled-skip and genuine-failure cases for
observability. Both cases return the IDENTICAL 401 page (status, template,
error text) so a prober can't distinguish "still guessing" from "now
throttled".
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from console import settings
from console.services import sessions


# ─── sessions.py — unit-level throttle logic ────────────────────────────────


@pytest.fixture
def tmp_session_db(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SESSION_DB_PATH", tmp_path / "sessions.db")
    return tmp_path


def test_no_failures_means_no_throttle(tmp_session_db):
    assert sessions.login_retry_after_seconds("joris", "1.2.3.4") == 0.0


def test_under_threshold_fails_are_free(tmp_session_db):
    for _ in range(5):
        sessions.record_login_failure("joris", "1.2.3.4")
    assert sessions.login_retry_after_seconds("joris", "1.2.3.4") == 0.0


def test_crossing_threshold_triggers_backoff(tmp_session_db):
    for _ in range(6):
        sessions.record_login_failure("joris", "1.2.3.4")
    retry_after = sessions.login_retry_after_seconds("joris", "1.2.3.4")
    assert retry_after > 0.0
    assert retry_after <= 300.0  # never exceeds the documented cap


def test_backoff_grows_with_more_failures(tmp_session_db):
    for _ in range(6):
        sessions.record_login_failure("joris", "1.2.3.4")
    after_6 = sessions.login_retry_after_seconds("joris", "1.2.3.4")
    for _ in range(3):
        sessions.record_login_failure("joris", "1.2.3.4")
    after_9 = sessions.login_retry_after_seconds("joris", "1.2.3.4")
    assert after_9 > after_6


def test_backoff_is_capped(tmp_session_db):
    for _ in range(200):
        sessions.record_login_failure("joris", "1.2.3.4")
    assert sessions.login_retry_after_seconds("joris", "1.2.3.4") <= 300.0


def test_backoff_expires_over_time(tmp_session_db, monkeypatch):
    for _ in range(6):
        sessions.record_login_failure("joris", "1.2.3.4")
    assert sessions.login_retry_after_seconds("joris", "1.2.3.4") > 0.0
    # Simulate time passing well past any possible backoff window.
    import time as _time
    real_time = _time.time
    monkeypatch.setattr(_time, "time", lambda: real_time() + 400)
    assert sessions.login_retry_after_seconds("joris", "1.2.3.4") == 0.0


def test_successful_login_clears_failures(tmp_session_db):
    for _ in range(6):
        sessions.record_login_failure("joris", "1.2.3.4")
    assert sessions.login_retry_after_seconds("joris", "1.2.3.4") > 0.0
    sessions.clear_login_failures("joris", "1.2.3.4")
    assert sessions.login_retry_after_seconds("joris", "1.2.3.4") == 0.0


def test_throttle_is_per_username_and_ip_pair(tmp_session_db):
    """A flood against one (user, ip) pair must not lock out a DIFFERENT
    user from the same IP, nor the same user from a different IP."""
    for _ in range(10):
        sessions.record_login_failure("joris", "1.2.3.4")
    assert sessions.login_retry_after_seconds("joris", "1.2.3.4") > 0.0
    assert sessions.login_retry_after_seconds("jade", "1.2.3.4") == 0.0
    assert sessions.login_retry_after_seconds("joris", "9.9.9.9") == 0.0


def test_unknown_key_lookup_never_raises(tmp_session_db):
    # Sanity: usernames/IPs with unusual characters must not break the DB key.
    sessions.record_login_failure("weird\tuser", "::1")
    assert sessions.login_retry_after_seconds("weird\tuser", "::1") == 0.0  # only 1 fail


# ─── auth.py /login route — end-to-end throttle behavior ───────────────────


@pytest.fixture
def login_ctx(monkeypatch, tmp_path, app):
    """Mirrors test_auth_login.py's login_ctx fixture exactly (same
    live-module-rebinding rationale — see that file's NOTE)."""
    import sys
    from types import SimpleNamespace
    live_settings = sys.modules["console.settings"]
    live_sessions = sys.modules["console.services.sessions"]
    monkeypatch.setattr(
        live_settings, "LOGIN_USERS_JSON",
        json.dumps({"joris": live_sessions.hash_password("alpha")}))
    monkeypatch.setattr(live_settings, "LOGIN_PASSWORD_HASH", "")
    monkeypatch.setattr(live_settings, "SESSION_DB_PATH", tmp_path / "s.db")
    return SimpleNamespace(client=TestClient(app), settings=live_settings,
                           sessions=live_sessions)


def test_repeated_bad_logins_eventually_throttled(login_ctx):
    last = None
    for _ in range(10):
        last = login_ctx.client.post(
            "/login", data={"username": "joris", "password": "WRONG"},
            follow_redirects=False,
        )
        assert last.status_code == 401
    # After enough fails, the SAME (user, ip) must now be throttled — proven
    # by a CORRECT password still being rejected (verify_login is skipped
    # entirely once throttled).
    r = login_ctx.client.post(
        "/login", data={"username": "joris", "password": "alpha"},
        follow_redirects=False,
    )
    assert r.status_code == 401
    assert login_ctx.settings.SESSION_COOKIE not in r.cookies


def test_throttled_and_wrong_password_responses_are_identical(login_ctx):
    """The whole point: a prober must not be able to tell 'still guessing'
    apart from 'now throttled' from the HTTP response alone."""
    for _ in range(10):
        login_ctx.client.post(
            "/login", data={"username": "joris", "password": "WRONG"},
            follow_redirects=False,
        )
    throttled_resp = login_ctx.client.post(
        "/login", data={"username": "joris", "password": "WRONG"},
        follow_redirects=False,
    )

    fresh_wrong_resp = login_ctx.client.post(
        "/login", data={"username": "someoneelse", "password": "WRONG"},
        follow_redirects=False,
    )
    assert throttled_resp.status_code == fresh_wrong_resp.status_code == 401
    assert throttled_resp.text == fresh_wrong_resp.text


def test_successful_login_after_some_fails_still_works_and_clears_counter(login_ctx):
    for _ in range(3):  # under threshold — must still be able to log in
        login_ctx.client.post(
            "/login", data={"username": "joris", "password": "WRONG"},
            follow_redirects=False,
        )
    r = login_ctx.client.post(
        "/login", data={"username": "joris", "password": "alpha"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert login_ctx.settings.SESSION_COOKIE in r.cookies
    # The counter is now cleared — a fresh run of fails starts from zero.
    ip = "testclient"  # starlette TestClient's default client host
    assert login_ctx.sessions.login_retry_after_seconds("joris", ip) == 0.0


def test_failed_login_logs_warning_with_user_and_ip_never_password(login_ctx, caplog):
    with caplog.at_level(logging.WARNING, logger="console.routes.auth"):
        login_ctx.client.post(
            "/login", data={"username": "joris", "password": "TOTALLY-SECRET-PW"},
            follow_redirects=False,
        )
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("login failed" in m and "user=joris" in m and "ip=" in m for m in warnings), warnings
    assert not any("TOTALLY-SECRET-PW" in m for m in warnings), (
        "the password must NEVER appear in a log line"
    )


def test_throttled_login_logs_warning(login_ctx, caplog):
    for _ in range(10):
        login_ctx.client.post(
            "/login", data={"username": "joris", "password": "WRONG"},
            follow_redirects=False,
        )
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="console.routes.auth"):
        login_ctx.client.post(
            "/login", data={"username": "joris", "password": "WRONG"},
            follow_redirects=False,
        )
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("throttled" in m and "user=joris" in m for m in warnings), warnings
