"""Business-unit lens for /kanban (board #1603). Read-only.

Card -> BU rule (reuses the fleet's existing taxonomy, no new one):
  1. an explicit ``bu:<slug>`` label on the card wins;
  2. otherwise the card's ``dept:<x>`` owner maps to the union of the
     ``business_unit`` values declared by that dept's missions (dept.yaml +
     missions/*.yaml, the same reader the operations map uses).
Slugs are scripts.lib.mission_alignment.BUSINESS_UNITS; ``steering`` = Pilotage.
A card/dept with no mapping belongs to no BU (visible only in the unfiltered view).
"""
from __future__ import annotations

import yaml

from console.services import dept_registry, github_reader
from scripts.lib.mission_alignment import BUSINESS_UNITS

BU_LABELS = {"fund": "Fonds", "ai_methods": "Méthodes IA",
             "pro_clients": "Clients pros", "steering": "Pilotage"}


def normalize_bu(value) -> str | None:
    """Valid BU slug or None (unknown/empty param = no filter)."""
    return value if isinstance(value, str) and value in BUSINESS_UNITS else None


def dept_units() -> dict[str, set[str]]:
    """dept slug -> set of BU slugs declared by its missions."""
    out: dict[str, set[str]] = {}
    for dept in dept_registry.list_departments():
        try:
            missions = github_reader.list_missions_full(dept.slug)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        units: set[str] = set()
        for m in missions or []:
            raw = m.get("business_unit") if isinstance(m, dict) else None
            for u in raw if isinstance(raw, list) else [raw]:
                if isinstance(u, str) and u in BUSINESS_UNITS:
                    units.add(u)
        if units:
            out[dept.slug] = units
    return out


def card_units(card: dict, by_dept: dict[str, set[str]]) -> set[str]:
    label = normalize_bu(card.get("bu_label"))
    if label:
        return {label}
    return set(by_dept.get((card.get("owner") or "").strip().lower(), ()))


def filter_cards(cards: list, bu: str | None, by_dept: dict[str, set[str]]) -> list:
    if not bu:
        return cards
    return [c for c in cards if bu in card_units(c, by_dept)]
