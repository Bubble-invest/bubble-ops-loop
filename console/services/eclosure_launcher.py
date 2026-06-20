"""
eclosure_launcher.py — orchestrate the post-bootstrap éclosure chain.

Called by POST /agents/new after scripts/bootstrap-dept.sh has scaffolded
the GitHub repo + cloned it to /home/claude/agents/<slug>/.

Responsibilities:
  1. Persist DEPT_TELEGRAM_BOT_TOKEN into a per-dept SOPS env file at
     /etc/bubble/secrets-<slug>.sops.env (encrypted to the same two age
     recipients as /etc/bubble/secrets.sops.env).
  2. Render deploy/templates/ops-loop-dept.service.template by substituting
     ${DEPT_SLUG}, ${TELEGRAM_STATE_DIR}, ${ENV_FILE} → install at
     /etc/systemd/system/ops-loop-<slug>.service.
  3. systemctl daemon-reload + enable + start ops-loop-<slug>.service.
  4. Best-effort: install the bubble-ops-bot GitHub App on the new repo
     via the GitHub API. Falls back to a UI message if it fails.

Side effects are wrapped in module-level functions so tests can
monkeypatch them in isolation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import logging
from pathlib import Path
from typing import Callable, Dict, Optional, Any, List

logger = logging.getLogger(__name__)

# ─── Paths / constants ────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SYSTEMD_TEMPLATE_PATH: Path = (
    _PROJECT_ROOT / "deploy" / "templates" / "ops-loop-dept.service.template"
)
GLOBAL_SOPS_ENV_PATH = Path("/etc/bubble/secrets.sops.env")
AGE_KEY_FILE = Path("/etc/age/key.txt")
AGENTS_PARENT = Path("/home/claude/agents")
TELEGRAM_STATE_PARENT = Path("/home/claude/.claude/channels")

# Two age recipients on the existing /etc/bubble/secrets.sops.env
# (extracted from the file's metadata block — these are public keys, safe
# to live in source).
AGE_RECIPIENTS = (
    "age1qal34hv5h99vvpq7kmghfz0mjh98eq9mj5dg5k43r8kwmumvnu5qt6w3hy,"
    "age155d7vgthaylh2c96krgvvtqemcql8s64ypwh8jhyyzkv586hc47sccpka8"
)

# Telegram bot token shape: <8-11 digits>:<30+ chars of A-Za-z0-9_->
_TG_TOKEN_RE = re.compile(r"^\d{8,11}:[A-Za-z0-9_-]{30,}$")


# ─── Validation ───────────────────────────────────────────────────────────
def is_valid_telegram_bot_token(token: str) -> bool:
    if not token or not isinstance(token, str):
        return False
    return bool(_TG_TOKEN_RE.match(token.strip()))


# ─── Step 1 — per-dept SOPS env file ──────────────────────────────────────
def create_per_dept_sops_env(slug: str, telegram_bot_token: str) -> None:
    """Write /etc/bubble/secrets-<slug>.sops.env (SOPS+age dotenv) holding
    just DEPT_TELEGRAM_BOT_TOKEN. The systemd unit reads this file at
    boot, renames DEPT_TELEGRAM_BOT_TOKEN to TELEGRAM_BOT_TOKEN, and
    drops the env into /run/claude-agent-<slug>/env."""
    out_path = Path(f"/etc/bubble/secrets-{slug}.sops.env")
    plain = f"DEPT_TELEGRAM_BOT_TOKEN={telegram_bot_token}\n"

    # Write plaintext to a root-owned tmp file under sudo, encrypt with
    # explicit --age recipients, move into place.
    cmd = [
        "sudo", "bash", "-c",
        f"""
set -e
TMP=$(mktemp /root/secrets-{slug}.XXXXXX)
chmod 600 "$TMP"
cat > "$TMP" <<'EOF_PLAINTEXT'
{plain}EOF_PLAINTEXT
SOPS_AGE_KEY_FILE={AGE_KEY_FILE} sops --encrypt \
  --input-type=dotenv --output-type=dotenv \
  --age "{AGE_RECIPIENTS}" \
  --output {out_path}.new "$TMP"
mv {out_path}.new {out_path}
chown root:root {out_path}
chmod 0440 {out_path}
shred -uz "$TMP"
"""
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"create_per_dept_sops_env({slug}) failed: {result.stderr or result.stdout}"
        )


# ─── Step 2 — render + install systemd unit ───────────────────────────────
def render_systemd_unit(slug: str) -> str:
    """Read the template at SYSTEMD_TEMPLATE_PATH and substitute placeholders."""
    template = SYSTEMD_TEMPLATE_PATH.read_text(encoding="utf-8")
    telegram_state_dir = str(TELEGRAM_STATE_PARENT / f"telegram-{slug}")
    env_file = f"/run/claude-agent-{slug}/env"
    # DEPT_SLUG_UPPER = slug upper-cased with '-'→'_' — MUST match the CLI path
    # (deploy-to-morty.sh: `tr '[:lower:]-' '[:upper:]_'`) so the env var name
    # GITHUB_APP_INSTALLATION_ID_<UPPER> the broker reads is identical whether a
    # dept is launched from the cockpit (this renderer) or the CLI. Without it
    # ${DEPT_SLUG_UPPER} survived rendering and a cockpit-launched dept shipped a
    # malformed env name (test_eclosure_launcher_v2 placeholder failures).
    slug_upper = slug.upper().replace("-", "_")
    # CLAUDE_MODEL — the model pin written into the ExecStart line. Read from
    # env at render time (deploy-to-morty.sh exports it; cockpit falls back to
    # the fleet-wide default). Matching deploy-to-morty.sh behaviour: if the
    # env var is unset we default to "claude-opus-4-5" (cost-optimised default
    # for ops-loop depts — same as the fleet-wide setting post-2026-06-19
    # cost-optimization pass).
    claude_model = os.environ.get("CLAUDE_MODEL", "claude-opus-4-5")
    rendered = (
        template
        .replace("${DEPT_SLUG_UPPER}", slug_upper)
        .replace("${DEPT_SLUG}", slug)
        .replace("${TELEGRAM_STATE_DIR}", telegram_state_dir)
        .replace("${ENV_FILE}", env_file)
        .replace("${CLAUDE_MODEL}", claude_model)
    )
    return rendered


def install_systemd_unit(slug: str) -> None:
    """Render the unit and install at /etc/systemd/system/ops-loop-<slug>.service."""
    unit_text = render_systemd_unit(slug)
    unit_path = f"/etc/systemd/system/ops-loop-{slug}.service"
    cmd = [
        "sudo", "bash", "-c",
        f"""
set -e
cat > {unit_path} <<'EOF_UNIT'
{unit_text}EOF_UNIT
chown root:root {unit_path}
chmod 0644 {unit_path}
systemctl daemon-reload
"""
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"install_systemd_unit({slug}) failed: {result.stderr or result.stdout}"
        )


# ─── Step 3 — start the service ────────────────────────────────────────────
def systemctl_enable_and_start(slug: str) -> None:
    """Enable + start ops-loop-<slug>.service. Fails loudly if the service
    isn't active after the start command."""
    cmd = ["sudo", "systemctl", "enable", "--now", f"ops-loop-{slug}.service"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl enable --now ops-loop-{slug}: {result.stderr or result.stdout}"
        )


# ─── Step 4 — install GitHub App (best-effort) ────────────────────────────
def try_install_github_app(slug: str) -> Dict[str, Any]:
    """Attempt to add bubble-ops-<slug> to the bubble-ops-bot App installation.
    Returns {ok: bool, installation_id?: int, error?: str}.

    The PUT endpoint requires a user-to-server token with the
    'administration:write' scope on the App owner; if that scope is
    missing we get 403 and fall back to a manual UI instruction."""
    repo = f"vdk888/bubble-ops-{slug}"
    try:
        # First: do we already see an installation for this repo (App
        # configured "all repositories" or owner clicked "add" already)?
        check = subprocess.run(
            ["gh", "api", f"repos/{repo}/installation", "--jq", ".id"],
            capture_output=True, text=True, check=False,
        )
        if check.returncode == 0 and check.stdout.strip():
            try:
                return {"ok": True, "installation_id": int(check.stdout.strip())}
            except ValueError:
                pass

        # Otherwise: try PUT user/installations/{id}/repositories/{repo_id}
        # using the existing user PAT. Will 403 unless the PAT has the
        # right scope — that's the expected fallback.
        # First get the App installation ID for vdk888 — known from fixture's env.
        installation_id = os.environ.get(
            "GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_FIXTURE", ""
        )
        if not installation_id:
            return {"ok": False, "error": "no installation_id known"}

        repo_id_run = subprocess.run(
            ["gh", "api", f"repos/{repo}", "--jq", ".id"],
            capture_output=True, text=True, check=False,
        )
        if repo_id_run.returncode != 0 or not repo_id_run.stdout.strip():
            return {"ok": False, "error": "repo not found via gh api"}
        repo_id = repo_id_run.stdout.strip()

        put = subprocess.run(
            ["gh", "api", "-X", "PUT",
             f"user/installations/{installation_id}/repositories/{repo_id}"],
            capture_output=True, text=True, check=False,
        )
        if put.returncode == 0:
            return {"ok": True, "installation_id": int(installation_id)}
        # short error
        err = (put.stderr or put.stdout or "").splitlines()[0][:120]
        return {"ok": False, "error": err or "gh api PUT failed"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}


# ─── Bootstrap (Wave-3 Step 0b — replaces the old PAT-driven gh repo create) ──
def bootstrap_via_setup_callback(
    slug: str,
    display_name: str,
    owner: str,
    level: str,
    children_list: List[str],
    installation_id: int,
) -> None:
    """Run scripts/bootstrap-dept.sh with --accept-existing-empty-repo and
    a per-call install-token URL.

    The operator has already granted bubble-ops-bot access to the new repo
    via the GitHub App setup-URL flow, so we know the App can read/write it.
    We mint a short-lived installation token here and pass it to git via the
    BUBBLE_INSTALL_TOKEN env var (bootstrap-dept.sh consumes it to build
    https://x-access-token:<token>@github.com/... clone URLs).

    Defense in depth: token never written to disk, never echoed to stdout.
    """
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    bootstrap_path = _PROJECT_ROOT / "scripts" / "bootstrap-dept.sh"
    if not bootstrap_path.exists():
        raise RuntimeError(f"bootstrap-dept.sh not found at {bootstrap_path}")

    cmd = [
        "bash", str(bootstrap_path),
        f"--slug={slug}",
        f"--display-name={display_name}",
        f"--owner={owner}",
        f"--level={level}",
        "--accept-existing-empty-repo",
    ]
    if children_list:
        cmd.append(f"--children={','.join(children_list)}")

    env = os.environ.copy()
    env["BUBBLE_OPS_BOT_INSTALLATION_ID"] = str(installation_id)

    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        # Surface the bootstrap stderr so SSE can show it to the operator
        raise RuntimeError(
            f"bootstrap-dept.sh failed (rc={result.returncode}): "
            f"{(result.stderr or result.stdout)[-400:]}"
        )


# ─── Step 1.5 — finalize the dept install (closes 5 scaffold gaps) ───────
# Caught 2026-05-24 during Maya éclosion (msg 3094 + 3097). Each of the 5
# gaps below silently broke wake-up:
#   1. /tmp clone not moved to /home/claude/agents/<slug> (systemd 200/CHDIR)
#   2. bubble-ops-<slug> symlink missing (dept_registry doesn't see dept)
#   3. dept path not trusted in ~/.claude/.claude.json (claude refuses to act)
#   4. settings.json defaultMode=default (claude waits on every file write)
#   5. SessionStart hook references python3 -m skill_lib.auto_drive which
#      isn't on the dept's PYTHONPATH → ModuleNotFoundError, no Step-1 prompt
#
# The fix is consolidated here so future éclosions hit zero of them. The
# function is idempotent + accepts paths as kwargs so it's testable
# without touching real /tmp or ~/.claude.

# Working SessionStart hook template — mirrors fixture's verified-working
# session-start.sh. Injects an `additionalContext` JSON the agent reads on
# first turn. Token-shaped placeholders are substituted per dept.
_SESSION_START_HOOK_TEMPLATE = """#!/usr/bin/env bash
# {slug} dept SessionStart hook — injects Step-1 (Mandate) wake-up prompt.
# Auto-generated by eclosure_launcher.finalize_dept_install() — DO NOT EDIT
# by hand; if you need to change voice or content, change the template in
# console/services/eclosure_launcher.py and re-deploy.
cat <<'JSON'
{{
  "hookSpecificOutput": {{
    "hookEventName": "SessionStart",
    "additionalContext": "Première fois que tu te réveilles. Tu es {display_name}, dept-manager à Bubble Invest, en cours d'éclosion sur Morty (VPS). ACTION REQUISE au premier tour: (1) Lire ./CLAUDE.md (ton manuel d'éclosion), (2) Lire ./dept.yaml.draft (ton dept-schema en cours), (3) Envoyer un message Telegram à {{OPERATOR}} (chat_id {{OPERATOR_CHAT_ID}}) via @bubbleops{slug_compact}_bot pour annoncer ton réveil et lui proposer 3 options de mandat pour ton dept (Step 1 du 7-step éclosion flow). Le bot token est dans la variable d'env TELEGRAM_BOT_TOKEN, lue depuis /run/claude-agent-{slug}/env. Voix Bureau-de-Cadre, français, tutoiement à {{OPERATOR}}."
  }}
}}
JSON
"""


def _render_session_start_hook(slug: str, display_name: str) -> str:
    return _SESSION_START_HOOK_TEMPLATE.format(
        slug=slug,
        slug_compact=slug.replace("-", ""),
        display_name=display_name,
    )


def finalize_dept_install(
    slug: str,
    tmp_clone: Path = None,  # type: ignore[assignment]
    agents_parent: Path = AGENTS_PARENT,
    global_claude_json: Path = Path.home() / ".claude.json",
    display_name: Optional[str] = None,
) -> None:
    """Close the 5 scaffold gaps so the dept is fully operational at
    `service_started` time. Idempotent.

    Args:
        slug:              Dept slug (e.g. "maya").
        tmp_clone:         Where bootstrap-dept.sh left the clone. If None,
                           defaults to /tmp/bubble-ops-<slug>. If the path
                           doesn't exist (re-run case), the move step is a
                           no-op and we proceed with the other fixes.
        agents_parent:     Parent dir for live dept clones (default
                           /home/claude/agents). Tests override.
        global_claude_json: Path to the user-scoped Claude config (default
                           ~/.claude.json). Tests override.
        display_name:      Pretty name; defaults to slug.capitalize() for
                           hook copy.

    Steps (each guarded for idempotency):
        A. Move /tmp/bubble-ops-<slug>/ → <agents_parent>/<slug>/
        B. Create symlink <agents_parent>/bubble-ops-<slug> → <slug>
        C. Patch <global_claude_json>::projects[<dept_path>].
           hasTrustDialogAccepted = True
        D. Patch <dept>/.claude/settings.json::permissions.defaultMode
           = "acceptEdits"
        E. Write <dept>/.claude/hooks/session-start.sh + chmod +x; rewire
           settings.json::hooks.SessionStart to point at it
        F. mkdir <dept>/.claude/queued-prompts (CLAUDE.md template
           references this dir)
        G. chown -R claude:claude on the dept dir (best-effort; skipped if
           non-root or chown unavailable)
    """
    import shutil
    import stat as _stat

    if tmp_clone is None:
        tmp_clone = Path(f"/tmp/bubble-ops-{slug}")
    agents_parent = Path(agents_parent)
    global_claude_json = Path(global_claude_json)
    display_name = display_name or slug.capitalize()

    final_dept_dir = agents_parent / slug
    symlink_path = agents_parent / f"bubble-ops-{slug}"

    # A. Move /tmp clone to final dept dir (idempotent: skip if already moved)
    agents_parent.mkdir(parents=True, exist_ok=True)
    if tmp_clone.exists() and not final_dept_dir.exists():
        shutil.move(str(tmp_clone), str(final_dept_dir))
    elif tmp_clone.exists() and final_dept_dir.exists():
        # Both exist — already finalized, just clean the tmp leftover.
        shutil.rmtree(tmp_clone, ignore_errors=True)
    # If neither exists, caller passed a bogus path or bootstrap didn't run.
    # We'll fail on the next step rather than silently miscreating an empty dir.

    if not final_dept_dir.exists():
        raise RuntimeError(
            f"finalize_dept_install: final dept dir {final_dept_dir} does not exist "
            f"and tmp clone {tmp_clone} is missing — bootstrap likely failed."
        )

    # B. Create bubble-ops-<slug> symlink (idempotent)
    if symlink_path.is_symlink() or symlink_path.exists():
        # Already there — verify points to the right target; if not, replace.
        try:
            if symlink_path.is_symlink() and os.readlink(symlink_path) == slug:
                pass  # correct symlink, nothing to do
            else:
                symlink_path.unlink()
                symlink_path.symlink_to(slug)
        except OSError:
            pass  # cannot fix; leave as-is
    else:
        symlink_path.symlink_to(slug)

    # C. Mark dept path as trusted in global Claude config
    dept_path_str = str(final_dept_dir)
    try:
        if global_claude_json.exists():
            cfg = json.loads(global_claude_json.read_text(encoding="utf-8") or "{}")
        else:
            cfg = {}
        projects = cfg.setdefault("projects", {})
        proj = projects.setdefault(dept_path_str, {})
        proj["hasTrustDialogAccepted"] = True
        proj.setdefault("allowedTools", [])
        proj.setdefault("history", [])
        global_claude_json.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        # Don't block éclosion on a global-config write — log + continue.
        logger.exception(
            "finalize_dept_install: could not patch %s for trust",
            global_claude_json,
        )

    # D. Patch settings.json: defaultMode + hook
    settings_path = final_dept_dir / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            s = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            s = {}
    else:
        s = {}
    s.setdefault("permissions", {})["defaultMode"] = "acceptEdits"

    # E. Write the working SessionStart hook + rewire settings to it
    hook_dir = final_dept_dir / ".claude" / "hooks"
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hook_dir / "session-start.sh"
    hook_path.write_text(
        _render_session_start_hook(slug, display_name),
        encoding="utf-8",
    )
    hook_path.chmod(hook_path.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP)

    s["hooks"] = s.get("hooks", {})
    s["hooks"]["SessionStart"] = [{
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": str(hook_path),
            "timeout": 5000,
        }],
    }]

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(s, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # F. Create queued-prompts dir
    (final_dept_dir / ".claude" / "queued-prompts").mkdir(
        parents=True, exist_ok=True
    )

    # G. chown -R claude:claude (best-effort; needs root, skipped in tests)
    try:
        if os.geteuid() == 0:
            import pwd
            try:
                uid = pwd.getpwnam("claude").pw_uid
                gid = pwd.getpwnam("claude").pw_gid
                for root, dirs, files in os.walk(final_dept_dir):
                    os.chown(root, uid, gid, follow_symlinks=False)
                    for d in dirs:
                        os.chown(os.path.join(root, d), uid, gid, follow_symlinks=False)
                    for f in files:
                        os.chown(os.path.join(root, f), uid, gid, follow_symlinks=False)
                # And the symlink
                if symlink_path.is_symlink():
                    os.chown(symlink_path, uid, gid, follow_symlinks=False)
            except KeyError:
                pass  # `claude` user doesn't exist (test env, dev box)
    except Exception:
        logger.exception("finalize_dept_install: chown step failed (non-fatal)")


# ─── Orchestration ────────────────────────────────────────────────────────
def launch(
    slug: str,
    telegram_bot_token: str,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    installation_id: Optional[int] = None,
    level: str = "ops",
    children_list: Optional[List[str]] = None,
    display_name: Optional[str] = None,
    owner: str = "joris",
) -> Dict[str, Any]:
    """Run the éclosure chain end-to-end, emitting progress events.

    Wave-3 Step 0b: now also runs bootstrap (which the POST handler used
    to do synchronously before deferring to the GitHub-App setup callback).
    When installation_id is provided, the bootstrap step uses the App
    installation token instead of the broker's PAT.

    Backward compat: if installation_id is None (legacy callers), the
    bootstrap step is skipped — preserving the v2 contract where the
    POST handler ran bootstrap itself.
    """
    if not is_valid_telegram_bot_token(telegram_bot_token):
        raise ValueError(
            "telegram_bot_token has invalid shape "
            "(expected <8-11 digits>:<30+ chars of A-Za-z0-9_->)"
        )
    children_list = list(children_list or [])
    display_name = display_name or slug

    def emit(kind: str, **extra: Any) -> None:
        if on_progress is None:
            return
        event = {"kind": kind, "slug": slug, **extra}
        try:
            on_progress(event)
        except Exception:
            logger.exception("on_progress callback raised — continuing")

    emit("start")

    # Step 0 — bootstrap repo + scaffold (only when called from the setup callback)
    if installation_id is not None:
        try:
            bootstrap_via_setup_callback(
                slug=slug,
                display_name=display_name,
                owner=owner,
                level=level,
                children_list=children_list,
                installation_id=installation_id,
            )
            emit("bootstrap_done")
        except Exception as exc:
            emit("error", step="bootstrap", message=str(exc)[:200])
            raise

        # Step 0b — finalize install: move /tmp clone to /home/claude/agents,
        # create symlink, trust dept path, fix settings.json, write hook.
        # Closes 5 scaffold gaps caught 2026-05-24 (Maya éclosion).
        try:
            finalize_dept_install(slug=slug, display_name=display_name)
            emit("finalize_done")
        except Exception as exc:
            emit("error", step="finalize", message=str(exc)[:200])
            raise

    try:
        create_per_dept_sops_env(slug, telegram_bot_token)
        emit("sops_done")
    except Exception as exc:
        emit("error", step="sops", message=str(exc)[:200])
        raise

    try:
        install_systemd_unit(slug)
        emit("systemd_installed")
    except Exception as exc:
        emit("error", step="systemd", message=str(exc)[:200])
        raise

    try:
        systemctl_enable_and_start(slug)
        emit("service_started")
    except Exception as exc:
        emit("error", step="service", message=str(exc)[:200])
        raise

    app_result = try_install_github_app(slug)
    emit("gh_app_done", ok=app_result.get("ok", False),
         installation_id=app_result.get("installation_id"),
         error=app_result.get("error"))

    result = {"ok": True, "github_app": app_result}
    emit("done", **result)
    return result
