#!/usr/bin/env bash
# install-cloud-wiki-compile.sh — deploy the cloud-wiki-compile skill + units to the VPS.
# Idempotent: safe to re-run. Part of bubble-ops-loop install manifest.
#
# Installs: the launcher script, intent audit + backfill mission, the
# memory-hygiene notifier (invoked by the pruning step), BOTH SKILLs
# (cloud-wiki-compile + the #1222 skill-authoring skill
# it now also drives via the `skillsmith` mode), each mode's per-headless-run
# skill-visibility symlink (board #1493), the templated service, and the four
# timers (compile nightly; synthesis + pruning + skillsmith weekly).
#
# Run ON the VPS (joris-cx33) as a user with sudo (typically `claude`).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILL_SRC="$REPO_ROOT/skills/cloud-wiki-compile"
SKILLSMITH_SRC="$REPO_ROOT/skills/skill-authoring"
DEPLOY="$REPO_ROOT/deploy"
UNIT_DIR="/etc/systemd/system"

# Tests set a prefix to exercise the real asset-install path without touching
# /home or systemd. Production leaves this empty and retains the live paths.
INSTALL_ROOT="${CLOUD_WIKI_INSTALL_ROOT:-}"
DEPLOY_HOME="${INSTALL_ROOT%/}/home/claude"

SCRIPT_DST="$DEPLOY_HOME/scripts/cloud-wiki-compile.sh"
SKILL_DST="$DEPLOY_HOME/.claude/skills/cloud-wiki-compile/SKILL.md"
INTENT_AUDIT_DST="$DEPLOY_HOME/scripts/wiki-intent-audit.py"
INTENT_PROPOSER_DST="$DEPLOY_HOME/scripts/propose-operator-intents.py"
DELTA_DST="$DEPLOY_HOME/scripts/wiki-delta.py"
CITATION_LINT_DST="$DEPLOY_HOME/scripts/wiki-citation-lint.py"
INTENT_MISSION_DST="$DEPLOY_HOME/.claude/skills/cloud-wiki-compile/missions/intent-backfill.md"
# The pruning step invokes this notifier by absolute path (see SKILL.md). It was
# previously an untracked, hand-deployed orphan under /home/claude/scripts — which
# is how the #874 path-fix drifted out of version control and could not be
# re-verified. Deploying it from the repo keeps the fix + #1223 WORKING_MEMORY.md
# coverage under source control.
MEM_HYGIENE_DST="$DEPLOY_HOME/scripts/memory_hygiene_notify.py"
SKILLSMITH_DST="$DEPLOY_HOME/.claude/skills/skill-authoring"

install -d -m 0755 "$DEPLOY_HOME/scripts"

echo "[1/11] launcher script -> $SCRIPT_DST"
install -m 0755 "$SKILL_SRC/scripts/cloud-wiki-compile.sh" "$SCRIPT_DST"

echo "[2/11] delta planner + citation lint -> $DELTA_DST, $CITATION_LINT_DST"
install -m 0755 "$SKILL_SRC/scripts/wiki_delta.py" "$DELTA_DST"
install -m 0755 "$SKILL_SRC/scripts/wiki_citation_lint.py" "$CITATION_LINT_DST"

echo "[3/11] intent audit -> $INTENT_AUDIT_DST"
install -m 0755 "$SKILL_SRC/scripts/wiki_intent_audit.py" "$INTENT_AUDIT_DST"
install -m 0755 "$REPO_ROOT/tools/propose_operator_intents.py" "$INTENT_PROPOSER_DST"

echo "[4/11] intent backfill mission -> $INTENT_MISSION_DST"
install -d -m 0755 "$(dirname "$INTENT_MISSION_DST")"
install -m 0644 "$SKILL_SRC/missions/intent-backfill.md" "$INTENT_MISSION_DST"

echo "[5/11] memory-hygiene notifier -> $MEM_HYGIENE_DST"
install -m 0755 "$SKILL_SRC/scripts/memory_hygiene_notify.py" "$MEM_HYGIENE_DST"

echo "[6/11] wiki SKILL -> $SKILL_DST"
install -d -m 0755 "$(dirname "$SKILL_DST")"
install -m 0644 "$SKILL_SRC/SKILL.md" "$SKILL_DST"

echo "[7/11] skill-authoring SKILL (#1222) -> $SKILLSMITH_DST"
install -d -m 0755 "$SKILLSMITH_DST/scripts/lib"
install -m 0644 "$SKILLSMITH_SRC/SKILL.md" "$SKILLSMITH_DST/SKILL.md"
install -m 0755 "$SKILLSMITH_SRC/scripts/lib/"*.py "$SKILLSMITH_DST/scripts/lib/"

# Board #1493: `--setting-sources user` resolves each headless run's skills
# from $CLAUDE_CONFIG_DIR/skills, NOT from the shared $SKILLSMITH_DST/$SKILL_DST
# just installed above. The systemd unit's headless.conf drop-in (VPS-side,
# not tracked in this repo — see `systemctl cat cloud-wiki-compile@.service`)
# points CLAUDE_CONFIG_DIR at a per-mode state dir,
# /var/lib/bubble-headless-claude/cloud-wiki-compile-<mode>, root-owned
# (0755 root:root) so `claude` cannot even mkdir a missing one itself. compile/
# synthesis/pruning had a skills/<name> symlink into the shared dir hand-
# provisioned at some point outside this installer; skillsmith's was never
# created at all, so that run saw zero custom skills (only the built-in stock
# ones) and silently no-op'd — the actual #1493 root cause. Provision it here,
# for every mode, idempotently, so this can't drift/be-forgotten again.
#
# CONFIG_ROOT (like DEPLOY_HOME above) is sandboxed under CLOUD_WIKI_INSTALL_ROOT
# for tests, so this step's real logic — not a reimplementation of it — is what
# test_cloud_wiki_skill_visibility.sh exercises, with no sudo/root needed.
# In production (INSTALL_ROOT unset) it runs against the real, root-owned
# path via sudo; ownership is only forced to claude:claude in that case.
CONFIG_ROOT="${INSTALL_ROOT%/}/var/lib/bubble-headless-claude"
AS_ROOT=()
CHOWN_OWNER=()
if [ -z "$INSTALL_ROOT" ]; then
    AS_ROOT=(sudo)
    CHOWN_OWNER=(-o claude -g claude)
fi
echo "[8/11] per-mode headless skill visibility -> $CONFIG_ROOT/cloud-wiki-compile-<mode>/skills"
for mode_skill in compile:cloud-wiki-compile synthesis:cloud-wiki-compile pruning:cloud-wiki-compile skillsmith:skill-authoring; do
    mode="${mode_skill%%:*}"
    skill="${mode_skill#*:}"
    mode_dir="$CONFIG_ROOT/cloud-wiki-compile-$mode"
    target="$DEPLOY_HOME/.claude/skills/$skill"
    link="$mode_dir/skills/$skill"
    "${AS_ROOT[@]}" install -d -m 0700 "${CHOWN_OWNER[@]}" "$mode_dir"
    "${AS_ROOT[@]}" install -d -m 0700 "${CHOWN_OWNER[@]}" "$mode_dir/skills"
    if [ "$("${AS_ROOT[@]}" readlink "$link" 2>/dev/null || true)" != "$target" ]; then
        "${AS_ROOT[@]}" ln -sfn "$target" "$link"
        if [ -z "$INSTALL_ROOT" ]; then
            sudo chown -h claude:claude "$link"
        fi
        echo "  linked $mode -> $skill"
    fi
done

if [ -n "$INSTALL_ROOT" ]; then
    echo "[9/11] systemd units skipped (CLOUD_WIKI_INSTALL_ROOT test layout)"
    echo "[10/11] timer enable skipped (CLOUD_WIKI_INSTALL_ROOT test layout)"
    echo "[11/11] installed test layout under $INSTALL_ROOT"
    exit 0
fi

echo "[9/11] systemd units -> $UNIT_DIR (needs sudo)"
sudo install -m 0644 "$DEPLOY/templates/cloud-wiki-compile@.service"         "$UNIT_DIR/cloud-wiki-compile@.service"
sudo install -m 0644 "$DEPLOY/templates/cloud-wiki-compile-compile.timer"    "$UNIT_DIR/cloud-wiki-compile-compile.timer"
sudo install -m 0644 "$DEPLOY/templates/cloud-wiki-compile-synthesis.timer"  "$UNIT_DIR/cloud-wiki-compile-synthesis.timer"
sudo install -m 0644 "$DEPLOY/templates/cloud-wiki-compile-pruning.timer"    "$UNIT_DIR/cloud-wiki-compile-pruning.timer"
sudo install -m 0644 "$DEPLOY/templates/cloud-wiki-compile-skillsmith.timer" "$UNIT_DIR/cloud-wiki-compile-skillsmith.timer"

echo "[10/11] daemon-reload + enable timers"
sudo systemctl daemon-reload
sudo systemctl enable --now cloud-wiki-compile-compile.timer
sudo systemctl enable --now cloud-wiki-compile-synthesis.timer
sudo systemctl enable --now cloud-wiki-compile-pruning.timer
sudo systemctl enable --now cloud-wiki-compile-skillsmith.timer

echo "[11/11] done. Timers:"
systemctl list-timers --all --no-pager | grep cloud-wiki-compile || true
echo
echo "Manual smoke test (one compile now):"
echo "  sudo systemctl start cloud-wiki-compile@compile.service"
echo "  journalctl -u cloud-wiki-compile@compile.service -f"
echo
echo "Manual smoke test (skill-authoring #1222 now):"
echo "  sudo systemctl start cloud-wiki-compile@skillsmith.service"
echo "  journalctl -u cloud-wiki-compile@skillsmith.service -f"
