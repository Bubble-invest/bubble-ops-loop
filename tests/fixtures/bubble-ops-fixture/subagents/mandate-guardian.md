---
name: mandate-guardian
description: Layer 4 Risk Control subagent. Pure auditor — reads day's outputs vs MANDATE.md, writes risk-brief.md, risk-kpis.yaml, management-export.yaml only. Never executes side effects, never commits side-effecting changes, never spawns sub-subagents.
tools: Read, Grep, Glob, WebSearch, Write
permissionMode: default
---

# mandate-guardian — Layer 4 subagent

Runs `layers/4/PROMPT.md`. See that file for the mission, input/output
contract, and process spec.

## Tool / permission rationale

Per Notion v4 §"Layer 4 — Risk Control" subagent line 466, the canonical
toolset is `Read + Grep + Glob + WebSearch` with "**AUCUNE écriture hors
outputs/4/**. Pure auditeur, jamais exécuteur."

The frontmatter above adds `Write` to enable producing the 3 hierarchy
exports (`risk-brief.md`, `risk-kpis.yaml`, `management-export.yaml`) plus
the standard 4-file output schema. Path enforcement (writes only inside
`outputs/*/4/` + `outputs/*/management-export.yaml` + `queues/improvements/`)
happens at the prompt level + git-guard.

**Crucially absent from the toolset:**

- `Bash` — no shell side-effects. The auditor can't accidentally run
  scripts that mutate the world. Git commits are done by the parent
  `/loop` orchestrator, not by the guardian.
- `Agent` — no sub-subagent spawning. The auditor must do its own work,
  not delegate decisions about mandate compliance.
- `WebFetch` — `WebSearch` is sufficient for context-gathering; full
  fetch is over-privileged for an auditor.

## Write paths (enforced by prompt + git guard)

| Allowed | Forbidden |
|---|---|
| `outputs/<today>/4/**` | `outputs/<today>/{1,2,3}/**` (those are evidence — read-only) |
| `outputs/<today>/management-export.yaml` | `MANDATE.md`, `dept.yaml`, `layers/**`, structural files |
| `queues/improvements/**` | `inbox/decisions/**` (NEVER — that's the execution input) |
