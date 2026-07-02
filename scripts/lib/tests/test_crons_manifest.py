"""test_crons_manifest.py — declarative durable-cron manifest load + diff.

Board card #461 (child of #456): `CronCreate durable:true` is session-only
on headless CLI, so a systemd/launchd restart silently loses any durable
cron (Claudette's mail-brief being the concrete failure). Fix: a per-dept
`config/crons.yaml` manifest + a boot-rearm diff step that re-creates
whatever CronList is missing vs the manifest.

Covers the #461 evaluation criteria directly:
  - a manifest with 2 crons -> both come back missing when absent from
    CronList (i.e. the diff correctly flags both for re-arm);
  - idempotent: once live, a second diff against the now-complete CronList
    reports nothing missing (no dupes would be created on re-run);
  - a dept with no manifest file -> no-op (missing=(), nothing to alert on).

TDD: written to pin scripts/lib/crons_manifest.py's public contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.lib.crons_manifest import (
    CronEntry,
    CronsManifest,
    CronsManifestError,
    diff_manifest_against_live,
    load_manifest,
)


# ── load_manifest ────────────────────────────────────────────────────────────

def test_load_manifest_missing_file_is_noop(tmp_path: Path) -> None:
    """A dept with no config/crons.yaml at all -> None (nothing to re-arm)."""
    dept_dir = tmp_path / "some-dept"
    dept_dir.mkdir()
    assert load_manifest(dept_dir) is None


def test_load_manifest_two_crons(tmp_path: Path) -> None:
    dept_dir = tmp_path / "miranda"
    (dept_dir / "config").mkdir(parents=True)
    (dept_dir / "config" / "crons.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "crons": [
                    {
                        "name": "m1_content_scan",
                        "schedule": "0 6 * * *",
                        "prompt_ref": "Run M1.",
                        "description": "daily content scan",
                    },
                    {
                        "name": "loop_self_pace",
                        "schedule": "0 */2 * * *",
                        "prompt_ref": "Resume OODA loop.",
                        "critical": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest(dept_dir)
    assert manifest is not None
    assert manifest.version == 1
    assert len(manifest.crons) == 2
    by_name = manifest.by_name()
    assert set(by_name) == {"m1_content_scan", "loop_self_pace"}
    assert by_name["m1_content_scan"].critical is True   # default
    assert by_name["loop_self_pace"].critical is False


def test_load_manifest_missing_required_field_raises(tmp_path: Path) -> None:
    dept_dir = tmp_path / "broken-dept"
    (dept_dir / "config").mkdir(parents=True)
    (dept_dir / "config" / "crons.yaml").write_text(
        yaml.safe_dump({"crons": [{"name": "x", "schedule": "0 8 * * *"}]}),  # no prompt_ref
        encoding="utf-8",
    )
    with pytest.raises(CronsManifestError, match="prompt_ref"):
        load_manifest(dept_dir)


def test_load_manifest_duplicate_name_raises(tmp_path: Path) -> None:
    dept_dir = tmp_path / "dup-dept"
    (dept_dir / "config").mkdir(parents=True)
    (dept_dir / "config" / "crons.yaml").write_text(
        yaml.safe_dump(
            {
                "crons": [
                    {"name": "x", "schedule": "0 8 * * *", "prompt_ref": "a"},
                    {"name": "x", "schedule": "0 9 * * *", "prompt_ref": "b"},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CronsManifestError, match="duplicate"):
        load_manifest(dept_dir)


def test_load_manifest_not_a_mapping_raises(tmp_path: Path) -> None:
    dept_dir = tmp_path / "weird-dept"
    (dept_dir / "config").mkdir(parents=True)
    (dept_dir / "config" / "crons.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(CronsManifestError, match="mapping"):
        load_manifest(dept_dir)


# ── CronEntry.resolve_prompt ─────────────────────────────────────────────────

def test_resolve_prompt_literal() -> None:
    entry = CronEntry(name="x", schedule="0 8 * * *", prompt_ref="Do the thing.")
    assert entry.resolve_prompt(Path("/nonexistent")) == "Do the thing."


def test_resolve_prompt_file_ref(tmp_path: Path) -> None:
    (tmp_path / "config" / "crons").mkdir(parents=True)
    prompt_file = tmp_path / "config" / "crons" / "mail_brief.md"
    prompt_file.write_text("Full mail-brief prompt text.\n", encoding="utf-8")
    entry = CronEntry(name="mail_brief", schedule="32 6 * * *", prompt_ref="file:config/crons/mail_brief.md")
    assert entry.resolve_prompt(tmp_path) == "Full mail-brief prompt text.\n"


def test_resolve_prompt_file_ref_rejects_path_traversal(tmp_path: Path) -> None:
    """crons.yaml is dept-owned but its content feeds straight into a live
    CronCreate prompt — a file: ref must not escape the dept dir (e.g. to
    read another dept's secrets via ../../).
    """
    dept_dir = tmp_path / "some-dept"
    dept_dir.mkdir()
    secret = tmp_path / "SECRET_OUTSIDE_DEPT.txt"
    secret.write_text("top secret, not this dept's to read\n", encoding="utf-8")

    entry = CronEntry(name="evil", schedule="0 8 * * *", prompt_ref="file:../SECRET_OUTSIDE_DEPT.txt")
    with pytest.raises(CronsManifestError, match="outside the dept dir"):
        entry.resolve_prompt(dept_dir)


def test_resolve_prompt_file_ref_rejects_deeper_traversal(tmp_path: Path) -> None:
    nested_dept_dir = tmp_path / "agents" / "some-dept"
    nested_dept_dir.mkdir(parents=True)
    secret = tmp_path / "SECRET_OUTSIDE_DEPT.txt"
    secret.write_text("still not this dept's to read\n", encoding="utf-8")

    entry = CronEntry(name="evil", schedule="0 8 * * *", prompt_ref="file:../../SECRET_OUTSIDE_DEPT.txt")
    with pytest.raises(CronsManifestError, match="outside the dept dir"):
        entry.resolve_prompt(nested_dept_dir)


# ── diff_manifest_against_live — the #461 evaluation criteria ───────────────

def _two_cron_manifest() -> CronsManifest:
    return CronsManifest(
        crons=(
            CronEntry(name="mail_brief_0832", schedule="32 6 * * *", prompt_ref="brief"),
            CronEntry(name="loop_self_pace", schedule="0 */2 * * *", prompt_ref="loop", critical=False),
        )
    )


def test_diff_both_missing_when_live_cronlist_empty() -> None:
    """#461 eval: 'a manifest with 2 crons -> boot_rearm re-creates both'."""
    diff = diff_manifest_against_live(_two_cron_manifest(), live_names=[])
    assert diff.present == ()
    assert {c.name for c in diff.missing} == {"mail_brief_0832", "loop_self_pace"}
    # only the critical=True one should trigger the (c) alert
    assert [c.name for c in diff.missing_critical] == ["mail_brief_0832"]


def test_diff_idempotent_once_both_are_live() -> None:
    """#461 eval: idempotent — no dupes flagged on re-run once armed."""
    manifest = _two_cron_manifest()
    live_after_rearm = ["mail_brief_0832", "loop_self_pace"]
    diff = diff_manifest_against_live(manifest, live_names=live_after_rearm)
    assert diff.missing == ()
    assert diff.missing_critical == ()
    assert set(diff.present) == {"mail_brief_0832", "loop_self_pace"}


def test_diff_partial_present_only_flags_the_gap() -> None:
    manifest = _two_cron_manifest()
    diff = diff_manifest_against_live(manifest, live_names=["mail_brief_0832"])
    assert diff.present == ("mail_brief_0832",)
    assert [c.name for c in diff.missing] == ["loop_self_pace"]
    assert diff.missing_critical == ()  # the missing one is critical=False


def test_diff_no_manifest_is_noop() -> None:
    """#461 eval: 'a dept with no manifest -> no-op'."""
    diff = diff_manifest_against_live(None, live_names=["whatever"])
    assert diff == diff_manifest_against_live(None, live_names=[])
    assert diff.present == ()
    assert diff.missing == ()
    assert diff.missing_critical == ()


def test_diff_empty_crons_list_is_noop() -> None:
    empty = CronsManifest(crons=())
    diff = diff_manifest_against_live(empty, live_names=["anything"])
    assert diff.present == ()
    assert diff.missing == ()
    assert diff.missing_critical == ()
