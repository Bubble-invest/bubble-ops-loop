#!/usr/bin/env python3
"""arm_wake_cron_convert.py — Paris wall-clock -> host-local cron fields.

Extracted from scripts/arm-wake-cron.sh (board #1529): the conversion used
to be an embedded `python3 - ... <<'PY' ... PY` heredoc INSIDE a `$(...)`
command substitution. bash 3.2 (macOS stock /bin/bash, never upgraded past
3.2.57 post-GPLv3) mis-parses a heredoc nested inside `$(...)` — this is the
same parser defect fixed for the renderer in board #1529 / PR #512
(deploy/local/lib/local_loop_lib.sh + safe_secrets_loader.sh.tmpl): move the
body out of the heredoc into its own file, call it with a plain command
substitution (no heredoc for bash 3.2 to mis-parse).

Usage:
    arm_wake_cron_convert.py <Paris-HH:MM> <daily|one-shot> <explicit-date-or-empty>

Output (stdout): "MM HH" for daily, "MM HH DD MO" for one-shot — same format
the previous embedded heredoc produced, byte-identical.

Exit codes: 2 + stderr message + no stdout on an invalid/unresolvable TZ
(board #1515 fix, preserved verbatim) or a bad explicit date.
"""
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "ERROR: usage: arm_wake_cron_convert.py <Paris-HH:MM> <daily|one-shot> <explicit-date-or-empty>",
            file=sys.stderr,
        )
        return 2

    paris_time, mode, explicit_date = sys.argv[1], sys.argv[2], sys.argv[3]
    hh, mm = (int(x) for x in paris_time.split(":"))
    paris = ZoneInfo("Europe/Paris")

    # Fix #1515: validate the HOST zone actually resolves before trusting it.
    # `datetime.astimezone()` (no args) resolves the host's local zone the same
    # way the OS/C library does: TZ env var if set, else the system zone (e.g.
    # /etc/localtime). But an invalid/unknown TZ value (e.g.
    # TZ=Bogus/Nonexistent) does NOT raise there — it silently falls back to
    # UTC, which is exactly the silent-wrong-hour class of board #850 (a 2h/1h
    # offset nobody notices until a live order fires at the wrong time). Only
    # validate when TZ is actually SET and non-empty: an empty/unset TZ must
    # keep falling through to the system zone unchanged (that path already
    # works and must not be touched).
    tz_env = os.environ.get("TZ", "")
    if tz_env:
        try:
            ZoneInfo(tz_env)
        except (ZoneInfoNotFoundError, ValueError) as e:
            print(
                f"ERROR: TZ={tz_env!r} does not resolve to a known IANA "
                f"timezone ({e}); refusing to silently fall back to UTC",
                file=sys.stderr,
            )
            return 2

    today_paris = datetime.now(paris).date()

    if mode == "daily":
        target_date = today_paris
    elif explicit_date:
        target_date = date.fromisoformat(explicit_date)
    else:
        target_date = today_paris + timedelta(days=1)

    # Localize the target wall-clock time as Europe/Paris, then convert to
    # whatever timezone THIS HOST's system clock is actually set to (the VPS'
    # is UTC, the Macs' is Europe/Paris) — tz-database backed, so the Mar/Oct
    # DST flip on either end is handled automatically, never a hardcoded offset.
    dt_paris = datetime(target_date.year, target_date.month, target_date.day, hh, mm, tzinfo=paris)
    dt_local = dt_paris.astimezone()

    # MIN/HOUR are always printed zero-padded to 2 digits (matches the previous
    # GNU `date +'%M %H'` output format exactly, byte for byte). DAY/MONTH are
    # printed as plain decimal WITHOUT zero-padding (cron doesn't need it, and a
    # leading zero on a cron field risks being misread as octal by naive
    # parsers) — same convention the previous revision enforced explicitly.
    if mode == "daily":
        print(f"{dt_local.minute:02d} {dt_local.hour:02d}")
    else:
        print(f"{dt_local.minute:02d} {dt_local.hour:02d} {dt_local.day} {dt_local.month}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
