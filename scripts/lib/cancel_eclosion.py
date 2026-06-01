#!/usr/bin/env python3
"""
cancel_eclosion.py — Sprint Lifecycle Deliverable A.

Used to abandon a department that NEVER reached `Live`. Distinct from
`retire_dept` which decommissions a `Live` dept.

Use cases:
  - During development, the operator tries an eclosure multiple times
    before the agent feels right.
  - Joris changes his mind mid-eclosure about the concept.

Side effects (mocked in tests; real in production):
  1. SSH to Morty: `systemctl disable --now ops-loop-<slug>.service` +
     remove the unit file (the dept never goes live again).
  2. `gh repo archive vdk888/bubble-ops-<slug>` — non-destructive, the
     repo and history survive (operator can restore via gh repo unarchive).
  3. Update STATE.yaml: status -> "Cancelled" + cancelled_at = <utc iso>.
  4. Emit operator-facing BotFather instructions (Telegram bot deletion
     cannot be automated by the BotFather API; the operator has to do it).

Strict doctrine:
  - We ARCHIVE, never destroy. History is preserved.
  - We never SSH unless the test mock allows it.
  - --dry-run computes the plan but performs NO side effects.

Public API:
    cancel_eclosion(slug, repo_dir, dry_run=False) -> dict
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import yaml


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import state_yaml  # noqa: E402


# GitHub org we publish dept repos under. Mirrors activate_runner.
DEFAULT_GH_ORG = "vdk888"

# The systemd unit pattern matches deploy-to-morty.sh.
UNIT_PATTERN = "ops-loop-{slug}.service"

# Default SSH target for Morty (matches deploy-to-morty.sh).
DEFAULT_REMOTE = os.environ.get("BUBBLE_MORTY_HOST", "claude@morty")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _botfather_instructions(slug: str) -> List[str]:
    """Build the operator-facing manual checklist for Telegram bot deletion.

    BotFather does not expose a 'delete bot' API method, so this part of
    cancel-eclosion is intentionally manual.
    """
    bot_handle_compact = slug.replace("-", "")
    return [
        f"1. Open Telegram -> @BotFather -> /mybots -> select "
        f"@bubbleops{bot_handle_compact}_bot",
        "2. Choose 'Delete bot' -> confirm with the handle when prompted",
        "3. (Optional) The conversation history is preserved on your side; "
        "the bot account itself disappears from BotFather.",
    ]


def _disable_morty_unit(slug: str, remote: str = DEFAULT_REMOTE
                        ) -> subprocess.CompletedProcess:
    """SSH to Morty and disable+remove the systemd unit (mocked in tests)."""
    unit = UNIT_PATTERN.format(slug=slug)
    cmd = [
        "ssh", remote,
        f"sudo systemctl disable --now {unit} || true; "
        f"sudo rm -f /etc/systemd/system/{unit}; "
        "sudo systemctl daemon-reload",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _archive_github_repo(slug: str, org: str = DEFAULT_GH_ORG
                         ) -> subprocess.CompletedProcess:
    """Mark the dept repo as archived on GitHub (non-destructive; mocked)."""
    repo_qualified = f"{org}/bubble-ops-{slug}"
    cmd = ["gh", "repo", "archive", repo_qualified, "--yes"]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _mark_state_cancelled(state_path: Path) -> dict:
    """Flip STATE.yaml::status to 'Cancelled' + stamp cancelled_at."""
    doc = state_yaml.load_state(state_path)
    now = _now_iso()
    doc["status"] = "Cancelled"
    doc["cancelled_at"] = now
    doc["last_updated_at"] = now
    state_yaml.save_state(state_path, doc)
    return doc


def cancel_eclosion(
    slug: str,
    repo_dir: Path,
    dry_run: bool = False,
) -> dict:
    """Cancel an in-flight eclosure for `<slug>` rooted at `repo_dir`.

    Returns {status, reasons, operator_instructions}.
      status              : 'cancelled' on success, 'blocked' otherwise.
      reasons             : list of human-readable blockers (empty on success).
      operator_instructions: list of BotFather steps the operator must
                            perform manually (always present, even on dry-run).
    """
    repo_dir = Path(repo_dir)
    reasons: List[str] = []

    # ---- Pre-flight 1: repo + STATE.yaml exist ----------------------------
    state_path = repo_dir / "onboarding" / "STATE.yaml"
    if not repo_dir.exists() or not repo_dir.is_dir():
        reasons.append(f"Department repo does not exist: {repo_dir}")
        return {"status": "blocked", "reasons": reasons,
                "operator_instructions": _botfather_instructions(slug)}
    if not state_path.exists():
        reasons.append(f"STATE.yaml not found: {state_path}")
        return {"status": "blocked", "reasons": reasons,
                "operator_instructions": _botfather_instructions(slug)}

    # ---- Pre-flight 2: status must NOT be Live ----------------------------
    state = state_yaml.load_state(state_path)
    current_status = state.get("status", "Idea")
    if current_status == "Live":
        reasons.append(
            f"Department is currently 'Live'. Use retire-dept instead "
            "(cancel-eclosion only applies to depts that never reached Live)."
        )
        return {"status": "blocked", "reasons": reasons,
                "operator_instructions": _botfather_instructions(slug)}
    if current_status == "Cancelled":
        reasons.append(
            f"Department is already Cancelled (cancelled_at="
            f"{state.get('cancelled_at', 'unknown')}). No-op."
        )
        return {"status": "blocked", "reasons": reasons,
                "operator_instructions": _botfather_instructions(slug)}

    instructions = _botfather_instructions(slug)

    # ---- Dry-run short-circuit -------------------------------------------
    if dry_run:
        return {
            "status": "cancelled",
            "reasons": [],
            "operator_instructions": instructions,
            "dry_run": True,
        }

    # ---- Side effect 1: disable systemd unit on Morty --------------------
    morty_result = _disable_morty_unit(slug)
    if morty_result.returncode != 0:
        # Non-fatal — log but proceed. The unit may already be gone.
        print(
            f"[cancel-eclosion] WARN: morty disable returned "
            f"{morty_result.returncode}: {morty_result.stderr.strip()[:200]}",
            file=sys.stderr,
        )

    # ---- Side effect 2: archive the GitHub repo --------------------------
    gh_result = _archive_github_repo(slug)
    if gh_result.returncode != 0:
        print(
            f"[cancel-eclosion] WARN: gh archive returned "
            f"{gh_result.returncode}: {gh_result.stderr.strip()[:200]}",
            file=sys.stderr,
        )

    # ---- Side effect 3: flip STATE.yaml ---------------------------------
    _mark_state_cancelled(state_path)

    return {
        "status": "cancelled",
        "reasons": [],
        "operator_instructions": instructions,
        "dry_run": False,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint (used by scripts/cancel-eclosion.sh).
# ---------------------------------------------------------------------------

def _format_summary(slug: str, result: dict) -> str:
    out: List[str] = []
    out.append("")
    out.append("=" * 60)
    if result["status"] == "cancelled":
        out.append(f"  Eclosure cancelled for: {slug}")
    else:
        out.append(f"  Cancellation BLOCKED for: {slug}")
    out.append("=" * 60)
    if result["reasons"]:
        out.append("")
        out.append("Reasons:")
        for r in result["reasons"]:
            out.append(f"  - {r}")
    out.append("")
    out.append("Operator follow-up (Telegram bot deletion via BotFather):")
    for line in result["operator_instructions"]:
        out.append(f"  {line}")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Cancel an in-flight eclosure (pre-Live only).",
    )
    p.add_argument("--slug", required=True)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()
    result = cancel_eclosion(
        slug=args.slug,
        repo_dir=repo_dir,
        dry_run=args.dry_run,
    )
    print(_format_summary(args.slug, result))
    return 0 if result["status"] == "cancelled" else 2


if __name__ == "__main__":
    sys.exit(main())
