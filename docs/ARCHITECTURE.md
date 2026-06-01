# bubble-ops-loop — Architecture Reference

**Audience:** someone deciding whether to extend, fork, or migrate this system. Different from Notion (the design-time spec) and `MVP-ROADMAP.md` (the chronological build log) — this is the **post-build empirical map** as of 2026-05-20, after Steps 0-11 + UX-1 through UX-5 shipped with 200/200 tests passing on the fixture round-trip.

**Source of truth on contradictions:** Notion page `bubble-ops-loop — Architecture finale simplifiée` (id `366cfc52-0644-81dc-a58a-e2a41e79e11a`, last edited 2026-05-20T19:04 UTC). Local dump: `/tmp/notion_final.txt` (1091 lines). Where this document deviates from Notion, the deviation is called out inline.

---

## 1. System topology

```
                             ┌──────────────────────────────────────────────┐
                             │       ANTHROPIC CLOUD (Routines)             │
                             │  R1 Data | R2 Plan | R3 Exec | R4 Risk       │
                             │  (daily safety net; consume credits only     │
                             │   when /loop hasn't drained the queue)       │
                             └─────┬────────────────────────────────────────┘
                                   │ git pull/push (HTTPS, GitHub App tokens)
                                   ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │   GITHUB.COM   org=vdk888   private repos:                                 │
   │     bubble-ops-fixture  (live, v1 reference)                              │
   │     bubble-ops-loop     (this framework / template — Rick's WS)           │
   │     bubble-ops-<slug>   (future: maya, ben, tony, miranda, eliot)         │
   │   GitHub App `bubble-ops-bot` (App ID 3782718, Inst 134075326 for fixture)│
   │   .github/CODEOWNERS  +  branch protection (paywalled; CODEOWNERS = live) │
   └─────────────┬─────────────────────────────────────────────────────────────┘
                 │ HTTPS (git, gh api) + Webhook (future v2)
                 ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │   MORTY VPS  (Hetzner CX33, joris-cx33, Ubuntu 24.04, systemd 255)        │
   │                                                                           │
   │   /etc/systemd/system/                                                    │
   │     claude-agent-morty.service          (the bubble-internal agent)       │
   │     ops-loop-fixture.service            (the v1 dept; pty via script(1))  │
   │     ops-loop-<slug>.service             (future; one per dept)            │
   │     ops-loop-watchdog.timer + .service  (40-min cadence health check)     │
   │     bubble-token-broker.service.template (oneshot CLI; not daemon in v1)  │
   │                                                                           │
   │   /opt/bubble-token-broker/             (broker: mint + audit)            │
   │   /opt/bubble-git-guard/                (guard: path policy + push)       │
   │   /var/log/bubble-token-broker/audit.jsonl                                │
   │   /var/log/bubble-git-guard/audit.jsonl                                   │
   │   /srv/bubble-secrets/<pem-file>        (GitHub App private key, SOPS+age)│
   │   /etc/age/key.txt                      (the age decryption key, 0400)    │
   │   /etc/bubble/secrets-<slug>.sops.env   (per-dept SOPS env)               │
   │   /run/claude-agent-<slug>/env          (tmpfs decrypted env, 0400)       │
   │   /home/claude/agents/<slug>/           (the cloned dept repo, working)   │
   │   /home/claude/.claude/channels/telegram-<slug>/access.json               │
   └─────────────┬─────────────────────────────────────────────────────────────┘
                 │ Tailscale mesh (BUBBLE_MORTY_HOST=claude@morty.tailnet)
                 ▼
   ┌──────────────────────────────────┬──────────────────────────────────────┐
   │  JORIS'S MAC                     │   JORIS'S PHONE                       │
   │  bubble-ops-console              │   Telegram bots                       │
   │   (FastAPI + HTMX, single binary)│     @bubtiktikbot   (fixture)         │
   │   8 routes (home/dept/gate/      │     @ContentbubbleClawbot (morty)     │
   │    settings/health/agents/       │     @maya... (future per dept)        │
   │    onboarding/agents-new)        │   Gate cards + audit pings            │
   │   read-only on dept repos;       │   /pair flow per dept                 │
   │   write via broker+guard for     │   Watchdog alerts (40-min staleness)  │
   │   gate decisions                 │                                        │
   └──────────────────────────────────┴──────────────────────────────────────┘
```

---

## 2. Trust boundaries

| Asset | Storage | Access | Crosses internet? | Fail mode |
|---|---|---|---|---|
| GitHub App private key (`bubble-ops-bot.pem`) | `/srv/bubble-secrets/`, SOPS-encrypted with age | root-only, decrypted to in-memory `Callable[[], bytes]` by broker | Never crosses internet; in-tmpfs at runtime | fail-closed (broker exits 1) |
| age decryption key (`/etc/age/key.txt`) | Morty disk, mode 0400 root:root | Root-only via SOPS preexec | Never | fail-closed |
| Per-dept SOPS env (`/etc/bubble/secrets-<slug>.sops.env`) | Morty disk, encrypted | systemd `ExecStartPre=+` decrypts to `/run/claude-agent-<slug>/env` | Never | `ExecStopPost=+` rm on stop |
| GitHub App installation tokens (`ghs_*`) | In-memory only in broker process; cached for `expires_at - 60s` | Single-process lifetime; never logged; redacted from any stderr capture | Crosses internet inside `Authorization: bearer` header on git push | fail-closed; `FORBIDDEN_FIELDS` audit guard |
| Per-dept Telegram bot token | `/etc/bubble/secrets-<slug>.sops.env::DEPT_TELEGRAM_BOT_TOKEN` | Rewritten to `TELEGRAM_BOT_TOKEN=` in tmpfs env (drops morty's prod token) | Crosses internet to api.telegram.org | fail-open (bot silent) |
| Dept repo content (`outputs/`, `queues/`, `inbox/`) | Public Morty disk + private GitHub repo | claude:claude on Morty | Crosses internet (HTTPS git push) | git-guard fail-closed on path violation |
| Structural files (`dept.yaml`, `layers/`, `.claude/agents/`, `skills/`, `tools/`) | Same | claude:claude on Morty | Same; **PR-only via `settings_pr` action class** | guard rejects direct push |
| Audit JSONL (`/var/log/bubble-*/audit.jsonl`) | Morty disk, append-only | root + claude read | Never | metadata-only; secret-leak guards |
| Console authentication | Tailscale-fronted port + bearer token | Joris's Mac on Tailscale mesh only | Never (Tailscale-only) | 401 on missing/wrong bearer |

The **fail-open** vs **fail-closed** split is deliberate: anything touching authorization or git writes is fail-closed (broker, guard, schema validation). Anything touching observability (Telegram pings, heartbeats) is fail-open so a Telegram outage doesn't wedge the loop.

---

## 3. The 7 YAML contracts

All schemas live at `schemas-draft/`, validated by `tests/validate_all.py` against 14 positive + 10 negative examples. Empirical: `python3 schemas-draft/tests/validate_all.py` exits 0 with output `OK — 14 positive + 10 negative checks all matched expectations.`

Note: the README documents 6 schemas; an extra `state.schema.yaml` was added in UX-2 (onboarding STATE.yaml) — **7 total**.

### 3.1 `dept.schema.yaml` (v3, Draft-07)
**Purpose:** the canonical manifest for every department.
**Key fields:** `department{slug,level,mandate,owner,forbidden}` + `layers.subscribed[]` + `recurring_missions[]` + `skills{layer_1..4}` + `tools[]` + `gate_policies{<id>{...}}` + `hierarchy{level,parent,children,visibility,directive_policy}` + `optional_domain_ledger`.
**Positive:** `examples/dept-ops-leaf-fixture.yaml` (148 lines, every required field).
**Negative:** `tests/negative/dept-missing-department-wrapper.yaml` (asserts the v3 `department:` wrapper is required).

### 3.2 `recurring-mission.schema.yaml` (v3, new)
**Purpose:** standalone schema for `missions/<id>.yaml`. Identical shape to `dept.yaml::recurring_missions[]` items.
**Key fields:** `id, layer, cadence, time, day, active_hours, description, input_sources, output_queue, creates[], gate_policy_id`. Cadence allOf-conditional: `daily` requires `time:`; `weekly` requires `time:` + `day:`.
**Positive:** `examples/recurring-mission-hot-prospect.yaml` (Maya's `daily 07:00` mission).
**Negative:** `tests/negative/recurring-mission-invalid-cadence.yaml` (cadence: sometimes).

### 3.3 `queue-item.schema.yaml` (v3)
**Purpose:** the unit of inter-layer communication. Goes into `queues/<type>/<id>.yaml`.
**Key fields:** `id, kind` (snake_case extensible pattern), `priority, source, target_layer, payload, created_at`.
**Positive:** `examples/queue-item-prospect-research.yaml`.
**Negative:** `tests/negative/queue-item-missing-priority.yaml`.

### 3.4 `gate-item.schema.yaml` (v3, enriched)
**Purpose:** human-pending decision card in `queues/gates/<id>.yaml`. Drives the Telegram ping + console card.
**Key fields:** `id, kind, source_layer, target_layer, risk_level, requires_human, current_mode, future_eligible_modes, actions[]`. Domain kinds (`prospect_dm`, `trade_order`, `content_publish`, `domain:*` pattern) ALSO require `gate_policy_id` + `authorization_band_id`.
**Positive:** `examples/gate-item-prospect-dm.yaml` (Maya's "DM Jean Dupont" gate, verbatim from Notion lines 393-411).
**Negative:** `tests/negative/gate-item-invalid-current-mode.yaml` (current_mode: yolo).

### 3.5 `management-export.schema.yaml` (v3)
**Purpose:** the compact summary at `outputs/<date>/management-export.yaml` — the **only** file a parent dept is allowed to read from a child.
**Key fields:** `dept, date, gates_open, missions_completed, kpi_summary{}, links[]`, optional `autonomy_readiness{}` (14/30-day shadow-autonomy deltas).
**Positive:** `examples/management-export-ops-with-autonomy.yaml`.
**Negative:** `tests/negative/management-export-extra-field.yaml` (additionalProperties: false enforcement).

### 3.6 `directive.schema.yaml` (v2, unchanged)
**Purpose:** parent → child instruction opened as a PR to `queues/management/<id>.yaml`. Used by Tony's `open_priority_pr` action class.
**Key fields:** `id, kind, source_dept, target_dept, requires_acceptance, payload, created_at`.
**Positive:** `examples/directive-priority.yaml`.
**Negative:** `tests/negative/directive-missing-requires-acceptance.yaml`.

### 3.7 `state.schema.yaml` (NEW in UX-2)
**Purpose:** the onboarding state file at `onboarding/STATE.yaml`. Tracks the 7-status state machine + per-step commit SHAs.
**Key fields:** `slug, status` (enum: Idea/Configuring/Drafting/Needs validation/Dry run/Ready to activate/Live), `steps{<step>{validated_at,commit_sha,validated_by}}`.
**Positive:** `examples/state-mid-onboarding.yaml`.
**Negative:** `tests/negative/state-bad-status.yaml`.

---

## 4. Output schema standard (the 4-file quartet)

Every layer write on every tick produces **exactly four files** under `outputs/<YYYY-MM-DD>/<layer>/`:

```
outputs/2026-05-20/2/
├── summary.md          # human-readable narrative — what was done, why, what's next
├── artifacts/          # layer-specific structured outputs (.gitkeep if empty)
│   ├── research-001.md
│   └── ...
├── logs.jsonl          # structured action log; one JSON per line; every action a {ts, action, result, ...} record
└── .last-run           # ISO-8601 timestamp of the most recent completion; used by routines for "fresh enough" skip
```

Layer 4 additionally writes **two extra files**:
- `outputs/<date>/4/risk-kpis.yaml` — numeric KPI snapshot, CEO-readable, schema-validated.
- `outputs/<date>/management-export.yaml` — at the **dept level**, not inside `4/`. Schema-validated against `management-export.schema.yaml`. The file's location outside `4/` is a Notion v4 line-465 requirement.

Empirical proof of the quartet contract: `tests/round-trip/test_e2e_dispatch.py` asserts A1+A2 (all four files exist) after the live Layer-2 tick.

---

## 5. Persistence model

| Data | Location | Lifetime | Format |
|---|---|---|---|
| Canonical dept state | `vdk888/bubble-ops-<slug>` on GitHub | Permanent (git history = audit) | YAML + Markdown |
| Working copy | `/home/claude/agents/<slug>/` on Morty | Until manual `rm -rf` | git clone |
| Secrets | `/etc/bubble/secrets-<slug>.sops.env` | Permanent (encrypted) | SOPS+age |
| Decrypted env | `/run/claude-agent-<slug>/env` | Process lifetime (tmpfs, `ExecStopPost=+/bin/rm`) | KEY=val |
| GitHub App installation token | Broker process memory | `expires_at - 60s` (max 60 min TTL) | string `ghs_*` |
| Token broker audit | `/var/log/bubble-token-broker/audit.jsonl` + journald | Forever (journald rotates per logrotate policy) | JSONL, metadata-only |
| Git guard audit | `/var/log/bubble-git-guard/audit.jsonl` | Forever | JSONL |
| Heartbeat | `~/scripts/emit_heartbeat.sh` → `monitoring/heartbeats.jsonl` in Rick_RnD | Append-only | JSONL |
| Dry-run sandbox | `outputs/dry-run/<ts>/` (under any dept root) | Manual cleanup | Same 4-file quartet, deterministic with `--seed=` |
| Onboarding state | `onboarding/STATE.yaml` on `onboarding/<slug>` branch | Until merged at activation | YAML, schema state.schema.yaml |
| Console runtime state | None (stateless; reads GitHub + Morty live) | N/A | N/A |

---

## 6. Failure modes catalogue

The 12-step build produced 11 empirically-documented failure modes worth knowing before you extend this system. Numbers in `(STEP-N)` cite the results file.

| # | Failure mode | Detection signal | Mitigation |
|---|---|---|---|
| 1 | **tmux + `/loop` 404 regression** (STEP-4) | `Error: issue with the selected model` in fixture journal; `404` on every claude API call inside tmux | Banned tmux from systemd unit; use `/usr/bin/script -qfc` for pty allocation instead |
| 2 | **`claude -p` headless disables hooks** (STEP-4 + STEP-7) | Hooks declared in settings.json don't fire; permission prompts silently denied | Banned `claude -p`; the unit uses interactive `claude --dangerously-skip-permissions --channels plugin:telegram@...` |
| 3 | **env stripping at `git push`** (git-guard threat model) | `GITHUB_TOKEN` from env leaks into PAT-fallback code path | Guard explicitly strips `GITHUB_TOKEN`, sets `GIT_ASKPASS=/bin/true`, `GIT_TERMINAL_PROMPT=0`; injects token via local `http.extraheader` only |
| 4 | **`git diff --cached` outside worktree** (STEP-11 Fix 4) | `error: unknown option 'cached'` when guard runs from non-repo cwd; silent fallback to `--no-index` semantics | `staging.py` now calls `git rev-parse --is-inside-work-tree` first; fail-loud with clear error; 4 regression tests at guard test #73 |
| 5 | **Schema drift** (STEP-9 Schema Relaxation v3.1) | A live gate uses `kind: research_decision`, not in v3 enum nor `domain:*` pattern | Either tighten Layer-2 prompt to use `kind: decision` / `domain:research_decision`, or extend the enum; STEP-9 took the relaxation path |
| 6 | **Broker MCP disconnect** | `bubble-token-broker` subprocess returns non-zero or stdout is not `ghs_*` | Guard checks `_token.startswith("ghs_")` before invoking git; audit:mint_failed; fail-closed |
| 7 | **Double-cron tick** | Cloud Routine + `/loop` both run in the same window, writing to the same `outputs/<date>/<layer>/` | Routines read `.last-run` and skip if newer than the layer's cadence ("fresh enough" guard) |
| 8 | **Dry-run fixture path traversal** | A malicious `recurring_missions[0].output_queue` containing `../../etc/passwd` | Schema enforces `pattern: ^queues/.+/?$` on `output_queue:`; dry-run runner refuses outside dept root |
| 9 | **journald flag plumbed but never activated on Morty** (STEP-11 Fix 1) | `journalctl SYSLOG_IDENTIFIER=bubble-token-broker` returns "No entries" despite 100+ mints | systemd unit needs `Environment=BUBBLE_BROKER_JOURNAL=on`; broker wrapper forwards `--journal "$BUBBLE_BROKER_JOURNAL"` |
| 10 | **AppleDouble files (`._*`)** leaked from Mac `rsync` (QA-FIXES-COMPLEMENT) | `find /opt/bubble-token-broker -name '._*'` returned 22 entries owned by uid 501 | rsync `--exclude='._*'`; cleanup in INSTALL-ON-MORTY.md |
| 11 | **Branch protection paywalled on private repos** (QA-FIXES-COMPLEMENT Fix A) | `gh api .../branches/main/protection` returns HTTP 403 with "Upgrade to GitHub Pro" | Pivoted to `.github/CODEOWNERS` + path-policy GitHub Actions workflow; CODEOWNERS is the live defense |
| 12 | **Heartbeat fires once in 11 minutes** (QA-AUDIT-J2 finding) | `monitoring/heartbeats.jsonl` shows isolated entries, no `*/20` cadence | `ops-loop-watchdog.timer` (40-min threshold) Telegrams Joris when stale; Mac scheduled-tasks registry needs explicit cron entry |

---

## 7. Extension points

This system is designed to be extended in 4 well-defined places. Anything outside these is a structural change requiring a `settings_pr` PR.

### 7.1 Add a new layer

Currently 4 layers (Data Refresh / Research / Execution / Risk Control). Adding a 5th (e.g. "Verification") means:
1. Extend `dept.schema.yaml::layers.subscribed.items.enum` from `[1,2,3,4]` to `[1,2,3,4,5]`.
2. Add a 5th persona under `.claude/agents/<persona>.md` with isolated perms.
3. Extend the 4-file quartet test in `tests/non_negotiables_observable.py` to also expect `outputs/<date>/5/`.
4. Add Layer-5 prompt at `layers/5/PROMPT.md` in every dept that subscribes.
5. Optionally add a 5th Cloud Routine for safety net.

### 7.2 Add a new action class to the broker

Currently 4 classes (`runtime_read`, `runtime_write_own`, `open_priority_pr`, `settings_pr`). Adding e.g. `secrets_rotate` means:
1. Add to `token-broker/src/policy.py::PERMISSION_CLASSES` with the minimal GitHub-App perms dict.
2. Add path-policy in `git-guard/src/policy.py` (which paths the new class may write).
3. Update each `deploy/policies/<dept>-policy.yaml` with the actor's eligibility for the new class.
4. Write a test in `token-broker/tests/test_policy_<class>.py` (one per class, currently 4 files).

### 7.3 Add a new department template

The fastest path is to copy an existing dept policy template:
- `token-broker/deploy/policies/ops-leaf-policy.template.yaml` for ops leaves (Maya, Ben, Miranda, Eliot)
- `management-policy.template.yaml` for management depts (Tony)
- `console-policy.template.yaml` drop-in for `bubble-ops-console`

Replace `<DEPT_SLUG>` and `<CHILD_SLUG_*>` placeholders, then run the bootstrap+activate flow (see OPERATOR-GUIDE.md §6-§7).

### 7.4 Add a new gate-policy mode

Currently 5 modes (`manual_required` → `manual_unless_policy_passed` → `auto_if_policy_passed` → `auto_with_veto_window` → `disabled`). To add e.g. `auto_silent` (auto-fire without even a veto window for very-low-risk actions):
1. Extend `dept.schema.yaml::gate_policies.additionalProperties.properties.current_mode.enum` (and `eligible_future_modes.items.enum`).
2. Extend `gate-item.schema.yaml::current_mode.enum` to match.
3. Extend the console's autonomy-raise modal whitelist.
4. Add a test in `tests/subagent-perms/` asserting the new mode flows through the executor's decision.

### 7.5 Add a new console route

Console is FastAPI; routes live in `console/routes/`. Currently 8 route files providing 12 endpoints (home, dept, gate × 2, settings, health, agents × 4, onboarding). To add a route:
1. Create `console/routes/<name>.py` with a `router = APIRouter()` and `@router.get(...)` decorators.
2. Register in `console/main.py::create_app` via `app.include_router(<name>.router)`.
3. Add a Jinja template at `console/templates/<name>.html` (inherit from `base.html`).
4. Add tests at `console/tests/test_<name>.py` (HTMX-aware: assert response content-type + presence of expected `hx-*` attributes).

---

## 8. Component inventory (the post-build map)

| Component | Path | Purpose | Test count |
|---|---|---|---|
| Schemas | `schemas-draft/` | 7 YAML JSON-schemas + 14 positive + 10 negative examples | 1 validator (24 assertions) |
| Token broker | `token-broker/src/` (broker.py, policy.py, cli.py, audit.py) | GitHub-App installation-token minter; in-memory only | 77 tests, ~96% coverage |
| Git guard | `git-guard/src/` (guard.py, policy_loader.py, staging.py, audit.py, cli.py) | Path-policy enforcement at `git push` | 73 tests, ~90% coverage |
| Onboarding skill | `skills/department-onboarding-guide/` (SKILL.md + skill_lib/ + templates/) | 7-step eclosure state machine + dry-run simulator | 38 tests |
| Bootstrap scripts | `scripts/` (bootstrap-dept.sh + validate-step.sh + activate-dept.sh + deploy-to-morty.sh + run-dry-run.sh + lib/) | The CLI entry points an operator types | 27 tests (UX-2) + 9 tests (UX-5) + activation tests |
| Console | `console/` (FastAPI + HTMX, main.py + 8 routes + 12 templates) | The single-binary front-end on Joris's Mac | 21 tests |
| Systemd unit template | `deploy/templates/ops-loop-dept.service.template` | Rendered per dept by deploy-to-morty.sh | tested via test_deploy_to_morty.py |
| Watchdog | `scripts/loop-watchdog.sh` + `scripts/ops-loop-watchdog.{service,timer}` | 40-min cadence health check → Telegram on stale | live on Morty |
| Round-trip E2E | `tests/round-trip/test_e2e_dispatch.py` + `test_layer4_three_outputs.py` | Against live GitHub fixture | 6 + 11 = 17 assertions |
| Observable non-negotiables | `tests/non_negotiables_observable.py` | 6 read-only health checks against live Morty + GitHub | 6 assertions |
| Subagent perms | `tests/subagent-perms/*` | 4 personas × cross-pollination + perm-violation tests | 42 tests |

Total: ~340+ individual test functions across the project (excluding `bubble-vps-platform` which adds another 209).

---

## 9. Where this differs from Notion (post-build deviations)

Three deviations from the Notion v3 spec are documented with rationale:

1. **`state.schema.yaml`** is a 7th schema not described in Notion (which lists 6 contracts). Added in UX-2 to track onboarding state. Sub-schema for `onboarding/STATE.yaml` only — does not change any other contract.

2. **`loop_engine:` block intentionally OMITTED from `dept.yaml`** even though MVP-ROADMAP §3 Step 1 mentioned it. Notion v3 is authoritative and does not include it. Cadence lives in the systemd unit, not the dept manifest. Documented at `schemas-draft/examples/dept-ops-leaf-fixture.yaml` lines 17-27.

3. **Branch protection is documented xfail** because GitHub's API is paywalled on private repos. `.github/CODEOWNERS` is the live alternative; the path-policy GitHub Actions workflow surrogate is blocked on a missing `workflows: write` App permission. Both are tracked with remediation notes in `QA-FIXES-COMPLEMENT-RESULTS.md` Fix A.

---

## 10. Versioning + migration discipline

- **No backwards compat at the contract layer.** v3 dept.yaml does not accept v1 shape. Legacy crons run in parallel until parity is proven; then they are retired. Notion: *"on accepte de redémarrer le setup VPS from scratch si besoin."*
- **Schema versions are explicit in YAML headers** (e.g. `schema_version: dept.schema.yaml v3 (Draft-07)`). The v1 schema is preserved at `schemas-draft.bak-v1-20260520-1505/` for reference + the TDD harness pattern.
- **The MVP-ROADMAP is itself preserved versioned** at `MVP-ROADMAP.bak-v1-incomplete-20260520.md` for reference.
- **GitHub App permissions are scope-locked** at install time; widening requires re-installing the App (admin path, hors-runtime per Notion §"Cycle de vie des actions GitHub").

For future migrations (Maya, Ben, Tony, Miranda, Eliot), the Phase 3-5 sections of `ROADMAP.md` document the shadow-mode → parity → cutover pattern: 3 days of shadow output ±10% of legacy, ≥1 gate flowing through console, then `systemctl disable` (not `rm`) on legacy crons for a 30-day revert window.

---

*For day-2 operations (where to look when X breaks), read `docs/OPERATOR-GUIDE.md` §8. For the build-log narrative (RED→GREEN cycles, gotchas captured live), read the `STEP-*-RESULTS.md` files in chronological order.*
