#!/usr/bin/env python3
"""Atomically enrich an existing management export without replacing model KPIs."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import math
import re
from pathlib import Path

import yaml

try:
    from .mission_kpis import atomic_write, day
except ImportError:
    from mission_kpis import atomic_write, day


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate keys rather than silently discarding model content."""


def _mapping(loader, node):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def validate(value, schema: dict, path: str = "export") -> None:
    """Validate the schema's object, scalar and union rules using stdlib only."""
    if "oneOf" in schema:
        matches = 0
        for choice in schema["oneOf"]:
            try:
                validate(value, choice, path)
                matches += 1
            except ValueError:
                pass
        if matches != 1:
            raise ValueError(f"{path}: invalid union value")
        return
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float)}
    # PyYAML interprets an unquoted ISO date as datetime.date. Preserve its spelling.
    is_date = kind == "string" and schema.get("format") == "date" and type(value) is dt.date
    if kind in types and not is_date and (
        not isinstance(value, types[kind]) or kind in ("integer", "number") and isinstance(value, bool)
    ):
        raise ValueError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: invalid enum")
    if kind == "object":
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{path}: keys must be strings")
        if set(schema.get("required", [])) - set(value):
            raise ValueError(f"{path}: required keys missing")
        properties = schema.get("properties", {})
        for key, item in value.items():
            rule = properties.get(key, schema.get("additionalProperties", True))
            if rule is False:
                raise ValueError(f"{path}: unknown key {key}")
            if isinstance(rule, dict):
                validate(item, rule, f"{path}.{key}")
    elif kind == "array":
        for item in value:
            validate(item, schema.get("items", {}), path)
    elif kind == "string":
        text = str(value)
        if len(text) < schema.get("minLength", 0) or (
            "pattern" in schema and not re.search(schema["pattern"], text)
        ):
            raise ValueError(f"{path}: invalid string")
        if schema.get("format") == "date":
            day(text)
    elif kind in ("integer", "number"):
        if not math.isfinite(value) or value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ValueError(f"{path}: invalid number")


def load_export(path: Path) -> dict:
    doc = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    schema = yaml.safe_load((Path(__file__).resolve().parents[2] /
                             "schemas-draft/management-export.schema.yaml").read_text(encoding="utf-8"))
    validate(doc, schema)
    return doc


def enrich(export_path: Path, kpis: dict, dept_dir: Path, report_day: str) -> bool:
    """Return whether a valid export changed; a second identical run is a no-op."""
    export_path = Path(export_path)
    doc = load_export(export_path)
    day(report_day)
    if str(doc["date"]) != report_day:
        raise ValueError("export date differs from report day")
    original = copy.deepcopy(doc)
    flat = kpis["top_kpis_flat"]
    if not isinstance(flat, dict) or any(not isinstance(key, str) or isinstance(value, bool) or
            not isinstance(value, (int, float)) or not math.isfinite(value) for key, value in flat.items()):
        raise ValueError("KPI keys must be flat finite numbers")
    if not isinstance(kpis["attention"], list):
        raise ValueError("KPI attention must be a list")
    for key, value in flat.items():
        doc["top_kpis"].setdefault(key, value)
    attention = doc["needs_management_attention"]
    ids = {item["id"] for item in attention if isinstance(item, dict)}
    additions = list(kpis["attention"])
    report = Path(dept_dir) / "outputs" / report_day / "4/summary.md"
    try:
        report_exists = bool(report.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        report_exists = False
    if report_exists:
        doc["links"]["day_report"] = f"outputs/{report_day}/4/summary.md"
    else:
        additions.append(dict(id="kpi-day-report-missing", kind="mission_cost_quality", priority="low",
                              summary=f"Department day report is missing or empty for {report_day}"))
    for item in additions:
        if not isinstance(item, dict) or "id" not in item:
            raise ValueError("invalid KPI attention item")
        if item["id"] not in ids:
            attention.append(copy.deepcopy(item))
            ids.add(item["id"])
    if "export_enriched" in doc["top_kpis"] and (
        type(doc["top_kpis"]["export_enriched"]) is not int or doc["top_kpis"]["export_enriched"] != 1
    ):
        raise ValueError("existing export_enriched conflicts with enrichment stamp")
    doc["top_kpis"]["export_enriched"] = 1
    schema = yaml.safe_load((Path(__file__).resolve().parents[2] /
                             "schemas-draft/management-export.schema.yaml").read_text(encoding="utf-8"))
    validate(doc, schema)
    if doc == original:
        return False
    atomic_write(export_path, yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, type=Path)
    parser.add_argument("--kpis", required=True, type=Path)
    parser.add_argument("--dept-dir", required=True, type=Path)
    parser.add_argument("--day", required=True)
    args = parser.parse_args()
    try:
        kpis = json.loads(args.kpis.read_text(encoding="utf-8"))
        enrich(args.export, kpis, args.dept_dir, args.day)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        parser.error(f"cannot enrich management export: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
