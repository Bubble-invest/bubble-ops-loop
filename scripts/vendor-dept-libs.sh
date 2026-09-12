#!/usr/bin/env bash
# vendor-dept-libs.sh — boot-time re-vendor of the canonical shared libs into a
# dept tree, so they can NEVER drift stale ({{OPERATOR}} msg 4025, 2026-06-07).
#
# WHY: dispatch_helpers.py / notify.py / loop_notify.py / notion_logbook.py +
# tools/notify_layer.py are SHARED libs owned by the framework
# (/home/claude/bubble-ops-loop). They're vendored into each dept's scripts/lib
# + tools at onboarding, but they live ON-DISK and are NOT committed to the dept
# repo (non-structural-but-not-runtime-pushable). So any `git checkout`/reset/
# clean-reclone reverts them to the dept's ported baseline — which is exactly how
# safe_pull + the min-time dispatch model silently disappeared from tony/maya/ben
# (2026-06-07). Re-vendoring at EVERY service start makes the framework the single
# source of truth: drift self-heals on the next restart, no per-dept commit needed.
#
# Usage:  vendor-dept-libs.sh <dept-workdir>
#   e.g.  vendor-dept-libs.sh /home/claude/agents/bubble-ops-ben
#
# Idempotent, fail-OPEN (a copy problem must NEVER block the loop from starting):
# any error logs a warning and exits 0. Only copies when the framework file
# differs (cheap) and preserves the dept's own files for anything not in the set.
set -uo pipefail

DEPT="${1:-}"

# Resolve FRAMEWORK with host-aware fallback (fix #234: VPS-only default broke
# Mac host:local agents like Miranda on Jade's machine).
# Resolution order:
#   1. $BUBBLE_FRAMEWORK_ROOT if set (explicit override, highest priority).
#   2. /opt/bubble-ops-loop — board #1115: a ROOT-OWNED checkout (managed by
#      bubble-vps-platform's tasks/access/framework_checkout.py, cloned
#      directly from GitHub via a dedicated read-only deploy key,
#      independent of the claude-writable checkout below). This script is
#      invoked from ExecStartPre=+ (i.e. it runs AS ROOT) — reading the
#      framework source from a directory `claude` cannot write to closes
#      the "root executes claude-controlled code" gap for THIS script.
#      Checked ahead of the legacy candidates below so a box that HAS
#      completed the #1115 cutover automatically prefers it, with zero
#      further changes needed once /opt/bubble-ops-loop exists.
#   3. Sibling of the dept dir: $(dirname <dept-dir>)/bubble-ops-loop
#      — the Mac host:local layout where the dept workspace sits next to
#      bubble-ops-loop in the same parent directory.
#   4. /home/claude/bubble-ops-loop — the canonical VPS path (original
#      default; claude-owned — see board #1115, kept as the fallback for
#      boxes that haven't staged the #1115 cutover yet).
# The first candidate that actually exists on disk wins.
# If none resolve, FRAMEWORK stays empty and the existing fail-open guard below
# catches it (logs WARN and exits 0).
if [[ -n "${BUBBLE_FRAMEWORK_ROOT:-}" ]]; then
  FRAMEWORK="$BUBBLE_FRAMEWORK_ROOT"
else
  FRAMEWORK=""
  # candidate 2: root-owned checkout (board #1115)
  _root_owned="/opt/bubble-ops-loop"
  [[ -d "$_root_owned" ]] && FRAMEWORK="$_root_owned"
  # candidate 3: sibling of dept dir (Mac host:local layout)
  if [[ -z "$FRAMEWORK" && -n "$DEPT" ]]; then
    _sibling="$(dirname "$DEPT")/bubble-ops-loop"
    [[ -d "$_sibling" ]] && FRAMEWORK="$_sibling"
  fi
  # candidate 4: VPS path
  if [[ -z "$FRAMEWORK" ]]; then
    _vps="/home/claude/bubble-ops-loop"
    [[ -d "$_vps" ]] && FRAMEWORK="$_vps"
  fi
fi

log() { logger -t vendor-dept-libs "$*" 2>/dev/null; echo "[vendor-dept-libs] $*" >&2; }

[[ -n "$DEPT" && -d "$DEPT" ]] || { log "WARN: dept dir '$DEPT' missing — skip (fail-open)"; exit 0; }
[[ -d "$FRAMEWORK" ]] || { log "WARN: framework '$FRAMEWORK' missing — skip (fail-open)"; exit 0; }

# Keep the exact bytes last installed by this script under .git, away from the
# dept worktree and its `git add`/clean flows.  The ownership hierarchy is:
#   1. missing destination: copy canonical and record the baseline;
#   2. identical destination: record it as the baseline without rewriting;
#   3. destination == recorded baseline: update from canonical;
#   4. no baseline, or destination changed since baseline: DEFER and preserve.
# A backup followed by overwrite is not safe for a required fork: the daemon
# must keep running the reviewed working bytes while an operator reconciles it.
GIT_DIR="$(git -C "$DEPT" rev-parse --absolute-git-dir 2>/dev/null || true)"
VENDOR_STATE_DIR="${GIT_DIR:+${GIT_DIR}/vendor-dept-libs}"

record_last_vendored() {
  local dst="$1" rel="$2" last
  [[ -n "$VENDOR_STATE_DIR" ]] || {
    log "WARN: cannot record last-vendored copy for $rel — dept is not a git worktree"
    return 0
  }
  last="$VENDOR_STATE_DIR/$rel"
  mkdir -p "$(dirname "$last")" 2>/dev/null || {
    log "WARN: cannot create last-vendored state dir for $rel"
    return 0
  }
  cp -f "$dst" "$last" 2>/dev/null || \
    log "WARN: cannot record last-vendored copy for $rel"
}

copy_canonical_file() {
  local src="$1" dst="$2" dst_dir
  # GNU cp is the reviewed VPS/root path: -T refuses directory-target
  # reinterpretation and --no-dereference refuses a source symlink.
  if cp -T --no-dereference "$src" "$dst" 2>/dev/null; then
    return 0
  fi
  # macOS ships BSD cp without those flags.  The host:local vendor runs as
  # the same UID that owns the dept tree, never as a privileged cross-user
  # writer.  Keep the fallback Darwin-only and require that ownership before
  # using plain BSD cp; a root/foreign-owned destination continues to fail open.
  [[ "$(uname -s 2>/dev/null)" == "Darwin" ]] || return 1
  [[ ! -L "$src" && -f "$src" && ! -L "$dst" ]] || return 1
  [[ ! -e "$dst" || -f "$dst" ]] || return 1
  dst_dir="$(dirname "$dst")"
  [[ "$(stat -f %u "$dst_dir" 2>/dev/null)" == "$(id -u)" ]] || return 1
  cp -f "$src" "$dst" 2>/dev/null
}

permit_vendor_refresh() {
  local src="$1" dst="$2" rel="$3" last
  # A missing destination is safe to create from canonical source.
  [[ -e "$dst" || -L "$dst" ]] || return 0
  # Identical existing bytes need no copy; the caller records them as the
  # managed baseline in its idempotent branch.
  cmp -s "$dst" "$src" 2>/dev/null && return 0
  last="${VENDOR_STATE_DIR:+${VENDOR_STATE_DIR}/$rel}"
  # No baseline means ownership is unknown.  Preserve the live file in place:
  # a backup followed by overwrite is still an outage when the fork is required.
  if [[ -z "$last" || ! -f "$last" || -L "$last" ]]; then
    log "DEFERRED: $rel differs from canonical with no trusted last-vendored baseline — preserved destination"
    return 1
  fi
  # Only bytes still equal to our own prior successful publication are managed
  # and eligible for a routine canonical source update.
  if cmp -s "$dst" "$last" 2>/dev/null; then
    return 0
  fi
  log "DEFERRED: $rel changed since last vendor — preserved destination"
  return 1
}

# Canonical shared libs: "src-relative-to-framework  dest-relative-to-dept".
# Only files the dept actually uses; a dept missing the dest dir is skipped.
MAP=(
  "scripts/lib/dispatch_helpers.py   scripts/lib/dispatch_helpers.py"
  "scripts/lib/notify.py             scripts/lib/notify.py"
  "scripts/lib/loop_notify.py        scripts/lib/loop_notify.py"
  "scripts/lib/notion_logbook.py     scripts/lib/notion_logbook.py"
  "scripts/lib/budget.py             scripts/lib/budget.py"
  "tools/notify_layer.py             tools/notify_layer.py"
)

vendored=0
deferred=0
deferred_rels="|"
for pair in "${MAP[@]}"; do
  # shellcheck disable=SC2086
  set -- $pair
  src="$FRAMEWORK/$1"; dst="$DEPT/$2"
  [[ -f "$src" ]] || { log "skip $1 — not in framework"; continue; }
  # only copy if the dest dir exists (don't create new surfaces a dept doesn't use)
  dst_dir="$(dirname "$dst")"
  [[ -d "$dst_dir" ]] || { log "skip $2 — dept has no $dst_dir/"; continue; }
  # board #1115: refuse a symlink DEST outright rather than writing through
  # it. Plain `cp` (without --remove-destination) opens+truncates whatever
  # an existing dest symlink points to — this script runs as ROOT
  # (ExecStartPre=+), so a dept dir with a dst path replaced by a symlink
  # (e.g. by a compromised claude session with write access to the dept
  # tree) could otherwise redirect a root-run write to an arbitrary
  # root-writable path on the NEXT service restart.
  if [[ -L "$dst" ]]; then
    log "WARN: refusing $2 — dest is a symlink, not a regular file (fail-open, not copied)"
    continue
  fi
  if ! cmp -s "$src" "$dst" 2>/dev/null; then
    if ! permit_vendor_refresh "$src" "$dst" "$2"; then
      deferred=$((deferred+1))
      deferred_rels="${deferred_rels}${2}|"
      continue
    fi
    # -T: dst is always a normal file target (never "copy into directory").
    # --no-dereference: never follow a symlink SRC either (defense in depth).
    if copy_canonical_file "$src" "$dst"; then
      chown claude:claude "$dst" 2>/dev/null || true
      record_last_vendored "$dst" "$2"
      log "re-vendored $2 (was stale/missing)"
      vendored=$((vendored+1))
    else
      log "WARN: could not copy $2 (fail-open)"
    fi
  else
    # Bootstrap/repair the baseline even when no refresh was necessary.
    record_last_vendored "$dst" "$2"
  fi
done

# Fleet-wide kanban-emit capability — a DELIBERATE new shared surface for EVERY
# dept (unlike MAP above, which only fills existing dirs). Every agent must be
# able to file a board card; Ben hit this gap 2026-06-21 (no emit-kanban skill →
# fell back to an unwired local DB). So here we CREATE the dest dirs. The skill
# makes the capability discoverable; the tool is the executable; emit.sh is the
# portable wrapper the skill calls.
KANBAN_MAP=(
  "skills/emit-kanban-task/SKILL.md          skills/emit-kanban-task/SKILL.md"
  "skills/emit-kanban-task/scripts/emit.sh   skills/emit-kanban-task/scripts/emit.sh"
  "tools/kanban/emit_kanban_item.sh          tools/kanban/emit_kanban_item.sh"
)
for pair in "${KANBAN_MAP[@]}"; do
  # shellcheck disable=SC2086
  set -- $pair
  src="$FRAMEWORK/$1"; dst="$DEPT/$2"
  [[ -f "$src" ]] || { log "skip $1 — not in framework"; continue; }
  mkdir -p "$(dirname "$dst")" 2>/dev/null || true
  # board #1115: same symlink-dest refusal as the MAP loop above.
  if [[ -L "$dst" ]]; then
    log "WARN: refusing kanban $2 — dest is a symlink, not a regular file (fail-open, not copied)"
    continue
  fi
  if ! cmp -s "$src" "$dst" 2>/dev/null; then
    if ! permit_vendor_refresh "$src" "$dst" "$2"; then
      deferred=$((deferred+1))
      deferred_rels="${deferred_rels}${2}|"
      continue
    fi
    if copy_canonical_file "$src" "$dst"; then
      chmod +x "$dst" 2>/dev/null || true   # the .sh files must stay executable
      chown claude:claude "$dst" 2>/dev/null || true
      record_last_vendored "$dst" "$2"
      log "re-vendored kanban $2 (was stale/missing)"
      vendored=$((vendored+1))
    else
      log "WARN: could not copy $2 (fail-open)"
    fi
  else
    record_last_vendored "$dst" "$2"
  fi
done

# ---------------------------------------------------------------------------
# Wire .claude/skills/<name> -> ../../skills/<name> so Claude Code can actually
# DISCOVER the dept's DECLARED skills (board #1224).
#
# WHY: Claude Code auto-discovers project-scoped skills ONLY from
# `<cwd>/.claude/skills/`. A dept's declared skill SOURCES (and the fleet-shared
# emit-kanban-task we vendor above) live under `<dept>/skills/` — invisible to
# Claude Code unless each is linked into `.claude/skills/`. Working depts (ben,
# maya, …) got these symlinks by hand at onboarding; morty + claudette were
# missed, so they loaded ZERO SKILL.md skills — not even emit-kanban-task. The
# onboarding scaffold now generates these links too (isolation_scaffold.py
# scaffold_repo_skill_links), but re-asserting here at EVERY service start makes
# the wiring self-heal for existing depts and any that slip through, exactly like
# the vendored libs above.
#
# CRITICAL — ADDITIVE + NON-DESTRUCTIVE, ENSURE-set only (board #1224 review,
# corrected). `.claude/skills/` is a gitignored, HAND-CURATED set — NOT the same
# as the dept.yaml `skills:` block, and it must be preserved exactly. Two hazards
# to avoid, in BOTH directions:
#
#   (a) NEVER link a source just because it exists on disk. A dept's `skills/`
#       dir is a SUPERSET of what it should load. Ben (the live FUND agent) has
#       codex-write / fund-analysis-framework / fund-research on disk but
#       DELIBERATELY unlinked: codex-write has disable-model-invocation:true
#       (script-invoked, not a discovered skill); fund-analysis-framework is
#       superseded by fund-research; fund-research is a DRAFT successor loaded by
#       path from the L2 prompt, intentionally not discoverable. Auto-linking any
#       of them would materially change how the live fund agent researches.
#
#   (b) NEVER REMOVE/unlink an existing `.claude/skills/` entry. Maya declares
#       `skills: {}` yet has 14 working curated links; Ben declares 10 but has 14
#       (alpaca / emit-kanban-task / fund-ideas-scout / weekly-audio-report are
#       linked-but-undeclared and WORKING). Re-deriving the set from dept.yaml
#       would unlink those. We only ADD, never prune.
#
# So we ENSURE-PRESENT (add if missing, link nothing else, remove nothing) the
# union of:
#   - FLEET_SHARED_SKILLS — the skill(s) every dept must be able to use
#     (emit-kanban-task, the one KANBAN_MAP vendors above); Ben hit the gap when
#     his was missing (2026-06-21). Ensured for EVERY dept, incl. those with no
#     dept.yaml (morty/claudette → they get emit-kanban-task wired).
#   - the dept's DECLARED skills from `dept.yaml` `skills:` (flattened across any
#     layer_N sublists), mirroring scaffold_repo_skill_links(enabled_skills).
# A source not in this union is never auto-linked; an existing link outside it is
# never touched. We iterate the ENSURE names (not the on-disk dir listing), so
# "exists on disk" alone can never wire a skill.
#
# Same-uid, intra-repo, relative link → no cross-user (board #1120) issue. This
# runs via `runuser -u agent-<slug>` (the dept uid, NOT root — see
# bubble-agent-prepare), and we only CREATE/repoint symlinks (never write file
# content through a link).
#
# Idempotent + fail-open: an already-correct link is left alone; a real
# (non-symlink) dir/file at the dest is preserved and logged (never clobbered);
# a declared skill with no source on disk is skipped (no dangling link); any
# error logs a warning and continues.

# Flatten dept.yaml `skills:` into a newline list of declared skill names.
# Handles nested (`layer_2:`/`layer_3:` sub-lists), flat lists, and `skills: {}`
# (→ empty). Pure awk (no PyYAML dependency), fail-open to empty on any error.
declared_skills_from_yaml() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  awk '
    /^skills:[[:space:]]*/ { in_s=1; next }         # enter the skills: block
    /^[^[:space:]#]/       { in_s=0 }               # any next top-level key ends it
    in_s && /^[[:space:]]+-[[:space:]]*/ {          # a "  - name" list item
      s=$0
      sub(/^[[:space:]]*-[[:space:]]*/, "", s)      # strip the "- " bullet
      sub(/[[:space:]]*#.*/, "", s)                 # strip a trailing comment
      gsub(/["'\'']/, "", s)                        # strip quotes
      sub(/[[:space:]]+$/, "", s)                   # strip trailing space
      if (s != "") print s
    }
  ' "$f" 2>/dev/null
}

CLAUDE_DIR="$DEPT/.claude"
SKILLS_SRC_DIR="$DEPT/skills"
# A dept with no .claude/ at all is not a Claude-Code project → nothing to wire
# (fine for morty/claudette, which DO have .claude/; guards a non-CC dept).
# The fleet-shared skill(s) every dept must be able to use — ensured for EVERY
# dept regardless of dept.yaml. Keep in sync with KANBAN_MAP above (the skill it
# vendors into every dept's skills/). Space-separated.
FLEET_SHARED_SKILLS="emit-kanban-task"

if [[ -d "$CLAUDE_DIR" && -d "$SKILLS_SRC_DIR" ]]; then
  # Build the ENSURE-PRESENT set = FLEET_SHARED_SKILLS ∪ declared(dept.yaml).
  # ENSURE_NAMES is a newline list (deduped); ENSURE_SEEN is the pipe-delimited
  # membership guard — mirrors the deferred_rels pattern above and stays portable
  # to bash 3.2 (no associative arrays). We iterate these NAMES, never the on-disk
  # dir listing, so a source that merely exists is never auto-linked.
  DEPT_YAML="$DEPT/dept.yaml"
  ENSURE_SEEN="|"
  ENSURE_NAMES=""
  ensure_count=0
  for _sk in $FLEET_SHARED_SKILLS; do
    case "$ENSURE_SEEN" in *"|$_sk|"*) ;; *) ENSURE_SEEN="${ENSURE_SEEN}${_sk}|"; ENSURE_NAMES="${ENSURE_NAMES}${_sk}"$'\n'; ensure_count=$((ensure_count+1)) ;; esac
  done
  declared_count=0
  if [[ -f "$DEPT_YAML" ]]; then
    while IFS= read -r _sk; do
      [[ -n "$_sk" ]] || continue
      declared_count=$((declared_count+1))
      case "$ENSURE_SEEN" in *"|$_sk|"*) ;; *) ENSURE_SEEN="${ENSURE_SEEN}${_sk}|"; ENSURE_NAMES="${ENSURE_NAMES}${_sk}"$'\n'; ensure_count=$((ensure_count+1)) ;; esac
    done < <(declared_skills_from_yaml "$DEPT_YAML")
    log "skill wiring: ensure fleet-shared + $declared_count declared (dept.yaml)"
  else
    log "skill wiring: no dept.yaml — ensure fleet-shared only"
  fi
  # Match the .claude dir's owner so any link/dir we create stays owned by the
  # dept uid (robust across the pre/post-#1120 ownership models).
  claude_owner="$(stat -c '%u:%g' "$CLAUDE_DIR" 2>/dev/null \
                  || stat -f '%u:%g' "$CLAUDE_DIR" 2>/dev/null || true)"
  skills_link_dir="$CLAUDE_DIR/skills"
  if [[ ! -e "$skills_link_dir" ]]; then
    if mkdir -p "$skills_link_dir" 2>/dev/null; then
      [[ -n "$claude_owner" ]] && chown "$claude_owner" "$skills_link_dir" 2>/dev/null || true
    else
      log "WARN: cannot create $skills_link_dir — skip skill wiring (fail-open)"
    fi
  fi
  # Keep an untracked link out of the loop's `git add -A` autocommit (mirrors
  # the KANBAN untracked-file handling above): a runtime discovery link must
  # never be staged into a dept commit (→ push 403 / churn). A link already
  # TRACKED in the dept repo (e.g. a deliberately committed one) is left alone.
  exclude_untracked_link() {
    local rel="$1" excl
    git -C "$DEPT" ls-files --error-unmatch "$rel" >/dev/null 2>&1 && return 0
    excl="$DEPT/.git/info/exclude"
    [[ -f "$excl" ]] || return 0
    grep -qxF "$rel" "$excl" 2>/dev/null && return 0
    printf '%s\n' "$rel" >> "$excl" 2>/dev/null \
      && log "git-excluded untracked skill link $rel" || true
  }
  wired=0
  if [[ -d "$skills_link_dir" ]]; then
    while IFS= read -r name; do
      [[ -n "$name" ]] || continue
      skdir="$SKILLS_SRC_DIR/$name"
      # Ensure ONLY skills whose source actually exists on disk (a real skill dir
      # with a SKILL.md manifest) — a declared skill with no source is skipped so
      # we never create a dangling link.
      [[ -d "$skdir" && -f "$skdir/SKILL.md" ]] || { log "skip $name — no source skills/$name/SKILL.md"; continue; }
      link="$skills_link_dir/$name"
      rel=".claude/skills/$name"
      target="../../skills/$name"
      if [[ -L "$link" ]]; then
        # Already a symlink — repoint only if it aims elsewhere.
        if [[ "$(readlink "$link" 2>/dev/null)" == "$target" ]]; then
          exclude_untracked_link "$rel"
          continue
        fi
        ln -sfn "$target" "$link" 2>/dev/null \
          && { [[ -n "$claude_owner" ]] && chown -h "$claude_owner" "$link" 2>/dev/null || true; \
               exclude_untracked_link "$rel"; \
               log "re-pointed $rel -> $target"; wired=$((wired+1)); } \
          || log "WARN: could not repoint $rel (fail-open)"
      elif [[ -e "$link" ]]; then
        # A real dir/file already occupies the slot — never clobber it.
        log "DEFERRED: $rel exists as a non-symlink — preserved"
      else
        ln -s "$target" "$link" 2>/dev/null \
          && { [[ -n "$claude_owner" ]] && chown -h "$claude_owner" "$link" 2>/dev/null || true; \
               exclude_untracked_link "$rel"; \
               log "wired $rel -> $target"; wired=$((wired+1)); } \
          || log "WARN: could not wire $rel (fail-open)"
      fi
    done <<< "$ENSURE_NAMES"
    [[ "$wired" -gt 0 ]] && log "skill wiring: $wired link(s) (re)created for $(basename "$DEPT")"
  fi
fi

# skip-worktree the vendored TRACKED files so the loop's git add never picks up
# the framework-overwrite (else it commits structural libs → push 403; Tony
# 2026-06-07). Best-effort, fail-open. Covers BOTH the core libs and the
# kanban-capability files.
for pair in "${MAP[@]}" "${KANBAN_MAP[@]}"; do
  # shellcheck disable=SC2086
  set -- $pair
  dst="$DEPT/$2"
  [[ -f "$dst" ]] || continue
  was_deferred=0
  case "$deferred_rels" in
    *"|$2|"*) was_deferred=1 ;;
  esac
  if git -C "$DEPT" ls-files --error-unmatch "$2" >/dev/null 2>&1; then
    if [[ "$was_deferred" == 1 ]]; then
      # A preserved fork must stay visible to status/audit.  Clear any legacy
      # hide bit rather than reintroducing the drift that #1124 exposed.
      if git -C "$DEPT" update-index --no-skip-worktree "$2" 2>/dev/null \
          && git -C "$DEPT" update-index --no-assume-unchanged "$2" 2>/dev/null; then
        log "deferred tracked $2 remains visible (index hide flags cleared)"
      fi
    else
      # Managed canonical files retain the existing anti-autocommit behavior.
      git -C "$DEPT" update-index --skip-worktree "$2" 2>/dev/null \
        && log "skip-worktree set on $2" || true
    fi
  else
    [[ "$was_deferred" == 1 ]] && {
      log "deferred untracked $2 preserved (not newly added to local exclude)"
      continue
    }
    # UNTRACKED → add to .git/info/exclude (local, uncommitted) so `git add`
    # never stages the vendored file into a runtime commit.
    excl="$DEPT/.git/info/exclude"
    if [[ -f "$excl" ]] && ! grep -qxF "$2" "$excl" 2>/dev/null; then
      printf '%s\n' "$2" >> "$excl" && log "git-excluded untracked vendored $2" || true
    fi
  fi
done

log "done — $vendored file(s) refreshed, $deferred deferred for $(basename "$DEPT")"
exit 0
