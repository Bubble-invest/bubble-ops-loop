#!/usr/bin/env python3
"""Plan and explicitly acknowledge due missions for a Mac-hosted dept."""
from __future__ import annotations

import argparse
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
    DueMissionConfigError,
    due_mission_plan,
    due_watermark_path,
    read_due_watermarks,
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
        command = " ".join(
            [
                "python3",
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
        "or inbox-accepted work; an unacknowledged mission stays due and retries next tick. "
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
    plan = due_mission_plan(manifest, state, _now(args.now_epoch))
    if plan is None:
        return 0
    _validate_scoped_files(dept_dir, manifest)
    if args.format == "json":
        print(json.dumps({"configured": True, "due": plan}, sort_keys=True))
    elif args.format == "runner":
        periodic_due = any(item["cadence"] != "continuous" for item in plan)
        print(("1" if periodic_due else "0") + "\t" + _prompt(plan, dept_dir))
    else:
        print(_prompt(plan, dept_dir))
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
