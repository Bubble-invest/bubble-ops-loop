"""
morty_reader.py — real per-dept activity for the Carnet de bord (/health).

History: in UX-3 this was a thin stub that marked every (dept × layer) as
never-run + stale, so the Carnet de bord showed no live activity at all
(Joris msg 1180, 2026-06-01: "stale and not showing live real activity").
The "real journalctl wiring lands in UX-5" plan assumed the console ran
off-box and had to SSH into Morty. It no longer does — the console runs ON
the box and already reads each dept repo from disk. So the live source is
right here: the loop writes, every run,
    outputs/<date>/<layer>/.last-run     (ISO ts — when that moment last ran)
    outputs/<date>/heartbeat.log         (ISO ts per tick — loop liveness)
This module reads those directly. No SSH, no journald.
"""
from __future__ import annotations

import datetime as _dt
import glob
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from console.services.dept_registry import repo_path
from scripts.lib.loop_backup import latest_heartbeat_epoch

# A layer (a "moment") runs ~daily. Flag it as unusually silent if its last
# run is older than this. Generous so a normal day's gap never false-alarms.
_STALE_LAYER_SEC = 30 * 3600          # 30 h
# The loop ticks every few minutes; its heartbeat going quiet for this long
# means the /loop is dead/parked (same threshold as the backup safety net).
_STALE_PULSE_SEC = 90 * 60            # 90 min

_ISO_FMT = "%Y-%m-%dT%H:%M:%S"


@dataclass(frozen=True)
class LayerHeartbeat:
    dept: str
    layer: int
    last_success_iso: str    # "" if the layer never ran
    age_human: str           # "" if never; else "il y a 2 h" / "il y a 3 j"
    is_stale: bool           # never-run, or older than _STALE_LAYER_SEC
    never_run: bool = False  # True iff the layer has no .last-run at all
    age_sec: Optional[float] = None  # seconds since last run; None if never


@dataclass(frozen=True)
class LoopPulse:
    """Dept-level loop liveness — the most "live" signal (ticks every few
    minutes)."""
    dept: str
    heartbeat_iso: str       # "" if no heartbeat ever
    age_human: str
    alive: bool              # heartbeat seen within _STALE_PULSE_SEC
    age_sec: Optional[float] = None  # seconds since last heartbeat; None if never


def _parse_iso(s: str) -> Optional[float]:
    """Parse a 'YYYY-MM-DDTHH:MM:SSZ' (or with offset) stamp to epoch."""
    s = s.strip()
    if not s:
        return None
    cleaned = s[:19]  # drop any trailing Z/offset/fraction — second precision
    try:
        dt = _dt.datetime.strptime(cleaned, _ISO_FMT)
    except ValueError:
        return None
    return dt.replace(tzinfo=_dt.timezone.utc).timestamp()


def _age_human(age_sec: float) -> str:
    """Compact French relative age."""
    if age_sec < 0:
        return "à l'instant"
    if age_sec < 90:
        return "il y a moins d'une minute"
    if age_sec < 3600:
        return f"il y a {round(age_sec / 60)} min"
    if age_sec < 36 * 3600:
        return f"il y a {round(age_sec / 3600)} h"
    return f"il y a {round(age_sec / 86400)} j"


def _newest_layer_last_run(outputs_dir: str, layer: int) -> Optional[float]:
    """Newest `.last-run` epoch for a layer across all date dirs, or None."""
    pattern = os.path.join(outputs_dir, "*", str(layer), ".last-run")
    best: Optional[float] = None
    for fp in glob.glob(pattern):
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                ts = _parse_iso(fh.read())
        except OSError:
            ts = None
        if ts is None:
            try:
                ts = os.path.getmtime(fp)
            except OSError:
                continue
        if best is None or ts > best:
            best = ts
    return best


def per_dept_layer_heartbeats(
    depts: List[str],
    now_epoch: Optional[float] = None,
) -> List[LayerHeartbeat]:
    """One row per (dept × layer 1..4), from real on-disk loop outputs."""
    now = now_epoch if now_epoch is not None else time.time()
    rows: List[LayerHeartbeat] = []
    for dept in depts:
        root = repo_path(dept)
        outputs = str(root / "outputs") if root is not None else ""
        for layer in (1, 2, 3, 4):
            ts = _newest_layer_last_run(outputs, layer) if outputs else None
            if ts is None:
                rows.append(LayerHeartbeat(
                    dept=dept, layer=layer, last_success_iso="",
                    age_human="", is_stale=True, never_run=True, age_sec=None))
                continue
            age = now - ts
            iso = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
                _ISO_FMT + "Z")
            rows.append(LayerHeartbeat(
                dept=dept, layer=layer, last_success_iso=iso,
                age_human=_age_human(age), is_stale=age > _STALE_LAYER_SEC,
                never_run=False, age_sec=age))
    return rows


def loop_pulse(
    depts: List[str],
    now_epoch: Optional[float] = None,
) -> Dict[str, LoopPulse]:
    """Dept-level loop liveness from the heartbeat log — {slug: LoopPulse}."""
    now = now_epoch if now_epoch is not None else time.time()
    out: Dict[str, LoopPulse] = {}
    for dept in depts:
        root = repo_path(dept)
        hb = latest_heartbeat_epoch(str(root / "outputs")) if root is not None else None
        if hb is None:
            out[dept] = LoopPulse(dept=dept, heartbeat_iso="", age_human="",
                                  alive=False, age_sec=None)
            continue
        age = now - hb
        iso = _dt.datetime.fromtimestamp(hb, _dt.timezone.utc).strftime(
            _ISO_FMT + "Z")
        out[dept] = LoopPulse(dept=dept, heartbeat_iso=iso,
                              age_human=_age_human(age),
                              alive=age <= _STALE_PULSE_SEC, age_sec=age)
    return out
