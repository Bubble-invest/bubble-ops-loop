"""Board #1570: intent_alignment_check.py must read operator-intents from the
shared wiki's own layout (bubble-ops-loop#451's model), not the retired
root-owned mirror.

#451 (board #1333, Option C, Joris-approved 2026-09-14) rewired
wiki_intent_audit.py / cloud-wiki-compile.sh to read
``shared/operator-intents/`` straight out of the git-tracked shared wiki, but
deliberately left ``tools/kanban/intent_alignment_check.py`` on the old,
strict, root-owned mirror invariant (out of #451's named scope). Board #1570
is that named follow-up: this tool now reuses #451's own
``validate_intents_root`` (imported, not reimplemented) and a fallback chain
that mirrors cloud-wiki-compile.sh's ``INTENTS_ROOT`` resolution exactly:
``$BUBBLE_OPERATOR_INTENTS_MIRROR`` override first, then the wiki's own
``shared/`` subdirectory, home-relative so it resolves correctly on both the
Mac (``~joris/.claude/agent-memory/shared-wiki``) and the VPS
(``~claude/.claude/agent-memory/shared-wiki``, where the compile pipeline
already runs as the ``claude`` user).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools/kanban/intent_alignment_check.py"
SPEC = importlib.util.spec_from_file_location("intent_alignment_check_1570", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def _seed_wiki(wiki_root: Path) -> None:
    intents = wiki_root / "shared" / "operator-intents"
    intents.mkdir(parents=True)
    (intents / "system-convergence-north-star.md").write_text(
        "---\ntitle: System convergence\nstatus: confirmed\n---\n\n# Ask\n",
        encoding="utf-8",
    )


def test_reuses_451s_validator_not_a_parallel_one() -> None:
    """The whole point of #1570: import, don't reimplement."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "from wiki_intent_audit import" in source
    assert "validate_intents_root" in source
    assert "from readonly_intents_mirror import" not in source
    assert "import readonly_intents_mirror" not in source


def test_default_wiki_dir_is_home_relative_not_a_single_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert module.default_wiki_dir() == tmp_path / ".claude" / "agent-memory" / "shared-wiki"


def test_intents_root_prefers_env_override_when_it_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    mirror = tmp_path / "external-mirror"
    (mirror / "operator-intents").mkdir(parents=True)
    monkeypatch.setenv(module.MIRROR_ENV, str(mirror))
    assert module.default_intents_root() == mirror


def test_intents_root_falls_back_to_wiki_shared_when_no_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(module.MIRROR_ENV, raising=False)
    home = tmp_path / "home"
    _seed_wiki(home / ".claude" / "agent-memory" / "shared-wiki")
    monkeypatch.setenv("HOME", str(home))
    expected = home / ".claude" / "agent-memory" / "shared-wiki" / "shared"
    assert module.default_intents_root() == expected


def test_intents_root_ignores_a_nonexistent_override_and_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-existing-wins, exactly like cloud-wiki-compile.sh's chain: a
    stale/misconfigured override must not blind the tool to a perfectly good
    wiki default."""
    home = tmp_path / "home"
    _seed_wiki(home / ".claude" / "agent-memory" / "shared-wiki")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(module.MIRROR_ENV, str(tmp_path / "does-not-exist"))
    expected = home / ".claude" / "agent-memory" / "shared-wiki" / "shared"
    assert module.default_intents_root() == expected


def test_no_source_resolves_names_the_real_wiki_path_in_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(module.MIRROR_ENV, raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    expected_path = str(home / ".claude" / "agent-memory" / "shared-wiki" / "shared")
    with pytest.raises(module.IntentsRootValidationError, match=expected_path.replace("\\", "\\\\")):
        module.validate_intents_root(module.default_intents_root())


def _write_fake_gh(bin_dir: Path) -> None:
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "${1:-} ${2:-}" in\n'
        '  "issue list") echo \'[]\' ;;\n'
        '  "search prs") echo \'[]\' ;;\n'
        "  *) echo '[]' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)


def test_cli_end_to_end_resolves_intents_from_the_real_wiki_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exercises the real (non-test-override) resolver path end to end: a
    plain wiki-layout directory — no root ownership, no symlink, no
    manifest — must produce a usable taxonomy, matching the proof required by
    board #1570."""
    home = tmp_path / "home"
    _seed_wiki(home / ".claude" / "agent-memory" / "shared-wiki")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(module.MIRROR_ENV, raising=False)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_gh(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    code = module.main(["--format", "json"])
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["taxonomy"] == [
        {
            "slug": "system-convergence-north-star",
            "label": "intent:system-convergence-north-star",
            "title": "System convergence",
            "status": "confirmed",
            "path": "shared/operator-intents/system-convergence-north-star.md",
            "relationships": [],
        }
    ]
    assert output["summary"] == {
        "open_issues": 0,
        "orphan_issues": 0,
        "open_prs": 0,
        "orphan_prs": 0,
    }


def test_cli_reports_a_clear_error_when_nothing_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.delenv(module.MIRROR_ENV, raising=False)

    code = module.main(["--format", "json"])
    assert code == 1
    assert "operator-intents source validation failed" in capsys.readouterr().err
