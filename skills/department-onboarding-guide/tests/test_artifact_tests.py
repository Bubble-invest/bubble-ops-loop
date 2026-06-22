"""
test_artifact_tests.py — Refonte #1 of 3, Deliverable D.

Pins the contract of the `skill_lib.artifact_tests` package: a tiny
testing framework that the conversational step runners call to verify
the artifacts they (or the operator) have just produced — and to turn
opaque raw dicts into Bureau-de-Cadre French prose for Telegram.

3 artifact testers covered here:
  - mandate           — verifies the 6 Notion-mandated fields are present
  - dry_run_report    — humanizes the raw run-dry-run.sh JSON into prose
  - activation_pr     — verifies the humanized PR body has all 6 sections

Notion north star:
  - Lines 803-825: the 6 mandate clarifications
  - Lines 936-944: the dry-run report ✓/⚠ format
  - Lines 977-995: the activation PR body sections (humanized in
    activation_pr.py from msg 2702/2708 2026-05-21)
"""
from __future__ import annotations

import pytest

from skill_lib.artifact_tests import (
    TestResult,
    test_artifact,
)
from skill_lib.artifact_tests.activation_pr import test_activation_pr_body
from skill_lib.artifact_tests.dry_run_report import humanize_dry_run_report
from skill_lib.artifact_tests.mandate import test_mandate


# ----- TestResult dataclass shape -----


def test_test_result_dataclass_has_4_fields():
    """The result wraps passed/issues/suggestions/summary_md."""
    r = TestResult(passed=True, issues=[], suggestions=[], summary_md="ok")
    assert r.passed is True
    assert r.issues == []
    assert r.suggestions == []
    assert r.summary_md == "ok"


def test_dispatcher_routes_to_correct_tester():
    """test_artifact(kind, payload, ctx) dispatches on `kind`.

    Sprint correctif Fix 2 (2026-05-21): updated to the 7 canonical
    Notion v5 813-825 fields (level + status added; outputs and
    success_criteria removed).
    """
    payload = {
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "level": "ops",
            "status": "onboarding",
            "mandate": "Produire, planifier et auditer du contenu social pour Bubble.",
            "owner": "operator",
            "forbidden": ["publier sans validation"],
        },
    }
    r = test_artifact("mandate", payload, dept_context=None)
    assert isinstance(r, TestResult)
    assert r.passed is True


def test_dispatcher_unknown_kind_raises():
    with pytest.raises(ValueError):
        test_artifact("not_a_real_kind", {}, dept_context=None)


# ----- Mandate artifact tester -----


def _good_mandate_payload() -> dict:
    """Canonical 7-field mandate payload per Notion v5 813-825."""
    return {
        "department": {
            "slug": "miranda",
            "display_name": "Miranda",
            "level": "ops",
            "status": "onboarding",
            "mandate": "Produire, planifier et auditer du contenu social pour Bubble Invest.",
            "owner": "operator",
            "forbidden": [
                "publier informations confidentielles",
                "donner conseil financier",
            ],
        },
    }


def test_mandate_passes_when_all_7_fields_present():
    r = test_mandate(_good_mandate_payload(), dept_path=None)
    assert r.passed is True
    assert r.issues == []
    assert "Miranda" in r.summary_md or "miranda" in r.summary_md


def test_mandate_fails_when_mandate_sentence_too_short():
    payload = _good_mandate_payload()
    payload["department"]["mandate"] = "Court."
    r = test_mandate(payload, dept_path=None)
    assert r.passed is False
    assert any("20" in i or "court" in i.lower() for i in r.issues)


def test_mandate_fails_when_forbidden_empty():
    payload = _good_mandate_payload()
    payload["department"]["forbidden"] = []
    r = test_mandate(payload, dept_path=None)
    assert r.passed is False
    assert any("interdit" in i.lower() or "forbidden" in i.lower() for i in r.issues)


def test_mandate_fails_when_a_field_is_missing():
    """Drop `level` (now required) and confirm rejection."""
    payload = _good_mandate_payload()
    del payload["department"]["level"]
    r = test_mandate(payload, dept_path=None)
    assert r.passed is False
    assert any("level" in i or "niveau" in i.lower() for i in r.issues)


# ----- Dry-run report humanizer -----


def _raw_dry_run_all_green() -> dict:
    return {
        "overall_status": "PASSED",
        "can_advance_to_ready": True,
        "checks": [
            {"step": "layer_1_output_schema", "scope": "/x/1", "status": "passed",
             "message": "4-file output skeleton written"},
            {"step": "layer_2_gate_item_schema", "scope": "/x/2", "status": "passed",
             "message": "gate-item valid"},
            {"step": "layer_3_execution_valid", "scope": "/x/3", "status": "passed",
             "message": "Layer 3 fake execution wrote exec-log.jsonl"},
            {"step": "layer_4_three_outputs", "scope": "/x/4", "status": "passed",
             "message": "risk-brief.md + risk-kpis.yaml + management-export.yaml present"},
        ],
    }


def _raw_dry_run_warning_missing_brand_safety() -> dict:
    raw = _raw_dry_run_all_green()
    raw["overall_status"] = "WARNING"
    raw["can_advance_to_ready"] = False
    raw["checks"].append({
        "step": "layer_4_brand_safety_fixture",
        "scope": "/x/tests/brand_safety.yaml",
        "status": "warning",
        "message": "Missing brand safety test fixture",
    })
    return raw


def _raw_dry_run_failed() -> dict:
    raw = _raw_dry_run_all_green()
    raw["overall_status"] = "FAILED"
    raw["can_advance_to_ready"] = False
    raw["checks"][2]["status"] = "failed"
    raw["checks"][2]["message"] = "Layer 3 simulated execution crashed"
    return raw


def test_humanize_all_green_report_passes_and_uses_french():
    r = humanize_dry_run_report(_raw_dry_run_all_green())
    assert r.passed is True
    # Bureau-de-Cadre prose, French.
    assert "répétition" in r.summary_md.lower() or "répétition" in r.summary_md
    # Layer names humanized.
    assert "Le matin" in r.summary_md or "matin" in r.summary_md.lower()
    # The technical step ids should not be the dominant phrasing.
    assert "layer_1_output_schema" not in r.summary_md or "✓" in r.summary_md


def test_humanize_warning_report_lists_the_warning_clearly():
    r = humanize_dry_run_report(_raw_dry_run_warning_missing_brand_safety())
    assert r.passed is False
    # The brand safety warning is surfaced.
    assert "brand safety" in r.summary_md.lower() or "brand_safety" in r.summary_md.lower()
    # ⚠ symbol per Notion v5 line 944.
    assert "⚠" in r.summary_md or "warning" in r.summary_md.lower()
    # Suggestions guide the operator.
    assert r.suggestions, "humanizer should suggest next steps on WARNING"


def test_humanize_failed_report_blocks():
    r = humanize_dry_run_report(_raw_dry_run_failed())
    assert r.passed is False
    assert any("layer 3" in i.lower() or "layer_3" in i.lower() for i in r.issues)
    # No "tu valides" — failures need a fix, not a validation.
    # (We don't constrain phrasing further; just that summary mentions it.)


# ----- Activation PR body tester -----


_GOOD_PR_BODY = """# Lettre d'arrivée de Miranda

Une fois cette lettre acceptée par l'équipe, Miranda rejoint officiellement l'équipe.

## Sa mission

Produire, planifier et auditer du contenu social.

## Ce qu'elle fera chaque jour

- `content_scan` — chaque jour

## Ses 4 moments de la journée

- **Le matin** — elle s'appuie sur : `content-signal-scanner`
- _La recherche — elle n'intervient pas à ce moment-là._
- _L'exécution — elle n'intervient pas à ce moment-là._
- _Le débrief du soir — elle n'intervient pas à ce moment-là._

## Les décisions qu'elle prend

_(aucune décision particulière à encadrer pour l'instant.)_

## Sa répétition à blanc

- Elle a fait sa répétition à blanc et elle est passée.

## Ce qu'il faut vérifier avant la cérémonie

Avant d'envoyer la lettre :

- [ ] Item 1
- [ ] Item 2
- [ ] Item 3
- [ ] Item 4
- [ ] Item 5
"""


def test_activation_pr_body_passes_on_well_formed_body():
    r = test_activation_pr_body(_GOOD_PR_BODY)
    assert r.passed is True, r.issues


def test_activation_pr_body_fails_when_h1_missing():
    body = _GOOD_PR_BODY.replace("# Lettre d'arrivée de Miranda", "")
    r = test_activation_pr_body(body)
    assert r.passed is False
    assert any("h1" in i.lower() or "lettre" in i.lower() for i in r.issues)


def test_activation_pr_body_fails_when_section_missing():
    body = _GOOD_PR_BODY.replace("## Sa mission", "## (oops)")
    r = test_activation_pr_body(body)
    assert r.passed is False


def test_activation_pr_body_fails_when_checklist_short():
    body = _GOOD_PR_BODY.replace("- [ ] Item 5\n", "")
    r = test_activation_pr_body(body)
    assert r.passed is False
    assert any("5" in i or "checklist" in i.lower() for i in r.issues)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
