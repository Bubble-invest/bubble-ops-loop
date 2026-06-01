"""
Gap C2 — CODEOWNERS as documented branch-protection substitute.

Empirical evidence (QA-FIXES-COMPLEMENT-RESULTS.md Fix A + Fix D):
  > "private repos require GitHub Pro — pivoted to `.github/CODEOWNERS`
  > (now live, the load-bearing piece of the at-rest defense)."
  > "AFTER → File present at `main` (verified: `gh api ...` returns
  > 200). Covers all 10 structural categories from Notion v4 line 700
  > [...], each owned by `@vdk888`."

The full GitHub-API enforcement of CODEOWNERS reviews would require
mocking GitHub's review API + creating a real PR from a non-owner
identity. That is a future ticket. This test pins the documented
invariants:

  1. `.github/CODEOWNERS` exists in the live local clone
     /tmp/bubble-ops-fixture/.
  2. It has at least one rule covering each of the structural-path
     categories named in Notion v4 line 700 (dept.yaml, missions/,
     tools/, policies/, layers/*/PROMPT.md, etc.).
  3. Each rule names a real reviewer (currently `@vdk888`).
  4. docs/ARCHITECTURE.md §"Failure modes catalogue" mentions
     CODEOWNERS as the documented substitute for branch protection.

NOTE: this is a DOCUMENTATION-CROSS-CHECK test. It does NOT prove that
GitHub actually rejects non-CODEOWNER pushes — that requires either
GitHub Pro or a real PR + non-owner credentials. Future ticket: add a
runtime guard that simulates a non-owner push and asserts a 4xx.

Run:
    python3 -m pytest tests/test_codeowners_enforcement_documented.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent  # bubble-ops-loop/

FIXTURE_CLONE = Path("/tmp/bubble-ops-fixture")
CODEOWNERS_PATH = FIXTURE_CLONE / ".github" / "CODEOWNERS"

ARCHITECTURE_DOC = PROJECT_ROOT / "docs" / "ARCHITECTURE.md"

# Structural-path categories from Notion v4 line 700, restated in
# QA-FIXES-COMPLEMENT-RESULTS.md Fix D. Each must have at least one
# rule in CODEOWNERS. (`.github/` is self-ownership; `missions/` was
# added in Step 11; layers/ covers `layers/*/PROMPT.md`.)
REQUIRED_STRUCTURAL_PATHS: tuple[str, ...] = (
    "/dept.yaml",
    "/missions/",
    "/tools/",
    "/policies/",
    "/layers/",  # covers `layers/*/PROMPT.md` via CODEOWNERS dir-prefix semantics
)


# -----------------------------------------------------------------------------
# Parsing — minimal CODEOWNERS grammar
# -----------------------------------------------------------------------------

_OWNER_RE = re.compile(r"@[\w][\w-]*(?:/[\w][\w-]*)?")


def _read_codeowners() -> str:
    return CODEOWNERS_PATH.read_text(encoding="utf-8")


def _parse_rules(text: str) -> list[tuple[str, list[str]]]:
    """Return [(path_pattern, [owners])] for each non-comment, non-blank line."""
    rules: list[tuple[str, list[str]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            # Could be a path with no owners (intentionally unowned) — skip,
            # since we're asserting ownership, not unownership.
            continue
        pattern = parts[0]
        owners = [tok for tok in parts[1:] if _OWNER_RE.fullmatch(tok)]
        rules.append((pattern, owners))
    return rules


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


def test_codeowners_file_exists_in_live_clone() -> None:
    """Substitute for branch protection: .github/CODEOWNERS must be present
    in the live fixture clone at /tmp/bubble-ops-fixture/.

    QA-FIXES-COMPLEMENT Fix A: "private repos require GitHub Pro —
    pivoted to `.github/CODEOWNERS` (now live, the load-bearing piece
    of the at-rest defense)."
    """
    assert FIXTURE_CLONE.exists(), (
        f"Fixture clone missing at {FIXTURE_CLONE} — "
        "`git clone https://github.com/vdk888/bubble-ops-fixture.git` first."
    )
    assert CODEOWNERS_PATH.is_file(), (
        f"CODEOWNERS missing at {CODEOWNERS_PATH}. "
        "Per QA-FIXES-COMPLEMENT Fix A, this file is the documented "
        "substitute for GitHub branch protection (paywalled on private "
        "free-tier repos). Without it, structural paths have no at-rest "
        "review gate."
    )


@pytest.mark.parametrize("required_path", REQUIRED_STRUCTURAL_PATHS)
def test_codeowners_covers_structural_path(required_path: str) -> None:
    """Each Notion v4 line 700 structural path must have a CODEOWNERS rule.

    DISCLAIMER (per test docstring): this asserts the rule is DECLARED,
    not that GitHub actually rejects a non-CODEOWNER push. GitHub
    enforcement requires either branch protection (paywalled on private
    repos) or a path-policy workflow (blocked on a missing workflow
    OAuth scope per QA-FIXES-COMPLEMENT Fix A xfail).
    """
    text = _read_codeowners()
    rules = _parse_rules(text)
    matching = [pattern for pattern, _owners in rules if pattern == required_path]
    assert matching, (
        f"CODEOWNERS has no rule for structural path {required_path!r}. "
        f"Per Notion v4 line 700 + QA-FIXES-COMPLEMENT Fix D, this path "
        f"requires a human-reviewed PR. Existing rules: "
        f"{[p for p, _ in rules]}"
    )


def test_every_codeowners_rule_has_at_least_one_real_owner() -> None:
    """A rule with no owners is a documentation lie — it claims to gate
    a path but actually leaves it unowned. Reject those.

    This is the substitute-quality check: a CODEOWNERS file that lists
    paths without owners would NOT be a valid substitute for branch
    protection."""
    text = _read_codeowners()
    rules = _parse_rules(text)
    assert rules, "CODEOWNERS has zero rules — substitute is empty"
    bad = [pattern for pattern, owners in rules if not owners]
    assert not bad, (
        f"CODEOWNERS rules with no owner: {bad}. "
        "An owner-less rule does not force review and is not a valid "
        "branch-protection substitute."
    )


def test_codeowners_designates_a_real_reviewer() -> None:
    """At least one rule must name `@vdk888` (the only human reviewer
    declared in QA-FIXES-COMPLEMENT Fix D).

    This is a sanity check — if the reviewer placeholder ever became
    `@TODO` or a non-existent handle, the substitute would silently
    decay to no-review."""
    text = _read_codeowners()
    assert "@vdk888" in text, (
        "CODEOWNERS does not name @vdk888. "
        "Per QA-FIXES-COMPLEMENT Fix D, @vdk888 is the at-rest reviewer. "
        "If this is being changed, update Fix D's documentation too."
    )


def test_architecture_doc_mentions_codeowners_as_substitute() -> None:
    """docs/ARCHITECTURE.md §"Failure modes catalogue" must document
    CODEOWNERS as the chosen substitute for branch protection.

    This is the cross-check: if someone removes the documentation, this
    test catches the drift. The doc + the file must stay in sync."""
    text = ARCHITECTURE_DOC.read_text(encoding="utf-8")

    # Find the Failure modes catalogue section
    catalogue_idx = text.find("Failure modes catalogue")
    assert catalogue_idx != -1, (
        "docs/ARCHITECTURE.md missing the 'Failure modes catalogue' section. "
        "This test expects a §labeled exactly 'Failure modes catalogue' "
        "(see ARCHITECTURE.md §6)."
    )
    # The catalogue should mention CODEOWNERS in context with the
    # branch-protection paywall. Scan a generous window after the heading.
    catalogue_section = text[catalogue_idx : catalogue_idx + 8000]
    assert "CODEOWNERS" in catalogue_section, (
        "docs/ARCHITECTURE.md §'Failure modes catalogue' does not mention "
        "CODEOWNERS as the documented branch-protection substitute. "
        "Either the doctrine changed (then update this test) or the "
        "documentation regressed (then update ARCHITECTURE.md)."
    )
    # And that mention should be tied to branch protection (not e.g.
    # CODEOWNERS in an unrelated bullet).
    assert (
        "branch protection" in catalogue_section.lower()
        or "Branch protection" in catalogue_section
    ), (
        "docs/ARCHITECTURE.md §'Failure modes catalogue' mentions CODEOWNERS "
        "but not in the context of branch protection. The substitute "
        "relationship must be explicit."
    )
