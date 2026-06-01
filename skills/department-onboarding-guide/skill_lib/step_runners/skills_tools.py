"""
step_runners/skills_tools.py — Refonte #3 of 3, Deliverable A.

Conversational runner for Step 4 of the Notion eclosure flow
(lines 863-893). Replaces the single-prompt minimal/standard/étendu
question with a granular per-layer per-skill flow:

  Phase 1 — Skills loop (per subscribed layer):
    substep A : surface the skill needs identified from this layer's
                focalisation (committed at step 3). Operator may adjust.
    substep B(i): propose one full SKILL card (Notion 887-893 — 5 fields).
                  Operator approves / edits / refines. On approve, test
                  via test_artifact("skill", card, ctx) and commit
                  skills/<name>/SKILL.md.

  Phase 2 — Tools loop (linear, single pass):
    substep A : identify cross-layer tools needed (from skills' inputs/
                outputs). Operator may adjust.
    substep B(j): propose one full TOOL card. Same approve/edit/refine
                  + test_artifact("tool", card, ctx) + commit
                  tools/<name>/TOOL.md.

State machine + persistence pattern mirrors missions.py and layers.py.
Global UX commands: `disable skill X` / `add skill X` / `disable tool X`
work at any time.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..artifact_tests import test_artifact
from .base import Action, StepRunner, register_runner


# ----- Constants -----

STEP_NAME = "skills_tools"

SUBSTEP_SKILL_NEEDS = "skill_needs"       # substep A per layer
SUBSTEP_SKILL_DRAFT = "skill_draft"       # substep B per skill
SUBSTEP_TOOLS_NEEDS = "tools_needs"       # substep A for tools
SUBSTEP_TOOL_DRAFT = "tool_draft"         # substep B per tool


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- Layer-specific skill catalogues (proposals) -----
#
# Per-layer canonical skill catalogue. The runner picks 2-3 entries
# from the relevant layer when no operator override is given, so each
# subscribed layer gets at least one proposal. Names are taken from
# Notion v5 lines 866-877 (Miranda example) and generalised.

_SKILL_CATALOGUE: Dict[int, List[Dict[str, Any]]] = {
    1: [
        {
            "name": "content-signal-scanner",
            "purpose": "Détecter des idées de contenu à partir du wiki, de LinkedIn et des notes.",
            "inputs": ["wiki", "linkedin", "notes"],
            "outputs": ["content_idea_task"],
            "tests": "Fixture wiki snapshot + 3 entrées LinkedIn ; vérifier qu'au moins 1 content_idea_task est produit.",
            "status": "draft",
        },
        {
            "name": "calendar-reader",
            "purpose": "Lire le calendrier éditorial et matérialiser les échéances du jour.",
            "inputs": ["calendar"],
            "outputs": ["calendar_refresh_task"],
            "tests": "Calendrier fixture + vérifier que les échéances du jour sont extraites.",
            "status": "draft",
        },
        {
            "name": "external-news-watcher",
            "purpose": "Surveiller la presse spécialisée et remonter les news pertinentes.",
            "inputs": ["external_api", "rss"],
            "outputs": ["news_signal"],
            "tests": "Fixture RSS + vérifier qu'au moins un news_signal est produit.",
            "status": "draft",
        },
    ],
    2: [
        {
            "name": "post-drafter",
            "purpose": "Transformer un content_idea_task en 2-3 drafts de post.",
            "inputs": ["queue_item"],
            "outputs": ["draft"],
            "tests": "Fixture content_idea_task ; vérifier que >= 2 drafts entre 800 et 1500 caractères sont produits.",
            "status": "draft",
        },
        {
            "name": "angle-generator",
            "purpose": "Générer 3 angles éditoriaux à partir d'un draft donné.",
            "inputs": ["draft"],
            "outputs": ["draft"],
            "tests": "Fixture draft ; vérifier que >= 3 angles distincts sont proposés.",
            "status": "draft",
        },
    ],
    3: [
        {
            "name": "post-publisher",
            "purpose": "Publier ou programmer un post approuvé sur le canal cible.",
            "inputs": ["queue_item"],
            "outputs": ["external_api"],
            "tests": "Fixture decision validée ; vérifier que l'appel API est tracé dans exec-log.jsonl.",
            "status": "draft",
        },
        {
            "name": "calendar-updater",
            "purpose": "Mettre à jour le calendrier éditorial après publication.",
            "inputs": ["queue_item"],
            "outputs": ["calendar"],
            "tests": "Fixture publication + vérifier que le calendrier reflète l'événement.",
            "status": "draft",
        },
    ],
    4: [
        {
            "name": "brand-safety-auditor",
            "purpose": "Auditer les posts publiés pour détecter toute violation de brand safety.",
            "inputs": ["outputs"],
            "outputs": ["kpis"],
            "tests": "Fixture posts du jour + vérifier que les violations sont remontées dans risk-kpis.yaml.",
            "status": "draft",
        },
        {
            "name": "performance-reviewer",
            "purpose": "Analyser la performance des publications sur 7/30 jours et synthétiser un brief.",
            "inputs": ["outputs", "kpis"],
            "outputs": ["kpis"],
            "tests": "Fixture publications + KPIs ; vérifier que le brief synthétise les écarts.",
            "status": "draft",
        },
    ],
}


# Tools catalogue — cross-layer, picks based on skill inputs/outputs.
_TOOLS_CATALOGUE: List[Dict[str, Any]] = [
    {
        "name": "linkedin-reader",
        "purpose": "Lire la timeline LinkedIn et extraire les posts pertinents.",
        "inputs": [],
        "outputs": ["linkedin_post_signal"],
        "tests": "Fixture HTML ; vérifier qu'au moins 1 signal est extrait.",
        "status": "draft",
    },
    {
        "name": "shared-wiki-reader",
        "purpose": "Lire les pages du wiki partagé et renvoyer le contenu indexé.",
        "inputs": [],
        "outputs": ["wiki_snapshot"],
        "tests": "Fixture wiki ; vérifier qu'une page connue est retournée intacte.",
        "status": "draft",
    },
    {
        "name": "post-scheduler",
        "purpose": "Programmer une publication sur LinkedIn / X à une date donnée.",
        "inputs": [],
        "outputs": ["scheduled_post_event"],
        "tests": "Stub API ; vérifier que le payload appel correspond à la spec.",
        "status": "draft",
    },
    {
        "name": "analytics-reader",
        "purpose": "Récupérer les KPIs de performance sur les publications récentes.",
        "inputs": [],
        "outputs": ["kpis"],
        "tests": "Stub API analytics ; vérifier que les KPIs sont parsés.",
        "status": "draft",
    },
]


# ----- Prompt copy (FR, Bureau-de-Cadre) -----


def prompt_skill_needs(layer: int, needs: List[Dict[str, Any]],
                       focalisation_snippet: str) -> str:
    """Surface the skill needs identified for a layer."""
    needs_str = "\n".join(f"- `{n['name']}` — {n['purpose']}" for n in needs)
    return (
        f"**Étape 4 — Skills pour Layer {layer}.**\n\n"
        f"À partir de la focalisation que tu as approuvée à l'étape 3 :\n"
        f"_« {focalisation_snippet[:200]}… »_\n\n"
        f"J'ai **identifié** les skills suivants comme nécessaires :\n\n"
        f"{needs_str}\n\n"
        f"Tu veux **ajuster** cette liste avant que je te propose les "
        f"cartes une par une ? Réponds `ok` pour commencer, ou décris "
        f"les ajustements en texte libre."
    )


def prompt_skill_card(skill: Dict[str, Any], layer: int, idx: int,
                      total_layer: int) -> str:
    """Render a skill proposal as a Bureau-de-Cadre card."""
    inputs_str = ", ".join(f"`{x}`" for x in skill["inputs"])
    outputs_str = ", ".join(f"`{x}`" for x in skill["outputs"])
    return (
        f"**Skill #{idx}/{total_layer} pour Layer {layer} — "
        f"`{skill['name']}`**\n\n"
        f"- Purpose : {skill['purpose']}\n"
        f"- Inputs : {inputs_str}\n"
        f"- Outputs : {outputs_str}\n"
        f"- Tests : {skill['tests']}\n"
        f"- Status : `{skill['status']}`\n\n"
        "Tu **approuves** ? (1) Approuve / (2) Édite (texte libre) / "
        "(3) Raffine (nouvelle proposition)"
    )


def prompt_more_skills_for_layer(layer: int, count: int) -> str:
    return (
        f"J'ai validé {count} skill(s) pour Layer {layer}. Tu veux que "
        f"j'en ajoute d'autres avant de passer au layer suivant ?\n\n"
        f"Réponds `oui` pour proposer un skill supplémentaire, ou `non` "
        f"pour avancer."
    )


def prompt_tool_needs(tools: List[Dict[str, Any]]) -> str:
    tools_str = "\n".join(f"- `{t['name']}` — {t['purpose']}" for t in tools)
    return (
        "**Étape 4 — Tools transverses.**\n\n"
        "Pour réaliser les skills que tu as validés, j'ai besoin des "
        "tools suivants :\n\n"
        f"{tools_str}\n\n"
        "Tu veux **ajouter** ou **enlever** un tool avant que je te "
        "propose les cartes une par une ? Réponds `ok` pour commencer, "
        "ou décris les ajustements en texte libre."
    )


def prompt_tool_card(tool: Dict[str, Any], idx: int, total: int) -> str:
    inputs_str = (
        ", ".join(f"`{x}`" for x in tool["inputs"])
        if tool["inputs"] else "_(externe)_"
    )
    outputs_str = ", ".join(f"`{x}`" for x in tool["outputs"])
    return (
        f"**Tool #{idx}/{total} — `{tool['name']}`**\n\n"
        f"- Purpose : {tool['purpose']}\n"
        f"- Inputs : {inputs_str}\n"
        f"- Outputs : {outputs_str}\n"
        f"- Tests : {tool['tests']}\n"
        f"- Status : `{tool['status']}`\n\n"
        "Tu **approuves** ? (1) Approuve / (2) Édite (texte libre) / "
        "(3) Raffine (nouvelle proposition)"
    )


PROMPT_MORE_TOOLS = (
    "J'ai validé {n} tool(s). Tu veux en ajouter d'autres avant qu'on "
    "passe à l'étape suivante ?\n\n"
    "Réponds `oui` pour proposer un tool supplémentaire, ou `non` pour "
    "clore l'étape."
)


# ----- Parsing helpers -----

_APPROVE_RE = re.compile(
    r"\b(approuve|approuv[eé]s?|valide|valid[eé]s?|ok|d'?accord|oui|go)\b",
    re.IGNORECASE,
)
_EDIT_RE = re.compile(r"\b(édit|edit|modifie|corrige|réécris)\w*\b", re.IGNORECASE)
_REFINE_RE = re.compile(r"\b(raffin|refine|précise|reformule|reformul)\w*\b", re.IGNORECASE)
_NO_RE = re.compile(r"\b(non|nope|stop|passe|suivant|fini|c'?est bon)\b", re.IGNORECASE)

# Global UX commands.
_DISABLE_SKILL_RE = re.compile(
    r"(?:désactive|disable|retire|supprime|enlève)\s+skill\s+([a-z0-9-]+)",
    re.IGNORECASE,
)
_DISABLE_TOOL_RE = re.compile(
    r"(?:désactive|disable|retire|supprime|enlève)\s+tool\s+([a-z0-9-]+)",
    re.IGNORECASE,
)


def _classify_intent(text: str) -> Optional[Action]:
    if _REFINE_RE.search(text):
        return Action.REFINE
    if _EDIT_RE.search(text):
        return Action.EDIT
    if _APPROVE_RE.search(text):
        return Action.APPROVE_SUBSTEP
    return None


# ----- I/O helpers (mirror missions.py / layers.py) -----


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _atomic_write_yaml(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    tmp.replace(path)


def _render_skill_md(skill: Dict[str, Any], layer: int) -> str:
    inputs_str = "\n".join(f"- `{x}`" for x in skill["inputs"])
    outputs_str = "\n".join(f"- `{x}`" for x in skill["outputs"])
    return (
        f"# Skill — `{skill['name']}`\n\n"
        f"_Layer {layer} — capability card per Notion v5 lines 886-893._\n\n"
        f"## Purpose\n\n{skill['purpose']}\n\n"
        f"## Inputs\n\n{inputs_str or '_(aucune)_'}\n\n"
        f"## Outputs\n\n{outputs_str or '_(aucune)_'}\n\n"
        f"## Tests\n\n{skill['tests']}\n\n"
        f"## Status\n\n`{skill['status']}`\n\n"
        f"---\n_Composé par `skills/department-onboarding-guide/"
        f"skill_lib/step_runners/skills_tools.py` à l'étape 4._\n"
    )


def _render_tool_md(tool: Dict[str, Any]) -> str:
    inputs_str = "\n".join(f"- `{x}`" for x in tool["inputs"])
    outputs_str = "\n".join(f"- `{x}`" for x in tool["outputs"])
    return (
        f"# Tool — `{tool['name']}`\n\n"
        f"_Capability card per Notion v5 lines 886-893._\n\n"
        f"## Purpose\n\n{tool['purpose']}\n\n"
        f"## Inputs\n\n{inputs_str or '_(externe)_'}\n\n"
        f"## Outputs\n\n{outputs_str or '_(aucune)_'}\n\n"
        f"## Tests\n\n{tool['tests']}\n\n"
        f"## Status\n\n`{tool['status']}`\n\n"
        f"---\n_Composé par `skills/department-onboarding-guide/"
        f"skill_lib/step_runners/skills_tools.py` à l'étape 4._\n"
    )


def _read_focalisation(dept_root: Path, layer: int) -> str:
    """Read the layer's PROMPT.md committed at step 3."""
    pmd = dept_root / "layers" / str(layer) / "PROMPT.md"
    if not pmd.exists():
        return ""
    body = pmd.read_text(encoding="utf-8")
    # Extract the "Focalisation" section if present, else first 200 chars.
    m = re.search(
        r"(?:#+\s*Focalisation[^\n]*\n+)(.*?)(?=\n#+\s|\Z)",
        body, re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    return body[:300]


def _refine_variant(skill: Dict[str, Any], variant: int) -> Dict[str, Any]:
    """Generate a visibly different card on refine — flip status / tweak purpose."""
    out = dict(skill)
    if variant % 3 == 1:
        out["status"] = "tested" if out["status"] == "draft" else "draft"
        out["purpose"] = out["purpose"].rstrip(".") + " (variante affinée)."
    elif variant % 3 == 2:
        out["purpose"] = "[Approche alternative] " + out["purpose"]
        # Tweak the tests sentence
        out["tests"] = out["tests"].rstrip(".") + " — variante plus stricte."
    return out


def _apply_inline_edit(card: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Apply an operator inline edit. Recognises `édite: purpose <new>`."""
    out = dict(card)
    m = re.search(
        r"(?:édit|edit|modifie|corrige|réécris)\w*\s*[:\-]?\s*purpose\s+(.+)$",
        text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        out["purpose"] = m.group(1).strip()
        return out
    # Generic edit catch — append text to purpose
    m = re.search(
        r"(?:édit|edit|modifie|corrige|réécris)\w*\s*[:\-]\s*(.+)$",
        text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        addition = m.group(1).strip()
        if addition:
            out["purpose"] = out["purpose"].rstrip(".") + " — " + addition
    return out


# ----- Runner -----


class SkillsToolsRunner(StepRunner):
    """Conversational runner for Step 4 (Skills & tools)."""

    step_name = STEP_NAME

    def __init__(self) -> None:
        super().__init__()
        # Sub-validated artifacts (both skills and tools).
        self._validated_skills: List[Dict[str, Any]] = []
        self._validated_tools: List[Dict[str, Any]] = []
        self._current_substep: Optional[Dict[str, Any]] = None
        self._current_status: str = "drafting"

        # Skills phase state.
        self._subscribed_layers: List[int] = []
        self._current_layer: Optional[int] = None
        self._pending_layers: List[int] = []
        self._pending_skills_for_layer: List[Dict[str, Any]] = []
        self._current_skill: Optional[Dict[str, Any]] = None
        self._skill_refine_variant: int = 0
        self._layer_needs_confirmed: bool = False
        self._layer_more_asked: bool = False

        # Tools phase state.
        self._pending_tools: List[Dict[str, Any]] = []
        self._current_tool: Optional[Dict[str, Any]] = None
        self._tool_refine_variant: int = 0
        self._tools_needs_confirmed: bool = False
        self._tools_more_asked: bool = False
        self._tools_closed: bool = False

        # Phase tracker:
        # "skills_layer_needs" → "skills_card" → "skills_more"
        # → "tools_needs" → "tools_card" → "tools_more" → "done"
        self._phase: str = "skills_layer_needs"
        self._artifacts_written: List[Path] = []

    # ----- lifecycle -----

    def start(self, state_path: Path, dept_yaml_draft_path: Path) -> None:
        self.state_path = Path(state_path)
        self.dept_yaml_draft_path = Path(dept_yaml_draft_path)
        # Subscribed layers come from dept.yaml.draft.
        # v3 stores `layers:` at root; v2 (legacy) nested it under department.
        draft = _read_yaml(self.dept_yaml_draft_path)
        dept = draft.get("department") or {}
        layers_section = draft.get("layers") or dept.get("layers") or {}
        sub = layers_section.get("subscribed") if isinstance(layers_section, dict) else None
        if isinstance(sub, list) and sub:
            self._subscribed_layers = sorted(int(x) for x in sub)
        else:
            # Defensive — without step 3, we treat all 4 layers as subscribed.
            self._subscribed_layers = [1, 2, 3, 4]

        # Restore from state.
        state_doc = _read_yaml(self.state_path)
        progress = (state_doc.get("step_progress") or {}).get(self.step_name) or {}
        validated = list(progress.get("sub_artifacts_validated") or [])
        self._validated_skills = [
            e for e in validated if e.get("type") == SUBSTEP_SKILL_DRAFT
        ]
        self._validated_tools = [
            e for e in validated if e.get("type") == SUBSTEP_TOOL_DRAFT
        ]
        self._tools_closed = bool(progress.get("tools_closed"))
        # Recompute pending layers from subscribed - those already done.
        done_layers = {int(e.get("layer", 0)) for e in self._validated_skills}
        # Distinguish "done" (≥1 skill committed AND more_asked=False) — we
        # store the closed layers explicitly.
        closed_layers = set(progress.get("closed_layers") or [])
        self._pending_layers = [
            n for n in self._subscribed_layers if n not in closed_layers
        ]
        self._current_status = progress.get("current_status", "drafting")
        cs = progress.get("current_substep") or {}
        if isinstance(cs, dict):
            self._current_substep = dict(cs)
            payload = cs.get("draft_payload") or {}
            if cs.get("type") == SUBSTEP_SKILL_DRAFT:
                self._current_layer = payload.get("layer")
                self._current_skill = payload.get("skill")
                self._pending_skills_for_layer = list(
                    payload.get("pending_skills_for_layer") or []
                )
                self._skill_refine_variant = int(payload.get("refine_variant", 0))
                self._layer_needs_confirmed = True
                self._phase = "skills_card"
            elif cs.get("type") == SUBSTEP_TOOL_DRAFT:
                self._current_tool = payload.get("tool")
                self._pending_tools = list(payload.get("pending_tools") or [])
                self._tool_refine_variant = int(payload.get("refine_variant", 0))
                self._tools_needs_confirmed = True
                self._phase = "tools_card"

        # Compute phase if nothing in flight.
        if not self._current_substep:
            self._derive_phase()

        self._persist_progress(closed_layers=closed_layers)

    def _derive_phase(self) -> None:
        """Decide the phase based on validated lists and pending state.

        Sprint correctif Fix 3 (2026-05-21): the order of checks matters.
        We MUST verify `_current_tool` / `_current_skill` first, because
        those mean a card is in flight (operator hasn't answered yet).
        The previous heuristic flipped to `tools_more` as soon as
        `_validated_tools` had ≥ 1 entry, dropping all subsequent tools
        in the queue (and same bug for skills).
        """
        if self._tools_closed:
            self._phase = "done"
            return
        # Tools phase?
        if not self._pending_layers and not self._current_layer:
            # IF a tool card is in flight, stay in tools_card. This is the
            # poka-yoke against the QA-E2E drop bug.
            if self._current_tool is not None:
                self._phase = "tools_card"
                return
            if self._tools_needs_confirmed and self._pending_tools:
                # Queue has more items; advance to next card.
                self._current_tool = self._pending_tools.pop(0)
                self._tool_refine_variant = 0
                self._phase = "tools_card"
                return
            if self._tools_needs_confirmed:
                # Queue empty, no in-flight card → ask "want more?".
                self._phase = "tools_more"
                return
            self._phase = "tools_needs"
            return
        # Still in skills phase.
        if self._current_layer is None and self._pending_layers:
            # Advance to next layer.
            self._current_layer = self._pending_layers.pop(0)
            self._layer_needs_confirmed = False
            self._layer_more_asked = False
            self._pending_skills_for_layer = list(_SKILL_CATALOGUE.get(self._current_layer, []))
        if self._current_layer is not None:
            if not self._layer_needs_confirmed:
                self._phase = "skills_layer_needs"
            elif self._current_skill is not None:
                # Card in flight — must stay in skills_card (Fix 3
                # poka-yoke against the same drop bug).
                self._phase = "skills_card"
            elif self._pending_skills_for_layer:
                # Advance to next skill in layer.
                self._current_skill = self._pending_skills_for_layer.pop(0)
                self._skill_refine_variant = 0
                self._phase = "skills_card"
            else:
                self._phase = "skills_more"

    # ----- conversational surface -----

    def next_prompt(self) -> Optional[str]:
        if self.is_done():
            return None
        self._derive_phase()

        # SKILLS — substep A
        if self._phase == "skills_layer_needs" and self._current_layer is not None:
            assert self.dept_yaml_draft_path is not None
            dept_root = self.dept_yaml_draft_path.parent
            foc = _read_focalisation(dept_root, self._current_layer)
            needs = list(_SKILL_CATALOGUE.get(self._current_layer, []))
            return prompt_skill_needs(self._current_layer, needs, foc)

        # SKILLS — substep B
        if self._phase == "skills_card" and self._current_skill is not None and self._current_layer is not None:
            idx = (
                len([s for s in self._validated_skills
                     if s.get("layer") == self._current_layer]) + 1
            )
            total = idx + len(self._pending_skills_for_layer)
            return prompt_skill_card(
                self._current_skill, self._current_layer, idx, total,
            )

        # SKILLS — closing per layer
        if self._phase == "skills_more" and self._current_layer is not None:
            count = len([
                s for s in self._validated_skills
                if s.get("layer") == self._current_layer
            ])
            self._layer_more_asked = True
            return prompt_more_skills_for_layer(self._current_layer, count)

        # TOOLS — substep A
        if self._phase == "tools_needs":
            if not self._pending_tools:
                self._pending_tools = list(_TOOLS_CATALOGUE)
            return prompt_tool_needs(self._pending_tools)

        # TOOLS — substep B
        if self._phase == "tools_card" and self._current_tool is not None:
            idx = len(self._validated_tools) + 1
            total = idx + len(self._pending_tools)
            return prompt_tool_card(self._current_tool, idx, total)

        # TOOLS — closing
        if self._phase == "tools_more":
            self._tools_more_asked = True
            return PROMPT_MORE_TOOLS.format(n=len(self._validated_tools))

        return None

    def on_answer(self, operator_text: str) -> Action:
        text = (operator_text or "").strip()
        if not text:
            return Action.CONTINUE

        # Global UX commands first.
        if self._handle_global_commands(text):
            return Action.CONTINUE

        # SKILLS — substep A
        if self._phase == "skills_layer_needs" and self._current_layer is not None:
            # "ok" / "approuve" / "oui" → confirm and move to first skill.
            if _APPROVE_RE.search(text):
                self._layer_needs_confirmed = True
                if not self._pending_skills_for_layer:
                    self._pending_skills_for_layer = list(
                        _SKILL_CATALOGUE.get(self._current_layer, [])
                    )
                # Pop first skill as current.
                if self._pending_skills_for_layer:
                    self._current_skill = self._pending_skills_for_layer.pop(0)
                    self._skill_refine_variant = 0
                    self._persist_skill_substep()
                self._phase = "skills_card"
                return Action.CONTINUE
            # If operator types a free-text adjustment, accept and proceed.
            # We keep the catalogue as-is and let them edit later via card.
            self._layer_needs_confirmed = True
            if not self._pending_skills_for_layer:
                self._pending_skills_for_layer = list(
                    _SKILL_CATALOGUE.get(self._current_layer, [])
                )
            if self._pending_skills_for_layer:
                self._current_skill = self._pending_skills_for_layer.pop(0)
                self._skill_refine_variant = 0
                self._persist_skill_substep()
            self._phase = "skills_card"
            return Action.CONTINUE

        # SKILLS — substep B
        if self._phase == "skills_card" and self._current_skill is not None:
            intent = _classify_intent(text)
            if intent == Action.APPROVE_SUBSTEP:
                committed = self._commit_current_skill()
                if not committed:
                    return Action.CONTINUE
                # Advance to next skill in the same layer OR ask "more?".
                # Set phase explicitly (Fix 3 poka-yoke) — never rely on
                # _derive_phase to figure out we're still in the loop.
                if self._pending_skills_for_layer:
                    self._current_skill = self._pending_skills_for_layer.pop(0)
                    self._skill_refine_variant = 0
                    self._phase = "skills_card"
                    self._persist_skill_substep()
                else:
                    self._current_skill = None
                    self._phase = "skills_more"
                    self._current_substep = None
                    self._persist_progress()
                return Action.APPROVE_SUBSTEP
            if intent == Action.EDIT:
                self._current_skill = _apply_inline_edit(self._current_skill, text)
                self._persist_skill_substep()
                return Action.EDIT
            if intent == Action.REFINE:
                self._skill_refine_variant += 1
                self._current_skill = _refine_variant(
                    self._current_skill, self._skill_refine_variant,
                )
                self._persist_skill_substep()
                return Action.REFINE
            return Action.CONTINUE

        # SKILLS — closing for the layer
        if self._phase == "skills_more" and self._current_layer is not None:
            if _NO_RE.search(text):
                # Close this layer; advance to the next one or to tools.
                self._close_current_layer()
                return Action.CONTINUE
            if _APPROVE_RE.search(text):
                # Operator wants more skills for this layer — refill catalogue.
                catalog = list(_SKILL_CATALOGUE.get(self._current_layer, []))
                # Skip already-validated ones.
                done_names = {
                    s.get("name") for s in self._validated_skills
                    if s.get("layer") == self._current_layer
                }
                self._pending_skills_for_layer = [
                    s for s in catalog if s["name"] not in done_names
                ]
                if self._pending_skills_for_layer:
                    self._current_skill = self._pending_skills_for_layer.pop(0)
                    self._skill_refine_variant = 0
                    self._phase = "skills_card"
                    self._persist_skill_substep()
                else:
                    self._close_current_layer()
                return Action.CONTINUE
            return Action.CONTINUE

        # TOOLS — substep A
        if self._phase == "tools_needs":
            self._tools_needs_confirmed = True
            if not self._pending_tools:
                self._pending_tools = list(_TOOLS_CATALOGUE)
            self._current_tool = self._pending_tools.pop(0)
            self._tool_refine_variant = 0
            self._phase = "tools_card"
            self._persist_tool_substep()
            return Action.CONTINUE

        # TOOLS — substep B
        if self._phase == "tools_card" and self._current_tool is not None:
            intent = _classify_intent(text)
            if intent == Action.APPROVE_SUBSTEP:
                committed = self._commit_current_tool()
                if not committed:
                    return Action.CONTINUE
                # Set phase explicitly (Fix 3 poka-yoke against the
                # _derive_phase drop bug).
                if self._pending_tools:
                    self._current_tool = self._pending_tools.pop(0)
                    self._tool_refine_variant = 0
                    self._phase = "tools_card"
                    self._persist_tool_substep()
                else:
                    self._current_tool = None
                    self._phase = "tools_more"
                    self._current_substep = None
                    self._persist_progress()
                return Action.APPROVE_SUBSTEP
            if intent == Action.EDIT:
                self._current_tool = _apply_inline_edit(self._current_tool, text)
                self._persist_tool_substep()
                return Action.EDIT
            if intent == Action.REFINE:
                self._tool_refine_variant += 1
                self._current_tool = _refine_variant(
                    self._current_tool, self._tool_refine_variant,
                )
                self._persist_tool_substep()
                return Action.REFINE
            return Action.CONTINUE

        # TOOLS — closing
        if self._phase == "tools_more":
            if _NO_RE.search(text):
                self._tools_closed = True
                self._current_status = "validated"
                self._phase = "done"
                self._persist_progress()
                return Action.DONE
            if _APPROVE_RE.search(text):
                # Refill catalogue minus already-validated tools.
                done_names = {t.get("name") for t in self._validated_tools}
                self._pending_tools = [
                    t for t in _TOOLS_CATALOGUE if t["name"] not in done_names
                ]
                if self._pending_tools:
                    self._current_tool = self._pending_tools.pop(0)
                    self._tool_refine_variant = 0
                    self._phase = "tools_card"
                    self._persist_tool_substep()
                else:
                    self._tools_closed = True
                    self._current_status = "validated"
                    self._phase = "done"
                    self._persist_progress()
                    return Action.DONE
                return Action.CONTINUE
            return Action.CONTINUE

        return Action.CONTINUE

    def is_done(self) -> bool:
        if not self._tools_closed:
            return False
        if not self._validated_skills or not self._validated_tools:
            return False
        # Every subscribed layer must have ≥1 skill committed.
        layers_done = {int(s.get("layer", 0)) for s in self._validated_skills}
        return set(self._subscribed_layers).issubset(layers_done)

    def artifacts_produced(self) -> List[Path]:
        return list(self._artifacts_written)

    # ----- internal -----

    def _close_current_layer(self) -> None:
        """Mark the current layer as closed and advance phase."""
        assert self._current_layer is not None
        closed = self._read_closed_layers()
        closed.add(self._current_layer)
        self._current_layer = None
        self._current_skill = None
        self._pending_skills_for_layer = []
        self._layer_needs_confirmed = False
        self._layer_more_asked = False
        self._current_substep = None
        # Move to next pending layer if any.
        if self._pending_layers:
            self._current_layer = self._pending_layers.pop(0)
            self._pending_skills_for_layer = list(
                _SKILL_CATALOGUE.get(self._current_layer, [])
            )
            self._phase = "skills_layer_needs"
        else:
            self._phase = "tools_needs"
        self._persist_progress(closed_layers=closed)

    def _read_closed_layers(self) -> set:
        if self.state_path is None:
            return set()
        doc = _read_yaml(self.state_path)
        progress = (doc.get("step_progress") or {}).get(self.step_name) or {}
        return set(progress.get("closed_layers") or [])

    def _persist_skill_substep(self) -> None:
        self._current_substep = {
            "type": SUBSTEP_SKILL_DRAFT,
            "draft_payload": {
                "layer": self._current_layer,
                "skill": self._current_skill,
                "pending_skills_for_layer": list(self._pending_skills_for_layer),
                "refine_variant": self._skill_refine_variant,
            },
        }
        self._current_status = "awaiting_validation"
        self._persist_progress()

    def _persist_tool_substep(self) -> None:
        self._current_substep = {
            "type": SUBSTEP_TOOL_DRAFT,
            "draft_payload": {
                "tool": self._current_tool,
                "pending_tools": list(self._pending_tools),
                "refine_variant": self._tool_refine_variant,
            },
        }
        self._current_status = "awaiting_validation"
        self._persist_progress()

    def _commit_current_skill(self) -> bool:
        """Test the current skill and, on PASS, write it to disk."""
        assert self._current_skill is not None
        assert self._current_layer is not None
        assert self.dept_yaml_draft_path is not None
        dept_root = self.dept_yaml_draft_path.parent
        ctx = {
            "dept_root": dept_root,
            "dept_yaml_draft_path": self.dept_yaml_draft_path,
        }
        result = test_artifact("skill", self._current_skill, ctx)
        if not result.passed:
            return False
        skill_dir = dept_root / "skills" / self._current_skill["name"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text(
            _render_skill_md(self._current_skill, self._current_layer),
            encoding="utf-8",
        )
        if skill_md not in self._artifacts_written:
            self._artifacts_written.append(skill_md)
        # Append to validated.
        self._validated_skills.append({
            "id": self._current_skill["name"],
            "name": self._current_skill["name"],
            "type": SUBSTEP_SKILL_DRAFT,
            "layer": self._current_layer,
            "card": dict(self._current_skill),
            "validated_at": _now_iso(),
        })
        # Sync into dept.yaml.draft.
        self._sync_skills_in_draft()
        return True

    def _commit_current_tool(self) -> bool:
        assert self._current_tool is not None
        assert self.dept_yaml_draft_path is not None
        dept_root = self.dept_yaml_draft_path.parent
        ctx = {
            "dept_root": dept_root,
            "dept_yaml_draft_path": self.dept_yaml_draft_path,
        }
        result = test_artifact("tool", self._current_tool, ctx)
        if not result.passed:
            return False
        tool_dir = dept_root / "tools" / self._current_tool["name"]
        tool_dir.mkdir(parents=True, exist_ok=True)
        tool_md = tool_dir / "TOOL.md"
        tool_md.write_text(_render_tool_md(self._current_tool), encoding="utf-8")
        if tool_md not in self._artifacts_written:
            self._artifacts_written.append(tool_md)
        self._validated_tools.append({
            "id": self._current_tool["name"],
            "name": self._current_tool["name"],
            "type": SUBSTEP_TOOL_DRAFT,
            "card": dict(self._current_tool),
            "validated_at": _now_iso(),
        })
        self._sync_tools_in_draft()
        return True

    def _sync_skills_in_draft(self) -> None:
        """Mirror per-layer skills into dept.yaml.draft.

        v3 schema layout (cf. schemas-draft/examples/dept-ops-maya.yaml):
        `skills:` (with `layer_1..4` lists) is a TOP-LEVEL sibling of
        `department:`, not nested under it.
        """
        assert self.dept_yaml_draft_path is not None
        draft = _read_yaml(self.dept_yaml_draft_path)
        # Ensure department block exists (mandate step seeds it).
        draft.setdefault("department", {})
        # Strip any legacy nested copy.
        draft["department"].pop("skills", None)
        skills_section: Dict[str, List[str]] = {
            "layer_1": [], "layer_2": [], "layer_3": [], "layer_4": [],
        }
        for s in self._validated_skills:
            lyr = int(s.get("layer", 1))
            skills_section[f"layer_{lyr}"].append(s["name"])
        # Drop empty layer keys so additionalProperties: false (which is on)
        # doesn't bite, and consumers don't see meaningless empty lists.
        skills_section = {k: v for k, v in skills_section.items() if v}
        draft["skills"] = skills_section
        _atomic_write_yaml(self.dept_yaml_draft_path, draft)
        if self.dept_yaml_draft_path not in self._artifacts_written:
            self._artifacts_written.append(self.dept_yaml_draft_path)

    def _sync_tools_in_draft(self) -> None:
        """Mirror tools list into dept.yaml.draft.

        v3 schema layout: `tools:` is a TOP-LEVEL sibling of `department:`.
        """
        assert self.dept_yaml_draft_path is not None
        draft = _read_yaml(self.dept_yaml_draft_path)
        draft.setdefault("department", {})
        # Strip any legacy nested copy.
        draft["department"].pop("tools", None)
        draft["tools"] = [t["name"] for t in self._validated_tools]
        _atomic_write_yaml(self.dept_yaml_draft_path, draft)

    # ----- UX commands -----

    def _handle_global_commands(self, text: str) -> bool:
        m = _DISABLE_SKILL_RE.search(text)
        if m:
            return self._disable_skill(m.group(1))
        m = _DISABLE_TOOL_RE.search(text)
        if m:
            return self._disable_tool(m.group(1))
        return False

    def _disable_skill(self, name: str) -> bool:
        assert self.dept_yaml_draft_path is not None
        dept_root = self.dept_yaml_draft_path.parent
        skill_dir = dept_root / "skills" / name
        if skill_dir.exists():
            try:
                (skill_dir / "SKILL.md").unlink(missing_ok=True)
                if not any(skill_dir.iterdir()):
                    skill_dir.rmdir()
            except Exception:  # pragma: no cover - defensive
                pass
        self._validated_skills = [
            s for s in self._validated_skills if s.get("name") != name
        ]
        self._sync_skills_in_draft()
        self._persist_progress()
        return True

    def _disable_tool(self, name: str) -> bool:
        assert self.dept_yaml_draft_path is not None
        dept_root = self.dept_yaml_draft_path.parent
        tool_dir = dept_root / "tools" / name
        if tool_dir.exists():
            try:
                (tool_dir / "TOOL.md").unlink(missing_ok=True)
                if not any(tool_dir.iterdir()):
                    tool_dir.rmdir()
            except Exception:  # pragma: no cover - defensive
                pass
        self._validated_tools = [
            t for t in self._validated_tools if t.get("name") != name
        ]
        self._sync_tools_in_draft()
        self._persist_progress()
        return True

    # ----- persistence -----

    def _persist_progress(self, closed_layers: Optional[set] = None) -> None:
        if self.state_path is None:
            return
        doc = _read_yaml(self.state_path)
        progress = doc.setdefault("step_progress", {})
        # Merge closed_layers.
        prior = (progress.get(self.step_name) or {}).get("closed_layers") or []
        cl = set(prior)
        if closed_layers is not None:
            cl = closed_layers
        entry: Dict[str, Any] = {
            "sub_artifacts_validated": (
                list(self._validated_skills) + list(self._validated_tools)
            ),
            "current_substep": self._current_substep,
            "current_status": self._current_status,
            "closed_layers": sorted(cl),
        }
        if self._tools_closed:
            entry["tools_closed"] = True
        progress[self.step_name] = entry
        doc["last_updated_at"] = _now_iso()
        _atomic_write_yaml(self.state_path, doc)


register_runner(STEP_NAME, SkillsToolsRunner)
