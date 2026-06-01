"""
test_notion_alignment_diff.py — drift detector with clear remediation output.

For each subagent, compute a structured diff between the actual
frontmatter (loaded from /tmp/bubble-ops-fixture/subagents/<name>.md)
and the Notion v4 contract dict (notion_v4_contract.py). On any
mismatch, the failure message includes BOTH sides + a remediation hint.

This test is the canary for Notion-v4 spec drift: if a Notion page edit
isn't propagated to the contract dict (or vice-versa), this test fires
with maximum signal.

Source: /tmp/notion_final.txt §"Les 4 layers — fractal OODA" L438-L466.
"""

from __future__ import annotations

import pytest

from conftest import SUBAGENT_NAMES, parse_tools_field
from notion_v4_contract import NOTION_V4_SUBAGENT_CONTRACTS


def _compute_diff(name: str, fm: dict) -> dict:
    """Return a structured diff of the frontmatter vs the Notion v4 contract.

    Diff shape:
        {
          'tools_extra':     set of tools in fm but not in contract.must_include,
                              that ARE in contract.must_exclude (true drift),
          'tools_missing':   contract.must_include - actual,
          'disallow_extra':  actual disallow values that contract forbids,
          'disallow_missing':contract.must_include disallow - actual,
          'permission_mode_mismatch': (actual, expected) or None,
        }
    """
    contract = NOTION_V4_SUBAGENT_CONTRACTS[name]
    actual_tools = parse_tools_field(fm.get("tools"))
    actual_disallow = parse_tools_field(fm.get("disallowedTools"))

    must_include = contract["tools_must_include"]
    must_exclude = contract["tools_must_exclude"]
    da_must_include = contract["disallowedTools_must_include"]
    da_must_exclude = contract["disallowedTools_must_exclude"]

    diff: dict = {}

    tools_missing = must_include - actual_tools
    if tools_missing:
        diff["tools_missing"] = sorted(tools_missing)

    tools_extra_forbidden = must_exclude & actual_tools
    if tools_extra_forbidden:
        diff["tools_extra_forbidden"] = sorted(tools_extra_forbidden)

    disallow_missing = da_must_include - actual_disallow
    if disallow_missing:
        diff["disallow_missing"] = sorted(disallow_missing)

    disallow_extra_forbidden = da_must_exclude & actual_disallow
    if disallow_extra_forbidden:
        diff["disallow_extra_forbidden"] = sorted(disallow_extra_forbidden)

    actual_mode = fm.get("permissionMode")
    expected_mode = contract["permissionMode"]
    if isinstance(expected_mode, tuple):
        ok = actual_mode in expected_mode
    else:
        ok = actual_mode == expected_mode
    if not ok:
        diff["permission_mode_mismatch"] = {
            "actual": actual_mode,
            "expected": expected_mode,
        }

    return diff


def _format_remediation(name: str, diff: dict) -> str:
    """Human-readable remediation guide for a failing diff."""
    contract = NOTION_V4_SUBAGENT_CONTRACTS[name]
    src = contract["notion_v4_source_lines"]
    lines = [
        f"\n=== {name} drifted from Notion v4 contract ===",
        f"Notion source: {src}",
        f"File:          /tmp/bubble-ops-fixture/subagents/{name}.md",
        "",
    ]
    if "tools_missing" in diff:
        lines.append(
            f"  ADD to tools:    {diff['tools_missing']}  (Notion-required, currently absent)"
        )
    if "tools_extra_forbidden" in diff:
        lines.append(
            f"  REMOVE from tools: {diff['tools_extra_forbidden']}  (Notion forbids these)"
        )
    if "disallow_missing" in diff:
        lines.append(
            f"  ADD to disallowedTools: {diff['disallow_missing']}  (defense-in-depth required)"
        )
    if "disallow_extra_forbidden" in diff:
        lines.append(
            f"  REMOVE from disallowedTools: {diff['disallow_extra_forbidden']}  "
            f"(denying these breaks the subagent)"
        )
    if "permission_mode_mismatch" in diff:
        pm = diff["permission_mode_mismatch"]
        lines.append(
            f"  FIX permissionMode: actual={pm['actual']!r}, expected={pm['expected']!r}"
        )
    lines.append("")
    lines.append(
        "Resolution: either edit the file to match the contract, or update "
        f"notion_v4_contract.py if Notion v4 itself changed (re-read {src})."
    )
    return "\n".join(lines)


@pytest.mark.parametrize("name", SUBAGENT_NAMES)
def test_subagent_aligns_with_notion_v4(name, subagent_file):
    """Each subagent's frontmatter matches the Notion v4 contract exactly.

    On failure, the message includes a remediation block citing the
    Notion line range and the exact field-level fixes needed.
    """
    fm = subagent_file(name)["frontmatter"]
    diff = _compute_diff(name, fm)
    assert not diff, _format_remediation(name, diff)
