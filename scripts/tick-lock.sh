#!/usr/bin/env bash
# tick-lock.sh — live-tick concurrency guard for ops-loop depts (board #861).
#
# ── The bug this closes ──────────────────────────────────────────────────────
# loop-backup.sh's `run_backup_tick()` ALREADY takes a non-blocking flock on
# `${LOCK_DIR}/ops-loop-<slug>.tick.lock` before it runs a `claude -p` backup
# tick, and SKIPS (no fire) if that lock is held (see run_backup_tick, "flock
# -n: if the live loop (or a prior backup) holds the lock, skip"). That half
# of the mutex is correct and UNTOUCHED by this file.
#
# The bug is that the LIVE tick never took that same lock. inject_live_loop()
# wakes a live session and polls its heartbeat.log mtime for up to 240s; if
# the live session's tick takes longer than that to reach the step that
# writes the heartbeat (any subagent over ~4 min — L1 7m23s, L2 5m29s,
# reply_handler 5m09s in the incident report), the floor concludes the inject
# failed and falls back to a HEADLESS `claude -p` tick on the SAME due-set —
# while the live tick is still mid-flight. Two agents run the same mission,
# and can disagree (board #861: a duplicated Layer-3 order or Layer-4 letter
# is the blast radius; the incident itself was a dropped inbound lead signal).
#
# ── The fix ───────────────────────────────────────────────────────────────────
# The live tick now holds the SAME lock for the duration of its tick
# (acquired at STEP 1/sync, released at the end of the tick — see the
# `/loop` protocol in scripts/lib/scaffold.py's CLAUDE_MD_OPERATING_TEMPLATE).
# While it's held, run_backup_tick's EXISTING `flock -n` sees it and skips —
# zero changes needed on the backup side.
#
# ── Why a detached holder process ────────────────────────────────────────────
# A live tick is not one continuous shell process — it is a Claude Code
# session issuing many SEPARATE Bash tool calls, each its own subprocess. An
# flock file descriptor cannot survive across those. So `acquire` spawns a
# small DETACHED holder (a short Python script) that opens the lock file,
# takes an exclusive flock(2), writes its own pid to a sidecar pidfile, and
# then just sleeps (bounded — see MAX_HOLD_SECS) until `release` kills it.
# `release` sends TERM (the holder's handler removes the pidfile and exits,
# dropping the flock) and falls back to KILL if it doesn't die promptly.
#
# ── Why this is FAIL-SAFE (the load-bearing property) ────────────────────────
# flock(2) is released by the kernel the INSTANT its holding process exits,
# for ANY reason — clean exit, SIGKILL, OOM-kill, or the whole box going
# down. The holder is a plain (background, disowned) child of whatever
# process ran `tick-lock.sh acquire`, which itself runs inside the dept's
# `ops-loop-<slug>.service` systemd unit. Cgroup membership does not depend
# on the parent/child process tree surviving — a process keeps the cgroup it
# was forked into even if reparented to init — and systemd's default
# KillMode=control-group means ANY stop/crash/restart of that unit kills
# EVERY process still in its cgroup, holder included. So a dept that crashes
# mid-tick can NEVER leave a stale lock behind: the crash and the lock
# release happen together, not "crash now, lock clears eventually."
#
# Belt-and-suspenders on top of that (in case the holder is somehow
# reparented outside the cgroup, or `release` is never called — an agent
# that forgets the last step of its protocol): the holder self-expires after
# MAX_HOLD_SECS (default 1800s = 30min, generously above any real tick in
# the incident report) and releases on its own. This is a THIRD, fully
# independent guarantee that this lock can never wedge a dept's ticking
# forever — it exists purely as a last resort and should never fire in
# normal operation.
#
# ── Why acquire() can NEVER block or fail the caller's tick ──────────────────
# `acquire` always returns 0. If the flock is (implausibly) already held by
# something else — e.g. a backup -p tick that is itself mid-run — the
# Python helper's non-blocking flock attempt fails, no pidfile is written,
# and `acquire` logs a WARN and returns 0 anyway: the live tick proceeds
# WITHOUT the extra guard for this one tick rather than stall. A live fund
# agent must never be blocked from ticking by its own concurrency guard.
#
# Usage:
#   scripts/tick-lock.sh acquire <slug>   # idempotent; never blocks; always ok
#   scripts/tick-lock.sh release <slug>   # idempotent; always ok
#   scripts/tick-lock.sh status  <slug>   # prints "held pid=<N> lock=<path>" (exit 0)
#                                         #     or "free lock=<path>" (exit 1)
#
# Env overrides (mirror loop-backup.sh's own — same lock file, same tests can
# point both scripts at one hermetic tmpdir):
#   BUBBLE_BACKUP_LOCK_DIR      default /run/lock
#   TICK_LOCK_MAX_HOLD_SECS     default 1800 (belt-and-suspenders self-expiry)
#   BUBBLE_TICK_LOCK_PY         default `python3` on PATH

set -uo pipefail
# NOTE: deliberately NOT `set -e` — every code path below must return 0 (or a
# deliberately non-fatal non-zero for `status`), never propagate an unrelated
# command failure as a hard stop. A lock helper that itself can crash the
# calling tick would defeat the entire point of this file.

LOCK_DIR="${BUBBLE_BACKUP_LOCK_DIR:-/run/lock}"
MAX_HOLD_SECS="${TICK_LOCK_MAX_HOLD_SECS:-1800}"
PY="${BUBBLE_TICK_LOCK_PY:-python3}"

TS() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(TS)] [tick-lock] $*"; }

lock_path() { printf '%s/ops-loop-%s.tick.lock' "$LOCK_DIR" "$1"; }
pid_path()  { printf '%s.holder.pid' "$(lock_path "$1")"; }

acquire() {
    local slug="${1:-}"
    if [[ -z "$slug" ]]; then
        log "acquire: missing <slug> argument — no-op (never blocks the caller)"
        return 0
    fi
    local lock; lock="$(lock_path "$slug")"
    local pidfile; pidfile="$(pid_path "$slug")"
    mkdir -p "$LOCK_DIR" 2>/dev/null || true

    # Idempotent: a holder from an EARLIER call in THIS same tick (or a very
    # tight double-invoke) is already alive → no-op success. This also means
    # a live session's back-to-back ticks naturally renew/share the hold
    # instead of contending with themselves.
    if [[ -f "$pidfile" ]]; then
        local existing; existing="$(cat "$pidfile" 2>/dev/null || true)"
        if [[ -n "$existing" ]] && kill -0 "$existing" 2>/dev/null; then
            log "already held for ${slug} (holder pid ${existing}) — no-op"
            return 0
        fi
        rm -f "$pidfile"   # stale pidfile from a dead holder — clean up
    fi

    "$PY" - "$lock" "$pidfile" "$MAX_HOLD_SECS" <<'PYEOF' &
import fcntl
import os
import signal
import sys
import time

lock_path, pidfile, max_hold = sys.argv[1], sys.argv[2], float(sys.argv[3])


def _cleanup(*_a):
    try:
        os.remove(pidfile)
    except OSError:
        pass
    os._exit(0)


signal.signal(signal.SIGTERM, _cleanup)
signal.signal(signal.SIGINT, _cleanup)

fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    # Someone else already holds it (e.g. a backup -p tick, vanishingly
    # unlikely to overlap the moment a live tick starts). Do NOT write a
    # pidfile and do NOT block: the caller must proceed with its tick
    # regardless — see the "never block" doctrine at the top of this file.
    sys.exit(1)

with open(pidfile, "w") as fh:
    fh.write(str(os.getpid()))

# Belt-and-suspenders self-expiry (see file header). Primary release is
# `tick-lock.sh release`; primary crash-safety is the kernel dropping the
# flock the instant THIS process dies for ANY reason, including a cgroup
# teardown on a crashed/restarted systemd unit. This sleep is a THIRD,
# independent guarantee for the case where the holder outlives everything
# else that should have reaped it.
time.sleep(max_hold)
_cleanup()
PYEOF
    local holder_bg_pid=$!
    disown "$holder_bg_pid" 2>/dev/null || true

    # Brief, BOUNDED wait (<=3s) for the holder to open+flock+write its
    # pidfile, so a `release` called immediately after a very short tick
    # doesn't race an empty pidfile. Advisory only — the tick proceeds
    # either way; this loop can never turn into a stall.
    local waited=0
    while [[ ! -s "$pidfile" && $waited -lt 30 ]]; do
        sleep 0.1
        waited=$((waited + 1))
    done

    if [[ -s "$pidfile" ]]; then
        log "acquired for ${slug} (holder pid $(cat "$pidfile" 2>/dev/null))"
    else
        log "WARN could not confirm the lock for ${slug} — proceeding with the tick regardless (this only weakens the anti-double-fire guard for THIS tick; it never blocks the tick itself)"
    fi
    return 0
}

release() {
    local slug="${1:-}"
    if [[ -z "$slug" ]]; then
        log "release: missing <slug> argument — no-op"
        return 0
    fi
    local pidfile; pidfile="$(pid_path "$slug")"
    if [[ -f "$pidfile" ]]; then
        local pid; pid="$(cat "$pidfile" 2>/dev/null || true)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill -TERM "$pid" 2>/dev/null || true
            local waited=0
            while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 30 ]]; do
                sleep 0.1
                waited=$((waited + 1))
            done
            kill -KILL "$pid" 2>/dev/null || true   # belt-and-suspenders
        fi
        rm -f "$pidfile"
    fi
    log "released for ${slug}"
    return 0
}

status() {
    local slug="${1:-}"
    local lock; lock="$(lock_path "$slug")"
    local pidfile; pidfile="$(pid_path "$slug")"
    if [[ -f "$pidfile" ]]; then
        local pid; pid="$(cat "$pidfile" 2>/dev/null || true)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            echo "held pid=${pid} lock=${lock}"
            return 0
        fi
    fi
    echo "free lock=${lock}"
    return 1
}

cmd="${1:-}"
case "$cmd" in
    acquire) shift; acquire "$@" ;;
    release) shift; release "$@" ;;
    status)  shift; status "$@" ;;
    *)
        echo "usage: tick-lock.sh {acquire|release|status} <slug>" >&2
        exit 2
        ;;
esac
