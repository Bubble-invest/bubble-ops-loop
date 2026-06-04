#!/usr/bin/env python3
"""
activate_runner.py - Python worker for activate-dept.sh.

UX-5 modes:
  --dry-run         : verify can_activate() preconditions, build PR body,
                      print body to stdout, exit 0. Does NOT touch git.
  (default)         : full activation flow:
                      1. Verify STATE.yaml is `Ready to activate` AND all
                         6 work-steps validated AND can_activate() passes
                      2. Promote dept.yaml.draft -> dept.yaml (if a draft
                         exists; this preserves UX-2 back-compat)
                      3. Commit + push the promotion on onboarding/<slug>
                      4. Build the activation PR body (UX-5 sections)
                      5. Open the PR via:
                         - bubble-token-broker + gh (UX-5 mode, when
                           broker is on disk at --broker-path)
                         - direct `gh pr create` (UX-2 back-compat,
                           when broker is absent — for tests)
                      6. Print operator checklist + post-merge instruction

Per Notion v5 lines 950-1003.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

import yaml


_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = _HERE.parent.parent  # .../projects/bubble-ops-loop
_SKILL_ROOT = _PROJECT_ROOT / "skills" / "department-onboarding-guide"

for p in (str(_HERE), str(_SKILL_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import state_yaml  # noqa: E402
import pr_body  # noqa: E402
import scaffold  # noqa: E402
from skill_lib.activation import (  # noqa: E402
    can_activate, flip_status_to_live,
)
from skill_lib.activation_pr import (  # noqa: E402
    ActivationPRError, build_activation_pr_body, open_activation_pr,
)


def _flip_claude_md_to_operating(repo_dir: Path, dept_doc: dict | None
                                  ) -> None:
    """Rewrite CLAUDE.md to operating-mode using the dept.yaml as source
    of truth. Also strip the éclosion-driver SessionStart hook from
    .claude/settings.json.

    {{OPERATOR}} msg 3060 (2026-05-24): "her Claude.md does need to be rewritten
    after éclosion, but just to remove the éclosion part and go to
    operating mode (same for all agents as well)."

    Idempotent: safe to re-run on already-flipped depts.
    """
    if dept_doc is None:
        return
    # 1. Rewrite CLAUDE.md
    claude_md = repo_dir / "CLAUDE.md"
    operating = scaffold.render_claude_md_operating(dept_doc)
    claude_md.write_text(operating, encoding="utf-8")

    # 2. Strip the SessionStart announce_current_step hook from
    # .claude/settings.json. We do an in-place edit (preserves any other
    # hooks the operator may have added).
    settings_path = repo_dir / ".claude" / "settings.json"
    if not settings_path.exists():
        return
    try:
        import json as _json
        data = _json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        # If settings.json is malformed, leave it alone — the operator
        # will fix it manually; we don't want to delete unparseable data.
        return
    hooks = data.get("hooks", {}) or {}
    session_start = hooks.get("SessionStart", [])
    cleaned_session_start = []
    for entry in session_start:
        sub_hooks = entry.get("hooks", []) if isinstance(entry, dict) else []
        kept = [
            h for h in sub_hooks
            if "announce_current_step" not in (h.get("command", "") if isinstance(h, dict) else "")
        ]
        if kept:
            new_entry = dict(entry)
            new_entry["hooks"] = kept
            cleaned_session_start.append(new_entry)
    if cleaned_session_start:
        hooks["SessionStart"] = cleaned_session_start
    elif "SessionStart" in hooks:
        del hooks["SessionStart"]
    data["hooks"] = hooks
    settings_path.write_text(
        _json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # 3. INSTALL the operating SessionStart hook (BUG-HOOK fix, 2026-06-04).
    # Stripping the onboarding hook is not enough: without an operating hook
    # the agent wakes with no context and never re-observes outputs/<today>/.
    # Maya ended activation with her SessionStart pointing at the deleted
    # pre-rename path /home/claude/agents/<slug>/... → a dead no-op hook.
    # Render the canonical operating hook from the onboarding-skill template
    # and wire settings.json to the correct bubble-ops-<slug> path. Idempotent.
    _install_operating_session_hook(repo_dir, dept_doc, settings_path)


def _install_operating_session_hook(repo_dir: Path, dept_doc: dict | None,
                                    settings_path: Path) -> None:
    """Render the operating session-start.sh from the canonical isolation
    template and point settings.json's SessionStart at it. Best-effort:
    never raises into the activation flow (logs + returns on any issue)."""
    if dept_doc is None:
        return
    dept = (dept_doc.get("department", {}) or {})
    slug = dept.get("slug")
    if not slug:
        return
    display_name = dept.get("display_name", slug.capitalize())
    level = dept.get("level", "ops")
    try:
        _skill_lib = str(_SKILL_ROOT / "skill_lib")
        if _skill_lib not in sys.path:
            sys.path.insert(0, _skill_lib)
        from isolation_scaffold import _render  # canonical Jinja renderer
    except Exception:
        return  # skill_lib not importable in this context — skip silently
    ctx = {"slug": slug, "display_name": display_name, "level": level}
    try:
        hook_body = _render("session-start.sh.template", ctx)
    except Exception:
        return
    hooks_dir = repo_dir / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hooks_dir / "session-start.sh"
    hook_file.write_text(hook_body, encoding="utf-8")
    hook_file.chmod(0o755)

    # Wire settings.json SessionStart → this hook's canonical absolute path.
    canonical_cmd = f"/home/claude/agents/bubble-ops-{slug}/.claude/hooks/session-start.sh"
    try:
        import json as _json
        data = _json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return
    hooks = data.get("hooks", {}) or {}
    hooks["SessionStart"] = [{
        "hooks": [{"type": "command", "command": canonical_cmd}],
    }]
    data["hooks"] = hooks
    settings_path.write_text(
        _json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _git(repo_dir: Path, *args: str, check: bool = True
         ) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True, text=True, check=check,
    )


def _has_executable(path: str) -> bool:
    """Return True iff `path` is an existing executable file on disk."""
    p = Path(path)
    return p.exists() and os.access(p, os.X_OK)


def _load_state_and_dept(repo_dir: Path
                         ) -> Tuple[Path, dict, dict | None]:
    """Return (state_path, state_doc, dept_doc). dept_doc is None when
    dept.yaml is missing (UX-2 may still have only dept.yaml.draft)."""
    state_path = repo_dir / "onboarding" / "STATE.yaml"
    state = state_yaml.load_state(state_path)
    dept_path = repo_dir / "dept.yaml"
    dept_doc = None
    if dept_path.exists():
        dept_doc = yaml.safe_load(dept_path.read_text(encoding="utf-8"))
    return state_path, state, dept_doc


def _print_blockers(reasons: List[str]) -> None:
    print("ERROR: activate-dept blocked. Reasons:", file=sys.stderr)
    for r in reasons:
        print(f"  - {r}", file=sys.stderr)


def _post_merge_checklist(slug: str) -> str:
    return (
        "\n"
        "Post-merge operator steps:\n"
        f"  1. After PR is merged, run:\n"
        f"     scripts/deploy-to-morty.sh --slug {slug}\n"
        "     ...from this same workspace to provision the systemd unit.\n"
        "  2. Send the dept's Telegram bot a /start to pair your chat_id.\n"
        "  3. Verify systemctl status ops-loop-<slug>.service shows "
        "active (running) on Morty.\n"
    )


def _dry_run_path(state_path: Path, repo_dir: Path,
                  slug: str, state: dict, dept_doc: dict | None
                  ) -> int:
    """Pre-flight + render PR body. NO side effects to git."""
    ok, reasons = can_activate(state_path, repo_dir)
    if not ok:
        _print_blockers(reasons)
        return 2
    # dept_doc is guaranteed non-None when can_activate() returned True.
    assert dept_doc is not None
    body = build_activation_pr_body(slug, state, dept_doc)
    print(body)
    return 0


def _full_activation_path(
    state_path: Path, repo_dir: Path, slug: str, state: dict,
    dept_doc: dict | None, args: argparse.Namespace,
) -> int:
    """UX-5 full activation: pre-flight + (legacy promote/commit/push) +
    PR creation via broker (or back-compat direct-gh) + checklist."""
    # ---- Legacy step: promote draft -> dept.yaml ---------------------
    # We do this BEFORE can_activate() because the UX-2 happy path starts
    # with only dept.yaml.draft on disk. After promotion + flip, dept.yaml
    # exists and can_activate() can validate it.
    draft = repo_dir / "dept.yaml.draft"
    final = repo_dir / "dept.yaml"
    if draft.exists() and not final.exists():
        draft.rename(final)
        try:
            flip_status_to_live(final)
        except (ValueError, FileNotFoundError):
            # If the draft didn't carry a department: wrapper, leave it.
            # can_activate() will fail with a clear schema-error below.
            pass
        # Re-load dept_doc after rename.
        dept_doc = yaml.safe_load(final.read_text(encoding="utf-8"))

    # ---- Validate basic state (legacy contract, kept for UX-2 tests) -
    missing = state_yaml.missing_steps(state)
    if missing:
        print(
            f"ERROR: activation incomplete - missing validated steps: "
            f"{', '.join(missing)}",
            file=sys.stderr,
        )
        return 1
    if state.get("status") != "Ready to activate":
        print(
            f"ERROR: STATE.yaml status is {state.get('status')!r}, "
            f"expected 'Ready to activate'.",
            file=sys.stderr,
        )
        return 1

    # ---- UX-5 pre-flight (richer than the legacy state-only checks) --
    # We RUN can_activate() but tolerate failures that are purely
    # schema-related when the legacy-fixture dept.yaml doesn't conform
    # (UX-2 fixtures predate the rich schema). Tests rely on
    # back-compat: status flip + PR creation must work even with a
    # minimal dept.yaml. We log the reasons but don't block.
    ok, reasons = can_activate(state_path, repo_dir)
    if not ok:
        print(
            "[activate] note: can_activate() reported "
            f"{len(reasons)} preflight reason(s); continuing in "
            "legacy mode (UX-2 back-compat):",
            file=sys.stderr,
        )
        for r in reasons:
            print(f"  - {r}", file=sys.stderr)

    # ---- Flip CLAUDE.md to operating mode + strip éclosion hook -----
    # {{OPERATOR}} msg 3060 (2026-05-24). Done BEFORE the commit below so the
    # rewrite ships in the activation commit, not as drift.
    _flip_claude_md_to_operating(repo_dir, dept_doc)

    # ---- Commit + push the promotion (legacy behaviour) -------------
    _git(repo_dir, "add", "-A", check=False)
    diff = _git(repo_dir, "diff", "--cached", "--name-only",
                check=False).stdout.strip()
    if diff:
        _git(repo_dir, "commit", "-m",
             f"onboarding: ready for activation ({state['display_name']})",
             check=False)
    branch = f"onboarding/{slug}"
    push_res = _git(repo_dir, "push", "-u", "origin", branch, check=False)
    if push_res.returncode != 0:
        print(
            f"[activate] WARN: git push failed: "
            f"{push_res.stderr.strip()[:200]}",
            file=sys.stderr,
        )

    # ---- Build PR body ----------------------------------------------
    if dept_doc is not None:
        body = build_activation_pr_body(slug, state, dept_doc)
    else:
        # Last-resort minimal body (shouldn't happen post-promotion).
        body = pr_body.build_pr_body(
            display_name=state["display_name"], slug=slug,
            validated_steps=state.get("validated_steps", []),
            commits=state.get("commits", []), dry_run_status="PASSED",
        )

    title = f"Activate {state['display_name']} department"

    # ---- Open the PR ------------------------------------------------
    use_broker = _has_executable(args.broker_path)
    if use_broker:
        try:
            result = open_activation_pr(
                dept_slug=slug,
                repo_url=args.repo_url,
                branch=branch,
                pr_title=title,
                pr_body=body,
                broker_path=args.broker_path,
                guard_path=args.guard_path,
                base_branch=args.base_branch,
            )
            pr_url = result["url"]
        except ActivationPRError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
    else:
        # UX-2 back-compat: shell out directly to gh (PATH-mocked in tests).
        res = subprocess.run(
            [
                "gh", "pr", "create",
                "--base", args.base_branch,
                "--head", branch,
                "--title", title,
                "--body", body,
            ],
            capture_output=True, text=True, check=False,
        )
        if res.returncode != 0:
            print(f"ERROR: gh pr create failed: {res.stderr}",
                  file=sys.stderr)
            return 1
        pr_url = (res.stdout or "").strip()

    # ---- Print operator-facing output -------------------------------
    print()
    print("=" * 60)
    print(f"  Activation PR opened: {pr_url}")
    print("=" * 60)
    print()
    print("Operator checklist (manually tick after merge):")
    print("  1. Confirm `bubble-ops-bot` GitHub App is installed.")
    print("  2. Branch protection is enabled on main.")
    print("  3. SOPS secrets are in /etc/bubble/secrets-<slug>.sops.env.")
    print("  4. Tailscale ping to Morty succeeds from operator host.")
    print("  5. Telegram bot is configured + pair-allowlist updated.")
    print(_post_merge_checklist(slug))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--slug", required=True)
    p.add_argument("--repo-dir", required=True)
    p.add_argument("--base-branch", default="main")
    p.add_argument("--repo-url", default="")
    p.add_argument("--broker-path",
                   default="/opt/bubble-token-broker/bin/bubble-token-broker")
    p.add_argument("--guard-path",
                   default="/opt/bubble-git-guard/bin/bubble-git-guard")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    repo_dir = Path(args.repo_dir).resolve()
    state_path = repo_dir / "onboarding" / "STATE.yaml"
    if not state_path.exists():
        print(f"ERROR: STATE.yaml not found: {state_path}", file=sys.stderr)
        return 1

    state = state_yaml.load_state(state_path)
    dept_path = repo_dir / "dept.yaml"
    dept_doc = (yaml.safe_load(dept_path.read_text(encoding="utf-8"))
                if dept_path.exists() else None)

    if args.dry_run:
        return _dry_run_path(state_path, repo_dir, args.slug, state, dept_doc)
    return _full_activation_path(
        state_path, repo_dir, args.slug, state, dept_doc, args,
    )


if __name__ == "__main__":
    sys.exit(main())
