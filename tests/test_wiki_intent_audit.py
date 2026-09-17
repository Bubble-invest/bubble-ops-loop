"""Behavioral tests for the structural wiki intent audit (board #1247)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath


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
        "operator-intents/system-convergence.md",
        "title: System convergence\ncore: true",
    )
    write_page(
        root,
        "operator-intents/human-review.md",
        "title: Human review\ncore: true",
    )
    write_page(
        root,
        "operator-intents/old-direction.md",
        "title: Old direction\nstatus: superseded\ncore: true",
    )


def candidate_by_path(report: dict[str, object]) -> dict[str, dict[str, object]]:
    return {item["path"]: item for item in report["candidates"]}


def test_scalar_and_block_list_links_are_structurally_valid(tmp_path: Path) -> None:
    wiki, mirror = tmp_path / "wiki", tmp_path / "mirror"
    seed_intents(mirror)
    write_page(
        wiki,
        "shared/systems/scalar.md",
        'title: Scalar\nintent: "[[shared/operator-intents/system-convergence]]"',
    )
    write_page(
        wiki,
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

    report = audit_module._audit_validated_release_for_tests(wiki, mirror)

    assert report["summary"]["pages_scanned"] == 2
    assert report["summary"]["pages_structurally_linked"] == 2
    assert report["candidates"] == []


def test_missing_and_explicitly_empty_intents_remain_candidates(tmp_path: Path) -> None:
    wiki, mirror = tmp_path / "wiki", tmp_path / "mirror"
    seed_intents(mirror)
    write_page(wiki, "rick_rnd/missing.md", "title: Missing")
    write_page(wiki, "rick_rnd/empty.md", "title: Empty\nintent: []")

    candidates = candidate_by_path(
        audit_module._audit_validated_release_for_tests(wiki, mirror)
    )

    assert candidates["rick_rnd/missing.md"]["issues"] == ["missing_intent"]
    assert candidates["rick_rnd/empty.md"]["issues"] == ["empty_intent"]


def test_audit_reports_shape_and_resolution_without_semantic_guessing(
    tmp_path: Path,
) -> None:
    wiki, mirror = tmp_path / "wiki", tmp_path / "mirror"
    seed_intents(mirror)
    write_page(
        wiki,
        "shared/systems/malformed.md",
        "title: Malformed\nintent: system-convergence",
    )
    write_page(
        wiki,
        "shared/systems/outside.md",
        'title: Outside\nintent: "[[shared/systems/something]]"',
    )
    write_page(
        wiki,
        "shared/systems/missing-target.md",
        'title: Missing target\nintent: "[[shared/operator-intents/does-not-exist]]"',
    )
    write_page(
        wiki,
        "shared/systems/unquoted.md",
        "title: Unquoted\nintent: [[shared/operator-intents/system-convergence]]",
    )
    write_page(
        wiki,
        "shared/systems/wrong-case.md",
        'title: Wrong case\nintent: "[[shared/operator-intents/System-Convergence]]"',
    )
    write_page(
        wiki,
        "shared/systems/superseded.md",
        'title: Superseded\nintent: "[[shared/operator-intents/old-direction]]"',
    )

    candidates = candidate_by_path(
        audit_module._audit_validated_release_for_tests(wiki, mirror)
    )

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
    wiki, mirror = tmp_path / "wiki", tmp_path / "mirror"
    seed_intents(mirror)
    write_page(wiki, "hooks/README.md", None)
    write_page(wiki, ".github/CONTRIBUTING.md", None)
    write_page(wiki, "index.md", "title: Index")

    report = audit_module._audit_validated_release_for_tests(wiki, mirror)

    assert report["summary"]["pages_scanned"] == 1
    assert candidate_by_path(report).keys() == {"index.md"}


def test_archive_and_proposals_segments_are_excluded_from_the_leak_check(
    tmp_path: Path,
) -> None:
    """Board #1265: archived/staged content is not live and needs no intent
    link, so it must never surface as a candidate — while a genuinely
    untagged LIVE page is still flagged (regression guard against a change
    that silences everything)."""

    wiki, mirror = tmp_path / "wiki", tmp_path / "mirror"
    seed_intents(mirror)
    write_page(wiki, "_archive/HANDOFF-old-thing.md", "title: Old handoff")
    write_page(
        wiki, "_archive/legacy/nested/hot.md", "title: Nested archive page"
    )
    write_page(wiki, "proposals/new-intent-draft.md", "title: Draft proposal")
    write_page(
        wiki, "rick_rnd/proposals/nested-draft.md", "title: Nested proposal"
    )
    # The REAL on-disk staging area (not the literal "proposals/" placeholder
    # above) — must also be excluded. Caught by adversarial review: an earlier
    # version of this fix only matched a literal "proposals" segment, which is
    # a no-op against the actual directory name.
    write_page(
        wiki,
        "shared/operator-intents-proposals/rnd.md",
        "title: R&D proposals",
    )
    # Must NOT be excluded: these only resemble the excluded segments.
    write_page(wiki, "shared/systems/archived-notes.md", "title: Archived notes")
    write_page(wiki, "notes/not_archive.md", "title: Not archive")
    write_page(wiki, "notes/proposals-overview.md", "title: Proposals overview")
    # A genuinely untagged LIVE page: must still be flagged.
    write_page(wiki, "rick_rnd/live-orphan.md", "title: Live orphan")

    report = audit_module._audit_validated_release_for_tests(wiki, mirror)
    scanned_paths = {c["path"] for c in report["candidates"]}

    for excluded in (
        "_archive/HANDOFF-old-thing.md",
        "_archive/legacy/nested/hot.md",
        "proposals/new-intent-draft.md",
        "rick_rnd/proposals/nested-draft.md",
        "shared/operator-intents-proposals/rnd.md",
    ):
        assert excluded not in scanned_paths, excluded

    for still_live in (
        "shared/systems/archived-notes.md",
        "notes/not_archive.md",
        "notes/proposals-overview.md",
        "rick_rnd/live-orphan.md",
    ):
        assert still_live in scanned_paths, still_live

    assert report["candidates"]  # never silences everything


def test_is_glob_excluded_matches_segment_not_substring() -> None:
    globs = audit_module.EXCLUDED_PATH_GLOBS
    assert globs == ("_archive/**", "proposals/**", "operator-intents-proposals/**")

    excluded = (
        "_archive/x.md",
        "a/_archive/b/x.md",
        "proposals/x.md",
        "a/proposals/b/c.md",
        "shared/operator-intents-proposals/x.md",
    )
    for path in excluded:
        assert audit_module._is_glob_excluded(PurePosixPath(path), globs), path

    not_excluded = (
        "archived-notes/x.md",
        "not_archive.md",
        "proposals-overview.md",
        "notes/proposals.md",  # a FILE named proposals.md, not the dir
    )
    for path in not_excluded:
        assert not audit_module._is_glob_excluded(PurePosixPath(path), globs), path


def test_internal_report_can_be_atomically_written(tmp_path: Path) -> None:
    wiki, mirror = tmp_path / "wiki", tmp_path / "mirror"
    seed_intents(mirror)
    write_page(wiki, "shared/systems/unresolved.md", "title: Unresolved")
    output = tmp_path / "monitoring/latest.json"

    report = audit_module._audit_validated_release_for_tests(wiki, mirror)
    audit_module._atomic_write(output, json.dumps(report) + "\n")

    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["schema_version"] == 1
    assert written["summary"]["candidate_pages"] == 1
    assert not list(output.parent.glob(".latest.json.*"))


def test_operational_cli_accepts_a_plain_readable_intents_directory(
    tmp_path: Path,
) -> None:
    """Board #1333 (Option C): the isolated, root-owned, filesystem-immutable
    mirror (#430/#1267) is retired for this consumer. The tamper guarantee is
    now the git-level branch-hook (#12), not filesystem immutability, so the
    operational CLI must accept an ordinary, writable directory — exactly the
    shape of the wiki's own shared/operator-intents/ — with no symlink, no
    root ownership, no 0555/manifest requirement."""
    wiki, mirror = tmp_path / "wiki", tmp_path / "mirror"
    seed_intents(mirror)
    write_page(wiki, "shared/systems/unresolved.md", "title: Unresolved")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--wiki",
            str(wiki),
            "--intents-root",
            str(mirror),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "not a stable symlink" not in result.stderr
    assert "wiki_intent_audit: scanned=1 linked=0 candidates=1" in result.stderr


def test_operational_cli_rejects_a_directory_with_no_intents_collection(
    tmp_path: Path,
) -> None:
    """The lenient #1333 contract still rejects obvious garbage: a directory
    that exists but has no operator-intents/ subdirectory at all, or one whose
    operator-intents/ collection is empty, is not a usable intents source."""
    wiki = tmp_path / "wiki"
    write_page(wiki, "shared/systems/unresolved.md", "title: Unresolved")

    no_collection = tmp_path / "empty-root"
    no_collection.mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--wiki", str(wiki), "--intents-root", str(no_collection)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "has no operator-intents/ collection" in result.stderr

    empty_collection = tmp_path / "empty-collection"
    (empty_collection / "operator-intents").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--wiki", str(wiki), "--intents-root", str(empty_collection)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "operator-intents/ collection is empty" in result.stderr

    missing_root = tmp_path / "does-not-exist"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--wiki", str(wiki), "--intents-root", str(missing_root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "intents root is not a directory" in result.stderr


def test_wiki_own_operator_intents_dir_is_accepted_and_used_end_to_end(
    tmp_path: Path,
) -> None:
    """Board #1333's actual production shape: --intents-root pointed straight
    at the wiki's own shared/ directory (so operator-intents/ resolves to
    shared/operator-intents/, exactly like cloud-wiki-compile.sh's new
    default `${WIKI_DIR}/shared`), with a page elsewhere in the same wiki
    checkout linking to one of those intents. Proves the wiki-dir intents
    source is accepted AND actually used for resolution — not just accepted
    and ignored."""
    wiki = tmp_path / "wiki"
    write_page(
        wiki,
        "shared/operator-intents/system-convergence.md",
        "title: System convergence\ncore: true",
    )
    write_page(
        wiki,
        "shared/systems/scalar.md",
        'title: Scalar\nintent: "[[shared/operator-intents/system-convergence]]"',
    )
    write_page(wiki, "rick_rnd/missing.md", "title: Missing")
    output = tmp_path / "latest.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--wiki",
            str(wiki),
            "--intents-root",
            str(wiki / "shared"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    report = json.loads(output.read_text(encoding="utf-8"))
    candidates = candidate_by_path(report)
    assert "shared/systems/scalar.md" not in candidates  # resolved via the wiki dir
    assert candidates["rick_rnd/missing.md"]["issues"] == ["missing_intent"]


def test_intent_target_resolves_only_against_the_passed_intents_root(
    tmp_path: Path,
) -> None:
    """Regression guard, still meaningful post-#1333: resolution always uses
    the specific --intents-root a caller passes — never an implicit fallback
    scan of the whole wiki checkout — even though under the new default that
    root is itself a subdirectory of the wiki. A page written directly under
    the WIKI's shared/operator-intents/ does not count as resolved unless the
    caller actually pointed --intents-root there."""
    wiki, mirror = tmp_path / "wiki", tmp_path / "mirror"
    seed_intents(mirror)
    write_page(
        wiki,
        "shared/operator-intents/wiki-only.md",
        "title: Unapproved writable copy\ncore: true",
    )
    write_page(
        wiki,
        "shared/systems/page.md",
        'title: Page\nintent: "[[shared/operator-intents/wiki-only]]"',
    )
    report = audit_module._audit_validated_release_for_tests(wiki, mirror)
    assert candidate_by_path(report)["shared/systems/page.md"]["issues"] == [
        "intent_target_missing"
    ]
