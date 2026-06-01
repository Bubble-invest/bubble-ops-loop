# Step 9 — Schema Relaxation v3.1 (Layer 4 drift accommodation)

**Date:** 2026-05-20
**Operator:** Rick (R&D)
**Trigger:** {{OPERATOR}} Telegram msg 2676 — *"Ok pour la step 9 tu peux assouplir."*
**Verdict:** ALL-GREEN

---

## TL;DR

The first manual Layer 4 run on `bubble-ops-fixture` (2026-05-20) produced a
`management-export.yaml` that drifted on two points vs the v3 schema:

1. `needs_management_attention[]` items as **strings** (not objects)
2. `links.*` containing **extra keys** beyond risk_kpis/risk_brief/gates

{{OPERATOR}} approved relaxing both. v3.1 of the schema accepts both shapes; root
remains strict (no smuggling).

## Changes

### `schemas-draft/management-export.schema.yaml`

| Before (v3) | After (v3.1) |
|---|---|
| `needs_management_attention.items: type: object, required: [id, kind, priority, summary]` | `items: oneOf: [type: string \| object {id,kind,priority,summary}]` |
| `links: additionalProperties: false` (only risk_kpis/risk_brief/gates) | `links: additionalProperties: {type: string, minLength: 1}` (named extras allowed; values must be non-empty strings) |
| Root: `additionalProperties: false` | **Unchanged** (root stays strict) |

### Fixtures added

- `schemas-draft/examples/management-export-relaxed-strings-and-extra-links.yaml`
  — exercises both relaxations together (the literal shape Layer 4 emitted).
- `schemas-draft/tests/negative/management-export-string-attention-with-root-leak.yaml`
  — pins the invariant: relaxation doesn't open the root-level back door.

## RED → GREEN

```
# RED (before edit)
[FAIL] examples/management-export-relaxed-strings-and-extra-links.yaml
       — expected PASS but got errors:
       ['links']: Additional properties not allowed
       ('improvements', 'layer1_summary', 'layer2_summary', 'open_gate')
       ['needs_management_attention', 0..2]: not of type 'object'

# GREEN (after edit)
OK — 14 positive + 10 negative checks all matched expectations.
```

## Round-trip Step 9 test (the real test)

```
$ python3 -m pytest tests/round-trip/test_layer4_three_outputs.py -v
============== 10 passed, 1 skipped in 1.82s ==============
```

All 10 hard assertions GREEN, including
`test_management_export_validates_against_schema`. Step 9 is now closed.

## Regression summary

| Suite | Before | After |
|---|---|---|
| schemas-draft (validate_all.py) | 13+9 PASS | 14+10 PASS |
| tests/round-trip + others | 1 fail (skeleton) + 1 fail (L4 schema) | 1 fail (skeleton, pre-existing, unrelated) |
| token-broker | 77 PASS | 77 PASS |
| git-guard | 73 PASS | 73 PASS |

## Why this is safe

The relaxation is **monotonic** — every payload that validated under v3 still
validates under v3.1 (the object form of `needs_management_attention` and the
canonical `links` triplet remain valid). v3.1 only **adds** acceptable shapes.

The console's renderer needs one small adapter (already noted as UX-3 follow-up
ticket): when a `needs_management_attention` item is a string, render it as
`{kind: note, priority: medium, summary: <the string>}`. The pinned negative
test guarantees no dept can smuggle ad-hoc root fields past Tony's import
boundary regardless.
