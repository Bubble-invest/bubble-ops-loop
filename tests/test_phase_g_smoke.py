"""
Phase G5 — end-to-end smoke test.

Walks through the whole Phase G chain in one go:
  1. Run bootstrap-dept.sh --dry-run (extends the script to skip gh + push).
  2. Verify CLAUDE.md contains auto-driving instructions.
  3. Verify the generated systemd unit is well-formed (no placeholders).
  4. Verify auto_drive.py can read the initial STATE.yaml + return step 1.
  5. Verify the console route /agents/<slug>/onboarding renders + the
     timeline fragment renders.

5 assertions, all must PASS.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent  # .../projects/bubble-ops-loop
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"

# Make the auto_drive module importable.
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))


SMOKE_SLUG = "smoketest"
SMOKE_DISPLAY = "Smoke Test"


@pytest.fixture
def smoke_dept(tmp_path: Path) -> Path:
    """Run bootstrap-dept.sh --dry-run and return the rendered repo path."""
    script = SCRIPTS_DIR / "bootstrap-dept.sh"
    env = os.environ.copy()
    env["BUBBLE_BOOTSTRAP_CLONE_DIR"] = str(tmp_path)
    res = subprocess.run(
        [
            "bash", str(script),
            f"--slug={SMOKE_SLUG}",
            f"--display-name={SMOKE_DISPLAY}",
            "--owner=operator",
            "--dry-run",
        ],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, \
        f"--dry-run bootstrap failed:\n{res.stdout}\n{res.stderr}"
    target = tmp_path / f"bubble-ops-{SMOKE_SLUG}"
    assert target.exists(), f"bootstrap did not render to {target}"
    return target


def test_smoke_1_claude_md_has_autodriving_instructions(smoke_dept: Path):
    """Assertion 1 — the generated CLAUDE.md has the auto-driving prompt."""
    cm = smoke_dept / "CLAUDE.md"
    assert cm.exists()
    text = cm.read_text(encoding="utf-8")
    # Must reference the SKILL, the Telegram bot, autonomy, the 7 steps.
    assert "department-onboarding-guide" in text
    assert "@bubbleopssmoketest_bot" in text
    assert "autonom" in text.lower()
    assert "français" in text.lower()


def test_smoke_2_systemd_unit_is_well_formed(smoke_dept: Path):
    """Assertion 2 — the systemd unit has no placeholders + is valid."""
    unit = smoke_dept / "deploy" / f"ops-loop-{SMOKE_SLUG}.service"
    assert unit.exists()
    text = unit.read_text(encoding="utf-8")
    # No unsubstituted placeholders
    assert "${DEPT_SLUG}" not in text
    assert "${TELEGRAM_STATE_DIR}" not in text
    assert "${ENV_FILE}" not in text
    # Must have [Unit] / [Service] / [Install] sections
    assert "[Unit]" in text
    assert "[Service]" in text
    assert "[Install]" in text
    # Per-dept env file path is substituted
    assert f"/run/claude-agent-{SMOKE_SLUG}/env" in text
    # Working directory is per-dept
    assert f"/home/claude/agents/{SMOKE_SLUG}" in text


def test_smoke_3_auto_drive_returns_step1_prompt(smoke_dept: Path):
    """Assertion 3 — auto_drive.get_current_step reads STATE.yaml + returns
    'mandate' as the first step; get_step_prompt('mandate') returns a
    non-empty French Bureau-de-Cadre prompt."""
    from skill_lib.auto_drive import get_current_step, get_step_prompt

    state_p = smoke_dept / "onboarding" / "STATE.yaml"
    assert state_p.exists()
    step = get_current_step(state_p)
    assert step == "mandate", \
        f"freshly-bootstrapped dept should be on step 'mandate', got {step!r}"
    prompt = get_step_prompt(step)
    assert len(prompt) > 50
    assert "mandat" in prompt.lower()
    # 3 options pattern
    assert "1." in prompt and "2." in prompt and "3." in prompt


def test_smoke_4_console_renders_onboarding_page_and_timeline_fragment(
    smoke_dept: Path, monkeypatch
):
    """Assertion 4 — the console renders the onboarding page + timeline
    fragment for the freshly-bootstrapped smoke dept."""
    # Point READ_FROM_DISK at the parent of smoke_dept so dept_registry sees it.
    parent = smoke_dept.parent
    monkeypatch.setenv("READ_FROM_DISK", str(parent))
    monkeypatch.setenv("CONSOLE_BEARER_TOKEN", "smoke-bearer")
    # Reset console modules so they re-read env.
    for mod in list(sys.modules):
        if mod == "console" or mod.startswith("console."):
            del sys.modules[mod]
    from console.main import create_app  # noqa: WPS433
    from fastapi.testclient import TestClient
    app = create_app()
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer smoke-bearer"})

    # Full page
    r = client.get(f"/agents/{SMOKE_SLUG}/onboarding")
    assert r.status_code == 200, r.text
    body = r.text
    assert SMOKE_DISPLAY in body
    # Wired HTMX polling for the timeline
    assert f'hx-get="/agents/{SMOKE_SLUG}/onboarding/timeline"' in body
    # Bot handle is present (G4)
    assert "@bubbleopssmoketest_bot" in body

    # Timeline fragment renders
    r2 = client.get(f"/agents/{SMOKE_SLUG}/onboarding/timeline")
    assert r2.status_code == 200, r2.text
    # Fragment, not full page
    assert "<html" not in r2.text.lower()
    # All 7 step bullets
    for step_id in ("mandate", "missions", "layers", "skills_tools",
                    "gates_kpis", "dry_run", "activation"):
        assert f'data-step="{step_id}"' in r2.text, \
            f"timeline missing {step_id}"


def test_smoke_5_initial_state_yaml_is_idea_with_no_validated_steps(
    smoke_dept: Path
):
    """Assertion 5 — the freshly-bootstrapped STATE.yaml is in 'Idea' status
    with an empty validated_steps list (clean baseline for the agent)."""
    state_p = smoke_dept / "onboarding" / "STATE.yaml"
    doc = yaml.safe_load(state_p.read_text(encoding="utf-8"))
    assert doc["status"] == "Idea"
    assert doc["validated_steps"] == []
    assert doc["slug"] == SMOKE_SLUG
    assert doc["display_name"] == SMOKE_DISPLAY
