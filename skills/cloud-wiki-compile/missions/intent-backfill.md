# Intent provenance backfill mission

Run this mission during every nightly COMPILE, after the knowledge write-back
and before the index is regenerated. It is deliberately gradual: inspect at
most **5 candidate pages per run** from
`/home/claude/monitoring/wiki-intent-audit/latest.json`.

The JSON is structural evidence only. A missing or malformed field makes a page
a candidate for review; it does not prove the page is waste or identify the
right intent. Never decide relevance with filenames, keywords, regexes, or the
Python collector.

## Contract

- One intent:
  `intent: "[[shared/operator-intents/<slug>]]"`
- More than one intent: a YAML block list, one quoted wikilink per item.
- Missing `intent`, `intent:`, and `intent: []` are allowed only as transitional
  unresolved states. They remain visible in the next audit.
- Every non-empty target resolves case-exactly to an existing `.md` below
  `shared/operator-intents/`. Omit `.md`; do not use aliases or headings.
- A link to an intent whose frontmatter says `status: superseded` remains an
  unresolved candidate. Read the page and current intent collection to choose a
  supported mapping; never infer the replacement from filenames or links.
- Never assign the north-star, or any other intent, as a blanket default.
- Never create, edit, rename, or delete anything below
  `shared/operator-intents/`. Pages with `core: true` are also read-only; if one
  needs provenance metadata, surface it for human handling.

## Per-page judgment

For each of the first 5 candidates, in report order:

1. Read the whole candidate page and the current operator-intent pages
   read-only. Follow the page's useful body links or source references when
   needed to understand why the page exists.
2. Decide semantically whether the evidence supports one or more existing
   intents. The mapping must be explainable from the page's purpose; shared
   vocabulary alone is not evidence.
3. If supported, edit only the ordinary page's frontmatter. Preserve every
   other field and all body content. Replace a malformed/outdated `intent`
   value with the canonical scalar or block-list form.
4. If the page is ambiguous, points to no current intent, or is read-only, leave
   it unchanged. This is a **candidate leak**, not a proven leak. Surface the
   path, structural issue, evidence considered, and the exact semantic question
   that remains.

## Surfacing unresolved candidate leaks

For each unresolved page in this bounded batch, search the board's open and
closed issues for the exact wiki path. If an issue already covers the same
unresolved mapping, do not duplicate it. Otherwise emit one finding:

```bash
EMIT=/home/claude/bubble-ops-loop/tools/kanban/emit_kanban_item.sh
"$EMIT" task=wiki-intent-candidate-leak \
  title="candidate intent leak: <relative wiki path>" \
  body="Structural evidence: <issues from audit>. Semantic review: <what was read and why no supported mapping is clear>. Question: which existing intent does this page serve, or should the page be retired? The compiler did not edit shared/operator-intents/**." \
  type=findings owner=rnd priority=normal budget=1
```

The board finding is the loop-visible signal. Do not emit a finding merely
because Python reported a missing field; emit only after this reading judgment
cannot establish a supported mapping. Return one compact summary:

`intent_backfill: reviewed=N tagged=T unresolved=U already_carded=D remaining=R`

After the batch, re-run the structural audit so `latest.json` reflects the
frontmatter just written. `remaining` is the new report's candidate count.
