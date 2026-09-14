#!/usr/bin/env python3
"""Plan and explicitly acknowledge due missions for a Mac-hosted dept."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import shlex
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.lib.loop_backup import (
    _MISSION_LIVE_STATUS,
    DueMissionConfigError,
    claim_due_missions,
    due_mission_plan,
    due_watermark_path,
    read_due_watermarks,
    release_due_claims,
    write_due_success,
)


def _now(epoch: int | None) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) if epoch is None else dt.datetime.fromtimestamp(
        epoch, dt.timezone.utc
    )


def _load_manifest(dept_dir: Path) -> dict:
    path = dept_dir / "dept.yaml"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise DueMissionConfigError(f"cannot read dept manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise DueMissionConfigError("dept manifest root must be a mapping")
    return value


def _validate_scoped_files(dept_dir: Path, manifest: dict) -> None:
    config = manifest.get("loop", {}).get("due_dispatch")
    if config is None:
        return
    scope = config.get("mission_ids", []) if isinstance(config, dict) else []
    raw = manifest.get("recurring_missions", [])
    by_id = {item.get("id"): item for item in raw if isinstance(item, dict)}
    for mission_id in scope:
        mission = by_id.get(mission_id)
        if not isinstance(mission, dict):
            continue  # the core validator emits the precise structural error
        # #1317: a non-live (planned/unknown-status) mission is never dispatched,
        # so its mission_file is not a precondition of this tick. Mirror the
        # due_mission_plan filter here so an unbuilt planned mission that has no
        # file yet cannot raise and starve the live missions' dispatch.
        if mission.get("status") != _MISSION_LIVE_STATUS:
            continue
        relative = mission.get("mission_file")
        if isinstance(relative, str) and not (dept_dir / relative).is_file():
            raise DueMissionConfigError(f"{mission_id}: mission file does not exist: {relative}")
    layers = manifest.get("layers")
    subscribed = layers.get("subscribed") if isinstance(layers, dict) else None
    if not isinstance(subscribed, list) or not subscribed:
        raise DueMissionConfigError("layers.subscribed must be a non-empty list")
    for layer in subscribed:
        if not isinstance(layer, int) or layer not in {1, 2, 3, 4}:
            raise DueMissionConfigError("layers.subscribed must contain only 1..4")
        if not (dept_dir / "layers" / str(layer) / "PROMPT.md").is_file():
            raise DueMissionConfigError(f"layer prompt does not exist: layers/{layer}/PROMPT.md")


def _prompt(plan: list[dict], dept_dir: Path) -> str:
    script = Path(__file__).resolve()
    items = ";".join(
        f"{item['id']}{{cadence={item['cadence']},period={item['period']},"
        f"layers={'/'.join(map(str, item['layers']))},file={item['mission_file']}}}"
        for item in plan
    )
    commands = []
    for item in plan:
        # Board #1330: a hardcoded "python3" resolves NON-DETERMINISTICALLY on
        # a Mac with more than one python3 on PATH (e.g. homebrew python@3.14
        # with no pyyaml vs. CommandLineTools python 3.9 with pyyaml) — the
        # "complete" command below then crashes on `import yaml` and the
        # mission silently fails to complete (looks identical to "never ran").
        # sys.executable is the interpreter that is ACTUALLY running this
        # script right now, which by construction already imported `yaml`
        # successfully above — so it is always yaml-capable, and it is an
        # absolute path (no further PATH lookup, no non-determinism). The
        # caller (local-loop-backup-runner.sh / _lll_py) is responsible for
        # invoking this script itself under a pinned, yaml-capable python;
        # this line then simply propagates that same interpreter forward.
        command = " ".join(
            [
                shlex.quote(sys.executable),
                shlex.quote(str(script)),
                "complete",
                "--dept-dir",
                shlex.quote(str(dept_dir)),
                "--mission",
                shlex.quote(item["id"]),
                "--period",
                shlex.quote(item["period"]),
            ]
        )
        commands.append(f"COMPLETE {item['id']} => {command}")
    return (
        "Resume Rick's OODA loop and run one full tick now. "
        f"DUE_MISSIONS=[{items}]. "
        "This is the exact M1-M8 scheduled-work allow-list for this tick: read each mission file "
        "and the attached layer prompts, execute every listed mission, and do not schedule any "
        "unlisted recurring mission. Preserve every human-approval gate and the PR-to-Joris-only "
        "self-modification guardrail; never self-merge mission/mandate/loop/agent-def changes. "
        "Mission completion is explicit and per mission: only after that mission actually succeeds, "
        "run its exact command below. Do not run it for failed, blocked, partial, merely dispatched, "
        "or inbox-accepted work. A periodic pending lease only prevents duplicate delivery while work "
        "is in flight; it is not success, and an uncompleted mission retries after lease expiry. "
        + " | ".join(commands)
        + " | Then write the normal heartbeat and arm only the existing normal self-paced next wake."
    )


def _validate_completion_period(cadence: str, period: str) -> None:
    patterns = {
        "continuous": r"continuous",
        "daily": r"\d{4}-\d{2}-\d{2}",
        "weekly": r"\d{4}-W\d{2}",
        "monthly": r"\d{4}-\d{2}",
    }
    pattern = patterns.get(cadence)
    if pattern is None or re.fullmatch(pattern, period) is None:
        raise DueMissionConfigError(f"invalid {cadence!r} completion period: {period!r}")


def _lease_seconds(manifest: dict) -> int:
    loop = manifest.get("loop")
    config = loop.get("due_dispatch") if isinstance(loop, dict) else None
    value = config.get("pending_lease_seconds") if isinstance(config, dict) else None
    if not isinstance(value, int) or not 900 <= value <= 86400:
        raise DueMissionConfigError("pending_lease_seconds must be between 900 and 86400")
    return value


def _claims_token(claims: dict) -> str:
    raw = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_claims(token: str) -> dict:
    try:
        value = json.loads(base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise DueMissionConfigError("claims token is invalid") from exc
    if not isinstance(value, dict):
        raise DueMissionConfigError("claims token must contain a mapping")
    return value


def _runner_output(plan: list[dict], claims: dict, dept_dir: Path) -> str:
    periodic_due = any(item["cadence"] != "continuous" for item in plan)
    return "\t".join(
        (("1" if periodic_due else "0"), _claims_token(claims), _prompt(plan, dept_dir))
    )


def _emit_skip_notice(skipped: list[dict]) -> None:
    """Emit ONE concise stderr line naming missions skipped for not being live.

    #1317: skipped ``status: planned`` (or missing/unknown-status) work must
    stay VISIBLE — "we have scheduled work that isn't built yet" is a signal,
    not something to hide. Deliberately ONE line per invocation (not one per
    mission), and on STDERR so it never corrupts the tab-separated ``runner``/
    ``json`` payload the local floor parses off STDOUT. Each entry shows the
    mission id and the offending status so a mistyped/missing status on a
    mission that was meant to be live is loud rather than silent.
    """
    if not skipped:
        return
    names = ", ".join(
        f"{item['id']}(status={item['status']!r})" for item in skipped
    )
    print(
        f"due-mission notice: skipped {len(skipped)} non-live mission(s) "
        f"(not dispatched, not claimed): {names}",
        file=sys.stderr,
    )


def command_plan(args: argparse.Namespace) -> int:
    dept_dir = Path(args.dept_dir).resolve()
    manifest = _load_manifest(dept_dir)
    loop = manifest.get("loop")
    if isinstance(loop, dict) and "due_dispatch" not in loop:
        return 0
    if loop is None:
        return 0
    watermark = due_watermark_path(str(dept_dir), manifest)
    state = read_due_watermarks(watermark)
    skipped: list[dict] = []
    plan = due_mission_plan(manifest, state, _now(args.now_epoch), skipped=skipped)
    if plan is None:
        return 0
    _emit_skip_notice(skipped)
    _validate_scoped_files(dept_dir, manifest)
    if args.format == "json":
        print(json.dumps({"configured": True, "due": plan}, sort_keys=True))
    elif args.format == "runner":
        print(_runner_output(plan, {}, dept_dir))
    else:
        print(_prompt(plan, dept_dir))
    return 0


def command_claim(args: argparse.Namespace) -> int:
    dept_dir = Path(args.dept_dir).resolve()
    manifest = _load_manifest(dept_dir)
    _validate_scoped_files(dept_dir, manifest)
    path = due_watermark_path(str(dept_dir), manifest)
    skipped: list[dict] = []
    plan, claims = claim_due_missions(
        path, manifest, _now(args.now_epoch), _lease_seconds(manifest), skipped=skipped
    )
    _emit_skip_notice(skipped)
    print(_runner_output(plan, claims, dept_dir))
    return 0


def command_release(args: argparse.Namespace) -> int:
    dept_dir = Path(args.dept_dir).resolve()
    manifest = _load_manifest(dept_dir)
    path = due_watermark_path(str(dept_dir), manifest)
    release_due_claims(path, _decode_claims(args.claims_token))
    return 0


def command_complete(args: argparse.Namespace) -> int:
    dept_dir = Path(args.dept_dir).resolve()
    manifest = _load_manifest(dept_dir)
    # Validating a plan with empty watermarks checks the whole scoped manifest,
    # including missing due rules, before any state mutation.
    all_scoped = due_mission_plan(manifest, {"version": 1, "missions": {}}, _now(args.now_epoch))
    if all_scoped is None:
        raise DueMissionConfigError("due dispatcher is not configured")
    _validate_scoped_files(dept_dir, manifest)
    mission = next((item for item in all_scoped if item["id"] == args.mission), None)
    if mission is None:
        raise DueMissionConfigError(f"mission is not in due-dispatch scope: {args.mission}")
    _validate_completion_period(mission["cadence"], args.period)
    path = due_watermark_path(str(dept_dir), manifest)
    write_due_success(path, args.mission, args.period, _now(args.now_epoch))
    print(f"completed {args.mission} for {args.period}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--dept-dir", required=True)
    plan.add_argument("--now-epoch", type=int)
    plan.add_argument("--format", choices=("prompt", "json", "runner"), default="prompt")
    plan.set_defaults(func=command_plan)
    claim = sub.add_parser("claim")
    claim.add_argument("--dept-dir", required=True)
    claim.add_argument("--now-epoch", type=int)
    claim.set_defaults(func=command_claim)
    release = sub.add_parser("release")
    release.add_argument("--dept-dir", required=True)
    release.add_argument("--claims-token", required=True)
    release.set_defaults(func=command_release)
    complete = sub.add_parser("complete")
    complete.add_argument("--dept-dir", required=True)
    complete.add_argument("--mission", required=True)
    complete.add_argument("--period", required=True)
    complete.add_argument("--now-epoch", type=int)
    complete.set_defaults(func=command_complete)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except DueMissionConfigError as exc:
        print(f"due-mission error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
