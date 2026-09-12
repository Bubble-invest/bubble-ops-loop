---
name: cloud-wiki-compile
description: VPS-side single always-on compiler for the shared wiki. Three modes — compile (nightly transcript mining), synthesis (weekly thesis), pruning (weekly TTL/cap maintenance). Mines 6 VPS agents + both Mac caches.
allowed-tools:
- Bash
- Read
- Skill
- Task
- WebFetch
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

## TRANSCRIPT SOURCES (Claude Code + Hermes)

You mine transcripts from three origins, all under `/home/claude/.claude/projects/`:

1. **VPS-native agents** — `_vps-<slug>/-srv-agents-<slug>/*.jsonl`. Post-#1120 the
   depts run isolated as `agent-<slug>` (HOME `/home/agent-<slug>`, 0750), so their
   live transcripts are UNREADABLE by this claude-run compile. The
   `wiki-transcript-sync.timer` (root, every 15 min) mirrors each dept's tree into
   the claude-readable `_vps-<slug>/` cache. **The old `-home-claude-agents-<dir>/`
   dirs are FROZEN at the #1120 cutover (~Sep 5) — historical only, do NOT mine
   them for current activity.**
   **Hermes conversations** are included separately in
   `_vps-<slug>-hermes/{default,<slug>}/*.jsonl` by the same root timer (#1181).
   Mine BOTH caches regardless of the dept's current harness: switching harness
   must not hide either history. Hermes-only depts need no Claude source dir.
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
| `tony_ceo`        | `_vps-tony/-srv-agents-tony` + `_vps-tony-hermes/` (post-#1120; legacy `-home-claude-agents-bubble-ops-tony` frozen ~Sep 5) | *(none — see `tonio_extrnd` below)*                              | *(none)*                           |
| `tonio_extrnd`    | *(none — Tonio is Mac-only, not a VPS-native agent)* | `_mac-joris/-Users-joris-claude-workspaces-Tony-CEO`  | *(none — Tonio runs on Joris's Mac only)* |
| `maya_sales`      | `_vps-maya/-srv-agents-maya` + `_vps-maya-hermes/` (post-#1120; legacy frozen ~Sep 5) | *(none — Maya is VPS-only; Joris-Mac copy is a dead fossil, newest jsonl 2026-06-28)* | *(none — never existed; phantom ref)* |
| `claudette`       | `_vps-claudette/-srv-agents-claudette` + `_vps-claudette-hermes/` (post-#1120; legacy frozen ~Sep 5) | *(none — Claudette is VPS-only; Joris-Mac copy is a dead fossil, newest jsonl 2026-06-19)* | *(none — never existed; phantom ref)* |
| `morty`           | `_vps-morty/-srv-agents-morty` + `_vps-morty-hermes/` (post-#1120; legacy frozen ~Sep 5) | *(none — VPS-only concierge)*                                   | *(none)*                           |
| `rick_rnd`        | *(none — Lab runs on the Mac)*            | `_mac-joris/-Users-joris-claude-workspaces-Rick-RnD`<br>`_mac-joris/-Users-joris-claude-workspaces-Rick-RnD-prototypes-deepseek-session` | `_mac-jade/...-Rick-RnD`           |
| `ben_fund`        | `_vps-ben/-srv-agents-ben` + `_vps-ben-hermes/` (post-#1120; legacy frozen ~Sep 5) | *(none — Ben is VPS-only; Joris-Mac copy is a dead fossil, newest jsonl 2026-06-07)* | *(none — never existed; phantom ref)* |
| `miranda_socials` | *(none)*                                  | *(none — moved to Jade Mac M1)*                                  | `_mac-jade/...-bubble-ops-content` |
| `ellie_assistant` | *(none — Jade's assistant, Jade-Mac only)* | *(none)*                                                       | `_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-ellie` |
| `geraldine_accounting` | *(none — moved to Jade Mac M5, 2026-07-02)* | *(none)*                                                    | `_mac-jade/-Users-jade-thi-viet-lanhoang-claude-workspaces-bubble-ops-accountant` |

### Hermes format and normalization (#1181)

Verified on the VPS (2026-09-08): named dept profiles live at
`/home/agent-<slug>/.hermes/profiles/<slug>/state.db`; the default profile may
also have `/home/agent-<slug>/.hermes/state.db`. Dept attribution comes from
that isolated home + same-slug profile, not `cwd` (real sessions include
`/root` and null cwd). Custom HERMES_HOME paths or differently named profiles
need an explicit sync mapping; do not scan other homes/profiles by guesswork.

The store is **SQLite**, with `sessions.id` and `messages.session_id`;
`messages` has `id`, `role`, `content` (text or JSON-encoded content blocks),
and `timestamp` (Unix seconds, REAL). `sessions/*.json` can be request/error
dumps, not conversation transcripts — never mine those or profile configs.
The root-installed `/usr/local/bin/wiki-hermes-export.py` normalizes only
user/assistant text into the existing Claude JSONL shape:

```json
{"type":"assistant","timestamp":"2026-09-06T15:04:52.149586Z","sessionId":"<redacted>","uuid":"hermes:<redacted>:<message-id>","message":{"role":"assistant","content":"<redacted text>"}}
```

Each filename is a hash of the original session ID, under its source profile
subdirectory. No system/tool rows, tool arguments/results, reasoning fields,
request dumps, or auth/config files are exported. Conversation text itself may
still contain sensitive information: apply the usual wiki curation rules.
Text blocks are retained; image/non-text blocks are discarded. The compile
reads only this cache using the SAME parser below, never isolated homes or
SQLite directly. Both nightly extraction and weekly skill-gap mining must
include the Hermes source paths. Imported conversations may overlap Claude
history: merge duplicate knowledge and do not count imported copies as distinct
recurrence evidence without confirming they are separate conversations.

The exporter copies DB + WAL bytes into a private temporary directory, retries
if either changes during copying, and parses only that copy. It never opens the
live source with SQLite or changes home permissions. A missing store is normal
for a dept that has never run Hermes; an unstable/invalid store fails the sync
and retains its previous export. Output mtimes reflect the latest exported
turn, NOT the timer tick, so the 30-hour filter remains meaningful.

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

## STEP 2 — Freshness sanity on all caches (log-only)

```bash
for c in _mac-joris _mac-jade _vps-tony _vps-maya _vps-ben _vps-claudette _vps-morty _vps-tony-hermes _vps-maya-hermes _vps-ben-hermes _vps-claudette-hermes _vps-morty-hermes; do
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
    echo "$c: ABSENT (Mac asleep or unused harness = normal; unexpectedly missing VPS history = check wiki-transcript-sync.timer)"
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
       find "$SD" -type f -name '*.jsonl' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-
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

7. RESEARCH_SEED (optional, zero or more) — if the reading surfaces something
   that would be valuable INPUT for {WIKI_FOLDER}'s NEXT research / L2 mission
   (an open question left hanging, a lead worth investigating, an assumption
   that went stale, an explicit "we should look into X later"), return:
     RESEARCH_SEED: <one-line lead for {WIKI_FOLDER}'s next research tick>
   These are LEADS for FUTURE research — NOT knowledge facts (those are the
   structured entries above) and NOT skill gaps (that's STEP 4.6). Omit if none.

Return ONLY structured entries + HOT_MD_CONTENT + HOT_MD_DATE + any
RESEARCH_SEED lines. No preamble.
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

## STEP 4.7 — Claimed-vs-done / follow-through accountability detector (NIGHTLY)

Runs on EVERY nightly compile (fresh completion claims are cheapest to verify
same-day — board #1225). This is a **THIRD extractor reading the SAME
reduced-text feed** STEP 4 already computed — piggyback, not a new scan. Do NOT
re-`find`/re-parse the transcripts; reuse STEP 4's per-folder reduced text (or,
if those subagents already freed their context, re-run only STEP 4's read/parse
commands — never add a new discovery mechanism, and never grep/regex the
transcripts for keywords — see RULE below).

**Its distinct job — FOLLOW-THROUGH / truthfulness of completion claims.** The
system now reads the one transcript pass through four non-overlapping lenses:

- **STEP 4 = KNOWLEDGE** (facts/decisions → wiki)
- **STEP 4.6 = KNOW-HOW gaps** (a capability was MISSING → skill candidates)
- **STEP 4.7 = FOLLOW-THROUGH** (a capability/task was CLAIMED done but the
  evidence is absent → verification cards)
- **STEP 4.8 = INTENT ALIGNMENT** (below) · **STEP 4.9 = DOC COMPLIANCE** (below)

4.7 surfaces things an agent SAID it did / fixed / deployed / promised where the
transcript evidence does not show it actually happened. (Live-relevant: agents in
this fleet have over-claimed — "#1179 called done-then-not", the "4 NAVs aligned"
misses.) This is DISTINCT from 4.6 (missing tooling) and from 4.8 (operator
intent, not the agent's own claim).

**Agentic reading judgment, NOT a keyword miner.** A grep for "done" / "I did" /
"deployed" is ~all false positives — only a reading agent can tell a real
concrete claim ("pushed PR #N", "deployed X", "installed the drop-in") from
narration or an unstarted plan. Do NOT write, or ask a subagent to write, a
regex/keyword pass here.

Spawn **ONE Task subagent, model sonnet** (fleet-wide accountability judgment,
same tier justification as STEP 4/4.6). Give it the full set of reduced-text
slices across ALL wiki folders.

### Claimed-vs-done extractor prompt template:

```
You are a follow-through / claimed-but-not-done auditor reading real agent
session transcripts (the same reduced user/assistant text turns already parsed
for wiki knowledge extraction this run). Your job is READING JUDGMENT, not
keyword matching — surface CONCRETE completion claims an agent made whose own
transcript gives NO corroborating evidence they actually happened.

TRANSCRIPT_SLICES = {full set of reduced text turns from ALL wiki folders this run}

## What counts as a candidate (read for MEANING)
A HIGH-signal candidate is ONE of:
- a NAMED deliverable claimed DONE (a PR/commit pushed, a file written, a
  service deployed/restarted, a config/drop-in installed, a card closed, a
  number "aligned/fixed") where nothing in the same OR adjacent turns shows the
  action actually executed (no tool call, no output, no confirmation); OR
- an explicit PROMISED follow-up ("I'll do X next", "next I will deploy Y")
  that never recurs anywhere later in the feed.

## EXCLUDE (not candidates even though they sound like claims)
1. Claims WITH corroborating evidence in the same/adjacent turns (a shown
   command + its output, a PR URL, a confirming read-back). The evidence being
   present is exactly what you check for — if it's there, DROP it.
2. Routine narration of trivial reads ("I read the file", "I checked status").
3. Plans/proposals explicitly framed as NOT-yet-done ("the plan is to…",
   "we should…") — an unstarted plan is not an over-claim.
4. Claims about work done OUTSIDE this transcript window (a reference to an
   earlier day's shipped work) — you cannot see that evidence, so do NOT flag it.

## Evidence-quote rule (mandatory, no exceptions)
Every candidate MUST carry the VERBATIM claim quote copied exactly from the
transcript — the sentence where the agent asserted completion. No quote = not a
candidate.

## De-dup against the board — BOTH open AND closed
A claim already carded/verified must not be re-surfaced. For each candidate run:
  gh issue list --repo Bubble-invest/bubble-ops-board --state open \
    --search "<distinctive terms>" --limit 20
  gh issue list --repo Bubble-invest/bubble-ops-board --state closed \
    --search "<distinctive terms>" --limit 20
Use the deliverable's distinctive terms (PR#, file path, service name, card #).
If either returns a card clearly covering the same claim, DROP it. If `gh` is
unavailable, say so explicitly rather than silently skipping de-dup.

## Output — RETURN to the parent (do NOT create cards yourself)
For each surviving candidate, one block:

### <agent slug> — <short deliverable>
CLAIM: "<verbatim quote>" (<session file basename>, <date>)
WHY_UNBACKED: <1-2 sentences — what corroboration is absent>
SUGGESTED_CARD_TITLE: verify: <agent> claimed <deliverable> done <date> — confirm it shipped
DEPT: <the dept/owner slug for this folder — rnd|ben|maya|tony|content|security|accountant|morty|claudette>
BOARD_CHECK: open+closed terms used: "<terms>" — no match

Also RETURN one compact summary line:
"claimed_vs_done: N verification candidates, M dropped as already-carded, K
dropped as evidence-backed/narration".
If zero survive, return ONLY that line with N=0. Do NOT pad.
```

Then the **PARENT** emits ONE verification card per surviving candidate — card
creation (a board mutation) stays in the parent, exactly as STEP 4.6 keeps its
file-write in the parent:

```bash
EMIT=/home/claude/bubble-ops-loop/tools/kanban/emit_kanban_item.sh
# for each returned candidate block:
"$EMIT" task=wiki-claim-audit \
  title="<SUGGESTED_CARD_TITLE>" \
  body="<CLAIM quote + WHY_UNBACKED + session ref>" \
  type=findings owner="<DEPT>" priority=normal budget=2
```

(Omit `host=` — let the owner map set the dept's natural host label: rnd/content/
claudette default `host:local`, ben/maya/tony/accountant/morty default `host:vps`.
Hardcoding `host=vps` would mislabel a Mac-resident dept's card.)

`emit_kanban_item.sh` dedups on task+title for OPEN cards, so a claim that
re-appears in tomorrow's overlapping 30h window collapses to the same card (the
subagent's open+closed board search covers the already-CLOSED case).
`type=findings` → `type:research` + `status:triage`, so a human/agent triages the
verification cheaply. **These cards are the durable output — they are emitted
even on a wiki-quiet night** (independent of the STEP 10 Telegram report).

Note the one-line summary in your STEP 10 report.

**Cadence knob:** nightly by default (#1225 — fresh claims are cheapest to verify
same-day). Joris may make it Sunday-only by adding the same Sunday-guard STEP 4.6
uses; leave nightly unless he says otherwise.

## STEP 4.8 — Intent-drift (map-vs-territory) detector (WEEKLY, Sunday)

**Only runs when today (UTC) is Sunday** (same reasoning as STEP 4.6 — intent
drift is a slow signal that accrues over a week; a daily re-read adds noise, not
signal). Piggyback the SAME reduced-text feed (reuse STEP 4's slices; if freed,
re-run ONLY STEP 4's read/parse commands — no new scan, no keyword pass).

**Its distinct job — INTENT ALIGNMENT (map vs territory).** STEP 4.7 checks
whether an agent did what IT claimed; THIS checks whether the fleet built/planned
what **Joris (the operator)** actually asked for and intended. Different subject
(operator intent vs agent self-claim), so it never double-reports with 4.7. It
(a) infers the operator intents visible in the feed, (b) compares them to what
depts actually built / are building, (c) surfaces DRIFT where execution diverged
from intent, (d) can ASK Joris to CLARIFY an ambiguous intent (a `needs:human`
card) rather than guessing, and (e) proposes changes to a dept's mission/mandate
file where the drift is really a stale mandate.

It writes into the **operator-intents wiki collection** (`shared/operator-intents/`)
— a durable, browsable record where inferred AND Joris-confirmed intents
accumulate over time (see "OPERATOR-INTENTS WIKI COLLECTION" below).

**Agentic judgment, not keyword.** What Joris "intended" is never a keyword — it
is read from what he asked, corrected, praised, or rejected across the feed.
**Never auto-EDIT a live dept mission/mandate file** — those are push-guarded
live-agent files; a mandate change is a PROPOSED card for Joris/Rick to apply.

Spawn **ONE Task subagent, model sonnet**. Give it the full reduced feed across
ALL folders (intent is cross-cutting) PLUS the current operator-intents
collection so it UPDATES rather than duplicates — the parent `cat`s
`shared/operator-intents/*.md` into the prompt (or passes "EMPTY (first run)").

### Intent-drift extractor prompt template:

```
You are a map-vs-territory / intent-alignment auditor. You read real agent
session transcripts (the same reduced turns already parsed this run) and judge
whether what the fleet BUILT or is BUILDING matches what JORIS (the operator)
actually asked for and intended. This is READING JUDGMENT — operator intent is
inferred from what Joris asked, corrected, approved, or rejected, never from a
keyword.

TRANSCRIPT_SLICES = {full reduced turns from ALL wiki folders this run}
EXISTING_OPERATOR_INTENTS = {current contents of shared/operator-intents/*.md,
                             or "EMPTY (first run)"}

## Produce THREE kinds of output

### A. OPERATOR_INTENTS (to accumulate in the wiki collection)
For each distinct, DURABLE operator intent you can read from the feed — a thing
Joris wants the fleet/a dept to be or do (a goal, a constraint, a priority, a
"stop doing X / always do Y"). One block each:
INTENT_ID: <stable-kebab-slug>   (REUSE the existing slug if updating one)
DEPT: <wiki folder / dept slug the intent is about, or "fleet">
STATUS: inferred | confirmed
  - confirmed ONLY if Joris explicitly stated/clarified it in THIS feed (e.g.
    answered a prior clarify-question, or stated it directly). Else inferred.
INTENT: <1-2 sentence statement of what Joris wants>
EVIDENCE: "<verbatim quote from the feed>" (<agent/session, date>)
FIRST_SEEN: <date>   LAST_SEEN: <date>
Return an intent as UPDATE (reusing its slug) only when you have NEW evidence — a
re-statement, a refinement, or a confirmation that flips inferred → confirmed.

### B. DRIFT findings (map vs territory)
Where execution diverged from an intent (a dept built something else, kept doing
a thing Joris said to stop, or a mission/mandate file now contradicts a confirmed
intent). One block each:
DRIFT: <what was intended vs what was built/done>
INTENT_REF: <INTENT_ID>
EVIDENCE: "<verbatim intent quote>" vs "<verbatim divergence quote>"
PROPOSED: <a concrete fix — e.g. "update dept X MANDATE.md: …", or the card>
SEVERITY: low | medium | high

### C. CLARIFY questions (needs:human)
Where an intent is genuinely AMBIGUOUS and you must NOT guess — a specific
question only Joris can answer. One block each:
QUESTION: <one precise question for Joris>
WHY: <why it's ambiguous — the two competing readings>
INTENT_REF: <INTENT_ID or "new">
DEPT: <dept slug the question is about, or rnd>

## Rules
- Evidence-quote mandatory for every intent, drift, and clarify item (verbatim).
- De-dup drift/clarify against the board (open+closed) as the other extractors
  do; drop already-carded. Say so if `gh` is unavailable.
- Do NOT invent intents from thin narration — a durable intent needs a real
  operator signal (Joris asked/corrected/approved), not an agent's own idea.
- NEVER propose silently auto-editing a live mission/mandate file — a mandate
  change is a PROPOSED card for Joris/Rick to apply.

RETURN all A/B/C blocks + one compact summary line:
"intent_drift: I intents (Xc/Yi), D drift findings, C clarify-questions, M
dropped as already-carded".
```

Then the **PARENT**:

1. Writes/updates the operator-intents collection from the returned **A** blocks
   (see recipe below) — parent-written so it is robust to the STEP 4.5 quiet-gate
   (it does not depend on the synthesis subagent running).
2. Emits ONE card per **B** DRIFT finding:
   `"$EMIT" task=wiki-intent-drift title="drift: <…>" body="<intent vs built + PROPOSED>" type=findings owner="<DEPT>" priority=normal budget=2`
   (for a proposed mandate change, put the exact proposed edit in the body).
3. Emits ONE `needs:human` card per **C** CLARIFY question:
   `"$EMIT" task=wiki-intent-clarify title="clarify: <…>" body="<QUESTION + the two readings>" type=decision owner="<DEPT>" priority=normal budget=2`
   (`type=decision` → `needs:human` routing — this is the "ask Joris to clarify"
   path; the specific question rides in the card body/comment).

Card mutations stay in the parent (like 4.6/4.7). Note the summary line in STEP 10.

### OPERATOR-INTENTS WIKI COLLECTION (`shared/operator-intents/`)

A durable, consultable record of what Joris wants — inferred by STEP 4.8 and
marked `confirmed` when Joris clarifies. **One page per dept (+ a `fleet` page)**
so it stays capped by dept and `wiki_search` finds it by dept. The parent writes
it (Edit/Write inside the wiki — allowed; the HARD RULE only forbids `git push`).
It materializes on the first Sunday compile run.

On first run, create the README index (idempotent):

```bash
WIKI=/home/claude/.claude/agent-memory/shared-wiki
TODAY=$(date -u +%Y-%m-%d)
DIR="$WIKI/shared/operator-intents"; mkdir -p "$DIR"
if [ ! -f "$DIR/README.md" ]; then
  cat > "$DIR/README.md" <<MD
---
title: Operator Intents — what Joris wants (inferred + confirmed)
type: operational
owner: cloud-wiki-compile
last_verified: ${TODAY}
tags: [operator-intents, alignment, map-vs-territory]
---

# Operator Intents

A durable, consultable record of operator (Joris) intent, accumulated weekly by
the wiki-compile intent-drift pass (STEP 4.8). Each dept page lists intents with
\`status: inferred\` (read from transcripts) or \`status: confirmed\` (Joris
stated or clarified it directly). Consult this before building — it is the map
the fleet's territory is measured against.

One page per dept: \`shared/operator-intents/<dept>.md\` (+ \`fleet.md\`).
MD
fi
```

For each returned **INTENT** block, append/update the dept page
`shared/operator-intents/<DEPT>.md` (Read it first; create with frontmatter —
`title`, `type: operational`, `owner: cloud-wiki-compile`, `last_verified`,
`tags: [operator-intents, <dept>]` — if absent). Each intent is a section keyed
by `INTENT_ID`, so an UPDATE rewrites that section in place and a confirmation
flips its status. **Never delete an intent — append-or-update only:**

```
## <INTENT_ID>
- **status:** inferred | confirmed
- **intent:** <statement>
- **evidence:** "<verbatim quote>" — <agent/session, date>
- **first_seen:** <date> · **last_seen:** <date>
```

A `status: inferred` entry becomes `confirmed` when a later run returns it with
STATUS=confirmed (Joris answered the clarify card, or stated it directly). STEP 9
adds `operator-intents` to the index so `wiki_search` surfaces it.

## STEP 4.9 — Compliance-drift-to-Anthropic-docs detector (WEEKLY, Sunday)

**Only runs on Sunday.** Piggyback the same reduced feed for the "what we
actually do" side; fetch the "what is current best practice" side from the live
Anthropic / Claude Code docs.

**Its distinct job — EXTERNAL best-practice drift.** STEP 4.8 measures drift from
the OPERATOR's intent (internal reference frame); THIS measures drift from
**Anthropic's documented guidance** (external reference frame) — skills,
subagents, hooks, memory, model policy, MCP, etc. Different reference frame, so it
never double-reports with 4.7/4.8.

**Boundary with STEP 4.6 (both Sunday, both write to `skill-updates/`).** 4.6 is
INTERNAL-friction signal — *our own transcripts* show a capability was missing and
got hand-rolled → propose a skill. 4.9 is EXTERNAL-doc signal — *the docs* show a
current/better/supported way we aren't using (whether or not anyone hand-rolled a
workaround). They can legitimately fire on the SAME incident (a hand-rolled
workaround that a documented feature would remove). To avoid two files reporting
the same fix the same week, 4.9 DEFERS to 4.6: it reads this week's 4.6
`candidates.md` (if already written this run — 4.6 runs first) and DROPS any
finding 4.6 already captured, noting "already in 4.6 candidates.md" in its report
so Rick cross-checks the two files together. 4.9 keeps only findings 4.6 did not
(and would not) surface — i.e. drift the transcripts alone can't reveal because it
requires reading the docs.

**Agentic judgment:** reading current docs and deciding whether our practice
meaningfully diverges is reasoning, not a diff. Do not reduce it to keyword
matching.

Spawn **ONE Task subagent, model sonnet, WITH WebFetch**. Give it the reduced
feed (our practice) and have it fetch the current docs.

### Compliance-drift extractor prompt template:

```
You audit whether the fleet's PRACTICE has drifted from CURRENT Anthropic /
Claude Code documentation & best practices. Reading judgment, not a keyword diff.

OUR_PRACTICE = {reduced turns from ALL wiki folders this run — how agents
actually use skills/subagents/hooks/memory/models/MCP} PLUS, where useful, the
live config you can read on this box (~/.claude/skills, settings.json, agent
.md files).
DOCS = fetch the current pages with WebFetch, e.g.:
  https://docs.anthropic.com/en/docs/claude-code   (+ its skills / subagents /
  hooks / memory / settings subpages)
  https://code.claude.com/docs
Fetch ONLY what you need to substantiate a specific finding. If WebFetch fails,
say so and report only findings you can ground without it — do NOT invent doc
claims.
SKILL_GAP_FILE = this week's STEP 4.6 report, i.e.
  /home/claude/monitoring/skill-updates/{YEAR}-W{WK}-workaround-candidates.md
  (read it if it exists — 4.6 runs before you this run; treat "absent" as empty).

## What counts as a finding
A concrete, CURRENT documented recommendation OR capability that our practice
contradicts or misses in a way that MATTERS: a deprecated pattern we still use,
a supported feature that would remove a hand-rolled workaround, a config that
diverges from documented guidance. NOT stylistic nitpicks; NOT things that are
deliberate local policy (e.g. our model-pinning doctrine, our push-guard).

## Rules
- Every finding cites (a) a VERBATIM practice quote OR a config path, AND (b) the
  specific doc URL + what it says. No doc citation = not a finding.
- De-dup against the board (open+closed); drop already-carded.
- DEFER to STEP 4.6: if a finding is already captured in SKILL_GAP_FILE (same
  underlying workaround/gap), DROP it and note "already in 4.6 candidates.md".
  Keep only doc-driven findings 4.6 did not (and could not) surface from the
  transcripts alone.
- Rank by impact; do not pad.

RETURN a markdown report, one block per finding:

### <short title> — <axis: skills|subagents|hooks|memory|models|mcp|other>
PRACTICE: "<quote or config path>"
DOC: <url> — "<what current docs say>"
DRIFT: <how we diverge + why it matters>
PROPOSED: <the change>

Plus one compact summary line:
"compliance_drift: F findings, M dropped as already-carded (report-only)".
```

Then the **PARENT** writes the report — **report-only**, Rick triages weekly
(matching STEP 4.6's conservative "no auto-card, Rick cards the real ones" idiom,
because doc-interpretation is fuzzy and auto-carding it would be noisy):

```
/home/claude/monitoring/skill-updates/{YEAR}-W{WK}-compliance-drift.md
```

(`{WK}` = ISO week from `date -u +%G-W%V`, same convention as STEP 4.6's
`candidates.md`.) It is NOT the wiki and NOT a board card. Note the summary line
in STEP 10.

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

(Note: STEPS 4.6–4.9 run BEFORE this gate and are INDEPENDENT of it — the
wiki-knowledge quiet-gate only governs the knowledge/synthesis path. A quiet
wiki night can still have: 4.7 verification cards emitted (nightly), and — on
Sunday — a non-empty 4.6 `candidates.md`, 4.8 operator-intent writes +
drift/clarify cards, and a 4.9 `compliance-drift.md`. Their outputs (board
cards, the operator-intents collection, the report files) are the durable
signal; the STEP 10 Telegram message stays silent on a quiet night regardless.)

Otherwise proceed to STEP 5.

## STEP 5 — Synthesis subagent (single, isolated context)

Spawn ONE Task subagent **using model sonnet** (the rules below are batched
Edit/Write — group by page, integrate entries, write hot.md with fixed
frontmatter — but it's writing into the fleet's shared memory, so it runs on
Sonnet rather than Haiku to keep the write quality at the same tier as the
nightly extraction. The deep weekly thesis is a separate Opus pass — see
SYNTHESIS MODE. Joris 2026-06-19). Pass it all structured entries + per-agent caps +
HOT_MD_CONTENT blocks + any RESEARCH_SEED lines from STEP 4. It does the entire Edit/Write loop in its own context and
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
RESEARCH_SEEDS_BY_AGENT: {AGENT: [one-line lead, ...]} (from STEP 4's RESEARCH_SEED lines; may be empty)
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
9. RESEARCH_SEEDS_BY_AGENT: for each agent with seeds, append them to
   shared/research-seeds/{agent}.md (create with frontmatter — title,
   type: operational, owner: cloud-wiki-compile, last_verified=TODAY,
   tags: [research-seeds, {agent}] — if absent). Append as dated bullets
   ("- **TODAY** — <lead>"); NEVER rewrite prior seeds. This is the dept's
   consultable backlog of leads for its next L2/research mission. Skip agents
   with no seeds. shared/research-seeds/ is uncapped (like shared/).
10. Stop after applying — don't verify/lint. Return summary and exit.

RETURN (one compact line): "pages_updated=N pages_created=M decisions_appended=K
hot_md_written=H research_seeds_appended=S skipped_at_cap=[...] skipped_over_limit=[...]"
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
SHARED_SUBS = ['systems','decisions','concepts','people','templates','meta','archive','operator-intents','research-seeds']

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
this stays a report-only artifact, never its own alert. **Likewise append the
one-line summaries from STEP 4.7 (claimed_vs_done), and — on Sunday — STEP 4.8
(intent_drift) and STEP 4.9 (compliance_drift)** to this same message when it
fires. Do NOT raise a Telegram message on a quiet night just because 4.7 emitted
verification cards — the cards themselves are the signal; append their count only
when the message is already firing for real knowledge:

```bash
ENV_FILE=/run/claude-agent/env
BOT_TOKEN=$(awk -F= '/^TELEGRAM_BOT_TOKEN=/{print $2; exit}' "$ENV_FILE" 2>/dev/null)
JORIS_TG=6532205130
if [ -n "${BOT_TOKEN:-}" ]; then
  curl -s --max-time 10 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    -d chat_id="$JORIS_TG" \
    -d text="🧠 wiki compile $(date -u +%Y-%m-%d): <synthesis summary> · <claimed_vs_done line><, on Sunday: + skill-gap: N candidates (M dropped) · intent_drift line · compliance_drift line>" >/dev/null 2>&1
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
