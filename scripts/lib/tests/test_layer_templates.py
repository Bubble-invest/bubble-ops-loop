"""test_layer_templates.py — canonical layer PROMPT.md templates."""
from __future__ import annotations

import pytest

from scripts.lib.layer_templates import render_layer_prompt


@pytest.mark.parametrize("n,moment", [
    (1, "Le matin"),
    (2, "recherche"),
    (3, "exécution"),
    (4, "débrief"),
])
def test_each_layer_has_its_moment_title(n, moment):
    t = render_layer_prompt(n, "maya", "Maya")
    assert moment.lower() in t.lower()


def test_display_name_is_filled():
    t = render_layer_prompt(1, "maya", "Maya")
    assert "Maya" in t
    assert "{display_name}" not in t  # no unfilled placeholder


def test_no_unfilled_placeholders():
    for n in (1, 2, 3, 4):
        t = render_layer_prompt(n, "tony", "Tony")
        # crude: no leftover {single_word} placeholders
        import re
        leftover = re.findall(r"\{[a-z_]+\}", t)
        assert leftover == [], f"layer {n} has unfilled placeholders: {leftover}"


def test_invalid_layer_raises():
    with pytest.raises(ValueError):
        render_layer_prompt(5, "maya", "Maya")


def test_l4_includes_notion_logbook_step():
    t = render_layer_prompt(4, "maya", "Maya")
    assert "logbook" in t.lower()
    assert "notion_logbook.py" in t
    assert "Agent Logbook" in t


def test_l4_logbook_uses_dept_slug():
    t = render_layer_prompt(4, "cgp", "Bubble CGP")
    assert "LOGBOOK_AGENT_ID=cgp" in t


def test_l4_logbook_is_noop_safe_without_key():
    t = render_layer_prompt(4, "maya", "Maya")
    # the prompt must tell the agent it's safe to skip without the key
    assert "no key" in t.lower() or "skip" in t.lower()


def test_layers_carry_idempotence_and_round_counter_refs():
    # L4 must reference the round counter; all reference .last-run
    t4 = render_layer_prompt(4, "maya", "Maya")
    assert "round_counter" in t4
    for n in (1, 2, 3, 4):
        t = render_layer_prompt(n, "maya", "Maya")
        assert ".last-run" in t


def test_stateless_subagent_framing_present():
    for n in (1, 2, 3, 4):
        t = render_layer_prompt(n, "maya", "Maya")
        assert "stateless" in t.lower()


def test_overrides_fill_dept_specific_work():
    t = render_layer_prompt(2, "maya", "Maya",
                            overrides={"l2_work": "Compose un DM LinkedIn."})
    assert "Compose un DM LinkedIn." in t
