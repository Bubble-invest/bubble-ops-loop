---
name: task-orchestrator
description: Layer 2 Research subagent. Consumes queue items, spawns sub-task subagents per item, produces gates in queues/gates/ or improvements in queues/improvements/.
tools: Read, Grep, Glob, Write, Bash, Agent
permissionMode: acceptEdits
---

# task-orchestrator — Layer 2 subagent

Runs `layers/2/PROMPT.md`. See that file for the mission, input/output
contract, and process spec.

## Tool / permission rationale

Per Notion v4 §"Layer 2 — Research / Plan Execution" subagent line 451,
this persona is `task-orchestrator` and CAN "spawn N sub-task-subagents
in parallel". `Agent` is in the toolset to enable that.

`permissionMode: acceptEdits` matches Notion v4 line 451 (`ask` is the
alternative for net-new MCPs).

## Write paths (enforced by prompt + git guard)

| Allowed | Forbidden |
|---|---|
| `outputs/<today>/2/**` | `outputs/<today>/{1,3,4}/**` |
| `queues/gates/**` | `MANDATE.md`, `dept.yaml`, `layers/**`, `subagents/**`, structural files |
| `queues/improvements/**` |  |
| `queues/research/**` (cleanup only) |  |
