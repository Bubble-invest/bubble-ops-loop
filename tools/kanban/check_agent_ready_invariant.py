#!/usr/bin/env python3
"""check_agent_ready_invariant.py — assert `agent:ready` ⟹ `risk:low` + scoped
body, across all OPEN board issues. READ-ONLY: reports violations, never
mutates the board (board #639).

Why this matters
-----------------
`agent:ready` is a permission grant, not a tag of intent. Rick's manager loop
dispatch rule reads *labels*, not intent: each tick it queries for
`risk:low` + `agent:ready` and hands what it finds to a worker subagent with
no human in the loop. If a card is `agent:ready` but NOT `risk:low` (or has
no `risk:` label at all, or is still an unscoped `(to be scoped...` stub),
any dispatch query that filters on `agent:ready` alone — or a future change
to the dispatch query — would send it to a worker unreviewed. Board #316
(wiring a secrets env var into an always-on session) sat mislabeled for
~3 weeks; it never fired only because the live dispatch query happened to
also filter on risk:low explicitly. That is a coincidence of query
construction, not a safety property this invariant enforces structurally.

Invariant asserted (one issue can trip more than one):
  1. `agent:ready` + risk:medium or risk:high        -> VIOLATION
  2. `agent:ready` + no risk:<x> label at all         -> VIOLATION
  3. `agent:ready` + its own `## Allowed` section still
     reads the literal "(to be scoped..." stub text    -> VIOLATION
     (checked on the card's OWN Allowed section only — a card that merely
     quotes another card's stub as an example, e.g. in a markdown table,
     does not trip this)

This script only READS (`gh issue list --json ...`). It never adds, removes,
or otherwise touches a label, comment, or issue state. A human or the
Manager loop decides what to do with what this script reports.

Usage:
    check_agent_ready_invariant.py [--repo OWNER/NAME] [--json]

Exit code: 0 always when the check itself ran successfully (this is a
REPORT, not a gate) EXCEPT --json/plain output always includes a nonzero
process exit status equal to the violation count capped at 1 for shell
`if` ergonomics is intentionally NOT done — see `main()` docstring for the
exact contract.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

DEFAULT_BOARD_REPO = "Bubble-invest/bubble-ops-board"
STUB_MARKER = "(to be scoped"

# Matches a "## Allowed" section header and captures everything up to the next
# "## " header (or end of body). Card bodies emitted by emit_kanban_item.sh /
# the scaffold template use this exact section name for "what a worker may
# touch" — the thing an unscoped stub genuinely fails to answer.
_ALLOWED_SECTION_RE = re.compile(r"##\s+Allowed\s*\n(.*?)(?=\n##\s|\Z)", re.DOTALL | re.IGNORECASE)


def _label_names(issue: dict) -> list[str]:
    return [(l.get("name") or "").strip() for l in (issue.get("labels") or [])]


def _risk_value(labels: list[str]) -> str | None:
    for name in labels:
        low = name.lower()
        if low.startswith("risk:"):
            return low[len("risk:"):]
    return None


def _allowed_section_is_stub(body: str) -> bool:
    """True iff the card's `## Allowed` section content starts with the literal
    "(to be scoped" stub marker (whitespace-stripped). Deliberately narrower
    than "the stub marker appears anywhere in the body" — a card that merely
    QUOTES another card's stub (e.g. this checker's own tracking card, which
    cites #364's old stub in a markdown table as a worked example) must not
    trip the check. The signal that actually matters is: is THIS card's own
    "what may a worker touch" section unfilled."""
    m = _ALLOWED_SECTION_RE.search(body or "")
    if not m:
        return False
    return m.group(1).strip().startswith(STUB_MARKER)


def check_issue(issue: dict) -> list[str]:
    """Return the list of violation reasons for one issue dict (shape matching
    `gh issue list --json number,title,labels,body`). Empty list = compliant.
    A single issue may return more than one reason (e.g. no risk: label AND a
    stub body)."""
    labels = _label_names(issue)
    label_set_lower = {l.lower() for l in labels}

    if "agent:ready" not in label_set_lower:
        return []  # invariant only applies to agent:ready cards

    reasons: list[str] = []
    risk = _risk_value(labels)

    if risk is None:
        reasons.append("agent:ready with no risk: label")
    elif risk != "low":
        reasons.append(f"agent:ready with risk:{risk} (must be risk:low)")

    body = issue.get("body") or ""
    if _allowed_section_is_stub(body):
        reasons.append(
            f'agent:ready with an unscoped "## Allowed" section (still reads '
            f'"{STUB_MARKER}...")'
        )

    return reasons


def check_issues(issues: list[dict]) -> list[dict[str, Any]]:
    """Run check_issue over a list of issues. Returns a list of violation
    records: {"number", "title", "reasons": [...]}, one per OFFENDING issue
    (compliant issues are omitted)."""
    violations = []
    for issue in issues:
        reasons = check_issue(issue)
        if reasons:
            violations.append({
                "number": issue.get("number"),
                "title": (issue.get("title") or "").strip(),
                "reasons": reasons,
            })
    return violations


def fetch_open_issues(repo: str) -> list[dict]:
    """Fetch all OPEN issues (number, title, labels, body) from `repo` via the
    gh CLI. Raises RuntimeError with a readable message on any failure (auth,
    network, gh missing) — the caller decides how to report that."""
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", repo,
                "--state", "open",
                "--limit", "500",
                "--json", "number,title,labels,body",
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"gh CLI not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh issue list timed out: {exc}") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"gh issue list failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh issue list returned unparseable JSON: {exc}") from exc


def format_report(violations: list[dict[str, Any]], total_checked: int) -> str:
    lines = [
        f"agent:ready invariant check — {total_checked} open issue(s) scanned, "
        f"{len(violations)} violation(s)."
    ]
    if not violations:
        lines.append("No violations. Every agent:ready card is risk:low and scoped.")
        return "\n".join(lines)
    for v in violations:
        lines.append(f"  #{v['number']}  {v['title'][:72]}")
        for reason in v["reasons"]:
            lines.append(f"      - {reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point. This is a REPORT, not a gate: it always exits 0 when the
    check ran to completion (violations found or not) so it is safe to wire
    into loop-tick reporting without ever failing a build/tick on its own.
    It exits 1 ONLY if the check itself could not run (gh missing/auth/network
    failure) — that is a check-infra failure, distinct from a found violation.
    Use --json and inspect the "violations" array length to gate on count,
    if a caller ever wants to."""
    ap = argparse.ArgumentParser(prog="check_agent_ready_invariant.py")
    ap.add_argument("--repo", default=DEFAULT_BOARD_REPO,
                     help=f"board repo (default: {DEFAULT_BOARD_REPO})")
    ap.add_argument("--json", action="store_true",
                     help="emit machine-readable JSON instead of the text report")
    args = ap.parse_args(argv)

    try:
        issues = fetch_open_issues(args.repo)
    except RuntimeError as exc:
        msg = f"check_agent_ready_invariant: could not read board — {exc}"
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(msg, file=sys.stderr)
        return 1

    violations = check_issues(issues)

    if args.json:
        print(json.dumps({
            "repo": args.repo,
            "total_checked": len(issues),
            "violation_count": len(violations),
            "violations": violations,
        }, indent=2))
    else:
        print(format_report(violations, len(issues)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
