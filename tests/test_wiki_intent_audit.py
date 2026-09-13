"""Behavioral tests for the structural wiki intent audit (board #1247)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/cloud-wiki-compile/scripts/wiki_intent_audit.py"
SPEC = importlib.util.spec_from_file_location("wiki_intent_audit", SCRIPT)
assert SPEC and SPEC.loader
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)


def write_page(root: Path, relative: str, frontmatter: str | None) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"---\n{frontmatter}\n---\n" if frontmatter is not None else ""
    path.write_text(prefix + "# Page\n", encoding="utf-8")


def seed_intents(root: Path) -> None:
    write_page(
        root,
        "shared/operator-intents/system-convergence.md",
        "title: System convergence\ncore: true",
    )
    write_page(
        root,
        "shared/operator-intents/human-review.md",
        "title: Human review\ncore: true",
    )
    write_page(
        root,
        "shared/operator-intents/old-direction.md",
        "title: Old direction\nstatus: superseded\ncore: true",
    )


def candidate_by_path(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {item["path"]: item for item in report["candidates"]}


def test_scalar_and_block_list_links_are_structurally_valid(tmp_path: Path) -> None:
    seed_intents(tmp_path)
    write_page(
        tmp_path,
        "shared/systems/scalar.md",
        'title: Scalar\nintent: "[[shared/operator-intents/system-convergence]]"',
    )
    write_page(
        tmp_path,
        "shared/systems/multiple.md",
        "\n".join(
            [
                "title: Multiple",
                "intent:",
                '  - "[[shared/operator-intents/system-convergence]]"',
                '  - "[[shared/operator-intents/human-review]]"',
            ]
        ),
    )

    report = audit_module.audit(tmp_path)

    assert report["summary"]["pages_scanned"] == 2
    assert report["summary"]["pages_structurally_linked"] == 2
    assert report["candidates"] == []


def test_missing_and_explicitly_empty_intents_remain_candidates(tmp_path: Path) -> None:
    seed_intents(tmp_path)
    write_page(tmp_path, "rick_rnd/missing.md", "title: Missing")
    write_page(tmp_path, "rick_rnd/empty.md", "title: Empty\nintent: []")

    candidates = candidate_by_path(audit_module.audit(tmp_path))

    assert candidates["rick_rnd/missing.md"]["issues"] == ["missing_intent"]
    assert candidates["rick_rnd/empty.md"]["issues"] == ["empty_intent"]


def test_audit_reports_shape_and_resolution_without_semantic_guessing(
    tmp_path: Path,
) -> None:
    seed_intents(tmp_path)
    write_page(
        tmp_path,
        "shared/systems/malformed.md",
        "title: Malformed\nintent: system-convergence",
    )
    write_page(
        tmp_path,
        "shared/systems/outside.md",
        'title: Outside\nintent: "[[shared/systems/something]]"',
    )
    write_page(
        tmp_path,
        "shared/systems/missing-target.md",
        'title: Missing target\nintent: "[[shared/operator-intents/does-not-exist]]"',
    )
    write_page(
        tmp_path,
        "shared/systems/unquoted.md",
        "title: Unquoted\nintent: [[shared/operator-intents/system-convergence]]",
    )
    write_page(
        tmp_path,
        "shared/systems/wrong-case.md",
        'title: Wrong case\nintent: "[[shared/operator-intents/System-Convergence]]"',
    )
    write_page(
        tmp_path,
        "shared/systems/superseded.md",
        'title: Superseded\nintent: "[[shared/operator-intents/old-direction]]"',
    )

    candidates = candidate_by_path(audit_module.audit(tmp_path))

    assert candidates["shared/systems/malformed.md"]["issues"] == [
        "malformed_intent",
        "unquoted_intent",
    ]
    assert candidates["shared/systems/outside.md"]["issues"] == [
        "intent_target_outside_core"
    ]
    assert candidates["shared/systems/missing-target.md"]["issues"] == [
        "intent_target_missing"
    ]
    assert candidates["shared/systems/unquoted.md"]["issues"] == [
        "unquoted_intent"
    ]
    assert candidates["shared/systems/wrong-case.md"]["issues"] == [
        "intent_target_missing"
    ]
    assert candidates["shared/systems/superseded.md"]["issues"] == [
        "intent_target_superseded"
    ]
    assert "semantic" not in json.dumps(candidates).lower()


def test_core_roots_and_repo_infrastructure_are_not_children(tmp_path: Path) -> None:
    seed_intents(tmp_path)
    write_page(tmp_path, "hooks/README.md", None)
    write_page(tmp_path, ".github/CONTRIBUTING.md", None)
    write_page(tmp_path, "index.md", "title: Index")

    report = audit_module.audit(tmp_path)

    assert report["summary"]["pages_scanned"] == 1
    assert candidate_by_path(report).keys() == {"index.md"}


def test_cli_atomically_writes_machine_readable_report(tmp_path: Path) -> None:
    seed_intents(tmp_path)
    write_page(tmp_path, "shared/systems/unresolved.md", "title: Unresolved")
    output = tmp_path / "monitoring/latest.json"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--wiki", str(tmp_path), "--output", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["summary"]["candidate_pages"] == 1
    assert "candidates=1" in result.stderr
    assert not list(output.parent.glob(".latest.json.*"))
