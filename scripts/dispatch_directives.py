#!/usr/bin/env python3
"""dispatch_directives — deliver {{OPERATOR}}-approved CEO directives to child depts.

THE PROBLEM IT SOLVES
---------------------
Tony (management dept) issues directives to his child depts, but he is ISOLATED:
his settings.json `Write` allow-list is `bubble-ops-tony/**` only, and his broker
policy is `own_repo: bubble-ops-tony` — he cannot write into, or PR against, a
child's repo. The existing `skills.directive_writer.emit_directive` writes a file
into the child's on-disk clone but (a) Tony can't run it (cross-tree write denied)
and (b) it never commits/pushes, so the file is wiped on the child's next reset.

THE FLOW (isolation-preserving — only THIS dispatcher crosses the boundary)
---------------------------------------------------------------------------
1. Tony (in his OWN repo, which he can push) writes approved directives to
   `bubble-ops-tony/queues/management/outbound/directive-<id>.yaml`, each carrying
   `target_dept` + `approved_by: joris` + `status: approved`.
2. This dispatcher (run by the layer-floor systemd tick as the `claude` user — the
   ONLY actor allowed to cross repos, exactly like loop-backup is the only thing
   that runs depts-not-itself) reads those, and for each `status: approved`:
     - writes `queues/management/directive-<id>.yaml` into the target child repo,
     - commits + pushes the CHILD repo (claude-user GitHub-App token, per-repo),
     - rewrites the source to `status: dispatched` (+ dispatched_at) and pushes Tony.
3. The child reads `queues/management/` for new `directive-*.yaml` at layer start.

HARD SAFETY RULES
-----------------
- APPROVAL GATE: a directive is dispatched ONLY if `approved_by == "joris"` AND
  `status == "approved"`. Anything else is skipped (logged). {{OPERATOR}} must approve
  before dispatch — there is no autonomous emission.
- ISOLATION: the dispatcher only writes child `queues/management/**` (a
  non-structural, allow-listed runtime path). It never touches a child's mission
  files, settings, or anything outside that inbox dir.
- IDEMPOTENT: a directive already `dispatched`, or already present in the child
  inbox, is a no-op. Safe to run on every tick (all 4 moments).
- NEVER FATAL: a delivery failure for one directive logs and continues; the tick
  must not abort. Mirrors loop-backup's posture.

NOT an LLM call. Pure mechanical relay (template Ban #2 forbids `claude -p`).
Deterministic, free, unit-testable.

Usage:
    dispatch_directives.py [--agents-root DIR] [--manager SLUG] [--dry-run]

Defaults: --agents-root /home/claude/agents, --manager tony.
Exit 0 always (delivery errors are per-directive, logged, non-fatal) unless a
structural precondition fails (manager repo missing) → exit 1.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("dispatch_directives: PyYAML required", file=sys.stderr)
    sys.exit(1)

_OUTBOUND_REL = "queues/management/outbound"
_INBOX_REL = "queues/management"
_CRED_HELPER = "/usr/local/bin/bubble-gh-credential-helper.sh"
_GH_ORG = "Bubble-invest"


def _log(msg: str) -> None:
    ts = _now_iso()
    print(f"[{ts}] [dispatch-directives] {msg}", flush=True)


def _now_iso() -> str:
    # UTC, second precision. Passed in by callers in tests via monkeypatch if needed.
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd: list[str], cwd: "Path | None" = None, stdin: "str | None" = None):
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, input=stdin,
        capture_output=True, text=True,
    )


def _mint_token(repo_name: str) -> "str | None":
    """Mint a short-lived GitHub App token for Bubble-invest/<repo_name> via the
    sudo-wrapped credential helper. Returns the ghs_ token or None on failure.
    The helper resolves the installation from path=org/repo and mints
    contents:write for a non-structural delta (queues/** is non-structural)."""
    res = _run(
        ["sudo", "-n", _CRED_HELPER, "get"],
        stdin=(
            "protocol=https\nhost=github.com\n"
            f"path={_GH_ORG}/{repo_name}.git\n\n"
        ),
    )
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        if line.startswith("password="):
            tok = line.split("=", 1)[1].strip()
            return tok if tok.startswith("ghs_") else None
    return None


def _push_repo(repo_dir: Path, repo_name: str, message: str, dry_run: bool) -> tuple[bool, str]:
    """Stage all, commit, push repo_dir's OWN remote via a freshly-minted token.
    Returns (ok, detail). Clean tree → (True, 'nothing to commit')."""
    status = _run(["git", "-C", str(repo_dir), "status", "--porcelain"])
    if status.returncode != 0:
        return False, f"git status failed: {status.stderr.strip()[:160]}"
    if not status.stdout.strip():
        return True, "nothing to commit"
    if dry_run:
        return True, f"[dry-run] would commit+push {repo_name}: {message!r}"
    if _run(["git", "-C", str(repo_dir), "add", "-A"]).returncode != 0:
        return False, "git add failed"
    commit = _run(["git", "-C", str(repo_dir), "commit", "-m", message])
    if commit.returncode != 0:
        out = (commit.stdout + commit.stderr).lower()
        if "nothing to commit" in out:
            return True, "nothing to commit"
        return False, f"git commit failed: {(commit.stderr or commit.stdout).strip()[:160]}"
    token = _mint_token(repo_name)
    if not token:
        return False, f"could not mint token for {repo_name}"
    url = f"https://x-access-token:{token}@github.com/{_GH_ORG}/{repo_name}.git"
    push = _run(["git", "-C", str(repo_dir), "push", url, "HEAD:main"])
    # Never let the token live in git config; we passed it inline only.
    if push.returncode != 0:
        return False, f"push rejected: {(push.stderr or push.stdout).strip()[:200]}"
    return True, "pushed"


def _load_yaml(p: Path) -> "dict | None":
    try:
        with p.open() as fh:
            d = yaml.safe_load(fh)
        return d if isinstance(d, dict) else None
    except Exception as exc:  # noqa: BLE001
        _log(f"WARN: unreadable directive {p.name}: {exc}")
        return None


def dispatch(agents_root: Path, manager: str, dry_run: bool) -> int:
    manager_repo = agents_root / f"bubble-ops-{manager}"
    if not (manager_repo / ".git").is_dir():
        _log(f"FATAL: manager repo not a git tree: {manager_repo}")
        return 1
    outbound = manager_repo / _OUTBOUND_REL
    if not outbound.is_dir():
        _log(f"no outbound dir ({outbound}) — nothing to dispatch")
        return 0

    drafts = sorted(outbound.glob("directive-*.yaml"))
    if not drafts:
        _log("outbound empty — nothing to dispatch")
        return 0

    delivered = 0
    skipped = 0
    failed = 0
    manager_dirty = False

    for draft in drafts:
        d = _load_yaml(draft)
        if d is None:
            skipped += 1
            continue
        did = str(d.get("directive_id") or draft.stem.replace("directive-", ""))
        target = d.get("target_dept")
        approved_by = d.get("approved_by")
        status = d.get("status")

        # ── APPROVAL GATE ──────────────────────────────────────────────
        if status == "dispatched":
            continue  # idempotent: already done
        if approved_by != "joris" or status != "approved":
            _log(f"SKIP {draft.name}: gate not satisfied "
                 f"(approved_by={approved_by!r} status={status!r}) — {{OPERATOR}} must approve")
            skipped += 1
            continue
        if not target or not isinstance(target, str):
            _log(f"SKIP {draft.name}: missing/invalid target_dept")
            skipped += 1
            continue

        child_repo = agents_root / f"bubble-ops-{target}"
        if not (child_repo / ".git").is_dir():
            _log(f"FAIL {draft.name}: target child repo missing: {child_repo}")
            failed += 1
            continue

        # ── DELIVER into child queues/management/ ──────────────────────
        inbox = child_repo / _INBOX_REL
        inbox.mkdir(parents=True, exist_ok=True)
        dest = inbox / f"directive-{did}.yaml"

        # The delivered file is the directive payload, minus dispatcher bookkeeping.
        payload = {k: v for k, v in d.items() if k not in ("status",)}
        payload["delivered_at"] = _now_iso()
        payload.setdefault("from", manager)

        if dest.exists():
            _log(f"NO-OP {draft.name}: already present in {target} inbox")
        else:
            if dry_run:
                _log(f"[dry-run] would write {dest}")
            else:
                dest.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

        ok, detail = _push_repo(
            child_repo, f"bubble-ops-{target}",
            f"directive: deliver {did} from {manager} ({{OPERATOR}}-approved)", dry_run,
        )
        if not ok:
            _log(f"FAIL deliver {did} -> {target}: {detail}")
            failed += 1
            # roll back the just-written file so we retry cleanly next tick
            if dest.exists() and not dry_run:
                _run(["git", "-C", str(child_repo), "checkout", "--", str(dest)])
                if dest.exists():
                    dest.unlink(missing_ok=True)
            continue

        _log(f"OK delivered {did} -> {target} ({detail})")
        delivered += 1

        # ── MARK source dispatched (in Tony's repo) ────────────────────
        if not dry_run:
            d["status"] = "dispatched"
            d["dispatched_at"] = _now_iso()
            draft.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
            manager_dirty = True

    # Push Tony's repo once if any source was marked dispatched.
    if manager_dirty:
        ok, detail = _push_repo(
            manager_repo, f"bubble-ops-{manager}",
            f"directive: mark {delivered} dispatched", dry_run,
        )
        _log(f"manager status push: {detail}" if ok else f"WARN manager push: {detail}")

    _log(f"done: delivered={delivered} skipped={skipped} failed={failed}")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="dispatch_directives.py")
    ap.add_argument("--agents-root", default="/home/claude/agents")
    ap.add_argument("--manager", default="tony")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    return dispatch(Path(args.agents_root), args.manager, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
