# bubble-ops-loop — Operator Guide

**Audience:** an engineer who knows Python + Docker + Linux + GitHub but is fresh to `bubble-ops-loop`. After reading this guide you can deploy, operate, debug, and extend the framework end-to-end.

**Status:** Live as of 2026-05-20. One reference department (`vdk888/bubble-ops-fixture`) is provisioned end-to-end on the Morty VPS; the Maya migration is the next planned department.

**Companion docs:** `docs/ARCHITECTURE.md` (post-build empirical map) · Notion source-of-truth `bubble-ops-loop — Architecture finale simplifiée` (id `366cfc52-0644-81dc-a58a-e2a41e79e11a`) · `MVP-ROADMAP.md` (12-step chronological build log).

---

## 1. Why bubble-ops-loop exists

Bubble Invest runs a stack of department-shaped agents (Maya does sales, Ben runs the fund, Tony coordinates, Miranda owns content, Eliot owns security). Before this framework, each department was a heap of disjointed cron jobs — every cron embedded its own "see something / decide / act / log" logic, every department wrote its own Telegram pings, every behaviour change required editing N crons in M repos. Adding a new department took days. Pausing a runaway action required hunting through `crontab -l` on a Mac that sometimes sleeps.

`bubble-ops-loop` collapses that into **one operating shape, applied per department**: each dept owns a GitHub repo with a `dept.yaml` manifest, four OODA layers (Data Refresh → Research → Execution → Risk Control), a flat `queues/` filesystem-as-bus, a `outputs/<date>/<layer>/` rolling audit trail, and a small set of "subagents" with isolated permissions. A `/loop` agent on the **Morty VPS** is the main engine — one long-running tmux-less systemd unit per dept, ticking every 20 minutes. **Four global Anthropic Cloud Routines** act as a daily safety net so the system keeps moving even if Morty dies.

Three deliberate constraints make it operable rather than ornate:
1. **No central orchestrator.** Git is the bus and the audit log. `queues/` directories are inboxes; `outputs/<date>/<layer>/` are append-only timelines.
2. **No long-lived PAT.** Every push goes through the `bubble-token-broker` (mints short-lived GitHub-App tokens) and the `bubble-git-guard` (path policy at push time). The agent itself never sees a PAT.
3. **Human-in-the-loop by default.** Every gated action starts in `manual_required` mode; autonomy is raised only after shadow-mode + KPI guardrails prove an action class safe.

Notion verbatim (line 7): *"Cible : Maya migrée fin sem 2, Tony + Ben + console fin sem 3, polish + retire legacy sem 4."*

---

## 2. The 4-layer OODA cycle

Every department subscribes to one or more of four OODA layers. Each layer is a prompt (`layers/<N>/PROMPT.md`) executed by a perm-scoped subagent. Per-layer outputs always follow the **4-file quartet**: `summary.md` + `artifacts/` + `logs.jsonl` + `.last-run`.

| Layer | Purpose | Cadence (default) | Subagent persona | Reads | Writes |
|---|---|---|---|---|---|
| 1 | **Data Refresh** | daily 06:00 UTC | `data-curator` | external feeds + previous day's `outputs/{2,3,4}/` | `outputs/<date>/1/` + materializes `recurring_missions:` into `queues/research/` |
| 2 | **Research / Plan** | every 20 min | `task-orchestrator` | `queues/research/` + `queues/management/` | `outputs/<date>/2/` + gates into `queues/gates/` |
| 3 | **Execution** | every 20 min | `executor` | `inbox/decisions/<id>.yaml` (human-validated) | broker actions + `outputs/<date>/3/exec-log.jsonl` |
| 4 | **Risk Control** | daily 22:00 UTC | `mandate-guardian` | the full day's `outputs/<date>/{1,2,3}/` + `MANDATE.md` | `outputs/<date>/4/risk-brief.md` + `risk-kpis.yaml` + `outputs/<date>/management-export.yaml` |

**Real-world flow observed on the fixture (STEP-8-ROUND-TRIP-RESULTS.md):** at 18:35 UTC a queue item `queues/research/research-roundtrip-test-001.yaml` was pushed to GitHub. The Morty `*/20` cron tick fired at 18:41:16 UTC; the `task-orchestrator` Layer-2 subagent ran, wrote the 4-file output quartet + a `queues/gates/gate-roundtrip-test-001.yaml`, and pushed commit `1a31ab7` to `main` at 18:44:52 UTC — **9 min 52 s end-to-end**, zero human touch. The input queue item was correctly consumed (GitHub Contents API returns 404 for the original path). Six pytest assertions in `tests/round-trip/test_e2e_dispatch.py` pass against the live GitHub state.

**Layer 4 specifically writes 3 outputs**, not one — `risk-brief.md` (qualitative), `risk-kpis.yaml` (numeric KPI snapshot), and `outputs/<date>/management-export.yaml` (compact, hierarchy-consumable, schema-validated against `management-export.schema.yaml`). The management export is the **only file a parent department is allowed to read** from its child.

---

## 3. Anatomy of a department repo

Every `bubble-ops-<slug>` repo has the same shape. The fixture (`vdk888/bubble-ops-fixture`) is the byte-exact reference.

```
bubble-ops-<slug>/
├── dept.yaml                          # the manifest — validates against schemas-draft/dept.schema.yaml v3
├── MANDATE.md                         # long-form mission statement (Layer 4 audits against this)
├── CLAUDE.md                          # /loop self-init prompt; cwd-anchored
├── .github/CODEOWNERS                 # path-policy at the GitHub side (Fix A in QA-FIXES-COMPLEMENT)
├── layers/
│   ├── 1/PROMPT.md   2/PROMPT.md   3/PROMPT.md   4/PROMPT.md
├── .claude/
│   ├── agents/                        # 4 subagent personas, perm-scoped
│   │   ├── data-curator.md   task-orchestrator.md   executor.md   mandate-guardian.md
│   └── settings.json
├── tools/<name>/                      # deterministic Python functions (e.g. tools/notify-gate/)
├── skills/<name>/SKILL.md             # agentic procedures that call tools
├── missions/                          # recurring-mission manifests (recurring-mission.schema.yaml)
├── queues/
│   ├── research/                      # Layer 2 inbox
│   ├── gates/                         # human-pending decisions
│   ├── management/                    # cross-hierarchy directives in
│   └── improvements/                  # Layer 4 → Layer 1 next-day feedback
├── inbox/decisions/                   # human-validated outputs of gates → Layer 3 reads here
├── outputs/<YYYY-MM-DD>/{1,2,3,4}/    # the 4-file quartet per layer per day
└── tests/                             # tests/run.sh + fixtures/{tool,skill,layer,department}/
```

The fixture's `dept.yaml` is 148 lines and exercises every required field including a never-auto gate policy (`mandate_breach_escalation` with `eligible_future_modes: []`). It is committed as the canonical reference at `schemas-draft/examples/dept-ops-leaf-fixture.yaml` and validates clean against `dept.schema.yaml` v3.

`recurring_missions:` is required even when empty (management departments declare `recurring_missions: []`). Notion calls this anti-footgun: *"un département a une identité logique et une policy d'accès, pas un secret permanent."*

---

## 4. The 5 autonomy modes

Every gated action class declares an autonomy mode in `dept.yaml::gate_policies.<id>.current_mode`. The five modes form a strictly ordered ladder; KPI degradation auto-downgrades a policy back to `manual_required`.

| # | Mode | Behaviour | Realistic example |
|---|---|---|---|
| 1 | `manual_required` | Every instance creates a gate; Joris approves/rejects in the console. v1 default for every domain action. | Maya sending a prospect DM today — every draft pings Telegram. |
| 2 | `manual_unless_policy_passed` | Gate raised only when an authorization-band check fails (e.g. cold prospect, off-hours). Warm/in-band instances still gate. | Same Maya DM, but warm-prospect instances skip the gate after KPIs stay green for 14 days. |
| 3 | `auto_if_policy_passed` | Action auto-fires if the policy passes; otherwise gates. Layer-4 monitors reply-rate / negative-reply / human-edit. | After Maya proves <2% negative-reply rate for 30 days, warm prospect DMs auto-send. |
| 4 | `auto_with_veto_window` | Action queued with a delay (e.g. 5 min); Joris can veto via Telegram before it fires. | Ben Layer 3 trade-order placement — auto-fires unless vetoed in 60s. |
| 5 | `disabled` | Action class is paused at policy level (no gates, no execution). | Used during incident response or KPI degradation. |

`eligible_future_modes: []` declares a policy can **never** be raised — used by `mandate_breach_escalation` on every dept. The console refuses to raise a policy outside its declared `eligible_future_modes` whitelist. Layer 4's `management-export.yaml` carries an optional `autonomy_readiness:` block with 14/30-day shadow-autonomy deltas (would-have-auto vs human-approved/modified/rejected), which is the data Joris uses to decide whether to raise a mode.

---

## 5. Token broker + git guard — the no-PAT chain

Every git write from a `/loop` agent flows through two co-resident components on Morty: `bubble-token-broker` (mints short-lived GitHub-App installation tokens) and `bubble-git-guard` (verifies path policy before any push). Together they replace the long-lived PAT model that v1 used. The `GITHUB_TOKEN` environment variable is **stripped** from the env passed to `git push`; there is no fallback path.

```
ops-loop-fixture.service
       │
       ▼
  bubble-git-guard push --dept fixture --action runtime_write_own --repo bubble-ops-fixture \
       --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \
       --broker /opt/bubble-token-broker/bin/bubble-token-broker \
       --audit-log /var/log/bubble-git-guard/audit.jsonl
       │
       │── 1. read `git diff --cached` + `git diff @{upstream}..HEAD` → list of paths
       │── 2. policy.enforce(each_path)   → any deny → audit:denied + exit 1, NO broker call
       │── 3. subprocess `bubble-token-broker mint --paths …`  → short-lived ghs_* token
       │── 4. `git -c http.extraheader='AUTHORIZATION: bearer <token>' push`
       │── 5. audit:pushed | push_failed → /var/log/bubble-git-guard/audit.jsonl + journald
       ▼
  exit 0 (success) | exit 1 (any fail, fail-CLOSED)
```

Four **action classes** are encoded in both the broker and the guard:
- `runtime_read` — `contents:read, metadata:read`, no path restriction (read-only).
- `runtime_write_own` — `contents:write` scoped to `outputs/**`, `queues/**`, `inbox/**` in the dept's own repo. Direct commit allowed.
- `open_priority_pr` — Tony → child dept; target `queues/management/**` only; PR-only.
- `settings_pr` — anything structural (`dept.yaml`, `layers/**`, `.claude/agents/**`, `skills/**`, `tools/**`, `.claude/settings.json`); PR-only.

**Audit trail** is double-rail: structured JSONL at `/var/log/bubble-token-broker/audit.jsonl` + `/var/log/bubble-git-guard/audit.jsonl`, plus journald (`journalctl SYSLOG_IDENTIFIER=bubble-token-broker`) with explicit fields `ACTION=runtime_read DEPT=fixture STATUS=issued`. The audit module's `FORBIDDEN_FIELDS` guard drops anything named `token`/`pem`/`secret`/`private_key`/`jwt`, and any value starting with `ghs_` raises `ValueError` before write. Empirical: zero `ghs_*` substrings have ever appeared in audit logs (verified by `grep -c "ghs_" /var/log/bubble-*` returning 0).

---

## 6. Onboarding a new department

A new dept is "eclosed" through the 7-step **Agent Nursery** flow (Notion §"UX — Agent Nursery / Department Onboarding"). The flow lives in the `department-onboarding-guide` skill (`skills/department-onboarding-guide/SKILL.md`) — a pure-Python helpers + Jinja templates + state-machine + deterministic dry-run simulator. The agent (Claude Code session) drives the conversation; the skill enforces the schema and the state transitions.

The 7 statuses are `Idea → Configuring → Drafting → Needs validation → Dry run → Ready to activate → Live`. Each step writes one artifact, validates it, commits on branch `onboarding/<slug>`, and updates `onboarding/STATE.yaml`.

| Step | Status after | Artifact | Validates against |
|---|---|---|---|
| 1. Mandate | Configuring | `dept.yaml.draft` (department block) | `dept.schema.yaml::department` |
| 2. Recurring missions | Drafting | `missions/*.yaml` | `recurring-mission.schema.yaml` |
| 3. Layer mapping | Drafting | `layers/{1,2,3,4}/PROMPT.md` | layer-presence + 4-file quartet contract |
| 4. Skills / tools | Drafting | `skills/<slug>/SKILL.md` + `tools/<slug>/` | Tool-vs-skill distinction check |
| 5. Gates / KPIs | Needs validation | `dept.yaml::gate_policies` | 5-mode enum + `eligible_future_modes` whitelist |
| 6. Dry-run | Dry run → Ready | `outputs/dry-run/<ts>/{1,2,3,4}/` | `run-dry-run.sh --dept-root=… --seed=42` (deterministic, can_advance_to_ready) |
| 7. Activation | Live | activation PR on `main` | `can_activate()` gate + all 6 work-steps validated |

**Operator entry points:**
```
$ ./scripts/bootstrap-dept.sh --slug=miranda --display-name="Miranda" --owner=joris
$ ./scripts/validate-step.sh --slug=miranda --step=mandate --repo-dir=/tmp/bubble-ops-miranda
$ ./scripts/run-dry-run.sh --dept-root=/tmp/bubble-ops-miranda --seed=42
$ ./scripts/activate-dept.sh --slug=miranda --dry-run     # preview the PR body
$ ./scripts/activate-dept.sh --slug=miranda               # opens the PR via broker+guard
```

The console at `/agents/<slug>/onboarding` renders the 3-pane Notion mock-up: status checklist (left), conversation context (centre), live artifact preview (right). The `Continue onboarding` button on the Agent Nursery card resumes the skill from the last persisted status.

---

## 7. Deploying to Morty

Once a department is activated on GitHub (`status: live`), Morty needs a systemd unit. The flow is two scripts + one Telegram pairing.

```
$ ./scripts/deploy-to-morty.sh --slug=miranda --dry-run     # renders the unit, prints every SSH command, no side effects
$ ./scripts/deploy-to-morty.sh --slug=miranda               # ssh in, install /etc/systemd/system/ops-loop-miranda.service, daemon-reload, enable+start
```

The script renders `deploy/templates/ops-loop-dept.service.template` with three placeholders (`${DEPT_SLUG}`, `${TELEGRAM_STATE_DIR}`, `${ENV_FILE}`). Three doctrine invariants are baked into the template:
1. `/usr/bin/script -qfc …` for pty allocation. **No tmux** — Step 4 documented a load-bearing tmux+`/loop` 404 regression that took 90 minutes to diagnose.
2. The interactive `claude` binary with `--dangerously-skip-permissions --channels plugin:telegram@claude-plugins-official`. **No `claude -p`** — becomes paid June 15 and headless mode disables hooks.
3. Per-dept SOPS env at `/etc/bubble/secrets-<slug>.sops.env`, decrypted to `tmpfs` at `/run/claude-agent-<slug>/env` mode `0400 claude:claude`, removed on `ExecStopPost`.

The deploy script **refuses** to touch `/etc/systemd/system/claude-agent-morty.service` (MD5 `ecfc78ac20e182ca302e5081e2c80943`). Production unit is sacred; deploys add sibling units only.

After provisioning, pair the dept's dedicated Telegram bot (one bot per dept; the fixture uses `@bubtiktikbot`, morty uses `@ContentbubbleClawbot`) by sending `/pair` from Joris's phone. The dept's `.claude/channels/telegram-<slug>/access.json` records the chat_id.

Verify:
```
$ ssh hetzner "systemctl status ops-loop-fixture.service | head -8"
$ ssh hetzner "sudo journalctl -u ops-loop-fixture -n 30 --no-pager"
$ ssh hetzner "ls /home/claude/agents/fixture/outputs/$(date +%F)/"
```

---

## 8. Day-2 operations — where to look when X breaks

| Symptom | First place to look | Then |
|---|---|---|
| `/loop` stopped ticking | `journalctl -u ops-loop-<slug> -n 100 --no-pager` | `ops-loop-watchdog.timer` should have fired Telegram (40-min threshold). The **layer floor** also auto-restarts a dead **department** (tony/ben/maya/accountant) when a backup tick can't revive it — max 3×/rolling-hour/dept, then it escalates to Telegram. **Concierges (morty/claudette) are never auto-restarted** (no loop — safety guard in `scripts/lib/auto_restart.py`). Disable with `BUBBLE_AUTORESTART=0`; opt a dept out with `BUBBLE_AUTORESTART_OPTOUT="<slug>"`. Restart history: `state/auto-restart.jsonl`. |
| Push silently failed | `tail /var/log/bubble-git-guard/audit.jsonl` — look for `denied` or `push_failed` events | Cross-check with `gh api repos/vdk888/bubble-ops-<slug>/commits` |
| Token mint failed | `sudo journalctl SYSLOG_IDENTIFIER=bubble-token-broker -n 20 --output=json` | Confirm `BUBBLE_BROKER_JOURNAL=on` env is present in the systemd unit |
| Gate didn't ping Telegram | Check `tools/notify-gate/` was invoked at Layer-2 step 7; tail `outputs/<date>/2/logs.jsonl` | Inspect `tools/notify-gate/schema.yaml` for I/O contract |
| Schema drift | `cd schemas-draft && python3 tests/validate_all.py` against suspect YAML | 14 positive + 10 negative examples must all match expectations |
| Subagent perm violation | `tests/subagent-perms/*.py` (42 tests) | `notion_v4_contract.py` is the single source of truth for the 4 personas |
| Branch protection bypass | `gh api repos/.../branches/main/protection` (paywalled on private repos) | `.github/CODEOWNERS` is the live fallback (Fix A in QA-FIXES-COMPLEMENT) |
| Cron-tick missing on Mac | `mcp__scheduled-tasks__list_scheduled_tasks` — there's a known gap where the fixture `/loop` cron has no Mac registry entry (STEP-10 §Gotcha) | Re-register via the scheduled-tasks skill |

The 6-non-negotiables observable test (`tests/non_negotiables_observable.py`) is the canonical health check: read-only, runs against the live GitHub repo + ssh hetzner, all 6 must pass on a healthy deployment. Run it before any structural change.

**Runbook snippet — verify schemas + the fixture's dept.yaml round-trip**

```
$ cd /Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/schemas-draft
$ python3 tests/validate_all.py
Loaded 7 schemas from .../schemas-draft:
  - dept.schema.yaml
  - directive.schema.yaml
  - gate-item.schema.yaml
  - management-export.schema.yaml
  - queue-item.schema.yaml
  - recurring-mission.schema.yaml
  - state.schema.yaml
Validating 14 positive examples (expect PASS):
[PASS] examples/dept-ops-leaf-fixture.yaml against dept.schema.yaml
...
Validating 10 negative examples (expect FAIL):
[PASS] tests/negative/dept-missing-department-wrapper.yaml against dept.schema.yaml (rejected as expected: []: 'department' is a required property)
...
OK — 14 positive + 10 negative checks all matched expectations.
$ echo "exit=$?"
exit=0
```

Then cross-check the live deploy with the local schema example (byte-identical = drift-free deployment):
```
$ diff -q schemas-draft/examples/dept-ops-leaf-fixture.yaml <(gh api repos/vdk888/bubble-ops-fixture/contents/dept.yaml --jq '.content' | base64 -d)
```
QA-AUDIT-J2 confirms this exits 0 on the live fixture today.

**Runbook snippet — preview a Morty systemd unit before provisioning**

```
$ ./scripts/deploy-to-morty.sh --slug=miranda --dry-run | head -30
==================== rendered unit ====================
# /etc/systemd/system/ops-loop-miranda.service
# Bubble Ops-Loop systemd unit — UX-5 deploy template.
...
[Unit]
Description=Claude Agent — ops-loop-miranda (bubble-ops-loop, dept=miranda)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=claude
Group=claude
WorkingDirectory=/home/claude/agents/miranda
...
```

This is the operator's pre-flight check: every placeholder substituted, every `ExecStartPre=` step printable, no SSH side effects until the `--dry-run` flag is removed.

---

## 9. The 6 doctrine non-negotiables

These six are structural — they must be true on day 1 of every dept or they leak as tech debt forever.

1. **`optional_domain_ledger:` slot present in `dept.yaml`** (even when null) — anti-footgun for future ledger-backed depts like Ben/SQLite.
2. **Tool vs Skill distinction** — sibling `tools/<slug>/` (deterministic Python) and `skills/<slug>/SKILL.md` (agentic procedure) directories. Notion: *"Tool = fonction déterministe ... Ne raisonne pas."*
3. **`tests/run.sh` harness covering 4 levels** (tool, skill, layer, department) exists and is executable.
4. **`queues/management/` directory present** in every dept (even leaf depts with no parent) — the cross-hierarchy escalation queue.
5. **Layer 4 produces 3 outputs** — `risk-brief.md` + `risk-kpis.yaml` + `outputs/<date>/management-export.yaml` (the last lives at dept-level, **not** inside `4/`).
6. **`hierarchy:` block in `dept.yaml`** with `level`/`parent`/`children`/`visibility`/`directive_policy`. `visibility.read_secrets` is schema-`const: false` — no dept can ever set it `true`.

---

## 10. Glossary

- **OODA** — Observe / Orient / Decide / Act. The four-layer cycle is a fractal OODA loop applied to a single department's daily operation.
- **gate-item** — A YAML file in `queues/gates/<id>.yaml` representing a decision waiting for a human. Schema: `gate-item.schema.yaml`. Domain kinds (`prospect_dm`, `trade_order`, `content_publish`, `domain:*`) require `gate_policy_id` + `authorization_band_id`.
- **recurring-mission** — A declarative scheduled job in `missions/<id>.yaml` or inline in `dept.yaml::recurring_missions[]`. Replaces v1 ad-hoc crons. Layer 1 materializes them into `queues/research/` queue items each tick.
- **queue-item** — A YAML in `queues/<type>/<id>.yaml`. The unit of inter-layer communication. Schema: `queue-item.schema.yaml`. `kind:` is a snake_case extensible enum.
- **directive** — A YAML in `queues/management/<id>.yaml` opened as a PR from a parent dept (Tony) to a child dept. Requires explicit acceptance. Schema: `directive.schema.yaml`.
- **management-export** — The compact, hierarchy-consumable summary at `outputs/<date>/management-export.yaml`. The **only** file a parent dept reads from a child. Schema: `management-export.schema.yaml`.
- **shadow autonomy** — Running a higher autonomy mode (e.g. `auto_if_policy_passed`) in parallel with `manual_required` for 14/30 days; Layer 4 tracks would-have-auto vs human-approved/modified/rejected; raised once KPIs prove safe.
- **dry-run** — Deterministic Layer 1→4 simulation against a fake queue item. `run-dry-run.sh --seed=42` produces byte-identical artifacts for identical seeds. Gate before activation.
- **eclosure** — The 7-step onboarding flow (Mandate → Activation). Notion: *"un agent à éclore existe déjà techniquement comme repo / session d'onboarding, mais il n'est pas encore live."*
- **dept.yaml** — The department manifest. `department:` wrapper + `layers:` + `recurring_missions:` + `skills:` + `tools:` + `gate_policies:` + `hierarchy:` + `optional_domain_ledger:`. Schema: `dept.schema.yaml` v3.
- **gate_policy** — A policy object in `dept.yaml::gate_policies.<id>` declaring `current_mode` + `eligible_future_modes` + `authorization_band` + `kpi_guardrail_set` for one action class.
- **action class** — One of the four token-broker permission tiers: `runtime_read`, `runtime_write_own`, `open_priority_pr`, `settings_pr`. Each maps to a minimal GitHub-App permission set at mint time.
- **filesystem-as-bus** — The architectural doctrine that all inter-layer / inter-dept communication happens via files in a git repo. No DB, no message queue, no orchestrator.
- **belt + suspenders** — The dual-rail safety doctrine: `/loop` is the main engine, Cloud Routines are the daily safety net; broker scopes the token, guard scopes the path; CODEOWNERS guards at the GitHub side, git-guard guards at the push side.
- **Morty** — The Hetzner CX33 VPS (`joris-cx33.tailnet`) hosting all `/loop` sessions. Provisioned by `bubble-vps-platform` (pyinfra, 209+ tests).

---

## 11. Operational doctrine — five rules that survived nine iterations

The Notion page records nine Telegram iterations between Joris and Lab before the architecture stabilised. Five operating rules came out of those rounds and are non-obvious enough to call out:

1. **The dept owns its own execution.** Cloud Routines and the Morty `/loop` both write to the same `outputs/` tree on the same git repo; they never coordinate via a third party. The "fresh enough" guard (`.last-run` newer than cadence → skip) is the only deconfliction mechanism. Notion: *"Le département cible reste propriétaire de son exécution."*

2. **Git is the bus AND the audit log.** Every state transition is a commit. The git log on a dept repo IS the day's narrative — you do not need a separate event store. This is why every layer write commits the 4-file quartet rather than streaming bytes into a database.

3. **No dept reads another dept's raw artifacts.** `hierarchy.visibility.read_raw_artifacts` is `false` everywhere; `read_secrets` is schema-`const: false`. The only cross-dept read is `outputs/<date>/management-export.yaml` — the compact, schema-validated, deliberately-narrow summary. Tony never grep's Maya's inbox.

4. **Onboarding intelligence lives in the agent, not in the front-end.** Notion line 1004 verbatim: *"L'intelligence d'onboarding vit dans l'agent et dans la skill `department-onboarding-guide`."* The console renders the chat + the live artifact pane; the skill provides templates + state machine + dry-run; the **agent** (Claude Code session) does the reasoning. The console is not a wizard.

5. **Autonomy raises require KPIs, not opinions.** Every gate policy declares `kpi_guardrail_set:`. Layer 4's `autonomy_readiness:` block carries 14/30-day shadow-mode deltas. The console refuses to raise a policy unless (a) it is in the `eligible_future_modes` whitelist AND (b) the KPI set is green. This is the only thing standing between "shadow Maya draft" and "auto-sent DM at 03:00 UTC."

---

*This guide is the operator-facing entry point. For the architectural reference (failure modes, trust boundaries, extension points), read `docs/ARCHITECTURE.md`. For the build narrative (12 steps, RED→GREEN cycles), read `MVP-ROADMAP.md` + the `STEP-*-RESULTS.md` reports. For the canonical design spec on contradictions, the Notion page wins.*
