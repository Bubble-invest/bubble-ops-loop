"""
Sprint H+I Fix 5 — SOPS guidance + auth skill cross-reference.

Bootstrap output's "next steps" tells the operator to paste the bot
token into /etc/bubble/secrets-<slug>.sops.env. But the original output
gave no concrete command and didn't cross-reference the existing `auth`
skill's `operator-set-secret` flow.

This fix adds 3 explicit safety messages:
  1. Mention the `auth` skill + operator-set-secret flow.
  2. Show the literal command line (so the operator can paste it).
  3. Warn against opening the .sops.env file directly in vim.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _run_bootstrap_dry(scripts_dir: Path, tmp_clone_dir: Path, slug: str = "miranda"):
    script = scripts_dir / "bootstrap-dept.sh"
    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(tmp_clone_dir)
    return subprocess.run(
        [
            "bash", str(script),
            f"--slug={slug}",
            "--display-name=Miranda",
            "--owner=operator",
            "--dry-run",
        ],
        env=env, capture_output=True, text=True,
    )


def test_bootstrap_output_mentions_auth_skill(
    scripts_dir: Path, tmp_clone_dir: Path,
) -> None:
    """The bootstrap output must reference the `auth` skill's
    operator-set-secret flow so the operator knows the right tool."""
    res = _run_bootstrap_dry(scripts_dir, tmp_clone_dir)
    assert res.returncode == 0
    combined = (res.stdout + res.stderr).lower()
    assert "auth" in combined and "operator-set-secret" in combined, (
        f"output must mention the auth skill + operator-set-secret flow; got:\n{combined}"
    )


def test_bootstrap_output_shows_literal_set_secret_command(
    scripts_dir: Path, tmp_clone_dir: Path,
) -> None:
    """The bootstrap output must include a literal command line the
    operator can paste — with --project, --key=DEPT_TELEGRAM_BOT_TOKEN,
    and a remote-prompt indication."""
    res = _run_bootstrap_dry(scripts_dir, tmp_clone_dir)
    combined = res.stdout + res.stderr
    # The command may use the long name (operator-set-secret.sh) or the
    # short shim (bubble-set-secret); we accept either.
    has_command = (
        "operator-set-secret" in combined
        or "bubble-set-secret" in combined
    )
    assert has_command, (
        f"output must show the literal operator-set-secret command; got:\n{combined}"
    )
    # The required arguments must all appear.
    assert "DEPT_TELEGRAM_BOT_TOKEN" in combined, (
        f"command must show --key=DEPT_TELEGRAM_BOT_TOKEN; got:\n{combined}"
    )
    assert "--project=" in combined or "--project " in combined, (
        f"command must show --project=...sops.env; got:\n{combined}"
    )
    assert "--remote-prompt" in combined or "remote-prompt" in combined, (
        f"command must show --remote-prompt=hetzner (or similar); got:\n{combined}"
    )


def test_bootstrap_output_warns_against_direct_vim_edit(
    scripts_dir: Path, tmp_clone_dir: Path,
) -> None:
    """The bootstrap output must warn the operator NOT to open the
    .sops.env file directly in vim (would defeat the encryption)."""
    res = _run_bootstrap_dry(scripts_dir, tmp_clone_dir)
    combined = (res.stdout + res.stderr).lower()
    # Accept any of the canonical phrasings.
    has_warning = (
        ("vim" in combined or "directly" in combined or "directement" in combined)
        and ("encryp" in combined or "chiffr" in combined or "cleartext" in combined)
    )
    assert has_warning, (
        f"output must warn against opening the .sops.env in vim/cleartext; got:\n{combined}"
    )
