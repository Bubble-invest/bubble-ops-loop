"""
layers.py — Step 3 helpers (mapping operator descriptions to 4-layer prompt stubs).
"""
from __future__ import annotations

from typing import Dict


def map_layers(ctx: Dict[str, str]) -> Dict[str, str]:
    """Pass through the operator-supplied 4-layer descriptions, validating shape."""
    required = {"layer_1", "layer_2", "layer_3", "layer_4"}
    missing = required - set(ctx.keys())
    if missing:
        raise ValueError(f"Layer mapping missing: {sorted(missing)}")
    return {k: ctx[k] for k in required}


def generate_layer_prompt_stub(layer: int, description: str) -> str:
    """
    Produce a layer-N PROMPT.md stub from a 1-sentence description.

    The stub is intentionally minimal: it states the layer, embeds the operator
    description verbatim (so the agent's intent is preserved), and adds the
    standard input/output expectations. The agent enriches this stub later.
    """
    if layer not in (1, 2, 3, 4):
        raise ValueError(f"layer must be 1..4, got {layer}")
    return (
        f"# Layer {layer} prompt stub\n\n"
        f"## Role\n{description}\n\n"
        f"## Input\n"
        f"- queue items in queues/ marked target_layer={layer}\n"
        f"- previous outputs under outputs/<date>/{layer - 1}/ (if any)\n\n"
        f"## Output\n"
        f"- outputs/<date>/{layer}/summary.md (canonical)\n"
        f"- optional next-layer queue items under queues/\n"
    )
