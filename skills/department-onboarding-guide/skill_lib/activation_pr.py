"""
activation_pr.py — UX-5 task 2.

Builds the activation PR body (Notion v5 lines 977-995) and opens the PR
via the real token broker + `gh` CLI. Subprocess-only — no requests, no
direct GitHub API calls. The broker call mints a short-lived
installation token; we inject it via GH_TOKEN env for the single `gh
pr create` call. Tests mock subprocess.run.

Public API:
  - build_activation_pr_body(dept_slug, state_yaml, dept_yaml) -> str
  - open_activation_pr(dept_slug, repo_url, branch, pr_title, pr_body,
                       broker_path, guard_path, ...) -> dict
  - ActivationPRError                — raised on broker or gh failure
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


class ActivationPRError(RuntimeError):
    """Raised when the broker mint or `gh pr create` invocation fails."""


# ---------------------------------------------------------------------------
# Build the PR body.
# ---------------------------------------------------------------------------


# Humanized French checklist (Bureau de Cadre, msg 2702/2708 2026-05-21).
# Each item is a concierge-style verification an operator can read and tick
# without knowing what `dept.yaml::department.status` or `sops` is.
_CHECKLIST_ITEMS = [
    "Son bureau est prêt à l'accueillir (l'équipe technique a déjà installé "
    "ce qu'il faut sur Morty)",
    "Ses identifiants sont rangés au coffre (env chiffré, "
    "`/etc/bubble/secrets-<slug>.sops.env`)",
    "Le canal de communication avec elle fonctionne "
    "(Telegram ping OK, Tailscale joignable)",
    "Le registre central reconnaît son arrivée "
    "(GitHub App `bubble-ops-bot` autorisée sur son repo)",
    "Tu as reçu les consignes pour la première conversation avec elle",
]

# Layer-number → human moment-of-day name (Bureau de Cadre vocabulary table).
# Kept in sync with console/templates/dept_detail.html and onboarding.html.
_LAYER_HUMAN_NAMES = {
    1: "Le matin",
    2: "La recherche",
    3: "L'exécution",
    4: "Le débrief du soir",
}

# Gate-policy mode → human phrasing for the activation preview.
# Mirrors the 5-mode doctrine (Notion v5 lines 254-260) with operator-facing
# copy. The enum slug stays available in a mono parenthetical so a reader can
# still cross-reference if they wish.
_MODE_HUMAN = {
    "manual_required": "Tu valides chaque fois",
    "manual_unless_policy_passed": "Tu valides sauf si elle respecte la règle",
    "auto_if_policy_passed": "Elle gère seule si la règle est OK",
    "auto_with_veto_window": "Elle agit, tu peux revenir dessus",
    "disabled": "désactivé",
}


def _render_missions_table(missions: List[dict]) -> str:
    """Render the dept's recurring missions in human prose (Bureau de Cadre).

    Was a markdown table with columns id/layer/cadence/creates/output_queue —
    now a French sentence list because the audience is non-technical
    (msg 2702/2708, 2026-05-21). The technical slug stays mono-quoted so a
    reader can cross-reference; the verb is human.
    """
    if not missions:
        return "_(aucune mission récurrente pour l'instant — c'est une équipe " \
               "qui réagit aux signaux, pas qui suit un calendrier)_"
    sentences = []
    for m in missions:
        if not isinstance(m, dict):
            continue
        mid = m.get("id", "?")
        cadence = m.get("cadence", "?")
        # Human cadence translation
        cadence_human = {
            "daily": "chaque jour",
            "hourly": "chaque heure",
            "weekly": "chaque semaine",
        }.get(cadence, cadence)
        sentences.append(f"- `{mid}` — {cadence_human}")
    return "\n".join(sentences)


def _render_layer_outputs(dept_yaml: dict) -> str:
    """Render the 4 moments of the day in human language.

    Was "**Layer N** (subscribed) — skills: X" — now uses the moment-of-day
    names from `_LAYER_HUMAN_NAMES` per Bureau de Cadre vocabulary table.
    Non-subscribed moments render in italic muted phrasing so the operator
    knows the colleague doesn't intervene there yet.
    """
    subscribed = (dept_yaml.get("layers") or {}).get("subscribed", []) or []
    skills = dept_yaml.get("skills") or {}
    lines = []
    for n in (1, 2, 3, 4):
        human_name = _LAYER_HUMAN_NAMES[n]
        present = n in subscribed
        per_layer_skills = skills.get(f"layer_{n}") or []
        if present:
            sk = ", ".join(f"`{s}`" for s in per_layer_skills) or \
                 "_(savoir-faire à préciser)_"
            lines.append(f"- **{human_name}** — elle s'appuie sur : {sk}")
        else:
            lines.append(
                f"- _{human_name} — elle n'intervient pas à ce moment-là._"
            )
    return "\n".join(lines)


def _render_gate_policy_summary(dept_yaml: dict) -> str:
    """Render gate policies in human prose with the 5-mode doctrine translated.

    Each policy = one bullet. The current mode is shown human-first
    ("Tu valides chaque fois") with the technical enum in a small mono
    parenthetical for cross-reference. Future-eligible modes get the same
    treatment so the operator can see what autonomy ladder the colleague
    could climb later.
    """
    policies = dept_yaml.get("gate_policies") or {}
    if not policies:
        return "_(aucune décision particulière à encadrer pour l'instant.)_"
    lines = []
    for pid, p in policies.items():
        if not isinstance(p, dict):
            continue
        cur = p.get("current_mode", "?")
        cur_human = _MODE_HUMAN.get(cur, cur)
        future = p.get("eligible_future_modes") or []
        if future:
            future_humans = [
                f"« {_MODE_HUMAN.get(f, f)} » (`{f}`)" for f in future
            ]
            future_s = ", ".join(future_humans)
            lines.append(
                f"- Sur `{pid}` : **{cur_human}** _(mode `{cur}`)_. "
                f"Elle pourrait apprendre : {future_s}."
            )
        else:
            lines.append(
                f"- Sur `{pid}` : **{cur_human}** _(mode `{cur}`)_. "
                f"Pas de palier d'autonomie supérieur prévu pour l'instant."
            )
    return "\n".join(lines)


def _render_dry_run_result(state_yaml: dict) -> str:
    """Surface dry-run signals as a human "répétition à blanc" report.

    Per Notion v5 line 988 we want PASS/WARN + last-run ts. STATE.yaml records
    validated steps + commits; canonical dry-run output lives at
    outputs/dry-run/<ts>/. We translate the technical "validated YES/NO" into
    Bureau de Cadre prose (msg 2702/2708, 2026-05-21).
    """
    validated = state_yaml.get("validated_steps", []) or []
    dr_validated = "dry_run" in validated
    commits = state_yaml.get("commits", []) or []
    dr_commit = next(
        (c for c in commits if isinstance(c, dict)
         and c.get("step") == "dry_run"), None)
    when = dr_commit.get("validated_at") if dr_commit else None
    if dr_validated:
        line1 = "- Elle a fait sa répétition à blanc et elle est passée."
        line2 = f"- Dernière répétition validée : {when or '_(date non notée)_'}."
    else:
        line1 = "- **Elle n'a pas encore fait sa répétition à blanc.**"
        line2 = ("- C'est l'étape juste avant la cérémonie d'arrivée — "
                 "elle ne peut pas être sautée.")
    line3 = (
        "- Si tu veux refaire une répétition avant d'envoyer la lettre, "
        "lance `scripts/run-dry-run.sh --slug <slug>`."
    )
    return "\n".join([line1, line2, line3])


def _render_risk_notes(state_yaml: dict) -> Optional[str]:
    notes = state_yaml.get("risk_notes")
    if not notes:
        return None
    if isinstance(notes, str):
        notes = [notes]
    lines = [f"- {n}" for n in notes if n]
    if not lines:
        return None
    return "\n".join(lines)


def build_activation_pr_body(
    dept_slug: str,
    state_yaml: dict,
    dept_yaml: dict,
) -> str:
    """Build the activation PR body per Notion v5 lines 977-995."""
    display_name = (state_yaml.get("display_name")
                    or dept_yaml.get("department", {}).get("display_name")
                    or dept_slug.capitalize())
    mandate = ((dept_yaml.get("department") or {}).get("mandate")
               or "_(no mandate declared)_")
    missions = dept_yaml.get("recurring_missions") or []

    blocks: List[str] = []
    # H1: "Lettre d'arrivée de [Nom]" (Bureau de Cadre, msg 2702/2708)
    blocks.append(f"# Lettre d'arrivée de {display_name}")
    # Branch explanation in human prose — no .yaml::field paths
    blocks.append(
        f"Une fois cette lettre acceptée par l'équipe, {display_name} "
        f"rejoint officiellement l'équipe et passe d'éclosion à en poste."
    )

    blocks.append("## Sa mission")
    blocks.append(mandate)

    blocks.append("## Ce qu'elle fera chaque jour")
    blocks.append(_render_missions_table(missions))

    blocks.append("## Ses 4 moments de la journée")
    blocks.append(_render_layer_outputs(dept_yaml))

    blocks.append("## Les décisions qu'elle prend")
    blocks.append(_render_gate_policy_summary(dept_yaml))

    blocks.append("## Sa répétition à blanc")
    blocks.append(_render_dry_run_result(state_yaml))

    blocks.append("## Ce qu'il faut vérifier avant la cérémonie")
    checklist = "\n".join(f"- [ ] {item}" for item in _CHECKLIST_ITEMS)
    blocks.append(
        "Avant d'envoyer la lettre, prends une minute pour cocher chacun "
        "de ces points :\n\n"
        + checklist
    )

    risk_notes = _render_risk_notes(state_yaml)
    if risk_notes is not None:
        blocks.append("## Points d'attention")
        blocks.append(risk_notes)

    blocks.append("---")
    blocks.append(
        "_Composée par `skills/department-onboarding-guide/skill_lib/"
        "activation_pr.py` — chaque arrivée passe par ce registre._"
    )

    return "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Open the PR.
# ---------------------------------------------------------------------------


_PR_URL_RE = re.compile(r"/pull/(\d+)")


def _extract_pr_number(gh_stdout: str) -> Optional[int]:
    m = _PR_URL_RE.search(gh_stdout)
    if m:
        return int(m.group(1))
    return None


def open_activation_pr(
    dept_slug: str,
    repo_url: str,
    branch: str,
    pr_title: str,
    pr_body: str,
    broker_path: str,
    guard_path: str,
    base_branch: str = "main",
    broker_audit_log: Optional[str] = None,
) -> Dict[str, Any]:
    """Mint a short-lived install token via the broker, then open the PR.

    Tests mock subprocess.run. No real GitHub call is made unless a real
    broker + gh are on PATH.

    Raises ActivationPRError on broker or gh failure.
    """
    # ---- Mint token --------------------------------------------------
    mint_cmd = [
        broker_path, "mint",
        "--dept", dept_slug,
        "--action", "open_priority_pr",
        "--repo", _repo_short(repo_url),
    ]
    if broker_audit_log:
        mint_cmd.extend(["--audit-log", broker_audit_log])
    res = subprocess.run(
        mint_cmd, capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        raise ActivationPRError(
            f"broker mint failed (rc={res.returncode}): "
            f"{(res.stderr or res.stdout).strip()[:240]}"
        )
    token = (res.stdout or "").strip()
    if not token:
        raise ActivationPRError("broker mint returned empty token")

    # ---- Open PR ------------------------------------------------------
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    gh_cmd = [
        "gh", "pr", "create",
        "--repo", _repo_short(repo_url),
        "--base", base_branch,
        "--head", branch,
        "--title", pr_title,
        "--body", pr_body,
    ]
    try:
        res = subprocess.run(
            gh_cmd, capture_output=True, text=True, check=False, env=env,
        )
    finally:
        # Scrub token from env dict to limit accidental leaks.
        env.pop("GH_TOKEN", None)

    if res.returncode != 0:
        raise ActivationPRError(
            f"gh pr create failed (rc={res.returncode}): "
            f"{(res.stderr or res.stdout).strip()[:240]}"
        )
    url = (res.stdout or "").strip()
    pr_number = _extract_pr_number(url) or -1
    return {"pr_number": pr_number, "url": url, "branch": branch}


def _repo_short(repo_url: str) -> str:
    """Convert https://github.com/vdk888/bubble-ops-miranda(.git) -> vdk888/bubble-ops-miranda."""
    s = repo_url.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    # Strip protocol + host
    for prefix in ("https://github.com/", "http://github.com/",
                   "git@github.com:"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s
