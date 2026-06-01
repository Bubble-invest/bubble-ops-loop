"""
auto_drive.py — Phase G2 helper for the eclosing agent to drive its own
7-step onboarding.

Public surface:
    get_current_step(state_path) -> str
    get_step_prompt(step) -> str
    record_step_completion(state_path, step, artifact_paths) -> None
    get_followup_prompt(step, operator_choice) -> str          (Fix 3)
    record_edit_request(state_path, step, operator_text)       (Fix 3)
    record_refine_request(state_path, step, reason)            (Fix 3)
    record_approval(state_path, step)                          (Fix 3)

CLI entry points (invoked from SessionStart hook + cron):
    python3 -m skill_lib.auto_drive announce_current_step <state_path>

The eclosing agent reads its SKILL on first boot, calls get_current_step()
to know what to do, calls get_step_prompt() to know what to ask Joris on
Telegram, waits for his answer, writes the artifact, then calls
record_step_completion() to advance.

Voice of the prompts: French, Bureau-de-Cadre (calm, concrete, 3 options).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

import yaml


# The 6 work-steps in order. Activation is step 7 but doesn't appear in
# validated_steps — it's the status flip Ready-to-activate -> Live.
ORDERED_WORK_STEPS: List[str] = [
    "mandate",
    "missions",
    "layers",
    "skills_tools",
    "gates_kpis",
    "dry_run",
]

# Status the dept should be in AFTER each work-step is validated.
STEP_TO_POST_STATUS = {
    "mandate":      "Configuring",
    "missions":     "Drafting",
    "layers":       "Drafting",
    "skills_tools": "Needs validation",
    "gates_kpis":   "Dry run",
    "dry_run":      "Ready to activate",
}

STATUSES_IN_ORDER: List[str] = [
    "Idea",
    "Configuring",
    "Drafting",
    "Needs validation",
    "Dry run",
    "Ready to activate",
    "Live",
]


# Per-step prompts. Each must:
#  - be French Bureau-de-Cadre voice (calm, concrete);
#  - propose 3 concrete options (1., 2., 3.);
#  - end with a direct question to Joris.
_STEP_PROMPTS: dict[str, str] = {
    "mandate": (
        "**Étape 1 — Mon mandat.**\n\n"
        "Avant d'avancer, j'ai besoin qu'on s'accorde sur la phrase qui me "
        "définit. Voici 3 options :\n\n"
        "1. *Mandat resserré* — je fais une seule chose, très bien.\n"
        "2. *Mandat équilibré* — je couvre 2 ou 3 sujets liés.\n"
        "3. *Mandat large* — je joue un rôle transversal pour l'équipe.\n\n"
        "Tu préfères laquelle ? Une fois choisi, je te propose une "
        "formulation en une phrase."
    ),
    "missions": (
        "**Étape 2 — Mes missions récurrentes.**\n\n"
        "Maintenant qu'on a le mandat, voici 3 propositions de rythme :\n\n"
        "1. *Quotidien* — 1 mission par jour, court mais régulier.\n"
        "2. *Hebdomadaire* — 1 grosse mission par semaine + 2 checks rapides.\n"
        "3. *À la demande* — 1 mission de fond + déclenchement par événement.\n\n"
        "Laquelle correspond le mieux à ce que tu attends de moi ?"
    ),
    "layers": (
        "**Étape 3 — Mes 4 moments de la journée.**\n\n"
        "Le flow standard a 4 couches OODA. Voici 3 répartitions possibles :\n\n"
        "1. *Toutes les 4* — je suis présente à chaque moment (charge max).\n"
        "2. *Recherche + Exécution* (layers 2 + 3) — je laisse le matin/soir "
        "aux autres.\n"
        "3. *Exécution uniquement* (layer 3) — je suis purement exécutante.\n\n"
        "Tu m'orientes vers laquelle ?"
    ),
    "skills_tools": (
        "**Étape 4 — Mes compétences et outils.**\n\n"
        "Pour tenir mon mandat, j'ai besoin d'un kit. Voici 3 niveaux :\n\n"
        "1. *Minimal* — uniquement lecture + Telegram (je remonte, tu décides).\n"
        "2. *Standard* — kit du dept (skills internes + outils SaaS lus seuls).\n"
        "3. *Étendu* — accès écriture sur 1 ou 2 systèmes (à valider gate par gate).\n\n"
        "Lequel te paraît juste pour mon démarrage ?"
    ),
    "gates_kpis": (
        "**Étape 5 — Mes garde-fous (gates) et mes KPIs.**\n\n"
        "Voici 3 niveaux d'autonomie possibles pour mes actions sensibles :\n\n"
        "1. *Manuel obligatoire* — chaque action passe par toi (rythme prudent).\n"
        "2. *Auto si policy passe* — j'agis seule si ma politique le permet.\n"
        "3. *Auto avec veto window* — j'agis, tu as 30 min pour annuler.\n\n"
        "Sur quel niveau on commence ? On pourra remonter après quelques semaines."
    ),
    "dry_run": (
        "**Étape 6 — Ma répétition à blanc.**\n\n"
        "Je vais simuler une journée type bout-en-bout. 3 modes possibles :\n\n"
        "1. *Smoke* — un seul cas d'usage, validation rapide.\n"
        "2. *Standard* — les 4 layers déroulés sur une fixture réaliste.\n"
        "3. *Stress* — fixtures dégradées pour tester les garde-fous.\n\n"
        "Lequel tu veux qu'on lance d'abord ? Je te rapporte PASS / WARN / FAIL."
    ),
    "activation": (
        "**Étape 7 — Mon activation.**\n\n"
        "Tout est validé. Avant que je rejoigne officiellement l'équipe, "
        "voici 3 manières d'ouvrir la PR :\n\n"
        "1. *Standard* — PR avec corps généré, j'attends ton merge.\n"
        "2. *Soft launch* — PR mergée + pause 24h avant que je tourne.\n"
        "3. *Plein régime* — PR mergée + activation systemd immédiate.\n\n"
        "Tu préfères laquelle ?"
    ),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_current_step(state_path: Path) -> str:
    """Return the next step the eclosing agent should drive.

    If all 6 work-steps are validated, returns 'activation'.
    """
    doc = yaml.safe_load(Path(state_path).read_text(encoding="utf-8"))
    validated = doc.get("validated_steps", []) if doc else []
    for step in ORDERED_WORK_STEPS:
        if step not in validated:
            return step
    return "activation"


def get_step_prompt(step: str) -> str:
    """Return the Telegram-ready FR prompt for `step`.

    Raises ValueError if `step` is not one of the 7 known step ids.
    """
    if step not in _STEP_PROMPTS:
        raise ValueError(
            f"Unknown step {step!r}; must be one of {list(_STEP_PROMPTS)}"
        )
    return _STEP_PROMPTS[step]


def record_step_completion(
    state_path: Path,
    step: str,
    artifact_paths: Optional[Iterable[Path]] = None,
) -> None:
    """Append `step` to validated_steps, update last_updated_at, advance status.

    Idempotent: if `step` is already in validated_steps, this is a no-op
    (status is not re-advanced, last_updated_at is not bumped).

    `artifact_paths` is recorded as informational metadata under
    commits[].artifacts; it is OPTIONAL — the agent passes it when it has
    just written/committed concrete files, an empty list/None is fine.

    Raises ValueError if `step` is not one of the 6 work-steps.
    (Step 7 'activation' is handled by scripts/activate-dept.sh, not here.)
    """
    if step not in STEP_TO_POST_STATUS:
        raise ValueError(
            f"Unknown work-step {step!r}; must be one of "
            f"{list(STEP_TO_POST_STATUS)}"
        )

    path = Path(state_path)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    validated = doc.setdefault("validated_steps", [])
    if step in validated:
        return  # idempotent

    validated.append(step)

    now = _now_iso()
    commit_row = {
        "step": step,
        "validated_at": now,
    }
    if artifact_paths:
        commit_row["artifacts"] = [str(p) for p in artifact_paths]
    doc.setdefault("commits", []).append(commit_row)

    # Advance status only forward.
    new_status = STEP_TO_POST_STATUS[step]
    cur = doc.get("status", "Idea")
    if cur in STATUSES_IN_ORDER and \
            STATUSES_IN_ORDER.index(new_status) > STATUSES_IN_ORDER.index(cur):
        doc["status"] = new_status

    doc["last_updated_at"] = now

    # Atomic write.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Fix 3 — Notion `[Approve / Edit / Ask agent to refine]` triplet helpers.
#
# Notion v5 lines 826-828 mandate that every step expose 3 operator actions:
#   [Approve mandate] [Edit] [Ask agent to refine]
# The triplet is the audit-trail spine: without it, every dept invents its
# own protocol and the audit log becomes inconsistent.
#
# `get_followup_prompt(step, operator_choice)` returns the 2nd-turn FR
# Bureau-de-Cadre formulation after the operator picks an option in turn 1.
# `record_edit_request`, `record_refine_request`, `record_approval` append
# entries to STATE.yaml::step_interactions for the audit trail.
# ---------------------------------------------------------------------------

ACTIONS = ("approve", "edit", "refine")

_FOLLOWUP_PROMPTS: dict[str, dict[str, str]] = {
    "mandate": {
        "1": (
            "Très bien, je pars sur un mandat resserré. Voici ma "
            "proposition en une phrase :\n\n"
            "> *« Je m'occupe d'une seule chose, très bien — à toi de me "
            "dire laquelle en 1 ligne. »*\n\n"
            "Tu **approuves** ce cadre, tu veux **éditer** la phrase, ou "
            "tu préfères que je te **propose une formulation plus précise** "
            "(refine) ?"
        ),
        "2": (
            "Compris, mandat équilibré. Voici ma proposition de cadre :\n\n"
            "> *« Je couvre 2 ou 3 sujets liés, sur un rythme régulier. »*\n\n"
            "Tu **approuves**, tu veux **éditer** ce cadre, ou tu "
            "préfères que je te **propose une version plus précise** (refine) ?"
        ),
        "3": (
            "Mandat large noté. Voici ma proposition de cadre :\n\n"
            "> *« Je joue un rôle transversal pour l'équipe, je connecte "
            "plusieurs flux. »*\n\n"
            "Tu **approuves**, tu veux **éditer**, ou tu préfères que je "
            "**refine** ?"
        ),
    },
    # Generic followup for other steps — produce the same triplet.
    # Steps that need step-specific copy can override here later.
}


def get_followup_prompt(step: str, operator_choice: str) -> str:
    """Return the 2nd-turn FR Bureau-de-Cadre prompt after the operator
    picks an option in turn 1.

    Always exposes the Approve/Edit/Refine triplet so the audit trail
    stays consistent across depts (Notion v5 lines 826-828).

    Raises ValueError if `step` is unknown.
    """
    if step not in _STEP_PROMPTS:
        raise ValueError(
            f"Unknown step {step!r}; must be one of {list(_STEP_PROMPTS)}"
        )

    step_followups = _FOLLOWUP_PROMPTS.get(step)
    if step_followups and operator_choice in step_followups:
        return step_followups[operator_choice]

    # Generic fallback — still surfaces the triplet so no dept misses it.
    return (
        f"Compris, option {operator_choice}. Je te formule ça précisément "
        f"dans la prochaine itération.\n\n"
        f"Tu **approuves** cette direction, tu veux **éditer** ma "
        f"proposition, ou tu préfères que je **refine** une nouvelle "
        f"version ?"
    )


def _append_interaction(
    state_path: Path,
    step: str,
    action: str,
    operator_text: Optional[str] = None,
) -> None:
    """Internal: append one entry to STATE.yaml::step_interactions (Fix 3).

    Audit-trail format:
      step_interactions:
        - step: mandate
          action: edit
          ts: 2026-05-21T10:00:00Z
          operator_text: "Préfère le mandat resserré mais ciblé sur Maya..."
    """
    if action not in ACTIONS:
        raise ValueError(
            f"Unknown action {action!r}; must be one of {ACTIONS}"
        )
    if step not in _STEP_PROMPTS:
        raise ValueError(
            f"Unknown step {step!r}; must be one of {list(_STEP_PROMPTS)}"
        )

    path = Path(state_path)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    entry: dict = {
        "step": step,
        "action": action,
        "ts": _now_iso(),
    }
    if operator_text is not None and operator_text != "":
        entry["operator_text"] = operator_text

    doc.setdefault("step_interactions", []).append(entry)
    doc["last_updated_at"] = entry["ts"]

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    tmp.replace(path)


def record_approval(state_path: Path, step: str) -> None:
    """Append an approve action to STATE.yaml::step_interactions.

    Note: this is the lightweight audit record for the [Approve] button;
    it does NOT itself advance validated_steps (that's
    record_step_completion's job, called after the artifact is written
    and committed).
    """
    _append_interaction(state_path, step, "approve")


def record_edit_request(state_path: Path, step: str, operator_text: str) -> None:
    """Append an edit action to STATE.yaml::step_interactions.

    `operator_text` is the verbatim edit the operator wrote (e.g. the
    rewritten mandate sentence). Stored for audit.
    """
    if not operator_text:
        raise ValueError("record_edit_request: operator_text must be non-empty")
    _append_interaction(state_path, step, "edit", operator_text=operator_text)


def record_refine_request(state_path: Path, step: str, reason: str) -> None:
    """Append a refine action to STATE.yaml::step_interactions.

    `reason` is the operator's short rationale ("too long", "missing
    forbidden list", etc.). Stored for audit so we can trace WHY the
    agent re-spun a proposal.
    """
    if not reason:
        raise ValueError("record_refine_request: reason must be non-empty")
    _append_interaction(state_path, step, "refine", operator_text=reason)


# ---------------------------------------------------------------------------
# Fix 2 — `announce_current_step` CLI entry point.
#
# Called from .claude/settings.json::hooks.SessionStart on first boot.
# Reads STATE.yaml, picks the next step, writes the FR prompt to
# .claude/queued-prompts/initial.md (which CLAUDE.md tells the agent to
# read and surface on Telegram on first turn).
#
# We do NOT inject directly into Telegram from the hook — too fragile
# (no chat_id known until first /start, no async event loop in a hook).
# The queued-prompt file is the durable handoff.
# ---------------------------------------------------------------------------

def announce_current_step(
    state_path: Path,
    *,
    queue_file: Optional[Path] = None,
) -> Path:
    """Write the current-step FR prompt to .claude/queued-prompts/initial.md.

    Args:
        state_path: path to onboarding/STATE.yaml.
        queue_file: where to write the prompt. Defaults to
            <repo_root>/.claude/queued-prompts/initial.md where repo_root
            is inferred as the parent of state_path.parent (i.e.
            parent of `onboarding/`).

    Returns the path of the written queue file.
    """
    state_path = Path(state_path).resolve()
    step = get_current_step(state_path)
    prompt = get_step_prompt(step)

    if queue_file is None:
        # state_path = <repo>/onboarding/STATE.yaml -> repo = state_path.parent.parent
        repo_root = state_path.parent.parent
        queue_file = repo_root / ".claude" / "queued-prompts" / "initial.md"

    queue_file = Path(queue_file)
    queue_file.parent.mkdir(parents=True, exist_ok=True)

    header = (
        f"<!-- auto-generated by skill_lib.auto_drive.announce_current_step "
        f"at {_now_iso()} -->\n"
        f"<!-- current_step: {step} -->\n\n"
    )
    queue_file.write_text(header + prompt + "\n", encoding="utf-8")
    return queue_file


def _cli_announce_current_step(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m skill_lib.auto_drive announce_current_step",
        description="Write the current-step FR prompt to .claude/queued-prompts/initial.md",
    )
    parser.add_argument("state_path", help="path to onboarding/STATE.yaml")
    parser.add_argument("--queue-file", default=None,
                        help="override target prompt file path")
    args = parser.parse_args(argv)
    queue = Path(args.queue_file) if args.queue_file else None
    out = announce_current_step(Path(args.state_path), queue_file=queue)
    print(str(out))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI dispatcher invoked by `python3 -m skill_lib.auto_drive ...`."""
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print(
            "usage: python3 -m skill_lib.auto_drive announce_current_step "
            "<state_path>",
            file=sys.stderr,
        )
        return 64

    sub = argv[0]
    rest = argv[1:]
    if sub == "announce_current_step":
        return _cli_announce_current_step(rest)

    print(f"unknown subcommand: {sub!r}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
