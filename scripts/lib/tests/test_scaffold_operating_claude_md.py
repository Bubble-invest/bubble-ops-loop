"""
test_scaffold_operating_claude_md.py — TDD tests for the post-éclosion
CLAUDE.md flip.

{{OPERATOR}} flag 2026-05-24 msg 3060:
  > The current operational manual for local maya will be in her mandate,
  > that's the whole point. Still, her Claude.md does need to be rewritten
  > after éclosion, but just to remove the éclosion part and go to
  > operating mode (same for all agents as well), explaining the setup,
  > mandate and layers. And including the parts about non tech user and
  > how to behave regarding doc, etc

This file tests `scaffold.render_claude_md_operating()` — the NEW renderer
that produces the post-activation CLAUDE.md from `dept.yaml` data
(no per-dept hardcoding).

The operating CLAUDE.md must:
  - mention the display_name and mandate (one-liner from dept.yaml)
  - list each subscribed layer
  - reference MANDATE.md for doctrine detail
  - keep evergreen content: voice rules, Karpathy garde-fous, /loop
    runtime, when-stuck protocol
  - DROP éclosion-driver content: 7-step flow, SessionStart hook for
    auto-driving, references to `department-onboarding-guide`
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path surgery (matches sibling test files).
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
SCRIPTS_LIB = HERE.parent
SCRIPTS_DIR = SCRIPTS_LIB.parent
PROJECT_ROOT = SCRIPTS_DIR.parent
SKILL_ROOT = PROJECT_ROOT / "skills" / "department-onboarding-guide"
for p in (str(SKILL_ROOT), str(SCRIPTS_LIB)):
    if p not in sys.path:
        sys.path.insert(0, p)

import scaffold  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_dept_yaml_ops(slug: str = "maya", display_name: str = "Maya",
                         mandate: str = "Sourcer, qualifier, amener à maturité commerciale les prospects LinkedIn.",
                         layers=(1, 2, 3, 4),
                         missions=("morning-sync", "discovery-feed-scan",
                                   "draft-batch", "daily-risk-audit")
                         ) -> dict:
    """Minimal but realistic dept.yaml shape an ops dept exposes."""
    return {
        "department": {
            "slug": slug,
            "display_name": display_name,
            "mandate": mandate,
            "level": "ops",
        },
        "layers": {"subscribed": list(layers)},
        "missions": [{"id": m, "cadence": "daily"} for m in missions],
        "gate_policies": {
            "draft_send": {
                "current_mode": "manual_required",
            },
        },
    }


def _make_dept_yaml_management(slug: str = "tony",
                                display_name: str = "Tony",
                                mandate: str = "Coordonner les dept-managers et synthétiser le brief CEO.",
                                children=("maya", "ben", "miranda"),
                                layers=(1, 4)) -> dict:
    return {
        "department": {
            "slug": slug,
            "display_name": display_name,
            "mandate": mandate,
            "level": "management",
        },
        "layers": {"subscribed": list(layers)},
        "missions": [{"id": "daily-risk-audit", "cadence": "daily"}],
        "hierarchy": {
            "level": "management",
            "children": list(children),
        },
        "gate_policies": {},
    }


# ---------------------------------------------------------------------------
# Renderer existence + signature
# ---------------------------------------------------------------------------

def test_render_claude_md_operating_function_exists():
    """The new function must exist as a top-level scaffold export."""
    assert hasattr(scaffold, "render_claude_md_operating"), (
        "scaffold.render_claude_md_operating(dept_yaml) is the post-éclosion "
        "CLAUDE.md renderer. Must be exported from scripts/lib/scaffold.py."
    )


def test_render_claude_md_operating_returns_string():
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    assert isinstance(out, str)
    assert len(out) > 200, "operating CLAUDE.md should be substantive"


# ---------------------------------------------------------------------------
# Content the operating CLAUDE.md MUST contain
# ---------------------------------------------------------------------------

def test_operating_mentions_display_name_and_mandate():
    """The dept's identity + 1-line mandate must appear in the doc."""
    dy = _make_dept_yaml_ops(
        display_name="Maya",
        mandate="Sourcer, qualifier, amener à maturité commerciale les prospects LinkedIn.",
    )
    out = scaffold.render_claude_md_operating(dy)
    assert "Maya" in out
    assert "Sourcer, qualifier" in out, (
        "mandate one-liner from dept.yaml must appear verbatim"
    )


def test_operating_lists_each_subscribed_layer():
    dy = _make_dept_yaml_ops(layers=(1, 2, 3, 4))
    out = scaffold.render_claude_md_operating(dy)
    # Some marker per layer — using the layer index is the simplest contract
    for n in (1, 2, 3, 4):
        assert f"Layer {n}" in out or f"layer {n}" in out.lower(), (
            f"operating CLAUDE.md must mention Layer {n}"
        )


def test_operating_lists_each_subscribed_layer_partial():
    """A dept that subscribes only to subset (e.g. management = [1, 4])
    must show ONLY those, not the unsubscribed ones."""
    dy = _make_dept_yaml_management(layers=(1, 4))
    out = scaffold.render_claude_md_operating(dy)
    assert "Layer 1" in out
    assert "Layer 4" in out
    # Layers it doesn't subscribe to should not be presented as "its" layer.
    # We allow the words 2/3 to appear (e.g. in the /loop runtime protocol
    # description) — only the per-layer listing must be filtered.
    # Easiest contract: there's a per-layer block "Layer N — ..." count.
    layer_blocks = out.count("Layer ")
    # 2 subscribed in "Mes 4 moments par jour" listing + the dispatch tree
    # (STEP C.0..C.4) legitimately names every layer (1, 2, 3, 4) since the
    # tree describes what *can* fire — even if this dept only subscribes to
    # 2 of them. Empirically ~13 for a 2-layer management dept; cap at 18
    # to leave headroom for minor prose tweaks while still catching a real
    # leak (e.g. accidentally listing all 4 in the per-layer block).
    assert layer_blocks <= 18, (
        f"too many Layer mentions ({layer_blocks}); the per-layer listing "
        "may be ignoring the subscribed filter"
    )


def test_operating_references_mandate_md_not_repeats_it():
    """Doctrine detail lives in MANDATE.md. The operating CLAUDE.md
    references it but does not embed the full doctrine."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    assert "MANDATE.md" in out, (
        "operating CLAUDE.md must reference MANDATE.md for doctrine detail"
    )


def test_operating_has_voice_rules_for_non_tech_user():
    """{{OPERATOR}} asked specifically for 'parts about non tech user and how to
    behave regarding doc, etc'. The operating CLAUDE.md must contain
    voice/audience rules."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    body_lower = out.lower()
    # Look for any of the canonical voice markers
    voice_markers = ["bureau-de-cadre", "non-tech", "non tech",
                     "novice", "tutoiement", "jargon", "audience"]
    found = [m for m in voice_markers if m in body_lower]
    assert found, (
        f"voice/audience section missing. Looked for {voice_markers}, "
        f"found none in operating CLAUDE.md."
    )


def test_operating_states_telegram_is_the_channel_to_joris():
    """{{OPERATOR}} msg 3594: every dept's CLAUDE.md must clearly state that its
    channel to {{OPERATOR}} IS its Telegram bot (escalations/questions/decisions go
    there; the session transcript does not reach him). Pins the directive so it
    can't silently regress + so future depts inherit it."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops(slug="maya"))
    low = out.lower()
    assert "canal vers joris" in low, "missing the explicit 'mon canal vers {{OPERATOR}}' directive"
    assert "@bubbleopsmaya_bot" in out, "the channel directive must name the dept's Telegram bot"
    # must convey 'always via Telegram' + 'transcript doesn't reach him'
    assert "toujours par là" in low or "toujours par la" in low
    assert "transcript" in low


def test_operating_keeps_karpathy_garde_fous():
    """The 5 garde-fous (think-before-act / simplicity / surgical /
    verifiable / scope) are evergreen — must be reused verbatim."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    body_lower = out.lower()
    markers = ["karpathy", "réfléchir", "simplicité",
               "chirurgical", "vérifiable", "périmètre"]
    found = [m for m in markers if m in body_lower]
    assert len(found) >= 4, (
        f"garde-fous section incomplete. Found {found}, expected at "
        f"least 4 of {markers}."
    )


def test_operating_keeps_loop_runtime_protocol():
    """The /loop protocol describes how the dept ticks at runtime — must
    remain in the operating manual. Post msg-3160 refactor, the prose
    moved from STEP A-F to a numbered 1-6 list that delegates to
    dispatch_helpers.decide_dispatch() instead of explaining the
    dispatch tree in long prose. We assert the protocol's essential
    pieces are still present (git pull, heartbeat, commit/push,
    Telegram notify) without locking the test to the old STEP A-F
    surface form."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    body_lower = out.lower()
    essential_markers = [
        "git pull",
        "heartbeat",
        "bubble-git-guard",
        "telegram",
        "decide_dispatch",
    ]
    missing = [m for m in essential_markers if m not in body_lower]
    assert not missing, (
        f"/loop runtime protocol is missing essential markers: {missing}"
    )


def test_operating_keeps_when_stuck_protocol():
    """2h relance / 6h pause is evergreen."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    body = out.lower()
    assert "2h" in body or "2 h" in body, (
        "when-stuck escalation thresholds must remain"
    )
    assert "6h" in body or "6 h" in body


# ---------------------------------------------------------------------------
# Content the operating CLAUDE.md MUST NOT contain
# ---------------------------------------------------------------------------

def test_operating_drops_eclosion_7_step_flow():
    """The 7-step éclosion flow is irrelevant once Live — must be removed."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    body_lower = out.lower()
    # Most distinctive markers from CLAUDE_MD_TEMPLATE's éclosion section
    forbidden = [
        "7 étapes d'éclosion",
        "department-onboarding-guide",
        "ma mission actuelle",  # éclosion driver's mission line
        "m'éclôre moi-même",
        "queued-prompts/initial.md",
        "announce_current_step",
    ]
    leaked = [f for f in forbidden if f in body_lower]
    assert not leaked, (
        f"éclosion-driver content leaked into operating CLAUDE.md: {leaked}"
    )


def test_operating_drops_session_start_hook_reference():
    """SessionStart hook is removed at activation (along with the .claude/
    settings.json hook entry) — operating CLAUDE.md must not mention it."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    assert "SessionStart" not in out, (
        "operating CLAUDE.md still references the éclosion-driver "
        "SessionStart hook"
    )


# ---------------------------------------------------------------------------
# Per-level differentiation
# ---------------------------------------------------------------------------

def test_operating_for_management_mentions_children():
    """Management dept's operating CLAUDE.md must list supervised children."""
    dy = _make_dept_yaml_management(children=("maya", "ben", "miranda"))
    out = scaffold.render_claude_md_operating(dy)
    for child in ("maya", "ben", "miranda"):
        assert child in out, (
            f"management operating CLAUDE.md must mention child '{child}'"
        )


def test_operating_for_ops_does_not_mention_children_section():
    """Ops depts have no children — the children section must be absent
    (not just empty)."""
    dy = _make_dept_yaml_ops()
    out = scaffold.render_claude_md_operating(dy)
    # The management-style children header should not appear for ops.
    assert "supervise" not in out.lower() or "supervisée" in out.lower(), (
        # we allow phrasing like "je suis supervisée par {{OPERATOR}}" — the
        # forbidden one is "je supervise les départements suivants"
        "ops dept must not have the management 'je supervise' section"
    )


# ---------------------------------------------------------------------------
# F. Simplified /loop prose ({{OPERATOR}} msg 3160, 2026-05-25)
# ---------------------------------------------------------------------------
# {{OPERATOR}} wants the /loop section to be short + declarative. Main session
# reads it, scans the 4 layer queues, and delegates to per-layer PROMPT.md
# via the Agent tool. The detailed dispatch logic moved into
# dispatch_helpers.decide_dispatch() — the prose just tells the agent to
# call it (via Bash) and obey the result.


def test_operating_loop_section_is_concise():
    """The simplified /loop block should be much shorter than the old
    71-line STEP A-F prose. Aim for <40 lines of loop-related content
    (msg 3160 — short + declarative)."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    import re
    m = re.search(r"## Mon protocole /loop.*?(?=\n## )", out, re.DOTALL)
    assert m, "operating CLAUDE.md must have a /loop protocol section"
    loop_block = m.group(0)
    n_lines = loop_block.count("\n")
    assert n_lines < 75, (
        f"/loop block is {n_lines} lines — {{OPERATOR}} asked for concise + "
        "declarative (msg 3160) AND for main to verify subagent outputs "
        "(msg 3164). The verify protocol legitimately adds ~25 lines. "
        "Threshold 75 = original 71-line STEP A-F baseline + small "
        "headroom. If you exceed this, push more logic into "
        "dispatch_helpers.py and leave the prose declarative."
    )


def test_operating_loop_section_mentions_decide_dispatch():
    """The simplified /loop tells main to call decide_dispatch() rather
    than re-explaining the dispatch tree in prose."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    assert "decide_dispatch" in out, (
        "Simplified /loop must reference the deterministic helper "
        "instead of duplicating the dispatch tree in prose."
    )


def test_operating_loop_section_mentions_layer_prompt_md():
    """The /loop tells main to pass layers/<N>/PROMPT.md as the task
    description to the spawned subagent."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    assert "layers/" in out and "PROMPT.md" in out, (
        "/loop must instruct main to read layers/<N>/PROMPT.md and pass "
        "it as the Agent tool's task description"
    )


def test_operating_loop_section_mentions_main_is_orchestrator():
    """Per {{OPERATOR}} msg 3157+3160: main session is the orchestrator, layers
    are stateless workers spawned per tick. The prose must make this
    explicit so the agent knows it CAN spawn subagents."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    # At minimum: mention "Agent tool" or "subagent" + "main session"
    out_low = out.lower()
    assert "agent" in out_low and (
        "spawn" in out_low or "subagent" in out_low
    ), "/loop must explain main-session-spawns-subagents-via-Agent-tool"


# ---------------------------------------------------------------------------
# G. Main-session verification of subagent outputs (msg 3164, 2026-05-25)
# ---------------------------------------------------------------------------
# {{OPERATOR}}: "the main loop should also verify the output from the layer
# subagents, check if all ok after retries on error, verify output etc,
# and then move on. Can't hurt to make main session aware of what
# employees do."
#
# The /loop block must instruct main to:
#  - After Agent returns, call validate_layer_output() on the subagent's
#    output dir
#  - If validation failed, re-spawn subagent (up to should_retry's cap)
#  - If still failed after MAX_RETRIES_DEFAULT: escalate (Telegram alert)
#  - Log what each subagent did (high-level summary, not full transcript)
#  - Only THEN move to step 5 (commit/push)


def test_operating_loop_main_verifies_subagent_output():
    """Main must call validate_layer_output() on each subagent's output
    dir before declaring the tick done."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    out_low = out.lower()
    assert "validate_layer_output" in out_low, (
        "/loop must instruct main to call validate_layer_output() on each "
        "subagent's outputs before moving on (msg 3164)."
    )


def test_operating_loop_main_handles_retry_exhaustion():
    """If a subagent fails validation 3x (should_retry cap reached),
    main must escalate to Telegram, not silently continue."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    out_low = out.lower()
    # Look for retry-exhaustion language tied to escalation
    retry_markers = ["retry", "réessai", "reessaye", "retries"]
    escalation_markers = ["escalad", "alerte", "alert", "telegram"]
    assert any(m in out_low for m in retry_markers), (
        "/loop must mention retry behavior on validation failure"
    )
    assert any(m in out_low for m in escalation_markers), (
        "/loop must mention escalation when retries exhausted"
    )


def test_operating_loop_main_logs_subagent_summary():
    """Main must log what each subagent did (high-level) so the tick has
    a coherent narrative — not just 'spawned agent, moved on'."""
    out = scaffold.render_claude_md_operating(_make_dept_yaml_ops())
    out_low = out.lower()
    # The agent must be told to read the subagent's summary.md or
    # logs.jsonl and surface it (in heartbeat.log or Telegram).
    summary_markers = ["summary.md", "résumé", "resume", "lire le résumé",
                        "lire le resume", "read summary"]
    assert any(m in out_low for m in summary_markers), (
        "/loop must instruct main to read the subagent's summary.md "
        "after it returns (msg 3164 — 'make main session aware of what "
        "employees do')"
    )
