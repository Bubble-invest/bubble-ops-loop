"""
artifact_tests/mandate.py — Refonte #1 of 3, Deliverable D.

Tester for the Step 1 mandate artifact. Verifies that the 6 Notion-
mandated fields (v5 lines 805-811) are present, that the mandate
sentence is substantive (>= 20 chars), and that `forbidden[]` is
non-empty (the dept owes itself at least one prohibited behavior).

Returns FR prose summary the mandate runner relays to Telegram.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .base import TestResult, register_tester


# Sprint correctif Fix 2 (2026-05-21) — aligned with Notion v5 lines
# 813-825 and dept.schema.yaml lines 32-97. The 7 canonical fields:
_REQUIRED_FIELDS = (
    "slug", "display_name", "level", "status",
    "mandate", "owner", "forbidden",
)


def test_mandate(payload: dict, dept_path: Optional[Path] = None) -> TestResult:
    """Verify the mandate artifact is substantively complete.

    `payload` is the dept.yaml.draft as a dict (with the `department:`
    wrapper). `dept_path` is informational only.
    """
    issues: list = []
    suggestions: list = []

    if not isinstance(payload, dict):
        return TestResult(
            passed=False,
            issues=["Le mandat n'est pas un dictionnaire — impossible à valider."],
            summary_md="**Mandat refusé** — format inattendu.",
        )

    dept = payload.get("department")
    if not isinstance(dept, dict):
        return TestResult(
            passed=False,
            issues=["Le bloc `department:` est manquant ou mal formé."],
            summary_md="**Mandat refusé** — bloc `department:` absent.",
        )

    # 1. All 6 required fields present + non-empty.
    for f in _REQUIRED_FIELDS:
        v = dept.get(f)
        if v is None or v == "" or v == [] or v == {}:
            issues.append(
                f"Champ `{f}` manquant ou vide dans le bloc `department:` "
                f"(cf. Notion v5 lignes 805-811)."
            )

    # 2. Mandate sentence is substantive.
    mandate_sentence = dept.get("mandate") or ""
    if isinstance(mandate_sentence, str) and 0 < len(mandate_sentence) < 20:
        issues.append(
            f"La phrase de mandat est trop courte ({len(mandate_sentence)} "
            f"caractères, minimum 20) — précise-la."
        )

    # 3. Forbidden list non-empty.
    forbidden = dept.get("forbidden")
    if isinstance(forbidden, list) and len(forbidden) == 0:
        issues.append(
            "La liste `forbidden` est vide. Au moins un interdit explicite "
            "est attendu (Notion v5 lignes 821-824)."
        )

    # 4. Soft suggestions.
    if isinstance(forbidden, list) and 0 < len(forbidden) < 2:
        suggestions.append(
            "Tu n'as déclaré qu'un seul interdit — la plupart des "
            "départements en listent 2 ou 3."
        )
    # Sprint correctif Fix 2 (2026-05-21): success_criteria are no longer
    # in dept.yaml — they live in MANDATE.md narrative instead.

    passed = not issues
    display = dept.get("display_name") or dept.get("slug", "?")
    if passed:
        summary = (
            f"**Mandat de {display} validé.**\n\n"
            f"- Slug : `{dept.get('slug', '?')}`\n"
            f"- Niveau : `{dept.get('level', '?')}`\n"
            f"- Owner : `{dept.get('owner', '?')}`\n"
            f"- Phrase : *« {mandate_sentence} »*\n"
            f"- {len(forbidden) if isinstance(forbidden, list) else 0} "
            f"interdit(s) déclaré(s)."
        )
        if suggestions:
            summary += "\n\n_Pistes de polish (non bloquantes) :_\n" + \
                "\n".join(f"- {s}" for s in suggestions)
    else:
        summary = (
            f"**Mandat de {display} — à compléter.**\n\n"
            "Voici ce qui manque :\n\n"
            + "\n".join(f"- {i}" for i in issues)
        )

    return TestResult(
        passed=passed,
        issues=issues,
        suggestions=suggestions,
        summary_md=summary,
    )


test_mandate.__test__ = False  # type: ignore[attr-defined]
register_tester("mandate", test_mandate)
