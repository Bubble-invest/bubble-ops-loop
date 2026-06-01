"""loop_backup.py — backup-execution decision for ops-loop depts.

A per-dept BACKUP runner fires twice a day (Joris 2026-06-01). For each
dept it decides whether the persistent /loop is alive (recent heartbeat →
skip) or dead/parked (stale heartbeat → run ONE backup dispatch tick).

This module is the PURE decision + a small heartbeat-locator helper. The
bash wrapper (loop-backup.sh) does the side effects: flock mutex, the
`claude -p` one-tick run, and Telegram notify.
"""
from __future__ import annotations

import glob
import os
import re
from typing import Optional


# An ISO-8601 UTC timestamp at the start of a heartbeat line, e.g.
# "2026-06-01T07:35:33Z tick ...". Used as a more precise liveness signal
# than file mtime (which a git checkout/rsync could bump).
_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z")


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
    """
    import datetime as _dt

    files = sorted(glob.glob(os.path.join(outputs_dir, "*", "heartbeat.log")))
    if not files:
        return None

    best: Optional[float] = None
    for fp in files:
        ts_epoch: Optional[float] = None
        try:
            # Read the tail; the last ISO ts is the most recent tick in that file.
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            matches = _ISO_RE.findall(text)
            if matches:
                dt = _dt.datetime.strptime(matches[-1], "%Y-%m-%dT%H:%M:%S")
                ts_epoch = dt.replace(tzinfo=_dt.timezone.utc).timestamp()
        except OSError:
            ts_epoch = None
        if ts_epoch is None:
            try:
                ts_epoch = os.path.getmtime(fp)
            except OSError:
                continue
        if best is None or ts_epoch > best:
            best = ts_epoch
    return best
