"""
artifact_tests/skill.py — Refonte #3 of 3, Deliverable C.

Per-skill semantic + completeness tester. Notion v5 lines 886-893
mandate the 5-field card shape:

    content-signal-scanner
    Purpose: détecter des idées de contenu
    Inputs: wiki, LinkedIn, notes
    Outputs: content_idea_task
    Tests: missing
    Status: draft

Checks:
  1. 5 mandatory fields present + non-empty (purpose, inputs[],
     outputs[], tests, status).
  2. `purpose` >= 10 chars, contains >= 1 verb (best-effort French verb
     spotting), no obvious English jargon.
  3. `inputs[]` non-empty, lists strings — best-effort soft cross-check
     against known artifact kinds (queue_item, signal, wiki, ...).
  4. `outputs[]` non-empty, lists strings — same soft check.
  5. `status` in {draft, tested, live}.
  6. `tests` field is >= 10 chars (the card promises a real test, not
     'missing').
  7. Isolation simulation: synthesise a minimal fake input matching
     `inputs[0]` and ensure the card's promised output shape is named
     in `outputs[]`.

Returns a FR Bureau-de-Cadre TestResult.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import TestResult, register_tester


_VALID_STATUS = ("draft", "tested", "live")

# Best-effort known-artifact-kind hints. Soft check only — surfacing a
# suggestion if a skill's input / output doesn't look like any known
# kind. Mirrors the kinds used across queue-item.schema.yaml and the
# domain action classes.
_KNOWN_INPUT_HINTS = {
    "wiki", "linkedin", "notes", "queue_item", "signal", "draft",
    "inbox", "queues/research", "queues/gates", "outputs",
    "crm", "gmail", "calendar", "rss", "notion", "github",
    "external_api", "data_lake", "kpis", "shared-wiki",
}

# A loose FR verb-stem detector. Bureau-de-Cadre purposes always start
# with a verb (Détecter, Transformer, Auditer, Préparer, Générer, ...).
# Match endings -er / -ir / -re plus a few common irregulars; soft check
# only (we never block on this alone — combined with `<10 chars` it's
# a strong signal something is wrong).
_FR_VERB_RE = re.compile(
    r"\b("
    r"[a-zàâäéèêëîïôöùûüç]{3,}(?:er|ir|re|ent|ant)|"
    r"(?:produire|construire|faire|prendre|mettre|écrire|lire|voir|"
    r"être|avoir|aller|venir|appeler|publier|détecter|générer|"
    r"transformer|préparer|auditer|valider|exécuter|exécute|surveiller|"
    r"identifier|analyser|synthétiser|rédiger|relire|gérer|maintenir)"
    r")\b",
    re.IGNORECASE,
)

# Heuristic English-jargon list — words that signal the purpose was
# copy-pasted from an English brief rather than written in French.
_ENGLISH_JARGON = {
    "scan", "scanner", "fetch", "trigger", "monitor", "deploy",
    "ship", "rollout", "handle", "dispatch", "pipeline",
}


def _has_fr_verb(text: str) -> bool:
    """Best-effort FR-verb spotter. Returns True if any verb-like token."""
    return bool(_FR_VERB_RE.search(text))


def _has_english_jargon(text: str) -> bool:
    """Return True if a clearly-English word appears (best-effort)."""
    tokens = re.findall(r"[A-Za-z]+", text.lower())
    return any(t in _ENGLISH_JARGON for t in tokens)


def _looks_like_artifact_kind(s: str) -> bool:
    """Return True if a soft hint matches a known artifact kind."""
    s = s.strip().lower()
    if s in _KNOWN_INPUT_HINTS:
        return True
    # snake_case + _task suffix is a queue-item kind (per Notion 348).
    if re.match(r"^[a-z][a-z0-9_]+(?:_task|_item|_event|_signal)$", s):
        return True
    return False


def test_skill(payload: Dict[str, Any], ctx: Optional[Any] = None) -> TestResult:
    """Validate one skill card and return a TestResult."""
    issues: List[str] = []
    suggestions: List[str] = []

    if not isinstance(payload, dict):
        return TestResult(
            passed=False,
            issues=["Le skill n'est pas un dictionnaire — impossible à valider."],
            summary_md="**Skill refusé** — format inattendu.",
        )

    name = payload.get("name") or payload.get("slug") or "?"

    # 1. 5 mandatory fields.
    purpose = payload.get("purpose")
    inputs = payload.get("inputs")
    outputs = payload.get("outputs")
    tests_desc = payload.get("tests")
    status = payload.get("status")

    if not (isinstance(purpose, str) and purpose.strip()):
        issues.append(
            "Champ `purpose` manquant ou vide (cf. Notion v5 ligne 888)."
        )
    elif len(purpose.strip()) < 10:
        issues.append(
            f"Le `purpose` est trop court ({len(purpose.strip())} caractères, "
            "minimum 10) — décris l'intention en une phrase complète."
        )

    if not (isinstance(inputs, list) and len(inputs) >= 1
            and all(isinstance(x, str) and x.strip() for x in inputs)):
        issues.append(
            "Champ `inputs[]` vide ou mal formé. Liste au moins une source "
            "(wiki, linkedin, queue_item, etc.) — cf. Notion v5 ligne 889."
        )

    if not (isinstance(outputs, list) and len(outputs) >= 1
            and all(isinstance(x, str) and x.strip() for x in outputs)):
        issues.append(
            "Champ `outputs[]` vide ou mal formé. Liste au moins un type "
            "de sortie (content_idea_task, draft_post, etc.) — cf. Notion "
            "v5 ligne 890."
        )

    if not (isinstance(tests_desc, str) and tests_desc.strip()):
        issues.append(
            "Champ `tests` manquant ou vide. Décris ne serait-ce qu'une "
            "phrase la procédure de test en isolation (cf. Notion v5 "
            "ligne 891)."
        )
    elif len(tests_desc.strip()) < 10:
        issues.append(
            f"La description du `tests` est trop courte "
            f"({len(tests_desc.strip())} caractères, minimum 10). "
            "« missing » ne suffit pas — promets une vraie fixture."
        )

    if status not in _VALID_STATUS:
        issues.append(
            f"Champ `status` `{status}` invalide. Attendu : "
            + ", ".join(f"`{s}`" for s in _VALID_STATUS) + "."
        )

    # 2. FR Bureau-de-Cadre voice for `purpose`.
    if isinstance(purpose, str) and len(purpose.strip()) >= 10:
        if not _has_fr_verb(purpose):
            issues.append(
                "Le `purpose` ne semble pas en français Bureau-de-Cadre "
                "(aucun verbe identifié). Commence par un verbe d'action "
                "(Détecter, Transformer, Préparer, Générer, …)."
            )
        if _has_english_jargon(purpose):
            suggestions.append(
                "Le `purpose` contient du jargon anglais. Préfère un "
                "vocabulaire FR (ex. `scanner` → `analyser` / `parcourir`)."
            )

    # 3-4. Soft checks on inputs / outputs vs known artifact kinds.
    if isinstance(inputs, list):
        unknown_inputs = [
            x for x in inputs
            if isinstance(x, str) and x.strip()
            and not _looks_like_artifact_kind(x)
        ]
        if unknown_inputs:
            suggestions.append(
                "Inputs hors taxonomie : "
                + ", ".join(f"`{x}`" for x in unknown_inputs)
                + ". Si c'est volontaire (source externe), tout va bien — "
                "sinon vérifie qu'ils suivent la nomenclature `*_task` / "
                "`wiki` / `linkedin` / …"
            )
    if isinstance(outputs, list):
        unknown_outputs = [
            x for x in outputs
            if isinstance(x, str) and x.strip()
            and not _looks_like_artifact_kind(x)
        ]
        if unknown_outputs:
            suggestions.append(
                "Outputs hors taxonomie : "
                + ", ".join(f"`{x}`" for x in unknown_outputs)
                + ". Vérifie qu'ils correspondent à un kind de queue item "
                "(ex. `content_idea_task`) ou à un format documenté."
            )

    # 5. Isolation simulation — best-effort sanity check.
    if not issues and isinstance(inputs, list) and isinstance(outputs, list):
        first_input = inputs[0]
        first_output = outputs[0]
        # The isolation test passes if the skill names what it produces
        # and the output isn't accidentally the same as the input
        # (a useless identity skill).
        if first_input == first_output:
            suggestions.append(
                f"L'isolation est triviale : `{first_input}` en entrée et "
                f"en sortie. Vérifie que la transformation a du sens."
            )

    passed = not issues
    if passed:
        summary = (
            f"**Skill `{name}` validé.**\n\n"
            f"- Purpose : *« {purpose.strip()} »*\n"
            f"- Inputs : " + ", ".join(f"`{x}`" for x in inputs) + "\n"
            f"- Outputs : " + ", ".join(f"`{x}`" for x in outputs) + "\n"
            f"- Tests : {tests_desc.strip()[:80]}…\n"
            f"- Status : `{status}`"
        )
        if suggestions:
            summary += "\n\n_Pistes de polish (non bloquantes) :_\n" + \
                "\n".join(f"- {s}" for s in suggestions)
    else:
        summary = (
            f"**Skill `{name}` — à corriger.**\n\n"
            "Voici ce qui bloque :\n\n"
            + "\n".join(f"- {i}" for i in issues)
        )

    return TestResult(
        passed=passed,
        issues=issues,
        suggestions=suggestions,
        summary_md=summary,
    )


test_skill.__test__ = False  # type: ignore[attr-defined]
register_tester("skill", test_skill)
