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
# 1b) .gitignore — keeps runtime artifacts/secrets/vault out of the ops-repo
#     so a stray non-allow-listed file never 403s the dept's runtime push
#     (the 2026-06-05 ben/maya/tony push-block).
# -------------------------------------------------------------------------
def test_scaffold_creates_gitignore_with_push_guards(scaffolded):
    dept_root, _ = scaffolded
    slug = "newdept"  # matches the `scaffolded` fixture
    gi = dept_root / ".gitignore"
    assert gi.is_file(), "missing .gitignore"
    content = gi.read_text()
    # vault lives in its own repo, never tracked here (else it blocks pushes)
    assert "vault/" in content
    # stray runtime DBs at root must never be tracked (root paths aren't
    # in the runtime_write_own allow-list)
    assert "/*.sqlite" in content
    # the .claude scheduled-tasks lock must never be tracked
    assert ".claude/scheduled_tasks.lock" in content
    # secrets / env never tracked
    assert "*.sops.env" in content
    # the vault note is parametrised to this dept's vault repo
    assert f"bubble-{slug}-vault" in content


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
    # Fleet-standard skills (e.g. plan-executor, board #911 part 2) are added
    # on top of the dept's own list, not instead of it.
    assert {"alpha-skill", "beta-skill", "google-workspace"} <= set(data["enabledSkills"])


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
# 4b) per-dept model pin (fleet cost-optimization, 2026-06-19)
#     - absent in dept.yaml  -> DEFAULT_MODEL (existing depts unchanged)
#     - present in dept.yaml  -> honoured verbatim and flows into settings.json
# -------------------------------------------------------------------------
def test_model_from_dept_yaml_defaults_when_absent():
    # No `department.model` -> existing depts keep the platform Opus pin.
    assert iso.model_from_dept_yaml({"department": {"slug": "ben"}}) == iso.DEFAULT_MODEL
    assert iso.model_from_dept_yaml({}) == iso.DEFAULT_MODEL
    assert iso.model_from_dept_yaml(None) == iso.DEFAULT_MODEL
    # Empty / whitespace-only is treated as unset.
    assert iso.model_from_dept_yaml({"department": {"model": "  "}}) == iso.DEFAULT_MODEL


def test_model_from_dept_yaml_honours_explicit_pin():
    dept_yaml = {"department": {"slug": "ben", "model": "sonnet"}}
    assert iso.model_from_dept_yaml(dept_yaml) == "sonnet"
    # A dept that must stay Opus pins it explicitly.
    assert (
        iso.model_from_dept_yaml({"department": {"model": "claude-opus-4-8[1m]"}})
        == "claude-opus-4-8[1m]"
    )


def test_scaffold_writes_per_dept_model_into_settings(tmp_path):
    # The resolved per-dept model lands in .claude/settings.json `model`.
    dept_root = tmp_path / "bubble-ops-ben"
    dept_root.mkdir()
    dept_yaml = {"department": {"slug": "ben", "model": "sonnet"}}
    iso.scaffold_isolation_surface(
        dept_root,
        slug="ben",
        display_name="Ben",
        level="ops",
        enabled_skills=["alpaca"],
        all_dept_slugs=["ben", "tony", "maya"],
        model=iso.model_from_dept_yaml(dept_yaml),
    )
    data = json.loads((dept_root / ".claude" / "settings.json").read_text())
    assert data["model"] == "sonnet"


# -------------------------------------------------------------------------
# 4c) fleet model doctrine (2026-06-19 REVISED): Opus orchestrator delegates
#     execution to SONNET subagents. The orchestrator model stays Opus by
#     default; the execution subagents (executor / task-orchestrator /
#     mandate-guardian) default to Sonnet for cost. Guards against a silent
#     flip back to the earlier (superseded) cheap-orchestrator/opus-subagent
#     inversion.
# -------------------------------------------------------------------------
def test_subagent_model_defaults_to_sonnet():
    import inspect

    sig = inspect.signature(iso.scaffold_isolation_surface)
    assert sig.parameters["subagent_model"].default == "sonnet"


def test_execution_subagents_render_sonnet(tmp_path):
    dept_root = tmp_path / "bubble-ops-probe"
    dept_root.mkdir()
    iso.scaffold_isolation_surface(
        dept_root,
        slug="probe",
        display_name="Probe",
        level="ops",
        enabled_skills=["x"],
        all_dept_slugs=["probe", "ben", "maya"],
    )

    def model_line(name):
        txt = (dept_root / "subagents" / name).read_text()
        return next(l for l in txt.splitlines() if l.startswith("model:"))

    # All four subagent personas run the cheap model — the Opus orchestrator
    # delegates every layer's bounded work (curation, planning, execution,
    # guardrail) down to Sonnet workers.
    for persona in (
        "data-curator.md",
        "task-orchestrator.md",
        "executor.md",
        "mandate-guardian.md",
    ):
        assert model_line(persona) == "model: sonnet", persona


# -------------------------------------------------------------------------
# 4d) `department.subagent_model` dept.yaml knob (board #908) — pins the
#     scaffolded subagents to a specific model version instead of drifting
#     with whatever the `sonnet` alias resolves to.
#     - absent in dept.yaml -> DEFAULT_SUBAGENT_MODEL ("sonnet", today's
#       default behaviour — no regression for existing depts)
#     - present in dept.yaml -> honoured verbatim and flows into every
#       scaffolded subagent's `model:` frontmatter line
# -------------------------------------------------------------------------
def test_subagent_model_from_dept_yaml_defaults_when_absent():
    assert (
        iso.subagent_model_from_dept_yaml({"department": {"slug": "ben"}})
        == iso.DEFAULT_SUBAGENT_MODEL
    )
    assert iso.subagent_model_from_dept_yaml({}) == iso.DEFAULT_SUBAGENT_MODEL
    assert iso.subagent_model_from_dept_yaml(None) == iso.DEFAULT_SUBAGENT_MODEL
    # Empty / whitespace-only is treated as unset.
    assert (
        iso.subagent_model_from_dept_yaml({"department": {"subagent_model": "  "}})
        == iso.DEFAULT_SUBAGENT_MODEL
    )


def test_subagent_model_from_dept_yaml_honours_explicit_pin():
    dept_yaml = {"department": {"slug": "ben", "subagent_model": "claude-opus-4-8"}}
    assert iso.subagent_model_from_dept_yaml(dept_yaml) == "claude-opus-4-8"


def test_scaffold_renders_pinned_subagent_model_into_frontmatter(tmp_path):
    """dept.yaml sets `department.subagent_model: claude-opus-4-8` -> the
    caller resolves it via subagent_model_from_dept_yaml and passes it
    through to scaffold_isolation_surface's existing `subagent_model` param
    -> every scaffolded subagent's frontmatter carries the pinned model."""
    dept_root = tmp_path / "bubble-ops-pinned"
    dept_root.mkdir()
    dept_yaml = {
        "department": {"slug": "pinned", "subagent_model": "claude-opus-4-8"}
    }
    iso.scaffold_isolation_surface(
        dept_root,
        slug="pinned",
        display_name="Pinned",
        level="ops",
        enabled_skills=["x"],
        all_dept_slugs=["pinned", "ben", "maya"],
        subagent_model=iso.subagent_model_from_dept_yaml(dept_yaml),
    )

    def model_line(name):
        txt = (dept_root / "subagents" / name).read_text()
        return next(l for l in txt.splitlines() if l.startswith("model:"))

    for persona in (
        "data-curator.md",
        "task-orchestrator.md",
        "executor.md",
        "mandate-guardian.md",
    ):
        assert model_line(persona) == "model: claude-opus-4-8", persona


def test_scaffold_subagent_model_absent_field_keeps_default_behavior(tmp_path):
    """No `department.subagent_model` in dept.yaml -> caller resolves via
    subagent_model_from_dept_yaml -> DEFAULT_SUBAGENT_MODEL ("sonnet") ->
    unchanged from today's default. Proves the omitted-field path is a
    no-op / non-breaking default, matching `model_from_dept_yaml`'s
    established back-compat contract."""
    dept_root = tmp_path / "bubble-ops-unpinned"
    dept_root.mkdir()
    dept_yaml = {"department": {"slug": "unpinned"}}  # no subagent_model key
    iso.scaffold_isolation_surface(
        dept_root,
        slug="unpinned",
        display_name="Unpinned",
        level="ops",
        enabled_skills=["x"],
        all_dept_slugs=["unpinned", "ben", "maya"],
        subagent_model=iso.subagent_model_from_dept_yaml(dept_yaml),
    )
    body = (dept_root / "subagents" / "executor.md").read_text()
    model_line = next(l for l in body.splitlines() if l.startswith("model:"))
    assert model_line == "model: sonnet"
    assert model_line == f"model: {iso.DEFAULT_SUBAGENT_MODEL}"


# -------------------------------------------------------------------------
# 4e) fleet-standard agents (board #911 part 2) — plan-executor folded into
#     the scaffold so new depts/machines get it via the standard deploy
#     (git clone of the dept repo) instead of manual scp to ~/.claude/.
# -------------------------------------------------------------------------
def test_scaffold_installs_fleet_plan_executor_agent(scaffolded):
    dept_root, written = scaffolded
    f = dept_root / ".claude" / "agents" / "plan-executor.md"
    assert f.is_file(), "plan-executor.md must be scaffolded into .claude/agents/"
    assert f in written


def test_scaffold_plan_executor_model_pin_preserved(scaffolded):
    """The version pin lives in the agent definition's own frontmatter (board
    #908 doctrine) and must survive vendoring UNCHANGED — it is a static
    copy, never Jinja-rendered, so a per-dept substitution can't touch it."""
    dept_root, _ = scaffolded
    body = (dept_root / ".claude" / "agents" / "plan-executor.md").read_text()
    assert "model: claude-opus-4-6" in body


def test_scaffold_installs_fleet_plan_executor_skill(scaffolded):
    dept_root, written = scaffolded
    f = dept_root / ".claude" / "skills" / "plan-executor" / "SKILL.md"
    assert f.is_file(), "plan-executor SKILL.md must be scaffolded into .claude/skills/"
    assert f in written
    assert "plan-executor" in f.read_text()


def test_scaffold_enables_plan_executor_skill_by_default(scaffolded):
    """Even when the caller's enabled_skills list (Step 4's dept-declared
    skills) doesn't mention it, plan-executor is a fleet-standard skill and
    must show up in the rendered enabledSkills allowlist."""
    dept_root, _ = scaffolded
    data = json.loads((dept_root / ".claude" / "settings.json").read_text())
    assert "plan-executor" in data["enabledSkills"]


def test_scaffold_fleet_agents_content_matches_source_verbatim(scaffolded):
    """Vendored copy must be byte-identical to the canonical skill_lib source
    (a static fleet artifact, not a template — no drift allowed)."""
    from skill_lib.isolation_scaffold import _FLEET_DIR

    dept_root, _ = scaffolded
    scaffolded_agent = (dept_root / ".claude" / "agents" / "plan-executor.md").read_text()
    canonical_agent = (_FLEET_DIR / "agents" / "plan-executor.md").read_text()
    assert scaffolded_agent == canonical_agent

    scaffolded_skill = (
        dept_root / ".claude" / "skills" / "plan-executor" / "SKILL.md"
    ).read_text()
    canonical_skill = (_FLEET_DIR / "skills" / "plan-executor" / "SKILL.md").read_text()
    assert scaffolded_skill == canonical_skill


def test_scaffold_fleet_agents_idempotent_on_rerun(tmp_path):
    dept_root = tmp_path / "bubble-ops-rerun"
    dept_root.mkdir()
    kwargs = dict(
        slug="rerun",
        display_name="Rerun",
        level="ops",
        enabled_skills=["x"],
        all_dept_slugs=["rerun", "ben"],
    )
    iso.scaffold_isolation_surface(dept_root, **kwargs)
    first = (dept_root / ".claude" / "agents" / "plan-executor.md").read_text()
    iso.scaffold_isolation_surface(dept_root, **kwargs)
    second = (dept_root / ".claude" / "agents" / "plan-executor.md").read_text()
    assert first == second


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
