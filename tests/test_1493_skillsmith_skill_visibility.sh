#!/usr/bin/env bash
# test_1493_skillsmith_skill_visibility.sh — board #1493 root-cause regression
# test: install-cloud-wiki-compile.sh must provision a per-mode CLAUDE_CONFIG_DIR
# skills/ symlink for EVERY headless mode (compile/synthesis/pruning AND
# skillsmith), not just the three that happened to get it hand-provisioned
# outside the installer. Without it, `--setting-sources user` (headless.conf's
# CLAUDE_CONFIG_DIR=/var/lib/bubble-headless-claude/cloud-wiki-compile-<mode>)
# resolves zero custom skills and the run silently no-ops (exactly what
# happened to skillsmith: exit 0 / is_error=false / subtype=success while the
# model never saw skill-authoring at all).
#
# Hermetic, no sudo/root needed: CLOUD_WIKI_INSTALL_ROOT sandboxes BOTH
# DEPLOY_HOME (/home/claude) and the new CONFIG_ROOT
# (/var/lib/bubble-headless-claude) under a tmp dir, so this test exercises
# the real installer logic (not a reimplementation of it), same pattern as
# tests/test_wiki_intent_installed_layout.sh.
#
# Run: bash tests/test_1493_skillsmith_skill_visibility.sh
# Returns 0 on pass, 1 on any failure.

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
TEST_ROOT=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

run_install() {
    CLOUD_WIKI_INSTALL_ROOT="$TEST_ROOT/root" \
        bash "$REPO_ROOT/scripts/install-cloud-wiki-compile.sh" >"$TEST_ROOT/install.log" 2>&1
}

run_install

DEPLOY_HOME="$TEST_ROOT/root/home/claude"
CONFIG_ROOT="$TEST_ROOT/root/var/lib/bubble-headless-claude"

# --- the #1493 regression check: skillsmith must get a skill-authoring link ---
SKILLSMITH_LINK="$CONFIG_ROOT/cloud-wiki-compile-skillsmith/skills/skill-authoring"
SKILLSMITH_TARGET="$DEPLOY_HOME/.claude/skills/skill-authoring"

[ -L "$SKILLSMITH_LINK" ] || fail "skillsmith skills symlink was not created: $SKILLSMITH_LINK"
[ "$(readlink "$SKILLSMITH_LINK")" = "$SKILLSMITH_TARGET" ] \
    || fail "skillsmith symlink points at $(readlink "$SKILLSMITH_LINK"), expected $SKILLSMITH_TARGET"
[ -f "$SKILLSMITH_LINK/SKILL.md" ] || fail "skillsmith symlink does not resolve to a real SKILL.md"
grep -q 'SKILLSMITH_DONE' "$SKILLSMITH_LINK/SKILL.md" \
    || fail "installed skill-authoring SKILL.md is missing the #1493 completion-marker contract"

# --- the three modes that already worked must still work (no regression) ---
for mode in compile synthesis pruning; do
    link="$CONFIG_ROOT/cloud-wiki-compile-$mode/skills/cloud-wiki-compile"
    target="$DEPLOY_HOME/.claude/skills/cloud-wiki-compile"
    [ -L "$link" ] || fail "$mode skills symlink was not created: $link"
    [ "$(readlink "$link")" = "$target" ] || fail "$mode symlink points at the wrong target"
    [ -f "$link/SKILL.md" ] || fail "$mode symlink does not resolve to a real SKILL.md"
done

# --- idempotency: re-running the installer must not fail or duplicate/relink ---
BEFORE_INODE=$(ls -di "$SKILLSMITH_LINK" | awk '{print $1}')
run_install
AFTER_INODE=$(ls -di "$SKILLSMITH_LINK" | awk '{print $1}')
[ -L "$SKILLSMITH_LINK" ] || fail "skillsmith symlink lost on re-install"
[ "$(readlink "$SKILLSMITH_LINK")" = "$SKILLSMITH_TARGET" ] || fail "re-install broke the skillsmith symlink target"
[ "$BEFORE_INODE" = "$AFTER_INODE" ] || fail "re-install churned the symlink unnecessarily (not idempotent)"

echo "PASS: install-cloud-wiki-compile.sh provisions the per-mode skill-visibility symlink for every mode, including skillsmith (#1493), idempotently"
