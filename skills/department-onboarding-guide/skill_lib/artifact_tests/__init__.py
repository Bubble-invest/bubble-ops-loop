"""
skill_lib.artifact_tests — per-artifact semantic + completeness testers.

Each module under this package registers a tester for a given artifact
kind. The dispatcher `test_artifact(kind, payload, ctx)` routes to the
right tester. All testers return a `TestResult` whose `summary_md` is
ready to relay to Telegram in Bureau-de-Cadre voice.

Refonte #1 of 3 ships 3 testers (mandate, dry_run_report,
activation_pr). Sub-agents #2 and #3 add `recurring_mission`,
`layer_focus`, `skill`, `tool`, `gate_policy`.
"""
from __future__ import annotations

from .base import (
    TestResult,
    test_artifact,
    register_tester,
    registered_kinds,
)

# Importing each sub-module fires its register_tester() call.
from . import mandate  # noqa: F401
from . import recurring_mission  # noqa: F401
from . import layer_focus  # noqa: F401
from . import dry_run_report  # noqa: F401
from . import activation_pr  # noqa: F401
from . import skill  # noqa: F401  (refonte #3 — per-skill tester)
from . import tool  # noqa: F401  (refonte #3 — per-tool tester)
from . import gate_policy  # noqa: F401  (refonte #3 — per-gate-policy tester)

__all__ = [
    "TestResult",
    "test_artifact",
    "register_tester",
    "registered_kinds",
]
