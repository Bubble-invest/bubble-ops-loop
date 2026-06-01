"""
test_cross_pollination.py — cross-subagent capability isolation.

These tests span the 4 subagent files and assert capability boundaries
that no single per-subagent test can catch. The big invariant: each
subagent's footprint is STRICTLY MINIMAL — no accidental superpowers
leak between personas.

Source-of-truth: /tmp/notion_final.txt §"Les 4 layers — fractal OODA"
(L438-L466). Each layer's subagent has a distinct power profile:

  L1 data-curator      — Web in, scoped Write
  L2 task-orchestrator — Agent (spawning), scoped Write
  L3 executor          — Bash + Write, NO web
  L4 mandate-guardian  — WebSearch + Write, NO Bash, NO Agent
"""

from __future__ import annotations

from conftest import parse_tools_field


def test_no_subagent_has_all_capabilities(all_subagents):
    """No subagent has WebSearch + Bash + Agent simultaneously.

    The combination of search (data acquisition) + shell (mutation) +
    delegation (force multiplier) would be a single-subagent
    super-admin — exactly what Notion v4's per-layer isolation forbids.
    """
    for name, loaded in all_subagents:
        tools = parse_tools_field(loaded["frontmatter"].get("tools"))
        super_set = {"WebSearch", "Bash", "Agent"}
        if super_set.issubset(tools):
            raise AssertionError(
                f"{name} holds super-admin combo {sorted(super_set)}; "
                f"violates per-layer isolation (Notion §Les 4 layers L438-L466)."
            )


def test_only_task_orchestrator_has_agent(all_subagents):
    """`Agent` (sub-spawning) is L2's defining capability per Notion L451.

    No other subagent should hold it. data-curator (L1) does data,
    executor (L3) does action, mandate-guardian (L4) does audit. None
    of them should be re-dispatching work.
    """
    holders = [
        name for name, loaded in all_subagents
        if "Agent" in parse_tools_field(loaded["frontmatter"].get("tools"))
    ]
    assert holders == ["task-orchestrator"], (
        f"`Agent` tool holders = {holders}; "
        f"expected only ['task-orchestrator'] per Notion L451."
    )


def test_only_executor_explicitly_denies_web(all_subagents):
    """executor is the ONLY subagent with `disallowedTools: WebFetch, WebSearch`.

    Per Step-5 doctrine (defense-in-depth on the sharpest tool):
    - data-curator NEEDS WebFetch → can't deny it.
    - task-orchestrator doesn't need web AND doesn't need explicit
      denial (the allowlist omission is enough at L2).
    - mandate-guardian NEEDS WebSearch → can't deny it.
    - executor is the unique case that benefits from belt+suspenders.
    """
    deniers = []
    for name, loaded in all_subagents:
        denied = parse_tools_field(loaded["frontmatter"].get("disallowedTools"))
        if {"WebFetch", "WebSearch"}.issubset(denied):
            deniers.append(name)
    assert deniers == ["executor"], (
        f"explicit-web-denial holders = {deniers}; "
        f"expected only ['executor'] per Step-5 defense-in-depth doctrine."
    )


def test_only_mandate_guardian_lacks_bash(all_subagents):
    """`mandate-guardian` is the ONLY subagent without `Bash`.

    Per Notion L466 'Pure auditeur, jamais exécuteur' — the auditor
    can't have shell access. The other three (L1, L2, L3) all need
    Bash for git operations / file shuffling / exec.
    """
    bash_lackers = []
    for name, loaded in all_subagents:
        tools = parse_tools_field(loaded["frontmatter"].get("tools"))
        if "Bash" not in tools:
            bash_lackers.append(name)
    assert bash_lackers == ["mandate-guardian"], (
        f"subagents lacking Bash = {bash_lackers}; "
        f"expected only ['mandate-guardian'] per Notion L466."
    )


def test_only_data_curator_has_webfetch_without_websearch(all_subagents):
    """data-curator has WebFetch but NOT WebSearch (per Notion L445).

    Notion grants the data-curator full URL fetch (deterministic data
    pull from known endpoints), but not generic web search (that's the
    auditor's job at L4 for context-gathering). This asymmetry is
    intentional.
    """
    matches = []
    for name, loaded in all_subagents:
        tools = parse_tools_field(loaded["frontmatter"].get("tools"))
        if "WebFetch" in tools and "WebSearch" not in tools:
            matches.append(name)
    assert matches == ["data-curator"], (
        f"WebFetch-only holders = {matches}; "
        f"expected only ['data-curator'] per Notion L445."
    )


def test_only_mandate_guardian_has_websearch(all_subagents):
    """`WebSearch` is L4's contextual-research capability per Notion L466.

    No other subagent should hold it. data-curator uses WebFetch
    (deterministic), executor uses neither (no web).
    """
    holders = [
        name for name, loaded in all_subagents
        if "WebSearch" in parse_tools_field(loaded["frontmatter"].get("tools"))
    ]
    assert holders == ["mandate-guardian"], (
        f"`WebSearch` tool holders = {holders}; "
        f"expected only ['mandate-guardian'] per Notion L466."
    )


def test_every_subagent_has_read_grep_glob(all_subagents):
    """ALL subagents have the read triad (Read+Grep+Glob).

    Every persona must be able to inspect its inputs. This is the
    baseline floor — no subagent can do its job without it.
    """
    floor = {"Read", "Grep", "Glob"}
    for name, loaded in all_subagents:
        tools = parse_tools_field(loaded["frontmatter"].get("tools"))
        missing = floor - tools
        assert not missing, (
            f"{name} missing read-triad floor: {sorted(missing)}; has: {sorted(tools)}"
        )


def test_no_subagent_has_unknown_tool(all_subagents):
    """Every tool in any `tools:` or `disallowedTools:` field is recognized.

    Catches typos like 'Webfetch' (wrong case) or 'Excecute' or
    'WebSearchTool' that would silently fail to restrict anything.
    """
    KNOWN_TOOLS = {
        # Core Claude Code tools (per docs.anthropic.com/claude-code)
        "Read", "Write", "Edit", "Grep", "Glob",
        "Bash", "WebFetch", "WebSearch",
        "Agent", "Task",
        "TodoWrite", "NotebookRead", "BashOutput", "KillBash",
        "SlashCommand", "ExitPlanMode",
    }
    for name, loaded in all_subagents:
        fm = loaded["frontmatter"]
        for field in ("tools", "disallowedTools"):
            declared = parse_tools_field(fm.get(field))
            unknown = declared - KNOWN_TOOLS
            assert not unknown, (
                f"{name}.{field} declares unknown tool(s): {sorted(unknown)}; "
                f"likely a typo (case-sensitive). Known: {sorted(KNOWN_TOOLS)}"
            )
