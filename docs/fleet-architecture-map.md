# Living fleet architecture map

`tools/fleet_architecture.py` inventories safe architecture metadata for every
agent in `fleet/fleet-architecture-sources.yaml` and renders the shared wiki map
requested by board card #1249.

## Refresh

The cloud wiki compiler runs:

```bash
python3 /home/claude/bubble-ops-loop/tools/fleet_architecture.py refresh \
  --config /home/claude/bubble-ops-loop/fleet/fleet-architecture-sources.yaml \
  --wiki-root "$WIKI"
```

The command writes only these managed outputs:

- `shared/systems/fleet-architecture-map.md`
- `shared/systems/fleet-architecture-inventory.json`
- `shared/fleet-architecture/<agent>/<kind>/<item>.md`

It refuses path traversal and any write below `shared/operator-intents/`. It
validates the entire in-memory inventory against
`fleet/fleet-architecture-inventory.schema.json` and validates every explicit
intent link before replacing managed files. A nonzero exit is a compile failure,
so the systemd job alerts instead of publishing a plausible but partial map.

## Evidence perimeter

The scanner reads only architecture surfaces below configured local roots:

- immediate names in `skills/`, `.claude/skills/`, `tools/`, and `integrations/`;
- mission filenames below `missions/`;
- selected structural keys from `dept.yaml` (mission IDs/cadence, declared
  skill/tool/integration names, and hierarchy links);
- cron names/schedules from `config/crons.yaml`, plus systemd timer and root
  launchd plist labels.

It does not read transcripts, source-code bodies, prompts, tool output, secrets,
client data, holdings, trades, or broker configuration. Ben's `dept.yaml` is
disabled in the source manifest; only safe structural directory/file names are
eligible for that agent.

The collector never opens SSH connections or calls GitHub. The existing
`mac-transcript-sync` launchd job runs `scan-visible` locally on M4/M1/M5 and
pushes the resulting JSON through its existing Mac→VPS rsync connection to:

```text
/home/claude/.claude/projects/_fleet-architecture-fragments/<joris-m4|jade-m1|jade-m5>/
```

`refresh` discovers that directory from the source manifest automatically.
Fragments carry an exact configured host identity, retain their original
observation time, and become stale after 36 hours. The Mac sync installer copies
the collector/config beside the installed sync script; merge/deploy this repo
before reinstalling `vdk888/bubble-rnd-workspace`'s existing sync job. No new
daemon, SSH key, credential, or reverse VPS→Mac trust is added.

When a configured source is unavailable, previous facts remain with `stale`
status. With no prior facts, categories are `unknown`; an empty or wrong root is
not evidence of an empty installed architecture. Items missing from a verified
surface remain as `removed` observations with the time they disappeared.

## Intent contract

Intent is judgment, not filename classification. The collector accepts only:

1. a bounded `intent_bindings` judgment in the source manifest, with a written
   `why` rationale;
2. an explicit `intent` field in source frontmatter/structured metadata; or
3. a reviewed `intent` plus `intent_rationale` already present in the generated
   item note.

Accepted values are exact, case-sensitive Obsidian links to an existing file
under `shared/operator-intents/`, for example:

```yaml
intent: "[[shared/operator-intents/example-intent]]"
```

or:

```yaml
intent:
  - "[[shared/operator-intents/example-intent]]"
```

Missing links stay `intent: []` and render as **unresolved intent**. The Python
collector does not choose a nearest intent, create placeholders, use aliases, or
assign a blanket north-star. The wiki compiler's agentic intent pass reviews
those unresolved notes separately.

## Host fragments

`scan-root` prints one agent object without writing locally. This is suitable for
a read-only remote shell invocation or a future host-sync job:

```bash
python3 tools/fleet_architecture.py scan-root \
  --agent-id maya --name Maya --role "Prospection and LinkedIn outbound" \
  --host VPS --wiki-folder maya_sales --root /srv/agents/maya
```

Pass the resulting JSON file to `refresh --fragment PATH` for a one-off run.
Recurring Mac fragments use `scan-visible` and automatic discovery as described
above.

Generated notes use a bounded managed block. Refresh preserves unknown
frontmatter and all body text outside that block. A foreign-owner or `core: true`
collision is left untouched and fails the refresh. Cleanup considers only files
carrying both `owner: fleet-architecture-collector` and
`type: fleet-architecture-item`; `--check` reports those planned removals.
