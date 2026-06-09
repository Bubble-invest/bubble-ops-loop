"""
dept_registry.py — enumerate live vs a-eclore departments.

In disk mode: scans READ_FROM_DISK for `bubble-ops-*` subdirs.
In github mode: `gh api orgs/<org>/repos` filtered by prefix `bubble-ops-`.

Each dept is classified by its onboarding/STATE.yaml::status:
  - "Live"                        -> live_departments
  - any other (Idea..Ready..)     -> agents_a_eclore
  - missing STATE.yaml            -> agents_a_eclore (status="Idea")
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

from console import settings
from console.services.state_yaml_reader import read_state_for_repo


@dataclass(frozen=True)
class DeptSummary:
    slug: str
    display_name: str
    status: str
    validated_steps: List[str]
    total_steps: int = 6  # 6 work-steps per state.schema.yaml

    @property
    def percent_complete(self) -> int:
        if self.total_steps == 0:
            return 0
        return int(round(100 * len(self.validated_steps) / self.total_steps))

    @property
    def is_live(self) -> bool:
        return self.status == "Live"

    @property
    def is_ancien(self) -> bool:
        """Sprint Lifecycle: True iff the dept is no longer active (Cancelled or
        Retired terminal). Mutually exclusive with is_live and with
        a-eclore."""
        return self.status in {"Cancelled", "Retired"}

    @property
    def last_validated_step(self) -> Optional[str]:
        return self.validated_steps[-1] if self.validated_steps else None


def list_departments() -> List[DeptSummary]:
    """Return all known departments (sorted by slug)."""
    if not settings.disk_mode():
        # github mode is a follow-up (UX-5); for v1 we expose disk-mode only,
        # which is also what tests exercise. Return [] gracefully.
        return []

    root = settings.disk_root()
    if not root.exists():
        return []

    out: List[DeptSummary] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not child.name.startswith("bubble-ops-"):
            continue
        slug = child.name[len("bubble-ops-"):]
        # Decommissioned — not part of the active team ({{OPERATOR}} 2026-06-09)
        if slug in ("cgp",):
            continue
        state = read_state_for_repo(child)
        if state is None:
            # No STATE.yaml -> minimal idea-stage record
            out.append(DeptSummary(
                slug=slug, display_name=slug, status="Idea",
                validated_steps=[]))
            continue
        out.append(DeptSummary(
            slug=state.get("slug", slug),
            display_name=state.get("display_name", slug),
            status=state.get("status", "Idea"),
            validated_steps=list(state.get("validated_steps", [])),
        ))
    return out


def live_departments() -> List[DeptSummary]:
    return [d for d in list_departments() if d.is_live]


def sidebar_agents() -> list:
    """Return live agents with latest activity summary for the sidebar."""
    out = []
    for d in live_departments():
        summary = _latest_summary(d.slug)
        out.append({
            "slug": d.slug,
            "display_name": d.display_name,
            "summary": summary,
        })
    return out


def _latest_summary(slug: str) -> str:
    """Get the latest L1 summary line for a dept, or empty string."""
    root = repo_path(slug)
    if root is None:
        return ""
    out_dir = root / "outputs"
    if not out_dir.exists():
        return ""
    dates = sorted(
        [x.name for x in out_dir.iterdir() if x.is_dir() and x.name[:1].isdigit()],
        reverse=True,
    )
    if not dates:
        return ""
    # Try L1 summary.md first
    l1 = out_dir / dates[0] / "1" / "summary.md"
    if l1.exists():
        try:
            text = l1.read_text(encoding="utf-8")
            # Extract first substantive line (skip title lines starting with #)
            for line in text.split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    return line[:120]
        except OSError:
            pass
    # Fallback: last heartbeat line
    hb = out_dir / dates[0] / "heartbeat.log"
    if hb.exists():
        try:
            last = hb.read_text().strip().split("\n")[-1]
            return last[:120]
        except OSError:
            pass
    return "En attente du premier cycle"


def agents_a_eclore() -> List[DeptSummary]:
    """Depts mid-eclosure (NOT Live, NOT Cancelled, NOT Retired).

    Sprint Lifecycle (2026-05-21) — Cancelled + Retired depts move out of
    'à éclore' into 'Anciens collègues' (see anciens_collegues() below).
    """
    return [d for d in list_departments()
            if not d.is_live and not d.is_ancien]


def anciens_collegues() -> List[DeptSummary]:
    """Sprint Lifecycle: depts in terminal Cancelled/Retired status.

    Surfaced as a 3rd section of /agents ("Anciens collègues" — read-only
    archive). Empty most of the time; populated as the operator cancels /
    retires depts.
    """
    return [d for d in list_departments() if d.is_ancien]


def get_department(slug: str) -> Optional[DeptSummary]:
    for d in list_departments():
        if d.slug == slug:
            return d
    return None


def repo_path(slug: str) -> Optional[Path]:
    """Return on-disk path to bubble-ops-<slug>, or None."""
    if not settings.disk_mode():
        return None
    p = settings.disk_root() / f"bubble-ops-{slug}"
    return p if p.exists() else None
