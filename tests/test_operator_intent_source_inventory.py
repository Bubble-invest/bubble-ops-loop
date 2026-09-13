import hashlib
import json
import subprocess
from pathlib import Path

from tools.operator_intent_source_inventory import (
    build_fleet_inventory,
    build_inventory,
    discover_departments,
    inventory_department,
    load_fleet_roster,
    parse_agent_sources,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_inventory_is_deterministic_and_uses_only_fixed_source_shapes(tmp_path: Path) -> None:
    repo = tmp_path / "bubble-ops-zeta"
    _write(
        repo / "dept.yaml",
        "department:\n  slug: zeta\n  display_name: Zeta\n"
        "recurring_missions:\n  - id: daily\n",
    )
    _write(repo / "MANDATE.md", "# Mandate\nServe operators.\n")
    _write(repo / "missions" / "daily.yaml", "id: daily\n")
    _write(repo / "missions" / "weekly.yml", "id: weekly\n")
    _write(repo / "missions" / "quarterly.md", "# Quarterly mission\n")
    _write(repo / "missions" / "daily" / "PROMPT.md", "# Daily\n")
    _write(repo / "missions" / "daily" / "extra.md", "excluded\n")
    _write(repo / "outputs" / "missions" / "receipt.yaml", "excluded: true\n")
    _write(repo / "secrets" / "mission.yaml", "excluded: true\n")

    first = build_inventory([repo])
    second = build_inventory([repo])

    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    sources = first["departments"][0]["sources"]
    assert [item["path"] for item in sources] == [
        "MANDATE.md",
        "dept.yaml",
        "missions/daily.yaml",
        "missions/daily/PROMPT.md",
        "missions/quarterly.md",
        "missions/weekly.yml",
    ]
    mandate = sources[0]
    expected = hashlib.sha256((repo / "MANDATE.md").read_bytes()).hexdigest()
    assert mandate["sha256"] == expected
    assert first["semantics"] == "unclassified; requires human or agent judgment"


def test_missing_sources_are_reported_without_dropping_department(tmp_path: Path) -> None:
    repo = tmp_path / "bubble-ops-empty"
    repo.mkdir()

    record = inventory_department(repo)

    assert record["department"] == "empty"
    assert record["sources"] == []
    assert record["coverage"] == {
        "mandate": "missing",
        "mission_source_count": 0,
        "mission_file_count": 0,
        "inline_recurring_mission_count": 0,
        "missing": ["MANDATE.md", "missions/*.yaml|*.yml|*.md or missions/*/PROMPT.md"],
    }


def test_discovery_sorts_bare_and_prefixed_department_roots(tmp_path: Path) -> None:
    for name in ("bubble-ops-zeta", "bubble-ops-loop", "bubble-ops-alpha", "bubble-ops-board", "morty"):
        (tmp_path / name).mkdir()

    found = discover_departments(tmp_path)

    assert [path.name for path in found] == ["bubble-ops-alpha", "bubble-ops-zeta", "morty"]


def test_discovery_skips_symlinked_department_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write(outside / "MANDATE.md", "must not be followed\n")
    root = tmp_path / "root"
    root.mkdir()
    (root / "bubble-ops-linked").symlink_to(outside, target_is_directory=True)

    assert discover_departments(root) == []


def test_git_head_is_recorded_when_available(tmp_path: Path) -> None:
    repo = tmp_path / "bubble-ops-alpha"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    _write(repo / "MANDATE.md", "# Mandate\n")
    subprocess.run(["git", "-C", str(repo), "add", "MANDATE.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    expected = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    assert inventory_department(repo)["git_head"] == expected


def test_git_repository_reads_committed_blobs_not_dirty_or_untracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "bubble-ops-alpha"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    committed = "# Committed mandate\n"
    _write(repo / "MANDATE.md", committed)
    subprocess.run(["git", "-C", str(repo), "add", "MANDATE.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)

    _write(repo / "MANDATE.md", "# Dirty mandate\nwith extra lines\n")
    _write(repo / "missions" / "untracked.yaml", "id: untracked\n")
    record = inventory_department(repo)

    assert record["source_snapshot"]["mode"] == "git_commit"
    assert [item["path"] for item in record["sources"]] == ["MANDATE.md"]
    mandate = record["sources"][0]
    assert mandate["sha256"] == hashlib.sha256(committed.encode()).hexdigest()
    assert mandate["lines"] == 1
    assert mandate["provenance_mode"] == "git_commit"


def test_fleet_inventory_keeps_every_roster_agent_and_persona_name(tmp_path: Path) -> None:
    miranda = tmp_path / "bubble-ops-content"
    _write(
        miranda / "dept.yaml",
        "department:\n"
        "  slug: content\n"
        "  display_name: Miranda\n"
        "recurring_missions:\n"
        "  - id: editorial\n",
    )
    _write(miranda / "MANDATE.md", "# Miranda mandate\n")
    _write(miranda / "missions" / "editorial.md", "# Editorial\n")
    agents = [
        {"id": "miranda", "name": "Miranda", "role": "Content", "host": "M1"},
        {"id": "geraldine", "name": "Géraldine", "role": "Accounting", "host": "M5"},
    ]

    inventory = build_fleet_inventory(
        agents,
        {"miranda": miranda, "geraldine": tmp_path / "missing-accountant"},
    )

    assert inventory["fleet_roster_bound"] is True
    assert inventory["summary"]["expected_agent_count"] == 2
    assert inventory["summary"]["agents_without_checkout"] == ["geraldine"]
    assert inventory["summary"]["fleet_coverage_complete"] is False
    records = {item["agent"]["id"]: item for item in inventory["departments"]}
    assert records["miranda"]["agent"]["display_name"] == "Miranda"
    assert records["miranda"]["department"] == "content"
    assert records["miranda"]["standard_shape"]["mandate"] == "present"
    assert records["miranda"]["standard_shape"]["dept_yaml"] == "present"
    assert records["miranda"]["coverage"]["inline_recurring_mission_count"] == 1
    assert records["miranda"]["identity_mismatches"] == []
    assert records["geraldine"]["agent"]["display_name"] == "Géraldine"
    assert records["geraldine"]["coverage"]["mandate"] == "unknown"


def test_fleet_roster_loader_and_agent_source_parser(tmp_path: Path) -> None:
    config = tmp_path / "fleet.yaml"
    _write(
        config,
        "agents:\n"
        "  - id: miranda\n"
        "    name: Miranda\n"
        "    role: Content\n"
        "    host: M1\n",
    )

    assert load_fleet_roster(config) == [
        {"id": "miranda", "name": "Miranda", "role": "Content", "host": "M1"}
    ]
    assert parse_agent_sources(["miranda=/tmp/content"]) == {
        "miranda": Path("/tmp/content")
    }
