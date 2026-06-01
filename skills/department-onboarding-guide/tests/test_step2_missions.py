"""
test_step2_missions.py — Step 2 (Recurring missions).

Notion v5 lines 830-846. Each mission must validate against
recurring-mission.schema.yaml. Cadence patterns supported per dept.schema.yaml
line 111: daily / weekly / hourly / every_Nh / every_Nm / cron:...
"""
from __future__ import annotations

import yaml

from skill_lib.templates import render_template


def test_mission_template_renders_with_cadence_layer_creates(stub_agent_context, schema_validator):
    ctx = stub_agent_context("step2_missions")
    # Render the FIRST mission only via the single-mission template.
    rendered = render_template("mission.yaml", {"mission": ctx["missions"][0]})
    mission = yaml.safe_load(rendered)
    v = schema_validator("recurring-mission")
    errors = sorted(v.iter_errors(mission), key=lambda e: e.path)
    assert not errors, [e.message for e in errors]
    assert mission["layer"] == 1
    assert mission["cadence"] == "daily"
    assert mission["creates"] == ["content_idea_task"]


def test_mission_template_supports_all_cadence_forms(schema_validator):
    """All 6 cadence shapes must render schema-valid."""
    cases = [
        {
            "id": "m_daily",
            "layer": 1,
            "cadence": "daily",
            "time": "07:00",
            "description": "Daily test mission.",
            "output_queue": "queues/research/",
            "creates": ["t"],
        },
        {
            "id": "m_weekly",
            "layer": 1,
            "cadence": "weekly",
            "day": "monday",
            "time": "07:00",
            "description": "Weekly test mission.",
            "output_queue": "queues/research/",
            "creates": ["t"],
        },
        {
            "id": "m_hourly",
            "layer": 1,
            "cadence": "hourly",
            "active_hours": "08:00-22:00",
            "description": "Hourly test mission.",
            "output_queue": "queues/research/",
            "creates": ["t"],
        },
        {
            "id": "m_every_n_h",
            "layer": 1,
            "cadence": "every_2h",
            "description": "Every-Nh test mission.",
            "output_queue": "queues/research/",
            "creates": ["t"],
        },
        {
            "id": "m_every_n_m",
            "layer": 1,
            "cadence": "every_30m",
            "description": "Every-Nm test mission.",
            "output_queue": "queues/research/",
            "creates": ["t"],
        },
        {
            "id": "m_cron",
            "layer": 1,
            "cadence": "cron:0 9 * * 1-5",
            "description": "Cron-escape-hatch test mission.",
            "output_queue": "queues/research/",
            "creates": ["t"],
        },
    ]
    v = schema_validator("recurring-mission")
    for case in cases:
        rendered = render_template("mission.yaml", {"mission": case})
        loaded = yaml.safe_load(rendered)
        errors = sorted(v.iter_errors(loaded), key=lambda e: e.path)
        assert not errors, f"cadence {case['cadence']}: {[e.message for e in errors]}"
