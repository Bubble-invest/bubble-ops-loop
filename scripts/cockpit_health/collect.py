#!/usr/bin/env python3
"""collect.py — cockpit health EVIDENCE COLLECTOR. NO verdicts.

Design principle (wiki shared/systems/cron-judgment-vs-tools.md, Joris msg
1794, reaffirmed board #1222 "we are agentic not deterministic"): tools are
evidence-collectors, the agent is the judgment layer. This script renders
each configured cockpit page in-process via the console's own FastAPI
TestClient (console_client.py — the SAME internal-API path
console/tests/conftest.py's `client` fixture uses; no new auth bypass) and
runs a fixed set of independent source-of-truth comparisons (checks.py).
Every fact gathered is structured JSON. judge.py (a separate process, run
next by the systemd unit) is the only place a verdict gets made.

Output: one evidence_<UTC-stamp>.json per run under --out-dir, plus
evidence_latest.json (judge.py's default read). Rotates old runs, keeping
the last --keep-last (default 48 — 2 days at hourly cadence).

Never prints/logs a secret: the only credential this script touches is
CONSOLE_BEARER_TOKEN, read once by console_client.build_client() and handed
straight to the TestClient's Authorization header — never echoed, never
placed in an evidence field or a log line.

Exit code: always 0 on a completed pass (a collector must not trip its own
systemd timer into `failed` over a page being down — a down page IS
evidence, not a collector bug); non-zero only on a structural error before
any evidence could be written at all.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.cockpit_health.console_client import build_client  # noqa: E402
from scripts.cockpit_health import checks as checks_mod  # noqa: E402

PAGES: List[Dict[str, str]] = [
    {"id": "home", "path": "/", "description": "Home / matin"},
    {"id": "kanban", "path": "/kanban", "description": "Kanban board"},
    {"id": "costs", "path": "/costs", "description": "Costs panel"},
    {"id": "health", "path": "/health", "description": "Carnet de bord"},
    {"id": "agents", "path": "/agents", "description": "Agents roster"},
    {"id": "dept_ben", "path": "/dept/ben", "description": "Ben dept page"},
    {"id": "dept_ben_portfolio", "path": "/dept/ben/portfolio",
     "description": "Ben portfolio / exposure"},
]

DEFAULT_OUT_DIR = REPO_ROOT / "monitoring" / "cockpit-health" / "evidence"


def render_pages(client, pages: List[Dict[str, str]] = PAGES) -> Dict[str, Any]:
    """GET every configured page once. Records http facts only — no
    figure-extraction or judgment here; that's checks.py's job for the pages
    that have one, and judge.py's job for everything else."""
    results: Dict[str, Any] = {}
    for page in pages:
        pid, path = page["id"], page["path"]
        if client is None:
            results[pid] = {
                "path": path, "description": page["description"],
                "http": {"status_code": None, "ok": False,
                         "latency_ms": None, "error": "no console client"},
                "checks": [],
            }
            continue
        t0 = time.perf_counter()
        try:
            resp = client.get(path)
            latency_ms = (time.perf_counter() - t0) * 1000
            results[pid] = {
                "path": path, "description": page["description"],
                "http": {"status_code": resp.status_code,
                         "ok": resp.status_code == 200,
                         "latency_ms": round(latency_ms, 1), "error": None},
                "checks": [],
            }
        except Exception as exc:  # noqa: BLE001 — a hung/broken page is evidence, not a crash
            results[pid] = {
                "path": path, "description": page["description"],
                "http": {"status_code": None, "ok": False,
                         "latency_ms": None, "error": str(exc)},
                "checks": [],
            }
    return results


def run_checks(client, pages: Dict[str, Any], ctx: Optional[checks_mod.Ctx] = None,
                check_fns: Optional[List] = None) -> Dict[str, Any]:
    """Run every registered evidence check and file each result under its
    declared `page`. A check that raises is recorded as its own error entry
    — it NEVER aborts the run (one broken check must not blind every other
    page's evidence for the whole cycle)."""
    ctx = ctx or checks_mod.Ctx()
    check_fns = check_fns if check_fns is not None else checks_mod.CHECKS
    for fn in check_fns:
        try:
            result = fn(client, ctx)
        except Exception as exc:  # noqa: BLE001
            result = {
                "id": getattr(fn, "__name__", "unknown"), "page": "_unassigned",
                "description": "", "observed": {}, "source": {},
                "consistent": None, "hard_inconsistency": False,
                "reason": f"check raised: {exc}", "error": str(exc),
            }
        items = result if isinstance(result, list) else [result]
        for item in items:
            page_id = item.get("page")
            if page_id in pages:
                pages[page_id]["checks"].append(item)
            else:
                pages.setdefault(
                    "_unassigned",
                    {"path": None,
                     "description": "checks whose page id isn't one of PAGES",
                     "http": None, "checks": []},
                )
                pages["_unassigned"]["checks"].append(item)
    return pages


def collect(out_dir: Path = DEFAULT_OUT_DIR, keep_last: int = 48,
            bearer_token_env: str = "CONSOLE_BEARER_TOKEN",
            client=None, client_error: Optional[str] = None,
            now: Optional[datetime] = None):
    """Orchestrates one full collection pass and writes it to disk.

    `client`/`client_error` are injectable for tests (skip build_client()
    entirely); production callers leave them None and let this function build
    the real one."""
    if client is None and client_error is None:
        client, client_error = build_client(bearer_token_env)

    pages = render_pages(client)
    ctx = checks_mod.Ctx(now_fn=(lambda: now.timestamp()) if now else None)
    pages = run_checks(client, pages, ctx=ctx)

    run_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bundle = {
        "run_at": run_at,
        "console_client_error": client_error,
        "pages": pages,
    }

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_at.replace(":", "").replace("-", "")
    out_path = out_dir / f"evidence_{stamp}.json"
    serialized = json.dumps(bundle, indent=2, default=str)
    out_path.write_text(serialized)
    (out_dir / "evidence_latest.json").write_text(serialized)

    _rotate(out_dir, keep_last)
    return bundle, out_path


def _rotate(out_dir: Path, keep_last: int) -> None:
    files = sorted(
        f for f in out_dir.glob("evidence_*.json") if f.name != "evidence_latest.json"
    )
    excess = len(files) - keep_last
    for f in files[:max(0, excess)]:
        try:
            f.unlink()
        except OSError:
            pass


def _count_hard(bundle: Dict[str, Any]) -> int:
    return sum(
        1
        for page in bundle["pages"].values()
        for c in (page.get("checks") or [])
        if c.get("hard_inconsistency")
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--keep-last", type=int, default=48)
    parser.add_argument("--bearer-token-env", default="CONSOLE_BEARER_TOKEN")
    args = parser.parse_args(argv)

    bundle, out_path = collect(
        Path(args.out_dir), args.keep_last, args.bearer_token_env,
    )
    n_hard = _count_hard(bundle)
    n_pages = len([p for p in bundle["pages"] if p != "_unassigned"])
    print(
        f"cockpit_health.collect: wrote {out_path} "
        f"({n_pages} pages, {n_hard} hard inconsistencies)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
