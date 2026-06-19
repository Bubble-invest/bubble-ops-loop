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
  tests/test_anti_regression_coverage.py   (the Part-A triple, dept-agnostic)

All per-dept bits (slug, display_name, level, enabled_skills, model, the sibling
dept slugs to deny) are parameterised via the skill's existing Jinja2 renderer
(skill_lib.templates._env / FileSystemLoader). Deterministic: same input -> same
output. Idempotent on dirs (exist_ok); files are overwritten with the rendered
canonical version.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterable

import jinja2

# Reuse the skill's Jinja2 env but point the loader at templates/isolation/.
_ISOLATION_DIR = Path(__file__).resolve().parent.parent / "templates" / "isolation"

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


def model_from_dept_yaml(dept_yaml: dict | None) -> str:
    """Resolve the per-dept model pin from a loaded dept.yaml mapping.

    Reads `department.model` (optional, schema-validated string) and falls back
    to DEFAULT_MODEL when absent or empty. This is the single point that turns
    the per-dept `model` field into the value `scaffold_isolation_surface`
    writes into .claude/settings.json — so existing depts that DON'T set the
    field keep the platform Opus pin unchanged, while a dept that pins
    `sonnet` (cheap orchestrator) or `opus[1m]` is honoured verbatim.

    Fleet cost-optimization (2026-06-19): the model default used to be a single
    global constant here. Making it dept-configurable lets the cheap Sonnet
    orchestrators (Tony / Ben / Maya / Eliot / Accountant / Miranda) coexist
    with the Opus-pinned depts (e.g. Claudette) WITHOUT a global flip that would
    wrongly downgrade everyone.
    """
    dept = (dept_yaml or {}).get("department") or {}
    model = dept.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    return DEFAULT_MODEL


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


def scaffold_isolation_surface(
    dept_root: Path,
    *,
    slug: str,
    display_name: str,
    level: str,
    enabled_skills: Iterable[str],
    all_dept_slugs: Iterable[str],
    model: str = DEFAULT_MODEL,
    subagent_model: str = "opus",
) -> list[Path]:
    """Write the full isolation + anti-regression surface into `dept_root`.

    Args:
        slug, display_name, level: dept identity.
        enabled_skills: this dept's owned/reused skill names (-> enabledSkills).
        all_dept_slugs: every dept slug on the platform; the OTHERS are added to
            the cross-dept deny list (this dept itself is excluded).
        model: model id (defaults to the platform model).

    Returns the list of files written.
    """
    dept_root = Path(dept_root)
    other_dept_slugs = sorted(s for s in all_dept_slugs if s != slug)
    ctx = {
        "slug": slug,
        "display_name": display_name,
        "level": level,
        "enabled_skills": list(enabled_skills),
        "other_dept_slugs": other_dept_slugs,
        "model": model,
        # The dept ORCHESTRATOR runs `model` (Sonnet for cost). The reasoning-heavy
        # subagents (executor = mission execution, task-orchestrator = planning,
        # mandate-guardian = judgment) run the BEST model so the cheap orchestrator
        # delegates the hard thinking up, not down. Joris 2026-06-19. opus is
        # entitled (opus[1m] too); sonnet[1m] is NOT — never pin sonnet[1m].
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

    # tests/test_anti_regression_coverage.py (the Part-A triple)
    tests = dept_root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    art = tests / "test_anti_regression_coverage.py"
    art.write_text(
        _render("test_anti_regression_coverage.py.template", ctx), encoding="utf-8"
    )
    written.append(art)

    return written
