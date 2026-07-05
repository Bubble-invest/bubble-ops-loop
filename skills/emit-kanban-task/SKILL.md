---
name: emit-kanban-task
description: >-
  Push an action-required item onto the Bubble ops board (the GitHub-Issues
  control plane Bubble-invest/bubble-ops-board, rendered in the cockpit /kanban)
  so a human or agent can triage it later — instead of letting a finding
  evaporate in chat. Use whenever you uncover something that needs follow-up but
  is OUT of the current scope: "add this to the kanban / backlog / for tomorrow",
  "flag this for {{OPERATOR}}", "we should track X", "create a card for…", "remember to
  do Y later", or any decision/approval/incident/finding that shouldn't block the
  current task but must not be lost. Reach for this the moment you think "that's
  worth doing later" — that's the signal.
---

# emit-kanban-task — file a tracked card on the Bubble board

## Why this exists

Findings that surface mid-task ("the auto-commit cron is dead", "this paid-IC path
has no gate", "verify the audio routing tomorrow") get lost if they only live in chat. The
kanban is the durable triage board: each card is a structured item a human (or the R&D
manager loop) acts on. **Emitting a card is how a side-discovery becomes tracked work instead
of a forgotten sentence.** Don't hand-write to a local DB or a markdown note — those don't
reach the board; use this skill.

## The command

Call the bundled wrapper `scripts/emit.sh` (in this skill's own directory). It resolves the
underlying emitter portably — your dept-local vendored copy first, then the framework path —
so you never have to figure out where the tool lives:

```bash
<this-skill-dir>/scripts/emit.sh \
  task=<source-id> \
  title="<short imperative — what needs to happen>" \
  body="<self-contained context, ≤2000 chars — readable COLD, no 'as we discussed'>" \
  type=<approval|decision|incident|findings|manual|bug|feature|infra|docs|chore|research> \
  priority=<normal|high|urgent> \
  owner=<your dept slug, e.g. ben|maya|tony|content|accountant|rnd|tonio> \
  budget=<REQUIRED integer USD, per-run, e.g. 10 — see Budget guidance below> \
  proj=<optional project slug, e.g. bubble-shield, client-dev, cockpit, fund> \
  due=<optional YYYY-MM-DD> \
  host=<optional local|vps — usually inferred from owner> \
  links=<optional typed links, e.g. parent:258;relates:318,324;blocks:340> \
  actions=<comma-separated, e.g. accept,investigate,escalate> \
  context_url=<optional link to wiki/PR/dashboard> \
  diagram_mermaid="<Mermaid source, ≤3000 chars>" \
  visual_attachments="<comma-separated repo-relative paths under outputs/>"
```

Required: `task` + `title` + **`budget`** (integer USD, per-run — every card must carry a
budget so cost is attributable per card from creation; board #537). A missing or
non-integer `budget=` makes the emit **fail loud and create no card** — you'll see a clear
`budget= is required` error on stderr, and nothing lands on the board. Everything else is
optional but `body`, `type`, `priority`, `owner` make a card actionable rather than a
mystery. The underlying tool exits 0 even on a valid-but-failed emission (emission must
never break your tick) — so confirm it landed (see "Verify" below) rather than trusting the
exit code alone.

**Picking a budget** (per-run/per-card USD estimate, tied to scope):
- **Small** (~$2–5): a quick lookup, a one-file fix, a single triage pass.
- **Medium** (~$10–20): a typical multi-step task — investigate + fix + test one thing.
- **Large** (~$30–60): a multi-step build spanning several files, a PR with tests, or a
  fleet-wide contract change.
When unsure, round up slightly rather than under-budget — the number is a planning signal,
not a hard cap.

## When to emit (and when NOT to)

**Emit a card when:**
- You find a real follow-up that's **out of the current scope** (don't derail the task you're
  on, but don't lose the finding) — e.g. a dead cron, a missing gate, a stale doc, a security
  smell you spotted in passing.
- The human asked to "track this / add to kanban / do this tomorrow / remember Y".
- A decision/approval needs a human and you'd otherwise bury it in a long summary.
- An incident/alert needs attention but isn't blocking right now.
- You want to hand work to another agent / to Rick (R&D) — file it with the right `owner`.

**Do NOT emit for:**
- Something you can just *do* now in a few seconds (do it instead).
- Something that needs THIS conversation's context to make sense (a card is read cold later —
  if it can't stand alone, it's not a card; finish it here or write a proper doc).
- A vague feeling ("this could be cleaner"). Cards are concrete, actionable items.
- Spamming many cards for one thing — one card per coherent piece of work.

Litmus test: *"If I say this once in chat and nobody acts in the next 10 minutes, will it be
lost — and does it matter?"* If yes to both → emit a card.

## Field guidance (where cards go wrong)
- **task** — a stable source-id slug (e.g. `ben-l4-debrief`, `maya-warming`). Reused id +
  same `<!-- emit-task: <task> -->` marker → no duplicate card (idempotent).
- **title** — imperative, what must HAPPEN, ≤200 chars. "Void the stale INDA trim order" —
  not "INDA stuff" or "there might be an issue with INDA".
- **body** — **self-contained.** The reader has zero context from your chat. Include the what,
  the why-now, the file paths / commands / evidence, and the success criterion. ≤2000 chars.
- **type** — `approval` (human must say yes), `decision` (a choice to make), `incident`
  (something broke), `findings` (something to look into), `bug`/`feature`/`infra`/`chore`.
- **priority** — `urgent` (today/now), `high` (this week), `normal` (backlog). Don't inflate;
  urgent-everything trains people to ignore it.
- **proj** — the project/initiative the card serves (the cockpit `proj:` grouping). Use
  `client-dev` (umbrella) + a per-product slug like `bubble-shield` for client work; or `cockpit`,
  `fund`, `infra`, etc. `dept:` = WHO acts, `proj:` = WHAT it serves — set BOTH where they differ
  (a Bubble Shield bug = `owner=security proj=bubble-shield`). Unknown slugs are auto-created.
- **due** — `YYYY-MM-DD` deadline → a `due:<date>` label. Renders on the cockpit **timeline**
  (Échéances) view + sorts surfaced cards (overdue first). Malformed dates are ignored.
- **budget** — **REQUIRED.** Integer USD, per-run (e.g. `budget=15`) → a `budget:$N` label.
  This is the card's real-$ (cache-excluded) cost attribution — see "Picking a budget" above.
  A missing, empty, or non-integer `budget=` is a **hard fail**: the emitter prints a clear
  `budget= is required` error to stderr and creates no card at all (not even a queued
  fallback). There is no default — every card must carry an explicit estimate.
- **host** — `local` or `vps`; normally inferred from owner (tonio/content/claudette→local,
  ben/maya/tony/accountant/morty→vps). `owner=tonio` → `dept:tony` + `host:local` (the local
  Tony, @ClaudeRickyBot). With `owner`+`host`, the card surfaces on THAT agent's loop tick.
- **links** — typed links to OTHER cards, to map out a project (Obsidian-style). Syntax:
  `links=parent:258;relates:318,324;blocks:340` — three kinds: `parent` (this card is a child of N),
  `relates` (soft link), `blocks` (this card blocks N). Rendered as a `## Links` section of `#N` refs
  (clickable on GitHub) → the cockpit shows link-chips on the card + a per-project **Carte des liens**
  graph (Mermaid). Use it to connect an epic to its sub-tasks and sequence dependencies.
- **owner** — who should act: your own dept slug, another dept, `rnd` (Rick/R&D for
  infra/tooling), or `operator`/`operator2` (principals). Omit only if genuinely unassigned.
- **actions** — the buttons the triager gets: e.g. `accept,reject,escalate` or
  `investigate,defer`. Match the type.
- **diagram_mermaid** — when a visual decision aid helps, write a Mermaid diagram (≤3000
  chars). Rendered client-side in the cockpit kanban. Keep it business/ops readable.
- **visual_attachments** — comma-separated repo-relative paths to PNG/JPG/SVG under your
  dept `outputs/` (e.g. `outputs/2026-06-20/charts/contrib.png`). Rendered inline.

## What happens after

The card becomes a **GitHub issue on the Bubble board** (`Bubble-invest/bubble-ops-board`) —
the control plane (single source of truth) rendered in the cockpit `/kanban` and mirrored
read-only to Notion. Your `owner`/`type` map to `dept:`/`type:` labels; the card lands as
`status:triage` for the R&D manager loop to classify (`approval`/`decision` types add
`needs:human`). **Auth is automatic:** on the VPS the tool mints a short-lived issues:write
board token via the root-owned minter (`bubble-board-token.sh`, sudoers NOPASSWD); on a dev
Mac it uses your authenticated `gh`. If GitHub is unreachable it falls back to the legacy
dashboard POST — so emission never fails your tick (exit is always 0).

**Verify it landed** (the tool is silent on success):
```bash
gh issue list --repo Bubble-invest/bubble-ops-board --search "<your title>"
```
Or open the cockpit `/kanban`. If you can't see it on the board, it did NOT land — do not
assume success from exit 0.

## If your card's work ends in a PR — the cockpit «Prêt à merger» conventions

The cockpit home page surfaces **merge-ready PRs** to the operator with a plain-French
explanation, evidence chips and a direct GitHub link (the «🟢 Prêt à merger» panel). For your
PR to appear there **correctly and legibly**, follow BOTH conventions:

1. **`RÉSUMÉ:` line — first line of every PR body.** One clear French sentence a non-developer
   reads cold: what this PR changes for the firm, not how. It becomes the card's headline on
   the cockpit (fallback = your technical title with its `feat(scope):`-style prefix stripped —
   always worse). Example: `RÉSUMÉ: Le cockpit affiche les PRs validées prêtes à merger, avec
   explication claire et lien direct.`
2. **The merge-ready marker — only in a genuine final verdict.** A PR is shown as merge-ready
   iff its **latest** review-verdict comment contains the exact phrase `Merge-ready for Joris`
   at the start of a line (the console matcher is literal, hardened against the phrase being
   *quoted* inside a FAIL/request-changes comment — quoting it is safe). Never write it as a
   verdict unless an **independent reviewer (maker≠checker) actually PASSED** the diff; include
   the evidence in the same comment (tests passed, mutation checks, scope).

Coverage: `bubble-ops-loop` PRs today; dept repos follow automatically once the board App's
grant widens (board #470) — same conventions, zero further change on your side.

## Example — a finding spotted mid-tick (don't derail, don't lose it)

```bash
scripts/emit.sh \
  task=ben-positions-snapshot \
  title="Positions snapshot is degenerate — poisons sizer + 2 KPIs" \
  body="The stored positions snapshot is a plumbing bug (live NAV/holdings are correct, but the snapshot is degenerate). It poisons downstream: the Ledoit sizer and two L4 KPIs read it. PR #61 fixes the sizer; the deeper consolidation fix is out of scope for this tick. Track it. Evidence: outputs/<date>/positions-snapshot.json vs live broker NAV." \
  type=bug priority=high owner=rnd budget=15 actions=accept,investigate,defer
```

## Note

This is for the **Bubble ops board kanban** (the cross-agent triage board). It is distinct
from the harness's own `spawn_task` chip and from `TaskCreate` (in-session todo list). Use
this one when the item belongs on the shared, persistent ops board for a human/agent to pick
up later. Do NOT write to any local `kanban_cards` DB table or a markdown note as a
substitute — those are unwired and do not reach the board.
