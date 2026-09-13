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
from pathlib import PurePosixPath
from typing import Iterable, Mapping

import yaml

SCHEMA_VERSION = 2
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


def _source_record(
    relative_path: str,
    source_type: str,
    data: bytes,
    provenance_mode: str,
) -> dict[str, object]:
    return {
        "path": relative_path,
        "source_type": source_type,
        "provenance_mode": provenance_mode,
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


def _source_type(relative_path: str) -> str | None:
    path = PurePosixPath(relative_path)
    if path.parts == ("dept.yaml",):
        return "department_manifest"
    if path.parts == ("MANDATE.md",):
        return "mandate"
    if len(path.parts) == 2 and path.parts[0] == "missions" and path.suffix in {".yaml", ".yml"}:
        return "mission_manifest"
    if len(path.parts) == 2 and path.parts[0] == "missions" and path.suffix == ".md":
        return "mission_document"
    if len(path.parts) == 3 and path.parts[0] == "missions" and path.parts[2] == "PROMPT.md":
        return "mission_prompt"
    return None


def _git_sources(repo: Path, revision: str) -> list[dict[str, object]]:
    tree = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", revision, "--", "dept.yaml", "MANDATE.md", "missions"],
        check=True,
        capture_output=True,
    ).stdout
    records: list[dict[str, object]] = []
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode = metadata.split(b" ", 1)[0]
        if mode not in {b"100644", b"100755"}:
            continue
        relative_path = raw_path.decode("utf-8", errors="strict")
        source_type = _source_type(relative_path)
        if source_type is None:
            continue
        data = subprocess.run(
            ["git", "-C", str(repo), "show", f"{revision}:{relative_path}"],
            check=True,
            capture_output=True,
        ).stdout
        records.append(_source_record(relative_path, source_type, data, "git_commit"))
    return sorted(records, key=lambda record: str(record["path"]))


def _worktree_sources(repo: Path) -> list[dict[str, object]]:
    """Return the supported source set in deterministic path order.

    The fixed shapes intentionally exclude outputs, vaults, secrets, connector
    configuration, and arbitrary recursive files.
    """
    candidates: list[tuple[Path, str]] = [
        (repo / "dept.yaml", "department_manifest"),
        (repo / "MANDATE.md", "mandate"),
    ]
    missions = repo / "missions"
    if missions.is_dir():
        candidates.extend((path, "mission_manifest") for path in missions.glob("*.yaml"))
        candidates.extend((path, "mission_manifest") for path in missions.glob("*.yml"))
        candidates.extend((path, "mission_document") for path in missions.glob("*.md"))
        candidates.extend((path, "mission_prompt") for path in missions.glob("*/PROMPT.md"))

    records = [
        _source_record(
            path.relative_to(repo).as_posix(),
            source_type,
            path.read_bytes(),
            "unversioned_worktree",
        )
        for path, source_type in candidates
        if _regular_file(path, repo)
    ]
    return sorted(records, key=lambda record: str(record["path"]))


def supported_sources(repo: Path, git_head: str | None = None) -> list[dict[str, object]]:
    """Read exact commit blobs when possible, otherwise an explicit worktree snapshot."""
    return _git_sources(repo, git_head) if git_head else _worktree_sources(repo)


def _exact_file(repo: Path, relative_path: str, git_head: str | None) -> bytes | None:
    if git_head:
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{git_head}:{relative_path}"],
            check=False,
            capture_output=True,
        )
        return result.stdout if result.returncode == 0 else None
    path = repo / relative_path
    return path.read_bytes() if _regular_file(path, repo) else None


def _tree_paths(repo: Path, git_head: str | None) -> set[str]:
    if git_head:
        output = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--name-only", git_head],
            check=True,
            capture_output=True,
        ).stdout
        return {item.decode("utf-8", errors="strict") for item in output.split(b"\0") if item}
    return {
        path.relative_to(repo).as_posix()
        for path in repo.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def standard_shape(repo: Path, git_head: str | None) -> dict[str, object]:
    paths = _tree_paths(repo, git_head)
    layers = {str(number): f"layers/{number}/PROMPT.md" in paths for number in range(1, 5)}
    missing = []
    if "dept.yaml" not in paths:
        missing.append("dept.yaml")
    if "MANDATE.md" not in paths:
        missing.append("MANDATE.md")
    missing.extend(f"layers/{number}/PROMPT.md" for number, present in layers.items() if not present)
    missions_present = any(path.startswith("missions/") for path in paths)
    if not missions_present:
        missing.append("missions/")
    return {
        "dept_yaml": "present" if "dept.yaml" in paths else "missing",
        "mandate": "present" if "MANDATE.md" in paths else "missing",
        "layers": {key: "present" if value else "missing" for key, value in layers.items()},
        "missions_directory": "present" if missions_present else "missing",
        "complete": not missing,
        "missing": missing,
    }


def declared_identity(repo: Path, git_head: str | None) -> dict[str, object] | None:
    data = _exact_file(repo, "dept.yaml", git_head)
    if data is None:
        return None
    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return {"error": "dept.yaml could not be parsed"}
    department = raw.get("department") if isinstance(raw, dict) else None
    if not isinstance(department, dict):
        return {"error": "dept.yaml has no department mapping"}
    return {
        "slug": department.get("slug"),
        "display_name": department.get("display_name"),
        "persona": department.get("persona"),
        "host": department.get("host"),
        "recurring_mission_count": len(raw.get("recurring_missions", []))
        if isinstance(raw.get("recurring_missions"), list)
        else 0,
        "source": _source_record(
            "dept.yaml",
            "department_manifest",
            data,
            "git_commit" if git_head else "unversioned_worktree",
        ),
    }


def _slug(repo: Path) -> str:
    prefix = "bubble-ops-"
    return repo.name[len(prefix) :] if repo.name.startswith(prefix) else repo.name


def inventory_department(
    repo: Path,
    agent: Mapping[str, object] | None = None,
) -> dict[str, object]:
    repo = repo.resolve()
    git_head = _git_head(repo)
    sources = supported_sources(repo, git_head)
    identity = declared_identity(repo, git_head)
    mandate_present = any(item["source_type"] == "mandate" for item in sources)
    mission_file_count = sum(str(item["source_type"]).startswith("mission_") for item in sources)
    inline_missions = (
        int(identity.get("recurring_mission_count", 0))
        if isinstance(identity, dict)
        else 0
    )
    mission_count = mission_file_count + (1 if inline_missions else 0)
    missing: list[str] = []
    if not mandate_present:
        missing.append("MANDATE.md")
    if mission_count == 0:
        missing.append("missions/*.yaml|*.yml|*.md or missions/*/PROMPT.md")
    record: dict[str, object] = {
        "department": (
            identity.get("slug")
            if isinstance(identity, dict) and identity.get("slug")
            else agent["id"] if agent is not None else _slug(repo)
        ),
        "repository": repo.name,
        "git_head": git_head,
        "source_snapshot": {
            "mode": "git_commit" if git_head else "unversioned_worktree",
            "revision": git_head,
        },
        "standard_shape": standard_shape(repo, git_head),
        "declared_identity": identity,
        "coverage": {
            "mandate": "present" if mandate_present else "missing",
            "mission_source_count": mission_count,
            "mission_file_count": mission_file_count,
            "inline_recurring_mission_count": inline_missions,
            "missing": missing,
        },
        "sources": sources,
    }
    if agent is not None:
        record["agent"] = {
            "id": agent["id"],
            "display_name": agent["name"],
            "role": agent["role"],
            "host": agent["host"],
        }
        declared = record["declared_identity"]
        mismatches: list[str] = []
        if isinstance(declared, dict) and declared.get("display_name") not in {None, agent["name"]}:
            mismatches.append(
                f"dept.yaml display_name {declared['display_name']!r} != roster name {agent['name']!r}"
            )
        record["identity_mismatches"] = mismatches
    return record


def discover_departments(root: Path, excluded_slugs: Iterable[str] = ()) -> list[Path]:
    excluded = DEFAULT_NON_DEPARTMENT_SLUGS | frozenset(excluded_slugs)
    return sorted(
        (
            child
            for child in root.iterdir()
            if child.is_dir()
            and not child.is_symlink()
            and not child.name.startswith(".")
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
        "fleet_roster_bound": False,
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


def load_fleet_roster(path: Path) -> list[dict[str, object]]:
    """Load canonical agent identities from the living fleet configuration."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    agents = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(agents, list):
        raise ValueError(f"{path}: expected an agents list")

    required = ("id", "name", "role", "host")
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, item in enumerate(agents):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: agents[{index}] must be a mapping")
        missing = [key for key in required if not str(item.get(key, "")).strip()]
        if missing:
            raise ValueError(f"{path}: agents[{index}] missing {', '.join(missing)}")
        agent_id = str(item["id"])
        if agent_id in seen:
            raise ValueError(f"{path}: duplicate agent id {agent_id!r}")
        seen.add(agent_id)
        records.append({key: item[key] for key in required})
    return records


def build_fleet_inventory(
    agents: Iterable[Mapping[str, object]],
    sources: Mapping[str, Path],
) -> dict[str, object]:
    """Inventory every roster agent, preserving missing checkout mappings as gaps."""
    roster = sorted(agents, key=lambda item: str(item["id"]))
    roster_ids = {str(item["id"]) for item in roster}
    unknown = sorted(set(sources) - roster_ids)
    if unknown:
        raise ValueError(f"agent source ids absent from fleet roster: {', '.join(unknown)}")

    records: list[dict[str, object]] = []
    without_checkout: list[str] = []
    for agent in roster:
        agent_id = str(agent["id"])
        repo = sources.get(agent_id)
        if repo is None or not repo.is_dir():
            without_checkout.append(agent_id)
            records.append(
                {
                    "agent": {
                        "id": agent["id"],
                        "display_name": agent["name"],
                        "role": agent["role"],
                        "host": agent["host"],
                    },
                    "department": None,
                    "repository": None,
                    "git_head": None,
                    "source_snapshot": {"mode": "missing", "revision": None},
                    "standard_shape": {
                        "dept_yaml": "unknown",
                        "mandate": "unknown",
                        "layers": {str(number): "unknown" for number in range(1, 5)},
                        "missions_directory": "unknown",
                        "complete": False,
                        "missing": ["source checkout mapping"],
                    },
                    "declared_identity": None,
                    "identity_mismatches": [],
                    "coverage": {
                        "mandate": "unknown",
                        "mission_source_count": 0,
                        "mission_file_count": 0,
                        "inline_recurring_mission_count": 0,
                        "missing": ["source checkout mapping"],
                    },
                    "sources": [],
                }
            )
            continue
        records.append(inventory_department(repo, agent))

    return {
        "schema_version": SCHEMA_VERSION,
        "semantics": "unclassified; requires human or agent judgment",
        "fleet_roster_bound": True,
        "departments": records,
        "summary": {
            "department_count": len(records),
            "expected_agent_count": len(roster),
            "agent_source_count": len(roster) - len(without_checkout),
            "agents_without_checkout": without_checkout,
            "fleet_coverage_complete": not without_checkout,
            "complete_source_sets": sum(
                not record["coverage"]["missing"] for record in records  # type: ignore[index]
            ),
            "departments_with_missing_sources": [
                record["agent"]["id"]
                for record in records
                if record["coverage"]["missing"]  # type: ignore[index]
            ],
        },
    }


def parse_agent_sources(values: Iterable[str]) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for value in values:
        agent_id, separator, raw_path = value.partition("=")
        if not separator or not agent_id or not raw_path:
            raise ValueError(f"invalid --agent-source {value!r}; expected AGENT_ID=PATH")
        if agent_id in sources:
            raise ValueError(f"duplicate --agent-source for {agent_id!r}")
        sources[agent_id] = Path(raw_path)
    return sources


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root", type=Path, help="Dedicated parent containing department roots")
    group.add_argument(
        "--department",
        action="append",
        type=Path,
        help="Explicit department repository path; repeat as needed",
    )
    group.add_argument(
        "--agent-source",
        action="append",
        default=[],
        metavar="AGENT_ID=PATH",
        help="Roster agent and source checkout; repeat for every fleet agent",
    )
    parser.add_argument(
        "--exclude-slug",
        action="append",
        default=[],
        help="Department slug to omit when using --root; repeat as needed",
    )
    parser.add_argument(
        "--fleet-config",
        type=Path,
        help="Canonical fleet-architecture-sources.yaml; required with --agent-source",
    )
    parser.add_argument(
        "--allow-partial-fleet",
        action="store_true",
        help="Return success even if a roster agent has no --agent-source mapping",
    )
    parser.add_argument("--output", type=Path, help="Write JSON to this path instead of stdout")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.agent_source:
        if args.fleet_config is None:
            raise SystemExit("--fleet-config is required with --agent-source")
        try:
            inventory = build_fleet_inventory(
                load_fleet_roster(args.fleet_config),
                parse_agent_sources(args.agent_source),
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        if args.fleet_config is not None:
            raise SystemExit("--fleet-config requires --agent-source")
        departments = (
            discover_departments(args.root, args.exclude_slug)
            if args.root is not None
            else args.department
        )
        inventory = build_inventory(departments)
    rendered = json.dumps(inventory, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    incomplete = inventory["summary"].get("agents_without_checkout", [])  # type: ignore[index]
    return 2 if incomplete and not args.allow_partial_fleet else 0


if __name__ == "__main__":
    raise SystemExit(main())
