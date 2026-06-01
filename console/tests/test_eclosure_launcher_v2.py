"""
test_eclosure_launcher_v2.py — GAP 7 integration tests.

Validates that the ops-loop-dept.service.template is truly dept-agnostic
and works for ANY slug, including management-level depts like "tony".

No level-management special-casing should exist in the template — Tony is
just another slug substitution.

Gap 7 requirement: render for slug=tony, assert output contains:
  - ops-loop-tony
  - /home/claude/agents/tony
  - /run/claude-agent-tony
  - /etc/bubble/secrets-tony.sops.env
  - No remaining ${} placeholders

Also verifies the template renders identically for ops-level slugs (maya,
ben, miranda, eliot) — same code path, same template.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _render_for_slug(slug: str, monkeypatch, tmp_path: Path) -> str:
    """Load the real template via render_systemd_unit(), return the result."""
    from console.services import eclosure_launcher

    # Use the real template (not a stub) — that is the whole point of GAP 7.
    real_template = eclosure_launcher.SYSTEMD_TEMPLATE_PATH
    assert real_template.exists(), (
        f"Template not found at {real_template} — cannot validate GAP 7"
    )
    return eclosure_launcher.render_systemd_unit(slug)


# ── GAP 7 — management dept (tony) ───────────────────────────────────────────

class TestRenderSystemdUnitForManagementDept:
    """Render for slug=tony; verify the template substitutes correctly."""

    def test_render_systemd_unit_for_management_dept(self, monkeypatch, tmp_path):
        """
        test_render_systemd_unit_for_management_dept — core GAP 7 requirement.

        Renders the ops-loop-dept.service.template for slug=tony and asserts
        the output contains the expected dept-specific paths and identifiers.
        No management-level special-casing should exist — same template works.
        """
        rendered = _render_for_slug("tony", monkeypatch, tmp_path)

        # Service description + unit name contain the slug
        assert "ops-loop-tony" in rendered, (
            "Rendered unit must reference 'ops-loop-tony' (from [Unit] Description or template comment)"
        )

        # WorkingDirectory uses the slug
        assert "/home/claude/agents/tony" in rendered, (
            "WorkingDirectory must be /home/claude/agents/tony"
        )

        # Runtime tmpfs dir for this dept
        assert "/run/claude-agent-tony" in rendered, (
            "ExecStartPre dirs and EnvironmentFile must reference /run/claude-agent-tony"
        )

        # SOPS secrets file for this dept
        assert "/etc/bubble/secrets-tony.sops.env" in rendered, (
            "SOPS decrypt ExecStartPre must reference /etc/bubble/secrets-tony.sops.env"
        )

        # EnvironmentFile path must be correct
        assert "/run/claude-agent-tony/env" in rendered, (
            "EnvironmentFile must be /run/claude-agent-tony/env"
        )

        # Telegram state dir must be correct
        assert "telegram-tony" in rendered, (
            "TELEGRAM_STATE_DIR must contain 'telegram-tony'"
        )

    def test_no_remaining_placeholders_for_tony(self, monkeypatch, tmp_path):
        """All ${PLACEHOLDER} tokens must be substituted — none left over."""
        rendered = _render_for_slug("tony", monkeypatch, tmp_path)
        remaining = re.findall(r"\$\{[A-Z_]+\}", rendered)
        assert remaining == [], (
            f"Unsubstituted placeholders remain after render for 'tony': {remaining}"
        )

    def test_no_fixture_leakage_in_tony_unit(self, monkeypatch, tmp_path):
        """The rendered unit for 'tony' must not reference fixture paths."""
        rendered = _render_for_slug("tony", monkeypatch, tmp_path)
        # These would indicate template pollution from a different dept
        assert "/home/claude/agents/fixture" not in rendered
        assert "/run/claude-agent-fixture" not in rendered
        assert "/etc/bubble/secrets-fixture.sops.env" not in rendered


# ── Template is dept-agnostic — verify for other slugs ───────────────────────

@pytest.mark.parametrize("slug", ["maya", "ben", "miranda", "eliot"])
class TestRenderSystemdUnitForOpsDepts:
    """The same template must work for all ops-level dept slugs."""

    def test_slug_appears_in_rendered_unit(self, slug, monkeypatch, tmp_path):
        rendered = _render_for_slug(slug, monkeypatch, tmp_path)
        assert f"ops-loop-{slug}" in rendered
        assert f"/home/claude/agents/{slug}" in rendered
        assert f"/run/claude-agent-{slug}" in rendered
        assert f"telegram-{slug}" in rendered

    def test_no_remaining_placeholders(self, slug, monkeypatch, tmp_path):
        rendered = _render_for_slug(slug, monkeypatch, tmp_path)
        remaining = re.findall(r"\$\{[A-Z_]+\}", rendered)
        assert remaining == [], (
            f"Unsubstituted placeholders remain for slug='{slug}': {remaining}"
        )


# ── Fixture service on Morty matches template rendering ──────────────────────

class TestTemplateMatchesLiveFixture:
    """
    Sanity check: render for 'fixture' and verify the output is structurally
    identical to what eclosure_launcher would produce. The live unit on Morty
    (ops-loop-fixture.service) pre-dates the template and has diverged
    (fixture-specific PEM block, BUBBLE_BROKER_PEM_PATH, etc.), so we only
    assert that the SHARED structural elements are correct, not byte-equality.
    """

    def test_render_for_fixture_slug(self, monkeypatch, tmp_path):
        rendered = _render_for_slug("fixture", monkeypatch, tmp_path)
        assert "ops-loop-fixture" in rendered
        assert "/home/claude/agents/fixture" in rendered
        assert "/run/claude-agent-fixture" in rendered
        assert "telegram-fixture" in rendered
        remaining = re.findall(r"\$\{[A-Z_]+\}", rendered)
        assert remaining == []


# ── G-3 — ExecStartPost loop re-init in the systemd template ─────────────────

class TestExecStartPostReinit:
    """
    G-3: The systemd template must include both ExecStartPost lines so that
    any service restart (including after Restart=on-failure) triggers an
    automatic /loop re-registration via Telegram.

    Requirements:
      - ExecStartPost=+/bin/sleep 5  (settle wait for Claude TUI)
      - ExecStartPost=+/usr/local/bin/bubble-loop-reinit.sh <slug>  (reinit trigger)
    """

    def test_execstartpost_sleep_present_for_tony(self, monkeypatch, tmp_path):
        """G-3a: Template must include ExecStartPost sleep line."""
        rendered = _render_for_slug("tony", monkeypatch, tmp_path)
        assert "ExecStartPost=+/bin/sleep 5" in rendered, (
            "Template must contain 'ExecStartPost=+/bin/sleep 5' to settle Claude TUI "
            "before sending the re-init Telegram message"
        )

    def test_execstartpost_reinit_script_present_for_tony(self, monkeypatch, tmp_path):
        """G-3b: Template must include ExecStartPost bubble-loop-reinit.sh with slug substituted."""
        rendered = _render_for_slug("tony", monkeypatch, tmp_path)
        assert "bubble-loop-reinit.sh" in rendered, (
            "Template must reference 'bubble-loop-reinit.sh' in ExecStartPost"
        )
        assert "ExecStartPost=+/usr/local/bin/bubble-loop-reinit.sh tony" in rendered, (
            "Template must contain 'ExecStartPost=+/usr/local/bin/bubble-loop-reinit.sh tony' "
            "(slug substituted, not ${DEPT_SLUG})"
        )

    def test_execstartpost_reinit_script_present_for_fixture(self, monkeypatch, tmp_path):
        """G-3c: Render for slug=fixture — slug correctly substituted in ExecStartPost."""
        rendered = _render_for_slug("fixture", monkeypatch, tmp_path)
        assert "ExecStartPost=+/usr/local/bin/bubble-loop-reinit.sh fixture" in rendered, (
            "Template render for slug=fixture must produce "
            "'ExecStartPost=+/usr/local/bin/bubble-loop-reinit.sh fixture'"
        )

    def test_no_remaining_dept_slug_placeholders_after_adding_execstartpost(
        self, monkeypatch, tmp_path
    ):
        """G-3d: No ${DEPT_SLUG} placeholders remain after render (template is complete)."""
        rendered = _render_for_slug("maya", monkeypatch, tmp_path)
        remaining = re.findall(r"\$\{[A-Z_]+\}", rendered)
        assert remaining == [], (
            f"Unsubstituted placeholders after adding ExecStartPost lines: {remaining}"
        )


# ── G-4 — PEM-decrypt block in systemd template ───────────────────────────────

class TestPemDecryptBlockInTemplate:
    """
    G-4: The systemd template must include the 3 ExecStartPre lines for
    pre-decrypting the GitHub App PEM key into a tmpfs path.

    The live ops-loop-fixture.service on Morty has these lines; the template
    was missing them. Per Team C's report, Tony/Maya cannot mint broker tokens
    without the PEM being pre-decrypted before Claude starts.

    The 3 lines (with ${DEPT_SLUG} substituted to the actual slug):
      ExecStartPre=+/bin/mkdir -p /run/bubble-<slug>
      ExecStartPre=+/bin/chown claude:claude /run/bubble-<slug>
      ExecStartPre=+/bin/chmod 0750 /run/bubble-<slug>
      ExecStartPre=+/bin/sh -c 'SOPS_AGE_KEY_FILE=... sops ... > /run/bubble-<slug>/pem.tmp'
      ExecStartPre=+/bin/mv /run/bubble-<slug>/pem.tmp /run/bubble-<slug>/pem
      ExecStartPre=+/bin/chmod 0400 /run/bubble-<slug>/pem
      ExecStartPre=+/bin/chown claude:claude /run/bubble-<slug>/pem

    Note: the live fixture has 3 blocks (mkdir, sops, mv) — we assert the
    key identifying lines are present in the rendered template.
    """

    def test_pem_decrypt_block_present_for_tony(self, monkeypatch, tmp_path):
        """G-4a: Render for tony — PEM tmpfs dir and sops decrypt lines are present."""
        rendered = _render_for_slug("tony", monkeypatch, tmp_path)
        assert "/run/bubble-tony" in rendered, (
            "Rendered unit for tony must reference /run/bubble-tony (PEM tmpfs dir)"
        )
        assert "github-app-bubble-ops-bot.private-key.sops.pem" in rendered, (
            "Rendered unit must reference the SOPS PEM path for broker pre-decryption"
        )
        assert "BUBBLE_BROKER_PEM_PATH" in rendered, (
            "Rendered unit must set BUBBLE_BROKER_PEM_PATH env var pointing to the tmpfs PEM"
        )

    def test_pem_decrypt_block_present_for_fixture(self, monkeypatch, tmp_path):
        """G-4b: Render for fixture — PEM block uses 'fixture' slug correctly."""
        rendered = _render_for_slug("fixture", monkeypatch, tmp_path)
        assert "/run/bubble-fixture" in rendered, (
            "Rendered unit for fixture must reference /run/bubble-fixture"
        )

    def test_pem_decrypt_block_no_remaining_placeholders(self, monkeypatch, tmp_path):
        """G-4c: After adding PEM block, no ${PLACEHOLDER} left for any slug."""
        for slug in ["tony", "maya", "fixture"]:
            rendered = _render_for_slug(slug, monkeypatch, tmp_path)
            remaining = re.findall(r"\$\{[A-Z_]+\}", rendered)
            assert remaining == [], (
                f"Slug={slug}: unsubstituted placeholders after G-4 patch: {remaining}"
            )
