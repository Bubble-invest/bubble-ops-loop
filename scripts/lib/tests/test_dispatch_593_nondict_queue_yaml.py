"""Tests for #593 — materialize_due_missions_for_tick crashes on a non-dict
top-level queue YAML document.

LIVE bug (verified on the VPS, not re-litigated here): the idempotency
queue-scan inside materialize_due_missions_for_tick does

    existing_data = yaml.safe_load(existing.read_text(encoding="utf-8")) or {}
    ...
    if existing_data.get("mission_id") == mid:

`or {}` only guards a falsy load (None/empty). A non-empty top-level LIST is
truthy, so it passes through, and `.get()` then raises
`AttributeError: 'list' object has no attribute 'get'`, killing the entire
dispatch tick before any mission fires. Trigger: two research items written
as a single-element top-level list by an L4 subagent, landing in
queues/research/.

Fix: guard the scan with `isinstance(existing_data, dict)` so a non-dict
document is treated like any other unreadable item — skipped via the same
skip-and-continue behavior the adjacent `except Exception: continue` already
establishes as the intended pattern for a malformed queue item.
"""
from __future__ import annotations

import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.lib.dispatch_helpers import (  # noqa: E402
    materialize_due_missions_for_tick,
    read_last_run,
)

# L2-eligible anchor (Paris CEST = UTC+2; 12:30 Paris = 10:30 UTC) — matches
# the convention used by the sibling #442 test file in this same directory.
_NOW = datetime(2026, 6, 21, 10, 30, 0, tzinfo=timezone.utc)
_TODAY = _NOW.strftime("%Y-%m-%d")


def _make_dept_yaml(repo: Path, missions: list[dict]) -> None:
    (repo / "dept.yaml").write_text(
        yaml.dump({"recurring_missions": missions}, allow_unicode=True,
                  default_flow_style=False),
        encoding="utf-8",
    )


def _today_dir(repo: Path) -> Path:
    td = repo / "outputs" / _TODAY
    td.mkdir(parents=True, exist_ok=True)
    return td


def test_list_shaped_queue_yaml_does_not_crash_the_tick(tmp_path: Path):
    """A queues/research/ dir containing a top-level-LIST YAML document must
    not raise AttributeError — it should be skipped like any other malformed
    item, and the tick must complete.

    FAILS on main: yaml.safe_load(...) or {} lets the truthy list through,
    then .get("mission_id") raises AttributeError('list' object has no
    attribute 'get'), which propagates out of materialize_due_missions_for_tick
    and kills the tick before any mission is materialized.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {
            "id": "content_daily_rotation",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/research/",
            "creates": ["idea_item"],
        }
    ])
    today_dir = _today_dir(repo)
    research_dir = repo / "queues" / "research"
    research_dir.mkdir(parents=True, exist_ok=True)

    # The exact poison shape from the live incident: an L4 subagent wrote two
    # research items as a single-element top-level LIST instead of a dict.
    poison = research_dir / "poison-list-item.yaml"
    poison.write_text(
        yaml.dump(
            [{"id": "some-item", "kind": "idea_item", "created_at": "2026-06-21T09:00:00+00:00"}],
            allow_unicode=True,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )

    # Must not raise.
    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    # The well-formed mission must still be processed despite the poison item
    # sitting alongside it in the same queue dir.
    assert len(created) == 1
    assert created[0]["mission_id"] == "content_daily_rotation"
    assert read_last_run(today_dir / "missions" / "content_daily_rotation") is not None

    # The poison item itself is left untouched — skipped, never repaired or
    # deleted (that's a separate concern from this fix).
    assert poison.exists()
    assert yaml.safe_load(poison.read_text(encoding="utf-8")) == [
        {"id": "some-item", "kind": "idea_item", "created_at": "2026-06-21T09:00:00+00:00"}
    ]


def test_wellformed_items_alongside_poison_item_still_dedupe(tmp_path: Path):
    """A well-formed EXISTING queue item (dict, matching mission_id) sitting
    next to a poison list-shaped item must still be found by the idempotency
    scan — the fix must not blind the scan to legitimate dict items, only
    skip the non-dict one."""
    repo = tmp_path / "repo"
    repo.mkdir()

    _make_dept_yaml(repo, [
        {
            "id": "content_daily_rotation",
            "layer": 2,
            "cadence": "daily",
            "time": "09:00",
            "output_queue": "queues/research/",
            "creates": ["idea_item"],
        }
    ])
    today_dir = _today_dir(repo)
    research_dir = repo / "queues" / "research"
    research_dir.mkdir(parents=True, exist_ok=True)

    # Poison list-shaped item.
    (research_dir / "poison-list-item.yaml").write_text(
        yaml.dump([{"id": "x"}], allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    # Legitimate existing item already queued for this mission_id.
    (research_dir / "existing-item.yaml").write_text(
        yaml.dump(
            {
                "id": "idea_item-content_daily_rotation-20260621-083000",
                "mission_id": "content_daily_rotation",
                "kind": "idea_item",
                "created_at": "2026-06-21T08:30:00+00:00",
                "created_by": "materialize_due_missions",
            },
            allow_unicode=True,
            default_flow_style=False,
        ),
        encoding="utf-8",
    )

    created = materialize_due_missions_for_tick(repo, today_dir, _NOW)

    # Already queued (idempotent dedup found the real dict item) → no new item.
    assert created == []
    yaml_files = [f for f in research_dir.iterdir()
                  if f.is_file() and not f.name.startswith(".")]
    assert len(yaml_files) == 2  # poison item + the pre-existing legit item, untouched
