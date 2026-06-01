"""
test_mandate_guardian_perms.py — semantic enforcement for mandate-guardian.

Notion v4 source: /tmp/notion_final.txt L459-L466 (Layer 4 — Risk Control).
Subagent line 466 VERBATIM: "tools = Read + Grep + Glob + WebSearch ;
AUCUNE écriture hors outputs/4/. Pure auditeur, jamais exécuteur."

The mandate-guardian is the PURE AUDITOR. It reads the day's outputs
vs the dept's MANDATE.md and writes a brief + KPIs + management export.
It is the ONLY subagent that lacks Bash (no shell side-effects). It is
intentionally less powerful than the executor — auditors who can act
become accomplices.
"""

from __future__ import annotations

from conftest import parse_tools_field

SUBAGENT = "mandate-guardian"


def test_mandate_guardian_tools_include_required(subagent_file, contract):
    """All required tools per Notion L466 are in `tools:`.

    Notion verbatim: Read + Grep + Glob + WebSearch. Step-5 adds Write
    (to emit the 3 hierarchy outputs); path-policy is body-enforced.
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    expected = contract(SUBAGENT)["tools_must_include"]
    actual = parse_tools_field(fm.get("tools"))
    missing = expected - actual
    assert not missing, (
        f"{SUBAGENT} missing required tools per Notion L466: {sorted(missing)}; "
        f"has: {sorted(actual)}"
    )


def test_mandate_guardian_tools_exclude_bash_agent_webfetch(subagent_file, contract):
    """PURE AUDITOR per Notion L466: no Bash, no Agent, no WebFetch.

    - Bash: an auditor with shell access can mutate the world it audits.
    - Agent: an auditor that delegates becomes a re-planner; we need it
      to do its own analysis.
    - WebFetch: WebSearch is sufficient for context; full fetch lets the
      auditor pull arbitrary URLs (over-privileged).
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    forbidden = contract(SUBAGENT)["tools_must_exclude"]
    actual = parse_tools_field(fm.get("tools"))
    leaked = forbidden & actual
    assert not leaked, (
        f"{SUBAGENT} violates 'Pure auditeur, jamais exécuteur' (Notion L466): "
        f"forbidden tools present: {sorted(leaked)}; full toolset: {sorted(actual)}."
    )


def test_mandate_guardian_disallowedtools_permits_write(subagent_file, contract):
    """`disallowedTools:` must NOT contain Write — guardian needs to emit outputs.

    Per Notion L465, the guardian writes risk-brief.md, risk-kpis.yaml,
    and management-export.yaml. Denying Write would block the hierarchy
    export pipeline.
    """
    fm = subagent_file(SUBAGENT)["frontmatter"]
    must_not_deny = contract(SUBAGENT)["disallowedTools_must_exclude"]
    denied = parse_tools_field(fm.get("disallowedTools"))
    overlap = must_not_deny & denied
    assert not overlap, (
        f"{SUBAGENT} disallowedTools wrongly denies critical capability: "
        f"{sorted(overlap)}. Path policy lives in the body, not the denylist."
    )


def test_mandate_guardian_permission_mode(subagent_file, contract):
    """`permissionMode` matches Notion-v4-derived contract.

    Notion L466 doesn't pin a mode; Step-5 chose `default` (read-mostly
    + WebSearch + scoped Write).
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


def test_mandate_guardian_body_states_pure_auditor(subagent_file, contract):
    """Body MUST contain 'pure auditor' (case-insensitive) + the 3 hierarchy outputs.

    Per Notion L466 verbatim ('Pure auditeur, jamais exécuteur'), the
    body must restate this so runtime readers see the persona.
    """
    body = subagent_file(SUBAGENT)["body"].lower()
    for needle in contract(SUBAGENT)["body_must_contain_substrings"]:
        assert needle.lower() in body, (
            f"{SUBAGENT} body missing required phrase {needle!r}; "
            f"body excerpt:\n{subagent_file(SUBAGENT)['body'][:600]}"
        )


def test_mandate_guardian_body_does_not_authorize_actions(subagent_file, contract):
    """Body must NOT contain POSITIVE-context action-permission phrases.

    Negative phrases like 'no sub-subagent spawning' / 'Git commits are
    done by the parent' are FINE — they reinforce the persona. But
    'may spawn' / 'may execute' / 'may commit' would contradict the
    pure-auditor mandate.
    """
    body = subagent_file(SUBAGENT)["body"].lower()
    for needle in contract(SUBAGENT)["body_must_exclude_substrings"]:
        assert needle.lower() not in body, (
            f"{SUBAGENT} body contains positive-context action auth {needle!r}; "
            f"contradicts 'Pure auditeur, jamais exécuteur' (Notion L466)."
        )
