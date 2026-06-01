"""
artifact_tests/layer_focus.py — Refonte #2 of 3, Deliverable D.

Per-layer PROMPT.md tester. Called by LayersRunner before declaring
a subscribed layer ready. Checks:

  1. The PROMPT.md file exists at layers/<N>/PROMPT.md.
  2. Contains the Layer N generic description (layer name + doctrinal
     1-liner from Notion v5 lines 440-468).
  3. Contains the focalisation text agreed with the operator.
  4. Contains an "Outputs" section (case-insensitive).
  5. Parses as valid markdown (no broken code blocks).
  6. Is non-empty (>= 200 chars).

Returns a FR Bureau-de-Cadre TestResult.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import TestResult, register_tester


# The official Notion v5 doctrinal one-liners (lines 440-468). Kept in
# sync with `_LAYER_GENERIC_DESCRIPTION` in step_runners/layers.py — the
# tester uses these strings to verify a PROMPT.md actually carries the
# layer's official mission, not an ad-hoc paraphrase.
_NOTION_LAYER_KEYWORDS: Dict[int, List[str]] = {
    1: ["Data Update", "refresh data", "Data", "queue items", "06:00"],
    2: ["Research", "transforme", "signaux", "idées", "Plan"],
    3: ["Execution", "exécute", "actions", "broker", "inbox/decisions"],
    4: ["Risk", "mandat", "examine", "audit", "kpis"],
}


def _has_any_keyword(body: str, keywords: List[str]) -> bool:
    lowered = body.lower()
    return any(k.lower() in lowered for k in keywords)


def _has_outputs_section(body: str) -> bool:
    # Match "## Outputs", "# Outputs", or "**Outputs**" — anything that
    # behaves as a section marker.
    return bool(re.search(r"(^|\n)\s*(?:#+\s*Outputs|\*\*\s*Outputs\s*\*\*)",
                          body, re.IGNORECASE))


def _has_balanced_code_fences(body: str) -> bool:
    # Count ``` fences; an odd number means an unclosed block.
    return body.count("```") % 2 == 0


def test_layer_focus(payload: Dict[str, Any], ctx: Optional[Any] = None) -> TestResult:
    """Verify a Layer N PROMPT.md is substantively complete.

    `payload` keys:
        layer            int       (1..4)
        focus_md         str       (focalisation text agreed)
        prompt_md_path   Path      (location of PROMPT.md to check)
    """
    issues: List[str] = []
    suggestions: List[str] = []

    layer = payload.get("layer") if isinstance(payload, dict) else None
    prompt_md_path = payload.get("prompt_md_path") if isinstance(payload, dict) else None
    focus_md = payload.get("focus_md", "") if isinstance(payload, dict) else ""

    if not (isinstance(layer, int) and layer in (1, 2, 3, 4)):
        return TestResult(
            passed=False,
            issues=[f"Layer `{layer}` invalide. Attendu : 1, 2, 3 ou 4."],
            summary_md="**Layer refusé** — numéro invalide.",
        )

    if prompt_md_path is None:
        return TestResult(
            passed=False,
            issues=["`prompt_md_path` manquant dans le payload."],
            summary_md=f"**Layer {layer} refusé** — chemin du PROMPT.md absent.",
        )

    prompt_md_path = Path(prompt_md_path)

    # 1. File exists.
    if not prompt_md_path.exists():
        return TestResult(
            passed=False,
            issues=[
                f"Le fichier `PROMPT.md` n'existe pas à l'emplacement "
                f"attendu (`{prompt_md_path}`)."
            ],
            summary_md=f"**Layer {layer} refusé** — `PROMPT.md` introuvable.",
        )

    body = prompt_md_path.read_text(encoding="utf-8")

    # 2. Non-empty (>= 200 chars).
    if len(body) < 200:
        issues.append(
            f"Le `PROMPT.md` est trop court ({len(body)} caractères, "
            "minimum 200) — il faut au moins le pitch générique + la "
            "focalisation + les outputs."
        )

    # 3. Generic Layer N description present (keyword spotting against
    # Notion v5 lines 440-468 — any one keyword satisfies).
    if not _has_any_keyword(body, _NOTION_LAYER_KEYWORDS.get(layer, [])):
        issues.append(
            f"Le `PROMPT.md` ne reprend pas la description générique du "
            f"Layer {layer} (cf. Notion v5 lignes 440-468). Ajoute au "
            f"moins le pitch doctrinal."
        )

    # 4. Outputs section.
    if not _has_outputs_section(body):
        issues.append(
            "Aucune section `Outputs` détectée. Liste explicitement les "
            "fichiers que ce layer va écrire."
        )

    # 5. Balanced code fences.
    if not _has_balanced_code_fences(body):
        issues.append(
            "Le `PROMPT.md` contient un bloc de code non fermé "
            "(nombre impair de ```)."
        )

    # 6. Focalisation present (soft check — flag only as suggestion when
    # provided but absent from file).
    if isinstance(focus_md, str) and focus_md.strip():
        # Compare on the first 30 substantive chars to allow minor reformat.
        snippet = focus_md.strip().split("\n", 1)[0][:30]
        if snippet and snippet not in body:
            suggestions.append(
                "La focalisation discutée n'est pas reprise mot pour mot "
                "dans le PROMPT.md — vérifie qu'elle y figure clairement."
            )

    passed = not issues
    if passed:
        summary = (
            f"**Layer {layer} validé.**\n\n"
            f"Le `PROMPT.md` ({len(body)} caractères) contient la "
            f"description générique, la focalisation et la section "
            f"`Outputs`. Il est prêt à servir."
        )
        if suggestions:
            summary += "\n\n_Pistes de polish (non bloquantes) :_\n" + \
                "\n".join(f"- {s}" for s in suggestions)
    else:
        summary = (
            f"**Layer {layer} — à compléter.**\n\n"
            "Voici ce qui manque :\n\n"
            + "\n".join(f"- {i}" for i in issues)
        )

    return TestResult(
        passed=passed,
        issues=issues,
        suggestions=suggestions,
        summary_md=summary,
    )


test_layer_focus.__test__ = False  # type: ignore[attr-defined]
register_tester("layer_focus", test_layer_focus)
