"""test_emit_dismiss_ledger.py — emit_kanban_item.sh must honor a persistent
dismiss-ledger so a once-reviewed-and-dismissed item is never re-emitted,
even after its board card is closed and the ephemeral issue number is gone
(board #1395).

The bug: wiki-compile's intent-audit and verify/incident extractors re-flagged
the SAME ~5-7 false-positive/resolved items every nightly compile, because
closing a board CARD never recorded the dismissal anywhere the next compile
could see. The open-issue idempotency check (task+title marker in an OPEN
issue body) only helps within a single still-open card's lifetime; once Rick
closes it, the next compile has no memory and re-cards it under a new number.

The fix: a small git-tracked JSON ledger (tools/kanban/dismissed_emit_keys.json)
keyed by the SAME task::title-slug emit_key the open-issue check already uses.
emit_kanban_item.sh consults it BEFORE ever calling `gh`, so it also works
when `gh` is unauthenticated (board #1422 hit exactly that).

We exercise the REAL lookup via the script's `--is-dismissed` dry-run hook (no
GitHub, no mocking) — same pattern as test_emit_dedup_by_task_title.py's
`--print-emit-key` hook.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
# repo root: scripts/lib/tests -> up 3
REPO_ROOT = HERE.parent.parent.parent
EMITTER = REPO_ROOT / "tools" / "kanban" / "emit_kanban_item.sh"
REAL_LEDGER = REPO_ROOT / "tools" / "kanban" / "dismissed_emit_keys.json"


def is_dismissed(task: str, title: str, ledger: Path | None = None) -> bool:
    """Return whether the emitter's real --is-dismissed check flags this key."""
    env_overrides = {}
    if ledger is not None:
        env_overrides["KANBAN_DISMISS_LEDGER"] = str(ledger)
    import os

    env = {**os.environ, **env_overrides}
    res = subprocess.run(
        ["bash", str(EMITTER), "--is-dismissed", f"task={task}", f"title={title}"],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    out = res.stdout.strip()
    assert out in ("true", "false"), f"unexpected --is-dismissed output: {out!r}"
    return out == "true"


def emit_key(task: str, title: str) -> str:
    res = subprocess.run(
        ["bash", str(EMITTER), "--print-emit-key", f"task={task}", f"title={title}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def write_ledger(path: Path, keys: list[str]) -> None:
    payload = {
        "schema_version": 1,
        "dismissed": [
            {"key": key, "reason": "test fixture", "dismissed_at": "2026-09-22", "dismissed_by": "test"}
            for key in keys
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_emitter_and_ledger_exist():
    assert EMITTER.exists(), f"emitter not found at {EMITTER}"
    assert REAL_LEDGER.exists(), f"seeded dismiss-ledger not found at {REAL_LEDGER}"


# ── Core behavior: a page on the ledger is NOT re-emitted; a new page IS ─────


def test_dismissed_key_is_flagged(tmp_path: Path):
    ledger = tmp_path / "ledger.json"
    task = "wiki-intent-candidate-leak"
    title = "candidate intent leak: cgp/hot.md"
    write_ledger(ledger, [emit_key(task, title)])

    assert is_dismissed(task, title, ledger) is True


def test_genuinely_new_item_is_not_flagged(tmp_path: Path):
    ledger = tmp_path / "ledger.json"
    task = "wiki-intent-candidate-leak"
    dismissed_title = "candidate intent leak: cgp/hot.md"
    new_title = "candidate intent leak: rick_rnd/genuinely-new-page.md"
    write_ledger(ledger, [emit_key(task, dismissed_title)])

    assert is_dismissed(task, new_title, ledger) is False


def test_lookup_is_keyed_by_task_and_title_not_title_alone(tmp_path: Path):
    """A title match under a DIFFERENT task must not be treated as dismissed —
    the key is task::slug, exactly like the open-issue idempotency check."""
    ledger = tmp_path / "ledger.json"
    write_ledger(ledger, [emit_key("wiki-intent-candidate-leak", "candidate intent leak: cgp/hot.md")])

    assert is_dismissed("some-other-task", "candidate intent leak: cgp/hot.md", ledger) is False


# ── Degrade-safe: missing/corrupt ledger never blocks emission or crashes ────


def test_missing_ledger_file_degrades_to_not_dismissed(tmp_path: Path):
    missing = tmp_path / "does-not-exist.json"
    assert is_dismissed("any-task", "any title", missing) is False


def test_corrupt_ledger_json_degrades_to_not_dismissed(tmp_path: Path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text("{not valid json", encoding="utf-8")
    assert is_dismissed("any-task", "any title", ledger) is False


def test_ledger_with_wrong_shape_degrades_to_not_dismissed(tmp_path: Path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"dismissed": "not-a-list"}), encoding="utf-8")
    assert is_dismissed("any-task", "any title", ledger) is False


# ── The seeded production ledger really matches the known false positives ───


KNOWN_FALSE_POSITIVE_PATHS = [
    "cgp/hot.md",
    "claudette/bubble-gtm-2026-06.md",
    "claudette/bubble-invest-campaign-2026-06.md",
    "claudette/bubble-invest-website-redesign.md",
    "claudette/bubble-labs-vision.md",
    "ben_fund/dispatch-marker-reread-quirk.md",
    "ben_fund/budget-ledger-undercounts-real-spend.md",
    "ben_fund/wiki-access-broken-on-vps.md",
    "ben_fund/fund-vault-gitignored-local-memory.md",
]


@pytest.mark.parametrize("wiki_path", KNOWN_FALSE_POSITIVE_PATHS)
def test_seeded_production_ledger_covers_known_false_positive(wiki_path: str):
    """Board #1395: these 9 pages were re-carded 2-3x each (#1361-1364/#1368,
    #1389-1393, #1410-1412, #1422/1423/1429/1430) before being closed as
    false-positive every time. The shipped ledger must cover every one, using
    the exact real emitter (no override) so this is a true regression test."""
    task = "wiki-intent-candidate-leak"
    title = f"candidate intent leak: {wiki_path}"
    assert is_dismissed(task, title) is True, (
        f"known false positive {wiki_path!r} is missing from the shipped "
        f"dismiss-ledger — it will be re-carded on the next compile"
    )


def test_seeded_ledger_is_valid_json_with_expected_shape():
    data = json.loads(REAL_LEDGER.read_text(encoding="utf-8"))
    assert data.get("schema_version") == 1
    entries = data["dismissed"]
    assert isinstance(entries, list) and len(entries) >= len(KNOWN_FALSE_POSITIVE_PATHS)
    for entry in entries:
        assert isinstance(entry.get("key"), str) and entry["key"]
        assert isinstance(entry.get("reason"), str) and entry["reason"]
