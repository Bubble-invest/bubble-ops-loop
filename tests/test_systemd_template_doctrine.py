"""
test_systemd_template_doctrine.py — guard against the
"tmux ban" and "claude -p ban" doctrines being conflated in the
systemd unit template comments.

Background (see docs/ARCHITECTURE.md §6 "Failure modes catalogue"):

  1. **tmux ban** — empirical regression in Claude Code v2.1.139:
     fresh session inside a tmux pty on Linux returns HTTP 404 on any
     model. Evidence: STEP-4-SMOKE-TEST-RESULTS.md (90 min diagnosis).
     Tooling bug, *not* a contract/policy choice.

  2. **`claude -p` headless ban** — unrelated: Anthropic is moving
     `-p` (print mode / headless) to a paid tier mid-June 2026, AND
     headless mode disables Claude Code hooks (which the unit relies on
     for env-read-alert + bash-validator). Policy + product change,
     *not* a tooling regression.

A sleep-deprived operator reading the template at 3 AM must not
conflate the two: adopting `claude -p` does NOT avoid the tmux 404,
and avoiding tmux does NOT restore `claude -p` hooks.

This test asserts the template's doctrine comment block:
  - mentions BOTH bans
  - keeps them in SEPARATE paragraphs (separated by a blank comment
    line `#` with nothing after the hash on that line)
  - cites the root cause keyword for each (tmux → "404";
    `claude -p` → "paid" or "hooks")

Run:
    python3 -m pytest \
      /Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop/tests/test_systemd_template_doctrine.py \
      -v
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(
    "/Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop"
)
TEMPLATE = REPO_ROOT / "deploy" / "templates" / "ops-loop-dept.service.template"


def _template_text() -> str:
    assert TEMPLATE.is_file(), f"template not found at {TEMPLATE}"
    return TEMPLATE.read_text(encoding="utf-8")


def _comment_paragraphs(text: str) -> list[list[str]]:
    """Split the file's comment lines into paragraphs.

    A paragraph = a maximal run of lines that start with '#'. Two
    paragraphs are separated by either a non-comment line OR a bare
    '#' line (i.e. a blank line *within* the comment header). Returns
    a list of paragraphs, each a list of the raw comment lines.
    """
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            # A bare '#' (no payload after the hash) is a paragraph break.
            payload = stripped[1:].strip()
            if not payload:
                if current:
                    paragraphs.append(current)
                    current = []
                continue
            current.append(stripped)
        else:
            if current:
                paragraphs.append(current)
                current = []
    if current:
        paragraphs.append(current)
    return paragraphs


def test_template_exists() -> None:
    assert TEMPLATE.is_file(), f"missing: {TEMPLATE}"


def test_template_mentions_tmux_ban() -> None:
    text = _template_text().lower()
    assert "tmux" in text, "template comments must mention the tmux ban"


def test_template_mentions_claude_p_ban() -> None:
    text = _template_text()
    # Match the literal token `claude -p` (with a space) in any case.
    assert re.search(r"claude\s+-p", text, flags=re.IGNORECASE), (
        "template comments must mention the `claude -p` ban explicitly"
    )


def test_tmux_doctrine_cites_404_root_cause() -> None:
    paragraphs = _comment_paragraphs(_template_text())
    tmux_paras = [p for p in paragraphs if any("tmux" in line.lower() for line in p)]
    assert tmux_paras, "no comment paragraph mentions tmux"
    cites_404 = any(
        any("404" in line for line in p) for p in tmux_paras
    )
    assert cites_404, (
        "the tmux-ban paragraph must cite '404' as the root cause "
        "(see STEP-4-SMOKE-TEST-RESULTS.md). Found tmux paragraphs:\n"
        + "\n---\n".join("\n".join(p) for p in tmux_paras)
    )


def test_claude_p_doctrine_cites_paid_or_hooks_root_cause() -> None:
    paragraphs = _comment_paragraphs(_template_text())
    claude_p_paras = [
        p
        for p in paragraphs
        if any(re.search(r"claude\s+-p", line, flags=re.IGNORECASE) for line in p)
    ]
    assert claude_p_paras, "no comment paragraph mentions `claude -p`"
    cites_root_cause = any(
        any(
            re.search(r"\b(paid|hooks?)\b", line, flags=re.IGNORECASE)
            for line in p
        )
        for p in claude_p_paras
    )
    assert cites_root_cause, (
        "the `claude -p`-ban paragraph must cite 'paid' or 'hooks' as "
        "the root cause (paid-tier move mid-June 2026, hooks disabled "
        "in headless mode). Found claude-p paragraphs:\n"
        + "\n---\n".join("\n".join(p) for p in claude_p_paras)
    )


def test_two_bans_are_in_separate_paragraphs() -> None:
    """The two doctrines must NOT share a single comment paragraph.

    A 3 AM operator reading a conflated paragraph might wrongly assume
    adopting one ban avoids the other's failure mode. The blank '#'
    line is the visual separator that prevents this.
    """
    paragraphs = _comment_paragraphs(_template_text())
    conflated = [
        p
        for p in paragraphs
        if any("tmux" in line.lower() for line in p)
        and any(re.search(r"claude\s+-p", line, flags=re.IGNORECASE) for line in p)
    ]
    assert not conflated, (
        "tmux ban and `claude -p` ban must live in SEPARATE comment "
        "paragraphs (separated by a bare '#' line). Conflated paragraph:\n"
        + "\n---\n".join("\n".join(p) for p in conflated)
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
