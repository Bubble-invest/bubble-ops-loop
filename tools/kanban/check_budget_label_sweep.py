#!/usr/bin/env python3
"""check_budget_label_sweep.py — report OPEN board cards that carry no `budget:`
label, and (only with --apply) back-fill a `budget:unset` marker so they are
visibly flagged instead of silently missing. READ-ONLY by default: it reports,
it does not touch the board (board #569).

Why this matters
-----------------
Every card is SUPPOSED to carry a per-run `budget:$N` label so cost is
attributable from creation (board #537). But that enforcement lives in the
`emit-kanban-task` SKILL, not at the board boundary — so anything that creates a
card via raw `gh`/API (including Rick's own manager loop) bypasses it. The result
is cards with no budget at all, and a missing label is invisible: you cannot
filter for "cards with no budget" when the absence is just... absence.

Joris's budget doctrine is **warn-first, never a hard gate**: a card is never
blocked for lacking a budget. So this is NOT a gate. It is a sweep that:
  1. REPORTS every open card lacking any `budget:` label (default, read-only), and
  2. with --apply ONLY, adds a `budget:unset` label to those cards so the gap is
     honest and filterable — an operator-run action, off by default.

`budget:unset` is a MARKER, not a number. It never fabricates a dollar figure:
the real budget is set later by a human via the emit skill (which replaces the
marker with a real `budget:$N`). An honest "unset" beats an invented number.

What counts as "has a budget"
-----------------------------
Any label whose name starts (case-insensitively) with `budget:` — this includes
both real `budget:$10` labels (emit_kanban_item.sh writes `budget:$N`) and the
`budget:unset` marker this sweep itself applies. So re-running --apply is
idempotent: a card already marked `budget:unset` is no longer "missing".

This script READS via `gh issue list`. It only WRITES when --apply is passed,
and then only ADDS the `budget:unset` label (via `gh issue edit --add-label`);
it never removes a label, changes state, or comments. Mirrors the report-only
default discipline of tools/kanban/check_agent_ready_invariant.py (board #639).

Usage:
    check_budget_label_sweep.py [--repo OWNER/NAME] [--json] [--apply]

Exit code: 0 when the sweep ran to completion (cards found or not) — this is a
REPORT, safe to wire into loop-tick reporting without ever failing a tick. It
exits 1 ONLY if the sweep itself could not run (gh missing/auth/network), or if
--apply was requested and one or more label writes failed — a real failure the
operator needs to see, distinct from "found cards to flag".
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

DEFAULT_BOARD_REPO = "Bubble-invest/bubble-ops-board"
BUDGET_PREFIX = "budget:"
UNSET_LABEL = "budget:unset"
# GitHub label colour (hex, no #). Grey — deliberately NOT the green 0e8a16 that
# emit_kanban_item.sh gives a real budget:$N, so "unset" reads visually distinct.
UNSET_COLOR = "cccccc"
UNSET_DESC = "No per-run budget set on this card (back-filled marker — set a real budget:$N via the emit skill)"


def _label_names(issue: dict) -> list[str]:
    return [(l.get("name") or "").strip() for l in (issue.get("labels") or [])]


def _has_budget_label(labels: list[str]) -> bool:
    """True iff any label name starts (case-insensitively) with `budget:`.
    Covers both real `budget:$N` and the `budget:unset` marker — so a card
    already marked unset is not re-reported, and --apply is idempotent."""
    return any(name.lower().startswith(BUDGET_PREFIX) for name in labels)


def cards_missing_budget(issues: list[dict]) -> list[dict[str, Any]]:
    """Return one record per OPEN issue that has NO `budget:` label:
    {"number", "title"}. Compliant cards are omitted."""
    missing = []
    for issue in issues:
        if not _has_budget_label(_label_names(issue)):
            missing.append({
                "number": issue.get("number"),
                "title": (issue.get("title") or "").strip(),
            })
    return missing


def fetch_open_issues(repo: str) -> list[dict]:
    """Fetch all OPEN issues (number, title, labels) from `repo` via the gh CLI.
    Raises RuntimeError with a readable message on any failure (auth, network,
    gh missing) — the caller decides how to report that."""
    try:
        proc = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", repo,
                "--state", "open",
                "--limit", "500",
                "--json", "number,title,labels",
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


def ensure_unset_label(repo: str) -> None:
    """Create the `budget:unset` label if it does not already exist. Idempotent
    (`--force` upserts). Raises RuntimeError on failure. Only called under
    --apply — a read-only sweep never touches the repo's label set."""
    proc = subprocess.run(
        [
            "gh", "label", "create", UNSET_LABEL,
            "--repo", repo,
            "--color", UNSET_COLOR,
            "--description", UNSET_DESC,
            "--force",
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not create/ensure label {UNSET_LABEL!r} (exit {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )


def apply_unset_label(repo: str, number: int) -> None:
    """Add the `budget:unset` label to one issue. Raises RuntimeError on failure.
    Only ADDS a label — never removes one, never changes state or comments."""
    proc = subprocess.run(
        [
            "gh", "issue", "edit", str(number),
            "--repo", repo,
            "--add-label", UNSET_LABEL,
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"could not add {UNSET_LABEL!r} to #{number} (exit {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )


def format_report(missing: list[dict[str, Any]], total_checked: int, applied: bool) -> str:
    verb = "flagged budget:unset" if applied else "missing a budget: label"
    lines = [
        f"budget-label sweep — {total_checked} open card(s) scanned, "
        f"{len(missing)} {verb}."
    ]
    if not missing:
        lines.append("Every open card carries a budget: label. Nothing to flag.")
        return "\n".join(lines)
    if not applied:
        lines.append("Run with --apply to back-fill budget:unset on these (operator action):")
    for m in missing:
        lines.append(f"  #{m['number']}  {m['title'][:72]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Default is READ-ONLY report: exits 0 when the sweep ran to
    completion (cards found or not), so it is safe in loop-tick reporting and
    never fails a tick on its own. --apply back-fills `budget:unset` on the
    missing cards (creating the label first if absent) — an operator action,
    off by default. Exits 1 only if the sweep could not run (gh missing/auth/
    network) or an --apply write failed."""
    ap = argparse.ArgumentParser(prog="check_budget_label_sweep.py")
    ap.add_argument("--repo", default=DEFAULT_BOARD_REPO,
                     help=f"board repo (default: {DEFAULT_BOARD_REPO})")
    ap.add_argument("--json", action="store_true",
                     help="emit machine-readable JSON instead of the text report")
    ap.add_argument("--apply", action="store_true",
                     help="back-fill budget:unset on cards lacking a budget: label "
                          "(operator action; default is report-only, mutates nothing)")
    args = ap.parse_args(argv)

    try:
        issues = fetch_open_issues(args.repo)
    except RuntimeError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"check_budget_label_sweep: could not read board — {exc}", file=sys.stderr)
        return 1

    missing = cards_missing_budget(issues)

    applied_numbers: list[int] = []
    apply_errors: list[str] = []
    if args.apply and missing:
        try:
            ensure_unset_label(args.repo)
        except RuntimeError as exc:
            if args.json:
                print(json.dumps({"error": str(exc)}))
            else:
                print(f"check_budget_label_sweep: {exc}", file=sys.stderr)
            return 1
        for m in missing:
            try:
                apply_unset_label(args.repo, m["number"])
                applied_numbers.append(m["number"])
            except RuntimeError as exc:
                apply_errors.append(str(exc))

    if args.json:
        out = {
            "repo": args.repo,
            "total_checked": len(issues),
            "missing_count": len(missing),
            "missing": missing,
            "apply": bool(args.apply),
            "applied": applied_numbers,
            "apply_errors": apply_errors,
        }
        print(json.dumps(out, indent=2))
    else:
        print(format_report(missing, len(issues), applied=bool(args.apply)))
        if apply_errors:
            print(f"\n{len(apply_errors)} label write(s) FAILED:", file=sys.stderr)
            for err in apply_errors:
                print(f"  - {err}", file=sys.stderr)

    return 1 if apply_errors else 0


if __name__ == "__main__":
    sys.exit(main())
