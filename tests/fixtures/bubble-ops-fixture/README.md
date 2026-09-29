# bubble-ops-fixture

**MVP fixture for the [bubble-ops-loop](https://www.notion.so/bubble-ops-loop-Architecture-finale-simplifi-e-366cfc52064481dca58ae2a41e79e11a) framework.**

This is a deliberately-trivial test department used to prove the end-to-end plumbing of the bubble-ops-loop pattern:

- 4-layer OODA cycle (Data → Research → Exec → Risk)
- `/loop` running in a tmux session on Morty VPS
- Subagents with isolated permissions per layer
- Filesystem-as-bus (`queues/`, `outputs/`, `inbox/`) with git commits as audit trail
- GitHub App `bubble-ops-bot` + token broker for cred-less push from VPS

**This repo has no real business domain.** Its purpose is structural validation only. Real departments (Maya, Ben, Tony, Miranda, Eliot) are separate repos forked from `bubble-ops-loop` template once the pattern is proven here.

## Status

- 🟡 MVP in progress (started 2026-05-20)
- See `Rick_RnD/projects/bubble-ops-loop/MVP-ROADMAP.md` (local) for the step-by-step build plan
- Notion architecture (canonical): https://www.notion.so/bubble-ops-loop-Architecture-finale-simplifi-e-366cfc52064481dca58ae2a41e79e11a

## What lands here

- `dept.yaml` — canonical fixture identity (validates against `bubble-ops-loop/schemas-draft/dept.schema.yaml`)
- `layers/{1,2,3,4}/PROMPT.md` — 4 layer prompts (stubs for MVP)
- `.claude/agents/*.md` — 4 subagent personas with scoped tools/permissions
- `tools/echo-tool/` + `skills/echo-skill/` — stub tool + skill to prove the two-tier pattern
- `tests/run.sh` — local harness, tool→skill→layer→department levels
- `queues/{research,gates,management,improvements}/.gitkeep`
- `inbox/decisions/.gitkeep`
- `outputs/<YYYY-MM-DD>/{1,2,3,4}/` — populated by the /loop at runtime
