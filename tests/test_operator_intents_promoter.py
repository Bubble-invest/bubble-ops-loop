"""Adversarial tests for the approved-intent vault-promotion helper."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/promote_operator_intents.py"
SPEC = importlib.util.spec_from_file_location("promote_operator_intents", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(module)

PATCH = """diff --git a/operator-intents/example-intent.md b/operator-intents/example-intent.md
new file mode 100644
--- /dev/null
+++ b/operator-intents/example-intent.md
@@ -0,0 +1 @@
+# Example
"""


def executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_patch_scope_accepts_only_operator_intents() -> None:
    module.validate_patch(PATCH)
    escaped = PATCH.replace(
        "operator-intents/example-intent.md",
        "operator-intents-proposals/example-intent.md",
    )
    with pytest.raises(module.PromotionError, match="may touch only"):
        module.validate_patch(escaped)


def test_helper_never_pushes_or_merges_main() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "HEAD:refs/heads/main" not in source
    assert '"merge"' not in source
    assert "gh pr merge" not in source
    assert module.VAULT_REPO == "Bubble-invest/bubble-operator-intents"
    assert module.VAULT_REMOTE == "git@github.com:Bubble-invest/bubble-operator-intents.git"
    assert ".data.repository.viewerPermission" in source


def test_missing_approval_comment_blocks_without_touching_git(
    tmp_path: Path, monkeypatch
) -> None:
    patch_file = tmp_path / "promotion.patch"
    patch_file.write_text(PATCH, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_called = tmp_path / "git-called"
    capture = tmp_path / "card-body"
    # gh api ... /comments returns no comments at all -> no approval evidence.
    executable(
        bin_dir / "gh",
        f'''case "$*" in
  *"/comments --jq"*) exit 0 ;;
  *) exit 1 ;;
esac
''',
    )
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

    code = module.promote(
        patch_file,
        "promote/example-intent-20260914",
        "promote: example",
        "review",
        1326,
        approved_inline=False,
        emit=emitter,
    )
    assert code == 2
    assert not git_called.exists()
    body = capture.read_text(encoding="utf-8")
    assert PATCH.rstrip() in body
    assert "no '✅ APPROVED by Joris' comment" in body


def test_approval_comment_must_come_from_an_allowed_approver() -> None:
    with pytest.raises(TypeError):
        # check_approval requires the issue argument; guards against silent
        # signature drift that would make the approval gate optional.
        module.check_approval()  # type: ignore[call-arg]


def test_inline_approval_requires_explicit_flag_and_marker(tmp_path: Path, monkeypatch) -> None:
    patch_file = tmp_path / "promotion.patch"
    patch_file.write_text(PATCH, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git_called = tmp_path / "git-called"
    capture = tmp_path / "card-body"
    executable(
        bin_dir / "gh",
        f'''case "$*" in
  *"/comments --jq"*) exit 0 ;;
  *"issues/1328 --jq .body"*) printf 'no marker here'; exit 0 ;;
  *) exit 1 ;;
esac
''',
    )
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

    code = module.promote(
        patch_file,
        "promote/example-intent-20260914",
        "promote: example",
        "review",
        1328,
        approved_inline=True,
        emit=emitter,
    )
    assert code == 2
    assert not git_called.exists()
    body = capture.read_text(encoding="utf-8")
    assert "inline-approval marker" in body


def test_helper_has_no_shared_wiki_write_and_targets_vault_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "vdk888/bubble-shared-wiki" not in source
    # The docstring may reference the sibling proposal path for context; the
    # CODE (not prose) must never reference it as something this script writes.
    assert module.ALLOWED_PREFIX == "operator-intents/"
    assert 'git", "push", "--dry-run' in source
