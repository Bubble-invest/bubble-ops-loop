---
name: department-onboarding-guide
description: Conversational onboarding skill for new bubble-ops-loop departments. Guides operator through the 7 eclosure steps (Mandate -> Missions -> Layers -> Skills/tools -> Gates/KPIs -> Dry-run -> Activation), produces schema-validated artifacts at each step, manages the onboarding branch + activation PR. Use when bringing up a new dept (Maya, Ben, Tony, Miranda, Eliot, etc.). The skill ships templates + state machine + dry-run simulator; the agent (Claude Code session) consumes it to drive the conversation.
---

# department-onboarding-guide

Pure-Python helpers + Jinja2 templates + state machine + deterministic
dry-run simulator. The agent uses the skill to drive an operator through
the 7-step eclosure of a new bubble-ops-loop department.

## How to use this skill if you ARE the eclosing agent (Phase G)

If you are the freshly spawned Claude Code session for a new
`bubble-ops-<slug>` repo, this skill is YOUR self-driving playbook:

0. **Read the shared wiki** before doing anything else:
   ```bash
   cat ~/.claude/agent-memory/shared-wiki/rnd/hot.md 2>/dev/null
   cat ~/.claude/agent-memory/shared-wiki/index.md 2>/dev/null
   ```
   The wiki is git-synced every 30 min and contains cross-team doctrine,
   architecture decisions, and changes that affect all agents.
1. **You drive the 7 steps yourself.** You do NOT wait for {{OPERATOR}} to
   tell you to start — you bootstrap immediately on first boot.
2. **At each step**, you (the eclosing agent) MUST:
   - call `skill_lib.auto_drive.get_current_step(state_path)` to know
     which step you're on;
   - call `skill_lib.auto_drive.get_step_prompt(step)` to get the
     French Bureau-de-Cadre prompt to send on Telegram (3 concrete
     options + a direct question);
   - send that prompt to {{OPERATOR}} on your dedicated Telegram bot
     (`@bubbleops<slug>_bot`);
   - wait for his answer;
   - write the artifact for the step (using the templates in
     `templates/`);
   - run `git add . && git commit -m "<step>: <summary>" && git push`;
   - call `skill_lib.auto_drive.record_step_completion(state_path,
     step, artifact_paths=[...])` to update `STATE.yaml`;
   - send a 1-line confirmation message to {{OPERATOR}} on Telegram
     (« ✓ Étape N validée — <name> »);
   - move to step N+1 immediately.
3. **You never expose technical slugs** (no `dept.yaml::field` paths,
   no schema enum strings) in messages to {{OPERATOR}}.
4. **You always tutoie {{OPERATOR}} and refer to yourself in 1st person.**
5. **If you are blocked > 2h** waiting for {{OPERATOR}}, send a polite
   reminder. If > 6h, pause and write a status note to
   `MORNING_BRIEF.md`.

The auto-driving entry point is `skill_lib.auto_drive`. The 7 step
prompts are in `_STEP_PROMPTS` there.

### Per-step operator triplet (Notion v5 lines 826-828)

Every step MUST expose the same 3 operator actions:

  **[Approve]**  **[Edit]**  **[Ask agent to refine]**

This is the audit-trail spine — without it, every dept invents its own
protocol and the audit log becomes inconsistent.

After turn 1 (the agent's 3-option prompt) and once {{OPERATOR}} has picked an
option, the agent calls:

  - `skill_lib.auto_drive.get_followup_prompt(step, operator_choice)`
    to get the 2nd-turn FR Bureau-de-Cadre prompt that surfaces the
    triplet to the operator.

When {{OPERATOR}} replies, the agent classifies his answer into one of the 3
actions and calls the matching recorder:

  - `record_approval(state_path, step)` — {{OPERATOR}} said "ok / approuve /
    go".
  - `record_edit_request(state_path, step, operator_text)` — {{OPERATOR}}
    pasted his own rewritten version. Persist `operator_text` verbatim.
  - `record_refine_request(state_path, step, reason)` — {{OPERATOR}} asked
    for another iteration. Persist the short `reason` ("trop long",
    "manque les forbidden", etc.).

All 3 helpers append to `STATE.yaml::step_interactions[]` (audit
trail, distinct from `commits[]` which records validation milestones).

Reference: Notion v5 §"UX — Agent Nursery / Department Onboarding"
(`/tmp/notion_final.txt` lines 734-1004). Verbatim cite:
> "L'intelligence d'onboarding vit dans l'agent et dans la skill
> `department-onboarding-guide`." (line 1004)
> "[Approve mandate] [Edit] [Ask agent to refine]" (line 828)

## When to use

Trigger this skill whenever:
- the operator says "I want to bring up a new dept" or names an "Agent a eclore"
- a `bubble-ops-<slug>` repo exists with `dept.yaml.draft` but `department.status` is `onboarding`
- the front-end Agent Nursery surfaces a card and the operator clicks "Continue onboarding"

DO NOT use this skill for:
- editing a `live` dept's `dept.yaml` (use a normal PR review flow)
- bumping autonomy modes on a gate policy (use the autonomy-raise flow)
- pausing or retiring a dept (separate flow; `status: paused | retired`)

## Operating principle

The skill DOES NOT itself converse with the operator. It exposes:

1. **Templates** under `templates/` that the agent renders at each step.
2. **State machine** at `skill_lib/state_machine.py` enforcing the 7 statuses.
3. **Dry-run simulator** at `skill_lib/dry_run.py` for Step 6.
4. **Helpers** for steps 3 (layers), 4 (skills/tools), 5 (gates), 7 (activation).

The agent (Claude Code session) handles the conversation, asks the operator
the questions, fills the template context dicts, calls the helpers, and
commits. This separation keeps the skill testable in pure Python without
needing an LLM.

## The 7 steps

For each step the section below states:
- the state covered (status transition)
- what the agent asks the operator
- what artifact is produced (with template reference)
- what validation runs
- how to commit (branch `onboarding/<slug>`, message conventions)

### Step 1 — Mandate

| Aspect | Value |
|---|---|
| Status before | `Idea` |
| Status after | `Configuring` |
| Template | `templates/dept.yaml.template` |
| Helper | `skill_lib.templates.render_template("dept.yaml", ctx)` |
| Validates against | `schemas-draft/dept.schema.yaml::department` (sub-block) |
| Commit | `onboarding: validate mandate` |

Agent asks operator:
- role of the dept (one sentence)
- owner (default: joris)
- forbidden list (3-5 hard "must never" items)
- expected outputs
- success criteria

Context dict shape (Step 1):
```python
{
  "slug": "miranda",
  "display_name": "Miranda",
  "level": "ops",   # ops | management | principal
  "mandate_text": "Produire, planifier et auditer du contenu social pour Bubble.",
  "owner": "joris",
  "forbidden": ["publier informations confidentielles", "donner conseil financier"],
}
```

### Step 1b — Working memory + mission-file lock (MANDATORY, generated)

| Aspect | Value |
|---|---|
| Template | `templates/WORKING_MEMORY.md.template` |
| Helper | `skill_lib.templates.render_template("WORKING_MEMORY.md", {"display_name": ...})` |
| Commit | `onboarding: working memory + mission-file lock` |

Every dept gets a writable **`WORKING_MEMORY.md`** at its repo root. This is the
ONE place the agent records **transient, time-bound topics** (e.g. "watch the
SpaceX IPO for the coming weeks") instead of editing its mission files. Render
the template into `WORKING_MEMORY.md` and commit it.

This pairs with the **mission-file lock** (governance fix 2026-06-01): the dept's
mission-definition files are STRUCTURAL and the agent **cannot push them** — the
box-side git credential helper mints a read-only token whenever a push touches
any structural path, so the only way to change a mission is a PR {{OPERATOR}}/{{OPERATOR_2}}
merges. Structural paths (see `token-broker/src/policy.py::STRUCTURAL_PATH_GLOBS`):
`CLAUDE.md, MANDATE.md, dept.yaml, skills_manifest.yaml, config.yaml,
gate_policy.yaml, layers/**, missions/**, skills/**, tools/**, subagents/**,
policies/**, templates/**, .claude/**`. `WORKING_MEMORY.md` and `whiteboard.yaml`
are deliberately NOT structural (writable runtime/working state).

The dept's `CLAUDE.md` MUST reference `WORKING_MEMORY.md` so the agent knows
where to write transient topics and that it cannot touch its own mission. Use
this standard block (translate to the dept's working language):

```markdown
## Mémoire de travail vs mission (NE JAMAIS confondre)

Ma **mission est fixe** : `CLAUDE.md`, `MANDATE.md`, `dept.yaml`, `layers/**`,
`missions/**`, `skills/**`, ... Je **ne peux pas** les modifier — seul {{OPERATOR}} ou
{{OPERATOR_2}} le peut, via une pull request qu'ils valident. Si on me demande d'ajouter
un sujet temporaire (ex. « surveille l'IPO SpaceX ces prochaines semaines »),
je l'écris dans **`WORKING_MEMORY.md`** (mon seul espace inscriptible pour les
sujets transitoires), JAMAIS dans un fichier de mission. Mes prompts de layer
lisent `WORKING_MEMORY.md` au début de chaque run et intègrent ses sujets actifs.
Si un sujet devient durable au point de mériter d'entrer dans ma mission, je le
signale à {{OPERATOR}}/{{OPERATOR_2}} pour qu'ILS le promeuvent — je ne touche jamais la mission
moi-même.
```

The layer prompts (`layers/*/PROMPT.md`) should read `WORKING_MEMORY.md` at the
start of each run and fold its active items into that run's work — that is how a
temporary instruction takes effect without drifting into the permanent spec.

### Step 2 — Recurring missions

| Aspect | Value |
|---|---|
| Status before | `Configuring` |
| Status after | `Drafting` |
| Template | `templates/mission.yaml.template` (one per mission) |
| Helper | `skill_lib.templates.render_template("mission.yaml", {"mission": m})` |
| Validates against | `schemas-draft/recurring-mission.schema.yaml` |
| Commit | `onboarding: add recurring missions` |

Agent asks operator: what must the dept watch / produce regularly?
Supports all 6 cadence shapes: daily, weekly, hourly, every_Nh, every_Nm, cron:...

### Step 3 — Layer mapping

| Aspect | Value |
|---|---|
| Status before | `Drafting` |
| Status after | `Drafting` (mid-step; doesn't move SM) |
| Helper | `skill_lib.layers.map_layers(ctx)` + `generate_layer_prompt_stub(layer, desc)` |
| Validates | each description >= 1 sentence; all 4 layers covered |
| Commit | `onboarding: map 4 layers` |

Agent asks: "What do you do at each of the 4 OODA layers?"
Produces 4 `PROMPT.md` stubs ready to drop into `layers/<N>/`.

### Step 4 — Skills & tools

| Aspect | Value |
|---|---|
| Status before | `Drafting` |
| Status after | `Needs validation` |
| Helper | `skill_lib.skills_tools.build_manifest(ctx)` + `validate_card(slug, card)` |
| Helper | `skill_lib.isolation_scaffold.scaffold_isolation_surface(...)` (isolation + anti-regression surface) |
| Validates | per-layer skill lists + flat tools list; cards have purpose/inputs/outputs/status |
| Commit | `onboarding: declare skills and tools` + `onboarding: scaffold isolation + anti-regression surface` |

#### Step 4b — Isolation + anti-regression surface (MANDATORY, generated)

Every dept MUST ship the per-dept isolation surface the architecture mandates
(notion_architecture.md ~12, ~30, ~551-570) AND the anti-regression test triple.
The systemic audit's root cause was that the template never generated these, so
each dept retrofitted them by hand (Maya herself lacked the isolation surface).
This is now scaffolded in one call:

```python
from skill_lib.isolation_scaffold import scaffold_isolation_surface
scaffold_isolation_surface(
    dept_root,
    slug="<slug>", display_name="<DisplayName>", level="<level>",
    enabled_skills=[...],          # this dept's owned/reused skills
    all_dept_slugs=[...],          # every platform dept; the OTHERS go in deny
    model="claude-opus-4-8[1m]",
)
```

It writes (parameterised per-dept via Jinja2):

- `queues/{research,gates,management,improvements}/.gitkeep` + `inbox/{decisions,feedback}/.gitkeep`
  — so a fresh `git clone` recreates these dirs and the first `/loop` tick does
  not crash with `FileNotFoundError` (this was CGP's CRIT-1).
- `.gitignore` — keeps runtime artifacts (root `*.sqlite`, `.claude/*.lock`),
  secrets (`*.sops.env`, `/run/`), and the split-out `vault/` OUT of the ops-repo.
  A dept's runtime push allow-list is `outputs/** queues/** inbox/**
  WORKING_MEMORY.md`; `git push` is all-or-nothing, so ANY stray non-allow-listed
  file 403s the whole push. (This was the 2026-06-05 ben/maya/tony push-block: a
  tracked root `fund.sqlite` + a `.claude` lock blocked all pushes for ~2 days.)
- `.claude/settings.json` — dept-scoped `permissions` (allow own tree, **deny
  every sibling dept** + SOPS sources + `git push`), `enabledSkills`,
  `enabledPlugins`, `model`, `env` (BUBBLE_DEPT*), and the SessionStart `hooks`
  wiring. Valid JSON; deny wins even under `--dangerously-skip-permissions`.
- `.claude/hooks/session-start.sh` — the SessionStart hook (executable; emits the
  dept's wake-context JSON).
- `subagents/{data-curator,task-orchestrator,executor,mandate-guardian}.md` —
  the four mandated isolated personas (one per OODA layer), each with `tools:`,
  `permission-mode:` and a `Forbidden` scope section.
- `tests/test_anti_regression_coverage.py` — the **Part-A test triple** (below),
  so every new dept is BORN with execute-the-code coverage, not just
  file-existence checks.

The reference implementations live in the live depts
(`/home/claude/agents/bubble-ops-cgp/.claude/settings.json`, its hook, its
`subagents/*.md`, and `tests/test_anti_regression_coverage.py`) — the templates
under `templates/isolation/` are derived from them.

#### Anti-regression test triple (the root-cause fix, generated into every dept)

Green tests used to hide real defects because no suite imported the skill
modules, exec'd the Python embedded in layer prompts, or ran tools through
argparse. The generated `tests/test_anti_regression_coverage.py` closes that gap:

1. **Import every shipped module + run its primary function** (catches doc-only
   skills: a SKILL.md that documents `from skills.X import Y` with no module).
   Tune `SKILL_MODULES` at the top of the generated file for module-shipping
   depts (Tony-style); leave `{}` for SKILL.md-only depts (CGP-style) — the
   vendored `dispatch_helpers` is still imported + exercised.
2. **`compile()` every fenced ```python block in every `layers/*/PROMPT.md`** and
   **`exec()`** the side-effect-safe ones (catches runtime `TypeError`s like the
   `write_last_run(Path(...))` 1-arg bug). Fragment blocks are skipped but every
   skip is **logged** (no silent skips).
3. **Run every `tools/*.py` through argparse** (`--help` exits 0 + prints usage)
   and assert **no tool advertised `active` returns a `noop_shim`** hollow
   success (catches shim tools shipped active-but-hollow).

Plus a DRY_RUN-footgun guard: `force_commit_and_push` under `DRY_RUN=1` must be
side-effect-free (HEAD + index + working tree untouched).

### Step 5 — Gates, autonomy bands, KPI guardrails

| Aspect | Value |
|---|---|
| Status before | `Needs validation` |
| Status after | `Dry run` |
| Template | `templates/gate_policy.yaml.template` |
| Helper | `skill_lib.gates.build_authorization_band(...)` |
| Validates against | `schemas-draft/dept.schema.yaml::gate_policies` |
| Commit | `onboarding: add gates and kpis` |

5 autonomy modes (from `skill_lib.gates.ALL_AUTONOMY_MODES`):
1. `manual_required` (v1 default)
2. `manual_unless_policy_passed`
3. `auto_if_policy_passed`
4. `auto_with_veto_window`
5. `disabled`

KPI guardrails are per-dept-specific (free-form keys).

### Step 6 — Tests / dry-run

| Aspect | Value |
|---|---|
| Status before | `Dry run` |
| Status after | `Ready to activate` (if all green or operator accepts warnings) |
| Template | `templates/test_fixture.template.yaml` |
| Helper | `skill_lib.dry_run.run_dry_run(...)` |
| Commit | `onboarding: add dry-run fixtures` |

Contract for `run_dry_run`:

Input: `dept_root` (Path), `fake_queue_item` (dict), `layer_checks` (dict),
`operator_accepts_warnings` (bool).

Output: `{outputs, checks, overall_status, can_advance_to_ready, dept_root}`
where `overall_status in {PASSED, WARNING, FAILED}`. Activation is blocked
unless `can_advance_to_ready` is True.

### Step 7 — Activation

| Aspect | Value |
|---|---|
| Status before | `Ready to activate` |
| Status after | `Live` |
| Helper | `skill_lib.activation.flip_status_to_live(dept_yaml_path)` |
| Helper | `skill_lib.activation.build_activation_pr_body(...)` |
| Branch | `onboarding/<slug>` (Notion v5 line 964) |
| PR title | `Activate <DisplayName> department` (Notion v5 line 975) |

Effect:
- `dept.yaml::department.status` flips `onboarding` -> `live`
- Front-end card moves: `Agents a eclore` -> `Live departments`
- Session becomes `ops-loop-<slug>`

### Step 7b — Runtime smoke test (MANDATORY before declaring Live)

**This step is not optional.** The test suite asserts artifact shape — not runtime
behaviour. A dept can be 100% green tests yet fail on the first `/loop` tick.
Run all three checks before flipping STATE to `Live`:

#### Check A — Import smoke
```bash
cd /home/claude/agents/bubble-ops-<slug>
python3 -c "from scripts.lib.dispatch_helpers import write_last_run, validate_layer_output, increment_round_counter, force_commit_and_push, decide_dispatch, build_dispatch_ctx, write_l1_baseline, read_l1_baseline; print('OK')"
```
If this raises `ModuleNotFoundError`: vendor `dispatch_helpers.py` from the loop:
```bash
mkdir -p scripts/lib
touch scripts/__init__.py scripts/lib/__init__.py
cp /home/claude/bubble-ops-loop/scripts/lib/dispatch_helpers.py scripts/lib/dispatch_helpers.py
```
For Mac-deployed products (CGP-type): copy, not symlink — the Mac won't have bubble-ops-loop checked out.

#### Check B — Cross-dept read paths (management depts only)
For each child dept declared in `dept.yaml::hierarchy.children`:
```bash
# Maya's canonical L4 write path is outputs/<date>/4/management-export.yaml
# Verify your layer prompts read the same path (with /4/ segment):
grep -n "management-export.yaml" layers/*/PROMPT.md dept.yaml
```
**Common mistake**: reading `outputs/<date>/management-export.yaml` (missing `/4/`). The `/4/` segment is where the child's L4 layer writes.

#### Check C — Mac vault paths (Mac-deployed products only)
For products using Obsidian vault (`optional_domain_ledger: null` with `~/cgp-vault/`):
```bash
# L3 PROMPT must never use bare vault/ (relative to cwd) — must be ~/cgp-vault/
grep -n "vault/" layers/3/PROMPT.md | grep -v "cgp-vault\|#"
```
Zero results expected. Any match means L3 outputs will land in the repo's empty `vault/` skeleton, not the client's actual Obsidian vault.

#### Check D — Tools are argparse-runnable + skill modules importable (EXECUTE, don't grep)

The whole-audit root cause was that onboarding QA only checked file existence, so
wrong-path tools, hollow shims and import failures passed green. QA must now
EXECUTE the code:

```bash
cd /home/claude/agents/bubble-ops-<slug>
# Every tool must run through argparse (exit 0 + usage), no broken import/wiring:
for t in tools/*.py; do python3 "$t" --help >/dev/null || echo "FAIL --help: $t"; done
# The anti-regression triple (import modules + exec prompt Python + argparse tools)
# must be green — this is the suite generated in Step 4b:
python3 -m pytest tests/test_anti_regression_coverage.py -q
```
Any `--help` non-zero, or any red in `test_anti_regression_coverage.py`, BLOCKS
activation. A tool advertised `active` that still emits `noop_shim`, a layer
PROMPT.md python block that `TypeError`s on exec, or a SKILL.md that documents a
non-importable module are all caught here — not in production.


#### Check E — OS sandbox engaged (Layer B — auto-inherited, but VERIFY)

New depts inherit the OS sandbox **automatically** from box-wide
`/etc/claude-code/managed-settings.json` — there is nothing to install per-dept,
and the dept cannot disable it (managed `enabled`/`failIfUnavailable` are
un-overridable). But verify it actually engaged after first start, because
behavioural write/curl probes LIE (the escape-hatch + unlocked domains cause
false negatives). The reliable signal is the **user-namespace comparison**:

```bash
# host userns:
readlink /proc/self/ns/user                       # e.g. user:[4026531837]
# what the dept's SANDBOXED bash sees (run as the claude user, in the dept cwd):
cd /home/claude/agents/bubble-ops-<slug>
claude --model "opus[1m]" --dangerously-skip-permissions --print \
  "Use the Bash tool to run exactly: readlink /proc/self/ns/user"
```
If the dept reports a **different** `user:[...]` than the host → ENGAGED (bwrap
jail active). Same value as host → NOT engaged: STOP, do not declare Live, check
that `bwrap`/`socat`/`@anthropic-ai/sandbox-runtime` + the AppArmor `bwrap` profile
are present (see `shared/systems/vps-agent-sandbox` in the wiki).

> **Do NOT widen the sandbox in the dept's project `.claude/settings.json`.** The
> sandbox arrays — `allowWrite`, `allowedDomains`, `excludedCommands` — MERGE
> across managed + project (depts can add, not remove). A broad `allowWrite` or an
> `excludedCommands` entry in a dept's project settings punches a hole in that
> dept's OWN jail. Keep dept sandbox arrays minimal and justified; the booleans
> (`enabled`, `failIfUnavailable`) are locked by managed and need no dept action.


#### Check F — Dispatch canonical + per-layer notify wiring (so the L1 fix + notifications stay default)

The dept's `scripts/lib/dispatch_helpers.py` MUST be byte-identical to the framework
canonical (bootstrap-dept.sh vendors it, but verify — a stale copy reintroduces the
L1-floods-every-tick bug Tony hit live, session ebb03972 2026-06-02):
```bash
cd /home/claude/agents/bubble-ops-<slug>
# 1. drift check against canonical (exit non-zero = drifted → re-sync):
bash /home/claude/bubble-ops-loop/scripts/sync-dispatch-lib.sh --check --depts="<slug>"
# 2. regression guard — build_dispatch_ctx MUST emit the two L1 keys, else L1
#    re-dispatches on every quiet tick (the ebb03972 bug):
python3 -c "from scripts.lib.dispatch_helpers import build_dispatch_ctx; c=build_dispatch_ctx('.'); assert 'layer_1_last_run_today' in c and 'layer_1_baseline_counter' in c, 'STALE dispatch_helpers — re-sync from canonical'; print('dispatch OK')"
```
If drifted: `bash /home/claude/bubble-ops-loop/scripts/sync-dispatch-lib.sh --depts="<slug>"`.

**L1 PROMPT must call `write_l1_baseline`** (after `increment_round_counter(..., layer=1)`)
so C.0's cycle-gate re-fires L1 only after a fresh L2/L3/L4 cycle:
```bash
grep -n "write_l1_baseline" layers/1/PROMPT.md   # expect ≥1 match
```

**Per-layer-fire notifications (optional but recommended — framework provides them):**
the framework ships `scripts/lib/notify.py` (email + Telegram) + `loop_notify.py`. To get
a Telegram ping each time a layer fires, the dept `/loop` protocol (CLAUDE.md) should:
- at STEP 3c (after `validate_layer_output` ok) call `notify_layer_fired(dept, N, summary_path, config=...)`
  for N∈{1,4} (immediate), and accumulate `layer_fires={"2":n,"3":n}` for L2/L3;
- at STEP 6 call `notify_layers_batched(dept, layer_fires, config=...)` once (batched).
Recipients come from the dept `config.yaml` (`accounts`/`notifications`); if the dept has no
`config.yaml` yet, the helpers default to {{OPERATOR}}'s chat_id. (tony/cgp still need a config.yaml.)
#### Check G — Layer floor coverage (4-cron floor — auto-inherited, but VERIFY)

New depts inherit the **4-cron layer floor automatically** — there is NOTHING to
install or configure per-dept. The floor is exactly four box-wide cron units
(`loop-layer1..4.timer`, one per OODA layer, L1 07:00 / L2 12:00 / L3 16:00 /
L4 19:00 Europe/Paris). Each fires its layer for EVERY eligible dept, discovered
at runtime by globbing `/home/claude/agents/bubble-ops-*`. A new dept is picked
up the moment its `ops-loop-<slug>.service` is **enabled** and it has
`layers/N/PROMPT.md` for the layer in question. No new unit, no `BUBBLE_BACKUP_DEPTS`
edit. (See `scripts/install-loop-backup.sh` + INSTALL.md step 4.)

This is the daily FLOOR: it guarantees each layer fires ≥1×/day for the dept even
if its live `/loop` dies (auth lapse, crash, parked). A heartbeat freshness gate
skips the dept while its live loop is healthy (no double-tick); a `flock` mutex
prevents a floor tick from overlapping a live tick.

Verify the new dept is in scope (read-only, spends no tick):

```bash
cd /home/claude/bubble-ops-loop
BUBBLE_BACKUP_DRY_RUN=1 BUBBLE_BACKUP_LOG=/tmp/floor-check.jsonl \
  scripts/loop-backup.sh --layer 1 2>&1 | grep -E "auto-discovered|<slug>"
rm -f /tmp/floor-check.jsonl
```
The `auto-discovered` line must list the new `<slug>`. If it shows
`SKIP — service ops-loop-<slug>.service not enabled` the dept's loop service
isn't enabled yet (expected until activation); if it's enabled but the layer is
missing you'll see `no layers/N/PROMPT.md` — that's correct for a dept that
genuinely doesn't run that layer (e.g. a concierge), wrong for a full OODA dept.

> The floor is "1 unit iterates N depts" by design — do **not** add a per-dept
> backup/floor unit. Inheritance is the whole point.

#### Check F — Service-start prerequisites (MANDATORY for a MANUAL deploy)

The console éclosure flow sets these up automatically. If you deploy a dept BY HAND
(clone → systemd unit → start, e.g. Ben 2026-06-03), three prerequisites are easy to
miss — each leaves the service `active` but the **Telegram poller never starts** (no
`bun server.ts` child, no `bot.pid`), so the agent can't receive DMs and sits idle.
Symptom: you message the bot and get no reply; `pstree -p <MainPID> | grep bun` is empty.

1. **Folder trust** — Claude Code shows *"Do you trust the files in this folder?"* on
   first run in a new dir. The headless `claude` **hangs at this prompt forever** (even
   with `--dangerously-skip-permissions`; pty decode shows repeated `trust?`), so it
   never reaches poller start. FIX (as the agent OS-user):
   ```bash
   python3 - <<'PY'
   import json; f="/home/<user>/.claude.json"; d=json.load(open(f))
   p="/home/<user>/agents/bubble-ops-<slug>"
   d["projects"].setdefault(p,{})["hasTrustDialogAccepted"]=True
   json.dump(d, open(f,"w"), indent=2)
   PY
   ```
   Every existing dept has `hasTrustDialogAccepted: True`; a fresh manual clone does not.

2. **Per-channel `.env`** — the telegram plugin (`server.ts`) reads its token from
   `$TELEGRAM_STATE_DIR/.env` (= `~/.claude/channels/telegram-<slug>/.env`), **NOT** from
   the systemd EnvironmentFile. The unit's `TELEGRAM_BOT_TOKEN` feeds the *agent*; the
   *poller* needs the channel `.env`. Create it (mode 600, agent-owned) with
   `TELEGRAM_BOT_TOKEN=<token>`, alongside `access.json` (allowlist) + an `approved/` dir.

3. **Dept `.venv`** — if the dept ships pandas/numpy/etc tools (e.g. Ben — unlike the
   stdlib-only depts), provision a `.venv` from `requirements.txt` at the workdir and
   wire the tool invocations at it (`FUND_PYTHON`/venv python). The VPS system python has
   no scientific stack.

4. **Telegram liveness watchdog** — a manual deploy installs `ops-loop-<slug>.service`
   but **NOT** the per-dept watchdog (`deploy-to-morty.sh` does not render it; the watchdog
   stack is hand-cloned per dept). Without it, a wedged loop goes silent with no alert —
   exactly the gap that left Ben unmonitored after 2026-06-03. Clone an existing dept's
   stack (maya/tony are byte-identical modulo slug) — FOUR artifacts:
   ```bash
   # As root on the box. Source dept = maya; target = <slug>.
   sed 's/maya/<slug>/g' /home/claude/scripts/telegram-watchdog-maya.sh \
     | install -o claude -g claude -m 0755 /dev/stdin /home/claude/scripts/telegram-watchdog-<slug>.sh
   sed 's/maya/<slug>/g' /etc/systemd/system/telegram-watchdog-maya.service \
     | install -o root -g root -m 0644 /dev/stdin /etc/systemd/system/telegram-watchdog-<slug>.service
   sed 's/maya/<slug>/g' /etc/systemd/system/telegram-watchdog-maya.timer \
     | install -o root -g root -m 0644 /dev/stdin /etc/systemd/system/telegram-watchdog-<slug>.timer
   # sudoers: VALIDATE with visudo BEFORE install (a bad drop-in locks sudo)
   sed 's/maya/<slug>/g' /etc/sudoers.d/claude-telegram-watchdog-maya > /tmp/wd-sudoers
   visudo -cf /tmp/wd-sudoers && install -o root -g root -m 0440 /tmp/wd-sudoers /etc/sudoers.d/claude-telegram-watchdog-<slug>; rm -f /tmp/wd-sudoers
   systemctl daemon-reload && systemctl enable --now telegram-watchdog-<slug>.timer
   ```
   The `.sh` MUST be mode `0755` — systemd fails `203/EXEC` on a non-executable ExecStart
   (a deploy that writes the script without `+x` silences the watchdog; see
   [[shared/systems/vps-telegram-watchdog]]). The pyinfra renderer self-heals mode, but a
   hand-clone does not — so always `test -x` after.
   NB: a watchdog is only for depts that **have a loop**. Concierges with no loop (morty)
   and intentionally-disabled depts (cgp) get NO watchdog — installing one produces 5-min
   false-alarm ticks.

Verify after start: `cat ~/.claude/channels/telegram-<slug>/bot.pid` (present = poller up);
`curl .../getWebhookInfo` shows `pending_update_count` + `last_error` WITHOUT consuming
updates (never `getUpdates` — it steals the agent's pending messages). `NRestarts=0` = clean.
Watchdog: `systemctl is-active telegram-watchdog-<slug>.timer` = `active`, `is-enabled` =
`enabled`, and `test -x /home/claude/scripts/telegram-watchdog-<slug>.sh`.

## Status state machine

7 statuses, strict linear progression (Notion v5 lines 794-801):

```
Idea -> Configuring -> Drafting -> Needs validation
     -> Dry run -> Ready to activate -> Live
```

Skipping states raises `InvalidTransition`. Explicit revert via `reset_to`
allowed when the operator invalidates a step (used by Step 5 -> Step 2 if
the mission list changes mid-flow).

## Files in this skill

```
SKILL.md                                # this file
README.md                               # end-to-end Miranda walkthrough
templates/
  dept.yaml.template                    # Step 1 artifact
  mission.yaml.template                 # Step 2 artifact (one per mission)
  gate_policy.yaml.template             # Step 5 artifact (one per policy)
  test_fixture.template.yaml            # Step 6 artifact
  isolation/                            # Step 4b — isolation + anti-regression surface
    settings.json.template              #   .claude/settings.json (dept-scoped perms)
    session-start.sh.template           #   .claude/hooks/session-start.sh
    subagent_data-curator.md.template       # the 4 mandated personas (one per layer)
    subagent_task-orchestrator.md.template
    subagent_executor.md.template
    subagent_mandate-guardian.md.template
    test_anti_regression_coverage.py.template  # the Part-A test triple (generated)
examples/
  maya/dept.yaml                        # live sales-prospection dept (full)
  ben/dept.yaml                         # live family-office dept (with SQLite ledger)
  miranda/dept.yaml                     # mid-onboarding content dept (status=onboarding)
skill_lib/
  __init__.py
  templates.py                          # render_template(name, ctx) - Jinja2 + StrictUndefined
  state_machine.py                      # OnboardingStateMachine, STATUSES, InvalidTransition
  layers.py                             # map_layers, generate_layer_prompt_stub
  skills_tools.py                       # build_manifest, validate_card
  gates.py                              # ALL_AUTONOMY_MODES, build_authorization_band
  dry_run.py                            # run_dry_run, DryRunStatus
  activation.py                         # flip_status_to_live, build_activation_pr_body
  isolation_scaffold.py                 # scaffold_isolation_surface(...) - Step 4b
tests/
  conftest.py
  test_step1_mandate.py
  test_isolation_scaffold.py            # asserts the Step-4b scaffolding
  test_step2_missions.py
  test_step3_layers.py
  test_step4_skills_tools.py
  test_step5_gates_kpis.py
  test_step6_dry_run.py
  test_step7_activation.py
  test_progression_state_machine.py
```

## Running tests

```bash
cd projects/bubble-ops-loop/skills/department-onboarding-guide
python3 -m pytest tests/ -v
```

Expected: 20 tests pass in ~0.2s.

## Schema dependency

The skill references schemas at
`projects/bubble-ops-loop/schemas-draft/*.schema.yaml`. Step 1 of the onboarding
flow requires schema v3.1 (which adds `status`, `display_name`, `owner`,
`forbidden` to the `department` block). Existing v3 fixtures stay valid
(all new fields are optional, status defaults to `live` when absent).
