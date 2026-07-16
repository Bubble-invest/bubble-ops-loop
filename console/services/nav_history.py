"""
nav_history.py — NAV-over-time series for the fund's dept page (board #364).

Ben (family-office dept) writes one row per snapshot to `db/fund.sqlite`'s
`kpi_snapshots` table (columns: snapshot_at, nav, notes, ...). This module
reads that table directly (same access pattern as dept_registry.repo_path —
no new write path, no API layer) and builds a per-day NAV time series with
precomputed SVG geometry, mirroring whiteboard_series.py's contract: the
service does all the math, the template just renders.

WHY NOT A NAIVE PLOT OF kpi_snapshots.nav (research on #364, Rick 2026-07-16):
  37% of snapshots (13/35 measured) were written DEGRADED — a broker leg's
  live read failed and the row was written with that leg at $0 (or carried
  forward), understating NAV by tens of thousands of dollars. A raw line
  through those rows reads as a sawtooth of fake crashes.

  Ben's own chart tool (tools/nav_vs_acwi_chart.py) hit this first and
  EXCLUDES any row whose `notes` contains a `[DEGRADED:` marker. We follow
  the same authoritative marker (case-insensitively — some rows write
  `[degraded:` lowercase) but do NOT exclude: the console is a diagnostic
  surface for {{OPERATOR}}, and #364's own spec calls for rendering degraded
  points as a visible gap/dashed segment rather than silently dropping them
  — a silent drop looks identical to "no snapshot that day", which hides
  the fact that a write happened and was bad.

  #638 (filed same tick, approved 2026-07-16, execution in progress) adds a
  structural `degraded` BOOLEAN column to kpi_snapshots so a reader doesn't
  need to parse English out of `notes`. That column does not exist yet in
  the live schema (verified 2026-07-16). `_row_degraded` below checks for
  the column FIRST and falls back to the notes-marker heuristic — so this
  reader upgrades for free the moment #638's migration lands, with no
  further change needed here.

  This module does NOT implement TWR / the deposit-break (card #364's other
  open question, still needs Joris's convention call) — it plots raw
  consolidated NAV, which is what "the NAV chart" means until that's
  decided. See dept.py's PR-A scope note.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from console.services.dept_registry import repo_path

_log = logging.getLogger(__name__)

# Case-insensitive: Ben's own writer has emitted both `[DEGRADED:` and
# `[degraded:` across its history (kpi_compute.py vs consolidated_nav.py
# call sites) — matching case-sensitively would silently miss half of them.
_DEGRADED_MARKER_RE = re.compile(r"\[degraded", re.IGNORECASE)

# Available range-toggle windows, in days. "all" is handled separately.
RANGE_DAYS: Dict[str, Optional[int]] = {"30": 30, "90": 90, "all": None}
DEFAULT_RANGE = "90"


@dataclass(frozen=True)
class NavPoint:
    date: str            # ISO date, one point per day (last snapshot wins)
    nav: float
    degraded: bool
    note_excerpt: str = ""  # short, for the hover tooltip


@dataclass(frozen=True)
class NavHistory:
    """A NAV series ready for the template: precomputed SVG geometry plus
    the raw points (for the tooltip/table). Segments are split at degraded
    points so the polyline never draws a solid line THROUGH a bad read —
    each contiguous run of clean points is its own polyline; degraded points
    render as individual gapped markers via `degraded_points`.
    """
    points: List[NavPoint] = field(default_factory=list)
    range_key: str = DEFAULT_RANGE
    # SVG geometry, 100x32 viewbox (matches kpi-sparkline.html's convention).
    segments: List[str] = field(default_factory=list)   # clean-run polylines
    degraded_x: List[float] = field(default_factory=list)  # x of each degraded pt
    degraded_y: List[float] = field(default_factory=list)
    all_x: List[float] = field(default_factory=list)     # x for every point (tooltip hit-targets)
    all_y: List[float] = field(default_factory=list)
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    last_value: Optional[float] = None
    degraded_count: int = 0

    @property
    def has_chart(self) -> bool:
        return len(self.points) >= 2

    @property
    def last_display(self) -> str:
        if self.last_value is None:
            return "—"
        return f"${self.last_value:,.0f}"

    @property
    def min_display(self) -> str:
        return f"${self.min_value:,.0f}" if self.min_value is not None else "—"

    @property
    def max_display(self) -> str:
        return f"${self.max_value:,.0f}" if self.max_value is not None else "—"


def _row_degraded(notes: Optional[str], row_keys: Optional[set] = None,
                   row: Optional[sqlite3.Row] = None) -> bool:
    """True iff this snapshot row should render as degraded.

    Prefers a structural `degraded` column (#638) the moment it exists;
    falls back to the `[degraded:` marker convention in `notes` (the
    authoritative heuristic Ben's own nav_vs_acwi_chart.py uses today).
    """
    if row is not None and row_keys and "degraded" in row_keys:
        val = row["degraded"]
        if val is not None:
            return bool(val)
    return bool(notes) and bool(_DEGRADED_MARKER_RE.search(notes))


def _excerpt(notes: Optional[str], limit: int = 160) -> str:
    if not notes:
        return ""
    flat = " ".join(notes.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _parse_ts(s: str) -> datetime:
    """Parse the mixed timestamp formats actually present in kpi_snapshots
    (some rows are bare dates like '2026-06-28', most are ISO datetimes with
    or without a timezone offset, some with trailing 'Z')."""
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # Bare date, e.g. "2026-06-28"
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _load_rows(db_path: Path) -> List[Tuple[str, float, Optional[str], Optional[bool]]]:
    """Read (snapshot_at, nav, notes, degraded_or_None) from kpi_snapshots,
    oldest -> newest. Returns [] on any DB error (missing file, locked,
    missing table) — the caller degrades to an empty series, never a 500."""
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(
                "SELECT * FROM kpi_snapshots WHERE nav IS NOT NULL "
                "ORDER BY snapshot_at"
            )
            rows = cur.fetchall()
            row_keys = set(rows[0].keys()) if rows else set()
            out = []
            for r in rows:
                notes = r["notes"] if "notes" in row_keys else None
                degraded = None
                if "degraded" in row_keys and r["degraded"] is not None:
                    degraded = bool(r["degraded"])
                out.append((r["snapshot_at"], float(r["nav"]), notes, degraded))
            return out
        finally:
            con.close()
    except sqlite3.Error as exc:
        _log.warning("nav_history: failed reading %s: %s", db_path, exc)
        return []


def _daily_points(rows: List[Tuple[str, float, Optional[str], Optional[bool]]]) -> List[NavPoint]:
    """Collapse to one point per calendar day (last snapshot of the day
    wins — matches nav_vs_acwi_chart.py's convention), keeping the degraded
    flag/notes of whichever row wins that day."""
    by_day: Dict[str, NavPoint] = {}
    for snapshot_at, nav, notes, degraded_col in rows:
        try:
            d = _parse_ts(snapshot_at).date().isoformat()
        except ValueError:
            continue
        is_degraded = degraded_col if degraded_col is not None else bool(
            notes and _DEGRADED_MARKER_RE.search(notes)
        )
        by_day[d] = NavPoint(date=d, nav=nav, degraded=is_degraded,
                              note_excerpt=_excerpt(notes))
    return [by_day[d] for d in sorted(by_day)]


def _filter_range(points: List[NavPoint], range_key: str) -> List[NavPoint]:
    days = RANGE_DAYS.get(range_key, RANGE_DAYS[DEFAULT_RANGE])
    if days is None:
        return points
    if not points:
        return points
    from datetime import date, timedelta
    cutoff = date.fromisoformat(points[-1].date) - timedelta(days=days)
    return [p for p in points if date.fromisoformat(p.date) >= cutoff]


def _build_geometry(points: List[NavPoint]) -> Dict[str, Any]:
    """100x32 viewbox, same convention as whiteboard_series._build_geometry.
    Splits the polyline into contiguous clean-run segments so a degraded
    point never draws a solid connecting line — it renders as a standalone
    gapped marker instead (rendered by the template as a dashed/hollow dot)."""
    values = [p.nav for p in points]
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1.0
    n = len(points)
    w, h, pad = 100.0, 32.0, 2.0

    xs: List[float] = []
    ys: List[float] = []
    for i, p in enumerate(points):
        x = pad + (w - 2 * pad) * (i / (n - 1) if n > 1 else 0)
        y = pad + (h - 2 * pad) * (1 - (p.nav - vmin) / span)
        xs.append(round(x, 2))
        ys.append(round(y, 2))

    segments: List[str] = []
    degraded_x: List[float] = []
    degraded_y: List[float] = []
    current: List[str] = []
    for i, p in enumerate(points):
        if p.degraded:
            degraded_x.append(xs[i])
            degraded_y.append(ys[i])
            if current:
                segments.append(" ".join(current))
                current = []
            continue
        current.append(f"{xs[i]},{ys[i]}")
    if current:
        segments.append(" ".join(current))

    return {
        "segments": segments,
        "degraded_x": degraded_x,
        "degraded_y": degraded_y,
        "all_x": xs,
        "all_y": ys,
        "min_value": vmin,
        "max_value": vmax,
        "last_value": values[-1],
    }


def load_nav_history(slug: str, range_key: str = DEFAULT_RANGE) -> NavHistory:
    """Return the NAV time series for a dept (currently meaningful for
    'ben' — any dept without db/fund.sqlite gets an empty, graceful result).
    """
    if range_key not in RANGE_DAYS:
        range_key = DEFAULT_RANGE
    root = repo_path(slug)
    if root is None:
        return NavHistory(range_key=range_key)

    db_path = root / "db" / "fund.sqlite"
    rows = _load_rows(db_path)
    if not rows:
        return NavHistory(range_key=range_key)

    points = _daily_points(rows)
    points = _filter_range(points, range_key)
    if not points:
        return NavHistory(range_key=range_key)

    degraded_count = sum(1 for p in points if p.degraded)
    geo = _build_geometry(points) if len(points) >= 2 else {}

    return NavHistory(
        points=points,
        range_key=range_key,
        segments=geo.get("segments", []),
        degraded_x=geo.get("degraded_x", []),
        degraded_y=geo.get("degraded_y", []),
        all_x=geo.get("all_x", []),
        all_y=geo.get("all_y", []),
        min_value=geo.get("min_value"),
        max_value=geo.get("max_value"),
        last_value=geo.get("last_value", points[-1].nav if points else None),
        degraded_count=degraded_count,
    )
