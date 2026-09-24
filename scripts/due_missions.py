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

# #1330 (fail-LOUD): bare `python3` on a host with more than one interpreter
# on PATH can resolve to one without pyyaml, crashing this module at IMPORT
# time with a bare `ModuleNotFoundError` traceback — the exact confirmed
# incident (2026-09-14: `complete` silently failed to run at all, the mission
# looked identical to one that never fired, #1235/#1316's own failure class).
# Deferring the import lets `main()` turn that into ONE unambiguous,
# actionable stderr line (naming the interpreter actually in use) instead of
# a traceback that can be missed or truncated — see `_require_yaml` below.
# This does NOT fix interpreter selection (that is #1330's own "pin a known-
# good interpreter" remedy, an infra/deploy change out of this module's
# scope) — it only makes the failure impossible to mistake for success.
try:
    import yaml
    _YAML_IMPORT_ERROR: "ImportError | None" = None
except ImportError as _exc:  # pragma: no cover - depends on the host's interpreter
    yaml = None  # type: ignore[assignment]
    _YAML_IMPORT_ERROR = _exc

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _require_yaml() -> None:
    """Raise a clear, actionable DueMissionConfigError if `yaml` failed to
    import — see the #1330 note above `import yaml`."""
    if _YAML_IMPORT_ERROR is not None:
        raise DueMissionConfigError(
            f"missing dependency 'yaml' (pyyaml) for interpreter "
            f"{sys.executable!r} — {_YAML_IMPORT_ERROR}. This due-mission "
            f"command did NOT run; nothing was claimed or completed. Pin "
            f"this script to a Python with pyyaml installed (see #1330)."
        )

from scripts.lib.loop_backup import (
    _MISSION_LIVE_STATUS,
    _STALE_CLAIM_FRACTION,
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
    _require_yaml()
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


def _dept_label(manifest: dict) -> str:
    """Possessive label for the wake-prompt preamble ("Resume <label> OODA
    loop…"), e.g. "Rick's" or "maya's".

    Falls back to a generic phrasing when `department.display_name` /
    `department.slug` is absent, rather than hardcoding one dept's name. This
    text must work fleet-wide (#1484 — every Mac and VPS dept generates its
    own self-wake prompt from this same envelope), not just for Rick's own
    dept.yaml, which is the only manifest that happened to exercise this
    function before #1484.
    """
    department = manifest.get("department")
    if isinstance(department, dict):
        name = department.get("display_name") or department.get("slug")
        if isinstance(name, str) and name:
            return f"{name}'s"
    return "the dept's"


def _prompt(plan: list[dict], dept_dir: Path, dept_label: str = "Rick's") -> str:
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
        f"Resume {dept_label} OODA loop and run one full tick now. "
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


def _runner_output(plan: list[dict], claims: dict, dept_dir: Path, dept_label: str = "Rick's") -> str:
    periodic_due = any(item["cadence"] != "continuous" for item in plan)
    return "\t".join(
        (("1" if periodic_due else "0"), _claims_token(claims), _prompt(plan, dept_dir, dept_label))
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


def _emit_stale_notice(stale: list[dict]) -> None:
    """Emit ONE concise stderr line naming missions whose pending lease has
    been held past #1316's staleness threshold with no completion.

    #1316: "claimed" silently standing in for "running" for hours (six
    missions, ~4h, no completion, no alert) is the exact failure this
    surfaces — a stuck claim must be visible in the tick's own output, not
    something an agent only notices by happening to read its own watermark
    file. Mirrors `_emit_skip_notice`'s one-line/stderr-only contract so it
    can never corrupt the tab-separated `runner`/`json` payload on stdout.
    """
    if not stale:
        return
    names = ", ".join(
        f"{item['id']}(claimed {item['claimed_at']}, "
        f"{item['age_fraction'] * 100:.0f}% of lease elapsed, no completion)"
        for item in stale
    )
    print(
        f"due-mission notice: {len(stale)} stale claim(s) held past "
        f"{int(_STALE_CLAIM_FRACTION * 100)}% of their lease with no completion: {names}",
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
    stale: list[dict] = []
    plan = due_mission_plan(manifest, state, _now(args.now_epoch), skipped=skipped, stale=stale)
    if plan is None:
        return 0
    _emit_skip_notice(skipped)
    _emit_stale_notice(stale)
    _validate_scoped_files(dept_dir, manifest)
    dept_label = _dept_label(manifest)
    if args.format == "json":
        print(json.dumps({"configured": True, "due": plan}, sort_keys=True))
    elif args.format == "runner":
        print(_runner_output(plan, {}, dept_dir, dept_label))
    else:
        print(_prompt(plan, dept_dir, dept_label))
    return 0


def command_claim(args: argparse.Namespace) -> int:
    dept_dir = Path(args.dept_dir).resolve()
    manifest = _load_manifest(dept_dir)
    _validate_scoped_files(dept_dir, manifest)
    path = due_watermark_path(str(dept_dir), manifest)
    skipped: list[dict] = []
    stale: list[dict] = []
    plan, claims = claim_due_missions(
        path, manifest, _now(args.now_epoch), _lease_seconds(manifest),
        skipped=skipped, stale=stale,
    )
    _emit_skip_notice(skipped)
    _emit_stale_notice(stale)
    print(_runner_output(plan, claims, dept_dir, _dept_label(manifest)))
    return 0


# ─── wake-prompt (#1484) ─────────────────────────────────────────────────────
#
# Board #1483/#1484: each dept re-arms its OWN next wake (CronCreate prompt) at
# boot-rearm, compact-rearm, and after every normal tick. Until now that prompt
# was FREE TEXT the agent composed itself each time ("your full tick protocol
# text (STEP A-F per CLAUDE.md)") — exactly the channel through which Ben's
# uncited "operator flagged spend — be cost-conscious" note (2026-09-11)
# self-reinforced across ~36 wake prompts and ~15 subagent briefs and silently
# dropped a mission deliverable for 12 days (see #1483's audit comment).
#
# Fix: the CronCreate prompt is GENERATED, never authored. `wake-prompt`
# prints the exact same deterministic envelope the floor/backup tick already
# uses (`_prompt()` — DUE_MISSIONS=[...] + per-mission COMPLETE commands),
# plus a fixed footer: a pointer to WORKING_MEMORY/HANDOFF.md, a citation rule
# on any operator-intent claim, and a staleness clause (the prompt is rendered
# at ARM time but fires hours later). The agent's only remaining choice is
# WHEN to arm the next wake (the cron time/cadence) — never WHAT it says.
#
# FAIL-CLOSED (#1484 PR-review finding): a VPS dept.yaml (e.g. Ben's, live)
# uses `recurring_missions: [{id, layer, cadence, time, ...}]` with NO
# `loop.due_dispatch` block at all — a different schema than the Mac
# due-dispatch scheme this module understands. Before this fix,
# `wake-prompt --dept-dir /srv/agents/ben` printed a perfectly well-formed
# `DUE_MISSIONS=[]` envelope — indistinguishable from "this dept genuinely has
# nothing due right now" — which is EXACTLY the failure #1483 exists to
# prevent: a VPS agent pasting that verbatim would arm its next wake with NO
# scheduled work listed. `wake-prompt` now REFUSES (raises
# `DueMissionConfigError`, non-zero exit, EMPTY stdout — nothing is ever
# printed before the checks below pass) whenever it cannot positively
# confirm real, live, currently-due work: `loop.due_dispatch` absent (schema
# not understood — deliberately still true for VPS today, see board #1487 for
# the follow-up) OR the resolved plan is empty (nothing is due this instant —
# including "no live missions are configured at all"). The caller
# (boot_rearm.ts / rearm-loop-on-compact.py / the agent's own re-arm step)
# MUST fall back to the previous free-text tick-protocol wake and record the
# refusal in its HEARTBEAT ONLY — never Telegram, since this fires on every
# re-arm/compaction and would spam the operator over a known, tracked gap —
# rather than trust a plausible-looking empty envelope.

WAKE_PROMPT_FOOTER = (
    " Before acting on this wake, read WORKING_MEMORY/HANDOFF.md for current state. "
    "Any sentence that attributes a rule, instruction, or behavior change to the "
    "operator (\"operator/Joris said/flagged/wants/asked/told...\" or equivalent) "
    "must cite a msg id / tg id / dated source; never carry forward an unsourced "
    "operator-intent claim from a prior tick, a prior self-note, or a compaction "
    "summary (board #1483: an uncited 'be cost-conscious' note self-reinforced "
    "across ~36 wake prompts and 15 subagent briefs and silently dropped a mission "
    "deliverable for 12 days). This prompt is machine-generated by "
    "`due_missions.py wake-prompt` — the only thing left to you is WHEN to arm the "
    "next wake (the cron time/cadence), never WHAT this prompt says: never compose, "
    "paraphrase, edit, or append your own wording to it — pass this exact text to "
    "CronCreate verbatim."
)


def _staleness_clause(dept_dir: Path) -> str:
    """Fixed instruction closing the arm-time/fire-time gap (#1484 PR review).

    This prompt is rendered when the wake is ARMED but does not fire until
    hours later (a self-paced cadence can be "wake tomorrow 08:03"), so a
    daily/weekly mission that becomes due overnight is invisible to the
    DUE_MISSIONS list baked in at arm time. Rather than trying to predict the
    future at render time, the fix is a fixed re-check instruction: at FIRE
    time, before running anything, re-run this exact same generator (its
    absolute-interpreter-pinned invocation, mirroring the #1330 lesson
    already applied to the per-mission COMPLETE commands in `_prompt()`) and
    treat ITS fresh output as authoritative for the tick — never the stale
    copy baked into the prompt that was actually delivered.
    """
    script = Path(__file__).resolve()
    refresh_command = " ".join(
        [
            shlex.quote(sys.executable),
            shlex.quote(str(script)),
            "wake-prompt",
            "--dept-dir",
            shlex.quote(str(dept_dir)),
        ]
    )
    return (
        " STALENESS: this DUE_MISSIONS list was computed when this wake was ARMED, "
        "not when it FIRES (a self-paced cadence can sit for hours). Before running "
        f"anything, re-run `{refresh_command}` and treat ITS fresh output as the "
        "authoritative DUE_MISSIONS for this tick — a mission that became due "
        "overnight, or one that already completed since this prompt was generated, "
        "must never be skipped or re-run just because this stale copy disagrees. If "
        "that refresh itself now fails or refuses (see the FAIL-CLOSED contract "
        "below), fall back to your normal full tick protocol for this tick and flag "
        "the failure — never fall back to composing your own DUE_MISSIONS list."
    )


def _wake_prompt(plan: list[dict], dept_dir: Path, dept_label: str) -> str:
    """The canonical, machine-generated CronCreate self-wake prompt (#1484).

    Reuses `_prompt()` verbatim (the SAME envelope the floor/backup tick
    already renders), then appends the staleness re-check clause and
    `WAKE_PROMPT_FOOTER`. Pure function of its inputs — same
    `plan`/`dept_dir`/`dept_label` always yields byte-identical output, so
    there is no free-text slot for the agent (or a subagent, or a compaction
    pass) to fill in. Callers MUST NOT invoke this with an empty `plan` — the
    CLI (`command_wake_prompt`) enforces that gate; this function only renders.
    """
    return _prompt(plan, dept_dir, dept_label) + _staleness_clause(dept_dir) + WAKE_PROMPT_FOOTER


def _plan_for_wake(dept_dir: Path, manifest: dict, now_epoch: "int | None") -> list[dict]:
    """Compute the due-mission plan for `wake-prompt`.

    Returns `[]` for a manifest that hasn't adopted `loop.due_dispatch` (a
    legacy Mac manifest, OR a VPS `recurring_missions:{layer,cadence,time}`
    manifest this module does not parse — see the FAIL-CLOSED note above).
    The caller (`command_wake_prompt`) is responsible for refusing on an
    empty result; this helper only computes.
    """
    loop = manifest.get("loop")
    if not isinstance(loop, dict) or "due_dispatch" not in loop:
        return []
    watermark = due_watermark_path(str(dept_dir), manifest)
    state = read_due_watermarks(watermark)
    skipped: list[dict] = []
    stale: list[dict] = []
    plan = due_mission_plan(manifest, state, _now(now_epoch), skipped=skipped, stale=stale)
    _emit_skip_notice(skipped)
    _emit_stale_notice(stale)
    _validate_scoped_files(dept_dir, manifest)
    return plan or []


def command_wake_prompt(args: argparse.Namespace) -> int:
    dept_dir = Path(args.dept_dir).resolve()
    manifest = _load_manifest(dept_dir)
    loop = manifest.get("loop")
    due_dispatch_configured = (
        isinstance(loop, dict) and "due_dispatch" in loop and loop.get("due_dispatch") is not None
    )
    if not due_dispatch_configured:
        # FAIL-CLOSED (#1484 PR review): this is the Ben repro — a VPS
        # `recurring_missions:{layer,cadence,time}` manifest has no
        # `loop.due_dispatch` at all. Refuse rather than silently printing a
        # plausible-looking DUE_MISSIONS=[]; nothing has been printed yet.
        raise DueMissionConfigError(
            "wake-prompt requires dept.yaml's loop.due_dispatch (the Mac due-dispatch "
            "schema); this manifest doesn't have it. This command does NOT understand "
            "the VPS recurring_missions:{layer,cadence,time} schema (board #1487 is "
            "the VPS follow-up; reusing the VPS selector cleanly is out of scope for "
            "this PR), so it refuses rather than silently emitting an empty "
            "DUE_MISSIONS=[] envelope. The caller MUST fall back to the previous "
            "free-text wake instruction for this dept and record this refusal in its "
            "heartbeat ONLY (never Telegram — a known gap until #1487 lands)."
        )
    plan = _plan_for_wake(dept_dir, manifest, args.now_epoch)
    if not plan:
        # FAIL-CLOSED: schema IS understood, but nothing is currently live/due —
        # never emit an empty envelope (#1484 PR review: "never emit or accept
        # an empty envelope"). This is expected to self-heal on the next tick
        # (continuous missions are always due; a purely calendar-cadence dept
        # can legitimately have a quiet moment) and is not itself an error in
        # the dept.yaml, so the caller's fallback + flag is the correct response,
        # not a crash.
        raise DueMissionConfigError(
            "wake-prompt: loop.due_dispatch is configured but no live mission is "
            "currently due — refusing to emit an empty DUE_MISSIONS=[] envelope "
            "(#1484 PR review: never emit or accept an empty envelope). The caller "
            "MUST fall back to the previous free-text wake instruction for this tick "
            "and record this refusal in its heartbeat ONLY (never Telegram); a later "
            "tick is expected to resolve this on its own."
        )
    print(_wake_prompt(plan, dept_dir, _dept_label(manifest)))
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
    # #1235/#1316/#1330: a completion is only real if the marker is actually
    # written — verify the write actually landed (read the persisted state
    # back, not just trust write_due_success's in-memory return) before
    # reporting success. A completion command that prints "completed" while
    # the marker silently failed to persist is indistinguishable from a
    # genuine success to anything reading this command's stdout/exit code —
    # exactly the ambiguity this whole family of cards is about closing.
    persisted = read_due_watermarks(path)
    recorded = persisted.get("missions", {}).get(args.mission, {}).get("last_success_period")
    if recorded != args.period:
        raise DueMissionConfigError(
            f"{args.mission}: completion for {args.period!r} did not persist "
            f"(watermark reads {recorded!r} after write) — treat this as a "
            f"FAILED completion, not a success"
        )
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
    wake_prompt = sub.add_parser("wake-prompt")
    wake_prompt.add_argument("--dept-dir", required=True)
    wake_prompt.add_argument("--now-epoch", type=int)
    wake_prompt.set_defaults(func=command_wake_prompt)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except DueMissionConfigError as exc:
        print(f"due-mission error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - #1330 fail-LOUD, deliberately broad
        # ANY uncaught exception here (e.g. the confirmed live #1330
        # ModuleNotFoundError, or anything else) must be unambiguous and
        # non-zero, not a bare traceback a caller could mistake for partial
        # output. This never masks the real error (it's printed AND
        # re-raised via a distinct exit code) — it only guarantees a
        # completion command that errors can never look like one that
        # silently ran and did nothing (#1235/#1316's own failure class).
        print(f"due-mission error (unexpected {type(exc).__name__}): {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
