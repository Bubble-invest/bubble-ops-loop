# CLAUDE.md — bubble-ops-fixture workspace operating doc (MVP)

> Workspace for `bubble-ops-fixture` dept (MVP). This file is read at every
> session boot by the agent running `/loop` inside the `ops-loop-fixture`
> tmux session on Morty VPS.

## Mission

Prove the bubble-ops-loop pattern end-to-end with a deliberately-trivial
fixture department. See `MANDATE.md` for the binding contract.

## Session boot protocol

At every session start (e.g. systemd respawn, manual `claude --resume`):

1. **Read `MANDATE.md` + `dept.yaml`** — the two load-bearing docs.
2. **List `recurring_missions:` from dept.yaml** — for the fixture, just
   one: `echo_heartbeat` cadence `every_2h` (Notion §"Gates levables &
   missions récurrentes" pattern).
3. **Self-prescribe the `/loop`** via the agent's CronCreate-equivalent:
   ```text
   /loop 20m  read dept.yaml ; check queues/management/, queues/research/,
              inbox/decisions/ ; if any items, dispatch to the appropriate
              layer subagent per the layer prompts at layers/N/PROMPT.md ;
              commit + push to bubble-ops-fixture repo using the
              bubble-token-broker. If nothing pending, write a tick to
              outputs/<date>/heartbeat.log.
   ```
4. **Report self-init success on Telegram** (`telegram-rnd` channel).

## File layout overview

| Path | Purpose |
|---|---|
| `MANDATE.md` | Load-bearing contract Layer 4 audits against |
| `dept.yaml` | Identity + hierarchy + recurring missions + gate policies |
| `layers/{1..4}/PROMPT.md` | The 4 OODA layer prompts (Data/Research/Exec/Risk) |
| `subagents/*.md` | 4 per-dept subagent personas with isolated tools/perms |
| `skills/echo-skill/SKILL.md` | Agentic procedure stub (decides; calls tools) |
| `tools/echo-tool/` | Deterministic Python tool (calculates; no decisions) |
| `tests/run.sh` | Local harness: tool→skill→layer→department, ≥1 test per level |
| `queues/research/` | Layer 1 → Layer 2 work items |
| `queues/gates/` | Layer 2 → human decisions awaiting validation |
| `queues/management/` | CEO/Tony directives (PRs land here, empty for fixture) |
| `queues/improvements/` | Layer 4 → Layer 1 tomorrow (improvement proposals) |
| `inbox/decisions/` | Human-validated decisions → Layer 3 reads these |
| `outputs/<YYYY-MM-DD>/{1,2,3,4}/` | Per-layer daily outputs (4-file schema) |
| `.claude/settings.json` | Workspace allow/deny path policy |

## Output schema (per Notion v4 §"Contrats minimaux à standardiser")

Every layer writes EXACTLY 4 files under `outputs/<date>/<layer>/`:

```
outputs/<date>/<layer>/
├── summary.md       # human-readable narrative
├── artifacts/       # any layer-specific artifacts (may be .gitkeep only)
├── logs.jsonl       # structured log of every action taken
└── .last-run        # ISO timestamp of last successful run (heartbeat)
```

**Layer 4 also writes 3 hierarchy-export files** (Notion §"Layer 4 — Risk
Control" + §"Hiérarchie & visibilité cross-dept"):

```
outputs/<date>/4/risk-brief.md          # qualitative risk audit
outputs/<date>/4/risk-kpis.yaml         # KPI snapshot (CEO-readable)
outputs/<date>/management-export.yaml   # compact summary (at outputs/<date>/, NOT inside 4/)
```

The third file lives at `outputs/<date>/management-export.yaml` (sibling to
`1/`, `2/`, `3/`, `4/` dirs), not inside `4/`, because it represents the
*department's* compact summary, not Layer 4's internal output.

## Subagent perm policy

See per-file frontmatter in `subagents/*.md`. Summary:

| Subagent | Layer | Tools | No-go |
|---|---|---|---|
| `data-curator` | 1 | Read, WebFetch, Bash, Write* | path-policy: outputs/*/1/ + queues/research/ |
| `task-orchestrator` | 2 | Read, Write, Bash, Agent | spawns sub-subagents |
| `executor` | 3 | Read, Write, Bash | **NO WebFetch / WebSearch** (pure executor) |
| `mandate-guardian` | 4 | Read, Grep, Glob, WebSearch, Write | **NO Bash, NO Agent** (pure auditor) |

(*Write is allowed at tool level for `data-curator` because Claude Code's
subagent perm scoping is binary — path enforcement happens at prompt level +
git-guard wrapper per Notion v4 line 725 "GitHub ne fournit pas un vrai
path-scope au niveau token contents:write".)

## Schemas (external — not bundled)

The 6 contract schemas (`dept`, `recurring-mission`, `queue-item`, `gate-item`,
`management-export`, `directive`) live OUTSIDE this repo at:

```
~/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/schemas-draft/
```

This fixture references them for validation context but does not bundle them —
they are the operator-side contract authority. Future depts (Maya, Tony, Ben)
will reference the same path.
