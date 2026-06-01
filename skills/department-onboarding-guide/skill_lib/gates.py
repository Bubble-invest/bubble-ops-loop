"""
gates.py — Step 5 helpers (autonomy modes + authorization bands).

Mirrors dept.schema.yaml lines 236-240 for the 5 modes.
"""
from __future__ import annotations

from typing import Dict, List


ALL_AUTONOMY_MODES: List[str] = [
    "manual_required",
    "manual_unless_policy_passed",
    "auto_if_policy_passed",
    "auto_with_veto_window",
    "disabled",
]


def build_authorization_band(
    band_id: str,
    allowed_types: List[str],
    forbidden: List[str],
) -> Dict[str, object]:
    """
    Build a per-dept authorization band (side-artifact accompanying gate policies).

    Bands are dept-domain-specific; they declare WHICH instances of an action
    class qualify for autonomy (e.g. low_risk_evergreen for social posts).
    """
    if not band_id:
        raise ValueError("band_id required")
    return {
        "id": band_id,
        "allowed_types": list(allowed_types),
        "forbidden": list(forbidden),
    }
