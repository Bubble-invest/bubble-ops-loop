"""
Surprise-1 guard — Layer 2 PROMPT.md must specify every gate-item v3
required field plus the 5-mode official autonomy vocabulary.

Empirical context (verified 2026-05-20 on /tmp/bubble-ops-fixture):
the live Layer-2 prompt emits gate YAML files that are missing 8
root-level required fields per `schemas-draft/gate-item.schema.yaml`
v3 (source_layer, target_layer, risk_level, requires_human, actions,
current_mode, future_eligible_modes, authorization_band_id) AND uses
a `kind: research_decision` that satisfies neither the v3 enum nor
the `^domain:[a-z_]+$` pattern.

Fix: rewrite Layer 2's PROMPT.md so it tells the orchestrator exactly
which fields to populate and which autonomy mode vocabulary to use.

This test guards against the prompt drifting AGAIN later. It loads the
canonical PROMPT.md the project ships and asserts:

  1. Every gate-item v3 required field name appears verbatim.
  2. The 5 OFFICIAL autonomy modes are mentioned by name.
  3. The shorthand `shadow_autonomy` / `full_autonomy` is NOT present.
  4. A YAML skeleton fence shows every field with an example value.

Run:
    python3 -m pytest tests/test_layer2_prompt_specifies_required_fields.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent

# The canonical Layer 2 PROMPT.md template the project ships. The live
# fixture's layers/2/PROMPT.md is expected to be kept in sync with this
# (Rick handles the push via broker+guard).
CANONICAL_PROMPT_PATH = (
    PROJECT_ROOT
    / "skills"
    / "department-onboarding-guide"
    / "templates"
    / "layer_2_prompt.md.template"
)

REQUIRED_GATE_FIELDS = [
    "id",
    "kind",
    "source_layer",
    "target_layer",
    "risk_level",
    "requires_human",
    "actions",
    "current_mode",
    "future_eligible_modes",
    "authorization_band_id",
]

OFFICIAL_AUTONOMY_MODES = [
    "manual_required",
    "manual_unless_policy_passed",
    "auto_if_policy_passed",
    "auto_with_veto_window",
    "disabled",
]

FORBIDDEN_SHORTHAND_MODES = ["shadow_autonomy", "full_autonomy"]


@pytest.fixture(scope="module")
def prompt_text() -> str:
    assert CANONICAL_PROMPT_PATH.exists(), (
        f"Canonical Layer-2 prompt missing at {CANONICAL_PROMPT_PATH}. "
        "Create it so the live fixture has a source-of-truth template."
    )
    return CANONICAL_PROMPT_PATH.read_text(encoding="utf-8")


@pytest.mark.parametrize("field", REQUIRED_GATE_FIELDS)
def test_prompt_mentions_required_gate_field(prompt_text: str, field: str) -> None:
    """Every gate-item v3 required field must be named in the prompt."""
    assert field in prompt_text, (
        f"Layer-2 PROMPT.md does not mention required gate field `{field}`. "
        "The generated gate will violate gate-item.schema.yaml v3."
    )


@pytest.mark.parametrize("mode", OFFICIAL_AUTONOMY_MODES)
def test_prompt_mentions_official_autonomy_mode(prompt_text: str, mode: str) -> None:
    """All 5 official autonomy modes (Notion v5 lines 256-260) must appear."""
    assert mode in prompt_text, (
        f"Layer-2 PROMPT.md is missing official autonomy mode `{mode}`. "
        "Operators may fall back to the deprecated shorthand vocabulary."
    )


@pytest.mark.parametrize("shorthand", FORBIDDEN_SHORTHAND_MODES)
def test_prompt_does_not_use_shorthand_autonomy_vocab(
    prompt_text: str, shorthand: str
) -> None:
    """The shorthand `shadow_autonomy` / `full_autonomy` must not appear.
    Use one of the 5 official modes (Notion v5 lines 256-260) instead."""
    assert shorthand not in prompt_text, (
        f"Layer-2 PROMPT.md mentions shorthand `{shorthand}` — replace with "
        "an official mode from the 5-mode enum (see Notion v5 lines 256-260)."
    )


def test_prompt_contains_yaml_skeleton_with_every_required_field(
    prompt_text: str,
) -> None:
    """The prompt must include at least one fenced YAML skeleton example
    that demonstrates every required field together (not just scattered
    prose mentions). Without a concrete example the orchestrator drifts.

    We look for an unbroken ```yaml...``` block that contains every
    required field name."""
    blocks: list[str] = []
    in_block = False
    current: list[str] = []
    for line in prompt_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```yaml"):
            in_block = True
            current = []
            continue
        if in_block and stripped.startswith("```"):
            blocks.append("\n".join(current))
            in_block = False
            current = []
            continue
        if in_block:
            current.append(line)

    assert blocks, "Layer-2 PROMPT.md has no ```yaml fenced block."

    fully_covering = [
        b for b in blocks if all(f in b for f in REQUIRED_GATE_FIELDS)
    ]
    assert fully_covering, (
        "No single fenced ```yaml block in Layer-2 PROMPT.md contains every "
        f"required field. Required: {REQUIRED_GATE_FIELDS}. Add a complete "
        "gate-item example so the orchestrator has a concrete shape to follow."
    )
