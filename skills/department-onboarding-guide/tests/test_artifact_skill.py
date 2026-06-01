"""
test_artifact_skill.py — Refonte #3 of 3, Deliverable C.

Per-skill semantic + completeness tester. Notion v5 lines 886-893 mandate
the 5-field card shape:

    content-signal-scanner
    Purpose: détecter des idées de contenu
    Inputs: wiki, LinkedIn, notes
    Outputs: content_idea_task
    Tests: missing
    Status: draft

The tester:
  1. Checks all 5 fields present + non-empty.
  2. Verifies `purpose` is in French Bureau-de-Cadre voice (no English
     jargon, >= 1 verb, >= 10 chars).
  3. Soft-checks `inputs[]` and `outputs[]` reference known artifact
     kinds.
  4. Verifies `status` is one of draft / tested / live.
  5. Simulates an isolation invocation: builds a minimal fake input
     matching `inputs[0]` and verifies the skill's declared `outputs`
     looks like it would be produced.
"""
from __future__ import annotations

import pytest

from skill_lib.artifact_tests import test_artifact
from skill_lib.artifact_tests.base import TestResult


# ----- canonical fixture per Notion 886-893 -----

CANONICAL_SKILL = {
    "name": "content-signal-scanner",
    "purpose": "Détecter des idées de contenu à partir du wiki et de LinkedIn.",
    "inputs": ["wiki", "linkedin", "notes"],
    "outputs": ["content_idea_task"],
    "tests": "Fixture wiki snapshot + 3 entrées LinkedIn ; vérifier qu'au moins 1 content_idea_task est produit.",
    "status": "draft",
}


def test_skill_canonical_notion_example_passes():
    result = test_artifact("skill", CANONICAL_SKILL, None)
    assert isinstance(result, TestResult)
    assert result.passed, result.issues


def test_skill_missing_purpose_fails():
    bad = dict(CANONICAL_SKILL)
    del bad["purpose"]
    result = test_artifact("skill", bad, None)
    assert not result.passed
    assert any("purpose" in i.lower() for i in result.issues)


def test_skill_missing_inputs_fails():
    bad = dict(CANONICAL_SKILL)
    bad["inputs"] = []
    result = test_artifact("skill", bad, None)
    assert not result.passed
    assert any("input" in i.lower() for i in result.issues)


def test_skill_missing_outputs_fails():
    bad = dict(CANONICAL_SKILL)
    bad["outputs"] = []
    result = test_artifact("skill", bad, None)
    assert not result.passed
    assert any("output" in i.lower() for i in result.issues)


def test_skill_purpose_too_short_fails():
    bad = dict(CANONICAL_SKILL)
    bad["purpose"] = "short"
    result = test_artifact("skill", bad, None)
    assert not result.passed


def test_skill_tests_field_too_short_fails():
    bad = dict(CANONICAL_SKILL)
    bad["tests"] = "x"
    result = test_artifact("skill", bad, None)
    assert not result.passed
    assert any("test" in i.lower() for i in result.issues)


def test_skill_invalid_status_fails():
    bad = dict(CANONICAL_SKILL)
    bad["status"] = "deployed"  # not in {draft, tested, live}
    result = test_artifact("skill", bad, None)
    assert not result.passed
    assert any("status" in i.lower() for i in result.issues)


def test_skill_status_live_passes():
    good = dict(CANONICAL_SKILL)
    good["status"] = "live"
    result = test_artifact("skill", good, None)
    assert result.passed


def test_skill_summary_md_is_french_bureau_de_cadre():
    result = test_artifact("skill", CANONICAL_SKILL, None)
    assert result.passed
    # FR voice: should mention the skill name + use French connectors.
    assert "content-signal-scanner" in result.summary_md
    # Bureau-de-Cadre voice: should reference the purpose
    assert "validé" in result.summary_md.lower() or "valide" in result.summary_md.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
