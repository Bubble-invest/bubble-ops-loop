#!/usr/bin/env python3
"""SessionStart hook — re-arm an ops-loop agent's /loop after a /compact (#754).

A /compact (manual OR automatic context-overflow) resets the in-memory,
session-only CronCreate task that drives the agent's /loop cadence, so the
agent goes silently dormant (alive via KeepAlive, but never self-ticks).
This hook fires on the compact/resume SessionStart source and self-injects a
re-arm turn — the compact-triggered twin of boot_rearm.ts (which only covers
poller startup).

SAFETY: fires ONLY when OPS_LOOP_BOOT_REARM=1 (the same opt-in boot_rearm
uses), so it can NEVER inject a phantom prompt into a human's interactive
/compact. Fail-closed on any missing env/path. Always exits 0 — a SessionStart
hook must never break session startup.

Delivery: appends the re-arm turn to the agent's telegram inject file, resolved by
GLOB ($HOME/.claude/channels/telegram-*/inject — exactly one, else no-op). We do
NOT build the path from OPS_LOOP_DEPT: verified on M1 that OPS_LOOP_DEPT="content"
but the real dir is telegram-socials, so a concat would miss. The plugin's
file-watcher drains the inject as a normal session turn. (A shell/py hook cannot
emit boot_rearm's MCP notification, so it uses the inject file — same turn,
different transport. The turn TEXT is shared with boot_rearm.ts.)
"""
from __future__ import annotations
import glob
import json
import os
import sys

# Byte-identical re-arm wording to deploy/telegram-plugin/boot_rearm.ts's `content`
# (minus the "[boot]" tag; this one is "[compact]"). Keep in sync — test asserts it.
REARM_TURN = (
    "[compact] Context was compacted — re-arm your /loop, SELF-PACED. "
    "This is a system signal, not an operator instruction. "
    "A /compact resets your in-memory scheduler, so your armed /loop cron is gone. "
    "FIRST run ONE normal session-start + dispatch tick now — do this unconditionally. "
    "THEN arm your OWN next wake with a single CronCreate (run CronList first and "
    "delete any stale/duplicate loop task so you never stack two). "
    "The CronCreate prompt MUST be your full tick protocol text (STEP A-F per CLAUDE.md), "
    "NEVER a bare slash-command like /loop-now. "
    "work pending or a layer still due today -> schedule toward that layer time; "
    "quiet but more may come today -> a longer cadence is fine (e.g. 0 */2 * * *); "
    "all layers done and nothing awaited -> set ONE one-shot for tomorrow 08:03 Paris "
    "(3 8 * * *) and arm nothing else. "
    "Never hardcode an hourly cron. Your loop-layer floor timers remain the safety net. "
    "Do not reply to a human; just resume cadence."
)

# Only these SessionStart sources indicate a lost in-memory scheduler.
# startup is covered by boot_rearm.ts (poller start); clear/fork are not loop resumptions.
REARM_SOURCES = {"compact", "resume"}


def main() -> int:
    # Hard safety gate FIRST — never fire without the opt-in env.
    if os.environ.get("OPS_LOOP_BOOT_REARM") != "1":
        return 0

    # Read + parse the SessionStart payload; malformed -> no-op.
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    if payload.get("source") not in REARM_SOURCES:
        return 0

    home = os.environ.get("HOME")
    if not home:
        return 0
    # Resolve the inject file by GLOB, not telegram-<OPS_LOOP_DEPT> concat:
    # on M1 OPS_LOOP_DEPT="content" but the real dir is telegram-socials.
    # Exactly one telegram-*/inject -> use it; zero or many -> fail-closed no-op.
    matches = glob.glob(os.path.join(home, ".claude", "channels", "telegram-*", "inject"))
    if len(matches) != 1:
        return 0  # zero (not wired) or ambiguous (never guess among many)
    inject = matches[0]

    # Idempotence: don't stack a re-arm on top of an un-drained turn.
    try:
        if os.path.getsize(inject) > 0:
            return 0
        with open(inject, "a", encoding="utf-8") as f:
            f.write(REARM_TURN + "\n")
    except Exception:
        return 0  # any I/O failure must not break session start

    return 0


if __name__ == "__main__":
    sys.exit(main())
