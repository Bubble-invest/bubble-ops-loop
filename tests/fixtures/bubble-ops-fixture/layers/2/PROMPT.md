---
name: layer-2-prompt
description: Layer 2 (Research / Plan Execution) prompt for bubble-ops-fixture. Consumes queue items, spawns task subagents, produces gates for humans or improvements.
---

# Layer 2 — Research / Plan Execution

> Notion v4 §"Layer 2 — Research / Plan Execution". Owned by the
> `task-orchestrator` subagent (see `subagents/task-orchestrator.md`).
> Default cadence: every 20 min, driven by `/loop`.

## Mission

Read the day's queue items from `queues/research/`. For each, spawn a
dedicated sub-task subagent that runs the relevant skill (per
`dept.yaml::skills.layer_2`) and either:

- emits a **gate** (`queues/gates/<id>.yaml`) when human validation is
  required, OR
- files an **improvement proposal** (`queues/improvements/<id>.yaml`)
  when no human decision is needed and the work can land directly in
  tomorrow's Layer 1 input set.

## Inputs

- `queues/research/*.yaml` — queue items materialized by Layer 1
- `outputs/<today>/1/summary.md` — context: what Layer 1 said about the day
- `dept.yaml::gate_policies` — for each queue item kind, look up which gate
  policy applies (`echo_action`, `mandate_breach_escalation`, etc.)

## Process

For each item in `queues/research/`:

1. Read the item; validate against `queue-item.schema.yaml` (operator-side).
2. **Spawn `task-orchestrator` subagent per item** with a focused prompt
   (one item, one skill, one decision-or-proposal).
3. The sub-subagent runs the skill (for the fixture: `echo-skill`).
4. Skill output → write to `outputs/<today>/2/research/<item-id>.md`.
5. Decide: gate or improvement?
   - **If `dept.yaml::gate_policies.<kind>.current_mode == manual_required`**
     → emit `queues/gates/<item-id>.yaml` (validates against
     `gate-item.schema.yaml`) + run `/skill telegram-reporter` to ping
     Joris with the gate path + summary.
   - **Else** → emit `queues/improvements/<item-id>.yaml` for Layer 1
     tomorrow.
6. Once an item is consumed, move it out of `queues/research/` (delete
   or rename with `.consumed` suffix).

## Outputs (the 4-file schema — per Notion v4 §"Output layer")

Write ALL FOUR files under `outputs/<today>/2/`:

| File | Content |
|---|---|
| `outputs/<today>/2/summary.md` | Narrative: items processed, gates created, improvements filed |
| `outputs/<today>/2/artifacts/.gitkeep` | Placeholder (per-item research briefs land here as `<id>.md`) |
| `outputs/<today>/2/logs.jsonl` | One JSON line per item processed: `{ts, item_id, decision, target_file}` |
| `outputs/<today>/2/.last-run` | ISO timestamp of this tick's completion |

Plus gates: `queues/gates/<item-id>.yaml` (one per gating decision).

## Side-effects

- `git add outputs/<today>/2/ queues/gates/ queues/improvements/ queues/research/`
- Commit message: `layer-2: N items processed, M gates, K improvements`.
- Push.
- Telegram ping per gate (via the `telegram-reporter` MCP skill).

## Perm policy (recap)

The `task-orchestrator` has `Agent` in its toolset (it spawns sub-task
subagents) and `permissionMode: acceptEdits`. Write paths must stay within
`outputs/*/2/` + `queues/gates/` + `queues/improvements/` + the cleanup of
consumed items in `queues/research/`.
