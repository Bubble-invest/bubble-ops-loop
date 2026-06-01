"""
artifact_tests/tool.py — Refonte #3 of 3, Deliverable D.

Per-tool semantic + completeness tester. Notion v5 lines 879-893
mandate that tools follow the same 5-field card shape as skills, plus
a kebab-case naming convention (lines 880-883).

Checks:
  1. 5 mandatory fields (name, purpose, inputs[], outputs[], tests,
     status).
  2. Tool `name` is kebab-case (lowercase letters, digits, hyphens —
     no underscores, no CamelCase).
  3. `inputs[]` may be empty (tools often pull from external SaaS).
  4. `outputs[]` must be non-empty.
  5. `status` in {draft, tested, live}.
  6. Discovery: best-effort check whether the tool exists at one of the
     known locations:
        - <dept_root>/tools/<name>/
        - ~/.claude/skills/<name>/
        - known SaaS list (linkedin-reader, gmail-reader, ...)
     If NOT found, emits a non-blocking suggestion that mentions
     installation. Does NOT FAIL.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import TestResult, register_tester


_VALID_STATUS = ("draft", "tested", "live")

_KEBAB_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

# Known SaaS / built-in tools that ship with the platform. If a card
# names one of these, no on-disk lookup is needed.
_KNOWN_SAAS_TOOLS = {
    "linkedin-reader",
    "gmail-reader",
    "shared-wiki-reader",
    "wiki-reader",
    "notion-crm",
    "notion-reader",
    "post-scheduler",
    "analytics-reader",
    "apollo-api",
    "telegram-reporter",
    "stripe-reader",
    "alpaca-reader",
}


def _tool_exists_locally(name: str, ctx: Optional[Any]) -> bool:
    """Best-effort discovery — does this tool exist at a known path?"""
    if name in _KNOWN_SAAS_TOOLS:
        return True
    # Check <dept_root>/tools/<name>/
    dept_root: Optional[Path] = None
    if isinstance(ctx, dict):
        dept_root = ctx.get("dept_root")
    elif ctx is not None:
        dept_root = getattr(ctx, "dept_root", None)
    if dept_root is not None:
        candidate = Path(dept_root) / "tools" / name
        if candidate.exists() and candidate.is_dir():
            return True
    # Check ~/.claude/skills/<name>/
    claude_skills = Path.home() / ".claude" / "skills" / name
    if claude_skills.exists() and claude_skills.is_dir():
        return True
    return False


def test_tool(payload: Dict[str, Any], ctx: Optional[Any] = None) -> TestResult:
    """Validate one tool card and return a TestResult."""
    issues: List[str] = []
    suggestions: List[str] = []

    if not isinstance(payload, dict):
        return TestResult(
            passed=False,
            issues=["Le tool n'est pas un dictionnaire — impossible à valider."],
            summary_md="**Tool refusé** — format inattendu.",
        )

    name = payload.get("name") or payload.get("slug") or ""

    # 1. 5 mandatory fields.
    purpose = payload.get("purpose")
    inputs = payload.get("inputs")
    outputs = payload.get("outputs")
    tests_desc = payload.get("tests")
    status = payload.get("status")

    if not name:
        issues.append("Champ `name` manquant.")
    elif not _KEBAB_RE.match(name):
        issues.append(
            f"Le nom `{name}` n'est pas en kebab-case (Notion v5 ligne "
            "880). Attendu : minuscules + chiffres + tirets, sans "
            "underscores ni majuscules (ex. `linkedin-reader`)."
        )

    if not (isinstance(purpose, str) and purpose.strip()):
        issues.append("Champ `purpose` manquant ou vide.")
    elif len(purpose.strip()) < 10:
        issues.append(
            f"Le `purpose` est trop court ({len(purpose.strip())} caractères, "
            "minimum 10)."
        )

    # inputs[] may be empty for tools that pull from external SaaS.
    if not isinstance(inputs, list):
        issues.append("Champ `inputs[]` manquant ou pas une liste.")
    elif not all(isinstance(x, str) for x in inputs):
        issues.append("Tous les éléments de `inputs[]` doivent être des strings.")

    # outputs[] MUST be non-empty.
    if not (isinstance(outputs, list) and len(outputs) >= 1
            and all(isinstance(x, str) and x.strip() for x in outputs)):
        issues.append(
            "Champ `outputs[]` vide ou mal formé. Un tool sans output "
            "est inutile — déclare au moins ce qu'il renvoie."
        )

    if not (isinstance(tests_desc, str) and tests_desc.strip()):
        issues.append("Champ `tests` manquant ou vide.")
    elif len(tests_desc.strip()) < 10:
        issues.append(
            f"La description du `tests` est trop courte "
            f"({len(tests_desc.strip())} caractères, minimum 10)."
        )

    if status not in _VALID_STATUS:
        issues.append(
            f"Champ `status` `{status}` invalide. Attendu : "
            + ", ".join(f"`{s}`" for s in _VALID_STATUS) + "."
        )

    # 5. Discovery (only when other checks pass — avoids noise).
    if not issues and name:
        if not _tool_exists_locally(name, ctx):
            suggestions.append(
                f"Le tool `{name}` est introuvable localement "
                "(ni dans `tools/<name>/`, ni dans `~/.claude/skills/<name>/`, "
                "ni dans la liste SaaS connue). À installer / créer avant "
                "le dry-run."
            )

    passed = not issues
    if passed:
        outputs_str = ", ".join(f"`{x}`" for x in outputs)
        inputs_str = (
            ", ".join(f"`{x}`" for x in inputs) if inputs else "_(externe)_"
        )
        summary = (
            f"**Tool `{name}` validé.**\n\n"
            f"- Purpose : *« {purpose.strip()} »*\n"
            f"- Inputs : {inputs_str}\n"
            f"- Outputs : {outputs_str}\n"
            f"- Tests : {tests_desc.strip()[:80]}…\n"
            f"- Status : `{status}`"
        )
        if suggestions:
            summary += "\n\n_Pistes de polish (non bloquantes) :_\n" + \
                "\n".join(f"- {s}" for s in suggestions)
    else:
        summary = (
            f"**Tool `{name or '?'}` — à corriger.**\n\n"
            "Voici ce qui bloque :\n\n"
            + "\n".join(f"- {i}" for i in issues)
        )

    return TestResult(
        passed=passed,
        issues=issues,
        suggestions=suggestions,
        summary_md=summary,
    )


test_tool.__test__ = False  # type: ignore[attr-defined]
register_tester("tool", test_tool)
