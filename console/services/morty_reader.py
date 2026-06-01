"""
morty_reader.py — read journald + audit-log data from Morty via SSH.

For v1 (this UX-3 PR) this is a thin stub returning placeholder rows. The
real journalctl wiring lands in UX-5 (deploy story). Tests don't depend on
SSH; this exists so /health can render structurally.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class LayerHeartbeat:
    dept: str
    layer: int
    last_success_iso: str  # "" if never
    cadence_minutes: int   # the dept's configured cadence
    is_stale: bool         # last_success older than 2x cadence


def per_dept_layer_heartbeats(depts: List[str]) -> List[LayerHeartbeat]:
    """
    Return one row per (dept x layer in {1,2,3,4}). v1 stub: marks all
    layers as never-run-yet (last_success_iso=""), cadence=20, stale=True.
    UX-5 wires this to `ssh hetzner journalctl -u ops-loop@<dept>` parsing.
    """
    rows: List[LayerHeartbeat] = []
    for dept in depts:
        for layer in (1, 2, 3, 4):
            rows.append(LayerHeartbeat(
                dept=dept, layer=layer, last_success_iso="",
                cadence_minutes=20, is_stale=True,
            ))
    return rows
