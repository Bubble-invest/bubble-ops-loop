---
name: echo-skill
description: Agentic stub skill that decides whether/when to call echo-tool and what to do with the result. Demonstrates the skill-vs-tool boundary per Notion v4 §"Skill vs tool". Used by all 4 layers in the fixture (real depts declare distinct per-layer skills).
---

# echo-skill — fixture agentic procedure

## Purpose

Demonstrate the **skill vs tool** boundary that Notion v4 calls out as the
load-bearing distinction for the bubble-ops-loop framework:

> Tool = fonction déterministe ... récupère, calcule, normalise. **Ne raisonne pas.**
> Skill = procédure agentique. **Décide quoi faire**, dans quel ordre, avec
> quels tools, puis produit un output standardisé pour le layer.
> — Notion v4 §"Skill vs tool"

The fixture re-uses `echo-skill` across all 4 layers (per
`dept.yaml::skills.{layer_1..layer_4}`) to keep the MVP commit small.
Real depts (e.g. Maya) declare distinct per-layer skills like
`prospect-researcher`, `dm-drafter`, `message-sender`, etc.

## Inputs

- A queue item or layer prompt (the context)
- The dept's MANDATE.md (for policy alignment)
- Access to `echo-tool` via Bash (`python3 tools/echo-tool/tool.py < input.json`)

## Process (agentic — decides at each step)

1. Read the layer context to determine the appropriate `message` payload.
2. **Decide** whether to call `echo-tool` at all (e.g. a Layer 4 audit pass
   might not need to echo anything).
3. If yes: call `echo-tool` with a JSON input matching `tools/echo-tool/schema.json`.
4. **Decide** what to do with the result — write a brief to `outputs/<today>/<layer>/summary.md`,
   file a queue item, or just log it.

## Outputs

- A line in `outputs/<today>/<layer>/logs.jsonl` for every tool call.
- A markdown brief in `outputs/<today>/<layer>/summary.md` summarizing the
  skill's decisions and tool calls for the tick.

## Why this is a skill, not a tool

The decision logic at step 2 ("should I call the tool?") and step 4 ("what
do I do with the result?") is where the agentic reasoning happens. Move
that logic into Python and you collapse the skill into a tool — which
breaks the separation of concerns Notion v4 mandates.
