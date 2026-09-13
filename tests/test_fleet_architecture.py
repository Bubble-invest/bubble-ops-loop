from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import jsonschema
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "fleet_architecture.py"
SPEC = importlib.util.spec_from_file_location("fleet_architecture", SCRIPT)
assert SPEC and SPEC.loader
fleet = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fleet
SPEC.loader.exec_module(fleet)


NOW = "2026-09-13T00:00:00Z"


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def config_for(root: Path, *, agents: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "agents": agents or [{
            "id": "maya",
            "name": "Maya",
            "role": "Prospection",
            "host": "VPS",
            "wiki_folder": "maya_sales",
            "roots": [str(root)],
        }],
    }


def make_wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    write(wiki / "shared/operator-intents/sales-quality.md", "---\ncore: true\n---\n")
    return wiki


def test_scan_collects_only_allowlisted_architecture_metadata(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    write(root / "skills/prospect-research/SKILL.md", "---\nname: prospect-research\nintent: '[[shared/operator-intents/sales-quality]]'\n---\nSECRET_BODY_SHOULD_NOT_APPEAR\n")
    write(root / "tools/enrich.py", "API_TOKEN = 'never collect source bodies'\n")
    write(root / "missions/discovery/PROMPT.md", "do private work\n")
    write(root / "integrations/linkedin.md", "client name must not be read\n")
    write(root / "config/crons.yaml", "crons:\n  - name: morning-sync\n    schedule: '0 7 * * *'\n    prompt_ref: file:private-prompt.md\n")
    write(root / "systemd/morning.timer", "[Timer]\nOnCalendar=daily\n")
    write(root / "dept.yaml", """
department: {slug: maya}
recurring_missions:
  - {id: qualify, layer: 2, cadence: daily}
skills: {layer_2: [signal-gate]}
tools:
  - name: outreach-ui
    intent: "[[shared/operator-intents/sales-quality]]"
integrations: [crm]
hierarchy: {parent: tony, children: []}
""")

    scanned = fleet.scan_root(config_for(root)["agents"][0], root, NOW)
    by_id = {entry["id"]: entry for entry in scanned["items"]}

    assert set(by_id) >= {
        "skill:prospect-research", "skill:signal-gate", "tool:enrich",
        "tool:outreach-ui", "mission:discovery", "mission:qualify",
        "cron:morning-sync", "cron:morning", "integration:linkedin",
        "integration:crm", "dependency:tony",
    }
    rendered = json.dumps(scanned)
    assert "SECRET_BODY_SHOULD_NOT_APPEAR" not in rendered
    assert "private-prompt" not in rendered
    assert by_id["tool:outreach-ui"]["intent"] == ["[[shared/operator-intents/sales-quality]]"]
    assert scanned["coverage"]["status"] == "verified"


def test_every_configured_agent_remains_visible_when_roots_are_missing(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    previous = {
        "version": 1,
        "agents": [{
            "id": "rick", "name": "Rick", "role": "R&D", "host": "M4", "wiki_folder": "rick_rnd",
            "coverage": {"status": "verified", "last_checked": "2026-09-12T00:00:00Z", "surfaces": {}},
            "items": [{"id": "tool:kanban", "kind": "tool", "name": "kanban", "status": "verified", "source": "rick@M4/tools/kanban", "last_observed": "2026-09-12T00:00:00Z", "intent": [], "details": {}}],
        }],
    }
    config = config_for(missing, agents=[
        {"id": "rick", "name": "Rick", "role": "R&D", "host": "M4", "wiki_folder": "rick_rnd", "roots": [str(missing)]},
        {"id": "ellie", "name": "Ellie", "role": "Developer", "host": "M5", "wiki_folder": "ellie_assistant", "roots": [str(missing / "ellie")]},
    ])

    inventory = fleet.collect(config, previous, {}, NOW)
    by_id = {agent["id"]: agent for agent in inventory["agents"]}

    assert by_id["rick"]["coverage"]["status"] == "stale"
    assert by_id["rick"]["items"][0]["status"] == "stale"
    assert by_id["ellie"]["coverage"]["status"] == "unknown"
    assert all(surface["status"] == "unknown" for surface in by_id["ellie"]["coverage"]["surfaces"].values())


def test_refresh_preserves_reviewed_note_intent_and_validates_schema(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    write(root / "tools/kanban.py", "print('not inspected')\n")
    wiki = make_wiki(tmp_path)
    note = wiki / "shared/fleet-architecture/maya/tools/kanban.md"
    write(note, "---\nowner: fleet-architecture-collector\ntype: fleet-architecture-item\nintent: '[[shared/operator-intents/sales-quality]]'\nintent_rationale: Reviewed link to the sales quality goal.\n---\nold body\n")
    config_path = tmp_path / "sources.yaml"
    write(config_path, yaml.safe_dump(config_for(root), sort_keys=False))

    exit_code = fleet.main(["refresh", "--config", str(config_path), "--wiki-root", str(wiki), "--now", NOW])

    assert exit_code == 0
    state = json.loads((wiki / fleet.STATE_REL).read_text(encoding="utf-8"))
    item = state["agents"][0]["items"][0]
    assert item["intent"] == ["[[shared/operator-intents/sales-quality]]"]
    assert item["intent_rationale"] == "Reviewed link to the sales quality goal."
    assert "Explicit intent" in note.read_text(encoding="utf-8")
    schema = json.loads((ROOT / "fleet/fleet-architecture-inventory.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(state, schema)


def test_invalid_or_missing_intent_target_fails_before_writes(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    write(root / "skills/bad/SKILL.md", "---\nname: bad\nintent: '[[shared/operator-intents/Does-Not-Exist]]'\n---\n")
    wiki = make_wiki(tmp_path)
    config_path = tmp_path / "sources.yaml"
    write(config_path, yaml.safe_dump(config_for(root), sort_keys=False))

    exit_code = fleet.main(["refresh", "--config", str(config_path), "--wiki-root", str(wiki), "--now", NOW])

    assert exit_code == 2
    assert not (wiki / fleet.MAP_REL).exists()
    assert not (wiki / fleet.STATE_REL).exists()


def test_intent_links_are_case_exact_even_on_case_insensitive_hosts(tmp_path: Path) -> None:
    wiki = make_wiki(tmp_path)
    errors = fleet.validate_intents(["[[shared/operator-intents/Sales-Quality]]"], wiki)
    assert errors
    assert "case-exactly" in errors[0]


def test_write_targets_can_never_enter_protected_intent_tree(tmp_path: Path) -> None:
    wiki = make_wiki(tmp_path)
    protected = Path("shared/operator-intents/forbidden.md")
    try:
        fleet.assert_write_targets(wiki, {protected: b"no"})
    except fleet.InventoryError as exc:
        assert "protected operator-intent write" in str(exc)
    else:
        raise AssertionError("protected write was accepted")


def test_source_manifest_names_all_canonical_agents() -> None:
    config = yaml.safe_load((ROOT / "fleet/fleet-architecture-sources.yaml").read_text(encoding="utf-8"))
    assert {agent["id"] for agent in config["agents"]} == {
        "tony", "ben", "maya", "claudette", "morty", "rick", "tonio",
        "deepseek", "miranda", "geraldine", "ellie",
    }


def test_removed_item_is_retained_with_removal_observation(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    tool = write(root / "tools/kanban.py", "print('x')\n")
    config = config_for(root)
    first = fleet.collect(config, {"agents": []}, {}, "2026-09-13T00:00:00Z")
    tool.unlink()

    second = fleet.collect(config, first, {}, "2026-09-13T01:00:00Z")
    removed = second["agents"][0]["items"][0]

    assert removed["id"] == "tool:kanban"
    assert removed["status"] == "removed"
    assert removed["last_observed"] == "2026-09-13T00:00:00Z"
    assert removed["details"]["removed_at"] == "2026-09-13T01:00:00Z"
    assert second["agents"][0]["coverage"]["surfaces"]["tool"]["count"] == 0


def test_refresh_preserves_custom_metadata_body_and_foreign_notes(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    write(root / "tools/kanban.py", "print('x')\n")
    wiki = make_wiki(tmp_path)
    config_path = write(tmp_path / "sources.yaml", yaml.safe_dump(config_for(root), sort_keys=False))
    assert fleet.main(["refresh", "--config", str(config_path), "--wiki-root", str(wiki), "--now", NOW]) == 0
    note = wiki / "shared/fleet-architecture/maya/tools/kanban.md"
    text = note.read_text(encoding="utf-8")
    note.write_text(text.replace("owner: fleet-architecture-collector\n", "owner: fleet-architecture-collector\nreviewed_by: human\n") + "\n## Human context\nKeep this rationale.\n", encoding="utf-8")
    foreign = write(wiki / "shared/fleet-architecture/human-note.md", "---\nowner: human\ntype: note\n---\nDo not delete.\n")

    assert fleet.main(["refresh", "--config", str(config_path), "--wiki-root", str(wiki), "--now", NOW]) == 0

    refreshed = note.read_text(encoding="utf-8")
    assert "reviewed_by: human" in refreshed
    assert "## Human context\nKeep this rationale." in refreshed
    assert refreshed.count(fleet.MANAGED_START) == 1
    assert foreign.read_text(encoding="utf-8").endswith("Do not delete.\n")


def test_check_reports_only_owned_pending_removals(tmp_path: Path, capsys) -> None:
    root = tmp_path / "agent"
    (root / "tools").mkdir(parents=True)
    wiki = make_wiki(tmp_path)
    config_path = write(tmp_path / "sources.yaml", yaml.safe_dump(config_for(root), sort_keys=False))
    assert fleet.main(["refresh", "--config", str(config_path), "--wiki-root", str(wiki), "--now", NOW]) == 0
    capsys.readouterr()
    orphan = write(
        wiki / "shared/fleet-architecture/maya/tools/old-owned.md",
        "---\nowner: fleet-architecture-collector\ntype: fleet-architecture-item\n---\nold\n",
    )
    foreign = write(wiki / "shared/fleet-architecture/maya/tools/human.md", "---\nowner: human\n---\nkeep\n")

    code = fleet.main(["refresh", "--config", str(config_path), "--wiki-root", str(wiki), "--now", NOW, "--check"])
    report = capsys.readouterr().out

    assert code == 1
    assert "old-owned.md" in report
    assert "human.md" not in report
    assert orphan.exists() and foreign.exists()


def test_empty_root_is_unknown_but_declared_empty_surface_is_verified(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    unknown = fleet.scan_root(config_for(empty)["agents"][0], empty, NOW)
    assert unknown["coverage"]["status"] == "unknown"
    assert all(value["status"] == "unknown" for value in unknown["coverage"]["surfaces"].values())

    (empty / "tools").mkdir()
    partial = fleet.scan_root(config_for(empty)["agents"][0], empty, NOW)
    assert partial["coverage"]["status"] == "partial"
    assert partial["coverage"]["surfaces"]["tool"] == {"status": "verified", "count": 0}
    assert partial["coverage"]["surfaces"]["skill"]["status"] == "unknown"


def test_refresh_discovers_cross_host_fragment_and_does_not_redate_it(tmp_path: Path) -> None:
    source = tmp_path / "mac-agent"
    write(source / "tools/kanban.py", "print('x')\n")
    fragments = tmp_path / "fragments"
    config = config_for(source)
    config["fragment_dirs"] = [str(fragments)]
    config["fragment_max_age_hours"] = 36
    config_path = write(tmp_path / "sources.yaml", yaml.safe_dump(config, sort_keys=False))
    assert fleet.main(["scan-visible", "--config", str(config_path), "--host", "VPS", "--output-dir", str(fragments), "--now", NOW]) == 0
    source.rename(tmp_path / "mac-agent-offline")
    wiki = make_wiki(tmp_path)

    assert fleet.main(["refresh", "--config", str(config_path), "--wiki-root", str(wiki), "--now", "2026-09-13T01:00:00Z"]) == 0
    state = json.loads((wiki / fleet.STATE_REL).read_text(encoding="utf-8"))
    agent = state["agents"][0]
    assert agent["coverage"]["last_checked"] == NOW
    assert agent["items"][0]["last_observed"] == NOW
    assert agent["items"][0]["id"] == "tool:kanban"


def test_fragment_host_must_match_configured_agent_host(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    config = config_for(missing)
    fragment = {
        "id": "maya", "name": "Maya", "role": "Prospection", "host": "Jade Mac M1", "wiki_folder": "maya_sales",
        "coverage": {"status": "unknown", "last_checked": NOW, "surfaces": {kind: {"status": "unknown", "count": 0} for kind in fleet.KINDS}},
        "items": [],
    }
    try:
        fleet.collect(config, {"agents": []}, {"maya": fragment}, NOW)
    except fleet.InventoryError as exc:
        assert "does not match configured host" in str(exc)
    else:
        raise AssertionError("cross-host fragment was trusted")


def test_old_fragment_is_consumed_as_stale_without_redating_items(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    config = config_for(missing)
    config["fragment_max_age_hours"] = 36
    fragment = {
        "id": "maya", "name": "Maya", "role": "Prospection", "host": "VPS", "wiki_folder": "maya_sales",
        "coverage": {"status": "partial", "last_checked": NOW, "surfaces": {kind: {"status": "unknown", "count": 0} for kind in fleet.KINDS}},
        "items": [{"id": "tool:kanban", "kind": "tool", "name": "kanban", "status": "verified", "source": "maya@vps/tools/kanban.py", "last_observed": NOW, "intent": [], "intent_rationale": "", "details": {}}],
    }
    result = fleet.collect(config, {"agents": []}, {"maya": fragment}, "2026-09-15T00:00:00Z")
    agent = result["agents"][0]
    assert agent["coverage"]["status"] == "stale"
    assert agent["items"][0]["status"] == "stale"
    assert agent["items"][0]["last_observed"] == NOW


def test_core_item_note_is_never_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "agent"
    write(root / "tools/kanban.py", "print('x')\n")
    wiki = make_wiki(tmp_path)
    note = write(
        wiki / "shared/fleet-architecture/maya/tools/kanban.md",
        "---\nowner: fleet-architecture-collector\ntype: fleet-architecture-item\ncore: true\n---\nprotected\n",
    )
    before = note.read_text(encoding="utf-8")
    config_path = write(tmp_path / "sources.yaml", yaml.safe_dump(config_for(root), sort_keys=False))
    assert fleet.main(["refresh", "--config", str(config_path), "--wiki-root", str(wiki), "--now", NOW]) == 2
    assert note.read_text(encoding="utf-8") == before
