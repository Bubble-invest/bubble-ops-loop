"""
pr_body.py — generate the activation PR body.

Thin wrapper around UX-1's `skill_lib.activation.build_activation_pr_body`
(no duplication). Adds UX-2-specific context such as the STATE.yaml summary.

Notion v5 lines 961-977.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

# Make the UX-1 skill_lib importable.
_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent  # .../projects/bubble-ops-loop
_SKILL_ROOT = _PROJECT_ROOT / "skills" / "department-onboarding-guide"
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from skill_lib.activation import build_activation_pr_body as _ux1_build  # noqa: E402


def build_pr_body(
    display_name: str,
    slug: str,
    validated_steps: List[str],
    commits: List[Dict],
    dry_run_status: str = "PASSED",
) -> str:
    """
    Build the activation PR body. Composes UX-1's body with extra context.
    """
    base = _ux1_build(
        display_name=display_name,
        slug=slug,
        validated_steps=validated_steps,
    )
    lines = [base.rstrip(), ""]
    lines.extend(
        [
            "## Onboarding commits",
            "",
        ]
    )
    if commits:
        for c in commits:
            sha = c.get("commit_sha", "?")
            step = c.get("step", "?")
            ts = c.get("validated_at", "?")
            who = c.get("validated_by", "?")
            lines.append(f"- `{sha}` step={step} by={who} at={ts}")
    else:
        lines.append("- (none recorded)")
    lines.extend(
        [
            "",
            "## Dry-run final status",
            "",
            f"- `{dry_run_status}`",
            "",
            "## Operator action",
            "",
            "- Review the changes; merge to flip the front-end card from "
            "`Agents a eclore` to `Live departments`.",
            "- Reminder: install the `bubble-ops-bot` GitHub App on this repo "
            "before merge if it isn't already (App ID 3782718; scoped per repo).",
        ]
    )
    return "\n".join(lines) + "\n"
