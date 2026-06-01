"""
test_readme_count_in_sync.py — guard against schema/README count drift.

The schemas-draft/README.md contains an English claim like
"6 schemas" or "7 schemas" in its "What's new" header. The validator
(validate_all.py) enforces EXPECTED_SCHEMA_COUNT against the filesystem.
Without this test, the README count silently drifts when a schema is
added/removed (as happened when state.schema.yaml was introduced and the
README was not bumped from 6 to 7).

This test makes the README authoritative by asserting:
  count(*.schema.yaml on disk) == count claimed in README.md

Run:
    python3 -m pytest \
      /Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/schemas-draft/tests/test_readme_count_in_sync.py \
      -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # schemas-draft/
README = ROOT / "README.md"


def _disk_schema_count() -> int:
    return len(list(ROOT.glob("*.schema.yaml")))


def _readme_claimed_counts() -> list[int]:
    """Find every "<N> schemas" claim in the README.

    We accept the pattern `\\b(\\d+)\\s+schemas\\b` (case-insensitive) and
    return all numeric matches in document order.
    """
    text = README.read_text(encoding="utf-8")
    # Match "<digits> schemas" but NOT inside backticks like `6-schema-`.
    # Simple word-boundary regex is sufficient given current README prose.
    matches = re.findall(r"\b(\d+)\s+schemas\b", text, flags=re.IGNORECASE)
    return [int(m) for m in matches]


def test_readme_exists() -> None:
    assert README.is_file(), f"README not found at {README}"


def test_readme_makes_a_schema_count_claim() -> None:
    claims = _readme_claimed_counts()
    assert claims, (
        f"README at {README} contains no '<N> schemas' claim. "
        f"Add one in the 'What's new' header so this test can verify it."
    )


def test_readme_schema_count_matches_filesystem() -> None:
    disk = _disk_schema_count()
    claims = _readme_claimed_counts()
    assert claims, "precondition: README must claim a count (see other test)"
    # All numeric claims that look like schema counts must match disk.
    # If the README mentions a count like "6 structural changes" that
    # happens to use the word "schemas" elsewhere, this test will catch
    # the divergence and force the author to disambiguate the prose.
    mismatched = [c for c in claims if c != disk]
    assert not mismatched, (
        f"README claims {claims} schemas but filesystem has {disk} "
        f"*.schema.yaml files in {ROOT}. "
        f"Bump the README to {disk} (or rephrase non-count uses of "
        f"'<N> schemas')."
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
