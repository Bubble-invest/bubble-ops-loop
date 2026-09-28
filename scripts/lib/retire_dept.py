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
  1. Telegram: send the final message via the dept's bot (urllib / API).
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
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import yaml


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import state_yaml  # noqa: E402
from notify import TelegramBackend  # noqa: E402


UNIT_PATTERN = "bubble-agent@{slug}.service"
DEFAULT_REMOTE = os.environ.get("BUBBLE_MORTY_HOST", "claude@morty")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Final Telegram message — FR Bureau-de-Cadre voice (warm, dignified).
# ---------------------------------------------------------------------------

def compose_final_telegram_message(display_name: str, reason: str) -> str:
    """The farewell message sent to the dept's Telegram chat.

    Tone: Bureau-de-Cadre — warm + dignified, not corporate, not robotic.
    {{OPERATOR}} validates this prose; tests assert key markers (Merci, retraite,
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
    """Send with Python-resolved credentials, never putting a token in argv.

    Prefer BUBBLE_BOT_TOKEN_<SLUG>, then TELEGRAM_BOT_TOKEN from the env
    or the existing $TELEGRAM_STATE_DIR/.env loader. The caller must select
    the retiring dept's credentials and set TELEGRAM_CHAT_ID to its chat.
    Return only safe diagnostics: HTTP errors can contain the token URL.
    """
    token_key = f"BUBBLE_BOT_TOKEN_{slug.replace('-', '_').upper()}"
    token = (os.environ.get(token_key)
             or os.environ.get("TELEGRAM_BOT_TOKEN")
             or TelegramBackend._read_token_from_state_dir())
    error = ""
    if not token:
        error = (f"Telegram token missing: set {token_key}, TELEGRAM_BOT_TOKEN "
                 "or TELEGRAM_STATE_DIR pointing to the dept's .env")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not error and not chat_id:
        error = "Telegram chat missing: set TELEGRAM_CHAT_ID for the retiring dept"
    if not error:
        try:
            request = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read())
            if not isinstance(body, dict) or body.get("ok") is not True:
                error = "Telegram API did not confirm farewell delivery"
        except urllib.error.HTTPError as exc:
            error = f"Telegram farewell failed: HTTP {exc.code}"
        except (OSError, ValueError):
            error = "Telegram farewell failed: transport error or invalid response"
    return subprocess.CompletedProcess(
        args=["telegram", "sendMessage"], returncode=1 if error else 0,
        stdout="", stderr=error,
    )


def _disable_morty_unit_graceful(slug: str, remote: str = DEFAULT_REMOTE
                                 ) -> subprocess.CompletedProcess:
    """`systemctl disable` the dept unit (graceful — no --now).

    Detects whether we're already ON Morty (the unit file exists locally)
    and skips the `ssh remote` prefix in that case. Self-SSH without a
    TTY silently fails — caught 2026-05-24 when console-triggered retire
    left ops-loop-fixture.service still active+enabled.
    """
    unit = UNIT_PATTERN.format(slug=slug)
    unit_path = Path(f"/etc/systemd/system/{unit}.d")
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
        return {"status": "blocked", "reasons": [tel_result.stderr],
                "final_telegram_msg": final_msg}

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
    out.append("Farewell message:")
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
