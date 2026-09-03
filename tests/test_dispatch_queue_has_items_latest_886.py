"""test_dispatch_queue_has_items_latest_886.py — board #886 / #1084:
`_queue_has_items` must NOT count `*-latest.yaml` hand-off/state files.

Bug (#886, diagnosed 2026-08-10 on the content dept): two PERMANENT hand-off
files live in `queues/research/` — `external-signal-latest.yaml` and
`linkedin-sage-scan-latest.yaml`. They legitimately carry no `kind:`, so the
kind-less fail-open in `_queue_has_items` counted them as pending work,
pinning `has_research_items=True` forever. That perpetual L2 signal starved
L1's daily floor, which in turn silently killed L4 (no feedback_digest for
weeks — the #432 true root cause).

The content dept carried the one-line fix as a LOCAL, unvendored stopgap on
its live `dispatch_helpers.py` (skip-worktree overlay, 2026-08-10). The #1084
re-vendor audit found canonical still lacked it — meaning a canonical
re-vendor onto that dept would have silently re-opened #886. This test pins
the upstreamed line so it can never be re-lost by a future re-vendor.

Run: python3 -m pytest tests/test_dispatch_queue_has_items_latest_886.py -q
"""
from __future__ import annotations

import yaml

from scripts.lib import dispatch_helpers


def _item(path, kind="research_item"):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"id": path.stem}
    if kind is not None:
        data["kind"] = kind
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _state_file(path):
    """A kind-less hand-off/state file, exactly like the two live pinners."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"last_scan": "2026-08-10T06:00:00Z", "seen": []}),
        encoding="utf-8",
    )


def test_lone_latest_file_does_not_pin_root(tmp_path):
    """THE #886 regression: a lone `*-latest.yaml` in queues/research must
    not make has_research True (kind-blind path)."""
    queue = tmp_path / "queues" / "research"
    _state_file(queue / "external-signal-latest.yaml")
    _state_file(queue / "linkedin-sage-scan-latest.yaml")
    assert dispatch_helpers._queue_has_items(queue) is False


def test_lone_latest_file_does_not_pin_kind_aware(tmp_path):
    """Same, on the kind-aware path: the kind-less fail-open must not apply to
    a `*-latest.yaml` state file."""
    queue = tmp_path / "queues" / "research"
    _state_file(queue / "external-signal-latest.yaml")
    assert dispatch_helpers._queue_has_items(
        queue, drainable_kinds={"research_item"}) is False


def test_lone_latest_file_in_todays_subdir_does_not_pin(tmp_path):
    """#898's today-scoped dated-subdir scan must apply the same exclusion."""
    queue = tmp_path / "queues" / "research"
    _state_file(queue / "2026-09-03" / "external-signal-latest.yaml")
    assert dispatch_helpers._queue_has_items(queue, today="2026-09-03") is False


def test_real_item_next_to_latest_file_still_counts(tmp_path):
    """The exclusion is surgical: a real pending item beside the state files
    is still seen (no silent starvation)."""
    queue = tmp_path / "queues" / "research"
    _state_file(queue / "external-signal-latest.yaml")
    _item(queue / "2026-09-03-01-lead.yaml")
    assert dispatch_helpers._queue_has_items(queue) is True
    assert dispatch_helpers._queue_has_items(
        queue, drainable_kinds={"research_item"}) is True


def test_kindless_real_item_still_fails_open(tmp_path):
    """The kind-less fail-open itself is preserved for NON-state files: the
    content dept's L1 gatherers write kind-less pool items
    (queues/research/<today>/<source>.yaml) and those must still count."""
    queue = tmp_path / "queues" / "research"
    _item(queue / "2026-09-03" / "youtube.yaml", kind=None)
    assert dispatch_helpers._queue_has_items(
        queue, drainable_kinds={"research_item"}, today="2026-09-03") is True


def test_build_ctx_not_pinned_by_latest_files(tmp_path):
    """End-to-end through build_dispatch_ctx: only the two live pinners in
    queues/research/ → has_research_items is False."""
    repo = tmp_path
    (repo / "queues" / "research").mkdir(parents=True)
    _state_file(repo / "queues" / "research" / "external-signal-latest.yaml")
    _state_file(repo / "queues" / "research" / "linkedin-sage-scan-latest.yaml")
    (repo / "layers").mkdir()
    ctx = dispatch_helpers.build_dispatch_ctx(repo)
    assert ctx["has_research_items"] is False
