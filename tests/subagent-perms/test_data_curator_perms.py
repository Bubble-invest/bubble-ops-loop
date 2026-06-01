"""
test_data_curator_perms.py — semantic enforcement for the data-curator subagent.

Notion v4 source: /tmp/notion_final.txt L440-L445 (Layer 1 — Data Update).
Subagent line 445: "tools = Read + WebFetch + Bash(read-only); zéro Write
hors de outputs/<date>/1/".

These tests assert the frontmatter at /tmp/bubble-ops-fixture/subagents/
data-curator.md matches the Notion v4 contract dict. They do NOT
runtime-test (we can't easily spawn a real subagent in pytest); they
verify the static declaration matches what Notion says.
"""

from __future__ import annotations

from conftest import parse_tools_field

SUBAGENT = "data-curator"


def test_data_curator_tools_include_required(subagent_file, contract):
    """All required tools per Notion L445 are in `tools:`.

    Notion: Read + WebFetch + Bash(read-only). Step-5 adds Grep+Glob
    (read variants) and Write (path-policy is body-enforced, not
    tool-level). The full required set comes from the contract dict.
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    expected = contract(SUBAGENT)["tools_must_include"]
    actual = parse_tools_field(fm.get("tools"))
    missing = expected - actual
    assert not missing, (
        f"{SUBAGENT} missing required tools per Notion L445: {sorted(missing)}; "
        f"has: {sorted(actual)}"
    )


def test_data_curator_tools_exclude_forbidden(subagent_file, contract):
    """No forbidden tools per Notion L445 are in `tools:`.

    The curator must NOT have Agent (no sub-spawning at L1), WebSearch
    (only WebFetch is granted), or Task (legacy alias).
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    forbidden = contract(SUBAGENT)["tools_must_exclude"]
    actual = parse_tools_field(fm.get("tools"))
    leaked = forbidden & actual
    assert not leaked, (
        f"{SUBAGENT} has forbidden tools (Notion L445 violation): {sorted(leaked)}; "
        f"full toolset: {sorted(actual)}"
    )


def test_data_curator_disallowedtools_does_not_block_writes(subagent_file, contract):
    """`disallowedTools:` must NOT contain Write/Read/WebFetch/Bash.

    Per the dual-enforcement doctrine (body + git-guard), the
    frontmatter MUST permit Write — denying it would cripple the
    curator's ability to emit outputs/<date>/1/. Path policy lives in
    the body, not the denylist.
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    must_not_deny = contract(SUBAGENT)["disallowedTools_must_exclude"]
    denied = parse_tools_field(fm.get("disallowedTools"))
    overlap = must_not_deny & denied
    assert not overlap, (
        f"{SUBAGENT} disallowedTools wrongly denies critical capabilities: "
        f"{sorted(overlap)}. Path policy belongs in the body, not the denylist."
    )


def test_data_curator_permission_mode(subagent_file, contract):
    """`permissionMode` matches the Notion-v4-derived contract.

    Notion L445 doesn't pin a mode; Step-5 chose `default` (read-mostly
    + WebFetch + scoped Write). The contract codifies that choice.
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


def test_data_curator_body_contains_path_policy(subagent_file, contract):
    """Body cites the path-policy fences per Notion §Queues + L445.

    Because tool-level perms are binary, the prompt body MUST tell the
    runtime where the curator may write. We assert the body mentions
    outputs/<date>/1/ and queues/research/ verbatim.
    """
    body = subagent_file(SUBAGENT)["body"]
    for needle in contract(SUBAGENT)["body_must_contain_substrings"]:
        assert needle in body, (
            f"{SUBAGENT} body missing required path-policy phrase {needle!r}; "
            f"body excerpt:\n{body[:400]}"
        )


def test_data_curator_body_excludes_overclaims(subagent_file, contract):
    """Body MUST NOT claim 'never write to X' (frontmatter can't enforce that).

    Saying 'never write to MANDATE.md' in the body would mislead a
    reader into thinking the subagent runtime guarantees it. The
    runtime can't — only the git-guard (Step 3c) does. So we forbid
    overclaim phrases here.
    """
    body = subagent_file(SUBAGENT)["body"].lower()
    for needle in contract(SUBAGENT)["body_must_exclude_substrings"]:
        assert needle.lower() not in body, (
            f"{SUBAGENT} body contains overclaim {needle!r}; "
            f"path policy is body+git-guard, not frontmatter-enforced."
        )
