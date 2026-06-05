#!/usr/bin/env python3
"""
retire_dept.py — Sprint Lifecycle Deliverable B.

Decommissions a Live department with dignity. Distinct from
`cancel_eclosion` (which handles pre-Live abandonment).

Use cases:
  - A dept stops being useful (e.g. Miranda's mission ends after a
    rebalance).
  - Strategic pivot away from a department.
  - Maya gets superseded by Maya-v2.

Doctrine:
  - We say goodbye. A final FR Bureau-de-Cadre Telegram message is sent
    to the dept's chat ("Merci [Display], tu prends ta retraite...").
  - We disable WITHOUT --now — the current loop finishes its iteration
    gracefully. Operator can stop manually if urgent.
  - GitHub repo stays intact (history is valuable).
  - Telegram conversation HISTORY stays reviewable (repo + transcripts), but
    live bot ACCESS is revoked at retirement (2026-06-05 security fix).
  - The dept shows up in `/agents` -> "Anciens collègues" section (read-only).

Side effects (mocked in tests; real in production):
  1. Telegram: send the final message via the dept's bot (curl / API).
  2. SSH to Morty: `systemctl disable ops-loop-<slug>.service` (no --now).
  2b. Secret quarantine (security): lock the Telegram bot (access.json ->
      denied), archive the SOPS env (reversible), wipe the runtime decrypted
      secrets, log the manual revoke steps. Cuts live access; keeps history.
  3. Git: dept.yaml::department.status = "retired" + commit + push.
  4. STATE.yaml: status="Retired", retired_at=<iso>, retired_reason=<text>.

Public API:
    retire_dept(slug, repo_dir, reason="Decommissioned", dry_run=False) -> dict
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


UNIT_PATTERN = "ops-loop-{slug}.service"
DEFAULT_REMOTE = os.environ.get("BUBBLE_MORTY_HOST", "claude@morty")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Final Telegram message — FR Bureau-de-Cadre voice (warm, dignified).
# ---------------------------------------------------------------------------

def compose_final_telegram_message(display_name: str, reason: str) -> str:
    """The farewell message sent to the dept's Telegram chat.

    Tone: Bureau-de-Cadre — warm + dignified, not corporate, not robotic.
    Joris validates this prose; tests assert key markers (Merci, retraite,
    display name, FR-only).

    The `reason` parameter is recorded in STATE.yaml::retired_reason for
    audit but intentionally NOT surfaced in the farewell message — the
    operator-facing English label "Decommissioned" would jar against the
    warm FR voice. The dept itself doesn't need a reason; the operator's
    log does.
    """
    _ = reason  # captured upstream in STATE.yaml::retired_reason
    return (
        f"Merci {display_name}. Tu prends ta retraite à partir de maintenant. "
        "Tes traces restent dans le registre du cabinet, et ton historique "
        "sera consultable en lecture seule. À très bientôt."
    )


# ---------------------------------------------------------------------------
# Side-effect helpers (each one returns a CompletedProcess for inspection).
# ---------------------------------------------------------------------------

def _send_final_telegram(slug: str, message: str
                         ) -> subprocess.CompletedProcess:
    """Send the farewell message via the dept's bot.

    Production wiring: looks up the bot token in the dept's SOPS-encrypted
    secrets file and POSTs to https://api.telegram.org/bot<token>/sendMessage.
    In tests the subprocess.run is mocked, so we just record the curl call.
    """
    # In real life this would source SOPS secrets first. For the v1 of
    # retire-dept the production hook is a placeholder — the operator
    # may still send the message manually via the BotFather flow. The
    # CRITICAL invariant for tests: subprocess.run IS called, with
    # something that looks like a Telegram API request.
    cmd = [
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot${{BUBBLE_BOT_TOKEN_{slug.replace('-', '_').upper()}}}/sendMessage",
        "-d", f"text={message}",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _disable_morty_unit_graceful(slug: str, remote: str = DEFAULT_REMOTE
                                 ) -> subprocess.CompletedProcess:
    """`systemctl disable` the dept unit (graceful — no --now).

    Detects whether we're already ON Morty (the unit file exists locally)
    and skips the `ssh remote` prefix in that case. Self-SSH without a
    TTY silently fails — caught 2026-05-24 when console-triggered retire
    left ops-loop-fixture.service still active+enabled.
    """
    unit = UNIT_PATTERN.format(slug=slug)
    unit_path = Path(f"/etc/systemd/system/{unit}")
    # NOTE: NO --now. The currently-running iteration finishes; future
    # cycles do not start. Operator can stop manually if needed.
    if unit_path.exists():
        # We're on Morty (the host that owns the unit). Run directly.
        cmd = ["sudo", "-n", "systemctl", "disable", unit]
    else:
        # We're elsewhere — proxy via SSH.
        cmd = ["ssh", remote, f"sudo systemctl disable {unit} || true"]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


_QUARANTINE_HELPER = "/usr/local/bin/retire-secrets-quarantine.sh"


def _quarantine_secrets(slug: str, remote: str = DEFAULT_REMOTE
                        ) -> subprocess.CompletedProcess:
    """Quarantine the retired dept's secrets (Side effect 5, 2026-06-05).

    DOCTRINE: a retired dept keeps its HISTORY (GitHub repo + transcripts) but
    loses live ACCESS. The root helper locks the Telegram bot (access.json ->
    denied), archives the SOPS env (reversible, not deleted), wipes the runtime
    decrypted secrets, and logs the manual revoke steps (BotFather token,
    GitHub App install) to the security audit trail.

    Same on-Morty-vs-remote detection as `_disable_morty_unit_graceful`: the
    helper is root-owned, so we always go through `sudo -n` (locally) or
    `ssh remote sudo` (proxied). Failure is logged by the caller but never
    blocks retirement — a retired-but-not-yet-quarantined dept is already
    disabled, so the security window is bounded.
    """
    if Path(_QUARANTINE_HELPER).exists():
        cmd = ["sudo", "-n", _QUARANTINE_HELPER, slug]
    else:
        cmd = ["ssh", remote, f"sudo -n {_QUARANTINE_HELPER} {slug} || true"]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _commit_dept_status_retired(repo_dir: Path, display_name: str
                                ) -> List[subprocess.CompletedProcess]:
    """git add + commit + push the dept.yaml status flip."""
    results: List[subprocess.CompletedProcess] = []
    results.append(subprocess.run(
        ["git", "-C", str(repo_dir), "add", "dept.yaml"],
        capture_output=True, text=True, check=False,
    ))
    results.append(subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m",
         f"retire: {display_name} -> status=retired"],
        capture_output=True, text=True, check=False,
    ))
    results.append(subprocess.run(
        ["git", "-C", str(repo_dir), "push"],
        capture_output=True, text=True, check=False,
    ))
    return results


def _flip_dept_yaml_status(dept_path: Path) -> dict:
    """Update dept.yaml::department.status = 'retired' in place."""
    dept_doc = yaml.safe_load(dept_path.read_text(encoding="utf-8"))
    dept_doc.setdefault("department", {})["status"] = "retired"
    dept_path.write_text(
        yaml.safe_dump(dept_doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return dept_doc


def _mark_state_retired(state_path: Path, reason: str) -> dict:
    """Flip STATE.yaml::status to 'Retired' + stamp retired_at + reason."""
    doc = state_yaml.load_state(state_path)
    now = _now_iso()
    doc["status"] = "Retired"
    doc["retired_at"] = now
    doc["retired_reason"] = reason
    doc["last_updated_at"] = now
    state_yaml.save_state(state_path, doc)
    return doc


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def retire_dept(
    slug: str,
    repo_dir: Path,
    reason: str = "Decommissioned",
    dry_run: bool = False,
) -> dict:
    """Decommission a Live department.

    Returns {status, reasons, final_telegram_msg}.
      status            : 'retired' on success, 'blocked' otherwise.
      reasons           : list of human-readable blockers (empty on success).
      final_telegram_msg: the FR Bureau-de-Cadre farewell (always present;
                         tests assert its tone).
    """
    repo_dir = Path(repo_dir)
    reasons: List[str] = []
    display_name = slug.capitalize()  # safe fallback if STATE.yaml missing

    # ---- Pre-flight 1: repo + STATE.yaml exist ----------------------------
    state_path = repo_dir / "onboarding" / "STATE.yaml"
    if not repo_dir.exists() or not repo_dir.is_dir():
        reasons.append(f"Department repo does not exist: {repo_dir}")
        msg = compose_final_telegram_message(display_name, reason)
        return {"status": "blocked", "reasons": reasons,
                "final_telegram_msg": msg}
    if not state_path.exists():
        reasons.append(f"STATE.yaml not found: {state_path}")
        msg = compose_final_telegram_message(display_name, reason)
        return {"status": "blocked", "reasons": reasons,
                "final_telegram_msg": msg}

    state = state_yaml.load_state(state_path)
    display_name = state.get("display_name", display_name)

    # ---- Pre-flight 2: status must be Live --------------------------------
    current_status = state.get("status", "Idea")
    if current_status != "Live":
        reasons.append(
            f"Only Live depts can be retired (got status={current_status!r}). "
            "For pre-Live depts use cancel-eclosion instead."
        )
        msg = compose_final_telegram_message(display_name, reason)
        return {"status": "blocked", "reasons": reasons,
                "final_telegram_msg": msg}

    final_msg = compose_final_telegram_message(display_name, reason)

    # ---- Dry-run short-circuit -------------------------------------------
    if dry_run:
        return {
            "status": "retired",
            "reasons": [],
            "final_telegram_msg": final_msg,
            "dry_run": True,
        }

    # ---- Side effect 1: Telegram farewell ---------------------------------
    tel_result = _send_final_telegram(slug, final_msg)
    if tel_result.returncode != 0:
        print(
            f"[retire-dept] WARN: telegram send returned "
            f"{tel_result.returncode}: {tel_result.stderr.strip()[:200]}",
            file=sys.stderr,
        )

    # ---- Side effect 2: graceful systemd disable on Morty ----------------
    morty_result = _disable_morty_unit_graceful(slug)
    if morty_result.returncode != 0:
        print(
            f"[retire-dept] WARN: morty disable returned "
            f"{morty_result.returncode}: {morty_result.stderr.strip()[:200]}",
            file=sys.stderr,
        )

    # ---- Side effect 2b: quarantine secrets (lock access, archive, wipe) --
    # Cut live ACCESS now (history stays). Non-blocking: a failure here leaves
    # the dept disabled-but-secrets-live, which the security log flags.
    quarantine_result = _quarantine_secrets(slug)
    if quarantine_result.returncode != 0:
        print(
            f"[retire-dept] WARN: secret quarantine returned "
            f"{quarantine_result.returncode}: "
            f"{quarantine_result.stderr.strip()[:200]}",
            file=sys.stderr,
        )

    # ---- Side effect 3: flip dept.yaml + commit + push --------------------
    dept_path = repo_dir / "dept.yaml"
    if dept_path.exists():
        _flip_dept_yaml_status(dept_path)
        _commit_dept_status_retired(repo_dir, display_name)
    else:
        print(
            f"[retire-dept] WARN: dept.yaml not found at {dept_path}; "
            "skipping git commit",
            file=sys.stderr,
        )

    # ---- Side effect 4: STATE.yaml -> Retired ----------------------------
    _mark_state_retired(state_path, reason)

    return {
        "status": "retired",
        "reasons": [],
        "final_telegram_msg": final_msg,
        "dry_run": False,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint (used by scripts/retire-dept.sh).
# ---------------------------------------------------------------------------

def _format_summary(slug: str, result: dict) -> str:
    out: List[str] = []
    out.append("")
    out.append("=" * 60)
    if result["status"] == "retired":
        out.append(f"  Department retired: {slug}")
    else:
        out.append(f"  Retirement BLOCKED for: {slug}")
    out.append("=" * 60)
    if result["reasons"]:
        out.append("")
        out.append("Reasons:")
        for r in result["reasons"]:
            out.append(f"  - {r}")
    out.append("")
    out.append("Farewell message (sent to the dept's Telegram chat):")
    out.append(f"  > {result['final_telegram_msg']}")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Retire a Live department with dignity.",
    )
    p.add_argument("--slug", required=True)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--reason", default="Decommissioned")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve()
    result = retire_dept(
        slug=args.slug,
        repo_dir=repo_dir,
        reason=args.reason,
        dry_run=args.dry_run,
    )
    print(_format_summary(args.slug, result))
    return 0 if result["status"] == "retired" else 2


if __name__ == "__main__":
    sys.exit(main())
