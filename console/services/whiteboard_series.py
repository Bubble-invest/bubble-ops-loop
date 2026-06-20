"""
whiteboard_series.py — build per-dept KPI time-series from Layer-4 history.

The whiteboard (dept_detail "Tableau de bord") shows two things:
  1. Curated KPI cards + notes — read from the dept's `whiteboard.yaml`
     (see github_reader.load_whiteboard). Layer 4 refreshes this each loop.
  2. Graphs of those KPIs over time — built HERE, by walking every
     Layer-4 daily output and extracting the numeric KPIs.

Layer 4 already emits, once per loop run (Joris msg 1163, 2026-06-01):
    outputs/<YYYY-MM-DD>/4/management-export.yaml   (top_kpis: flat numbers)
    outputs/<YYYY-MM-DD>/4/risk-kpis.yaml           (richer, nested)

So the history we need to plot already lives on disk — one datapoint per
day per KPI. We do NOT add a new write path; we read the canonical L4
artifact. `management-export.yaml::top_kpis` is the primary source (it is
explicitly "the export for the console" — flat, curated, numeric). When a
dept has no management-export we fall back to flattening risk-kpis.yaml.

A "metric" is one KPI tracked over time. We return a list of MetricSeries,
each with the points sorted oldest→newest plus precomputed SVG geometry so
the template stays logic-free.

Ben (family-office dept) special case (#139):
  Ben's management-export.yaml wraps everything under an `export:` key and
  emits NO `top_kpis` block — it uses richer named sub-dicts (`nav_summary`,
  `performance`, `sleeve_allocation_pct_nav`).  We synthesise the curated
  KPI set from those known blocks rather than requiring Ben to restructure his
  L4 prompt.  The synthesised keys are stable short names so the chart labels
  are human-readable and the series are comparable across dates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from console.services.dept_registry import repo_path

_log = logging.getLogger(__name__)

# Cap how many days back we plot — keeps the chart readable and the scan
# bounded. 60 days of daily loop runs is plenty of trend signal.
_MAX_POINTS = 60

# Cap how many KPIs we graph. Curated sources (top_kpis/kpis_snapshot) are
# small; this mainly guards the risk-kpis fallback, which can flatten into
# dozens of leaves (Maya's L4 has ~19). We keep the most *variable* series
# (those that actually move) and note the rest was dropped.
_MAX_SERIES = 12

# Curated KPI blocks inside management-export.yaml, in priority order. Depts
# drifted on the field name (Notion v4 spec said `top_kpis`; Maya's L4 emits
# `kpis_snapshot`) — accept either. First one that flattens to a non-empty
# numeric dict wins.
_CURATED_KEYS = ("top_kpis", "kpis_snapshot")

# Flattened risk-kpis leaf keys we never want to plot: constants (limits),
# booleans, and identity fields. Matched against the LAST dotted segment.
_SKIP_LEAF = {"limit", "breached", "dry_run", "date", "dept",
              "last_successful_layer"}


@dataclass(frozen=True)
class MetricSeries:
    """One KPI tracked across Layer-4 runs, with SVG geometry precomputed."""
    key: str                       # dotted metric key, e.g. "ops_health_score"
    label: str                     # humanized label for display
    points: List[Tuple[str, float]]  # [(iso_date, value), ...] oldest→newest
    # Precomputed for the template (logic-free rendering):
    polyline: str = ""             # "x,y x,y ..." in a 100x32 viewbox
    last_value: Optional[float] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    trend: str = "stable"          # up | down | stable (last vs first)
    area: str = ""                 # closed polygon points for the fill

    @property
    def has_chart(self) -> bool:
        """Need >=2 points to draw a meaningful line."""
        return len(self.points) >= 2

    @staticmethod
    def _fmt(v: Optional[float]) -> str:
        """Drop the trailing .0 on whole numbers; 1 decimal otherwise."""
        if v is None:
            return "—"
        return str(int(v)) if float(v).is_integer() else f"{v:.1f}"

    @property
    def last_display(self) -> str:
        return self._fmt(self.last_value)

    @property
    def min_display(self) -> str:
        return self._fmt(self.min_value)

    @property
    def max_display(self) -> str:
        return self._fmt(self.max_value)


def _humanize_key(key: str) -> str:
    """Turn a dotted snake_case metric key into operator-readable French-ish
    prose. Tight mapping for the known headline KPIs; graceful fallback for
    anything else (strip the snake_case → spaced words)."""
    known = {
        "ops_health_score": "Score de santé ops",
        "directives_emitted_today": "Directives émises / jour",
        "directives.emitted_today": "Directives émises / jour",
        "escalations_open": "Escalades ouvertes",
        "escalations.to_joris_open": "Escalades ouvertes",
        "open_gates": "Gates ouvertes",
        "open_exceptions": "Exceptions ouvertes",
        "gates.open_total": "Gates ouvertes (total)",
        "gates.opened_today": "Gates ouvertes / jour",
        "directives.open_pending_approval": "Directives en attente",
        # Maya (prospection) curated KPIs — kpis_snapshot block.
        "reply_rate_7d": "Taux de réponse (7j)",
        "validation_latency_p50_hours": "Latence validation p50 (h)",
        "drafts_pending": "Drafts en attente",
        "drafts_over_3d": "Drafts bloqués > 3j",
        # Ben (family-office) synthesised KPIs — from nav_summary + performance
        # + sleeve_allocation_pct_nav blocks in management-export.yaml (#139).
        "nav_usd": "NAV (USD)",
        "cash_pct": "Cash (%)",
        "return_itd_pct": "Rendement ITD (%)",
        "return_mtd_pct": "Rendement MTD (%)",
        "drawdown_pct": "Drawdown en cours (%)",
        "max_drawdown_pct": "Drawdown max ITD (%)",
        "sharpe_itd": "Sharpe ITD",
        "sleeve_etf_pct": "ETF backbone (% NAV)",
        "sleeve_single_stock_pct": "Actions single-stock (% NAV)",
        "sleeve_crypto_pct": "Crypto (% NAV)",
    }
    if key in known:
        return known[key]
    # Fallback: last segment, snake → spaced, capitalized.
    leaf = key.split(".")[-1]
    return leaf.replace("_", " ").capitalize()


def _flatten_numeric(obj: Any, prefix: str = "") -> Dict[str, float]:
    """Recursively flatten a dict into {dotted_key: float} keeping only
    numeric (int/float, non-bool) leaves. Booleans are skipped — `True` is an
    int subclass in Python and we never want to plot a flag."""
    out: Dict[str, float] = {}
    if not isinstance(obj, dict):
        return out
    for k, v in obj.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        leaf = str(k)
        if isinstance(v, dict):
            out.update(_flatten_numeric(v, key))
        elif isinstance(v, bool):
            continue
        elif isinstance(v, (int, float)):
            if leaf in _SKIP_LEAF:
                continue
            out[key] = float(v)
    return out


def _ben_kpis_from_export(export: Dict[str, Any]) -> Dict[str, float]:
    """Synthesise the curated KPI set for Ben (family-office dept) from the
    named sub-dicts he already emits in management-export.yaml.

    Ben does not emit a `top_kpis` block (#139 fix) — instead he writes
    `nav_summary`, `performance` (or `performance_vs_benchmark`), and
    `sleeve_allocation_pct_nav`.  We extract a stable, flat set of chartable
    KPIs from those, returning an empty dict when the export doesn't look like
    Ben's structure (so the caller falls through gracefully).

    Key design choices:
    - NAV: try both `consolidated_nav_usd` (recent) and
      `consolidated_nav_usd_true` (older weekly-review format).
    - Only pick *numeric, non-bool* values; skip string/None fields.
    - Keep the key set small (9 series) and stable across dates.
    """
    out: Dict[str, float] = {}

    nav = export.get("nav_summary")
    if isinstance(nav, dict):
        # NAV headline — field name drifted heavily across Ben's L4 versions.
        # Priority order: prefer the "true" consolidated NAV with the most
        # informative name, falling back to est/reference variants.
        for field_name in (
            "consolidated_nav_usd",         # standard since 06-10
            "consolidated_nav_usd_true",    # weekly-review format (06-13)
            "consolidated_nav_usd_corrected",  # saxo-degrade-corrected (06-09)
            "consolidated_nav_usd_est",     # early format (06-05, 06-07)
            "consolidated_nav_usd_reference",  # reference-only variant
        ):
            v = nav.get(field_name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out["nav_usd"] = float(v)
                break
        # If none of the known names matched, try any key containing
        # "consolidated_nav" (forward-compat for future field-name drift).
        if "nav_usd" not in out:
            for k, v in nav.items():
                if "consolidated_nav" in k and isinstance(v, (int, float)) and not isinstance(v, bool):
                    out["nav_usd"] = float(v)
                    break
        v = nav.get("cash_pct")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out["cash_pct"] = float(v)

    # performance block — named `performance` (recent) or
    # `performance_vs_benchmark` (older weekly-review format).
    perf = export.get("performance") or export.get("performance_vs_benchmark")
    if isinstance(perf, dict):
        _perf_map = {
            "total_return_itd_pct": "return_itd_pct",
            "total_return_mtd_pct": "return_mtd_pct",
            "current_drawdown_pct": "drawdown_pct",
            "max_drawdown_itd_pct": "max_drawdown_pct",
            "sharpe_itd": "sharpe_itd",
        }
        for src, dst in _perf_map.items():
            v = perf.get(src)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[dst] = float(v)

    sleeve = export.get("sleeve_allocation_pct_nav")
    if isinstance(sleeve, dict):
        # Sleeve keys also drifted: `single_stock` vs `single_stock_3a` etc.
        _sleeve_map: List[Tuple[str, str]] = [
            ("etf_backbone", "sleeve_etf_pct"),
            ("single_stock", "sleeve_single_stock_pct"),
            ("single_stock_3a", "sleeve_single_stock_pct"),
            ("crypto_true", "sleeve_crypto_pct"),
            ("crypto_3b", "sleeve_crypto_pct"),
        ]
        for src, dst in _sleeve_map:
            if dst in out:
                continue  # first match wins
            v = sleeve.get(src)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[dst] = float(v)

    return out


def _kpis_for_date(date_dir: Path, *, dept_slug: str = "") -> Dict[str, float]:
    """Extract the numeric KPIs for one date dir.

    Primary source: management-export.yaml's curated KPI block (`top_kpis` or
    `kpis_snapshot` — depts drifted on the name). Looked for both at
    `<date>/management-export.yaml` (Notion v4 spec) and
    `<date>/4/management-export.yaml` (where the live dept PROMPT.md writes
    it). Fallback: flatten `<date>/4/risk-kpis.yaml`.

    dept_slug is the canonical dept identifier (e.g. "ben").  It gates the
    Ben-specific KPI synthesiser: the synthesiser fires ONLY for slug=="ben",
    so a future dept that happens to emit a `nav_summary` block never gets
    Ben's fund-office synthesiser applied to it.  (#199)
    """
    # 1) management-export curated KPIs (preferred — the dept declares the few
    #    KPIs it wants charted via a `top_kpis`/`kpis_snapshot` block; the cockpit
    #    only renders what the dept curates). Depts wrap their export under a
    #    top-level `export:` key (e.g. Ben), so look for the curated block BOTH at
    #    the doc root AND one level down under `export:`. We do NOT synthesize a
    #    default curation here — absent a declared block we fall through to the
    #    risk-kpis fallback (curation is the dept's call, not the cockpit's).
    for rel in ("management-export.yaml", "4/management-export.yaml"):
        data = _safe_load_yaml(date_dir / rel)
        if not isinstance(data, dict):
            continue
        scopes = [data]
        if isinstance(data.get("export"), dict):
            scopes.append(data["export"])
        for scope in scopes:
            for block in _CURATED_KEYS:
                curated = scope.get(block)
                if isinstance(curated, dict):
                    flat = _flatten_numeric(curated)
                    if flat:
                        return flat
            # 1b) Ben-specific: synthesise from nav_summary + performance +
            #     sleeve_allocation_pct_nav when no top_kpis block present.
            #     GATE: only fires when dept_slug == "ben" — an explicit
            #     contract on the dept identity, NOT on field presence.
            #     A future dept that emits nav_summary must not get Ben's
            #     fund-office synthesiser applied to it.  (#139, hardened #199)
            if dept_slug == "ben" and isinstance(scope.get("nav_summary"), dict):
                ben_kpis = _ben_kpis_from_export(scope)
                if ben_kpis:
                    return ben_kpis

    # 2) fallback: risk-kpis.yaml (richer nested), flatten under its `kpis`
    #    block if present, else the whole doc.
    rk = _safe_load_yaml(date_dir / "4" / "risk-kpis.yaml")
    if isinstance(rk, dict):
        inner = rk.get("kpis") if isinstance(rk.get("kpis"), dict) else rk
        flat = _flatten_numeric(inner)
        if flat:
            return flat

    return {}


def _safe_load_yaml(p: Path) -> Optional[Any]:
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        _log.warning("yaml parse error for %s: %s", p, exc)
        return None


def _build_geometry(points: List[Tuple[str, float]]) -> Dict[str, Any]:
    """Map a series of (date, value) into a 100x32 SVG viewbox. Returns
    polyline points, area-fill polygon, min/max/last, and trend."""
    values = [v for _, v in points]
    vmin, vmax = min(values), max(values)
    last = values[-1]
    span = (vmax - vmin) or 1.0  # avoid /0 on a flat line
    n = len(points)
    w, h, pad = 100.0, 32.0, 2.0

    coords: List[Tuple[float, float]] = []
    for i, v in enumerate(values):
        x = pad + (w - 2 * pad) * (i / (n - 1) if n > 1 else 0)
        # invert y (SVG origin top-left): higher value → smaller y
        y = pad + (h - 2 * pad) * (1 - (v - vmin) / span)
        coords.append((round(x, 2), round(y, 2)))

    polyline = " ".join(f"{x},{y}" for x, y in coords)
    # Closed polygon for the soft area fill under the line.
    area = (
        f"{coords[0][0]},{h - pad} "
        + polyline
        + f" {coords[-1][0]},{h - pad}"
    )

    if values[-1] > values[0]:
        trend = "up"
    elif values[-1] < values[0]:
        trend = "down"
    else:
        trend = "stable"

    return {
        "polyline": polyline,
        "area": area,
        "last_value": last,
        "min_value": vmin,
        "max_value": vmax,
        "trend": trend,
    }


def load_whiteboard_series(slug: str) -> List[MetricSeries]:
    """Return the per-KPI time series for a dept, built from its Layer-4
    output history. Empty list if no history yet (graceful — the template
    shows an "appears after a few cycles" empty state).
    """
    root = repo_path(slug)
    if root is None:
        return []

    outputs_dir = root / "outputs"
    if not outputs_dir.exists():
        return []

    # Collect (date, kpis) for every YYYY-MM-DD output dir, newest first then
    # trimmed to _MAX_POINTS, then re-sorted oldest→newest for plotting.
    dated: List[Tuple[date, Dict[str, float]]] = []
    for child in outputs_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            d = date.fromisoformat(child.name)
        except ValueError:
            continue  # skips dry-run/, onboarding/, etc.
        kpis = _kpis_for_date(child, dept_slug=slug)
        if kpis:
            dated.append((d, kpis))

    if not dated:
        return []

    dated.sort(key=lambda t: t[0])
    dated = dated[-_MAX_POINTS:]

    # Pivot: {metric_key: [(iso_date, value), ...]}. A KPI present on some
    # days but not others simply has fewer points — we don't forward-fill.
    pivot: Dict[str, List[Tuple[str, float]]] = {}
    for d, kpis in dated:
        for key, val in kpis.items():
            pivot.setdefault(key, []).append((d.isoformat(), val))

    # Cap to keep the page readable. Prefer series that actually MOVE (a
    # flat-line KPI is noise on a dashboard), then fall back to alphabetical
    # for the tie-break so the order is stable across renders.
    if len(pivot) > _MAX_SERIES:
        def _variation(kv: Tuple[str, List[Tuple[str, float]]]) -> float:
            vals = [v for _, v in kv[1]]
            return (max(vals) - min(vals)) if len(vals) > 1 else 0.0
        kept = sorted(pivot.items(), key=lambda kv: (-_variation(kv), kv[0]))
        pivot = dict(kept[:_MAX_SERIES])
        _log.info("whiteboard_series[%s]: capped to %d of %d KPIs",
                  slug, _MAX_SERIES, len(kept))

    series: List[MetricSeries] = []
    for key in sorted(pivot):
        points = pivot[key]
        geo = _build_geometry(points) if len(points) >= 2 else {}
        series.append(MetricSeries(
            key=key,
            label=_humanize_key(key),
            points=points,
            polyline=geo.get("polyline", ""),
            area=geo.get("area", ""),
            last_value=geo.get("last_value", points[-1][1] if points else None),
            min_value=geo.get("min_value"),
            max_value=geo.get("max_value"),
            trend=geo.get("trend", "stable"),
        ))
    return series
