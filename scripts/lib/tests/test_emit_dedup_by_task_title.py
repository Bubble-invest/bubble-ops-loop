"""test_emit_dedup_by_task_title.py — emit_kanban_item.sh must dedup on
task + title, NOT task alone (board #418).

The bug: the idempotency check searched the board for `emit-task: <task>` and
skipped if ANY open issue carried that marker. So once one card existed for a
given task=, every later emit with the same task was silently swallowed — a
multi-finding cron (e.g. task=morty-agentic-audit) could only ever surface ONE
card per run; all other distinct findings vanished.

The fix: dedup on a key of `<task>::<title-slug>` so distinct findings each get
their own card, while a re-emit of the EXACT same finding still collapses to one.

We exercise the REAL slug/key logic via the script's `--print-emit-key` dry-run
hook (no GitHub, no mocking of the derivation). These keys are exactly what the
idempotency search matches on and what the body marker stores, so asserting on
them is a true regression test for the dedup behavior.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# repo root: scripts/lib/tests -> up 3
REPO_ROOT = HERE.parent.parent.parent
EMITTER = REPO_ROOT / "tools" / "kanban" / "emit_kanban_item.sh"


def emit_key(task: str, title: str) -> str:
    """Return the dedup key the emitter would use for this task+title."""
    res = subprocess.run(
        ["bash", str(EMITTER), "--print-emit-key", f"task={task}", f"title={title}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def test_emitter_script_exists():
    assert EMITTER.exists(), f"emitter not found at {EMITTER}"


def test_same_task_different_titles_do_not_collapse():
    """The core #418 regression: one task, two distinct findings → two keys.

    Pre-fix the dedup was on task alone, so these would have been treated as the
    same card and the second finding swallowed.
    """
    task = "morty-agentic-audit"
    k1 = emit_key(task, "L1 → L2 drift on Saxo/Crypto")
    k2 = emit_key(task, "Stale broker token on Alpaca")
    assert k1 != k2, f"distinct findings collapsed to one key: {k1!r}"


def test_same_task_same_title_is_idempotent():
    """Re-emitting the exact same finding must produce the same key (one card)."""
    task = "morty-agentic-audit"
    title = "Stale broker token on Alpaca"
    assert emit_key(task, title) == emit_key(task, title)


def test_key_is_task_plus_title_slug():
    """Key shape is <task>::<slug>; the task half is preserved verbatim."""
    key = emit_key("morty-agentic-audit", "Hello World")
    assert key == "morty-agentic-audit::hello-world"


def test_slug_robust_to_tricky_chars():
    """Real titles carry →, /, spaces and CVE colons — all must collapse to
    single '-' so the slug stays a clean, deterministic, GitHub-searchable token.
    """
    key = emit_key("t", "L1 → L2 drift on Saxo/Crypto")
    assert key == "t::l1-l2-drift-on-saxo-crypto"

    key2 = emit_key("t", "CVE-2024:1234 in token-broker")
    assert key2 == "t::cve-2024-1234-in-token-broker"


def test_slug_trims_leading_trailing_separators():
    """Leading/trailing non-alphanumerics must not leave dangling '-'."""
    key = emit_key("t", "  ::weird:: title!!  ")
    assert key == "t::weird-title", key


def test_titles_differing_only_in_case_or_punct_share_a_slug():
    """The slug is case/punctuation-insensitive, so cosmetically-different
    re-emits of the same finding still dedup to one card (idempotency holds)."""
    a = emit_key("t", "Rotate Stripe key!")
    b = emit_key("t", "rotate stripe key")
    assert a == b == "t::rotate-stripe-key"


def test_different_tasks_same_title_do_not_collapse():
    """Two different crons reporting the same-worded finding stay separate."""
    a = emit_key("morty-agentic-audit", "Token rotation overdue")
    b = emit_key("ben-risk-audit", "Token rotation overdue")
    assert a != b
