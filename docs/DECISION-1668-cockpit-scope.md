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

No current route should be deleted from the approved scope alone. The next implementation PR may only remove or fold a named candidate below after its listed preconditions pass.

**Proposed target:**

- **Keep as core:** `/`, `/gate/...`, `/kanban...`, `/pr/...`, `/health`, `/costs`, and the result/evidence subset of `/dept/<slug>`.
- **Keep as low-frequency operations:** `/agents...` and onboarding while a department is not yet Live.
- **Keep, but assign to a business-domain owner rather than the generic cockpit core:** `/dept/<slug>/portfolio`.
- **Propose retiring or folding:** standalone `/settings/<slug>`, standalone `/dept/<slug>/management-view`, live transcript feeds, the decorative/duplicative health graph and operations-map views, concierge detail pages, duplicate home-page panels, and onboarding polling after activation.

Approvals, board comments, gate decisions, structural-PR reviews, attachments, output files, mission files, and provenance timestamps are explicitly excluded from retirement.

## Evidence boundary

This inventory used read-only code, service metadata, filesystem metadata, the opaque-session database's non-secret columns, aggregated access-log counts, and public board comments. It did not print credentials, session ids, raw transcripts, raw access logs, or gate contents.

The usage evidence has important limits:

- The 14-day journal query contained 180 journal lines and 71 HTTP requests, of which 68 were non-static. The actual access records covered only **2026-09-17 through 2026-09-23**, not the full requested 14-day wall-clock interval.
- The 30-day query contained 763 HTTP requests, but access records stopped on **2026-09-23**.
- The console service was restarted on 2026-09-29, while the session database records authenticated activity by Jade on 2026-09-30 and Joris on 2026-10-01. Therefore the journal has a logging blind spot after 2026-09-23. An absent route in the sample is **not** evidence that the route is unused.
- `/dept/<slug>/inbox-fragment`, `/dept/<slug>/session`, and onboarding fragments are automatic polls. Their request counts are not user counts.
- Browser icon probes, login redirects, liveness probes, and static files are not product usage.
- The remaining on-disk gate/decision files are not a complete action history: agents process, archive, and move them. They are useful only to establish that multiple named operators have used gate paths.

## Actual user and consumer inventory

| Identity / consumer | Evidence | Role in the cockpit | Conclusion |
|---|---|---|---|
| **Joris** | Seven session rows; last seen 2026-10-01 11:51 UTC. Public board audit found 42 approvals and 3 rejections attributed as “Joris via cockpit” between 2026-07-02 and 2026-10-01. Remaining dept decision records also contain `joris`/`Joris`. | Primary operator and approver. | Every retained decision path must optimize for Joris and preserve exact audit/evidence links. |
| **Jade** | Four session rows; last seen 2026-09-30 16:36 UTC. Remaining decision records contain `jade`/`Jade`, especially Content and Maya. | Authenticated operator/viewer and department-scoped gate actor. | Do not assume Joris is the only human user. Jade's exact live RBAC grants are secret configuration and were not inspected. |
| `bearer-bootstrap` | Three historical session rows, last seen 2026-09-02. | Legacy/bootstrap identity, not a named human. | Do not use it to claim human adoption. Remove only through a separate auth migration, not this view-scope change. |
| `selftest` | One session row, last seen 2026-09-02. | Test identity. | Exclude from human usage conclusions. |
| API/CI bearer principal | Supported by middleware; access records are not attributable to a username. | Machine access and cutover fallback. | Preserve authentication compatibility until its callers are inventoried separately. |
| Department loops | Gate decision YAML in `inbox/decisions/`; queue and output readers. | Downstream consumers of human gate decisions. | Gate write shape and paths are contracts, not presentation details. |
| Rick's loop | GitHub issue labels/comments/state. | Downstream consumer of kanban decisions and comments. | Board mutation semantics and marker comments are contracts. |
| Structural merge guard | Cockpit-approver App review plus commit status. | Downstream consumer of `/pr/.../approve`. | Structural approval must remain isolated and tied to the exact reviewed SHA. |

The session database proves authenticated identities, not which view each identity used. Current access logging does not include the session username, so route-level attribution to Joris versus Jade is unavailable.

## Observed route sample

These counts are evidence of interaction or rendering, not unique visits or unique users.

| Surface | 30-day journal sample | 14-day journal sample | Interpretation |
|---|---:|---:|---|
| `/` | 65 GET | 5 GET | Repeated operator entry point. Keep, but narrow it to decisions and a compact operational summary. |
| `/dept/<slug>` | 38 GET | 4 GET | Used as an evidence/agent-detail surface. |
| `/dept/<slug>/inbox-fragment` | 118 GET | 1 GET | Mostly 5-second automatic polling from an open dept page. Keep queue data, not this polling rate as a product requirement. |
| `/dept/<slug>/session` | 116 GET | 1 GET | Automatic polling, not 116 intentional transcript reads. Candidate for retirement. |
| `/dept/<slug>/portfolio` | 20 GET | 0 | Real domain-specific usage before the logging blind spot; do not retire as “unused.” |
| `/gate/<slug>/kind/<kind>` | 19 GET | 0 | Gate triage is used. |
| `/gate/<slug>/<id>/decide` | 5 POST | 0 | Human decision operation. Must remain. |
| `/kanban` | 16 GET | 0 | Board view is used. |
| `/kanban/card/<n>/comment` | 2 POST | 0 | Human response path. Must remain. |
| `/pr/<owner>/<repo>/<n>` | 13 GET | 13 GET | Clear use of PR evidence view. |
| `/pr/<owner>/<repo>/<n>/approve` | 10 POST | 10 POST | Clear use of structural approval. Must remain. |
| `/agents/<slug>/onboarding` | 6 GET | 1 GET | Low-frequency but real lifecycle usage. |
| `/agents/setup-callback` | 3 GET | 0 | Eclosure lifecycle operation, not a decorative view. |
| `/agents` | 1 GET | 0 | Low-frequency operations landing page. Absence after 2026-09-23 is unknown. |
| `/health` | 0 observed | 0 observed | Logging blind spot and sparse sample prohibit an unused conclusion. Core scope requires freshness. |
| `/costs` | 0 observed | 0 observed | Logging blind spot and sparse sample prohibit an unused conclusion. Core scope explicitly includes cost. |
| `/settings/<slug>` | 0 observed | 0 observed | No unique operation or unique data; strongest fold/retire candidate. |
| `/concierge/<name>` | 0 observed | 0 observed | Candidate to fold into `/agents`, but only after operator confirmation because the sample is incomplete. |
| `/dept/<slug>/management-view` | 0 observed | 0 observed | Candidate to fold into Tony's dept view after source authority is fixed. |

A separate public-board audit found 45 cockpit decision comments: 42 Joris approvals and 3 Joris rejections. It found no Jade-attributed board decision marker, but that does not contradict Jade's gate usage: board decisions and department gates are separate paths.

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
| Health graph/dataflow (`/health/graph.json`, `/health/dataflow/<slug>.json`) | Registry/dept.yaml-derived architecture plus the same health signals | Interactive organization/data-flow visualization. | Adds presentation and maintenance surface; does not create a stronger authority than the underlying files. | **Propose retire** after the retained table shows source, as-of, stale threshold, and artifact links. Keep `/health/layer/...json` while it backs proof drill-down. |
| Dept detail (`/dept/<slug>`) | No single authority: dept.yaml, queues, outputs, decision files, runtime files, GitHub board snapshot, and domain stores | Large composite page. Some readers use `repo_path`; canonical NAV and some reports use `runtime_repo_path`; Tony's kanban snapshot uses a separate dashboard endpoint with 30-second cache. | Mixed authorities can disagree. The route exposes some timestamps, but not one consistent provenance contract for every panel. | **Keep a narrowed result/evidence page:** current mission result, as-of/source, pending gates/queues, latest output links, and decision history. Fold or remove panels listed below only after parity checks. |
| Output and mission-file routes | Allowlisted files in dept repo | Read-only evidence renderers with containment checks. | Freshness is the file's own date/mtime; no separate store. | **Keep.** These are evidence trails required by the approved scope. |
| Management view (`/dept/<slug>/management-view`) | Latest child Layer-4 `management-export.yaml`; risk files and gates are secondary inputs | Reads children from management dept.yaml, then reads child mirrors under `READ_FROM_DISK`. Canonical location is `outputs/<date>/4/management-export.yaml`, with a legacy root fallback. | Correctly reports `last_seen_at`/staleness, but stale mirrors remain stale data. Tony has no reliable single fresh feed today. | **Propose fold into `/dept/tony`, then retire the standalone route** only after every child has a reliable canonical export and stale/missing provenance is visible. Do not build a replacement state store. |
| Portfolio (`/dept/<slug>/portfolio`) | Investment-owned files/DB: pushed `graph-data.json`, runtime `fund.sqlite`, latest dated report fragments and charts | Domain-specific living report; latest pre-rendered fragments search back seven days and show a stale banner. | 20 observed GETs. Canonical NAV is explicitly the pushed audited morning `graph-data.json`; other tabs may use runtime DB or dated HTML. | **Keep, but classify as an investment-domain product owned by Ben, not generic cockpit core.** Its future can be decided by that domain, not by a fleet-console LOC target. |
| Operations map (`/dept/tony/operations-fragment`) | Dated operations map loaded from existing files | Auto-refreshes every 60 seconds. | Presentation duplicates architecture/mission information available elsewhere. | **Propose retire/fold** with the health graph; keep only dated operational facts needed for a decision. |

### 3. Cost

| Surface | Authority | Freshness | Observed live source | Proposal |
|---|---|---|---|---|
| `/costs` and `/costs.json` | Mirrored Claude/Hermes session JSONLs under `BUBBLE_COST_PROJECTS_DIR`; public/model price table; configured weekly envelopes | Per-session parses cached by file mtime; assembled report TTL is 45 seconds. It is an estimate for trend/relative use, not billing. A heartbeat-to-session invariant flags missing transcript coverage. | On 2026-10-01 12:19 UTC the source held 6,175 JSONLs, 549 modified in the prior seven days, and the newest mtime was 2026-10-01 12:07 UTC. | **Keep.** Add/retain source age and “estimate, not billing” labels. Do not create a second cost store. |

### 4. Team and lifecycle

| Surface | Authority | Freshness / behavior | Proposal |
|---|---|---|---|
| `/agents` | `onboarding/STATE.yaml`, known concierge registry, and service status | Direct filesystem reads; concierge service status cached 30 seconds. | **Keep as low-frequency Operations**, outside the primary daily decision flow. |
| `/agents/new`, activation, cancel, retire | Bootstrap/eclosure scripts, one-shot GitHub App setup state, activation PR, dry-run retirement | These are operations. Retirement remains preview-only and actual privileged execution stays outside the console. | **Keep.** Do not treat POST routes as removable views. Preserve preview/confirm boundaries. |
| `/agents/<slug>/onboarding` and fragments | `STATE.yaml`, dept draft/config, repo artifacts, chat log, latest git commit | Timeline and heartbeat poll every five seconds; STATE is read fresh. | **Keep only while status is not Live.** After activation, propose redirecting to `/dept/<slug>` and removing routine polling; retain historical artifacts through allowlisted evidence links. |
| `/settings/<slug>` | `dept.yaml` gate policies and config | Read-only; no unique write path; same source already loaded by `/dept/<slug>`. | **Propose fold into a collapsed “configuration source” section on `/dept/<slug>` and retire the standalone route.** Preserve exact source link and raw/allowlisted mission-file access. |
| `/concierge/<name>` | Explicit concierge registry; systemd status; project `STATUS.md`; newest session JSONL | Service status cached 30 seconds; session fragment polls live transcript. | **Propose fold status + latest project/result into `/agents`; retire the dedicated page after Joris/Jade confirm no unique daily workflow.** |
| `/concierge/<name>/session` and `/dept/<slug>/session` | Newest mirrored session JSONL | Reads and renders the last 30 turns; dept page polls every five seconds. It is a sensitive, implementation-level stream, and the polling dominates observed route counts. | **Propose retire.** Replace with last-activity timestamp plus mission result/proof links already present elsewhere. Do not remove raw transcripts from their audit/storage lifecycle. |

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

- `/login`, `/logout`, and authenticated-session attribution.
- `/health-noauth` as a machine liveness probe.
- `/` as the primary human decision inbox, narrowed rather than replaced.
- All `/gate/...` reads, decisions, undo semantics, attachments, and payload evidence.
- All `/kanban...` board reads, comments, and decision mutations.
- All `/pr/...` evidence and App-signed approval semantics.
- `/health` freshness table and `/health/layer/...json` proof drill-down.
- `/costs` and `/costs.json`.
- `/dept/<slug>` result/evidence core plus output and mission-file readers.
- `/agents...` lifecycle operations, including activation/cancel/retire safety boundaries.
- `/dept/<slug>/portfolio` as a separately owned investment-domain surface.

### Fold, then retire the standalone route or panel

1. **`/settings/<slug>`** → fold read-only config/gate-policy provenance into `/dept/<slug>`.
2. **`/dept/<slug>/management-view`** → fold the reliable subset into `/dept/tony` after canonical exports are current and provenance-complete.
3. **`/concierge/<name>`** → fold status, last activity, and latest project/result into `/agents` after human confirmation.
4. **Home duplicates** → remove duplicate team roster, full kanban groupings, full cost tables, concierge details, and domain report content from `/`; keep counts/errors and links.
5. **Onboarding for Live departments** → redirect to dept detail; keep onboarding only for Idea through Ready-to-activate states.

### Retire after parity/precondition checks

1. **`/dept/<slug>/session` and `/concierge/<name>/session`** live transcript panels.
2. **`/health/graph.json` and `/health/dataflow/<slug>.json`** when the interactive graph is removed and the retained table carries equivalent source/as-of/error information.
3. **`/dept/tony/operations-fragment`** when any decision-relevant dated facts are represented on the retained dept/health surfaces.
4. Five-second background polling for dept inbox/session and onboarding fragments where no active lifecycle operation requires it. Keep manual refresh or a slower visibility-aware refresh; do not add a new state or event store.

## Preconditions for any retirement PR

A future implementation PR must satisfy all of the following for each named route/panel:

1. **Name the user and downstream consumer.** Confirm with Joris and Jade when the telemetry cannot attribute route use.
2. **Name the authority.** Document the exact existing file/API/database and whether the cockpit reads runtime state or a mirror.
3. **Show provenance in the retained surface.** Source, as-of time, and stale/missing state must be visible, not inferred.
4. **Preserve operations and audit.** No change to gate decisions, board comments/labels, PR approvals, RBAC, decision signatures, attachments, output evidence, or mission-file evidence.
5. **No new state store.** Fold views onto existing authorities; do not create a database to mask stale mirrors.
6. **Prove parity.** Add route/template tests for retained information and security boundaries before deleting code.
7. **Make rollback mechanical.** One reversible PR, no data migration, no deletion of runtime files, and no deployment bundled with the scope decision.
8. **Verify live after deploy.** Confirm named routes, approvals, source timestamps, stale/error states, and links using non-secret metadata. Preserve the old route as a temporary redirect where bookmarks or Telegram deep links may exist.

## Recommended implementation order

1. Fix provenance/authority labels first, especially `/health` and management exports.
2. Fold `/settings/<slug>` into dept detail; it has no unique operation or source.
3. Stop rendering/polling live transcripts while preserving last-activity and evidence links.
4. Narrow the home page to decisions plus compact health/error/cost summaries.
5. Fold concierge detail and Live-dept onboarding into `/agents` and `/dept` respectively.
6. Only then remove graph/operations-map and standalone management-view code, after source parity is demonstrated.

This order reduces paths and polling without touching the human gates that make the fleet safe.
