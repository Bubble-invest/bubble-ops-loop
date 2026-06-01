"""
gate_grouping.py — group pending gates by `kind` for grouped-card UI.

Shared between:
  - home.py        (cross-dept dashboard)
  - dept.py        (per-dept detail page)

Why a shared module: home.py originally owned the helper, but dept_detail
was rendering one card per individual gate, which scaled badly (9 stale
echo gates = 9 cards). Joris flagged the inconsistency on 2026-05-24
("one card per task, shouldn't they be grouped?"). Single source of truth
keeps the grouping rules — and the operator's mental model — identical
on both pages.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List

from console.services.humanize import humanize_kind


def group_gates_by_kind(gates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group gates by `kind`. Returns a list of group dicts:

        {"kind": str, "kind_label": str, "gates": [gate, ...], "count": int}

    Order: deterministic — kinds appear in the order they were first seen
    in the input list (which itself is sorted by gate filename, see
    github_reader.list_pending_gates). Groups of size 1 keep the original
    single-card behaviour; groups of size >=2 render as a count-card.
    """
    buckets: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for g in gates:
        kind = g.get("kind") or "decision"
        buckets.setdefault(kind, []).append(g)
    out: List[Dict[str, Any]] = []
    for kind, items in buckets.items():
        out.append({
            "kind": kind,
            "kind_label": humanize_kind(kind),
            "gates": items,
            "count": len(items),
        })
    return out
