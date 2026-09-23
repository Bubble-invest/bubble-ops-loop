"""loop_backup.py — backup-execution decision for ops-loop depts.

A per-dept BACKUP runner fires twice a day ({{OPERATOR}} 2026-06-01). For each
dept it decides whether the persistent /loop is alive (recent heartbeat →
skip) or dead/parked (stale heartbeat → run ONE backup dispatch tick).

This module is the PURE decision + a small heartbeat-locator helper. The
bash wrapper (loop-backup.sh) does the side effects: flock mutex, the
`claude -p` one-tick run, and Telegram notify.

It also owns the EVENT LOG ({{OPERATOR}} msg 1171, 2026-06-01): every fire appends
one JSON line per dept to a central jsonl so the cockpit can surface the
safety-net result in the front end (was journal-only, invisible to the UI).
Writer lives here; the console reads it back via the same `read_events`.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# An ISO-8601 UTC timestamp at the start of a heartbeat line. Used as a more
# precise liveness signal than file mtime (which a git checkout/rsync could
# bump). Accepts BOTH canonical forms agents emit in the wild:
#   "2026-06-01T07:35:33Z tick ..."                 (hand-built, Z suffix)
#   "2026-06-02T13:30:35.931407+00:00 tick ..."     (datetime.isoformat())
# Bug 2026-06-04: the old regex required a literal Z, so the microsecond/offset
# form never matched → latest_heartbeat_epoch silently fell back to file mtime
# → a frozen-date loop read as FALSE-FRESH on the cockpit (masking staleness —
# the exact failure this function's mtime-avoidance was meant to prevent).
_ISO_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))"
)
# Trailing colon-less UTC offset ("+0200") — see _normalize_iso_offset below.
_COLONLESS_OFFSET_RE = re.compile(r"([+-]\d{2})(\d{2})$")


def _normalize_iso_offset(raw: str) -> str:
    """Normalize an ISO-8601 timestamp string for ``datetime.fromisoformat``.

    #1456 (confirmed live incident, content dept on jade-m1): ``_ISO_RE``
    deliberately matches BOTH a colon-form UTC offset ("+00:00") and a
    colon-less one ("+0200", via ``[+-]\\d{2}:?\\d{2}``) — the latter to
    tolerate whatever an agent's stdlib ``strftime('%z')`` happens to emit.
    But ``datetime.fromisoformat()`` on Python 3.9/3.10 (the pinned Mac/VPS
    interpreter — confirmed 3.9.6 on the incident host) REJECTS the
    colon-less form outright (3.11+ silently accepts it, which is why this
    bug is invisible in a CI environment pinned to 3.12: the interpreter gap
    is exactly what makes this a *production* incident and not a test
    failure there). Before this normalizer existed, a single OLD
    heartbeat.log line in the colon-less form raised an uncaught
    ``ValueError`` out of ``latest_heartbeat_epoch`` entirely (the
    surrounding ``try/except`` only ever caught ``OSError``), which the
    caller's fail-safe then turned into "ALWAYS stale, regardless of true
    freshness" — content dept's Mac wake-catch re-injected "run one full
    tick" every ~15 minutes for hours even though its heartbeat was, in
    truth, only ~15 minutes old each time.

    Also normalizes a trailing ``Z`` to ``+00:00`` (pre-existing behavior,
    kept here so every offset form funnels through one normalizer).
    Idempotent and a no-op on an already-colon offset or a bare local time.
    """
    raw = raw.replace("Z", "+00:00")
    return _COLONLESS_OFFSET_RE.sub(r"\1:\2", raw)


class HarnessSelectorError(ValueError):
    """A harness selector exists but cannot be trusted or interpreted."""


class DueMissionConfigError(ValueError):
    """The due-mission manifest or watermark cannot be used safely."""


_DUE_CADENCES = {"continuous", "daily", "weekly", "monthly"}
_MISSION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# A recurring mission is only dispatchable/claimable when its per-mission
# ``status`` is EXACTLY this. Anything else — ``planned``, some other value, or
# a missing/non-string status — is treated as NOT live and skipped fail-closed
# (#1317): we never claim a lease against, nor dispatch, work whose live-ness we
# cannot confirm. This is a pure FILTER: flipping a mission's status back to
# ``live`` in dept.yaml makes it dispatch again with no other change.
_MISSION_LIVE_STATUS = "live"

# #1316: fraction of a claim's own lease window after which an uncompleted
# claim is surfaced as "stale" rather than silently waited out. A claim IS
# doing its documented job up to this point (preventing duplicate delivery
# while work may still be in flight); past it, a live mission silently not
# completing looks identical to one that is still running, and #1316's
# confirmed incident (6 of 8 missions held ~4h into a lease with no
# completion, invisible until an agent happened to read its own watermark
# file) is exactly that ambiguity going unnoticed. 0.5 is a deliberately
# simple, non-configurable threshold — this is an observability signal, not
# a dispatch decision, so it never affects whether/when a mission re-fires.
_STALE_CLAIM_FRACTION = 0.5


def _claim_age_fraction(pending: dict, now_utc: _dt.datetime) -> "float | None":
    """Fraction of `pending`'s own lease window elapsed since it was claimed,
    or None if it cannot be computed. Never raises — this is a pure
    observability signal computed from an already-validated pending lease
    and must not be able to affect the (already-decided) dispatch outcome.
    """
    claimed_at = pending.get("claimed_at")
    expires_at = pending.get("expires_at_epoch")
    if not isinstance(claimed_at, str) or not isinstance(expires_at, int):
        return None
    try:
        claimed_dt = _dt.datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    claimed_epoch = claimed_dt.timestamp()
    total = expires_at - claimed_epoch
    if total <= 0:
        return None
    elapsed = now_utc.timestamp() - claimed_epoch
    return elapsed / total


def due_period(cadence: str, now_utc: _dt.datetime, timezone_name: str = "UTC") -> str:
    """Return the calendar-period token containing ``now_utc``.

    Periods deliberately have no wall-clock deadline. If a Mac sleeps through
    a day/week/month boundary, the first later tick gets a different token and
    the mission is due. ``continuous`` is always selected by the planner; its
    token exists only so successful ticks still leave an auditable watermark.
    """
    if cadence not in _DUE_CADENCES:
        raise DueMissionConfigError(f"unsupported cadence: {cadence!r}")
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=_dt.timezone.utc)
    try:
        local = now_utc.astimezone(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError as exc:
        raise DueMissionConfigError(f"unknown due timezone: {timezone_name!r}") from exc
    if cadence == "daily":
        return local.date().isoformat()
    if cadence == "weekly":
        iso_year, iso_week, _ = local.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if cadence == "monthly":
        return f"{local.year:04d}-{local.month:02d}"
    return "continuous"


def _due_dispatch_config(manifest: dict) -> Optional[dict]:
    loop = manifest.get("loop")
    if loop is None:
        return None
    if not isinstance(loop, dict):
        raise DueMissionConfigError("loop must be a mapping")
    config = loop.get("due_dispatch")
    if config is None:
        return None
    if not isinstance(config, dict):
        raise DueMissionConfigError("loop.due_dispatch must be a mapping")
    return config


def due_mission_plan(
    manifest: dict,
    watermarks: dict,
    now_utc: _dt.datetime,
    skipped: Optional[List[dict]] = None,
    stale: Optional[List[dict]] = None,
) -> Optional[List[dict]]:
    """Validate and return the scoped missions due in the current period.

    ``None`` means this is a legacy manifest with no due dispatcher and lets the
    generic local floor keep its old wake. Once ``loop.due_dispatch`` exists,
    every allow-listed mission and rule is validated fail-closed. Missions not
    in that explicit list are ignored even if they have a cadence (Rick's later
    M9 placeholder is intentionally outside the M1-M8 schedule).

    Per-mission ``status`` filter (#1317): a scoped mission whose ``status`` is
    not exactly ``live`` (``planned``, any other value, or missing/non-string)
    is NEVER dispatched — it is skipped BEFORE its cadence/due rule is validated
    so an unbuilt planned mission can never crash the plan and starve the live
    ones (the inverted failure is worse than the original bug). This filter is
    the single chokepoint for BOTH the plan and the claim path (``claim_due_
    missions`` calls this function), so a planned mission is never leased either.
    Skipped missions are NOT hidden: pass a list as ``skipped`` and each is
    appended as ``{"id", "status"}`` so the caller can emit one visible notice —
    "we have scheduled work that isn't live yet" must stay loud, and a live
    mission with a mistyped/missing status surfaces here rather than vanishing.

    Stale-claim detector (#1316): a LIVE mission's own unexpired pending lease
    that has been held for more than ``_STALE_CLAIM_FRACTION`` of its own
    lease window, with no completion recorded, is surfaced via ``stale``
    (each entry: ``{"id", "period", "claimed_at", "expires_at_epoch",
    "age_fraction"}``) rather than silently waited out — the confirmed
    incident where 6 of 8 missions sat PENDING for ~4h with no delivery and
    nothing reported it. This is a PURE observability addition: it never
    changes ``result`` (the mission is still excluded from the plan exactly
    as before — the lease is doing its job of preventing duplicate delivery
    while work MAY be in flight) and never raises (a malformed/missing
    ``claimed_at`` just means staleness can't be computed for that entry).
    """
    if not isinstance(manifest, dict):
        raise DueMissionConfigError("dept.yaml root must be a mapping")
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=_dt.timezone.utc)
    config = _due_dispatch_config(manifest)
    if config is None:
        return None
    scope = config.get("mission_ids")
    if not isinstance(scope, list) or not scope:
        raise DueMissionConfigError("loop.due_dispatch.mission_ids must be a non-empty list")
    if any(not isinstance(item, str) or not _MISSION_ID_RE.fullmatch(item) for item in scope):
        raise DueMissionConfigError("due-dispatch mission ids must be safe snake_case strings")
    if len(scope) != len(set(scope)):
        raise DueMissionConfigError("due-dispatch mission ids must be unique")

    raw_missions = manifest.get("recurring_missions")
    if not isinstance(raw_missions, list):
        raise DueMissionConfigError("recurring_missions must be a list")
    missions: Dict[str, dict] = {}
    for item in raw_missions:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise DueMissionConfigError("every recurring mission needs a string id")
        mission_id = item["id"]
        if mission_id in missions:
            raise DueMissionConfigError(f"duplicate recurring mission id: {mission_id}")
        missions[mission_id] = item

    if not isinstance(watermarks, dict):
        raise DueMissionConfigError("watermark root must be a mapping")
    watermark_missions = watermarks.get("missions", {})
    if not isinstance(watermark_missions, dict):
        raise DueMissionConfigError("watermark missions must be a mapping")

    result: List[dict] = []
    for mission_id in scope:
        mission = missions.get(mission_id)
        if mission is None:
            raise DueMissionConfigError(f"scoped mission is missing: {mission_id}")
        # #1317: skip anything not EXACTLY live BEFORE validating its cadence/due
        # rule. Fail-closed (missing/other status → not live) and defensive: a
        # not-yet-built planned mission with an incomplete due rule must never
        # raise here and take the live missions down with it. Record it so the
        # caller can surface the skipped work instead of hiding it.
        status = mission.get("status")
        if status != _MISSION_LIVE_STATUS:
            if skipped is not None:
                skipped.append({"id": mission_id, "status": status})
            continue
        cadence = mission.get("cadence")
        if cadence not in _DUE_CADENCES:
            raise DueMissionConfigError(f"{mission_id}: unsupported cadence {cadence!r}")
        due = mission.get("due")
        if not isinstance(due, dict):
            raise DueMissionConfigError(f"{mission_id}: missing due rule")
        policy = due.get("policy")
        if cadence == "continuous":
            if policy != "every_tick" or set(due) != {"policy"}:
                raise DueMissionConfigError(
                    f"{mission_id}: continuous cadence requires only due.policy=every_tick"
                )
            timezone_name = "UTC"
        else:
            if policy != "calendar_period":
                raise DueMissionConfigError(
                    f"{mission_id}: {cadence} cadence requires due.policy=calendar_period"
                )
            timezone_name = due.get("timezone")
            if not isinstance(timezone_name, str) or not timezone_name:
                raise DueMissionConfigError(f"{mission_id}: calendar due rule needs a timezone")
            if set(due) != {"policy", "timezone"}:
                raise DueMissionConfigError(f"{mission_id}: unknown due-rule keys")

        mission_file = mission.get("mission_file")
        if not isinstance(mission_file, str) or not mission_file or Path(mission_file).is_absolute() \
                or ".." in Path(mission_file).parts:
            raise DueMissionConfigError(f"{mission_id}: mission_file must be a safe relative path")
        layer = mission.get("layer")
        layers = layer if isinstance(layer, list) else [layer]
        if not layers or any(not isinstance(value, int) or value not in {1, 2, 3, 4} for value in layers):
            raise DueMissionConfigError(f"{mission_id}: layer must contain only 1..4")

        period = due_period(cadence, now_utc, timezone_name)
        prior = watermark_missions.get(mission_id, {})
        if not isinstance(prior, dict):
            raise DueMissionConfigError(f"{mission_id}: watermark entry must be a mapping")
        last_period = prior.get("last_success_period")
        if last_period is not None and not isinstance(last_period, str):
            raise DueMissionConfigError(f"{mission_id}: last_success_period must be a string")
        if cadence != "continuous" and last_period == period:
            continue
        if cadence != "continuous":
            pending = prior.get("pending")
            if pending is not None:
                if not isinstance(pending, dict):
                    raise DueMissionConfigError(f"{mission_id}: pending lease must be a mapping")
                pending_period = pending.get("period")
                claim_id = pending.get("claim_id")
                expires_at = pending.get("expires_at_epoch")
                if not isinstance(pending_period, str) or not isinstance(claim_id, str) \
                        or not isinstance(expires_at, int):
                    raise DueMissionConfigError(f"{mission_id}: pending lease has an invalid shape")
                if pending_period == period and expires_at > int(now_utc.timestamp()):
                    if stale is not None:
                        fraction = _claim_age_fraction(pending, now_utc)
                        if fraction is not None and fraction >= _STALE_CLAIM_FRACTION:
                            stale.append({
                                "id": mission_id,
                                "period": pending_period,
                                "claimed_at": pending.get("claimed_at"),
                                "expires_at_epoch": expires_at,
                                "age_fraction": round(fraction, 3),
                            })
                    continue
        result.append(
            {
                "id": mission_id,
                "cadence": cadence,
                "period": period,
                "layers": layers,
                "mission_file": mission_file,
            }
        )
    return result


def due_watermark_path(dept_dir: str, manifest: dict) -> Path:
    """Resolve the configured watermark while containing it inside dept_dir."""
    config = _due_dispatch_config(manifest)
    if config is None:
        raise DueMissionConfigError("due dispatcher is not configured")
    relative = config.get("watermark")
    if not isinstance(relative, str) or not relative:
        raise DueMissionConfigError("loop.due_dispatch.watermark is required")
    rel_path = Path(relative)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise DueMissionConfigError("due watermark must be a safe relative path")
    root = Path(dept_dir).resolve()
    target = (root / rel_path).resolve()
    if target != root and root not in target.parents:
        raise DueMissionConfigError("due watermark escapes dept directory")
    return target


def read_due_watermarks(path: Path) -> dict:
    """Read the atomic JSON watermark, failing closed on malformed state."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"version": 1, "missions": {}}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DueMissionConfigError("due watermark is not a regular non-symlink file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DueMissionConfigError(f"due watermark is unreadable: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != 1 \
            or not isinstance(value.get("missions"), dict):
        raise DueMissionConfigError("due watermark has an unsupported shape")
    return value


def write_due_success(
    path: Path,
    mission_id: str,
    period: str,
    completed_at: _dt.datetime,
) -> dict:
    """Atomically record one explicit mission-success acknowledgement."""
    if not _MISSION_ID_RE.fullmatch(mission_id) or not isinstance(period, str) or not period:
        raise DueMissionConfigError("unsafe mission completion arguments")
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=_dt.timezone.utc)
    lock_fd = _open_due_lock(path)
    try:
        state = read_due_watermarks(path)
        entry = state["missions"].setdefault(mission_id, {})
        if not isinstance(entry, dict):
            raise DueMissionConfigError(f"{mission_id}: watermark entry must be a mapping")
        previous = entry.get("last_success_period")
        if previous is not None and not isinstance(previous, str):
            raise DueMissionConfigError(f"{mission_id}: last_success_period must be a string")
        # A delayed old-period acknowledgement must never regress a newer
        # success. Period tokens are lexically ordered within one cadence.
        if previous is None or previous <= period:
            entry["last_success_period"] = period
            entry["completed_at"] = completed_at.astimezone(_dt.timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        # Clear only the lease for the period that actually completed. A newer
        # period may already have been claimed at a calendar boundary.
        pending = entry.get("pending")
        if isinstance(pending, dict) and pending.get("period") == period:
            del entry["pending"]
        _write_due_state_unlocked(path, state)
        return state
    finally:
        os.close(lock_fd)


def _write_due_state_unlocked(path: Path, state: dict) -> None:
    """Publish state atomically while the caller holds the sibling lock."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        body = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if os.write(fd, body) != len(body):
            raise OSError("incomplete due-watermark write")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temp_name, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _open_due_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".lock")
    import fcntl

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, flags, 0o600)
    info = os.fstat(lock_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        os.close(lock_fd)
        raise DueMissionConfigError("due watermark lock is unsafe")
    os.fchmod(lock_fd, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd


def _purge_inert_leases(state: dict, skipped: List[dict]) -> bool:
    """Remove any pending lease still held against a now-non-live mission.

    #1316 (Rick's own follow-up observation): once #1317 made a non-live
    mission's ``status`` filter run BEFORE the pending-lease check, a lease
    claimed while the mission was still ``live`` and never released stops
    being touched by ANYTHING — #1317's filter skips the mission before the
    lease-release logic ever runs, so it can neither complete (nothing
    dispatches it) nor expire-and-clear itself in any way that's visible.
    It just sits in the watermark file as inert cruft forever. This purges
    it opportunistically on every claim tick: a mission ``due_mission_plan``
    just reported as skipped (non-live) has no legitimate reason to hold a
    lease, so any lease found for it is stale by construction and removed.

    Pure hygiene, no functional effect on dispatch (the #1317 filter already
    makes the mission's OWN lease irrelevant to whether it fires) — this
    only stops the watermark file from accumulating leases that can never
    self-clear. Returns True iff `state` was mutated (caller uses this to
    decide whether a write is needed even when no new claim was made).
    """
    changed = False
    for item in skipped:
        mission_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(mission_id, str):
            continue
        entry = state.get("missions", {}).get(mission_id)
        if isinstance(entry, dict) and isinstance(entry.get("pending"), dict):
            del entry["pending"]
            if not entry:
                del state["missions"][mission_id]
            changed = True
    return changed


def claim_due_missions(
    path: Path,
    manifest: dict,
    now_utc: _dt.datetime,
    lease_seconds: int,
    skipped: Optional[List[dict]] = None,
    stale: Optional[List[dict]] = None,
) -> tuple:
    """Atomically claim currently due periodic mission-periods.

    The pending lease prevents the backup and wake-catch LaunchAgents from
    appending duplicate work after their shorter inbox cooldown expires. It is
    only a delivery/in-flight marker: last_success_period remains untouched.

    Claiming goes through ``due_mission_plan``, so the #1317 status filter
    applies identically to the claim path: a non-live mission is never in the
    plan here and therefore never gets a pending lease. ``skipped`` and
    ``stale`` are passed straight through so a claim-path caller can emit the
    same visible notices as the plan path.

    #1316: any mission `due_mission_plan` reports as skipped (non-live) also
    has any pending lease it might still be holding purged here (see
    `_purge_inert_leases`) — this is the one write-capable call site in the
    due-dispatch pipeline, so it is the natural place to clear cruft a
    read-only `plan` cannot.
    """
    if not isinstance(lease_seconds, int) or not 900 <= lease_seconds <= 86400:
        raise DueMissionConfigError("pending_lease_seconds must be between 900 and 86400")
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=_dt.timezone.utc)
    lock_fd = _open_due_lock(path)
    try:
        state = read_due_watermarks(path)
        local_skipped: List[dict] = []
        plan = due_mission_plan(manifest, state, now_utc, skipped=local_skipped, stale=stale)
        if plan is None:
            raise DueMissionConfigError("due dispatcher is not configured")
        if skipped is not None:
            skipped.extend(local_skipped)
        purged = _purge_inert_leases(state, local_skipped)
        claims: Dict[str, str] = {}
        for item in plan:
            if item["cadence"] == "continuous":
                continue
            claim_id = uuid.uuid4().hex
            entry = state["missions"].setdefault(item["id"], {})
            entry["pending"] = {
                "period": item["period"],
                "claim_id": claim_id,
                "claimed_at": now_utc.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "expires_at_epoch": int(now_utc.timestamp()) + lease_seconds,
            }
            claims[item["id"]] = claim_id
        if claims or purged:
            _write_due_state_unlocked(path, state)
        return plan, claims
    finally:
        os.close(lock_fd)


def release_due_claims(path: Path, claims: Dict[str, str]) -> None:
    """Release only claim IDs owned by a wake that was not accepted."""
    if not claims:
        return
    if any(not _MISSION_ID_RE.fullmatch(key) or not isinstance(value, str) or not value
           for key, value in claims.items()):
        raise DueMissionConfigError("unsafe due-claim release arguments")
    lock_fd = _open_due_lock(path)
    try:
        state = read_due_watermarks(path)
        changed = False
        for mission_id, claim_id in claims.items():
            entry = state["missions"].get(mission_id)
            pending = entry.get("pending") if isinstance(entry, dict) else None
            if isinstance(pending, dict) and pending.get("claim_id") == claim_id:
                del entry["pending"]
                if not entry:
                    del state["missions"][mission_id]
                changed = True
        if changed:
            _write_due_state_unlocked(path, state)
    finally:
        os.close(lock_fd)


def read_harness_selector(
    path: str,
    *,
    max_bytes: int = 64,
    require_root_owner: bool = False,
) -> str:
    """Read one trusted per-department harness selector.

    A missing or whitespace-only selector preserves the fleet default
    (``claude``).  Existing selectors must be small, regular, non-symlink
    files owned by root (the VPS production shape) or the current effective
    user (local/test shape), and must not be writable by group or other. When
    ``require_root_owner`` is true, only UID 0 is accepted; isolated production
    callers use that stricter contract so their service user cannot rewrite
    its own harness classification.

    The file is checked both before and after opening with ``O_NOFOLLOW`` so a
    path-shape race fails closed.  This helper has no side effects and never
    guesses when an existing selector is unsafe or unknown.
    """
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return "claude"
    except OSError as exc:
        raise HarnessSelectorError(f"harness selector unavailable: {exc}") from exc

    if stat.S_ISLNK(before.st_mode):
        raise HarnessSelectorError("harness selector is a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise HarnessSelectorError("harness selector is not a regular file")

    allowed_owners = {0} if require_root_owner else {0, os.geteuid()}
    if before.st_uid not in allowed_owners:
        raise HarnessSelectorError("harness selector has an untrusted owner")
    if before.st_mode & 0o022:
        raise HarnessSelectorError("harness selector is writable by group or other")
    if before.st_size > max_bytes:
        raise HarnessSelectorError("harness selector is oversized")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HarnessSelectorError(f"harness selector could not be opened: {exc}") from exc
    try:
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise HarnessSelectorError("harness selector changed while opening")
        if not stat.S_ISREG(after.st_mode):
            raise HarnessSelectorError("harness selector changed to a non-regular file")
        if after.st_uid not in allowed_owners:
            raise HarnessSelectorError("harness selector has an untrusted owner")
        if after.st_mode & 0o022:
            raise HarnessSelectorError("harness selector is writable by group or other")
        if after.st_size > max_bytes:
            raise HarnessSelectorError("harness selector is oversized")
        raw = os.read(fd, max_bytes + 1)
    except OSError as exc:
        raise HarnessSelectorError(f"harness selector could not be read: {exc}") from exc
    finally:
        os.close(fd)

    if len(raw) > max_bytes:
        raise HarnessSelectorError("harness selector is oversized")
    try:
        value = raw.decode("utf-8", "strict").strip()
    except UnicodeDecodeError as exc:
        raise HarnessSelectorError("harness selector is not valid UTF-8") from exc
    if not value:
        return "claude"
    if value not in {"claude", "hermes"}:
        raise HarnessSelectorError(f"unknown harness selector value: {value!r}")
    return value


def backup_decision(
    latest_heartbeat_epoch: Optional[float],
    now_epoch: float,
    stale_after_sec: int,
) -> dict:
    """Decide whether to run a backup tick.

    Parameters
    ----------
    latest_heartbeat_epoch : float | None
        Epoch seconds of the most recent heartbeat, or None if none found.
    now_epoch : float
        Current epoch seconds.
    stale_after_sec : int
        A loop is considered dead/parked if its last heartbeat is older
        than this many seconds.

    Returns
    -------
    dict
        {"action": "run"|"skip", "reason": str, "age_sec": int|None}
    """
    if latest_heartbeat_epoch is None:
        return {
            "action": "run",
            "reason": "no heartbeat found — loop never ticked or output missing",
            "age_sec": None,
        }
    age = int(now_epoch - latest_heartbeat_epoch)
    if age < 0:
        # Clock skew: heartbeat in the future. Treat as fresh (alive).
        return {
            "action": "skip",
            "reason": "loop alive (heartbeat fresh — future ts, clock skew)",
            "age_sec": age,
        }
    if age <= stale_after_sec:
        return {
            "action": "skip",
            "reason": f"loop alive (heartbeat fresh, age={age}s ≤ {stale_after_sec}s)",
            "age_sec": age,
        }
    return {
        "action": "run",
        "reason": f"loop stale (heartbeat age={age}s > {stale_after_sec}s) — backing up",
        "age_sec": age,
    }


def latest_heartbeat_epoch(outputs_dir: str) -> Optional[float]:
    """Return the epoch of the newest heartbeat across recent date dirs.

    Reads the last ISO timestamp from each ``<outputs>/<YYYY-MM-DD>/heartbeat.log``
    and returns the maximum (most recent). Falls back to the newest file
    mtime if no parseable timestamp is found. Returns None if no heartbeat
    file exists at all.

    Heartbeats are the dept's own liveness signal — using the in-file ISO
    timestamp (not mtime) avoids false-fresh readings if a deploy/rsync
    touches the file without the loop actually ticking.

    Truthful-heartbeat exclusion (Rick 2026-06-19; extended #749/#750 defect
    (b)): the floor appends a ``... tick BACKUP-FAILED ... — dept DOWN`` line
    when its OWN backup tick crashed (dept genuinely down) — or, since defect
    (b), one of the other floor-authored non-liveness outcomes
    (BACKUP-BUDGET-EXCEEDED / BACKUP-AUTH-FAILED / BACKUP-TIMEOUT): the FLOOR
    wrote these, not the dept, so none of them are evidence the dept's own
    loop ticked. Any of these lines carries a CURRENT timestamp, so a naive
    "last ISO ts" read would treat the dept as FRESH-ALIVE and the floor
    would stop re-firing — exactly the false-fresh failure this signal exists
    to kill. So NONE of the _HB_NOT_LIVENESS tokens count as a liveness
    signal: we fall back to the last REAL heartbeat before them (or None). A
    ``BACKUP-RAN`` or ``DEGRADED-L4`` line DOES count — the dept was actually
    serviced.
    """
    import datetime as _dt

    files = sorted(glob.glob(os.path.join(outputs_dir, "*", "heartbeat.log")))
    if not files:
        return None

    best: Optional[float] = None
    for fp in files:
        ts_epoch: Optional[float] = None
        text = ""
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            # Walk lines newest→oldest; the freshness signal is the most recent
            # ISO-stamped line that is NOT a floor-authored non-liveness marker.
            for line in reversed(text.splitlines()):
                if any(tok in line for tok in _HB_NOT_LIVENESS):
                    continue  # down/budget/auth/timeout marker: never a liveness signal
                m = _ISO_RE.search(line)
                if not m:
                    continue
                # #1456: normalize both the Z-suffix and colon-less-offset wire
                # forms — see _normalize_iso_offset's docstring for why this is
                # needed on the pinned Python 3.9/3.10 interpreter. A line that
                # is STILL unparseable after normalizing falls through to
                # `continue` (try the next older line) rather than crashing
                # the whole liveness read.
                raw = _normalize_iso_offset(m.group(1))
                try:
                    dt = _dt.datetime.fromisoformat(raw)
                except ValueError:
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_dt.timezone.utc)
                ts_epoch = dt.timestamp()
                break
        except OSError:
            ts_epoch = None
        if ts_epoch is None:
            # No parseable non-excluded line. If the file has ONLY excluded
            # markers we must NOT fall back to mtime (that would re-introduce
            # false-fresh); only fall back to mtime when there were no ISO
            # lines at all.
            if any(tok in text for tok in _HB_NOT_LIVENESS):
                continue
            try:
                ts_epoch = os.path.getmtime(fp)
            except OSError:
                continue
        if best is None or ts_epoch > best:
            best = ts_epoch
    return best


# ─── Event log ───────────────────────────────────────────────────────────
#
# Each fire of the backup timer appends one line per dept. The cockpit reads
# this back to render the "Filet de sécurité" block on each dept page + the
# home roll-up. Kept dead-simple (append-only JSONL) so a half-written line
# never corrupts the file and the reader can skip it.


def now_iso() -> str:
    """Current time as an ISO-8601 UTC second-resolution stamp (matches the
    heartbeat-line format the rest of the system uses)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_event(
    slug: str,
    action: str,
    reason: str,
    age_sec: Optional[int] = None,
    exit_code: Optional[int] = None,
    ts: Optional[str] = None,
) -> dict:
    """Build one event record. `ts` defaults to now (UTC ISO).

    action is "skip" (loop alive — no tick) or "run" (loop stale — a backup
    tick was attempted). `exit_code` is set only for runs.
    """
    ev: dict = {
        "ts": ts or now_iso(),
        "slug": slug,
        "action": action,
        "reason": reason,
    }
    if age_sec is not None:
        ev["age_sec"] = int(age_sec)
    if exit_code is not None:
        ev["exit"] = int(exit_code)
    return ev


def append_event(path: str, event: dict) -> None:
    """Append one event as a JSON line, creating parent dirs as needed."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def read_events(
    path: str,
    slug: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[dict]:
    """Read the event log (chronological, oldest→newest).

    Skips blank and unparseable lines (a half-written tail never breaks the
    reader). Optionally filter by `slug` and keep only the last `limit`.
    Returns [] if the file is absent.
    """
    if not os.path.exists(path):
        return []
    out: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if slug is not None and ev.get("slug") != slug:
                    continue
                out.append(ev)
    except OSError:
        return []
    if limit is not None and limit >= 0:
        out = out[-limit:]
    return out


def latest_per_dept(events: List[dict]) -> dict:
    """Reduce a chronological event list to {slug: most-recent-event}."""
    latest: dict = {}
    for ev in events:
        s = ev.get("slug")
        if s:
            latest[s] = ev
    return latest


# ─── Truthful external heartbeat (Rick 2026-06-19) ───────────────────────────
#
# The fleet liveness signal is a free-text line each dept writes ITSELF into
# outputs/<date>/heartbeat.log. Every consumer (watchdog, this floor's
# freshness gate, cockpit) only checks the timestamp FRESHNESS, never the
# truth/content. So when a session dies the thing that writes the heartbeat
# dies with it — a silent hole with no "I'm down" signal (Maya 2026-06-18: 13h
# hole; Ben 2026-06-18: 0 heartbeat lines, the degraded honesty lived only in
# state/loop-backup.jsonl which the watchdog ignores).
#
# Fix: the backup floor — the external observer that already detected the
# staleness — becomes the AUTHORITATIVE writer of a TRUTHFUL liveness line into
# the dept's OWN heartbeat.log, encoding the real OUTCOME of its intervention.
# This collapses the two channels (heartbeat freshness vs loop-backup.jsonl
# truth) into one signal: a downstream consumer reading the heartbeat tail now
# sees WHY it's the floor writing and whether the dept is actually up.
#
# The line shape mirrors the agents' own `<iso> tick ...` convention so the
# existing _ISO_RE parser + every freshness consumer keeps working unchanged;
# the trailing token is the truthful outcome the consumer can grep for.

# Stable outcome tokens (grep-able by the watchdog / cockpit).
HB_BACKUP_RAN = "BACKUP-RAN-FOR-DEPT"   # floor ran a layer tick OK in the dept's place
HB_BACKUP_FAILED = "BACKUP-FAILED"      # floor tried, tick crashed for an UNKNOWN reason → dept DOWN
HB_DEGRADED_L4 = "DEGRADED-L4"          # degraded L4 carried-over (loop was down today)
# ── #749/#750 defect (b): a non-zero exit is not automatically "dept DOWN" ──
# `claude -p ... --max-budget-usd N` returns shell exit=1 whenever the tick
# merely EXCEEDS its budget cap (subtype "error_max_budget_usd") — the dept
# ran fine, it just hit the spend ceiling. Blanket-labelling every non-zero
# exit "dept DOWN" (Defect 2, reference_floor_cron_marker_and_budget_defects.md,
# reproduced live 2026-07-25: `--max-budget-usd 0.001` → subtype
# error_max_budget_usd → exit=1) falsely reports a live, budget-capped dept as
# a dead one — same defect class as #758 (an alert asserting a cause it never
# verified). These tokens give each DISTINCT failure its own honest label so a
# human reads "budget exceeded" vs "auth failed" vs "actually down" and knows
# which needs a different response (raise the cap / re-login / restart).
HB_BACKUP_BUDGET_EXCEEDED = "BACKUP-BUDGET-EXCEEDED"  # tick ran, hit --max-budget-usd — NOT down
HB_BACKUP_AUTH_FAILED = "BACKUP-AUTH-FAILED"          # tick couldn't authenticate — needs re-login, not a restart
HB_BACKUP_TIMEOUT = "BACKUP-TIMEOUT"                  # tick ran out of time — may be transient
# #1313 step 2: the floor DEFERRED — it correctly declined to act (unsafe/
# unreadable harness selector, or a live primary it couldn't wake, so the
# headless fallback was refused) rather than crashing. Distinct from every
# token above: nothing ran AT ALL, so this is even less evidence of liveness
# than a crashed tick — it must never be mistaken for "the dept is fine".
HB_BACKUP_DEFERRED = "BACKUP-DEFERRED"

# Every outcome token the FLOOR (not the dept) writes into the dept's own
# heartbeat.log. None of these are evidence the dept's own loop ticked, so
# latest_heartbeat_epoch must exclude ALL of them from freshness, not just
# HB_BACKUP_FAILED (pre-#749/#750-(b) behavior only excluded the crash case).
_HB_NOT_LIVENESS = (
    HB_BACKUP_FAILED,
    HB_BACKUP_BUDGET_EXCEEDED,
    HB_BACKUP_AUTH_FAILED,
    HB_BACKUP_TIMEOUT,
    HB_BACKUP_DEFERRED,
)


def classify_tick_outcome(
    exit_code: int,
    subtype: Optional[str] = None,
    is_error: Optional[bool] = None,
    raw_output: str = "",
) -> str:
    """Pure classification of a `claude -p` backup tick's outcome.

    Distinguishes an ACTUAL dead department from a tick that ran and hit a
    known, honest failure mode. `subtype`/`is_error` come from the
    `--output-format json` envelope when it parsed (`obj.get("subtype")`,
    `obj.get("is_error")`); `raw_output` is the tick's raw stdout+stderr,
    used only as a fallback text-signature scan when the JSON didn't parse
    (e.g. an auth failure prints plain text, not JSON — verified live
    2026-07-25: "Not logged in · Please run /login").

    Returns one of the HB_* outcome tokens. `exit_code == 0` always returns
    HB_BACKUP_RAN regardless of subtype (mirrors the pre-existing behavior:
    only a non-zero exit needs classifying at all).

    #825 hardening — TIMEOUT is trusted ONLY on a deterministic signal
    (`subtype == "error_timeout"`, parsed from the JSON envelope, or
    `exit_code == 124`, the POSIX `timeout(1)` convention for "killed by
    SIGTERM after the wall-clock deadline"). Both are facts the caller
    MEASURED, not a text guess. Before this fix, a bare `"timed out" in
    raw_output.lower()` substring was sufficient on its own: a dept that is
    genuinely DOWN due to a network-layer failure (DNS/proxy/firewall to the
    Anthropic API) can emit CLI/curl text containing "timed out" with
    neither signal present, which would have classified it BACKUP-TIMEOUT
    ("may be transient" — no restart) and silently denied the auto-restart a
    truly-dead dept needs. A fuzzy text-only "timed out" no longer qualifies
    as TIMEOUT; it now falls through to the auth check and then to the
    unknown-crash default (HB_BACKUP_FAILED), which biases every ambiguous
    case toward restart instead of the benign label — restarting a dept that
    merely timed out costs one guardrail slot; silently skipping the restart
    of a dead one costs the dept. The auth substring scan is left as-is: it
    is the verified, dominant real signature for that failure mode (plain
    text, not JSON — see 2026-07-25 note above), and a false "not logged in"
    match is not a plausible network-failure text collision the way "timed
    out" is.
    """
    if exit_code == 0:
        return HB_BACKUP_RAN
    if subtype == "error_max_budget_usd":
        return HB_BACKUP_BUDGET_EXCEEDED
    if subtype == "error_timeout" or exit_code == 124:
        return HB_BACKUP_TIMEOUT
    text_l = raw_output.lower()
    if (
        "not logged in" in text_l
        or "please run /login" in text_l
        or "please run `/login`" in text_l
        or subtype == "error_auth"
    ):
        return HB_BACKUP_AUTH_FAILED
    # Unknown non-zero exit with none of the above signatures — the ONLY case
    # that should say "dept DOWN". This is now also the fallback for a
    # fuzzy/unverified "timed out" text match (see #825 note above):
    # ambiguous → restart, never silently benign.
    return HB_BACKUP_FAILED


def format_external_heartbeat(
    outcome: str,
    layer: Optional[int] = None,
    exit_code: Optional[int] = None,
    ts: Optional[str] = None,
    detail: Optional[str] = None,
) -> str:
    """Build ONE truthful heartbeat line the floor appends to the dept's
    outputs/<today>/heartbeat.log when the live loop is stale.

    The line starts with an ISO-8601 UTC timestamp (so latest_heartbeat_epoch
    and every freshness consumer keep parsing it) followed by ``tick`` and a
    grep-able outcome token. Cases (one per call site):

      * floor ran a layer tick OK   → ``<iso> tick BACKUP-RAN-FOR-DEPT layer=N exit=0``
      * tick hit its budget cap     → ``<iso> tick BACKUP-BUDGET-EXCEEDED exit=N — tick ran, hit budget cap (not down)``
      * tick couldn't authenticate  → ``<iso> tick BACKUP-AUTH-FAILED exit=N — needs re-login``
      * tick ran out of time        → ``<iso> tick BACKUP-TIMEOUT exit=N``
      * tick crashed, unknown cause → ``<iso> tick BACKUP-FAILED exit=N — dept DOWN``
      * degraded L4 carried-over    → ``<iso> tick DEGRADED-L4 carried-over``
      * floor deferred (#1313)      → ``<iso> tick BACKUP-DEFERRED — <detail>``

    Budget/auth/timeout are DISTINCT from a genuine crash (#749/#750 defect
    (b)): the tick ran, it just hit a known, honest limit — NOT evidence the
    dept process is dead. Only HB_BACKUP_FAILED (no known signature matched)
    means "dept DOWN". BACKUP-DEFERRED (#1313 step 2) is a THIRD category:
    nothing ran at all — the floor correctly declined to act rather than
    crash or fall back to a competing headless model.

    `outcome` is one of the HB_* constants. `ts` defaults to now (UTC ISO,
    second resolution — same shape as now_iso()). `detail` is only used by
    BACKUP-DEFERRED, to carry the specific reason (mirrors the same string
    the caller already passed to emit_event) so a human reading the raw
    heartbeat file — not just the cockpit — sees WHY, not just THAT.
    """
    stamp = ts or now_iso()
    if outcome == HB_BACKUP_RAN:
        body = HB_BACKUP_RAN
        if layer is not None:
            body += f" layer={int(layer)}"
        # exit is part of the truth even on the OK path (always 0 here, but
        # explicit so a consumer never has to assume).
        body += f" exit={int(exit_code) if exit_code is not None else 0}"
    elif outcome == HB_BACKUP_FAILED:
        code = int(exit_code) if exit_code is not None else 1
        body = f"{HB_BACKUP_FAILED} exit={code} — dept DOWN"
    elif outcome == HB_BACKUP_BUDGET_EXCEEDED:
        code = int(exit_code) if exit_code is not None else 1
        body = f"{HB_BACKUP_BUDGET_EXCEEDED} exit={code} — tick ran, hit budget cap (not down)"
    elif outcome == HB_BACKUP_AUTH_FAILED:
        code = int(exit_code) if exit_code is not None else 1
        body = f"{HB_BACKUP_AUTH_FAILED} exit={code} — needs re-login"
    elif outcome == HB_BACKUP_TIMEOUT:
        code = int(exit_code) if exit_code is not None else 1
        body = f"{HB_BACKUP_TIMEOUT} exit={code}"
    elif outcome == HB_DEGRADED_L4:
        body = f"{HB_DEGRADED_L4} carried-over"
    elif outcome == HB_BACKUP_DEFERRED:
        body = HB_BACKUP_DEFERRED
        if detail:
            body += f" — {detail}"
    else:
        raise ValueError(f"unknown external-heartbeat outcome: {outcome!r}")
    return f"{stamp} tick {body}"


def append_external_heartbeat(
    heartbeat_path: str,
    outcome: str,
    layer: Optional[int] = None,
    exit_code: Optional[int] = None,
    ts: Optional[str] = None,
    detail: Optional[str] = None,
) -> str:
    """Append one truthful heartbeat line (format_external_heartbeat) to the
    dept's heartbeat.log, creating parent dirs as needed. Returns the line
    written (without the trailing newline) so the caller can log it.

    Append-only + a trailing newline so a half-written line never corrupts the
    file and the freshness reader (which reads the LAST ISO ts) keeps working.
    """
    line = format_external_heartbeat(outcome, layer=layer, exit_code=exit_code, ts=ts, detail=detail)
    parent = os.path.dirname(heartbeat_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(heartbeat_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return line
