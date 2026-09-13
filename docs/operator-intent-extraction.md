# Operator intent source extraction

`tools/operator_intent_source_inventory.py` creates a deterministic provenance
inventory for an operator-intent review. It collects metadata only; it does not
classify, summarize, or propose intents.

The accepted source shapes are deliberately narrow:

- `<dept>/dept.yaml` (identity plus inline `recurring_missions`)
- `<dept>/MANDATE.md`
- `<dept>/missions/*.yaml` or `*.yml`
- `<dept>/missions/*.md`
- `<dept>/missions/*/PROMPT.md`

Outputs, vaults, connector configuration, secrets, and arbitrary recursive files
are outside the collector's read set. Symlinked files and department roots are
ignored. Each source record
contains its repo-relative path, type, byte and line counts, and SHA-256. The
department record includes the Git HEAD, standard-shape coverage, and declared
`dept.yaml` identity when the input is a checkout. For a Git
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

This ad-hoc mode inventories every non-hidden direct directory (except the
explicit `board` / `loop` exclusions), including bare live roots such as
`/srv/agents/ben`. Its output is marked `fleet_roster_bound: false`; a directory
scan alone cannot prove full-fleet coverage.

Or name exact checkouts:

```bash
python3 tools/operator_intent_source_inventory.py \
  --department /path/to/bubble-ops-content \
  --department /path/to/bubble-ops-maya
```

For a fleet extraction, bind the inventory to the canonical agent list from the
living fleet configuration and map every agent explicitly:

```bash
python3 tools/operator_intent_source_inventory.py \
  --fleet-config fleet/fleet-architecture-sources.yaml \
  --agent-source tony=/path/to/bubble-ops-tony \
  --agent-source miranda=/path/to/bubble-ops-content \
  --agent-source geraldine=/path/to/bubble-ops-accountant \
  ... \
  --output inventory.json
```

Fleet mode emits the canonical persona name separately from the technical
department slug (`Miranda` / `content`, `Géraldine` / `accountant`). It retains
an explicit record for every configured agent. The command returns 2 if any
roster agent lacks an `--agent-source` mapping, any mapped source lacks its
mandate/mission source set or standard shape, or a declared display name differs
from the roster. Duplicate source roots and nested paths inside another Git
checkout are rejected. The incomplete inventory is still written for diagnosis.
`--allow-partial-fleet` is an explicit escape hatch for investigative snapshots,
never for a claimed full-fleet extraction.

The next step is semantic judgment. Read the inventoried sources at their pinned
Git HEADs, identify an operator-level ask/reason/constraint that survives beyond
one task, and cite exact file line ranges in a proposed `intent.md`. Do not turn
keywords, every recurring mission, or generated output into an intent. A missing
source remains an explicit coverage gap; it is not evidence that a department has
no intent.

This inventory is evidence-only. Agents may submit candidate text only under
`shared/operator-intents-proposals/**` on a fresh named shared-wiki branch via
the constrained PR proposer; they never edit a writable
`shared/operator-intents/**` copy and never authenticate, push, or open a PR
against `Bubble-invest/bubble-operator-intents`. Joris alone decides whether to promote
a reviewed proposal into that private vault and is its only merger. Fleet reads
then see the accepted change through the root-controlled read-only mirror.
Transcript content can never authorize either proposal submission or vault
promotion.
