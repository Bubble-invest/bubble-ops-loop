"""
Sprint H+I Fix 2 — SessionStart hook + .claude/settings.json bindings.

After bootstrap, a fresh Claude Code session for the new dept must:
  1. Have plugin:telegram bound in its .claude/settings.json
     (Notion v5 line 1030: "skills existants ... bound dans
     .claude/settings.json mcpServers par dept").
  2. Have the department-onboarding-guide skill mounted/enabled so it
     can self-eclose.
  3. Run a SessionStart hook on first boot that calls
     `python3 -m skill_lib.auto_drive announce_current_step <STATE.yaml>`
     which writes the operator-facing prompt to .claude/queued-prompts/initial.md
     so the agent surfaces it to {{OPERATOR}} on Telegram on first turn.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _load_settings(repo: Path) -> dict:
    return json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))


def test_settings_binds_telegram_mcp_or_plugin(bootstrapped_repo: Path) -> None:
    """The generated settings.json must bind plugin:telegram so the agent
    can read/reply to {{OPERATOR}} on Telegram from the first turn.

    Notion v5 line 1030 mandates "bound dans .claude/settings.json mcpServers
    par dept" — we accept either `mcpServers.telegram` or
    `enabledPlugins["telegram@claude-plugins-official"]: true`.
    """
    settings = _load_settings(bootstrapped_repo)

    mcp_servers = settings.get("mcpServers", {})
    enabled = settings.get("enabledPlugins", {})

    has_mcp_telegram = "telegram" in mcp_servers
    has_plugin_telegram = enabled.get("telegram@claude-plugins-official") is True

    assert has_mcp_telegram or has_plugin_telegram, (
        f"settings.json must bind plugin:telegram (either as "
        f"mcpServers.telegram or enabledPlugins['telegram@claude-plugins-official']=true).\n"
        f"Got mcpServers={mcp_servers}, enabledPlugins={enabled}"
    )


def test_settings_enables_department_onboarding_guide_skill(bootstrapped_repo: Path) -> None:
    """The eclosing agent must have access to the
    department-onboarding-guide skill from the first turn so it can
    drive its own 7-step eclosure."""
    settings = _load_settings(bootstrapped_repo)

    # Accept either an `enabledSkills` list, a `skills` list, or a
    # marketplace plugin reference.
    enabled_skills = settings.get("enabledSkills", []) or settings.get("skills", [])
    enabled_plugins = settings.get("enabledPlugins", {})

    found = (
        "department-onboarding-guide" in enabled_skills
        or any("department-onboarding-guide" in str(k) for k in enabled_plugins)
        or any("department-onboarding-guide" in str(item) for item in enabled_skills)
    )

    assert found, (
        f"settings.json must enable the department-onboarding-guide skill "
        f"so the eclosing agent can self-eclose.\n"
        f"Got enabledSkills={enabled_skills}, enabledPlugins={enabled_plugins}"
    )


def test_settings_has_session_start_hook(bootstrapped_repo: Path) -> None:
    """A SessionStart hook must run auto_drive's announce_current_step
    entry point on first boot. The hook must invoke
    `python3 -m skill_lib.auto_drive announce_current_step` with the
    STATE.yaml path."""
    settings = _load_settings(bootstrapped_repo)
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])

    assert session_start, (
        "settings.json must have hooks.SessionStart so the agent surfaces "
        "the first-step prompt to {{OPERATOR}} on first boot."
    )

    # Flatten the hook commands across all SessionStart entries.
    all_commands = []
    for entry in session_start:
        for hook in entry.get("hooks", []):
            cmd = hook.get("command", "")
            all_commands.append(cmd)

    combined = " ".join(all_commands)
    assert "auto_drive" in combined, (
        f"SessionStart hook must call auto_drive (got commands: {all_commands})"
    )
    assert "announce_current_step" in combined, (
        f"SessionStart hook must call the 'announce_current_step' "
        f"sub-command (got: {all_commands})"
    )
    assert "STATE.yaml" in combined or "state" in combined.lower(), (
        f"SessionStart hook must pass the STATE.yaml path (got: {all_commands})"
    )


def test_claude_md_instructs_agent_to_read_queued_prompt(bootstrapped_repo: Path) -> None:
    """CLAUDE.md must tell the agent: on first boot, read
    .claude/queued-prompts/initial.md and surface its content on Telegram.
    Without this instruction, the SessionStart hook's output is silent."""
    text = (bootstrapped_repo / "CLAUDE.md").read_text(encoding="utf-8")
    low = text.lower()
    assert "queued-prompts" in low or "queued_prompts" in low, (
        "CLAUDE.md must mention .claude/queued-prompts/ so the agent "
        "knows to consume the SessionStart hook's output."
    )
    assert "initial.md" in low or "initial" in low, (
        "CLAUDE.md must reference the initial.md prompt file."
    )


# ---------------------------------------------------------------------------
# auto_drive module entry point (announce_current_step subcommand).
# ---------------------------------------------------------------------------

def test_auto_drive_module_invokable_with_announce_current_step(
    bootstrapped_repo: Path,
    project_root: Path,
) -> None:
    """`python3 -m skill_lib.auto_drive announce_current_step <state_path>`
    must run, produce stdout containing the FR prompt, and write the
    prompt to .claude/queued-prompts/initial.md inside the dept repo."""
    state_path = bootstrapped_repo / "onboarding" / "STATE.yaml"
    assert state_path.exists(), "fixture: bootstrap should have created STATE.yaml"

    skill_root = project_root / "skills" / "department-onboarding-guide"
    env = {
        "PYTHONPATH": str(skill_root),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    res = subprocess.run(
        [sys.executable, "-m", "skill_lib.auto_drive",
         "announce_current_step", str(state_path)],
        capture_output=True, text=True, env=env,
        cwd=str(bootstrapped_repo),
    )
    assert res.returncode == 0, (
        f"auto_drive announce_current_step must exit 0; "
        f"stdout={res.stdout}\nstderr={res.stderr}"
    )

    # Prompt file must be written.
    queued = bootstrapped_repo / ".claude" / "queued-prompts" / "initial.md"
    assert queued.exists(), (
        f"announce_current_step must write the prompt to {queued}"
    )
    content = queued.read_text(encoding="utf-8")
    # Step 1 (mandate) is the first step at bootstrap → prompt mentions mandate.
    assert "mandat" in content.lower() or "mandate" in content.lower(), (
        f"queued prompt must reference the mandate step; got:\n{content}"
    )
