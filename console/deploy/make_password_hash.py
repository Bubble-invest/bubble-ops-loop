#!/usr/bin/env python3
"""Generate a cockpit login password hash (board #997 option C).

Self-contained (stdlib only, no imports from the console package) so an operator
can run it anywhere without PYTHONPATH. The pbkdf2 scheme MIRRORS
``console/services/sessions.hash_password`` — keep them in sync.

The password is read from an interactive prompt (getpass), never from argv, so
it never lands in shell history or the process list. Output is a JSON fragment
for the ``CONSOLE_LOGIN_USERS`` env var (store it in the SOPS-encrypted env);
merge multiple people into one object, e.g. {"joris": "...", "jade": "..."}.

Usage:
    python3 console/deploy/make_password_hash.py
    # prompts for username + password (twice), prints:  {"joris": "pbkdf2_sha256$..."}
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import secrets
import sys

_ITERATIONS = 600_000  # keep in sync with services/sessions._DEFAULT_ITERATIONS


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    dk_b64 = base64.b64encode(dk).decode("ascii")
    return f"pbkdf2_sha256${_ITERATIONS}${salt_b64}${dk_b64}"


def main() -> int:
    username = input("username (e.g. joris): ").strip()
    if not username:
        print("username required", file=sys.stderr)
        return 1
    pw = getpass.getpass("password: ")
    if len(pw) < 8:
        print("password too short (min 8 chars)", file=sys.stderr)
        return 1
    if pw != getpass.getpass("confirm : "):
        print("passwords do not match", file=sys.stderr)
        return 1
    entry = {username: hash_password(pw)}
    print("\nAdd this to CONSOLE_LOGIN_USERS (merge with any existing users):")
    print(json.dumps(entry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
