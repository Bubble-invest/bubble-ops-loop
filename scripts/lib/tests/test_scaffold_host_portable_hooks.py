"""
test_scaffold_host_portable_hooks.py — regression test for board card #471.

Bug: scaffold.py's CLAUDE_SETTINGS_MINIMAL hardcoded a bare VPS-absolute path
(`python3 /opt/bubble-mission-guard/mission-file-guard.py`) as the scaffolded
dept's PreToolUse hook. On a host:local dept (e.g. the M5, a Mac) that path
doesn't exist, so python3 exits 2 (ModuleNotFoundError / file-not-found) and
Claude Code's PreToolUse contract treats exit 2 as a hard DENY — the freshly
scaffolded dept boots fully Bash/Edit/Write/NotebookEdit-BLOCKED from turn
one. Hit live on Geraldine's first hours on the M5. Same class of trap in the
scaffolded SessionStart hook, which assumed the VPS PYTHONPATH was already
set globally.

Fix: emit host-portable hooks — candidate-path resolution ($HOME/.scripts
first, /opt fallback), `exec` so a REAL deny verdict (exit 2) from an
installed guard is still preserved, and fail-OPEN (exit 0) only when the
guard file is genuinely absent from every candidate. The exact pattern is
ported from the fix already shipped + independently verified on Geraldine's
live workspace (bubble-ops-accountant commit 77d7e2a): benign Bash -> allow
rc=0; structural edit (layers/1/PROMPT.md) -> proper deny JSON.

This test locks in the fix at the factory (scaffold.py) so every future
scaffolded dept — VPS or host:local — gets a hook that can never hard-deny
just because /opt doesn't exist on that host.
"""
from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path surgery so scaffold.py and its deps are importable (mirrors the other
# scripts/lib/tests/test_scaffold_*.py files in this directory).
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent               # scripts/lib/
SCRIPTS_DIR = SCRIPTS_LIB.parent        # scripts/
PROJECT_ROOT = SCRIPTS_DIR.parent       # bubble-ops-loop/
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"

for p in (str(SKILL_ROOT), str(SCRIPTS_LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

import scaffold  # noqa: E402  (after path surgery)


def _pretooluse_command() -> str:
    hooks = scaffold.CLAUDE_SETTINGS_MINIMAL["hooks"]["PreToolUse"]
    return hooks[0]["hooks"][0]["command"]


def _sessionstart_command() -> str:
    hooks = scaffold.CLAUDE_SETTINGS_MINIMAL["hooks"]["SessionStart"]
    return hooks[0]["hooks"][0]["command"]


def test_pretooluse_hook_has_no_bare_opt_path():
    """The emitted PreToolUse command must never invoke a bare /opt/ path
    directly — every /opt/ reference must be reachable only through the
    candidate-path fallback (i.e. preceded by a $HOME candidate + an
    `[ -f ... ] ||` style guard), never as the literal command that always
    executes regardless of whether the file exists."""
    cmd = _pretooluse_command()
    assert "/opt/" in cmd, "sanity: VPS path should still appear as a fallback"
    # The bug pattern: a command that STARTS with (or IS) a bare `python3
    # /opt/...` invocation with no candidate resolution before it.
    assert not cmd.startswith("python3 /opt/"), (
        f"PreToolUse command invokes /opt/ directly with no host fallback: {cmd!r}"
    )
    # Positive assertions on the actual portable pattern.
    assert "$HOME/.scripts/mission-file-guard.py" in cmd, (
        "must try the user-level ($HOME/.scripts) guard candidate first"
    )
    assert "[ -f \"$f\" ]" in cmd, "must check candidate existence before exec"
    assert "exec python3" in cmd, (
        "must exec (not just call) the guard so real deny verdicts (exit 2) "
        "propagate as this hook's own exit code"
    )
    assert "else exit 0; fi" in cmd, (
        "must fail OPEN only when the guard is absent from every candidate "
        "(never hard-deny just because /opt doesn't exist on this host)"
    )


def test_sessionstart_hook_has_no_bare_module_invocation():
    """The emitted SessionStart command must not assume a globally-set VPS
    PYTHONPATH — it must inline candidate paths and never let import failure
    abort the session start (advisory hook, not a gate)."""
    cmd = _sessionstart_command()
    assert not cmd.startswith("python3 -m skill_lib.auto_drive"), (
        f"SessionStart invokes the module with no PYTHONPATH fallback: {cmd!r}"
    )
    assert "PYTHONPATH=" in cmd, "must inline a PYTHONPATH with host candidates"
    assert "department-onboarding-guide" in cmd
    assert cmd.rstrip().endswith("|| true"), (
        "must not hard-fail SessionStart when the skill_lib import fails "
        "on a host where the VPS layout candidate doesn't apply"
    )


def test_build_settings_ops_and_management_both_carry_the_fix(tmp_path):
    """The fix must reach the actual scaffolded dict for both dept levels —
    not just the CLAUDE_SETTINGS_MINIMAL constant in isolation."""
    for level in ("ops", "management"):
        settings = scaffold._build_settings("somedept", level=level)
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert not cmd.startswith("python3 /opt/"), (
            f"level={level}: _build_settings still emits a bare /opt/ invocation"
        )
        assert "$HOME/.scripts/mission-file-guard.py" in cmd
