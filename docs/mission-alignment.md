# Mission alignment

Recurring missions may declare these optional fields in `dept.yaml` or a
standalone mission YAML:

```yaml
business_unit: steering  # fund | ai_methods | pro_clients | steering (or a list of these, for multi-BU missions)
serves_intents: [operator-intent-slug]
floor: measure          # distribute | package | produce | measure | steer | support
```

Intent slugs are filenames without `.md` in the shared wiki's
`shared/operator-intents/`. Tony owns the actual mappings. No department mapping
is supplied by this framework. `floor` describes the business pipeline stage;
it does not change the mission's execution `layer` or cadence.

Run the read-only report with:

```sh
python3 scripts/mission_alignment_lint.py --depts-root /path/to/depts \
  --intents-dir /path/to/wiki/shared/operator-intents --json
```

The CLI recursively reads `dept.yaml` files. Without `--intents-dir`, it uses
`OPERATOR_INTENTS_DIR`, defaulting to
`~/.claude/agent-memory/shared-wiki/shared/operator-intents`. The console uses
the same environment setting and the same lint logic, with its existing
manifest/department readers and `GH_CACHE_TTL_SECONDS` cache duration. No new
credentials or remote reader are introduced.

A mission is mapped when it has a valid business unit and a nonempty list of
known intent slugs (and a valid floor, if supplied). Missing either mapping
field means unmapped. Unknown-intent counts count affected missions, not slugs;
multiple diagnostics may apply to one mission. Invalid metadata and unreadable
manifests are reported separately. An unavailable intent catalog produces an
explicit unverified state instead of treating all references as unknown or
claiming that they are mapped. An empty, readable catalog has no known intents.

Report findings always return exit code zero. The report is not invoked by the
scheduler and cannot gate mission execution. Schema validation allows legacy
missions with all alignment fields absent.

The `/kanban` landing card links to the canonical operations artifact in a new
tab so operator comments remain there. It also shows total and per-department
alignment counts. This is a structural cockpit change requiring cockpit
approval before merge; it introduces no write actions.
