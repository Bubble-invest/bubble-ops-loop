"""auto_restart.py — auto-restart dead DEPARTMENTS (Joris-approved doctrine).

Background (Rick 2026-06-19, Maya/Ben 2026-06-18): the watchdog/floor doctrine
was ALERTING-ONLY — a dead dept stayed dead until a human ran
`systemctl restart`. Joris approved auto-restart with an EXACT, narrow scope:

  • ONLY departments (the agents that have loops): tony, ben, maya, accountant.
  • NEVER the concierges (morty, claudette) — Joris msg 4636: "only depts need
    restarts, concierges don't have loops". This is a SAFETY INVARIANT, enforced
    as a GUARD (an allowlist check that REFUSES anything not a dept), not merely a
    config default — a misconfigured/compromised caller still cannot restart a
    concierge.
  • GUARDRAIL: max 3 restarts per rolling hour per dept. On the 4th within the
    hour, STOP auto-restarting and ESCALATE to Telegram (human intervention).
    Prevents a crash-loop from hammering restarts.
  • DEFAULT-ON for the 4 depts; opt-out per dept; concierges hard-excluded.

This module is the PURE decision + the small restart-history state I/O. The
bash caller (loop-backup.sh / the watchdog) does the side effect
(`systemctl restart ops-loop-<slug>.service`) and the Telegram escalation.

Design mirrors loop_backup.py: pure functions, append-only JSONL state, never
raises on a missing/corrupt state file.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from typing import List, Optional


# ── The allowlist / denylist (the SAFETY INVARIANT) ──────────────────────────
#
# DEPT_ALLOWLIST is the ONLY set of slugs auto-restart will ever act on. The
# concierges are listed EXPLICITLY in CONCIERGE_DENYLIST too, so the refusal is
# self-documenting and a test can assert a concierge is never restarted even if
# it were somehow added to the allowlist by mistake. The guard order is:
#   1. concierge?  → REFUSE (hard, explicit) — belt
#   2. in allowlist? → ok to consider           — suspenders
# Both must pass; a slug that is neither (e.g. a new unknown agent) is REFUSED
# (fail-closed: we never restart something we don't recognise as a dept).
DEPT_ALLOWLIST = frozenset({"tony", "ben", "maya", "accountant"})
CONCIERGE_DENYLIST = frozenset({"morty", "claudette"})

# Joris-approved guardrail: at most this many auto-restarts per rolling hour
# per dept; the next one escalates instead.
DEFAULT_MAX_PER_HOUR = 3
ROLLING_WINDOW_SEC = 3600


# Decision action constants (the bash caller switches on these).
ACT_RESTART = "restart"               # fire systemctl restart ops-loop-<slug>
ACT_ESCALATE = "escalate"             # guardrail tripped → Telegram a human
ACT_REFUSE_CONCIERGE = "refuse-concierge"   # SAFETY: concierge, never restart
ACT_REFUSE_NOT_DEPT = "refuse-not-dept"     # unknown slug / not a dept → refuse
ACT_REFUSE_OPTED_OUT = "refuse-opted-out"   # auto-restart disabled for this dept


def is_department(slug: str) -> bool:
    """True iff `slug` is a known department that MAY be auto-restarted.

    The hard guard: a concierge is NEVER a department here, even if some caller
    passed it in the allowlist by mistake (concierge check wins). An unknown
    slug is also not a department (fail-closed).
    """
    if slug in CONCIERGE_DENYLIST:
        return False
    return slug in DEPT_ALLOWLIST


def _parse_ts(ts: str) -> Optional[float]:
    """Parse an ISO-8601 UTC stamp (the now_iso() shape) to epoch, or None."""
    try:
        raw = ts.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


def restarts_in_window(
    history: List[dict],
    slug: str,
    now_epoch: float,
    window_sec: int = ROLLING_WINDOW_SEC,
) -> int:
    """Count this dept's restart events within the rolling window ending now.

    `history` is the chronological list read from the restart-state JSONL (each
    record {"ts": iso, "slug": ..., "action": "restart"}). Only `restart`
    actions count toward the guardrail (escalations / refusals do not consume
    the budget). Records with an unparseable ts or outside the window are
    ignored.
    """
    cutoff = now_epoch - window_sec
    n = 0
    for ev in history:
        if ev.get("slug") != slug:
            continue
        if ev.get("action") != ACT_RESTART:
            continue
        ep = _parse_ts(ev.get("ts", ""))
        if ep is None:
            continue
        if ep > now_epoch:           # future record (clock skew) — ignore
            continue
        if ep >= cutoff:
            n += 1
    return n


def decide_restart(
    slug: str,
    history: List[dict],
    now_epoch: float,
    *,
    max_per_hour: int = DEFAULT_MAX_PER_HOUR,
    window_sec: int = ROLLING_WINDOW_SEC,
    opted_out: bool = False,
) -> dict:
    """Decide what to do about a dead dept whose backup tick could NOT revive it.

    Returns {"action": <ACT_*>, "reason": str, "count": int} where `count` is
    the number of restarts already done for this dept in the rolling window.

    Order of guards (SAFETY FIRST):
      1. concierge slug                → ACT_REFUSE_CONCIERGE (hard, never restart)
      2. not a known department        → ACT_REFUSE_NOT_DEPT  (fail-closed)
      3. auto-restart opted out        → ACT_REFUSE_OPTED_OUT
      4. count < max_per_hour          → ACT_RESTART
      5. count >= max_per_hour         → ACT_ESCALATE (guardrail tripped)

    The caller only fires `systemctl restart` on ACT_RESTART; on ACT_ESCALATE it
    pings a human; on any refuse it does nothing (refuses are logged, not acted).
    """
    if slug in CONCIERGE_DENYLIST:
        return {
            "action": ACT_REFUSE_CONCIERGE,
            "reason": f"{slug} is a concierge (no loop) — auto-restart REFUSED (safety invariant)",
            "count": 0,
        }
    if not is_department(slug):
        return {
            "action": ACT_REFUSE_NOT_DEPT,
            "reason": f"{slug} is not a known department — auto-restart refused (fail-closed)",
            "count": 0,
        }
    if opted_out:
        return {
            "action": ACT_REFUSE_OPTED_OUT,
            "reason": f"auto-restart opted out for {slug}",
            "count": 0,
        }
    count = restarts_in_window(history, slug, now_epoch, window_sec)
    if count < max_per_hour:
        return {
            "action": ACT_RESTART,
            "reason": (
                f"{slug} dead + backup could not revive — restart "
                f"{count + 1}/{max_per_hour} this hour"
            ),
            "count": count,
        }
    return {
        "action": ACT_ESCALATE,
        "reason": (
            f"{slug} hit the guardrail ({count} restarts in the last "
            f"{window_sec // 60}m ≥ {max_per_hour}) — STOP auto-restarting, "
            f"escalate to a human"
        ),
        "count": count,
    }


# ── Restart-history state (append-only JSONL, mirrors loop_backup event log) ──


def now_iso() -> str:
    """Current time as an ISO-8601 UTC second-resolution stamp."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_restart_event(
    slug: str,
    action: str,
    reason: str = "",
    ts: Optional[str] = None,
) -> dict:
    """Build one restart-history record."""
    return {
        "ts": ts or now_iso(),
        "slug": slug,
        "action": action,
        "reason": reason,
    }


def append_restart_event(path: str, event: dict) -> None:
    """Append one restart record as a JSON line, creating parent dirs."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_restart_events(path: str, slug: Optional[str] = None) -> List[dict]:
    """Read the restart-history JSONL (chronological). Skips blank/garbage lines;
    returns [] if absent. Optionally filter by slug."""
    if not os.path.exists(path):
        return []
    out: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if slug is not None and ev.get("slug") != slug:
                    continue
                out.append(ev)
    except OSError:
        return []
    return out
