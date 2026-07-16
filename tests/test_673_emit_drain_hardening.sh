#!/usr/bin/env bash
# test_673_emit_drain_hardening.sh — verify board #673's two fixes:
#
#   1. emit_kanban_item.sh::_resolve_gh_token must VERIFY the board is
#      reachable (gh api repos/$BOARD_REPO --jq .name) before trusting
#      ambient auth — a SCOPED GITHUB_TOKEN passes `gh auth status` (it's a
#      real, non-empty token) while every board API call still 404s (the
#      #536 signature, distinct from the #536 EMPTY-token case already
#      covered by test_emit_kanban_fallback_loud.sh). This bit Morty for
#      ~3 weeks.
#
#   2. drain_kanban_queue.sh dedupe must key on normalized TITLE, never on
#      task alone. Observed live: one open issue with
#      task=morty-agentic-audit caused 11 distinct queued replays (each a
#      different finding, same task) to all be archived as "already exists"
#      without ever landing on the board.
#
# Also covers: the drain must handle BOTH queue-line shapes it can see in
# the wild — the per-card {"task":...,"kanban_items":[{...}]} shape written
# by emit_kanban_item.sh's _dashboard_emit, and the notify-stack shape where
# kanban_items[] comes from a monitor-event payload with other fields set
# (severity/message/steps) — both parse through the same PARSED python3
# block in drain_kanban_queue.sh, so this is a shape-coverage check, not a
# separate code path.
#
# Run: bash tests/test_673_emit_drain_hardening.sh
# Returns 0 on pass, 1 on any failure.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EMITTER="$REPO_ROOT/tools/kanban/emit_kanban_item.sh"
DRAIN="$REPO_ROOT/tools/kanban/drain_kanban_queue.sh"

PASS=0; FAIL=0
ok()  { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

echo "== test_673_emit_drain_hardening.sh =="

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ══════════════════════════════════════════════════════════════════════════
# Part 1 — emit_kanban_item.sh: scoped-but-wrong-repo token must NOT be
# trusted just because `gh auth status` passes.
# ══════════════════════════════════════════════════════════════════════════
echo "1. poisoned-env fixture: auth-status-pass but board-api-404 falls through to token file"

STUB1="$WORK/stub1"
mkdir -p "$STUB1"
MARKER1_AMBIENT="$WORK/gh-create-used-ambient"
MARKER1_FILE="$WORK/gh-create-used-filetoken"

# Fake gh: `auth status` ALWAYS passes (simulates a real, non-empty, but
# wrongly-scoped GITHUB_TOKEN — the #536-signature case, distinct from an
# empty token). `gh api repos/<board>` 404s UNLESS GH_TOKEN is the good
# ghs_ file token — this is the reachability probe the fix must perform.
# `issue list`/`label create`/`issue create` succeed only for the good
# token, and record which credential reached them.
cat > "$STUB1/gh" <<FAKEGH
#!/usr/bin/env bash
_good() { [ "\${GH_TOKEN:-}" = "ghs_goodfiletoken" ]; }
case "\${1:-} \${2:-}" in
  "auth status")
    # Always "authed" — mirrors a real, non-empty, merely wrong-scope token.
    exit 0
    ;;
  "api repos/Bubble-invest/bubble-ops-board")
    _good && { echo '"bubble-ops-board"'; exit 0; } || { echo "HTTP 404: Not Found" >&2; exit 1; }
    ;;
  "issue list")
    _good || { echo "GraphQL: Could not resolve to a Repository" >&2; exit 1; }
    echo ""
    ;;
  "label create") _good && exit 0 || exit 1 ;;
  "issue create")
    if _good; then
      touch "$MARKER1_FILE"
      echo "https://github.com/Bubble-invest/bubble-ops-board/issues/88888"
    else
      touch "$MARKER1_AMBIENT"
      echo "GraphQL: Could not resolve to a Repository" >&2
      exit 1
    fi
    ;;
  *) exit 0 ;;
esac
FAKEGH
chmod +x "$STUB1/gh"
cat > "$STUB1/ssh" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
cat > "$STUB1/sudo" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$STUB1/ssh" "$STUB1/sudo"

TOKFILE1="$WORK/board-token-1"
echo "ghs_goodfiletoken" > "$TOKFILE1"
QUEUE1="$WORK/kanban_queue1.jsonl"

# Simulate the scoped-token env exactly as Morty's cron would set it: a real
# (non-empty) GITHUB_TOKEN, aliased into GH_TOKEN, that authenticates but
# cannot see the board repo.
PATH="$STUB1:$PATH" \
GH_TOKEN="ghs_scopedbutwrongrepo" \
BOARD_TOKEN_FILE="$TOKFILE1" \
KANBAN_QUEUE="$QUEUE1" \
TELEGRAM_BOT_TOKEN="" \
BUBBLE_OPERATOR_CHAT_ID="" \
bash "$EMITTER" \
  task=test-673-scoped-token \
  title="673 scoped-token board-reachability card" \
  type=incident \
  owner=rnd \
  budget=10 >/dev/null 2>&1

[ -f "$MARKER1_AMBIENT" ] && bad "emitter tried gh issue create with the scoped/unreachable ambient token (reachability probe did not gate ambient auth)"
[ -f "$MARKER1_AMBIENT" ] || ok "emitter never attempted issue create with the scoped/unreachable ambient token"

[ -f "$MARKER1_FILE" ] || bad "emitter did NOT fall through to the board-token FILE despite ambient auth being unreachable — card lost (#673)"
[ -f "$MARKER1_FILE" ] && ok "emitter fell through to the board-token file and created the issue via the reachable token (#673)"

if [ -f "$QUEUE1" ] && grep -q "673 scoped-token board-reachability card" "$QUEUE1"; then
  bad "card fell all the way to the dead local queue despite a reachable file token existing"
else
  ok "card did not fall to the dead local queue (file-token path was used instead)"
fi

# ── 1b. sanity: a genuinely reachable ambient token IS still trusted (no
#      regression — we don't want to always skip ambient auth now) ─────────
STUB1B="$WORK/stub1b"
mkdir -p "$STUB1B"
MARKER1B="$WORK/gh-create-ambient-ok"
cat > "$STUB1B/gh" <<FAKEGH
#!/usr/bin/env bash
case "\${1:-} \${2:-}" in
  "auth status") exit 0 ;;
  "api repos/Bubble-invest/bubble-ops-board") echo '"bubble-ops-board"'; exit 0 ;;
  "issue list") echo "" ;;
  "label create") exit 0 ;;
  "issue create")
    touch "$MARKER1B"
    echo "https://github.com/Bubble-invest/bubble-ops-board/issues/77777"
    ;;
  *) exit 0 ;;
esac
FAKEGH
chmod +x "$STUB1B/gh"

QUEUE1B="$WORK/kanban_queue1b.jsonl"
PATH="$STUB1B:$PATH" \
GH_TOKEN="ghs_reallygoodtoken" \
KANBAN_QUEUE="$QUEUE1B" \
TELEGRAM_BOT_TOKEN="" \
bash "$EMITTER" \
  task=test-673-good-ambient \
  title="673 good ambient token still works" \
  type=incident \
  owner=rnd \
  budget=10 >/dev/null 2>&1

[ -f "$MARKER1B" ] && ok "a genuinely board-reachable ambient token is still trusted (no over-correction)" \
  || bad "a genuinely reachable ambient token was rejected — reachability probe over-broad"

# ══════════════════════════════════════════════════════════════════════════
# Part 2 — drain_kanban_queue.sh: dedupe must be title-level, not task-level.
# ══════════════════════════════════════════════════════════════════════════
echo "2. title-level dedupe (same task, different titles both land; same title archived)"

FAKEBIN2="$WORK/fakebin2"
mkdir -p "$FAKEBIN2"
# Fake gh for drain: `gh issue list --search '"<title>" in:title'` returns one
# existing open issue ONLY when the search string contains "Existing card
# already on board" (case-insensitive-ish exact match simulated below).
# `issue create` always succeeds and records titles it was called with.
CREATED2="$WORK/created-titles-2.txt"
cat > "$FAKEBIN2/gh" <<FAKEGH
#!/usr/bin/env bash
if [[ "\$1 \$2" == "auth status" ]]; then
  exit 0
fi
if [[ "\$1 \$2" == "issue list" ]]; then
  args=("\$@")
  search=""
  for i in "\${!args[@]}"; do
    if [[ "\${args[\$i]}" == "--search" ]]; then
      search="\${args[\$((i+1))]}"
    fi
  done
  # Simulate ONE existing open issue titled exactly "Duplicate finding X"
  # (normalized case-insensitive). --jq extraction happens in the caller via
  # python3, so this fake just prints tab-separated number/title rows as the
  # real \`gh issue list --json number,title --jq '.[] | [...] | @tsv'\` would.
  lc_search=\$(printf '%s' "\$search" | tr '[:upper:]' '[:lower:]')
  if [[ "\$lc_search" == *"duplicate finding x"* ]]; then
    echo -e "42\tDuplicate finding X"
  fi
  exit 0
fi
if [[ "\$1 \$2" == "label create" ]]; then
  exit 0
fi
if [[ "\$1 \$2" == "issue create" ]]; then
  args=("\$@")
  title=""
  for i in "\${!args[@]}"; do
    if [[ "\${args[\$i]}" == "--title" ]]; then
      title="\${args[\$((i+1))]}"
    fi
  done
  echo "\$title" >> "$CREATED2"
  echo "https://github.com/Bubble-invest/bubble-ops-board/issues/99991"
  exit 0
fi
exit 0
FAKEGH
chmod +x "$FAKEBIN2/gh"

QUEUE2="$WORK/kanban_queue2.jsonl"
# Three queued cards, ALL with the SAME task (mirrors the live #673 incident:
# one task id, many distinct findings):
#   - Card A: title differs from anything on the board -> must land.
#   - Card B: title differs (from A and from the board) -> must ALSO land
#     (this is the regression check: task-only dedupe would have swallowed
#     this as "already exists" because task=morty-agentic-audit matched).
#   - Card C: title EXACTLY matches an existing open issue (case/whitespace
#     variation) -> must be archived as a real duplicate.
cat > "$QUEUE2" <<'EOF'
{"task":"morty-agentic-audit","severity":"kanban_only","message":"(kanban-only emit) finding A","steps":[],"kanban_items":[{"title":"Finding A: stale credential in vault","type":"incident","priority":"normal","owner":"rnd"}]}
{"task":"morty-agentic-audit","severity":"kanban_only","message":"(kanban-only emit) finding B","steps":[],"kanban_items":[{"title":"Finding B: orphaned cron on host X","type":"incident","priority":"normal","owner":"rnd"}]}
{"task":"morty-agentic-audit","severity":"kanban_only","message":"(kanban-only emit) dup","steps":[],"kanban_items":[{"title":"  DUPLICATE finding X  ","type":"incident","priority":"normal","owner":"rnd"}]}
EOF

DRAIN_OUT2=$(PATH="$FAKEBIN2:$PATH" KANBAN_QUEUE="$QUEUE2" DRAIN_DRY_RUN=0 bash "$DRAIN" 2>&1)
DRAIN_RC2=$?

[ "$DRAIN_RC2" -eq 0 ] && ok "drain exits 0 (all 3 lines resolved: 2 created, 1 archived-as-dup)" \
  || bad "drain exit was $DRAIN_RC2, expected 0. Output: $DRAIN_OUT2"

if [ -f "$CREATED2" ]; then
  grep -qF "Finding A: stale credential in vault" "$CREATED2" \
    && ok "Card A (distinct title, same task) landed on the board" \
    || bad "Card A did not land — task-level dedupe regression. Created: $(cat "$CREATED2")"
  grep -qF "Finding B: orphaned cron on host X" "$CREATED2" \
    && ok "Card B (distinct title, same task as A) ALSO landed — task alone did not swallow it (#673 fix)" \
    || bad "Card B was swallowed — this is exactly the #673 bug (task-only dedupe ate a distinct finding). Created: $(cat "$CREATED2")"
  grep -qi "duplicate finding x" "$CREATED2" \
    && bad "Card C (title matches an existing open issue) was created anyway — dedupe did not catch a real duplicate"
else
  bad "no issues were created at all — expected 2 (Card A, Card B)"
fi

echo "$DRAIN_OUT2" | grep -qi "already exists with matching title" \
  && ok "Card C was logged as archived via title-dedupe" \
  || bad "Card C's title-dedupe archive log line not found. Output: $DRAIN_OUT2"

# ── Card F (r15 review): INTERNAL-whitespace duplicate ───────────────────────
# The board holds "Fleet  audit:   gh   RCE  pending" (irregular internal ws).
# A queued card titled "Fleet audit: gh RCE pending" (normal ws) is the SAME
# card and must be archived, not double-created. This is the raw-$TITLE-search
# bug: pre-fix, the gh-side search used the unnormalized queue title and could
# miss the stored variant.
QUEUE3="$WORK/queue3.jsonl"
CREATED3="$WORK/created3.txt"
FAKEBIN3="$WORK/fakebin3"; mkdir -p "$FAKEBIN3"
cat > "$FAKEBIN3/gh" <<GHEOF
#!/usr/bin/env bash
case "\$1 \$2" in
  "auth status") exit 0 ;;
  "api repos/Bubble-invest/bubble-ops-board") echo bubble-ops-board; exit 0 ;;
  "api "*) exit 0 ;;
esac
if [ "\$1" = "issue" ] && [ "\$2" = "list" ]; then
  printf '77\tFleet  audit:   gh   RCE  pending\n'
  exit 0
fi
if [ "\$1" = "issue" ] && [ "\$2" = "create" ]; then
  while [ \$# -gt 0 ]; do [ "\$1" = "--title" ] && echo "\$2" >> "$CREATED3"; shift; done
  echo "https://github.com/Bubble-invest/bubble-ops-board/issues/999"
  exit 0
fi
exit 0
GHEOF
chmod +x "$FAKEBIN3/gh"
cat > "$QUEUE3" <<'EOF3'
{"task":"morty-agentic-audit","severity":"kanban_only","message":"m","steps":[],"kanban_items":[{"title":"Fleet audit: gh RCE pending","type":"incident","priority":"normal","owner":"rnd"}]}
EOF3
DRAIN_OUT3=$(PATH="$FAKEBIN3:$PATH" KANBAN_QUEUE="$QUEUE3" DRAIN_DRY_RUN=0 bash "$DRAIN" 2>&1)
if [ -f "$CREATED3" ] && grep -qi "fleet audit" "$CREATED3"; then
  bad "Card F: internal-ws duplicate was double-created (raw-title search bug). Output: $DRAIN_OUT3"
else
  echo "$DRAIN_OUT3" | grep -qi "already exists with matching title" \
    && ok "Card F: internal-whitespace duplicate correctly archived (normalized search+compare)" \
    || bad "Card F: no dedupe archive logged. Output: $DRAIN_OUT3"
fi

# ── Card G (r15 review): drain's own resolver rejects a poisoned token ───────
# fake gh: auth status OK, board api 404 → resolver must NOT return 0 on the
# ambient path (it falls to minter/queue); we assert the poisoned env is unset
# in the resolver path by checking the drain still completes without creating
# via the poisoned ambient auth.
FAKEBIN4="$WORK/fakebin4"; mkdir -p "$FAKEBIN4"
cat > "$FAKEBIN4/gh" <<GHEOF
#!/usr/bin/env bash
case "\$1 \$2" in
  "auth status") exit 0 ;;
  "api repos/Bubble-invest/bubble-ops-board") echo "Not Found" >&2; exit 1 ;;
esac
# any issue create under poisoned ambient auth = the bug
if [ "\$1" = "issue" ]; then echo POISONED_CREATE >> "$WORK/poisoned4.txt"; exit 1; fi
exit 1
GHEOF
chmod +x "$FAKEBIN4/gh"
QUEUE4="$WORK/queue4.jsonl"
printf '{"task":"t","severity":"kanban_only","message":"m","steps":[],"kanban_items":[{"title":"G case title","type":"incident","priority":"normal","owner":"rnd"}]}\n' > "$QUEUE4"
DRAIN_OUT4=$(PATH="$FAKEBIN4:$PATH" GH_TOKEN=poisoned GITHUB_TOKEN=poisoned KANBAN_QUEUE="$QUEUE4" DRAIN_DRY_RUN=0 bash "$DRAIN" 2>&1)
grep -q "POISONED_CREATE" "$WORK/poisoned4.txt" 2>/dev/null \
  && bad "Card G: drain attempted create on the poisoned ambient path" \
  || ok "Card G: drain resolver refused the poisoned ambient token (board-reachability gate)"

DRAINED2="${QUEUE2%.jsonl}.drained"
if [ -f "$DRAINED2" ]; then
  grep -qi "DUPLICATE finding X" "$DRAINED2" \
    && ok "Card C archived to .drained (real duplicate correctly short-circuited)" \
    || bad "Card C not found in .drained archive"
else
  bad ".drained archive not created for Card C"
fi

# Live queue must now be empty/gone (all 3 lines resolved: 2 created + 1 archived).
if [ -f "$QUEUE2" ]; then
  bad "live queue still has content after all lines resolved: $(cat "$QUEUE2")"
else
  ok "live queue fully drained (no leftover lines)"
fi

# ══════════════════════════════════════════════════════════════════════════
# Part 3 — queue-line shape coverage: per-card shape AND the notify-stack
# kanban_items[] shape (a monitor-event payload with severity/message/steps
# populated, same shape _dashboard_emit's sibling callers in the notify
# stack produce) both drain correctly through the same parser.
# ══════════════════════════════════════════════════════════════════════════
echo "3. queue-line shapes: per-card AND notify-stack kanban_items[] both drain"

FAKEBIN3="$WORK/fakebin3"
mkdir -p "$FAKEBIN3"
CREATED3="$WORK/created-titles-3.txt"
cat > "$FAKEBIN3/gh" <<FAKEGH
#!/usr/bin/env bash
if [[ "\$1 \$2" == "auth status" ]]; then exit 0; fi
if [[ "\$1 \$2" == "issue list" ]]; then exit 0; fi
if [[ "\$1 \$2" == "label create" ]]; then exit 0; fi
if [[ "\$1 \$2" == "issue create" ]]; then
  args=("\$@")
  title=""
  for i in "\${!args[@]}"; do
    if [[ "\${args[\$i]}" == "--title" ]]; then
      title="\${args[\$((i+1))]}"
    fi
  done
  echo "\$title" >> "$CREATED3"
  echo "https://github.com/Bubble-invest/bubble-ops-board/issues/99992"
  exit 0
fi
exit 0
FAKEGH
chmod +x "$FAKEBIN3/gh"

QUEUE3="$WORK/kanban_queue3.jsonl"
# Line 1: the plain per-card shape written by _dashboard_emit (minimal
# severity/message/steps).
# Line 2: the notify-stack shape — a real monitor-event payload where
# kanban_items[] rides alongside a populated severity/message/steps (a cron
# health alert that ALSO wants a kanban card), exactly what a notify-stack
# caller (not emit_kanban_item.sh) would queue.
cat > "$QUEUE3" <<'EOF'
{"task":"shape-test-percard","severity":"kanban_only","message":"(kanban-only emit) per-card shape","steps":[],"kanban_items":[{"title":"Per-card shape queued item","type":"incident","priority":"normal","owner":"rnd","budget":15}]}
{"task":"shape-test-notifystack","severity":"warning","message":"cron heartbeat missed 3x on host vps-2","steps":["checked systemctl status","checked journalctl -u bubble-loop"],"kanban_items":[{"title":"Notify-stack shape queued item","type":"incident","priority":"high","owner":"ben","budget":20}]}
EOF

DRAIN_OUT3=$(PATH="$FAKEBIN3:$PATH" KANBAN_QUEUE="$QUEUE3" DRAIN_DRY_RUN=0 bash "$DRAIN" 2>&1)
DRAIN_RC3=$?

[ "$DRAIN_RC3" -eq 0 ] && ok "drain exits 0 for both queue-line shapes" \
  || bad "drain exit was $DRAIN_RC3, expected 0. Output: $DRAIN_OUT3"

if [ -f "$CREATED3" ]; then
  grep -qF "Per-card shape queued item" "$CREATED3" \
    && ok "per-card shape line drained correctly" \
    || bad "per-card shape line was not drained. Created: $(cat "$CREATED3")"
  grep -qF "Notify-stack shape queued item" "$CREATED3" \
    && ok "notify-stack kanban_items[] shape (with severity/message/steps populated) drained correctly" \
    || bad "notify-stack shape line was not drained. Created: $(cat "$CREATED3")"
else
  bad "no issues created for shape coverage test"
fi

echo ""
echo "== RESULT: $PASS passed, $FAIL failed =="
[[ "$FAIL" == "0" ]] && exit 0 || exit 1
