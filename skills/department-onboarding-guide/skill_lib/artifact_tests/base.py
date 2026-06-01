"""
artifact_tests/base.py — Refonte #1 of 3, Deliverable D.

Defines the minimal `TestResult` dataclass that every per-artifact
tester returns + the central dispatcher.

Sub-agents #2 and #3 will add `recurring_mission.py`, `layer_focus.py`,
`skill.py`, `tool.py`, and `gate_policy.py` testers. They register via
`register_tester(kind, fn)` and the dispatcher routes on the `kind`
argument.

The framework intentionally does NOT validate against the JSON schemas
itself — that's the schema validator's job. These testers focus on
**semantic** + **completeness** checks: "is this mandate substantive
enough to ship?", "is this gate policy mode consistent with the
guardrails declared?", etc.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


@dataclass
class TestResult:
    """Outcome of a single artifact test.

    Fields:
      passed       — True iff the artifact is good enough to ship as-is.
      issues       — non-empty list of blocking problems (when passed=False).
                     Each issue is a short FR sentence the operator can
                     act on directly.
      suggestions  — non-blocking polish suggestions. May be set even
                     when passed=True (the runner can choose to surface
                     them or stay quiet).
      summary_md   — short markdown the runner relays to Telegram.
                     Bureau-de-Cadre voice, French.
    """

    # Tell pytest this is not a test class (the Test* prefix would
    # otherwise cause collection warnings).
    __test__ = False

    passed: bool
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    summary_md: str = ""


# A tester takes (payload, dept_context) and returns a TestResult.
# `dept_context` carries auxiliary info (the dept slug, dept root path,
# the full dept.yaml.draft, etc.) — testers ignore it if they don't
# need it.
TesterFn = Callable[[Any, Any], TestResult]


_REGISTRY: Dict[str, TesterFn] = {}


def register_tester(kind: str, fn: TesterFn) -> None:
    """Register a tester for a given artifact kind."""
    _REGISTRY[kind] = fn


def test_artifact(
    artifact_kind: str,
    artifact_payload: Any,
    dept_context: Optional[Any] = None,
) -> TestResult:
    """Dispatch to the registered tester for `artifact_kind`.

    Raises ValueError if no tester is registered for the kind.
    """
    if artifact_kind not in _REGISTRY:
        known = sorted(_REGISTRY.keys()) or ["<none registered yet>"]
        raise ValueError(
            f"No artifact tester registered for {artifact_kind!r}. "
            f"Known: {known}"
        )
    return _REGISTRY[artifact_kind](artifact_payload, dept_context)


# Pytest would otherwise try to collect this as a test (name starts with
# `test_`). Mark it so. Same trick for each per-artifact tester below.
test_artifact.__test__ = False  # type: ignore[attr-defined]


def registered_kinds() -> List[str]:
    return sorted(_REGISTRY.keys())
