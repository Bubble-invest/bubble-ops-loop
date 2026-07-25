"""
risk_clusters.py — cluster rollup + bet drill-down for the dept page (board #364, PR-B).

Ben's `tools/cluster_analysis.py` runs weekly and writes one memo per cluster
to `vault/clusters/<cluster_id>.md` (frontmatter: `cluster_id`, `members`
(ticker list), `nav_weight_pct`, `mv_usd` — see cluster_analysis.py's
ClusterRun dataclass) plus a cross-cluster `vault/clusters/_summary.md`. This
module reads those memos directly (same repo_path(slug) access pattern as
nav_history.py / whiteboard_series.py — read-only, no recompute) and rolls
each cluster up with the individual bets (theses) that back its member
tickers, sourced from `vault/investment-cases/*.md`.

WHY A CUSTOM PARSER, NOT A STRICT SCHEMA (research on #364, Rick 2026-07-24):
  investment-cases/*.md frontmatter is NOT uniform. Reading 30+ live files in
  the ben repo found THREE different ticker-carrying fields in active use,
  depending on when/how the case was authored:
    - `ticker: XXX`            (singular string) — single-name idea files,
                                  e.g. `005380.KS.md`, `*-idea-*.md`.
    - `tickers: [A, B, C]`      (list) — most dated investment-case memos.
    - `positions_affected: [A, B]` (list) — a third convention used by a
                                  chunk of the 06-16..06-19 dated memos.
  A parser that only reads one of these silently drops a third to a half of
  the real theses. `_case_tickers()` below checks all three and unions them.

  `status`/`kind`/`type` are similarly inconsistent (`status: active` vs
  `kind: investment_case` vs `type: investment-case` vs `type:
  investment-case-capsule` vs `kind: process_note`). We do NOT filter cases
  by status/kind here — Joris's spec is "bets/theses… one per active thesis"
  but the on-disk reality mixes closed/active/process-note files under one
  status vocabulary that isn't stable enough to hard-filter without silently
  hiding real theses (a `status: closed` case can still be the only memo for
  a still-held ticker). We surface `status` as a displayed field (best-effort
  from whichever key is present) instead of using it as a filter — the
  operator decides what's stale, the table doesn't hide it.

  `conviction` also drifts format: `2/5`, `3` (bare int meaning /5 by
  convention), or absent. Rendered as-is (string), no forced numeric parse.

  The thesis one-liner is the case's H1 heading (first `# ...` line after
  frontmatter) — every sampled file has one and it's already written as a
  human summary line (e.g. "Hyundai Motor — cyclical auto OEM with a Boston
  Dynamics call option (WATCH)"), better than re-deriving a snippet from body
  prose.

Clusters are matched to bets by exact ticker string against the cluster's
`members` list — the SAME identity `cluster_analysis.py` itself uses (no
ISIN/name fuzzy-matching; a case that doesn't carry a member ticker in one of
the three known fields simply won't show up under that cluster, same as
cluster_analysis.py's own weights lookup would miss it).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from console.services.dept_registry import repo_path

_log = logging.getLogger(__name__)

# Cluster memo files to skip — not per-cluster data.
_CLUSTER_SKIP_FILES = {"_summary.md", "_config.yaml"}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Bet:
    """One investment-case thesis, as it will be rendered in a cluster row's
    drill-down."""
    ticker: str
    thesis: str            # H1 heading — the case's own one-line summary
    status: str = ""
    conviction: str = ""
    sleeve: str = ""
    source_file: str = ""  # repo-relative path, for the "how we parsed this" trail


@dataclass(frozen=True)
class ClusterRow:
    """One cluster rollup row, with its member bets attached."""
    cluster_id: str
    name: str
    nav_weight_pct: Optional[float]
    mv_usd: Optional[float]
    members: List[str] = field(default_factory=list)
    bets: List[Bet] = field(default_factory=list)

    @property
    def bet_count(self) -> int:
        return len(self.bets)

    @property
    def weight_display(self) -> str:
        if self.nav_weight_pct is None:
            return "—"
        return f"{self.nav_weight_pct:.2f}%"

    @property
    def mv_display(self) -> str:
        if self.mv_usd is None:
            return "—"
        return f"${self.mv_usd:,.0f}"

    @property
    def unmatched_members(self) -> List[str]:
        """Member tickers with no matching investment-case file — surfaced so
        the empty state inside a cluster row is explicit, not a silent gap."""
        matched = {b.ticker for b in self.bets}
        return [m for m in self.members if m not in matched]


@dataclass(frozen=True)
class RiskClusterTable:
    clusters: List[ClusterRow] = field(default_factory=list)

    @property
    def has_clusters(self) -> bool:
        return len(self.clusters) > 0

    @property
    def total_bets(self) -> int:
        return sum(c.bet_count for c in self.clusters)


def _split_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    """Split a vault memo into (frontmatter_dict, body). Returns ({}, text)
    if there's no valid `---`-delimited frontmatter block (never raises —
    a malformed memo degrades to 'no metadata' rather than a 500)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        _log.warning("risk_clusters: frontmatter parse error: %s", exc)
        return {}, m.group(2)
    if not isinstance(fm, dict):
        fm = {}
    return fm, m.group(2)


def _first_h1(body: str) -> str:
    m = _H1_RE.search(body)
    return m.group(1).strip() if m else ""


def _as_str_list(val: Any) -> List[str]:
    """Coerce a YAML value into a list of plain strings. Handles the single
    string case (`ticker: XXX`), the list case (`tickers: [A, B]`), and
    ignores non-list/non-str junk rather than raising."""
    if val is None:
        return []
    if isinstance(val, str):
        v = val.strip()
        return [v] if v else []
    if isinstance(val, list):
        return [str(x).strip() for x in val if x is not None and str(x).strip()]
    return []


def _case_tickers(fm: Dict[str, Any]) -> List[str]:
    """Union of every ticker-carrying frontmatter field seen across the live
    vault (see module docstring): `ticker`, `tickers`, `positions_affected`.
    Order-preserving de-dupe (a case that lists the same ticker under two
    keys — seen in practice — shouldn't render twice)."""
    out: List[str] = []
    seen = set()
    for key in ("ticker", "tickers", "positions_affected"):
        for t in _as_str_list(fm.get(key)):
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _load_cases(vault_dir: Path) -> Dict[str, List[Bet]]:
    """Read every vault/investment-cases/*.md and index Bets by ticker.
    Returns {ticker: [Bet, ...]} — a ticker can have >1 case file (revisit
    memos), all are kept so the drill-down shows the full thesis history,
    not just the most recent."""
    cases_dir = vault_dir / "investment-cases"
    by_ticker: Dict[str, List[Bet]] = {}
    if not cases_dir.exists():
        return by_ticker

    for p in sorted(cases_dir.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("risk_clusters: failed reading %s: %s", p, exc)
            continue
        fm, body = _split_frontmatter(text)
        tickers = _case_tickers(fm)
        if not tickers:
            continue
        thesis = fm.get("title") or _first_h1(body) or p.stem
        status = str(fm.get("status") or fm.get("kind") or "")
        conviction = fm.get("conviction")
        conviction_str = "" if conviction is None else str(conviction)
        sleeve = str(fm.get("sleeve") or "")
        rel = f"investment-cases/{p.name}"
        for t in tickers:
            by_ticker.setdefault(t, []).append(Bet(
                ticker=t,
                thesis=str(thesis),
                status=status,
                conviction=conviction_str,
                sleeve=sleeve,
                source_file=rel,
            ))
    return by_ticker


def _load_clusters(vault_dir: Path) -> List[ClusterRow]:
    """Read every vault/clusters/<id>.md memo (skip _summary.md/_config.yaml)
    and build the base cluster rows (no bets attached yet)."""
    clusters_dir = vault_dir / "clusters"
    rows: List[ClusterRow] = []
    if not clusters_dir.exists():
        return rows

    for p in sorted(clusters_dir.glob("*.md")):
        if p.name in _CLUSTER_SKIP_FILES or p.name.startswith("."):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("risk_clusters: failed reading %s: %s", p, exc)
            continue
        fm, body = _split_frontmatter(text)
        cluster_id = str(fm.get("cluster_id") or p.stem)
        name = str(fm.get("title") or _first_h1(body) or cluster_id)
        # Title in memos is often "Cluster — <Name>" — strip that prefix for
        # a cleaner table cell; fall back to the raw title if it doesn't
        # match (never guess past what's actually there).
        name = re.sub(r"^Cluster\s*[—-]\s*", "", name).strip() or cluster_id
        members = _as_str_list(fm.get("members"))
        nav_weight = fm.get("nav_weight_pct")
        mv_usd = fm.get("mv_usd")
        try:
            nav_weight = float(nav_weight) if nav_weight is not None else None
        except (TypeError, ValueError):
            nav_weight = None
        try:
            mv_usd = float(mv_usd) if mv_usd is not None else None
        except (TypeError, ValueError):
            mv_usd = None
        rows.append(ClusterRow(
            cluster_id=cluster_id,
            name=name,
            nav_weight_pct=nav_weight,
            mv_usd=mv_usd,
            members=members,
        ))
    return rows


def load_risk_clusters(slug: str) -> RiskClusterTable:
    """Return the cluster-rollup + bet-drill-down table for a dept. Empty
    (graceful) for any dept without a vault/clusters/ dir — only Ben has
    cluster_analysis.py today."""
    root = repo_path(slug)
    if root is None:
        return RiskClusterTable()

    vault_dir = root / "vault"
    if not vault_dir.exists():
        return RiskClusterTable()

    cluster_rows = _load_clusters(vault_dir)
    if not cluster_rows:
        return RiskClusterTable()

    cases_by_ticker = _load_cases(vault_dir)

    enriched: List[ClusterRow] = []
    for row in cluster_rows:
        bets: List[Bet] = []
        for member in row.members:
            bets.extend(cases_by_ticker.get(member, []))
        enriched.append(ClusterRow(
            cluster_id=row.cluster_id,
            name=row.name,
            nav_weight_pct=row.nav_weight_pct,
            mv_usd=row.mv_usd,
            members=row.members,
            bets=bets,
        ))

    # Sort by weight (largest first) — mirrors _summary.md's own table order
    # (concentration risk first). Rows with no weight (parse failure) sink
    # to the bottom rather than sorting arbitrarily.
    enriched.sort(key=lambda c: (c.nav_weight_pct is None, -(c.nav_weight_pct or 0)))

    return RiskClusterTable(clusters=enriched)
