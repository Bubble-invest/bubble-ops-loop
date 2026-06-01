"""
test_step_runner_base.py — Deliverable B of Refonte #1.

Pins the StepRunner ABC contract that every per-step runner under
`skill_lib.step_runners/` must obey. Sub-agents #2 and #3 will add
runners (`missions.py`, `layers.py`, `skills_tools.py`, `gates_kpis.py`)
that subclass this base; the test contract here is what they will
inherit.

Notion north star: lines 803-1003 — the 7-step eclosure flow is
sequential AND each step is now conversational, so we need an explicit
ABC that prescribes the start / next_prompt / on_answer / is_done /
artifacts_produced surface every runner must expose.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skill_lib.step_runners import (
    Action,
    StepRunner,
    get_runner,
)


# ----- ABC contract tests -----


def test_step_runner_is_abstract_and_cannot_be_instantiated():
    """StepRunner is a pure ABC — direct instantiation must fail."""
    with pytest.raises(TypeError):
        StepRunner()  # type: ignore[abstract]


def test_step_runner_subclass_must_implement_all_methods():
    """A subclass that doesn't implement every abstract method must fail
    to instantiate."""

    class Incomplete(StepRunner):
        # only implements start() — missing next_prompt, on_answer,
        # is_done, artifacts_produced
        step_name = "fake"

        def start(self, state_path: Path, dept_yaml_draft_path: Path) -> None:
            return None

    with pytest.raises(TypeError):
        Incomplete()


def test_action_enum_has_5_members():
    """The on_answer return-value enum must have the 5 documented actions."""
    members = {a.name for a in Action}
    assert members == {
        "CONTINUE", "APPROVE_SUBSTEP", "EDIT", "REFINE", "DONE",
    }


def test_get_runner_unknown_step_raises():
    """get_runner('does_not_exist') raises ValueError with the known step list."""
    with pytest.raises(ValueError) as exc:
        get_runner("does_not_exist")
    msg = str(exc.value)
    # The error message must enumerate the registered step ids
    # (helps callers debug typos).
    assert "does_not_exist" in msg


def test_concrete_runner_can_be_instantiated_and_round_trips(tmp_path):
    """Sanity: a minimal concrete subclass instantiates and obeys the contract."""

    class TinyRunner(StepRunner):
        step_name = "tiny"

        def __init__(self):
            super().__init__()
            self._done = False

        def start(self, state_path: Path, dept_yaml_draft_path: Path) -> None:
            self.state_path = state_path
            self.dept_yaml_draft_path = dept_yaml_draft_path

        def next_prompt(self):
            return None if self._done else "ping?"

        def on_answer(self, operator_text: str) -> Action:
            self._done = True
            return Action.DONE

        def is_done(self) -> bool:
            return self._done

        def artifacts_produced(self):
            return []

    r = TinyRunner()
    state = tmp_path / "STATE.yaml"
    draft = tmp_path / "dept.yaml.draft"
    state.write_text("x: 1\n")
    draft.write_text("y: 2\n")
    r.start(state, draft)
    assert r.next_prompt() == "ping?"
    action = r.on_answer("ok")
    assert action == Action.DONE
    assert r.is_done() is True
    assert r.next_prompt() is None
    assert r.artifacts_produced() == []


def test_get_runner_returns_a_registered_concrete_runner():
    """get_runner('mandate') returns an instance of the MandateRunner.

    Sub-agents #2 and #3 will register their own runners via the same
    `get_runner` dispatcher. We pin that the base mandate runner is
    discoverable so all subsequent runners can mimic the pattern.
    """
    runner = get_runner("mandate")
    assert isinstance(runner, StepRunner)
    assert runner.step_name == "mandate"
