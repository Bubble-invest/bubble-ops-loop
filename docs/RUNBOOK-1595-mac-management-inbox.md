# #1595 — Rick and Tonio management directives

Both Mac manager loops consume the existing `queues/management/*.yaml` contract
in **Bubble-invest/bubble-ops-rnd**. Rick's private workspace and Tonio's private
workspace must not become relay destinations: they contain unrelated projects
and are outside the relay's fixed Bubble-invest/bubble-ops-* repository mapping.
The scoped mirror already preserves queues; do not add queues to its source
export allowlist. Inbox ownership belongs to the relay and consumers.

Tonio uses a documented equivalent to a separate child repo: Tony's approved
draft has `target_dept: rnd` (transport) and `audience: [tonio]` (sole consumer).
For Rick use `audience: [rnd]`; absent audience defaults to rnd for old notes.
Use separate globally unique IDs for each recipient. Never a mixed audience:
the standard `.consumed.json` ledger is keyed by ID, not by recipient. This
routing does not add Tonio to Tony's hierarchy, change mandate, bypass approval,
or grant access to the client workspace. The cockpit lists both under rnd;
ack metadata includes `actor`. Accept that display limitation for this minimum.
Tonio's write access to bubble-ops-rnd comes from the Mac's shared `vdk888` Git identity; no new scoped grant is added.

## Install after review

1. Merge the loop reader PR and Tony routing-instruction PR after review.
2. Rick opens his own workspace PR from the supplied `rick-side/` proposal:
   `RICK_LOOP.md`, `tools/management_inbox.py`, and
   `tools/rnd_scoped_mirror_publish.sh`. The reader is a byte-for-byte copy of
   this repo's `scripts/management_inbox.py`; update it from this canonical file.
3. Tonio's workspace owner opens its corresponding proposal PR for
   `TONIO_LOOP.md` and the same `tools/management_inbox.py`.
4. Both Mac users need existing Git read/write authentication for the fixed
   mirror and a workspace Python venv with PyYAML. No token appears in arguments.
   Do not expand relay credentials or install into live trees from this PR.
5. At the very start of **every** tick (including heartbeat ticks), run:
   `.venv/bin/python tools/management_inbox.py --actor rnd scan`
   (Tonio uses `--actor tonio`). Act within mandate, record an outcome in the
   tick's local evidence, then `ack --id ID --sha256 HASH --action applied
   --outcome EVIDENCE_REFERENCE` with the same actor. `deferred` and `conflict`
   require a follow-up/escalation reference. A successful ack is consumption,
   not necessarily completion of the requested work.

Each command uses a fresh private clone, stages only the scan marker or consumed
ledger, commits and pushes main without force. Push races retry from remote,
preserving concurrent acknowledgements. An ack matches recipient and exact
scanned bytes; changed notes require a rescan. Scan uses unconsumed IDs, never
creation-time watermarks. Scans with no pending notes for the actor leave the
marker unchanged and make no commit or push. The marker is retained because
shared dispatcher and console code read it. The shared `.last-mgmt-scan` records
a scan that found pending notes, not that both actors handled every note.
No new scheduler is introduced.

On failure log `[context-skip directives]` and continue the tick; retry next tick.
An action may have succeeded before a failed ack: consult outcome evidence before
repeating it. This is the existing at-least-once consumption contract, not an
exactly-once side-effect guarantee. Malformed ledger/notes fail visibly without
stamping a successful scan. Do not put client data or secrets in this inbox.

## Acceptance — Joris must approve each test through the existing gate

No test directive is authorized merely by merging this code. Use the two
synthetic directives in the delivery's TEST_PLAN.md. Verify source dispatched,
remote delivered payload/digest, next-tick evidence, consumed actor/outcome,
second-tick non-repetition, and preservation across a scoped mirror publish.
Do not mark #1595 live-proven before both workspace proposals are installed and
all those observations are recorded.
