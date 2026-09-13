"""Adversarial tests for the shared-wiki PR-only proposal helper."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/propose_operator_intents.py"
SPEC = importlib.util.spec_from_file_location("propose_operator_intents", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)

PATCH = """diff --git a/shared/operator-intents-proposals/rnd.md b/shared/operator-intents-proposals/rnd.md
new file mode 100644
--- /dev/null
+++ b/shared/operator-intents-proposals/rnd.md
@@ -0,0 +1 @@
+# Proposal
"""


def executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_patch_scope_accepts_only_shared_wiki_proposals() -> None:
    module.validate_patch(PATCH)
    escaped = PATCH.replace(
        "shared/operator-intents-proposals/rnd.md",
        "shared/operator-intents/rnd.md",
    )
    with pytest.raises(module.ProposalError, match="may touch only"):
        module.validate_patch(escaped)


def test_missing_credentials_emits_complete_patch_and_never_runs_git(
    tmp_path: Path, monkeypatch
) -> None:
    patch_file = tmp_path / "proposal.patch"
    patch_file.write_text(PATCH, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_called = tmp_path / "git-called"
    capture = tmp_path / "card-body"
    executable(bin_dir / "gh", "exit 1\n")
    executable(bin_dir / "git", f"touch '{git_called}'\nexit 99\n")
    emitter = tmp_path / "emit"
    executable(
        emitter,
        f'''for arg in "$@"; do
  case "$arg" in body=*) printf '%s' "${{arg#body=}}" > '{capture}' ;; esac
done
exit 0
''',
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    code = module.propose(
        patch_file,
        "proposal/operator-intents-rnd-20260913",
        "intent proposal: rnd",
        "review",
        emitter,
    )
    assert code == 2
    assert not git_called.exists()
    body = capture.read_text(encoding="utf-8")
    assert PATCH.rstrip() in body
    assert "Joris then decides" in body


def test_helper_has_no_private_vault_remote_or_live_wiki_write() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "vdk888/bubble-shared-wiki" in source
    assert "vdk888/bubble-operator-intents.git" not in source
    assert "/home/claude/.claude/agent-memory/shared-wiki" not in source
    assert "git\", \"push\", \"--dry-run" in source
    assert ".data.repository.viewerPermission" in source
