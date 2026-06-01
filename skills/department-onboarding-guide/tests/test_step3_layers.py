"""
test_step3_layers.py — Step 3 (Mapping the 4 layers).

Notion v5 lines 847-862. Operator describes what the dept does at each OODA
layer; the skill turns those descriptions into 4 prompt-stub strings that can
be dropped into layers/<N>/PROMPT.md.
"""
from __future__ import annotations

from skill_lib.layers import map_layers, generate_layer_prompt_stub


def test_layer_mapping_produces_4_layer_descriptions(stub_agent_context):
    ctx = stub_agent_context("step3_layers")
    result = map_layers(ctx)
    assert set(result.keys()) == {"layer_1", "layer_2", "layer_3", "layer_4"}
    for key in ("layer_1", "layer_2", "layer_3", "layer_4"):
        assert isinstance(result[key], str)
        # >= 1 sentence ~ at least a period or substantial length.
        assert len(result[key].strip()) >= 10


def test_layer_descriptions_inform_prompt_generation(stub_agent_context):
    """For each of the 4 layers, the description must produce a non-trivial
    PROMPT.md stub that references the layer number AND the dept role."""
    ctx = stub_agent_context("step3_layers")
    mapping = map_layers(ctx)
    for n in (1, 2, 3, 4):
        stub = generate_layer_prompt_stub(layer=n, description=mapping[f"layer_{n}"])
        assert f"Layer {n}" in stub
        # The user-supplied description must appear verbatim (so the stub is
        # actually useful, not boilerplate alone).
        assert mapping[f"layer_{n}"].split(".")[0] in stub
