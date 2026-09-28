"""Read-only mission alignment diagnostics; never part of dispatch validation."""
from __future__ import annotations

import os
from pathlib import Path

BUSINESS_UNITS = ("fund", "ai_methods", "pro_clients", "steering")
FLOORS = ("distribute", "package", "produce", "measure")


def default_intents_dir() -> Path:
    """Use the shared wiki mirror; deployments may override its local path."""
    return Path(os.environ.get(
        "OPERATOR_INTENTS_DIR",
        str(Path.home() / ".claude/agent-memory/shared-wiki/shared/operator-intents"),
    ))


def read_intent_slugs(directory: Path) -> set[str] | None:
    try:
        return {p.stem for p in directory.iterdir() if p.is_file() and p.suffix == ".md"}
    except OSError:
        return None  # unavailable catalog is not an empty catalog


def inspect_department(dept: str, manifest: object, intents: set[str] | None) -> dict:
    """Mapped means valid unit + nonempty, known intent list + valid optional floor.

    Missing either mapping field is unmapped. Diagnostics can overlap; counts
    count missions, not individual bad slugs. An unavailable catalog leaves
    otherwise complete mappings unverified (never falsely mapped or unknown).
    """
    result = {"dept": dept, "missions": [], "errors": [], "mapped": 0,
              "unmapped": 0, "unknown_intents": 0, "invalid_business_unit": 0,
              "unverified": 0, "invalid_metadata": 0}
    if not isinstance(manifest, dict):
        result["errors"].append("manifest unavailable or not a mapping")
        return result
    missions = manifest.get("recurring_missions", [])
    if not isinstance(missions, list):
        result["errors"].append("recurring_missions must be an array")
        return result
    for index, mission in enumerate(missions):
        if not isinstance(mission, dict):
            result["errors"].append(f"mission {index + 1} must be a mapping")
            continue
        unit = mission.get("business_unit")
        slugs = mission.get("serves_intents")
        issues = []
        missing = not unit or not slugs
        if missing:
            issues.append("unmapped")
        if unit is not None and unit not in BUSINESS_UNITS:
            issues.append("invalid_business_unit")
        valid_slugs = isinstance(slugs, list) and all(
            isinstance(s, str) and bool(s) and s not in (".", "..")
            and "/" not in s and "\\" not in s and not s.endswith(".md")
            for s in slugs
        )
        if slugs is not None and not valid_slugs:
            issues.append("invalid_serves_intents")
        if "floor" in mission and mission["floor"] not in FLOORS:
            issues.append("invalid_floor")
        unknown = sorted(set(slugs) - intents) if valid_slugs and intents is not None else []
        if unknown:
            issues.append("unknown_intents")
        if valid_slugs and slugs and intents is None:
            issues.append("unverified")
        if not issues:
            issues.append("mapped")
        for key in ("mapped", "unmapped", "unknown_intents", "invalid_business_unit", "unverified"):
            result[key] += key in issues
        result["invalid_metadata"] += any(i.startswith("invalid_") for i in issues)
        result["missions"].append({"mission": str(mission.get("id", f"#{index + 1}")),
                                   "statuses": issues, "unknown_intents": unknown})
    return result


def alignment_report(manifests, intents: set[str] | None) -> dict:
    departments = [inspect_department(dept, manifest, intents) for dept, manifest in manifests]
    keys = ("mapped", "unmapped", "unknown_intents", "invalid_business_unit", "unverified", "invalid_metadata")
    return {"departments": departments, "catalog_available": intents is not None,
            "totals": {key: sum(d[key] for d in departments) for key in keys}}
