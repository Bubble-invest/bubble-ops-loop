"""test_host_field_hybrid_agent.py — Hybrid local/VPS agent ({{OPERATOR}} msg 4258/4274).

A dept can run its bubble-ops-loop on the VPS (default) OR on a local machine
(e.g. Miranda on {{OPERATOR_2}}'s Mac — to use real Chrome tabs / local apps) while staying
a first-class team member: visible + governable in the cockpit, gates in the UI,
its /loop running the REAL framework, just on a different host.

The `host` field in onboarding/STATE.yaml declares where the dept runs:
  - "vps"   (default — absent means vps, for back-compat with every existing dept)
  - "local"

Tests:
  1. STATE.yaml WITHOUT host validates (back-compat: existing depts → treated vps).
  2. STATE.yaml with host="vps" validates.
  3. STATE.yaml with host="local" validates.
  4. STATE.yaml with an invalid host ("cloud") is REJECTED.
"""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    import yaml
    from jsonschema import Draft7Validator
except ImportError as exc:  # pragma: no cover
    pytest.fail(f"missing dep: {exc}")


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # schemas-draft/
STATE_SCHEMA = ROOT / "state.schema.yaml"


def _schema() -> dict:
    return yaml.safe_load(STATE_SCHEMA.read_text(encoding="utf-8"))


def _base_state() -> dict:
    """A minimal valid STATE.yaml (mirrors the required fields)."""
    return {
        "schema_version": 1,
        "slug": "content",
        "display_name": "Miranda",
        "owner": "jade",
        "created_at": "2026-06-11T20:00:00Z",
        "status": "Idea",
        "validated_steps": [],
        "last_updated_at": "2026-06-11T20:00:00Z",
        "commits": [],
    }


def _errors(doc: dict) -> list:
    return list(Draft7Validator(_schema()).iter_errors(doc))


def test_state_without_host_is_valid_backcompat():
    doc = _base_state()
    assert "host" not in doc
    assert _errors(doc) == [], "existing STATE.yaml (no host) must stay valid"


def test_state_host_vps_is_valid():
    doc = _base_state()
    doc["host"] = "vps"
    assert _errors(doc) == []


def test_state_host_local_is_valid():
    doc = _base_state()
    doc["host"] = "local"
    assert _errors(doc) == []


def test_state_host_invalid_is_rejected():
    doc = _base_state()
    doc["host"] = "cloud"  # not a permitted host
    errs = _errors(doc)
    assert errs, "an unknown host value must be rejected by the schema"
