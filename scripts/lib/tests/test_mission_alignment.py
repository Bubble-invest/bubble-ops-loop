import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from scripts.lib.mission_alignment import alignment_report, inspect_department, read_intent_slugs
from scripts.lib.loop_backup import due_mission_plan
from scripts.mission_alignment_lint import scan

ROOT = Path(__file__).resolve().parents[3]


def test_statuses_and_mission_counts():
    missions = [
        {"id": "legacy"},
        {"id": "mapped", "business_unit": "fund", "serves_intents": ["growth"]},
        {"id": "unknown", "business_unit": "steering", "serves_intents": ["bad", "worse"]},
        {"id": "unit", "business_unit": ["fund"], "serves_intents": ["growth"]},
        {"id": "shape", "serves_intents": "growth", "floor": {}},
        {"id": "partial", "business_unit": "fund", "serves_intents": []},
    ]
    result = alignment_report([("dept", {"recurring_missions": missions})], {"growth"})
    assert result["totals"] == dict(mapped=1, unmapped=3, unknown_intents=1,
                                    invalid_business_unit=1, unverified=0, invalid_metadata=2)
    assert result["departments"][0]["missions"][2]["unknown_intents"] == ["bad", "worse"]


def test_missing_catalog_does_not_claim_unknown_or_mapped(tmp_path):
    assert read_intent_slugs(tmp_path / "missing") is None
    result = inspect_department("dept", {"recurring_missions": [
        {"business_unit": "ai_methods", "serves_intents": ["growth"]}]}, None)
    assert result["unverified"] == 1
    assert result["mapped"] == result["unknown_intents"] == 0


@pytest.mark.parametrize("manifest", [None, [], {"recurring_missions": {}}, {"recurring_missions": [None]}])
def test_malformed_manifest_is_diagnostic(manifest):
    assert inspect_department("dept", manifest, set())["errors"]


def test_cli_exit_zero_with_findings_and_bad_yaml(tmp_path):
    intents = tmp_path / "intents"
    intents.mkdir()
    (intents / "growth.md").write_text("intent")
    (intents / "ignored.txt").write_text("not an intent")
    depts = tmp_path / "depts"
    for name, content in {"good": "recurring_missions:\n- id: legacy\n", "bad": "[broken"}.items():
        directory = depts / name
        directory.mkdir(parents=True)
        (directory / "dept.yaml").write_text(content)
    args = [sys.executable, str(ROOT / "scripts/mission_alignment_lint.py"),
            "--depts-root", str(depts), "--intents-dir", str(intents)]
    proc = subprocess.run(args + ["--json"], capture_output=True, text=True)
    assert proc.returncode == 0
    report = json.loads(proc.stdout)
    assert report == scan(depts, intents)
    assert report["totals"]["unmapped"] == 1
    assert report["departments"][0]["errors"]
    assert read_intent_slugs(intents) == {"growth"}
    assert "good/legacy: unmapped" in subprocess.check_output(args, text=True)
    assert scan(tmp_path / "missing", intents)["errors"]


@pytest.mark.parametrize("inline", [True, False])
def test_schema_backward_compat_and_optional_fields(inline):
    schema = yaml.safe_load((ROOT / "schemas-draft" / (
        "dept.schema.yaml" if inline else "recurring-mission.schema.yaml")).read_text())
    if inline:
        schema = schema["properties"]["recurring_missions"]["items"]
    mission = dict(id="daily_scan", layer=1, cadence="hourly", description="Scan the daily inputs",
                   output_queue="queues/data/", creates=["scan"])
    jsonschema.validate(mission, schema)
    mission.update(business_unit="pro_clients", serves_intents=["growth"], floor="produce")
    jsonschema.validate(mission, schema)
    for key, value in [("business_unit", "bogus"), ("serves_intents", "growth"), ("floor", "bogus")]:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(dict(mission, **{key: value}), schema)


def test_alignment_metadata_does_not_change_due_dispatch():
    mission = dict(id="scan", layer=1, cadence="continuous", status="live",
                   due={"policy": "every_tick"}, mission_file="missions/scan.md")
    manifest = {"loop": {"due_dispatch": {"mission_ids": ["scan"],
                "watermark": "monitoring/due-mission-watermarks.json"}}, "recurring_missions": [mission]}
    now = dt.datetime(2026, 9, 28, 12, tzinfo=dt.timezone.utc)
    before = due_mission_plan(manifest, {}, now)
    mission.update(business_unit="fund", serves_intents=["growth"], floor="measure")
    assert due_mission_plan(manifest, {}, now) == before
    mission.update(business_unit="invalid", serves_intents=None)
    assert due_mission_plan(manifest, {}, now) == before


def test_scaffold_retains_declared_alignment():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(
        ROOT / "skills/department-onboarding-guide/templates"))
    mission = dict(id="scan", layer=1, cadence="hourly", description="Scan daily inputs",
                   output_queue="queues/data/", creates=["scan"], business_unit="fund",
                   serves_intents=["growth"], floor="measure")
    rendered = yaml.safe_load(env.get_template("mission.yaml.template").render(mission=mission))
    for key in ("business_unit", "serves_intents", "floor"):
        assert rendered[key] == mission[key]
