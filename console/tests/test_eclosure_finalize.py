"""
test_eclosure_finalize.py — RED tests for the finalize_dept_install() step
that closes ALL the scaffold gaps caught during Maya éclosion on 2026-05-24.

Bugs Maya éclosion hit (each ONE blocked her wake-up; combined = 5h debug):

1. **Symlink missing**: scaffold wrote `/home/claude/agents/<slug>/` but
   `dept_registry` scans for `bubble-ops-<slug>/`. Without the symlink the
   dept never appears in the UI even though her systemd service is up.

2. **`hasTrustDialogAccepted: false`**: Claude refuses to do anything on
   an "untrusted" cwd. Scaffold never marks the new dept path as trusted
   in `~/.claude/.claude.json`, so Maya woke up + sat idle silently.

3. **`defaultMode: default`**: Claude waits for human accept-edits prompt
   on every operation. Fixture uses `acceptEdits`. Scaffold writes
   `default` → Maya stuck on every file write.

4. **Broken SessionStart hook**: scaffold wires
   `python3 -m skill_lib.auto_drive announce_current_step` but skill_lib
   is NOT on the dept's PYTHONPATH (lives in
   `/home/claude/bubble-ops-loop/skills/department-onboarding-guide/`).
   Hook silently `ModuleNotFoundError`s → no Step-1 prompt → Maya idle.

5. **Scaffold not at the systemd path**: bootstrap-dept.sh clones to
   `/tmp/bubble-ops-<slug>/`. systemd's `ops-loop-<slug>.service` uses
   WorkingDirectory `/home/claude/agents/<slug>/`. No code moves the
   clone. Service restart-loops on `200/CHDIR`.

This file tests a NEW function `eclosure_launcher.finalize_dept_install(slug)`
that, given a slug whose bootstrap has already pushed the repo + cloned
to /tmp, completes the install so the dept is fully operational:

  - Moves /tmp/bubble-ops-<slug> to /home/claude/agents/<slug>
  - Creates the `bubble-ops-<slug>` symlink alongside (for dept_registry)
  - Marks the dept path as trusted in ~/.claude/.claude.json
  - Patches the dept's .claude/settings.json:
      - permissions.defaultMode = "acceptEdits"
      - hooks.SessionStart → working shell hook script
  - Writes .claude/hooks/session-start.sh from a template
  - Creates .claude/queued-prompts/ dir
  - chown -R claude:claude on the dept dir

The function must be idempotent — safe to re-run on already-finalized depts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fake_bootstrap_clone(tmp_clone_parent: Path, slug: str) -> Path:
    """Simulate what bootstrap-dept.sh leaves at /tmp/bubble-ops-<slug>/
    when it succeeds: a freshly-cloned dept skeleton."""
    clone_dir = tmp_clone_parent / f"bubble-ops-{slug}"
    clone_dir.mkdir(parents=True)
    (clone_dir / "CLAUDE.md").write_text(
        f"# Je suis {slug.capitalize()}. On m'éclôt.\n",
        encoding="utf-8",
    )
    (clone_dir / "dept.yaml.draft").write_text(
        f"department:\n  slug: {slug}\n  display_name: {slug.capitalize()}\n",
        encoding="utf-8",
    )
    (clone_dir / "onboarding").mkdir()
    (clone_dir / "onboarding" / "STATE.yaml").write_text(
        f"slug: {slug}\ndisplay_name: {slug.capitalize()}\nstatus: Idea\n",
        encoding="utf-8",
    )
    # Scaffold's broken settings.json (the wired-but-not-working state we're fixing)
    (clone_dir / ".claude").mkdir()
    (clone_dir / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"defaultMode": "default"},
        "enabledPlugins": {"telegram@claude-plugins-official": True},
        "enabledSkills": ["department-onboarding-guide"],
        "hooks": {
            "SessionStart": [{
                "hooks": [{
                    "type": "command",
                    "command": "python3 -m skill_lib.auto_drive announce_current_step onboarding/STATE.yaml",
                }],
            }],
        },
    }, indent=2), encoding="utf-8")
    return clone_dir


# ── Function existence + signature ───────────────────────────────────────────

def test_finalize_dept_install_function_exists():
    """finalize_dept_install(slug, *, tmp_clone, agents_parent, global_claude_json)
    must be a top-level export."""
    from console.services import eclosure_launcher
    assert hasattr(eclosure_launcher, "finalize_dept_install"), (
        "Function eclosure_launcher.finalize_dept_install missing. "
        "It must consolidate the 5 scaffold gaps caught 2026-05-24 (Maya)."
    )


# ── Bug 5 (clone-to-systemd-path) ────────────────────────────────────────────

def test_finalize_moves_clone_to_agents_parent(tmp_path):
    """After finalize, /home/claude/agents/<slug>/ must exist with all
    bootstrap content + /tmp clone must be gone."""
    from console.services import eclosure_launcher
    clone = _fake_bootstrap_clone(tmp_path, "alpha")
    agents = tmp_path / "agents"
    global_claude_json = tmp_path / ".claude.json"
    global_claude_json.write_text("{}", encoding="utf-8")

    eclosure_launcher.finalize_dept_install(
        slug="alpha",
        tmp_clone=clone,
        agents_parent=agents,
        global_claude_json=global_claude_json,
    )

    final = agents / "alpha"
    assert final.is_dir(), f"dept dir not moved to {final}"
    assert (final / "CLAUDE.md").exists()
    assert (final / "dept.yaml.draft").exists()
    assert (final / "onboarding" / "STATE.yaml").exists()
    assert not clone.exists(), f"/tmp clone still present at {clone}"


# ── Bug 1 (symlink for dept_registry) ────────────────────────────────────────

def test_finalize_creates_bubble_ops_symlink(tmp_path):
    """After finalize, agents_parent must have BOTH `<slug>/` (real dir)
    AND `bubble-ops-<slug>` (symlink to <slug>) so:
      - systemd (WorkingDirectory=/home/claude/agents/<slug>) finds the dir
      - dept_registry (scans bubble-ops-* prefix) finds the symlink
    """
    from console.services import eclosure_launcher
    clone = _fake_bootstrap_clone(tmp_path, "beta")
    agents = tmp_path / "agents"
    global_claude_json = tmp_path / ".claude.json"
    global_claude_json.write_text("{}", encoding="utf-8")

    eclosure_launcher.finalize_dept_install(
        slug="beta",
        tmp_clone=clone,
        agents_parent=agents,
        global_claude_json=global_claude_json,
    )

    symlink = agents / "bubble-ops-beta"
    assert symlink.is_symlink(), (
        f"symlink {symlink} not created — dept_registry will not see this dept"
    )
    target = os.readlink(symlink)
    assert target in ("beta", str(agents / "beta")), (
        f"symlink points to {target!r}, expected 'beta'"
    )


# ── Bug 2 (trust dialog) ─────────────────────────────────────────────────────

def test_finalize_marks_dept_path_trusted_in_global_claude_json(tmp_path):
    """After finalize, ~/.claude/.claude.json must have the dept path under
    `projects` with `hasTrustDialogAccepted: true`."""
    from console.services import eclosure_launcher
    clone = _fake_bootstrap_clone(tmp_path, "gamma")
    agents = tmp_path / "agents"
    global_claude_json = tmp_path / ".claude.json"
    global_claude_json.write_text(json.dumps({
        "projects": {
            "/home/claude/some/other/path": {"hasTrustDialogAccepted": True},
        },
    }), encoding="utf-8")

    eclosure_launcher.finalize_dept_install(
        slug="gamma",
        tmp_clone=clone,
        agents_parent=agents,
        global_claude_json=global_claude_json,
    )

    g = json.loads(global_claude_json.read_text(encoding="utf-8"))
    expected_path = str(agents / "gamma")
    assert expected_path in g.get("projects", {}), (
        f"Dept path {expected_path} not registered in global .claude.json projects"
    )
    assert g["projects"][expected_path].get("hasTrustDialogAccepted") is True
    # Preserve existing entries (don't clobber other projects)
    assert "/home/claude/some/other/path" in g["projects"]


# ── Bug 3 (defaultMode) ──────────────────────────────────────────────────────

def test_finalize_sets_default_mode_to_accept_edits(tmp_path):
    """The dept's .claude/settings.json must have
    permissions.defaultMode = 'acceptEdits' so Claude can write files
    without per-action prompts."""
    from console.services import eclosure_launcher
    clone = _fake_bootstrap_clone(tmp_path, "delta")
    agents = tmp_path / "agents"
    global_claude_json = tmp_path / ".claude.json"
    global_claude_json.write_text("{}", encoding="utf-8")

    eclosure_launcher.finalize_dept_install(
        slug="delta",
        tmp_clone=clone,
        agents_parent=agents,
        global_claude_json=global_claude_json,
    )

    settings = json.loads(
        (agents / "delta" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings.get("permissions", {}).get("defaultMode") == "acceptEdits"


# ── Bug 4 (working SessionStart hook) ────────────────────────────────────────

def test_finalize_writes_working_session_start_hook(tmp_path):
    """The dept's .claude/hooks/session-start.sh must exist + be executable
    + contain valid hookSpecificOutput JSON."""
    from console.services import eclosure_launcher
    clone = _fake_bootstrap_clone(tmp_path, "epsilon")
    agents = tmp_path / "agents"
    global_claude_json = tmp_path / ".claude.json"
    global_claude_json.write_text("{}", encoding="utf-8")

    eclosure_launcher.finalize_dept_install(
        slug="epsilon",
        tmp_clone=clone,
        agents_parent=agents,
        global_claude_json=global_claude_json,
    )

    hook_path = agents / "epsilon" / ".claude" / "hooks" / "session-start.sh"
    assert hook_path.exists(), f"hook script not written at {hook_path}"
    assert os.access(hook_path, os.X_OK), f"hook script {hook_path} not executable"
    body = hook_path.read_text(encoding="utf-8")
    assert "hookSpecificOutput" in body
    assert "additionalContext" in body
    assert "epsilon" in body, "hook must mention the dept slug for the wake-up prompt"


def test_finalize_rewires_settings_json_hook_to_shell_script(tmp_path):
    """settings.json's SessionStart hook must point to the shell script,
    NOT the broken python3 -m skill_lib.auto_drive command."""
    from console.services import eclosure_launcher
    clone = _fake_bootstrap_clone(tmp_path, "zeta")
    agents = tmp_path / "agents"
    global_claude_json = tmp_path / ".claude.json"
    global_claude_json.write_text("{}", encoding="utf-8")

    eclosure_launcher.finalize_dept_install(
        slug="zeta",
        tmp_clone=clone,
        agents_parent=agents,
        global_claude_json=global_claude_json,
    )

    settings = json.loads(
        (agents / "zeta" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    hooks = settings.get("hooks", {}).get("SessionStart", [])
    flat = json.dumps(hooks)
    assert "auto_drive" not in flat, (
        "settings.json still references the broken auto_drive command"
    )
    assert "session-start.sh" in flat, (
        "settings.json must point to the working shell hook script"
    )


# ── Bug-adjacent (queued-prompts dir) ────────────────────────────────────────

def test_finalize_creates_queued_prompts_dir(tmp_path):
    """The CLAUDE.md éclosion-driver template tells the agent to read
    .claude/queued-prompts/initial.md on first turn. The dir must exist."""
    from console.services import eclosure_launcher
    clone = _fake_bootstrap_clone(tmp_path, "eta")
    agents = tmp_path / "agents"
    global_claude_json = tmp_path / ".claude.json"
    global_claude_json.write_text("{}", encoding="utf-8")

    eclosure_launcher.finalize_dept_install(
        slug="eta",
        tmp_clone=clone,
        agents_parent=agents,
        global_claude_json=global_claude_json,
    )

    qp = agents / "eta" / ".claude" / "queued-prompts"
    assert qp.is_dir()


# ── Idempotency ──────────────────────────────────────────────────────────────

def test_finalize_is_idempotent(tmp_path):
    """Re-running finalize on an already-finalized dept must succeed
    silently (no exception, no clobber)."""
    from console.services import eclosure_launcher
    clone = _fake_bootstrap_clone(tmp_path, "theta")
    agents = tmp_path / "agents"
    global_claude_json = tmp_path / ".claude.json"
    global_claude_json.write_text("{}", encoding="utf-8")

    eclosure_launcher.finalize_dept_install(
        slug="theta", tmp_clone=clone, agents_parent=agents,
        global_claude_json=global_claude_json,
    )
    # 2nd call should not raise — the tmp clone is gone so this signals
    # "already finalized" and exits cleanly.
    eclosure_launcher.finalize_dept_install(
        slug="theta", tmp_clone=clone, agents_parent=agents,
        global_claude_json=global_claude_json,
    )
    # All artifacts still in place
    assert (agents / "theta").is_dir()
    assert (agents / "bubble-ops-theta").is_symlink()


# ── Launch-integration: finalize is called when installation_id is set ───────

def test_launch_calls_finalize_after_bootstrap(monkeypatch):
    """When launch() runs the bootstrap step (installation_id is not None),
    it must ALSO call finalize_dept_install so the dept is fully
    operational by the time service_started fires."""
    from console.services import eclosure_launcher

    calls = []
    monkeypatch.setattr(eclosure_launcher, "bootstrap_via_setup_callback",
                        lambda **kw: calls.append(("bootstrap", kw)))
    monkeypatch.setattr(eclosure_launcher, "finalize_dept_install",
                        lambda *a, **kw: calls.append(("finalize", a, kw)))
    monkeypatch.setattr(eclosure_launcher, "create_per_dept_sops_env",
                        lambda *a, **kw: calls.append(("sops",)))
    monkeypatch.setattr(eclosure_launcher, "install_systemd_unit",
                        lambda *a, **kw: calls.append(("systemd",)))
    monkeypatch.setattr(eclosure_launcher, "systemctl_enable_and_start",
                        lambda *a, **kw: calls.append(("start",)))
    monkeypatch.setattr(eclosure_launcher, "try_install_github_app",
                        lambda *a, **kw: {"ok": True, "installation_id": 1})

    eclosure_launcher.launch(
        slug="iota",
        telegram_bot_token="12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
        installation_id=42,
        level="ops",
        children_list=[],
        display_name="Iota",
        owner="joris",
    )

    kinds = [c[0] for c in calls]
    assert "bootstrap" in kinds, "bootstrap was not called"
    assert "finalize" in kinds, "finalize was not called after bootstrap"
    # Order: bootstrap MUST come before finalize, and finalize MUST come
    # before sops (sops writes /etc/bubble/secrets-<slug>.sops.env which is
    # independent of the dept dir, but finalize MUST happen before
    # service_started so systemd finds the dir).
    assert kinds.index("bootstrap") < kinds.index("finalize")
    assert kinds.index("finalize") < kinds.index("start")
