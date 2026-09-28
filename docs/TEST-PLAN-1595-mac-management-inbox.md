# #1595 — exact live acceptance plan

**Approval state:** this is the plan for Joris to approve, not a claim of approval.
The brief authorizes PR preparation only. Do not synthesize cockpit decisions,
mark approvals manually, merge, deploy, or send test directives from this task.

Prerequisites: review/merge the two PRs; Rick opens and deploys his workspace
proposal; Tonio's workspace owner opens/deploys the provided proposal. Each uses
its workspace venv with PyYAML and existing Git access to bubble-ops-rnd. Install
the publisher proposal before testing mirror interaction. No workspace repo was
pushed by this worker. Confirm each active runtime actually reads its runbook.

1. Tony creates two separate harmless drafts and cockpit gates, IDs
   `inbox1595-rnd-<UTC-date>-01` and `inbox1595-tonio-<UTC-date>-01`.
   Both specify `target_dept: rnd`; audiences are `[rnd]` and `[tonio]` respectively.
   Body: "On your next tick, record this directive ID and recipient in your local
   tick summary, then acknowledge applied with that summary's path. Do not change
   any project, contact anyone, or operate client/fleet services."
   Subject identifies the actual recipient; use current UTC created_at, deadline
   one day later, and evidence `Bubble-invest/bubble-ops-board#1595`.
2. **Joris approves each specific draft and audience through the existing cockpit
   gate.** Record gate IDs and signed decision references. Without both approvals,
   stop live testing. An unsigned/manual approved_by field is not authorization.
3. Tony runs the existing tools/emit-directive.py separately for each ID with
   `--target-dept rnd --gate-id <its-gate-id> --draft-path <its-approved-draft>`.
   Commit/push Tony's outbound queue through his normal runtime mechanism.
4. Let Tony's existing floor run dispatch_directives.py in remote-delivery mode.
   For EACH ID, inspect remote Tony
   `queues/management/outbound/directive-<id>.yaml`: `status: dispatched` and
   dispatched_at must be present. Inspect remote bubble-ops-rnd
   `queues/management/directive-<id>.yaml`: the same ID, correct audience/body,
   delivered_at and delivery_digest must be present. Record source and child
   commit SHAs. If either remains approved, capture relay error; do not forge ack.
5. Observe each agent's next actual tick after delivery (or a Joris-authorized
   manual tick). Record tick/session ID, start time and local outcome summary.
   Rick must act only on the rnd directive; Tonio only on the tonio directive.
   Both use scan output ID+sha256 for explicit ack after writing that summary.
6. Read remote `.consumed.json`: each ID has the correct actor, action=applied,
   timestamp and evidence path; `.last-mgmt-scan` is updated. Record its commit.
   A scan marker alone or dispatched alone is NOT proof of pickup.
7. Observe the next tick: neither test note is returned or acted on again. Have
   the normal scoped publisher run; compare inbox files and consumed entries
   before/after (byte content preserved; marker may advance with legitimate ticks).
   Confirm both IDs remain consumed. The publisher must not upload workspace
   queues. A push race may fail visibly and retry; no force push is permitted.
8. Attach this correlated evidence to #1595 through the authorized operator's
   normal reporting workflow. Declare success only after both recipients pass.
   Retain test notes and acks for audit; no destructive cleanup is required.

Offline tests cover late timestamps, concurrent ack retries, old ledger arrays,
wrong actor/digest, malformed notes and failed pushes. They do not prove installed
Git authorization, active runtime pickup or Joris's live gate decisions.
