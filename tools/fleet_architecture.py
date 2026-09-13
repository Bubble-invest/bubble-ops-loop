#!/usr/bin/env python3
"""Collect safe fleet metadata and render the living wiki architecture map.

The collector intentionally reads only allow-listed architecture surfaces.  It
does not inspect transcripts, secrets, business data, source-code contents, or
broker/client artifacts.  Intent is never inferred: only exact intent links
already present in source frontmatter or a previously reviewed generated note
are carried forward.

Board card: Bubble-invest/bubble-ops-board#1249.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from xml.parsers.expat import ExpatError

import yaml

try:
    import jsonschema
except ImportError:  # surfaced as an actionable runtime error by validation
    jsonschema = None


VERSION = 1
KINDS = ("skill", "tool", "mission", "cron", "integration", "dependency")
GENERATED_OWNER = "fleet-architecture-collector"
MANAGED_START = "<!-- fleet-architecture:managed:start -->"
MANAGED_END = "<!-- fleet-architecture:managed:end -->"
MANAGED_NOTE_KEYS = {
    "title", "type", "owner", "agent", "kind", "item_id", "status",
    "last_verified", "intent", "intent_rationale",
}
MAP_REL = Path("shared/systems/fleet-architecture-map.md")
STATE_REL = Path("shared/systems/fleet-architecture-inventory.json")
NOTES_REL = Path("shared/fleet-architecture")
INTENT_PREFIX = "shared/operator-intents/"
INTENT_RE = re.compile(r"^\[\[(shared/operator-intents/[A-Za-z0-9._/-]+)\]\]$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@ -]*$")
IGNORE_NAMES = {
    "__pycache__",
    ".git",
    ".gitkeep",
    ".DS_Store",
    "README",
    "README.md",
    "tests",
    "test",
}


class InventoryError(ValueError):
    """Raised for invalid configuration or unsafe/invalid generated state."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: object, *, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise InventoryError(f"invalid {field} timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise InventoryError(f"{field} timestamp must include a timezone: {value!r}")
    return parsed.astimezone(dt.timezone.utc)


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InventoryError(f"cannot read YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InventoryError(f"{path}: top level must be a mapping")
    return value


def slug(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return out or "unnamed"


def safe_label(value: object, *, field: str) -> str:
    text = str(value).strip()
    if not text or not SAFE_NAME.fullmatch(text):
        raise InventoryError(f"unsafe or empty {field}: {text!r}")
    return text


def rel_evidence(path: Path, root: Path, alias: str) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        raise InventoryError(f"evidence escaped source root: {path} not under {root}")
    return f"{alias}/{rel.as_posix()}"


def frontmatter(path: Path) -> dict[str, Any]:
    """Read YAML frontmatter only, capped to keep source reads narrow."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines: list[str] = []
            first = handle.readline()
            if first.rstrip("\n") != "---":
                return {}
            for _ in range(120):
                line = handle.readline()
                if not line or line.rstrip("\n") == "---":
                    break
                lines.append(line)
            else:
                return {}
    except OSError:
        return {}
    try:
        parsed = yaml.safe_load("".join(lines)) or {}
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def markdown_parts(path: Path) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        return {}, ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InventoryError(f"cannot read managed note {path}: {exc}") from exc
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end < 0:
        raise InventoryError(f"unterminated frontmatter in {path}")
    try:
        parsed = yaml.safe_load(raw[4:end]) or {}
    except yaml.YAMLError as exc:
        raise InventoryError(f"invalid frontmatter in {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise InventoryError(f"frontmatter in {path} must be a mapping")
    return parsed, raw[end + 5:]


def normalize_intents(raw: object) -> list[str]:
    if raw in (None, "", []):
        return []
    values = raw if isinstance(raw, list) else [raw]
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            result.append(value.strip())
    return sorted(set(result))


def validate_intents(intents: Sequence[str], wiki_root: Path) -> list[str]:
    errors: list[str] = []
    for link in intents:
        match = INTENT_RE.fullmatch(link)
        if not match:
            errors.append(f"{link!r} is not an exact operator-intent wikilink")
            continue
        relative = Path(f"{match.group(1)}.md")
        if any(part in {".", ".."} for part in relative.parts):
            errors.append(f"{link!r} contains a non-canonical path component")
            continue
        target = wiki_root / relative
        try:
            target.resolve().relative_to((wiki_root / INTENT_PREFIX).resolve())
        except ValueError:
            errors.append(f"{link!r} escapes the operator-intents collection")
            continue
        current = wiki_root
        case_exact = True
        for part in relative.parts:
            try:
                names = {child.name for child in current.iterdir()}
            except OSError:
                case_exact = False
                break
            if part not in names:
                case_exact = False
                break
            current = current / part
        if not case_exact or not target.is_file():
            errors.append(f"{link!r} does not resolve case-exactly to {target}")
    return errors


@dataclass(frozen=True)
class Item:
    kind: str
    name: str
    source: str
    details: Mapping[str, Any]
    intents: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.kind}:{slug(self.name)}"

    def as_dict(self, observed_at: str, status: str = "verified") -> dict[str, Any]:
        return {
            "id": self.key,
            "kind": self.kind,
            "name": self.name,
            "status": status,
            "source": self.source,
            "last_observed": observed_at,
            "intent": list(self.intents),
            "intent_rationale": "",
            "details": dict(self.details),
        }


def item(kind: str, name: object, source: str, *, details: Optional[Mapping[str, Any]] = None,
         intents: Iterable[str] = ()) -> Item:
    if kind not in KINDS:
        raise InventoryError(f"unsupported item kind: {kind}")
    return Item(
        kind=kind,
        name=safe_label(name, field=f"{kind} name"),
        source=source,
        details=details or {},
        intents=tuple(sorted(set(intents))),
    )


def iter_visible_children(directory: Path) -> Iterable[Path]:
    try:
        if not directory.is_dir():
            return ()
        children = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except (OSError, PermissionError):
        return ()
    return (
        child for child in children
        if child.name not in IGNORE_NAMES and not child.name.startswith(".")
    )


def safe_rglob(directory: Path, pattern: str) -> list[Path]:
    try:
        return sorted(directory.rglob(pattern)) if directory.is_dir() else []
    except (OSError, PermissionError):
        return []


def path_probe(path: Path) -> str:
    """Return readable, missing, or inaccessible without treating absence as proof."""
    try:
        if not path.exists():
            return "missing"
        if not path.is_dir():
            return "readable" if os.access(path, os.R_OK) else "inaccessible"
        list(path.iterdir())
        return "readable"
    except (OSError, PermissionError):
        return "inaccessible"


def surface_status(paths: Sequence[Path], item_count: int) -> str:
    probes = [path_probe(path) for path in paths]
    readable = any(probe == "readable" for probe in probes)
    blocked = any(probe == "inaccessible" for probe in probes)
    if item_count and blocked:
        return "partial"
    if item_count or readable:
        return "verified"
    return "unknown"


def scan_skills(root: Path, alias: str) -> list[Item]:
    found: dict[str, Item] = {}
    for base in (root / "skills", root / ".claude" / "skills"):
        for child in iter_visible_children(base):
            skill_file = child / "SKILL.md" if child.is_dir() else child
            if not skill_file.is_file() or skill_file.name != "SKILL.md":
                continue
            fm = frontmatter(skill_file)
            name = fm.get("name") or child.name
            candidate = item(
                "skill", name, rel_evidence(skill_file, root, alias),
                intents=normalize_intents(fm.get("intent")),
            )
            found.setdefault(candidate.key, candidate)
    return list(found.values())


def scan_tools(root: Path, alias: str) -> list[Item]:
    found: dict[str, Item] = {}
    base = root / "tools"
    for child in iter_visible_children(base):
        if child.is_file() and child.suffix not in {".py", ".sh", ".md"}:
            continue
        name = child.stem if child.is_file() else child.name
        evidence = rel_evidence(child, root, alias)
        fm = frontmatter(child) if child.is_file() and child.suffix == ".md" else {}
        candidate = item("tool", name, evidence, intents=normalize_intents(fm.get("intent")))
        found.setdefault(candidate.key, candidate)
    return list(found.values())


def mission_name(path: Path, base: Path) -> str:
    rel = path.relative_to(base)
    if path.stem.lower() in {"prompt", "mission", "readme"} and len(rel.parts) > 1:
        return rel.parts[-2]
    return path.stem


def scan_mission_files(root: Path, alias: str) -> list[Item]:
    found: dict[str, Item] = {}
    base = root / "missions"
    try:
        is_directory = base.is_dir()
    except (OSError, PermissionError):
        is_directory = False
    if not is_directory:
        return []
    for path in safe_rglob(base, "*"):
        if not path.is_file() or path.suffix.lower() not in {".md", ".yaml", ".yml", ".py", ".sh"}:
            continue
        if any(part.startswith(".") or part in IGNORE_NAMES for part in path.relative_to(base).parts):
            continue
        fm = frontmatter(path) if path.suffix.lower() == ".md" else {}
        candidate = item(
            "mission", mission_name(path, base), rel_evidence(path, root, alias),
            intents=normalize_intents(fm.get("intent")),
        )
        found.setdefault(candidate.key, candidate)
    return list(found.values())


def scan_integrations(root: Path, alias: str) -> list[Item]:
    found: dict[str, Item] = {}
    for child in iter_visible_children(root / "integrations"):
        name = child.stem if child.is_file() else child.name
        candidate = item("integration", name, rel_evidence(child, root, alias))
        found.setdefault(candidate.key, candidate)
    return list(found.values())


def scan_dept_yaml(root: Path, alias: str) -> list[Item]:
    path = root / "dept.yaml"
    if not path.is_file():
        return []
    raw = load_yaml(path)
    source = rel_evidence(path, root, alias)
    found: list[Item] = []

    for mission in raw.get("recurring_missions") or []:
        if not isinstance(mission, dict) or not mission.get("id"):
            continue
        details = {
            key: mission[key] for key in ("layer", "cadence", "day", "time")
            if key in mission and isinstance(mission[key], (str, int, float, bool))
        }
        found.append(item(
            "mission", mission["id"], source, details=details,
            intents=normalize_intents(mission.get("intent")),
        ))

    def walk_named(value: object) -> Iterable[tuple[str, object]]:
        if isinstance(value, str):
            yield value, None
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, str):
                    yield entry, None
                elif isinstance(entry, dict) and entry.get("name"):
                    yield str(entry["name"]), entry.get("intent")
        elif isinstance(value, dict):
            if value.get("name"):
                yield str(value["name"]), value.get("intent")
            else:
                for nested in value.values():
                    yield from walk_named(nested)

    for name, intent_value in walk_named(raw.get("skills") or {}):
        found.append(item("skill", name, source, intents=normalize_intents(intent_value)))
    for name, intent_value in walk_named(raw.get("tools") or []):
        found.append(item("tool", name, source, intents=normalize_intents(intent_value)))
    for name, intent_value in walk_named(raw.get("integrations") or []):
        found.append(item("integration", name, source, intents=normalize_intents(intent_value)))

    hierarchy = raw.get("hierarchy") or {}
    if isinstance(hierarchy, dict):
        parent = hierarchy.get("parent")
        if isinstance(parent, str) and parent.strip():
            found.append(item("dependency", parent, source, details={"relationship": "parent"}))
        for child in hierarchy.get("children") or []:
            if isinstance(child, str) and child.strip():
                found.append(item("dependency", child, source, details={"relationship": "child"}))
    return found


def scan_crons(root: Path, alias: str) -> list[Item]:
    found: dict[str, Item] = {}
    manifest = root / "config" / "crons.yaml"
    try:
        manifest_is_file = manifest.is_file()
    except (OSError, PermissionError):
        manifest_is_file = False
    if manifest_is_file:
        raw = load_yaml(manifest)
        for entry in raw.get("crons") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            details = {}
            if isinstance(entry.get("schedule"), str):
                details["schedule"] = entry["schedule"]
            candidate = item(
                "cron", entry["name"], rel_evidence(manifest, root, alias),
                details=details, intents=normalize_intents(entry.get("intent")),
            )
            found.setdefault(candidate.key, candidate)

    for base_name in ("systemd", "deploy", "config"):
        base = root / base_name
        for path in safe_rglob(base, "*.timer"):
            if path.is_file():
                candidate = item("cron", path.stem, rel_evidence(path, root, alias), details={"scheduler": "systemd"})
                found.setdefault(candidate.key, candidate)

    plist_paths = set(root.glob("*.plist"))
    tools_dir = root / "tools"
    if path_probe(tools_dir) == "readable":
        plist_paths.update(safe_rglob(tools_dir, "*.plist"))
    for path in sorted(plist_paths):
        if not path.is_file():
            continue
        name = path.stem
        details: dict[str, Any] = {"scheduler": "launchd"}
        try:
            with path.open("rb") as handle:
                parsed = plistlib.load(handle)
            if isinstance(parsed, dict) and isinstance(parsed.get("Label"), str):
                name = parsed["Label"]
            if isinstance(parsed, dict) and isinstance(parsed.get("StartInterval"), int):
                details["interval_seconds"] = parsed["StartInterval"]
            elif isinstance(parsed, dict) and "StartCalendarInterval" in parsed:
                details["schedule"] = "calendar interval (see source)"
        except (OSError, plistlib.InvalidFileException, ExpatError):
            pass
        candidate = item("cron", name, rel_evidence(path, root, alias), details=details)
        found.setdefault(candidate.key, candidate)
    return list(found.values())


def scan_root(agent: Mapping[str, Any], root: Path, observed_at: str, *, alias: str | None = None) -> dict[str, Any]:
    agent_id = safe_label(agent.get("id"), field="agent id")
    source_alias = alias or f"{agent_id}@{slug(agent.get('host', 'host'))}"
    if not root.is_dir():
        raise InventoryError(f"source root does not exist: {root}")
    policy = agent.get("scan") or {}
    if not isinstance(policy, dict):
        raise InventoryError(f"agent {agent_id}: scan must be a mapping")
    scan_dept = bool(policy.get("dept_yaml", True))

    items: list[Item] = []
    items.extend(scan_skills(root, source_alias))
    items.extend(scan_tools(root, source_alias))
    items.extend(scan_mission_files(root, source_alias))
    items.extend(scan_integrations(root, source_alias))
    items.extend(scan_crons(root, source_alias))
    if scan_dept:
        items.extend(scan_dept_yaml(root, source_alias))

    merged: dict[str, dict[str, Any]] = {}
    for found in items:
        current = merged.get(found.key)
        if current is None:
            merged[found.key] = found.as_dict(observed_at)
            continue
        current_intents = set(current.get("intent") or [])
        current_intents.update(found.intents)
        current["intent"] = sorted(current_intents)
        if found.source not in current["source"].split("; "):
            current["source"] += f"; {found.source}"
        current["details"].update(found.details)

    by_kind = {kind: 0 for kind in KINDS}
    for found in merged.values():
        by_kind[found["kind"]] += 1
    cron_sources = [root / "config" / "crons.yaml", root / "systemd", root / "deploy"]
    cron_sources.extend(path for path in root.glob("*.plist"))
    status_by_kind = {
        "skill": surface_status((root / "skills", root / ".claude" / "skills"), by_kind["skill"]),
        "tool": surface_status((root / "tools",), by_kind["tool"]),
        "mission": surface_status((root / "missions",), by_kind["mission"]),
        "cron": surface_status(cron_sources, by_kind["cron"]),
        "integration": surface_status((root / "integrations",), by_kind["integration"]),
        "dependency": surface_status((root / "dept.yaml",), by_kind["dependency"]) if scan_dept else "unknown",
    }
    values = set(status_by_kind.values())
    coverage_status = "verified" if values == {"verified"} else ("unknown" if values == {"unknown"} else "partial")
    return {
        "id": agent_id,
        "name": safe_label(agent.get("name") or agent_id, field="agent name"),
        "role": str(agent.get("role") or "Unknown").strip(),
        "host": str(agent.get("host") or "unknown").strip(),
        "wiki_folder": str(agent.get("wiki_folder") or "").strip(),
        "coverage": {
            "status": coverage_status,
            "last_checked": observed_at,
            "source": source_alias,
            "root": str(root),
            "surfaces": {
                kind: {
                    "status": status_by_kind[kind],
                    "count": by_kind[kind],
                }
                for kind in KINDS
            },
        },
        "items": sorted(merged.values(), key=lambda value: (value["kind"], value["name"].lower())),
    }


def load_previous(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": VERSION, "agents": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InventoryError(f"cannot read previous inventory {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("agents", []), list):
        raise InventoryError(f"invalid previous inventory: {path}")
    return value


def schema_spec() -> dict[str, Any]:
    if jsonschema is None:
        raise InventoryError("jsonschema is required to validate fleet architecture data")
    path = Path(__file__).resolve().parents[1] / "fleet" / "fleet-architecture-inventory.schema.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        jsonschema.Draft7Validator.check_schema(value)
    except (OSError, json.JSONDecodeError, jsonschema.SchemaError) as exc:
        raise InventoryError(f"cannot load inventory schema {path}: {exc}") from exc
    return value


def load_fragments(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InventoryError(f"cannot read fragment {path}: {exc}") from exc
        agents = value.get("agents") if isinstance(value, dict) else None
        if agents is None and isinstance(value, dict) and value.get("id"):
            agents = [value]
        if not isinstance(agents, list):
            raise InventoryError(f"fragment {path} must contain an agent or agents list")
        for agent in agents:
            if not isinstance(agent, dict) or not agent.get("id"):
                raise InventoryError(f"fragment {path} contains invalid agent data")
            agent_id = str(agent["id"])
            observed = parse_time((agent.get("coverage") or {}).get("last_checked"), field=f"fragment {path} last_checked")
            spec = schema_spec()
            try:
                jsonschema.Draft7Validator(spec).validate({
                    "version": VERSION,
                    "generated_at": str((agent.get("coverage") or {}).get("last_checked")),
                    "agents": [agent],
                })
            except jsonschema.ValidationError as exc:
                raise InventoryError(f"fragment {path} fails schema validation: {exc.message}") from exc
            prior = result.get(agent_id)
            if prior is None or observed > parse_time((prior.get("coverage") or {}).get("last_checked"), field=f"fragment {agent_id} last_checked"):
                result[agent_id] = agent
    return result


def discover_fragment_paths(config: Mapping[str, Any], explicit: Sequence[str]) -> list[Path]:
    paths = [Path(path) for path in explicit]
    configured = config.get("fragment_dirs") or []
    if not isinstance(configured, list):
        raise InventoryError("fragment_dirs must be a list")
    for raw in configured:
        directory = Path(os.path.expandvars(os.path.expanduser(str(raw))))
        if directory.is_dir():
            paths.extend(sorted(directory.rglob("*.json")))
    # The same path can be supplied explicitly and discovered.
    return list(dict.fromkeys(path.resolve() for path in paths))


def mark_stale(previous: Mapping[str, Any], config_agent: Mapping[str, Any], checked_at: str, reason: str) -> dict[str, Any]:
    carried = json.loads(json.dumps(previous)) if previous else {
        "id": config_agent["id"],
        "name": config_agent.get("name") or config_agent["id"],
        "role": config_agent.get("role") or "Unknown",
        "host": config_agent.get("host") or "unknown",
        "wiki_folder": config_agent.get("wiki_folder") or "",
        "items": [],
    }
    carried.update({key: config_agent.get(key, carried.get(key, "")) for key in ("name", "role", "host", "wiki_folder")})
    prior_status = (carried.get("coverage") or {}).get("status")
    status = "stale" if carried.get("items") and prior_status != "unknown" else "unknown"
    surfaces: dict[str, dict[str, Any]] = {}
    for kind in KINDS:
        count = sum(
            1 for entry in carried.get("items", [])
            if entry.get("kind") == kind and entry.get("status") != "removed"
        )
        surfaces[kind] = {"status": status, "count": count}
    carried["coverage"] = {
        "status": status,
        "last_checked": checked_at,
        "last_verified": (previous.get("coverage") or {}).get("last_checked") if previous else None,
        "reason": reason,
        "surfaces": surfaces,
    }
    for entry in carried.get("items", []):
        if entry.get("status") != "removed":
            entry["status"] = "stale"
    return carried


def merge_item_history(current: dict[str, Any], previous: Mapping[str, Any], checked_at: str) -> dict[str, Any]:
    """Keep missing observations as removed or stale instead of erasing history."""
    existing = {str(entry["id"]): entry for entry in current.get("items", [])}
    surfaces = current["coverage"]["surfaces"]
    for old in previous.get("items", []):
        old_id = str(old.get("id") or "")
        if not old_id or old_id in existing:
            continue
        retained = json.loads(json.dumps(old))
        kind = str(retained.get("kind") or "")
        if retained.get("status") == "removed":
            pass
        elif (surfaces.get(kind) or {}).get("status") == "verified":
            retained["status"] = "removed"
            details = retained.get("details") if isinstance(retained.get("details"), dict) else {}
            details["removed_at"] = checked_at
            retained["details"] = details
        else:
            retained["status"] = "stale"
        existing[old_id] = retained
    current["items"] = sorted(existing.values(), key=lambda value: (value["kind"], value["name"].lower()))
    for kind in KINDS:
        surfaces[kind]["count"] = sum(
            1 for entry in current["items"]
            if entry["kind"] == kind and entry.get("status") != "removed"
        )
    return current


def collect(config: Mapping[str, Any], previous: Mapping[str, Any], fragments: Mapping[str, dict[str, Any]], checked_at: str) -> dict[str, Any]:
    if config.get("version") != VERSION:
        raise InventoryError(f"config version must be {VERSION}")
    agents_cfg = config.get("agents")
    if not isinstance(agents_cfg, list) or not agents_cfg:
        raise InventoryError("config agents must be a non-empty list")
    previous_by_id = {str(a.get("id")): a for a in previous.get("agents", []) if isinstance(a, dict)}
    seen: set[str] = set()
    agents: list[dict[str, Any]] = []
    for cfg in agents_cfg:
        if not isinstance(cfg, dict):
            raise InventoryError("every configured agent must be a mapping")
        agent_id = safe_label(cfg.get("id"), field="agent id")
        if agent_id in seen:
            raise InventoryError(f"duplicate agent id: {agent_id}")
        seen.add(agent_id)
        if agent_id in fragments:
            current = json.loads(json.dumps(fragments[agent_id]))
            fragment_host = str(current.get("host") or "")
            expected_host = str(cfg.get("host") or "")
            if fragment_host != expected_host:
                raise InventoryError(
                    f"fragment {agent_id} host {fragment_host!r} does not match configured host {expected_host!r}"
                )
            current.update({key: cfg.get(key, current.get(key, "")) for key in ("name", "role", "host", "wiki_folder")})
            checked = parse_time(checked_at, field="refresh")
            observed = parse_time((current.get("coverage") or {}).get("last_checked"), field=f"fragment {agent_id}")
            max_age = float(config.get("fragment_max_age_hours", 36))
            if observed > checked + dt.timedelta(minutes=5):
                raise InventoryError(f"fragment {agent_id} is future-dated: {observed.isoformat()}")
            if checked - observed > dt.timedelta(hours=max_age):
                current = mark_stale(current, cfg, checked_at, f"host fragment is older than {max_age:g}h")
            current = merge_item_history(current, previous_by_id.get(agent_id, {}), checked_at)
            agents.append(current)
            continue
        if cfg.get("excluded"):
            agents.append(mark_stale(previous_by_id.get(agent_id, {}), cfg, checked_at, str(cfg.get("excluded"))))
            continue
        roots = cfg.get("roots") or []
        if not isinstance(roots, list):
            raise InventoryError(f"agent {agent_id}: roots must be a list")
        available = [Path(os.path.expandvars(os.path.expanduser(str(root)))) for root in roots]
        available = [root for root in available if root.is_dir()]
        if not available:
            reason = str(cfg.get("unavailable_reason") or "no configured source root is available on this host")
            agents.append(mark_stale(previous_by_id.get(agent_id, {}), cfg, checked_at, reason))
            continue
        scans = [scan_root(cfg, root, checked_at, alias=f"{agent_id}@{slug(cfg.get('host', 'host'))}:{index + 1}") for index, root in enumerate(available)]
        combined = scans[0]
        by_key = {entry["id"]: entry for entry in combined["items"]}
        sources = [combined["coverage"]["source"]]
        roots_used = [combined["coverage"]["root"]]
        for scan in scans[1:]:
            sources.append(scan["coverage"]["source"])
            roots_used.append(scan["coverage"]["root"])
            for entry in scan["items"]:
                if entry["id"] not in by_key:
                    by_key[entry["id"]] = entry
                else:
                    existing = by_key[entry["id"]]
                    existing["intent"] = sorted(set(existing.get("intent", [])) | set(entry.get("intent", [])))
                    existing["source"] += f"; {entry['source']}"
                    existing["details"].update(entry.get("details") or {})
        combined["items"] = sorted(by_key.values(), key=lambda value: (value["kind"], value["name"].lower()))
        combined["coverage"]["source"] = ", ".join(sources)
        combined["coverage"]["root"] = roots_used
        for kind in KINDS:
            combined["coverage"]["surfaces"][kind]["count"] = sum(1 for entry in combined["items"] if entry["kind"] == kind)
        combined = merge_item_history(combined, previous_by_id.get(agent_id, {}), checked_at)
        agents.append(combined)
    return {"version": VERSION, "generated_at": checked_at, "agents": agents}


def note_path(agent_id: str, entry: Mapping[str, Any]) -> Path:
    return NOTES_REL / slug(agent_id) / f"{entry['kind']}s" / f"{slug(str(entry['name']))}.md"


def read_reviewed_note_intents(wiki_root: Path, inventory: dict[str, Any]) -> None:
    for agent in inventory["agents"]:
        for entry in agent.get("items", []):
            path = wiki_root / note_path(agent["id"], entry)
            fm = frontmatter(path) if path.is_file() else {}
            reviewed = normalize_intents(fm.get("intent"))
            if reviewed:
                entry["intent"] = reviewed
                entry["intent_rationale"] = str(fm.get("intent_rationale") or "").strip()


def apply_intent_bindings(config: Mapping[str, Any], inventory: dict[str, Any]) -> tuple[list[str], str]:
    """Apply agent-reviewed intent judgments from config; never derive them."""
    raw = config.get("intent_bindings") or {}
    if not isinstance(raw, dict):
        raise InventoryError("intent_bindings must be a mapping")

    def parse_binding(value: object, label: str) -> tuple[list[str], str]:
        if not isinstance(value, dict):
            raise InventoryError(f"intent binding {label} must be a mapping")
        links = normalize_intents(value.get("links"))
        why = str(value.get("why") or "").strip()
        if links and not why:
            raise InventoryError(f"intent binding {label} has links but no explicit why")
        return links, why

    map_links, map_why = parse_binding(raw.get("map") or {}, "map")
    item_bindings = raw.get("items") or {}
    if not isinstance(item_bindings, dict):
        raise InventoryError("intent_bindings.items must be a mapping")
    by_agent = {str(agent["id"]): agent for agent in inventory["agents"]}
    for agent_id, bindings in item_bindings.items():
        if agent_id not in by_agent:
            raise InventoryError(f"intent binding names unknown agent {agent_id!r}")
        if not isinstance(bindings, dict):
            raise InventoryError(f"intent bindings for {agent_id} must be a mapping")
        entries = {str(entry["id"]): entry for entry in by_agent[agent_id].get("items", [])}
        for item_id, binding in bindings.items():
            if item_id not in entries:
                # A host may be unavailable in a fresh install. The missing fact
                # stays visibly unknown; a reviewed binding never manufactures it.
                continue
            links, why = parse_binding(binding, f"{agent_id}/{item_id}")
            entries[item_id]["intent"] = links
            entries[item_id]["intent_rationale"] = why
    return map_links, map_why


def validate_inventory(inventory: Mapping[str, Any], wiki_root: Path) -> None:
    ids: set[str] = set()
    errors: list[str] = []
    for agent in inventory.get("agents", []):
        agent_id = str(agent.get("id") or "")
        if not agent_id or agent_id in ids:
            errors.append(f"duplicate/empty agent id: {agent_id!r}")
        ids.add(agent_id)
        item_ids: set[str] = set()
        for entry in agent.get("items", []):
            if entry.get("kind") not in KINDS:
                errors.append(f"{agent_id}: invalid kind {entry.get('kind')!r}")
            if entry.get("id") in item_ids:
                errors.append(f"{agent_id}: duplicate item id {entry.get('id')!r}")
            item_ids.add(str(entry.get("id")))
            errors.extend(f"{agent_id}/{entry.get('id')}: {err}" for err in validate_intents(entry.get("intent") or [], wiki_root))
            if entry.get("intent") and not str(entry.get("intent_rationale") or "").strip():
                errors.append(f"{agent_id}/{entry.get('id')}: explicit intent has no reviewed rationale")
    if errors:
        raise InventoryError("inventory validation failed:\n- " + "\n- ".join(errors))
    schema_path = Path(__file__).resolve().parents[1] / "fleet" / "fleet-architecture-inventory.schema.json"
    spec = schema_spec()
    try:
        jsonschema.Draft7Validator(spec).validate(inventory)
    except jsonschema.ValidationError as exc:
        raise InventoryError(f"inventory does not satisfy {schema_path}: {exc}") from exc


def yaml_frontmatter(values: Mapping[str, Any]) -> str:
    return "---\n" + yaml.safe_dump(dict(values), allow_unicode=True, sort_keys=False).rstrip() + "\n---\n"


def note_frontmatter(agent: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any]:
    intents = entry.get("intent") or []
    return {
        "title": f"{agent['name']} · {entry['kind']} · {entry['name']}",
        "type": "fleet-architecture-item",
        "owner": GENERATED_OWNER,
        "agent": agent["id"],
        "kind": entry["kind"],
        "item_id": entry["id"],
        "status": entry["status"],
        "last_verified": entry.get("last_observed"),
        "intent": intents,
        "intent_rationale": str(entry.get("intent_rationale") or ""),
    }


def legacy_note_body(entry: Mapping[str, Any]) -> str:
    intents = entry.get("intent") or []
    lines = [f"# {entry['name']}\n"]
    if entry.get("status") == "removed":
        removed_at = (entry.get("details") or {}).get("removed_at", "unknown")
        lines.append(
            f"Previously observed as a **{entry['kind']}** in `{entry['source']}`; "
            f"absent from a verified surface at `{removed_at}`. Last positive observation: "
            f"`{entry.get('last_observed')}`.\n"
        )
    else:
        lines.append(f"Observed as a **{entry['kind']}** in `{entry['source']}`.\n")
    if entry.get("details"):
        lines.append("## Observed metadata\n")
        for key, value in sorted(entry["details"].items()):
            lines.append(f"- **{key.replace('_', ' ').title()}:** `{value}`")
        lines.append("")
    lines.append("## Why it exists\n")
    if intents:
        lines.append("Explicit intent: " + ", ".join(intents) + ".")
        lines.append("\n**Reviewed why:** " + str(entry.get("intent_rationale") or "_missing_"))
    else:
        lines.append("> [!warning] Intent unresolved\n> No explicit, reviewed operator-intent link has been recorded. The collector does not guess one.")
    lines.append("\n_Managed factual fields are refreshed from the inventory. Human/agent intent review belongs in the `intent` frontmatter field._\n")
    return "\n".join(lines)


def managed_note_body(entry: Mapping[str, Any]) -> str:
    return f"{MANAGED_START}\n{legacy_note_body(entry).rstrip()}\n{MANAGED_END}\n"


def render_note(
    agent: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    existing_path: Optional[Path] = None,
    previous_entry: Optional[Mapping[str, Any]] = None,
) -> str:
    generated_fm = note_frontmatter(agent, entry)
    existing_fm, existing_body = markdown_parts(existing_path) if existing_path else ({}, "")
    if existing_fm.get("core") is True:
        raise InventoryError(f"refusing to edit core:true note: {existing_path}")
    if existing_path and existing_path.is_file() and existing_fm.get("owner") != GENERATED_OWNER:
        raise InventoryError(f"managed note path collides with foreign owner at {existing_path}")

    merged_fm = dict(existing_fm)
    for key in MANAGED_NOTE_KEYS:
        if key in generated_fm:
            merged_fm[key] = generated_fm[key]

    managed = managed_note_body(entry)
    if MANAGED_START in existing_body or MANAGED_END in existing_body:
        if existing_body.count(MANAGED_START) != 1 or existing_body.count(MANAGED_END) != 1:
            raise InventoryError(f"malformed managed block in {existing_path}")
        before, rest = existing_body.split(MANAGED_START, 1)
        _, after = rest.split(MANAGED_END, 1)
        body = before + managed + after.lstrip("\n")
    elif existing_body:
        legacy = legacy_note_body(previous_entry).strip() if previous_entry else None
        if legacy and existing_body.strip() == legacy:
            body = managed
        else:
            # Unknown content is human-authored by default. Keep it outside the
            # managed block; subsequent refreshes replace only the block.
            body = managed.rstrip() + "\n\n" + existing_body.lstrip()
    else:
        body = managed
    return yaml_frontmatter(merged_fm) + "\n" + body.lstrip("\n")


def item_link(agent: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    rel = note_path(agent["id"], entry).with_suffix("").as_posix()
    why = ", ".join(entry.get("intent") or []) or "⚠ unresolved intent"
    state = f" · {entry.get('status')}" if entry.get("status") != "verified" else ""
    return f"[[{rel}|{entry['name']}]] — {why}{state}"


def render_map(inventory: Mapping[str, Any], map_intents: Sequence[str] = (), map_rationale: str = "") -> str:
    generated_at = str(inventory["generated_at"])
    fm = yaml_frontmatter({
        "title": "Fleet Architecture Map",
        "status": "active",
        "type": "reference",
        "owner": GENERATED_OWNER,
        "last_verified": generated_at[:10],
        "intent": list(map_intents),
        "intent_rationale": map_rationale,
        "tags": ["architecture", "fleet", "inventory", "generated"],
    })
    lines = [fm, "# Fleet Architecture Map", ""]
    lines.extend([
        f"> Generated from allow-listed architecture metadata at `{generated_at}`.",
        "> `verified` means an architecture surface was readable or an item was observed in this run; `partial` means only part of the configured surface was observable; `stale` is carried from an older host fragment; `unknown` means absence cannot be distinguished from missing access/configuration. `removed` items remain as dated history.",
        "> Intent links are explicit only. `unresolved intent` is a review queue, not an inferred justification.",
        "",
        "## Why this map exists",
        "",
        ((", ".join(map_intents) + " — " + map_rationale) if map_intents else "_Intent unresolved._"),
        "",
        "## Host topology",
        "",
        "```mermaid",
        "flowchart LR",
    ])
    hosts: dict[str, list[Mapping[str, Any]]] = {}
    for agent in inventory["agents"]:
        hosts.setdefault(str(agent["host"]), []).append(agent)
    for host_index, (host, agents) in enumerate(hosts.items(), start=1):
        host_node = f"H{host_index}"
        lines.append(f'  {host_node}["{host}"]')
        for agent_index, agent in enumerate(agents, start=1):
            agent_node = f"A{host_index}_{agent_index}"
            lines.append(f'  {host_node} --> {agent_node}["{agent["name"]} · {agent["role"]}"]')
    lines.extend(["```", "", "## Coverage", "", "| Agent | Host | Source coverage | Skills | Tools | Missions | Crons | Integrations | Dependencies |", "|---|---|---|---:|---:|---:|---:|---:|---:|"])
    for agent in inventory["agents"]:
        coverage = agent["coverage"]
        surface = coverage["surfaces"]
        counts = [str(surface[kind]["count"]) if surface[kind]["status"] == "verified" else f"{surface[kind]['count']} ({surface[kind]['status']})" for kind in KINDS]
        lines.append(f"| [[#{slug(agent['name'])}|{agent['name']}]] | {agent['host']} | **{coverage['status']}** | " + " | ".join(counts) + " |")
    lines.append("")

    for agent in inventory["agents"]:
        coverage = agent["coverage"]
        lines.extend([f"## {agent['name']}", "", f"**Role:** {agent['role']}", "", f"**Host:** {agent['host']}", "", f"**Coverage:** {coverage['status']} (checked {coverage['last_checked']})", ""])
        if coverage.get("reason"):
            lines.extend([f"**Coverage note:** {coverage['reason']}", ""])
        if agent.get("wiki_folder"):
            lines.extend([f"**Wiki area:** [[{agent['wiki_folder']}/hot]]", ""])
        entries = agent.get("items", [])
        plural = {"dependency": "Dependencies"}
        for kind in KINDS:
            lines.extend([f"### {plural.get(kind, kind.title() + 's')}", ""])
            matching = [entry for entry in entries if entry["kind"] == kind]
            if matching:
                lines.extend(f"- {item_link(agent, entry)}" for entry in matching)
            else:
                status = coverage["surfaces"][kind]["status"]
                lines.append(f"- _{('None observed' if status == 'verified' else 'Unknown')} ({status})._")
            lines.append("")
    lines.extend([
        "## Refresh contract",
        "",
        "The existing Mac transcript-sync job emits exact-host M4/M1/M5 fragments every 15 minutes through its current rsync trust path. The nightly wiki compile discovers those fragments, scans VPS-local roots, and runs this collector after intent curation and before index regeneration. Old fragments and unavailable roots retain prior facts as stale, so one sleeping Mac cannot erase the map. A nonzero exit blocks the compile and surfaces an operational failure.",
        "",
        "See `bubble-ops-loop/docs/fleet-architecture-map.md` for the source and safety contract.",
        "",
    ])
    return "\n".join(lines)


def render_outputs(
    inventory: Mapping[str, Any],
    map_intents: Sequence[str] = (),
    map_rationale: str = "",
    *,
    wiki_root: Optional[Path] = None,
    previous: Optional[Mapping[str, Any]] = None,
) -> dict[Path, bytes]:
    outputs: dict[Path, bytes] = {
        MAP_REL: render_map(inventory, map_intents, map_rationale).encode("utf-8"),
        STATE_REL: (json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }
    previous_agents = {
        str(agent.get("id")): agent for agent in (previous or {}).get("agents", [])
        if isinstance(agent, dict)
    }
    for agent in inventory["agents"]:
        previous_items = {
            str(entry.get("id")): entry
            for entry in previous_agents.get(str(agent["id"]), {}).get("items", [])
            if isinstance(entry, dict)
        }
        for entry in agent.get("items", []):
            path = note_path(agent["id"], entry)
            if path in outputs:
                digest = hashlib.sha256(entry["source"].encode("utf-8")).hexdigest()[:8]
                path = path.with_stem(f"{path.stem}-{digest}")
            existing_path = (wiki_root / path) if wiki_root else None
            outputs[path] = render_note(
                agent,
                entry,
                existing_path=existing_path,
                previous_entry=previous_items.get(str(entry["id"])),
            ).encode("utf-8")
    return outputs


def assert_write_targets(wiki_root: Path, outputs: Mapping[Path, bytes]) -> None:
    root = wiki_root.resolve()
    protected = (wiki_root / INTENT_PREFIX).resolve()
    for rel in outputs:
        if rel.is_absolute() or ".." in rel.parts:
            raise InventoryError(f"unsafe output path: {rel}")
        target = (wiki_root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise InventoryError(f"output escaped wiki root: {target}")
        try:
            target.relative_to(protected)
        except ValueError:
            pass
        else:
            raise InventoryError(f"refusing protected operator-intent write: {target}")


def managed_removals(wiki_root: Path, outputs: Mapping[Path, bytes]) -> list[Path]:
    managed_root = wiki_root / NOTES_REL
    if not managed_root.is_dir():
        return []
    desired = {(wiki_root / rel).resolve() for rel in outputs if NOTES_REL in rel.parents}
    removals: list[Path] = []
    for candidate in managed_root.rglob("*.md"):
        if candidate.resolve() in desired:
            continue
        fm = frontmatter(candidate)
        if fm.get("core") is True:
            continue
        if fm.get("owner") == GENERATED_OWNER and fm.get("type") == "fleet-architecture-item":
            removals.append(candidate.resolve())
    return sorted(removals)


def write_outputs(wiki_root: Path, outputs: Mapping[Path, bytes], removals: Sequence[Path] = ()) -> None:
    assert_write_targets(wiki_root, outputs)
    managed_root = wiki_root / NOTES_REL
    previous: dict[Path, Optional[bytes]] = {}
    targets = [(wiki_root / rel).resolve() for rel in outputs]
    for target in targets + removals:
        previous[target] = target.read_bytes() if target.is_file() else None
    try:
        for rel, content in outputs.items():
            target = wiki_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, target)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        for stale in removals:
            stale.unlink()
        for directory in sorted((p for p in managed_root.rglob("*") if p.is_dir()), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
    except Exception:
        for target, content in previous.items():
            if content is None:
                if target.exists():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        raise


def cmd_scan_root(args: argparse.Namespace) -> int:
    agent = {"id": args.agent_id, "name": args.name or args.agent_id, "role": args.role or "Unknown", "host": args.host, "wiki_folder": args.wiki_folder or "", "scan": {"dept_yaml": not args.no_dept_yaml}}
    result = scan_root(agent, Path(args.root), args.now or utc_now())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_scan_visible(args: argparse.Namespace) -> int:
    config = load_yaml(Path(args.config))
    host = str(args.host).strip()
    if not host:
        raise InventoryError("scan-visible requires a non-empty host selector")
    selected: list[dict[str, Any]] = []
    for agent in config.get("agents") or []:
        if not isinstance(agent, dict) or str(agent.get("host") or "") != host:
            continue
        roots = [Path(os.path.expandvars(os.path.expanduser(str(root)))) for root in agent.get("roots") or []]
        if any(path_probe(root) == "readable" for root in roots):
            selected.append(agent)
    if not selected:
        raise InventoryError(f"no readable configured roots for host {host!r}")
    observed_at = args.now or utc_now()
    fragment_config = dict(config)
    fragment_config["agents"] = selected
    inventory = collect(fragment_config, {"agents": []}, {}, observed_at)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for agent in inventory["agents"]:
        if agent["host"] != host:
            raise InventoryError(f"host selector mismatch for {agent['id']}")
        target = output_dir / f"{slug(host)}--{slug(agent['id'])}.json"
        payload = (json.dumps(agent, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(output_dir))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        written.append(str(target))
    print(json.dumps({"host": host, "fragments": written}, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    config = load_yaml(Path(args.config))
    wiki_root = Path(args.wiki_root).resolve()
    if not wiki_root.is_dir():
        raise InventoryError(f"wiki root does not exist: {wiki_root}")
    state_path = wiki_root / STATE_REL
    previous = load_previous(state_path)
    fragments = load_fragments(discover_fragment_paths(config, args.fragment))
    inventory = collect(config, previous, fragments, args.now or utc_now())
    map_intents, map_rationale = apply_intent_bindings(config, inventory)
    read_reviewed_note_intents(wiki_root, inventory)
    map_fm = frontmatter(wiki_root / MAP_REL)
    reviewed_map_intents = normalize_intents(map_fm.get("intent"))
    if reviewed_map_intents:
        map_intents = reviewed_map_intents
        map_rationale = str(map_fm.get("intent_rationale") or "").strip()
    map_intent_errors = validate_intents(map_intents, wiki_root)
    if map_intent_errors:
        raise InventoryError("map intent validation failed:\n- " + "\n- ".join(map_intent_errors))
    if map_intents and not map_rationale:
        raise InventoryError("map has an explicit intent but no reviewed rationale")
    validate_inventory(inventory, wiki_root)
    outputs = render_outputs(
        inventory,
        map_intents,
        map_rationale,
        wiki_root=wiki_root,
        previous=previous,
    )
    assert_write_targets(wiki_root, outputs)
    removals = managed_removals(wiki_root, outputs)
    if args.check:
        changed = [str(rel) for rel, data in outputs.items() if not (wiki_root / rel).is_file() or (wiki_root / rel).read_bytes() != data]
        removed = [str(path.relative_to(wiki_root)) for path in removals]
        print(json.dumps({"valid": True, "would_change": changed, "would_remove": removed}, indent=2))
        return 1 if changed or removed else 0
    write_outputs(wiki_root, outputs, removals)
    summary = {agent["id"]: agent["coverage"]["status"] for agent in inventory["agents"]}
    print(f"fleet architecture refreshed: {len(inventory['agents'])} agents; coverage={json.dumps(summary, sort_keys=True)}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", help="collect visible roots and refresh managed wiki outputs")
    refresh.add_argument("--config", required=True)
    refresh.add_argument("--wiki-root", required=True)
    refresh.add_argument("--fragment", action="append", default=[], help="optional per-host JSON fragment")
    refresh.add_argument("--now", help="fixed ISO timestamp for reproducible tests")
    refresh.add_argument("--check", action="store_true", help="validate and report drift without writing")
    refresh.set_defaults(func=cmd_refresh)

    scan = commands.add_parser("scan-root", help="emit one safe source-root inventory as JSON")
    scan.add_argument("--agent-id", required=True)
    scan.add_argument("--name")
    scan.add_argument("--role")
    scan.add_argument("--host", required=True)
    scan.add_argument("--wiki-folder")
    scan.add_argument("--root", required=True)
    scan.add_argument("--no-dept-yaml", action="store_true")
    scan.add_argument("--now")
    scan.set_defaults(func=cmd_scan_root)

    visible = commands.add_parser("scan-visible", help="write fragments for readable roots on one exact host")
    visible.add_argument("--config", required=True)
    visible.add_argument("--host", required=True)
    visible.add_argument("--output-dir", required=True)
    visible.add_argument("--now")
    visible.set_defaults(func=cmd_scan_visible)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.func(args))
    except InventoryError as exc:
        print(f"fleet-architecture: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
