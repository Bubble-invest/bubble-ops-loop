#!/usr/bin/env python3
"""Mint a one-time cockpit login link for a user (board #997 passwordless flow).

Run on the cockpit host, from the repo root, so it writes into the SAME session
DB the console reads (settings.SESSION_DB_PATH):

    cd /home/claude/bubble-ops-loop
    PYTHONPATH=. python3 console/deploy/make_login_link.py joris

Prints the full URL to send (privately) to that person. The token is single-use
and expires (default 15min). The person opens it once → logged in as `username`.
Pass --ttl for a longer-lived enrollment link if needed.
"""
from __future__ import annotations

import argparse

from console.services import sessions

_DEFAULT_BASE = "https://joris-cx33.tail408dcc.ts.net:8443"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("username", help="login username to attribute the session to (e.g. joris)")
    # Default matches sessions.create_login_token's (#1073 review finding 1:
    # a short TTL bounds the token's prefetch/access-log exposure window).
    # Override with a larger --ttl for a deliberately longer-lived enrollment
    # link if the operator needs one.
    ap.add_argument("--ttl", type=int, default=900, help="seconds until the link expires (default 15min)")
    ap.add_argument("--base", default=_DEFAULT_BASE, help="cockpit base URL")
    args = ap.parse_args()
    token = sessions.create_login_token(args.username, args.ttl)
    print(f"{args.base.rstrip('/')}/login/link?t={token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
