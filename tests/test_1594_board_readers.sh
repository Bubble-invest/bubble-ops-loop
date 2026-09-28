#!/usr/bin/env bash
# Hermetic auth, read-only API, pagination and failure coverage for board readers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FIX=$(mktemp -d)
trap 'rm -rf "$FIX"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
mkdir -p "$FIX/bin"
export PATH="$FIX/bin:$PATH" BOARD_TOKEN_FILE="$FIX/token" BUBBLE_DEPT=tony
export READ_LOG="$FIX/read.log" GH_TOKEN=ghs_TEST_wrong GITHUB_TOKEN=ghs_TEST_wrong
cat > "$FIX/bin/gh" <<'STUB'
#!/usr/bin/env bash
set -eu
if [[ "$*" == 'auth status' ]]; then
  [[ "${AMBIENT:-}" == yes || "${AMBIENT:-}" == wrong ]]
  exit
fi
if [[ "$*" == 'api repos/Bubble-invest/bubble-ops-board --jq .name' ]]; then
  [[ "${AMBIENT:-}" == yes ]] || exit 1
  echo bubble-ops-board
  exit
fi
case "${GH_TOKEN:-}" in
  ghs_TEST_dept) echo dept >> "$READ_LOG" ;;
  ghs_TEST_shared) echo shared >> "$READ_LOG" ;;
  ghs_TEST_wrong) [[ "${AMBIENT:-}" == yes ]] || exit 1; echo ambient >> "$READ_LOG" ;;
  *) exit 1 ;;
esac
case "$*" in
  'issue list --repo Bubble-invest/bubble-ops-board --state open --label dept:tony --limit 100 --json number,title,labels,createdAt')
    echo '[{"number":1594,"title":"Reader fixture","labels":[],"createdAt":"2026-09-28T00:00:00Z"}]' ;;
  'api repos/Bubble-invest/bubble-ops-board/issues/1594')
    [[ "${API_FAIL:-}" != issue ]] || exit 1
    if [[ "${API_FAIL:-}" == malformed ]]; then echo '{'; exit; fi
    echo '{"number":1594,"title":"Reader fixture","state":"open","labels":[{"name":"dept:tony"}],"body":"Card body"}' ;;
  'api --paginate --slurp repos/Bubble-invest/bubble-ops-board/issues/1594/comments?per_page=100')
    [[ "${API_FAIL:-}" != comments ]] || exit 1
    # Three pages, deliberately out of order, and a deleted author.
    echo '[[{"id":3,"user":null,"created_at":"2026-09-28T03:00:00Z","body":"Third"}],[{"id":1,"user":{"login":"alice"},"created_at":"2026-09-28T01:00:00Z","body":"First"}],[{"id":2,"user":{"login":"bob"},"created_at":"2026-09-28T02:00:00Z","body":"Second"}]]' ;;
  *) echo 'Unexpected gh operation' >&2; exit 1 ;;
esac
STUB
for tool in sudo ssh; do
  printf '#!/usr/bin/env bash\nexit 1\n' > "$FIX/bin/$tool"
done
chmod +x "$FIX/bin/"*
run_reader() {
  local reader="$1"
  shift
  if [[ "$reader" == list_my_board_cards.sh ]]; then
    bash "$ROOT/tools/kanban/$reader" tony "$@"
  else
    bash "$ROOT/tools/kanban/$reader" 1594 "$@"
  fi
}
for reader in list_my_board_cards.sh view_board_card.sh; do
  printf 'ghs_TEST_dept\n' > "$BOARD_TOKEN_FILE.tony"
  printf 'ghs_TEST_shared\n' > "$BOARD_TOKEN_FILE"
  for ambient in no wrong yes; do
    : > "$READ_LOG"
    AMBIENT="$ambient" run_reader "$reader" > "$FIX/out" 2> "$FIX/err"
    expected=dept; [[ "$ambient" != yes ]] || expected=ambient
    if [[ ! -s "$READ_LOG" ]] || grep -qv "^$expected$" "$READ_LOG"; then fail "$reader auth precedence ($ambient)"; fi
  done
  rm "$BOARD_TOKEN_FILE.tony"
  : > "$READ_LOG"
  run_reader "$reader" > "$FIX/out" 2> "$FIX/err"
  if [[ ! -s "$READ_LOG" ]] || grep -qv '^shared$' "$READ_LOG"; then fail "$reader shared fallback"; fi
  # Empty ambient variables must not bypass the token-file fallback.
  GH_TOKEN=' ' GITHUB_TOKEN='' run_reader "$reader" > "$FIX/out" 2> "$FIX/err"
  echo "PASS: $reader ambient, per-dept and shared auth"
done
run_reader view_board_card.sh --json > "$FIX/card.json"
python3 - "$FIX/card.json" <<'PY'
import json, sys
card = json.load(open(sys.argv[1]))
assert card['title'] == 'Reader fixture' and card['body'] == 'Card body'
assert [c['id'] for c in card['comments']] == [1, 2, 3]
assert [c['body'] for c in card['comments']] == ['First', 'Second', 'Third']
PY
run_reader view_board_card.sh > "$FIX/card.txt"
python3 - "$FIX/card.txt" <<'PY'
import sys
text = open(sys.argv[1]).read()
for expected in ('#1594 Reader fixture', 'State: open', 'Labels: dept:tony',
                 'Card body', 'Comments (3):', 'alice', 'bob', '[deleted]',
                 '2026-09-28T01:00:00Z'):
    assert expected in text
assert text.index('First') < text.index('Second') < text.index('Third')
PY
echo 'PASS: paginated JSON and text include all comments in chronological order'
for failure in issue comments malformed; do
  if API_FAIL="$failure" run_reader view_board_card.sh > "$FIX/out" 2> "$FIX/err"; then
    fail "view succeeded on $failure failure"
  fi
  [[ ! -s "$FIX/out" && -s "$FIX/err" ]] || fail "$failure failure was silent or printed a partial card"
done
rm "$BOARD_TOKEN_FILE"
if run_reader view_board_card.sh > "$FIX/out" 2> "$FIX/err"; then fail 'view succeeded without auth'; fi
grep -q 'authentication unavailable' "$FIX/err" || fail 'no clear auth diagnostic'
[[ ! -s "$FIX/out" ]] || fail 'no-auth printed a card'
run_reader list_my_board_cards.sh > "$FIX/out" 2> "$FIX/err"
if bash "$ROOT/tools/kanban/view_board_card.sh" invalid > "$FIX/out" 2> "$FIX/err"; then fail 'invalid issue accepted'; fi
grep -q Usage "$FIX/err" || fail 'missing usage'
if grep -q 'ghs_' "$FIX/out" "$FIX/err" "$FIX/read.log" "$FIX/card.txt" "$FIX/card.json"; then fail 'credential leaked'; fi
echo 'PASS: auth/API failures are loud, list remains fail-open, invalid arguments rejected'
