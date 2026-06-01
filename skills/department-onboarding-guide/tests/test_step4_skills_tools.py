"""
test_step4_skills_tools.py — Step 4 (Skills & tools).

Notion v5 lines 863-893. Output:
  - per-layer skill lists (skills.layer_1..4)
  - flat tool list
  - per-entry "card" metadata: purpose, inputs, outputs, status.
"""
from __future__ import annotations

from skill_lib.skills_tools import build_manifest, validate_card


def test_skills_tools_manifest_has_per_layer_lists(stub_agent_context):
    ctx = stub_agent_context("step4_skills_tools")
    manifest = build_manifest(ctx)
    assert set(manifest["skills"].keys()) == {"layer_1", "layer_2", "layer_3", "layer_4"}
    assert manifest["skills"]["layer_2"] == ["post-drafter", "angle-generator"]
    assert isinstance(manifest["tools"], list)
    assert "linkedin-reader" in manifest["tools"]


def test_skills_tools_cards_have_purpose_inputs_outputs_status(stub_agent_context):
    ctx = stub_agent_context("step4_skills_tools")
    card = ctx["cards"]["content-signal-scanner"]
    # validate_card raises if a card is malformed; returns canonical form otherwise.
    canon = validate_card("content-signal-scanner", card)
    assert canon["purpose"] == "detecter des idees de contenu"
    assert canon["inputs"] == ["wiki", "linkedin", "notes"]
    assert canon["outputs"] == ["content_idea_task"]
    assert canon["status"] in ("draft", "tested", "live")
