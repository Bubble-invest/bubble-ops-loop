# Operator intent source extraction

`tools/operator_intent_source_inventory.py` creates a deterministic provenance
inventory for an operator-intent review. It collects metadata only; it does not
classify, summarize, or propose intents.

The accepted source shapes are deliberately narrow:

- `<dept>/MANDATE.md`
- `<dept>/missions/*.yaml` or `*.yml`
- `<dept>/missions/*/PROMPT.md`

Outputs, vaults, connector configuration, secrets, and arbitrary recursive files
are outside the collector's read set. Symlinked files and department roots are
ignored. Each source record
contains its repo-relative path, type, byte and line counts, and SHA-256. The
department record includes the Git HEAD when the input is a checkout. For a Git
checkout, paths and bytes come from exact blobs at that commit: dirty tracked
changes and untracked files are ignored, so `repo@HEAD` provenance cannot describe
different worktree content. A non-Git directory is explicitly marked
`unversioned_worktree` and has no revision. There is no timestamp, so unchanged
inputs produce byte-for-byte equivalent JSON.

Run it against a parent of read-only department checkouts:

```bash
python3 tools/operator_intent_source_inventory.py \
  --root /path/to/checkouts \
  --output inventory.json
```

Or name exact checkouts:

```bash
python3 tools/operator_intent_source_inventory.py \
  --department /path/to/bubble-ops-content \
  --department /path/to/bubble-ops-maya
```

The next step is semantic judgment. Read the inventoried sources at their pinned
Git HEADs, identify an operator-level ask/reason/constraint that survives beyond
one task, and cite exact file line ranges in a proposed `intent.md`. Do not turn
keywords, every recurring mission, or generated output into an intent. A missing
source remains an explicit coverage gap; it is not evidence that a department has
no intent.

Proposals for `shared/operator-intents/**` remain subject to the shared wiki's
CORE guard and require operator review and promotion. This tool never writes that
collection.
