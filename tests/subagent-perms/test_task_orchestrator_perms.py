"""
test_task_orchestrator_perms.py — semantic enforcement for task-orchestrator.

Notion v4 source: /tmp/notion_final.txt L446-L451 (Layer 2 — Research /
Plan Execution). Subagent line 451: "peut spawn N sub-task-subagents en
parallèle, permissionMode: ask pour les MCPs nouveaux".

The orchestrator is the only subagent allowed to spawn sub-subagents.
That single capability is what makes Layer 2 the dispatch hub.
"""

from __future__ import annotations

from conftest import parse_tools_field

SUBAGENT = "task-orchestrator"


def test_task_orchestrator_tools_include_required(subagent_file, contract):
    """All required tools per Notion L451 are in `tools:`.

    Includes Agent (sub-spawning capability — the orchestrator's
    defining trait per Notion line 451).
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    expected = contract(SUBAGENT)["tools_must_include"]
    actual = parse_tools_field(fm.get("tools"))
    missing = expected - actual
    assert not missing, (
        f"{SUBAGENT} missing required tools per Notion L451: {sorted(missing)}; "
        f"has: {sorted(actual)}"
    )


def test_task_orchestrator_has_agent_capability(subagent_file):
    """`Agent` MUST be in `tools:` — sub-spawning is the orchestrator's job.

    Per Notion line 451, this is the ONLY subagent allowed to call
    `Agent`. If this assertion fails, the L2 dispatcher can't dispatch.
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    actual = parse_tools_field(fm.get("tools"))
    assert "Agent" in actual, (
        f"{SUBAGENT} missing `Agent` tool — sub-subagent spawning impossible. "
        f"has: {sorted(actual)}"
    )


def test_task_orchestrator_tools_exclude_forbidden(subagent_file, contract):
    """Orchestrator must NOT have direct web access — that's L1/L4's job.

    Notion §Layer 2 doesn't grant WebFetch or WebSearch to the
    orchestrator. If web work is needed, it spawns a sub-subagent with
    that capability.
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    forbidden = contract(SUBAGENT)["tools_must_exclude"]
    actual = parse_tools_field(fm.get("tools"))
    leaked = forbidden & actual
    assert not leaked, (
        f"{SUBAGENT} has forbidden tools (Notion L451 violation): {sorted(leaked)}; "
        f"web work should be delegated to sub-subagents."
    )


def test_task_orchestrator_permission_mode(subagent_file, contract):
    """`permissionMode` matches Notion-v4-derived contract.

    Notion L451 literally says 'permissionMode: ask' for net-new MCPs.
    Step-5 chose 'acceptEdits' as a pragmatic deviation (the loop tick
    can't pause for human ask every iteration). We accept either.
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    expected = contract(SUBAGENT)["permissionMode"]
    actual = fm.get("permissionMode")
    if isinstance(expected, tuple):
        assert actual in expected, (
            f"{SUBAGENT} permissionMode={actual!r}; expected one of {expected} "
            f"(Notion L451 says 'ask'; 'acceptEdits' is Step-5 deviation)"
        )
    else:
        assert actual == expected, (
            f"{SUBAGENT} permissionMode={actual!r}; expected {expected!r}"
        )


def test_task_orchestrator_body_describes_spawning(subagent_file, contract):
    """Body MUST describe sub-subagent spawning + gate output.

    Per Notion L451, the orchestrator spawns and produces gates. The
    body must mention both so a runtime reader understands the role.
    """
    body = subagent_file(SUBAGENT)["body"]
    for needle in contract(SUBAGENT)["body_must_contain_substrings"]:
        assert needle in body, (
            f"{SUBAGENT} body missing required phrase {needle!r}; "
            f"body excerpt:\n{body[:400]}"
        )
