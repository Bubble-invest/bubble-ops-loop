#!/usr/bin/env python3
"""Inventory board-card and PR links to the live operator-intent collection.

The collector is deliberately structural.  It can prove that a card or PR has
no usable intent link (an orphan), but it cannot prove semantic alignment or a
contradiction.  Those decisions belong to the manager agent after it reads the
work item and the resolved intent chain.

All modes are read-only by default.  ``--mode labels --apply`` is the only
write path and merely creates missing ``intent:<slug>`` labels; it never edits
issues, pull requests, or ``shared/operator-intents/**``.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


DEFAULT_BOARD = "Bubble-invest/bubble-ops-board"
DEFAULT_PR_OWNERS = ("Bubble-invest", "vdk888")
DEFAULT_INTENT_REPO = "vdk888/bubble-shared-wiki"
INTENT_DIR = "shared/operator-intents"
LABEL_COLOR = "1d76db"
LABEL_DESCRIPTION_PREFIX = "Operator intent: "

WIKILINK_RE = re.compile(
    r"\[\[shared/operator-intents/([A-Za-z0-9][A-Za-z0-9_-]*)\]\]"
)
ANY_INTENT_WIKILINK_RE = re.compile(r"\[\[shared/operator-intents/([^\]]+)\]\]")
INTENT_LABEL_RE = re.compile(r"^intent:([A-Za-z0-9._-]+)$", re.IGNORECASE)
BODY_LINE_RE = re.compile(r"^\s*Serves-intent\(s\):\s*(.*)$", re.IGNORECASE | re.MULTILINE)
BODY_LABEL_RE = re.compile(r"(?<![\w-])intent:([A-Za-z0-9._-]+)", re.IGNORECASE)
FULL_BOARD_REF_RE = re.compile(
    r"(?:https://github\.com/|https://www\.github\.com/)?"
    r"Bubble-invest/bubble-ops-board(?:/issues/|#)(\d+)",
    re.IGNORECASE,
)
CLOSING_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(.*)$",
    re.IGNORECASE,
)
BARE_REF_RE = re.compile(r"#(\d+)")
FRONTMATTER_LINK_FIELDS = {
    "parent",
    "parents",
    "children",
    "components",
    "relationships",
    "related",
    "dependency",
    "dependencies",
}


@dataclass
class Intent:
    slug: str
    title: str
    status: str
    path: str
    body: str
    edges: set[str] = field(default_factory=set)

    @property
    def active(self) -> bool:
        return self.status.lower() != "superseded"


class CommandError(RuntimeError):
    """A required read or explicitly requested write failed."""


def _run_json(args: list[str], timeout: int = 90) -> Any:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise CommandError(f"command not available: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"command timed out: {' '.join(args[:4])}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise CommandError(f"command failed ({proc.returncode}): {detail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CommandError(f"command returned invalid JSON: {exc}") from exc


def _frontmatter(text: str) -> dict[str, Any]:
    """Parse the small YAML subset used by intent relationship metadata.

    This intentionally avoids making PyYAML a runtime dependency.  Scalar
    fields and block/inline lists are sufficient for the #1248 parent,
    children, components, and relationship convention.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}

    data: dict[str, Any] = {}
    current: str | None = None
    for raw in lines[1:end]:
        if raw.startswith((" ", "\t")) and current and (
            raw.strip().startswith("-") or current in FRONTMATTER_LINK_FIELDS
        ):
            value = raw.strip()
            if value.startswith("-"):
                value = value[1:].strip()
            value = value.strip('"\'')
            existing = data.setdefault(current, [])
            if not isinstance(existing, list):
                existing = data[current] = [existing]
            existing.append(value)
            continue
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", raw)
        if not match:
            current = None
            continue
        key, value = match.group(1), match.group(2).strip()
        current = key
        if value == "":
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [
                item.strip().strip('"\'')
                for item in value[1:-1].split(",")
                if item.strip()
            ]
        else:
            data[key] = value.strip('"\'')
    return data


def _links(value: Any) -> set[str]:
    if isinstance(value, list):
        raw = "\n".join(str(item) for item in value)
    else:
        raw = str(value or "")
    return set(WIKILINK_RE.findall(raw))


def parse_intent_documents(documents: dict[str, str]) -> dict[str, Intent]:
    """Build the active taxonomy and hierarchy graph from actual documents."""
    intents: dict[str, Intent] = {}
    metadata_by_slug: dict[str, dict[str, Any]] = {}
    for filename, text in sorted(documents.items()):
        if not filename.endswith(".md") or filename in {"README.md", "TEMPLATE.md"}:
            continue
        slug = filename[:-3]
        metadata = _frontmatter(text)
        metadata_by_slug[slug] = metadata
        title = str(metadata.get("title") or slug).strip()
        status = str(metadata.get("status") or "unspecified").strip()
        intents[slug] = Intent(
            slug=slug,
            title=title,
            status=status,
            path=f"{INTENT_DIR}/{filename}",
            body=text,
        )

    # Relationships form a traversable chain.  Parent/child links are made
    # reciprocal in memory even when a live document only carries one side.
    for slug, metadata in metadata_by_slug.items():
        for key in FRONTMATTER_LINK_FIELDS:
            for target in _links(metadata.get(key)):
                if target in intents and target != slug:
                    intents[slug].edges.add(target)
                    intents[target].edges.add(slug)
    return intents


def load_intents_from_root(wiki_root: Path) -> dict[str, Intent]:
    directory = wiki_root / INTENT_DIR
    if not directory.is_dir():
        raise CommandError(f"operator-intents directory not found: {directory}")
    documents = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.md"))
        if path.is_file() and not path.is_symlink()
    }
    return parse_intent_documents(documents)


def load_intents_from_github(repo: str = DEFAULT_INTENT_REPO) -> dict[str, Intent]:
    """Read the current main collection without cloning or modifying it."""
    listing = _run_json(["gh", "api", f"repos/{repo}/contents/{INTENT_DIR}?ref=main"])
    documents: dict[str, str] = {}
    for entry in listing:
        name = str(entry.get("name") or "")
        if not name.endswith(".md"):
            continue
        payload = _run_json(
            ["gh", "api", f"repos/{repo}/contents/{INTENT_DIR}/{name}?ref=main"]
        )
        try:
            documents[name] = base64.b64decode(payload["content"]).decode("utf-8")
        except (KeyError, ValueError, UnicodeDecodeError) as exc:
            raise CommandError(f"could not decode live intent document {name}: {exc}") from exc
    return parse_intent_documents(documents)


def label_names(item: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for label in item.get("labels") or []:
        value = label.get("name") if isinstance(label, dict) else label
        if value:
            names.append(str(value))
    return names


def extract_direct_intents(
    item: dict[str, Any],
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    from_labels = {
        match.group(1)
        for label in label_names(item)
        if (match := INTENT_LABEL_RE.fullmatch(label.strip()))
    }
    from_body: set[str] = set()
    malformed: set[str] = set()
    for match in BODY_LINE_RE.finditer(str(item.get("body") or "")):
        value = match.group(1)
        from_body.update(WIKILINK_RE.findall(value))
        from_body.update(BODY_LABEL_RE.findall(value))
        for raw_target in ANY_INTENT_WIKILINK_RE.findall(value):
            if not WIKILINK_RE.fullmatch(
                f"[[shared/operator-intents/{raw_target}]]"
            ):
                malformed.add(raw_target)
    direct = sorted(from_labels | from_body)
    return (
        direct,
        {"labels": sorted(from_labels), "body": sorted(from_body)},
        sorted(malformed),
    )


def intent_chain(slugs: Iterable[str], intents: dict[str, Intent]) -> list[str]:
    """Return the active connected chain reachable from direct intent links."""
    seen: set[str] = set()
    queue = deque(slug for slug in slugs if slug in intents and intents[slug].active)
    while queue:
        slug = queue.popleft()
        if slug in seen:
            continue
        seen.add(slug)
        for target in sorted(intents[slug].edges):
            if target not in seen and intents[target].active:
                queue.append(target)
    return sorted(seen)


def closing_board_cards(pr: dict[str, Any], board: str) -> list[int]:
    """Resolve explicit closing references to board cards.

    Cross-repo PRs must use the full ``Bubble-invest/bubble-ops-board#N`` or
    board-issue URL form.  Bare ``Closes #N`` is only a board link when the PR
    itself lives in the board repository; otherwise it closes an issue in the
    PR's own repository and must not be mis-attributed.
    """
    repo_value = pr.get("repository") or {}
    pr_repo = (
        repo_value.get("nameWithOwner")
        if isinstance(repo_value, dict)
        else str(repo_value)
    ) or str(pr.get("repo") or "")
    found: set[int] = set()
    for line in str(pr.get("body") or "").splitlines():
        closing = CLOSING_LINE_RE.match(line)
        if not closing:
            continue
        tail = closing.group(1)
        found.update(int(number) for number in FULL_BOARD_REF_RE.findall(tail))
        if pr_repo.lower() == board.lower():
            found.update(int(number) for number in BARE_REF_RE.findall(tail))
    return sorted(found)


def _alignment_record(
    item: dict[str, Any], intents: dict[str, Intent], inherited: Iterable[str] = ()
) -> dict[str, Any]:
    direct, sources, malformed = extract_direct_intents(item)
    inherited_set = set(inherited)
    all_references = sorted(set(direct) | inherited_set)
    resolved = sorted(
        slug for slug in all_references if slug in intents and intents[slug].active
    )
    invalid = sorted(slug for slug in all_references if slug not in intents)
    superseded = sorted(
        slug for slug in all_references if slug in intents and not intents[slug].active
    )
    chain = intent_chain(resolved, intents)
    return {
        "direct_intents": direct,
        "direct_sources": sources,
        "inherited_intents": sorted(inherited_set),
        "resolved_intents": resolved,
        "intent_chain": chain,
        "invalid_intents": invalid,
        "superseded_intents": superseded,
        "malformed_intents": malformed,
        "orphan": not bool(chain),
        # Explicitly unscored: a manager agent must read the proposal and docs.
        "contradiction": None,
        "judgment": "agent_required",
    }


def build_report(
    intents: dict[str, Intent],
    issues: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    board: str = DEFAULT_BOARD,
    limit: int | None = None,
    pr_owner_counts: dict[str, int] | None = None,
    linked_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    issue_rows: list[dict[str, Any]] = []
    issue_by_number: dict[int, dict[str, Any]] = {}
    for issue in issues:
        alignment = _alignment_record(issue, intents)
        row = {
            "number": issue.get("number"),
            "title": str(issue.get("title") or ""),
            "url": issue.get("url"),
            "created_at": issue.get("createdAt"),
            "body": str(issue.get("body") or ""),
            **alignment,
        }
        issue_rows.append(row)
        if isinstance(issue.get("number"), int):
            issue_by_number[issue["number"]] = row

    # A PR can legitimately link a closed/non-open board card.  Those cards are
    # lookup-only: they contribute inheritance but are not added to the open
    # card inventory or summary.
    for issue in linked_issues or []:
        number = issue.get("number")
        if not isinstance(number, int) or number in issue_by_number:
            continue
        issue_by_number[number] = {
            "number": number,
            **_alignment_record(issue, intents),
        }

    pr_rows: list[dict[str, Any]] = []
    for pr in prs:
        linked_cards = closing_board_cards(pr, board)
        inherited = {
            slug
            for number in linked_cards
            for slug in issue_by_number.get(number, {}).get("resolved_intents", [])
        }
        alignment = _alignment_record(pr, intents, inherited)
        repo_value = pr.get("repository") or {}
        repo = (
            repo_value.get("nameWithOwner")
            if isinstance(repo_value, dict)
            else str(repo_value)
        ) or str(pr.get("repo") or "")
        missing_cards = [number for number in linked_cards if number not in issue_by_number]
        pr_rows.append(
            {
                "repo": repo,
                "number": pr.get("number"),
                "title": str(pr.get("title") or ""),
                "url": pr.get("url"),
                "created_at": pr.get("createdAt"),
                "body": str(pr.get("body") or ""),
                "linked_board_cards": linked_cards,
                "linked_cards_unresolved": missing_cards,
                **alignment,
            }
        )

    issue_rows.sort(key=lambda row: int(row.get("number") or 0))
    pr_rows.sort(key=lambda row: (str(row.get("repo") or ""), int(row.get("number") or 0)))
    active = [intent for intent in intents.values() if intent.active]
    superseded = [intent for intent in intents.values() if not intent.active]
    coverage_warnings: list[str] = []
    if limit is not None and len(issue_rows) >= limit:
        coverage_warnings.append(
            f"open issue query reached --limit={limit}; increase it before treating this as complete"
        )
    if limit is not None:
        for owner, count in sorted((pr_owner_counts or {}).items()):
            if count >= limit:
                coverage_warnings.append(
                    f"open PR query for {owner} reached --limit={limit}; "
                    "increase it before treating this as complete"
                )
    return {
        "schema_version": 1,
        "policy": {
            "missing_intent": "FLAG_ORPHAN_NEVER_BLOCK",
            "contradiction": "AGENT_JUDGMENT_SURFACE_NEEDS_HUMAN",
            "intent_collection": "READ_ONLY",
        },
        "taxonomy": [
            {
                "slug": intent.slug,
                "label": f"intent:{intent.slug}",
                "title": intent.title,
                "status": intent.status,
                "path": intent.path,
                "relationships": sorted(intent.edges),
            }
            for intent in sorted(active, key=lambda value: value.slug)
        ],
        "superseded_taxonomy": [
            {"slug": intent.slug, "status": intent.status, "path": intent.path}
            for intent in sorted(superseded, key=lambda value: value.slug)
        ],
        "summary": {
            "open_issues": len(issue_rows),
            "orphan_issues": sum(bool(row["orphan"]) for row in issue_rows),
            "open_prs": len(pr_rows),
            "orphan_prs": sum(bool(row["orphan"]) for row in pr_rows),
        },
        "coverage": {
            "complete": not coverage_warnings,
            "requested_limit": limit,
            "warnings": coverage_warnings,
        },
        "issues": issue_rows,
        "pull_requests": pr_rows,
    }


def fetch_open_issues(board: str, limit: int) -> list[dict[str, Any]]:
    return _run_json(
        [
            "gh", "issue", "list", "--repo", board, "--state", "open",
            "--limit", str(limit), "--json",
            "number,title,body,url,labels,createdAt",
        ]
    )


def fetch_open_prs(
    owners: Iterable[str], limit: int
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Fetch and URL-dedupe open PRs for each explicit GitHub owner scope."""
    by_url: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for owner in owners:
        rows = _run_json(
            [
                "gh", "search", "prs", "--owner", owner, "--state", "open",
                "--limit", str(limit), "--json",
                "number,title,body,url,repository,labels,createdAt",
            ]
        )
        counts[owner] = len(rows)
        for row in rows:
            key = str(row.get("url") or f"{owner}:{row.get('number')}:{row.get('title')}")
            by_url[key] = row
    return list(by_url.values()), counts


def fetch_linked_board_issues(
    board: str,
    prs: Iterable[dict[str, Any]],
    open_issues: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[int]]:
    """Read closed/non-open board cards referenced by open PR closing lines."""
    open_numbers = {
        row.get("number") for row in open_issues if isinstance(row.get("number"), int)
    }
    wanted = sorted(
        {
            number
            for pr in prs
            for number in closing_board_cards(pr, board)
            if number not in open_numbers
        }
    )
    found: list[dict[str, Any]] = []
    unresolved: list[int] = []
    for number in wanted:
        try:
            found.append(
                _run_json(
                    [
                        "gh", "issue", "view", str(number), "--repo", board,
                        "--json", "number,title,body,url,labels,createdAt,state",
                    ]
                )
            )
        except CommandError:
            unresolved.append(number)
    return found, unresolved


def backfill_batch(
    report: dict[str, Any], batch_size: int, offset: int = 0
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for kind, rows in (
        ("issue", report["issues"]),
        ("pull_request", report["pull_requests"]),
    ):
        for row in rows:
            if not row["orphan"]:
                continue
            candidates.append(
                {
                    "kind": kind,
                    "repo": DEFAULT_BOARD if kind == "issue" else row.get("repo"),
                    "number": row.get("number"),
                    "title": row.get("title"),
                    "url": row.get("url"),
                    "body": row.get("body"),
                    "suggested_intents": [],
                    "decision": "agent_required",
                    "instruction": (
                        "Read this work item and the available intent documents; propose one "
                        "or more semantically justified links, or leave unresolved with a reason."
                    ),
                }
            )
    candidates.sort(
        key=lambda row: (0 if row["kind"] == "issue" else 1, int(row.get("number") or 0))
    )
    total = len(candidates)
    if total:
        start = offset % total
        selected_count = min(batch_size, total)
        end = start + selected_count
        selected = candidates[start:end] + candidates[: max(0, end - total)]
        next_offset = end % total
        wrapped = end >= total
    else:
        start = 0
        selected_count = 0
        selected = []
        next_offset = 0
        wrapped = False
    return {
        "schema_version": 1,
        "mode": "backfill_proposal",
        "mutation": False,
        "batch_size": batch_size,
        "coverage": report["coverage"],
        "selection": {
            "requested_offset": offset,
            "offset": start,
            "total_candidates": total,
            "selected_count": selected_count,
            "next_offset": next_offset,
            "wrapped": wrapped,
        },
        "remaining_after_batch": max(0, total - selected_count),
        "taxonomy": report["taxonomy"],
        "candidates": selected,
    }


def fetch_label_names(board: str) -> set[str]:
    rows = _run_json(
        ["gh", "label", "list", "--repo", board, "--limit", "1000", "--json", "name"]
    )
    return {str(row.get("name") or "") for row in rows}


def label_plan(intents: dict[str, Intent], existing: set[str]) -> dict[str, Any]:
    wanted = sorted(f"intent:{slug}" for slug, intent in intents.items() if intent.active)
    existing_by_fold: dict[str, list[str]] = {}
    for label in existing:
        existing_by_fold.setdefault(label.casefold(), []).append(label)
    wanted_folds = {label.casefold() for label in wanted}
    casing_drift = []
    canonical_existing = []
    missing = []
    for label in wanted:
        matches = sorted(existing_by_fold.get(label.casefold(), []))
        if not matches:
            missing.append(label)
            continue
        canonical_existing.append(label)
        casing_drift.extend(
            {"expected": label, "actual": actual}
            for actual in matches
            if actual != label
        )
    return {
        "mode": "label_taxonomy",
        "mutation": False,
        "wanted": wanted,
        "existing": canonical_existing,
        "missing": missing,
        "noncanonical_casing": casing_drift,
        "obsolete_not_removed": sorted(
            label
            for label in existing
            if label.casefold().startswith("intent:") and label.casefold() not in wanted_folds
        ),
    }


def apply_label_plan(board: str, plan: dict[str, Any]) -> None:
    for label in plan["missing"]:
        slug = label.split(":", 1)[1]
        proc = subprocess.run(
            [
                "gh", "label", "create", label, "--repo", board,
                "--color", LABEL_COLOR,
                "--description", f"{LABEL_DESCRIPTION_PREFIX}{slug}",
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            raise CommandError(
                f"could not create label {label}: {proc.stderr.strip() or proc.stdout.strip()}"
            )
    plan["mutation"] = True
    plan["created"] = list(plan["missing"])


def _text_report(report: dict[str, Any]) -> str:
    if report.get("mode") == "label_taxonomy":
        lines = ["intent label taxonomy (dry-run)" if not report["mutation"] else "intent labels applied"]
        lines.extend(f"  missing: {label}" for label in report["missing"])
        lines.extend(
            f"  casing drift: {item['actual']} (canonical {item['expected']})"
            for item in report["noncanonical_casing"]
        )
        if not report["missing"]:
            lines.append("  all live intent labels already exist")
        return "\n".join(lines)
    if report.get("mode") == "backfill_proposal":
        selection = report["selection"]
        lines = [
            f"intent backfill proposal: {len(report['candidates'])} candidate(s), "
            f"{report['remaining_after_batch']} remaining; offset={selection['offset']} "
            f"next_offset={selection['next_offset']}"
        ]
        lines.extend(
            f"  {row['kind']} {row['repo']}#{row['number']} {row['title']}"
            for row in report["candidates"]
        )
        lines.extend(f"  COVERAGE WARNING: {warning}" for warning in report["coverage"]["warnings"])
        return "\n".join(lines)
    summary = report["summary"]
    lines = [
        f"intent alignment inventory: {summary['open_issues']} cards "
        f"({summary['orphan_issues']} ORPHAN), {summary['open_prs']} PRs "
        f"({summary['orphan_prs']} ORPHAN)",
        "Contradiction is not mechanically scored; the manager agent must judge it.",
    ]
    for row in report["issues"]:
        if row["orphan"]:
            lines.append(f"  ORPHAN card #{row['number']} {row['title']}")
    for row in report["pull_requests"]:
        if row["orphan"]:
            lines.append(f"  ORPHAN PR {row['repo']}#{row['number']} {row['title']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intent_alignment_check.py")
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument(
        "--org", action="append", dest="orgs",
        help=(
            "GitHub owner whose open PRs are inventoried; repeatable. "
            "Default: Bubble-invest and vdk888"
        ),
    )
    parser.add_argument("--intent-repo", default=DEFAULT_INTENT_REPO)
    parser.add_argument("--wiki-root", type=Path)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--mode", choices=("audit", "backfill", "labels"), default="audit")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument(
        "--backfill-offset", type=int, default=0,
        help="rotation offset for --mode backfill; persist returned selection.next_offset externally",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="only valid with --mode labels; creates missing labels and changes nothing else",
    )
    args = parser.parse_args(argv)
    if args.limit < 1 or args.batch_size < 1:
        parser.error("--limit and --batch-size must be positive")
    if args.backfill_offset < 0:
        parser.error("--backfill-offset must be nonnegative")
    if args.apply and args.mode != "labels":
        parser.error("--apply is only valid with --mode labels")

    try:
        intents = (
            load_intents_from_root(args.wiki_root)
            if args.wiki_root
            else load_intents_from_github(args.intent_repo)
        )
        if args.mode == "labels":
            output = label_plan(intents, fetch_label_names(args.board))
            if args.apply:
                apply_label_plan(args.board, output)
        else:
            pr_rows, pr_owner_counts = fetch_open_prs(
                args.orgs or DEFAULT_PR_OWNERS, args.limit
            )
            issue_rows = fetch_open_issues(args.board, args.limit)
            linked_issues, unresolved_link_reads = fetch_linked_board_issues(
                args.board, pr_rows, issue_rows
            )
            report = build_report(
                intents,
                issue_rows,
                pr_rows,
                args.board,
                args.limit,
                pr_owner_counts,
                linked_issues,
            )
            if unresolved_link_reads:
                report["coverage"]["complete"] = False
                report["coverage"]["warnings"].append(
                    "could not read linked board card(s): "
                    + ", ".join(f"#{number}" for number in unresolved_link_reads)
                )
            output = (
                backfill_batch(report, args.batch_size, args.backfill_offset)
                if args.mode == "backfill"
                else report
            )
    except CommandError as exc:
        print(f"intent_alignment_check: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(output, indent=2) if args.format == "json" else _text_report(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
