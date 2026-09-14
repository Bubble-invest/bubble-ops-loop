"""Coverage for the shared, still-strict `tools/readonly_intents_mirror.py`
validator.

Board #1333 (Option C, Joris-approved 2026-09-14) retired the isolated,
root-owned, filesystem-immutable operator-intents mirror (#430/#1267) as the
model for the cloud-wiki-compile pipeline: `wiki_intent_audit.py` now uses
its own lenient `validate_intents_root()` (see `tests/test_wiki_intent_audit.py`)
and reads the wiki's own `shared/operator-intents/` by default.

`tools/readonly_intents_mirror.py`'s strict `validate_mirror()` is
DELIBERATELY left unchanged by #1333: it is a separate, still-live
dependency of `tools/fleet_architecture.py` (#1249) and
`tools/kanban/intent_alignment_check.py` (#1254, exercised by
`scripts/lib/tests/test_1254_intent_traceability.py`), both out of this
card's scope. This file used to live inside
`tests/test_operator_intents_mac_mirror.py` alongside the Mac
install/sync/plist/verify daemon scripts that #1333 retired; those files are
removed (git history preserves them) and this one assertion — the only part
of that file testing something still shipped — moved here.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_shared_consumer_validator_still_checks_release_ownership_and_mode() -> None:
    validator = (ROOT / "tools/readonly_intents_mirror.py").read_text(encoding="utf-8")
    assert "release_info = release.lstat()" in validator
    assert "release_info.st_uid != 0 or release_info.st_gid != 0" in validator
    assert "stat.S_IMODE(release_info.st_mode) != 0o555" in validator
    assert 'path.name == ".git"' in validator
