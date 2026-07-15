#!/usr/bin/env python3
"""cost_tracker.py — per-agent / per-job token & cost scanner for the VPS console.

Ported from the local Tailscale dashboard's `token_usage.py` (Tony_CEO/workspace/
org-dashboard/lib), adapted for the VPS:
  - VPS agent session dirs live under ~/.claude/projects/-home-claude-agents-<...>
  - the wiki-compile + loop-backup floor cron run as `claude -p` under -home-claude
  - Mac caches (_mac-{{OPERATOR_USER}}, _mac-{{OPERATOR_2_USER}}) are rsync'd in (Rick + local Tony live on the Mac)

It reads token `usage` straight from each assistant message in the session JSONLs —
that data is present in BOTH `claude -p` cron sessions AND interactive (--channels)
dept-loop sessions, so every agent is covered (only the $ field total_cost_usd is
-p-only, which is why we price from tokens here, not from that field).

Output JSON (see build_report): per-agent + per-job totals, today / 7d, per-model
breakdown, est. USD. Per-session parses are cached by file mtime, and the
assembled report itself is held for a short TTL (see _REPORT_TTL_SECONDS) so
repeat /costs hits within the window skip the directory walk entirely.

Usage:
    python3 cost_tracker.py            # scan + print JSON
    python3 cost_tracker.py --refresh  # ignore cache, re-parse everything
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

HOME = Path(os.environ.get("HOME", "/home/claude"))
PROJECTS_DIR = HOME / ".claude" / "projects"
CACHE_DIR = HOME / ".claude" / "cache"
CACHE_FILE = CACHE_DIR / "console-cost-sessions.json"

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


def _cost_split(model_usage: dict, pricing: dict) -> dict:
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
    return {"real": round(real, 4), "cache": round(cache, 4)}


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
# job below. Mac caches (_mac-{{OPERATOR_USER}}/_mac-{{OPERATOR_2_USER}}) hold Rick + local-Tony.
def classify(dir_name: str) -> Optional[str]:
    """Map a project-dir name (top-level, OR a Mac-cache 'cache/workspace' pair
    joined by '/') → a friendly agent/job label. VPS-live agents live in
    -home-claude-agents-bubble-ops-<slug>. The -home-claude dir holds the
    `claude -p` cron sessions. Mac caches are NESTED: _mac-{{OPERATOR_USER}}/<workspace> and
    _mac-{{OPERATOR_2_USER}}/<workspace> — Rick + local Tony + Miranda ({{OPERATOR}} Mac), Miranda
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
    # Mac caches (nested): "_mac-<operator>/<workspace-dir>" — one cache dir per
    # operator Mac. The operator label is derived from the dir suffix so no
    # operator name is hardcoded here.
    if d.startswith("_mac-"):
        prefix = d.split("/", 1)[0]          # e.g. "_mac-<operator>"
        whose = prefix[len("_mac-"):] or "operator"
        # the workspace part after the cache prefix + '/'
        ws = d.split("/", 1)[1] if "/" in d else ""
        wsl = ws.lower()
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
                ("tony-ceo", "tony (local)"),
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


def parse_session(filepath: Path) -> Optional[dict]:
    """Return per-session per-model usage + the detected -p job (if any)."""
    model_usage: dict[str, dict] = {}
    first_user_text = ""
    n_turns = 0
    try:
        with open(filepath, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not first_user_text and d.get("type") == "user":
                    msg = d.get("message")
                    if isinstance(msg, dict):
                        c = msg.get("content")
                        if isinstance(c, str):
                            first_user_text = c[:2000]
                        elif isinstance(c, list):
                            first_user_text = " ".join(
                                it.get("text", "") for it in c if isinstance(it, dict)
                            )[:2000]
                if d.get("type") == "assistant":
                    msg = d.get("message")
                    if isinstance(msg, dict):
                        u = msg.get("usage")
                        if isinstance(u, dict):
                            model = msg.get("model", "unknown")
                            mu = model_usage.setdefault(
                                model, {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
                            )
                            mu["input"] += u.get("input_tokens", 0) or 0
                            mu["output"] += u.get("output_tokens", 0) or 0
                            mu["cache_read"] += u.get("cache_read_input_tokens", 0) or 0
                            mu["cache_create"] += u.get("cache_creation_input_tokens", 0) or 0
                            n_turns += 1
    except FileNotFoundError:
        return None
    if not model_usage:
        return None
    return {
        "model_usage": model_usage,
        "first_user_text": first_user_text,
        "n_turns": n_turns,
        "mtime": filepath.stat().st_mtime,
    }


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache))
    except Exception:
        pass


# ── Report-level TTL cache. `build_report` still walks every project dir + stats
# every JSONL to check mtimes even when the per-session parse is cache-hit (the
# walk itself is the cost on large trees) — so on top of the mtime cache, keep
# the assembled report around for a short window. `refresh=True` always bypasses
# this (and the mtime cache below), so the explicit-refresh escape hatch still
# forces a full rescan.
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


def build_report(refresh: bool = False) -> dict:
    if not refresh:
        cached = _cached_report()
        if cached is not None:
            return cached

    report = _build_report_uncached(refresh=refresh)
    _store_report(report)
    return report


def _build_report_uncached(refresh: bool = False) -> dict:
    pricing = _load_pricing()
    cache = {} if refresh else _load_cache()
    new_cache: dict = {}

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    cutoff_7d = (now - timedelta(days=7)).timestamp()
    start_today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()

    # agent -> {today:{...}, week:{...}} accumulators
    def _blank():
        return {
            "today": {"cost": 0.0, "cache_cost": 0.0, "tokens": 0, "runs": 0, "by_model": {}},
            "week": {"cost": 0.0, "cache_cost": 0.0, "tokens": 0, "runs": 0, "by_model": {}},
        }

    agents: dict[str, dict] = {}

    if not PROJECTS_DIR.is_dir():
        return {"scanned_at": now.isoformat(), "agents": {}, "totals": _blank(),
                "note": "no projects dir"}

    # Build (label, jsonl-files) work units. Flat dirs map directly; the nested
    # Mac caches (_mac-{{OPERATOR_USER}}/<ws>, _mac-{{OPERATOR_2_USER}}/<ws>) are descended one level.
    work = []  # list of (label0, file_iterable)
    for proj in PROJECTS_DIR.iterdir():
        if not proj.is_dir():
            continue
        if proj.name.startswith("_mac-"):
            for sub in proj.iterdir():
                if not sub.is_dir():
                    continue
                label0 = classify(f"{proj.name}/{sub.name}")
                if label0 is None:
                    continue
                work.append((label0, sub.glob("*.jsonl")))
        else:
            label0 = classify(proj.name)
            if label0 is None:
                continue
            work.append((label0, proj.glob("*.jsonl")))

    for label0, files in work:
        for f in files:
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff_7d:
                continue  # only last 7d matters for the panel
            key = f"{f}"
            cached = cache.get(key)
            if cached and cached.get("mtime") == mtime:
                parsed = cached
            else:
                parsed = parse_session(f)
                if parsed is None:
                    continue
            new_cache[key] = parsed

            # resolve the real label (split the -p cron dir into jobs)
            label = label0
            if label0 == "_p_crons":
                label = _detect_job(parsed.get("first_user_text", ""))

            _split = _cost_split(parsed["model_usage"], pricing)
            cost = _split["real"]          # neutralized: non-cache (the headline cost)
            cache_cost = _split["cache"]   # shown separately on /costs
            toks = sum(sum(mu.values()) for mu in parsed["model_usage"].values())

            ag = agents.setdefault(label, _blank())
            buckets = [ag["week"]]
            if mtime >= start_today:
                buckets.append(ag["today"])
            for b in buckets:
                b["cost"] += cost
                b["cache_cost"] += cache_cost
                b["tokens"] += toks
                b["runs"] += 1
                for model, mu in parsed["model_usage"].items():
                    if "synthetic" in model or sum(mu.values()) == 0:
                        continue
                    short = ("opus" if "opus" in model else "sonnet" if "sonnet" in model
                             else "haiku" if "haiku" in model else model)
                    bm = b["by_model"].setdefault(short, {"tokens": 0, "cost": 0.0})
                    bm["tokens"] += sum(mu.values())
                    bm["cost"] += _cost_of({model: mu}, pricing)

    _save_cache(new_cache)

    # round + totals
    totals = _blank()
    for ag in agents.values():
        for span in ("today", "week"):
            ag[span]["cost"] = round(ag[span]["cost"], 3)
            ag[span]["cache_cost"] = round(ag[span]["cache_cost"], 3)
            totals[span]["cost"] += ag[span]["cost"]
            totals[span]["cache_cost"] += ag[span]["cache_cost"]
            totals[span]["tokens"] += ag[span]["tokens"]
            totals[span]["runs"] += ag[span]["runs"]
            for m, bm in ag[span]["by_model"].items():
                bm["cost"] = round(bm["cost"], 3)
    for span in ("today", "week"):
        totals[span]["cost"] = round(totals[span]["cost"], 3)
        totals[span]["cache_cost"] = round(totals[span]["cache_cost"], 3)

    # sort agents by week cost desc
    agents_sorted = dict(sorted(agents.items(), key=lambda kv: kv[1]["week"]["cost"], reverse=True))
    return {
        "scanned_at": now.isoformat(),
        "today_date": today,
        "agents": agents_sorted,
        "totals": totals,
        "pricing_note": "Estimate from token counts × public list prices — for trend/relative cost, not billing.",
    }


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
# "ben (mac-legacy)") and a couple of workspace→agent aliases (content→miranda).
# To roll a dept's spend up from the per-agent report we normalise each agent
# key back to its dept slug: strip any " (...)" suffix, then map known aliases.
_AGENT_KEY_TO_SLUG_ALIAS = {
    "miranda": "content",  # Miranda IS the content dept's agent (workspace bubble-ops-content)
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
    (see agent_key_base) plus a small alias map (miranda→content). Agent keys
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    print(json.dumps(build_report(refresh=a.refresh), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
