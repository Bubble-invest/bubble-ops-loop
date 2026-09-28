"""Read-only company operations grid (#1602); no dispatch/materialization calls."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import yaml

from console.services import dept_registry, github_reader, morty_reader
from scripts.lib.dispatch_helpers import (
    _parse_iso, is_mission_due, paris_today, read_dispatch_ledger, read_last_run,
)

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
STATUS_LABELS = {
    "completed": "Terminé aujourd’hui", "due": "À exécuter",
    "not-run": "Non exécuté aujourd’hui", "unknown": "État inconnu",
}


def _completion(day_dir, mid, now):
    entry = read_dispatch_ledger(day_dir).get(mid, {})
    try:
        stamp = _parse_iso(entry["completed_at"])
    except (KeyError, ValueError, TypeError, AttributeError):
        stamp = read_last_run(day_dir / "missions" / mid)
    return stamp if stamp and stamp <= now else None


def _mission_state(root, mission, now):
    if root is None or mission is None:
        return "unknown", None
    today = paris_today(now)
    stamp = _completion(root / "outputs" / today, mission["id"], now)
    completed_today = stamp is not None and paris_today(stamp) == today
    # Cron catch-up needs a cross-day completion watermark. Never count a
    # materialization marker, or a sibling mission's layer marker, as success.
    if str(mission.get("cadence", "")).startswith("cron:") and stamp is None:
        outputs = root / "outputs"
        if outputs.is_dir():
            for day in sorted(outputs.iterdir(), reverse=True):
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name) and day.name < today:
                    stamp = _completion(day, mission["id"], now)
                    if stamp:
                        break
    due = is_mission_due(mission, now=now, last_fired=stamp)
    # Repeating missions may be due again after completing earlier today.
    return ("due" if due else "completed" if completed_today else "not-run",
            stamp.isoformat() if stamp else None)


def load_operations_map(now=None):
    now = now or datetime.now(timezone.utc)
    result = {"available": False, "as_of": now.isoformat(), "date": paris_today(now),
              "units": {}, "rows": [], "drift": [], "departments": [],
              "status_labels": STATUS_LABELS}
    root = dept_registry.runtime_repo_path("tony")
    if root is None:
        return result
    try:
        mapping = yaml.safe_load((root / "outputs/operations-map/operations-map.yaml")
                                 .read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return result
    if not isinstance(mapping, dict) or not isinstance(mapping.get("business_units"), dict):
        return result
    units = {str(k): dict(v) if isinstance(v, dict) else {"label": str(v)}
             for k, v in mapping["business_units"].items()}
    if not units:
        return result
    raw_layers = mapping.get("layers") or []
    layers = [x for x in raw_layers if isinstance(x, str)] if isinstance(raw_layers, list) else []
    missions = {}
    roots = {}
    for dept in dept_registry.list_departments():
        if not _SAFE_ID.fullmatch(dept.slug):
            continue
        runtime = dept_registry.runtime_repo_path(dept.slug)
        roots[dept.slug] = runtime
        # Existing console mission reader merges dept.yaml + missions/*.yaml.
        try:
            declared = github_reader.list_missions_full(dept.slug, root=runtime)
        except (OSError, ValueError, yaml.YAMLError):
            declared = []
        for mission in declared:
            mid = str(mission.get("id", ""))
            if _SAFE_ID.fullmatch(mid):
                missions[(dept.slug, mid)] = mission
    pulses = morty_reader.loop_pulse(list(roots), now_epoch=now.timestamp(), roots=roots)
    result["departments"] = [{"slug": slug, "pulse": pulses.get(slug)} for slug in roots]
    placed = set()
    cells = {}

    def add(entry, slug, mid):
        key = (slug, mid)
        mission = missions.get(key)
        try:
            status, last_run = _mission_state(roots.get(slug), mission, now)
        except (OSError, ValueError, TypeError):
            status, last_run = "unknown", None
        item = {"dept": slug, "id": mid, "status": status, "last_run": last_run,
                "mission_layer": (mission or {}).get("layer"),
                "time": (mission or {}).get("time") or entry.get("time", ""),
                "note": entry.get("note", ""), "missing": mission is None}
        targets = entry.get("unit")
        targets = targets if isinstance(targets, list) else [targets]
        layer = entry.get("layer")
        valid = [u for u in targets if isinstance(u, str) and u not in ("none", "")]
        if not valid or not isinstance(layer, str) or not layer:
            result["drift"].append(item)
        else:
            if layer not in layers:
                layers.append(layer)
            for unit in valid:
                units.setdefault(unit, {"label": unit})
                cells.setdefault((layer, unit), {"missions": [], "gaps": []})["missions"].append(item)
        placed.add(key)

    entries = mapping.get("missions") or []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        slug, mid = entry.get("dept"), entry.get("id")
        if not isinstance(mid, str) or not _SAFE_ID.fullmatch(mid):
            continue
        if slug == "*":
            for s, m in missions:
                if m == mid:
                    add(entry, s, m)
        elif isinstance(slug, str) and _SAFE_ID.fullmatch(slug):
            add(entry, slug, mid)
    for (slug, mid) in sorted(missions.keys() - placed):
        add({}, slug, mid)
    gaps = mapping.get("gaps") or []
    for gap in gaps if isinstance(gaps, list) else []:
        if not isinstance(gap, dict):
            continue
        unit, layer = gap.get("unit"), gap.get("layer")
        if not isinstance(unit, str) or not isinstance(layer, str):
            continue
        units.setdefault(unit, {"label": unit})
        if layer not in layers:
            layers.append(layer)
        cells.setdefault((layer, unit), {"missions": [], "gaps": []})["gaps"].append(gap.get("what", ""))
    result.update(available=True, units=units,
                  rows=[{"layer": layer, "cells": [cells.get((layer, unit),
                         {"missions": [], "gaps": []}) for unit in units]} for layer in layers])
    return result
