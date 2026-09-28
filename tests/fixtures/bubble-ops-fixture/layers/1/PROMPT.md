---
name: layer-1-prompt
description: Layer 1 (Data Update) prompt for bubble-ops-fixture. Refreshes external/internal state, reads recurring missions, materializes them into queue items.
---

# Layer 1 — Data Update

> Notion v4 §"Layer 1 — Data Update". Owned by the `data-curator` subagent
> (see `subagents/data-curator.md`). Default cadence: daily 06:00 UTC, but
> the `/loop` engine can also trigger sub-daily refreshes if recurring
> missions declare smaller cadences (e.g. fixture's `echo_heartbeat`
> at `every_2h`).

## Mission

Refresh data (external + internal — yesterday's outputs from layers 2/3/4),
read due recurring missions from `dept.yaml::recurring_missions`, read any
priority directives in `queues/management/`, then **materialize the day's
needs as standardized queue items** in `queues/research/`.

Recurring missions are not mini-apps; they are declarative needs that
become queue items consumed downstream by Layer 2.

## Inputs

- `dept.yaml` — read `recurring_missions:` and check which are due
  (cadence: `daily`, `every_2h`, `weekly`, etc.)
- `queues/management/*.yaml` — Tony's priority directives, read FIRST
  (Notion v4 line 213: "Layer 1 lit `queues/management/` avant de
  construire le plan du jour")
- `outputs/<yesterday>/{2,3,4}/` — what shipped yesterday
- `queues/improvements/*.yaml` — Layer 4's overnight improvement proposals
  (Notion v4 §"Layer 4" — items filed here are consumed by tomorrow's L1)

## Process

1. Compute `<today> = $(date -u +%F)` and ensure `outputs/<today>/1/`
   exists.
2. For each entry in `queues/management/` → emit a high-priority
   `queue-item` in `queues/research/` whose `payload.source: management`.
3. For each due recurring mission → emit a `queue-item` per its `creates:`
   list. Validate against `queue-item.schema.yaml` (operator-side at
   `Rick_RnD/projects/bubble-ops-loop/schemas-draft/`).
4. For the fixture, the only mission is `echo_heartbeat` — emit one
   `echo_task` queue item per fire.

## Outputs (the 4-file schema — per Notion v4 §"Output layer")

Write ALL FOUR files under `outputs/<today>/1/`:

| File | Content |
|---|---|
| `outputs/<today>/1/summary.md` | Narrative: what was refreshed, what queue items were created, any directives picked up |
| `outputs/<today>/1/artifacts/.gitkeep` | Placeholder dir (may carry raw feeds, fetched JSON, etc.) |
| `outputs/<today>/1/logs.jsonl` | One JSON line per action: `{ts, action, target, result}` |
| `outputs/<today>/1/.last-run` | ISO timestamp of this run's completion (heartbeat for monitoring) |

## Side-effects

- `git add outputs/<today>/1/ queues/research/` and commit via the token
  broker (Morty `bubble-token-broker`). Commit message:
  `layer-1: refresh + N queue items materialized`.
- Push to `vdk888/bubble-ops-fixture` (workspace token scoped to this repo).

## Perm policy (recap)

The `data-curator` subagent runs this. It has Write at the tool level but
the prompt enforces path-policy: **writes ONLY inside `outputs/*/1/` or
`queues/research/`**. A git pre-commit guard (Step 3c, separate work)
will harden this at commit time.
