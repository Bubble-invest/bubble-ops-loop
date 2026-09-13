"""telegram_gap_detector.py — PURE decision logic for the Telegram
delivery-GAP detector (board #1284 pt E — Claudette silent message loss).

The problem this exists for: the telegram channel plugin (grammy) advances the
Telegram getUpdates offset the moment it fetches a batch, BEFORE the handler
enqueues the turn into Claude. A crash / restart / harness-takeover in that
window loses the acked-but-undelivered updates PERMANENTLY, with NO signal — the
existing watchdogs only check liveness ("is the poller up?"), never completeness
("did every update Telegram accepted reach the session?"). On 2026-09-10→11 that
silently ate 19 of Jade's messages to Claudette (update-bearing ids 7527–7545).

The delivery-ledger plugin patch (deploy/telegram-plugin/delivery-ledger.block.ts)
appends one line per received update to <state>/delivery-ledger.jsonl, in the
strictly-increasing update_id order grammy delivers them. This module turns that
ledger + an optional server-side pending-update probe into a decision:

  * GAP     — a numeric jump in consecutive update_ids (the crash-loss
              fingerprint: …7526 then 7546 ⇒ 7527–7545 lost).
  * WEDGE   — Telegram reports pending updates the (alive) poller is not
              consuming, across >=2 consecutive probes (getWebhookInfo, a
              read-only probe that does NOT consume updates).
  * DEAD    — the poller (bot.pid) is not alive at all.

All logic here is pure and side-effect-free so it is unit-testable; the thin
scripts/telegram-gap-detector.py wraps it with discovery, the ledger read, the
optional probe, and the loud out-of-band alert.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Consecutive stuck probes before a "pending updates not consumed" wedge is
# declared — matches hermes's own get_webhook_info escalation (#42909): a single
# probe can race a legitimately in-flight long-poll.
DEFAULT_WEDGE_STUCK_PROBES = 2


@dataclass
class LedgerEntry:
    update_id: int
    ts: str = ""

    @staticmethod
    def from_line(line: str) -> Optional["LedgerEntry"]:
        line = line.strip()
        if not line:
            return None
        try:
            d = json.loads(line)
        except (ValueError, TypeError):
            return None
        uid = d.get("update_id")
        if not isinstance(uid, int):
            return None
        return LedgerEntry(update_id=uid, ts=str(d.get("ts", "")))


@dataclass
class Gap:
    after: int          # last update_id seen before the gap
    before: int         # first update_id seen after the gap
    missing_count: int  # how many update_ids are unaccounted for

    @property
    def key(self) -> str:
        return f"{self.after}-{self.before}"

    @property
    def missing_range(self) -> Tuple[int, int]:
        return (self.after + 1, self.before - 1)


@dataclass
class State:
    """Persisted between ticks so the same gap/wedge is not re-alerted forever."""
    alerted_gaps: List[str] = field(default_factory=list)
    consecutive_pending_stuck: int = 0
    last_max_update_id: Optional[int] = None

    @staticmethod
    def load(d: Optional[dict]) -> "State":
        d = d or {}
        return State(
            alerted_gaps=list(d.get("alerted_gaps", [])),
            consecutive_pending_stuck=int(d.get("consecutive_pending_stuck", 0)),
            last_max_update_id=d.get("last_max_update_id"),
        )

    def dump(self) -> dict:
        # Cap the alerted-gap memory so the state file cannot grow unbounded.
        return {
            "alerted_gaps": self.alerted_gaps[-500:],
            "consecutive_pending_stuck": self.consecutive_pending_stuck,
            "last_max_update_id": self.last_max_update_id,
        }


@dataclass
class Decision:
    slug: str
    new_gaps: List[Gap] = field(default_factory=list)
    wedged: bool = False
    dead: bool = False
    pending_count: Optional[int] = None
    max_update_id: Optional[int] = None
    state: State = field(default_factory=State)

    @property
    def alert(self) -> bool:
        return bool(self.new_gaps) or self.wedged or self.dead

    @property
    def total_missing(self) -> int:
        return sum(g.missing_count for g in self.new_gaps)

    def alert_text(self) -> str:
        parts: List[str] = [f"⚠️ Telegram DELIVERY problem — {self.slug}"]
        for g in self.new_gaps:
            lo, hi = g.missing_range
            parts.append(
                f"• GAP: update_ids {lo}–{hi} ({g.missing_count} update"
                f"{'s' if g.missing_count != 1 else ''}) were acked to Telegram "
                f"but never reached the session — SILENT LOSS."
            )
        if self.dead:
            parts.append("• POLLER DEAD: bot.pid is not alive — inbound is not being received.")
        elif self.wedged:
            parts.append(
                f"• POLLER WEDGED: {self.pending_count} update(s) pending at "
                f"Telegram that the (alive) poller is not consuming."
            )
        parts.append(
            "Recovery has been attempted; verify the poller and re-request the "
            "lost messages from the sender (Telegram cannot redeliver acked updates)."
        )
        return "\n".join(parts)


def find_gaps(entries: List[LedgerEntry]) -> List[Gap]:
    """All numeric gaps in the (deduped, sorted) update_id stream."""
    ids = sorted({e.update_id for e in entries})
    gaps: List[Gap] = []
    for a, b in zip(ids, ids[1:]):
        if b > a + 1:
            gaps.append(Gap(after=a, before=b, missing_count=b - a - 1))
    return gaps


def decide(
    slug: str,
    entries: List[LedgerEntry],
    state: State,
    *,
    bot_alive: bool,
    pending_count: Optional[int] = None,
    wedge_stuck_probes: int = DEFAULT_WEDGE_STUCK_PROBES,
) -> Decision:
    """Pure verdict for one dept. Mutates a COPY of state (returned on Decision)."""
    new_state = State(
        alerted_gaps=list(state.alerted_gaps),
        consecutive_pending_stuck=state.consecutive_pending_stuck,
        last_max_update_id=state.last_max_update_id,
    )

    all_gaps = find_gaps(entries)
    already = set(new_state.alerted_gaps)
    new_gaps = [g for g in all_gaps if g.key not in already]
    for g in new_gaps:
        new_state.alerted_gaps.append(g.key)

    max_uid = max((e.update_id for e in entries), default=None)
    if max_uid is not None:
        new_state.last_max_update_id = max_uid

    dead = not bot_alive

    # Wedge: pending updates the alive poller is not consuming, sustained across
    # consecutive probes. A dead poller is reported as DEAD, not wedged.
    wedged = False
    if pending_count is not None and bot_alive:
        if pending_count > 0:
            new_state.consecutive_pending_stuck += 1
            if new_state.consecutive_pending_stuck >= wedge_stuck_probes:
                wedged = True
        else:
            new_state.consecutive_pending_stuck = 0
    elif bot_alive:
        # No probe this tick — do not disturb the running counter.
        pass
    else:
        new_state.consecutive_pending_stuck = 0

    return Decision(
        slug=slug,
        new_gaps=new_gaps,
        wedged=wedged,
        dead=dead,
        pending_count=pending_count,
        max_update_id=max_uid,
        state=new_state,
    )
