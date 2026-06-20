"""
test_materialize_input_kinds.py — unit tests for fix #175.

Verifies that ``materialize_due_missions`` gates output-only kinds
(those not declared as ``input_kinds`` by any mission) so they never
materialise as phantom cockpit cards.

Backward-compat: when NO mission declares ``input_kinds`` the old
behaviour is preserved (all creates[] emitted).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_LIB = PROJECT_ROOT / "scripts" / "lib"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import materialize_due_missions  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 6, 20, 10, 0, 0, tzinfo=timezone.utc)
_LAST: dict[str, datetime] = {}  # nothing fired yet → every cadence is due


def _due_mission(mission_id: str, creates: list[str],
                 input_kinds: list[str] | None = None,
                 layer: int = 2) -> dict:
    """Return a minimal mission dict that is always due (daily at 09:00)."""
    m: dict = {
        "id": mission_id,
        "layer": layer,
        "cadence": "daily",
        "time": "09:00",
        "output_queue": "queues/research/",
        "creates": creates,
    }
    if input_kinds is not None:
        m["input_kinds"] = input_kinds
    return m


def _kinds(items: list[dict]) -> set[str]:
    return {i["kind"] for i in items}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_output_only_kind_not_materialized_when_input_kinds_declared():
    """
    Mission A creates [prospect_dm, warming_outcome].
    Mission B declares input_kinds: [prospect_dm].
    Only prospect_dm should be emitted — warming_outcome is output-only.
    """
    mission_a = _due_mission("send_dm", creates=["prospect_dm", "warming_outcome"])
    mission_b = _due_mission("consume_dm", creates=[], input_kinds=["prospect_dm"])

    result = materialize_due_missions(
        [mission_a, mission_b],
        now=_NOW,
        last_fired_per_mission=_LAST,
    )
    emitted = _kinds(result)
    assert "prospect_dm" in emitted, "prospect_dm must be emitted (it's in input_kinds)"
    assert "warming_outcome" not in emitted, (
        "warming_outcome must be filtered (no mission declares it in input_kinds)"
    )


def test_no_input_kinds_backward_compat():
    """
    Same missions but WITHOUT any input_kinds declaration.
    Both prospect_dm AND warming_outcome must be emitted (old behaviour).
    """
    mission_a = _due_mission("send_dm", creates=["prospect_dm", "warming_outcome"])
    mission_b = _due_mission("other", creates=[])  # no input_kinds key at all

    result = materialize_due_missions(
        [mission_a, mission_b],
        now=_NOW,
        last_fired_per_mission=_LAST,
    )
    emitted = _kinds(result)
    assert "prospect_dm" in emitted
    assert "warming_outcome" in emitted, (
        "Without any input_kinds declaration, all creates[] must be emitted (backward-compat)"
    )


def test_kind_in_multiple_layers_allowed_kinds():
    """
    Mission A creates [kind_x, kind_y].
    Mission B has input_kinds: [kind_x].
    Mission C has input_kinds: [kind_y].
    Both kind_x and kind_y must be emitted (each appears in some layer's input_kinds).
    """
    mission_a = _due_mission("producer", creates=["kind_x", "kind_y"])
    mission_b = _due_mission("consumer_x", creates=[], input_kinds=["kind_x"])
    mission_c = _due_mission("consumer_y", creates=[], input_kinds=["kind_y"])

    result = materialize_due_missions(
        [mission_a, mission_b, mission_c],
        now=_NOW,
        last_fired_per_mission=_LAST,
    )
    emitted = _kinds(result)
    assert "kind_x" in emitted, "kind_x is declared in mission B's input_kinds → must be emitted"
    assert "kind_y" in emitted, "kind_y is declared in mission C's input_kinds → must be emitted"


def test_all_creates_filtered_when_none_in_input_kinds():
    """
    Mission A creates [only_output_kind].
    Mission B declares input_kinds: [something_else].
    Nothing should be emitted for mission A — its kind is not consumed anywhere.
    """
    mission_a = _due_mission("writer", creates=["only_output_kind"])
    mission_b = _due_mission("consumer", creates=[], input_kinds=["something_else"])

    result = materialize_due_missions(
        [mission_a, mission_b],
        now=_NOW,
        last_fired_per_mission=_LAST,
    )
    assert result == [], (
        "only_output_kind is not in any input_kinds → nothing should be emitted"
    )
