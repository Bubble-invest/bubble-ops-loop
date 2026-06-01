"""
test_progression_state_machine.py — onboarding status state machine.

Notion v5 lines 794-801 — 7 statuses in this order:
  Idea -> Configuring -> Drafting -> Needs validation -> Dry run ->
  Ready to activate -> Live.

Invalid transitions (e.g. skipping states, or moving backwards without an
explicit revert) raise.
"""
from __future__ import annotations

import pytest

from skill_lib.state_machine import (
    OnboardingStateMachine,
    InvalidTransition,
    STATUSES,
)


def test_valid_status_transitions():
    sm = OnboardingStateMachine(current="Idea")
    for nxt in ["Configuring", "Drafting", "Needs validation", "Dry run", "Ready to activate", "Live"]:
        sm.advance_to(nxt)
        assert sm.current == nxt
    assert sm.current == "Live"


def test_cannot_skip_states():
    sm = OnboardingStateMachine(current="Idea")
    with pytest.raises(InvalidTransition):
        sm.advance_to("Live")  # skipping 5 states
    # Sanity: still at Idea after the failure.
    assert sm.current == "Idea"


def test_statuses_enumerated_in_canonical_order():
    """Defends the canonical order baked into the state machine."""
    assert STATUSES == [
        "Idea",
        "Configuring",
        "Drafting",
        "Needs validation",
        "Dry run",
        "Ready to activate",
        "Live",
    ]
