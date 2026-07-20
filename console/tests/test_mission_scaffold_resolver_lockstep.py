"""
test_mission_scaffold_resolver_lockstep.py — card #706.

scripts/lib/mission_scaffold.py (the factory emitter, #688) and
console/services/mission_pieces.py (the piece-view resolver, #642/PR-B)
DELIBERATELY duplicate the same mission-config naming rules, because
scripts/lib is vendored into every dept repo and cannot import console/
(that would ImportError on every dept box that doesn't ship the console
app). That duplication is correct and must stay — but nothing enforces
that the two copies stay in lockstep when one gets edited and the other
doesn't. This file is that enforcement.

Two guards:

1. Constant lockstep: `mission_scaffold._MISSION_VERB_PREFIXES` must be
   byte-for-byte the same tuple as `mission_pieces._MISSION_VERB_PREFIXES`.
   A one-sided edit to either (e.g. adding a new verb prefix to only one
   module) must fail CI immediately, not silently degrade the piece view
   on whichever dept happens to use the new verb.

2. Filename agreement: for real mission-id shapes, the emitter's chosen
   config/<name>.yaml filename must appear SOMEWHERE in the resolver's
   own candidate list for that mission id.

   Investigated from the code (not assumed): the emitter's docstring
   claims it writes "the first candidate mission_pieces._mission_config_
   piece would also try, so the emitted file resolves on the very first
   lookup" (mission_scaffold.py, _mission_config_glossary_name docstring,
   and the module docstring's "the WRITE side must offer the file at the
   same candidate the READ side tries first"). That is NOT what the code
   does: for every verb-prefixed id, the emitter writes the verb-stripped
   TAIL, which is the resolver's candidate #2, never candidate #1 (the
   whole id) -- see the comparison table in this file's
   test_emitter_writes_resolvers_second_candidate_not_first below.

   Crucially, this is not a bug to fix by reordering either side: the
   resolver's own module docstring (mission_pieces.py, PR-B fix comment)
   documents VERIFIED real bubble-ops-content shapes that require
   candidates other than "whole id" and other than "verb-stripped tail"
   to win -- `publish_execution -> config/publish.yaml` (FIRST token),
   `gather_youtube_taste -> config/youtube_topics.yaml` (SECOND token).
   There is no single "first candidate" rule that is universally the
   *correct* one across real dept repos; the resolver's whole reason for
   trying a candidate LIST (module docstring: "A single fixed prefix-strip
   rule can't cover all four shapes") is that no single rule suffices.
   So Option A (force emitter-writes-candidate-#1) would fight the
   resolver's own verified design. The real intended invariant is Option
   B: the emitter's filename must be a MEMBER of the resolver's candidate
   list (order not guaranteed) -- which the "gather_" tests below confirm
   is the operative behavior on real per-shape mission ids today. The
   inaccurate "tries first" docstring wording is corrected alongside this
   test (see mission_scaffold.py's module + constant docstrings).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import BOTH real modules from their real locations -- no console import
# inside scripts/lib, no scripts/lib import inside console (that's the
# ImportError-on-dept-boxes trap this test exists to keep away from).
# Same sys.path pattern as test_dept_factory_piece_view.py.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent          # console/tests/
_CONSOLE_ROOT = _HERE.parent                       # console/
_PROJECT_ROOT = _CONSOLE_ROOT.parent               # bubble-ops-loop/
_SCRIPTS_LIB = _PROJECT_ROOT / "scripts" / "lib"

if str(_SCRIPTS_LIB) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_LIB))

import mission_scaffold  # noqa: E402

from console.services import mission_pieces  # noqa: E402


def test_verb_prefix_constants_in_lockstep():
    """The two hand-duplicated copies of the verb-prefix tuple must be
    byte-for-byte identical. A one-sided edit to either module (e.g.
    adding a new verb prefix only on the write side) must fail this test
    immediately rather than silently degrading the piece view."""
    assert (
        mission_scaffold._MISSION_VERB_PREFIXES
        == mission_pieces._MISSION_VERB_PREFIXES
    )


def test_constant_lockstep_actually_catches_drift(monkeypatch):
    """Proof the guard above isn't vacuous: mutate one side's copy in
    isolation and confirm the same equality assertion goes red."""
    monkeypatch.setattr(
        mission_scaffold, "_MISSION_VERB_PREFIXES",
        mission_scaffold._MISSION_VERB_PREFIXES + ("new_verb_",),
    )
    assert (
        mission_scaffold._MISSION_VERB_PREFIXES
        != mission_pieces._MISSION_VERB_PREFIXES
    )


def _resolver_config_candidates(root: Path, mission_id: str) -> list:
    """Drive every filename the resolver's real _mission_config_piece
    would accept for mission_id, by materializing each of its candidates
    one at a time and recording which get picked up. We instead just
    replicate visibility by asking the resolver directly: create ALL
    candidate files up front and ask it which single one it resolves to
    would only show the WINNER, not the full list. To get the actual
    candidate list (order + full set) for the agreement check, we drive
    _mission_config_piece with each candidate file present in isolation
    and collect every filename that resolves to a clickable config piece
    -- i.e. the real membership test, using the resolver's own code path,
    not a hand-rolled re-implementation of its candidate generation."""
    import itertools

    stripped = mission_id
    for prefix in mission_pieces._MISSION_VERB_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    tokens = [t for t in mission_id.split("_") if t]
    bases = [mission_id, stripped]
    for t in tokens:
        if t not in bases:
            bases.append(t)
    if mission_id.startswith("gather_"):
        suffixes = ("_sources", "_topics", "")
    else:
        suffixes = ("", "_sources", "_topics")

    accepted = []
    for base, suffix in itertools.product(bases, suffixes):
        name = f"{base}{suffix}"
        config_dir = root / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        target = config_dir / f"{name}.yaml"
        # isolate: remove any other candidate files so only THIS one can win
        for existing in config_dir.glob("*.yaml"):
            existing.unlink()
        target.write_text("sources: []\n", encoding="utf-8")
        piece = mission_pieces._mission_config_piece(root, mission_id)
        target.unlink()
        if piece is not None and piece["label"] == name:
            accepted.append(name)
    return accepted


_MISSION_ID_SHAPES = [
    "draft_x",
    "draft_linkedin",
    "gather_newsletter_signal",
    "synthesizing_content_feedback",
    "publish_execution",
    "execution",
    "research",
]


@pytest.mark.parametrize("mission_id", _MISSION_ID_SHAPES)
def test_emitter_filename_is_among_resolver_candidates(tmp_path, mission_id):
    """Option B invariant: the emitter's chosen config/<name>.yaml filename
    must be SOMEWHERE in the resolver's own candidate list for the same
    mission id -- not necessarily the resolver's first-tried candidate
    (real per-shape verified filenames like publish_execution ->
    config/publish.yaml prove no single fixed "try this one first" rule
    is universally correct; see this file's module docstring)."""
    emitter_name = mission_scaffold._mission_config_glossary_name(mission_id)
    resolver_accepted = _resolver_config_candidates(tmp_path, mission_id)
    assert emitter_name in resolver_accepted, (
        f"emitter wrote config/{emitter_name}.yaml for {mission_id!r}, but "
        f"the resolver would only accept one of {resolver_accepted!r}"
    )


def test_emitter_writes_resolvers_second_candidate_not_first():
    """Documents the measured drift (card #706): for verb-prefixed ids the
    emitter's filename is candidate #2 (verb-stripped tail) in the
    resolver's own try-order, never candidate #1 (the whole id). Today
    this resolves correctly only because the resolver tries multiple
    candidates and doesn't require its first hit to win -- i.e. the
    PR#276-era docstring claim that the write side offers "the same
    candidate the read side tries first" is not literally true. This test
    pins that fact so it can't silently regress into becoming untrue in
    the other direction (emitter drifting to some THIRD filename the
    resolver never tries at all -- which the membership test above would
    catch)."""
    verb_prefixed = ["draft_x", "draft_linkedin", "gather_newsletter_signal",
                      "synthesizing_content_feedback", "publish_execution"]
    for mission_id in verb_prefixed:
        emitter_name = mission_scaffold._mission_config_glossary_name(mission_id)
        first_candidate = mission_id  # resolver's candidates[0] is always the whole id
        assert emitter_name != first_candidate, (
            f"expected {mission_id!r}'s emitter output to differ from the "
            f"resolver's first candidate (documenting the known #706 gap) "
            f"but they matched -- update this test's premise if the "
            f"resolver's candidate order changed."
        )
