#!/usr/bin/env bash
# =============================================================================
# bootstrap-dept.sh - UX-2 Component A: scaffold a new bubble-ops-<slug> repo
# on the `onboarding/<slug>` branch, ready for the UX-1 skill to drive.
#
# Notion v5 reference: lines 751-762 (on-disk shape) + 961-977 (branch + PR).
# UX-1 dependency: skills/department-onboarding-guide/ (templates + skill_lib).
#
# Usage:
#   ./bootstrap-dept.sh --slug=miranda --display-name="Miranda" --owner=joris
#
# Test-hook env vars (used by tests/onboarding-bootstrap/ ONLY):
#   BUBBLE_BOOTSTRAP_CLONE_DIR  - override the default /tmp clone parent dir
#   FAKE_GH_REPO_URL            - bypass real github URL; use this for clone
#   FAKE_GH_REPO_EXISTS         - if "1", treat `gh repo view` as exists
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_ROOT="$PROJECT_ROOT/skills/department-onboarding-guide"

# -----------------------------------------------------------------------------
# WS2 hook — vendor the CANONICAL dispatch_helpers.py (+ sibling dispatch tests)
# into a freshly-scaffolded dept clone, so new depts start byte-identical to the
# framework instead of inheriting a stale template. Without this, scaffold.py
# references scripts/lib/dispatch_helpers.py in the generated CLAUDE.md but never
# materializes it (the drift bug WS1/WS2 close). Decision (plan): vendored-at-
# scaffold + sync-script, NO git-submodule.
#
# Idempotent, additive, and SILENT-SAFE: if the canonical is missing it WARNS but
# does not abort the bootstrap (the sync-script can heal later).
# -----------------------------------------------------------------------------
vendor_canonical_dispatch_lib() {
  local target="$1"
  local canon_lib="$PROJECT_ROOT/scripts/lib/dispatch_helpers.py"
  local canon_tests="$PROJECT_ROOT/scripts/lib/tests"
  # Sibling dispatch test files that travel WITH dispatch_helpers.py (kept in
  # sync with scripts/sync-dispatch-lib.sh's DISPATCH_TEST_FILES list).
  local dispatch_test_files=(
    "test_build_dispatch_ctx.py"
    "test_dispatch_layer1_daily.py"
    "test_dispatch_retry_and_push.py"
    "test_layer1_data_sources.py"
    "test_loop_dispatch_layer1.py"
  )
  if [[ ! -f "$canon_lib" ]]; then
    echo "[bootstrap] WARN: canonical dispatch_helpers.py not found at $canon_lib — new dept will be UNSYNCED; run scripts/sync-dispatch-lib.sh to heal." >&2
    return 0
  fi
  mkdir -p "$target/scripts/lib/tests"
  cp -f "$canon_lib" "$target/scripts/lib/dispatch_helpers.py"
  [[ -f "$canon_tests/__init__.py" ]] && cp -f "$canon_tests/__init__.py" "$target/scripts/lib/tests/__init__.py" || : > "$target/scripts/lib/tests/__init__.py"
  local tf
  for tf in "${dispatch_test_files[@]}"; do
    [[ -f "$canon_tests/$tf" ]] && cp -f "$canon_tests/$tf" "$target/scripts/lib/tests/$tf"
  done
  echo "[bootstrap] vendored canonical dispatch_helpers.py (md5 $(md5sum "$canon_lib" | awk '{print $1}')) into $target/scripts/lib/" >&2

  # Per-layer-fire notification stack ({{OPERATOR}} msg 3898, 2026-06-06): every dept
  # MUST ping when a layer fires. Vendor notify.py + loop_notify.py + notion_logbook.py (libs) and
  # tools/notify_layer.py (the CLI wrapper CLAUDE.md STEP F calls). Best-effort.
  local nf
  for nf in notify.py loop_notify.py notion_logbook.py; do
    [[ -f "$PROJECT_ROOT/scripts/lib/$nf" ]] && cp -f "$PROJECT_ROOT/scripts/lib/$nf" "$target/scripts/lib/$nf"
  done
  if [[ -f "$PROJECT_ROOT/tools/notify_layer.py" ]]; then
    mkdir -p "$target/tools"
    cp -f "$PROJECT_ROOT/tools/notify_layer.py" "$target/tools/notify_layer.py"
    chmod +x "$target/tools/notify_layer.py" 2>/dev/null || true
    echo "[bootstrap] vendored notify stack (notify.py, loop_notify.py, tools/notify_layer.py) into $target" >&2
  fi
}

# -----------------------------------------------------------------------------
# usage()
# -----------------------------------------------------------------------------
usage() {
  cat <<'USAGE'
Usage: bootstrap-dept.sh --slug=<slug> --display-name=<name> --owner=<owner> [--level=ops|management] [--children=<slugs>] [--force-recreate] [--accept-existing-empty-repo] [--dry-run]

Synopsis:
  Scaffolds a new bubble-ops-<slug> GitHub repo with the onboarding skeleton,
  on a dedicated onboarding/<slug> branch. Idempotent: refuses to recreate
  an existing repo unless --force-recreate is passed.

Arguments:
  --slug=<slug>            Kebab-case slug (e.g. miranda). Must match ^[a-z][a-z0-9-]+$.
  --display-name=<name>    Human-readable display name (e.g. Miranda).
  --owner=<owner>          Slug of the human operator (e.g. joris).
  --level=ops|management   Department level (default: ops). Use 'management' for
                           aggregator depts like Tony. Required with --children.
  --children=<slugs>       Comma-separated child dept slugs (e.g. ben,maya,miranda,eliot).
                           Required when --level=management. Error if passed without
                           --level=management.
  --force-recreate         Destroy + recreate (NOT auto-destroy; you must
                           explicitly opt in to bypass the existing-repo guard).
  --accept-existing-empty-repo
                           If the GitHub repo already exists AND has 0 commits
                           (empty placeholder), reuse it instead of failing.
                           Safe: nothing to clobber. Useful when an operator
                           pre-creates the repo (e.g. because their broker PAT
                           lacks `repo` scope to create repos via the API).
  --dry-run                Render the skeleton + CLAUDE.md + systemd unit to
                           the local clone dir, but do NOT call gh, do NOT
                           git-init/clone, and do NOT push. Used by Phase G
                           end-to-end smoke harness.
  --help                   Show this message and exit.

Example (ops leaf):
  ./bootstrap-dept.sh --slug=miranda --display-name="Miranda" --owner=joris

Example (management dept):
  ./bootstrap-dept.sh --slug=tony --display-name=Tony --owner=joris \
    --level=management --children=ben,maya,miranda,eliot

Test hooks (env vars):
  BUBBLE_BOOTSTRAP_CLONE_DIR  Override the parent dir for the local clone
                              (default: /tmp). Used by the test suite.
  FAKE_GH_REPO_URL            Replace the github.com URL with a local path
                              (for offline tests). Used by the test suite.
  FAKE_GH_REPO_EXISTS         "1" => pretend the repo already exists. Used
                              by the test suite to exercise the refusal path.

Notion v5 references:
  lines 751-762   on-disk shape of an "agent a eclore"
  lines 961-977   branch + commits + PR pattern

USAGE
}

# -----------------------------------------------------------------------------
# arg parsing
# -----------------------------------------------------------------------------
SLUG=""
DISPLAY_NAME=""
OWNER=""
LEVEL="ops"
CHILDREN=""
FORCE_RECREATE=0
ACCEPT_EMPTY=0
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --slug=*) SLUG="${arg#*=}" ;;
    --display-name=*) DISPLAY_NAME="${arg#*=}" ;;
    --owner=*) OWNER="${arg#*=}" ;;
    --level=*) LEVEL="${arg#*=}" ;;
    --children=*) CHILDREN="${arg#*=}" ;;
    --force-recreate) FORCE_RECREATE=1 ;;
    --accept-existing-empty-repo) ACCEPT_EMPTY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

# Validation: slug shape.
if [[ -z "$SLUG" ]]; then
  echo "ERROR: --slug is required" >&2; usage >&2; exit 64
fi
if [[ ! "$SLUG" =~ ^[a-z][a-z0-9-]+$ ]]; then
  echo "ERROR: --slug '$SLUG' is not kebab-case (^[a-z][a-z0-9-]+$)" >&2
  exit 64
fi
if [[ -z "$DISPLAY_NAME" ]]; then
  echo "ERROR: --display-name is required" >&2; usage >&2; exit 64
fi
if [[ -z "$OWNER" ]]; then
  echo "ERROR: --owner is required" >&2; usage >&2; exit 64
fi

# Validation: level enum.
if [[ "$LEVEL" != "ops" && "$LEVEL" != "management" ]]; then
  echo "ERROR: --level must be 'ops' or 'management', got: '$LEVEL'" >&2
  exit 64
fi

# Validation: children is only valid with --level=management.
if [[ -n "$CHILDREN" && "$LEVEL" != "management" ]]; then
  echo "ERROR: --children is only valid with --level=management. Got --level=$LEVEL." >&2
  exit 64
fi

# Validation: management requires children.
if [[ "$LEVEL" == "management" && -z "$CHILDREN" ]]; then
  echo "ERROR: --level=management requires --children=<comma,separated,slugs>." >&2
  exit 64
fi

REPO_NAME="bubble-ops-${SLUG}"
# GITHUB_OWNER defaults to vdk888 for back-compat with existing fixture
# repos, but can be overridden via env (e.g. Bubble-invest org since
# 2026-05-24 — GitHub Apps can't createRepository on personal user
# accounts, only on orgs, so all new depts target the org).
GITHUB_OWNER="${BUBBLE_GITHUB_OWNER:-vdk888}"
FULL_NAME="${GITHUB_OWNER}/${REPO_NAME}"
BRANCH="onboarding/${SLUG}"

# -----------------------------------------------------------------------------
# Fix 4 — Pre-flight: Telegram bot handle length + uniqueness warning.
#
# BotFather usernames are global + must be <= 32 chars. The convention
# `bubbleops<slug-no-dashes>_bot` = 13 + len(slug-no-dashes) chars.
# So slug-no-dashes max = 19. We fail fast above that with a clear error,
# and emit a global-uniqueness warning when the length is fine.
# -----------------------------------------------------------------------------
SLUG_COMPACT="${SLUG//-/}"
BOT_HANDLE="bubbleops${SLUG_COMPACT}_bot"
HANDLE_LEN=${#BOT_HANDLE}
if (( HANDLE_LEN > 32 )); then
  cat >&2 <<EOF
ERROR: dept slug '$SLUG' produces a Telegram bot handle longer than 32 chars.

  Generated handle: @${BOT_HANDLE}
  Length:           ${HANDLE_LEN} (Telegram BotFather max = 32)

Telegram bot usernames are hard-capped at 32 chars by BotFather. The
convention is 'bubbleops<slug-without-dashes>_bot', so your slug (sans
dashes) must be <= 19 chars.

Pick a shorter slug (e.g. 'ben', 'maya', 'miranda') and re-run bootstrap.
EOF
  exit 64
fi

# Handle fits — warn about global uniqueness on Telegram (the @handle is
# global; BotFather may refuse if it's already taken).
echo "[bootstrap] Note: Telegram bot usernames are global — verify @${BOT_HANDLE} is not already taken before /newbot in BotFather." >&2

CLONE_PARENT="${BUBBLE_BOOTSTRAP_CLONE_DIR:-/tmp}"
CLONE_DIR="${CLONE_PARENT}/${REPO_NAME}"

# -----------------------------------------------------------------------------
# DRY-RUN short-circuit (Phase G1).
# Skip gh, git clone, and git push; render the skeleton into $CLONE_DIR only.
# Used by the Phase G end-to-end smoke harness to verify the rendered tree
# without touching GitHub or the network.
# -----------------------------------------------------------------------------
if [[ "$DRY_RUN" == "1" ]]; then
  # Dry-run is meant to be re-runnable (operator iterating on flags).
  # scaffold.py's init_state() refuses to overwrite an existing STATE.yaml,
  # so we wipe the target before re-rendering. Two safe-cases for the wipe:
  #   1. $CLONE_PARENT is under /tmp/ (the default) — clearly ephemeral.
  #   2. $BUBBLE_BOOTSTRAP_CLONE_DIR was set by the caller — caller owns the
  #      path semantics (used by tests + by the Phase G smoke harness).
  if [[ -d "$CLONE_DIR" ]]; then
    _can_wipe=0
    case "$CLONE_PARENT" in
      /tmp/*|/private/tmp/*|/tmp|/private/tmp) _can_wipe=1 ;;
    esac
    if [[ -n "${BUBBLE_BOOTSTRAP_CLONE_DIR:-}" ]]; then
      _can_wipe=1   # caller-controlled override path
    fi
    if [[ "$_can_wipe" == "1" ]]; then
      rm -rf "$CLONE_DIR"
    else
      echo "[bootstrap] WARNING: dry-run target $CLONE_DIR is not under /tmp/ and BUBBLE_BOOTSTRAP_CLONE_DIR is not set; refusing to wipe." >&2
      echo "[bootstrap]          Delete it manually and re-run, or pass a different --slug." >&2
      exit 1
    fi
  fi
  mkdir -p "$CLONE_DIR"
  echo "[bootstrap] --dry-run: rendering skeleton at $CLONE_DIR (no git, no gh)..."
  _scaffold_args=(
    --slug="$SLUG"
    --display-name="$DISPLAY_NAME"
    --owner="$OWNER"
    --target="$CLONE_DIR"
    --level="$LEVEL"
  )
  if [[ -n "$CHILDREN" ]]; then
    _scaffold_args+=(--children="$CHILDREN")
  fi
  python3 "$SCRIPT_DIR/lib/scaffold.py" "${_scaffold_args[@]}"

  # WS2: vendor the canonical dispatch_helpers.py + sibling tests so the
  # rendered dry-run tree is already in sync with the framework.
  vendor_canonical_dispatch_lib "$CLONE_DIR"

  # Telegram bot handle convention: strip dashes from the slug.
  # (Both SLUG_COMPACT and BOT_HANDLE were already computed above for the
  # length pre-flight, so we just reuse them with the @ prefix.)
  BOT_HANDLE_AT="@${BOT_HANDLE}"
  cat <<EOF

============================================================
  Bootstrap of $DISPLAY_NAME ($REPO_NAME) - DRY-RUN OK
============================================================

Local clone:    $CLONE_DIR
Branch:         (skipped — no git in --dry-run)
GitHub:         (skipped — no gh in --dry-run)

NEXT STEPS (Telegram bot — REQUIRED before deploy):

1. Open Telegram and chat with @BotFather.

2. Send '/newbot'. Use display name '${DISPLAY_NAME}' and username
   '${BOT_HANDLE}'. BotFather will give you a token.
   (Reminder: Telegram bot usernames are global and may already be taken.
   If BotFather refuses, pick a slightly different handle.)

3. Note the token from BotFather (you'll paste it in Step 4 — do NOT
   put it in chat or save it to disk in cleartext).

4. Store the token securely with the 'auth' skill's operator-set-secret
   flow. This writes to the SOPS-encrypted env file without ever showing
   the token in cleartext on disk:

       operator-set-secret.sh \\
           --remote-prompt=hetzner \\
           --project=/etc/bubble/secrets-${SLUG}.sops.env \\
           --key=DEPT_TELEGRAM_BOT_TOKEN

   (Short shim equivalent: 'bubble-set-secret' with the same flags.)

   DO NOT open /etc/bubble/secrets-${SLUG}.sops.env directly in vim /
   nano / any editor — that would defeat the encryption and risk
   committing the cleartext token. Always go through the auth skill
   helper above.

EOF
  exit 0
fi

# -----------------------------------------------------------------------------
# Pre-flight: handle the case where the remote already exists.
#
# Default policy: refuse (safety — won't clobber an existing dept).
# Operator overrides:
#   --force-recreate            destroy + recreate (still unimplemented in v1)
#   --accept-existing-empty-repo  reuse the existing repo iff it has 0 commits
#                                  (safe: nothing to clobber). Useful when the
#                                  operator's PAT lacks `repo` scope to create
#                                  repos but a Bubble admin pre-created an
#                                  empty placeholder.
# -----------------------------------------------------------------------------
echo "[bootstrap] checking whether $FULL_NAME already exists on GitHub..."
REPO_EXISTS=0
REPO_IS_EMPTY=0
if gh repo view "$FULL_NAME" >/dev/null 2>&1; then
  REPO_EXISTS=1
  # An "empty repo" has no default branch (no commits, no main, no anything).
  # gh's defaultBranchRef returns null/missing for empty repos.
  DEFAULT_BRANCH=$(gh api "repos/$FULL_NAME" --jq '.default_branch // "null"' 2>/dev/null)
  if [[ "$DEFAULT_BRANCH" == "null" || -z "$DEFAULT_BRANCH" ]]; then
    REPO_IS_EMPTY=1
  fi
fi

if [[ "$REPO_EXISTS" == "1" ]]; then
  if [[ "$ACCEPT_EMPTY" == "1" && "$REPO_IS_EMPTY" == "1" ]]; then
    echo "[bootstrap] $FULL_NAME exists but is EMPTY (no commits) — reusing per --accept-existing-empty-repo."
    # Skip the gh repo create step below; jump straight to clone+push.
    SKIP_REPO_CREATE=1
  elif [[ "$ACCEPT_EMPTY" == "1" && "$REPO_IS_EMPTY" != "1" ]]; then
    cat >&2 <<EOF
ERROR: repository $FULL_NAME already exists AND has commits.

--accept-existing-empty-repo only allows reuse when the repo has zero
commits (so nothing can be clobbered). This repo has a default branch
($DEFAULT_BRANCH) — it is NOT empty. Refusing to proceed.

If you want to recreate, delete the repo manually and rerun without
--accept-existing-empty-repo.
EOF
    exit 1
  elif [[ "$FORCE_RECREATE" == "1" ]]; then
    echo "[bootstrap] --force-recreate set, but real recreation is unimplemented in v1." >&2
    echo "[bootstrap] please delete the repo manually and rerun." >&2
    exit 2
  else
    cat >&2 <<EOF
ERROR: repository $FULL_NAME already exists.

Bootstrap refuses to overwrite or auto-destroy existing repos. Options:
  --accept-existing-empty-repo  reuse the repo iff it has 0 commits
  --force-recreate              destroy + recreate (NOT auto-destroy;
                                manual cleanup required in v1)

If the repo exists from a prior aborted bootstrap, you probably want to:
  1. cd $CLONE_DIR
  2. git checkout onboarding/${SLUG}
  3. open a Claude Code session there and continue from where you left off.
EOF
    exit 1
  fi
fi
SKIP_REPO_CREATE="${SKIP_REPO_CREATE:-0}"

# -----------------------------------------------------------------------------
# Step 1: create the GitHub repo.
# -----------------------------------------------------------------------------
if [[ "$SKIP_REPO_CREATE" != "1" ]]; then
  echo "[bootstrap] creating GitHub repo $FULL_NAME..."
  gh repo create "$FULL_NAME" \
    --private \
    --description "Bubble Ops dept: $DISPLAY_NAME (onboarding)"
fi

# -----------------------------------------------------------------------------
# Step 2: clone locally.
# -----------------------------------------------------------------------------
mkdir -p "$CLONE_PARENT"
if [[ -d "$CLONE_DIR" ]]; then
  # Local clone exists. Two cases:
  # (a) Reusing-empty-repo path: a prior run left a directory but the remote
  #     was empty. Safe to wipe locally and re-clone (no commits on the
  #     remote to lose).
  # (b) Otherwise: refuse, the operator should investigate.
  if [[ "$ACCEPT_EMPTY" == "1" && "$REPO_IS_EMPTY" == "1" ]]; then
    echo "[bootstrap] local clone $CLONE_DIR exists; wiping (remote is empty, --accept-existing-empty-repo set)..."
    rm -rf "$CLONE_DIR"
  else
    echo "ERROR: local clone $CLONE_DIR already exists." >&2
    echo "       Move or delete $CLONE_DIR manually, then re-run." >&2
    exit 3
  fi
fi

REMOTE_URL="${FAKE_GH_REPO_URL:-https://github.com/${FULL_NAME}.git}"
echo "[bootstrap] cloning $REMOTE_URL -> $CLONE_DIR..."
git clone "$REMOTE_URL" "$CLONE_DIR" 2>&1 | sed 's/^/[git] /' || {
  # Fresh GitHub repo has no HEAD; clone may fail with "warning: You appear..."
  # but still produce a working directory. If not, init manually.
  if [[ ! -d "$CLONE_DIR/.git" ]]; then
    mkdir -p "$CLONE_DIR"
    git -C "$CLONE_DIR" init
    git -C "$CLONE_DIR" remote add origin "$REMOTE_URL"
  fi
}
# Belt-and-braces: ensure origin is set.
if ! git -C "$CLONE_DIR" remote get-url origin >/dev/null 2>&1; then
  git -C "$CLONE_DIR" remote add origin "$REMOTE_URL"
fi

# -----------------------------------------------------------------------------
# Step 3: create the onboarding/<slug> branch.
# -----------------------------------------------------------------------------
echo "[bootstrap] creating branch $BRANCH..."
git -C "$CLONE_DIR" checkout -b "$BRANCH"

# Configure a local identity if none (test env). Use generic ops-loop identity.
if ! git -C "$CLONE_DIR" config user.email >/dev/null 2>&1; then
  git -C "$CLONE_DIR" config user.email "ops-loop-bot@bubble.invest"
  git -C "$CLONE_DIR" config user.name "ops-loop-bot"
fi

# -----------------------------------------------------------------------------
# Step 4: render skeleton via Python (delegates to lib/scaffold.py for
# template rendering using UX-1's skill_lib).
# -----------------------------------------------------------------------------
echo "[bootstrap] rendering skeleton..."
_scaffold_args=(
  --slug="$SLUG"
  --display-name="$DISPLAY_NAME"
  --owner="$OWNER"
  --target="$CLONE_DIR"
  --level="$LEVEL"
)
if [[ -n "$CHILDREN" ]]; then
  _scaffold_args+=(--children="$CHILDREN")
fi
python3 "$SCRIPT_DIR/lib/scaffold.py" "${_scaffold_args[@]}"

# WS2: vendor the canonical dispatch_helpers.py + sibling tests BEFORE the
# initial commit, so the new dept's first commit already carries the in-sync
# dispatch lib (no stale-template drift). Healed later by sync-dispatch-lib.sh.
vendor_canonical_dispatch_lib "$CLONE_DIR"

# -----------------------------------------------------------------------------
# Step 5: initial commit.
# -----------------------------------------------------------------------------
echo "[bootstrap] staging + committing skeleton..."
git -C "$CLONE_DIR" add -A
git -C "$CLONE_DIR" commit \
  -m "bootstrap: ${DISPLAY_NAME} dept (onboarding/${SLUG}) - empty skeleton ready for step 1"

# -----------------------------------------------------------------------------
# Step 6: push the branch (as main AND onboarding/<slug>) using an
# installation-token URL.
#
# Why this dance: bare `git push` over HTTPS uses git's own credential
# helper chain, NOT the GH_TOKEN env var that gh CLI consumes. When
# bubble-ops-bot is the auth path ({{OPERATOR}}'s personal account can't
# createRepository via App token, but the org install can), GH_TOKEN holds
# a `ghs_*` installation token. We have to write that token into the
# remote URL explicitly so git can use it. Then we strip it after the
# push so the saved remote stays clean.
# Caught 2026-05-24 (Maya éclosion msg 3094): without this, push fails
# silently and the GitHub repo stays empty, and Maya's wake-up scaffold
# can't be cloned by anyone else.
# -----------------------------------------------------------------------------
echo "[bootstrap] pushing $BRANCH + main to origin..."
# If GH_TOKEN looks like an App installation token (ghs_*) OR a fine-grained
# PAT (github_pat_*), inject it into the URL for the push. Otherwise rely
# on whatever credential helper is configured (back-compat for ssh remotes).
_push_url="$REMOTE_URL"
case "${GH_TOKEN:-}" in
  ghs_*|github_pat_*|ghp_*)
    # Strip any embedded http(s):// then re-add with the token prefix
    _bare_url="$(echo "$REMOTE_URL" | sed -E 's#^https?://[^/]*##')"
    _push_url="https://x-access-token:${GH_TOKEN}@github.com${_bare_url}"
    ;;
esac
# Temporarily set the token URL, push BOTH branches, then revert.
git -C "$CLONE_DIR" remote set-url origin "$_push_url"
# Push the work branch first
git -C "$CLONE_DIR" push -u origin "$BRANCH" 2>&1 | sed 's/^/[git] /' || {
  git -C "$CLONE_DIR" remote set-url origin "$REMOTE_URL"
  echo "[bootstrap] ERROR: push of $BRANCH failed; aborting" >&2
  exit 1
}
# Also push as main so the repo has a default branch other systems can
# see (dept_registry, gh CLI, web UI etc.).
git -C "$CLONE_DIR" push origin "HEAD:main" 2>&1 | sed 's/^/[git] /' || {
  echo "[bootstrap] WARN: could not push main (branch may already exist)" >&2
}
# Wipe the token from the saved remote URL.
git -C "$CLONE_DIR" remote set-url origin "$REMOTE_URL"

# -----------------------------------------------------------------------------
# Output: hand off to operator.
# -----------------------------------------------------------------------------
cat <<EOF

============================================================
  Bootstrap of $DISPLAY_NAME ($REPO_NAME) - SUCCESS
============================================================

Local clone:    $CLONE_DIR
Remote URL:     ${REMOTE_URL}
Branch:         $BRANCH
GitHub URL:     https://github.com/${FULL_NAME}/tree/${BRANCH}

NEXT STEPS:

1. Install the GitHub App 'bubble-ops-bot' on the new repo (manual; the App
   was created with 'Only select repositories', so each new repo must be
   added via the install UI).
   Install link: https://github.com/apps/bubble-ops-bot/installations/new

2. CREATE THE DEDICATED TELEGRAM BOT (REQUIRED — manual via BotFather):

   a) Open Telegram and chat with @BotFather.
   b) Send '/newbot'.
   c) Use display name '${DISPLAY_NAME}' and username
      '${BOT_HANDLE}'  (Telegram bot usernames must end in _bot).
      (Reminder: handles are global — pick another if BotFather refuses.)
   d) BotFather will return a token. Store it with the 'auth' skill's
      operator-set-secret flow (no cleartext on disk):

          operator-set-secret.sh \\
              --remote-prompt=hetzner \\
              --project=/etc/bubble/secrets-${SLUG}.sops.env \\
              --key=DEPT_TELEGRAM_BOT_TOKEN

      DO NOT open /etc/bubble/secrets-${SLUG}.sops.env directly in vim /
      nano / any editor — that would defeat the encryption.

   Once the token is in /run/claude-agent-${SLUG}/env (decrypted by the
   systemd unit at startup), ${DISPLAY_NAME} will start her own onboarding
   autonomously — she'll ping you on Telegram from her dedicated bot
   @${BOT_HANDLE}. You answer her there; she drives the 7 steps.

3. (Skipping the Claude Code session step: the agent is self-driving via
   CLAUDE.md + the SKILL 'department-onboarding-guide'. Just paste the
   bot token and watch the console at /agents/${SLUG}/onboarding.)

The dept will progress through the 7 statuses:
  Idea -> Configuring -> Drafting -> Needs validation -> Dry run
       -> Ready to activate -> Live

At the end, run scripts/activate-dept.sh to open the activation PR.

EOF
