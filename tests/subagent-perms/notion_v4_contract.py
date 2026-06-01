"""
notion_v4_contract.py — canonical Notion v4 subagent perm contract.

Source-of-truth: `/tmp/notion_final.txt` (817-line dump of the Notion v4
architecture page, fetched 2026-05-20T15:54). Every entry in the dict below
cites the exact Notion line range it enforces.

This dict is the SPEC. The 4 subagent files at
`/tmp/bubble-ops-fixture/subagents/*.md` are the IMPLEMENTATION. The test
suite under `tests/subagent-perms/` asserts impl matches spec; on drift,
either the spec dict is wrong (update it after re-reading Notion) or the
file is wrong (fix the file in the fixture repo).

KEY DESIGN NOTE on Step-5 deviation: Claude Code subagent perms are binary
allow/deny at the tool-name level — there's no native path-scoping. So
`data-curator` and `mandate-guardian` MUST keep `Write` in `tools:` even
though Notion line 445 says "zéro Write hors de outputs/<date>/1/" and
line 466 says "AUCUNE écriture hors outputs/4/". Path enforcement is
delegated to the prompt body + the git-guard (Step 3c). This contract
codifies that two-rail approach: the frontmatter allows the tool, the
body says where it may write.
"""

from __future__ import annotations

# Notion v4 line refs:
#   L445 — data-curator     — "tools = Read + WebFetch + Bash(read-only); zéro Write hors de outputs/<date>/1/"
#   L451 — task-orchestrator — "peut spawn N sub-task-subagents en parallèle, permissionMode: ask pour les MCPs nouveaux"
#   L458 — executor         — "tools = Read + Write + Bash(scoped allowlist) + MCP scoped au dept; poka-yoke allow_live=True"
#   L466 — mandate-guardian — "tools = Read + Grep + Glob + WebSearch; AUCUNE écriture hors outputs/4/. Pure auditeur, jamais exécuteur."

NOTION_V4_SUBAGENT_CONTRACTS = {
    "data-curator": {
        # Notion line 445 names: Read + WebFetch + Bash(read-only). Step-5
        # adds Grep + Glob (search variants of Read; harmless) and Write
        # (path-policy is body-enforced, see note above).
        "tools_must_include": {"Read", "Grep", "Glob", "Bash", "WebFetch", "Write"},
        # Notion never grants the curator agentic delegation, WebSearch
        # (only WebFetch), or Task. None of these may appear.
        "tools_must_exclude": {"Agent", "WebSearch", "Task"},
        # No denylist needed — path policy is body-enforced. The critical
        # constraint is that Write MUST NOT be in disallowedTools (else the
        # curator can't write its outputs).
        "disallowedTools_must_include": set(),
        "disallowedTools_must_exclude": {"Write", "Read", "WebFetch", "Bash"},
        # Notion line 445 says no permissionMode; Step-5 chose `default`
        # (read-mostly + WebFetch + Write to a scoped path).
        "permissionMode": "default",
        # No MCP constraint in v1 fixture.
        "mcpServers_must_be_subset_of": None,
        # Body content checks: the file MUST tell readers about the path
        # policy since the frontmatter can't enforce it.
        "body_must_contain_substrings": [
            "path policy",
            "outputs/<date>/1/",     # exact phrase from description (line 3)
            "queues/research/",       # canonical output queue per Notion §Queues
        ],
        # NEGATIVE-context phrases like "never write" would over-claim
        # — the frontmatter doesn't enforce it, only the prompt does.
        # So we explicitly forbid that misleading wording.
        "body_must_exclude_substrings": ["never write to", "cannot write"],
        "notion_v4_source_lines": "L440-L445 (Layer 1 — Data Update; subagent line 445)",
    },
    "task-orchestrator": {
        # Notion line 451 says: "peut spawn N sub-task-subagents en parallèle".
        # That mandates `Agent`. The orchestrator also needs the basic
        # read/write/exec stack to dispatch.
        "tools_must_include": {"Read", "Grep", "Glob", "Write", "Bash", "Agent"},
        # The orchestrator is NOT a data-curator (no web fetch) and NOT
        # an auditor (no web search). It dispatches to sub-subagents that
        # have web access if needed.
        "tools_must_exclude": {"WebFetch", "WebSearch"},
        "disallowedTools_must_include": set(),
        "disallowedTools_must_exclude": set(),
        # Notion line 451 literally says `permissionMode: ask`. Step-11
        # robustness sweep (QA-AUDIT-J2 §3.4 DRIFT-3) tightened this to a
        # single value: `acceptEdits`.
        #
        # Rationale (Joris-approved decision, 2026-05-20):
        #   - The fixture has ZERO MCPs configured, so `ask` would never
        #     prompt for a NEW MCP — but `ask` would still block on tool
        #     uses Claude Code's permission system flags (Edit, Write,
        #     Bash with new args) every single /loop iteration. That makes
        #     the headless loop non-functional.
        #   - `acceptEdits` lets the loop progress while still requiring
        #     a clean exit-then-re-enter for shell-side-effect operations
        #     outside the allowlist.
        #
        # Notion deviation: explicit and documented here, in the subagent
        # file body, and in QA-AUDIT-J2.md. Real depts (Maya, Tony) may
        # revisit when MCP wiring is added — at that point `ask` may
        # become viable if MCPs only initialize once per session.
        "permissionMode": "acceptEdits",
        "mcpServers_must_be_subset_of": None,
        "body_must_contain_substrings": ["spawn", "queues/gates"],
        "body_must_exclude_substrings": [],
        "notion_v4_source_lines": "L446-L451 (Layer 2 — Research / Plan Execution; subagent line 451)",
    },
    "executor": {
        # Notion line 458: "Read + Write + Bash(scoped allowlist) + MCP scoped au dept".
        # Step-5 adds Grep + Glob (read variants).
        "tools_must_include": {"Read", "Grep", "Glob", "Write", "Bash"},
        # CRITICAL ISOLATION per Notion §Layer 3: the executor must NEVER
        # browse/search (decisions already made) and must NEVER spawn
        # sub-agents (no re-planning at exec time).
        "tools_must_exclude": {"WebFetch", "WebSearch", "Agent"},
        # Defense-in-depth: even if tools_must_exclude removes them from
        # the allowlist, an explicit denylist guarantees rejection if the
        # allowlist ever grows.
        "disallowedTools_must_include": {"WebFetch", "WebSearch"},
        "disallowedTools_must_exclude": set(),
        # Notion §Layer 3 doesn't pin a permissionMode; Step-5 chose
        # `acceptEdits` (same as orchestrator — fast loop, scoped writes).
        "permissionMode": "acceptEdits",
        "mcpServers_must_be_subset_of": None,
        "body_must_contain_substrings": ["inbox/decisions", "exec-log.jsonl"],
        # The body explicitly forbids web browsing in NEGATIVE-context
        # phrases (e.g., "NEVER browse the web"). We can't exclude those
        # substrings without false-positive. Instead, we forbid
        # POSITIVE-context phrases that would contradict the persona.
        "body_must_exclude_substrings": [
            "may browse the web",
            "is allowed to fetch",
            "may search the web",
        ],
        "notion_v4_source_lines": "L452-L458 (Layer 3 — Execution; subagent line 458)",
    },
    "mandate-guardian": {
        # Notion line 466: "Read + Grep + Glob + WebSearch". Step-5 adds
        # Write because the guardian MUST emit the 3 hierarchy outputs
        # (risk-brief.md, risk-kpis.yaml, management-export.yaml). Path
        # policy is body-enforced (same dual-rail as data-curator).
        "tools_must_include": {"Read", "Grep", "Glob", "WebSearch", "Write"},
        # PURE AUDITOR per Notion line 466: "jamais exécuteur".
        #   - no Bash    → no shell side-effects
        #   - no Agent   → no sub-subagent spawning, the auditor does its own work
        #   - no WebFetch → WebSearch is sufficient for context, fetch is over-priv
        "tools_must_exclude": {"Bash", "Agent", "WebFetch"},
        "disallowedTools_must_include": set(),
        "disallowedTools_must_exclude": {"Write"},  # needs Write for the 3 outputs
        # Notion §Layer 4 doesn't pin a permissionMode; Step-5 chose
        # `default` (read-mostly + WebSearch; writes are scoped via body).
        "permissionMode": "default",
        "mcpServers_must_be_subset_of": None,
        # Per Notion line 466 verbatim "Pure auditeur, jamais exécuteur".
        # The body must restate this. Step-5 wrote "Pure auditor" in the
        # description (line 3, in the frontmatter) and "Pure auditeur,
        # jamais exécuteur" in the body rationale (line 17). We accept
        # either spelling (case-insensitive) since the description is
        # technically frontmatter and our body-content check looks at
        # the body only.
        #
        # We accept both 'pure auditor' AND 'pure auditeur' here by
        # using only the substring 'pure aud' which is a prefix of
        # both — case-insensitive matching applied at assertion time.
        "body_must_contain_substrings": [
            "pure aud",                # matches 'pure auditor' OR 'pure auditeur'
            "risk-brief.md",
            "risk-kpis.yaml",
            "management-export.yaml",
            "outputs/<today>/4/",       # body uses <today>, description uses <date>
        ],
        # Negative-context phrases like "no spawning" / "never execute"
        # / "Git commits are done by the parent" exist in the body and
        # are FINE — they reinforce the persona. We exclude only
        # POSITIVE-context phrases that would contradict.
        "body_must_exclude_substrings": [
            "may spawn",
            "may execute",
            "may commit",
        ],
        "notion_v4_source_lines": "L459-L466 (Layer 4 — Risk Control; subagent line 466 verbatim 'AUCUNE écriture hors outputs/4/. Pure auditeur, jamais exécuteur.')",
    },
}
