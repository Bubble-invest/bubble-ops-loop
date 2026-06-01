"""
artifact_tests/gate_policy.py — Refonte #3 of 3, Deliverable E.

Per-gate-policy tester. Notion v5 lines 894-924 mandate the policy
shape with 4 mandatory blocks:

  gate_policies:
    social_post:
      current_mode: manual_required
      eligible_future_modes:
        - auto_with_veto_window
        - auto_if_policy_passed
      authorization_bands:
        low_risk_evergreen:
          allowed_post_types: [...]
          forbidden: [...]
      kpi_guardrails:
        brand_safety_breaches: 0
        ...

Doctrine guards (Notion lines 250-263 + 421-436):
  - `current_mode` MUST be `manual_required` in v1 (line 895).
  - `eligible_future_modes` MUST be a subset of the 5 OFFICIAL modes
    (lines 256-260). The deprecated shorthand vocabulary that was
    eliminated in PR #4 is doctrinally invalid — the observation phase
    is NOT a mode of autonomy, it's a phase that runs ACROSS modes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..gates import ALL_AUTONOMY_MODES
from .base import TestResult, register_tester


# v1 always demands the most conservative mode.
_REQUIRED_CURRENT_MODE = "manual_required"

# The 5 official modes per Notion v5 lines 256-260. We import the
# canonical list from skill_lib.gates to keep the source of truth in
# one place. The OBSERVATION phase doctrine (Notion 251 + 421-436) is
# NOT a 6th mode — it runs across modes.
_OFFICIAL_MODES_SET = set(ALL_AUTONOMY_MODES)


def test_gate_policy(payload: Dict[str, Any], ctx: Optional[Any] = None) -> TestResult:
    """Validate one gate policy dict and return a TestResult."""
    issues: List[str] = []
    suggestions: List[str] = []

    if not isinstance(payload, dict):
        return TestResult(
            passed=False,
            issues=[
                "La policy n'est pas un dictionnaire — impossible à valider."
            ],
            summary_md="**Gate policy refusée** — format inattendu.",
        )

    # 1. current_mode — MUST be manual_required in v1.
    current_mode = payload.get("current_mode")
    if current_mode is None:
        issues.append(
            "Champ `current_mode` manquant. En v1 il doit être "
            f"`{_REQUIRED_CURRENT_MODE}` (cf. Notion v5 ligne 895)."
        )
    elif current_mode != _REQUIRED_CURRENT_MODE:
        issues.append(
            f"`current_mode` `{current_mode}` invalide. En v1, tous les "
            f"gates sont en `{_REQUIRED_CURRENT_MODE}` (cf. Notion v5 "
            "ligne 895 : « L'agent propose les gates et l'autonomie "
            "future, sans l'activer en v1 »). Les autres modes peuvent "
            "apparaître dans `eligible_future_modes`, pas en cours."
        )

    # 2. eligible_future_modes — subset of the 5 official modes.
    efm = payload.get("eligible_future_modes")
    if efm is None:
        issues.append(
            "Champ `eligible_future_modes` manquant. Liste au moins un "
            "mode visé (parmi les 5 officiels : "
            + ", ".join(f"`{m}`" for m in ALL_AUTONOMY_MODES) + ")."
        )
    elif not isinstance(efm, list):
        issues.append(
            "`eligible_future_modes` doit être une liste."
        )
    else:
        bad_modes = [
            m for m in efm
            if not isinstance(m, str) or m not in _OFFICIAL_MODES_SET
        ]
        if bad_modes:
            bad_str = ", ".join(f"`{m}`" for m in bad_modes)
            issues.append(
                f"Modes inconnus dans `eligible_future_modes` : {bad_str}. "
                "Doctrine Notion v5 lignes 256-260 — uniquement les 5 modes "
                "officiels sont acceptés : "
                + ", ".join(f"`{m}`" for m in ALL_AUTONOMY_MODES) + ". "
                "La phase d'observation (Notion lignes 421-436) n'est PAS "
                "un mode — c'est une phase qui traverse les modes."
            )

    # 3. authorization_bands — at least 1 band declared.
    bands = payload.get("authorization_bands")
    if bands is None:
        issues.append(
            "Champ `authorization_bands` manquant. Déclare au moins une "
            "bande (ex. `low_risk_evergreen`) — cf. Notion v5 ligne 903."
        )
    elif not isinstance(bands, dict) or not bands:
        issues.append(
            "`authorization_bands` doit être un dict avec au moins une "
            "bande. La bande déclare QUELLES instances de l'action class "
            "deviennent éligibles à l'autonomie."
        )
    else:
        # Each band must carry `allowed_*` AND `forbidden[]`.
        for band_id, band in bands.items():
            if not isinstance(band, dict):
                issues.append(
                    f"Bande `{band_id}` mal formée — attendu un dict."
                )
                continue
            allowed_keys = [k for k in band.keys() if k.startswith("allowed_")]
            if not allowed_keys:
                issues.append(
                    f"Bande `{band_id}` : aucun champ `allowed_*` "
                    "(ex. `allowed_post_types`, `allowed_recipients`)."
                )
            forbidden = band.get("forbidden")
            if not (isinstance(forbidden, list) and len(forbidden) >= 1):
                issues.append(
                    f"Bande `{band_id}` : `forbidden[]` vide ou manquant. "
                    "Tout dept doit lister au moins un interdit explicite."
                )

    # 4. kpi_guardrails — non-empty map.
    kpis = payload.get("kpi_guardrails")
    if kpis is None:
        issues.append(
            "Champ `kpi_guardrails` manquant. Déclare au moins un KPI de "
            "maintien (cf. Notion v5 lignes 913-917)."
        )
    elif not isinstance(kpis, dict) or not kpis:
        issues.append(
            "`kpi_guardrails` doit être un dict non vide. Au moins un KPI "
            "est nécessaire pour qu'une dégradation puisse re-fermer la "
            "gate (cf. Notion v5 lignes 262-265)."
        )

    passed = not issues
    if passed:
        bands_str = ", ".join(f"`{b}`" for b in (bands or {}).keys())
        kpis_str = ", ".join(f"`{k}`" for k in (kpis or {}).keys())
        modes_str = ", ".join(f"`{m}`" for m in (efm or []))
        summary = (
            "**Gate policy validée.**\n\n"
            f"- Niveau actuel : `{current_mode}`\n"
            f"- Modes futurs possibles : {modes_str}\n"
            f"- Bandes : {bands_str}\n"
            f"- KPI garde-fous : {kpis_str}"
        )
        if suggestions:
            summary += "\n\n_Pistes de polish (non bloquantes) :_\n" + \
                "\n".join(f"- {s}" for s in suggestions)
    else:
        summary = (
            "**Gate policy — à corriger.**\n\n"
            "Voici ce qui bloque :\n\n"
            + "\n".join(f"- {i}" for i in issues)
        )

    return TestResult(
        passed=passed,
        issues=issues,
        suggestions=suggestions,
        summary_md=summary,
    )


test_gate_policy.__test__ = False  # type: ignore[attr-defined]
register_tester("gate_policy", test_gate_policy)
