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

# Line-scan fallback for frontmatter blocks that fail a full `yaml.safe_load`
# (board #364 review finding — see _split_frontmatter docstring). Matches the
# same 3 ticker-carrying keys _case_tickers() reads, plus `status`, each
# anchored to start-of-line so a colon inside an unrelated value (e.g. a
# `title:` with an em-dash + colon) can't be mistaken for a key.
_LINE_SCALAR_RE = re.compile(r"^(ticker|status):\s*(.+?)\s*$", re.MULTILINE)
_LINE_LIST_RE = re.compile(r"^(tickers|positions_affected):\s*\[(.*?)\]\s*$", re.MULTILINE)


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
    # NAV% not captured by any cluster (ETF-backbone-only clustering leaves
    # the uncovered-ETF / cash / single-stock (§3a) / crypto (§3b) sleeves
    # out by design — see _summary.md's "Non-cluster NAV breakdown"). None
    # when it couldn't be sourced at all (no _summary.md AND no weights to
    # derive it from) — the template must not show a fabricated 0%.
    uncovered_pct: Optional[float] = None
    uncovered_source: str = ""  # "summary" | "derived" | "" — for tests/debug
    # Deduplicated clustered NAV% — the figure the uncovered_pct ACTUALLY
    # reconciles against (100% = dedup_clustered_pct + uncovered_pct), NOT
    # the raw sum of the individual cluster rows above (those double-count
    # positions that are members of 2+ clusters by design — see
    # _load_dedup_clustered_pct()'s docstring). None when it couldn't be
    # sourced/derived — the template must not show a fabricated figure.
    dedup_clustered_pct: Optional[float] = None
    dedup_clustered_source: str = ""  # "summary" | "derived" | ""

    @property
    def has_clusters(self) -> bool:
        return len(self.clusters) > 0

    @property
    def total_bets(self) -> int:
        return sum(c.bet_count for c in self.clusters)

    @property
    def uncovered_display(self) -> str:
        if self.uncovered_pct is None:
            return "—"
        return f"{self.uncovered_pct:.2f}%"

    @property
    def dedup_clustered_display(self) -> str:
        if self.dedup_clustered_pct is None:
            return "—"
        return f"{self.dedup_clustered_pct:.2f}%"

    @property
    def reconciliation_total_display(self) -> str:
        """dedup_clustered + uncovered, for the footer's "= 100%" line. Only
        meaningful when both figures are available — the template only shows
        this when dedup_clustered_pct is not None (uncovered_pct is already
        gated the same way upstream of it)."""
        if self.dedup_clustered_pct is None or self.uncovered_pct is None:
            return "—"
        return f"{self.dedup_clustered_pct + self.uncovered_pct:.2f}%"


def _regex_fallback_frontmatter(raw_fm: str) -> Dict[str, Any]:
    """Line-scan recovery for a frontmatter block that fails full YAML parse
    (board #364 review finding, 2026-07-24: 7 of 145 live investment-case
    files have an unquoted `:` inside an unrelated value — e.g. `title: INSM
    (Insmed) — L2 underwrite: first-in-class...` — which breaks the WHOLE
    block, so `yaml.safe_load` raising discards a correctly-formatted
    `ticker:`/`tickers:`/`positions_affected:` line right along with it. That
    silently drops a real bet with a valid ticker over a syntax error
    elsewhere in the same file (live cases affected: INSM, LIN, LNG, CSGP,
    VRSK, ADYEN).

    This does NOT attempt to be a YAML parser — it only recovers the
    specific keys this module reads (ticker-carrying fields + status), each
    matched at start-of-line so it can't be fooled by a colon appearing
    inside another key's value. Each matched value is parsed with
    `yaml.safe_load` individually (safe: a single scalar/flow-list, not the
    whole broken block) so quoting/brackets are still honoured; a value that
    itself fails to parse is skipped rather than raising."""
    out: Dict[str, Any] = {}
    for key, val in _LINE_SCALAR_RE.findall(raw_fm):
        if key in out:
            continue
        try:
            parsed = yaml.safe_load(val)
        except yaml.YAMLError:
            continue
        out[key] = parsed
    for key, inner in _LINE_LIST_RE.findall(raw_fm):
        if key in out:
            continue
        try:
            parsed = yaml.safe_load("[" + inner + "]")
        except yaml.YAMLError:
            continue
        if isinstance(parsed, list):
            out[key] = parsed
    return out


def _split_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    """Split a vault memo into (frontmatter_dict, body). Returns ({}, text)
    if there's no valid `---`-delimited frontmatter block (never raises —
    a malformed memo degrades to 'no metadata' rather than a 500).

    When the full YAML parse of the frontmatter block fails, falls back to
    `_regex_fallback_frontmatter()` to recover whichever ticker/status lines
    are still well-formed, instead of discarding the whole block (see that
    function's docstring — board #364 review finding). Behaviour when YAML
    parses cleanly is unchanged."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        _log.warning(
            "risk_clusters: frontmatter parse error, falling back to "
            "line-scan recovery: %s", exc,
        )
        return _regex_fallback_frontmatter(m.group(1)), m.group(2)
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


_UNCOVERED_ROW_RE = re.compile(
    r"\|\s*\*\*Total non-cluster\*\*\s*\|\s*\*\*([\d.]+)%\*\*\s*\|",
)


def _load_uncovered_pct(vault_dir: Path, cluster_rows: List[ClusterRow]) -> tuple[Optional[float], str]:
    """Source the NAV% not captured by any cluster (board #364 review addition
    (b), Joris-approved). `vault/clusters/_summary.md`'s "Non-cluster NAV
    breakdown" table lists the sleeves clustering deliberately excludes
    (Uncovered ETF, crypto §3b, single-stock §3a, cash) and rolls them into
    one bold **Total non-cluster** row, e.g.:

        | Bucket | NAV % | $ |
        |---|---:|---:|
        | Uncovered ETF (no roster) | 28.89% | $66,157 |
        | Crypto sleeve (§3b — excluded by design) | 0.56% | $1,285 |
        | Single-stock sleeve (§3a — excluded by design) | 3.01% | $6,888 |
        | Cash (deployable) | 36.97% | $84,662 |
        | **Total non-cluster** | **69.43%** | |

    That row is read directly when present (it's _summary.md's own
    deduplicated figure — trust it over recomputing). Falls back to
    `100 - sum(cluster weights)` only when _summary.md is missing/unreadable
    or doesn't have that row, since a naive sum-of-cluster-weights double
    counts positions that are members of 2+ clusters by design (same
    "Sum check" note _summary.md prints) and would UNDER-state uncovered NAV.
    Returns (None, "") if neither source is available — the template must
    not render a fabricated 0%."""
    summary_path = vault_dir / "clusters" / "_summary.md"
    if summary_path.exists():
        try:
            text = summary_path.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("risk_clusters: failed reading %s: %s", summary_path, exc)
            text = ""
        m = _UNCOVERED_ROW_RE.search(text)
        if m:
            try:
                return float(m.group(1)), "summary"
            except ValueError:
                pass

    # Fallback: derive from cluster weights. Explicitly approximate (see
    # docstring) — only used when _summary.md can't give us the real figure.
    weighted = [c.nav_weight_pct for c in cluster_rows if c.nav_weight_pct is not None]
    if not weighted:
        return None, ""
    derived = 100.0 - sum(weighted)
    return derived, "derived"


# Matches _summary.md's own "Sum check (deduplicated)" line, e.g.:
#   **Sum check (deduplicated):** clustered 30.57% + non-cluster 69.43% = 99.99% (should be ~100%)
# Captures the "clustered X%" figure — the DEDUPLICATED clustered total,
# distinct from the raw sum of the individual per-cluster NAV% rows (which
# double-counts multi-cluster-membership positions by design; see the
# "Note:" line _summary.md prints directly below this one).
_DEDUP_CLUSTERED_RE = re.compile(
    r"Sum check \(deduplicated\)[^\n]*?clustered\s+([\d.]+)%",
)


def _load_dedup_clustered_pct(
    vault_dir: Path, uncovered_pct: Optional[float],
) -> tuple[Optional[float], str]:
    """Source the DEDUPLICATED clustered NAV% — the figure that actually
    reconciles to 100% against `uncovered_pct`.

    board #364 review finding (2026-07-24): the individual cluster rows in
    the table body sum to ~25.58% RAW on the live vault, because a position
    that's a member of 2+ clusters is intentionally counted in each of those
    clusters' rows (analytical, overlapping view). `_summary.md` itself
    documents this and states the deduplicated figure explicitly in its own
    "Sum check" line, e.g.:

        **Sum check (deduplicated):** clustered 30.57% + non-cluster 69.43% = 99.99% (should be ~100%)

    That "clustered 30.57%" is read directly (same trust-the-source pattern
    as `_load_uncovered_pct`'s "Total non-cluster" row) — it is NOT the sum
    of the visible per-cluster rows, and must never be computed as such here
    (that would just reproduce the ~25.58% double-counted figure the
    reconciliation exists to correct).

    Falls back to `100 - uncovered_pct` only when _summary.md is missing/
    unreadable or doesn't have the "Sum check" line — this is the same
    complement relationship _summary.md's own line asserts, just computed
    from the side we do have. Returns (None, "") if `uncovered_pct` is also
    unavailable — the template must not render a fabricated figure."""
    summary_path = vault_dir / "clusters" / "_summary.md"
    if summary_path.exists():
        try:
            text = summary_path.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("risk_clusters: failed reading %s: %s", summary_path, exc)
            text = ""
        m = _DEDUP_CLUSTERED_RE.search(text)
        if m:
            try:
                return float(m.group(1)), "summary"
            except ValueError:
                pass

    # Fallback: complement of uncovered_pct. Only used when _summary.md
    # can't give us the real figure directly.
    if uncovered_pct is None:
        return None, ""
    return 100.0 - uncovered_pct, "derived"


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

    uncovered_pct, uncovered_source = _load_uncovered_pct(vault_dir, enriched)
    dedup_clustered_pct, dedup_clustered_source = _load_dedup_clustered_pct(vault_dir, uncovered_pct)

    return RiskClusterTable(
        clusters=enriched,
        uncovered_pct=uncovered_pct,
        uncovered_source=uncovered_source,
        dedup_clustered_pct=dedup_clustered_pct,
        dedup_clustered_source=dedup_clustered_source,
    )
