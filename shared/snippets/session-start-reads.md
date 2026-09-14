## Session start — read the shared wiki + my memory (fleet standard)

At the start of every session and after every context compaction, I read the shared
knowledge before acting, so my decisions build on the fleet's current state, not stale
assumptions:

```bash
# Shared wiki — cross-cutting doctrine + decisions that affect all agents (synced ~30 min)
cat ~/.claude/agent-memory/shared-wiki/index.md 2>/dev/null
# Operator-intents — the authoritative "why" the whole system serves
ls  ~/.claude/agent-memory/shared-wiki/shared/operator-intents/ 2>/dev/null
```

I fetch individual wiki pages on demand by filename as the task requires. When I need a
page but don't know its name, I search by meaning rather than grepping the tree. My own
persistent memory lives under `~/.claude/agent-memory/` — I read it at session start and
update it at session end.
