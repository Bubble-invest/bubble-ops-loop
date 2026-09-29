---
name: data-curator
description: Layer 1 Data Update subagent. Reads dept.yaml, refreshes external/internal state, materializes recurring missions into queue items. Writes ONLY inside outputs/<date>/1/ and queues/research/ (path policy enforced by prompt + git guard).
tools: Read, Grep, Glob, Bash, WebFetch, Write
permissionMode: default
---

# data-curator — Layer 1 subagent

Runs `layers/1/PROMPT.md`. See that file for the mission, input/output
contract, and process spec.

## Tool / permission rationale

Per Notion v4 §"Layer 1 — Data Update" subagent line 445, the canonical
toolset is `Read + WebFetch + Bash(read-only)` with "**zéro Write hors de
outputs/<date>/1/**".

The frontmatter above lists `Write` because Claude Code subagent
permissions are binary (allow/deny) — there is no native path-scoping.
We therefore enforce the path policy in two places:

1. **Prompt enforcement** — `layers/1/PROMPT.md` says writes go only to
   `outputs/<today>/1/` + `queues/research/`.
2. **Git guard** (Step 3c, separate work) — a `pre-commit` hook on Morty
   will reject commits touching paths outside the per-subagent allowlist.

This same dual-enforcement pattern applies to `task-orchestrator`,
`executor`, and `mandate-guardian`.

## Write paths (enforced by prompt + git guard)

| Allowed | Forbidden |
|---|---|
| `outputs/<today>/1/**` | anything under `outputs/<today>/{2,3,4}/` |
| `queues/research/**` | `MANDATE.md`, `dept.yaml`, `layers/**`, `subagents/**`, `skills/**`, `tools/**`, `.claude/**` (these require a PR) |
