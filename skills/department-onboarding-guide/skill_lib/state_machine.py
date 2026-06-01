"""
state_machine.py — onboarding status state machine.

Notion v5 lines 794-801: 7 statuses, strict linear progression.
"""
from __future__ import annotations

from typing import List


STATUSES: List[str] = [
    "Idea",
    "Configuring",
    "Drafting",
    "Needs validation",
    "Dry run",
    "Ready to activate",
    "Live",
]


class InvalidTransition(Exception):
    """Raised when a state transition would skip states or move backwards."""


class OnboardingStateMachine:
    """Strict linear state machine over the 7 onboarding statuses."""

    def __init__(self, current: str = "Idea") -> None:
        if current not in STATUSES:
            raise ValueError(f"Unknown status: {current}")
        self.current = current

    def advance_to(self, target: str) -> None:
        """Advance to `target`. Must be the immediate next status."""
        if target not in STATUSES:
            raise InvalidTransition(f"Unknown status: {target}")
        cur_idx = STATUSES.index(self.current)
        tgt_idx = STATUSES.index(target)
        if tgt_idx != cur_idx + 1:
            raise InvalidTransition(
                f"Cannot advance from {self.current!r} to {target!r}: "
                f"must move to {STATUSES[cur_idx + 1]!r} next"
                if cur_idx + 1 < len(STATUSES)
                else f"Cannot advance past terminal state {self.current!r}"
            )
        self.current = target

    def reset_to(self, target: str) -> None:
        """Explicit revert (used only by operator when a step is invalidated)."""
        if target not in STATUSES:
            raise InvalidTransition(f"Unknown status: {target}")
        self.current = target
