"""
artifact_tests/activation_pr.py — Refonte #1 of 3, Deliverable D.

Verifies that the activation PR body built by
`skill_lib.activation_pr.build_activation_pr_body()` is the humanized
French Bureau-de-Cadre version (not the legacy English one). This
tester is what Step 7 calls before showing the PR body to {{OPERATOR}} on
Telegram — if FAILED the runner blocks.

Notion v5 lines 977-995 specify the PR body sections (the heading
copy is ours; what matters is that the 6 humanized sections + h1 +
5-item checklist are all present).
"""
from __future__ import annotations

import re
from typing import Any

from .base import TestResult, register_tester


_REQUIRED_H1_PATTERNS = [
    re.compile(r"^#\s+Lettre d'arrivée de\s+\S+", re.MULTILINE),
    re.compile(r"^#\s+Cérémonie d'arrivée de\s+\S+", re.MULTILINE),
    re.compile(r"^#\s+Bienvenue à\s+\S+", re.MULTILINE),
]

_REQUIRED_SECTIONS = [
    "## Sa mission",
    "## Ce qu'elle fera chaque jour",
    "## Ses 4 moments de la journée",
    "## Les décisions qu'elle prend",
    "## Sa répétition à blanc",
    "## Ce qu'il faut vérifier avant la cérémonie",
]

_CHECKLIST_RE = re.compile(r"^\s*-\s*\[\s\]\s+", re.MULTILINE)

_FORBIDDEN_ENGLISH = [
    "## Mandate",
    "## Recurring missions",
    "## Layer outputs",
    "## Gate policy summary",
    "## Dry-run result",
    "## Activation checklist",
    "Activate ",  # part of legacy "Activate X department"
]


def test_activation_pr_body(pr_body_md: Any, dept_context: Any = None) -> TestResult:
    """Verify the activation PR body has all 6 humanized sections + h1.

    Returns a TestResult whose `summary_md` is operator-facing FR
    prose; on failure it lists exactly which sections are missing so
    the runner can refuse to ship cleanly.
    """
    if not isinstance(pr_body_md, str) or not pr_body_md.strip():
        return TestResult(
            passed=False,
            issues=["Le corps de la PR d'activation est vide."],
            summary_md="**Lettre d'arrivée vide.**",
        )

    issues = []
    suggestions = []

    # 1. h1 must be one of the 3 humanized variants.
    if not any(p.search(pr_body_md) for p in _REQUIRED_H1_PATTERNS):
        issues.append(
            "Le titre h1 ne suit aucun des 3 formats humanisés attendus "
            "(« Lettre d'arrivée de … », « Cérémonie d'arrivée de … », "
            "« Bienvenue à … »)."
        )

    # 2. 6 humanized sections.
    for section in _REQUIRED_SECTIONS:
        if section not in pr_body_md:
            issues.append(f"Section humanisée manquante : `{section}`.")

    # 3. Checklist ≥ 5 items.
    checklist_count = len(_CHECKLIST_RE.findall(pr_body_md))
    if checklist_count < 5:
        issues.append(
            f"La checklist d'avant-cérémonie ne compte que {checklist_count} "
            f"items (minimum attendu : 5)."
        )

    # 4. No legacy English headings should slip back in.
    for bad in _FORBIDDEN_ENGLISH:
        if bad in pr_body_md:
            issues.append(
                f"Vocabulaire anglais à supprimer : `{bad}` (cf. msg 2702/2708)."
            )

    passed = not issues

    if passed:
        summary = (
            "**Lettre d'arrivée prête.** Toutes les sections humanisées "
            f"sont présentes, et la checklist compte {checklist_count} items."
        )
    else:
        summary = (
            "**Lettre d'arrivée à corriger avant envoi :**\n\n"
            + "\n".join(f"- {i}" for i in issues)
        )

    return TestResult(
        passed=passed,
        issues=issues,
        suggestions=suggestions,
        summary_md=summary,
    )


test_activation_pr_body.__test__ = False  # type: ignore[attr-defined]
register_tester("activation_pr", test_activation_pr_body)
