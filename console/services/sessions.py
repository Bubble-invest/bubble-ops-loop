"""Opaque server-side session store + password auth for the cockpit /login.

Option C of board #997 — replace the raw-bearer-in-a-30-day-cookie gate with a
proper login page + a *sliding* opaque session, so the operator stops having to
regenerate a `?token=` bootstrap URL from SOPS every month.

Design (deliberately dependency-free — stdlib only, no infra):
  - Password store: per-user hashes in ``CONSOLE_LOGIN_USERS`` (JSON
    ``{"joris": "<hash>", "jade": "<hash>"}``); falls back to a single shared
    ``CONSOLE_LOGIN_PASSWORD_HASH`` (any username) if the map isn't set. Hashes
    are pbkdf2_sha256 (see ``hash_password``); generate them with
    ``console/deploy/make_password_hash.py`` and store in the SOPS env.
  - Sessions: a tiny SQLite table of opaque ids (``secrets.token_urlsafe``).
    The browser cookie carries only the opaque id, NEVER the bearer. A session
    slides — every authenticated request pushes ``expires_at`` forward by the
    idle window, capped by an absolute lifetime — so an actively-used cockpit
    never expires, while an abandoned one does. Revocable per-session (logout).

All settings are read at call-time from ``console.settings`` so tests can
monkeypatch them (the DB path in particular) without re-importing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from typing import Dict, Optional

from console import settings

# ---- password hashing (pbkdf2_sha256, stdlib) ------------------------

_PBKDF2_ALGO = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 600_000  # OWASP-2023 floor for pbkdf2-hmac-sha256


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """Return an encoded ``pbkdf2_sha256$iters$salt_b64$dk_b64`` string."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    dk_b64 = base64.b64encode(dk).decode("ascii")
    return f"{_PBKDF2_ALGO}${iterations}${salt_b64}${dk_b64}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time verify of ``password`` against an encoded hash. Never raises."""
    try:
        algo, iters_s, salt_b64, dk_b64 = encoded.split("$", 3)
        if algo != _PBKDF2_ALGO:
            return False
        iterations = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


# A real-cost dummy hash, computed once at import time at the same
# `_DEFAULT_ITERATIONS` as a genuine credential. Used by `verify_login` to
# equalize the unknown-username timing against a real (wrong-password) check
# — a hardcoded/degenerate dummy (e.g. 1 iteration) would run ~600,000x
# faster than a real verify and give the timing oracle right back (#1073
# review finding 3).
_DUMMY_VERIFY_HASH = hash_password(secrets.token_hex(16))


def _load_users() -> Dict[str, str]:
    """Parse ``CONSOLE_LOGIN_USERS`` (JSON) into ``{username: hash}``.

    Malformed JSON / non-dict / non-string values degrade to ``{}`` rather than
    crashing auth (mirrors settings' other JSON-override loaders).
    """
    raw = settings.LOGIN_USERS_JSON
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k): str(v) for k, v in obj.items() if isinstance(v, str)}


def verify_login(username: str, password: str) -> bool:
    """True iff (username, password) matches a configured credential.

    Order: the per-user ``CONSOLE_LOGIN_USERS`` map wins; otherwise the shared
    ``CONSOLE_LOGIN_PASSWORD_HASH`` (username ignored). With neither configured,
    password login is disabled (returns False) — the bearer fallback still
    admits the operator, so this is *not* a lockout.
    """
    users = _load_users()
    if users:
        encoded = users.get(username)
        if not encoded:
            # Run a dummy verify so a bad username costs ~the same as a bad
            # password (blunt timing oracle for username enumeration). Must
            # use the real-cost dummy hash — a low-iteration one defeats the
            # whole point (#1073 review finding 3).
            verify_password(password, _DUMMY_VERIFY_HASH)
            return False
        return verify_password(password, encoded)
    shared = settings.LOGIN_PASSWORD_HASH
    if shared:
        return verify_password(password, shared)
    return False


def login_configured() -> bool:
    """True iff at least one password credential is configured."""
    return bool(_load_users()) or bool(settings.LOGIN_PASSWORD_HASH)


# ---- opaque session store (SQLite, stdlib) ---------------------------


def _connect() -> sqlite3.Connection:
    path = settings.SESSION_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions ("
        " id TEXT PRIMARY KEY,"
        " username TEXT NOT NULL,"
        " created_at REAL NOT NULL,"
        " last_seen REAL NOT NULL,"
        " expires_at REAL NOT NULL)"
    )
    # One-time passwordless login links (board #997): a single-use, short-lived
    # token the operator opens once to mint a session as `username`. Lets the
    # manager enroll Joris/Jade from Telegram without anyone typing a password.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS login_tokens ("
        " token TEXT PRIMARY KEY,"
        " username TEXT NOT NULL,"
        " created_at REAL NOT NULL,"
        " expires_at REAL NOT NULL,"
        " used_at REAL)"
    )
    return conn


def create_session(username: str) -> str:
    """Mint a new opaque session id for ``username`` and persist it."""
    sid = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + settings.SESSION_IDLE_SECONDS
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, username, created_at, last_seen, expires_at)"
            " VALUES (?,?,?,?,?)",
            (sid, username, now, now, expires),
        )
    return sid


def validate_and_touch(session_id: str) -> Optional[str]:
    """Return the session's username if valid, else None. Slides the window.

    A valid session's ``expires_at`` is pushed to ``min(now + idle, created +
    absolute)`` on every call — active use never expires (until the absolute
    cap), inactivity does. Expired/unknown ids are treated as logged-out (and
    an expired row is opportunistically deleted).
    """
    if not session_id:
        return None
    now = time.time()
    with _connect() as conn:
        row = conn.execute(
            "SELECT username, created_at, expires_at FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        username, created_at, expires_at = row
        # Expired if past the sliding idle window OR the hard absolute cap.
        if now > expires_at or now > created_at + settings.SESSION_ABSOLUTE_SECONDS:
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return None
        new_expires = min(now + settings.SESSION_IDLE_SECONDS,
                          created_at + settings.SESSION_ABSOLUTE_SECONDS)
        conn.execute(
            "UPDATE sessions SET last_seen=?, expires_at=? WHERE id=?",
            (now, new_expires, session_id),
        )
    return str(username)


def revoke(session_id: str) -> None:
    """Delete one session (logout). No-op if unknown."""
    if not session_id:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def purge_expired() -> int:
    """Best-effort cleanup of expired rows; returns the count removed."""
    now = time.time()
    with _connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        conn.execute("DELETE FROM login_tokens WHERE expires_at < ?", (now,))
        return cur.rowcount or 0


# ---- one-time passwordless login links -------------------------------


def create_login_token(username: str, ttl_seconds: int = 900) -> str:
    """Mint a single-use login token for `username`, valid for `ttl_seconds`.

    Default shortened from 24h to 15min (#1073 review finding 1): the token
    rides a plain clickable GET (`/login/link?t=<token>`) so it can still leak
    via prefetch/proxy/journal despite `main.py`'s access-log redaction — a
    short TTL bounds that residual exposure window.
    """
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO login_tokens (token, username, created_at, expires_at, used_at)"
            " VALUES (?,?,?,?,NULL)",
            (token, username, now, now + ttl_seconds),
        )
    return token


def consume_login_token(token: str) -> Optional[str]:
    """Return the token's username and mark it used, or None if the token is
    unknown, already used, or expired.

    Atomic claim (#1073 review finding 2): the original SELECT-then-UPDATE was
    a TOCTOU race — two concurrent opens of the same link (e.g. a prefetching
    proxy racing the real click) could both pass the SELECT before either
    UPDATE landed, so both would be treated as valid. A single conditional
    UPDATE makes the claim atomic under SQLite's own write-locking: only the
    caller whose UPDATE actually flips a row (`rowcount == 1`) wins the token;
    every other concurrent caller gets None.
    """
    if not token:
        return None
    now = time.time()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE login_tokens SET used_at=?"
            " WHERE token=? AND used_at IS NULL AND expires_at > ?",
            (now, token, now),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT username FROM login_tokens WHERE token=?", (token,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0])
