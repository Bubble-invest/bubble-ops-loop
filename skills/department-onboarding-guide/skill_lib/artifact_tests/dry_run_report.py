"""
artifact_tests/dry_run_report.py — Refonte #1 of 3, Deliverable D.

Turns the raw `run-dry-run.sh` / `run_dry_run_full()` result dict (or
DryRunResult.to_dict()) into Bureau-de-Cadre French prose for Telegram.

Notion north star (v5 lines 936-944):
    Résultat UX :
    ```plain text
    Dry run result:
    ✓ Layer 1 output valid
    ✓ Queue schema valid
    ✓ Layer 2 draft produced
    ✓ Gate produced
    ✓ Layer 3 dry-run execution valid
    ⚠ Missing brand safety test fixture
    ```

Our humanizer takes that signal and re-narrates it in FR with the
4-moments-of-the-day vocabulary (Le matin / La recherche / L'exécution
/ Le débrief du soir) so the operator never sees raw `layer_N` slugs.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import TestResult, register_tester


_LAYER_MOMENT: Dict[int, str] = {
    1: "Le matin",
    2: "La recherche",
    3: "L'exécution",
    4: "Le débrief du soir",
}


def _layer_from_step(step: str) -> int:
    """Map a check `step` id like 'layer_3_execution_valid' to the layer number.

    Returns 0 when the step doesn't carry a layer prefix.
    """
    if not isinstance(step, str):
        return 0
    parts = step.split("_")
    if len(parts) >= 2 and parts[0] == "layer":
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return 0


_STATUS_GLYPH = {
    "passed": "✓",
    "warning": "⚠",
    "failed": "✗",
}


def _french_status_phrase(layer: int, status: str) -> str:
    moment = _LAYER_MOMENT.get(layer, f"Étape {layer}")
    glyph = _STATUS_GLYPH.get(status, "·")
    if status == "passed":
        return f"{glyph} **{moment}** — tout est OK"
    if status == "warning":
        return f"{glyph} **{moment}** — un point à regarder"
    if status == "failed":
        return f"{glyph} **{moment}** — quelque chose a planté"
    return f"{glyph} **{moment}** — statut inconnu"


def humanize_dry_run_report(raw: Dict[str, Any]) -> TestResult:
    """Convert raw dry-run report into a Bureau-de-Cadre French TestResult.

    Args:
      raw: dict-like, with at minimum `overall_status` and `checks` keys
           (matches DryRunResult.to_dict() and the JSON emitted by
           scripts/run-dry-run.sh).
    """
    if not isinstance(raw, dict):
        return TestResult(
            passed=False,
            issues=["Le rapport de répétition à blanc n'est pas exploitable."],
            summary_md="**Rapport illisible.**",
        )

    overall = str(raw.get("overall_status", "")).upper()
    checks = raw.get("checks") or []
    can_advance = bool(raw.get("can_advance_to_ready", False))

    # Aggregate per-layer worst status.
    per_layer: Dict[int, str] = {}
    other_checks: List[Dict[str, Any]] = []
    severity = {"passed": 0, "warning": 1, "failed": 2}
    for c in checks:
        if not isinstance(c, dict):
            continue
        step = c.get("step", "")
        layer = _layer_from_step(step)
        status = str(c.get("status", "passed")).lower()
        if layer in (1, 2, 3, 4):
            cur = per_layer.get(layer, "passed")
            if severity.get(status, 0) > severity.get(cur, 0):
                per_layer[layer] = status
        else:
            other_checks.append(c)

    # Per-layer lines.
    layer_lines = [
        _french_status_phrase(n, per_layer.get(n, "passed"))
        for n in (1, 2, 3, 4)
    ]

    # Collect issues + suggestions from any warning/failed checks.
    issues: List[str] = []
    suggestions: List[str] = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        status = str(c.get("status", "")).lower()
        message = c.get("message", "")
        step = c.get("step", "")
        layer = _layer_from_step(step)
        moment = _LAYER_MOMENT.get(layer, "Étape inconnue")
        if status == "warning":
            issues.append(f"⚠ {moment} : {message}")
            suggestions.append(
                f"Tu peux **valider** le warning et avancer, ou demander à "
                f"y revenir avant l'activation."
            )
        elif status == "failed":
            issues.append(f"✗ {moment} : {message}")

    # Compose the summary prose.
    if overall == "PASSED":
        opener = "**Ma répétition à blanc est passée.** Voici le détail :"
        ask = (
            "Tu **valides** que je passe en statut « Ready to activate », "
            "ou tu préfères que je rejoue une répétition différente ?"
        )
    elif overall == "WARNING":
        opener = (
            "**Ma répétition à blanc s'est terminée avec un avertissement.** "
            "Voici le détail :"
        )
        ask = (
            "Tu **valides** ce warning (j'avance vers l'activation) ou tu "
            "préfères que je corrige avant de continuer ?"
        )
    elif overall == "FAILED":
        opener = (
            "**Ma répétition à blanc a échoué.** Je ne peux pas avancer "
            "vers l'activation tant que ce n'est pas réglé."
        )
        ask = (
            "Dis-moi comment je corrige : tu veux que je **raffine** une "
            "étape, que tu **édites** un fixture, ou que je passe la main ?"
        )
    else:
        opener = "**Statut de répétition inconnu.** Voici ce que j'ai eu :"
        ask = "Comment tu veux qu'on procède ?"

    body = "\n".join([
        opener,
        "",
        *layer_lines,
        "",
    ])
    if issues:
        body += "\n_Points relevés :_\n" + "\n".join(f"- {i}" for i in issues) + "\n"
    body += "\n" + ask

    return TestResult(
        passed=(overall == "PASSED" and can_advance),
        issues=issues,
        suggestions=suggestions,
        summary_md=body,
    )


register_tester("dry_run_report", humanize_dry_run_report)
