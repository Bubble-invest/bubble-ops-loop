"""
dept_registry.py — enumerate live vs a-eclore departments + concierges.

In disk mode: scans READ_FROM_DISK for `bubble-ops-*` subdirs (departments)
and unprefixed agent dirs (concierges: morty, claudette).

Each dept is classified by its onboarding/STATE.yaml::status:
  - "Live"                        -> live_departments
  - any other (Idea..Ready..)     -> agents_a_eclore
  - missing STATE.yaml            -> agents_a_eclore (status="Idea")

Concierges are always "Live" — they're persistent agents without layers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from console import settings
from console.services.state_yaml_reader import read_state_for_repo


# ── Concierges ──────────────────────────────────────────────────────────────
# Concierges are always-on agents that don't follow the dept layer lifecycle.
# Their dirs live alongside bubble-ops-* but without the prefix.
KNOWN_CONCIERGE_SLUGS: dict[str, str] = {
    "morty": "Morty",
    "claudette": "Claudette",
}


@dataclass(frozen=True)
class DeptSummary:
    slug: str
    display_name: str
    status: str          # "Live" | anything else
    validated_steps: List[str]
    host: str = "vps"    # "vps" | "local" — hybrid local/VPS agent (2026-06-11)

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


def _is_concierge_dir(dirname: str) -> bool:
    """True if the directory name matches a known concierge."""
    return dirname in KNOWN_CONCIERGE_SLUGS


def list_departments() -> List[DeptSummary]:
    """Return all known departments (sorted by slug)."""
    if not settings.disk_mode():
        return []

    root = settings.disk_root()
    if not root.exists():
        return []

    out: List[DeptSummary] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        # ── Departments (bubble-ops-* prefix) ──
        if child.name.startswith("bubble-ops-"):
            slug = child.name[len("bubble-ops-"):]
            # Decommissioned — not part of the active team (Joris 2026-06-09)
            if slug in ("cgp",):
                continue
            state = read_state_for_repo(child)
            if state is None:
                out.append(DeptSummary(
                    slug=slug, display_name=slug, status="Idea",
                    validated_steps=[]))
                continue
            out.append(DeptSummary(
                slug=state.get("slug", slug),
                display_name=state.get("display_name", slug),
                status=state.get("status", "Idea"),
                validated_steps=list(state.get("validated_steps", [])),
                # Hybrid local/VPS agent (2026-06-11): absent → "vps" (back-compat).
                host=state.get("host", "vps"),
            ))
        # ── Concierges (unprefixed, always Live) ──
        elif _is_concierge_dir(child.name):
            slug = child.name
            out.append(DeptSummary(
                slug=slug,
                display_name=KNOWN_CONCIERGE_SLUGS[slug],
                status="Live",
                validated_steps=list(KNOWN_CONCIERGE_SLUGS.keys()),
            ))
    return out


def live_departments() -> List[DeptSummary]:
    return [d for d in list_departments() if d.is_live]


def sidebar_agents() -> list:
    """Return live agents with latest activity summary for the sidebar.
    Departments first, then concierges."""
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
    """Get the latest activity summary line for an agent."""
    root = repo_path(slug)
    if root is None:
        return ""

    # ── Look for the most recent output across all layers ──
    outputs_dir = root / "outputs"
    if not outputs_dir.exists():
        return ""

    latest = ""
    latest_ts = ""
    pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})$")
    for date_dir in sorted(outputs_dir.iterdir(), reverse=True):
        if not date_dir.is_dir() or not pattern.match(date_dir.name):
            continue
        for layer_dir in sorted(date_dir.iterdir(), reverse=True):
            if not layer_dir.is_dir():
                continue
            summary_file = layer_dir / "summary.md"
            if summary_file.exists():
                text = summary_file.read_text(encoding="utf-8").strip()
                # Take first meaningful line
                for line in text.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        ts = f"{date_dir.name}/{layer_dir.name}"
                        if ts > latest_ts:
                            latest_ts = ts
                            latest = line[:120]
                        break
    return latest


def repo_path(slug: str) -> Optional[Path]:
    root = settings.disk_root()
    # Departments: bubble-ops-<slug>
    dept = root / f"bubble-ops-{slug}"
    if dept.exists():
        return dept.resolve()
    # Concierges: <slug> (unprefixed)
    concierge = root / slug
    if concierge.exists():
        return concierge.resolve()
    return None
