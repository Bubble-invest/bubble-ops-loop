# Skill-gap miner — DRY-RUN sample output (NOT a real weekly run)

**This file is a demonstration artifact only**, produced by hand-simulating
the STEP 4.6 extractor prompt (`skills/cloud-wiki-compile/SKILL.md`) against
3 real Rick/R&D session transcripts, to show the reasoning the prompt
produces and confirm the open+closed board de-dup actually catches a known
case. It is NOT written by the extractor itself (no live compile was run —
forbidden by this card's scope) and would NOT ship under this filename in
production (real runs use `{YEAR}-W{WK}-workaround-candidates.md`, no
`-DRYRUN-SAMPLE` suffix).

**Corpus:** 3 most-recent Rick/R&D `.jsonl` transcripts as of 2026-07-21
(excluding the live session doing this dry-run itself), reduced to
user/assistant text turns >50 chars, truncated to 500 chars — the identical
transform STEP 4 already applies.

**Raw keyword hits found:** 9 (grep over `manually|workaround|by hand|
hand-roll|should be a skill|capability gap|no skill|doesn't exist yet|
not auto-firing|un-carded`). Of these 9, **6 were dropped as false positives**
on reading — matching the research memo's ~60-70% FP rate finding almost
exactly (6/9 ≈ 67%).

---

### 1. Local-dept sync mechanism proven but not yet on a timer — FRICTION

**Evidence:** "The sync script works perfectly right now... So the *mechanism*
is proven; it just needs to run on a timer instead of manually." /
"I can run this manually anytime (it's claude-executable), so the mirror
isn't stuck — it just won't *auto*-refresh until the system timer is
installed." — Rick, session `8a3472dd-033a-4700-811f-4d3c5abf6cf6`

**Why it's a gap:** Not dispatch-cause, not verification, not discipline
narration — the agent is describing a genuinely manual step (re-running a
sync script by hand) standing in for automation that doesn't exist yet. Low
ambiguity: the agent frames it as a known, not-yet-closed gap ("won't
auto-refresh until") rather than incidental narration.

**Recurrence:** 1 distinct session (both quotes are the same session, same
underlying gap — not double-counted as 2).

**Proposed action:** fix-existing / operational — install the system timer
already described in the same session (the fix path is already known; this
is a "finish the install" gap, not a new-skill gap).

**Board check:** searched `gh issue list --repo Bubble-invest/bubble-ops-board
--state open` and `--state closed` for "local-dept sync timer manual
ProtectSystem" — no match found in either state.

---

### 2. systemd sandbox (ProtectSystem=strict) breaks tmp-file writes — BROKEN

**Evidence:** "there's a real bug exposed by the systemd sandbox: the service
has ProtectSystem=strict which makes /tmp read-only, and the sync script
writes a temp file /tmp/.sync-local-3601009 → 'Read-only file system' → the
pull fails (failures=1). It worked when I ran it manually (no sandbox) but
fails under the hardened unit." — Rick, session `8a3472dd-033a-4700-811f-4d3c5abf6cf6`

**Why it's a gap:** BROKEN class — an existing mechanism (the sync script)
fails only under the hardened systemd unit; "worked manually" here is
diagnostic framing (isolating the sandbox as the cause), not a workaround the
agent is proposing to keep using. Included because it is a concrete,
self-diagnosed root cause ("a real bug exposed by...") with a verbatim error
string, not a bare keyword hit.

**Recurrence:** 1 distinct session.

**Proposed action:** fix-existing (PR to the sync script or unit file — write
temp files to a path ProtectSystem allows, e.g. `/home/claude/tmp` or
`PrivateTmp` handling).

**Board check:** searched open+closed for "systemd ProtectSystem tmp sync
script read-only" — no match found in either state.

---

### Dropped as false-positive pattern (6 of 9 raw hits — shown for transparency, not filed)

| Raw hit (session) | FP bucket | Why dropped |
|---|---|---|
| "audit is done by hand and reported" (`211ce675`) | verification-step narration | Describes how an audit was conducted/reported, not an agent hitting a missing capability. |
| "reproduce its format by hand" (`211ce675`) | discussing a THIRD prompt's own documented fallback, in a PR-review context | Not this agent's workaround — it's reviewing someone else's fallback design. |
| "agent manually grabbing it during an ad-hoc request" (`8a3472dd`) | hypothetical-risk narration | Describes a danger scenario ("its only danger is..."), not an actual workaround being performed. |
| "Jade is driving it by hand" (`8a3472dd`) | describes a HUMAN's manual driving of an interactive session | Not an agent capability gap — a person operating a terminal. |
| "run the runner manually with --activate-tick once" (`8a3472dd`) | verification-step narration | Explicit pre-activation validation step before turning on automation — due diligence, not a workaround. |
| "nothing regenerates it... manual artifact I placed by hand... exactly the kind of fix that silently rots" (`211ce675`) | self-diagnosed but same-session self-fix, no cross-session recurrence, and the agent fixed it durably in the same turn | Borderline case: has the self-diagnosis LANGUAGE pattern but the agent already resolved it inline this session with no repeat elsewhere in the sample — noted here rather than promoted to a ranked candidate, since "already fixed, once, this session" is weak signal for a *recurring* gap. A real weekly run spanning more agents/sessions might see this recur and promote it. |

---

### De-dup mechanism — validated against the real board

Ran the two required `gh` searches for each surviving candidate. One of the
searches (a distinct check, run to confirm the closed-card de-dup actually
works, not one of the two candidates above) against the terms `"safe_pull
sandboxed manual push"` returned:

```
453  CLOSED  Cross-dept: sandboxed safe_pull fails every tick (.gitmodules
             perm + git junk) → depts fall back to manual push
             risk:low, type:bug, status:done, dept:rnd, proj:infra, host:vps
             2026-07-02T15:15:33Z
```

This confirms the closed-card de-dup instruction in the prompt actually
catches a real, previously-carded-and-closed gap when the search terms match
— exactly the failure mode (re-surfacing #452/#453/#563/#620 forever) the
#103 research memo flagged as the central design risk. Neither of the two
candidates kept above matched this or any other open/closed issue.

---

**skill_gap: 2 candidates written, 1 dropped as already-carded (closed card
#453, confirmed via targeted search — not one of the 2 kept candidates), 6
dropped as false-positive-pattern.**
