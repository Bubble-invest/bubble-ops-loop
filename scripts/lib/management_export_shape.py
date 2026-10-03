"""Safe model-export shape reporting; never writes model content."""
from __future__ import annotations
import datetime as dt
import math
import re
from pathlib import Path
import yaml
try:
    from .mission_kpis import day
except ImportError:
    from mission_kpis import day

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



def export_shape(raw: bytes | None) -> tuple[str, str]:
    if raw is None:
        return "absent", "export absent"
    if len(raw) > 1024 * 1024:
        return "unparseable", "export exceeds 1 MiB"
    try:
        doc = yaml.load(raw.decode("utf-8"), Loader=UniqueLoader)
        schema = yaml.safe_load((Path(__file__).resolve().parents[2] /
                                 "schemas-draft/management-export.schema.yaml").read_text())
        keys = set(doc) if isinstance(doc, dict) else set()
        missing = sorted(set(schema["required"]) - keys)
        extra = sorted(keys - set(schema["properties"]), key=str)
        def name(key):
            return re.sub(r"[^a-zA-Z0-9_.-]", "?", str(key))[:80]
        notes = []
        if missing:
            notes.append("missing required root keys: " + ", ".join(missing))
        if extra:
            notes.append("extra root keys: " + ", ".join(name(k) for k in extra))
        if isinstance(doc, dict) and len(doc) == 1:
            key = next(iter(doc))
            if isinstance(key, str) and key not in schema["properties"] and isinstance(doc[key], dict):
                return "wrapped:" + name(key), "; ".join(notes)
        try:
            validate(doc, schema)
        except ValueError:
            return "nonconforming", "; ".join(notes) or "invalid schema field types or constraints"
        return "schema", "schema-shaped root"
    except Exception:
        # Includes YAML aliases causing recursion, invalid UTF-8 and unsafe tags.
        return "unparseable", "export cannot be safely parsed"
