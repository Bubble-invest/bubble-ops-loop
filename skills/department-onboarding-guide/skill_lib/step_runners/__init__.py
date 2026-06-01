"""
skill_lib.step_runners — per-step conversational runners.

Refonte #1 of 3 (2026-05-21) replaces the legacy single-prompt-per-step
flow in `auto_drive.py` with a granular, conversational flow. Each of
the 7 Notion eclosure steps now has its own runner subclass under
`step_runners/<step>.py`.

Importing this package eagerly imports every registered runner so the
dispatcher (`get_runner`) is populated before first use. New runners
added by sub-agents #2 and #3 should follow the same pattern: define a
subclass of `StepRunner`, call `register_runner('<step>', MyRunner)` at
module bottom, and add an import line below.
"""
from __future__ import annotations

from .base import (
    Action,
    StepRunner,
    get_runner,
    register_runner,
    registered_steps,
)

# Import every runner module so its `register_runner()` call fires.
# Sub-agents #2 (missions, layers) and #3 (skills_tools, gates_kpis)
# add their imports here.
from . import mandate  # noqa: F401  (registers 'mandate')
from . import missions  # noqa: F401  (registers 'missions')
from . import layers  # noqa: F401  (registers 'layers')
from . import skills_tools  # noqa: F401  (refonte #3 — registers 'skills_tools')
from . import gates_kpis  # noqa: F401  (refonte #3 — registers 'gates_kpis')
from . import dry_run  # noqa: F401  (registers 'dry_run')
from . import activation  # noqa: F401  (registers 'activation')

__all__ = [
    "Action",
    "StepRunner",
    "get_runner",
    "register_runner",
    "registered_steps",
]
