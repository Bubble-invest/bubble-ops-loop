#!/usr/bin/env python3
"""cost_tracker.py — per-agent / per-job token & cost scanner for the VPS console.

Ported from the local Tailscale dashboard's `token_usage.py` (Tony_CEO/workspace/
org-dashboard/lib), adapted for the VPS:
  - isolated VPS sessions are mirrored into
    /home/claude/.claude/projects/_vps-<slug>/<mangled-workdir>/
  - legacy shared-uid sessions remain under
    /home/claude/.claude/projects/-home-claude-agents-<...>
  - the wiki-compile + loop-backup floor cron run as `claude -p` under -home-claude
  - Mac caches are rsync'd in (Rick and Tonio live on the Mac)

Reads Claude assistant usage and root-exported Hermes SQLite usage, using the
existing pricing table. Claude days are message-time Europe/Paris days;
Hermes cumulative session counters are attributed to their start day with a note.
Usage records are cached by file fingerprint and the report for 45 seconds.

Usage:
    python3 cost_tracker.py --refresh
    python3 cost_tracker.py --fleet-summary --day YYYY-MM-DD --out PATH

"""
from __future__ import annotations

import argparse
import hashlib
from contextlib import closing
from itertools import chain
import json
import logging
import math
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

FLEET_TIMEZONE = "Europe/Paris"
PARIS = ZoneInfo(FLEET_TIMEZONE)
TOKEN_CLASSES = ("input", "output", "cache_read", "cache_create", "reasoning")

_log = logging.getLogger(__name__)

HOME = Path(os.environ.get("HOME", "/home/claude"))
# Production sets this explicitly because the cockpit runs as `bubble-console`:
# its own $HOME has no Claude sessions. Root's transcript-sync timer mirrors
# isolated `/home/agent-<slug>/.claude/projects` trees into this shared,
# read-only source. The HOME-relative fallback keeps local development and the
# existing hermetic tests unchanged.
PROJECTS_DIR = Path(os.environ.get(
    "BUBBLE_COST_PROJECTS_DIR", str(HOME / ".claude" / "projects")
))
CACHE_DIR = Path(os.environ.get("BUBBLE_COST_CACHE_DIR", str(HOME / ".claude" / "cache")))
CACHE_FILE = CACHE_DIR / "console-cost-sessions.sqlite3"


# ── Pricing (USD per 1M tokens). Current public list prices; override via
# BUBBLE_COST_PRICING_JSON (a JSON file path) if they change. Cache-read is
# 10% of input; cache-creation 125% of input (5m TTL). Keep it simple +
# clearly-labelled "estimate" in the UI — for trend/relative use, not billing.
# Keys are model-name substrings; none may be a substring of another (the
# lookup in _price_for_model returns the first key that matches).
_DEFAULT_PRICING = {
    # model-substring : {input, output, cache_read, cache_write} per 1M tokens
    "fable":  {"input": 10.0, "output": 50.0, "cache_read": 1.00, "cache_write": 12.50},
    "opus":   {"input": 5.0,  "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "sonnet": {"input": 3.0,  "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "haiku":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.10, "cache_write": 1.25},
}


def _load_pricing() -> dict:
    override = os.environ.get("BUBBLE_COST_PRICING_JSON")
    if override and Path(override).is_file():
        try:
            return json.loads(Path(override).read_text())
        except Exception:
            pass
    return _DEFAULT_PRICING


def _price_for_model(model: str, pricing: dict) -> dict:
    m = (model or "").lower()
    for key, rates in pricing.items():
        if key in m:
            return rates
    # unknown / non-Anthropic model (e.g. deepseek) → zero-cost so it's never
    # silently over-billed at some Anthropic rate. Better to under-count an
    # unpriced model than to attribute phantom Anthropic dollars to it.
    return {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


def _cost_split(model_usage: dict, pricing: dict, *, rounded: bool = True) -> dict:
    """Split cost into {real, cache}. real = input+output (the neutralized
    'real-equivalent API cost' — what the work would cost without prompt caching);
    cache = cache_read + cache_write (shown SEPARATELY on /costs, board #358).
    Cache-read is ~98% of token VOLUME (the loop re-reading context each turn) and
    is noise for a real-cost/budget read — so we surface the non-cache figure as the
    headline and keep cache discrete."""
    real = 0.0
    cache = 0.0
    for model, u in model_usage.items():
        r = _price_for_model(model, pricing)
        real += (u.get("input", 0) * r["input"] + u.get("output", 0) * r["output"]) / 1_000_000.0
        cache += (u.get("cache_read", 0) * r["cache_read"]
                  + u.get("cache_create", 0) * r["cache_write"]) / 1_000_000.0
    return {"real": round(real, 4), "cache": round(cache, 4)} if rounded else {"real": real, "cache": cache}


def _cost_of(model_usage: dict, pricing: dict) -> float:
    """model_usage = {model: {input, output, cache_read, cache_create}}."""
    total = 0.0
    for model, u in model_usage.items():
        r = _price_for_model(model, pricing)
        total += (
            u.get("input", 0) * r["input"]
            + u.get("output", 0) * r["output"]
            + u.get("cache_read", 0) * r["cache_read"]
            + u.get("cache_create", 0) * r["cache_write"]
        ) / 1_000_000.0
    return round(total, 4)


# ── Agent attribution: map a project-dir name → a friendly agent/job label.
# VPS-live agents live in -home-claude-agents-bubble-ops-<slug> (or
# -home-claude-agents-<name> for concierges). The -home-claude dir holds the
# `claude -p` cron sessions (wiki-compile, loop-backup floor) — attributed by
# job below. Mac caches (_mac-{{OPERATOR_USER}}/_mac-{{OPERATOR_2_USER}}) hold Rick + Tonio.
def classify(dir_name: str) -> Optional[str]:
    """Map a project-dir name (top-level, OR a Mac-cache 'cache/workspace' pair
    joined by '/') → a friendly agent/job label. VPS-live agents live in
    -home-claude-agents-bubble-ops-<slug>. The -home-claude dir holds the
    `claude -p` cron sessions. Mac caches are NESTED: _mac-{{OPERATOR_USER}}/<workspace> and
    _mac-{{OPERATOR_2_USER}}/<workspace> — Rick + Tonio + Miranda ({{OPERATOR}} Mac), Miranda
    ({{OPERATOR_2}} Mac). We attribute Mac sessions by workspace, suffixed by whose Mac."""
    d = dir_name
    if d.startswith("-home-claude-agents-bubble-ops-"):
        return d[len("-home-claude-agents-bubble-ops-"):]
    if d.startswith("-home-claude-agents-"):
        rest = d[len("-home-claude-agents-"):]
        if rest.startswith(("fixture", "morty-workspace", "ricky")):
            return None
        return rest
    if d == "-home-claude":
        return "_p_crons"  # split into jobs by cron-marker in parse
    # Post-#1120 isolated VPS agents are copied by wiki-transcript-sync into
    # `_vps-<slug>/<mangled-workdir>/`. The slug belongs to the source root,
    # not the mangled child path. Hermes mirrors use the sibling
    # `_vps-<slug>-hermes/` convention, with root-exported SQLite usage.
    if d.startswith("_vps-"):
        source = d.split("/", 1)[0]
        slug = source[len("_vps-"):]
        if slug.endswith("-hermes"):
            slug = slug[:-len("-hermes")]
        return slug or None
    # Mac caches (nested): "_mac-<operator>/<workspace-dir>" — one cache dir per
    # operator Mac. The operator label is derived from the dir suffix so no
    # operator name is hardcoded here.
    if d.startswith("_mac-"):
        prefix = d.split("/", 1)[0]          # e.g. "_mac-<operator>"
        whose = prefix[len("_mac-"):] or "operator"
        # the workspace part after the cache prefix + '/'
        ws = d.split("/", 1)[1] if "/" in d else ""
        wsl = ws.lower().replace("_", "-")
        name = None

        # 1) bubble-ops-<slug> convention (the robust core). Any dept whose Mac
        #    workspace follows the same `bubble-ops-<slug>` convention that
        #    dept_registry.list_departments() uses is attributed automatically —
        #    so a NEW or RENAMED dept never silently drops off /costs. Take the
        #    substring after the LAST 'bubble-ops-'; the workspace tail is a
        #    single dir name so what follows IS the slug. Defensive split on '/'
        #    in case a trailing path segment ever sneaks in.
        marker = "bubble-ops-"
        if marker in wsl:
            slug = wsl.rsplit(marker, 1)[1].split("/", 1)[0]
            if slug:
                # ALIAS only where the friendly agent name differs from the slug.
                # Everything else resolves to the slug itself (accountant→
                # accountant, and a future bubble-ops-ben/-maya/-eliot →
                # ben/maya/eliot — the whole point of the convention).
                _MAC_SLUG_ALIAS = {
                    "content": "miranda",  # workspace is bubble-ops-content, agent is Miranda
                }
                name = _MAC_SLUG_ALIAS.get(slug, slug)

        # 2) Explicit non-bubble-ops workspaces (legacy / differently-named),
        #    only consulted when the convention above didn't match.
        if name is None:
            for key, label in (
                ("rick-rnd", "rick"),
                ("tony-ceo", "tonio"),
                ("miranda-socials", "miranda"),        # legacy workspace → still miranda
                ("ellie", "ellie"),                    # concierge, not bubble-ops-prefixed
                ("ben-fund", "ben (mac-legacy)"),
                ("maya-sales", "maya (mac-legacy)"),
                ("eliot-security", "eliot (mac-legacy)"),
            ):
                if key in wsl:
                    name = label
                    break

        # 3) disambiguate Miranda across the two Macs
        if name == "miranda":
            return f"miranda ({whose}-mac)"
        if name is not None:
            return name

        # 4) Unattributed. Still drop it (None) — we don't count random dirs —
        #    but make a real agent-workspace drop VISIBLE instead of silent, so
        #    a future rename that escapes both the convention and the legacy map
        #    surfaces a warning rather than quietly vanishing from /costs. Gate
        #    on 'claude-workspaces' so sub-path / noise dirs don't spam the log.
        if "claude-workspaces" in wsl:
            _log.warning(
                "cost_tracker: unattributed _mac workspace %s — sessions not counted on /costs",
                ws,
            )
        return None
    return None


# `claude -p` cron job detection: the launcher prompts are distinctive. Match a
# few stable phrases to label the -home-claude sessions.
_JOB_MARKERS = [
    ("wiki-compile", ("cloud-wiki-compile skill", "shared wiki", "mine today")),
    ("loop-backup-floor", ("forced layer", "loop-backup", "FLOOR")),
    ("morty-audit", ("morty-agentic-audit", "audit")),
]


def _detect_job(first_user_text: str) -> str:
    t = (first_user_text or "").lower()
    for label, needles in _JOB_MARKERS:
        if any(n.lower() in t for n in needles):
            return label
    return "other-p-cron"


def _note_unreadable(
    filepath: Path, on_unreadable: Optional[Callable[[Path], None]]
) -> None:
    """Record an unreadable transcript path without making callers fail."""
    if on_unreadable is not None:
        on_unreadable(filepath)
    else:
        _log.warning("cost_tracker: unreadable transcript path skipped: %s", filepath)


def _timestamp(value) -> Optional[datetime]:
    """Require an explicit offset for strings; Hermes numeric times are Unix seconds."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc)
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def _number(value, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
    except OverflowError:
        return None
    if integer and int(value) != value:
        return None
    return int(value) if integer else float(value)


def _tokens(usage: dict, source: str) -> dict:
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "reasoning_tokens") if source == "claude" else (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens")
    return {name: _number(usage.get(key), integer=True) or 0
            for name, key in zip(TOKEN_CLASSES, keys)}


def _token_total(usage: dict) -> int:
    # Reasoning is a diagnostic subset of output; never charge/count it twice.
    return sum(usage.get(key, 0) for key in TOKEN_CLASSES if key != "reasoning")


def _merge_record(records: dict, record: dict) -> None:
    """Claude emits content-block/stream snapshots sharing one message identity.

    Keep maxima for cumulative usage classes rather than billing each snapshot.
    ID-less copies use timestamp/model/usage/role content fingerprints.
    """
    key = record["identity"]
    if key not in records:
        records[key] = record
    else:
        old = records[key]
        for name in TOKEN_CLASSES:
            old["usage"][name] = max(old["usage"][name], record["usage"][name])


def _session_records(filepath: Path, meta: dict):
    """Stream billable rows; production never retains a whole transcript in RAM."""
    if filepath.is_symlink():
        return
    with open(filepath, "r", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(d, dict) or not isinstance(d.get("message"), dict):
                continue
            msg = d["message"]
            if not meta["first_user_text"] and d.get("type") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    meta["first_user_text"] = content[:2000]
                elif isinstance(content, list):
                    meta["first_user_text"] = " ".join(
                        it.get("text", "") for it in content
                        if isinstance(it, dict) and isinstance(it.get("text", ""), str)
                    )[:2000]
            if (d.get("type") != "assistant" or not isinstance(msg.get("usage"), dict)
                    or msg.get("model") == "<synthetic>"):
                continue
            model = msg.get("model") if isinstance(msg.get("model"), str) else "unknown"
            usage = _tokens(msg["usage"], "claude")
            if msg.get("id"):
                identity = f"message:{msg['id']}"
            elif d.get("uuid"):
                identity = f"uuid:{d['uuid']}"
            else:
                # No file/line component: identical resumed/sub-agent copies collapse.
                # Truly independent calls with identical timestamp/model/usage/role
                # also collapse; explicit provider IDs always take precedence.
                content_key = json.dumps([d.get("timestamp"), model,
                    [usage[k] for k in TOKEN_CLASSES], msg.get("role", "assistant")],
                    separators=(",", ":"), sort_keys=True)
                identity = "content:" + hashlib.blake2b(content_key.encode(), digest_size=16).hexdigest()
            yield {"identity": identity, "session_id": d.get("sessionId") or filepath.stem,
                   "timestamp": d.get("timestamp"), "model": model,
                   "usage": usage, "source": "claude"}


def parse_session(
    filepath: Path,
    on_unreadable: Optional[Callable[[Path], None]] = None,
) -> Optional[dict]:
    """Uncached compatibility/reference parser; reports use the streaming index."""
    records = {}
    meta = {"first_user_text": ""}
    try:
        for rec in _session_records(filepath, meta):
            _merge_record(records, rec)
        mtime = filepath.stat().st_mtime
    except OSError:
        _note_unreadable(filepath, on_unreadable)
        return None
    if not records:
        return None
    model_usage = {}
    for rec in records.values():
        mu = model_usage.setdefault(rec["model"], dict.fromkeys(TOKEN_CLASSES, 0))
        for key in TOKEN_CLASSES:
            mu[key] += rec["usage"][key]
    return {"model_usage": model_usage, "records": list(records.values()),
            **meta, "n_turns": len(records), "mtime": mtime}


def parse_session_for_day(
    filepath: Path, day: str,
    on_unreadable: Optional[Callable[[Path], None]] = None,
) -> Optional[dict]:
    """Only assistant usage timestamped on this Europe/Paris calendar day."""
    parsed = parse_session(filepath, on_unreadable)
    if parsed is None:
        return None
    records = [r for r in parsed["records"] if (dt := _timestamp(r["timestamp"]))
               and dt.astimezone(PARIS).date().isoformat() == day]
    if not records:
        return None
    models = {}
    for rec in records:
        mu = models.setdefault(rec["model"], dict.fromkeys(TOKEN_CLASSES, 0))
        for key in TOKEN_CLASSES:
            mu[key] += rec["usage"][key]
    return {**parsed, "records": records, "model_usage": models, "n_turns": len(records)}


# ── Report-level TTL cache. `build_report` still walks every project dir + stats
# every JSONL to check mtimes even when the per-session parse is cache-hit (the
# walk itself is the cost on large trees) — so on top of the mtime cache, keep
# the assembled report around for a short window. `refresh=True` always bypasses
# this TTL, so explicit refresh discovers newly changed sources immediately.
# Unchanged fingerprints are reused even for day-specific/refresh reports.
_REPORT_TTL_SECONDS = 45
_report_cache: dict = {"report": None, "built_at": 0.0}


def _cached_report() -> Optional[dict]:
    report = _report_cache["report"]
    if report is None:
        return None
    if (time.monotonic() - _report_cache["built_at"]) >= _REPORT_TTL_SECONDS:
        return None
    return report


def _store_report(report: dict) -> None:
    _report_cache["report"] = report
    _report_cache["built_at"] = time.monotonic()


def build_report(refresh: bool = False, day: Optional[str] = None) -> dict:
    """Today and seven calendar days use message time in Europe/Paris.

    day adds an arbitrary Paris day. File mtime is only a parse-cache key.
    """
    if day is not None:
        datetime.strptime(day, "%Y-%m-%d")
    if day is None and not refresh:
        cached = _cached_report()
        if cached is not None and cached.get("today_date") == datetime.now(PARIS).date().isoformat():
            return cached
    report = _build_report_uncached(refresh=refresh, day=day)
    if day is None:
        _store_report(report)
    return report


def _hermes_records(path: Path, notes: set) -> list[dict]:
    try:
        if path.is_symlink():
            notes.add(f"Skipped symlink Hermes export: {path.parent.name}")
            return []
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            raise ValueError("invalid export shape")
        notes.update(str(n) for n in data.get("notes", []) if isinstance(n, str))
        records = []
        for row in data["rows"]:
            if (not isinstance(row, dict) or not isinstance(row.get("session_id"), (str, int))
                    or not isinstance(row.get("model"), str)
                    or _number(row.get("input_tokens"), integer=True) is None
                    or _number(row.get("output_tokens"), integer=True) is None):
                notes.add(f"Invalid Hermes usage row: {path.parent.name}")
                continue
            records.append({"identity": f"hermes:{row['session_id']}:{row['model']}",
                            "session_id": row["session_id"], "timestamp": row.get("started_at"),
                            "model": row["model"], "usage": _tokens(row, "hermes"),
                            "actual_cost_usd": _number(row.get("actual_cost_usd")),
                            "estimated_cost_usd": _number(row.get("estimated_cost_usd")),
                            "session_actual_cost_usd": _number(row.get("session_actual_cost_usd")),
                            "session_estimated_cost_usd": _number(row.get("session_estimated_cost_usd")),
                            "source": "hermes"})
        groups = {}
        for rec in records:
            groups.setdefault(str(rec["session_id"]), []).append(rec)
        for group in groups.values():
            # Preserve an available session bill without allocating invented
            # per-model prices or copying the whole bill into every model.
            bill = next((r["session_actual_cost_usd"] for r in group
                         if r["session_actual_cost_usd"] is not None), None)
            needs_session_bill = any(r["actual_cost_usd"] is None for r in group)
            if bill is None:
                bill = next((r["session_estimated_cost_usd"] for r in group
                             if r["session_estimated_cost_usd"] is not None), None)
                needs_session_bill = any(r["actual_cost_usd"] is None and r["estimated_cost_usd"] is None for r in group)
            if bill is not None and needs_session_bill:
                for index, rec in enumerate(sorted(group, key=lambda r: r["model"])):
                    rec["session_bill_usd"] = bill if index == 0 else 0.0
                notes.add("Hermes session-level bills are counted once; their per-model dollar split is unavailable.")
        notes.add("Hermes counters are cumulative session/model totals attributed to the Paris session start day; per-day usage within long-lived sessions is unavailable.")
        notes.add("Reasoning tokens are treated as an output subset and excluded from token totals/additional pricing; verify provider semantics before billing use.")
        notes.add("Hermes actual/estimated total bills have no separate cache-dollar split; cache_cost is null for those rows.")
        return records
    except (OSError, ValueError, TypeError):
        notes.add(f"Missing or corrupt Hermes usage export: {path.parent.name}")
        return []


def _build_report_uncached(refresh: bool = False, day: Optional[str] = None) -> dict:
    # Defer sibling imports: mission_kpis loads pricing constants by file spec.
    if __package__:
        from .cost_cache import UsageCache
    else:
        from cost_cache import UsageCache
    pricing = _load_pricing()
    now = datetime.now(timezone.utc)
    today = now.astimezone(PARIS).date()
    week_start = today - timedelta(days=6)
    spans = ("today", "week", "day") if day is not None else ("today", "week")
    notes = set()
    unreadable = set()

    def record_unreadable(path):
        unreadable.add(str(path))
        _log.warning("cost_tracker: unreadable transcript path skipped: %s", path)

    def blank_bucket():
        return {"cost": 0.0, "cache_cost": 0.0, "tokens": 0, "runs": 0,
                "by_model": {}, "tokens_by_class": dict.fromkeys(TOKEN_CLASSES, 0),
                "cost_usd_estimate": 0.0, "cost_usd_estimate_priced_only": 0.0}

    def blank():
        return {span: blank_bucket() for span in spans}

    # Deterministic discovery: never recurse through a directory/file symlink.
    def walk(path):
        try:
            for child in sorted(path.iterdir()):
                if child.is_symlink():
                    continue
                if child.is_dir():
                    yield from walk(child)
                elif child.suffix == ".jsonl" and child.is_file():
                    yield child
        except OSError:
            record_unreadable(path)

    try:
        projects = sorted(PROJECTS_DIR.iterdir()) if not PROJECTS_DIR.is_symlink() else []
    except OSError:
        record_unreadable(PROJECTS_DIR)
        projects = []
    records = {}  # Hermes exports only; Claude records stay on disk.
    claude_runs = {}
    def paris_day(timestamp):
        dt = _timestamp(timestamp)
        return dt.astimezone(PARIS).date().toordinal() if dt else None

    with closing(UsageCache(CACHE_FILE)) as cache:
        for proj in projects:
            if proj.is_symlink() or not proj.is_dir():
                continue
            if proj.name.startswith("_vps-") and proj.name.endswith("-hermes"):
                label = classify(proj.name)
                for rec in _hermes_records(proj / "hermes-usage.json", notes):
                    rec["label"] = label
                    records.setdefault((label, rec["identity"]), rec)
                # state.db is authoritative; never also bill compatible Hermes JSONLs.
                continue
            try:
                roots = [(classify(f"{proj.name}/{sub.name}"), sub) for sub in sorted(proj.iterdir())
                         if not sub.is_symlink() and sub.is_dir()] if proj.name.startswith("_mac-") else [(classify(proj.name), proj)]
            except OSError:
                record_unreadable(proj)
                continue
            for label, root in roots:
                if label is None:
                    continue
                for path in walk(root):
                    try:
                        stat = path.stat()
                    except OSError:
                        record_unreadable(path)
                        continue
                    meta = {"first_user_text": ""}
                    try:
                        cache.sync_file(path, (stat.st_mtime_ns, stat.st_size), label,
                                        lambda: _session_records(path, meta), meta, paris_day, _detect_job)
                    except OSError:
                        record_unreadable(path)
        cache.finish(prune=not unreadable)
        if cache.invalid_timestamp():
            notes.add("Usage without a valid timezone-aware timestamp was excluded; file mtime is never a day fallback.")
        requested = datetime.strptime(day, "%Y-%m-%d").date().toordinal() if day else None
        for span, start, end in (("today", today.toordinal(), today.toordinal()),
                                 ("week", week_start.toordinal(), today.toordinal()),
                                 ("day", requested, requested)):
            if start is not None:
                for label, runs in cache.run_counts(start, end).items():
                    claude_runs[label, span] = runs
        # Materialize only day/model aggregates, never message identities.
        claude_records = [
            {"label": label, "model": model, "usage": usage, "source": "claude",
             "timestamp": datetime.fromordinal(date).replace(tzinfo=PARIS).isoformat()}
            for date, label, model, usage in cache.rows(week_start.toordinal(), today.toordinal(), requested)
        ]

    # A valid Hermes export identifies an agent even if its cumulative sessions
    # started before the window. Keep a visible zero start-day bucket.
    agents = {rec["label"]: {**blank(), "sources": ["hermes"]}
              for rec in records.values() if rec["source"] == "hermes"}
    run_sets = {}
    for rec in chain(claude_records, records.values()):
        if rec["model"] == "<synthetic>":
            continue
        dt = _timestamp(rec["timestamp"])
        if dt is None:
            notes.add("Usage without a valid timezone-aware timestamp was excluded; file mtime is never a day fallback.")
            continue
        date = dt.astimezone(PARIS).date()
        buckets = []
        if week_start <= date <= today:
            buckets.append("week")
        if date == today:
            buckets.append("today")
        if day is not None and date.isoformat() == day:
            buckets.append("day")
        if not buckets:
            continue
        label, model, usage = rec["label"], rec["model"], rec["usage"]
        ag = agents.setdefault(label, blank())
        sources = ag.setdefault("sources", [])
        if rec["source"] not in sources:
            sources.append(rec["source"])
        # Round after bucket aggregation, never once per tiny API call.
        split = _cost_split({model: usage}, pricing, rounded=False)
        total_cost = split["real"] + split["cache"]
        cache_cost = split["cache"]
        headline = split["real"]
        known_model = any(key in model.lower() for key in pricing)
        has_tokens = any(usage.get(key, 0) > 0 for key in TOKEN_CLASSES)
        if rec["source"] == "hermes":
            own_cost = rec.get("session_bill_usd", rec.get("actual_cost_usd"))
            if own_cost is None:
                own_cost = rec.get("estimated_cost_usd")
            if own_cost is not None:
                total_cost = headline = own_cost
                cache_cost = None  # Hermes costs have no separate cache-dollar split.
            elif not known_model and has_tokens:
                total_cost = headline = cache_cost = None
                notes.add(f"Unpriced Hermes model for {label}: {model}; cost is null.")
        elif not known_model and has_tokens:
            # Preserve cockpit's legacy zero headline, but summary must not imply free.
            total_cost = None
            notes.add(f"Unpriced Claude model for {label}: {model}; summary cost is null.")
        for span in buckets:
            b = ag[span]
            for field, amount in (("cost", headline), ("cache_cost", cache_cost), ("cost_usd_estimate", total_cost)):
                b[field] = None if b[field] is None or amount is None else b[field] + amount
            b["cost_usd_estimate_priced_only"] += total_cost if total_cost is not None else 0.0
            b["tokens"] += _token_total(usage)
            for name in TOKEN_CLASSES:
                b["tokens_by_class"][name] += usage[name]
            sessions = run_sets.setdefault((label, span), set())
            if rec["source"] == "hermes":
                sessions.add(str(rec["session_id"]))
            b["runs"] = claude_runs.get((label, span), 0) + len(sessions)
            if _token_total(usage):
                short = next((m for m in ("opus", "sonnet", "haiku") if m in model), model)
                bm = b["by_model"].setdefault(short, {"tokens": 0, "cost": 0.0})
                bm["tokens"] += _token_total(usage)
                model_cost = None if "session_bill_usd" in rec else total_cost
                bm["cost"] = None if bm["cost"] is None or model_cost is None else bm["cost"] + model_cost
    totals = blank()
    for ag in agents.values():
        ag["sources"].sort()
        for span in spans:
            b = ag[span]
            for key in ("cost", "cache_cost", "cost_usd_estimate", "cost_usd_estimate_priced_only"):
                b[key] = round(b[key], 4) if b[key] is not None else None
                t = totals[span]
                t[key] = None if t[key] is None or b[key] is None else t[key] + b[key]
            for key in ("tokens", "runs"):
                totals[span][key] += b[key]
            for key in TOKEN_CLASSES:
                totals[span]["tokens_by_class"][key] += b["tokens_by_class"][key]
            for bm in b["by_model"].values():
                if bm["cost"] is not None:
                    bm["cost"] = round(bm["cost"], 4)
    for span in spans:
        for key in ("cost", "cache_cost", "cost_usd_estimate", "cost_usd_estimate_priced_only"):
            if totals[span][key] is not None:
                totals[span][key] = round(totals[span][key], 4)
    out = {"scanned_at": now.isoformat(), "today_date": today.isoformat(),
           "timezone": FLEET_TIMEZONE, "agents": dict(sorted(agents.items(), key=lambda kv: kv[1]["week"]["cost"] or 0, reverse=True)),
           "totals": totals, "notes": sorted(notes),
           "pricing_note": "Claude: token × existing list-price estimate (cache separate). Hermes: actual/estimated total cost when available, otherwise matched list price; null means unavailable.",
           "unreadable_transcripts": len(unreadable), "unreadable_transcript_paths": sorted(unreadable)[:3]}
    if not projects:
        out["note"] = "no readable projects dir"
    if day is not None:
        out["day_requested"] = day
    return out


def fleet_summary(day: str, refresh: bool = False) -> dict:
    report = build_report(refresh=refresh, day=day)
    if str(PROJECTS_DIR) in report["unreadable_transcript_paths"] or PROJECTS_DIR.is_symlink():
        raise OSError(f"Fleet summary projects directory is unreadable: {PROJECTS_DIR}")
    def row(bucket):
        return {key: bucket[key] for key in ("tokens", "tokens_by_class", "cost_usd_estimate", "cost_usd_estimate_priced_only", "cache_cost", "runs")}
    agents = {name: {**row(ag["day"]), "source": ag["sources"][0] if len(ag["sources"]) == 1 else ag["sources"]}
              for name, ag in report["agents"].items()}
    expected = sorted(name for name, ag in report["agents"].items() if ag["week"]["runs"])
    notes = list(report["notes"])
    missing_costs = sorted(name for name, ag in agents.items() if ag["cost_usd_estimate"] is None)
    if missing_costs:
        notes.append(f"Fleet cost is null; missing costs for agents: {', '.join(missing_costs)}. cost_usd_estimate_priced_only is a lower bound.")
    if report["unreadable_transcripts"]:
        notes.append(f"{report['unreadable_transcripts']} unreadable transcript paths skipped; totals may be incomplete.")
    if report.get("note"):
        notes.append(report["note"])
    return {"generated_at": report["scanned_at"], "day": day, "timezone": FLEET_TIMEZONE,
            "agents": agents, "totals": row(report["totals"]["day"]),
            "expected_agents": expected, "expected_agents_without_data": [name for name in expected if not agents[name]["runs"]],
            "notes": notes}


# ── Budget (read-only operator steer, board #524d) ──────────────────────
# Budgets are set by the operator in each dept.yaml under recurring_missions[]
# (each mission entry MAY carry `budget_usd`). We only READ them here — never
# write. The enumeration mirrors dataflow._all_mission_entries / _missions:
# entries live under any of layers|recurring_missions|missions.
_BUDGET_MISSION_KEYS = ("layers", "recurring_missions", "missions")


def dept_weekly_envelope(dept_yaml: Optional[dict]) -> Optional[float]:
    """Read `department.budget_weekly_usd` from a dept.yaml (board #466, child
    of #404). This is a NEW, distinct field from mission `budget_usd` above —
    it lives once on the `department:` block (operator-owned, push-locked,
    same convention as mission budget_usd) and is the per-dept WEEKLY envelope
    itself, not a mission-cycle amount to sum.

    Returns the float, or None when absent/malformed (missing dept_yaml, no
    `department` block, no `budget_weekly_usd`, or a non-numeric/bool value)
    so the caller can render "non défini" instead of a misleading $0 or
    crashing. Never raises.
    """
    if not isinstance(dept_yaml, dict):
        return None
    dept = dept_yaml.get("department")
    if not isinstance(dept, dict):
        return None
    v = dept.get("budget_weekly_usd")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v), 2)
    return None


def mission_budget_total(dept_yaml: Optional[dict]) -> Optional[float]:
    """Sum `budget_usd` across a dept.yaml's mission entries.

    Returns the summed budget as a float, or None when NO mission entry carries
    a `budget_usd` at all (so the caller can render "budget non défini" instead
    of a misleading $0). A malformed / missing dept_yaml → None. Never raises.
    """
    if not isinstance(dept_yaml, dict):
        return None
    total = 0.0
    found = False
    for key in _BUDGET_MISSION_KEYS:
        v = dept_yaml.get(key)
        if not isinstance(v, list):
            continue
        for m in v:
            if not isinstance(m, dict):
                continue
            b = m.get("budget_usd")
            if isinstance(b, (int, float)) and not isinstance(b, bool):
                total += float(b)
                found = True
    return round(total, 2) if found else None


# Report agent-keys carry disambiguation suffixes (e.g. "miranda (jade-mac)",
# "ben (mac-legacy)") and workspace/agent names that differ from dept slugs.
# To roll a dept's spend up from the per-agent report we normalise each agent
# key back to its dept slug: strip any " (...)" suffix, then map known aliases.
_AGENT_KEY_TO_SLUG_ALIAS = {
    "miranda": "content",  # Miranda IS the content dept's agent (workspace bubble-ops-content)
    "rick": "rnd",
    "tonio": "tonio",  # External R&D has its own spend; never roll into Tony/CEO.
    "eliot": "security",
}


def agent_key_base(agent_key: str) -> str:
    """Normalise a report agent-key to its comparable base name: drop the
    ' (mac...)' disambiguation suffix and lower-case. e.g.
    'miranda (jade-mac)' → 'miranda', 'ben (mac-legacy)' → 'ben'."""
    base = (agent_key or "").split(" (", 1)[0].strip().lower()
    return base


def spent_by_dept(report: dict, span: str = "week") -> dict:
    """Roll the per-agent report up to a {dept_slug: real-$ spend} map.

    Matches each report agent-key to a dept slug by its normalised base name
    (see agent_key_base) plus the small known agent→dept alias map. Agent keys
    that don't map to a dept slug (e.g. `claude -p` cron jobs like
    'wiki-compile') are simply left out of the map. Never raises.
    """
    out: dict[str, float] = {}
    agents = report.get("agents") if isinstance(report, dict) else None
    if not isinstance(agents, dict):
        return out
    for key, a in agents.items():
        base = agent_key_base(key)
        slug = _AGENT_KEY_TO_SLUG_ALIAS.get(base, base)
        try:
            cost = float(a.get(span, {}).get("cost", 0.0))
        except (AttributeError, TypeError, ValueError):
            cost = 0.0
        out[slug] = round(out.get(slug, 0.0) + cost, 3)
    return out


def session_health(report: dict, recent_heartbeat_depts: list[str]) -> dict:
    """Check the /costs data-path invariant for recently-active departments.

    A department with a heartbeat in the last 24 hours must have at least one
    session in the report's seven-day bucket. Keeping this pure (the caller
    supplies the recent heartbeat slugs) makes the cross-source check easy to
    exercise with fixtures and avoids coupling transcript parsing to the
    heartbeat reader.
    """
    runs_by_dept: dict[str, int] = {}
    agents = report.get("agents") if isinstance(report, dict) else None
    if isinstance(agents, dict):
        for key, agent in agents.items():
            base = agent_key_base(str(key))
            slug = _AGENT_KEY_TO_SLUG_ALIAS.get(base, base)
            try:
                runs = int(agent.get("week", {}).get("runs", 0))
            except (AttributeError, TypeError, ValueError):
                runs = 0
            runs_by_dept[slug] = runs_by_dept.get(slug, 0) + max(runs, 0)

    recent = sorted(set(recent_heartbeat_depts))
    violations = [slug for slug in recent if runs_by_dept.get(slug, 0) <= 0]
    return {
        "ok": not violations,
        "invariant": "sessions>0 when a dept had a heartbeat in the last 24h",
        "recent_heartbeat_depts": recent,
        "violations": violations,
        "sessions_by_dept": {slug: runs_by_dept.get(slug, 0) for slug in recent},
    }


def budget_status(spent: float, budget: Optional[float]) -> dict:
    """Return a render-ready budget row: {spent, budget, pct, level, defined}.

    level ∈ {"ok" (<80%), "warn" (80–100%), "over" (>100%)} drives the
    green/amber/red progress bar. When budget is None or <= 0, defined=False,
    pct=None (no bar, no div-by-zero). Never raises.
    """
    spent = float(spent or 0.0)
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget is None or budget <= 0:
        return {"spent": round(spent, 2), "budget": None, "pct": None,
                "level": "none", "defined": False}
    pct = round(spent / budget * 100.0, 1)
    level = "ok" if pct < 80 else ("warn" if pct <= 100 else "over")
    return {"spent": round(spent, 2), "budget": round(float(budget), 2),
            "pct": pct, "level": level, "defined": True}


def main() -> int:
    # Keep constant-only imports (mission_kpis.pricing_table) self-contained.
    if __package__:
        from .cost_io import atomic_json
    else:
        from cost_io import atomic_json
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--day", help="YYYY-MM-DD, Europe/Paris calendar day")
    ap.add_argument("--fleet-summary", action="store_true")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--latest", type=Path, help="Also atomically publish the same fleet summary here")
    a = ap.parse_args()
    if a.latest and not a.fleet_summary:
        ap.error("--latest requires --fleet-summary")
    day = a.day or datetime.now(PARIS).date().isoformat()
    result = fleet_summary(day, refresh=a.refresh) if a.fleet_summary else build_report(refresh=a.refresh, day=a.day)
    if a.out:
        atomic_json(a.out, result)
    else:
        print(json.dumps(result, indent=2, allow_nan=False))
    if a.latest:
        atomic_json(a.latest, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
