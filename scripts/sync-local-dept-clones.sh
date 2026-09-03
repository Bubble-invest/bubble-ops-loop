#!/usr/bin/env bash
# sync-local-dept-clones.sh — keep the VPS's READ-ONLY clones of host:local depts
# fresh from GitHub, so the disk-mode cockpit renders their latest gates / state /
# heartbeat.
#
# Why (Hybrid local/VPS agent, {{OPERATOR}} msg 4258, 2026-06-11): a dept may run its
# /loop on its OWN machine (e.g. Miranda's bubble-ops-content on {{OPERATOR_2}}'s Mac —
# real Chrome/tools) by declaring `host: local` in onboarding/STATE.yaml. That
# dept NEVER executes on the VPS (see loop-backup.sh host-skip), but the cockpit
# is disk-mode and renders from disk — so the VPS keeps a READ-ONLY MIRROR of the
# dept's repo and pulls it on a cadence. The dept pushes its outputs/gates/state
# to GitHub from its own machine; this script syncs them down so the operator
# sees the current picture. Pure mirror: NO commit, NO push, NO execution here.
#
# Behaviour:
#   - Glob $AGENTS_ROOT/bubble-ops-* and, for each whose STATE.yaml says
#     host: local, sync it to origin/<default-branch>.
#   - host: vps / host-absent / malformed STATE → SKIP (not a local mirror;
#     fail-safe to "not local", never pull a vps dept by accident).
#   - FAIL-SAFE: a sync failure on one dept LOGS + SKIPS it and the loop
#     CONTINUES to the next dept — one bad mirror must never wedge the others.
#     The script still exits 0 on a transient miss so the systemd timer doesn't
#     flap; a genuinely broken mirror surfaces in the journal.
#   - Idempotent: a clean, up-to-date mirror re-syncs to a no-op.
#
# ── Self-heal (board #667, 2026-07-16) ──────────────────────────────────────
# The mirror is READ-ONLY and GitHub is authoritative for it (inbox/decisions
# hide-markers excepted — see below). A plain `git pull --ff-only` can freeze
# FOREVER against any of three failure modes observed live:
#
#   1. skip-worktree-flagged vendored files (e.g. scripts/lib/dispatch_helpers.py,
#      budget.py) diverging from incoming main. On a read mirror the
#      skip-worktree bit is actively harmful — it hides local divergence from
#      git's own change detection, so `pull --ff-only` aborts ("would be
#      overwritten") without even showing up as a normal dirty-tree case. Fix:
#      clear ALL skip-worktree flags before every sync (a read mirror has no
#      business hiding paths from itself).
#   2. root-owned paths inside the mirror (e.g. .git/index touched by a stray
#      root process — #265 class) → "Permission denied" mid-merge. The sync runs
#      as `claude` and cannot chown. For the common benign case — a root-owned
#      .git/index (derived, and .git/ itself is claude-owned) — we SELF-HEAL by
#      rebuilding the index as claude (rm + reset). For any other root-owned path
#      (real data) we DETECT and WARN loudly with the exact `chown` command, then
#      skip that dept for this tick (never crash the whole run, never touch data).
#   3. aborted-merge debris (MERGE_HEAD / half-written index from an interrupted
#      checkout) blocking every subsequent pull.
#
# Since origin is authoritative and this is a read-only mirror, the robust
# strategy replaces ff-only pull with a converge-to-origin reset:
#   preserve inbox/decisions/* (untracked hide-markers, see below)
#     -> clear all skip-worktree flags
#     -> fetch origin
#     -> reset --hard origin/<default-branch>
#     -> clean -fd (excluding inbox/decisions/)
#     -> restore the preserved hide-markers
# A WARN is logged naming the dept whenever the reset actually diverged from a
# plain fast-forward (i.e. this path did real work beyond what --ff-only would
# have done) — so a self-heal is visible in the journal, not silent.
#
# ── Endangered-state quarantine (review round 2, board #667) ───────────────
# A converge-to-origin reset --hard can ALSO destroy genuine local work, not
# just harmless drift: (1) an uncommitted edit to a TRACKED file (a hot-patch
# not yet committed), or (2) a local commit that never got pushed (HEAD not a
# strict ancestor of upstream). Both are detected BEFORE the reset. If either
# is true:
#   -> quarantine first (best-effort, never blocks convergence):
#        dirty tracked tree  -> `git stash push --include-untracked`
#        local-only commit(s) -> `git bundle create
#          <mirror>/.selfheal-quarantine/<ts>.bundle <upstream>..HEAD`
#   -> after the reset, log a DISTINCT loud line —
#        `WARN <dept>: DISCARDED <N> uncommitted change(s) / <M> local-only
#        commit(s) (<shas>) — verify this wasn't real work` —
#      instead of the generic "mirror self-healed" WARN used for the benign
#      skip-worktree/untracked-collision cases, so an operator scanning the
#      journal can tell "nothing to see here" from "we ate real work".
#
# ── Stranded-branch self-heal (board #1064, 2026-09-03) ─────────────────────
# The converge-to-origin strategy above still assumed the clone was checked
# out on a branch with a live upstream (`@{u}`) — it fetched, resolved
# `@{u}`, then reset --hard to it. If a worker (or manual op) leaves the
# clone checked out on a STALE FEATURE BRANCH with NO upstream configured
# (e.g. `git checkout -b worker-953` with no `git push -u`), `@{u}` resolves
# to nothing and the script used to WARN "no upstream tracking branch
# configured — skipping" and leave the mirror exactly as-is, EVERY 15-min
# run, FOREVER — nothing about that failure mode is self-correcting. Real
# incident: bubble-ops-content stranded on worker-953 for ~8 days while
# Miranda was producing daily; the cockpit read her as cold/not-wired.
#
# Fix: on EVERY sync, resolve origin's canonical (default) branch — via the
# locally-cached `refs/remotes/origin/HEAD` (set by `git clone`; free, no
# network), falling back to `git remote set-head origin --auto` then
# `git remote show origin` for a stale/missing cache — and self-heal to it
# whenever the clone has no upstream OR is not already on that branch:
#   git checkout -B <canonical> origin/<canonical>
#   git branch --set-upstream-to=origin/<canonical> <canonical>
# This is exactly the manual fix that unstuck the real incident, now run
# automatically. It is SAFE to `reset`/`checkout -B` straight to origin's
# branch here because this mirror is READ-ONLY and origin is authoritative —
# there is no local work on it worth preserving (see file header, top) — but
# as belt-and-suspenders we still run the SAME quarantine_endangered_state()
# used by the converge-to-origin path first, so even a stray dirty file
# (tracked OR untracked — review round: an untracked file is quarantined
# too, see any_dirt_count()) or an accidental local commit on the stranded
# branch is stashed/bundled before we abandon it, never silently dropped.
# This matters more here than on the converge-to-origin path: abandoning a
# whole branch (this path) is a strictly riskier operation than converging an
# already-canonical branch, so the net has to be complete, not best-effort
# on tracked dirt alone.
#
# 🔴 FORBIDDEN, and unchanged by this fix: nothing here ever pushes to origin
# or touches a dept repo's branches ON GITHUB — `checkout`/`reset`/`branch
# --set-upstream-to` are all 100% local to the clone.
#
# Escalation: if the canonical branch can't be resolved at all (origin
# unreachable / has no HEAD) — the one case self-heal genuinely can't run —
# we WARN-skip as before, but track a per-dept consecutive-miss counter
# (${AGENTS_ROOT}/.sync-local-dept-clones-state/<slug>.skips, deliberately
# OUTSIDE the mirror so the mirror's own `git clean -fd` can never reset it)
# and, once it reaches ESCALATE_AFTER runs, emit a kanban card via
# tools/kanban/emit_kanban_item.sh — this is the "never silently skip
# forever" half of the fix, for the residual case self-heal can't cover.
#
# inbox/decisions/* hide-markers (board wiki, 2026-07-12 incident): the cockpit
# writes an UNTRACKED decision file straight onto the mirror's disk so a
# resolved gate disappears immediately (list_pending_gates filters on it)
# without waiting for a GitHub round-trip. These files are NEVER pushed/pulled
# through git — they are local-only cockpit state that must survive every
# sync, so `clean -fd` explicitly excludes inbox/decisions/ and the sync
# additionally snapshots + restores that dir around the reset as a second
# guarantee (belt-and-suspenders against a future clean-path change).
#
# Usage: sync-local-dept-clones.sh [--agents-root <dir>]
#   --agents-root  base dir holding bubble-ops-<slug> clones (default
#                  /home/claude/agents). Parameterized so the harness can run
#                  hermetically inside a tmpdir.
#
# Deploy: paired with deploy/templates/sync-local-dept-clones.{service,timer}
# (every ~15 min on the VPS, which has systemd).
set -uo pipefail

AGENTS_ROOT="${BUBBLE_SYNC_AGENTS_ROOT:-/home/claude/agents}"

# ── Stranded-clone self-heal config (board #1064) ───────────────────────────
# ESCALATE_AFTER: consecutive sync misses on one dept before we stop just
# WARNing into the journal and push a loud signal a human/agent will actually
# see (kanban card). Overridable for tests / a tighter operational SLA.
ESCALATE_AFTER="${BUBBLE_SYNC_ESCALATE_AFTER:-3}"
# EMIT_KANBAN: the repo's kanban emitter. Fixed VPS-deployed path by default
# (same convention as scripts/emit_rick_request.sh); overridable so tests can
# point it at a stub without touching the real board.
EMIT_KANBAN="${BUBBLE_SYNC_EMIT_KANBAN:-/home/claude/scripts/emit_kanban_item.sh}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --agents-root) AGENTS_ROOT="${2:?--agents-root needs a value}"; shift 2 ;;
        --agents-root=*) AGENTS_ROOT="${1#--agents-root=}"
                         [ -n "$AGENTS_ROOT" ] || { echo "FATAL: --agents-root= needs a value" >&2; exit 1; }
                         shift ;;
        *) echo "ERR: unknown argument '$1'" >&2; exit 2 ;;
    esac
done

TS()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(TS)] [sync-local-dept-clones] $*"; }

# dept_host <dir>: echo the top-level host: from <dir>/onboarding/STATE.yaml,
# or "vps" if the field/file is absent, unreadable or not exactly "local"
# (fail-safe: never treat a vps dept as a local mirror by accident).
dept_host() {
    local dir="$1"
    local state="${dir}/onboarding/STATE.yaml"
    [[ -f "$state" ]] || { echo "vps"; return 0; }
    local val
    val="$(grep -E '^host:[[:space:]]*' "$state" 2>/dev/null | head -n1 \
            | sed -E 's/^host:[[:space:]]*//; s/[[:space:]]*(#.*)?$//; s/^"(.*)"$/\1/; s/^'\''(.*)'\''$/\1/')"
    [[ "$val" == "local" ]] && echo "local" || echo "vps"
}

# preserve_hide_markers <dir> <stash_dir>: copy inbox/decisions/* (if any) out
# of the mirror into a throwaway stash dir, so a reset/clean cannot touch them.
# Returns the count copied (0 if the dir doesn't exist / is empty).
preserve_hide_markers() {
    local dir="$1" stash="$2"
    local src="${dir}/inbox/decisions"
    [[ -d "$src" ]] || { echo 0; return 0; }
    local n
    n="$(find "$src" -type f 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "${n:-0}" -gt 0 ]]; then
        mkdir -p "$stash"
        cp -a "$src/." "$stash/" 2>/dev/null
    fi
    echo "${n:-0}"
}

# tracked_dirt_count <dir>: count of TRACKED paths with uncommitted changes
# (modified/added/deleted/renamed at HEAD) — i.e. `git status --porcelain`
# lines that are NOT untracked ("??"). Untracked collisions (T7) are a benign,
# already-handled case (origin's tracked version just wins at that path); a
# dirty TRACKED file is a local edit that would otherwise be silently
# clobbered by `reset --hard` with no distinguishing signal. Used ONLY for the
# endangered-state DISCARDED accounting/log (tracked edits + local-only
# commits) — NOT for the quarantine trigger itself, see any_dirt_count below.
tracked_dirt_count() {
    local dir="$1"
    git -C "$dir" status --porcelain 2>/dev/null | grep -vc '^?? ' || true
}

# any_dirt_count <dir> [exclude_regex]: count of ALL `git status --porcelain`
# lines — tracked edits AND untracked paths alike — optionally excluding any
# line matching <exclude_regex> (grep -E, matched against the whole porcelain
# line). Review-round fix (board #1064): the quarantine trigger in
# quarantine_endangered_state() must NOT gate on tracked_dirt_count() alone,
# or a working tree whose ONLY dirt is an untracked file (never `git add`-ed
# — the realistic shape of stray work left on an abandoned branch) never
# triggers the stash at all, and a subsequent `git clean -fd` (branch-abandon
# path) wipes it with nothing quarantined — silent data loss, not just benign
# drift. The optional exclude_regex (board #1096) exists for the #667
# converge path's own endangered-state gate below: inbox/decisions/*
# hide-markers are untracked-but-benign (already copied aside + restored by
# preserve_hide_markers/restore_hide_markers, and already excluded from this
# script's own `clean -fd --exclude=inbox/decisions`), so counting them here
# would misfire the loud "DISCARDED" WARN on every ordinary sync of a dept
# with a pending hide-marker. Called with NO exclude_regex (default "",
# unchanged behavior) on the stranded-branch path, per #339/#1064 — every
# untracked path there is genuinely at risk once the branch is abandoned.
any_dirt_count() {
    local dir="$1" exclude="${2:-}"
    if [[ -n "$exclude" ]]; then
        git -C "$dir" status --porcelain 2>/dev/null | grep -vE "$exclude" | grep -c '.' || true
    else
        git -C "$dir" status --porcelain 2>/dev/null | grep -c '.' || true
    fi
}

# local_only_commit_shas <dir> <upstream_head>: short SHAs (oldest-first) of
# commits reachable from HEAD but not from upstream_head — i.e. commits that
# exist ONLY in this local mirror and would become unreachable (and
# eventually GC'd) once `reset --hard` moves HEAD to upstream. Empty if HEAD
# is already an ancestor of upstream (nothing local-only).
local_only_commit_shas() {
    local dir="$1" upstream_head="$2"
    git -C "$dir" log --reverse --format='%h' "${upstream_head}..HEAD" 2>/dev/null
}

# quarantine_endangered_state <dir> <upstream_head> <quarantine_root> <ts>:
# best-effort safety net for the cases that reset --hard / git clean -fd
# would otherwise silently destroy — NEVER blocks convergence (always
# returns 0, caller ignores failures beyond a WARN). Stashes ALL dirty work
# (tracked edits AND untracked paths, via --include-untracked — gated on
# any_dirt_count, not tracked_dirt_count, so untracked-only dirt is not
# missed, board #1064 review round); bundles local-only commits so a human
# can recover either post-hoc.
quarantine_endangered_state() {
    local dir="$1" upstream_head="$2" qroot="$3" ts="$4"
    if [[ "$(any_dirt_count "$dir")" -gt 0 ]]; then
        git -C "$dir" stash push --include-untracked -m "selfheal-quarantine-${ts}" \
            >/dev/null 2>&1 || log "WARN $(basename "$dir"): quarantine stash FAILED — uncommitted changes could not be saved before reset"
    fi
    local shas; shas="$(local_only_commit_shas "$dir" "$upstream_head")"
    if [[ -n "$shas" ]]; then
        mkdir -p "${qroot}" 2>/dev/null
        git -C "$dir" bundle create "${qroot}/${ts}.bundle" "${upstream_head}..HEAD" \
            >/dev/null 2>&1 || log "WARN $(basename "$dir"): quarantine bundle FAILED — local-only commit(s) ${shas//$'\n'/,} could not be saved before reset"
    fi
    return 0
}

# restore_hide_markers <dir> <stash_dir>: copy the stashed inbox/decisions/*
# files back onto the mirror (creating the dir if the reset/clean removed it),
# then discard the stash. No-op if nothing was stashed.
#
# NO-CLOBBER (cp -n): a hide-marker's path can ALSO be committed upstream by
# the console (host:local gate approval → _write_gate_decision_github commits
# inbox/decisions/<gate_id>.yaml to the dept's own repo). If the reset just
# converged that path to a TRACKED file from origin, origin is authoritative
# and must win — restoring the stashed (pre-sync, local-only) copy over it
# would silently revert an upstream update. cp -n only fills back paths that
# are STILL absent after the reset/clean (i.e. still genuinely untracked).
restore_hide_markers() {
    local dir="$1" stash="$2"
    [[ -d "$stash" ]] || return 0
    local dest="${dir}/inbox/decisions"
    mkdir -p "$dest"
    cp -an "$stash/." "$dest/" 2>/dev/null
    rm -rf "$stash"
}

# clear_skip_worktree <dir>: drop the skip-worktree bit on every path that
# carries it. A READ mirror gains nothing from skip-worktree (it never has
# local edits worth hiding) and it actively breaks convergence — git treats a
# skip-worktree path's on-disk drift as invisible, so a pull/merge that needs
# to touch that path aborts instead of just overwriting it. Echoes the count
# cleared (0 if none).
clear_skip_worktree() {
    local dir="$1"
    local flagged
    flagged="$(git -C "$dir" ls-files -v 2>/dev/null | awk '/^S/ {print substr($0,3)}')"
    [[ -z "$flagged" ]] && { echo 0; return 0; }
    local n=0
    while IFS= read -r path; do
        [[ -z "$path" ]] && continue
        git -C "$dir" update-index --no-skip-worktree -- "$path" 2>/dev/null && n=$((n + 1))
    done <<< "$flagged"
    echo "$n"
}

# root_owned_paths <dir>: list paths under <dir> not owned by the current
# (claude) user. The sync runs as claude and cannot chown these itself; the
# caller logs a loud WARN with the exact fix command and skips the dept.
root_owned_paths() {
    local dir="$1"
    find "$dir" -not -user "$(id -un)" 2>/dev/null
}

# clear_merge_debris <dir>: abort a half-finished merge/checkout (MERGE_HEAD /
# CHERRY_PICK_HEAD present) so it can't block every subsequent sync. Safe on a
# read-only mirror — origin is authoritative, so an in-progress merge here is
# never work worth keeping. Echoes 1 if debris was found+cleared, else 0.
clear_merge_debris() {
    local dir="$1"
    local found=0
    if [[ -f "${dir}/.git/MERGE_HEAD" ]]; then
        git -C "$dir" merge --abort 2>/dev/null
        found=1
    fi
    if [[ -f "${dir}/.git/CHERRY_PICK_HEAD" ]]; then
        git -C "$dir" cherry-pick --abort 2>/dev/null
        found=1
    fi
    # Belt-and-suspenders: a merge/checkout can also die leaving index.lock
    # behind, which blocks EVERY future git invocation in this repo with
    # "Unable to create '.../index.lock': File exists" even though no process
    # is actually running. Safe to remove — flock-adjacent, not the index itself.
    if [[ -f "${dir}/.git/index.lock" ]]; then
        rm -f "${dir}/.git/index.lock" 2>/dev/null
        found=1
    fi
    echo "$found"
}

# resolve_canonical_branch <dir>: echo origin's default branch NAME (e.g.
# "main"), or "" if it cannot be determined. Read-only — never writes to
# origin. Tries, in order:
#   1. the LOCAL cache refs/remotes/origin/HEAD (set by `git clone`, or by a
#      prior successful resolution below) — pure local lookup, no network.
#   2. `git remote set-head origin --auto` (a lightweight query against
#      origin to (re)populate that cache) then re-read it.
#   3. `git remote show origin`'s "HEAD branch:" line — last-resort parse for
#      a git/transport quirk that leaves symbolic-ref empty even after
#      set-head --auto.
resolve_canonical_branch() {
    local dir="$1"
    local ref=""
    ref="$(git -C "$dir" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)"
    ref="${ref#origin/}"
    if [[ -z "$ref" ]]; then
        git -C "$dir" remote set-head origin --auto >/dev/null 2>&1 || true
        ref="$(git -C "$dir" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || true)"
        ref="${ref#origin/}"
    fi
    if [[ -z "$ref" ]]; then
        ref="$(git -C "$dir" remote show origin 2>/dev/null \
                | sed -n 's/^[[:space:]]*HEAD branch:[[:space:]]*//p' | head -n1)"
        [[ "$ref" == "(unknown)" ]] && ref=""
    fi
    echo "$ref"
}

# ── Per-dept consecutive-skip tracking (board #1064) ────────────────────────
# Deliberately stored OUTSIDE any dept mirror: a mirror's own `git clean -fd`
# (run on every SUCCESSFUL sync) would otherwise silently reset a counter we
# specifically need to survive across a run where healing did NOT happen.
SKIP_STATE_DIR="${AGENTS_ROOT}/.sync-local-dept-clones-state"

skip_count_file() { echo "${SKIP_STATE_DIR}/${1}.skips"; }

# bump_skip_count <slug>: increment (creating the state dir as needed) and
# echo the new consecutive-skip count for this dept.
bump_skip_count() {
    local slug="$1" f n
    f="$(skip_count_file "$slug")"
    mkdir -p "$SKIP_STATE_DIR" 2>/dev/null || true
    n=0
    [[ -f "$f" ]] && n="$(cat "$f" 2>/dev/null || echo 0)"
    [[ "$n" =~ ^[0-9]+$ ]] || n=0
    n=$((n + 1))
    echo "$n" > "$f" 2>/dev/null || true
    echo "$n"
}

# clear_skip_count <slug>: a healthy sync (self-heal or plain success) resets
# the streak — escalation is for a dept that's STUCK across runs, not a
# one-off blip.
clear_skip_count() {
    rm -f "$(skip_count_file "$1")" 2>/dev/null || true
}

# escalate_stuck_dept <slug> <dir> <reason> <skip_n>: the "never silently
# skip forever" half of #1064 — for the one case self-heal genuinely can't
# run (origin's canonical branch itself can't be resolved), push a loud
# signal after ESCALATE_AFTER consecutive misses instead of leaving it to a
# human noticing a stale journal WARN. Best-effort: must never crash the sync
# (a dead board/kanban emitter degrades to a log line, nothing more).
escalate_stuck_dept() {
    local slug="$1" dir="$2" reason="$3" skip_n="$4"
    log "ESCALATE ${slug}: stuck for ${skip_n} consecutive sync(s) — ${reason}"
    if [[ -x "$EMIT_KANBAN" ]]; then
        "$EMIT_KANBAN" \
            task="sync-local-dept-clones" \
            title="${slug} mirror stuck ${skip_n}x - ${reason}" \
            body="scripts/sync-local-dept-clones.sh could not converge ${dir} for ${skip_n} consecutive runs (${reason}). Manual fix: git -C ${dir} checkout -B <canonical> origin/<canonical>; git -C ${dir} branch --set-upstream-to=origin/<canonical> <canonical>; git -C ${dir} reset --hard origin/<canonical>." \
            type="incident" \
            priority="high" \
            owner="rnd" \
            budget="5" \
            >/dev/null 2>&1 \
        || log "WARN ${slug}: escalation emit failed (kanban unreachable) — see the WARN above, fix manually"
    else
        log "WARN ${slug}: escalation target ${EMIT_KANBAN} not found/executable — no kanban card emitted, see log only"
    fi
}

# ── Destructive-blast-radius containment (r16 review, 2026-07-16) ──────────
# This script runs reset --hard + clean -fd unattended. Two independent guards:
#  (a) a NON-DEFAULT agents root requires BUBBLE_SYNC_UNSAFE_ROOT=1 — a mistyped
#      --agents-root must fail loudly, never silently converge a workspace;
#  (b) per-repo (below): only dirs whose origin is a Bubble-invest/* GitHub repo
#      are ever touched — a foreign clone matching the glob is SKIPPED with a WARN.
DEFAULT_ROOT="/home/claude/agents"
if [ "$AGENTS_ROOT" != "$DEFAULT_ROOT" ] && [ "${BUBBLE_SYNC_UNSAFE_ROOT:-0}" != "1" ]; then
    echo "FATAL: agents-root '$AGENTS_ROOT' != $DEFAULT_ROOT — refusing destructive sync (set BUBBLE_SYNC_UNSAFE_ROOT=1 to override for tests)" >&2
    exit 1
fi

log "START agents_root=${AGENTS_ROOT}"

LOCAL_COUNT=0
FAIL_COUNT=0
for dir in "${AGENTS_ROOT}"/bubble-ops-*; do
    # Guard (b): never converge a repo whose origin is not a Bubble-invest remote.
    _origin=$(git -C "$dir" remote get-url origin 2>/dev/null || true)
    # BUBBLE_SYNC_ORIGIN_ALLOW: extra grep -E pattern for test fixtures (local bare
    # repos). NEVER set in production units — the Bubble-invest match is the prod rule.
    if printf '%s' "$_origin" | grep -qE 'github\.com[:/]Bubble-invest/'; then
        :
    elif [ -n "${BUBBLE_SYNC_ORIGIN_ALLOW:-}" ] && printf '%s' "$_origin" | grep -qE "${BUBBLE_SYNC_ORIGIN_ALLOW}"; then
        :
    else
        log "WARN $(basename "$dir"): origin '$_origin' is not a Bubble-invest repo — SKIPPED (containment guard)"
        continue
    fi
    [[ -d "$dir" ]] || continue          # no match → glob stays literal; -d guards it
    slug="$(basename "$dir")"; slug="${slug#bubble-ops-}"

    if [[ "$(dept_host "$dir")" != "local" ]]; then
        # vps / host-absent / malformed → not a local mirror; the VPS owns its
        # state directly. Nothing to sync.
        continue
    fi

    LOCAL_COUNT=$((LOCAL_COUNT + 1))
    if [[ ! -d "$dir/.git" ]]; then
        # The read-only clone hasn't been created yet (an activation-time step);
        # log + skip rather than error — never wedge the run on a missing mirror.
        log "skip ${slug}: no git clone at ${dir} yet (mirror not created — activation step)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        continue
    fi

    # Failure mode 2: root-owned paths inside the mirror (#265 class). The
    # sync runs as `claude` and CANNOT fix ownership itself — detect it, log
    # the exact remediation command, and skip this dept for this tick rather
    # than fail mid-reset with a raw "Permission denied".
    root_owned="$(root_owned_paths "$dir")"
    # Self-heal the common footgun: a root-owned .git/index (left by a root
    # restic-restore or a stray sudo git — seen 2026-07-19, blocked the content
    # mirror for days). The index is a DERIVED file and .git/ itself is claude-owned,
    # so we rebuild it as claude WITHOUT chown — rm + reset regenerates it from HEAD.
    # Only auto-heal when EVERY root-owned path is a .git/index; any other root-owned
    # path (real data) we never touch → fall through to the WARN+skip below.
    if [[ -n "$root_owned" ]] && ! echo "$root_owned" | grep -qvE '/\.git/index$'; then
        echo "$root_owned" | while read -r p; do [[ -n "$p" ]] && rm -f "$p"; done
        git -C "$dir" reset -q 2>/dev/null || true
        log "${slug}: self-healed root-owned .git/index (rebuilt as $(id -un)) — was blocking the mirror pull"
        root_owned="$(root_owned_paths "$dir")"
    fi
    if [[ -n "$root_owned" ]]; then
        root_owned_count="$(echo "$root_owned" | grep -c .)"
        log "WARN ${slug}: ${root_owned_count} root-owned path(s) in mirror — sync cannot proceed as user '$(id -un)'. Fix on the VPS: chown -R $(id -un):$(id -gn) '${dir}'"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        continue
    fi

    # Failure mode 3: aborted-merge debris from a prior interrupted run. Clear
    # it BEFORE anything else — a stale MERGE_HEAD/index.lock blocks every git
    # call below, not just the final reset.
    debris_cleared="$(clear_merge_debris "$dir")"
    if [[ "$debris_cleared" == "1" ]]; then
        log "${slug}: cleared aborted-merge debris (MERGE_HEAD/CHERRY_PICK_HEAD/index.lock) left by a prior interrupted sync"
    fi

    # Preserve inbox/decisions/* hide-markers (untracked, cockpit-local,
    # NEVER pushed/pulled — see header) before any reset/clean can touch them.
    stash_dir="$(mktemp -d "/tmp/.sync-local-hide-XXXXXX")"
    hide_marker_count="$(preserve_hide_markers "$dir" "$stash_dir")"

    # Snapshot tracked dirt BEFORE clearing skip-worktree flags. A
    # skip-worktree-flagged vendored file's on-disk drift (failure mode 1) is
    # NOT real local work — it was hidden from git precisely because it's
    # mirror/vendored content, and clearing the flag is what makes that drift
    # visible to `git status`. Snapshotting first means the endangered-state
    # check below (review round 2) only fires on dirt a plain `git status`
    # would ALREADY have shown the operator before any of this script's own
    # bookkeeping ran — i.e. genuine local edits, not self-heal side effects.
    tracked_dirt_n="$(tracked_dirt_count "$dir")"
    # any_dirt_n (tracked + untracked) snapshotted at the same point, for the
    # same reason — feeds the endangered-state gate below (board #1096: an
    # untracked-only dirty tree must not slip past that gate uncounted).
    # inbox/decisions/* is excluded — see any_dirt_count's header comment.
    # Two porcelain shapes both need excluding: an individual untracked file
    # under an already-(partly)-tracked inbox/ ("?? inbox/decisions/x.yaml",
    # the common production shape once inbox/decisions/.gitkeep is tracked),
    # and the WHOLE untracked "inbox/" dir collapsed to one line by git
    # ("?? inbox/") when nothing under it is tracked yet.
    any_dirt_n="$(any_dirt_count "$dir" '^\?\? "?inbox/decisions/|^\?\? "?inbox/?$')"

    # Failure mode 1: clear skip-worktree flags. A read mirror has no business
    # hiding paths from git's own change detection.
    skip_cleared="$(clear_skip_worktree "$dir")"
    if [[ "${skip_cleared:-0}" -gt 0 ]]; then
        log "${slug}: cleared skip-worktree flag on ${skip_cleared} vendored file(s) before sync"
    fi

    # Determine whether a plain fast-forward would have sufficed, so the
    # reset-path can WARN specifically when it did MORE than a ff-pull would
    # have (i.e. an actual self-heal happened, not just a routine advance).
    before_head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo "")"

    if ! git -C "$dir" fetch --quiet origin >/tmp/.sync-local-fetch-$$ 2>&1; then
        log "WARN ${slug}: git fetch failed — skipping (mirror left as-is): $(tail -n1 /tmp/.sync-local-fetch-$$ 2>/dev/null)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        rm -f /tmp/.sync-local-fetch-$$
        restore_hide_markers "$dir" "$stash_dir"
        continue
    fi
    rm -f /tmp/.sync-local-fetch-$$

    current_branch="$(git -C "$dir" symbolic-ref --short -q HEAD 2>/dev/null || echo "")"
    # NOTE: checked via the command's own exit status, not just "is stdout
    # empty" — on an unborn/empty-origin clone, `rev-parse '@{u}'` can FAIL
    # (exit 128, no real upstream) while still echoing the literal '@{u}'
    # token to stdout (a git quirk in its "unresolved revision" message). A
    # naive `|| true` capture would then treat that literal token AS the
    # upstream and misdetect this dept as healthy.
    if upstream="$(git -C "$dir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
        :
    else
        upstream=""
    fi
    # Always resolve canonical — cheap in the healthy common case (a local
    # symbolic-ref lookup, no network) and the only way to catch BOTH stale-
    # clone shapes: no upstream at all, or an upstream that tracks a branch
    # other than the dept's actual canonical branch (board #1064).
    canonical_branch="$(resolve_canonical_branch "$dir")"

    stale_branch=0
    if [[ -z "$upstream" ]]; then
        stale_branch=1
    elif [[ -n "$canonical_branch" && "$current_branch" != "$canonical_branch" ]]; then
        stale_branch=1
    fi

    if [[ "$stale_branch" == "1" ]]; then
        if [[ -z "$canonical_branch" ]]; then
            # Can't even determine what to heal TO — never guess at a branch
            # name. ESCALATE after N consecutive misses instead of warning
            # into the void forever (the exact #1064 failure mode).
            skip_n="$(bump_skip_count "$slug")"
            log "WARN ${slug}: no upstream tracking branch (on '${current_branch:-detached HEAD}') and origin's canonical branch could not be resolved — skipping (mirror left as-is) [consecutive misses: ${skip_n}]"
            [[ "$skip_n" -ge "$ESCALATE_AFTER" ]] && escalate_stuck_dept "$slug" "$dir" "no upstream + canonical branch unresolvable" "$skip_n"
            FAIL_COUNT=$((FAIL_COUNT + 1))
            restore_hide_markers "$dir" "$stash_dir"
            continue
        fi

        target_ref="origin/${canonical_branch}"
        target_head="$(git -C "$dir" rev-parse "$target_ref" 2>/dev/null || echo "")"
        if [[ -z "$target_head" ]]; then
            skip_n="$(bump_skip_count "$slug")"
            log "WARN ${slug}: resolved canonical branch '${canonical_branch}' but ${target_ref} doesn't exist locally after fetch — skipping (mirror left as-is) [consecutive misses: ${skip_n}]"
            [[ "$skip_n" -ge "$ESCALATE_AFTER" ]] && escalate_stuck_dept "$slug" "$dir" "canonical branch '${canonical_branch}' unreachable" "$skip_n"
            FAIL_COUNT=$((FAIL_COUNT + 1))
            restore_hide_markers "$dir" "$stash_dir"
            continue
        fi

        # Best-effort quarantine BEFORE abandoning the stranded branch — same
        # machinery the converge-to-origin path uses below (git stash for
        # dirty files — tracked OR untracked, see any_dirt_count() — a git
        # bundle for commits reachable only from the branch we're about to
        # leave). Belt-and-suspenders: the read-only-mirror assumption (file
        # header, top) says there SHOULDN'T be any real local work here, but
        # we never bet silent data loss on an assumption holding 100% of the
        # time — and the untracked case matters here specifically: the
        # unconditional `git clean -fd` a few lines below (via the shared
        # reset/clean flow) would otherwise wipe an untracked file with no
        # quarantine and no WARN.
        quarantine_endangered_state "$dir" "$target_head" "${dir}/.selfheal-quarantine" "$(date -u +%Y%m%dT%H%M%SZ)-$$"

        if git -C "$dir" checkout -q -B "$canonical_branch" "$target_ref" >/tmp/.sync-local-checkout-$$ 2>&1; then
            git -C "$dir" branch -q --set-upstream-to="$target_ref" "$canonical_branch" 2>/dev/null || true
            rm -f /tmp/.sync-local-checkout-$$
            log "${slug}: self-healed stranded clone — was on '${current_branch:-detached HEAD}' (upstream=${upstream:-none}), checked out canonical branch '${canonical_branch}' + set upstream to ${target_ref}"
            clear_skip_count "$slug"
            # We're now clean and exactly at target_head (checkout -B just
            # reset the tree) — don't let the pre-heal tracked-dirt snapshot
            # (captured above, on the branch we just abandoned) double-fire
            # the endangered-state DISCARDED warning below for state we
            # already quarantined+logged under our own, more specific line.
            tracked_dirt_n=0
            any_dirt_n=0
            current_branch="$canonical_branch"
            upstream="$target_ref"
        else
            skip_n="$(bump_skip_count "$slug")"
            log "WARN ${slug}: self-heal checkout of canonical branch '${canonical_branch}' FAILED — skipping (mirror left as-is): $(tail -n1 /tmp/.sync-local-checkout-$$ 2>/dev/null) [consecutive misses: ${skip_n}]"
            rm -f /tmp/.sync-local-checkout-$$
            [[ "$skip_n" -ge "$ESCALATE_AFTER" ]] && escalate_stuck_dept "$slug" "$dir" "self-heal checkout of '${canonical_branch}' failed" "$skip_n"
            FAIL_COUNT=$((FAIL_COUNT + 1))
            restore_hide_markers "$dir" "$stash_dir"
            continue
        fi
    fi
    upstream_head="$(git -C "$dir" rev-parse "$upstream" 2>/dev/null || echo "")"

    # Would a plain --ff-only pull have converged cleanly from here? That's
    # true iff origin is a strict descendant of (or equal to) our current
    # HEAD — i.e. HEAD is an ancestor of upstream. Used purely to decide
    # whether to WARN below; the reset itself always runs regardless.
    would_have_ff=0
    if [[ -n "$before_head" && -n "$upstream_head" ]]; then
        if git -C "$dir" merge-base --is-ancestor "$before_head" "$upstream_head" 2>/dev/null; then
            would_have_ff=1
        fi
    fi
    # Untracked/tracked dirt in the working tree also would have blocked a
    # plain ff-pull even when the ancestry is clean.
    dirty="$(git -C "$dir" status --porcelain 2>/dev/null)"
    [[ -n "$dirty" ]] && would_have_ff=0

    # ── Endangered-state detection (review round 2, board #667; extended
    # board #1096) ──────────────────────────────────────────────────────────
    # reset --hard is about to run regardless (origin is authoritative for a
    # read-only mirror) but THREE cases would silently destroy REAL local
    # work rather than routine drift: (1) a dirty TRACKED file at HEAD (an
    # uncommitted hot-patch — tracked_dirt_n was snapshotted BEFORE the
    # skip-worktree clear above, so benign vendored-file drift never counts
    # here), (2) an UNTRACKED-only file (any_dirt_n, same snapshot point —
    # board #1096: gating on tracked_dirt_n alone missed this, so the
    # unconditional `git clean -fd` below wiped it with no quarantine, the
    # identical gap #339/#1064 fixed on the stranded-branch path), and
    # (3) HEAD not a strict ancestor of upstream (a local-only commit that
    # never got pushed). Detect ALL THREE before the reset so we can
    # quarantine + WARN distinctly instead of using the same generic
    # self-heal wording as the benign skip-worktree/collision cases.
    local_only_shas="$(local_only_commit_shas "$dir" "$upstream_head")"
    local_only_n=0
    [[ -n "$local_only_shas" ]] && local_only_n="$(echo "$local_only_shas" | grep -c .)"
    endangered=0
    if [[ "${tracked_dirt_n:-0}" -gt 0 || "${any_dirt_n:-0}" -gt 0 || "${local_only_n:-0}" -gt 0 ]]; then
        endangered=1
    fi

    if [[ "$endangered" == "1" ]]; then
        quarantine_ts="$(date -u +%Y%m%dT%H%M%SZ)-$$"
        quarantine_root="${dir}/.selfheal-quarantine"
        quarantine_endangered_state "$dir" "$upstream_head" "$quarantine_root" "$quarantine_ts" || true
    fi

    # Read-only converge-to-origin: origin is authoritative for this mirror,
    # so reset --hard + clean -fd (excluding inbox/decisions, which we already
    # stashed above as a second guarantee, AND .selfheal-quarantine/ — the
    # bundle we may have just written above is untracked and would otherwise
    # be deleted by this same clean) instead of a ff-only pull that can
    # freeze forever on any of the three failure modes.
    reset_ok=1
    if ! git -C "$dir" reset --hard "$upstream_head" >/tmp/.sync-local-reset-$$ 2>&1; then
        log "WARN ${slug}: git reset --hard failed — skipping (mirror left as-is): $(tail -n1 /tmp/.sync-local-reset-$$ 2>/dev/null)"
        reset_ok=0
    fi
    rm -f /tmp/.sync-local-reset-$$

    if [[ "$reset_ok" == "1" ]]; then
        git -C "$dir" clean -fd --exclude=inbox/decisions --exclude=.selfheal-quarantine >/tmp/.sync-local-clean-$$ 2>&1
        rm -f /tmp/.sync-local-clean-$$
    fi

    # Restore the hide-markers regardless of reset/clean outcome — they are
    # cockpit-local state, never git-tracked, and must never be lost even on
    # a failed sync.
    restore_hide_markers "$dir" "$stash_dir"

    if [[ "$reset_ok" != "1" ]]; then
        FAIL_COUNT=$((FAIL_COUNT + 1))
        continue
    fi

    after_head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo "")"
    if [[ "$endangered" == "1" ]]; then
        # DISTINCT loud line (review round 2; count fixed board #1096): this is
        # NOT routine drift — the tree had uncommitted tracked and/or untracked
        # changes and/or local-only commits that reset --hard + clean -fd just
        # discarded. any_dirt_n (not tracked_dirt_n) is used for the count so
        # an untracked-only tree doesn't misreport "0 uncommitted change(s)"
        # despite genuinely having some. Wording is deliberately different
        # from the benign self-heal WARN below so an operator scanning the
        # journal can tell "nothing to see here" from "we ate real work" at a
        # glance.
        log "WARN ${slug}: DISCARDED ${any_dirt_n:-0} uncommitted change(s) (tracked+untracked) / ${local_only_n:-0} local-only commit(s) (${local_only_shas//$'\n'/,}) — verify this wasn't real work (quarantined under ${dir}/.selfheal-quarantine/)"
    elif [[ "$before_head" == "$after_head" ]]; then
        log "synced ${slug}: already up to date (${after_head:0:12})"
    elif [[ "$would_have_ff" == "1" ]]; then
        log "synced ${slug}: fast-forwarded ${before_head:0:12}..${after_head:0:12}"
    else
        # The reset did MORE than a plain ff-pull would have — a self-heal
        # actually fired. Always surface this loudly (board #667 requirement).
        log "WARN ${slug}: reset required to converge (plain ff-pull would have failed/aborted) ${before_head:0:12}..${after_head:0:12} — mirror self-healed"
    fi
    if [[ "${hide_marker_count:-0}" -gt 0 ]]; then
        log "${slug}: preserved ${hide_marker_count} inbox/decisions hide-marker(s) across sync"
    fi
done

log "DONE local_depts=${LOCAL_COUNT} failures=${FAIL_COUNT}"
# Always exit 0: a transient sync miss must not flap the systemd timer. Genuine
# breakage is visible in the journal (the WARN lines + the failures= count).
exit 0
