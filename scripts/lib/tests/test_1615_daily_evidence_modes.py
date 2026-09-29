"""Board #1615: isolated Tony can read non-secret daily run evidence."""
from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from scripts.lib.dispatch_helpers import (
    commit_dispatch,
    increment_round_counter,
    write_last_materialized,
    write_last_run,
)
from scripts.lib.loop_backup import HB_BACKUP_RAN, append_external_heartbeat
from scripts.lib.scaffold import CLAUDE_MD_OPERATING_TEMPLATE


NOW = datetime(2026, 9, 28, 18, 5, tzinfo=timezone.utc)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@contextmanager
def _umask(mask: int):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def test_layer_and_mission_last_run_are_readable_under_restrictive_umask(
    tmp_path: Path,
):
    paths = (
        tmp_path / "outputs" / "2026-09-28" / "2",
        tmp_path / "outputs" / "2026-09-28" / "missions" / "daily_scan",
    )
    with _umask(0o077):
        for directory in paths:
            write_last_run(directory, NOW)

    assert [_mode(path / ".last-run") for path in paths] == [0o644, 0o644]


def test_last_run_rewrite_repairs_owner_only_mode(tmp_path: Path):
    layer = tmp_path / "outputs" / "2026-09-28" / "1"
    layer.mkdir(parents=True)
    marker = layer / ".last-run"
    marker.write_text("old", encoding="utf-8")
    marker.chmod(0o600)

    write_last_run(layer, NOW)

    assert _mode(marker) == 0o644


def test_dispatch_ledger_is_readable_under_restrictive_umask(tmp_path: Path):
    mission = {"id": "daily_scan", "layer": 2, "cadence": "daily"}
    with _umask(0o077):
        assert commit_dispatch(
            tmp_path,
            mission,
            dispatched_at=NOW,
            completed_at=NOW,
            materialize_outputs=False,
        )

    ledger = tmp_path / "outputs" / "2026-09-28" / "dispatch.json"
    assert _mode(ledger) == 0o644


def test_external_heartbeat_is_readable_and_repairs_owner_only_mode(tmp_path: Path):
    heartbeat = tmp_path / "outputs" / "2026-09-28" / "heartbeat.log"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.write_text("old\n", encoding="utf-8")
    heartbeat.chmod(0o600)

    with _umask(0o077):
        append_external_heartbeat(
            str(heartbeat),
            HB_BACKUP_RAN,
            layer=2,
            exit_code=0,
            ts="2026-09-28T18:05:00Z",
        )

    assert _mode(heartbeat) == 0o644


def test_unrelated_atomic_runtime_state_stays_owner_only(tmp_path: Path):
    """The readable mode is scoped to Tony's allowlisted evidence surface."""
    with _umask(0o077):
        write_last_materialized(tmp_path / "missions" / "daily_scan", NOW)
        increment_round_counter(tmp_path / "today", layer=2)

    assert (
        _mode(tmp_path / "missions" / "daily_scan" / ".last-materialized")
        == 0o600
    )
    assert _mode(tmp_path / "today" / "round_counter.json") == 0o600


def test_primary_loop_is_told_to_publish_only_heartbeat_evidence():
    assert "chmod 0644 outputs/<today>/heartbeat.log" in CLAUDE_MD_OPERATING_TEMPLATE
    assert (
        "Do not relax permissions on any other file or secret"
        in CLAUDE_MD_OPERATING_TEMPLATE
    )
