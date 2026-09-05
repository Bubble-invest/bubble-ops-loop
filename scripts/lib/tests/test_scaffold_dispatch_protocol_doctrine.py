"""Regression guard — #1119 review finding #4 (scope-creep revert).

The `feat/1119-agent-unit-generator` PR's `scripts/lib/scaffold.py` diff was
found to ALSO silently revert #1117's already-shipped decide/commit-ledger
dispatch protocol doctrine (`commit_dispatch`, `reconcile_gate_dir`,
`event_trigger_ids_for_dispatch`) back to an older marker convention
(`outputs/<today>/missions/<id>/.last-run` instead of the live
`outputs/<today>/dispatch.json`) in BOTH scaffolded CLAUDE.md templates —
unrelated to #1119's systemd-unification scope, and almost certainly a
bad-rebase pickup (the PR branch predates #1117 landing on main; those
functions are still live in scripts/lib/dispatch_helpers.py on both
branches, so this was never a case of "docs catching up to a deleted API").

Nothing previously locked this doc text down, so the drift went in
silently. This test pins BOTH the pre-activation eclosion CLAUDE.md
(`render_claude_md`, CLAUDE_MD_TEMPLATE) and the post-activation operating
CLAUDE.md (`render_claude_md_operating`, CLAUDE_MD_OPERATING_TEMPLATE) to
the CURRENT (#1117) doctrine so a future rebase/merge can't quietly
resurrect the stale one.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent
for p in (str(SCRIPTS_LIB),):
    if p not in sys.path:
        sys.path.insert(0, p)

import scaffold  # noqa: E402


def _operating_dept_yaml(slug: str = "maya", display_name: str = "Maya") -> dict:
    return {
        "department": {
            "slug": slug,
            "display_name": display_name,
            "mandate": "Sourcer, qualifier, amener a maturite commerciale les prospects.",
            "level": "ops",
        },
        "layers": {"subscribed": [1, 2, 3, 4]},
        "missions": [{"id": "morning-sync", "cadence": "daily"}],
        "gate_policies": {},
    }


def _eclosion_docs() -> list[str]:
    """CLAUDE_MD_TEMPLATE / CLAUDE_MD_MANAGEMENT_TEMPLATE via render_claude_md —
    the pre-activation, "being hatched" doc a freshly scaffolded dept gets."""
    ops = scaffold.render_claude_md("maya", "Maya", level="ops")
    management = scaffold.render_claude_md(
        "tony", "Tony", level="management", children=["maya", "ben"]
    )
    return [ops, management]


def test_eclosion_claude_md_documents_the_live_decide_commit_ledger_helpers():
    for doc in _eclosion_docs():
        for helper in (
            "commit_dispatch",
            "reconcile_gate_dir",
            "event_trigger_ids_for_dispatch",
        ):
            assert helper in doc, (
                f"{helper} missing from the eclosion-phase CLAUDE.md — the "
                "#1117 decide/commit-ledger doctrine must not be silently "
                "reverted"
            )


def test_eclosion_claude_md_uses_the_live_dispatch_json_marker_not_the_stale_one():
    for doc in _eclosion_docs():
        assert "outputs/<today>/dispatch.json" in doc
        assert "missions/<id>/.last-run" not in doc, (
            "the stale pre-#1117 per-mission marker convention "
            "(outputs/<today>/missions/<id>/.last-run) must not reappear — "
            "the live convention is the single dispatch.json ledger"
        )


def test_operating_claude_md_documents_the_live_decide_commit_ledger_helpers():
    doc = scaffold.render_claude_md_operating(_operating_dept_yaml())
    for helper in (
        "commit_dispatch",
        "reconcile_gate_dir",
        "event_trigger_ids_for_dispatch",
    ):
        assert helper in doc, (
            f"{helper} missing from the operating-phase CLAUDE.md — the "
            "#1117 decide/commit-ledger doctrine must not be silently "
            "reverted"
        )
    assert "missions/<id>/.last-run" not in doc, (
        "the stale pre-#1117 per-mission marker convention must not "
        "reappear in the operating CLAUDE.md"
    )


def test_scaffolded_claude_md_still_reflects_the_1119_canonical_unit_naming():
    """Sanity: the revert above must NOT have also clawed back #1119's own
    (legitimate) systemd-naming updates. Only the ops-level eclosion doc and
    the operating doc embed a dept's own systemd wiring."""
    ops_doc = scaffold.render_claude_md("maya", "Maya", level="ops")
    operating_doc = scaffold.render_claude_md_operating(_operating_dept_yaml())

    assert "/run/bubble-agent-" in ops_doc
    assert "bubble-agent@" in operating_doc
    assert "/run/bubble-agent-" in operating_doc

    for doc in [*_eclosion_docs(), operating_doc]:
        assert "ops-loop-{slug}.service" not in doc
        assert "/run/claude-agent-{slug}/env" not in doc
