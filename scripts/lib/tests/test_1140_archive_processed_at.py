"""Cross-midnight L3 archive evidence regression tests for board #1140."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from scripts.lib.dispatch_helpers import (
    _event_archive_evidence_today,
    archive_successful_decision,
)


MISSION = {
    "id": "execution",
    "layer": 3,
    "cadence": "event",
    "input_queue": "inbox/decisions",
}
REPO_ROOT = Path(__file__).resolve().parents[3]


def _write_decision(repo: Path, decision_id: str, created_at: datetime) -> Path:
    path = repo / "inbox" / "decisions" / f"{decision_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "id": decision_id,
                "status": "approved",
                "created_at": created_at.isoformat(),
                "payload": {"synthetic": True},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("created_at", "processed_at", "observed_at"),
    [
        # Summer UTC+2: approved 23:50, processed 00:10 next Paris day.
        (
            datetime(2026, 7, 1, 21, 50, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 22, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc),
        ),
        # DST starts: approved 23:50 CET, processed 03:10 CEST next day.
        (
            datetime(2026, 3, 28, 22, 50, tzinfo=timezone.utc),
            datetime(2026, 3, 29, 1, 10, tzinfo=timezone.utc),
            datetime(2026, 3, 29, 18, 0, tzinfo=timezone.utc),
        ),
        # DST ends: approved 23:50 CEST, processed 02:30 CET next day.
        (
            datetime(2026, 10, 24, 21, 50, tzinfo=timezone.utc),
            datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc),
            datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_archive_signals_the_paris_processing_day(
    tmp_path, created_at, processed_at, observed_at
):
    source = _write_decision(tmp_path, "approved-001", created_at)

    archived = archive_successful_decision(
        tmp_path,
        "approved-001",
        execution_succeeded=True,
        processed_at=processed_at,
    )

    assert not source.exists()
    assert archived == source.parent / ".processed" / source.name
    data = yaml.safe_load(archived.read_text(encoding="utf-8"))
    assert data["processed_at"] == processed_at.isoformat().replace("+00:00", "Z")
    assert data["payload"] == {"synthetic": True}
    assert _event_archive_evidence_today(
        tmp_path, MISSION, now_utc=observed_at
    ) is True
    assert _event_archive_evidence_today(
        tmp_path, MISSION, now_utc=created_at
    ) is False


def test_consumer_prefers_processed_at_over_created_at(tmp_path):
    processed = tmp_path / "inbox" / "decisions" / ".processed"
    processed.mkdir(parents=True)
    (processed / "item.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "item",
                "processed_at": "2026-07-01T20:00:00Z",
                "created_at": "2026-07-02T08:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    assert _event_archive_evidence_today(
        tmp_path,
        MISSION,
        now_utc=datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc),
    ) is False


def test_legacy_created_at_fallback_remains_supported(tmp_path):
    processed = tmp_path / "inbox" / "decisions" / ".processed"
    processed.mkdir(parents=True)
    (processed / "legacy.yaml").write_text(
        "id: legacy\ncreated_at: '2026-07-02T08:00:00Z'\n",
        encoding="utf-8",
    )
    assert _event_archive_evidence_today(
        tmp_path,
        MISSION,
        now_utc=datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc),
    ) is True


def test_failed_execution_is_not_stamped_or_archived(tmp_path):
    source = _write_decision(
        tmp_path,
        "failed-001",
        datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="without successful execution"):
        archive_successful_decision(
            tmp_path,
            "failed-001",
            execution_succeeded=False,
            processed_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
        )

    assert source.exists()
    assert "processed_at" not in yaml.safe_load(source.read_text(encoding="utf-8"))
    assert not (source.parent / ".processed" / source.name).exists()


def test_held_abandoned_and_active_items_are_not_evidence(tmp_path):
    decisions = tmp_path / "inbox" / "decisions"
    for subdir in (".held", ".abandoned"):
        target = decisions / subdir / "item.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "id: item\nprocessed_at: '2026-07-02T09:00:00Z'\n",
            encoding="utf-8",
        )
    _write_decision(
        tmp_path,
        "active-001",
        datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
    )

    assert _event_archive_evidence_today(
        tmp_path,
        MISSION,
        now_utc=datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc),
    ) is False


def test_replay_preserves_original_processing_timestamp(tmp_path):
    _write_decision(
        tmp_path,
        "replay-001",
        datetime(2026, 7, 1, 21, 50, tzinfo=timezone.utc),
    )
    first_time = datetime(2026, 7, 1, 22, 10, tzinfo=timezone.utc)
    archived = archive_successful_decision(
        tmp_path,
        "replay-001",
        execution_succeeded=True,
        processed_at=first_time,
    )
    replayed = archive_successful_decision(
        tmp_path,
        "replay-001",
        execution_succeeded=True,
        processed_at=datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc),
    )

    assert replayed == archived
    assert yaml.safe_load(archived.read_text(encoding="utf-8"))["processed_at"] == (
        "2026-07-01T22:10:00Z"
    )


def test_malformed_or_unsafe_decision_never_moves(tmp_path):
    decisions = tmp_path / "inbox" / "decisions"
    decisions.mkdir(parents=True)
    malformed = decisions / "bad.yaml"
    malformed.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be a mapping"):
        archive_successful_decision(
            tmp_path,
            "bad",
            execution_succeeded=True,
            processed_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="safe filename stem"):
        archive_successful_decision(
            tmp_path,
            "../bad",
            execution_succeeded=True,
        )

    assert malformed.exists()
    assert not (decisions / ".processed" / "bad.yaml").exists()


def test_canonical_l3_prompts_use_success_only_archive_helper():
    template = (REPO_ROOT / "scripts" / "lib" / "layer_templates.py").read_text(
        encoding="utf-8"
    )
    ben_prompt = (
        REPO_ROOT / "agents" / "ben" / "layers" / "3" / "PROMPT.md"
    ).read_text(encoding="utf-8")

    for source in (template, ben_prompt):
        assert "archive_successful_decision" in source
        assert "execution_succeeded=True" in source
    assert "processed_at" in ben_prompt
    assert "real processing time" in template
