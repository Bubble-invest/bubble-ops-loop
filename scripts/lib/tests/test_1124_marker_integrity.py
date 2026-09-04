"""Atomic marker writes and torn-marker tolerance for board #1124."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.lib import dispatch_helpers as helpers


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def test_required_marker_and_counter_writes_use_same_dir_atomic_replace(
    tmp_path: Path, monkeypatch,
):
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def checked_replace(src, dst):
        src_path, dst_path = Path(src), Path(dst)
        assert src_path.parent == dst_path.parent
        assert src_path.name.endswith(".tmp")
        assert src_path.exists()
        replacements.append((src_path, dst_path))
        real_replace(src, dst)

    monkeypatch.setattr(helpers.os, "replace", checked_replace)
    helpers.write_last_run(tmp_path / "layer", NOW)
    helpers.write_last_materialized(tmp_path / "mission", NOW)
    assert helpers.increment_round_counter(tmp_path / "today", layer=2) == 1

    assert [dst.name for _, dst in replacements] == [
        ".last-run", ".last-materialized", "round_counter.json",
    ]


def test_failed_replace_preserves_old_marker_and_removes_temp(
    tmp_path: Path, monkeypatch,
):
    layer = tmp_path / "layer"
    layer.mkdir()
    marker = layer / ".last-run"
    marker.write_text("old-complete-value", encoding="utf-8")

    def fail_replace(src, dst):
        raise OSError("simulated interrupted replace")

    monkeypatch.setattr(helpers.os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted replace"):
        helpers.write_last_run(layer, NOW)

    assert marker.read_text(encoding="utf-8") == "old-complete-value"
    assert list(layer.glob("..last-run.*.tmp")) == []


@pytest.mark.parametrize(
    ("name", "reader"),
    [
        (".last-run", helpers.read_last_run),
        (".last-materialized", helpers.read_last_materialized),
    ],
)
def test_torn_iso_marker_is_absent_and_warns(tmp_path: Path, name, reader):
    marker = tmp_path / name
    marker.write_text("2026-09-04T12:", encoding="utf-8")
    with pytest.warns(RuntimeWarning, match="WARN: ignoring malformed"):
        assert reader(tmp_path) is None


def test_torn_management_marker_is_absent_and_warns(tmp_path: Path):
    marker = tmp_path / "queues" / "management" / ".last-mgmt-scan"
    marker.parent.mkdir(parents=True)
    marker.write_text("half-a-timestamp", encoding="utf-8")
    with pytest.warns(RuntimeWarning, match="WARN: ignoring malformed"):
        assert helpers.read_last_mgmt_scan(tmp_path) is None


def test_torn_dispatched_item_marker_does_not_block_item(tmp_path: Path):
    marker = tmp_path / "missions" / "daily" / "dispatched-items" / "item-1"
    marker.parent.mkdir(parents=True)
    marker.write_text("2026-09-04T", encoding="utf-8")
    with pytest.warns(RuntimeWarning, match="WARN: ignoring malformed"):
        dispatched = helpers._dispatched_trigger_ids(
            tmp_path, "daily", before=NOW,
        )
    assert "item-1" not in dispatched


def test_torn_round_counter_is_absent_and_warns(tmp_path: Path):
    (tmp_path / "round_counter.json").write_text('{"1":', encoding="utf-8")
    with pytest.warns(RuntimeWarning, match="WARN: ignoring malformed"):
        assert helpers.read_round_counter(tmp_path) == {}
