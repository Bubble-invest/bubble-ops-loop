"""
morty_reader.py — real per-dept activity for the Carnet de bord (/health).

History: in UX-3 this was a thin stub that marked every (dept × layer) as
never-run + stale, so the Carnet de bord showed no live activity at all
({{OPERATOR}} msg 1180, 2026-06-01: "stale and not showing live real activity").
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
    """Dept-level loop liveness — {slug: LoopPulse}.

    Liveness = the NEWEST of (a) the heartbeat.log timestamp AND (b) the newest
    per-layer `.last-run` across all layers. A dept writes heartbeat.log only on
    an IDLE tick (decision=heartbeat); on a DISPATCH tick (L1/L2/L3/L4) it writes
    `.last-run` + round_counter but NOT a heartbeat line. So a busy dept that
    keeps dispatching has a STALE heartbeat.log while being perfectly alive —
    keying liveness off heartbeat.log alone shows an actively-working loop as
    DEAD (the 2026-06-05 silent-failure bug: ben red-flagged at 17h heartbeat
    while he'd just ticked L3 97 min ago). Both signals mean "the loop ran", so
    we take their max.
    """
    now = now_epoch if now_epoch is not None else time.time()
    out: Dict[str, LoopPulse] = {}
    for dept in depts:
        root = repo_path(dept)
        outputs = str(root / "outputs") if root is not None else ""
        hb = latest_heartbeat_epoch(outputs) if outputs else None
        # newest dispatch-tick signal across all layers
        last_run = None
        if outputs:
            for _layer in (1, 2, 3, 4):
                lr = _newest_layer_last_run(outputs, _layer)
                if lr is not None and (last_run is None or lr > last_run):
                    last_run = lr
        # loop is alive if EITHER signal is recent
        pulse_ts = max((t for t in (hb, last_run) if t is not None), default=None)
        if pulse_ts is None:
            out[dept] = LoopPulse(dept=dept, heartbeat_iso="", age_human="",
                                  alive=False, age_sec=None)
            continue
        age = now - pulse_ts
        iso = _dt.datetime.fromtimestamp(pulse_ts, _dt.timezone.utc).strftime(
            _ISO_FMT + "Z")
        out[dept] = LoopPulse(dept=dept, heartbeat_iso=iso,
                              age_human=_age_human(age),
                              alive=age <= _STALE_PULSE_SEC, age_sec=age)
    return out


# Files we never surface as "artifacts" — they're plumbing, not output.
_ARTIFACT_SKIP = {".last-run", "summary.md", "logs.jsonl", "round_counter.json"}
_SUMMARY_MAX_CHARS = 1400


def _newest_layer_dir(outputs_dir: str, layer: int) -> Optional[str]:
    """Path to the most-recent `<outputs>/<date>/<layer>/` dir, or None."""
    best_fp, best_ts = None, None
    for fp in glob.glob(os.path.join(outputs_dir, "*", str(layer))):
        if not os.path.isdir(fp):
            continue
        lr = os.path.join(fp, ".last-run")
        ts = None
        try:
            with open(lr, "r", encoding="utf-8", errors="replace") as fh:
                ts = _parse_iso(fh.read())
        except OSError:
            ts = None
        if ts is None:
            try:
                ts = os.path.getmtime(fp)
            except OSError:
                continue
        if best_ts is None or ts > best_ts:
            best_fp, best_ts = fp, ts
    return best_fp


def layer_output_detail(dept: str, layer: int,
                        now_epoch: Optional[float] = None) -> Dict[str, object]:
    """Detail for ONE (dept × layer), for the click-through panel on /health.

    Returns: {dept, layer, never_run, last_iso, age_human, age_sec,
              summary (markdown snippet or ""), artifacts: [filename,...]}.
    Reads the newest date-dir for that layer; degrades gracefully to a
    never-run shell if nothing on disk."""
    now = now_epoch if now_epoch is not None else time.time()
    root = repo_path(dept)
    base = {"dept": dept, "layer": layer, "never_run": True,
            "last_iso": "", "age_human": "", "age_sec": None,
            "summary": "", "artifacts": []}
    if root is None:
        return base
    ldir = _newest_layer_dir(str(root / "outputs"), layer)
    if ldir is None:
        return base
    # last-run
    ts = None
    try:
        with open(os.path.join(ldir, ".last-run"), "r",
                  encoding="utf-8", errors="replace") as fh:
            ts = _parse_iso(fh.read())
    except OSError:
        ts = None
    if ts is None:
        try:
            ts = os.path.getmtime(ldir)
        except OSError:
            ts = None
    if ts is not None:
        base["never_run"] = False
        base["last_iso"] = _dt.datetime.fromtimestamp(
            ts, _dt.timezone.utc).strftime(_ISO_FMT + "Z")
        base["age_sec"] = now - ts
        base["age_human"] = _age_human(now - ts)
    # summary snippet
    try:
        with open(os.path.join(ldir, "summary.md"), "r",
                  encoding="utf-8", errors="replace") as fh:
            base["summary"] = fh.read(_SUMMARY_MAX_CHARS)
    except OSError:
        base["summary"] = ""
    # artifacts (real output files, not plumbing)
    try:
        base["artifacts"] = sorted(
            f for f in os.listdir(ldir)
            if f not in _ARTIFACT_SKIP and not f.startswith(".")
            and os.path.isfile(os.path.join(ldir, f))
        )
    except OSError:
        base["artifacts"] = []
    return base
