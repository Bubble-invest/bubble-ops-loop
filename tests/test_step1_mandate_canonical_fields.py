"""
test_step1_mandate_canonical_fields.py — Sprint correctif Fix 2.

After Step 1 (Mandate) is walked end-to-end, the `department:` block in
dept.yaml.draft must contain EXACTLY the 7 canonical Notion v5 fields
(lines 813-825):

    slug, display_name, level, status, mandate, owner, forbidden

NO extras (`outputs` and `success_criteria` are not in Notion 813-825 and
the schema rejects them via `additionalProperties: false` on the
department block — cf. dept.schema.yaml line 35).

`level` MUST be one of: ops, management, principal (schema lines 49-56).
The runner must ask the operator for it during substep B.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
sys.path.insert(0, str(SKILL_ROOT))

from skill_lib.step_runners import get_runner


_CANONICAL_FIELDS = {
    "slug", "display_name", "level", "status", "mandate", "owner", "forbidden",
}

_FORBIDDEN_LEGACY_FIELDS = {"outputs", "success_criteria"}


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


def _walk_full_step1(tmp_path: Path) -> dict:
    """Drive the mandate runner end-to-end with the new (level-aware) substep B."""
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    # Substep A
    runner.on_answer("1")
    runner.on_answer("approuve")
    # Substep B — new shape: interdits / niveau / owner (3 lines).
    body = (
        "publier sans validation, nommer clients, conseil financier\n"
        "ops\n"
        "joris"
    )
    runner.on_answer(body)
    runner.on_answer("approuve")
    return yaml.safe_load(draft.read_text(encoding="utf-8"))


def test_dept_block_has_exactly_seven_canonical_fields(tmp_path):
    """After substep B, the `department:` block holds EXACTLY 7 fields."""
    doc = _walk_full_step1(tmp_path)
    dept = doc["department"]
    keys = set(dept.keys())
    extras = keys - _CANONICAL_FIELDS
    missing = _CANONICAL_FIELDS - keys
    assert not extras, (
        f"Extra fields in department block: {sorted(extras)}. "
        "Schema (dept.schema.yaml line 35) rejects additional properties."
    )
    assert not missing, f"Missing canonical fields: {sorted(missing)}"


def test_dept_block_has_no_legacy_outputs_or_success_criteria(tmp_path):
    """The legacy `outputs` and `success_criteria` fields must NOT appear.

    Notion v5 lines 813-825 do NOT mention them; they are a narrative
    concern (live in MANDATE.md), not a schema concern.
    """
    doc = _walk_full_step1(tmp_path)
    dept = doc["department"]
    for forbidden_key in _FORBIDDEN_LEGACY_FIELDS:
        assert forbidden_key not in dept, (
            f"Field `{forbidden_key}` must not appear in dept.yaml — it is "
            "not in the canonical Notion v5 813-825 spec and the schema "
            "rejects it."
        )


def test_dept_block_level_is_one_of_three_official_values(tmp_path):
    """`level` must be ops / management / principal (schema line 51)."""
    doc = _walk_full_step1(tmp_path)
    dept = doc["department"]
    assert "level" in dept
    assert dept["level"] in {"ops", "management", "principal"}


def test_substep_b_prompt_asks_for_level_not_outputs(tmp_path):
    """The new substep B prompt asks `interdits / niveau / owner`,
    not `outputs / interdits / critères / owner`.
    """
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    runner.on_answer("1")
    runner.on_answer("approuve")
    prompt = runner.next_prompt()
    assert prompt is not None
    pl = prompt.lower()
    # Must mention level/niveau, interdits, owner
    assert "niveau" in pl or "level" in pl, "substep B must ask for `level`"
    assert "interdits" in pl
    assert "owner" in pl
    # The legacy `outputs` and `success_criteria` ask must be gone.
    assert "outputs" not in pl, "outputs no longer collected in substep B"
    assert "critères de succès" not in pl


def test_is_done_returns_true_after_seven_fields_written(tmp_path):
    """is_done() must return True once the 7 canonical fields are present."""
    doc = _walk_full_step1(tmp_path)
    state = doc  # full draft
    state_path = _seed_state(Path(state.get("__noop__", "/tmp")))  # noop reseed
    # Just check via a fresh runner pointed at the same dir
    tmp_path_real = Path(state_path).parent  # bring along same tmp_path
    # Walk again in a fresh runner to confirm is_done correctly says yes.
    state = _seed_state(tmp_path)
    draft = _seed_draft(tmp_path)
    runner = get_runner("mandate")
    runner.start(state, draft)
    runner.on_answer("1")
    runner.on_answer("approuve")
    body = (
        "publier sans validation, nommer clients\n"
        "ops\n"
        "joris"
    )
    runner.on_answer(body)
    runner.on_answer("approuve")
    assert runner.is_done() is True


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
