#!/usr/bin/env python3
"""Collect structural evidence about shared-wiki intent frontmatter.

This tool deliberately does not decide whether a page is relevant to an
operator intent. It reports frontmatter shape and target resolution only; the
cloud-wiki-compile agent makes the semantic judgment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable


CORE_DIR = PurePosixPath("shared/operator-intents")
NON_CONTENT_PREFIXES = (PurePosixPath(".github"), PurePosixPath("hooks"))
WIKILINK_RE = re.compile(r"^\[\[([^\[\]|#]+)\]\]$")
FIELD_RE = re.compile(r"^intent\s*:\s*(.*?)\s*$")
LIST_ITEM_RE = re.compile(r"^\s+-\s*(.*?)\s*$")


def _is_below(path: PurePosixPath, parent: PurePosixPath) -> bool:
    return path == parent or parent in path.parents


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _is_quoted(value: str) -> bool:
    return len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}


def _frontmatter(text: str) -> tuple[list[str] | None, str | None]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "missing_frontmatter"
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], None
    return None, "unterminated_frontmatter"


def _intent_values(lines: list[str]) -> tuple[list[str], list[str]]:
    """Return raw intent values and shape issues from a frontmatter block."""

    fields = [index for index, line in enumerate(lines) if FIELD_RE.match(line)]
    if not fields:
        return [], ["missing_intent"]
    if len(fields) > 1:
        return [], ["duplicate_intent"]

    index = fields[0]
    match = FIELD_RE.match(lines[index])
    assert match is not None
    inline = match.group(1).strip()
    if inline:
        if inline == "[]":
            return [], ["empty_intent"]
        if inline.startswith("[") and not inline.startswith("[["):
            return [], ["unsupported_inline_list"]
        value = _unquote(inline)
        if not value:
            return [], ["empty_intent"]
        return [value], ([] if _is_quoted(inline) else ["unquoted_intent"])

    values: list[str] = []
    issues: list[str] = []
    cursor = index + 1
    while cursor < len(lines):
        line = lines[cursor]
        if not line.strip():
            cursor += 1
            continue
        item = LIST_ITEM_RE.match(line)
        if not item:
            break
        raw_value = item.group(1)
        value = _unquote(raw_value)
        if value:
            values.append(value)
            if not _is_quoted(raw_value):
                issues.append("unquoted_intent")
        cursor += 1
    return (values, issues) if values else ([], ["empty_intent"])


def _case_exact_file(root: Path, relative: PurePosixPath) -> Path | None:
    current = root
    for part in relative.parts:
        try:
            if part not in {entry.name for entry in current.iterdir()}:
                return None
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return None
        current = current / part
    if not current.is_file():
        return None
    try:
        current.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return current


def _target_issue(wiki_root: Path, value: str) -> tuple[str | None, str | None]:
    match = WIKILINK_RE.fullmatch(value)
    if not match:
        return None, "malformed_intent"

    target = match.group(1)
    target_path = PurePosixPath(target)
    if target_path.is_absolute() or ".." in target_path.parts:
        return target, "intent_target_outside_core"
    if target_path.suffix:
        return target, "intent_target_must_omit_extension"
    if not _is_below(target_path, CORE_DIR) or target_path == CORE_DIR:
        return target, "intent_target_outside_core"

    target_file = PurePosixPath(f"{target}.md")
    if _case_exact_file(wiki_root, target_file) is None:
        return target, "intent_target_missing"
    return target, None


def _iter_pages(wiki_root: Path) -> Iterable[Path]:
    for page in sorted(wiki_root.rglob("*.md")):
        relative = PurePosixPath(page.relative_to(wiki_root).as_posix())
        if _is_below(relative, CORE_DIR):
            continue  # The operator-intent pages are roots, not children.
        if any(_is_below(relative, prefix) for prefix in NON_CONTENT_PREFIXES):
            continue
        if any(part.startswith(".") for part in relative.parts):
            continue
        yield page


def audit(wiki_root: Path) -> dict[str, object]:
    wiki_root = wiki_root.resolve()
    candidates: list[dict[str, object]] = []
    scanned = 0
    linked = 0

    for page in _iter_pages(wiki_root):
        scanned += 1
        relative = page.relative_to(wiki_root).as_posix()
        frontmatter, frontmatter_issue = _frontmatter(
            page.read_text(encoding="utf-8", errors="replace")
        )
        issues: list[str] = []
        targets: list[str] = []
        if frontmatter_issue:
            issues.append(frontmatter_issue)
        else:
            assert frontmatter is not None
            values, value_issues = _intent_values(frontmatter)
            issues.extend(value_issues)
            for value in values:
                target, target_issue = _target_issue(wiki_root, value)
                if target:
                    targets.append(target)
                if target_issue:
                    issues.append(target_issue)

        if issues:
            candidates.append(
                {
                    "path": relative,
                    "issues": sorted(set(issues)),
                    "intent_targets": targets,
                }
            )
        else:
            linked += 1

    issue_counts = Counter(
        issue for candidate in candidates for issue in candidate["issues"]
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "wiki_root": str(wiki_root),
        "contract": {
            "scalar": 'intent: "[[shared/operator-intents/<slug>]]"',
            "multiple": "quoted block list",
            "unresolved": "missing, empty, or [] remains a candidate",
            "semantics": "agent judgment required; this report is structural evidence only",
        },
        "summary": {
            "pages_scanned": scanned,
            "pages_structurally_linked": linked,
            "candidate_pages": len(candidates),
            "issue_counts": dict(sorted(issue_counts.items())),
        },
        "candidates": candidates,
    }


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki", type=Path, required=True, help="shared-wiki root")
    parser.add_argument("--output", type=Path, help="atomically write JSON here")
    args = parser.parse_args()

    if not args.wiki.is_dir():
        parser.error(f"wiki root is not a directory: {args.wiki}")
    report = audit(args.wiki)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        _atomic_write(args.output, payload)
    else:
        print(payload, end="")
    summary = report["summary"]
    print(
        "wiki_intent_audit: "
        f"scanned={summary['pages_scanned']} "
        f"linked={summary['pages_structurally_linked']} "
        f"candidates={summary['candidate_pages']}",
        file=os.sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
