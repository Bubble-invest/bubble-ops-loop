"""
Sprint H+I Fix 6 — reconcile step-1 artifact contradiction.

Before this fix:
  - CLAUDE.md (scaffold.py::CLAUDE_MD_TEMPLATE) said step 1 produces
    `MANDATE.md`.
  - onboarding/1-mandate/README.md (scaffold.py::STEP_README) said step 1
    produces `dept.yaml.draft` (department block).

Per Notion v5 lines 812-825 the canonical artifact is the YAML
`department:` block (which lives in `dept.yaml.draft`). The
human-readable narrative `MANDATE.md` is a useful companion. Both
should be documented in BOTH places, consistently.
"""
from __future__ import annotations

import re
from pathlib import Path


def _extract_step1_paragraph_from_claude_md(text: str) -> str:
    """Return only the step-1 paragraph from CLAUDE.md (the line under
    the 7-steps section heading that starts with '1.').

    Scaffold v2 renamed the section from '## Les 7 étapes' to
    '## The 7 hatching steps I drive on my own' (English voice migration).
    Accept both headings for forward/backward compatibility.
    """
    in_enum = False
    out = []
    for line in text.splitlines():
        stripped_line = line.strip()
        if (stripped_line.startswith("## Les 7 étapes") or
                stripped_line.startswith("## The 7 hatching steps")):
            in_enum = True
            continue
        if in_enum:
            stripped = line.lstrip()
            if stripped.startswith("2."):
                break
            out.append(line)
    return "\n".join(out)


def test_step1_readme_and_claude_md_reference_same_artifacts(
    bootstrapped_repo: Path,
) -> None:
    """Both files must reference the SAME artifacts for step 1.

    Canonical (per Notion v5 lines 812-825 + the spec hint in the audit):
      - dept.yaml.draft (the YAML department block — machine-readable)
      - MANDATE.md      (human-readable mandate narrative)
    """
    readme_text = (
        bootstrapped_repo / "onboarding" / "1-mandate" / "README.md"
    ).read_text(encoding="utf-8")

    claude_md_text = (bootstrapped_repo / "CLAUDE.md").read_text(encoding="utf-8")
    step1_para = _extract_step1_paragraph_from_claude_md(claude_md_text)

    # Both must mention BOTH artifacts.
    for artifact in ("dept.yaml.draft", "MANDATE.md"):
        assert artifact in readme_text, (
            f"onboarding/1-mandate/README.md must reference '{artifact}'; "
            f"got:\n{readme_text}"
        )
        assert artifact in step1_para, (
            f"CLAUDE.md step-1 paragraph must reference '{artifact}'; "
            f"got:\n{step1_para}"
        )


def test_step1_readme_explains_role_split(bootstrapped_repo: Path) -> None:
    """The README must clarify that dept.yaml.draft is machine-readable
    (gets validated against the schema) and MANDATE.md is the human
    narrative."""
    readme_text = (
        bootstrapped_repo / "onboarding" / "1-mandate" / "README.md"
    ).read_text(encoding="utf-8").lower()
    # Must hint at both roles.
    has_machine_hint = (
        "machine" in readme_text
        or "schema" in readme_text
        or "yaml" in readme_text
    )
    has_human_hint = (
        "human" in readme_text
        or "narrative" in readme_text
        or "lisible" in readme_text
        or "humain" in readme_text
        or "narrati" in readme_text
    )
    assert has_machine_hint, (
        f"README must hint at the machine-readable role of dept.yaml.draft; "
        f"got:\n{readme_text}"
    )
    assert has_human_hint, (
        f"README must hint at the human-readable role of MANDATE.md; "
        f"got:\n{readme_text}"
    )


def test_step1_claude_md_still_mentions_dept_yaml_field(
    bootstrapped_repo: Path,
) -> None:
    """CLAUDE.md step-1 paragraph must still say the mandate ALSO populates
    the dept.yaml.draft::department.mandate field (so the agent doesn't
    forget to do both)."""
    claude_md_text = (bootstrapped_repo / "CLAUDE.md").read_text(encoding="utf-8")
    step1_para = _extract_step1_paragraph_from_claude_md(claude_md_text)
    assert "dept.yaml.draft" in step1_para, (
        f"step-1 paragraph must mention dept.yaml.draft; got:\n{step1_para}"
    )
