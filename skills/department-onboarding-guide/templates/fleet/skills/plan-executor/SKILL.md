---
name: plan-executor
description: How and when to dispatch the fleet's plan-executor subagent — a worker pinned to Claude Opus 4.6, which follows an explicit, numbered, step-by-step plan exceptionally faithfully (it executes exactly what's written and rarely improvises). USE THIS SKILL whenever Joris asks you to "use a 4.6 subagent" / "spawn a 4.6" / "have a 4.6 do this", whenever you already have a detailed numbered plan that must be carried out verbatim without re-scoping, or whenever faithful execution of an explicit plan matters more than initiative or discovery. It covers when to pick 4.6 over a normal 4.8 subagent, how to write a plan it will follow well, and the exact dispatch call. Reach for it even if the words "plan-executor" aren't said — the trigger is "run a specific model / 4.6 subagent" or "do these steps exactly".
---

# Plan Executor — dispatching the fleet's 4.6 step-by-step worker

The **plan-executor** is a subagent available on every machine in the fleet. It is pinned to **Claude Opus 4.6**, which is unusually good at following an explicit, numbered plan *exactly* — in order, verifying each step, without re-scoping or getting creative. You (a dept manager: Rick, Tony, Ben, Maya, Miranda, Ellie, …) dispatch it when you want a task done **precisely as specified**, not reinterpreted.

Joris may ask for this directly — "use a 4.6 subagent for this," "have a 4.6 run these steps." When he does, **this is the tool**: dispatch `plan-executor`. You don't need him to name it; any request for tight, literal, step-by-step execution is the cue.

## When to use it (and when NOT to)

**Use plan-executor when:**
- Joris explicitly asks for a 4.6 subagent.
- You have a concrete, ordered plan (numbered steps naming exact files/commands/expected results) and you want it executed **verbatim**.
- Faithfulness beats initiative — e.g. a delicate multi-step migration, a fixed runbook, a repro sequence, applying a reviewed patch step by step, a checklist that must not be improvised around.

**Do NOT use it for** (reach for a normal Opus 4.8 subagent instead — 4.8 is more environment-aware):
- Open-ended research or "figure out how to…" work.
- Design/architecture or judgement calls the plan leaves open.
- Anything where the best outcome requires departing from the plan or exercising discretion.

Rule of thumb: **4.8 to decide *what* the plan is; 4.6 to execute the plan faithfully.**

## How to dispatch it

Use the Agent (or Task) tool with `subagent_type: "plan-executor"`. The prompt you pass **is the plan** — write it as explicit numbered steps.

```
Agent(
  subagent_type: "plan-executor",
  description: "<3–5 word task>",
  prompt: """
  Execute this plan exactly, step by step, verifying each:

  Step 1: <precise action — exact file/path/command, and what to change>
  Step 2: <…>
  Step 3: <verification: what to check and the expected value>
  ...
  Report a numbered ledger (what you did + evidence per step), then DONE or STOPPED-AT-STEP-N.
  """
)
```

You do **not** pass a `model` override — the 4.6 pin lives in the subagent's own definition (the spawn-time `model` param only accepts family aliases like `opus`/`sonnet`, so it can't select a version; the definition is what pins 4.6). Just dispatching `plan-executor` gets you 4.6.

## How to write a plan it will follow well

The plan-executor is faithful, which means a vague plan produces a faithful-but-useless result. Give it precision:

1. **Number the steps** and keep each one atomic (one action per step).
2. **Name exact targets** — file paths, line ranges, command strings, the precise edit. Don't say "update the config"; say "in `config.yaml`, set `timeout: 30`".
3. **Make each step verifiable** — say what "done" looks like (file contains X, command exits 0, endpoint returns Y). The executor checks each step against this.
4. **State scope boundaries** — what it must NOT touch. It won't scope-creep, but tell it the edges anyway.
5. **Say what to do on trouble** — by default it HALTS and reports on any ambiguity/failure rather than guessing. That's usually what you want; if a step is genuinely optional, mark it so.

## What you get back

A step-by-step ledger mirroring your plan: per step, what it did + the verification evidence, and a final `DONE` or `STOPPED-AT-STEP-N` with any issues it flagged (things it noticed but correctly did not act on). Read the ledger to confirm each step actually landed — then take it from there (review, merge, next stage).

## Notes

- **Availability:** `plan-executor` is deployed fleet-wide (Mac, VPS depts, M5, M1) — every manager can dispatch it. If a `subagent_type 'plan-executor' not found` error appears, the agent's session predates the deploy; it picks up on the next session/restart.
- **Model:** pinned to `claude-opus-4-6` in the definition at `~/.claude/agents/plan-executor.md`. Managers themselves stay on 4.8 (environment-aware); 4.6 is the on-demand step-follower.
- Keep the maker≠checker discipline: plan-executor is a *maker*. If its output is a code change, still run a separate reviewer before anything merges.
