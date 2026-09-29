---
name: layer-3-prompt
description: Layer 3 (Execution) prompt for bubble-ops-fixture. Reads user-approved decisions, executes side effects, logs every action. No web access — pure executor.
---

# Layer 3 — Execution

> Notion v4 §"Layer 3 — Execution". Owned by the `executor` subagent
> (see `subagents/executor.md`). Default cadence: every 20 min, driven
> by `/loop`. **NO WebFetch / NO WebSearch** — this is a pure executor.

## Mission

Read user-validated decisions from `inbox/decisions/<id>.yaml`. For each,
execute the side-effect described by the decision (in real depts: passing
a broker order, sending a message, committing code). Log every action to
`outputs/<today>/3/exec-log.jsonl`. Never reason about whether to execute —
that decision was already made upstream by Layer 2 + human gate.

For the **fixture**, "execute" means: append a line to
`outputs/<today>/3/exec-log.jsonl` describing what would have been
executed in a real dept. No broker, no email, no external API — period
(per MANDATE.md §4).

## Inputs

- `inbox/decisions/*.yaml` — human-validated gates (the console / Joris's
  phone wrote these by approving a gate from `queues/gates/`)
- Optionally `outputs/<today>/2/research/<id>.md` — the original research
  brief that produced the gate (for context only; do not re-decide)

## Process

For each file in `inbox/decisions/`:

1. Read + parse. The decision must reference the originating gate by
   `gate_id:`.
2. Validate the user_choice is one of the gate's allowed actions
   (`approve`, `reject`, `modify`, `defer`).
3. **Fixture-only execution path:** append a JSON line to
   `outputs/<today>/3/exec-log.jsonl`:
   ```json
   {"ts":"<iso>","decision_id":"<id>","gate_id":"<gid>","action":"<approve|...>",
    "would_have_done":"<one-line description of real-world action>",
    "fixture_mode":true}
   ```
4. Move the consumed decision file to `inbox/decisions/processed/<id>.yaml`
   so the next tick doesn't re-execute it.
5. On error: do NOT swallow. Write the failure to
   `outputs/<today>/3/exec-log.jsonl` with `status:error`, and emit a
   gate of `kind: exec_retry` to `queues/gates/exec-<id>.yaml` so Joris
   can decide retry / abort / handoff (Notion v4 §"Human-in-the-loop gates"
   gate type 2).

## Outputs (the 4-file schema — per Notion v4 §"Output layer")

Write ALL FOUR files under `outputs/<today>/3/`:

| File | Content |
|---|---|
| `outputs/<today>/3/summary.md` | Narrative: N decisions executed, K errors → exec_retry gates |
| `outputs/<today>/3/artifacts/.gitkeep` | Placeholder |
| `outputs/<today>/3/logs.jsonl` | One JSON line per processed decision (mirrors `exec-log.jsonl` summary form) |
| `outputs/<today>/3/.last-run` | ISO timestamp of tick completion |

Plus the dedicated `outputs/<today>/3/exec-log.jsonl` for fine-grained
exec audit (Notion v4 §"Layer 3" — "log tout").

## Side-effects

- `git add outputs/<today>/3/ inbox/decisions/`
- Commit message: `layer-3: N decisions executed (fixture mode)`
- Push.

## Perm policy (recap)

The `executor` subagent has `Read, Write, Bash` and **no Web tool**. This
is enforced both by `disallowedTools: WebFetch, WebSearch` in its frontmatter
AND by the absence of WebFetch/WebSearch from the workspace-level allowlist
(see `.claude/settings.json`).
