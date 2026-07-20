# skill-updates/ — skill-gap miner output (#103, Phase-1 MVP)

This directory holds the **report-only** weekly output of the skill-gap
miner: an agentic extractor bolted onto `skills/cloud-wiki-compile`'s Phase 1
(STEP 4.6 in `skills/cloud-wiki-compile/SKILL.md`). It runs once a week (the
Sunday compile pass), reading the same reduced-text session-transcript feed
the compile job already parses for wiki knowledge — no separate scan.

**Live path note:** on the VPS this directory is `/home/claude/monitoring/
skill-updates/` — a fixed local path (this repo isn't wired into an
automatic vendor path yet, see `skills/cloud-wiki-compile/DEPLOY.md`), not
necessarily the same on-disk copy as this repo's `monitoring/skill-updates/`.
This repo's copy documents the format and carries the dry-run sample; the
live weekly files accumulate at the VPS path above until a copy of this repo
is checked out there.

## What lands here

`{YEAR}-W{WK}-workaround-candidates.md` (e.g. `2026-W29-workaround-candidates.md`)
— a ranked list of skill-gap / recurring-manual-workaround candidates, each
with:

- a **verbatim evidence quote** from a real transcript (mandatory — no quote,
  no candidate)
- the signal class (`MISSING` / `BROKEN` / `EXTEND` / `FRICTION`)
- why the extractor judged it a gap (not just a keyword match)
- recurrence (how many distinct sessions/agents it appeared in)
- a proposed action (new-skill / fix-existing / extend-existing)
- confirmation of the open+closed board de-dup search it ran before including
  the candidate

## What does NOT happen automatically

- **No auto-carding.** Nothing here becomes a board card by itself.
- **No `emit-kanban-task` call.** The extractor's only side effect is writing
  this file.
- **No confidence-bar tuning / auto-triage.** This is Phase-1 MVP only —
  Phases 2-4 (auto-carding, per-dept L4 `skill_gap_review` missions, Mac
  crons, 3-repo scaffolding) are explicitly deferred pending Phase-1 results
  (see the go/no-go research memo, `Rick_RnD/projects/skill-improvement-system/RESEARCH.md`).

## Human loop

Rick reviews this file by hand, weekly. Real, actionable, not-already-carded
candidates get carded manually through the normal board flow. Everything else
is just left here as a dated record — it is cheap, reversible, and
append-only across weeks (each week gets its own dated file, nothing is
overwritten).

## Why "report-only" matters here

The #103 research memo found the loudest recurring signal in the fleet (the
git-push-guard recipe) was already carded four times, several CLOSED — a
miner that cards automatically without checking CLOSED cards would re-file
it every week forever. Report-only + mandatory open+closed de-dup + human
triage is the guardrail against that failure mode while the miner's
candidate quality is still being proven out.
