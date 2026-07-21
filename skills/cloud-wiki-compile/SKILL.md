---
name: cloud-wiki-compile
description: VPS-side single always-on compiler for the shared wiki. Three modes — compile (nightly transcript mining), synthesis (weekly thesis), pruning (weekly TTL/cap maintenance). Mines 6 VPS agents + both Mac caches.
allowed-tools:
- Bash
- Read
- Skill
- Task
---
# Cloud Wiki Compiler — single always-on VPS compiler

You are the **single, always-on compiler** for the shared wiki. The VPS is
always on, so it owns ALL wiki compilation — the three former Mac crons
(shared-wiki-compile, wiki-weekly-synthesis, wiki-pruning) are retired. You run
as a headless `claude -p` session, silent and factual, and prefer UPDATING
existing pages over creating new ones.

The wiki is a git clone at `~/.claude/agent-memory/shared-wiki/` (= the GitHub
repo `vdk888/bubble-shared-wiki`) kept in lockstep by `cloud-wiki-sync.timer`.
**You only Edit/Write files — you do NOT git push.** The sync timer handles
push/pull every 30 min. Just leave the working tree dirty; sync commits it.

## MODE (passed in your prompt)

You run in ONE of three modes. Read the prompt to know which:

- **compile** (nightly, the main job) — mine today's transcripts → wiki pages. Do STEP 0 → 11.
- **synthesis** (weekly, Sun) — read the week's git diffs → write the synthesis meta-doc. Jump to the SYNTHESIS section at the bottom.
- **pruning** (weekly, Sun) — TTL staleness review + per-agent cap enforcement. Jump to the PRUNING section at the bottom.

Everything between here and "## SYNTHESIS MODE" is the **compile** path.

---

# COMPILE MODE

## THREE TRANSCRIPT SOURCES

You mine transcripts from three origins, all under `/home/claude/.claude/projects/`:

1. **VPS-native agents** — `-home-claude-agents-<dir>/*.jsonl` (written live on the box).
2. **Joris's Mac** — `_mac-joris/-Users-joris-claude-workspaces-<WS>/*.jsonl` (rsync'd in every 15 min by the Mac push job).
3. **Jade's Mac** — `_mac-jade/-Users-...-claude-workspaces-<WS>/*.jsonl` (same, when her Mac is on the tailnet).

A Mac cache may be **absent or stale** if that laptop was asleep — that is
NORMAL, never an error. Skip a missing source silently; never fail the compile
because a Mac is off.

## CANONICAL AGENT MAP (wiki-folder ← all source paths)

Each wiki folder is fed by one OR MORE source paths (a VPS-native session AND/OR
a Mac-cache session — an agent like Maya or Claudette runs on the box but Joris
also has a local workspace for them). Merge all sources for a given folder.

| wiki folder       | VPS-native session dir                    | Joris-Mac cache dir                                              | Jade-Mac cache dir (same WS names) |
|-------------------|-------------------------------------------|-----------------------------------------------------------------|------------------------------------|
| `tony_ceo`        | `-home-claude-agents-bubble-ops-tony`     | *(none — see `tonio_extrnd` below)*                              | *(none)*                           |
| `tonio_extrnd`    | *(none — Tonio is Mac-only, not a VPS-native agent)* | `_mac-joris/-Users-joris-claude-workspaces-Tony-CEO`  | *(none — Tonio runs on Joris's Mac only)* |
| `maya_sales`      | `-home-claude-agents-bubble-ops-maya`     | *(none — Maya is VPS-only; Joris-Mac copy is a dead fossil, newest jsonl 2026-06-28)* | *(none — never existed; phantom ref)* |
| `claudette`       | `-home-claude-agents-claudette`           | *(none — Claudette is VPS-only; Joris-Mac copy is a dead fossil, newest jsonl 2026-06-19)* | *(none — never existed; phantom ref)* |
| `morty`           | `-home-claude-agents-morty`               | *(none — VPS-only concierge)*                                   | *(none)*                           |
| `rick_rnd`        | *(none — Lab runs on the Mac)*            | `_mac-joris/-Users-joris-claude-workspaces-Rick-RnD`<br>`_mac-joris/-Users-joris-claude-workspaces-Rick-RnD-prototypes-deepseek-session` | `_mac-jade/...-Rick-RnD`           |
| `ben_fund`        | `-home-claude-agents-bubble-ops-ben`      | *(none — Ben is VPS-only; Joris-Mac copy is a dead fossil, newest jsonl 2026-06-07)* | *(none — never existed; phantom ref)* |
| `miranda_socials` | *(none)*                                  | *(none — moved to Jade Mac M1)*                                  | `_mac-jade/...-bubble-ops-content` |
| `ellie_assistant` | *(none — Jade's assistant, Jade-Mac only)* | *(none)*                                                       | `_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-ellie` |
| `geraldine_accounting` | *(none — moved to Jade Mac M5, 2026-07-02)* | *(none)*                                                    | `_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-bubble-ops-accountant` |

**Ignore** these VPS dirs entirely: `-home-claude-agents-fixture` (test),
`-home-claude-agents-ricky` (legacy/empty), `-home-claude-agents-morty-workspace-*`
(old archived paths), `-home-claude-agents-bubble-ops-cgp` (dormant test/external
dept, excluded from compile), `-home-claude-agents-bubble-ops-accountant` (dead
fossil — Geraldine migrated to Jade Mac M5 on 2026-07-02, this VPS copy is stale).
They are not live agents.

**`miranda_socials` migration note (2026-07-16):** the Miranda clean-workspace
cutover (Joris-approved, board card #657, PR `bubble-ops-content#32` merged
2026-07-16 ~14:25 UTC) made `bubble-ops-content` her SOLE workspace going
forward. `_mac-jade/...-Miranda-Socials` was archived to
`~/claude-workspaces/_archive/` on Jade's Mac at the same cutover (tag
`archive-cutover-20260716`) and is now a dead, frozen source (newest jsonl
2026-07-16 ~06:30 UTC, before the switch) — historical only, do not re-add as
a live source. `_mac-jade/...-bubble-ops-content` is the live replacement,
confirmed by transcript content (dept="content", the new L1→L4 mission-loop
dispatch logic, e.g. `draft_linkedin`/`draft_x`/`draft_substack_note`), not
just name mentions.

## STEP 1 — Read current wiki state

```bash
cat ~/.claude/agent-memory/shared-wiki/index.md 2>/dev/null | head -60
```

Understand what pages exist and per-agent counts. If the index looks stale
(old date), don't fail — STEP 9 regenerates it from disk.

## STEP 2 — Freshness sanity on the Mac caches (log-only)

```bash
for c in _mac-joris _mac-jade; do
  d=/home/claude/.claude/projects/$c
  if [ -d "$d" ]; then
    newest=$(find "$d" -name '*.jsonl' -printf '%T@\n' 2>/dev/null | sort -nr | head -1)
    if [ -n "$newest" ]; then
      age_h=$(python3 -c "import time,sys; print(round((time.time()-float('$newest'))/3600,1))")
      echo "$c: newest transcript ${age_h}h old"
    else
      echo "$c: present but no transcripts"
    fi
  else
    echo "$c: ABSENT (that Mac hasn't pushed yet — normal if asleep)"
  fi
done
```

Note stale/absent caches in your final report, but proceed regardless.

## STEP 3 — Per-folder page counts

```bash
for a in tony_ceo tonio_extrnd maya_sales claudette morty rick_rnd ben_fund miranda_socials ellie_assistant geraldine_accounting; do
  dir=~/.claude/agent-memory/shared-wiki/$a
  if [ -d "$dir" ]; then
    n=$(find "$dir" -name '*.md' ! -name 'hot.md' | wc -l | tr -d ' ')
  else
    n=0
  fi
  echo "$a: $n/30"
done
```

Folders AT 30 accept UPDATE only, no CREATE.

## STEP 4 — Spawn extraction subagents (parallel, one per wiki folder)

For EACH of the 10 wiki folders above, **spawn a Task subagent using model sonnet**
(set the subagent's model to sonnet). Deciding *what knowledge is worth keeping for
other agents* — the rationale behind a decision, a root cause, a non-obvious tool
learning — is a JUDGMENT task, not mechanical extraction: the JSON parsing is
mechanical, but choosing what matters from the parsed turns is reasoning, and this
is the fleet's shared memory where curation quality compounds. So it runs on Sonnet,
not Haiku (Joris 2026-06-19, aligning the wiki job with the fleet model doctrine:
cheap model only for truly mechanical work, the stronger model for judgment).
Send all Task calls in ONE assistant message so they run concurrently. Pass each subagent the
full list of source dirs for its folder (from the CANONICAL AGENT MAP — could be
1, 2, or 3 dirs).

### Extraction subagent prompt template:

```
You are a wiki extraction assistant for the {WIKI_FOLDER} agent.

WIKI_FOLDER = {WIKI_FOLDER}
SOURCE_DIRS = {space-separated absolute paths — the VPS-native dir and/or Mac-cache dirs for this folder}
WIKI_PATH = /home/claude/.claude/agent-memory/shared-wiki
AT_CAP = {true|false}

TODAY=$(date -u +%Y-%m-%d)
YESTERDAY=$(date -u -d 'yesterday' +%Y-%m-%d)

1. Find session files modified in the last 30 hours across ALL source dirs.
   This is Linux (GNU find) — use a TIME-BASED filter (locale-proof):

   for SD in {SOURCE_DIRS}; do
     find "$SD" -name '*.jsonl' -mmin -1800 -type f 2>/dev/null
   done | sort

   (-mmin -1800 = modified in the last 30h. Catches long sessions that
   started yesterday AND today, regardless of compile run time.)

   MANDATORY FALLBACK — if that returns empty for ALL source dirs:
     For each SD, take the single most recent file:
       ls -t "$SD"/*.jsonl 2>/dev/null | head -1
     Check its tail for ISO timestamps from $TODAY or $YESTERDAY:
       tail -50 "$LATEST" | python3 -c "
       import json,sys
       for l in sys.stdin:
           try:
               o=json.loads(l); ts=o.get('timestamp','')[:10]
               if ts in ('$TODAY','$YESTERDAY'): print('has_recent_activity'); sys.exit(0)
           except: pass
       "
     Only return NO_NEW_KNOWLEDGE if NO source dir has timestamps from
     TODAY or YESTERDAY.

2. For each session file, parse meaningful content:
   cat SESSION_FILE | python3 -c "
   import json,sys
   for l in sys.stdin:
       try:
           o=json.loads(l.strip())
           if o.get('type') in ('user','assistant'):
               m=o.get('message',{}); c=m.get('content','')
               if isinstance(c,list):
                   c=' '.join(x.get('text','') for x in c if isinstance(x,dict) and x.get('type')=='text')
               if isinstance(c,str) and len(c)>50: print(f'[{o[\"type\"]}] {c[:500]}')
       except: pass
   " 2>/dev/null

3. Extract knowledge useful to OTHER agents or Joris/Jade:
   decisions+rationale, project status changes, technical discoveries/tool
   learnings, architecture changes, failures+root causes, patterns.

4. Extract the last 5-10 notable actions for this agent's hot.md (significant
   outcomes/discoveries/decisions — not routine reads).

5. For each knowledge item return a structured entry:
   DESTINATION: {WIKI_FOLDER | shared/systems | shared/decisions | shared/concepts | shared/people}
   PAGE: suggested filename (e.g. maya_sales/foo.md or shared/systems/bar.md)
   ACTION: UPDATE (exists) or CREATE (new — ignored if AT_CAP=true for an agent folder)
   CONTENT: concise facts, reference format.

6. Return HOT_MD_CONTENT — markdown bullets, last 5-10 notable actions.
   Every bullet MUST contain at least one [[wikilink]] (Obsidian [[path/page]]
   syntax, NOT markdown links). Link to touched/created pages; fallback to
   [[{WIKI_FOLDER}/...]] or [[shared/decisions/log]].
   Format: - **YYYY-MM-DD** — <summary> — see [[namespace/page-name]]
   Also set HOT_MD_DATE = $TODAY.

Return ONLY structured entries + HOT_MD_CONTENT + HOT_MD_DATE. No preamble.
If nothing significant: "NO_NEW_KNOWLEDGE".
```

Wait for ALL extraction subagents.

## STEP 4.6 — Skill-gap miner (weekly, Sunday compile only)

**Only runs when today (UTC) is Sunday.** Any other day of the week, skip this
step entirely — jump straight to STEP 4.5. Gaps only matter once they've had a
chance to recur across a week of sessions, and running this daily would just
be a daily re-read of the same slowly-accumulating evidence for no benefit
(#103 research memo, ruling GO-SIMPLIFIED: weekly cadence, not daily).

This is a **second extractor reading the SAME reduced-text feed** STEP 4
already computed for each wiki folder — piggyback, not a new scan. Do NOT
re-`find`/re-parse the transcripts; reuse the per-folder text you already
have in context from STEP 4's subagent dispatch (or, if STEP 4 subagents
already exited and freed their context, re-run only the STEP 4 read/parse
commands — never add a new discovery mechanism, and never grep/regex the
transcripts for keywords — see RULE below).

**This step is agentic reading judgment, NOT a keyword/regex miner.** A
keyword grep over "manually"/"workaround"/"by hand" was tried and rejected
(#103 research: ~60-70% of such hits are NOT gaps — they are agents
narrating their own discipline, a verification step, or a dispatch cause,
all using the same words as a real gap). Only a reading agent can tell these
apart. Do not write, or ask a subagent to write, a regex/keyword pass here.

Spawn **ONE Task subagent, model sonnet** (judgment task, same tier
justification as STEP 4 — this is fleet-wide friction data, curation quality
matters). Give it the full set of reduced-text slices across ALL wiki
folders (not just one) — the skill-gap signal is fleet-wide, unlike the
per-folder knowledge extraction.

### Skill-gap extractor prompt template:

```
You are a skill-gap / recurring-manual-workaround extractor reading real
agent session transcripts (the same reduced user/assistant text turns
already parsed for wiki knowledge extraction this run). Your job is READING
JUDGMENT, not keyword matching — you are looking for moments where an agent
needed a capability, and either (a) no skill existed and it hand-rolled a
multi-step workaround, (b) an existing skill broke/errored and got worked
around, (c) an existing skill almost fit but needed bolted-on extra steps,
or (d) the SAME manual workaround recurs across ≥2 distinct sessions/agents.

TRANSCRIPT_SLICES = {the full set of reduced text turns from ALL wiki
folders' source dirs this run, i.e. everything STEP 4 already extracted from}

## What counts as a candidate (read for MEANING, not words)

A phrase containing "manually", "workaround", "by hand", "hand-rolled" etc.
is NOT automatically a candidate. In practice roughly two-thirds of such
phrases in real transcripts are NOT gaps. Explicitly EXCLUDE these three
patterns even though they use gap-sounding language:

1. **Dispatch-cause narration** — "Joris triggered this manually", "manually
   kicked off by the operator" — describes WHY a run happened, not a missing
   capability. Not a candidate.
2. **Verification-step narration** — "let me confirm/verify sync manually",
   "manually double-checked the output" — this is the agent doing due
   diligence, not working around a gap. Not a candidate.
3. **Discipline narration** — "I do NOT hand-roll dispatch", "I never
   manually edit X" — the agent is explicitly AVOIDING a workaround. This is
   the opposite of a gap. Not a candidate.

Only flag an ACTUAL gap: the agent needed to do something, a skill should
have covered it (or an existing skill mis-fired), and instead the agent
improvised — especially if it did so more than once.

## Ranking — weight self-diagnosis highest

Rank candidates by how the agent itself framed it, highest confidence first:
1. **Explicit self-diagnosis** — the agent said something like "that's a
   real, un-carded capability gap" or "this should be a skill" in its own
   reasoning. Near-zero false-positive rate. Rank these at the top.
2. **Error-then-fix, repeated across ≥2 DISTINCT sessions** — the same
   error followed by the same remediation, seen in more than one session.
   Count distinct sessions, not repeated occurrences within one session
   (a single session retrying 4 times is recurrence=1, not 4).
3. **Exact-command / exact-recipe fingerprint repeated across sessions** —
   but EXCLUDE anything that is normal loop-framework boilerplate (heartbeat
   checks, safe_pull, date-formatting, routine dispatch-decision commands).
   Those are the loop working as designed, not a gap.
4. **Bare keyword mention with no repetition or self-diagnosis** — lowest
   confidence. Include only if genuinely compelling; do not pad the list
   with these.

## Evidence-quote rule (mandatory, no exceptions)

Every candidate MUST carry at least one VERBATIM quote copied exactly from
the transcript text you were given — not a paraphrase, not a summary of what
the agent meant. If you cannot produce an exact quote substantiating a
candidate, DO NOT list it. No quote = not a candidate. This is the guardrail
against inventing gaps.

## De-dup against the board — BOTH open AND closed cards

Before finalizing your candidate list, check whether each candidate is
already known to the board. This is NOT optional and NOT limited to open
cards — the single loudest recurring signal in this fleet's history (the
git-push-guard recipe) was already carded FOUR times, several of them
CLOSED, and a miner that only checks open cards would re-surface it as
its #1 finding every single week forever.

Run BOTH of these before emitting your final list:

  gh issue list --repo Bubble-invest/bubble-ops-board --state open \
    --search "<keywords from your candidate>" --limit 20
  gh issue list --repo Bubble-invest/bubble-ops-board --state closed \
    --search "<keywords from your candidate>" --limit 20

For each candidate, search using its most distinctive terms (the tool/skill
name, the error string, the recipe keywords — e.g. "GITHUB_APP_ID",
"push-guard", "unsandboxed --repo-dir"). If either search returns an issue
(any state) whose title/body clearly already describes the same underlying
gap, DROP the candidate from your list entirely — do not include it even
as a low-ranked mention, and do not re-file it. Note in your one-line
summary how many candidates you dropped this way and why (so a human can
sanity-check the de-dup, not just trust it silently).

If `gh` is unavailable or the search errors, say so explicitly in your
output instead of silently skipping de-dup — do not guess whether something
is already carded.

## Output format — report-only, no side effects

Write ONLY a markdown report. Do NOT create a board card, do NOT call
emit-kanban-task, do NOT run any tool that mutates the board or the wiki.
This step's only side effect is writing one file.

For each surviving candidate, in ranked order (self-diagnosed first):

```
### <N>. <short title> — <signal class: MISSING|BROKEN|EXTEND|FRICTION>

**Evidence:** "<verbatim quote>" — <agent slug>, <session file basename>
**Why it's a gap:** <1-2 sentences, your reasoning>
**Recurrence:** seen in <N> distinct session(s)/agent(s)
**Proposed action:** new-skill | fix-existing | extend-existing — <1 line>
**Board check:** open+closed search terms used: "<terms>" — no match found
```

If you found zero candidates that survive both the FP filters and the
de-dup check, write a one-line file saying so — do not pad with weak
candidates to look productive.

RETURN (to the parent, not written to disk): one compact summary line —
"skill_gap: N candidates written, M dropped as already-carded (open+closed),
K dropped as false-positive-pattern".
```

Write its output to (create the directory + file if absent — this is a
fixed VPS-local path, independent of wherever a `bubble-ops-loop` checkout
happens to live, matching this SKILL's existing convention of hardcoded
`/home/claude/...` paths rather than a repo-relative one):

```
/home/claude/monitoring/skill-updates/{YEAR}-W{WK}-workaround-candidates.md
```

(`{WEEK}` = ISO week, e.g. `2026-W29`; compute with
`date -u +%G-W%V`.) Rick reviews this path by hand weekly (over SSH, same as
he reads any other VPS-local state). It is NOT the shared wiki (do not write
it under `shared-wiki/`) and it is NOT a board card. No auto-carding, no
confidence-bar gate, no `emit-kanban-task` call — Rick triages
`candidates.md` manually and cards the real ones himself.

Note the one-line summary in your final compile report (STEP 10).

## STEP 4.5 — Quiet-gate

If ALL spawned extractors returned `NO_NEW_KNOWLEDGE`:

1. Skip the synthesis subagent entirely.
2. Bump only `last_updated` in each existing hot.md:

```bash
TODAY=$(date -u +%Y-%m-%d)
for a in tony_ceo tonio_extrnd maya_sales claudette morty rick_rnd ben_fund miranda_socials ellie_assistant geraldine_accounting; do
  HOT=/home/claude/.claude/agent-memory/shared-wiki/$a/hot.md
  [ -f "$HOT" ] || continue
  python3 -c "
import re,pathlib
p=pathlib.Path('$HOT'); t=p.read_text()
n=re.sub(r'^last_updated:.*\$','last_updated: $TODAY',t,count=1,flags=re.M)
if n!=t: p.write_text(n); print('bumped: $a')
"
done
```

3. Jump to STEP 9 (index) → STEP 10 (report). Skip 5-8. **Stay silent on Telegram** (quiet night).

(Note: if STEP 4.6 ran because today is Sunday, its `candidates.md` was
already written before this gate — the wiki-knowledge quiet-gate and the
skill-gap miner are independent; a quiet wiki night can still have a
non-empty workaround report, and vice versa.)

Otherwise proceed to STEP 5.

## STEP 5 — Synthesis subagent (single, isolated context)

Spawn ONE Task subagent **using model sonnet** (the rules below are batched
Edit/Write — group by page, integrate entries, write hot.md with fixed
frontmatter — but it's writing into the fleet's shared memory, so it runs on
Sonnet rather than Haiku to keep the write quality at the same tier as the
nightly extraction. The deep weekly thesis is a separate Opus pass — see
SYNTHESIS MODE. Joris 2026-06-19). Pass it all structured entries + per-agent caps +
HOT_MD_CONTENT blocks. It does the entire Edit/Write loop in its own context and
returns a one-paragraph summary. (Keeps the parent's context tiny — this is the
cost-control architecture.)

### Synthesis subagent prompt template:

```
You apply structured wiki entries in ONE batched session, then return a
one-paragraph summary. Minimize tool calls.

WIKI_PATH = /home/claude/.claude/agent-memory/shared-wiki
TODAY = {YYYY-MM-DD}
PER_AGENT_CAPS = {folder: X/30, ...}
STRUCTURED_ENTRIES (one per line): {DESTINATION/PAGE/ACTION/CONTENT}
HOT_MD_CONTENT_BY_AGENT: {AGENT, bullets, HOT_MD_DATE}
DECISIONS_TO_LOG: {title+body for DESTINATION=shared/decisions}
NEW_PAGES_LIMIT = 5 (across all agents combined)

RULES:
1. Group entries by target page. One Read → one Edit/Write per page. Never
   re-Read a page you already touched.
2. UPDATE: Read once, integrate ALL its entries in one Edit, set last_verified=TODAY.
3. CREATE: skip if agent folder AT_CAP; skip if over NEW_PAGES_LIMIT; else Write
   with full frontmatter (title, domain, owner, created, last_verified, type, tags)
   + [[wikilinks]]. (geraldine_accounting/ may be created fresh — new folder.)
4. DECISIONS_TO_LOG: append to shared/decisions/log.md (never modify past entries):
   ## YYYY-MM-DD — Title
   **What:** ... **Why:** ... **Source:** {agent} session
5. Write each agent's hot.md from HOT_MD_CONTENT_BY_AGENT (ONE Write each):
   ---
   name: {agent} — Recent actions
   type: operational
   owner: {agent}
   last_updated: {HOT_MD_DATE}
   ---
   # Recent actions — {agent}
   {bullets}
6. Wikilinks always [[path/page]], never markdown links.
7. Do NOT rewrite index.md (parent regenerates it).
8. Do NOT read pages not in STRUCTURED_ENTRIES.
9. Stop after applying — don't verify/lint. Return summary and exit.

RETURN (one compact line): "pages_updated=N pages_created=M decisions_appended=K
hot_md_written=H skipped_at_cap=[...] skipped_over_limit=[...]"
```

Wait for it. Capture the summary string.

## STEP 9 — Regenerate index.md (deterministic shell — no model tokens)

Run AFTER synthesis so it sees new pages. Rebuild from filesystem state:

```bash
python3 << 'PYEOF'
import os, pathlib, re
from datetime import datetime, timezone

WIKI = pathlib.Path('/home/claude/.claude/agent-memory/shared-wiki')
TODAY = datetime.now(timezone.utc).strftime('%Y-%m-%d')
AGENTS = ['tony_ceo','tonio_extrnd','maya_sales','claudette','morty','rick_rnd','ben_fund','miranda_socials','ellie_assistant','geraldine_accounting']
SHARED_SUBS = ['systems','decisions','concepts','people','templates','meta','archive']

def list_md(d, exclude=()):
    if not d.exists(): return []
    return [p.name for p in sorted(d.glob('*.md')) if p.name not in exclude]

def title_from(p):
    try:
        for line in p.read_text().splitlines()[:20]:
            m=re.match(r'^title:\s*(.+)$',line)
            if m: return m.group(1).strip()
            if line.startswith('# '): return line[2:].strip()
    except Exception: pass
    return p.stem

lines = ['---','title: Shared Wiki — Index','type: operational','owner: cloud-wiki-compile',
         f'last_updated: {TODAY}','---','','# Shared Wiki — Index','',
         f'_Compiled on the VPS (always-on). Last: {TODAY} UTC._','']
for a in AGENTS:
    d = WIKI/a
    pages = list_md(d, exclude=('hot.md',))
    lines.append(f'## {a} ({len(pages)}/30)')
    for name in pages:
        lines.append(f'- [{title_from(d/name)}]({a}/{name})')
    lines.append('')
lines.append('## shared/')
for sub in SHARED_SUBS:
    d = WIKI/'shared'/sub
    pages = list_md(d)
    if not pages: continue
    lines.append(f'### shared/{sub} ({len(pages)})')
    for name in pages:
        lines.append(f'- [{title_from(d/name)}](shared/{sub}/{name})')
    lines.append('')
(WIKI/'index.md').write_text('\n'.join(lines)+'\n')
print('index.md regenerated')
PYEOF
```

## STEP 10 — Report (Telegram, ONLY if real knowledge was written)

**Silent on quiet nights** (quiet-gate fired) and on any run where synthesis
wrote nothing. Otherwise post ONE concise summary to Joris via the bot token in
`/run/claude-agent/env`. If STEP 4.6 ran (Sunday) and wrote a non-empty
`candidates.md`, append its one-line summary (candidate count + dropped
counts) to this same message rather than sending a second Telegram message —
this stays a report-only artifact, never its own alert:

```bash
ENV_FILE=/run/claude-agent/env
BOT_TOKEN=$(awk -F= '/^TELEGRAM_BOT_TOKEN=/{print $2; exit}' "$ENV_FILE" 2>/dev/null)
JORIS_TG=6532205130
if [ -n "${BOT_TOKEN:-}" ]; then
  curl -s --max-time 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="$JORIS_TG" \
    -d text="🧠 wiki compile $(date -u +%Y-%m-%d): <your one-line summary from the synthesis string><, on Sunday: + skill-gap: N candidates (M already-carded dropped)>" >/dev/null 2>&1
fi
unset BOT_TOKEN
```

Then exit. **Do NOT git push** — cloud-wiki-sync handles it.

---

# SYNTHESIS MODE

It's Sunday. You perform the weekly synthesis: read the week's wiki git diffs
and form a one-paragraph THESIS about what the multi-agent system actually
learned (or failed to learn) this week. You are NOT summarising a git log —
you're forming a judgment about the system's epistemic state. (Bubble principle,
Joris msg 2108: intelligence is in the agent's reading, not a regex over keywords.)

```bash
cd /home/claude/.claude/agent-memory/shared-wiki
git log --since="7 days ago" --oneline --stat | head -200
COMMITS=$(git log --since="7 days ago" --oneline | wc -l)
[ "$COMMITS" -gt 0 ] && git diff "HEAD~${COMMITS}..HEAD" -- . ':(exclude)meta/' | head -2000
```

If COMMITS is 0, write a minimal note about the quiet week and exit (silent on
Telegram). Otherwise:

1. Read the diffs. Write a 3-5 sentence honest thesis (what was actually
   learned / failed to be learned — specifics, not counts).
2. Look through ONLY the 2-3 lenses your thesis demands (activity / cross-cutting
   / failure-modes / gaps), not a fixed checklist.
3. Write the synthesis to `shared/meta/synthesis-<YYYY-MM-DD>.md` with frontmatter
   (type: operational, owner: cloud-wiki-compile). Lead with the thesis.
4. Do NOT git push. Telegram only if the thesis surfaces something Joris should
   act on (cost-of-inaction line for any CEO-attention item).

---

# PRUNING MODE

Weekly maintenance. Verify knowledge is still accurate, archive what's stale,
keep each agent folder under 30 pages. Be CONSERVATIVE — only archive if
genuinely no longer relevant.

TTL by `type` frontmatter:
- `reference` → 90 days
- `operational` → 30 days
- `decision` → never auto-prune
- `shared/decisions/log.md`, `shared/templates/*`, all `hot.md` → never prune

```bash
WIKI=/home/claude/.claude/agent-memory/shared-wiki
for a in tony_ceo tonio_extrnd maya_sales claudette morty rick_rnd ben_fund miranda_socials ellie_assistant geraldine_accounting; do
  [ -d "$WIKI/$a" ] || continue
  n=$(find "$WIKI/$a" -name '*.md' ! -name 'hot.md' | wc -l | tr -d ' ')
  echo "$a: $n/30"
done
grep -rl "last_verified:" "$WIKI" --include='*.md' --exclude-dir=archive --exclude-dir=templates
```

For each page: read frontmatter, compute staleness vs TTL. A page is stale if
`(today - last_verified) > TTL`. Then:

| Condition | Action |
|-----------|--------|
| Subject exists AND content accurate | bump last_verified to today |
| Subject exists BUT outdated | update content + last_verified |
| Subject gone (project dir / config / scheduled task removed) | move to shared/archive/ |
| Folder AT cap, page trivial+low-backlinks | archive lowest-value first |

To verify "subject exists": check the referenced project dir / config file /
scheduled task / systemd unit actually exists (on VPS or via the path in the
page). People pages: check recent session logs. Concept pages: check still
referenced by other pages' `[[links]]`.

Shared pages are NEVER pruned for capacity — only for staleness/inaccuracy.

Apply moves with `git mv` where possible (preserve history). Do NOT git push —
cloud-wiki-sync handles it. Regenerate index.md (the STEP 9 block above) after
pruning so counts are correct. Telegram only if you archived something notable.

## PRUNING STEP — private agent-memory hygiene (run the notifier)

The steps above prune the SHARED wiki. Agents ALSO keep PRIVATE memory
(`~/.claude/agent-memory/<agent>/MEMORY.md` + reference files) that nothing else
grooms. We do NOT prune it centrally — only the owning agent knows which entries
are still load-bearing, so a blind cap would delete memory it relies on. Instead
we DETECT clutter and NUDGE the owning agent to groom its own memory (it stays
"in the agent's consciousness flow"). This is mechanical detection — just run the
tool; it never edits anyone's memory:

```bash
sudo -u claude python3 /home/claude/scripts/memory_hygiene_notify.py
```

What it does (no judgment needed from you — it's deterministic):
- Scans each agent's canonical MEMORY.md (the Mac caches synced up by
  mac-transcript-sync + VPS-native memory; largest copy wins).
- For any over budget (~24KB / many over-long index lines / many dup slugs):
  - VPS-native depts (ben/maya/tony/accountant) → injects a grooming nudge
    straight into their live session.
  - Mac-resident agents (content/rnd/claudette/security) → queues the nudge in
    that Mac's outbox; the Mac's own sync run injects it locally (trust arrow is
    laptop→cloud only, so the VPS can't inject into a laptop directly).
- A per-agent 6-day cooldown stamp prevents re-nudging weekly before the agent
  has groomed. Healthy memories are silently skipped.

Just run it and note its one-line-per-agent output in your final report. Do not
edit any agent's private memory yourself.

## PRUNING STEP — skill-sync hygiene (run the checker, self-heal the easy case)

A Claude **skill** can silently go stale: the REGISTERED copy
(`~/.claude/skills/<name>/SKILL.md`, what every agent auto-loads) can drift from
its WORKSPACE SOURCE or from `origin/main`, because editing+committing a skill does
NOT auto-update the registered copy. An agent then keeps loading an OLD skill
(this bit `rnd_loop` 2026-06-19). Card #653 added a narrow **self-heal**: the
checker now fixes the unambiguous case itself (source confirmed newer + clean) and
still files a card — same as before — for everything it can't safely resolve. Run
it with `--fix`:

```bash
sudo -u claude python3 /home/claude/scripts/skill_sync_check.py --fix
```

What it does (deterministic):
- For each registered skill on each machine (VPS-native + the Mac caches synced up
  by mac-transcript-sync into `_mac-<slug>-skills/registered` + `/source`), checks
  TWO drifts: (a) registered ≠ workspace-source, (b) registered ≠ origin/main.
- Skills with NO workspace source (registry-only, e.g. `auth`) are skipped — nothing
  to compare, not flagged.
- **`--fix` heals ONE direction only (source → registered), ONLY when unambiguous**:
  workspace source is clean/committed AND confirmed newer than the registered copy
  (via git-commit recency, falling back to mtime). Every heal (applied or refused)
  is logged to `.hygiene_tidy_log.jsonl`.
- **Refuses + reports instead of clobbering** whenever the registered copy might be
  the newer/authoritative side — e.g. it was hot-edited directly in
  `~/.claude/skills/<name>/SKILL.md` and not yet copied back to source (this
  happened for real: `telegram-message-A2A` had a security-tightening live ONLY in
  the registry, with a clean committed-but-stale source — a blind source→registry
  copy would have destroyed it). Also refuses when source is dirty/uncommitted, the
  skill has no workspace source, or recency direction can't be determined. Refused
  skills still show up on the drift card exactly as before.
- For any skill still drifted after healing (refused-to-heal or genuinely
  ambiguous), files ONE kanban board card per host (owner=rnd — R&D owns the
  skill registry) listing them + the reason, so the loop triages it and a
  human/the owning agent decides the direction manually.

Note its one-line-per-host output (scan + heal lines) in your final report.

---

# HARD RULES (all modes)

- You only Edit/Write/git-mv inside the wiki. **You NEVER `git push`** — the
  30-min cloud-wiki-sync owns push/pull. Leaving the tree dirty is correct.
- A missing/stale Mac cache is normal (laptop asleep) — never fail on it.
- `shared/decisions/log.md` is append-only. `shared/templates/*` never modified.
- Stay silent on Telegram unless real knowledge was written / a real signal surfaced.
- Wikilinks are Obsidian `[[path/page]]`, never markdown `[text](url)`.
