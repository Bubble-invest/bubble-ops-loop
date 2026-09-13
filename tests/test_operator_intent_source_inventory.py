import hashlib
import json
import subprocess
from pathlib import Path

from tools.operator_intent_source_inventory import (
    build_inventory,
    discover_departments,
    inventory_department,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_inventory_is_deterministic_and_uses_only_fixed_source_shapes(tmp_path: Path) -> None:
    repo = tmp_path / "bubble-ops-zeta"
    _write(repo / "MANDATE.md", "# Mandate\nServe operators.\n")
    _write(repo / "missions" / "daily.yaml", "id: daily\n")
    _write(repo / "missions" / "weekly.yml", "id: weekly\n")
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
        "missions/daily.yaml",
        "missions/daily/PROMPT.md",
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
        "missing": ["MANDATE.md", "missions/*.yaml|*.yml or missions/*/PROMPT.md"],
    }


def test_discovery_sorts_departments_and_skips_non_department_repositories(tmp_path: Path) -> None:
    for name in ("bubble-ops-zeta", "bubble-ops-loop", "bubble-ops-alpha", "bubble-ops-board"):
        (tmp_path / name).mkdir()

    found = discover_departments(tmp_path)

    assert [path.name for path in found] == ["bubble-ops-alpha", "bubble-ops-zeta"]


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
