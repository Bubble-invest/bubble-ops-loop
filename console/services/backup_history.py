"""
backup_history.py — surface the loop-backup safety net in the cockpit.

{{OPERATOR}} msg 1171 (2026-06-01): a backup scheduled task (loop-backup.timer,
08:00 + 14:00 Europe/Paris) complements the persistent `/loop`s. For each
dept it either SKIPS (the live loop is healthy — recent heartbeat) or runs
ONE dispatch tick (the loop is dead/parked). Until now it logged only to the
systemd journal — invisible in the front end. The timer now appends one
event per dept per fire to a central JSONL (see scripts/loop-backup.sh +
scripts/lib/loop_backup.py); this service reads it back for the UI.

The dept page renders a "Filet de sécurité" block (latest verdict + recent
checks); the home page shows a one-line roll-up.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from console import settings

# Reuse the writer's parser so reader/writer can never drift (single source
# of truth for the on-disk format).
from scripts.lib.loop_backup import latest_per_dept, read_events

# Newest-first, capped — a dept page never needs the full year of fires.
_MAX_PER_DEPT = 10


@dataclass(frozen=True)
class BackupEvent:
    ts: str
    # "skip" (loop alive) | "run" (loop stale → tick) | "deferred" (floor
    # correctly declined to act — unsafe/unreadable harness selector, or a
    # live primary that couldn't be woken so the headless fallback was
    # refused, board #1313) | "degraded" (a degraded L4 carried-over export).
    action: str
    reason: str
    age_sec: Optional[int] = None
    exit_code: Optional[int] = None

    @property
    def is_run(self) -> bool:
        return self.action == "run"

    @property
    def is_deferred(self) -> bool:
        return self.action in ("deferred", "degraded")

    @property
    def ok(self) -> bool:
        """A skip is always fine; a run is fine iff its tick exited 0.

        #1313 step 2: a deferral/degraded event is NOT "ok" — it means the
        floor could not confirm the dept is fine and needs a human to look
        (a recurring deferral is exactly the "stale for days, nothing woke
        it" shape) — but it is a DISTINCT, calmer signal than a crashed run,
        which the UI must still be able to tell apart (verdict_fr below).
        """
        if self.action == "skip":
            return True
        if self.is_deferred:
            return False
        return self.exit_code == 0

    @property
    def severity(self) -> str:
        """3-way UI severity: 'ok' | 'warn' | 'fail'.

        A deferral/degraded event uses the existing (previously orphaned)
        `--warn` amber styling — visibly distinct from both a healthy skip
        and a crashed run, per #1313 step 2 (a correct refusal to act is not
        a crash, but it is also not nothing)."""
        if self.ok:
            return "ok"
        if self.is_deferred:
            return "warn"
        return "fail"

    @property
    def age_human(self) -> Optional[str]:
        """Heartbeat age at decision time, as a compact French duration."""
        if self.age_sec is None:
            return None
        s = self.age_sec
        if s < 0:
            return "à l'instant"
        if s < 90:
            return f"{s} s"
        if s < 5400:
            return f"{round(s / 60)} min"
        return f"{round(s / 3600, 1)} h"

    @property
    def verdict_fr(self) -> str:
        """One-line human verdict for the UI."""
        if self.action == "skip":
            return "Boucle active — sauvegarde non nécessaire"
        if self.action == "deferred":
            return "Boucle arrêtée — filet différé (n'a pas pu agir sans risque) ⏸"
        if self.action == "degraded":
            return "Boucle arrêtée — export dégradé (reporté depuis le dernier état connu)"
        if self.exit_code == 0:
            return "Boucle arrêtée — tick de secours exécuté ✓"
        if self.exit_code is None:
            return "Boucle arrêtée — tick de secours déclenché"
        return "Boucle arrêtée — tick de secours en échec ✗"


def _to_event(raw: dict) -> Optional[BackupEvent]:
    ts = raw.get("ts")
    action = raw.get("action")
    # #1313 step 2: "deferred"/"degraded" must render too — before this fix a
    # dept stuck repeatedly deferring (the exact Maya-incident shape) had its
    # entire "Filet de sécurité" panel silently vanish from the cockpit once
    # its last _MAX_PER_DEPT events were all deferrals, since a dropped event
    # here is indistinguishable from "no events at all" to recent_backups()/
    # latest_backup(). A deferral must stay visible, not skip/run-shaped.
    if not ts or action not in ("skip", "run", "deferred", "degraded"):
        return None
    age = raw.get("age_sec")
    exit_code = raw.get("exit")
    return BackupEvent(
        ts=str(ts),
        action=str(action),
        reason=str(raw.get("reason", "")),
        age_sec=int(age) if isinstance(age, (int, float)) else None,
        exit_code=int(exit_code) if isinstance(exit_code, (int, float)) else None,
    )


def recent_backups(slug: str, limit: int = _MAX_PER_DEPT) -> List[BackupEvent]:
    """This dept's recent safety-net events, NEWEST FIRST. [] if none yet."""
    raw = read_events(str(settings.BACKUP_LOG_PATH), slug=slug, limit=limit)
    events = [e for e in (_to_event(r) for r in raw) if e is not None]
    events.reverse()  # read_events is chronological; UI wants newest first
    return events


def latest_backup(slug: str) -> Optional[BackupEvent]:
    """The single most recent event for this dept, or None."""
    events = recent_backups(slug, limit=1)
    return events[0] if events else None


@dataclass(frozen=True)
class BackupRollup:
    """Cross-dept summary for the home banner."""
    last_fire_ts: Optional[str]
    backed_up: int            # depts whose loop was stale → a tick ran
    healthy: int              # depts whose loop was alive → skipped
    failed: int               # backup ticks that exited non-zero
    # #1313 step 2: depts whose LATEST event is a deferral/degraded carry-over
    # — the floor correctly declined to act, but that is neither "healthy"
    # (the primary loop IS stale) nor "backed_up" (no tick actually ran).
    # Recurring here across fires is the exact Maya-incident shape.
    deferred: int = 0

    @property
    def any_activity(self) -> bool:
        return self.last_fire_ts is not None


def rollup() -> BackupRollup:
    """Summarise the most recent state of every dept seen in the log.

    "Last fire" is the newest ts across all depts. Counts reflect each dept's
    LATEST event (so a dept that recovered shows as healthy, not stuck)."""
    raw = read_events(str(settings.BACKUP_LOG_PATH))
    if not raw:
        return BackupRollup(last_fire_ts=None, backed_up=0, healthy=0, failed=0, deferred=0)
    latest = latest_per_dept(raw)
    backed_up = healthy = failed = deferred = 0
    last_ts: Optional[str] = None
    for r in latest.values():
        ev = _to_event(r)
        if ev is None:
            continue
        if last_ts is None or ev.ts > last_ts:
            last_ts = ev.ts
        if ev.action == "run":
            backed_up += 1
            if ev.exit_code not in (0, None):
                failed += 1
        elif ev.is_deferred:
            deferred += 1
        else:
            healthy += 1
    return BackupRollup(last_fire_ts=last_ts, backed_up=backed_up,
                        healthy=healthy, failed=failed, deferred=deferred)
