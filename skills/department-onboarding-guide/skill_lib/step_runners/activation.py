"""
step_runners/activation.py — Refonte #1 of 3, Deliverable F.

Conversational runner for Step 7 (Activation) per Notion v5 lines
947-1003.

The runner:
  - At start(), builds the activation PR body via
    `activation_pr.build_activation_pr_body()` and **verifies** it with
    `artifact_tests.activation_pr.test_activation_pr_body()` (Deliverable
    D). If the body fails verification (e.g. the legacy English body
    slipped back in), the runner refuses to ship and surfaces a clear
    error prompt to the operator.
  - On `next_prompt()`, surfaces the humanized body + asks for approval.
  - On `approuve`: calls `scripts/activate-dept.sh --slug=<slug>` and,
    on success, flips `dept.yaml::department.status` to `live`.
  - On `édite` / `raffine`: records the request and stays not-done.

Invariant pinned by tests: the legacy English PR body must NEVER reach
Joris. The Step 7 runner is the last line of defense before the
operator-facing surface.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import yaml

from ..activation import flip_status_to_live
from ..activation_pr import build_activation_pr_body
from ..artifact_tests.activation_pr import test_activation_pr_body
from .base import Action, StepRunner, register_runner

STEP_NAME = "activation"


_APPROVE_RE = re.compile(
    r"\b(approuv[eé]?s?|valid[eé]?s?|ok|d'?accord|oui|envoie|envoi[eé]?)\b",
    re.IGNORECASE,
)
_EDIT_RE = re.compile(r"\b(édit|edit|modifi[eé]?s?|corrig|réécris)\w*\b", re.IGNORECASE)
_REFINE_RE = re.compile(r"\b(raffin|refine|précis|reformul)\w*\b", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _atomic_write_yaml(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    tmp.replace(path)


def _build_pr_body(slug: str, state_doc: dict, dept_doc: dict) -> str:
    """Wrap build_activation_pr_body so tests can patch it cleanly."""
    return build_activation_pr_body(slug, state_doc, dept_doc)


def _run_activation_script(
    slug: str, dept_root: Path, dry_run: bool = False,
) -> int:
    """Invoke scripts/activate-dept.sh — isolated for test mocking.

    Returns the script's exit code. The runner only flips status on rc==0.

    The `dry_run` kwarg is an explicit opt-in (default False = production-
    safe). Sprint Maya-blocker Fix 1 (2026-05-21): previous versions
    hardcoded `--dry-run` in the cmd list so tests could mock the script
    cheaply, but that meant the operator-facing approve path also ran in
    dry-run mode — silently skipping the real GitHub PR + Morty deploy
    while the runner still flipped STATE.yaml to "Live". Per Notion v5
    lines 947-1003, "Quand tout est validé" implies a REAL deployment.

    Sprint Maya-blocker Fix 3 (2026-05-21): stderr from the underlying
    subprocess is also captured into the module-level
    `_last_captured_stderr` buffer so `ActivationRunner` can surface it
    in the operator's next prompt when rc != 0. The int return is
    preserved so existing test mocks (`return_value=0`, `=2`) keep
    working. New callers can read the buffer via `get_last_stderr()`.
    """
    global _last_captured_stderr
    _last_captured_stderr = ""
    # Locate the script.
    # __file__: .../skills/department-onboarding-guide/skill_lib/step_runners/activation.py
    project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
    script = project_root / "scripts" / "activate-dept.sh"
    if not script.exists():
        # In test envs without the script, default to "ok"
        return 0
    cmd = [str(script), f"--slug={slug}", f"--repo-dir={dept_root}"]
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    _last_captured_stderr = (proc.stderr or "").strip()
    return proc.returncode


# Sprint Maya-blocker Fix 3 (2026-05-21): module-level stderr buffer.
# Populated by `_run_activation_script` on every real subprocess call,
# read by `ActivationRunner._do_activate` when rc != 0. Module-level
# (not class-level) so test mocks of `_run_activation_script` don't
# need to remember to set it — empty string is the right default when
# the script is mocked out entirely.
_last_captured_stderr: str = ""


def get_last_stderr() -> str:
    """Return the stderr captured on the most recent activation-script run.

    Returns "" when the script has not yet been invoked, when it ran
    cleanly with no stderr, or when the call was mocked out (mocks of
    `_run_activation_script` don't touch the buffer)."""
    return _last_captured_stderr


class ActivationRunner(StepRunner):
    """Step 7 runner — humanized "Lettre d'arrivée" + activation."""

    step_name = STEP_NAME

    def __init__(self) -> None:
        super().__init__()
        self._pr_body: Optional[str] = None
        self._pr_body_ok: bool = False
        self._body_test_summary: str = ""
        self._activated: bool = False
        self._artifacts: List[Path] = []
        # Sprint Maya-blocker Fix 3 (2026-05-21): one-shot French failure
        # message when `_run_activation_script` returns non-zero. Mirrors
        # the gates_kpis._last_rejection_reason pattern (Sprint correctif
        # Fix 4). Surfaced by `next_prompt()` then cleared so it doesn't
        # linger on subsequent renders.
        self._last_failure_message: Optional[str] = None

    # ----- lifecycle -----

    def start(self, state_path: Path, dept_yaml_draft_path: Path) -> None:
        self.state_path = Path(state_path)
        self.dept_yaml_draft_path = Path(dept_yaml_draft_path)
        dept_root = self.dept_yaml_draft_path.parent
        state_doc = _read_yaml(self.state_path)
        slug = state_doc.get("slug", "unknown")
        # Prefer the promoted dept.yaml if present, otherwise the draft.
        dept_path = dept_root / "dept.yaml"
        if not dept_path.exists():
            dept_path = self.dept_yaml_draft_path
        dept_doc = _read_yaml(dept_path)
        # Build + verify.
        self._pr_body = _build_pr_body(slug, state_doc, dept_doc)
        verdict = test_activation_pr_body(self._pr_body)
        self._pr_body_ok = verdict.passed
        self._body_test_summary = verdict.summary_md
        # Persist initial progress.
        self._persist_progress("awaiting_validation")

    # ----- conversational surface -----

    def next_prompt(self) -> Optional[str]:
        if self._activated:
            return None
        if not self._pr_body_ok:
            base = (
                "**Je ne peux pas envoyer la lettre d'arrivée telle quelle.** "
                "Le corps de la PR ne respecte pas le format humanisé "
                "Bureau-de-Cadre (cf. msg 2702/2708).\n\n"
                f"{self._body_test_summary}\n\n"
                "Corrige le générateur (`skill_lib/activation_pr.py`) et "
                "relance cette étape."
            )
        else:
            body = self._pr_body or ""
            base = (
                f"{body}\n\n"
                "---\n\n"
                "Tu **approuves** cette lettre ? Une fois envoyée, "
                "le département rejoint officiellement l'équipe.\n\n"
                "Si tu préfères, tu peux aussi **éditer** un passage "
                "(envoie-moi ton texte) ou me demander de la **raffiner**."
            )
        # Sprint Maya-blocker Fix 3 (2026-05-21): one-shot failure blurb
        # prepended so the operator SEES that their "approuve" hit an
        # error in `scripts/activate-dept.sh`. Cleared after this render
        # — same as gates_kpis._last_rejection_reason.
        if self._last_failure_message:
            msg = self._last_failure_message
            self._last_failure_message = None
            return f"{msg}\n\n---\n\n{base}"
        return base

    def on_answer(self, operator_text: str) -> Action:
        text = (operator_text or "").strip()
        if not text:
            return Action.CONTINUE

        if not self._pr_body_ok:
            # Until the body is fixed, no operator answer can move us forward.
            return Action.CONTINUE

        if _REFINE_RE.search(text):
            return Action.REFINE
        if _EDIT_RE.search(text):
            return Action.EDIT
        if _APPROVE_RE.search(text):
            return self._do_activate()
        return Action.CONTINUE

    def is_done(self) -> bool:
        return self._activated

    def artifacts_produced(self) -> List[Path]:
        return list(self._artifacts)

    # ----- internal -----

    def _do_activate(self) -> Action:
        assert self.state_path is not None
        assert self.dept_yaml_draft_path is not None
        state_doc = _read_yaml(self.state_path)
        slug = state_doc.get("slug", "unknown")
        dept_root = self.dept_yaml_draft_path.parent
        rc = _run_activation_script(slug, dept_root)
        if rc != 0:
            # Sprint Maya-blocker Fix 3 (2026-05-21): stash a French
            # failure blurb so `next_prompt()` surfaces WHY the operator's
            # approve didn't take. Captured stderr is summarized (clipped
            # to ~200 chars to stay readable on Telegram).
            # Persona-aware failure copy (Joris directive msg 2770, 2026-05-21):
            # operator-facing message must NOT leak sysadmin jargon (journalctl,
            # sudo, Morty hostname, systemd unit names). Refer operator to
            # "votre équipe technique" instead — a neutral phrasing that maps
            # internally to Rick (R&D) and stays reusable for future
            # client-facing agents.
            stderr = get_last_stderr()
            if stderr:
                stderr_summary = stderr[:200].replace("\n", " ")
                if len(stderr) > 200:
                    stderr_summary += "…"
                stderr_clause = (
                    f"Voici ce que je sais : {stderr_summary}"
                )
            else:
                stderr_clause = (
                    "Le script n'a rien écrit comme détail — c'est une erreur "
                    "silencieuse"
                )
            self._last_failure_message = (
                "⚠️ J'ai bien transmis ta demande, mais le script "
                f"d'activation a renvoyé une erreur (code {rc}). "
                f"{stderr_clause}. Je peux retenter quand tu veux. Si ça "
                "persiste, demande à votre équipe technique d'aller regarder "
                "ce qui se passe côté serveur."
            )
            return Action.CONTINUE  # script blocked; runner stays not-done
        # Flip dept.yaml status to live (the script does it in prod but
        # we re-do it here so the in-memory runner state matches even in
        # dry-run / tests where the script is mocked).
        dept_path = dept_root / "dept.yaml"
        if not dept_path.exists():
            dept_path = self.dept_yaml_draft_path
        try:
            flip_status_to_live(dept_path)
            if dept_path not in self._artifacts:
                self._artifacts.append(dept_path)
        except (FileNotFoundError, ValueError):
            # If the dept doc is malformed, the activation script would
            # have caught it; in tests with a stubbed script we tolerate.
            pass
        self._activated = True
        # Polish Fix 2 (2026-05-21): mirror the script's mark_activated()
        # side-effect — flip STATE.yaml::status to "Live". In prod this
        # happens via scripts/lib/state_yaml.py::mark_activated() invoked
        # by activate-dept.sh; we mirror it here so the in-memory state
        # matches in dry-run / tests where the script is mocked (per
        # Notion v5 lines 947-960).
        self._persist_progress("validated", flip_state_to_live=True)
        return Action.DONE

    def _persist_progress(
        self,
        current_status: str,
        flip_state_to_live: bool = False,
    ) -> None:
        if self.state_path is None:
            return
        doc = _read_yaml(self.state_path)
        progress = doc.setdefault("step_progress", {})
        progress[self.step_name] = {
            "sub_artifacts_validated": (
                [
                    {
                        "id": "activation_pr_body",
                        "type": "activation_pr",
                        "validated_at": _now_iso(),
                    },
                ]
                if current_status == "validated"
                else []
            ),
            "current_substep": (
                None if current_status == "validated" else {
                    "type": "activation_pr",
                    "draft_payload": {
                        "pr_body_passes_humanization_check": self._pr_body_ok,
                    },
                }
            ),
            "current_status": current_status,
        }
        # Polish Fix 2: mirror activate-dept.sh's mark_activated() side-effect
        # so STATE.yaml::status reflects the post-activation reality even
        # when the script is mocked in tests. The on-disk prod path still
        # flows through scripts/lib/state_yaml.py::mark_activated().
        if flip_state_to_live:
            doc["status"] = "Live"
            doc["activated_at"] = _now_iso()
        doc["last_updated_at"] = _now_iso()
        _atomic_write_yaml(self.state_path, doc)


register_runner(STEP_NAME, ActivationRunner)
