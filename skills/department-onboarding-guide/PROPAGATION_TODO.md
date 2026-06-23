# PROPAGATION_TODO — department-onboarding-guide (LOCAL-ONLY changes)

**Why this file exists:** `/home/claude/bubble-ops-loop/skills/department-onboarding-guide/`
on the box is **NOT a git repo**. The changes below were made LOCAL-ONLY by the
final systemic anti-regression pass. They will NOT persist across a redeploy and
are NOT yet in the source of truth. The VPS lead must propagate them via
pyinfra into `bubble-vps-data` (the canonical copy of `bubble-ops-loop`), then
re-deploy.

Status of the box-local change: **applied + template pytest green (see bottom).**
Status of propagation to `bubble-vps-data`: **TODO (this file).**

---

## Files ADDED (copy verbatim into the canonical skill dir)

Canonical source = the box-local files just written, under
`/home/claude/bubble-ops-loop/skills/department-onboarding-guide/`:

```
skill_lib/isolation_scaffold.py
templates/isolation/settings.json.template
templates/isolation/session-start.sh.template
templates/isolation/subagent_data-curator.md.template
templates/isolation/subagent_task-orchestrator.md.template
templates/isolation/subagent_executor.md.template
templates/isolation/subagent_mandate-guardian.md.template
templates/isolation/test_anti_regression_coverage.py.template
tests/test_isolation_scaffold.py
```

## Files MODIFIED (re-apply the edits in the canonical copy)

```
SKILL.md
  - Step 4 table: added the isolation_scaffold helper row + commit.
  - NEW "Step 4b — Isolation + anti-regression surface (MANDATORY, generated)"
    subsection documenting scaffold_isolation_surface() and the Part-A triple.
  - Step 7b: NEW "Check D — Tools are argparse-runnable + skill modules
    importable (EXECUTE, don't grep)".
  - "Files in this skill" tree: added templates/isolation/*, isolation_scaffold.py,
    test_isolation_scaffold.py.
```

---

## What the new scaffolding produces (root-cause fixes propagated UP)

`skill_lib.isolation_scaffold.scaffold_isolation_surface(dept_root, slug=...,
display_name=..., level=..., enabled_skills=[...], all_dept_slugs=[...],
model=...)` writes, for every NEW dept:

1. `queues/{research,gates,management,improvements}/.gitkeep`
   + `inbox/{decisions,feedback}/.gitkeep` — CGP CRIT-1 (fresh-clone crash).
2. `.claude/settings.json` — dept-scoped perms (deny every sibling dept + SOPS
   sources + `git push`), enabledSkills/Plugins, model, env, SessionStart hook.
3. `.claude/hooks/session-start.sh` — executable SessionStart hook.
4. `subagents/{data-curator,task-orchestrator,executor,mandate-guardian}.md` —
   the 4 mandated isolated personas (the isolation gap; Maya herself lacked it).
5. `tests/test_anti_regression_coverage.py` — the Part-A test triple
   (import modules / exec prompt python / argparse tools + DRY_RUN guard).

## Reference implementations the templates are derived from

The templates are parameterised generalisations of the just-created live versions.
When propagating, treat these as the canonical "golden" copies to diff against:

```
/home/claude/agents/bubble-ops-cgp/.claude/settings.json
/home/claude/agents/bubble-ops-cgp/.claude/hooks/session-start.sh
/home/claude/agents/bubble-ops-cgp/subagents/{data-curator,task-orchestrator,executor,mandate-guardian}.md
/home/claude/agents/bubble-ops-cgp/tests/test_anti_regression_coverage.py
/home/claude/agents/bubble-ops-tony/tests/test_anti_regression_coverage.py
```

---

## Remaining / NOT done in this pass (flag for follow-up)

- **Wire scaffold_isolation_surface() into the Step-4 step runner.** Today it is
  a standalone helper the eclosing agent must call (documented in SKILL.md Step
  4b). A tighter integration would call it automatically from
  `skill_lib/step_runners/skills_tools.py` so the surface is emitted without the
  agent remembering to. Left out to keep this pass small + the step-runner
  contract stable; do it when the step-runner is next revised.
- **`enabled_skills` / `all_dept_slugs` sourcing.** The scaffolder takes these as
  args. The step runner (when wired) should derive `enabled_skills` from the
  Step-4 manifest and `all_dept_slugs` from the live dept registry, rather than
  the agent passing them by hand.
- **CGP-strict deny extras.** The generic `settings.json.template` does NOT
  include CGP's PII-exfiltration deny verbs (scp/rsync/curl/wget/tar of the
  vault) or the `CGP_VAULT_ROOT`/`CGP_PII_BOUNDARY` env — those are
  client-product-specific. If a future dept is a Mac-deployed PII product,
  parameterise a `pii_strict=True` branch in the template (the CGP live
  settings.json is the reference).

---

## Verification (box-local, before propagation)

```
cd /home/claude/bubble-ops-loop/skills/department-onboarding-guide
python3 -m pytest -q
# => 269 passed (was 259; +10 from tests/test_isolation_scaffold.py)
```
