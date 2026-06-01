"""
step_runners/gates_kpis.py — Refonte #3 of 3, Deliverable B.

Conversational runner for Step 5 of the Notion eclosure flow
(lines 894-924). Replaces the single-prompt question with a granular
per-action-class flow:

  Phase 1 — Action class detection (substep A):
    Read dept.yaml.draft::skills (committed at step 4) and infer the
    action classes from skills' outputs. Surface the list to operator.

  Phase 2 — Per-class policy proposal (substep B(i)):
    For EACH detected class, propose a structured policy with the 4
    mandatory Notion blocks:
      - current_mode (always `manual_required` in v1 — doctrine guard)
      - eligible_future_modes (subset of the 5 official modes)
      - authorization_bands (≥1 band with allowed_* + forbidden[])
      - kpi_guardrails (≥1 KPI)
    Operator approves / edits / refines. On approve, test via
    test_artifact("gate_policy", policy_dict, ctx) and commit
    dept.yaml.draft::gate_policies.<class> + policies/<class>.yaml.

CRITICAL doctrine guards:
  - `current_mode` is HARDCODED to `manual_required` in every proposal.
    Any EDIT that tries to flip it is rejected with a clear FR
    explanation.
  - `eligible_future_modes` MUST come from the 5 OFFICIAL modes per
    Notion v5 lines 256-260. The deprecated shorthand vocabulary
    (eliminated in PR #4) is doctrinally invalid — the observation
    phase is NOT a 6th mode, it's a phase that runs ACROSS modes
    (Notion lines 421-436).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

from ..artifact_tests import test_artifact
from ..gates import ALL_AUTONOMY_MODES
from .base import Action, StepRunner, register_runner


# ----- Constants -----

STEP_NAME = "gates_kpis"

SUBSTEP_ACTION_CLASSES_LIST = "action_classes_list"   # substep A
SUBSTEP_POLICY_DRAFT = "policy_draft"                 # substep B

# v1 doctrine: every gate starts manual_required.
_REQUIRED_CURRENT_MODE = "manual_required"

# The 5 official modes (canonical source: ALL_AUTONOMY_MODES).
_OFFICIAL_MODES_SET: Set[str] = set(ALL_AUTONOMY_MODES)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----- Skill output → action class inference -----
#
# Heuristic mapping. If a skill's `outputs[]` includes a value matching
# any of these patterns, we infer the corresponding action class.
# Mapping is intentionally narrow — when in doubt we surface a generic
# class so the operator can rename it.

_OUTPUT_TO_ACTION_CLASS: List[tuple] = [
    # (regex on output kind, action class id)
    (r"^(?:draft_post|content_idea_task|post_draft|published_post)$",
     "social_post"),
    (r"^(?:prospect_dm|dm_message|outbound_message)$", "prospect_dm"),
    (r"^(?:trade_order|order_intent|broker_intent)$", "trade_order"),
    (r"^(?:directive_pr|management_directive)$", "directive_pr"),
    (r"^scheduled_post_event$", "social_post"),
    (r"^external_api$", "social_post"),  # publisher emitting to API ⇒ publish
]

# Fallback name-based heuristic for when SKILL.md isn't on disk yet
# (operator may be coming back to step 5 from a partially-stubbed
# step 4). Maps skill name patterns to inferred action classes.
_SKILL_NAME_TO_ACTION_CLASS: List[tuple] = [
    (re.compile(r"(?:post|content).*(?:publish|publisher|scheduler|sender)", re.I),
     "social_post"),
    (re.compile(r"(?:dm|message).*(?:send|sender|drafter|publisher)", re.I),
     "prospect_dm"),
    (re.compile(r"(?:trade|order).*(?:send|sender|executor|publisher)", re.I),
     "trade_order"),
    (re.compile(r"(?:directive).*(?:pr|publisher|writer)", re.I),
     "directive_pr"),
]

# Per-class proposal templates. Keys are inferred class names; each
# value is the prototype passed to the operator.

_POLICY_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "social_post": {
        "eligible_future_modes": [
            "auto_with_veto_window",
            "auto_if_policy_passed",
        ],
        "authorization_bands": {
            "low_risk_evergreen": {
                "allowed_post_types": [
                    "educational",
                    "evergreen",
                    "repost_with_comment",
                ],
                "forbidden": [
                    "client_names",
                    "financial_advice",
                    "controversial_topics",
                ],
            },
        },
        "kpi_guardrails": {
            "brand_safety_breaches": 0,
            "human_edit_rate_30d": "<= 20%",
            "negative_feedback_rate": "<= 1%",
            "quality_score_30d": ">= 0.8",
        },
    },
    "prospect_dm": {
        "eligible_future_modes": [
            "auto_with_veto_window",
            "auto_if_policy_passed",
        ],
        "authorization_bands": {
            "warm_prospect_band": {
                "allowed_recipients": ["warm", "inbound", "previously_contacted"],
                "forbidden": ["cold_prospect", "spam_risk_account"],
            },
        },
        "kpi_guardrails": {
            "reply_rate_30d": ">= 5%",
            "negative_reply_rate_30d": "<= 2%",
            "human_edit_rate_30d": "<= 25%",
            "tone_breaches": 0,
        },
    },
    "trade_order": {
        "eligible_future_modes": [
            "manual_unless_policy_passed",
            "auto_if_policy_passed",
        ],
        "authorization_bands": {
            "low_risk_existing_line": {
                "allowed_order_types": ["limit", "market_small"],
                "forbidden": ["new_ticker", "leverage_above_2x"],
            },
        },
        "kpi_guardrails": {
            "execution_error_rate": "<= 0.1%",
            "slippage_bps": "<= 5",
            "reconciliation_breaks": 0,
        },
    },
    "directive_pr": {
        "eligible_future_modes": ["manual_unless_policy_passed"],
        "authorization_bands": {
            "low_risk_directive": {
                "allowed_directive_types": ["priority_change", "queue_reorder"],
                "forbidden": ["mandate_change", "capital_allocation"],
            },
        },
        "kpi_guardrails": {
            "directive_acceptance_rate_30d": ">= 80%",
            "unresolved_cross_dept_conflicts": 0,
        },
    },
}


def _generic_policy_template(class_id: str) -> Dict[str, Any]:
    """Fallback template for an action class not in _POLICY_TEMPLATES."""
    return {
        "eligible_future_modes": ["auto_with_veto_window"],
        "authorization_bands": {
            f"{class_id}_low_risk_band": {
                "allowed_types": ["low_risk_default"],
                "forbidden": ["high_risk_default"],
            },
        },
        "kpi_guardrails": {
            f"{class_id}_quality_score_30d": ">= 0.8",
            "human_intervention_rate_30d": "<= 20%",
        },
    }


def _detect_action_classes(dept: Dict[str, Any]) -> List[str]:
    """Infer action classes from dept.yaml.draft::skills.

    For each skill's `outputs[]`, look up _OUTPUT_TO_ACTION_CLASS and
    accumulate unique classes. If no skills have a recognised output,
    surface a generic `<dept_slug>_action` class so the operator can
    rename it.

    Note: `dept` is the department block. The caller resolves whether
    skills live at root (v3) or nested (legacy v2) and merges them into
    this block before calling.
    """
    skills_section = dept.get("skills") or {}
    classes: List[str] = []
    seen: Set[str] = set()
    # The skills section may be a dict layer_1..4 -> list of card names
    # OR a dict with full card dicts (when committed). Either way, look
    # in the SKILL.md if needed. We accept both shapes.
    for lyr_key, skills in (skills_section.items() if isinstance(skills_section, dict) else []):
        if not isinstance(skills, list):
            continue
        for skill in skills:
            outputs = []
            if isinstance(skill, dict):
                outputs = skill.get("outputs") or []
            # If only a name, peek into <dept_root>/skills/<name>/SKILL.md.
            # Done by caller — here we just handle dict shape.
            for out in outputs:
                for pat, klass in _OUTPUT_TO_ACTION_CLASS:
                    if re.match(pat, str(out)):
                        if klass not in seen:
                            classes.append(klass)
                            seen.add(klass)
    return classes


def _detect_action_classes_from_disk(
    dept_root: Path, dept: Dict[str, Any],
) -> List[str]:
    """Same as _detect_action_classes but also reads SKILL.md cards
    when dept.yaml only stores skill names (current step-4 output shape).
    """
    seen: Set[str] = set()
    out: List[str] = []
    # 1. Try the structured-skill path (skills with dict shape carrying outputs[]).
    for klass in _detect_action_classes(dept):
        if klass not in seen:
            out.append(klass)
            seen.add(klass)

    # 2. For each skill name, read SKILL.md if present and look at Outputs.
    skills_section = dept.get("skills") or {}
    if isinstance(skills_section, dict):
        for lyr_key, names in skills_section.items():
            if not isinstance(names, list):
                continue
            for name in names:
                if not isinstance(name, str):
                    continue
                # 2a. Try SKILL.md on disk.
                skill_md = dept_root / "skills" / name / "SKILL.md"
                if skill_md.exists():
                    body = skill_md.read_text(encoding="utf-8")
                    m = re.search(
                        r"##\s*Outputs\s*\n(.+?)(?=\n##|\Z)",
                        body, re.DOTALL,
                    )
                    if m:
                        outputs_block = m.group(1)
                        for outm in re.finditer(r"`([^`]+)`", outputs_block):
                            val = outm.group(1).strip()
                            for pat, klass in _OUTPUT_TO_ACTION_CLASS:
                                if re.match(pat, val):
                                    if klass not in seen:
                                        out.append(klass)
                                        seen.add(klass)
                # 2b. Name-based heuristic fallback.
                for pat, klass in _SKILL_NAME_TO_ACTION_CLASS:
                    if pat.search(name):
                        if klass not in seen:
                            out.append(klass)
                            seen.add(klass)
    # 3. If still empty, fall back to a generic class so the operator can
    # rename / extend.
    if not out:
        slug = dept.get("slug", "dept")
        out = [f"{slug}_action"]
    return out


def _build_policy(class_id: str) -> Dict[str, Any]:
    """Build a fully-formed policy proposal for an action class.

    `current_mode` is HARDCODED to `manual_required` — doctrine guard.
    """
    tmpl = _POLICY_TEMPLATES.get(class_id) or _generic_policy_template(class_id)
    # Ensure modes in eligible_future_modes are all official.
    efm = [
        m for m in tmpl.get("eligible_future_modes", [])
        if m in _OFFICIAL_MODES_SET
    ]
    return {
        "current_mode": _REQUIRED_CURRENT_MODE,
        "eligible_future_modes": efm or ["auto_with_veto_window"],
        "authorization_bands": dict(tmpl.get("authorization_bands") or {}),
        "kpi_guardrails": dict(tmpl.get("kpi_guardrails") or {}),
    }


def _refine_policy(policy: Dict[str, Any], variant: int) -> Dict[str, Any]:
    """Generate a visibly different policy on refine — swap modes or
    tighten KPIs."""
    out = {
        "current_mode": _REQUIRED_CURRENT_MODE,  # NEVER change in v1
        "eligible_future_modes": list(policy.get("eligible_future_modes") or []),
        "authorization_bands": dict(policy.get("authorization_bands") or {}),
        "kpi_guardrails": dict(policy.get("kpi_guardrails") or {}),
    }
    if variant % 3 == 1:
        # Tighter mode list — only the most conservative future mode.
        out["eligible_future_modes"] = ["manual_unless_policy_passed"]
    elif variant % 3 == 2:
        # Broader mode list — add another official mode.
        seen = set(out["eligible_future_modes"])
        for m in ALL_AUTONOMY_MODES:
            if m not in seen and m != _REQUIRED_CURRENT_MODE and m != "disabled":
                out["eligible_future_modes"].append(m)
                break
    return out


# ----- humanized rendering -----


def _humanize_mode_local(mode: str) -> str:
    """Local fallback for humanize_mode (avoids circular import)."""
    table = {
        "manual_required": "Tu valides chaque fois.",
        "manual_unless_policy_passed": "Tu valides sauf si la règle est OK.",
        "auto_if_policy_passed": "Elle gère seule si la règle est OK.",
        "auto_with_veto_window": "Elle agit ; tu peux revenir dessus.",
        "disabled": "Désactivé pour l'instant.",
    }
    return table.get(mode, mode)


# ----- Prompt copy (FR, Bureau-de-Cadre) -----


def prompt_action_classes(classes: List[str]) -> str:
    classes_str = "\n".join(f"- `{c}`" for c in classes)
    return (
        f"**Étape 5 — Gates, bandes d'autonomie, KPIs.**\n\n"
        f"J'ai identifié **{len(classes)} action class(es)** à encadrer, "
        f"à partir des skills que tu as validés à l'étape 4 :\n\n"
        f"{classes_str}\n\n"
        f"On va définir une gate policy pour chacune (niveau actuel, "
        f"niveau futur possible, bande d'autorisation, KPI garde-fous). "
        f"Tu veux **ajouter** ou **enlever** une classe avant qu'on "
        f"commence ?\n\n"
        f"Réponds `ok` pour commencer, ou décris les ajustements en "
        f"texte libre."
    )


def prompt_kpi_naming(class_id: str) -> str:
    """Polish Fix 3 — ask the operator to name the kpi_guardrail_set.

    Default `<class>_kpis` is surfaced explicitly so the operator can
    accept it with `ok` / `défaut`. A meaningful name (snake_case
    ≤30 chars) is preferred per Notion v5 lines 894-918.
    """
    return (
        f"**Comment veux-tu nommer ce jeu de garde-fous KPI pour "
        f"`{class_id}` ?**\n\n"
        f"Par défaut : `{class_id}_kpis`. Tu peux proposer un nom plus "
        f"parlant pour les rapports et la console — par exemple "
        f"`quality_floor`, `posts_safe_zone`, `trade_safety_net`.\n\n"
        f"Réponds `ok` pour garder le défaut, ou écris un nom au format "
        f"`snake_case` (≤30 caractères)."
    )


def prompt_band_naming(class_id: str, default_band: str) -> str:
    """Polish Fix 3 — ask the operator to name the authorization_band slug."""
    return (
        f"**Et la bande d'autorisation pour `{class_id}` ?**\n\n"
        f"Par défaut : `{default_band}`. Tu peux proposer un nom plus "
        f"parlant — par exemple `posts_safe_zone`, `warm_prospect_only`, "
        f"`existing_lines_only`.\n\n"
        f"Réponds `ok` pour garder le défaut, ou écris un nom au format "
        f"`snake_case` (≤30 caractères)."
    )


def prompt_policy_card(class_id: str, policy: Dict[str, Any],
                       idx: int, total: int) -> str:
    bands = policy.get("authorization_bands") or {}
    bands_lines: List[str] = []
    for bid, band in bands.items():
        bands_lines.append(f"  - Bande `{bid}` :")
        for k, v in band.items():
            if isinstance(v, list):
                bands_lines.append(
                    f"    - {k} : " + ", ".join(f"`{x}`" for x in v)
                )
            else:
                bands_lines.append(f"    - {k} : `{v}`")
    bands_str = "\n".join(bands_lines) or "  _(aucune)_"

    kpis = policy.get("kpi_guardrails") or {}
    kpi_lines = [f"  - `{k}` : `{v}`" for k, v in kpis.items()]
    kpi_str = "\n".join(kpi_lines) or "  _(aucune)_"

    efm = policy.get("eligible_future_modes") or []
    futur_str = ", ".join(f"`{m}`" for m in efm) or "_(aucun)_"

    current = policy.get("current_mode", "?")
    current_human = _humanize_mode_local(current)

    return (
        f"**Gate policy #{idx}/{total} pour `{class_id}`**\n\n"
        f"- **Niveau actuel** : `{current}` — {current_human}\n"
        f"- **Niveau futur possible** : {futur_str}\n"
        f"- **Bandes d'autorisation** :\n{bands_str}\n"
        f"- **KPI garde-fous** :\n{kpi_str}\n\n"
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

# Inline edits.
_EDIT_CURRENT_MODE_RE = re.compile(
    r"current_mode\s*[:=]?\s*([a-z_]+)", re.IGNORECASE,
)
_EDIT_FUTURE_MODE_ADD_RE = re.compile(
    r"(?:ajoute|add)\s+([a-z_]+)\s+(?:dans|to|au)\s*future", re.IGNORECASE,
)


def _classify_intent(text: str) -> Optional[Action]:
    if _REFINE_RE.search(text):
        return Action.REFINE
    if _EDIT_RE.search(text):
        return Action.EDIT
    if _APPROVE_RE.search(text):
        return Action.APPROVE_SUBSTEP
    return None


# ----- Schema-shape projection -----


def _project_to_schema_shape(
    class_id: str,
    policy: Dict[str, Any],
    band_override: Optional[str] = None,
    kpi_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Project a full-detail policy dict into the dept.yaml schema shape.

    The dept.yaml `gate_policies.<class>` entry is the SUMMARY shape per
    dept.schema.yaml lines 267-316 + schemas-draft/examples/dept-ops-maya.yaml
    lines 78-85:
        current_mode: <mode>
        eligible_future_modes: [<mode>, ...]
        authorization_band: <band_slug>   (singular string)
        kpi_guardrail_set: <kpis_slug>    (singular string)

    The full-detail plural shape (with `authorization_bands: {...}` and
    `kpi_guardrails: {kpi: threshold, ...}`) stays in `policies/<class>.yaml`.

    Polish Fix 3 (2026-05-21): the operator can override the auto-generated
    `<class>_kpis` and `<first_band>` slugs with a meaningful name (e.g.
    `quality_floor`, `posts_safe_zone`). When `*_override` is None, fall back
    to the legacy auto-generated slug.
    """
    bands = policy.get("authorization_bands") or {}
    if band_override is not None:
        band_slug = band_override
    elif isinstance(bands, dict) and bands:
        # Take the first band's id as the canonical slug for the dept.yaml.
        band_slug = next(iter(bands.keys()))
    else:
        band_slug = f"{class_id}_default_band"
    kpi_slug = kpi_override if kpi_override is not None else f"{class_id}_kpis"
    return {
        "current_mode": policy.get("current_mode", "manual_required"),
        "eligible_future_modes": list(policy.get("eligible_future_modes") or []),
        "authorization_band": band_slug,
        "kpi_guardrail_set": kpi_slug,
    }


# ----- Naming validation (Polish Fix 3) -----

# snake_case identifier, ≤30 chars. The operator's reply is accepted when it
# matches this regex AFTER stripping leading/trailing whitespace.
_NAMING_RE = re.compile(r"^[a-z][a-z0-9_]{0,29}$")


def _parse_naming_answer(text: str) -> Optional[str]:
    """Parse operator's free-text answer to a naming question.

    Returns:
      None  → keep default (empty, "ok", "défaut", or garbage)
      str   → operator-chosen identifier (snake_case ≤30 chars)
    """
    t = (text or "").strip()
    if not t:
        return None
    tl = t.lower()
    # "ok" / "défaut" / "default" / "garde le défaut" → keep default
    if tl in {"ok", "oui", "défaut", "defaut", "default", "non"}:
        return None
    # "garde le défaut" / "keep default" → keep default
    if "défaut" in tl or "defaut" in tl or "default" in tl:
        return None
    # Accept only the first whitespace-separated token; reject if not snake_case.
    first = t.split()[0]
    if _NAMING_RE.match(first):
        return first
    return None


# ----- I/O helpers -----


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


class GatesKpisRunner(StepRunner):
    """Conversational runner for Step 5 (Gates, autonomy bands, KPIs)."""

    step_name = STEP_NAME

    def __init__(self) -> None:
        super().__init__()
        self._validated_policies: List[Dict[str, Any]] = []
        self._current_substep: Optional[Dict[str, Any]] = None
        self._current_status: str = "drafting"

        self._detected_action_classes: List[str] = []
        self._classes_confirmed: bool = False
        self._pending_classes: List[str] = []
        self._current_class: Optional[str] = None
        self._current_policy: Optional[Dict[str, Any]] = None
        self._refine_variant: int = 0

        # Phase: "classes_list" → "policy_card" → "kpi_naming" →
        #        "band_naming" → (commit) → next class or "done"
        self._phase: str = "classes_list"
        self._artifacts_written: List[Path] = []
        # Sprint correctif Fix 4 (2026-05-21): one-shot Bureau-de-Cadre
        # French explanation when `_apply_inline_edit` refuses a doctrine
        # violation. Surfaced by `next_prompt()` then cleared.
        self._last_rejection_reason: Optional[str] = None
        # Polish Fix 3 (2026-05-21): operator-named slugs for the current
        # in-flight policy. None = use default. Cleared after each commit.
        self._pending_kpi_name: Optional[str] = None
        self._pending_band_name: Optional[str] = None

    # ----- lifecycle -----

    def start(self, state_path: Path, dept_yaml_draft_path: Path) -> None:
        self.state_path = Path(state_path)
        self.dept_yaml_draft_path = Path(dept_yaml_draft_path)
        dept_root = self.dept_yaml_draft_path.parent
        draft = _read_yaml(self.dept_yaml_draft_path)
        dept = dict(draft.get("department") or {})
        # In v3 the skills section lives at draft.skills (root). Merge it
        # into the dept block we hand to the detector so the detector keeps
        # its single-source-of-truth shape. Legacy v2 carriers (skills under
        # `department:`) still work via the .get fallback.
        if "skills" in draft and "skills" not in dept:
            dept["skills"] = draft["skills"]

        # Detect action classes.
        self._detected_action_classes = _detect_action_classes_from_disk(
            dept_root, dept,
        )

        # Restore from state.
        state_doc = _read_yaml(self.state_path)
        progress = (state_doc.get("step_progress") or {}).get(self.step_name) or {}
        self._validated_policies = list(progress.get("sub_artifacts_validated") or [])
        self._current_status = progress.get("current_status", "drafting")
        self._classes_confirmed = bool(progress.get("classes_confirmed"))

        # Compute pending classes minus validated.
        done = {p.get("class_id") for p in self._validated_policies}
        self._pending_classes = [
            c for c in self._detected_action_classes if c not in done
        ]

        cs = progress.get("current_substep")
        if isinstance(cs, dict):
            self._current_substep = dict(cs)
            payload = cs.get("draft_payload") or {}
            if cs.get("type") == SUBSTEP_POLICY_DRAFT:
                self._current_class = payload.get("class_id")
                self._current_policy = payload.get("policy")
                self._refine_variant = int(payload.get("refine_variant", 0))
                self._classes_confirmed = True
                self._phase = "policy_card"

        if not self._current_substep:
            self._derive_phase()
        self._persist_progress()

    def _derive_phase(self) -> None:
        if not self._classes_confirmed:
            self._phase = "classes_list"
            return
        if self._current_class is None and not self._pending_classes:
            self._phase = "done"
            return
        if self._current_class is None and self._pending_classes:
            self._current_class = self._pending_classes.pop(0)
            self._current_policy = _build_policy(self._current_class)
            self._refine_variant = 0
            self._phase = "policy_card"
            return
        # Polish Fix 3 (2026-05-21): preserve naming sub-phases. Only
        # fall back to "policy_card" when the current phase isn't one
        # of the in-flight per-class sub-phases.
        if self._phase not in ("policy_card", "kpi_naming", "band_naming"):
            self._phase = "policy_card"

    # ----- conversational surface -----

    def next_prompt(self) -> Optional[str]:
        if self.is_done():
            return None
        self._derive_phase()

        base: Optional[str] = None
        if self._phase == "classes_list":
            base = prompt_action_classes(self._detected_action_classes)

        elif self._phase == "policy_card" and self._current_class is not None and self._current_policy is not None:
            idx = len(self._validated_policies) + 1
            total = idx + len(self._pending_classes)
            base = prompt_policy_card(
                self._current_class, self._current_policy, idx, total,
            )

        elif self._phase == "kpi_naming" and self._current_class is not None:
            base = prompt_kpi_naming(self._current_class)

        elif self._phase == "band_naming" and self._current_class is not None and self._current_policy is not None:
            # Default band slug for the prompt = first band id in the
            # full-detail policy, falling back to `<class>_default_band`.
            bands = self._current_policy.get("authorization_bands") or {}
            default_band = (
                next(iter(bands.keys())) if isinstance(bands, dict) and bands
                else f"{self._current_class}_default_band"
            )
            base = prompt_band_naming(self._current_class, default_band)

        if base is None:
            return None

        # Sprint correctif Fix 4 (2026-05-21): one-shot doctrine-guard
        # rejection message prepended so the operator sees WHY their edit
        # was ignored. Cleared immediately after this render.
        if self._last_rejection_reason:
            reason = self._last_rejection_reason
            self._last_rejection_reason = None
            return f"⚠ {reason}\n\n---\n\n{base}"
        return base

    def on_answer(self, operator_text: str) -> Action:
        text = (operator_text or "").strip()
        if not text:
            return Action.CONTINUE

        # Substep A — confirm classes list.
        if self._phase == "classes_list":
            # Any reasonable response confirms the list (operator may add
            # / remove via free-text but we keep the catalogue as-is).
            self._classes_confirmed = True
            # Advance to first class.
            if self._pending_classes:
                self._current_class = self._pending_classes.pop(0)
                self._current_policy = _build_policy(self._current_class)
                self._refine_variant = 0
                self._persist_substep()
            self._phase = "policy_card"
            return Action.CONTINUE

        # Substep B — per-policy.
        if self._phase == "policy_card" and self._current_policy is not None:
            intent = _classify_intent(text)
            if intent == Action.APPROVE_SUBSTEP:
                # Polish Fix 3: don't commit yet. Move into kpi_naming
                # sub-phase so the operator can name the guardrail set.
                self._pending_kpi_name = None
                self._pending_band_name = None
                self._phase = "kpi_naming"
                self._persist_substep()
                return Action.APPROVE_SUBSTEP
            if intent == Action.EDIT:
                self._apply_inline_edit(text)
                self._persist_substep()
                return Action.EDIT
            if intent == Action.REFINE:
                self._refine_variant += 1
                self._current_policy = _refine_policy(
                    self._current_policy, self._refine_variant,
                )
                self._persist_substep()
                return Action.REFINE
            return Action.CONTINUE

        # Polish Fix 3 — naming sub-phases.
        if self._phase == "kpi_naming":
            self._pending_kpi_name = _parse_naming_answer(text)
            self._phase = "band_naming"
            self._persist_substep()
            return Action.CONTINUE

        if self._phase == "band_naming":
            self._pending_band_name = _parse_naming_answer(text)
            # Now actually commit the policy with the operator-chosen slugs.
            committed = self._commit_current_policy()
            # Clear naming state regardless (avoid leak between classes).
            self._pending_kpi_name = None
            self._pending_band_name = None
            if not committed:
                # Snap back to policy_card so the operator can re-approve /
                # edit (e.g. the tester refused the underlying policy).
                self._phase = "policy_card"
                self._persist_substep()
                return Action.CONTINUE
            # Advance to next class.
            if self._pending_classes:
                self._current_class = self._pending_classes.pop(0)
                self._current_policy = _build_policy(self._current_class)
                self._refine_variant = 0
                self._phase = "policy_card"
                self._persist_substep()
            else:
                self._current_class = None
                self._current_policy = None
                self._current_substep = None
                self._phase = "done"
                self._current_status = "validated"
                self._persist_progress()
            return Action.APPROVE_SUBSTEP

        return Action.CONTINUE

    def is_done(self) -> bool:
        if not self._classes_confirmed:
            return False
        if not self._detected_action_classes:
            return False
        validated_classes = {p.get("class_id") for p in self._validated_policies}
        return set(self._detected_action_classes).issubset(validated_classes)

    def artifacts_produced(self) -> List[Path]:
        return list(self._artifacts_written)

    # ----- internal -----

    def _apply_inline_edit(self, text: str) -> None:
        """Apply operator's inline edit. Doctrine guards enforced here.

        Sprint correctif Fix 4 (2026-05-21): when a guard refuses, set
        `_last_rejection_reason` to a Bureau-de-Cadre French sentence so
        the operator sees WHY their edit was ignored. Previously this was
        a silent reject → operator concluded the system was broken.
        """
        assert self._current_policy is not None
        # Try to change current_mode — but reject if not manual_required.
        m = _EDIT_CURRENT_MODE_RE.search(text)
        if m:
            new_mode = m.group(1).strip()
            if new_mode != _REQUIRED_CURRENT_MODE:
                self._last_rejection_reason = (
                    "J'ai entendu ta demande de passer le `current_mode` "
                    f"à `{new_mode}`, mais la doctrine v1 m'oblige à "
                    "rester en `manual_required` pour toutes les action "
                    "classes (cf. Notion v5 ligne 895 : « L'agent propose "
                    "les gates et l'autonomie future, sans l'activer en "
                    "v1 »). On pourra élever le niveau d'autonomie plus "
                    "tard via une PR `settings_pr` explicite, après une "
                    "phase d'observation (Notion v5 lignes 421-436) qui "
                    "confirme que les KPIs restent verts."
                )
                return
            # If they're "changing" to manual_required (no-op), fine.
        # Try to add a future mode — only if official.
        m = _EDIT_FUTURE_MODE_ADD_RE.search(text)
        if m:
            cand = m.group(1).strip()
            if cand in _OFFICIAL_MODES_SET:
                efm = list(self._current_policy.get("eligible_future_modes") or [])
                if cand not in efm:
                    efm.append(cand)
                    self._current_policy["eligible_future_modes"] = efm
                return
            # Doctrine guard — set a French explanation.
            modes_str = " / ".join(ALL_AUTONOMY_MODES)
            self._last_rejection_reason = (
                f"`{cand}` n'est pas un mode d'autonomie — c'est une "
                "phase doctrinale (cf. Notion v5 lignes 421-436 sur la "
                "phase d'observation). Les 5 modes officiels sont : "
                f"{modes_str}. La phase d'observation traverse les modes, "
                "elle ne les remplace pas — l'agent peut être en "
                "`manual_required` ET être observé en parallèle pour "
                "préparer une élévation future."
            )
            return

    def _persist_substep(self) -> None:
        self._current_substep = {
            "type": SUBSTEP_POLICY_DRAFT,
            "draft_payload": {
                "class_id": self._current_class,
                "policy": self._current_policy,
                "refine_variant": self._refine_variant,
            },
        }
        self._current_status = "awaiting_validation"
        self._persist_progress()

    def _commit_current_policy(self) -> bool:
        """Test current_policy and, on PASS, write to disk + dept.yaml."""
        assert self._current_class is not None
        assert self._current_policy is not None
        assert self.dept_yaml_draft_path is not None
        # Hard doctrine guard before testing.
        if self._current_policy.get("current_mode") != _REQUIRED_CURRENT_MODE:
            return False
        for m in self._current_policy.get("eligible_future_modes") or []:
            if m not in _OFFICIAL_MODES_SET:
                return False

        dept_root = self.dept_yaml_draft_path.parent
        ctx = {
            "dept_root": dept_root,
            "dept_yaml_draft_path": self.dept_yaml_draft_path,
        }
        result = test_artifact("gate_policy", self._current_policy, ctx)
        if not result.passed:
            return False
        # Write per-class file.
        policy_file = dept_root / "policies" / f"{self._current_class}.yaml"
        _atomic_write_yaml(policy_file, dict(self._current_policy))
        if policy_file not in self._artifacts_written:
            self._artifacts_written.append(policy_file)
        # Append to validated list. Polish Fix 3: also record the operator's
        # naming choices so a later sync (or resume) can replay them.
        self._validated_policies.append({
            "id": self._current_class,
            "class_id": self._current_class,
            "type": SUBSTEP_POLICY_DRAFT,
            "policy": dict(self._current_policy),
            "kpi_guardrail_set_override": self._pending_kpi_name,
            "authorization_band_override": self._pending_band_name,
            "validated_at": _now_iso(),
        })
        # Sync dept.yaml.draft::gate_policies.
        self._sync_gate_policies_in_draft()
        return True

    def _sync_gate_policies_in_draft(self) -> None:
        """Mirror gate_policies into dept.yaml.draft.

        v3 schema layout (cf. Notion v5 lines 894-918, dept.schema.yaml
        lines 259-316): `gate_policies:` is a TOP-LEVEL sibling of
        `department:`, not nested under it. The dept.yaml entry is the
        SUMMARY shape (slug references), per
        schemas-draft/examples/dept-ops-maya.yaml lines 78-85:
            authorization_band: <band_slug>     (string, singular)
            kpi_guardrail_set: <kpis_slug>      (string, singular)
        The DETAIL shape (plural `authorization_bands:` + `kpi_guardrails:`
        dicts) stays in `policies/<class>.yaml` per Notion v5 894-918.
        """
        assert self.dept_yaml_draft_path is not None
        draft = _read_yaml(self.dept_yaml_draft_path)
        draft.setdefault("department", {})
        # Strip any legacy nested copy.
        draft["department"].pop("gate_policies", None)
        gp: Dict[str, Any] = {}
        for entry in self._validated_policies:
            class_id = entry["class_id"]
            policy = dict(entry["policy"])
            # Polish Fix 3: thread the operator's naming choices through.
            gp[class_id] = _project_to_schema_shape(
                class_id, policy,
                band_override=entry.get("authorization_band_override"),
                kpi_override=entry.get("kpi_guardrail_set_override"),
            )
        draft["gate_policies"] = gp
        _atomic_write_yaml(self.dept_yaml_draft_path, draft)
        if self.dept_yaml_draft_path not in self._artifacts_written:
            self._artifacts_written.append(self.dept_yaml_draft_path)

    # ----- persistence -----

    def _persist_progress(self) -> None:
        if self.state_path is None:
            return
        doc = _read_yaml(self.state_path)
        progress = doc.setdefault("step_progress", {})
        entry: Dict[str, Any] = {
            "sub_artifacts_validated": list(self._validated_policies),
            "current_substep": self._current_substep,
            "current_status": self._current_status,
            "classes_confirmed": self._classes_confirmed,
        }
        progress[self.step_name] = entry
        doc["last_updated_at"] = _now_iso()
        _atomic_write_yaml(self.state_path, doc)


register_runner(STEP_NAME, GatesKpisRunner)
