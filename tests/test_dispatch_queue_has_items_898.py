"""test_dispatch_queue_has_items_898.py — board #898: `_queue_has_items`
must see items inside dated pool subdirs (`queues/research/<YYYY-MM-DD>/`).

Bug (#898): `_queue_has_items` globbed only the queue ROOT (`*.yaml`). Maya's
L1 materializes research items into a dated subdir `queues/research/<today>/`,
so `has_research_items` was always False and content's L2 (6 drafting desks)
never fired despite a full pool.

Fix: also scan ONE level of subdirectories named `YYYY-MM-DD`. Deliberately
NOT every subdirectory — archive layouts (`processed/`, `.processed/`,
`_processed/`) must stay excluded, or processed items would count as pending
and fire-spin the layer forever.

Run: python3 -m pytest tests/test_dispatch_queue_has_items_898.py -q
"""
from __future__ import annotations

import pytest
import yaml
from datetime import datetime, timezone

from scripts.lib import dispatch_helpers


def _item(path, kind="research_item"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"kind": kind, "id": path.stem}),
                    encoding="utf-8")


def test_dated_subdir_item_counts(tmp_path):
    """THE #898 regression: an item in queues/research/<date>/ is seen."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "2026-08-25" / "lead-1.yaml")
    assert dispatch_helpers._queue_has_items(queue) is True


def test_dated_subdir_respects_drainable_kinds(tmp_path):
    """Kind-aware quarantine applies inside dated subdirs too: an orphan
    kind must NOT pin the layer."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "2026-08-25" / "orphan.yaml", kind="discovery_sweep")
    assert dispatch_helpers._queue_has_items(
        queue, drainable_kinds={"research_item"}) is False


def test_root_level_item_still_counts(tmp_path):
    """Pre-existing behaviour unchanged: root-level item counts."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "lead-0.yaml")
    assert dispatch_helpers._queue_has_items(queue) is True


@pytest.mark.parametrize("archive", ["processed", ".processed", "_processed"])
def test_archive_subdirs_do_not_count(tmp_path, archive):
    """Fire-spin guard: processed/archived subdirs stay invisible, even when
    the archived files carry date-shaped names inside them."""
    queue = tmp_path / "queues" / "research"
    _item(queue / archive / "2026-08-25" / "done.yaml")
    assert dispatch_helpers._queue_has_items(queue) is False


def test_non_date_subdir_does_not_count(tmp_path):
    """Only DATE-shaped subdirs are descended into — an arbitrary live-looking
    subdir (e.g. a scratch folder) must not silently become queue surface."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "scratch" / "lead-x.yaml")
    assert dispatch_helpers._queue_has_items(queue) is False


def test_empty_and_missing_dirs_fail_safe(tmp_path):
    queue = tmp_path / "queues" / "research"
    queue.mkdir(parents=True)
    assert dispatch_helpers._queue_has_items(queue) is False
    assert dispatch_helpers._queue_has_items(
        tmp_path / "queues" / "nope") is False


def test_dotfile_only_queue_does_not_count(tmp_path):
    """.gitkeep-style helpers never count (pre-existing rule, kept)."""
    queue = tmp_path / "queues" / "research"
    queue.mkdir(parents=True)
    (queue / ".gitkeep").write_text("", encoding="utf-8")
    assert dispatch_helpers._queue_has_items(queue) is False


def test_build_ctx_sees_dated_pool(tmp_path):
    """End-to-end through build_dispatch_ctx: has_research_items flips True
    once a dated-pool item exists. materialize=False keeps the build
    read-only (context building must never write queue surface)."""
    repo = tmp_path / "dept"
    (repo / "queues" / "research" / "2026-08-25").mkdir(parents=True)
    _item(repo / "queues" / "research" / "2026-08-25" / "lead-9.yaml")

    # Minimal dept tree: no dept.yaml -> the producer-side drainable-kinds
    # lookup fails open to kind-blind counting (by contract).
    ctx = dispatch_helpers.build_dispatch_ctx(
        repo_dir=repo,
        now_utc=datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc),
        materialize=False,
    )
    assert ctx["has_research_items"] is True
