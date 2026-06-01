"""
test_isolation_scaffold.py — the onboarding template now SCAFFOLDS the per-dept
isolation surface + the anti-regression test triple for every new dept.

Root-cause propagation: the systemic audit found that the template never generated
(a) the .gitkeep queue/inbox dirs (CGP CRIT-1 crash on fresh clone), (b) the
.claude/settings.json + SessionStart hook + the 4 subagent personas (the isolation
gap — Maya herself lacked it), or (c) the Part-A anti-regression tests. These tests
lock that propagation in.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from skill_lib import isolation_scaffold as iso


@pytest.fixture()
def scaffolded(tmp_path):
    dept_root = tmp_path / "bubble-ops-newdept"
    dept_root.mkdir()
    written = iso.scaffold_isolation_surface(
        dept_root,
        slug="newdept",
        display_name="NewDept",
        level="management",
        enabled_skills=["alpha-skill", "beta-skill", "google-workspace"],
        all_dept_slugs=["newdept", "tony", "cgp", "maya"],
        model="claude-opus-4-8[1m]",
    )
    return dept_root, written


# -------------------------------------------------------------------------
# 1) .gitkeep for queue/inbox dirs (CGP CRIT-1)
# -------------------------------------------------------------------------
def test_scaffold_creates_queue_and_inbox_gitkeeps(scaffolded):
    dept_root, _ = scaffolded
    for d in ("research", "gates", "management", "improvements"):
        assert (dept_root / "queues" / d / ".gitkeep").is_file(), f"missing queues/{d}/.gitkeep"
    for d in ("decisions", "feedback"):
        assert (dept_root / "inbox" / d / ".gitkeep").is_file(), f"missing inbox/{d}/.gitkeep"


# -------------------------------------------------------------------------
# 2) .claude/settings.json — valid JSON, dept-scoped, deny-list isolates
# -------------------------------------------------------------------------
def test_scaffold_settings_json_valid_and_scoped(scaffolded):
    dept_root, _ = scaffolded
    settings = dept_root / ".claude" / "settings.json"
    assert settings.is_file()
    data = json.loads(settings.read_text())  # raises on malformed JSON
    for key in ("permissions", "enabledPlugins", "enabledSkills", "model", "env", "hooks"):
        assert key in data, f"settings.json missing {key}"
    assert data["env"]["BUBBLE_DEPT"] == "newdept"
    assert data["env"]["BUBBLE_DEPT_ROOT"] == "/home/claude/agents/bubble-ops-newdept"
    assert data["env"]["BUBBLE_DEPT_LEVEL"] == "management"
    assert data["model"] == "claude-opus-4-8[1m]"
    assert set(data["enabledSkills"]) == {"alpha-skill", "beta-skill", "google-workspace"}


def test_scaffold_settings_deny_isolates_other_depts(scaffolded):
    dept_root, _ = scaffolded
    data = json.loads((dept_root / ".claude" / "settings.json").read_text())
    deny = " ".join(data["permissions"]["deny"])
    # Sibling depts are denied; this dept's OWN tree is not in deny.
    assert "bubble-ops-tony" in deny and "bubble-ops-cgp" in deny
    assert "bubble-ops-newdept" not in deny
    # SOPS / secret sources hard-denied; push is broker-only.
    assert "/etc/bubble" in deny and "git push" in deny


def test_scaffold_settings_hook_wired(scaffolded):
    dept_root, _ = scaffolded
    data = json.loads((dept_root / ".claude" / "settings.json").read_text())
    cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cmd == "/home/claude/agents/bubble-ops-newdept/.claude/hooks/session-start.sh"


# -------------------------------------------------------------------------
# 3) session-start hook — present + executable
# -------------------------------------------------------------------------
def test_scaffold_hook_executable(scaffolded):
    dept_root, _ = scaffolded
    hook = dept_root / ".claude" / "hooks" / "session-start.sh"
    assert hook.is_file()
    assert hook.stat().st_mode & stat.S_IXUSR, "session-start.sh must be executable"
    assert hook.read_text().startswith("#!/usr/bin/env bash")


# -------------------------------------------------------------------------
# 4) the four mandated personas — present + scoped
# -------------------------------------------------------------------------
@pytest.mark.parametrize("persona", iso.MANDATED_PERSONAS)
def test_scaffold_persona_present_and_scoped(scaffolded, persona):
    dept_root, _ = scaffolded
    f = dept_root / "subagents" / f"{persona}.md"
    assert f.is_file(), f"missing persona {persona}"
    body = f.read_text()
    assert "tools:" in body and "permission-mode:" in body
    assert "Forbidden" in body
    assert "newdept" in body  # parameterised to the dept


# -------------------------------------------------------------------------
# 5) the generated anti-regression test triple is present + valid Python
# -------------------------------------------------------------------------
def test_scaffold_emits_anti_regression_test(scaffolded):
    dept_root, _ = scaffolded
    art = dept_root / "tests" / "test_anti_regression_coverage.py"
    assert art.is_file(), "the Part-A anti-regression test must be scaffolded"
    src = art.read_text()
    compile(src, str(art), "exec")  # must be valid Python (Jinja rendered cleanly)
    # Covers all three dimensions + the DRY_RUN guard.
    assert "test_dim1_" in src
    assert "test_dim2_every_python_block_compiles" in src
    assert "test_dim3_no_active_tool_returns_noop_shim" in src
    assert "test_dry_run_does_not_mutate_repo" in src
    # Parameterised slug landed in the DRY_RUN repo target.
    assert "bubble-ops-newdept" in src
