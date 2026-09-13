#!/usr/bin/env python3
"""Inventory mandate and mission sources for human intent extraction.

This collector deliberately does not infer intent.  It emits stable provenance
metadata for a human or semantic agent to read and judge separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1
DEFAULT_NON_DEPARTMENT_SLUGS = frozenset({"board", "loop"})


def _git_head(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _source_record(repo: Path, path: Path, source_type: str) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": path.relative_to(repo).as_posix(),
        "source_type": source_type,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "lines": len(data.splitlines()),
    }


def _regular_file(path: Path, repo: Path) -> bool:
    """Accept only ordinary files which resolve inside the department root."""
    if not path.is_file() or path.is_symlink():
        return False
    try:
        path.resolve().relative_to(repo.resolve())
    except ValueError:
        return False
    return True


def supported_sources(repo: Path) -> list[dict[str, object]]:
    """Return the supported source set in deterministic path order.

    The fixed shapes intentionally exclude outputs, vaults, secrets, connector
    configuration, and arbitrary recursive files.
    """
    candidates: list[tuple[Path, str]] = [(repo / "MANDATE.md", "mandate")]
    missions = repo / "missions"
    if missions.is_dir():
        candidates.extend((path, "mission_manifest") for path in missions.glob("*.yaml"))
        candidates.extend((path, "mission_manifest") for path in missions.glob("*.yml"))
        candidates.extend((path, "mission_prompt") for path in missions.glob("*/PROMPT.md"))

    records = [
        _source_record(repo, path, source_type)
        for path, source_type in candidates
        if _regular_file(path, repo)
    ]
    return sorted(records, key=lambda record: str(record["path"]))


def _slug(repo: Path) -> str:
    prefix = "bubble-ops-"
    return repo.name[len(prefix) :] if repo.name.startswith(prefix) else repo.name


def inventory_department(repo: Path) -> dict[str, object]:
    repo = repo.resolve()
    sources = supported_sources(repo)
    mandate_present = any(item["source_type"] == "mandate" for item in sources)
    mission_count = sum(item["source_type"] != "mandate" for item in sources)
    missing: list[str] = []
    if not mandate_present:
        missing.append("MANDATE.md")
    if mission_count == 0:
        missing.append("missions/*.yaml|*.yml or missions/*/PROMPT.md")
    return {
        "department": _slug(repo),
        "repository": repo.name,
        "git_head": _git_head(repo),
        "coverage": {
            "mandate": "present" if mandate_present else "missing",
            "mission_source_count": mission_count,
            "missing": missing,
        },
        "sources": sources,
    }


def discover_departments(root: Path, excluded_slugs: Iterable[str] = ()) -> list[Path]:
    excluded = DEFAULT_NON_DEPARTMENT_SLUGS | frozenset(excluded_slugs)
    return sorted(
        (
            child
            for child in root.iterdir()
            if child.is_dir()
            and child.name.startswith("bubble-ops-")
            and _slug(child) not in excluded
        ),
        key=lambda path: path.name,
    )


def build_inventory(departments: Iterable[Path]) -> dict[str, object]:
    records = sorted(
        (inventory_department(path) for path in departments),
        key=lambda record: str(record["department"]),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "semantics": "unclassified; requires human or agent judgment",
        "departments": records,
        "summary": {
            "department_count": len(records),
            "complete_source_sets": sum(
                not record["coverage"]["missing"] for record in records  # type: ignore[index]
            ),
            "departments_with_missing_sources": [
                record["department"]
                for record in records
                if record["coverage"]["missing"]  # type: ignore[index]
            ],
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root", type=Path, help="Parent containing bubble-ops-* repositories")
    group.add_argument(
        "--department",
        action="append",
        type=Path,
        help="Explicit department repository path; repeat as needed",
    )
    parser.add_argument(
        "--exclude-slug",
        action="append",
        default=[],
        help="Department slug to omit when using --root; repeat as needed",
    )
    parser.add_argument("--output", type=Path, help="Write JSON to this path instead of stdout")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    departments = (
        discover_departments(args.root, args.exclude_slug)
        if args.root is not None
        else args.department
    )
    rendered = json.dumps(build_inventory(departments), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
