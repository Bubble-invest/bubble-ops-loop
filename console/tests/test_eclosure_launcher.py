"""
test_eclosure_launcher.py — unit tests for console/services/eclosure_launcher.py.

The launcher owns the post-bootstrap chain:
  1. Create per-dept SOPS env file with DEPT_TELEGRAM_BOT_TOKEN.
  2. Render systemd unit template + install at /etc/systemd/system/.
  3. Enable + start ops-loop-<slug>.service.
  4. (Best-effort) install the GitHub App on the new repo via API.

All side-effecting calls (sops, systemctl, gh) are monkeypatched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_telegram_token_validation_accepts_valid_shape():
    from console.services.eclosure_launcher import is_valid_telegram_bot_token
    assert is_valid_telegram_bot_token("12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa") is True
    assert is_valid_telegram_bot_token("8590766379:AABCDEFG_hijklmnopqrstuvwxyz12345-_") is True


def test_telegram_token_validation_rejects_garbage():
    from console.services.eclosure_launcher import is_valid_telegram_bot_token
    assert is_valid_telegram_bot_token("") is False
    assert is_valid_telegram_bot_token("not-a-token") is False
    assert is_valid_telegram_bot_token("12345:short") is False
    assert is_valid_telegram_bot_token("abc:xyz") is False
    # missing colon
    assert is_valid_telegram_bot_token("123456789abc") is False


def test_render_systemd_unit_substitutes_slug(tmp_path: Path, monkeypatch):
    from console.services import eclosure_launcher
    template_text = (
        "Description=Claude Agent — ops-loop-${DEPT_SLUG}\n"
        "WorkingDirectory=/home/claude/agents/${DEPT_SLUG}\n"
        "ExecStartPre=+/bin/mkdir -p /run/claude-agent-${DEPT_SLUG}\n"
        "ExecStartPre=+/bin/sh -c '... /etc/bubble/secrets-${DEPT_SLUG}.sops.env'\n"
        "EnvironmentFile=-${ENV_FILE}\n"
    )
    tpl_path = tmp_path / "tpl.service.template"
    tpl_path.write_text(template_text, encoding="utf-8")
    monkeypatch.setattr(eclosure_launcher, "SYSTEMD_TEMPLATE_PATH", tpl_path)

    rendered = eclosure_launcher.render_systemd_unit("maya")
    assert "ops-loop-maya" in rendered
    assert "/home/claude/agents/maya" in rendered
    assert "/run/claude-agent-maya" in rendered
    assert "/etc/bubble/secrets-maya.sops.env" in rendered
    assert "/run/claude-agent-maya/env" in rendered
    # placeholders fully substituted (no ${} left)
    assert "${DEPT_SLUG}" not in rendered
    assert "${ENV_FILE}" not in rendered


def test_launch_dispatches_4_steps_in_order(tmp_path: Path, monkeypatch):
    """launch() must run, in order: create_sops_env -> install_systemd_unit
    -> systemctl_enable_start -> install_github_app. Each step is observed
    by appending its name to the calls log."""
    from console.services import eclosure_launcher

    calls = []
    monkeypatch.setattr(
        eclosure_launcher,
        "create_per_dept_sops_env",
        lambda slug, token: calls.append(f"sops:{slug}"),
    )
    monkeypatch.setattr(
        eclosure_launcher,
        "install_systemd_unit",
        lambda slug: calls.append(f"install:{slug}"),
    )
    monkeypatch.setattr(
        eclosure_launcher,
        "systemctl_enable_and_start",
        lambda slug: calls.append(f"start:{slug}"),
    )
    monkeypatch.setattr(
        eclosure_launcher,
        "try_install_github_app",
        lambda slug: calls.append(f"gh-app:{slug}") or {"ok": True, "installation_id": 42},
    )

    result = eclosure_launcher.launch(
        slug="maya",
        telegram_bot_token="12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
    )
    assert calls == ["sops:maya", "install:maya", "start:maya", "gh-app:maya"]
    assert result["ok"] is True
    assert result["github_app"]["ok"] is True


def test_launch_rejects_invalid_token(tmp_path: Path):
    from console.services import eclosure_launcher

    with pytest.raises(ValueError, match=r"telegram.*token.*shape"):
        eclosure_launcher.launch(slug="maya", telegram_bot_token="garbage")


def test_try_install_github_app_targets_settings_org(monkeypatch):
    """try_install_github_app must build the repo path from settings.GITHUB_ORG,
    not a hardcoded org — a hardcoded org 404s during éclosure for any org other
    than the one baked in, silently failing to grant the App repo access
    (board #448)."""
    from console.services import eclosure_launcher

    monkeypatch.setattr(eclosure_launcher.settings, "GITHUB_ORG", "Bubble-invest-custom")

    captured_cmds = []

    def fake_run(cmd, *a, **kw):
        captured_cmds.append(cmd)

        class R:
            returncode = 1
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(eclosure_launcher.subprocess, "run", fake_run)

    eclosure_launcher.try_install_github_app("maya")

    assert captured_cmds, "expected at least one gh api call"
    joined = " ".join(" ".join(cmd) for cmd in captured_cmds)
    assert "repos/Bubble-invest-custom/bubble-ops-maya" in joined, joined
    assert "vdk888" not in joined


def test_launch_swallows_github_app_failure_but_reports_it(monkeypatch):
    """If the GitHub App install fails (403, etc.), launch() must still
    return ok=True for the local éclosure, and surface a github_app.error
    so the UI can show a manual-fallback message."""
    from console.services import eclosure_launcher

    monkeypatch.setattr(eclosure_launcher, "create_per_dept_sops_env", lambda *a, **kw: None)
    monkeypatch.setattr(eclosure_launcher, "install_systemd_unit", lambda *a, **kw: None)
    monkeypatch.setattr(eclosure_launcher, "systemctl_enable_and_start", lambda *a, **kw: None)
    monkeypatch.setattr(
        eclosure_launcher,
        "try_install_github_app",
        lambda slug: {"ok": False, "error": "403 Forbidden"},
    )

    result = eclosure_launcher.launch(
        slug="maya",
        telegram_bot_token="12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
    )
    assert result["ok"] is True
    assert result["github_app"]["ok"] is False
    assert "403" in result["github_app"]["error"]


def test_progress_events_emitted_for_each_step(monkeypatch):
    """launch() should emit progress events to a passed-in callback so the
    SSE endpoint can stream them to the browser in real time."""
    from console.services import eclosure_launcher

    monkeypatch.setattr(eclosure_launcher, "create_per_dept_sops_env", lambda *a, **kw: None)
    monkeypatch.setattr(eclosure_launcher, "install_systemd_unit", lambda *a, **kw: None)
    monkeypatch.setattr(eclosure_launcher, "systemctl_enable_and_start", lambda *a, **kw: None)
    monkeypatch.setattr(eclosure_launcher, "try_install_github_app", lambda *a, **kw: {"ok": True, "installation_id": 1})

    events = []
    eclosure_launcher.launch(
        slug="maya",
        telegram_bot_token="12345678:AAGreatExampleTokenAaaaaaaaaaaaaaaa",
        on_progress=lambda ev: events.append(ev),
    )
    # Expect at least: start, sops_done, systemd_installed, service_started, gh_app_done, done
    kinds = [e["kind"] for e in events]
    assert "start" in kinds
    assert "sops_done" in kinds
    assert "systemd_installed" in kinds
    assert "service_started" in kinds
    assert "gh_app_done" in kinds
    assert "done" in kinds
