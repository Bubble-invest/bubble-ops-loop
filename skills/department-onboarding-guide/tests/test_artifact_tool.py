"""
test_artifact_tool.py — Refonte #3 of 3, Deliverable D.

Per-tool semantic + completeness tester. Notion v5 lines 879-893 mandate
that tools follow the same 5-field card shape as skills, plus a
kebab-case naming convention (lines 880-883):

    tools:
      - linkedin-reader
      - shared-wiki-reader
      - post-scheduler
      - analytics-reader

Checks:
  1. 5 mandatory fields present.
  2. Tool `name` is kebab-case.
  3. `inputs[]` may be empty (tools often pull from external sources).
  4. `outputs[]` must be non-empty (tools without an output are useless).
  5. Discovery: best-effort check whether the tool exists at a known
     location (tools/<name>/, ~/.claude/skills/<name>/, or known
     SaaS list). If not found, return a WARN (suggestion, not FAIL).
"""
from __future__ import annotations

import pytest

from skill_lib.artifact_tests import test_artifact
from skill_lib.artifact_tests.base import TestResult


CANONICAL_TOOL = {
    "name": "linkedin-reader",
    "purpose": "Lire la timeline LinkedIn et extraire les posts pertinents.",
    "inputs": [],  # tools often have no internal inputs
    "outputs": ["linkedin_post_signal"],
    "tests": "Fixture HTML + vérifier que >= 1 signal LinkedIn est extrait.",
    "status": "draft",
}


def test_tool_canonical_notion_example_passes():
    result = test_artifact("tool", CANONICAL_TOOL, None)
    assert isinstance(result, TestResult)
    assert result.passed, result.issues


def test_tool_non_kebab_name_fails():
    bad = dict(CANONICAL_TOOL)
    bad["name"] = "LinkedInReader"  # camelCase
    result = test_artifact("tool", bad, None)
    assert not result.passed
    assert any("kebab" in i.lower() or "nom" in i.lower() for i in result.issues)


def test_tool_snake_case_name_fails():
    bad = dict(CANONICAL_TOOL)
    bad["name"] = "linkedin_reader"  # snake_case
    result = test_artifact("tool", bad, None)
    assert not result.passed


def test_tool_missing_outputs_fails():
    bad = dict(CANONICAL_TOOL)
    bad["outputs"] = []
    result = test_artifact("tool", bad, None)
    assert not result.passed


def test_tool_empty_inputs_is_allowed():
    """Tools can have no internal inputs (they pull from external SaaS)."""
    result = test_artifact("tool", CANONICAL_TOOL, None)
    assert result.passed


def test_tool_unknown_location_warns_does_not_fail(tmp_path):
    """If the tool isn't found at any known location, return WARN."""
    ctx = {"dept_root": tmp_path}
    payload = dict(CANONICAL_TOOL)
    payload["name"] = "completely-unknown-tool"
    result = test_artifact("tool", payload, ctx)
    # Either passed=True with a suggestion mentioning installation,
    # OR an explicit suggestion mentions install/locate.
    assert result.passed
    assert any("install" in s.lower() or "introuvable" in s.lower()
               or "locate" in s.lower() for s in result.suggestions)


def test_tool_invalid_status_fails():
    bad = dict(CANONICAL_TOOL)
    bad["status"] = "rolled-out"
    result = test_artifact("tool", bad, None)
    assert not result.passed


def test_tool_summary_md_is_french():
    result = test_artifact("tool", CANONICAL_TOOL, None)
    assert result.passed
    assert "linkedin-reader" in result.summary_md
    assert "valide" in result.summary_md.lower() or "validé" in result.summary_md.lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
