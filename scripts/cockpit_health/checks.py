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
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

CheckResult = Union[Dict[str, Any], List[Dict[str, Any]]]

_COST_ZERO_EPSILON = 0.01          # USD — below this, treat spend as effectively $0
_KANBAN_DRIFT_RATIO = 0.15         # served vs independent REST count
_NAV_STALE_DAYS_THRESHOLD = 2      # canonical NAV as-of older than this = hard
_NAV_WIDE_SCAN_DAYS = 30           # independent on-disk scan window (canonical_nav's own is 7)
_DEPT_STALE_HOURS_THRESHOLD = 36   # raw on-disk writes older than this while claimed "alive" = hard
_TRANSCRIPT_WINDOW_SECONDS = 24 * 60 * 60
_BOARD_REPO = "Bubble-invest/bubble-ops-board"
_BOARD_TOKEN_FILE = Path("/run/bubble-board/token")
_TRANSCRIPT_MIRROR_ROOT = Path("/home/claude/.claude/projects")
_AGENT_HOME_ROOT = Path("/home")


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


def _iso_day(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
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


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_board_token(token_file: Path = _BOARD_TOKEN_FILE) -> Optional[str]:
    """Read the same short-lived token file as the kanban route.

    The value is used only in an in-memory Authorization header and is never
    included in evidence or errors.
    """
    try:
        token = Path(token_file).read_text().strip()
        if token:
            return token
    except OSError:
        pass
    return os.environ.get("GH_TOKEN") or None


def _github_open_issue_count(repo: str, timeout: int = 20,
                             token_file: Path = _BOARD_TOKEN_FILE):
    """Count open board issues through REST, without requiring the `gh` CLI.

    This intentionally reuses only the kanban route's credential source, not
    its cached result. Returns ``(count, error)`` and never raises.
    """
    token = _read_board_token(token_file)
    if not token:
        return None, "no board API token available"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bubble-cockpit-health",
    }
    count = 0
    try:
        for page in range(1, 6):
            url = (f"https://api.github.com/repos/{repo}/issues"
                   f"?state=open&per_page=100&page={page}")
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                batch = json.load(resp)
            if not isinstance(batch, list):
                return None, "board API returned a non-list payload"
            count += sum(1 for item in batch
                         if isinstance(item, dict) and "pull_request" not in item)
            if len(batch) < 100:
                break
    except urllib.error.HTTPError as exc:
        return None, f"board API HTTP {exc.code}: {exc.reason}"
    except Exception as exc:  # noqa: BLE001
        return None, f"board API fetch failed: {exc}"
    return count, None


def _mac_workspace_slug(name: str) -> Optional[str]:
    """Map a mirrored Mac workspace directory to the /costs agent/dept key."""
    lower = name.lower()
    if "bubble-ops-" in lower:
        slug = lower.rsplit("bubble-ops-", 1)[1].split("/", 1)[0]
        return {"content": "content"}.get(slug, slug) or None
    for marker, slug in (
        ("rick-rnd", "rnd"),
        ("tony-ceo", "tony"),
        ("miranda-socials", "content"),
        ("ellie", "ellie"),
        ("ben-fund", "ben"),
        ("maya-sales", "maya"),
        ("eliot-security", "security"),
    ):
        if marker in lower:
            return slug
    return None


def _served_agent_slug(name: str) -> str:
    base = name.split(" (", 1)[0].strip().lower()
    return {"miranda": "content", "rick": "rnd", "eliot": "security"}.get(base, base)


def _recent_transcript_facts(now_epoch: float,
                             agent_home_root: Path = _AGENT_HOME_ROOT,
                             mirror_root: Path = _TRANSCRIPT_MIRROR_ROOT,
                             window_seconds: int = _TRANSCRIPT_WINDOW_SECONDS) -> Dict[str, Any]:
    """Count recent transcript files without parsing usage or importing costs.

    Direct post-isolation homes are preferred. The root-synced ``_vps-*``
    cache is a live-safe fallback for the isolated service uid; Mac mirrors
    are always additive. Only mtimes are read, so this source stays independent
    from cost_tracker's JSON parsing, cache, attribution, and pricing code.
    """
    per_agent: Dict[str, Dict[str, Any]] = {}
    direct_slugs: set[str] = set()

    def scan(slug: Optional[str], root: Path, source: str) -> None:
        if not slug or not root.is_dir():
            return
        newest: Optional[float] = None
        recent = 0
        try:
            files = root.rglob("*.jsonl")
            for path in files:
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                age = now_epoch - mtime
                if age <= window_seconds:
                    recent += 1
                    newest = mtime if newest is None else max(newest, mtime)
        except OSError:
            return
        if recent == 0:
            return
        row = per_agent.setdefault(slug, {
            "recent_transcript_count": 0,
            "newest_mtime": None,
            "sources": [],
        })
        row["recent_transcript_count"] += recent
        row["newest_mtime"] = newest if row["newest_mtime"] is None else max(
            row["newest_mtime"], newest)
        if source not in row["sources"]:
            row["sources"].append(source)

    try:
        homes = list(Path(agent_home_root).glob("agent-*"))
    except OSError:
        homes = []
    for home in homes:
        slug = home.name[len("agent-"):]
        projects = home / ".claude" / "projects"
        if projects.is_dir() and os.access(projects, os.R_OK | os.X_OK):
            direct_slugs.add(slug)
            scan(slug, projects, "agent_home")

    mirror_root = Path(mirror_root)
    try:
        mirrors = list(mirror_root.iterdir()) if mirror_root.is_dir() else []
    except OSError:
        mirrors = []
    for source_root in mirrors:
        if not source_root.is_dir():
            continue
        name = source_root.name
        if name.startswith("_vps-"):
            slug = name[len("_vps-"):]
            # This invariant is deliberately about Claude projects JSONL only.
            # Hermes transcripts use a different schema and are not evidence
            # that the token-priced /costs report must contain a run.
            if not slug.endswith("-hermes") and slug not in direct_slugs:
                scan(slug, source_root, "vps_mirror")
        elif name.startswith("_mac-"):
            try:
                workspaces = list(source_root.iterdir())
            except OSError:
                continue
            for workspace in workspaces:
                if workspace.is_dir():
                    scan(_mac_workspace_slug(workspace.name), workspace, "mac_mirror")

    total = 0
    for row in per_agent.values():
        total += row["recent_transcript_count"]
        newest = row.pop("newest_mtime")
        row["newest_at"] = datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()
        row["newest_age_hours"] = round(max(0.0, now_epoch - newest) / 3600.0, 2)
        row["sources"].sort()
    return {
        "window_hours": round(window_seconds / 3600.0, 2),
        "total_recent_transcripts": total,
        "agents": dict(sorted(per_agent.items())),
    }


# ─────────────────────────────────────────────────────────────────────────
# 1. Costs page — served report vs independent transcript-file activity
# ─────────────────────────────────────────────────────────────────────────

def check_costs_freshness(client, ctx: Ctx) -> Dict[str, Any]:
    """Compare /costs.json with transcript mtimes from outside cost_tracker.

    The source side deliberately does not import cost_tracker, parse usage, use
    its cache, or reuse its attribution/pricing. This catches the exact #1613
    class where that whole reader can consistently report $0 from the wrong
    directory while live session files continue changing elsewhere.
    """
    check_id = "costs_freshness"
    page = "costs"
    if client is None:
        return _skip(check_id, page, "no console client")

    try:
        resp = client.get("/costs.json")
    except Exception as exc:  # noqa: BLE001
        return _error(check_id, page, f"GET /costs.json failed: {exc}")

    try:
        served = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:  # noqa: BLE001
        return _error(check_id, page, f"could not parse /costs.json: {exc}")

    facts = _recent_transcript_facts(ctx.now_epoch())
    served_by_agent: Dict[str, Dict[str, Any]] = {}
    agents = served.get("agents") if isinstance(served, dict) else None
    if isinstance(agents, dict):
        for name, spans in agents.items():
            slug = _served_agent_slug(str(name))
            week = spans.get("week", {}) if isinstance(spans, dict) else {}
            today = spans.get("today", {}) if isinstance(spans, dict) else {}
            row = served_by_agent.setdefault(slug, {
                "week_sessions": 0, "week_cost_usd": 0.0,
                "today_sessions": 0, "today_cost_usd": 0.0,
            })
            row["week_sessions"] += int(_number(week.get("runs"), 0.0))
            row["week_cost_usd"] += _number(week.get("cost"))
            row["today_sessions"] += int(_number(today.get("runs"), 0.0))
            row["today_cost_usd"] += _number(today.get("cost"))

    totals = served.get("totals", {}) if isinstance(served, dict) else {}
    week_totals = totals.get("week", {}) if isinstance(totals, dict) else {}
    today_totals = totals.get("today", {}) if isinstance(totals, dict) else {}
    observed = {
        "http_status": resp.status_code,
        "week_sessions": int(_number(week_totals.get("runs"), 0.0)),
        "week_cost_usd": _number(week_totals.get("cost")),
        "today_sessions": int(_number(today_totals.get("runs"), 0.0)),
        "today_cost_usd": _number(today_totals.get("cost")),
        "agents": dict(sorted(served_by_agent.items())),
    }

    hard = False
    reasons: List[str] = []
    if resp.status_code != 200:
        hard = True
        reasons.append(f"/costs.json returned HTTP {resp.status_code}")

    soft = False
    for slug, source_row in facts["agents"].items():
        served_row = served_by_agent.get(slug, {})
        sessions = int(_number(served_row.get("week_sessions"), 0.0))
        cost = _number(served_row.get("week_cost_usd"))
        count = source_row["recent_transcript_count"]
        if sessions <= 0:
            hard = True
            reasons.append(
                f"{slug}: {count} transcript(s) changed within 24h but "
                f"/costs.json reports 0 sessions over 7d"
            )
        elif cost <= _COST_ZERO_EPSILON:
            soft = True
            reasons.append(
                f"{slug}: {count} transcript(s) changed within 24h and "
                f"{sessions} session(s) are shown, but shown 7d cost is $0"
            )

    return {
        "id": check_id, "page": page,
        "description": "Costs page sessions/cost vs independent 24h transcript mtimes",
        "observed": observed,
        "source": facts,
        "consistent": not hard and not soft,
        "hard_inconsistency": hard,
        "reason": "; ".join(reasons) or (
            "recent transcript activity has corresponding sessions and non-zero cost"
            if facts["total_recent_transcripts"] else
            "no transcript files changed within the 24h source window"
        ),
        "error": None,
    }


# ─────────────────────────────────────────────────────────────────────────
# 2. Kanban page — console's own issue fetch vs an independent REST count
# ─────────────────────────────────────────────────────────────────────────

def check_kanban_open_count(client, ctx: Ctx) -> Dict[str, Any]:
    """`console/routes/kanban.py`'s `_fetch_issues()` has a TTL cache and its
    own board-token read path — exactly the shape that can silently serve a
    stale or empty count. Compares its (private, documented reuse — same
    module the page itself calls) count against an independent REST read. The
    private-function reuse is deliberate: it's the ONLY way to
    observe what the page actually saw, cache included; independence lives
    in the second REST request and uncached count, not the credential."""
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

    fresh_count, fresh_error = _github_open_issue_count(_BOARD_REPO)

    hard = False
    reasons: List[str] = []
    if resp.status_code != 200:
        hard = True
        reasons.append(f"/kanban returned HTTP {resp.status_code}")
    if served_error:
        hard = True
        reasons.append(f"console's own issue fetch errored: {served_error}")
    if fresh_error:
        reasons.append(f"independent REST read errored (not fatal): {fresh_error}")
    elif served_count is not None and fresh_count is not None and fresh_count > 0:
        drift = abs(served_count - fresh_count) / fresh_count
        if drift > _KANBAN_DRIFT_RATIO:
            hard = True
            reasons.append(
                f"console shows {served_count} open cards, independent REST "
                f"read shows {fresh_count} (drift {drift:.0%})"
            )

    return {
        "id": check_id, "page": page,
        "description": "Console's cached open-issue count vs an independent GitHub REST read",
        "observed": {"served_open_count": served_count, "http_status": resp.status_code},
        # Keep the evidence field stable for existing shadow-log consumers;
        # the implementation behind it is now REST rather than the gh binary.
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
                         wide_scan_days: int = _NAV_WIDE_SCAN_DAYS,
                         portfolio_slugs: tuple[str, ...] = ("ben",)) -> List[Dict[str, Any]]:
    """Check each configured fund page through the canonical runtime resolver.

    `canonical_nav()` (console/services/canonical_nav.py, board #1209) is
    already the fleet's ONE audited NAV resolver — this check does NOT
    re-derive NAV, it independently re-scans outputs/<date>/graph-data.json
    over a WIDER window than canonical_nav's own 7-day lookback. The explicit
    page slugs are important: enumerating the legacy management mirror made
    the first live pass silently omit Ben even though canonical_nav correctly
    resolves the cockpit read path under /srv/agents/ben.
    """
    try:
        from console.services.dept_registry import runtime_repo_path
        from console.services.canonical_nav import canonical_nav
    except Exception as exc:  # noqa: BLE001
        return [_error("nav_freshness_ben", "dept_ben_portfolio",
                       f"console import failed: {exc}")]

    out: List[Dict[str, Any]] = []
    for slug in portfolio_slugs:
        page_id = f"dept_{slug}_portfolio"
        root = runtime_repo_path(slug)
        canonical = canonical_nav(slug)
        widest_day = (
            _scan_latest_graph_data_day(root, wide_scan_days)
            if root is not None and root.is_dir() else None
        )

        today = ctx.today_iso()
        hard = False
        reasons: List[str] = []

        if root is None or not root.is_dir():
            hard = True
            reasons.append("canonical runtime repository could not be resolved")
        elif canonical.get("nav") is None and widest_day is not None:
            hard = True
            reasons.append(
                f"canonical_nav() returns no data (7-day window) but "
                f"outputs/{widest_day}/graph-data.json exists on disk — an "
                f"outage older than 7 days looks like an empty state, not a "
                f"stale one"
            )
        elif canonical.get("nav") is None:
            hard = True
            reasons.append("canonical NAV and graph-data.json are both unavailable")
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
                       "runtime_repo_path": str(root) if root is not None else None,
                       "scan_window_days": wide_scan_days},
            "consistent": not hard,
            "hard_inconsistency": hard,
            "reason": "; ".join(reasons) or "canonical NAV looks fresh and pages render",
            "error": None,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────
# 4. Fund exposure — rendered exposure date vs canonical NAV snapshot date
# ─────────────────────────────────────────────────────────────────────────

def check_exposure_nav_date(client, ctx: Ctx,
                            portfolio_slugs: tuple[str, ...] = ("ben",)) -> List[Dict[str, Any]]:
    """Prove that the exposure payload and NAV snapshot describe one date.

    ``build_thesis_data`` is the exact data object rendered by the portfolio
    page; its ``generated_at`` stamps the exposure view. ``canonical_nav`` is
    the cockpit's audited NAV resolver. No figures are recomputed here.
    """
    try:
        from console.services.canonical_nav import canonical_nav
        from console.services.thesis_book import build_thesis_data
    except Exception as exc:  # noqa: BLE001
        return [_error("exposure_nav_date_ben", "dept_ben_portfolio",
                       f"console import failed: {exc}")]

    out: List[Dict[str, Any]] = []
    for slug in portfolio_slugs:
        page_id = f"dept_{slug}_portfolio"
        try:
            canonical = canonical_nav(slug)
            exposure = build_thesis_data(slug)
        except Exception as exc:  # noqa: BLE001
            out.append(_error(f"exposure_nav_date_{slug}", page_id,
                              f"portfolio resolver failed: {exc}"))
            continue

        generated_at = exposure.get("generated_at") if isinstance(exposure, dict) else None
        exposure_day = _iso_day(generated_at)
        nav_day = _iso_day(canonical.get("as_of") if isinstance(canonical, dict) else None)
        comparable = exposure_day is not None and nav_day is not None
        matches = comparable and exposure_day == nav_day
        hard = comparable and not matches
        error = None
        if not comparable:
            missing = []
            if exposure_day is None:
                missing.append("exposure generated_at")
            if nav_day is None:
                missing.append("canonical NAV as_of")
            reason = "cannot compare dates: missing " + " and ".join(missing)
            error = reason
            consistent: Optional[bool] = None
        elif matches:
            reason = f"exposure and canonical NAV are both dated {nav_day}"
            consistent = True
        else:
            reason = (
                f"exposure is dated {exposure_day} while latest canonical NAV "
                f"snapshot is dated {nav_day}"
            )
            consistent = False

        out.append({
            "id": f"exposure_nav_date_{slug}", "page": page_id,
            "description": f"{slug}: exposure as-of date vs latest canonical NAV snapshot date",
            "observed": {"exposure_generated_at": generated_at,
                         "exposure_as_of": exposure_day},
            "source": {"latest_nav_snapshot_date": nav_day,
                       "nav_source": canonical.get("source") if isinstance(canonical, dict) else None},
            "consistent": consistent,
            "hard_inconsistency": hard,
            "reason": reason,
            "error": error,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────
# 5. Carnet de bord (/health) — raw on-disk write age vs the page's alive claim
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
    check_exposure_nav_date,
    check_dept_heartbeat_age,
]
