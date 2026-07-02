#!/usr/bin/env bash
# test_kanban_drain_wiring.sh — verify the #440 dead-letter drain fix.
#
# #440's gap: tools/kanban/drain_kanban_queue.sh already existed and worked,
# but NOTHING ever called it — no timer, no cron, no loop hook. A card that
# fell to the local kanban_queue.jsonl fallback stayed parked forever.
#
# This test covers two things:
#
#   1. WIRING — the new kanban-queue-drain.service/.timer + install script
#      exist, are shaped like the repo's other periodic-sweep units (mirrors
#      secrets-tmp-sweep), and the install script is idempotent-safe (no
#      systemctl calls unless run as an actual installer — we only check
#      structure/paths here, not a live systemd install).
#
#   2. FUNCTIONAL DRAIN — a 2-entry fixture queue (one entry with a
#      resolvable gh auth path that succeeds, one that fails because gh
#      create errors) drains correctly: the success entry is removed from
#      the live queue and archived to <queue>.drained; the failure entry
#      stays in the live queue and is logged.
#
# Run: bash tests/test_kanban_drain_wiring.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRAIN="$REPO_ROOT/tools/kanban/drain_kanban_queue.sh"
SERVICE="$REPO_ROOT/deploy/templates/kanban-queue-drain.service"
TIMER="$REPO_ROOT/deploy/templates/kanban-queue-drain.timer"
INSTALLER="$REPO_ROOT/scripts/install-kanban-drain.sh"

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "== test_kanban_drain_wiring.sh =="

# ── 1. Wiring exists and is shaped correctly ─────────────────────────────────
echo "1. wiring files"

[[ -f "$DRAIN" ]]     && ok "drain_kanban_queue.sh exists" || bad "drain_kanban_queue.sh missing"
[[ -x "$DRAIN" ]]     && ok "drain_kanban_queue.sh is executable" || bad "drain_kanban_queue.sh not executable"
[[ -f "$SERVICE" ]]   && ok "kanban-queue-drain.service exists" || bad "kanban-queue-drain.service missing"
[[ -f "$TIMER" ]]     && ok "kanban-queue-drain.timer exists" || bad "kanban-queue-drain.timer missing"
[[ -f "$INSTALLER" ]] && ok "install-kanban-drain.sh exists" || bad "install-kanban-drain.sh missing"
[[ -x "$INSTALLER" ]] && ok "install-kanban-drain.sh is executable" || bad "install-kanban-drain.sh not executable"

if [[ -f "$SERVICE" ]]; then
  grep -q "^Type=oneshot" "$SERVICE" && ok "service is Type=oneshot" || bad "service not Type=oneshot"
  grep -q "ExecStart=.*drain_kanban_queue.sh" "$SERVICE" && ok "service ExecStart points at drain_kanban_queue.sh" || bad "service ExecStart wrong"
  grep -q "^\[Install\]" "$SERVICE" && ok "service has [Install] section" || bad "service missing [Install]"
fi

if [[ -f "$TIMER" ]]; then
  grep -q "Unit=kanban-queue-drain.service" "$TIMER" && ok "timer targets kanban-queue-drain.service" || bad "timer Unit= wrong"
  grep -q "OnUnitActiveSec=" "$TIMER" && ok "timer has a recurring interval" || bad "timer missing OnUnitActiveSec"
  grep -q "^Persistent=true" "$TIMER" && ok "timer is Persistent (catches up after sleep/downtime)" || bad "timer not Persistent"
fi

if [[ -f "$INSTALLER" ]]; then
  grep -q "kanban-queue-drain.service" "$INSTALLER" && grep -q "kanban-queue-drain.timer" "$INSTALLER" \
    && ok "installer references both unit files" || bad "installer missing unit file references"
  grep -q "enable --now kanban-queue-drain.timer" "$INSTALLER" && ok "installer enables the timer" || bad "installer does not enable the timer"
fi

# ── 2. Functional drain — 2-entry fixture, one drains, one stays ────────────
echo "2. functional drain against fixture queue"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
QUEUE="$WORK/kanban_queue.jsonl"
DRAINED="$WORK/kanban_queue.drained"

# Entry A: will "succeed" (fake gh returns a URL for tasks matching *-ok-*).
# Entry B: will "fail" (fake gh errors for tasks matching *-fail-*).
cat > "$QUEUE" <<'EOF'
{"task":"drain-test-ok-1","severity":"kanban_only","message":"(kanban-only emit) OK card","steps":[],"kanban_items":[{"title":"Drain fixture OK card","type":"incident","priority":"normal","owner":"rnd"}]}
{"task":"drain-test-fail-1","severity":"kanban_only","message":"(kanban-only emit) FAIL card","steps":[],"kanban_items":[{"title":"Drain fixture FAIL card","type":"incident","priority":"normal","owner":"rnd"}]}
EOF

# Fake `gh` on PATH: `gh issue list` always reports no existing issue
# (idempotency check passes through); `gh issue create` succeeds for the OK
# task and fails for the FAIL task, simulating one entry recovering (e.g.
# #180's token-path fix) and one still genuinely broken.
FAKEBIN="$WORK/fakebin"
mkdir -p "$FAKEBIN"
cat > "$FAKEBIN/gh" <<'FAKEGH'
#!/usr/bin/env bash
if [[ "$1" == "auth" && "$2" == "status" ]]; then
  exit 0
fi
if [[ "$1" == "issue" && "$2" == "list" ]]; then
  # No pre-existing issue — force real create path.
  echo ""
  exit 0
fi
if [[ "$1" == "issue" && "$2" == "create" ]]; then
  body=""
  title=""
  args=("$@")
  for i in "${!args[@]}"; do
    if [[ "${args[$i]}" == "--body-file" ]]; then
      body="${args[$((i+1))]}"
    fi
    if [[ "${args[$i]}" == "--title" ]]; then
      title="${args[$((i+1))]}"
    fi
  done
  if grep -q "emit-task: drain-test-fail-1" "$body" 2>/dev/null || [[ "$title" == *"FAIL card"* ]]; then
    echo "error: simulated gh failure for fail-path fixture" >&2
    exit 1
  fi
  echo "https://github.com/Bubble-invest/bubble-ops-board/issues/99999"
  exit 0
fi
exit 0
FAKEGH
chmod +x "$FAKEBIN/gh"

DRAIN_OUT=$(PATH="$FAKEBIN:$PATH" KANBAN_QUEUE="$QUEUE" DRAIN_DRY_RUN=0 bash "$DRAIN" 2>&1)
DRAIN_RC=$?

[[ "$DRAIN_RC" -eq 2 ]] && ok "drain exits 2 (partial: one failed, expected)" || bad "drain exit was $DRAIN_RC, expected 2"

echo "$DRAIN_OUT" | grep -q "created https://github.com/Bubble-invest/bubble-ops-board/issues/99999" \
  && ok "OK entry reported as created" || bad "OK entry not reported as created. Output: $DRAIN_OUT"

echo "$DRAIN_OUT" | grep -q "FAILED for 'Drain fixture FAIL card'" \
  && ok "FAIL entry reported as failed" || bad "FAIL entry not reported as failed. Output: $DRAIN_OUT"

# The OK entry must be gone from the live queue and archived; the FAIL entry
# must remain in the live queue (fail-open: nothing lost, nothing invented).
if [[ -f "$QUEUE" ]]; then
  grep -q "drain-test-ok-1" "$QUEUE" && bad "OK entry still in live queue (should have drained)" || ok "OK entry removed from live queue"
  grep -q "drain-test-fail-1" "$QUEUE" && ok "FAIL entry still in live queue (left for retry)" || bad "FAIL entry missing from live queue (should be retried, not lost)"
else
  bad "live queue file vanished entirely (should still hold the FAIL entry)"
fi

if [[ -f "$DRAINED" ]]; then
  grep -q "drain-test-ok-1" "$DRAINED" && ok "OK entry archived to .drained" || bad "OK entry not found in .drained archive"
else
  bad ".drained archive not created"
fi

# ── 3. Idempotency — a second run with the SAME fake gh doesn't re-emit the
#      already-drained OK card and leaves the FAIL entry retried the same way ──
echo "3. re-run is safe (only the still-failing entry is retried)"

DRAIN_OUT2=$(PATH="$FAKEBIN:$PATH" KANBAN_QUEUE="$QUEUE" DRAIN_DRY_RUN=0 bash "$DRAIN" 2>&1)
DRAIN_RC2=$?
[[ "$DRAIN_RC2" -eq 2 ]] && ok "re-run still exits 2 (FAIL entry still stuck)" || bad "re-run exit was $DRAIN_RC2, expected 2"
echo "$DRAIN_OUT2" | grep -qc "drain-test-ok-1" && bad "re-run touched the already-drained OK entry" || ok "re-run did not re-touch the drained OK entry"

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
