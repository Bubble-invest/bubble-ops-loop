"""
isolation_scaffold.py — generate the per-dept isolation + anti-regression surface.

Root-cause fix propagated UP into the onboarding template so EVERY new dept is
born with the surface the architecture mandates (notion_architecture.md ~12, ~30,
~551-570) AND the anti-regression test triple — instead of each fixer retrofitting
it dept-by-dept (the systemic gap: Maya herself lacked it, proving the template
never generated it).

What `scaffold_isolation_surface()` writes into a dept root:

  queues/{research,gates,management,improvements}/.gitkeep   (CGP CRIT-1)
  inbox/{decisions,feedback}/.gitkeep
  .claude/settings.json            (dept-scoped perms / skills / hooks / env)
  .claude/hooks/session-start.sh   (SessionStart hook, chmod +x)
  subagents/{data-curator,task-orchestrator,executor,mandate-guardian}.md
  .claude/agents/plan-executor.md          (fleet-standard, board #911 part 2)
  .claude/skills/plan-executor/SKILL.md    (fleet-standard, board #911 part 2)
  tests/test_anti_regression_coverage.py   (the Part-A triple, dept-agnostic)

All per-dept bits (slug, display_name, level, enabled_skills, model, the sibling
dept slugs to deny) are parameterised via the skill's existing Jinja2 renderer
(skill_lib.templates._env / FileSystemLoader). Deterministic: same input -> same
output. Idempotent on dirs (exist_ok); files are overwritten with the rendered
canonical version.

Fleet-standard agents (board #911 part 2): unlike the four MANDATED_PERSONAS
above (which are dept-parameterised — their body text is rendered per-slug),
a fleet-standard agent is a STATIC artifact shared verbatim by every dept/machine
on the platform (its whole point is to be identical everywhere, e.g. the
version-pinned `model:` frontmatter). These are copied byte-for-byte from
`templates/fleet/` into `.claude/agents/<name>.md` + `.claude/skills/<name>/` in
the dept root — Claude Code auto-discovers project-scoped `.claude/agents/` and
`.claude/skills/` from the session's cwd, so a fresh `git clone` of the dept repo
(what the standard deploy already does — see `scripts/deploy-to-morty.sh`) is
enough; no manual `scp` to the machine's `~/.claude/` is needed anymore. Also
auto-added to `enabledSkills` so the skill is usable by default.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Iterable

import jinja2

# Reuse the skill's Jinja2 env but point the loader at templates/isolation/.
_ISOLATION_DIR = Path(__file__).resolve().parent.parent / "templates" / "isolation"

# Canonical, machine-agnostic source of the fleet-standard agents/skills this
# scaffold now owns (board #911 part 2 — folds plan-executor into the standard
# deploy instead of manual scp). Vendored verbatim from Rick_RnD/fleet/.
_FLEET_DIR = Path(__file__).resolve().parent.parent / "templates" / "fleet"

# name -> (agent .md relative to _FLEET_DIR/agents/, skill dir relative to
# _FLEET_DIR/skills/). Every dept gets these by default, in addition to its
# own enabled_skills. Add new fleet-standard artifacts here.
FLEET_STANDARD_AGENTS = {
    "plan-executor": {
        "agent": "agents/plan-executor.md",
        "skill_dir": "skills/plan-executor",
    },
}

# The four mandated isolated personas (Notion arch — one per layer).
MANDATED_PERSONAS = (
    "data-curator",
    "task-orchestrator",
    "executor",
    "mandate-guardian",
)

# Standard queue + inbox input dirs every dept needs on a fresh clone.
QUEUE_DIRS = ("research", "gates", "management", "improvements")
INBOX_DIRS = ("decisions", "feedback")

DEFAULT_MODEL = "claude-opus-4-8[1m]"
DEFAULT_SUBAGENT_MODEL = "sonnet"


def model_from_dept_yaml(dept_yaml: dict | None) -> str:
    """Resolve the per-dept model pin from a loaded dept.yaml mapping.

    Reads `department.model` (optional, schema-validated string) and falls back
    to DEFAULT_MODEL when absent or empty. This is the single point that turns
    the per-dept `model` field into the value `scaffold_isolation_surface`
    writes into .claude/settings.json — so existing depts that DON'T set the
    field keep the platform Opus pin unchanged, while a dept that pins a
    different model (e.g. `opus[1m]`) is honoured verbatim.

    Fleet model doctrine (2026-06-19, REVISED — supersedes the earlier same-day
    cost-switch): the dept ORCHESTRATOR runs Opus (the strongest model) so the
    long-horizon planning, coordination and judgment that the orchestrator owns
    stays high-quality. Cost is controlled NOT by downgrading the orchestrator
    but by having it DELEGATE bounded execution work to Sonnet SUBAGENTS (see
    `subagent_model` below). This reverses the earlier 2026-06-19 attempt that
    ran cheap Sonnet orchestrators with Opus subagents — {{OPERATOR}} decided the
    opposite is right: smart orchestrator, cheap executors. The per-dept `model`
    field still lets a special dept override the Opus orchestrator pin if needed.
    """
    dept = (dept_yaml or {}).get("department") or {}
    model = dept.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_MODEL


def subagent_model_from_dept_yaml(dept_yaml: dict | None) -> str:
    """Resolve the per-dept SUBAGENT model pin from a loaded dept.yaml mapping.

    Mirrors `model_from_dept_yaml` above but for the bounded-scope subagents
    (executor / task-orchestrator / mandate-guardian / data-curator) rather
    than the dept orchestrator. Reads `department.subagent_model` (optional,
    schema-validated string) and falls back to DEFAULT_SUBAGENT_MODEL
    ("sonnet" — today's behaviour) when absent or empty. This is the single
    point that turns the per-dept `subagent_model` field into the value
    callers pass through to `scaffold_isolation_surface(subagent_model=...)`
    — so existing depts that DON'T set the field keep the current Sonnet
    subagent default unchanged, while a dept that pins its subagents to a
    specific model (e.g. `claude-opus-4-8` to lock a version, or `opus` to
    upgrade subagent quality) is honoured verbatim.

    Doctrine (board #908): a version PIN belongs in the subagent's own
    agent-definition frontmatter (`model:` in subagents/<persona>.md, which
    accepts a full dated model id per Claude Code docs), not in the
    spawn-time `model` param passed to the Agent/Task tool — that param is
    alias-only in this harness. `department.subagent_model` is how a dept
    author sets that pinned value at scaffold time.
    """
    dept = (dept_yaml or {}).get("department") or {}
    subagent_model = dept.get("subagent_model")
    if isinstance(subagent_model, str) and subagent_model.strip():
        return subagent_model.strip()
    return DEFAULT_SUBAGENT_MODEL


def _env() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_ISOLATION_DIR)),
        autoescape=False,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _render(name: str, ctx: dict) -> str:
    return _env().get_template(name).render(**ctx)


def scaffold_gitkeeps(dept_root: Path) -> list[Path]:
    """Create queues/* and inbox/* with a tracked .gitkeep each (CGP CRIT-1:
    a fresh clone must recreate these dirs or the first tick crashes)."""
    dept_root = Path(dept_root)
    written: list[Path] = []
    for d in QUEUE_DIRS:
        gk = dept_root / "queues" / d / ".gitkeep"
        gk.parent.mkdir(parents=True, exist_ok=True)
        gk.write_text("", encoding="utf-8")
        written.append(gk)
    for d in INBOX_DIRS:
        gk = dept_root / "inbox" / d / ".gitkeep"
        gk.parent.mkdir(parents=True, exist_ok=True)
        gk.write_text("", encoding="utf-8")
        written.append(gk)
    return written


def scaffold_fleet_agents(dept_root: Path) -> list[Path]:
    """Copy the fleet-standard agents/skills (FLEET_STANDARD_AGENTS) verbatim
    into `dept_root/.claude/agents/` + `dept_root/.claude/skills/<name>/`.

    Static copy (shutil, not Jinja) — these artifacts are identical fleet-wide
    by design (e.g. plan-executor's version-pinned `model: claude-opus-4-6`
    frontmatter must NOT be templated away). Idempotent: overwrites on rerun.
    """
    dept_root = Path(dept_root)
    written: list[Path] = []

    agents_dir = dept_root / ".claude" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    skills_root = dept_root / ".claude" / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)

    for name, spec in FLEET_STANDARD_AGENTS.items():
        src_agent = _FLEET_DIR / spec["agent"]
        dst_agent = agents_dir / f"{name}.md"
        dst_agent.write_text(src_agent.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(dst_agent)

        src_skill_dir = _FLEET_DIR / spec["skill_dir"]
        dst_skill_dir = skills_root / name
        dst_skill_dir.mkdir(parents=True, exist_ok=True)
        for src_file in src_skill_dir.rglob("*"):
            if src_file.is_dir():
                continue
            rel = src_file.relative_to(src_skill_dir)
            dst_file = dst_skill_dir / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            written.append(dst_file)

    return written


def scaffold_isolation_surface(
    dept_root: Path,
    *,
    slug: str,
    display_name: str,
    level: str,
    enabled_skills: Iterable[str],
    all_dept_slugs: Iterable[str],
    model: str = DEFAULT_MODEL,
    subagent_model: str = DEFAULT_SUBAGENT_MODEL,
) -> list[Path]:
    """Write the full isolation + anti-regression surface into `dept_root`.

    Args:
        slug, display_name, level: dept identity.
        enabled_skills: this dept's owned/reused skill names (-> enabledSkills).
            FLEET_STANDARD_AGENTS (e.g. "plan-executor") are appended
            automatically — every dept gets them enabled by default, no
            per-dept opt-in required.
        all_dept_slugs: every dept slug on the platform; the OTHERS are added to
            the cross-dept deny list (this dept itself is excluded).
        model: model id (defaults to the platform model).

    Returns the list of files written.
    """
    dept_root = Path(dept_root)
    other_dept_slugs = sorted(s for s in all_dept_slugs if s != slug)
    # dict.fromkeys dedupes while preserving order, in case a caller already
    # listed a fleet-standard name explicitly.
    all_enabled_skills = list(dict.fromkeys([*enabled_skills, *FLEET_STANDARD_AGENTS]))
    ctx = {
        "slug": slug,
        "display_name": display_name,
        "level": level,
        "enabled_skills": all_enabled_skills,
        "other_dept_slugs": other_dept_slugs,
        "model": model,
        # The dept ORCHESTRATOR runs `model` (Opus — the strongest model) so the
        # long-horizon planning, coordination and judgment it owns stays sharp.
        # The bounded-scope subagents (executor = mission execution,
        # task-orchestrator = per-task planning, mandate-guardian = guardrail
        # check) run Sonnet (`subagent_model`) for cost: the smart orchestrator
        # delegates the WELL-SCOPED execution work DOWN to cheap workers.
        # {{OPERATOR}} 2026-06-19 (REVISED, supersedes the earlier same-day inversion).
        # opus / opus[1m] / sonnet are entitled; sonnet[1m] is NOT — never pin it.
        "subagent_model": subagent_model,
    }

    written: list[Path] = []
    written += scaffold_gitkeeps(dept_root)

    # .gitignore — keep runtime artifacts/secrets/vault OUT of the ops-repo so a
    # stray non-allow-listed file never 403s the dept's runtime push (the
    # 2026-06-05 ben/maya/tony push-block: a tracked root fund.sqlite + a
    # .claude lock blocked all pushes; git push is all-or-nothing).
    gitignore = dept_root / ".gitignore"
    gitignore.write_text(_render("gitignore.template", ctx), encoding="utf-8")
    written.append(gitignore)

    # .claude/settings.json
    claude = dept_root / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    settings = claude / "settings.json"
    settings.write_text(_render("settings.json.template", ctx), encoding="utf-8")
    written.append(settings)

    # .claude/hooks/session-start.sh (executable)
    hooks = claude / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "session-start.sh"
    hook.write_text(_render("session-start.sh.template", ctx), encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    written.append(hook)

    # subagents/{persona}.md
    sub = dept_root / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    for persona in MANDATED_PERSONAS:
        f = sub / f"{persona}.md"
        f.write_text(_render(f"subagent_{persona}.md.template", ctx), encoding="utf-8")
        written.append(f)

    # .claude/agents/plan-executor.md + .claude/skills/plan-executor/ (board
    # #911 part 2 — fleet-standard agents, static copy, not Jinja-rendered).
    written += scaffold_fleet_agents(dept_root)

    # tests/test_anti_regression_coverage.py (the Part-A triple)
    tests = dept_root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    art = tests / "test_anti_regression_coverage.py"
    art.write_text(
        _render("test_anti_regression_coverage.py.template", ctx), encoding="utf-8"
    )
    written.append(art)

    return written
