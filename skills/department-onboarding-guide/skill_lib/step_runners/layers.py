"""
step_runners/layers.py — Refonte #2 of 3, Deliverable C.

Conversational runner for Step 3 of the Notion eclosure flow
(lines 847-862). Replaces the single-prompt 3-subscription-pattern
question with a granular per-layer flow that:

  1. asks which of the 4 layers the dept subscribes to (substep A);
  2. then, for EACH subscribed layer (substep B(N)):
       a. recalls the doctrinal 1-liner from Notion v5 lines 440-468;
       b. proposes 2-3 sentences of focalisation for THIS dept;
       c. on APPROVE, writes layers/<N>/PROMPT.md and tests via
          test_artifact("layer_focus", ...).

State machine + persistence pattern mirrors `mandate.py` and
`missions.py` so sub-agent #3 can copy-paste the same idioms for
Step 4 (skills_tools) and Step 5 (gates_kpis).
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

STEP_NAME = "layers"

SUBSTEP_SUBSCRIBED_LAYERS = "subscribed_layers"   # substep A
SUBSTEP_LAYER_FOCUS = "layer_focus"               # substep B(N)


# Verbatim from Notion v5 lines 440-468. Each entry carries:
#   name      — French display name (Data / Recherche / Exécution / Risk)
#   one_liner — the doctrinal 1-2 sentence pitch
#   outputs   — the canonical write targets (used in the rendered PROMPT.md)
_LAYER_GENERIC_DESCRIPTION: Dict[int, Dict[str, Any]] = {
    1: {
        "name": "Data Update",
        "one_liner": (
            "Refresh data externe + interne, lire les recurring missions "
            "dues, puis matérialiser les besoins du jour en queue items."
        ),
        "default_cadence": "06:00 UTC daily",
        "outputs": ["outputs/<date>/1/plan.md", "queues/research/"],
    },
    2: {
        "name": "Research / Plan",
        "one_liner": (
            "C'est le moment où je transforme les signaux en idées "
            "exploitables (drafts, plans, variantes) — l'orchestrateur "
            "consomme les queue items et spawn des sub-agents dédiés."
        ),
        "default_cadence": "every 20-60 min",
        "outputs": ["outputs/<date>/2/research/*.md", "queues/gates/<id>.yaml"],
    },
    3: {
        "name": "Execution",
        "one_liner": (
            "Lit les choix utilisateur depuis `inbox/decisions/`, exécute "
            "(passage d'ordres, envoi de messages, commits de code), log tout."
        ),
        "default_cadence": "every 20-60 min",
        "outputs": [
            "actions broker/email/git",
            "outputs/<date>/3/exec-log.jsonl",
        ],
    },
    4: {
        "name": "Risk / Quality",
        "one_liner": (
            "Examine TOUTES les actions et résultats du jour vs mandat. "
            "Analyse perfs dept, trace les bugs, compile brief d'amélioration."
        ),
        "default_cadence": "22:00 UTC daily",
        "outputs": [
            "outputs/<date>/4/risk-kpis.yaml",
            "outputs/<date>/4/risk-brief.md",
            "outputs/<date>/management-export.yaml",
        ],
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- Prompt copy (FR, Bureau-de-Cadre) -----

PROMPT_SUBSTEP_A = (
    "**Étape 3 — Mes 4 moments de la journée.**\n\n"
    "Quelles couches du flow standard tu veux que je couvre ? Je peux "
    "être présente :\n\n"
    "1. **Layer 1 — Data** (matin, 06:00 UTC) — refresh des données + "
    "lecture des missions du jour.\n"
    "2. **Layer 2 — Research / Plan** (en journée) — transformation des "
    "signaux en idées exploitables.\n"
    "3. **Layer 3 — Execution** (en journée) — exécution des choix "
    "validés (publier, envoyer, commiter).\n"
    "4. **Layer 4 — Risk / Quality** (soir, 22:00 UTC) — audit du jour "
    "vs mandat + brief d'amélioration.\n\n"
    "Réponds avec les numéros (`1, 3` / `1 et 3` / `tous`) ou les "
    "moments en clair (`matin et exécution`)."
)


def prompt_substep_b(layer: int, focalisation_md: str) -> str:
    """Render the per-layer focalisation proposal."""
    desc = _LAYER_GENERIC_DESCRIPTION[layer]
    return (
        f"**Étape 3 — Layer {layer} ({desc['name']}).**\n\n"
        f"_Rappel doctrinal :_ {desc['one_liner']}\n\n"
        f"Pour toi, à ce moment-là, voici ce que je vais faire :\n\n"
        f"{focalisation_md}\n\n"
        f"Tu **approuves** cette focalisation pour Layer {layer} ? "
        "(1) Approuve / (2) Édite (texte libre) / (3) Raffine "
        "(nouvelle proposition)"
    )


# ----- Parsing helpers -----

_APPROVE_RE = re.compile(
    r"\b(approuve|approuv[eé]s?|valide|valid[eé]s?|ok|d'?accord|oui|go)\b",
    re.IGNORECASE,
)
_EDIT_RE = re.compile(r"\b(édit|edit|modifie|corrige|réécris)\w*\b", re.IGNORECASE)
_REFINE_RE = re.compile(r"\b(raffin|refine|précise|reformule|reformul)\w*\b", re.IGNORECASE)


def _classify_intent(text: str) -> Optional[Action]:
    """Map operator free text to APPROVE_SUBSTEP / EDIT / REFINE."""
    if _REFINE_RE.search(text):
        return Action.REFINE
    if _EDIT_RE.search(text):
        return Action.EDIT
    if _APPROVE_RE.search(text):
        return Action.APPROVE_SUBSTEP
    return None


# Named moments → layer number, per Notion v5 lines 91-97.
_MOMENT_TO_LAYER: Dict[str, int] = {
    "matin": 1, "data": 1, "data update": 1,
    "recherche": 2, "research": 2, "plan": 2,
    "exécution": 3, "execution": 3, "exec": 3,
    "soir": 4, "débrief": 4, "debrief": 4, "risk": 4, "audit": 4,
}


def _parse_subscribed_layers(text: str) -> List[int]:
    """Parse an operator answer into a sorted list of layer numbers.

    Recognises:
      - "tous" / "tout" / "all"        → [1, 2, 3, 4]
      - digit lists "1, 3" / "1 et 3"  → [1, 3]
      - named moments "matin et exécution" → [1, 3]
      - mixed "1 et exécution"         → [1, 3]
    """
    t = text.strip().lower()
    if not t:
        return []
    if re.search(r"\b(tous|toutes|tout|all)\b", t):
        return [1, 2, 3, 4]
    layers: set = set()
    # Digits
    for m in re.finditer(r"(?<!\d)([1-4])(?!\d)", t):
        layers.add(int(m.group(1)))
    # Named moments
    for name, n in _MOMENT_TO_LAYER.items():
        if name in t:
            layers.add(n)
    return sorted(layers)


def _propose_focalisation(layer: int, dept: Dict[str, Any], variant: int = 0) -> str:
    """Generate 2-3 sentences of focalisation for `layer` based on the dept.

    Variants let the REFINE branch produce a visibly different proposal.
    """
    display = dept.get("display_name") or dept.get("slug", "ce département")
    mandate = dept.get("mandate", "")
    outputs = dept.get("outputs", "")
    desc = _LAYER_GENERIC_DESCRIPTION[layer]

    base_lines: Dict[int, List[List[str]]] = {
        1: [
            [
                f"- Scanner les sources externes + internes pertinentes pour `{display}`.",
                f"- Lire les missions récurrentes dues et les matérialiser dans `queues/research/`.",
                f"- Préparer un `plan.md` du jour avec les options stratégiques.",
            ],
            [
                f"- Faire un refresh ciblé sur les signaux clés du dept (cf. mandate : « {mandate[:60]}… »).",
                f"- Détecter les anomalies par rapport à hier (`outputs/<yesterday>/4/`).",
                f"- Prioriser les queue items selon l'impact attendu.",
            ],
        ],
        2: [
            [
                f"- Consommer chaque queue item de `queues/research/` et produire un draft.",
                f"- Générer 2-3 variantes / angles quand le sujet le mérite.",
                f"- Créer un `queues/gates/<id>.yaml` pour chaque action sensible.",
            ],
            [
                f"- Décomposer chaque queue item en sub-tasks parallélisables.",
                f"- Produire des research notes courtes dans `outputs/<date>/2/research/`.",
                f"- Lever un gate pour toute décision qui touche `{outputs[:60]}…`.",
            ],
        ],
        3: [
            [
                f"- Lire `inbox/decisions/*.yaml` et appliquer les choix validés.",
                f"- Logger chaque action dans `outputs/<date>/3/exec-log.jsonl`.",
                f"- Re-tenter immédiatement après itération si modifs utilisateur.",
            ],
            [
                f"- Exécuter en mode `allow_live=True` uniquement sur actions approuvées.",
                f"- Tracer les effets de bord (`exec-log.jsonl` + commits + envois).",
                f"- Remonter au Layer 4 toute erreur d'exécution.",
            ],
        ],
        4: [
            [
                f"- Auditer les actions du jour vs mandat de `{display}`.",
                f"- Mesurer les KPIs définis à l'étape 5 sur 14/30 jours.",
                f"- Compiler un brief avec les improvements possibles dans `queues/improvements/`.",
            ],
            [
                f"- Comparer ce que j'ai auto-approuvé vs ce que tu as validé/modifié/rejeté.",
                f"- Mesurer l'écart shadow vs prod (`shadow autonomy deltas`).",
                f"- Émettre un `risk-brief.md` + `risk-kpis.yaml` + `management-export.yaml`.",
            ],
        ],
    }
    options = base_lines[layer]
    return "\n".join(options[variant % len(options)])


# ----- I/O helpers (mirror mandate.py / missions.py) -----


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


def _render_prompt_md(
    layer: int,
    dept: Dict[str, Any],
    focalisation_md: str,
) -> str:
    desc = _LAYER_GENERIC_DESCRIPTION[layer]
    display = dept.get("display_name") or dept.get("slug", "?")
    outputs_lines = "\n".join(f"- `{o}`" for o in desc["outputs"])
    return (
        f"# Layer {layer} — {desc['name']} (pour {display})\n\n"
        f"## Mission générique\n\n"
        f"{desc['one_liner']}\n\n"
        f"_Cadence par défaut : {desc['default_cadence']}._\n\n"
        f"## Focalisation pour ce département\n\n"
        f"{focalisation_md}\n\n"
        f"## Outputs\n\n"
        f"{outputs_lines}\n\n"
        f"---\n"
        f"_Composé par `skills/department-onboarding-guide/skill_lib/"
        f"step_runners/layers.py` à l'étape 3 de l'éclosion._\n"
    )


# ----- Runner -----


class LayersRunner(StepRunner):
    """Conversational runner for Step 3 (Mapping des 4 layers)."""

    step_name = STEP_NAME

    def __init__(self) -> None:
        super().__init__()
        self._sub_validated: List[Dict[str, Any]] = []
        self._current_substep: Optional[Dict[str, Any]] = None
        self._current_status: str = "drafting"
        # Substep A.
        self._subscribed_layers: List[int] = []
        # Substep B(N).
        self._pending_layers: List[int] = []
        self._current_layer: Optional[int] = None
        self._current_focalisation: Optional[str] = None
        self._refine_variant: int = 0
        # Phase tracker: "subscribe" / "focus" / "done"
        self._phase: str = "subscribe"
        self._artifacts_written: List[Path] = []

    # ----- lifecycle -----

    def start(self, state_path: Path, dept_yaml_draft_path: Path) -> None:
        self.state_path = Path(state_path)
        self.dept_yaml_draft_path = Path(dept_yaml_draft_path)
        state_doc = _read_yaml(self.state_path)
        progress = (state_doc.get("step_progress") or {}).get(self.step_name) or {}
        self._sub_validated = list(progress.get("sub_artifacts_validated") or [])
        self._current_status = progress.get("current_status", "drafting")
        cs = progress.get("current_substep")
        draft = _read_yaml(self.dept_yaml_draft_path)
        dept = draft.get("department") or {}
        # Restore subscribed layers from dept.yaml.draft if present.
        # v3 stores `layers:` at root; v2 (legacy) nested it under department.
        layers_section = draft.get("layers") or dept.get("layers") or {}
        if isinstance(layers_section, dict):
            sub = layers_section.get("subscribed")
            if isinstance(sub, list) and sub:
                self._subscribed_layers = sorted(int(x) for x in sub)
        if isinstance(cs, dict):
            self._current_substep = dict(cs)
            payload = cs.get("draft_payload") or {}
            if cs.get("type") == SUBSTEP_LAYER_FOCUS:
                self._current_layer = payload.get("layer")
                self._current_focalisation = payload.get("focalisation_md")
                self._refine_variant = int(payload.get("refine_variant", 0))
                self._pending_layers = list(payload.get("pending_layers") or [])
                self._phase = "focus"
        # Compute pending list from subscribed - validated.
        if not self._pending_layers:
            validated_layers = {
                int(e["id"]) for e in self._sub_validated
                if e.get("type") == SUBSTEP_LAYER_FOCUS and str(e.get("id", "")).isdigit()
            }
            self._pending_layers = [n for n in self._subscribed_layers
                                    if n not in validated_layers
                                    and n != self._current_layer]
        if self._subscribed_layers and self._phase == "subscribe":
            self._phase = "focus"
        self._persist_progress()

    # ----- conversational surface -----

    def next_prompt(self) -> Optional[str]:
        if self.is_done():
            return None
        if self._phase == "subscribe" or not self._subscribed_layers:
            return PROMPT_SUBSTEP_A
        # We're in focus phase.
        if self._current_layer is None:
            # Advance to the next pending layer.
            self._advance_to_next_layer()
        if self._current_layer is None:
            # No pending layers left → step done implicitly.
            return None
        assert self._current_focalisation is not None
        return prompt_substep_b(self._current_layer, self._current_focalisation)

    def on_answer(self, operator_text: str) -> Action:
        text = (operator_text or "").strip()
        if not text:
            return Action.CONTINUE

        # Phase: subscribe.
        if self._phase == "subscribe" or not self._subscribed_layers:
            layers = _parse_subscribed_layers(text)
            if not layers:
                return Action.CONTINUE
            self._subscribed_layers = layers
            self._pending_layers = list(layers)
            # Persist into dept.yaml.draft::layers.subscribed.
            self._sync_subscribed_in_draft()
            self._current_substep = {
                "type": SUBSTEP_SUBSCRIBED_LAYERS,
                "draft_payload": {"subscribed": list(layers)},
            }
            self._phase = "focus"
            self._advance_to_next_layer()
            self._persist_progress()
            return Action.CONTINUE

        # Phase: focus.
        if self._current_layer is not None:
            intent = _classify_intent(text)
            if intent == Action.APPROVE_SUBSTEP:
                committed = self._commit_current_layer()
                if not committed:
                    return Action.CONTINUE
                self._advance_to_next_layer()
                if self.is_done():
                    return Action.DONE
                return Action.APPROVE_SUBSTEP
            if intent == Action.EDIT:
                edited = self._apply_inline_edit(text)
                if edited:
                    self._persist_current_substep()
                return Action.EDIT
            if intent == Action.REFINE:
                self._refine_variant += 1
                dept = (_read_yaml(self.dept_yaml_draft_path).get("department") or {})
                self._current_focalisation = _propose_focalisation(
                    self._current_layer, dept, variant=self._refine_variant,
                )
                self._persist_current_substep()
                return Action.REFINE
            return Action.CONTINUE

        return Action.CONTINUE

    def is_done(self) -> bool:
        if not self._subscribed_layers:
            return False
        validated_layers = {
            int(e["id"]) for e in self._sub_validated
            if e.get("type") == SUBSTEP_LAYER_FOCUS and str(e.get("id", "")).isdigit()
        }
        if set(self._subscribed_layers) != validated_layers:
            return False
        # Every subscribed layer must have a PROMPT.md on disk.
        if self.dept_yaml_draft_path is None:
            return False
        dept_root = self.dept_yaml_draft_path.parent
        for n in self._subscribed_layers:
            if not (dept_root / "layers" / str(n) / "PROMPT.md").exists():
                return False
        return True

    def artifacts_produced(self) -> List[Path]:
        return list(self._artifacts_written)

    # ----- internal -----

    def _advance_to_next_layer(self) -> None:
        """Pop the next pending layer and craft a focalisation proposal."""
        self._current_layer = None
        self._current_focalisation = None
        self._refine_variant = 0
        if not self._pending_layers:
            self._current_substep = None
            self._persist_progress()
            return
        n = self._pending_layers.pop(0)
        dept = (_read_yaml(self.dept_yaml_draft_path).get("department") or {})
        self._current_layer = n
        self._current_focalisation = _propose_focalisation(n, dept, variant=0)
        self._persist_current_substep()

    def _persist_current_substep(self) -> None:
        self._current_substep = {
            "type": SUBSTEP_LAYER_FOCUS,
            "draft_payload": {
                "layer": self._current_layer,
                "focalisation_md": self._current_focalisation,
                "pending_layers": list(self._pending_layers),
                "refine_variant": self._refine_variant,
            },
        }
        self._current_status = "awaiting_validation"
        self._persist_progress()

    def _apply_inline_edit(self, text: str) -> bool:
        """Append the operator's edit to the current focalisation.

        Recognises "édite: <text>" and appends the text as a new bullet.
        """
        m = re.search(
            r"(?:édit|edit|modifie|corrige|réécris)\w*\s*[:\-]\s*(.+)$",
            text, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            return False
        addition = m.group(1).strip()
        if len(addition) < 5:
            return False
        assert self._current_focalisation is not None
        self._current_focalisation = (
            self._current_focalisation.rstrip()
            + f"\n- {addition}"
        )
        return True

    def _commit_current_layer(self) -> bool:
        """Write PROMPT.md and test it via test_artifact('layer_focus')."""
        assert self._current_layer is not None
        assert self._current_focalisation is not None
        assert self.dept_yaml_draft_path is not None
        dept_root = self.dept_yaml_draft_path.parent
        dept = (_read_yaml(self.dept_yaml_draft_path).get("department") or {})
        pmd_path = dept_root / "layers" / str(self._current_layer) / "PROMPT.md"
        pmd_path.parent.mkdir(parents=True, exist_ok=True)
        body = _render_prompt_md(self._current_layer, dept, self._current_focalisation)
        pmd_path.write_text(body, encoding="utf-8")
        # Test it.
        result = test_artifact(
            "layer_focus",
            {
                "layer": self._current_layer,
                "focus_md": self._current_focalisation,
                "prompt_md_path": pmd_path,
            },
            {"dept_root": dept_root},
        )
        if not result.passed:
            # Roll back the file to avoid a half-baked artifact on disk.
            pmd_path.unlink(missing_ok=True)
            return False
        if pmd_path not in self._artifacts_written:
            self._artifacts_written.append(pmd_path)
        self._sub_validated.append({
            "id": str(self._current_layer),
            "type": SUBSTEP_LAYER_FOCUS,
            "validated_at": _now_iso(),
        })
        self._sync_subscribed_in_draft()
        return True

    def _sync_subscribed_in_draft(self) -> None:
        """Mirror subscribed layers into dept.yaml.draft.

        v3 schema layout (cf. schemas-draft/examples/dept-ops-maya.yaml):
        `layers: {subscribed: [...]}` is a TOP-LEVEL sibling of
        `department:`, not nested under it.
        """
        assert self.dept_yaml_draft_path is not None
        draft = _read_yaml(self.dept_yaml_draft_path)
        # Ensure department block exists for the mandate step's data.
        draft.setdefault("department", {})
        # Strip any legacy nested copy (back-compat for resumed sessions).
        draft["department"].pop("layers", None)
        layers_section = draft.setdefault("layers", {})
        layers_section["subscribed"] = list(self._subscribed_layers)
        _atomic_write_yaml(self.dept_yaml_draft_path, draft)
        if self.dept_yaml_draft_path not in self._artifacts_written:
            self._artifacts_written.append(self.dept_yaml_draft_path)

    # ----- persistence -----

    def _persist_progress(self) -> None:
        if self.state_path is None:
            return
        doc = _read_yaml(self.state_path)
        progress = doc.setdefault("step_progress", {})
        progress[self.step_name] = {
            "sub_artifacts_validated": list(self._sub_validated),
            "current_substep": self._current_substep,
            "current_status": self._current_status,
            "subscribed_layers": list(self._subscribed_layers),
        }
        doc["last_updated_at"] = _now_iso()
        _atomic_write_yaml(self.state_path, doc)


register_runner(STEP_NAME, LayersRunner)
