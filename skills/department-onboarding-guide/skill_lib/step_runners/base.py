"""
step_runners/base.py — Refonte #1 of 3, Deliverable B.

The conversational onboarding flow needs each of the 7 Notion eclosure
steps (mandate, missions, layers, skills_tools, gates_kpis, dry_run,
activation) to be granular and iterative: the operator may validate
sub-artifacts (one mission at a time, one skill at a time) within a
step. This module defines the ABC that every per-step runner must obey
so the orchestration layer (`auto_drive.py` v2) can dispatch uniformly.

Public surface:
    Action               — enum of the 5 possible on_answer outcomes
    StepRunner           — ABC with 5 abstract methods
    get_runner(step_name)— factory dispatcher (registered runners only)

Design rules — do NOT break (sub-agents #2 and #3 rely on these):
  - All operator-facing text is French, Bureau-de-Cadre voice.
  - All artifacts the runner writes are recorded via artifacts_produced().
  - on_answer() never raises on a garbled operator answer; it returns
    Action.CONTINUE and leaves it to next_prompt() to re-explain.
  - is_done() must be a pure read: calling it does not mutate state.
  - step_name is a class attribute; subclasses MUST set it.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Type


class Action(str, Enum):
    """Outcomes of `StepRunner.on_answer()`.

    The runner uses this enum to tell the orchestration layer what just
    happened so it can decide whether to:
      - keep asking the same question (CONTINUE),
      - move to the next sub-artifact (APPROVE_SUBSTEP),
      - record an operator edit (EDIT),
      - record an operator refine request (REFINE),
      - close the step (DONE).
    """

    CONTINUE = "continue"
    APPROVE_SUBSTEP = "approve_substep"
    EDIT = "edit"
    REFINE = "refine"
    DONE = "done"


class StepRunner(ABC):
    """Abstract base class for every per-step runner.

    Concrete runners live in `step_runners/<step>.py` and are registered
    in `_REGISTRY` below. Each runner owns:

      - reading + writing its sub-section of `step_progress` in STATE.yaml
      - composing the Telegram-ready prompt for the operator
      - parsing the operator's free-text answer into an Action
      - writing the artifact(s) (MANDATE.md, dept.yaml.draft sections,
        missions/*.yaml, etc.) that the step produces

    The base class does NOT impose a particular state-storage shape on
    subclasses — it only pins the conversational contract.
    """

    #: Subclasses MUST set this. Used by get_runner() and for tagging
    #: entries in STATE.yaml::step_progress.
    step_name: str = ""

    def __init__(self) -> None:
        # Common fields. Subclasses populate them in start().
        self.state_path: Optional[Path] = None
        self.dept_yaml_draft_path: Optional[Path] = None

    @abstractmethod
    def start(
        self,
        state_path: Path,
        dept_yaml_draft_path: Path,
    ) -> None:
        """Initialize step_progress[self.step_name] and stash paths.

        Called once per step. Idempotent — if the step was already
        started (operator resumed mid-flow), the runner must pick up
        where it left off rather than reset.
        """

    @abstractmethod
    def next_prompt(self) -> Optional[str]:
        """Return the next Telegram-ready FR prompt, or None if the step
        is fully done.

        next_prompt() may be called multiple times in a row (the
        orchestrator polls); the runner must return the SAME prompt
        until the operator answers, then advance.
        """

    @abstractmethod
    def on_answer(self, operator_text: str) -> Action:
        """Parse the operator's free-text answer.

        Must never raise on garbled input — return Action.CONTINUE
        instead so next_prompt() can re-explain.

        Side-effects allowed: append to step_progress[step].
        sub_artifacts_validated, update current_substep, write a draft
        to dept.yaml.draft, etc.
        """

    @abstractmethod
    def is_done(self) -> bool:
        """True iff all required sub-artifacts for this step are validated.

        Pure read — no mutations. Used by the orchestrator's idempotent
        post-step check (before calling record_step_completion()).
        """

    @abstractmethod
    def artifacts_produced(self) -> List[Path]:
        """Return the list of artifact files the runner has written so far.

        Used by record_step_completion() to populate commits[].artifacts
        for audit.
        """


# ---------------------------------------------------------------------------
# Registry + dispatcher
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, Type[StepRunner]] = {}


def register_runner(step_name: str, cls: Type[StepRunner]) -> None:
    """Add `cls` to the dispatch table under `step_name`.

    Sub-agents register their step-runners by importing their module
    (the import-time side-effect adds the entry). The registry is
    process-local; tests can rely on it stabilising once
    `skill_lib.step_runners` is imported.
    """
    if not issubclass(cls, StepRunner):
        raise TypeError(f"{cls!r} is not a StepRunner subclass")
    _REGISTRY[step_name] = cls


def get_runner(step_name: str) -> StepRunner:
    """Return a fresh instance of the runner registered for `step_name`.

    Raises ValueError when the step has no registered runner; the
    message lists the known step ids so callers can spot typos.
    """
    if step_name not in _REGISTRY:
        known = sorted(_REGISTRY.keys()) or ["<none registered yet>"]
        raise ValueError(
            f"No StepRunner registered for {step_name!r}. "
            f"Known: {known}"
        )
    cls = _REGISTRY[step_name]
    return cls()


def registered_steps() -> List[str]:
    """Return the list of registered step ids (sorted, copy)."""
    return sorted(_REGISTRY.keys())
