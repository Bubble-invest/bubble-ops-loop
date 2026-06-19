"""test_backup_spawn_strict_mcp.py — PR1: fix the broken recovery path.

Root cause (Maya/Ben 2026-06-18): the backup floor's `claude --print` spawn
returns exit=1 and never executes a layer subagent. Per
MCP-WEDGE-ROOTCAUSE.md path #2, `--setting-sources user` makes the headless
child load the dept's telegram --channels plugin + its MCP server, spinning up
a SECOND bun poller against the same bot token / bot.pid as the live session.
The two pollers collide → the headless MCP load aborts → the spawn exits
non-zero → the safety net silently does nothing.

Fix: pass `--strict-mcp-config` (with NO --mcp-config) so the backup tick loads
ZERO MCP servers — no telegram plugin, no second poller, no collision — while
`--setting-sources user` still loads hooks/permissions/CLAUDE.md.

These are static-source assertions (portable, no Linux/systemd needed) so the
GitHub CI (python-only, scripts/lib/tests/) catches a regression that strips
the flag back out or removes the unit-hygiene reset-failed.

TDD: written against the FIXED source; a revert of either change goes RED.
"""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
BACKUP_SH = os.path.join(REPO_ROOT, "scripts", "loop-backup.sh")
LAYER_SVC = os.path.join(REPO_ROOT, "deploy", "templates", "loop-layer@.service")
INSTALL_SH = os.path.join(REPO_ROOT, "scripts", "install-loop-backup.sh")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _spawn_block(src: str) -> str:
    """Isolate the `claude --print` invocation block inside run_backup_tick.

    The spawn is the only `--print` invocation; grab the line span from
    `"$CLAUDE_BIN" \\` through the closing prompt arg so the assertions can't
    be satisfied by a flag that lives in a comment elsewhere.
    """
    m = re.search(r'"\$CLAUDE_BIN"\s*\\(.*?)"\$TICK_PROMPT"', src, re.DOTALL)
    assert m, "could not locate the claude --print spawn block in loop-backup.sh"
    return m.group(1)


def test_backup_spawn_uses_strict_mcp_config():
    """The headless backup tick must pass --strict-mcp-config (the wedge fix)."""
    block = _spawn_block(_read(BACKUP_SH))
    assert "--strict-mcp-config" in block, (
        "backup spawn missing --strict-mcp-config — the telegram MCP plugin "
        "will load in the headless child and wedge the tick (exit 1)"
    )


def test_backup_spawn_passes_no_mcp_config():
    """--strict-mcp-config with NO --mcp-config = zero MCP servers loaded.

    A regression that ADDS a --mcp-config (re-introducing servers) would
    defeat the fix; assert the spawn loads no MCP config file.
    """
    block = _spawn_block(_read(BACKUP_SH))
    assert "--mcp-config" not in block, (
        "backup spawn must NOT pass --mcp-config (strict-mcp-config + no "
        "config = zero MCP servers, which is the point of the fix)"
    )


def test_backup_spawn_keeps_print_and_setting_sources():
    """We strip MCP only — hooks/permissions/CLAUDE.md (setting-sources user)
    and the one-shot --print posture are intentionally KEPT."""
    block = _spawn_block(_read(BACKUP_SH))
    assert "--print" in block
    assert "--setting-sources user" in block


def test_layer_service_clears_own_failed_state():
    """Unit hygiene: the templated layer service resets its OWN failed marker
    before each run so a one-off non-zero tick never lingers red."""
    svc = _read(LAYER_SVC)
    assert "reset-failed" in svc, (
        "loop-layer@.service must clear its stale `failed` state "
        "(ExecStartPre reset-failed %n) so the timer chain isn't permanently red"
    )
    # Must be best-effort (leading `-`) so a missing grant / not-failed state
    # never blocks the actual tick.
    assert re.search(r"ExecStartPre=-.*reset-failed\s+%n", svc), (
        "reset-failed must be a non-fatal ExecStartPre (`-` prefix) on %n"
    )


def test_installer_grants_scoped_resetfailed_sudoers():
    """The installer must lay down a TIGHTLY SCOPED sudoers grant: only
    reset-failed, only loop-layer@*.service — never a blanket systemctl."""
    inst = _read(INSTALL_SH)
    assert "reset-failed loop-layer@*.service" in inst, (
        "installer must grant claude NOPASSWD for `systemctl reset-failed "
        "loop-layer@*.service` so the ExecStartPre hygiene actually works"
    )
    # Scope guard: the grant must be reset-failed-only (no bare `systemctl *`).
    assert "NOPASSWD: /bin/systemctl reset-failed loop-layer@*.service" in inst, (
        "sudoers grant must be scoped to reset-failed of the layer units only"
    )
    # And it must be visudo-validated before install (never lock sudo).
    assert "visudo -cf" in inst, "sudoers drop-in must be visudo-validated"
