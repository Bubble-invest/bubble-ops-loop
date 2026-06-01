"""
test_executor_perms.py — semantic enforcement for the executor subagent.

Notion v4 source: /tmp/notion_final.txt L452-L458 (Layer 3 — Execution).
Subagent line 458: "tools = Read + Write + Bash(scoped allowlist) + MCP
scoped au dept; poka-yoke allow_live=True sur toute action irréversible".

The executor is the SHARPEST tool in the kit: it does side effects on
the world (commits, messages, orders). It MUST NOT browse or search —
all decisions were made upstream at L2 + human gate. Defense-in-depth:
both the allowlist excludes web tools AND the explicit `disallowedTools`
re-denies them.
"""

from __future__ import annotations

from conftest import parse_tools_field

SUBAGENT = "executor"


def test_executor_tools_include_required(subagent_file, contract):
    """All required tools per Notion L458 are in `tools:`.

    Notion: Read + Write + Bash. Step-5 adds Grep+Glob (read variants).
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    expected = contract(SUBAGENT)["tools_must_include"]
    actual = parse_tools_field(fm.get("tools"))
    missing = expected - actual
    assert not missing, (
        f"{SUBAGENT} missing required tools per Notion L458: {sorted(missing)}; "
        f"has: {sorted(actual)}"
    )


def test_executor_tools_exclude_web_and_agent(subagent_file, contract):
    """Executor MUST NOT have WebFetch, WebSearch, or Agent.

    Per Notion §Layer 3, the executor reads decisions already made by
    L2 + human gate. Re-planning at exec time defeats the gate model.
    Web access risks leaking secrets via outbound traffic.
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    forbidden = contract(SUBAGENT)["tools_must_exclude"]
    actual = parse_tools_field(fm.get("tools"))
    leaked = forbidden & actual
    assert not leaked, (
        f"{SUBAGENT} has forbidden capabilities (critical isolation breach): "
        f"{sorted(leaked)}; this defeats the L2-gate model."
    )


def test_executor_disallowedtools_explicitly_denies_web(subagent_file, contract):
    """`disallowedTools:` MUST explicitly deny WebFetch + WebSearch.

    Defense-in-depth: even if a future edit accidentally adds WebFetch
    to the allowlist, the denylist rejects it at the tool-routing
    layer. The executor is the only subagent with this explicit
    denial — that's the cross-pollination test in
    test_cross_pollination.py.
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    expected = contract(SUBAGENT)["disallowedTools_must_include"]
    denied = parse_tools_field(fm.get("disallowedTools"))
    missing = expected - denied
    assert not missing, (
        f"{SUBAGENT} disallowedTools missing required denials: {sorted(missing)}; "
        f"has: {sorted(denied)}. Defense-in-depth requires both allowlist "
        f"exclusion AND explicit denial."
    )


def test_executor_permission_mode(subagent_file, contract):
    """`permissionMode` matches the Notion-v4-derived contract.

    Notion §Layer 3 doesn't pin a mode; Step-5 chose `acceptEdits` (same
    as orchestrator — fast loop, scoped writes, gates already passed).
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    expected = contract(SUBAGENT)["permissionMode"]
    actual = fm.get("permissionMode")
    if isinstance(expected, tuple):
        assert actual in expected, (
            f"{SUBAGENT} permissionMode={actual!r}; expected one of {expected}"
        )
    else:
        assert actual == expected, (
            f"{SUBAGENT} permissionMode={actual!r}; expected {expected!r}"
        )


def test_executor_body_describes_inbox_and_exec_log(subagent_file, contract):
    """Body MUST cite inbox/decisions/ (input) + exec-log.jsonl (output).

    Per Notion L456-L458, the executor reads inbox/decisions/<id>.yaml
    and writes outputs/<date>/3/exec-log.jsonl. The body must restate
    that contract so a runtime reader can verify the persona.
    """
    body = subagent_file(SUBAGENT)["body"]
    for needle in contract(SUBAGENT)["body_must_contain_substrings"]:
        assert needle in body, (
            f"{SUBAGENT} body missing required phrase {needle!r}; "
            f"body excerpt:\n{body[:400]}"
        )


def test_executor_body_does_not_authorize_web(subagent_file, contract):
    """Body must NOT contain POSITIVE-context web-permission phrases.

    Negative phrases like 'NEVER browse the web' are FINE — they
    reinforce the persona. But 'may browse the web' or 'is allowed to
    fetch' would contradict the frontmatter's disallowedTools.
    """
    body = subagent_file(SUBAGENT)["body"].lower()
    for needle in contract(SUBAGENT)["body_must_exclude_substrings"]:
        assert needle.lower() not in body, (
            f"{SUBAGENT} body contains positive-context web auth {needle!r}; "
            f"contradicts disallowedTools=[WebFetch, WebSearch]."
        )
