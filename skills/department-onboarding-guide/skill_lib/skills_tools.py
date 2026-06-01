"""
skills_tools.py — Step 4 helpers (skills/tools manifest + card validation).
"""
from __future__ import annotations

from typing import Any, Dict, List


VALID_STATUSES = ("draft", "tested", "live")


def build_manifest(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the per-layer skills + flat tools manifest.

    Input ctx must have keys: skills (dict layer_1..layer_4 -> list), tools (list).
    Returns a copy in the canonical shape.
    """
    skills = ctx.get("skills", {})
    for lyr in ("layer_1", "layer_2", "layer_3", "layer_4"):
        skills.setdefault(lyr, [])
    return {
        "skills": {k: list(skills[k]) for k in ("layer_1", "layer_2", "layer_3", "layer_4")},
        "tools": list(ctx.get("tools", [])),
    }


def validate_card(slug: str, card: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate one skill/tool card.

    Required fields: purpose (str), inputs (list[str]), outputs (list[str]),
    status (one of draft|tested|live).
    """
    for k in ("purpose", "inputs", "outputs", "status"):
        if k not in card:
            raise ValueError(f"Card {slug!r} missing required field: {k}")
    if not isinstance(card["inputs"], list) or not all(isinstance(x, str) for x in card["inputs"]):
        raise ValueError(f"Card {slug!r} field `inputs` must be list[str]")
    if not isinstance(card["outputs"], list) or not all(isinstance(x, str) for x in card["outputs"]):
        raise ValueError(f"Card {slug!r} field `outputs` must be list[str]")
    if card["status"] not in VALID_STATUSES:
        raise ValueError(
            f"Card {slug!r} status {card['status']!r} not in {VALID_STATUSES}"
        )
    return {
        "slug": slug,
        "purpose": card["purpose"],
        "inputs": list(card["inputs"]),
        "outputs": list(card["outputs"]),
        "status": card["status"],
    }
