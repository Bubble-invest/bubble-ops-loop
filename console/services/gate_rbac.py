"""Authorization policy for mutating human gate decisions.

Authentication establishes *who* is calling the console.  This module answers
the separate question of whether that principal may decide or undo a gate for
a particular department.
"""
from __future__ import annotations

import json

from console import settings


def _load_grants() -> dict[str, frozenset[str]]:
    """Parse ``CONSOLE_GATE_RBAC`` without ever failing open.

    The accepted shape is ``{"rick": ["*"] , "jade": ["content"]}``.
    Malformed JSON, non-list grants, and non-string entries are ignored.  An
    absent or malformed policy therefore authorizes nobody.
    """
    raw = settings.GATE_RBAC_JSON
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, dict):
        return {}

    grants: dict[str, frozenset[str]] = {}
    for principal, departments in value.items():
        if not isinstance(principal, str) or not isinstance(departments, list):
            continue
        clean = frozenset(
            department.strip()
            for department in departments
            if isinstance(department, str) and department.strip()
        )
        if clean:
            grants[principal] = clean
    return grants


def may_decide(principal: str | None, department: str) -> bool:
    """Return whether ``principal`` may mutate gates for ``department``."""
    if not principal or not department:
        return False
    allowed = _load_grants().get(principal)
    return bool(allowed and ("*" in allowed or department in allowed))
