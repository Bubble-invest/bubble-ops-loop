"""loop_backup.py — backup-execution decision for ops-loop depts.

A per-dept BACKUP runner fires twice a day (Joris 2026-06-01). For each
dept it decides whether the persistent /loop is alive (recent heartbeat →
skip) or dead/parked (stale heartbeat → run ONE backup dispatch tick).

This module is the PURE decision + a small heartbeat-locator helper. The
bash wrapper (loop-backup.sh) does the side effects: flock mutex, the
`claude -p` one-tick run, and Telegram notify.

It also owns the EVENT LOG (Joris msg 1171, 2026-06-01): every fire appends
one JSON line per dept to a central jsonl so the cockpit can surface the
safety-net result in the front end (was journal-only, invisible to the UI).
Writer lives here; the console reads it back via the same `read_events`.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
from typing import List, Optional


# An ISO-8601 UTC timestamp at the start of a heartbeat line. Used as a more
# precise liveness signal than file mtime (which a git checkout/rsync could
# bump). Accepts BOTH canonical forms agents emit in the wild:
#   "2026-06-01T07:35:33Z tick ..."                 (hand-built, Z suffix)
#   "2026-06-02T13:30:35.931407+00:00 tick ..."     (datetime.isoformat())
# Bug 2026-06-04: the old regex required a literal Z, so the microsecond/offset
# form never matched → latest_heartbeat_epoch silently fell back to file mtime
# → a frozen-date loop read as FALSE-FRESH on the cockpit (masking staleness —
# the exact failure this function's mtime-avoidance was meant to prevent).
_ISO_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))"
)


def backup_decision(
    latest_heartbeat_epoch: Optional[float],
    now_epoch: float,
    stale_after_sec: int,
) -> dict:
    """Decide whether to run a backup tick.

    Parameters
    ----------
    latest_heartbeat_epoch : float | None
        Epoch seconds of the most recent heartbeat, or None if none found.
    now_epoch : float
        Current epoch seconds.
    stale_after_sec : int
        A loop is considered dead/parked if its last heartbeat is older
        than this many seconds.

    Returns
    -------
    dict
        {"action": "run"|"skip", "reason": str, "age_sec": int|None}
    """
    if latest_heartbeat_epoch is None:
        return {
            "action": "run",
            "reason": "no heartbeat found — loop never ticked or output missing",
            "age_sec": None,
        }
    age = int(now_epoch - latest_heartbeat_epoch)
    if age < 0:
        # Clock skew: heartbeat in the future. Treat as fresh (alive).
        return {
            "action": "skip",
            "reason": "loop alive (heartbeat fresh — future ts, clock skew)",
            "age_sec": age,
        }
    if age <= stale_after_sec:
        return {
            "action": "skip",
            "reason": f"loop alive (heartbeat fresh, age={age}s ≤ {stale_after_sec}s)",
            "age_sec": age,
        }
    return {
        "action": "run",
        "reason": f"loop stale (heartbeat age={age}s > {stale_after_sec}s) — backing up",
        "age_sec": age,
    }


def latest_heartbeat_epoch(outputs_dir: str) -> Optional[float]:
    """Return the epoch of the newest heartbeat across recent date dirs.

    Reads the last ISO timestamp from each ``<outputs>/<YYYY-MM-DD>/heartbeat.log``
    and returns the maximum (most recent). Falls back to the newest file
    mtime if no parseable timestamp is found. Returns None if no heartbeat
    file exists at all.

    Heartbeats are the dept's own liveness signal — using the in-file ISO
    timestamp (not mtime) avoids false-fresh readings if a deploy/rsync
    touches the file without the loop actually ticking.

    Truthful-heartbeat exclusion (Rick 2026-06-19): the floor appends a
    ``... tick BACKUP-FAILED ... — dept DOWN`` line when its OWN backup tick
    failed (dept genuinely down). That line carries a CURRENT timestamp, so a
    naive "last ISO ts" read would treat the dept as FRESH-ALIVE and the floor
    would stop re-firing — exactly the false-fresh failure this signal exists to
    kill. So a ``BACKUP-FAILED`` line is NOT counted as a liveness signal: we
    fall back to the last REAL heartbeat before it (or None). A ``BACKUP-RAN``
    or ``DEGRADED-L4`` line DOES count — the dept was actually serviced.
    """
    import datetime as _dt

    files = sorted(glob.glob(os.path.join(outputs_dir, "*", "heartbeat.log")))
    if not files:
        return None

    best: Optional[float] = None
    for fp in files:
        ts_epoch: Optional[float] = None
        text = ""
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            # Walk lines newest→oldest; the freshness signal is the most recent
            # ISO-stamped line that is NOT a BACKUP-FAILED ("dept DOWN") marker.
            for line in reversed(text.splitlines()):
                if HB_BACKUP_FAILED in line:
                    continue  # down-marker: never a liveness signal
                m = _ISO_RE.search(line)
                if not m:
                    continue
                # Normalise trailing Z → +00:00 so fromisoformat() (3.9) accepts
                # both the "...SSZ" and "...SS.ffffff+00:00" forms agents emit.
                raw = m.group(1).replace("Z", "+00:00")
                dt = _dt.datetime.fromisoformat(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_dt.timezone.utc)
                ts_epoch = dt.timestamp()
                break
        except OSError:
            ts_epoch = None
        if ts_epoch is None:
            # No parseable non-down line. If the file has ONLY down-markers we
            # must NOT fall back to mtime (that would re-introduce false-fresh);
            # only fall back to mtime when there were no ISO lines at all.
            if HB_BACKUP_FAILED in text:
                continue
            try:
                ts_epoch = os.path.getmtime(fp)
            except OSError:
                continue
        if best is None or ts_epoch > best:
            best = ts_epoch
    return best


# ─── Event log ───────────────────────────────────────────────────────────
#
# Each fire of the backup timer appends one line per dept. The cockpit reads
# this back to render the "Filet de sécurité" block on each dept page + the
# home roll-up. Kept dead-simple (append-only JSONL) so a half-written line
# never corrupts the file and the reader can skip it.


def now_iso() -> str:
    """Current time as an ISO-8601 UTC second-resolution stamp (matches the
    heartbeat-line format the rest of the system uses)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_event(
    slug: str,
    action: str,
    reason: str,
    age_sec: Optional[int] = None,
    exit_code: Optional[int] = None,
    ts: Optional[str] = None,
) -> dict:
    """Build one event record. `ts` defaults to now (UTC ISO).

    action is "skip" (loop alive — no tick) or "run" (loop stale — a backup
    tick was attempted). `exit_code` is set only for runs.
    """
    ev: dict = {
        "ts": ts or now_iso(),
        "slug": slug,
        "action": action,
        "reason": reason,
    }
    if age_sec is not None:
        ev["age_sec"] = int(age_sec)
    if exit_code is not None:
        ev["exit"] = int(exit_code)
    return ev


def append_event(path: str, event: dict) -> None:
    """Append one event as a JSON line, creating parent dirs as needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(
    path: str,
    slug: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[dict]:
    """Read the event log (chronological, oldest→newest).

    Skips blank and unparseable lines (a half-written tail never breaks the
    reader). Optionally filter by `slug` and keep only the last `limit`.
    Returns [] if the file is absent.
    """
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
    if limit is not None and limit >= 0:
        out = out[-limit:]
    return out


def latest_per_dept(events: List[dict]) -> dict:
    """Reduce a chronological event list to {slug: most-recent-event}."""
    latest: dict = {}
    for ev in events:
        s = ev.get("slug")
        if s:
            latest[s] = ev
    return latest


# ─── Truthful external heartbeat (Rick 2026-06-19) ───────────────────────────
#
# The fleet liveness signal is a free-text line each dept writes ITSELF into
# outputs/<date>/heartbeat.log. Every consumer (watchdog, this floor's
# freshness gate, cockpit) only checks the timestamp FRESHNESS, never the
# truth/content. So when a session dies the thing that writes the heartbeat
# dies with it — a silent hole with no "I'm down" signal (Maya 2026-06-18: 13h
# hole; Ben 2026-06-18: 0 heartbeat lines, the degraded honesty lived only in
# state/loop-backup.jsonl which the watchdog ignores).
#
# Fix: the backup floor — the external observer that already detected the
# staleness — becomes the AUTHORITATIVE writer of a TRUTHFUL liveness line into
# the dept's OWN heartbeat.log, encoding the real OUTCOME of its intervention.
# This collapses the two channels (heartbeat freshness vs loop-backup.jsonl
# truth) into one signal: a downstream consumer reading the heartbeat tail now
# sees WHY it's the floor writing and whether the dept is actually up.
#
# The line shape mirrors the agents' own `<iso> tick ...` convention so the
# existing _ISO_RE parser + every freshness consumer keeps working unchanged;
# the trailing token is the truthful outcome the consumer can grep for.

# Stable outcome tokens (grep-able by the watchdog / cockpit).
HB_BACKUP_RAN = "BACKUP-RAN-FOR-DEPT"   # floor ran a layer tick OK in the dept's place
HB_BACKUP_FAILED = "BACKUP-FAILED"      # floor tried but the backup tick itself failed → dept DOWN
HB_DEGRADED_L4 = "DEGRADED-L4"          # degraded L4 carried-over (loop was down today)


def format_external_heartbeat(
    outcome: str,
    layer: Optional[int] = None,
    exit_code: Optional[int] = None,
    ts: Optional[str] = None,
) -> str:
    """Build ONE truthful heartbeat line the floor appends to the dept's
    outputs/<today>/heartbeat.log when the live loop is stale.

    The line starts with an ISO-8601 UTC timestamp (so latest_heartbeat_epoch
    and every freshness consumer keep parsing it) followed by ``tick`` and a
    grep-able outcome token. Cases (one per call site):

      * floor ran a layer tick OK   → ``<iso> tick BACKUP-RAN-FOR-DEPT layer=N exit=0``
      * floor's backup tick FAILED  → ``<iso> tick BACKUP-FAILED exit=N — dept DOWN``
      * degraded L4 carried-over    → ``<iso> tick DEGRADED-L4 carried-over``

    `outcome` is one of the HB_* constants. `ts` defaults to now (UTC ISO,
    second resolution — same shape as now_iso()).
    """
    stamp = ts or now_iso()
    if outcome == HB_BACKUP_RAN:
        body = HB_BACKUP_RAN
        if layer is not None:
            body += f" layer={int(layer)}"
        # exit is part of the truth even on the OK path (always 0 here, but
        # explicit so a consumer never has to assume).
        body += f" exit={int(exit_code) if exit_code is not None else 0}"
    elif outcome == HB_BACKUP_FAILED:
        code = int(exit_code) if exit_code is not None else 1
        body = f"{HB_BACKUP_FAILED} exit={code} — dept DOWN"
    elif outcome == HB_DEGRADED_L4:
        body = f"{HB_DEGRADED_L4} carried-over"
    else:
        raise ValueError(f"unknown external-heartbeat outcome: {outcome!r}")
    return f"{stamp} tick {body}"


def append_external_heartbeat(
    heartbeat_path: str,
    outcome: str,
    layer: Optional[int] = None,
    exit_code: Optional[int] = None,
    ts: Optional[str] = None,
) -> str:
    """Append one truthful heartbeat line (format_external_heartbeat) to the
    dept's heartbeat.log, creating parent dirs as needed. Returns the line
    written (without the trailing newline) so the caller can log it.

    Append-only + a trailing newline so a half-written line never corrupts the
    file and the freshness reader (which reads the LAST ISO ts) keeps working.
    """
    line = format_external_heartbeat(outcome, layer=layer, exit_code=exit_code, ts=ts)
    parent = os.path.dirname(heartbeat_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(heartbeat_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line
