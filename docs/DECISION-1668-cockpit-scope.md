# #1668 — cockpit source, user, and view inventory

**Status:** proposed keep/retire decision; no UI or route removal is authorized by this document.

**Observed:** 2026-10-01 UTC.

**Code baseline:** `e2877d138caff591c144d9cafb6f47050566de05`; the local `origin/main` and the deployed `/opt/bubble-ops-loop` checkout matched this commit during the inventory.

**Serves intent:** `rick-controlled-rnd-convergence` and the operator-approved conservative scope recorded on board #1668.

## Decision summary

Keep the cockpit as a small human control plane, not as a general-purpose agent browser.

The retained core is:

1. human decisions and approvals, including their evidence and audit trail;
2. mission result and proof access;
3. health/freshness with explicit source and as-of time;
4. cost/error visibility;
5. the minimum lifecycle controls needed to add, activate, cancel, or retire a department safely.

No current route should be deleted from the approved scope alone. The next implementation PR may only remove or fold a named candidate below after its listed preconditions pass. An operator-requested semantic view stays until the retained surface has full semantic parity and Joris explicitly approves the replacement.

**Proposed target:**

- **Keep as core:** `/`, `/gate/...`, `/kanban...`, `/pr/...`, `/health` including its hierarchy, four-moment, rail, layer-proof, and source-flow views, `/costs`, and the result/evidence subset of `/dept/<slug>`.
- **Keep as low-frequency operations:** `/agents...` and onboarding while a department is not yet Live.
- **Keep, but assign to a business-domain owner rather than the generic cockpit core:** `/dept/<slug>/portfolio`.
- **Keep as an explicit operator feature:** Tony's company operations map from board #1602, including missions by business unit and layer, live state, gaps, and drift. #1602 also requests an explicit failed mission state and a load row showing missions per agent plus open `needs:human`; the current implementation exposes neither. It is not decorative and has no demonstrated feature-parity replacement.
- **Keep as an authentication operation:** one-time `/login/link` enrollment with its single-use claim, 15-minute default TTL, and access/app-log token redaction.
- **Propose folding only after parity:** standalone `/settings/<slug>`, standalone `/dept/<slug>/management-view`, duplicate home-page panels, and onboarding polling after activation. Concierge/detail and transcript routes require workflow confirmation; transcript exposure is also a separate current security defect, not a usage-based deletion decision.

Approvals, board comments, gate decisions, structural-PR reviews, attachments, output files, mission files, and provenance timestamps are explicitly excluded from retirement.

## Evidence boundary

This inventory used read-only code, service metadata, filesystem metadata, the opaque-session database's non-secret columns, aggregated access-log counts, and public board comments. It did not print credentials, session ids, raw transcripts, raw access logs, or gate contents. The correction also uses an independent reviewer's already-supplied aggregate journal result; this document does not reproduce the privileged query or its log contents.

The usage evidence has important limits:

- The original 180-line journal result was a permission-filtered `claude`-visible subset. Its 71 HTTP requests covered **2026-09-17 through 2026-09-23**. It is useful only for positive lower-bound observations; it is not the service's complete 14-day history.
- An independent reviewer with aggregate root-journal access reported **5,087 journal lines and 2,601 HTTP requests**, including requests to `/health` and `/costs`. That directly invalidates the prior claim that access logging stopped on 2026-09-23 and invalidates all route-level “0 observed” or “unused” conclusions derived from the permission-filtered subset.
- The aggregate supplied in review does not attribute requests to Joris or Jade, does not establish unique visits, and did not include route-by-route counts in the review finding. This document therefore uses it only to establish that the earlier dataset was incomplete and that `/health` and `/costs` were requested.
- `/dept/<slug>/inbox-fragment`, `/dept/<slug>/session`, and onboarding fragments are automatic polls. Their request counts are not user counts.
- Browser icon probes, login redirects, liveness probes, and static files are not product usage.
- The remaining on-disk gate/decision files are not a complete action history: agents process, archive, and move them. They are useful only to establish that multiple named operators have used gate paths.

## Actual user and consumer inventory

| Identity / consumer | Evidence | Role in the cockpit | Conclusion |
|---|---|---|---|
| **Joris** | Seven session rows; last seen 2026-10-01 11:51 UTC. Public board audit found both approval and rejection markers attributed as “Joris via cockpit”; duplicate marker comments mean comment totals are not unique-decision totals. Remaining dept decision records also contain `joris`/`Joris`. | Primary operator and approver. | Every retained decision path must optimize for Joris and preserve exact audit/evidence links. |
| **Jade** | Four session rows; last seen 2026-09-30 16:36 UTC. Remaining decision records contain `jade`/`Jade`, especially Content and Maya. | Authenticated operator/viewer and department-scoped gate actor. | Do not assume Joris is the only human user. Jade's exact live RBAC grants are secret configuration and were not inspected. |
| `bearer-bootstrap` | Three historical session rows, last seen 2026-09-02. | Legacy/bootstrap identity, not a named human. | Do not use it to claim human adoption. Remove only through a separate auth migration, not this view-scope change. |
| `selftest` | One session row, last seen 2026-09-02. | Test identity. | Exclude from human usage conclusions. |
| API/CI bearer principal | Supported by middleware; access records are not attributable to a username. | Machine access and cutover fallback. | Preserve authentication compatibility until its callers are inventoried separately. |
| Department loops | Gate decision YAML in `inbox/decisions/`; queue and output readers. | Downstream consumers of human gate decisions. | Gate write shape and paths are contracts, not presentation details. |
| Rick's loop | GitHub issue labels/comments/state. | Downstream consumer of kanban decisions and comments. | Board mutation semantics and marker comments are contracts. |
| Structural merge guard | Cockpit-approver App review plus commit status. | Downstream consumer of `/pr/.../approve`. | Structural approval must remain isolated and tied to the exact reviewed SHA. |

The session database proves authenticated identities, not which view each identity used. Current access logging does not include the session username, so route-level attribution to Joris versus Jade is unavailable.

## Observed route evidence

The permission-filtered request counts in the first draft were not complete service totals. They are retained only as positive lower bounds; absence from that subset is discarded. The independent reviewer aggregate establishes 2,601 HTTP requests and specifically confirms `/health` and `/costs`, but it does not provide a safe route-by-route count for this document.

| Surface | Positive evidence safe to use | Decision consequence |
|---|---|---|
| `/`, `/dept/<slug>`, `/gate/...`, `/kanban...`, `/pr/...`, `/agents...`, `/dept/<slug>/portfolio` | Each appeared in the permission-filtered subset; writes were also observed on gate, kanban-comment, and PR-approval paths. Counts are lower bounds, and polling requests are not intentional visits. | Preserve operations and evidence. Do not rank or delete these surfaces from the partial counts. |
| `/health`, `/costs` | The independent reviewer aggregate explicitly included both routes. | They are used routes as well as approved core capabilities; remove the earlier zero-use claim. |
| `/settings/<slug>`, `/concierge/<name>`, `/dept/<slug>/management-view` | No reliable conclusion from the supplied aggregates. | Any fold/retire proposal must rest on semantic parity and operator confirmation, not apparent absence. |
| `/dept/<slug>/session`, `/concierge/<name>/session` | Polling can inflate counts; code inspection establishes that the routes expose recent transcript content. | Treat workflow usage as unknown and handle the security defect separately; do not infer either popularity or dispensability from request volume. |

A separate public-board audit found cockpit approval and rejection marker comments attributed to Joris. Issues #288 and #381 each contain duplicate marker comments, so volatile comment counts are not treated as unique-decision counts. The audit found no Jade-attributed board decision marker, but that does not contradict Jade's gate usage: board decisions and department gates are separate paths.

## Source and freshness inventory

### 1. Decisions and approvals

| Surface | Designed authority | Read/write behavior | Freshness / caveat | Proposal |
|---|---|---|---|---|
| Home decision sections (`/`) | Dept gate YAML and decision YAML; GitHub board issues; GitHub PR state | Reads pending gates and recent decisions from dept checkouts; board list uses a 60-second in-process cache; merge-ready PR data is read-only. | The page combines several authorities and can silently mix fresh GitHub state with stale checkout mirrors. | **Keep**, but retain only decision queues, merge-ready items, recent decision audit, and a compact health/error summary. Link to dedicated team/cost/board views rather than duplicating them. |
| Gate detail/batch (`/gate/...`) | `queues/gates/*.yaml` plus allowlisted payloads/attachments | Direct read; POST writes `inbox/decisions/<gate-id>.yaml`; undo deletes only an unprocessed decision. Gate RBAC fails closed. | Source freshness equals the checkout being read. For host-local or stale mirrors, the UI can show stale gates; the checkout-staleness badge is advisory and itself cached for 300 seconds. | **Keep all reads, writes, attachments, and evidence.** Do not change decision shape or path in a view-scope PR. |
| Kanban (`/kanban...`) | `Bubble-invest/bubble-ops-board` issues/comments/labels | GitHub REST; board list cached 60 seconds; single-card detail/comments are uncached; decision/comment POSTs mutate the issue and invalidate the cache. | GitHub is authoritative. Record-only decisions are later consumed by Rick. | **Keep.** It is both an operator view and a control path. |
| Structural PR (`/pr/...`) | Live GitHub PR, changed files, merge-guard check, exact head SHA | GET is read-only; POST submits an App-signed APPROVED review and best-effort commit status. | PR detail is fetched live. Approval rejects a missing/malformed or changed SHA. | **Keep.** It is a security gate, not optional UI. |

### 2. Health, results, and evidence

| Surface | Designed authority | Current implementation | Freshness contract / caveat | Proposal |
|---|---|---|---|---|
| Health table (`/health`) | Actual loop heartbeat and per-layer last success | Reads `outputs/<date>/heartbeat.log` and `outputs/<date>/<layer>/.last-run`; layer stale threshold is 30 hours, loop pulse threshold 90 minutes. | `morty_reader` currently resolves through `dept_registry.repo_path`, i.e. `/home/claude/agents/bubble-ops-*`, not `runtime_repo_path`. After uid/runtime isolation this can measure a mirror rather than the actual runtime. | **Keep the freshness table and layer proof drill-down, but correct and label source authority before treating it as fleet truth.** |
| Health graph/dataflow (`/health/graph.json`, `/health/dataflow/<slug>.json`) | Registry/dept.yaml-derived architecture, per-dept declared sources/queues/repos, and the same health signals | The `/health` page's interactive graph is the current implementation of the requested hierarchy, four-moment loop, two rails, layer proof, and source-flow drill-down. `/health/graph.json` is also consumed by `scripts/cockpit_health/checks.py` for independent heartbeat-age checks. | The graph does not improve the underlying authority, so provenance defects still apply. But the table alone is not semantic parity for hierarchy, rails, relationships, or source flow, and the JSON route has a non-UI consumer. | **Keep.** Do not label it decorative or retire it unless a replacement preserves every named semantic and downstream contract and Joris explicitly approves the replacement. |
| Dept detail (`/dept/<slug>`) | No single authority: dept.yaml, queues, outputs, decision files, runtime files, GitHub board snapshot, and domain stores | Large composite page. Some readers use `repo_path`; canonical NAV and some reports use `runtime_repo_path`; Tony's kanban snapshot uses a separate dashboard endpoint with 30-second cache. | Mixed authorities can disagree. The route exposes some timestamps, but not one consistent provenance contract for every panel. | **Keep a narrowed result/evidence page:** current mission result, as-of/source, pending gates/queues, latest output links, and decision history. Fold or remove panels listed below only after parity checks. |
| Output and mission-file routes | Allowlisted files in dept repo | Read-only evidence renderers with containment checks. | Freshness is the file's own date/mtime; no separate store. | **Keep.** These are evidence trails required by the approved scope. |
| Management view (`/dept/<slug>/management-view`) | Latest child Layer-4 `management-export.yaml`; risk files and gates are secondary inputs | Reads children from management dept.yaml, then reads child mirrors under `READ_FROM_DISK`. Canonical location is `outputs/<date>/4/management-export.yaml`, with a legacy root fallback. | Correctly reports `last_seen_at`/staleness, but stale mirrors remain stale data. Tony has no reliable single fresh feed today. | **Propose fold into `/dept/tony`, then retire the standalone route** only after every child has a reliable canonical export and stale/missing provenance is visible. Do not build a replacement state store. |
| Portfolio (`/dept/<slug>/portfolio`) | Investment-owned files/DB: pushed `graph-data.json`, runtime `fund.sqlite`, latest dated report fragments and charts | Domain-specific living report; latest pre-rendered fragments search back seven days and show a stale banner. | It appeared in the permission-filtered request subset. Canonical NAV is explicitly the pushed audited morning `graph-data.json`; other tabs may use runtime DB or dated HTML. | **Keep, but classify as an investment-domain product owned by Ben, not generic cockpit core.** Its future can be decided by that domain, not by a fleet-console LOC target. |
| Operations map (`/dept/tony/operations-fragment`) | Tony's `outputs/operations-map/operations-map.yaml`; live dept mission declarations; dispatch ledgers and per-mission `.last-run`; dept pulse | Read-only, refreshed every 60 seconds. It renders business-unit columns, operational-layer rows, mission live state and last run, declared gaps, unmapped-mission drift, and department pulse. Its states are completed, due, not-run, and unknown; it cannot report failed missions, and it has no load row. | Board #1602 records Joris's request for a real-time Joris/Tony operations view. The current map uniquely combines missions, business units, layers, live state, gaps, and drift; connections, bottlenecks, counters, an explicit failed state, and a load row showing missions per agent plus open `needs:human` remain unmet operator requirements. | **Keep.** It is a unique operator-requested control view, not a duplicate of the architecture graph. Replace or fold it only after full feature parity is demonstrated or those unmet requirements are explicitly decided, and Joris re-approves the replacement. |

### 3. Cost

| Surface | Authority | Freshness | Observed live source | Proposal |
|---|---|---|---|---|
| `/costs` and `/costs.json` | Mirrored Claude/Hermes session JSONLs under `BUBBLE_COST_PROJECTS_DIR`; public/model price table; configured weekly envelopes | Per-session parses cached by file mtime; assembled report TTL is 45 seconds. It is an estimate for trend/relative use, not billing. A heartbeat-to-session invariant flags missing transcript coverage. | On 2026-10-01 12:19 UTC the source held 6,175 JSONLs, 549 modified in the prior seven days, and the newest mtime was 2026-10-01 12:07 UTC. | **Keep.** Add/retain source age and “estimate, not billing” labels. Do not create a second cost store. |

### 4. Team and lifecycle

| Surface | Authority | Freshness / behavior | Proposal |
|---|---|---|---|
| `/agents` | `onboarding/STATE.yaml`, known concierge registry, and service status | Direct filesystem reads; concierge service status cached 30 seconds. | **Keep as low-frequency Operations**, outside the primary daily decision flow. |
| `/agents/new`, activation, cancel, retire | Bootstrap/eclosure scripts, one-shot GitHub App setup state, activation PR, dry-run retirement | These are operations. Retirement remains preview-only and actual privileged execution stays outside the console. | **Keep.** Do not treat POST routes as removable views. Preserve preview/confirm boundaries. |
| `/login/link` | SQLite login-token row bound to a username | A valid token is atomically claimed once, defaults to a 900-second TTL, creates an attributed session, and is then unusable. Query-token values are redacted from access and app/root logs; a preview/prefetch can still consume the GET before the human. | **Keep.** Preserve single-use consumption, the 15-minute default TTL, and log redaction. It is an enrollment/authentication operation, not a removable convenience page. |
| `/agents/<slug>/onboarding` and fragments | `STATE.yaml`, dept draft/config, repo artifacts, chat log, latest git commit | Timeline and heartbeat poll every five seconds; STATE is read fresh. | **Keep only while status is not Live.** After activation, propose redirecting to `/dept/<slug>` and removing routine polling; retain historical artifacts through allowlisted evidence links. |
| `/settings/<slug>` | `dept.yaml` gate policies and config | Read-only; no unique write path; same source already loaded by `/dept/<slug>`. | **Propose fold into a collapsed “configuration source” section on `/dept/<slug>` and retire the standalone route.** Preserve exact source link and raw/allowlisted mission-file access. |
| `/concierge/<name>` | Explicit concierge registry; systemd status; project `STATUS.md`; newest session JSONL | Service status cached 30 seconds; session fragment polls live transcript. | Workflow use is unknown. **Keep pending Joris/Jade confirmation.** A later fold of status + latest project/result into `/agents` is eligible only if the dedicated page has no unique workflow and transcript security is handled separately. |
| `/concierge/<name>/session` and `/dept/<slug>/session` | Newest mirrored session JSONL | Both render recent user/assistant message text. Dept tool details pass through a limited secret-shape scrubber; concierge tool details are rendered from raw tool input without that scrubber. Neither path redacts arbitrary sensitive prose in message text. | **Current security defect; no usage conclusion.** Do not remove the UI blindly in this scope decision. Track and remediate exposure separately with explicit authorization/redaction requirements, while preserving any confirmed operator workflow and the transcript audit/storage lifecycle. |

### Current transcript-exposure security defect

This defect exists independently of whether the panels are popular:

- `agent_session.read_session_turns()` renders user and assistant text blocks verbatim. Its tool-detail scrubber covers a finite set of secret shapes and truncates output, but it does not sanitize arbitrary sensitive message text.
- `concierge_reader.read_recent_session()` also renders message text verbatim, and its `_tool_detail()` returns raw command/URL/query-like values without the dept reader's secret scrubber.
- Global cockpit authentication limits the audience but is not content-level least privilege. Route counts cannot prove that every authenticated viewer should see every department or concierge transcript.

The correction is a separate security change: define the authorized audience and a fail-closed rendering/redaction contract, add tests for sensitive prose and tool inputs, and decide with the operator whether full live text is still required. A scope-reduction PR must not silently substitute route deletion for that security decision.

## Live source-freshness snapshot

The table below reports the **source visible to the deployed cockpit under `/home/claude/agents/bubble-ops-*`**, not necessarily the actual agent runtime. It is evidence of the authority problem, not a claim that the underlying agent failed to run.

| Dept | Host declaration | Latest visible canonical `management-export.yaml` date | Age on 2026-10-01 | Meaning |
|---|---|---:|---:|---|
| accountant | local | 2026-09-27 | 4 days | Mirror/export is not current-day. |
| ben | vps | 2026-09-11 | 20 days | Strong evidence that the cockpit mirror is not a safe live authority for management state. |
| content | local | 2026-09-28 | 3 days | Recent, but not current-day. |
| maya | vps | 2026-09-09 | 22 days | Strong evidence of stale mirror/feed. |
| rnd | local | 2026-09-28 | 3 days | Recent, but not current-day. |
| tony | vps | 2026-09-05 | 26 days | Confirms the known lack of a reliable current Tony feed. |

The correct response is not to copy this data into a new database. The response is to choose the existing authoritative runtime/export path per data class, expose its provenance and age, and fail visibly when that source is missing.

## Proposed keep/fold/retire list

### Keep without route removal

- `/login`, `/logout`, authenticated-session attribution, and one-time `/login/link` with atomic single use, 15-minute default TTL, and log redaction.
- `/health-noauth` as a machine liveness probe.
- `/` as the primary human decision inbox, narrowed rather than replaced.
- All `/gate/...` reads, decisions, undo semantics, attachments, and payload evidence.
- All `/kanban...` board reads, comments, and decision mutations.
- All `/pr/...` evidence and App-signed approval semantics.
- `/health` freshness table, hierarchy/four-moment/two-rail graph, `/health/graph.json`, source-flow drill-down, `/health/dataflow/<slug>.json`, and `/health/layer/...json` proof drill-down.
- `/costs` and `/costs.json`.
- `/dept/<slug>` result/evidence core plus output and mission-file readers.
- `/dept/tony/operations-fragment`, preserving the #1602 mission/business-unit/layer/live-state/gap/drift semantics and completing or explicitly deciding the requested connection, bottleneck, counter, failed-state, and load-row semantics; the load row means missions per agent plus open `needs:human`.
- `/agents...` lifecycle operations, including activation/cancel/retire safety boundaries.
- `/dept/<slug>/portfolio` as a separately owned investment-domain surface.
- Current concierge and transcript routes until their workflows are confirmed and the transcript-exposure defect is resolved by a separate security decision.

### Fold, then retire the standalone route or panel

1. **`/settings/<slug>`** → fold read-only config/gate-policy provenance into `/dept/<slug>`.
2. **`/dept/<slug>/management-view`** → fold the reliable subset into `/dept/tony` after canonical exports are current and provenance-complete.
3. **`/concierge/<name>`** → only after Joris/Jade confirm no unique workflow, fold status, last activity, and latest project/result into `/agents`; do not treat the transcript defect as evidence that the whole detail route is redundant.
4. **Home duplicates** → remove duplicate team roster, full kanban groupings, full cost tables, concierge details, and domain report content from `/`; keep counts/errors and links.
5. **Onboarding for Live departments** → redirect to dept detail; keep onboarding only for Idea through Ready-to-activate states.

### Eligible for later retirement only after parity/precondition checks

1. Five-second background polling for dept inbox/session and onboarding fragments where no active lifecycle operation requires it. Keep manual refresh or a slower visibility-aware refresh; do not add a new state or event store.
2. A standalone route already named in the fold list, but only after its retained destination has full semantic parity, its users approve, tests cover the replacement, and redirects preserve deep links.

The health graph/dataflow routes, Tony operations map, and transcript routes are **not** retirement candidates in this decision. The first two are explicit operator-requested semantics with no parity replacement. The transcript routes require a separate security/workflow decision before scope can be changed safely.

## Preconditions for any retirement PR

A future implementation PR must satisfy all of the following for each named route/panel:

1. **Name the user and downstream consumer.** Confirm with Joris and Jade when the telemetry cannot attribute route use.
2. **Name the authority.** Document the exact existing file/API/database and whether the cockpit reads runtime state or a mirror.
3. **Show provenance in the retained surface.** Source, as-of time, and stale/missing state must be visible, not inferred.
4. **Preserve operations and audit.** No change to gate decisions, board comments/labels, PR approvals, RBAC, decision signatures, attachments, output evidence, or mission-file evidence.
5. **No new state store.** Fold views onto existing authorities; do not create a database to mask stale mirrors.
6. **Prove full semantic parity.** For operator-requested views, enumerate the original semantics—not just source/as-of fields—and prove the replacement retains them. This includes health hierarchy, four moments, two rails, relationships, layer proof, and source flow; and #1602 missions, business units, layers, live state including failure, gaps, drift, connections, bottlenecks, counters, and the load row of missions per agent plus open `needs:human`.
7. **Require an explicit operator decision.** Joris must re-approve replacement of an operator-requested view after seeing the parity evidence; feature similarity or lower LOC is insufficient.
8. **Make rollback mechanical.** One reversible PR, no data migration, no deletion of runtime files, and no deployment bundled with the scope decision.
9. **Verify live after deploy.** Confirm named routes, approvals, source timestamps, stale/error states, and links using non-secret metadata. Preserve the old route as a temporary redirect where bookmarks or Telegram deep links may exist.

## Recommended implementation order

1. Open and resolve the transcript-exposure security defect separately: define audience and redaction/fail-closed rendering, test sensitive prose and tool inputs, and get an operator decision on required live detail. Do not bundle route deletion into that fix.
2. Fix provenance/authority labels, especially `/health`, the health graph/dataflow, the operations map, and management exports.
3. Preserve and complete the operator-requested health and #1602 semantics; do not replace either with a table-only approximation.
4. Fold `/settings/<slug>` into dept detail only after its source/provenance presentation has parity.
5. Narrow the home page to decisions plus compact health/error/cost summaries without removing dedicated views.
6. Consider concierge detail, Live-dept onboarding, and standalone management-view folds only after workflow confirmation, parity tests, and explicit approval where operator-requested semantics are affected.

This order addresses the security and source-authority defects first, reduces only proven duplication, and preserves the operator-requested views and human gates that make the fleet useful and safe.
