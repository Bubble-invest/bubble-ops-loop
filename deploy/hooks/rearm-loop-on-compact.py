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

IDEMPOTENCE (#754 durable fix): dedupe on the re-arm SENTINEL, NOT on "is the
inject non-empty". The original guard skipped whenever ANY content was pending —
so an unrelated un-drained turn (a cross-agent inject, a not-yet-drained operator
message) SILENTLY SWALLOWED the re-arm and left the loop dormant, the exact bug
this card chases. Now: a re-arm already queued -> skip (never stack two); anything
else pending -> still append (on its own line).

OBSERVABILITY: on every fire/skip it writes ONE line to
<channel_dir>/rearm-on-compact.log so a REAL /compact leaves greppable proof the
hook ran — the acceptance blocker this card was stuck on ("no clear auto-fire
evidence"). Best-effort; a logging failure never changes the exit code.
"""
from __future__ import annotations
import datetime
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
    "The box's clock is UTC, not Paris: NEVER hand-write a Paris HH:MM as the cron "
    "literal (board #850 — treating 08:03 Paris as `3 8 * * *` fired a live market "
    "order 2h late). For any Paris-anchored target, derive the box-UTC cron via "
    "`scripts/arm-wake-cron.sh <Paris-HH:MM> [daily|one-shot]` (DST-safe — reads the "
    "tz database, never a hardcoded offset) and CronCreate the expression it prints. "
    "Sanity-check with `TZ=Europe/Paris date` at the top of every tick rather than "
    "trusting a stale header. "
    "The CronCreate prompt MUST be your full tick protocol text (STEP A-F per CLAUDE.md), "
    "NEVER a bare slash-command like /loop-now. "
    "work pending or a layer still due today -> schedule toward that layer time; "
    "quiet but more may come today -> a longer cadence is fine (e.g. 0 */2 * * *, "
    "TZ-neutral); "
    "all layers done and nothing awaited -> run `scripts/arm-wake-cron.sh 08:03 "
    "one-shot` for the correct box-UTC one-shot and arm nothing else. "
    "Never hardcode an hourly cron. Your loop-layer floor timers remain the safety net. "
    "Do not reply to a human; just resume cadence."
)

# Only these SessionStart sources indicate a lost in-memory scheduler.
# startup is covered by boot_rearm.ts (poller start); clear/fork are not loop resumptions.
REARM_SOURCES = {"compact", "resume"}

# Stable sentinel that identifies an ALREADY-QUEUED re-arm turn (this hook's own,
# or boot_rearm.ts's, or a manual telegram-inject re-arm using the same wording).
# Idempotence dedupes on THIS phrase — NOT on "is the inject non-empty" — so an
# unrelated pending turn (a cross-agent inject, an operator message not yet
# drained) can never SILENTLY SWALLOW the re-arm (#754: the dormant-after-compact
# failure mode). The phrase is a load-bearing prefix of REARM_TURN and of
# boot_rearm.ts's content; both carry "re-arm your /loop, SELF-PACED".
REARM_SENTINEL = "re-arm your /loop, SELF-PACED"


def _audit(channel_dir: str, decision: str, source: str) -> None:
    """Best-effort, single-line audit trail so a REAL /compact leaves greppable
    evidence the hook ran (the #754 acceptance blocker: 'no clear auto-fire
    evidence'). Written next to the inject file. NEVER raises — a SessionStart
    hook must always exit 0, so any logging failure is swallowed."""
    try:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"{ts}\tsource={source}\tdecision={decision}\n"
        with open(os.path.join(channel_dir, "rearm-on-compact.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def main() -> int:
    # Hard safety gate FIRST — never fire without the opt-in env.
    # (A human interactive /compact never sets this, so it stays a pure no-op with
    # zero side effects — not even an audit line.)
    if os.environ.get("OPS_LOOP_BOOT_REARM") != "1":
        return 0

    # Read + parse the SessionStart payload; malformed -> no-op.
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    # Valid JSON can still parse to a non-dict (e.g. `[1,2,3]`, `null`); guard
    # so `.get()` never raises — the hook MUST always exit 0 (#754 reviewer).
    if not isinstance(payload, dict):
        return 0
    source = payload.get("source")
    if source not in REARM_SOURCES:
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
    channel_dir = os.path.dirname(inject)

    # Deliver the re-arm. Idempotence dedupes on the re-arm SENTINEL, not on
    # "inject is non-empty": if a re-arm is ALREADY queued we skip (never stack
    # two), but an UNRELATED pending turn must NOT suppress the re-arm — the
    # earlier "getsize>0 -> skip" guard silently dropped the re-arm whenever any
    # message happened to be un-drained at compact time, leaving the loop dormant.
    try:
        try:
            with open(inject, "r", encoding="utf-8") as f:
                pending = f.read()
        except FileNotFoundError:
            pending = ""
        if REARM_SENTINEL in pending:
            _audit(channel_dir, "skip:already-queued", source)
            return 0
        # Separate our turn from any unrelated pending content with a newline so
        # the two never merge into one malformed line.
        prefix = "" if (pending == "" or pending.endswith("\n")) else "\n"
        with open(inject, "a", encoding="utf-8") as f:
            f.write(prefix + REARM_TURN + "\n")
        _audit(channel_dir, "fired", source)
    except Exception:
        return 0  # any I/O failure must not break session start

    return 0


if __name__ == "__main__":
    sys.exit(main())
