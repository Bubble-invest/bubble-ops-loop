"""
test_step1_mandate_runner.py — Refonte #1 of 3, Deliverable C.

Pins the conversational behavior of `MandateRunner` (the per-step
runner that walks the operator through Notion's 6 mandatory
clarifications at Step 1 of the eclosure).

Notion v5 lines 803-829 (verbatim):
    805  L'agent clarifie :
    806  - rôle du département ;
    807  - utilisateur / owner ;
    808  - outputs attendus ;
    809  - hors périmètre ;
    810  - interdits ;
    811  - critères de succès.

The runner must produce a `dept.yaml.draft` whose `department:` block
holds: slug, display_name, mandate (the sentence), owner, outputs,
forbidden[], success_criteria[]. The runner also writes MANDATE.md
(the long-form mandate doc).
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from skill_lib.step_runners import Action, get_runner
from skill_lib.step_runners.mandate import (
    MandateRunner,
    SUBSTEP_CLARIFICATIONS,
    SUBSTEP_STYLE_AND_SENTENCE,
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
        "status": "Configuring",
        "validated_steps": [],
        "last_updated_at": "2026-05-21T08:00:00Z",
        "commits": [],
    }, sort_keys=False), encoding="utf-8")
    return state


def _seed_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "dept.yaml.draft"
    draft.write_text("", encoding="utf-8")
    return draft


def _walk_substep_a(runner: MandateRunner, style: str = "1") -> None:
    """Drive substep A through to validation with the seed sentence."""
    runner.on_answer(style)
    runner.on_answer("approuve")


def _walk_substep_b(runner: MandateRunner) -> None:
    """Drive substep B with a canonical 3-line answer (forbidden / level / owner).

    Sprint correctif Fix 2 (2026-05-21): outputs + success_criteria
    removed (they violate the dept.schema.yaml additionalProperties:false
    on the department block); `level` added (now required, schema lines
    49-56).
    """
    body = (
        "publier sans validation, nommer clients\n"
        "ops\n"
        "joris"
    )
    runner.on_answer(body)
    runner.on_answer("approuve")


# ----- tests -----


def test_first_prompt_is_substep_a_style_choice(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    prompt = runner.next_prompt()
    assert prompt is not None
    assert "Étape 1" in prompt
    assert "Mandat resserré" in prompt
    assert "Mandat équilibré" in prompt
    assert "Mandat large" in prompt
    # Bureau-de-Cadre voice → no English keywords.
    assert "Approve" not in prompt and "Refine" not in prompt


def test_substep_a_style_choice_then_seed_sentence(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    # Pick style "2" (équilibré)
    action = runner.on_answer("2")
    assert action == Action.CONTINUE
    p2 = runner.next_prompt()
    assert p2 is not None
    assert "équilibré" in p2
    # The seed sentence must be quoted.
    assert "Je couvre" in p2 or "couvre 2" in p2


def test_substep_a_approve_writes_mandate_to_draft(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    runner.on_answer("1")
    action = runner.on_answer("approuve")
    assert action == Action.APPROVE_SUBSTEP
    doc = yaml.safe_load(draft.read_text(encoding="utf-8"))
    assert "department" in doc
    assert doc["department"]["mandate"].startswith("Je m'occupe")
    assert doc["department"]["slug"] == "miranda"
    assert doc["department"]["status"] == "onboarding"


def test_substep_a_edit_records_new_sentence(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    runner.on_answer("3")
    # Edit with inline sentence
    action = runner.on_answer("édite: Je connecte 3 flux internes pour Tony.")
    assert action == Action.EDIT
    # Now the next prompt should quote the edited sentence
    p = runner.next_prompt()
    assert p is not None
    assert "Je connecte 3 flux internes" in p


def test_substep_b_starts_after_substep_a(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    _walk_substep_a(runner, style="1")
    p = runner.next_prompt()
    assert p is not None
    assert "2/2" in p
    # The 3 clarification subjects per Notion v5 813-825 must be listed.
    # Sprint correctif Fix 2 (2026-05-21).
    for keyword in ("interdits", "niveau", "owner"):
        assert keyword in p, f"substep B prompt missing `{keyword}`"


def test_substep_b_parses_3_line_answer_into_dept_yaml(tmp_path):
    """Sprint correctif Fix 2 (2026-05-21): substep B is now 3 lines
    (interdits / niveau / owner) per Notion v5 813-825.
    """
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    _walk_substep_a(runner, style="2")
    # Send the 3-line block.
    body = (
        "publier sans validation, nommer clients, conseil financier\n"
        "ops\n"
        "joris"
    )
    action = runner.on_answer(body)
    assert action == Action.CONTINUE  # parsed, awaiting approve
    # Now the prompt should echo back the parsed dict for approval.
    p = runner.next_prompt()
    assert p is not None
    assert "publier sans validation" in p
    assert "ops" in p
    # Approve → step done.
    action = runner.on_answer("approuve")
    assert action in (Action.APPROVE_SUBSTEP, Action.DONE)
    doc = yaml.safe_load(draft.read_text(encoding="utf-8"))
    dept = doc["department"]
    assert "publier sans validation" in dept["forbidden"]
    assert "conseil financier" in dept["forbidden"]
    assert dept["level"] == "ops"
    assert dept["owner"] == "joris"
    # Legacy fields must NOT appear (schema rejects them).
    assert "outputs" not in dept
    assert "success_criteria" not in dept


def test_runner_writes_mandate_md_when_done(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    _walk_substep_a(runner, style="1")
    _walk_substep_b(runner)
    mandate_md = tmp_path / "MANDATE.md"
    assert mandate_md.exists()
    body = mandate_md.read_text(encoding="utf-8")
    assert "# Mandat de Miranda" in body
    assert "Je m'occupe" in body  # the sentence
    assert "Ce que je dois produire" in body
    assert "Ce que je ne dois jamais faire" in body
    assert "Comment on saura" in body


def test_is_done_false_until_all_7_fields_present(tmp_path):
    """Sprint correctif Fix 2 (2026-05-21): 7 canonical fields (added
    level + status; removed outputs + success_criteria).
    """
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    assert runner.is_done() is False
    _walk_substep_a(runner, style="1")
    assert runner.is_done() is False  # only substep A done
    _walk_substep_b(runner)
    assert runner.is_done() is True


def test_artifacts_produced_lists_draft_and_mandate_md(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    _walk_substep_a(runner, style="1")
    _walk_substep_b(runner)
    files = runner.artifacts_produced()
    file_names = {p.name for p in files}
    assert "dept.yaml.draft" in file_names
    assert "MANDATE.md" in file_names


def test_runner_persists_progress_in_state_yaml(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    runner.on_answer("1")  # style picked → current_substep populated
    doc = yaml.safe_load(state.read_text(encoding="utf-8"))
    assert "step_progress" in doc
    progress = doc["step_progress"]["mandate"]
    assert progress["current_status"] == "awaiting_validation"
    assert progress["current_substep"]["type"] == SUBSTEP_STYLE_AND_SENTENCE
    assert progress["current_substep"]["draft_payload"]["style"] == "1"


def test_runner_is_idempotent_on_resume(tmp_path):
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner1 = get_runner("mandate")
    runner1.start(state, draft)
    _walk_substep_a(runner1, style="2")
    # Simulate session restart: a fresh runner picks up where we left off.
    runner2 = get_runner("mandate")
    runner2.start(state, draft)
    assert runner2.is_done() is False
    # The next prompt should be substep B, not substep A again.
    p = runner2.next_prompt()
    assert p is not None
    assert "2/2" in p


def test_runner_garbled_answer_returns_continue(tmp_path):
    """on_answer must never raise on garbled input — return CONTINUE."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    # No style digit; the runner should keep waiting.
    action = runner.on_answer("euh, je sais pas")
    assert action == Action.CONTINUE
    # The original prompt should still appear.
    p = runner.next_prompt()
    assert p is not None
    assert "Mandat resserré" in p


def test_final_dept_yaml_passes_schema_for_required_subset(tmp_path):
    """The dept.yaml.draft after step 1 contains all 7 canonical Notion fields.

    Sprint correctif Fix 2 (2026-05-21): updated to v3 canonical 7-field
    shape (slug / display_name / level / status / mandate / owner / forbidden).
    """
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    _walk_substep_a(runner, style="2")
    _walk_substep_b(runner)
    doc = yaml.safe_load(draft.read_text(encoding="utf-8"))
    dept = doc["department"]
    for f in ("slug", "display_name", "level", "status",
              "mandate", "owner", "forbidden"):
        assert dept.get(f) is not None, f"missing field: {f!r}"
    assert isinstance(dept["forbidden"], list)
    assert dept["level"] in {"ops", "management", "principal"}
    assert len(dept["mandate"]) >= 20  # non-trivial


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
