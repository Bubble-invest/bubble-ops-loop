"""test_dispatch_queue_has_items_898.py — board #898: `_queue_has_items`
must see items inside TODAY's dated pool subdir (`queues/research/<today>/`)
and ONLY today's — never an older dated subdir.

Bug (#898): `_queue_has_items` globbed only the queue ROOT (`*.yaml`). Maya's
L1 materializes research items into a dated subdir `queues/research/<today>/`,
so `has_research_items` was always False and content's L2 (6 drafting desks)
never fired despite a full pool.

First fix attempt (PR #319, 2026-08-25): scan EVERY subdirectory named
`YYYY-MM-DD`, not just today's. Caught in independent review (2026-09-02):
that is only safe if the DRAIN (the L2 desk that actually reads/consumes
research items) also reads every dated subdir. It doesn't — the content
dept's L2 desk prompt (`layers/2/PROMPT.md`) reads ONLY
`queues/research/<today>/*.yaml`, the same `<today>` L1's gatherers write
into (`layers/1/PROMPT.md`). An all-dates COUNT would let a stale,
never-drained OLDER dated subdir pin `has_research_items=True` forever
(counted every tick, read by no desk) — the exact fire-spin class #898 exists
to prevent, just relocated from "kind" to "date".

Fix (this revision): `_queue_has_items` takes an explicit `today` kwarg and
scans ONLY `queue_dir/<today>` (plus the root, unchanged) — matching the
drain's actual scope exactly. `today=None` (the default) scans the root
only, identical to pre-#898 behaviour, so callers with no dated-pool
convention (e.g. inbox/decisions) are unaffected.

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


def test_todays_dated_subdir_item_counts(tmp_path):
    """THE #898 regression: an item in queues/research/<today>/ is seen when
    `today` is passed and matches the subdir name."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "2026-08-25" / "lead-1.yaml")
    assert dispatch_helpers._queue_has_items(queue, today="2026-08-25") is True


def test_no_today_kwarg_does_not_see_dated_subdir(tmp_path):
    """Without `today`, behaviour is exactly pre-#898: dated subdirs are
    invisible (root-only scan). Callers with no dated-pool convention (e.g.
    inbox/decisions) must not regress just because this helper grew the
    dated-subdir capability."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "2026-08-25" / "lead-1.yaml")
    assert dispatch_helpers._queue_has_items(queue) is False


def test_OLD_dated_subdir_never_counted_fire_spin_guard(tmp_path):
    """THE follow-up regression (2026-09-02 review): an item sitting in an
    OLDER dated subdir (not today's) must NOT count, no matter how long it
    has sat there un-drained. Counting it would fire-spin L2 forever, since
    no desk ever reads a subdir other than `<today>`."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "2020-01-01" / "stale-lead.yaml")
    assert dispatch_helpers._queue_has_items(queue, today="2026-08-25") is False


def test_dated_subdir_respects_drainable_kinds(tmp_path):
    """Kind-aware quarantine applies inside today's dated subdir too: an
    orphan kind must NOT pin the layer."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "2026-08-25" / "orphan.yaml", kind="discovery_sweep")
    assert dispatch_helpers._queue_has_items(
        queue, drainable_kinds={"research_item"}, today="2026-08-25") is False


def test_root_level_item_still_counts(tmp_path):
    """Pre-existing behaviour unchanged: root-level item counts regardless of
    `today`."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "lead-0.yaml")
    assert dispatch_helpers._queue_has_items(queue) is True
    assert dispatch_helpers._queue_has_items(queue, today="2026-08-25") is True


@pytest.mark.parametrize("archive", ["processed", ".processed", "_processed"])
def test_archive_subdirs_do_not_count(tmp_path, archive):
    """Fire-spin guard: processed/archived subdirs stay invisible, even when
    the archived files sit under today's date and `today` is passed."""
    queue = tmp_path / "queues" / "research"
    _item(queue / archive / "2026-08-25" / "done.yaml")
    assert dispatch_helpers._queue_has_items(queue, today="2026-08-25") is False


def test_non_date_subdir_does_not_count(tmp_path):
    """An arbitrary live-looking subdir (e.g. a scratch folder) must not
    silently become queue surface, even when `today` is passed."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "scratch" / "lead-x.yaml")
    assert dispatch_helpers._queue_has_items(queue, today="2026-08-25") is False


def test_empty_and_missing_dirs_fail_safe(tmp_path):
    queue = tmp_path / "queues" / "research"
    queue.mkdir(parents=True)
    assert dispatch_helpers._queue_has_items(queue, today="2026-08-25") is False
    assert dispatch_helpers._queue_has_items(
        tmp_path / "queues" / "nope", today="2026-08-25") is False


def test_dotfile_only_queue_does_not_count(tmp_path):
    """.gitkeep-style helpers never count (pre-existing rule, kept)."""
    queue = tmp_path / "queues" / "research"
    queue.mkdir(parents=True)
    (queue / ".gitkeep").write_text("", encoding="utf-8")
    assert dispatch_helpers._queue_has_items(queue, today="2026-08-25") is False


def test_build_ctx_sees_todays_dated_pool(tmp_path):
    """End-to-end through build_dispatch_ctx: has_research_items flips True
    once TODAY's dated-pool item exists. materialize=False keeps the build
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
    assert ctx["today"] == "2026-08-25"
    assert ctx["has_research_items"] is True


def test_build_ctx_does_not_fire_spin_on_old_dated_pool(tmp_path):
    """THE follow-up end-to-end regression: a stale item left over in an
    OLDER dated subdir (from a day the dept never drained) must NOT pin
    has_research_items=True on a LATER tick. Under the all-dates first
    attempt this would have fire-spun L2 forever; under this fix it is
    correctly invisible (no desk will ever read that old subdir either)."""
    repo = tmp_path / "dept"
    (repo / "queues" / "research" / "2020-01-01").mkdir(parents=True)
    _item(repo / "queues" / "research" / "2020-01-01" / "stale-lead.yaml")

    ctx = dispatch_helpers.build_dispatch_ctx(
        repo_dir=repo,
        now_utc=datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc),
        materialize=False,
    )
    assert ctx["today"] == "2026-08-25"
    assert ctx["has_research_items"] is False


def test_mission_input_ready_scopes_to_today(tmp_path):
    """The mission-centric consumer gate (_mission_input_ready) must agree
    with has_research_items: an old dated-subdir item does not make a
    `queues/research/`-consuming mission look ready, but today's does."""
    repo = tmp_path / "dept"
    (repo / "queues" / "research" / "2020-01-01").mkdir(parents=True)
    _item(repo / "queues" / "research" / "2020-01-01" / "stale-lead.yaml")

    ctx = dispatch_helpers.build_dispatch_ctx(
        repo_dir=repo,
        now_utc=datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc),
        materialize=False,
    )
    mission = {"id": "draft_x", "input_queue": "queues/research/"}
    assert dispatch_helpers._mission_input_ready(ctx, mission) is False

    _item(repo / "queues" / "research" / "2026-08-25" / "fresh-lead.yaml")
    ctx2 = dispatch_helpers.build_dispatch_ctx(
        repo_dir=repo,
        now_utc=datetime(2026, 8, 25, 10, 0, 0, tzinfo=timezone.utc),
        materialize=False,
    )
    assert dispatch_helpers._mission_input_ready(ctx2, mission) is True
