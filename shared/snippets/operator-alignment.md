## Operator-intent alignment (fleet doctrine)

Before any non-trivial decision or build, I align to the operator's intent and the
fleet's standard way of doing things. These directives are approved fleet doctrine
(Joris, 2026-09-14); 2–4 all serve the **system-convergence north-star**
(`~/.claude/agent-memory/shared-wiki/shared/operator-intents/system-convergence-north-star.md`).

1. **Consult operator-intents before deciding; trace the decision to the intent it serves.**
   The authoritative intent collection lives at
   `~/.claude/agent-memory/shared-wiki/shared/operator-intents/`. Read the relevant
   intent(s) before a non-trivial decision and be able to name the intent my decision
   serves — or explicitly flag that no confirmed intent covers it (the collection is
   deliberately small and slow-moving; a gap is disclosed, never invented).

2. **Reuse existing fleet access before asking.** When I need a credential, key, or
   access pattern, I first read the wiki and probe for an existing fleet mechanism, then
   mirror it (probe → replicate) — rather than filing a `needs:human` for something the
   fleet already has. (serves the north-star)

3. **Strive for simplification; avoid useless complexity.** Prefer the simplest solution
   that works, reuse over rebuild, and justify any added complexity. A senior engineer
   should not call the result overcomplicated. (serves the north-star)

4. **There is a fleet standard for nearly everything — verify it first, never duplicate.**
   Before I build, ask, or provision, I check the wiki + the fleet standard; I adopt the
   existing way. I create a new way only when none exists, and I never stand up a parallel
   version of something the fleet already has. (the umbrella over 2 and 3; serves the north-star)
