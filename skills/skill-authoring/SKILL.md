---
name: skill-authoring
allowed-tools:
- Bash
- Read
- Write
- Skill
- Task
description: >-
  Weekly, cheap, eval-gated, anti-bloat skill authoring + usage-based pruning
  agent (#1222). The KNOW-HOW half of the fleet's transcript-mining system:
  wiki-compile detects (STEP 4.6 skill-gap candidates) and writes knowledge;
  THIS skill CONSUMES those candidates and either AUTHORS a new/extended skill
  (only if an eval WITH-vs-WITHOUT proves it helps) or, on the prune side,
  measures actual skill usage and flags genuinely-dead skills for a human to
  remove. Runs headless in the `skillsmith` mode of cloud-wiki-compile.sh, once
  a week (Sunday 23:30 UTC), on a cheap model. Do NOT re-mine transcripts —
  that is STEP 4.6's job. Everything critical or removal-related is
  needs:human, never auto-applied.
---

# skill-authoring — author + prune skills, weekly, eval-gated, anti-bloat

> **Doctrine boundary (Joris, #1222):** *wiki = knowledge, skills = know-how —
> complementary, NOT overlapping.* wiki-compile owns knowledge + skill-gap
> **detection** (STEP 4.6). This skill owns know-how **authoring + pruning**. It
> **consumes** 4.6's candidates; it does **not** re-mine transcripts.

> **Doctrine: tools-as-evidence, agent-as-judgment**
> (`shared/systems/cron-judgment-vs-tools.md`). The bundled Python collects
> *mechanical facts* (which candidates 4.6 emitted, how often each skill was
> actually invoked, embedding similarity). **YOU (the agent) make every
> judgment** — "is this a real skill candidate?", "does the draft help?", "is
> this skill genuinely dead vs rare-but-critical?". A bare count or keyword
> gate deciding any of those is the exact anti-pattern the wiki warns against
> (it would prune `auth`, `polymarket-vpn-bringup`, `dept-spawner` — low
> frequency, high criticality).

You are invoked once a week by `cloud-wiki-compile.sh skillsmith` (Sunday 23:30
UTC). The Sunday order is synthesis(18:00) → pruning(19:00) → compile+STEP-4.6
(22:00) → **you (23:30)** — deliberately AFTER the 22:00 compile so this week's
transcripts are mined and 4.6's `candidates.md` (stamped with Sunday's ISO week)
already exists, and still ON Sunday so the ISO-week stamp matches (Monday would
roll to the next week and miss the file). Cheap model (Haiku). Run BOTH arms,
then report. Be silent on a quiet week (nothing authored, nothing to prune).

Bundled evidence collectors (call them; never re-implement their logic inline):
- `scripts/lib/list_candidates.py` — locate + structure THIS week's 4.6 candidates + enumerate the skill registry (ARM A).
- `scripts/lib/skill_usage_count.py` — count actual `Skill` invocations across the centralized transcript corpus + list zero-use registered skills (ARM B).
- `scripts/lib/eval_harness.py` — optional standalone WITH/WITHOUT runner for an independently authenticated CLI; headless ARM A uses native Task subagents below.

The skill dir on the VPS is `/home/claude/.claude/skills/skill-authoring`; call
the scripts by that absolute path (or relative to the skill dir).

---

## ARM A — CREATE (consume 4.6 candidates → dedupe → eval-gate → validation-gate)

### A0. Collect the evidence (mechanical — ~0 tokens)
```
python3 /home/claude/.claude/skills/skill-authoring/scripts/lib/list_candidates.py \
    --registry /home/claude/.claude/skills
```
This returns THIS week's 4.6 candidate blocks (already agentically detected,
deduped against the board), the raw report, and the registered-skill list with
descriptions. **If `found` is false or `candidate_count` is 0 → ARM A is done,
nothing to author this week.** If the status note says it fell back to an older
week, treat those candidates with suspicion (they may already be carded/handled)
and say so in your report.

> **Why this is not re-mining:** you are reading a *report 4.6 already wrote*, not
> the transcripts. You never open a `.jsonl` in ARM A.

### A1. Judge which candidates should become know-how (AGENTIC)
Read each candidate block. Decide, per candidate, whether it is a genuine
recurring multi-step procedure that *should* be a skill (Anthropic's documented
"you keep pasting the same procedure" signal), vs. noise 4.6 let through. This
reading judgment is yours — there is no scoring formula. Drop the weak ones.

### A2. Dedupe against existing skills (mechanical similarity → your call)
For each survivor, compare against the registered skills from A0. If local
embeddings are available (`nomic-embed` via the wiki tooling), use them for
similarity; otherwise compare titles/descriptions by reading. **Overlap ⇒
propose EXTEND skill X, not a new skill** (anti-sprawl). Only clearly-novel
know-how becomes a new skill.

### A3. Draft the survivors (cheap) into staging — never live
Write each draft to a **staging dir** (`skills-proposed/<name>/SKILL.md` under a
scratch path — NEVER into `~/.claude/skills` and NEVER into a dept's live skill
dir). Follow the SKILL.md spec (frontmatter `name` + `description`, tight prose,
bundled scripts only if they exist). Reuse the `skill-creator` meta-skill if it
is registered; if not, author by hand to the spec.

### A4. Eval-first gate (the anti-bloat mechanism) — AUTHOR ONLY IF IT HELPS
For each draft, generate 2–3 self-contained probe prompts from the candidate's
own mined examples. Include synthetic inputs and ask for the proposed procedure
or output; probes must not require live credentials, network calls, or mutations.
Write the prompts to `probes.json` as `[{"id":"p1","prompt":"..."}]`.

**Headless mechanism (#1496): use the native Task tool**, as the wiki compiler
already does. Spawn two fresh `general-purpose` Task subagents with `model=haiku`
for each probe (4–6 calls per draft). Do not resume an earlier subagent or pass
parent conversation/history. Use the same prompt and fixtures for both arms:

- WITHOUT: send only the probe plus the instruction to answer from the supplied
  inputs, without tools, reading skills, or making external changes.
- WITH: send that identical message plus the complete staged SKILL.md body,
  labelled as available know-how to use if relevant.

Do not give either arm the other arm's response or the desired verdict. Save
both returned responses to `<eval_out>/<id>.without.txt` and
`<eval_out>/<id>.with.txt`, and record tool errors beside them. These are
procedure/output probes, not proof that a live integration works.

Do not launch nested `claude -p` processes from this headless session: the
standalone `eval_harness.py` needs its own CLI authentication and can fail under
the headless environment. Do not search for credentials or loosen the sandbox
to make it work. The Task calls use the running session's native subagent path.

Read the paired outputs. **Author the skill ONLY if the draft measurably
improved the task on the majority of probes** (better recipe, fewer missteps,
correct where the bare run was wrong). Tie or no improvement on completed
probes → **DISCARD the draft** and note it in the report.

If Task is unavailable, either arm errors, or a probe still requires missing
inputs/auth, mark the candidate **eval inconclusive**, never passed or
discarded-by-eval. Preserve the staged draft and evidence; a strong candidate
may be filed as a **needs:human** draft with the blocker and pending eval stated
explicitly. It cannot enter the agent:ready path until the paired eval passes.
Continue ARM B and report the inconclusive count separately. This explicit
blocked-eval disposition completes ARM A for the completion marker; an
unhandled error does not.

### A5. Validation-gate (criticality) → route
For each draft that PASSED the eval:
- **Critical** — touches auth / secrets / broker-fund / deploy, is
  behavior-changing, or **edits an existing skill** → file **needs:human** via
  `emit-kanban-task` with the full draft/diff, and send a Telegram heads-up.
  Never self-author these.
- **Non-critical, genuinely new** → file an **agent:ready PR-to-Joris** card via
  `emit-kanban-task` (owner = the inferred owning dept) carrying the staged draft
  for review. Do NOT copy it into any live skills dir yourself (maker ≠ checker,
  never auto-merge — this skill only proposes).
- Nudge the owning agent via the memory-hygiene detect→nudge routing (below).

---

## ARM B — PRUNE (usage-measured → agent-judged → human-gated; never auto-remove)

### B1. Measure usage (mechanical — ~0 tokens)
```
python3 /home/claude/.claude/skills/skill-authoring/scripts/lib/skill_usage_count.py \
    --registry /home/claude/.claude/skills --window-days 45
```
This counts `Skill` tool_use blocks across the centralized transcript corpus
(the same feed wiki-compile mines — VPS agents + both Mac caches) and lists
every registered skill, including `count: 0` (never invoked in the window). That
zero/low-use set is your prune-CANDIDATE list. (Optional cross-check of per-skill
context cost: `claude -p /skill-doctor` if available — coverage excludes bundled
+ enterprise skills, so treat it as a supplement, not the source of truth.)

> **Native telemetry, honestly:** Claude Code *does* expose usage via
> `/skill-doctor` and the `/plugin` Stats tab, but those are per-machine,
> per-session, interactive, and the auto "unused" list only covers self-installed
> marketplace plugins. Our fleet's skills are mostly standalone `~/.claude/skills`
> + workspace skills across many machines, so the durable fleet-wide signal is the
> transcript-corpus count above, not the native views.

### B2. Judge dead vs rare-but-critical (AGENTIC — the critical step)
For each low/zero-use skill, **read its SKILL.md** and judge: is it genuinely
dead (superseded, one-off, no longer wired) or rare-but-critical (a break-glass
capability like `auth`, `polymarket-vpn-bringup`, `dept-spawner` that is *meant*
to sit idle)? **A low count is EVIDENCE, never a verdict.** Keep only the ones
you judge genuinely dead.

### B3. Flag for a human — NEVER auto-remove
Removal is behavior-changing and irreversible, so it is `needs:human`. For each
skill you judge dead, file ONE `emit-kanban-task` card (type=decision,
needs:human) with the usage evidence (count, last_used, agents) + your rationale,
and send a Telegram heads-up. Nudge the owning agent. **Do not delete or move any
skill file.** (Mirrors `memory_hygiene_notify`: detect → nudge, never delete.)

---

## Filing + nudging (shared plumbing — reuse, don't reinvent)

- **Board cards:** the `emit-kanban-task` skill (its bundled emitter
  `emit_kanban_item.sh`, at `~/claude-workspaces/Rick_RnD/tools/kanban/` on the
  Mac and installed under `/home/claude/scripts/` on the VPS). Use `owner=<dept>`,
  `type=decision` for needs:human, `type=feature`/`chore` for agent:ready PRs.
  It is idempotent on the `<!-- emit-task: <task> -->` marker — use a stable
  `task=` slug per candidate (e.g. `task=skill-author-<name>-<week>`,
  `task=skill-prune-<name>`) so a re-run doesn't duplicate.
- **Nudge the owning agent:** the memory-hygiene detect→nudge routing
  (`memory_hygiene_notify.py`) — VPS depts inject locally, Mac agents via the
  outbox. Only nudge when you filed something actionable for that agent.

## Reporting rule (stay cheap + quiet — best-effort notification QUEUE, mirrors cloud-wiki-compile's STEP 10)
Post ONE Telegram line at the end **only if** you authored/proposed a skill OR
flagged a prune (i.e. real output). On a quiet week, be silent.

**You do not have the credential to send it, and that is by design** — the
bot token lives in `/run/claude-agent/env`, which this headless session's
sandbox deliberately denies you (same boundary cloud-wiki-compile's STEP 10
documents; board #1482). Do not try to read that file, source it, `awk` it, or
reach for a Telegram MCP tool (that would also risk booting a poller on the
claude-user token — Morty's bot token, #1425/#1406). **Never conclude "no
token exists" — you cannot see whether it exists; that is not your check to
make.** Your only job is to **queue the message text as a plain file**; the
launcher (`cloud-wiki-compile.sh skillsmith`) already holds this mode's
pre-filtered token outside your sandbox — the SAME `filter-headless-env.py`
ExecStartPre every `cloud-wiki-compile@` mode gets, into
`/run/bubble-headless-cloud-wiki-skillsmith/env` — and sends your queued
message with a single HTTP POST right after it verifies your completion
marker below (board #1572: this launcher-side send previously did not exist
for skillsmith at all, which is why "no fleet-wiki bot token" used to be the
only honest thing you could say — do not say that again; queue the file
instead and trust the launcher to send it):

```bash
REPORT_DIR=/home/claude/monitoring/skillsmith
REPORT_FILE="$REPORT_DIR/telegram-report.txt"
mkdir -p "$REPORT_DIR"
# Quiet week / nothing to report: make sure no stale message lingers.
rm -f "$REPORT_FILE"
# Otherwise, atomically queue the composed message text (plain UTF-8 — the
# launcher passes it through --data-urlencode, not shell interpolation):
TMP_REPORT="$(mktemp "${REPORT_FILE}.XXXXXX")"
printf '%s' "🛠️ skill-authoring <week>: authored N (proposed M PRs, K needs:human), pruned-flagged P, discarded-by-eval Q" > "$TMP_REPORT"
mv -f "$TMP_REPORT" "$REPORT_FILE"
```

Format: `🛠️ skill-authoring <week>: authored N (proposed M PRs, K needs:human), pruned-flagged P, discarded-by-eval Q`.

If the queue write itself fails (disk full, permission oddity — all
unexpected), do NOT retry it as a blocker and do NOT surface it in your final
response; note it at most in your own reasoning, then continue to the
completion marker exactly as if this step had succeeded — a send-side hiccup
is the launcher's problem (it WARNs and leaves the file queued for next week),
never a reason to withhold `SKILLSMITH_DONE`.

## Completion marker (required — the launcher fails loudly without it, #1493)
The launcher (`cloud-wiki-compile.sh skillsmith`) validates this run the same
way COMPILE mode validates `WIKI_COMPILE_RECEIPT`: a "successful" exit
(`is_error=false`) without the exact marker below is treated as FAILURE, not
silently accepted. This closes board #1493 — a prior run returned
`is_error=false`/`success` while this skill had never actually loaded (it
replied "I don't see a 'skill-authoring' skill"), and that no-op went
undetected for weeks.

The invoking prompt tells you today's date (e.g. "Today is 2026-09-24 (UTC)").
Once you have completed BOTH arms end-to-end (including filing/nudging and the
Telegram report step per the Reporting rule above — or correctly determined
there is nothing to do this week), your final response must CONTAIN, on its
own line:

```
SKILLSMITH_DONE:<today's date, YYYY-MM-DD>
```

e.g. `SKILLSMITH_DONE:2026-09-24`. It may follow a short summary — the launcher
only requires the exact string to be present in the result text, not that it
be the entire response. If you cannot find or load this skill, cannot
complete ARM A or ARM B, or hit an error partway through, do NOT print this
line — say plainly what went wrong instead, so the run fails loudly instead
of silently no-op'ing again.

## Cost profile (why this stays cheap)
One weekly pass, Haiku, bounded. The A0/A2/B1 deterministic pre-passes burn ~0
model tokens and kill most candidates before any reasoning. Eval subagents fire
only on the handful of survivors. Anti-bloat is enforced by the eval-gate (A4)
and the needs:human gate (B3), not by spend.

## What this skill will NEVER do
- Re-mine transcripts for candidates (that's 4.6).
- Copy a draft into a live `~/.claude/skills` or dept skill dir (proposes only).
- Delete/move any skill (prune is flag-only, human-gated).
- Author a critical/secret/broker/deploy/behavior-changing skill without
  needs:human.
- Decide "candidate/dead/helps" with a deterministic score (judgment is agentic).
