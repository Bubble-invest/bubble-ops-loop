"""
step_runners/dry_run.py — Refonte #1 of 3, Deliverable E.

Conversational runner for Step 6 (Tests / dry-run) per Notion v5 lines
925-946. Runs the full dry-run simulator at start(), humanizes the raw
report via `humanize_dry_run_report` (Deliverable D), and walks the
operator through the result with the Approve/Edit/Refine triplet —
respecting the Notion rule that FAILED never advances and WARNING only
advances on explicit operator acceptance.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

import yaml

from ..artifact_tests.dry_run_report import humanize_dry_run_report
from .base import Action, StepRunner, register_runner

STEP_NAME = "dry_run"


# Intent regexes (operator-facing French).
_APPROVE_RE = re.compile(
    r"\b(valid[eé]?s?|approuv[eé]?s?|accept[eé]?s?|ok|d'?accord|oui|go)\b",
    re.IGNORECASE,
)
_EDIT_RE = re.compile(r"\b(modifi[eé]?s?|édit|edit|corrig|réécris)\w*\b", re.IGNORECASE)
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


def _invoke_dry_run(dept_root: Path):
    """Run the full dry-run simulator. Isolated for test mocking.

    Returns a DryRunResult-like object exposing `.to_dict()`,
    `.overall_status` (str), and `.can_advance_to_ready` (bool).
    """
    # Lazy import: keep startup time small and let tests mock cleanly.
    from skill_lib.dry_run import run_dry_run_full
    return run_dry_run_full(
        dept_root=dept_root,
        operator_accepts_warnings=False,  # let the runner gate this
        seed=1,
    )


class DryRunRunner(StepRunner):
    """Step 6 runner — round-trip simulation + humanized Telegram report."""

    step_name = STEP_NAME

    def __init__(self) -> None:
        super().__init__()
        self._raw_report: Optional[dict] = None
        self._overall_status: str = "UNKNOWN"
        self._can_advance: bool = False
        self._humanized: Optional[str] = None
        self._operator_accepted: bool = False
        self._done: bool = False
        self._artifacts: List[Path] = []

    # ----- lifecycle -----

    def start(self, state_path: Path, dept_yaml_draft_path: Path) -> None:
        self.state_path = Path(state_path)
        self.dept_yaml_draft_path = Path(dept_yaml_draft_path)
        # Run the simulator (mockable in tests).
        dept_root = self.dept_yaml_draft_path.parent
        result = _invoke_dry_run(dept_root)
        raw = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        self._raw_report = raw
        self._overall_status = str(raw.get("overall_status", "UNKNOWN")).upper()
        self._can_advance = bool(raw.get("can_advance_to_ready", False))
        # Humanize once and cache.
        humanized = humanize_dry_run_report(raw)
        self._humanized = humanized.summary_md
        # If the report is all-green and the simulator already says we can
        # advance, mark current_status awaiting_validation (the operator
        # still has to confirm — Notion line 946 wants explicit acceptance).
        self._persist_progress(
            current_status="awaiting_validation",
            sub_artifacts=self._sub_artifact_snapshot(),
        )

    # ----- conversational surface -----

    def next_prompt(self) -> Optional[str]:
        if self._done:
            return None
        return self._humanized

    def on_answer(self, operator_text: str) -> Action:
        text = (operator_text or "").strip()
        if not text:
            return Action.CONTINUE

        # Failed: only refine / edit are meaningful. valide is rejected.
        if self._overall_status == "FAILED":
            if _REFINE_RE.search(text):
                return Action.REFINE
            if _EDIT_RE.search(text):
                return Action.EDIT
            # An "approve" attempt on a FAILED report is a no-op
            return Action.CONTINUE

        # Warning: APPROVE requires explicit acceptance ("valide" / "accepte")
        if self._overall_status == "WARNING":
            if _APPROVE_RE.search(text):
                self._operator_accepted = True
                self._mark_done()
                return Action.DONE
            if _REFINE_RE.search(text):
                return Action.REFINE
            if _EDIT_RE.search(text):
                return Action.EDIT
            return Action.CONTINUE

        # Passed: a simple ok/oui advances. refine/edit are still allowed
        # (operator may want to rerun for paranoia).
        if self._overall_status == "PASSED":
            if _APPROVE_RE.search(text):
                self._mark_done()
                return Action.DONE
            if _REFINE_RE.search(text):
                return Action.REFINE
            if _EDIT_RE.search(text):
                return Action.EDIT
            return Action.CONTINUE

        return Action.CONTINUE

    def is_done(self) -> bool:
        return self._done

    def artifacts_produced(self) -> List[Path]:
        return list(self._artifacts)

    # ----- internal -----

    def _sub_artifact_snapshot(self) -> List[dict]:
        # Sub-artifacts here = the dry-run run itself + the humanized report
        # (intentionally lightweight — the heavy lifting is in dry_run.py).
        return [
            {
                "id": f"dry_run_overall_{self._overall_status.lower()}",
                "type": "dry_run_report",
                "validated_at": _now_iso(),
            },
        ]

    def _mark_done(self) -> None:
        self._done = True
        self._persist_progress(
            current_status="validated",
            sub_artifacts=self._sub_artifact_snapshot(),
        )

    def _persist_progress(
        self,
        current_status: str,
        sub_artifacts: List[dict],
    ) -> None:
        if self.state_path is None:
            return
        doc = _read_yaml(self.state_path)
        progress = doc.setdefault("step_progress", {})
        progress[self.step_name] = {
            "sub_artifacts_validated": list(sub_artifacts),
            "current_substep": (
                None if current_status == "validated" else {
                    "type": "dry_run_report",
                    "draft_payload": {
                        "overall_status": self._overall_status,
                        "can_advance": self._can_advance,
                    },
                }
            ),
            "current_status": current_status,
        }
        doc["last_updated_at"] = _now_iso()
        _atomic_write_yaml(self.state_path, doc)


register_runner(STEP_NAME, DryRunRunner)
