"""checks.py — pluggable per-page EVIDENCE checks for cockpit_health.

Every check independently re-derives a fact — never trusting the console's
own computation as its only source — and compares it to what the rendered
page (or the console's own served JSON) shows. A check returns one (or a list
of) structured evidence dict(s), never a verdict:

    {
      "id": "...",                 # stable check id
      "page": "...",               # which PAGES entry (collect.py) this attaches to
      "description": "...",
      "observed": {...},           # what the console/page showed
      "source": {...},             # the independently-derived fact
      "consistent": bool|None,     # None only on a genuine skip/error
      "hard_inconsistency": bool,  # judge.py's asymmetric rule: True here
                                    # forces the page 'suspicious' REGARDLESS
                                    # of what Jev says — Jev may only escalate,
                                    # never downgrade a hard inconsistency.
      "reason": "...",             # human-readable, for the board card / log
      "error": str|None,
    }

Every function's signature is `(client, ctx) -> dict | list[dict]` so
collect.py's `run_checks` can dispatch uniformly. `client` may be None (no
console app available — see console_client.py); a check must degrade
gracefully, never raise (collect.py also wraps each call defensively, but a
check should not rely on that as its only guard).
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

CheckResult = Union[Dict[str, Any], List[Dict[str, Any]]]

_COST_ZERO_EPSILON = 0.01          # USD — below this, treat "today" spend as effectively $0
_COST_DRIFT_RATIO = 0.25           # served vs fresh recompute — drift beyond this is "hard"
_KANBAN_DRIFT_RATIO = 0.15         # served vs independent `gh` count
_NAV_STALE_DAYS_THRESHOLD = 2      # canonical NAV as-of older than this = hard
_NAV_WIDE_SCAN_DAYS = 30           # independent on-disk scan window (canonical_nav's own is 7)
_DEPT_STALE_HOURS_THRESHOLD = 36   # raw on-disk writes older than this while claimed "alive" = hard
_BOARD_REPO = "Bubble-invest/bubble-ops-board"


class Ctx:
    """Injectable clock + small helpers, so every check is unit-testable
    without real wall-clock time or real subprocess calls."""

    def __init__(self, now_fn=None):
        self._now_fn = now_fn or time.time

    def now_epoch(self) -> float:
        return self._now_fn()

    def today_iso(self) -> str:
        return datetime.fromtimestamp(self._now_fn(), tz=timezone.utc).date().isoformat()


def _skip(check_id: str, page: str, reason: str) -> Dict[str, Any]:
    return {
        "id": check_id, "page": page, "description": "",
        "observed": {}, "source": {}, "consistent": None,
        "hard_inconsistency": False, "reason": reason, "error": None,
    }


def _error(check_id: str, page: str, message: str) -> Dict[str, Any]:
    return {
        "id": check_id, "page": page, "description": "",
        "observed": {}, "source": {}, "consistent": None,
        "hard_inconsistency": False, "reason": message, "error": message,
    }


def _days_between(as_of_iso: Optional[str], today_iso: str) -> Optional[int]:
    if not as_of_iso:
        return None
    try:
        return (date.fromisoformat(today_iso) - date.fromisoformat(as_of_iso[:10])).days
    except (ValueError, TypeError):
        return None


def _scan_latest_graph_data_day(root: Path, max_days: int) -> Optional[str]:
    """Independent filesystem scan for the newest outputs/<date>/graph-data.json
    on disk, over a WIDER window than canonical_nav()'s own 7-day lookback —
    so an outage older than 7 days is caught as 'stale data exists but is
    being silently swallowed as an empty state', not invisible."""
    today = date.today()
    for offset in range(max_days):
        day = (today - timedelta(days=offset)).isoformat()
        if (root / "outputs" / day / "graph-data.json").exists():
            return day
    return None


def _newest_mtime(root: Path, patterns: List[str]) -> Optional[float]:
    newest = None
    for pattern in patterns:
        for p in root.glob(pattern):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    return newest


def _sum_today_cost(report: Any) -> float:
    if not isinstance(report, dict):
        return 0.0
    try:
        return float(report.get("totals", {}).get("today", {}).get("cost", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _gh_open_issue_count(repo: str, timeout: int = 20, gh_bin: str = "gh"):
    """Independent `gh issue list` count. Returns (count, error) — never
    raises. Never accepts/echoes a token: `gh` reads its own auth from the
    environment (GH_TOKEN/GITHUB_TOKEN/`gh auth`), untouched here."""
    try:
        out = subprocess.run(
            [gh_bin, "issue", "list", "--repo", repo, "--state", "open",
             "--limit", "500", "--json", "number"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)
    if out.returncode != 0:
        return None, (out.stderr or "gh issue list failed").strip()[:300]
    try:
        return len(json.loads(out.stdout or "[]")), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not parse gh output: {exc}"


# ─────────────────────────────────────────────────────────────────────────
# 1. Costs page — served /costs.json vs a FRESH cost_tracker recompute
# ─────────────────────────────────────────────────────────────────────────

def check_costs_freshness(client, ctx: Ctx) -> Dict[str, Any]:
    """Catches the stale-cache / 'serves $0 while real spend happened' bug
    class — the same failure shape as #1207's NAV-showed-$0 bug: a page
    trusting a stale or wrong-source read instead of the true current state."""
    check_id = "costs_freshness"
    page = "costs"
    if client is None:
        return _skip(check_id, page, "no console client")

    try:
        resp = client.get("/costs.json")
    except Exception as exc:  # noqa: BLE001
        return _error(check_id, page, f"GET /costs.json failed: {exc}")

    served_ok = resp.status_code == 200
    served = resp.json() if served_ok else {}

    try:
        from console.services import cost_tracker
        fresh = cost_tracker.build_report(refresh=True)
    except Exception as exc:  # noqa: BLE001
        return _error(check_id, page, f"cost_tracker.build_report failed: {exc}")

    served_total = _sum_today_cost(served)
    fresh_total = _sum_today_cost(fresh)

    hard = False
    reasons: List[str] = []
    if not served_ok:
        hard = True
        reasons.append(f"/costs.json returned HTTP {resp.status_code}")
    elif fresh_total > _COST_ZERO_EPSILON and served_total <= _COST_ZERO_EPSILON:
        hard = True
        reasons.append(
            f"served page shows ~$0 today while a fresh recompute finds "
            f"${fresh_total:.2f} — matches the stale-serve bug class"
        )
    elif fresh_total > 0:
        drift = abs(fresh_total - served_total) / fresh_total
        if drift > _COST_DRIFT_RATIO:
            hard = True
            reasons.append(f"served total drifts {drift:.0%} from a fresh recompute")

    return {
        "id": check_id, "page": page,
        "description": "Costs page served total vs a fresh cost_tracker recompute",
        "observed": {"served_today_usd": served_total, "http_status": resp.status_code},
        "source": {"fresh_today_usd": fresh_total},
        "consistent": not hard,
        "hard_inconsistency": hard,
        "reason": "; ".join(reasons) or "served total matches a fresh recompute",
        "error": None,
    }


# ─────────────────────────────────────────────────────────────────────────
# 2. Kanban page — console's own issue fetch vs an independent `gh` count
# ─────────────────────────────────────────────────────────────────────────

def check_kanban_open_count(client, ctx: Ctx) -> Dict[str, Any]:
    """`console/routes/kanban.py`'s `_fetch_issues()` has a TTL cache and its
    own board-token read path — exactly the shape that can silently serve a
    stale or empty count. Compares its (private, documented reuse — same
    module the page itself calls) count against an independent `gh issue
    list`. The private-function reuse is deliberate: it's the ONLY way to
    observe what the page actually saw, cache included; independence lives
    in the `gh` half of the comparison, not the fetch mechanism."""
    check_id = "kanban_open_count"
    page = "kanban"
    if client is None:
        return _skip(check_id, page, "no console client")

    served_count: Optional[int] = None
    served_error: Optional[str] = None
    try:
        from console.routes.kanban import _fetch_issues
        issues, err = _fetch_issues()
        served_error = err
        served_count = len(issues) if err is None else None
    except Exception as exc:  # noqa: BLE001
        served_error = str(exc)

    try:
        resp = client.get("/kanban")
    except Exception as exc:  # noqa: BLE001
        return _error(check_id, page, f"GET /kanban failed: {exc}")

    fresh_count, fresh_error = _gh_open_issue_count(_BOARD_REPO)

    hard = False
    reasons: List[str] = []
    if resp.status_code != 200:
        hard = True
        reasons.append(f"/kanban returned HTTP {resp.status_code}")
    if served_error:
        hard = True
        reasons.append(f"console's own issue fetch errored: {served_error}")
    if fresh_error:
        reasons.append(f"independent gh read errored (not fatal): {fresh_error}")
    elif served_count is not None and fresh_count is not None and fresh_count > 0:
        drift = abs(served_count - fresh_count) / fresh_count
        if drift > _KANBAN_DRIFT_RATIO:
            hard = True
            reasons.append(
                f"console shows {served_count} open cards, independent gh "
                f"read shows {fresh_count} (drift {drift:.0%})"
            )

    return {
        "id": check_id, "page": page,
        "description": "Console's own open-issue count vs an independent `gh issue list`",
        "observed": {"served_open_count": served_count, "http_status": resp.status_code},
        "source": {"gh_open_count": fresh_count},
        "consistent": not hard,
        "hard_inconsistency": hard,
        "reason": "; ".join(reasons) or "counts agree within tolerance",
        "error": None,
    }


# ─────────────────────────────────────────────────────────────────────────
# 3. Fund-shaped dept portfolio pages — canonical NAV freshness
# ─────────────────────────────────────────────────────────────────────────

def check_nav_freshness(client, ctx: Ctx,
                         stale_days_threshold: int = _NAV_STALE_DAYS_THRESHOLD,
                         wide_scan_days: int = _NAV_WIDE_SCAN_DAYS) -> List[Dict[str, Any]]:
    """Generalized over every live dept that has ever pushed a
    graph-data.json — today that's only Ben (the fund dept; 'Ben exposure
    page stale again' is the bug this exists for), kept dept-agnostic so a
    future fund-shaped dept is covered for free (CO-BUILDER doctrine:
    generalize a one-off fix into shared scaffolding rather than hardcode it).

    `canonical_nav()` (console/services/canonical_nav.py, board #1209) is
    already the fleet's ONE audited NAV resolver — this check does NOT
    re-derive NAV, it independently re-scans outputs/<date>/graph-data.json
    over a WIDER window than canonical_nav's own 7-day lookback, and
    cross-checks the rendered dept + portfolio pages actually render."""
    try:
        from console.services.dept_registry import live_departments, runtime_repo_path
        from console.services.canonical_nav import canonical_nav
    except Exception as exc:  # noqa: BLE001
        return [_error("nav_freshness", "unknown", f"console import failed: {exc}")]

    out: List[Dict[str, Any]] = []
    for d in live_departments():
        slug = d.slug
        root = runtime_repo_path(slug)
        if root is None or not root.is_dir():
            continue

        widest_day = _scan_latest_graph_data_day(root, wide_scan_days)
        canonical = canonical_nav(slug)
        if canonical.get("nav") is None and widest_day is None:
            continue  # not a fund-shaped dept — nothing to check

        page_id = f"dept_{slug}_portfolio"
        today = ctx.today_iso()
        hard = False
        reasons: List[str] = []

        if canonical.get("nav") is None and widest_day is not None:
            hard = True
            reasons.append(
                f"canonical_nav() returns no data (7-day window) but "
                f"outputs/{widest_day}/graph-data.json exists on disk — an "
                f"outage older than 7 days looks like an empty state, not a "
                f"stale one"
            )
        elif canonical.get("as_of"):
            age_days = _days_between(canonical["as_of"], today)
            if age_days is not None and age_days > stale_days_threshold:
                hard = True
                reasons.append(
                    f"canonical NAV as-of {canonical['as_of']} is {age_days}d "
                    f"old (> {stale_days_threshold}d threshold)"
                )

        if client is not None:
            for path, label in ((f"/dept/{slug}", "dept_page"),
                                 (f"/dept/{slug}/portfolio", "portfolio_page")):
                try:
                    resp = client.get(path)
                except Exception as exc:  # noqa: BLE001
                    reasons.append(f"{label} GET failed: {exc}")
                    continue
                if resp.status_code != 200:
                    hard = True
                    reasons.append(f"{label} returned HTTP {resp.status_code}")

        out.append({
            "id": f"nav_freshness_{slug}", "page": page_id,
            "description": f"{slug}: canonical NAV freshness vs a wide on-disk scan + rendered pages",
            "observed": {"canonical_nav": canonical},
            "source": {"latest_graph_data_day_on_disk": widest_day,
                       "scan_window_days": wide_scan_days},
            "consistent": not hard,
            "hard_inconsistency": hard,
            "reason": "; ".join(reasons) or "canonical NAV looks fresh and pages render",
            "error": None,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────
# 4. Carnet de bord (/health) — raw on-disk write age vs the page's alive claim
# ─────────────────────────────────────────────────────────────────────────

def check_dept_heartbeat_age(client, ctx: Ctx,
                              stale_hours_threshold: int = _DEPT_STALE_HOURS_THRESHOLD) -> Dict[str, Any]:
    """`/health/graph.json`'s per-dept `pulse.alive` comes from
    `morty_reader.loop_pulse()` (heartbeat.log + per-layer .last-run). This
    check does a DUMBER, independent raw filesystem scan of the same files —
    not loop_pulse's own logic — so a bug IN loop_pulse (a timezone error, a
    wrong glob, a stale in-process cache) can't hide behind its own claim."""
    check_id = "dept_heartbeat_age"
    page = "health"
    try:
        from console.services.dept_registry import live_departments, repo_path
    except Exception as exc:  # noqa: BLE001
        return _error(check_id, page, f"console import failed: {exc}")

    graph = None
    if client is not None:
        try:
            resp = client.get("/health/graph.json")
            if resp.status_code == 200:
                graph = resp.json()
            else:
                return _error(check_id, page, f"GET /health/graph.json returned HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            return _error(check_id, page, f"GET /health/graph.json failed: {exc}")

    nodes_by_id: Dict[str, Any] = {}
    if isinstance(graph, dict):
        for n in graph.get("nodes", []) or []:
            if isinstance(n, dict) and isinstance(n.get("id"), str):
                nodes_by_id[n["id"]] = n

    now = ctx.now_epoch()
    hard = False
    findings: List[str] = []
    checked = 0
    for d in live_departments():
        root = repo_path(d.slug)
        if root is None or not root.is_dir():
            continue
        mtime = _newest_mtime(root, ["outputs/*/heartbeat.log", "outputs/*/*/.last-run"])
        if mtime is None:
            continue  # never run — onboarding-state territory, not this check's job
        checked += 1
        age_hours = (now - mtime) / 3600.0
        node = nodes_by_id.get(f"dept:{d.slug}")
        claimed_alive = bool(
            node and isinstance(node.get("pulse"), dict) and node["pulse"].get("alive")
        )
        if age_hours > stale_hours_threshold and claimed_alive:
            hard = True
            findings.append(
                f"{d.slug}: raw on-disk writes are {age_hours:.0f}h old "
                f"(> {stale_hours_threshold}h) but /health/graph.json still "
                f"shows it alive"
            )

    return {
        "id": check_id, "page": page,
        "description": "Per-dept on-disk write age vs the /health page's alive/dead claim",
        "observed": {"nodes_in_graph": len(nodes_by_id)},
        "source": {"depts_checked": checked, "findings": findings},
        "consistent": not hard,
        "hard_inconsistency": hard,
        "reason": "; ".join(findings) or "all live depts' on-disk activity matches the health page",
        "error": None,
    }


CHECKS = [
    check_costs_freshness,
    check_kanban_open_count,
    check_nav_freshness,
    check_dept_heartbeat_age,
]
