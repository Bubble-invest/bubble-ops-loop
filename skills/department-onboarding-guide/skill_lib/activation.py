"""
activation.py — Step 7 helpers (status flip + PR body builder + pre-flight gate).

Notion v5 lines 947-1003.

Public API:
  - flip_status_to_live(dept_yaml_path)             — UX-1 stub kept for back-compat
  - build_activation_pr_body(display_name, slug, ..)— UX-1 stub kept for back-compat
  - can_activate(state_yaml_path, dept_root)        — UX-5: full pre-flight gate
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Tuple

import yaml


# Trade/action mission kinds that REQUIRE a gate_policy.yaml file at the
# dept root. Derived from Notion v5 lines 974-995 (the activation PR body
# section lists "Gate policy summary" as a mandatory block when any of
# these kinds appear in recurring_missions).
_TRADE_ACTION_KINDS = (
    "trade", "trade_proposal", "order", "order_proposal",
    "action", "action_proposal", "publish", "post_publish",
)


def flip_status_to_live(dept_yaml_path: Path) -> None:
    """
    Mutate a dept.yaml file: set department.status from `onboarding` to `live`.

    Reads / parses / re-emits with yaml.safe_dump. Indentation is the standard
    2-space block style; comments in the source ARE lost (acceptable for the
    activation commit per Notion v5 line 977).
    """
    if not dept_yaml_path.exists():
        raise FileNotFoundError(dept_yaml_path)
    doc = yaml.safe_load(dept_yaml_path.read_text(encoding="utf-8"))
    if "department" not in doc:
        raise ValueError(f"{dept_yaml_path}: missing `department:` wrapper")
    doc["department"]["status"] = "live"
    dept_yaml_path.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


_STEP_LABELS = {
    "mandate": "1. Mandate",
    "missions": "2. Recurring missions",
    "layers": "3. Layer mapping",
    "skills_tools": "4. Skills & tools",
    "gates_kpis": "5. Gates & KPIs",
    "dry_run": "6. Dry run",
}


def build_activation_pr_body(
    display_name: str,
    slug: str,
    validated_steps: List[str],
) -> str:
    """Build the body of the activation PR per Notion v5 lines 961-977."""
    lines = [
        f"# Activate {display_name} department",
        "",
        f"Branch: `onboarding/{slug}` (per Notion v5 line 964).",
        "",
        "## Validated steps",
        "",
    ]
    for step in validated_steps:
        label = _STEP_LABELS.get(step, step)
        # Surface both the human label and the raw step-id (for audit grep).
        lines.append(f"- [x] {label} (`{step}`)")
    lines.extend(
        [
            "",
            "## Activation effect",
            "",
            f"- `dept.yaml::department.status`: `onboarding` -> `live`",
            f"- Front-end card moves: `Agents a eclore` -> `Live departments`",
            f"- Session becomes: `ops-loop-{slug}`",
        ]
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# UX-5: pre-flight gate.
# ---------------------------------------------------------------------------

_REQUIRED_STEPS = (
    "mandate", "missions", "layers",
    "skills_tools", "gates_kpis", "dry_run",
)


def _has_trade_or_action_mission(dept_doc: dict) -> bool:
    """Return True iff dept declares a recurring mission whose `creates[]`
    list contains any trade/action kind (Notion v5 lines 974-995)."""
    missions = dept_doc.get("recurring_missions") or []
    for m in missions:
        if not isinstance(m, dict):
            continue
        creates = m.get("creates") or []
        for c in creates:
            c_lower = str(c).lower()
            for needle in _TRADE_ACTION_KINDS:
                if needle in c_lower:
                    return True
    return False


def _mandate_is_placeholder(text: str) -> bool:
    """Return True iff MANDATE.md's content is trivial/placeholder-only.

    Strips markdown heading lines (`#...`) and blank lines, then checks
    whatever prose remains has some non-trivial length. Catches a stub
    file (e.g. just a title, or created-but-never-filled) without being
    so strict that a legitimately terse mandate gets rejected.
    """
    body_lines = [
        ln for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    body = " ".join(body_lines).strip()
    return len(body) < 30


def _check_core_dept_shape(dept_root: Path, dept_doc: dict) -> List[str]:
    """UX-5 card #1268: verify the standard dept shape exists on disk.

    `can_activate()`'s other checks (validated_steps, dept.yaml schema,
    dry-run) all trust STATE.yaml / dept.yaml bookkeeping. That's exactly
    the gap that let Géraldine reach "Live" without a real MANDATE.md:
    her Step 1 commit only touched dept.yaml.draft, `validated_steps`
    said "mandate" was done, and nothing re-checked the filesystem before
    promotion. This function is that re-check — pure filesystem
    verification, independent of what STATE.yaml claims.

    Returns a list of operator-actionable reasons (empty == shape OK).
    """
    reasons: List[str] = []

    # -- MANDATE.md present AND non-trivial -------------------------------
    mandate_path = dept_root / "MANDATE.md"
    if not mandate_path.exists():
        reasons.append(
            f"MANDATE.md not found at {mandate_path}. Activation requires "
            f"a committed, non-empty narrative mandate — re-run Step 1 "
            f"(scripts/validate-step.sh --step=mandate)."
        )
    else:
        try:
            mandate_text = mandate_path.read_text(encoding="utf-8")
        except OSError as exc:
            reasons.append(f"MANDATE.md could not be read ({exc}).")
            mandate_text = None
        if mandate_text is not None and _mandate_is_placeholder(mandate_text):
            reasons.append(
                f"MANDATE.md at {mandate_path} looks like an empty "
                f"placeholder (no real narrative content). Fill it in via "
                f"Step 1 before activation."
            )

    # -- layers/<N>/PROMPT.md present for each subscribed layer -----------
    subscribed = (dept_doc.get("layers") or {}).get("subscribed") or []
    for n in subscribed:
        prompt_path = dept_root / "layers" / str(n) / "PROMPT.md"
        if not prompt_path.exists():
            reasons.append(
                f"layers/{n}/PROMPT.md not found but dept.yaml declares "
                f"layers.subscribed includes {n}. Re-run Step 3 "
                f"(scripts/validate-step.sh --step=layers) for layer {n}."
            )
        elif prompt_path.stat().st_size == 0:
            reasons.append(
                f"layers/{n}/PROMPT.md is empty. Re-run Step 3 "
                f"(scripts/validate-step.sh --step=layers) for layer {n}."
            )

    # -- declared recurring_missions have a missions/<id>.yaml -------------
    missions = dept_doc.get("recurring_missions") or []
    missions_dir = dept_root / "missions"
    for m in missions:
        if not isinstance(m, dict):
            continue
        mission_id = m.get("id")
        if not mission_id:
            continue
        mission_path = missions_dir / f"{mission_id}.yaml"
        if not mission_path.exists():
            reasons.append(
                f"missions/{mission_id}.yaml not found but dept.yaml "
                f"declares recurring_missions[].id={mission_id!r}. Re-run "
                f"Step 2 (scripts/validate-step.sh --step=missions)."
            )

    return reasons


def _load_dept_schema_validator():
    """Lazy-load dept.schema.yaml as a jsonschema.Draft7Validator. Returns
    None if jsonschema isn't available."""
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return None
    schema_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "schemas-draft" / "dept.schema.yaml"
    )
    if not schema_path.exists():
        return None
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    import jsonschema
    return jsonschema.Draft7Validator(schema)


def can_activate(
    state_yaml_path: Path, dept_root: Path,
) -> Tuple[bool, List[str]]:
    """Pre-flight gate for `activate-dept.sh`.

    Returns (True, []) when ALL of these hold:
      1. STATE.yaml::status == "Ready to activate"
      2. All 6 prior steps in validated_steps[]
      3. dept.yaml present, parses, schema-valid
      4. Core dept shape actually exists on disk (card #1268, fail-closed):
         MANDATE.md present + non-trivial, layers/<N>/PROMPT.md present for
         every subscribed layer, missions/<id>.yaml present for every
         declared recurring mission. Re-verified independently of
         validated_steps[] / dept.yaml claims, since those can be wrong
         (see Géraldine incident: "mandate" was marked validated while
         MANDATE.md was never committed).
      5. gate_policy.yaml present when dept declares any trade/action mission
      6. dry_run.run_dry_run_full() returns can_advance_to_ready=True

    Returns (False, [reasons...]) otherwise. Each reason is a short
    operator-actionable sentence. Pure function except for the dry-run
    invocation which writes into <dept_root>/outputs/dry-run/<ts>/.
    """
    reasons: List[str] = []

    state_yaml_path = Path(state_yaml_path)
    dept_root = Path(dept_root)

    # ---- 1. STATE.yaml exists + parses ----------------------------------
    if not state_yaml_path.exists():
        return False, [
            f"STATE.yaml not found at {state_yaml_path}. "
            f"Run scripts/bootstrap-dept.sh first."
        ]
    try:
        state = yaml.safe_load(state_yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return False, [
            f"STATE.yaml is not valid YAML ({exc}). Fix syntax errors first."
        ]
    if not isinstance(state, dict):
        return False, ["STATE.yaml does not contain a mapping at the root."]

    # ---- 2. status must be 'Ready to activate' --------------------------
    status = state.get("status")
    if status != "Ready to activate":
        reasons.append(
            f"STATE.yaml::status is {status!r}, expected 'Ready to activate'. "
            f"Validate the remaining steps via scripts/validate-step.sh."
        )

    # ---- 3. all 6 steps validated ---------------------------------------
    validated = set(state.get("validated_steps", []) or [])
    missing = [s for s in _REQUIRED_STEPS if s not in validated]
    if missing:
        reasons.append(
            f"Missing validated steps: {', '.join(missing)}. "
            f"Run scripts/validate-step.sh --step=<step> for each."
        )

    # ---- 4. dept.yaml present, parses, schema-valid ---------------------
    dept_path = dept_root / "dept.yaml"
    dept_doc = None
    if not dept_path.exists():
        reasons.append(
            f"dept.yaml not found at {dept_path}. "
            f"Activation requires the promoted (non-draft) dept.yaml."
        )
    else:
        try:
            dept_doc = yaml.safe_load(dept_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            reasons.append(
                f"dept.yaml is not valid YAML ({exc}). Fix syntax errors first."
            )
        if dept_doc is not None:
            validator = _load_dept_schema_validator()
            if validator is not None:
                errors = sorted(validator.iter_errors(dept_doc),
                                key=lambda e: e.path)
                if errors:
                    msgs = [e.message for e in errors[:3]]
                    reasons.append(
                        f"dept.yaml fails schema validation: "
                        f"{'; '.join(msgs)}. Fix per schemas-draft/dept.schema.yaml."
                    )

    # ---- 4b. core dept shape present on disk (fail-closed, card #1268) --
    # MUST run even when the dept.yaml schema check above already passed:
    # STATE.yaml::validated_steps is a claim, not a fact. Re-verify the
    # files it claims to have produced actually exist and aren't stubs.
    if dept_doc is not None:
        reasons.extend(_check_core_dept_shape(dept_root, dept_doc))

    # ---- 5. gate_policy.yaml required iff trade/action mission ----------
    if dept_doc is not None and _has_trade_or_action_mission(dept_doc):
        gp_path = dept_root / "gate_policy.yaml"
        if not gp_path.exists():
            reasons.append(
                f"gate_policy.yaml required at {gp_path} because dept "
                f"declares a trade/action recurring mission. "
                f"Author it per schemas-draft/gate-item.schema.yaml."
            )

    # ---- 6. dry-run hard gate -------------------------------------------
    # Re-run the full dry-run simulator so we know we're testing against
    # the current dept fixtures. Pure-Python; writes outputs/dry-run/<ts>/.
    if dept_doc is not None:
        # Make sibling modules importable.
        _skill_dir = Path(__file__).resolve().parent
        if str(_skill_dir.parent) not in sys.path:
            sys.path.insert(0, str(_skill_dir.parent))
        try:
            from skill_lib.dry_run import run_dry_run_full
            result = run_dry_run_full(
                dept_root=dept_root,
                operator_accepts_warnings=False,
                seed=1,
            )
            if not result.can_advance_to_ready:
                reasons.append(
                    f"Latest dry-run failed ({result.overall_status}). "
                    f"Re-run scripts/run-dry-run.sh and address blocking "
                    f"checks before activation."
                )
        except Exception as exc:  # noqa: BLE001
            reasons.append(
                f"Dry-run simulator raised {type(exc).__name__}: {exc}. "
                f"Investigate via scripts/run-dry-run.sh."
            )

    return (not reasons), reasons
