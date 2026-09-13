# Board and PR intent traceability

Every open cockpit-board card and open Bubble pull request should trace to at
least one non-superseded operator intent. This is an alignment reference and a
leak detector, not an execution gate: missing lineage is flagged for triage and
never blocks legitimate or urgent work.

## Convention and taxonomy

A board card or PR can carry one or several of:

- an `intent:<slug>` label;
- `Serves-intent(s): [[shared/operator-intents/<slug>]]` in its body.

The wikilink is case-exact and extensionless. For example,
`[[shared/operator-intents/live.md]]` is malformed rather than an alias for
`[[shared/operator-intents/live]]`; tools do not silently bless or repair it.

A PR can instead inherit its links from a board card named in an explicit
closing line, for example (closed/non-open referenced cards are read lookup-only
and do not enter the open-card inventory):

```markdown
Closes Bubble-invest/bubble-ops-board#1254
```

A bare `Closes #1254` in another repository means that repository's issue, not
the board card. `Closes`, `Fixes`, and `Resolves` and their common inflections
are recognized.

The taxonomy is **derived from the actual `.md` filenames on the shared wiki's
`main` branch** under `shared/operator-intents/`. README, TEMPLATE, and documents
with `status: superseded` are excluded. Labels therefore follow the live
collection instead of a hard-coded project list. As verified on 2026-09-13, the
live collection contains one intent document, so its current taxonomy is:

```text
intent:system-convergence-north-star
```

Wiki PR #9's proposal artifacts are not live intents and are deliberately not
labels. They enter the taxonomy only if their protected-path content is later
promoted to the actual collection through Joris's review flow.

Links point **to** the write-locked collection. None of the tools here writes
`shared/operator-intents/**`; a new or changed intent still requires Joris's
protected-path PR.

## Creation behavior

`tools/kanban/emit_kanban_item.sh` accepts:

```bash
intent=system-convergence-north-star
intent=first-slug,second-slug
```

Bare slugs, `intent:<slug>`, and canonical operator-intent wikilinks are
normalized. The canonical body line is always present. Existing intent labels
are applied case-insensitively (reusing an existing noncanonically-cased label
rather than attempting a duplicate), but an absent label is only a warning:
the body link is retained and issue creation continues. A `.md`-suffixed link
is left unresolved. When no usable intent is supplied, the emitter
writes `Serves-intent(s): UNRESOLVED`, warns, and still creates or queues the
card. This preserves the fail-open creation rail while making the orphan
visible.

The fallback JSONL queue stores the proposed intents. The queue drain restores
the body links and applies only already-installed labels, so an offline emit
does not lose lineage and an unsynchronised label cannot strand the card.

## Triage-time inventory

Run the structural collector from the deployed framework checkout:

```bash
python3 tools/kanban/intent_alignment_check.py \
  --board Bubble-invest/bubble-ops-board \
  --format json
```

The operational default reads the collection from
`vdk888/bubble-shared-wiki@main` with `gh api`, avoiding a stale local mirror.
`--wiki-root /path/to/shared-wiki` is available for an explicit pinned/offline
checkout or a test fixture; do not use it casually in the live loop. The tool
reads all open board issues
and searches all open PRs owned by both `Bubble-invest` and `vdk888`, deduped by
URL. Repeat `--org` to replace those defaults with explicit owner scopes.
`--limit` defaults to 1000 per query; the report marks coverage as incomplete
if the issue query or either owner query reaches that ceiling.

For each card and PR the JSON reports:

- direct body and label references;
- malformed links plus invalid or superseded targets;
- the active chain reached through `parent`, `parents`, `children`,
  `components`, `dependency`, `dependencies`, `relationships`, or `related`
  frontmatter links;
- a PR's linked open board cards and inherited resolved intents;
- a mechanical `orphan` boolean.

The collector always returns `contradiction: null` and
`judgment: agent_required`. The manager agent reads each item's proposal and
the resolved intent documents. If it judges an **obvious contradiction**, it
surfaces the item as `needs:human` with evidence and a specific question. The
Python tool never keyword-scores or automatically labels a contradiction.

An orphan is likewise a candidate leak, not proof the work is wrong. The loop
may link it to a semantically justified live intent, explain why it remains
unresolved, or raise a new-intent proposal for Joris. It must not default every
orphan to the north-star merely to clear the report.

## Gradual backfill

Backfill is bounded and proposal-only:

```bash
python3 tools/kanban/intent_alignment_check.py \
  --mode backfill --batch-size 5 --backfill-offset 0 --format json
```

The output contains the next orphan cards/PRs, their bodies, the live taxonomy,
empty `suggested_intents`, and `decision: agent_required`. It also preserves the
audit's `coverage` object: a truncated `--limit` warning can never disappear in
backfill mode. It mutates nothing.

The response's `selection.next_offset` advances through a deterministic,
rotating candidate list. The loop persists that integer in its own state and
passes it as the next tick's `--backfill-offset`; the CLI writes no cursor or
state. Once the end is reached the selection wraps, so early unresolved items
stay eligible without starving later cards or PRs. If the candidate set changes,
the offset is safely reduced modulo the current total.
An agent reviews the batch and proposes justified links; board/PR edits remain
separate, deliberate actions under the loop's normal authority rules.

## Label synchronization

Label taxonomy synchronization is also dry-run by default:

```bash
python3 tools/kanban/intent_alignment_check.py --mode labels --format json
```

This lists wanted, existing, missing, noncanonical-casing drift, and
obsolete-but-not-removed intent labels. Matching is case-insensitive, so an
existing `Intent:foo` prevents creation of a duplicate `intent:foo` while still
being reported for cleanup. `--mode labels --apply` is an explicit
operator/deploy action that only creates genuinely missing labels. It does not
remove stale labels, rename casing drift, or touch work items.

## Deployment and verification

Merging this code alone does not change the board, fleet, or loop runtime.
Deployment requires updating the framework checkout used on each relevant
host, re-vendoring the emitter and `emit-kanban-task` skill into departments,
and wiring the separate canonical R&D-loop Phase 2 instruction to run the
inventory. Then verify, without creating production cards first:

1. the live collection produces the expected taxonomy;
2. label sync dry-run shows the intended changes, then an operator applies it;
3. an authorized synthetic emit records the body line and installed label;
4. a forced fallback/replay retains the same intent link;
5. the live inventory sees that card and a linked PR as non-orphans, while a
   fixture/no-op orphan remains flagged and never blocked.
