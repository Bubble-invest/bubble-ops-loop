"""
step_runners/missions.py — Refonte #2 of 3, Deliverable A.

Conversational runner for Step 2 of the Notion eclosure flow
(lines 830-846). Replaces the single-prompt rhythm-choice question with
a granular per-mission flow that walks the operator through Notion's
5 UX actions:

    + Add mission         (substep B(i) — propose, then approve/edit/refine)
    Disable mission       (free-text command at any time)
    Change cadence        (free-text command at any time)
    Change layer          (free-text command at any time)
    Test mission          (handled by the per-mission artifact tester
                           which simulates a queue item from creates[])

State machine:
  substep A — collect a list of mission topic keywords (or 1-2 sentences)
              from the operator. Persisted in `_pending_topics`.
  substep B(i) — for EACH topic, propose a fully-formed mission card
                  (id / cadence / layer / creates / outputs / description).
                  On APPROVE: test via `test_artifact("recurring_mission",
                  mission_dict, ctx)`. If PASS → write missions/<id>.yaml,
                  append to sub_artifacts_validated, move to next topic.
                  On EDIT: apply operator's inline correction.
                  On REFINE: regenerate a different proposal.
  closing  — after every pending topic is validated, ask "you want more?".
             On "non" → is_done() True. On "oui" → back to substep A.

Persisted to `STATE.yaml::step_progress.missions` in the same shape as
sub-agent #1's MandateRunner uses for `mandate`.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..artifact_tests import test_artifact
from .base import Action, StepRunner, register_runner

# card #688: mission_scaffold.py (scripts/lib/) is the single shared
# emitter that materializes #642's frozen piece-view conventions
# (missions/<id>/PROMPT.md, own-skill/config/memory/voice) for a
# mission dict. scaffold.py's bootstrap path and THIS conversational
# Step-2 "+ add mission" path both call it, so a dept hatched purely
# through Step 2 (no --starter-missions at bootstrap) still gets the
# full cockpit architecture view natively. Path surgery mirrors
# scaffold.py's own sys.path insert for its sibling scripts/lib
# imports — this module may be imported by a caller that never set up
# scripts/lib on sys.path (e.g. the skill's own test conftest only
# inserts SKILL_ROOT), so we add it defensively here rather than
# assume it.
_SCRIPTS_LIB = Path(__file__).resolve().parent.parent.parent.parent.parent / "scripts" / "lib"
if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))
import mission_scaffold  # noqa: E402


# ----- Constants -----

STEP_NAME = "missions"

SUBSTEP_TOPIC_LIST = "missions_topic_list"   # substep A
SUBSTEP_MISSION_DRAFT = "mission_draft"      # substep B(i)


# Sprint correctif Fix 5 (2026-05-21): translate common French cadence
# shorthands to the canonical schema vocabulary BEFORE validating. Keys
# are lowercased; values are the schema-canonical form. The schema
# regex (recurring-mission.schema.yaml line 153) is
#   ^(daily|weekly|hourly|every_\d+h|every_\d+m|cron:.+)$
# so we map every common FR shortcut to one of those.
_CADENCE_FR_TO_CANONICAL: Dict[str, str] = {
    "hebdo": "weekly",
    "hebdomadaire": "weekly",
    "hebdomadaires": "weekly",
    "chaque semaine": "weekly",
    "toutes les semaines": "weekly",
    "quotidien": "daily",
    "quotidienne": "daily",
    "tous les jours": "daily",
    "chaque jour": "daily",
    "journalier": "daily",
    "journaliere": "daily",
    "journalière": "daily",
    "horaire": "hourly",
    "toutes les heures": "hourly",
    "chaque heure": "hourly",
}


def _translate_cadence_fr(value: Any) -> Any:
    """Translate a free-text FR cadence to the schema-canonical form.

    Returns the value unchanged when it isn't a string or doesn't match
    any known FR shortcut. Case-insensitive, whitespace-tolerant.
    """
    if not isinstance(value, str):
        return value
    k = value.strip().lower()
    return _CADENCE_FR_TO_CANONICAL.get(k, k)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- Prompt copy (FR, Bureau-de-Cadre) -----

PROMPT_SUBSTEP_A = (
    "**Étape 2 — Mes missions récurrentes (1/N — les sujets).**\n\n"
    "Quelles activités récurrentes tu attends de moi ? Donne-moi 3-5 "
    "mots-clés (ex : `signal scan, draft posts, audit hebdo`) ou décris en "
    "une ou deux phrases.\n\n"
    "Je te proposerai ensuite une carte par mission, que tu pourras "
    "**approuver**, **éditer** ou me demander de **raffiner**."
)


PROMPT_MORE_MISSIONS = (
    "J'ai validé {n} mission(s). Tu en veux d'autres avant qu'on passe à "
    "l'étape suivante, ou on continue ?\n\n"
    "Réponds `oui` pour ajouter d'autres sujets, ou `non` pour clore "
    "l'étape."
)


def _render_mission_card(mission: Dict[str, Any], idx: int) -> str:
    """Render a mission proposal as a Bureau-de-Cadre card."""
    cadence_str = mission["cadence"]
    if "time" in mission:
        cadence_str = f"{mission['cadence']} ({mission['time']} UTC)"
    if "day" in mission:
        cadence_str = f"{mission['cadence']} — {mission['day']} {mission.get('time', '')}".strip()
    creates_str = ", ".join(mission.get("creates", []))
    return (
        f"**Mission #{idx} — `{mission['id']}`**\n"
        f"- Cadence : {cadence_str}\n"
        f"- Layer : {mission['layer']}\n"
        f"- Crée : `{creates_str}`\n"
        f"- Outputs : `{mission['output_queue']}`\n"
        f"- Notes : {mission['description']}\n\n"
        "Tu **approuves** ? (1) Approuve / (2) Édite (texte libre) / "
        "(3) Raffine (nouvelle proposition)"
    )


# ----- Parsing helpers -----

_APPROVE_RE = re.compile(
    r"\b(approuve|approuv[eé]s?|valide|valid[eé]s?|ok|d'?accord|oui|go)\b",
    re.IGNORECASE,
)
_EDIT_RE = re.compile(r"\b(édit|edit|modifie|corrige|réécris)\w*\b", re.IGNORECASE)
_REFINE_RE = re.compile(r"\b(raffin|refine|précise|reformule|reformul)\w*\b", re.IGNORECASE)
_NO_RE = re.compile(r"\b(non|nope|stop|passe|suivant|fini|c'?est bon)\b", re.IGNORECASE)

# Disable / change commands (the 4 UX verbs from Notion 843-845).
_DISABLE_RE = re.compile(
    r"(?:désactive|disable|retire|supprime|enlève)\s+(?:mission\s+)?([a-z0-9_]+)",
    re.IGNORECASE,
)
_CHANGE_CADENCE_RE = re.compile(
    r"change\s+(?:la\s+)?cadence\s+de\s+([a-z0-9_]+)\s+(?:à|to|en)\s+([a-z0-9_:]+)",
    re.IGNORECASE,
)
_CHANGE_LAYER_RE = re.compile(
    r"change\s+(?:le\s+)?layer\s+de\s+([a-z0-9_]+)\s+(?:à|to|en)\s+(\d+)",
    re.IGNORECASE,
)


def _classify_intent(text: str) -> Optional[Action]:
    """Map operator free text to one of APPROVE_SUBSTEP / EDIT / REFINE."""
    # Test REFINE before EDIT — "édite et raffine" must win REFINE.
    if _REFINE_RE.search(text):
        return Action.REFINE
    if _EDIT_RE.search(text):
        return Action.EDIT
    if _APPROVE_RE.search(text):
        return Action.APPROVE_SUBSTEP
    return None


def _slugify_topic(topic: str) -> str:
    """Turn a free-text topic into a snake_case mission id."""
    s = topic.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    if not s:
        s = "mission"
    if not s[0].isalpha():
        s = "m_" + s
    return s


def _parse_topic_list(text: str) -> List[str]:
    """Split a free-text answer into a list of topic strings.

    Accepts comma-separated keywords ("signal scan, draft posts") OR
    a 1-2 sentence answer ("je veux X et Y le matin").
    """
    text = text.strip()
    if not text:
        return []
    # First, comma split (Notion's 3-5 keyword path).
    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if parts:
            return parts
    # Second path: sentences, split on " et " / " puis " / ".".
    parts = re.split(r"\s+et\s+|\s+puis\s+|\.\s+|;\s+", text)
    parts = [p.strip() for p in parts if p.strip() and len(p.strip()) > 2]
    # Strip stop-word lead-ins like "Je veux que tu" / "je veux".
    cleaned: List[str] = []
    for p in parts:
        p = re.sub(
            r"^(?:je\s+veux\s+que\s+tu\s+|je\s+veux\s+|tu\s+)\s*",
            "", p, flags=re.IGNORECASE,
        )
        if p.strip():
            cleaned.append(p.strip())
    return cleaned or [text]


def _propose_mission(topic: str, refine_variant: int = 0) -> Dict[str, Any]:
    """Generate a fully-formed mission proposal from a topic string.

    `refine_variant=0` is the canonical proposal (daily 06:00 layer 1).
    Higher variants change cadence/layer/wording so the REFINE branch
    can produce a visibly different card on each tick.
    """
    mission_id = _slugify_topic(topic) + "_task" if not _slugify_topic(topic).endswith("_task") else _slugify_topic(topic)
    # Keep the id <= 40 chars and ensure snake_case + leading alpha.
    mission_id = mission_id[:40].rstrip("_")
    # Variant catalogue (cycled).
    variants = [
        {"cadence": "daily", "time": "06:00", "layer": 1,
         "desc_suffix": "scanne et matérialise les besoins du jour."},
        {"cadence": "weekly", "time": "09:00", "day": "monday", "layer": 1,
         "desc_suffix": "récap hebdomadaire de l'activité."},
        {"cadence": "hourly", "active_hours": "08:00-20:00", "layer": 2,
         "desc_suffix": "rafraîchit l'état pendant les heures actives."},
        {"cadence": "every_2h", "active_hours": "08:00-20:00", "layer": 2,
         "desc_suffix": "tick toutes les 2h, en heures actives."},
    ]
    variant = variants[refine_variant % len(variants)]
    creates_kind = mission_id  # snake_case kind matches the id
    if not re.match(r"^[a-z][a-z_]+$", creates_kind):
        creates_kind = "generic_task"
    description = f"Mission « {topic} » — {variant['desc_suffix']}"
    if len(description) < 10:
        description = description + " (auto-générée, à préciser)."
    mission: Dict[str, Any] = {
        "id": mission_id,
        "layer": variant["layer"],
        "cadence": variant["cadence"],
        "description": description,
        "output_queue": "queues/research/",
        "creates": [creates_kind],
    }
    for k in ("time", "day", "active_hours"):
        if k in variant:
            mission[k] = variant[k]
    return mission


def _apply_inline_edit(mission: Dict[str, Any], edit_text: str) -> Dict[str, Any]:
    """Mutate a mission dict from an operator's free-text edit instruction.

    Recognises:
      - "cadence ... weekly" / "passe la cadence à hebdo"
      - "layer N"
      - "active_hours HH:MM-HH:MM"
    """
    out = dict(mission)
    # Cadence
    m = re.search(
        r"cadence[^a-z0-9]+([a-z0-9_:]+(?:\.[a-z0-9_]+)*)",
        edit_text, re.IGNORECASE,
    )
    if m:
        cand = m.group(1).lower()
        # Sprint correctif Fix 5 (2026-05-21): centralised FR translation
        # (mirrors _change_field), so substep B inline edits accept the
        # same shortcuts as the imperative `change cadence` UX command.
        cand = _CADENCE_FR_TO_CANONICAL.get(cand, cand)
        if re.match(r"^(daily|weekly|hourly|every_\d+h|every_\d+m|cron:.+)$", cand):
            out["cadence"] = cand
            # Drop incompatible fields.
            if cand not in ("daily", "weekly") and "time" in out:
                pass  # keep time, harmless
            if cand != "weekly" and "day" in out:
                out.pop("day", None)
    # Layer
    m = re.search(r"layer[^0-9]+([1-4])", edit_text, re.IGNORECASE)
    if m:
        out["layer"] = int(m.group(1))
    return out


# ----- I/O helpers (mirror mandate.py) -----


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


# ----- Runner -----


class MissionsRunner(StepRunner):
    """Conversational runner for Step 2 (Missions récurrentes)."""

    step_name = STEP_NAME

    def __init__(self) -> None:
        super().__init__()
        # State.
        self._sub_validated: List[Dict[str, Any]] = []
        self._current_substep: Optional[Dict[str, Any]] = None
        self._current_status: str = "drafting"
        # Substep A: list of operator-provided topics still waiting to
        # be proposed as missions.
        self._pending_topics: List[str] = []
        # Substep B(i): the mission currently being negotiated.
        self._current_mission: Optional[Dict[str, Any]] = None
        self._current_topic: Optional[str] = None
        self._refine_variant: int = 0
        # Closing phase: True once we've asked "you want more?" and the
        # operator said "non".
        self._operator_closed: bool = False
        # Tracking phase: "topic_list" / "mission_proposal" / "closing"
        self._phase: str = "topic_list"
        # Artifacts written.
        self._artifacts_written: List[Path] = []
        # Sprint correctif Fix 5 (2026-05-21): one-shot Bureau-de-Cadre
        # explanation prepended to next_prompt() when _change_field
        # rejects an unrecognised cadence (or any other validation
        # failure on the imperative UX commands).
        self._last_rejection_reason: Optional[str] = None

    # ----- lifecycle -----

    def start(self, state_path: Path, dept_yaml_draft_path: Path) -> None:
        self.state_path = Path(state_path)
        self.dept_yaml_draft_path = Path(dept_yaml_draft_path)
        state_doc = _read_yaml(self.state_path)
        progress = (state_doc.get("step_progress") or {}).get(self.step_name) or {}
        self._sub_validated = list(progress.get("sub_artifacts_validated") or [])
        self._current_status = progress.get("current_status", "drafting")
        cs = progress.get("current_substep")
        if isinstance(cs, dict):
            self._current_substep = dict(cs)
            payload = cs.get("draft_payload") or {}
            if cs.get("type") == SUBSTEP_TOPIC_LIST:
                self._pending_topics = list(payload.get("pending_topics") or [])
                self._phase = "topic_list" if not self._pending_topics else "mission_proposal"
            elif cs.get("type") == SUBSTEP_MISSION_DRAFT:
                self._pending_topics = list(payload.get("pending_topics") or [])
                self._current_topic = payload.get("topic")
                self._current_mission = payload.get("mission")
                self._refine_variant = int(payload.get("refine_variant", 0))
                self._phase = "mission_proposal"
        else:
            # No active substep, but we may have validated missions from
            # a prior session — advance phase to closing if relevant.
            if self._sub_validated:
                self._phase = "closing"
        # If there is a "closed" marker, restore it.
        if progress.get("operator_closed"):
            self._operator_closed = True
        self._persist_progress()

    # ----- conversational surface -----

    def next_prompt(self) -> Optional[str]:
        if self.is_done():
            return None
        base: Optional[str] = None
        # Substep A.
        if self._phase == "topic_list" and not self._pending_topics and self._current_mission is None:
            base = PROMPT_SUBSTEP_A
        # Substep B(i).
        elif self._current_mission is not None:
            idx = len(self._sub_validated) + 1
            base = _render_mission_card(self._current_mission, idx)
        # If we have pending topics but no current mission, propose the next one.
        elif self._pending_topics:
            self._advance_to_next_topic()
            if self._current_mission is not None:
                idx = len(self._sub_validated) + 1
                base = _render_mission_card(self._current_mission, idx)
        # All topics handled; ask "more?"
        if base is None:
            self._phase = "closing"
            base = PROMPT_MORE_MISSIONS.format(n=len(self._sub_validated))

        # Sprint correctif Fix 5 (2026-05-21): one-shot rejection reason
        # surfaced + cleared so the operator sees WHY a `change cadence`
        # or `change layer` command was ignored.
        if self._last_rejection_reason:
            reason = self._last_rejection_reason
            self._last_rejection_reason = None
            return f"⚠ {reason}\n\n---\n\n{base}"
        return base

    def on_answer(self, operator_text: str) -> Action:
        text = (operator_text or "").strip()
        if not text:
            return Action.CONTINUE

        # Global UX commands (Notion 843-845): work at any time.
        if self._handle_global_commands(text):
            return Action.CONTINUE

        # Phase: topic list collection.
        if self._phase == "topic_list" and self._current_mission is None and not self._pending_topics:
            topics = _parse_topic_list(text)
            if not topics:
                return Action.CONTINUE
            self._pending_topics = topics
            self._current_substep = {
                "type": SUBSTEP_TOPIC_LIST,
                "draft_payload": {"pending_topics": list(topics)},
            }
            self._current_status = "drafting"
            # Immediately advance to mission proposal for topic #1.
            self._advance_to_next_topic()
            self._persist_progress()
            return Action.CONTINUE

        # Phase: mission proposal (substep B).
        if self._current_mission is not None:
            intent = _classify_intent(text)
            if intent == Action.APPROVE_SUBSTEP:
                committed = self._commit_current_mission()
                if not committed:
                    # Test failed — stay on the same mission.
                    return Action.CONTINUE
                # Advance to next topic, or to closing if list empty.
                self._advance_to_next_topic()
                return Action.APPROVE_SUBSTEP
            if intent == Action.EDIT:
                self._current_mission = _apply_inline_edit(self._current_mission, text)
                self._persist_current_substep()
                return Action.EDIT
            if intent == Action.REFINE:
                self._refine_variant += 1
                assert self._current_topic is not None
                self._current_mission = _propose_mission(
                    self._current_topic, refine_variant=self._refine_variant,
                )
                self._persist_current_substep()
                return Action.REFINE
            return Action.CONTINUE

        # Phase: closing ("you want more?")
        if self._phase == "closing":
            if _NO_RE.search(text):
                self._operator_closed = True
                self._current_status = "validated"
                self._persist_progress()
                return Action.DONE
            if _APPROVE_RE.search(text):
                # Operator said "oui" → back to topic-list collection.
                self._phase = "topic_list"
                self._current_substep = None
                self._persist_progress()
                return Action.CONTINUE
            return Action.CONTINUE

        return Action.CONTINUE

    def is_done(self) -> bool:
        return self._operator_closed and len(self._sub_validated) >= 1

    def artifacts_produced(self) -> List[Path]:
        return list(self._artifacts_written)

    # ----- internal -----

    def _advance_to_next_topic(self) -> None:
        """Pop the next pending topic and craft a mission proposal."""
        self._current_mission = None
        self._current_topic = None
        self._refine_variant = 0
        if not self._pending_topics:
            self._current_substep = None
            self._phase = "closing"
            self._persist_progress()
            return
        topic = self._pending_topics.pop(0)
        self._current_topic = topic
        self._current_mission = _propose_mission(topic, refine_variant=0)
        self._phase = "mission_proposal"
        self._persist_current_substep()

    def _persist_current_substep(self) -> None:
        self._current_substep = {
            "type": SUBSTEP_MISSION_DRAFT,
            "draft_payload": {
                "topic": self._current_topic,
                "mission": self._current_mission,
                "pending_topics": list(self._pending_topics),
                "refine_variant": self._refine_variant,
            },
        }
        self._current_status = "awaiting_validation"
        self._persist_progress()

    def _dept_display_name(self, dept_root: Path) -> str:
        """Best-effort display name for the mission_scaffold piece emitter
        (used in rendered comments/headers only, never validated). Prefers
        STATE.yaml::display_name (the source of truth per mandate.py),
        falls back to dept.yaml.draft::department.display_name, else the
        dept root dirname."""
        if self.state_path is not None:
            state_doc = _read_yaml(self.state_path)
            name = state_doc.get("display_name")
            if isinstance(name, str) and name:
                return name
        draft = _read_yaml(self.dept_yaml_draft_path) if self.dept_yaml_draft_path else {}
        name = (draft.get("department") or {}).get("display_name")
        if isinstance(name, str) and name:
            return name
        return dept_root.name

    def _commit_current_mission(self) -> bool:
        """Test the current mission and, on PASS, write it to disk.

        Returns True on commit, False on test failure.
        """
        assert self._current_mission is not None
        assert self.dept_yaml_draft_path is not None
        dept_root = self.dept_yaml_draft_path.parent
        ctx = {
            "dept_root": dept_root,
            "dept_yaml_draft_path": self.dept_yaml_draft_path,
        }
        result = test_artifact("recurring_mission", self._current_mission, ctx)
        if not result.passed:
            # Keep current_mission, do not advance, do not write.
            return False
        # Write the mission YAML.
        mission_path = dept_root / "missions" / f"{self._current_mission['id']}.yaml"
        _atomic_write_yaml(mission_path, dict(self._current_mission))
        if mission_path not in self._artifacts_written:
            self._artifacts_written.append(mission_path)
        # card #688: emit the full #642 piece-view convention set for this
        # mission (missions/<id>/PROMPT.md, own-skill/config/memory/voice)
        # so the cockpit renders it natively — not a retrofit — the SAME
        # emitter scaffold.py's bootstrap --starter-missions path uses.
        display_name = self._dept_display_name(dept_root)
        for written in mission_scaffold.scaffold_mission_pieces(
            dept_root, self._current_mission, dept_root.name, display_name,
        ):
            if written not in self._artifacts_written:
                self._artifacts_written.append(written)
        if str(self._current_mission.get("output_queue") or "").rstrip("/") == "queues/research":
            schema_path = mission_scaffold.scaffold_pool_schema(dept_root, display_name)
            if schema_path is not None and schema_path not in self._artifacts_written:
                self._artifacts_written.append(schema_path)
        # Append to sub_validated.
        self._sub_validated.append({
            "id": self._current_mission["id"],
            "type": SUBSTEP_MISSION_DRAFT,
            "validated_at": _now_iso(),
        })
        # Reflect in dept.yaml.draft::recurring_missions.
        self._sync_recurring_missions_in_draft()
        return True

    def _sync_recurring_missions_in_draft(self) -> None:
        """Mirror the list of validated missions into dept.yaml.draft.

        v3 schema layout (cf. schemas-draft/examples/dept-ops-maya.yaml):
        `recurring_missions:` is a TOP-LEVEL sibling of `department:`,
        not nested under it.
        """
        assert self.dept_yaml_draft_path is not None
        draft = _read_yaml(self.dept_yaml_draft_path)
        # Ensure the department block exists (mandate runner seeds it).
        draft.setdefault("department", {})
        dept_root = self.dept_yaml_draft_path.parent
        missions: List[Dict[str, Any]] = []
        for entry in self._sub_validated:
            path = dept_root / "missions" / f"{entry['id']}.yaml"
            if path.exists():
                missions.append(yaml.safe_load(path.read_text(encoding="utf-8")))
        # Strip any legacy nested copy (back-compat for resumed sessions).
        draft["department"].pop("recurring_missions", None)
        draft["recurring_missions"] = missions
        _atomic_write_yaml(self.dept_yaml_draft_path, draft)
        if self.dept_yaml_draft_path not in self._artifacts_written:
            self._artifacts_written.append(self.dept_yaml_draft_path)

    # ----- UX commands (5 actions from Notion 842-846) -----

    def _handle_global_commands(self, text: str) -> bool:
        """Recognise and execute the 4 imperative UX commands.

        Returns True iff a command was matched and executed.
        """
        m = _DISABLE_RE.search(text)
        if m:
            return self._disable_mission(m.group(1))
        m = _CHANGE_CADENCE_RE.search(text)
        if m:
            return self._change_field(m.group(1), "cadence", m.group(2))
        m = _CHANGE_LAYER_RE.search(text)
        if m:
            try:
                layer = int(m.group(2))
            except ValueError:
                return False
            return self._change_field(m.group(1), "layer", layer)
        return False

    def _disable_mission(self, mission_id: str) -> bool:
        assert self.dept_yaml_draft_path is not None
        dept_root = self.dept_yaml_draft_path.parent
        mission_path = dept_root / "missions" / f"{mission_id}.yaml"
        # Drop from disk if present.
        if mission_path.exists():
            mission_path.unlink()
        # Drop from sub_validated.
        self._sub_validated = [
            e for e in self._sub_validated if e.get("id") != mission_id
        ]
        # Drop from artifacts_written if recorded.
        self._artifacts_written = [
            p for p in self._artifacts_written if p != mission_path
        ]
        self._sync_recurring_missions_in_draft()
        self._persist_progress()
        return True

    def _change_field(self, mission_id: str, field: str, value: Any) -> bool:
        assert self.dept_yaml_draft_path is not None
        dept_root = self.dept_yaml_draft_path.parent
        mission_path = dept_root / "missions" / f"{mission_id}.yaml"
        if not mission_path.exists():
            self._last_rejection_reason = (
                f"Je ne trouve pas de mission `{mission_id}` à éditer. "
                "Vérifie l'id (ils sont en snake_case) ou ajoute-la "
                "d'abord avec `+ add mission`."
            )
            return False
        body = yaml.safe_load(mission_path.read_text(encoding="utf-8")) or {}
        original_value = value
        # Sprint correctif Fix 5 (2026-05-21): translate FR shorthand for
        # cadence before validating.
        if field == "cadence":
            value = _translate_cadence_fr(value)
        body[field] = value
        # Cadence transition guards (the schema enforces conditional
        # requirements; we materialise sensible defaults so a one-shot
        # "change cadence" command from Telegram doesn't fail validation).
        if field == "cadence":
            if value == "weekly" and "day" not in body:
                body["day"] = "monday"
            if value == "weekly" and "time" not in body:
                body["time"] = "09:00"
            if value == "daily" and "time" not in body:
                body["time"] = "06:00"
            # Sub-daily cadences don't need `time` / `day`; drop them
            # if they were set so we don't mislead the consumer.
            if value not in ("daily", "weekly"):
                body.pop("day", None)
        # Re-validate before writing. Tell the tester this is an
        # in-place update so the duplicate-id guard doesn't fire on the
        # mission we're editing.
        ctx = {
            "dept_root": dept_root,
            "dept_yaml_draft_path": self.dept_yaml_draft_path,
            "updating_mission_id": mission_id,
        }
        result = test_artifact("recurring_mission", body, ctx)
        if not result.passed:
            # Sprint correctif Fix 5: surface a Bureau-de-Cadre French
            # rejection reason so the operator sees WHY their command
            # was ignored (previously a silent false).
            if field == "cadence":
                self._last_rejection_reason = (
                    f"Cadence `{original_value}` non reconnue. Les "
                    "cadences acceptées sont : `daily` (chaque jour, "
                    "alias `quotidien`), `weekly` (chaque semaine, alias "
                    "`hebdo`), `hourly` (chaque heure, alias `horaire`), "
                    "`every_<N>h` (ex. `every_2h`), `every_<N>m` (ex. "
                    "`every_30m`), ou `cron:<expr>` (échappatoire avancée)."
                )
            else:
                first_issue = (result.issues or ["validation refusée"])[0]
                self._last_rejection_reason = (
                    f"Édition refusée sur `{field}` = `{original_value}` : "
                    f"{first_issue}"
                )
            return False
        _atomic_write_yaml(mission_path, body)
        self._sync_recurring_missions_in_draft()
        self._persist_progress()
        return True

    # ----- persistence -----

    def _persist_progress(self) -> None:
        if self.state_path is None:
            return
        doc = _read_yaml(self.state_path)
        progress = doc.setdefault("step_progress", {})
        entry = {
            "sub_artifacts_validated": list(self._sub_validated),
            "current_substep": self._current_substep,
            "current_status": self._current_status,
        }
        if self._operator_closed:
            entry["operator_closed"] = True
        progress[self.step_name] = entry
        doc["last_updated_at"] = _now_iso()
        _atomic_write_yaml(self.state_path, doc)


register_runner(STEP_NAME, MissionsRunner)
