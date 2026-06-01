"""
step_runners/mandate.py — Refonte #1 of 3, Deliverable C.

Conversational runner for Step 1 of the Notion eclosure flow
(lines 803-829). Replaces the single-turn `get_step_prompt('mandate')`
formulation with a 2-substep guided dialogue that captures the 7
canonical Notion v5 lines 813-825 department fields:

    1. rôle du département / phrase    (substep A — style choice + sentence)
    2. interdits                       (substep B, line 1 = `forbidden` list)
    3. niveau                          (substep B, line 2 = `level` enum)
    4. utilisateur / owner             (substep B, line 3 = `owner`)
    + slug / display_name / status     (seeded from STATE.yaml at substep A)

After both substeps are validated, the runner writes:
  - MANDATE.md                 — the long-form mandate doc (outputs +
                                 success_criteria narrative live here)
  - dept.yaml.draft            — `department:` block with EXACTLY these 7
                                 fields: slug / display_name / level /
                                 status / mandate / owner / forbidden
                                 (cf. dept.schema.yaml lines 32-97).

Sprint correctif Fix 2 (2026-05-21) — dropped `outputs` and
`success_criteria` from the department block (they violated the schema's
`additionalProperties: false` on the department wrapper); replaced with
the required `level` field (schema lines 49-56).

The dialogue uses the existing Approve / Edit / Refine triplet helpers
from `skill_lib.auto_drive` so the audit-trail format stays uniform.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .base import Action, StepRunner, register_runner


# ----- Constants -----

STEP_NAME = "mandate"

#: The two substeps the runner walks the operator through.
SUBSTEP_STYLE_AND_SENTENCE = "mandate_sentence"   # substep A
SUBSTEP_CLARIFICATIONS = "clarifications"         # substep B

#: The 3 fields substep B captures from the operator's structured reply.
#: Notion v5 lines 813-825 — forbidden + level + owner (the other 4 fields
#: slug / display_name / status / mandate are seeded earlier or by substep A).
CLARIFICATION_FIELDS = ("forbidden", "level", "owner")

#: Recognised level values (schema dept.schema.yaml lines 49-56).
_VALID_LEVELS = {"ops", "management", "principal"}

#: Common French / English aliases for levels (loose parsing).
_LEVEL_ALIASES = {
    "ops": "ops",
    "operation": "ops",
    "opérationnel": "ops",
    "operationnel": "ops",
    "leaf": "ops",
    "management": "management",
    "gestion": "management",
    "aggregator": "management",
    "agregateur": "management",
    "agrégateur": "management",
    "principal": "principal",
    "root": "principal",
    "racine": "principal",
}

#: Style → seed mandate sentence (Bureau-de-Cadre tone). Operator may
#: edit; this is only the agent's first proposal.
_STYLE_TO_SEED_SENTENCE: Dict[str, str] = {
    "1": "Je m'occupe d'une seule chose, très bien — à toi de me dire laquelle.",
    "2": "Je couvre 2 ou 3 sujets liés, sur un rythme régulier.",
    "3": "Je joue un rôle transversal pour l'équipe et je connecte plusieurs flux.",
}

_STYLE_LABEL: Dict[str, str] = {
    "1": "resserré",
    "2": "équilibré",
    "3": "large",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- Prompt copy (FR, Bureau-de-Cadre) -----

PROMPT_SUBSTEP_A = (
    "**Étape 1 — Mon mandat (1/2 — la phrase).**\n\n"
    "Avant qu'on aille plus loin, j'ai besoin qu'on s'accorde sur la phrase "
    "qui me définit. Voici 3 styles possibles :\n\n"
    "1. *Mandat resserré* — je fais une seule chose, très bien.\n"
    "2. *Mandat équilibré* — je couvre 2 ou 3 sujets liés.\n"
    "3. *Mandat large* — je joue un rôle transversal pour l'équipe.\n\n"
    "Tu choisis 1, 2 ou 3 ? Je te propose ensuite une formulation que tu "
    "pourras **approuver**, **éditer** ou me demander de **raffiner**."
)


def prompt_substep_a_followup(style_choice: str, seed_sentence: str) -> str:
    """Return the 2nd-turn prompt for substep A after the operator picks a style."""
    label = _STYLE_LABEL.get(style_choice, "personnalisé")
    return (
        f"Très bien, mandat **{label}**. Voici ma proposition de phrase :\n\n"
        f"> *« {seed_sentence} »*\n\n"
        "Tu **approuves** cette phrase, tu veux l'**éditer** "
        "(envoie-moi ta version), ou tu préfères que je te propose une "
        "formulation plus précise (**raffiner**) ?"
    )


PROMPT_SUBSTEP_B = (
    "**Étape 1 — Mon mandat (2/2 — les 3 dernières précisions).**\n\n"
    "On a la phrase. Avant que je grave tout dans `dept.yaml`, j'ai besoin "
    "de 3 précisions pour respecter le format canonique (cf. Notion v5 "
    "lignes 813-825). Réponds en **3 lignes** dans cet ordre :\n\n"
    "1. **interdits** — qu'est-ce que je ne dois jamais faire ? "
    "(liste, séparateurs `,`)\n"
    "2. **niveau** — `ops` (département feuille : Ben/Maya/Miranda/Eliot), "
    "`management` (agrégateur : Tony), ou `principal` (racine : "
    "{{OPERATOR}}/{{OPERATOR_2}}) ?\n"
    "3. **owner** — je confirme que c'est bien `joris` qui m'arbitre, "
    "ou tu donnes un autre slug ?\n\n"
    "Note : la narration longue (ce que je dois produire, critères de "
    "réussite) vit dans `MANDATE.md`, pas dans `dept.yaml` — qui ne "
    "porte que l'identité canonique.\n\n"
    "Exemple :\n"
    "```\n"
    "publier sans validation, nommer clients, conseil financier\n"
    "ops\n"
    "joris\n"
    "```"
)


PROMPT_SUBSTEP_B_FOLLOWUP_TEMPLATE = (
    "Très bien. Voici ce que j'ai retenu :\n\n"
    "- **interdits** : {forbidden}\n"
    "- **niveau** : `{level}`\n"
    "- **owner** : `{owner}`\n\n"
    "Tu **approuves** tel quel, tu veux **éditer** (renvoie les 3 lignes "
    "corrigées), ou tu préfères que je **raffine** une de ces lignes ?"
)


# ----- Parsing helpers -----

_APPROVE_RE = re.compile(r"\b(approuve|approuvé|valide|ok|d'?accord|oui|go)\b", re.IGNORECASE)
_EDIT_RE = re.compile(r"\b(édit|edit|modifie|corrige|réécris)\w*\b", re.IGNORECASE)
_REFINE_RE = re.compile(r"\b(raffin|refine|précise|reformule|reformul)\w*\b", re.IGNORECASE)


def _classify_intent(text: str) -> Optional[Action]:
    """Map operator free text to one of APPROVE_SUBSTEP / EDIT / REFINE.

    Returns None when the text doesn't match any of the 3 — caller
    should treat as CONTINUE and re-prompt.
    """
    if _APPROVE_RE.search(text):
        return Action.APPROVE_SUBSTEP
    if _REFINE_RE.search(text):
        # Test "raffin" before "edit" because "édit" could appear in
        # phrasings like "édite et raffine" — we want the more specific
        # refine intent to win.
        return Action.REFINE
    if _EDIT_RE.search(text):
        return Action.EDIT
    return None


def _parse_style_choice(text: str) -> Optional[str]:
    """Return '1', '2', or '3' if found in the text, else None."""
    s = text.strip()
    # Standalone digit
    for c in ("1", "2", "3"):
        if re.search(rf"(?<!\d){c}(?!\d)", s):
            return c
    return None


def _normalise_level(raw: str) -> Optional[str]:
    """Map a free-text level answer to one of {ops, management, principal}."""
    k = raw.strip().lower()
    if not k:
        return None
    if k in _VALID_LEVELS:
        return k
    if k in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[k]
    # Match the first valid level token found in the string (handles
    # answers like "je suis ops" or "ops dept").
    for token in re.findall(r"[a-zéè]+", k):
        if token in _VALID_LEVELS:
            return token
        if token in _LEVEL_ALIASES:
            return _LEVEL_ALIASES[token]
    return None


def _parse_clarifications_block(text: str) -> Optional[Dict[str, Any]]:
    """Parse the 3-line structured reply into a dict.

    Returns None if the reply doesn't have at least 3 non-empty lines OR
    if the `level` line can't be mapped to one of the 3 official values.
    The dict has keys forbidden / level / owner.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Strip optional leading bullet/numbering tokens.
    cleaned: List[str] = []
    for ln in lines:
        ln = re.sub(r"^\s*[\-\*•]\s*", "", ln)
        ln = re.sub(r"^\s*\d+\s*[\.\)]\s*", "", ln)
        # Strip bold markdown like "**interdits** : ..."
        ln = re.sub(r"^\*\*[^*]+\*\*\s*[:\-]?\s*", "", ln)
        # Strip plain "interdits:" / "niveau:" / "owner:" prefixes.
        ln = re.sub(
            r"^(interdits|niveau|level|owner)\s*[:\-]\s*",
            "", ln, flags=re.IGNORECASE,
        )
        cleaned.append(ln.strip())
    cleaned = [ln for ln in cleaned if ln]
    if len(cleaned) < 3:
        return None
    forb, level_raw, owner = cleaned[:3]
    level = _normalise_level(level_raw)
    if level is None:
        return None
    return {
        "forbidden": [x.strip() for x in forb.split(",") if x.strip()],
        "level": level,
        "owner": owner.strip(),
    }


# ----- Runner -----


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


class MandateRunner(StepRunner):
    """Conversational runner for Step 1 (Mandate).

    State machine:
      substep A — choose mandate style, then approve/edit/refine the sentence
                  -> writes department.mandate (string)
      substep B — provide outputs / forbidden / success_criteria / owner
                  -> writes department.{outputs, forbidden, success_criteria, owner}

    When both substeps are approved, the runner also writes MANDATE.md.
    """

    step_name = STEP_NAME

    def __init__(self) -> None:
        super().__init__()
        # In-memory state, mirrored in STATE.yaml::step_progress.mandate.
        self._sub_validated: List[Dict[str, Any]] = []
        self._current_substep: Optional[Dict[str, Any]] = None
        self._current_status: str = "drafting"
        # Tracks intra-substep cursor for substep A:
        #   "style"     - waiting on operator to pick 1/2/3
        #   "sentence"  - waiting on operator to approve/edit/refine
        #   "done"      - substep A finished
        self._substep_a_phase: str = "style"
        self._chosen_style: Optional[str] = None
        self._chosen_sentence: Optional[str] = None
        self._clarifications_payload: Optional[Dict[str, Any]] = None
        self._artifacts_written: List[Path] = []

    # ----- lifecycle -----

    def start(self, state_path: Path, dept_yaml_draft_path: Path) -> None:
        self.state_path = Path(state_path)
        self.dept_yaml_draft_path = Path(dept_yaml_draft_path)
        # Idempotent resume: pull any existing progress from STATE.yaml.
        state_doc = _read_yaml(self.state_path)
        progress = (state_doc.get("step_progress") or {}).get(self.step_name) or {}
        sub_validated = progress.get("sub_artifacts_validated") or []
        self._sub_validated = list(sub_validated)
        self._current_status = progress.get("current_status", "drafting")
        cs = progress.get("current_substep")
        if isinstance(cs, dict):
            self._current_substep = dict(cs)
            # Restore intra-A cursor when a partial draft is captured.
            if cs.get("type") == SUBSTEP_STYLE_AND_SENTENCE:
                payload = cs.get("draft_payload") or {}
                self._chosen_style = payload.get("style")
                self._chosen_sentence = payload.get("sentence")
                if self._chosen_sentence:
                    self._substep_a_phase = "sentence"
                else:
                    self._substep_a_phase = "style"
        # Reflect already-validated substeps in the cursor.
        for entry in self._sub_validated:
            etype = entry.get("type")
            if etype == SUBSTEP_STYLE_AND_SENTENCE:
                self._substep_a_phase = "done"
                # Pull the stored sentence + style from dept.yaml.draft if present.
                draft = _read_yaml(self.dept_yaml_draft_path)
                dept = draft.get("department") or {}
                self._chosen_sentence = dept.get("mandate") or self._chosen_sentence
        # Persist the (possibly unchanged) progress entry so STATE.yaml is
        # never missing a step_progress block for an active step.
        self._persist_progress()

    # ----- conversational surface -----

    def next_prompt(self) -> Optional[str]:
        if self.is_done():
            return None
        # Substep A first.
        if self._substep_a_phase != "done":
            if self._substep_a_phase == "style":
                return PROMPT_SUBSTEP_A
            # phase == "sentence"
            assert self._chosen_style is not None
            assert self._chosen_sentence is not None
            return prompt_substep_a_followup(self._chosen_style, self._chosen_sentence)
        # Substep B.
        if self._clarifications_payload is None:
            return PROMPT_SUBSTEP_B
        return PROMPT_SUBSTEP_B_FOLLOWUP_TEMPLATE.format(
            forbidden=", ".join(self._clarifications_payload["forbidden"]) or "_(vide)_",
            level=self._clarifications_payload["level"],
            owner=self._clarifications_payload["owner"],
        )

    def on_answer(self, operator_text: str) -> Action:
        text = (operator_text or "").strip()
        if not text:
            return Action.CONTINUE

        # ---- Substep A: style choice ----
        if self._substep_a_phase == "style":
            choice = _parse_style_choice(text)
            if choice is None:
                return Action.CONTINUE
            self._chosen_style = choice
            self._chosen_sentence = _STYLE_TO_SEED_SENTENCE[choice]
            self._substep_a_phase = "sentence"
            self._current_substep = {
                "type": SUBSTEP_STYLE_AND_SENTENCE,
                "draft_payload": {
                    "style": choice,
                    "sentence": self._chosen_sentence,
                },
            }
            self._current_status = "awaiting_validation"
            self._persist_progress()
            return Action.CONTINUE

        # ---- Substep A: sentence approve / edit / refine ----
        if self._substep_a_phase == "sentence":
            intent = _classify_intent(text)
            if intent == Action.APPROVE_SUBSTEP:
                # Commit substep A.
                self._validate_substep_a()
                return Action.APPROVE_SUBSTEP
            if intent == Action.EDIT:
                # The edit may be the same message (operator wrote
                # "édite: ma version" in one shot). Try to extract the
                # new sentence; if absent, just re-prompt later.
                new_sentence = self._extract_edit_text(text)
                if new_sentence:
                    self._chosen_sentence = new_sentence
                    assert self._chosen_substep_payload() is not None
                    self._current_substep = {
                        "type": SUBSTEP_STYLE_AND_SENTENCE,
                        "draft_payload": {
                            "style": self._chosen_style,
                            "sentence": new_sentence,
                        },
                    }
                    self._persist_progress()
                return Action.EDIT
            if intent == Action.REFINE:
                # Bump the sentence to a marginally-more-precise variant.
                # (The full refine is up to the agent's narrative layer; we
                # only record that the operator asked for one.)
                self._chosen_sentence = self._refine_sentence(self._chosen_sentence or "")
                self._current_substep = {
                    "type": SUBSTEP_STYLE_AND_SENTENCE,
                    "draft_payload": {
                        "style": self._chosen_style,
                        "sentence": self._chosen_sentence,
                    },
                }
                self._persist_progress()
                return Action.REFINE
            # Garbled intent — re-prompt.
            return Action.CONTINUE

        # ---- Substep B: clarifications draft ----
        # Phase: either waiting on a fresh 4-line block, or
        # waiting on approve/edit/refine of the parsed dict.
        if self._clarifications_payload is None:
            payload = _parse_clarifications_block(text)
            if payload is None:
                return Action.CONTINUE
            self._clarifications_payload = payload
            self._current_substep = {
                "type": SUBSTEP_CLARIFICATIONS,
                "draft_payload": payload,
            }
            self._current_status = "awaiting_validation"
            self._persist_progress()
            return Action.CONTINUE

        # Confirmation phase for substep B.
        intent = _classify_intent(text)
        if intent == Action.APPROVE_SUBSTEP:
            self._validate_substep_b()
            return Action.DONE if self.is_done() else Action.APPROVE_SUBSTEP
        if intent == Action.EDIT:
            # Try to re-parse a fresh 4-line block from the same answer.
            new_payload = _parse_clarifications_block(text)
            if new_payload is not None:
                self._clarifications_payload = new_payload
                self._current_substep = {
                    "type": SUBSTEP_CLARIFICATIONS,
                    "draft_payload": new_payload,
                }
                self._persist_progress()
            return Action.EDIT
        if intent == Action.REFINE:
            # Keep the parsed payload; the agent will narrate a refine
            # ask in the next turn.
            return Action.REFINE
        return Action.CONTINUE

    def is_done(self) -> bool:
        if len(self._sub_validated) < 2:
            return False
        types_done = {e.get("type") for e in self._sub_validated}
        if not {SUBSTEP_STYLE_AND_SENTENCE, SUBSTEP_CLARIFICATIONS}.issubset(types_done):
            return False
        # All 7 canonical Notion v5 813-825 fields must be present.
        if self.dept_yaml_draft_path is None:
            return False
        draft = _read_yaml(self.dept_yaml_draft_path)
        dept = draft.get("department") or {}
        for f in ("slug", "display_name", "level", "status",
                  "mandate", "owner", "forbidden"):
            if f not in dept:
                return False
        # MANDATE.md exists too.
        mandate_md = self.dept_yaml_draft_path.parent / "MANDATE.md"
        return mandate_md.exists()

    def artifacts_produced(self) -> List[Path]:
        return list(self._artifacts_written)

    # ----- internal helpers -----

    def _chosen_substep_payload(self) -> Optional[Dict[str, Any]]:
        return self._current_substep

    @staticmethod
    def _extract_edit_text(text: str) -> Optional[str]:
        """If the operator wrote `édite: <new sentence>` in one go,
        pull the new sentence out. Returns None when no inline edit."""
        m = re.search(r"(?:édit|edit|modifie|corrige|réécris)\w*\s*[:\-]\s*(.+)$",
                      text, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        candidate = m.group(1).strip().strip('"').strip("'")
        # Require non-trivial length to avoid eating filler.
        if len(candidate) < 5:
            return None
        return candidate

    @staticmethod
    def _refine_sentence(seed: str) -> str:
        """Marginal refinement — adds a clarification clause if none exists."""
        if "—" in seed or "(" in seed:
            return seed  # already qualified
        return seed.rstrip(".") + " — à préciser ensemble."

    def _validate_substep_a(self) -> None:
        """Commit substep A: write department.mandate to dept.yaml.draft
        and append an entry to sub_artifacts_validated."""
        assert self._chosen_sentence is not None
        assert self.dept_yaml_draft_path is not None
        draft = _read_yaml(self.dept_yaml_draft_path)
        dept = draft.setdefault("department", {})
        dept["mandate"] = self._chosen_sentence
        # Seed slug + display_name if absent (STATE.yaml is the source
        # of truth — copy them across).
        state_doc = _read_yaml(self.state_path)
        if "slug" in state_doc and "slug" not in dept:
            dept["slug"] = state_doc["slug"]
        if "display_name" in state_doc and "display_name" not in dept:
            dept["display_name"] = state_doc["display_name"]
        dept.setdefault("status", "onboarding")
        _atomic_write_yaml(self.dept_yaml_draft_path, draft)
        if self.dept_yaml_draft_path not in self._artifacts_written:
            self._artifacts_written.append(self.dept_yaml_draft_path)
        self._sub_validated.append({
            "id": f"mandate_sentence_style_{self._chosen_style}",
            "type": SUBSTEP_STYLE_AND_SENTENCE,
            "validated_at": _now_iso(),
        })
        self._substep_a_phase = "done"
        self._current_substep = None
        self._current_status = "drafting"
        self._persist_progress()

    def _validate_substep_b(self) -> None:
        """Commit substep B: write the 3 clarifications + MANDATE.md.

        Sprint correctif Fix 2 (2026-05-21): also strips any legacy
        `outputs` / `success_criteria` fields from the department block
        (back-compat for sessions resumed from the old runner).
        """
        assert self._clarifications_payload is not None
        assert self.dept_yaml_draft_path is not None
        draft = _read_yaml(self.dept_yaml_draft_path)
        dept = draft.setdefault("department", {})
        for f in CLARIFICATION_FIELDS:
            dept[f] = self._clarifications_payload[f]
        # Strip legacy fields the schema now rejects.
        for legacy in ("outputs", "success_criteria"):
            dept.pop(legacy, None)
        _atomic_write_yaml(self.dept_yaml_draft_path, draft)
        if self.dept_yaml_draft_path not in self._artifacts_written:
            self._artifacts_written.append(self.dept_yaml_draft_path)
        # Write MANDATE.md.
        mandate_md = self.dept_yaml_draft_path.parent / "MANDATE.md"
        mandate_md.write_text(self._render_mandate_md(dept), encoding="utf-8")
        if mandate_md not in self._artifacts_written:
            self._artifacts_written.append(mandate_md)
        self._sub_validated.append({
            "id": "clarifications_block",
            "type": SUBSTEP_CLARIFICATIONS,
            "validated_at": _now_iso(),
        })
        self._current_substep = None
        self._current_status = "validated"
        self._persist_progress()

    @staticmethod
    def _render_mandate_md(dept: Dict[str, Any]) -> str:
        slug = dept.get("slug", "?")
        display = dept.get("display_name", slug.capitalize() if slug != "?" else "?")
        sentence = dept.get("mandate", "")
        owner = dept.get("owner", "joris")
        level = dept.get("level", "ops")
        forbidden = dept.get("forbidden") or []
        forb_lines = "\n".join(f"- {x}" for x in forbidden) if forbidden else "_(rien d'interdit déclaré)_"
        return (
            f"# Mandat de {display}\n\n"
            f"_Slug : `{slug}` — Niveau : `{level}` — Owner : `{owner}` — "
            f"Statut : `onboarding`_\n\n"
            f"## La phrase\n\n"
            f"> *« {sentence} »*\n\n"
            f"## Ce que je dois produire\n\n"
            f"_(à détailler ici — c'est la narration longue, "
            f"complémentaire au `dept.yaml`)_\n\n"
            f"## Ce que je ne dois jamais faire\n\n"
            f"{forb_lines}\n\n"
            f"## Comment on saura que j'ai bien fait mon job\n\n"
            f"_(critères de succès à préciser ici — pas dans `dept.yaml`)_\n\n"
            f"---\n"
            f"_Composé par `skills/department-onboarding-guide/skill_lib/"
            f"step_runners/mandate.py` à l'étape 1 de l'éclosion._\n"
        )

    def _persist_progress(self) -> None:
        """Mirror in-memory state to STATE.yaml::step_progress.mandate."""
        if self.state_path is None:
            return
        doc = _read_yaml(self.state_path)
        progress = doc.setdefault("step_progress", {})
        progress[self.step_name] = {
            "sub_artifacts_validated": list(self._sub_validated),
            "current_substep": self._current_substep,
            "current_status": self._current_status,
        }
        doc["last_updated_at"] = _now_iso()
        _atomic_write_yaml(self.state_path, doc)


register_runner(STEP_NAME, MandateRunner)
