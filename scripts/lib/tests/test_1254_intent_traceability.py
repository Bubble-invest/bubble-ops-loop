"""Regression coverage for board #1254 intent lineage."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tools/kanban/intent_alignment_check.py"
EMITTER = REPO / "tools/kanban/emit_kanban_item.sh"
DRAIN = REPO / "tools/kanban/drain_kanban_queue.sh"
SPEC = importlib.util.spec_from_file_location("intent_alignment_check", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def intent_doc(
    title: str,
    *,
    status: str = "confirmed",
    parent: str | None = None,
    children: list[str] | None = None,
    dependencies: list[str] | None = None,
) -> str:
    lines = ["---", f"title: {title}", f"status: {status}"]
    if parent:
        lines.append(f'parent: "[[shared/operator-intents/{parent}]]"')
    for field, values in (("children", children), ("dependencies", dependencies)):
        if values is not None:
            lines.append(f"{field}:")
            lines.extend(f'  - "[[shared/operator-intents/{value}]]"' for value in values)
    lines.extend(["---", f"# {title}", "", "## Ask", "Keep it aligned."])
    return "\n".join(lines) + "\n"


def seed_intents() -> dict[str, object]:
    return module.parse_intent_documents(
        {
            "README.md": "not taxonomy\n",
            "system-convergence.md": intent_doc(
                "System convergence", children=["controlled-rnd"]
            ),
            "controlled-rnd.md": intent_doc(
                "Controlled R&D",
                parent="system-convergence",
                dependencies=["operator-control"],
            ),
            "operator-control.md": intent_doc("Operator control"),
            "old-direction.md": intent_doc("Old direction", status="superseded"),
        }
    )


def test_taxonomy_chain_orphans_and_contradiction_stay_structural() -> None:
    intents = seed_intents()
    issues = [
        {
            "number": 1,
            "title": "Linked by label",
            "body": "work",
            "labels": [{"name": "intent:controlled-rnd"}],
        },
        {
            "number": 2,
            "title": "Invalid body target",
            "body": "Serves-intent(s): [[shared/operator-intents/not-live]]",
            "labels": [],
        },
        {
            "number": 3,
            "title": "Superseded target",
            "body": "Serves-intent(s): intent:old-direction",
            "labels": [],
        },
        {
            "number": 4,
            "title": "Extension is not canonical",
            "body": "Serves-intent(s): [[shared/operator-intents/operator-control.md]]",
            "labels": [],
        },
        {
            "number": 5,
            "title": "Wrong case is invalid",
            "body": "Serves-intent(s): [[shared/operator-intents/Operator-Control]]",
            "labels": [],
        },
    ]
    report = module.build_report(intents, issues, [], limit=1000, pr_owner_counts={})
    by_number = {row["number"]: row for row in report["issues"]}

    assert by_number[1]["intent_chain"] == [
        "controlled-rnd",
        "operator-control",
        "system-convergence",
    ]
    assert by_number[1]["orphan"] is False
    assert by_number[1]["contradiction"] is None
    assert by_number[1]["judgment"] == "agent_required"
    assert by_number[2]["invalid_intents"] == ["not-live"]
    assert by_number[2]["orphan"] is True
    assert by_number[3]["superseded_intents"] == ["old-direction"]
    assert by_number[3]["orphan"] is True
    assert by_number[4]["malformed_intents"] == ["operator-control.md"]
    assert by_number[4]["orphan"] is True
    assert by_number[5]["invalid_intents"] == ["Operator-Control"]
    assert by_number[5]["orphan"] is True
    assert [row["label"] for row in report["taxonomy"]] == [
        "intent:controlled-rnd",
        "intent:operator-control",
        "intent:system-convergence",
    ]
    assert report["policy"]["missing_intent"] == "FLAG_ORPHAN_NEVER_BLOCK"


def test_pr_inherits_only_from_explicit_cross_repo_closing_reference() -> None:
    intents = seed_intents()
    issue = {
        "number": 1254,
        "title": "Board work",
        "body": "Serves-intent(s): [[shared/operator-intents/controlled-rnd]]",
        "labels": [],
    }
    prs = [
        {
            "repository": {"nameWithOwner": "Bubble-invest/bubble-ops-loop"},
            "number": 8,
            "title": "Correct cross-repo link",
            "body": "Closes Bubble-invest/bubble-ops-board#1254",
            "labels": [],
        },
        {
            "repository": {"nameWithOwner": "Bubble-invest/bubble-ops-loop"},
            "number": 9,
            "title": "Bare reference belongs to this repo",
            "body": "Closes #1254",
            "labels": [],
        },
        {
            "repository": {"nameWithOwner": "Bubble-invest/bubble-ops-loop"},
            "number": 10,
            "title": "Direct body link",
            "body": "Serves-intent(s): intent:operator-control",
            "labels": [],
        },
        {
            "repository": {"nameWithOwner": "Bubble-invest/bubble-ops-loop"},
            "number": 11,
            "title": "Inherit from closed card",
            "body": "Resolves https://github.com/Bubble-invest/bubble-ops-board/issues/99",
            "labels": [],
        },
    ]
    closed_issue = {
        "number": 99,
        "title": "Already closed board work",
        "body": "Serves-intent(s): intent:operator-control",
        "labels": [],
        "state": "CLOSED",
    }
    report = module.build_report(intents, [issue], prs, linked_issues=[closed_issue])
    by_number = {row["number"]: row for row in report["pull_requests"]}

    assert by_number[8]["linked_board_cards"] == [1254]
    assert by_number[8]["inherited_intents"] == ["controlled-rnd"]
    assert by_number[8]["orphan"] is False
    assert by_number[9]["linked_board_cards"] == []
    assert by_number[9]["orphan"] is True
    assert by_number[10]["resolved_intents"] == ["operator-control"]
    assert by_number[11]["inherited_intents"] == ["operator-control"]
    assert by_number[11]["linked_cards_unresolved"] == []
    assert report["summary"]["open_issues"] == 1


def test_backfill_is_bounded_proposal_only_and_label_plan_never_removes() -> None:
    intents = seed_intents()
    issues = [
        {"number": number, "title": f"Orphan {number}", "body": "", "labels": []}
        for number in range(1, 5)
    ]
    prs = [
        {
            "repository": {"nameWithOwner": "vdk888/example"},
            "number": 7,
            "title": "Orphan PR",
            "body": "",
            "labels": [],
        }
    ]
    report = module.build_report(intents, issues, prs)
    backfill = module.backfill_batch(report, 3, offset=0)

    assert backfill["mutation"] is False
    assert [row["number"] for row in backfill["candidates"]] == [1, 2, 3]
    assert backfill["remaining_after_batch"] == 2
    assert backfill["selection"]["next_offset"] == 3
    assert all(row["suggested_intents"] == [] for row in backfill["candidates"])
    assert all(row["decision"] == "agent_required" for row in backfill["candidates"])

    # The loop persists next_offset externally.  Early unresolved rows remain
    # candidates, but the next pass still reaches later cards and the PR.
    second = module.backfill_batch(
        report, 3, offset=backfill["selection"]["next_offset"]
    )
    assert [(row["kind"], row["number"]) for row in second["candidates"]] == [
        ("issue", 4),
        ("pull_request", 7),
        ("issue", 1),
    ]
    assert second["selection"]["wrapped"] is True

    plan = module.label_plan(intents, {"Intent:controlled-rnd", "intent:legacy"})
    assert plan["mutation"] is False
    assert "intent:operator-control" in plan["missing"]
    assert "intent:controlled-rnd" not in plan["missing"]
    assert plan["noncanonical_casing"] == [
        {"expected": "intent:controlled-rnd", "actual": "Intent:controlled-rnd"}
    ]
    assert plan["obsolete_not_removed"] == ["intent:legacy"]


def test_coverage_warns_at_each_query_ceiling() -> None:
    intents = seed_intents()
    issues = [{"number": 1, "title": "x", "body": "", "labels": []}]
    report = module.build_report(
        intents,
        issues,
        [],
        limit=1,
        pr_owner_counts={"Bubble-invest": 0, "vdk888": 1},
    )
    assert report["coverage"]["complete"] is False
    assert len(report["coverage"]["warnings"]) == 2
    assert "open issue query" in report["coverage"]["warnings"][0]
    assert "vdk888" in report["coverage"]["warnings"][1]
    backfill = module.backfill_batch(report, 1, offset=0)
    assert backfill["coverage"]["complete"] is False
    assert backfill["coverage"]["warnings"] == report["coverage"]["warnings"]


def test_cli_backfill_limit_one_cannot_hide_incomplete_coverage(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    mirror = tmp_path / "mirror"
    intent_dir = mirror / "operator-intents"
    intent_dir.mkdir(parents=True)
    (intent_dir / "live.md").write_text(intent_doc("Live intent"), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
case "${1:-} ${2:-}" in
  "issue list")
    echo '[{"number":1,"title":"orphan","body":"","url":"https://example/i/1","labels":[],"createdAt":"2026-01-01"}]' ;;
  "search prs")
    echo '[{"number":2,"title":"orphan pr","body":"","url":"https://example/p/2","repository":{"nameWithOwner":"Bubble-invest/repo"},"labels":[],"createdAt":"2026-01-01"}]' ;;
  *) exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    code = module.main(
        [
            "--mode",
            "backfill",
            "--batch-size",
            "1",
            "--limit",
            "1",
            "--format",
            "json",
        ],
        _validated_release_for_tests=mirror,
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["coverage"]["complete"] is False
    assert len(output["coverage"]["warnings"]) == 3
    assert output["selection"]["selected_count"] == 1


def test_default_intent_source_is_os_mirror_never_github(monkeypatch) -> None:
    monkeypatch.setenv("BUBBLE_OPERATOR_INTENTS_MIRROR", "/controlled/mirror")
    assert module.default_mirror_root() == Path("/controlled/mirror")
    source = SCRIPT.read_text(encoding="utf-8")
    assert "load_intents_from_github" not in source
    assert "--intent-repo" not in source
    assert "--wiki-root" not in source
    assert "--intent-root" not in source
    assert "gh\", \"api\"" not in source


def test_default_mirror_validation_fails_closed_on_writable_fixture(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    with pytest.raises(module.MirrorValidationError, match="not a stable symlink"):
        module.validate_mirror(mirror)


def fake_gh(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env bash
set -u
case "${1:-} ${2:-}" in
  "auth status") exit 0 ;;
  "api repos/Bubble-invest/bubble-ops-board") echo 'bubble-ops-board'; exit 0 ;;
  "issue list") exit 0 ;;
  "label create") exit 0 ;;
  "label list")
    # Model gh's --jq semantics closely enough that the old case-sensitive
    # `startswith("intent:")` prefilter drops `Intent:foo`.  The production
    # code must request all names and perform its casefold in shell.
    if [[ "$*" == *'startswith("intent:")'* && "${INSTALLED_LABEL:-}" != intent:* ]]; then
      exit 0
    fi
    [ -n "${INSTALLED_LABEL:-}" ] && echo "${INSTALLED_LABEL}"
    exit 0 ;;
  "issue create")
    args=("$@")
    for i in "${!args[@]}"; do
      if [ "${args[$i]}" = "--body-file" ]; then
        cp "${args[$((i+1))]}" "$CAPTURE_BODY"
      elif [ "${args[$i]}" = "--label" ]; then
        echo "${args[$((i+1))]}" >> "$CAPTURE_LABELS"
      fi
    done
    echo 'https://github.com/Bubble-invest/bubble-ops-board/issues/999'
    exit 0 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def emitter_env(tmp_path: Path, installed_label: str = "") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh(bin_dir / "gh")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "INSTALLED_LABEL": installed_label,
            "CAPTURE_BODY": str(tmp_path / "body.md"),
            "CAPTURE_LABELS": str(tmp_path / "labels.txt"),
            "TELEGRAM_BOT_TOKEN": "",
            "BUBBLE_OPERATOR_CHAT_ID": "",
        }
    )
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return env


def test_emitter_records_body_and_only_applies_installed_intent_label(tmp_path: Path) -> None:
    env = emitter_env(tmp_path, "Intent:system-convergence-north-star")
    result = subprocess.run(
        [
            "bash", str(EMITTER), "task=traceability-test", "title=Trace this card",
            "budget=5", "owner=rnd", "intent=intent:system-convergence-north-star",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    body = Path(env["CAPTURE_BODY"]).read_text(encoding="utf-8")
    labels = Path(env["CAPTURE_LABELS"]).read_text(encoding="utf-8").splitlines()
    assert (
        "Serves-intent(s): [[shared/operator-intents/system-convergence-north-star]]"
        in body
    )
    assert "Intent:system-convergence-north-star" in labels
    assert "normalized intent=" in result.stderr
    assert "label casing drift" in result.stderr


def test_emitter_missing_label_or_intent_warns_but_still_creates(tmp_path: Path) -> None:
    with_slug = tmp_path / "with-slug"
    with_slug.mkdir()
    env = emitter_env(with_slug)
    result = subprocess.run(
        [
            "bash", str(EMITTER), "task=missing-label", "title=Still create one",
            "budget=5", "owner=rnd", "intent=system-convergence-north-star",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "label intent:system-convergence-north-star is not installed" in result.stderr
    assert Path(env["CAPTURE_BODY"]).exists()
    assert "intent:system-convergence-north-star" not in Path(
        env["CAPTURE_LABELS"]
    ).read_text(encoding="utf-8")

    no_slug = tmp_path / "no-slug"
    no_slug.mkdir()
    env2 = emitter_env(no_slug)
    result2 = subprocess.run(
        [
            "bash", str(EMITTER), "task=no-intent", "title=Urgent orphan still lands",
            "budget=5", "owner=rnd",
        ],
        env=env2,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "FLAGGED as an intent ORPHAN" in result2.stderr
    assert "Serves-intent(s): UNRESOLVED" in Path(env2["CAPTURE_BODY"]).read_text(
        encoding="utf-8"
    )

    md_slug = tmp_path / "md-slug"
    md_slug.mkdir()
    env3 = emitter_env(md_slug, "intent:system-convergence-north-star")
    result3 = subprocess.run(
        [
            "bash", str(EMITTER), "task=md-intent", "title=Do not bless suffix",
            "budget=5", "owner=rnd",
            "intent=[[shared/operator-intents/system-convergence-north-star.md]]",
        ],
        env=env3,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "no usable intent=" in result3.stderr
    assert "Serves-intent(s): UNRESOLVED" in Path(env3["CAPTURE_BODY"]).read_text(
        encoding="utf-8"
    )


def test_fallback_queue_and_drain_preserve_intent(tmp_path: Path) -> None:
    fail_bin = tmp_path / "fail-bin"
    fail_bin.mkdir()
    (fail_bin / "gh").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (fail_bin / "gh").chmod(0o755)
    queue = tmp_path / "queue.jsonl"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fail_bin}:{env['PATH']}",
            "KANBAN_QUEUE": str(queue),
            "TELEGRAM_BOT_TOKEN": "",
            "BUBBLE_OPERATOR_CHAT_ID": "",
        }
    )
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    result = subprocess.run(
        [
            "bash", str(EMITTER), "task=queued-intent", "title=Queued lineage",
            "budget=5", "owner=rnd", "intent=system-convergence-north-star",
            "body=first line\nsecond line",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # Board #1251: the emitter now exits non-zero whenever the card falls to
    # the local queue instead of reaching the board (fail loud) — it is NOT
    # exit 0 here. The queue write itself (asserted below) still happens;
    # only the exit code changed from the old always-0 contract.
    assert result.returncode != 0, (
        f"expected non-zero exit for a queued (not-on-board) emit, got "
        f"{result.returncode}: stderr={result.stderr!r}"
    )
    payload = json.loads(queue.read_text(encoding="utf-8"))
    assert payload["kanban_items"][0]["intents"] == ["system-convergence-north-star"]
    assert payload["kanban_items"][0]["body"] == "first line\nsecond line"

    drain_dir = tmp_path / "drain-bin"
    drain_dir.mkdir()
    fake_gh(drain_dir / "gh")
    drain_env = os.environ.copy()
    drain_env.update(
        {
            "PATH": f"{drain_dir}:{drain_env['PATH']}",
            "KANBAN_QUEUE": str(queue),
            "INSTALLED_LABEL": "intent:system-convergence-north-star",
            "CAPTURE_BODY": str(tmp_path / "drained-body.md"),
            "CAPTURE_LABELS": str(tmp_path / "drained-labels.txt"),
        }
    )
    drain_env.pop("GH_TOKEN", None)
    drain_env.pop("GITHUB_TOKEN", None)
    subprocess.run(["bash", str(DRAIN)], env=drain_env, check=True, capture_output=True)
    assert (
        "Serves-intent(s): [[shared/operator-intents/system-convergence-north-star]]"
        in Path(drain_env["CAPTURE_BODY"]).read_text(encoding="utf-8")
    )
    assert "first line\nsecond line" in Path(drain_env["CAPTURE_BODY"]).read_text(
        encoding="utf-8"
    )
    assert "intent:system-convergence-north-star" in Path(
        drain_env["CAPTURE_LABELS"]
    ).read_text(encoding="utf-8")
