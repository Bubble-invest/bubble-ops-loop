# `bubble-ops-loop` — MVP-ROADMAP v2 (Notion-aligned, fixture round-trip on the VPS)

**Owner:** Rick (R&D) • **Co-builder:** {{OPERATOR}} (founder) • **Target:** {{VPS_HOST}}
**Source of truth:** Notion page `bubble-ops-loop — Architecture finale simplifiée` (id `366cfc52-0644-81dc-a58a-e2a41e79e11a`, last edited **2026-05-20T14:01 UTC** by {{OPERATOR}}). Dump at `/tmp/notion_final.txt` (464 lines).
**Scope:** Smallest possible slice that compiles end-to-end with **full Notion v2 contracts** (not a stripped-down precursor). One fixture dept, one repo, one tmux+systemd unit, one `/loop`.
**Budget:** ≤5 working days (target: 3).
**Predecessor:** `MVP-ROADMAP.bak-v1-incomplete-20260520.md` — keep its valid steps, fix its 6 misses.

---

## 0. Notion alignment check — the 6 non-negotiables, encoded

Read `/tmp/notion_final.txt` in full. Citation proof: page TL;DR line 7 reads "Cible : Maya migrée fin sem 2, Tony + Ben + console fin sem 3, polish + retire legacy sem 4." Other unique strings verified: "Filesystem-as-bus", "belt + suspenders", "fmp-data-curator décide quelles données FMP appeler", "Le département cible reste propriétaire de son exécution."

| # | Notion non-negotiable | How the v2 MVP bakes it in |
|---|---|---|
| 1 | **Domain ledger optional but allowed** (Ben SQLite ok; Maya/Tony filesystem) | `dept.yaml` ships with `optional_domain_ledger: null` field present. Schema (`templates/schemas/dept.schema.yaml`) declares it. Fixture leaves it null but the slot exists day-1. |
| 2 | **Tool vs Skill distinction** | Repo ships **both** `tools/echo-tool/` (deterministic Python function + JSON-schema for IO) and `skills/echo-skill/SKILL.md` (agentic procedure that calls the tool). Two stubs prove the two-tier pattern; future depts copy the shape. |
| 3 | **`tests/` + per-component fixtures** | `tests/` dir with `fixtures/tool/echo-input.json`, `fixtures/skill/echo-context.yaml`, `fixtures/layer/queue-item.yaml`, `fixtures/department/dry-run-input.yaml`, plus `tests/run.sh` harness that runs ≥1 test per level (tool→skill→layer→department). Tests may be stub-grade but the harness is real. |
| 4 | **`queues/management/` in every dept** | Directory present (with `.gitkeep`) even though the fixture has no parent. Layer 1 stub scans `queues/management/` before `queues/research/`. Pattern is consistent from day 1. |
| 5 | **Layer 4 produces 3 outputs** | Layer 4 stub writes ALL of: `outputs/<date>/4/risk-brief.md` (qual), `outputs/<date>/4/risk-kpis.yaml` (KPI), `outputs/<date>/management-export.yaml` (compact summary). All 3 schema-validated against `templates/schemas/`. |
| 6 | **`hierarchy:` block in `dept.yaml`** | Fixture's `dept.yaml` carries the full `hierarchy:` block (`level: ops, parent: null, children: [], visibility:{...}, directive_policy:{can_open_priority_prs:false}`). Schema requires it. Future Tony dept reuses same schema. |

**Also from Notion (not on the 6 list but binding):**
- **Output schema is 4 files per layer write** (`summary.md` + `artifacts/` + `logs.jsonl` + `.last-run`) — enforced in every stub prompt.
- **Standardized YAML contracts** for queue-item, gate-item, management-export, directive — all 5 JSON-schemas land in `templates/schemas/` in commit 1.
- **Subagent personas** (4: data-curator, task-orchestrator, executor, mandate-guardian) — kept from v1 plan, confirmed by Notion §"4 personas subagents".
- **`/loop 20m` as main engine, Cloud Routines as safety net** — MVP is the engine slice only (Cloud Routines = Phase 1 of v1, deliberately out of scope here).

---

## 1. Audit delta vs v1 MVP-ROADMAP (re-verified 2026-05-20)

Re-ran the audit commands. **Zero material change** since the v1 attempt. Confirming the v1 audit (§1.1–§1.6 of `MVP-ROADMAP.bak-v1-incomplete-20260520.md`) still holds:

- `claude-agent-morty.service` → still `active`. ExecStartPre/SOPS pattern unchanged.
- `gh` on the VPS → still **NOT installed**. Git auth = custom `git-credential-helper.sh` + `GITHUB_TOKEN`.
- `Bubble-invest/bubble-ops-fixture` → still does NOT exist. `Bubble-invest/bubble-ops-loop` → still does NOT exist.
- `/home/claude/depts/` → does not exist yet; will be created by `ops-loop-fixture.service` `ExecStartPre`.
- VPS disk/RAM/Tailscale/systemd-timers → unchanged.
- `/loop` semantics (built-in in v2.1.x, headless-tmux behavior unproven on Linux) → still the load-bearing unknown → **Step 4 dedicated smoke test stays**.

**New since v1:** the Notion page was edited **2026-05-20T14:01** (after v1 MVP-ROADMAP was written at 16:20 local time but with stale Notion context). v2 reflects the post-14:01 architecture in full.

---

## 2. MVP architecture (ASCII) — fixture slice with Layer 4 3-output split

```
┌─────────────────────────────────────────────────────────────────────────┐
│             GitHub  Bubble-invest/bubble-ops-fixture (PRIVATE)                 │
│  dept.yaml (hierarchy + optional_domain_ledger:null)                    │
│  templates/schemas/{dept,queue-item,gate-item,mgmt-export,directive}    │
│  layers/{1,2,3,4}/PROMPT.md   .claude/agents/*.md   .claude/settings.json│
│  tools/echo-tool/             skills/echo-skill/SKILL.md                │
│  tests/run.sh + fixtures/{tool,skill,layer,department}/                 │
│  queues/{research,gates,management,improvements}/                       │
│  inbox/decisions/                                                       │
│  outputs/<YYYY-MM-DD>/{1,2,3,4}/ + outputs/<YYYY-MM-DD>/management-export.yaml │
└─────┬───────────────────────────────────────────────▲───────────────────┘
   git pull│ (every tick)                       git push│ (every mutation)
          ▼                                              │
┌─────────────────────────────────────────────────────────┴───────────────┐
│ VPS ({{VPS_HOST}}, Hetzner CX33)                                          │
│  systemd: ops-loop-fixture.service (Restart=always, copies existing unit pattern)  │
│   └─ /usr/bin/script -qfc 'loop-autostart.sh' /dev/null   (pty wrap)    │
│       └─ tmux new-session -A -s ops-loop-fixture                        │
│           └─ claude --resume --dangerously-skip-permissions \           │
│               -p '/loop 20m  read dept.yaml; dispatch layers; commit'   │
│               ├─ spawn data-curator    (Layer 1, daily, perm-scoped)    │
│               ├─ spawn task-orchestrator (Layer 2, every tick)          │
│               ├─ spawn executor        (Layer 3, every tick)            │
│               └─ spawn mandate-guardian (Layer 4, daily) ───┐           │
│  GITHUB_TOKEN in /run/claude-agent/env (existing wiring)    │           │
│  heartbeat: ~/scripts/emit_heartbeat.sh ops-loop-fixture    │           │
└──────────────────────────────────────────────────────────────┼──────────┘
                                                               │
                              ┌───── Layer 4 writes 3 outputs ─┘
                              ▼
                outputs/<date>/4/risk-brief.md     (qualitative)
                outputs/<date>/4/risk-kpis.yaml    (KPI snapshot, CEO-readable)
                outputs/<date>/management-export.yaml  (compact, hierarchy-consumable)
                                  │
                                  ▼ Telegram (gate creation only)
                          {{OPERATOR}}'s phone — approve/reject
```

---

## 3. Step-by-step roadmap (chronological, each step ≤4 h, reversible)

### Step 0 — Pin the contracts (1.5 h)  *(new vs v1)*

- **What:** In `Rick_RnD/projects/bubble-ops-loop/`, draft `schemas-draft/` directory with the 5 JSON-schemas the fixture will ship: `dept.schema.yaml`, `queue-item.schema.yaml`, `gate-item.schema.yaml`, `management-export.schema.yaml`, `directive.schema.yaml`. Use plain YAML w/ `$schema: https://json-schema.org/draft-07/schema#`. Hand-validate one example doc against each (PyYAML + `jsonschema` lib OR `python3 -c 'import yaml; yaml.safe_load(open(...))'` for the loose path).
- **Why:** Notion §"Contrats minimaux à standardiser" is the load-bearing structural choice. Defining schemas BEFORE the fixture means later commits cannot drift the contract.
- **Acceptance:** All 5 schemas parse; one passing example per schema in `schemas-draft/examples/`.
- **Effort:** 1.5 h • **Risk:** Low • **Blocked by:** nothing.

### Step 1 — Lock fixture's `dept.yaml` w/ full Notion shape (1 h)  *(expanded vs v1)*

- **What:** Write fixture `dept.yaml` instance with:
  - `hierarchy:` block (level=`ops`, parent=`null`, children=`[]`, visibility w/ empty reads, directive_policy w/ `can_open_priority_prs: false`)
  - `optional_domain_ledger: null` (slot present, value null per Notion §"Maya ou Tony peuvent rester filesystem-only en v1")
  - `subscribed_layers: [1,2,3,4]`
  - `cadence:` per layer (1=daily 06:00, 2=20m, 3=20m, 4=daily 22:00)
  - `gate_policy:` (telegram on, immediate, no digest for MVP)
  - `loop_engine:` (cadence_minutes=20, on_error=log_and_continue)
- **Why:** Schema's worthless if the first instance doesn't exercise every required field. Tony migration in Phase 4 will copy this shape.
- **Acceptance:** `dept.yaml` validates against `dept.schema.yaml`; every required field is present.
- **Effort:** 1 h • **Risk:** Low • **Blocked by:** Step 0.

### Step 2 — Create `Bubble-invest/bubble-ops-fixture` (15 min)

- **What:** From Mac:
  ```bash
  gh repo create Bubble-invest/bubble-ops-fixture --private \
    --description "MVP fixture for bubble-ops-loop /loop pattern (Notion-aligned)"
  git clone git@github.com:Bubble-invest/bubble-ops-fixture
  ```
  Empty repo with just `README.md` + `.gitignore`. The full skeleton lands in Step 5.
- **Why:** Repo URL is needed by Step 3 (token-scope widening).
- **Acceptance:** `gh repo view Bubble-invest/bubble-ops-fixture` returns 200; first commit lands.
- **Effort:** 15 min • **Blocked by:** Step 1.

### Step 3 — Widen the VPS GITHUB_TOKEN to include the fixture repo (15 min)

- **What:** github.com → Settings → Developer settings → edit the existing PAT in `secrets.sops.env`. Add `Bubble-invest/bubble-ops-fixture` to repo scope (fine-grained) OR confirm `repo` classic scope already covers it.
- **Verify on the VPS:**
  ```bash
  ssh hetzner "sudo systemctl restart claude-agent-morty.service && sleep 5 && \
    sudo -u claude bash -c 'source /run/claude-agent/env && \
    git ls-remote https://x-access-token:\$GITHUB_TOKEN@github.com/Bubble-invest/bubble-ops-fixture.git'"
  ```
- **Why:** without push perm, loop can't commit and round-trip dies.
- **Acceptance:** `git ls-remote` returns HEAD SHA.
- **Effort:** 15 min • **Blocked by:** Step 2.

### Step 4 — Smoke-test `/loop` in disposable tmux on the VPS (1 h) — **single biggest unknown**

- **What:**
  ```bash
  ssh hetzner -t "tmux new-session -d -s loop-smoke && \
    tmux send-keys -t loop-smoke 'sudo -u claude -i' Enter && \
    tmux send-keys -t loop-smoke 'cd /tmp && rm -rf loop-smoke-wd && mkdir loop-smoke-wd && cd loop-smoke-wd && \
      claude --dangerously-skip-permissions' Enter"
  # Send into tmux: /loop 1m write the current date to ./tick.log
  # Observe 3 ticks via: tmux capture-pane -t loop-smoke -p
  ```
- **Acceptance:** `tick.log` has ≥3 timestamps ~1 min apart; survives `tmux detach` + reattach.
- **Decision point:** if `/loop` fails → switch to **bash-while-loop fallback** documented at end of Step 7. If success → proceed as designed.
- **Effort:** 1 h • **Risk:** **HIGHEST** • **Mitigation:** dedicated step, no parallel work • **Cleanup:** `tmux kill-session -t loop-smoke; rm -rf /tmp/loop-smoke-wd`.
- **Blocked by:** Step 3 (so we know git auth works for the followup commits, even though smoke test doesn't push).

### Step 5 — Land the full Notion-aligned skeleton as commit 1 (2 h)  *(expanded vs v1)*

- **What:** In the cloned fixture repo, create the tree from §6 below in one commit. Stub contents:
  - `dept.yaml` from Step 1
  - `templates/schemas/*.yaml` from Step 0 (the 5 schemas)
  - `layers/{1,2,3,4}/PROMPT.md` — each produces the **full 4-file output schema** (`summary.md` + `artifacts/.gitkeep` + `logs.jsonl` + `.last-run`)
  - `layers/4/PROMPT.md` additionally produces `risk-kpis.yaml` + `../management-export.yaml`
  - `.claude/agents/*.md` (4 personas, perm-scoped)
  - `.claude/settings.json` (model, mcpServers, allowed-tools)
  - `tools/echo-tool/{tool.py, schema.json, README.md}` (deterministic stub function)
  - `skills/echo-skill/SKILL.md` (agentic stub procedure that calls echo-tool)
  - `tests/run.sh` + `tests/fixtures/{tool,skill,layer,department}/` with one test each (echo PASS is OK)
  - `queues/{research,gates,management,improvements}/.gitkeep`
  - `inbox/decisions/.gitkeep`
- **Why:** the Notion non-negotiables are all structural — they must be present on commit 1 or they leak as tech debt forever.
- **Acceptance:**
  - `tree -L 4` matches §6
  - `bash tests/run.sh` exits 0 with output `tool: PASS / skill: PASS / layer: PASS / department: PASS`
  - All 5 schemas validate (run a Python validator script as part of `tests/run.sh`)
  - `git push` lands the commit.
- **Effort:** 2 h • **Risk:** Med (lots of small files; mitigation = generate from a template script). **Blocked by:** Step 0, 1, 2, 4.

### Step 6 — Lock subagent perms tight before the loop runs (1 h)

- **What:** The 4 `.claude/agents/*.md` files dropped in Step 5 must use the Notion-spec perm scopes:
  - `data-curator`: tools = `Read, WebFetch, Bash(git:*, ls:*, cat:*)`; disallowed = `Write outside layers/1/, outputs/*/1/`; permissionMode = `default`.
  - `task-orchestrator`: tools = `Read, Write, Bash(git:*), Agent`; permissionMode = `acceptEdits`; allowed write paths = `outputs/*/2/, queues/gates/`.
  - `executor`: tools = `Read, Write, Bash(git:*)`; **disallowedTools = `WebFetch, WebSearch`**; write paths = `outputs/*/3/, inbox/decisions/*/processed/`.
  - `mandate-guardian`: tools = `Read, Grep, Glob, WebSearch`; **write only inside `outputs/*/4/` + `outputs/*/management-export.yaml` + `queues/improvements/`**. Pure auditor per Notion line 279.
- **Why:** isolation is the entire safety story. Per Notion §"Subagent" + line 219 "Le département cible reste propriétaire de son exécution."
- **Acceptance:** 4 negative tests (each subagent invoked w/ a forbidden write target is refused). Captured into `tests/fixtures/department/perm-violation.log`.
- **Effort:** 1 h • **Blocked by:** Step 5.

### Step 7 — Hand-roll `ops-loop-fixture.service` + autostart wrapper (1.5 h)

- **What:** Two files, scp'd to the VPS (no pyinfra for MVP):
  1. `/etc/systemd/system/ops-loop-fixture.service` — clone of the existing agent service unit with:
     - `WorkingDirectory=/home/claude/depts/bubble-ops-fixture`
     - `ExecStartPre=+/bin/sh -c 'test -d /home/claude/depts/bubble-ops-fixture || (mkdir -p /home/claude/depts && chown claude:claude /home/claude/depts && sudo -u claude git clone https://x-access-token:$(grep ^GITHUB_TOKEN /run/claude-agent/env | cut -d= -f2-)@github.com/Bubble-invest/bubble-ops-fixture /home/claude/depts/bubble-ops-fixture)'`
     - `ExecStart=/bin/sh -c '/usr/bin/script -qfc "/home/claude/scripts/loop-autostart.sh" /dev/null'`
     - `Restart=always`, `RestartSec=5`
     - `EnvironmentFile=-/run/claude-agent/env`
  2. `/home/claude/scripts/loop-autostart.sh`:
     ```bash
     #!/usr/bin/env bash
     set -euo pipefail
     cd /home/claude/depts/bubble-ops-fixture
     git pull --quiet || true
     exec /usr/bin/tmux new-session -A -s ops-loop-fixture \
       "/usr/bin/claude --resume --dangerously-skip-permissions -p \
        '/loop 20m  read dept.yaml; scan queues/management/ then queues/research/; dispatch to the right layer subagent per dept.yaml.cadence; on each layer write produce summary.md + artifacts/.gitkeep + logs.jsonl + .last-run; layer 4 also writes risk-kpis.yaml + ../management-export.yaml; commit + push.'"
     ```
- **Why:** wires the proven agent service pattern (PATH-for-bun, SOPS env, pty wrap) to the new loop. The /loop prompt explicitly mentions the 4-file output schema + Layer 4's 3-output split so it can't drift.
- **Acceptance:**
  - `systemctl status ops-loop-fixture` → active (running) within 30 s of `systemctl start`.
  - `tmux ls` shows `ops-loop-fixture`.
  - `systemctl kill ops-loop-fixture` → auto-restart within 5 s. Repeat 3×.
- **Effort:** 1.5 h • **Risk:** Med (tmux-inside-systemd; mitigation = pty wrap proven on VPS).
- **Fallback if Step 4 said /loop is broken:** replace `claude --resume + /loop 20m ...` with `while true; do claude -p "$(cat .claude/loop-prompt.md)"; sleep 1200; done` wrapper.
- **Blocked by:** Step 6.

### Step 8 — Round-trip test: queue → layer 2 → gate → commit (1 h)

- **What:** From Mac in cloned fixture repo:
  ```bash
  cat > queues/research/test-001.yaml <<EOF
  id: research-test-001
  kind: research
  source_layer: 1
  target_layer: 2
  priority: high
  created_at: $(date -u +%FT%TZ)
  payload:
    question: "write 'hello from layer 2 on $(date -u +%F)' to a file"
  EOF
  git add queues/ && git commit -m "test: first round-trip" && git push
  ```
  Watch via: `ssh hetzner "tmux capture-pane -t ops-loop-fixture -p | tail -40"` and `gh api repos/Bubble-invest/bubble-ops-fixture/commits --jq '.[0:5][] | {sha,message: .commit.message}'`.
- **Acceptance:** within ≤40 min (2 ticks worst-case):
  - `outputs/<date>/2/summary.md` exists on github
  - `outputs/<date>/2/artifacts/.gitkeep`, `logs.jsonl`, `.last-run` all present  *(the full 4-file schema)*
  - `queues/gates/<id>.yaml` exists, conforms to `gate-item.schema.yaml`
  - The input task is either moved out of `queues/research/` or marked consumed
- **Effort:** 1 h • **Blocked by:** Step 7.

### Step 9 — Layer-4 daily run produces all 3 outputs (1 h)  *(new vs v1)*

- **What:** Manually trigger a Layer 4 run by sending `/run-layer 4` into the tmux session (or wait for 22:00 UTC and verify natural fire). Layer 4 stub must produce ALL 3 outputs per Notion line 278:
  - `outputs/<date>/4/risk-brief.md` (qualitative, stub content OK)
  - `outputs/<date>/4/risk-kpis.yaml` (must validate vs `templates/schemas/` — KPI fields: nav_status/exec_status/risk_status/stale_runs)
  - `outputs/<date>/management-export.yaml` (must validate vs `management-export.schema.yaml` — fields: dept, date, status, top_kpis, needs_management_attention, links)
- **Why:** the v1 MVP missed this entirely. If Layer 4 doesn't write the 3 files day-1, the hierarchy contract is fictional.
- **Acceptance:** all 3 files present, all validate vs their schemas, `git log -1` shows the commit. Cite the file paths in the run report.
- **Effort:** 1 h • **Blocked by:** Step 8.

### Step 10 — Wire Telegram gate notification (45 min)

- **What:** In `layers/2/PROMPT.md`, after "create gate", add: "run `/skill telegram-reporter` with the gate path + summary."
- **Why:** acceptance criterion #6.
- **Acceptance:** {{OPERATOR}}'s phone pings when Step 8's round-trip creates a gate.
- **Effort:** 45 min • **Blocked by:** Step 9.

### Step 11 — Robustness sweep + non-negotiables observability check (2 h)  *(expanded vs v1)*

- **What:** Run all 3 v1 robustness checks PLUS verify all 6 Notion non-negotiables are observable in the running fixture:
  1. `systemctl kill ops-loop-fixture` → auto-restart in ≤5 s. Repeat 3×.
  2. `sudo reboot` the VPS → after boot, unit active + tmux session reachable within 60 s.
  3. Push malformed `dept.yaml` → loop writes `outputs/<date>/error.log`, does NOT exit; revert recovers.
  4. **Non-negotiables observable:**
     - `gh api repos/Bubble-invest/bubble-ops-fixture/contents/dept.yaml --jq .content | base64 -d | grep optional_domain_ledger` → present
     - `gh api .../contents/tools` returns 200; `.../contents/skills` returns 200
     - `gh api .../contents/tests/run.sh` returns 200; running it locally exits 0
     - `gh api .../contents/queues/management` returns 200 (with `.gitkeep`)
     - Today's `outputs/<date>/4/` contains all 3 files (brief + kpis + ../mgmt-export)
     - `dept.yaml` shows full `hierarchy:` block under jq inspection
- **Effort:** 2 h • **Risk:** Med • **Blocked by:** Step 10.

### Step 12 — Document + update strategy log (30 min)

- **What:** Write `MVP-COMPLETE.md` capturing: what shipped, the 6 non-negotiables verified, what we learned about `/loop` headless, the next 3 unknowns for Phase 1. Update `.claude/strategy-log.md`: archive MVP, surface Phase 1 (Cloud Routines) as new top priority.
- **Effort:** 30 min • **Blocked by:** Step 11.

### Effort totals

| Step | Hours |
|---|---|
| 0 Pin contracts (schemas) | 1.5 |
| 1 Lock dept.yaml | 1.0 |
| 2 Create repo | 0.25 |
| 3 Widen token | 0.25 |
| 4 /loop smoke test | 1.0 |
| 5 Full skeleton commit | 2.0 |
| 6 Subagent perms | 1.0 |
| 7 systemd + autostart | 1.5 |
| 8 Round-trip | 1.0 |
| 9 Layer 4 triple output | 1.0 |
| 10 Telegram gate | 0.75 |
| 11 Robustness + non-neg check | 2.0 |
| 12 Docs + strategy | 0.5 |
| **Total** | **~13.75 h ≈ 2–3 working days** |

Buffer: 1 day for `/loop` surprises in Step 4. Realistic ship: **Day 3** (target), **Day 5** (worst).

---

## 4. Risks specific to MVP v2

| # | Risk | P | I | Mitigation |
|---|---|---|---|---|
| R1 | `/loop` does not wake reliably in headless tmux on the VPS | Med | **Critical** | Step 4 smoke test BEFORE wiring systemd; bash-while-loop fallback documented in Step 7 |
| R2 | `GITHUB_TOKEN` scope wrong → push fails silently | Med | High | Step 3 verifies push before Step 7; loop prompt writes errors to `outputs/<date>/error.log` |
| R3 | tmux dies but systemd reports active (semantic gap) | Low | Med | `tmux new-session -A` idempotent; heartbeat checks `tmux ls`; reuse `phone-home.sh` to alert |
| R4 | Subagent perm allowlist syntax wrong → leak | Med | High | Step 6 includes 4 negative tests; cite https://code.claude.com/docs/en/sub-agents §Supported frontmatter |
| R5 | Layer 4 stub forgets one of the 3 outputs | **Med** | **High** | **Step 9 dedicated checklist (3 files must exist); schema-validate each; failing test = block Step 11** |
| R6 | Schemas under `templates/schemas/` drift from instances | Low | High | `tests/run.sh` validates every dept.yaml / queue-item / gate-item / mgmt-export / directive against schema; CI step in tests/run.sh |
| R7 | Round-trip latency > 40 min | Low | Med | Drop to `/loop 5m` for MVP demo; revert post-MVP |
| R8 | Cost overrun (subagents on tight loop) | Med | Med | Stub prompts ≤500 tokens; demote to Sonnet if needed via subagent frontmatter |
| R9 | Tests directory becomes theatre (echo PASS) and provides no real signal | Med | Med | Acceptable for MVP per the brief; flag in `MVP-COMPLETE.md` as "real tests = first task per dept in Phase 1+" |
| R10 | `optional_domain_ledger: null` slot tempts someone to set it for the fixture | Low | Low | Comment in `dept.yaml` explicitly: "fixture is filesystem-only; set non-null only when a dept (e.g. Ben) has transactional truth to keep" |

---

## 5. First commit to land in `bubble-ops-fixture` — exact tree

```
bubble-ops-fixture/
├── README.md
├── .gitignore
├── dept.yaml                                 # See §5.2 below
├── templates/
│   └── schemas/
│       ├── dept.schema.yaml
│       ├── queue-item.schema.yaml
│       ├── gate-item.schema.yaml
│       ├── management-export.schema.yaml
│       └── directive.schema.yaml
├── .claude/
│   ├── settings.json                         # model, mcpServers, allowed-tools
│   └── agents/
│       ├── data-curator.md                   # Read+WebFetch+Bash(git/ls/cat); write only layers/1, outputs/*/1
│       ├── task-orchestrator.md              # Read+Write+Bash(git)+Agent; write outputs/*/2, queues/gates
│       ├── executor.md                       # Read+Write+Bash(git); disallowed WebFetch/WebSearch; write outputs/*/3, inbox/*/processed
│       └── mandate-guardian.md               # Read+Grep+Glob+WebSearch; write ONLY outputs/*/4, outputs/*/management-export.yaml, queues/improvements
├── layers/
│   ├── 1/PROMPT.md                           # Daily 06:00. Output: outputs/<date>/1/{summary.md,artifacts/.gitkeep,logs.jsonl,.last-run}
│   ├── 2/PROMPT.md                           # Every 20 min. Output: same 4-file schema; creates queues/gates/<id>.yaml
│   ├── 3/PROMPT.md                           # Every 20 min, gated by inbox/decisions/. Same 4-file schema.
│   └── 4/PROMPT.md                           # Daily 22:00. Output: 4-file schema + risk-kpis.yaml + ../management-export.yaml
├── subagents/                                # empty for MVP (subagents live in .claude/agents/ for project scope)
│   └── .gitkeep
├── skills/
│   └── echo-skill/
│       └── SKILL.md                          # Agentic stub: calls echo-tool, writes brief
├── tools/
│   └── echo-tool/
│       ├── tool.py                           # Deterministic: returns {"echo": input, "ts": now}
│       ├── schema.json                       # JSON schema for input/output
│       └── README.md
├── tests/
│   ├── run.sh                                # Runs all 4 levels; exits 0 = pass
│   └── fixtures/
│       ├── tool/echo-input.json              # {"msg": "hello"}
│       ├── skill/echo-context.yaml           # mandate + mocked tool reply
│       ├── layer/queue-item.yaml             # Valid queue-item fixture per schema
│       └── department/dry-run-input.yaml     # End-to-end dry-run setup
├── queues/
│   ├── research/.gitkeep
│   ├── gates/.gitkeep
│   ├── management/.gitkeep                   # CEO directives land here (empty for MVP)
│   └── improvements/.gitkeep
├── inbox/
│   └── decisions/.gitkeep
└── outputs/
    └── .gitkeep                              # Date subdirs created at runtime
```

### 5.2 `dept.yaml` (exact content for first commit)

```yaml
# bubble-ops-fixture/dept.yaml
# Validates against templates/schemas/dept.schema.yaml v1
schema_version: 1
dept_name: fixture
mandate: |
  Trivial round-trip target for the bubble-ops-loop MVP.
  Reads queue items, writes outputs (4-file schema per layer), creates gates,
  runs Layer 4 daily with 3-output split (risk-brief + risk-kpis + management-export),
  commits, pushes. No real-world side effects.

owner:
  github: Bubble-invest
  telegram_user_id: "{{OPERATOR_CHAT_ID}}"

# Notion §"Hiérarchie & visibilité cross-dept" — present from day 1 even for a leaf ops dept
hierarchy:
  level: ops                       # ops | management | principal
  parent: null                     # null for fixture; will be "tony" for real ops depts
  children: []                     # ops depts have no children
  visibility:
    read_outputs: []               # fixture reads nobody else
    read_risk_kpis: false
    read_risk_briefs: false
    read_raw_artifacts: false
    read_secrets: false
  directive_policy:
    can_open_priority_prs: false   # ops depts CANNOT push directives; only management can
    target_queue: null
    requires_human_gate_for: []

# Notion §"Doctrine storage" — slot is present, value null. Set to a dict only when
# the dept's domain genuinely needs transactional truth (Ben/family office case).
optional_domain_ledger: null

subscribed_layers: [1, 2, 3, 4]

cadence:
  layer_1_data: "0 6 * * *"        # daily 06:00 UTC
  layer_2_research: "*/20 * * * *" # every 20 min (driven by /loop)
  layer_3_exec: "*/20 * * * *"
  layer_4_risk: "0 22 * * *"       # daily 22:00 UTC

gate_policy:
  notify_telegram: true
  telegram_chat_id: "auto"         # MVP: use the rnd channel
  digest_minutes: 0                # MVP: immediate ping per gate

loop_engine:
  cadence_minutes: 20
  max_iterations_per_day: 72
  on_error: log_and_continue       # vs halt

outputs_schema:
  required_files: [summary.md, logs.jsonl, .last-run]
  required_dirs: [artifacts]       # may contain only .gitkeep
  layer_4_extras:
    - risk-brief.md
    - risk-kpis.yaml
  layer_4_root_extras:
    - management-export.yaml       # written at outputs/<date>/, not outputs/<date>/4/
```

---

## 6. What to verify after MVP "done" before declaring victory

**24 h soak before claiming Phase 1 readiness.** Each check is a hard gate.

1. **Continuous run:** 24 h `systemctl status ops-loop-fixture` always active; `journalctl -u ops-loop-fixture --since "24h ago" | grep -iE 'error|fail|crash' | wc -l` < 5.
2. **Steady-state cadence:** ≥60 `git push` commits over 24 h (~72 ticks × ≥1 commit/tick). Fewer → loop stalling.
3. **Gate UX:** {{OPERATOR}} approves 3 gates from phone via Telegram or by committing `inbox/decisions/<id>.yaml` from a phone-friendly path. Each consumed in ≤2 ticks.
4. **Cost:** total Anthropic tokens ≤ $5 over the 24 h. >$5 → demote to Haiku/Sonnet via subagent frontmatter.
5. **Memory:** `ps aux | grep claude` stays bounded over 24 h.
6. **No regression:** existing agent services still active; telegram-rnd channel still responsive.
7. **All 6 Notion non-negotiables observable in running fixture:**
   - `dept.yaml.hierarchy` present and non-empty (jq)
   - `dept.yaml.optional_domain_ledger` field exists (even if null)
   - `tools/` and `skills/` both contain ≥1 stub
   - `tests/run.sh` exits 0 against latest HEAD
   - `queues/management/` exists (with .gitkeep)
   - Today's `outputs/<date>/4/risk-brief.md` + `risk-kpis.yaml` + `outputs/<date>/management-export.yaml` ALL present and schema-valid
8. **Schema discipline:** every YAML in `queues/`, `inbox/`, `outputs/<date>/*/management-export.yaml`, `outputs/<date>/4/risk-kpis.yaml` validates against its schema. Run `tests/run.sh` against HEAD daily.

If any of 1–8 fails: single-line entry in `monitoring/BACKLOG.md`, do NOT advance to Phase 1.

---

## 7. Go / no-go for Phase 1 (Cloud Routines)

| Signal observed in MVP | Decision |
|---|---|
| `/loop` headless behaves; 24 h soak passes 1–8 | **STRONG GO** — Phase 1 Day 4. Cloud Routines = safety net per ROADMAP §2.3 |
| `/loop` flaky (bash-while-loop fallback in Step 4) | **YELLOW** — Phase 1 proceeds but Cloud Routines become MAIN engine; tmux+/loop demoted to "best-effort". Re-architect ROADMAP §2.3 |
| Round-trip latency consistently > 40 min | **YELLOW** — Phase 1 proceeds with `/loop 5m` (4× more ticks). Reassess cost D+3 |
| Cost > $5/day on stub fixture | **NO-GO** until cost-model understood. Risk: $50–500/day at 5 depts × real prompts |
| Subagent perms leaked (executor can WebFetch despite disallow) | **NO-GO** hard stop. Re-verify isolation; fresh fixture |
| Schemas drift (jsonschema validation fails on any output) | **NO-GO** — schema is the load-bearing contract. Fix before any 2nd dept |
| Layer 4 fails to write any of 3 outputs reliably (24h) | **NO-GO** for adding Tony in Phase 4 (hierarchy contract = fictional) |
| systemd survives reboot but tmux doesn't reattach | **YELLOW** — fix `tmux new-session -A` idempotence before Phase 1 |
| All 12 steps shipped + 24 h soak clean + all 6 non-neg observable | **STRONG GO** — Phase 1 starts Day 4, Maya unlocked Day 10 per v1 ROADMAP |

**Accelerators discovered during MVP** that compress Phase 1:
- `cloud-wiki-sync.sh` pattern transfers cleanly to fixture-sync → Cloud Routine `_dept_scan.py` half-written
- Existing `phone-home.sh` heartbeat fits `outputs/<date>/<layer>/.last-run` → no new monitoring
- `tests/run.sh` harness gives Phase 1 routines a free CI gate per dept

**Blockers discovered during MVP** that defer Phase 1:
- If `GITHUB_TOKEN` must split per-dept for security → per-dept secret rotation flow before Phase 1
- If Telegram gate spam needs digest at 1 dept → digest_minutes >0 support before adding depts
- If schema drift surfaces frequently → CI on every push to fixture repo before Phase 1
- If Layer 4 management-export.yaml proves consistently incomplete → Tony migration (Phase 4) deferred until Layer 4 contract is tight

---

*Authored 2026-05-20 by Rick (R&D). v2 supersedes v1 (backed up at `MVP-ROADMAP.bak-v1-incomplete-20260520.md`). Grounded in the Notion final architecture (last-edited 2026-05-20T14:01 UTC) + commands re-run against `hetzner` ssh host + Mac dev box. The 6 Notion non-negotiables are encoded in commit 1 of the fixture, not deferred. Companion to `ROADMAP.md` (v1, 444 lines — do not duplicate). Next review: end of Step 4 (the `/loop` smoke test).*
