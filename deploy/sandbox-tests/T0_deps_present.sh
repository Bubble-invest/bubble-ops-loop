#!/usr/bin/env bash
set -euo pipefail
# =============================================================================
# T0_deps_present.sh  —  Host dependency probe (Wave 1)
# -----------------------------------------------------------------------------
# WHAT THIS PROVES
#   The four host-level prerequisites for Claude Code's OS sandbox on this box
#   (Hetzner, Ubuntu 24.04, kernel 6.8) are present:
#     1. bwrap  (bubblewrap) — the unprivileged fs-isolation tool          [DOCS]
#     2. socat                — network relay to the sandbox proxy          [DOCS]
#     3. npm -g @anthropic-ai/sandbox-runtime — seccomp helper that adds
#        Unix-domain-socket blocking                                        [DOCS]
#     4. An AppArmor profile for /usr/bin/bwrap that re-permits the
#        unprivileged user namespace bwrap needs (Ubuntu 24.04 default
#        sets kernel.apparmor_restrict_unprivileged_userns=1 which blocks it).
#
#   Doc confirmation (https://code.claude.com/docs/en/sandboxing):
#     "the sandbox relies on two packages ... bubblewrap ... socat"
#     "Install it with `npm install -g @anthropic-ai/sandbox-runtime` if it is
#      missing." (the seccomp filter)
#     "On Ubuntu 24.04 and later, the default AppArmor policy prevents
#      bubblewrap from creating the user namespaces it needs ... add an
#      AppArmor profile that grants `bwrap` this capability".
#
# RED  (exit 1) = one or more of the four are MISSING. This is the EXPECTED
#                 state today, before host-prep runs.
# GREEN(exit 0) = all four present  →  the box is ready for sandbox init (T1).
#
# All checks run READ-ONLY over `ssh hetzner-root '<cmd>'`. Installs nothing.
# Idempotent and re-runnable.
# =============================================================================

SSH_HOST="${SSH_HOST:-hetzner-root}"
fail=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  GREEN  %s\n' "$*"; }
bad()  { printf '  RED    %s\n' "$*"; fail=1; }

say "=== T0 deps_present — checking host $SSH_HOST ==="

# Pull all evidence in a single ssh round-trip; parse locally.
EVID="$(ssh "$SSH_HOST" '
  echo "BWRAP=$(command -v bwrap 2>/dev/null || true)"
  echo "SOCAT=$(command -v socat 2>/dev/null || true)"
  if npm ls -g @anthropic-ai/sandbox-runtime >/dev/null 2>&1; then
    echo "RUNTIME=present"
  else
    echo "RUNTIME=missing"
  fi
  if sudo apparmor_status 2>/dev/null | grep -qiE "(^| )bwrap"; then
    echo "APPARMOR=loaded"
  elif sudo test -f /etc/apparmor.d/bwrap 2>/dev/null; then
    echo "APPARMOR=profile-file-present-but-not-loaded"
  else
    echo "APPARMOR=absent"
  fi
' 2>/dev/null)"

# shellcheck disable=SC2046
eval "$(printf '%s\n' "$EVID" | sed -n 's/^\([A-Z]*\)=\(.*\)$/\1="\2"/p')"

[ -n "${BWRAP:-}" ]                 && ok "bwrap present: $BWRAP"          || bad "bwrap MISSING"
[ -n "${SOCAT:-}" ]                 && ok "socat present: $SOCAT"          || bad "socat MISSING"
[ "${RUNTIME:-}" = "present" ]      && ok "@anthropic-ai/sandbox-runtime installed (npm -g)" \
                                     || bad "npm -g @anthropic-ai/sandbox-runtime MISSING"
[ "${APPARMOR:-}" = "loaded" ]      && ok "AppArmor bwrap profile loaded"  \
                                     || bad "AppArmor bwrap profile not loaded (state: ${APPARMOR:-unknown})"

echo
if [ "$fail" -ne 0 ]; then
  say "T0 RESULT: RED  — host-prep not yet applied. Evidence:"
  printf '%s\n' "$EVID" | sed 's/^/    /'
  exit 1
fi
say "T0 RESULT: GREEN — all four sandbox prerequisites present."
exit 0
