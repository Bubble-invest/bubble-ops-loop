"""
test_step3_layers_runner.py — Refonte #2 of 3, Deliverable C.

Pins the conversational behavior of `LayersRunner` (the per-step
runner that walks the operator through Notion's per-layer focalisation
question for Step 3 — Mapping des 4 layers).

Notion v5 lines 847-862 (verbatim):
    847  ### 3. Mapping des 4 layers
    848  L'agent demande : "Que dois-je faire à chaque étape du flow
    849  standard ?"
    850  Exemple Miranda :
    851    Layer 1 — Data
    852    Scanner signaux contenus, calendrier, idées, performances passées.
    853    Layer 2 — Research / Plan
    854    Transformer les signaux en idées de posts, drafts, variantes, angles.
    855    Layer 3 — Execution
    856    Programmer / publier / mettre en draft après gate.
    857    Layer 4 — Risk / Quality
    858    Auditer brand safety, ton, performance, répétition, fatigue audience.

Plus the generic Layer N descriptions from Notion v5 lines 440-468.

The runner first asks which layers the dept subscribes to (substep A),
then loops over each subscribed layer as substep B(N) — recalling the
doctrinal 1-liner for that layer, proposing a focalisation, then on
APPROVE writing layers/<N>/PROMPT.md.
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.layers import (
    LayersRunner,
    SUBSTEP_SUBSCRIBED_LAYERS,
    SUBSTEP_LAYER_FOCUS,
    _LAYER_GENERIC_DESCRIPTION,
)


# ----- helpers -----


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "STATE.yaml"
    state.write_text(yaml.safe_dump({
        "schema_version": 1,
        "slug": "miranda",
        "display_name": "Miranda",
        "owner": "joris",
        "created_at": "2026-05-21T08:00:00Z",
        "status": "Drafting",
        "validated_steps": ["mandate", "missions"],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text(yaml.safe_dump({
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "owner": "joris",
            "mandate": "Produire, planifier et auditer du contenu social.",
            "outputs": "drafts de posts, calendrier",
            "forbidden": ["publier sans validation"],
            "success_criteria": ["0 breach", "edit_rate <= 20%"],
            "status": "onboarding",
        }
    }, sort_keys=False), encoding="utf-8")
    return draft


def _start(tmp_path: Path) -> LayersRunner:
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("layers")
    runner.start(state, draft)
    return runner


# ----- substep A : subscribed layers selection -----


def test_first_prompt_is_substep_a_layer_choice(tmp_path):
    runner = _start(tmp_path)
    prompt = runner.next_prompt()
    assert prompt is not None
    assert "Étape 3" in prompt
    # Must mention each of the 4 layers and the moment naming.
    for needle in ("Layer 1", "Layer 2", "Layer 3", "Layer 4"):
        assert needle in prompt
    assert "matin" in prompt.lower() or "Data" in prompt


def test_substep_a_parses_numeric_list(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("1, 3")
    assert runner._subscribed_layers == [1, 3]


def test_substep_a_parses_et_separator(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("1 et 3")
    assert runner._subscribed_layers == [1, 3]


def test_substep_a_parses_named_moments(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("matin et exécution")
    # matin → Layer 1, exécution → Layer 3
    assert 1 in runner._subscribed_layers
    assert 3 in runner._subscribed_layers


def test_substep_a_parses_tous(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("tous")
    assert runner._subscribed_layers == [1, 2, 3, 4]


# ----- substep B : per-layer focalisation -----


def test_each_subscribed_layer_triggers_substep(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("1, 2")
    # Now next_prompt should propose Layer 1 focalisation.
    p = runner.next_prompt()
    assert p is not None
    assert "Layer 1" in p


def test_generic_layer_description_is_surfaced_verbatim(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("2")
    p = runner.next_prompt()
    assert p is not None
    # The doctrinal 1-liner must appear.
    assert _LAYER_GENERIC_DESCRIPTION[2]["one_liner"] in p


def test_approve_triggers_prompt_md_generation_and_commit(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("1")
    runner.next_prompt()
    action = runner.on_answer("approuve")
    assert action in (Action.APPROVE_SUBSTEP, Action.DONE)
    pmd = tmp_path / "layers" / "1" / "PROMPT.md"
    assert pmd.exists()
    body = pmd.read_text(encoding="utf-8")
    # The generic description for Layer 1 must be in the file.
    assert _LAYER_GENERIC_DESCRIPTION[1]["one_liner"] in body
    # And the focalisation block.
    assert "focalisation" in body.lower() or "Focalisation" in body


def test_layer_1_prompt_md_has_outputs_section(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("1")
    runner.next_prompt()
    runner.on_answer("approuve")
    pmd = tmp_path / "layers" / "1" / "PROMPT.md"
    body = pmd.read_text(encoding="utf-8")
    assert "Outputs" in body or "outputs" in body


def test_is_done_only_when_all_subscribed_layers_have_prompt(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("1, 2")
    assert runner.is_done() is False
    runner.next_prompt()
    runner.on_answer("approuve")  # Layer 1
    assert runner.is_done() is False
    runner.next_prompt()
    runner.on_answer("approuve")  # Layer 2
    assert runner.is_done() is True


def test_non_subscribed_layer_has_no_prompt_md(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("1, 3")
    runner.next_prompt()
    runner.on_answer("approuve")
    runner.next_prompt()
    runner.on_answer("approuve")
    # Layer 2 was not subscribed → no PROMPT.md
    assert not (tmp_path / "layers" / "2" / "PROMPT.md").exists()
    # Layer 4 also not subscribed
    assert not (tmp_path / "layers" / "4" / "PROMPT.md").exists()
    # 1 and 3 are present
    assert (tmp_path / "layers" / "1" / "PROMPT.md").exists()
    assert (tmp_path / "layers" / "3" / "PROMPT.md").exists()


def test_idempotent_on_resume(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner1 = get_runner("layers")
    runner1.start(state, draft)
    runner1.on_answer("1, 2")
    runner1.next_prompt()
    runner1.on_answer("approuve")  # Layer 1
    # Resume with a fresh runner
    runner2 = get_runner("layers")
    runner2.start(state, draft)
    # Layer 1 should be remembered
    assert runner2.is_done() is False
    p = runner2.next_prompt()
    assert p is not None
    # Should be on Layer 2 now
    assert "Layer 2" in p


def test_edit_applies_operator_text_to_focalisation(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("1")
    runner.next_prompt()
    action = runner.on_answer(
        "édite: je vais aussi scanner les comptes concurrents"
    )
    assert action == Action.EDIT
    p = runner.next_prompt()
    assert p is not None
    assert "concurrents" in p


def test_refine_generates_different_focalisation(tmp_path):
    runner = _start(tmp_path)
    runner.on_answer("1")
    p1 = runner.next_prompt()
    action = runner.on_answer("raffine")
    assert action == Action.REFINE
    p2 = runner.next_prompt()
    assert p2 is not None
    assert p1 != p2


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
