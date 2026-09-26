# wiki-search — fleet-standard search over the shared wiki

Board `Bubble-invest/bubble-ops-board#1505`, step 2. Promotes the step-1
research (`~/claude-workspaces/Rick_RnD/prototypes/jev-local/wiki-rerank/`,
`RESULTS_WIKI_RERANK_V2.md`) from a Rick-Mac-only prototype to a fleet
standard: every dept gets this tool vendored (`scripts/vendor-dept-libs.sh`)
and can search its own copy of `~/.claude/agent-memory/shared-wiki` with an
optional cheap Jev rerank pass, without needing Ollama, an API key at hand,
or any per-dept setup.

**Step 2 of the rollout ships the tool only** — it is not yet wired into
any layer template, dept CLAUDE.md, or SKILL.md instruction telling agents
to actually *use* it in their own reasoning loop. That's step 3, a separate
PR, per the board card.

## Usage

```bash
# Default: auto-detect candidate source, rerank pool=5, top 5 results
python3 tools/wiki-search/search.py "how does the wiki sync to VPS depts"

# Force a candidate source
python3 tools/wiki-search/search.py --candidate-source fts_only "..."
python3 tools/wiki-search/search.py --candidate-source hybrid   "..."   # Rick's Mac only

# Skip the rerank pass (plain candidate-source top-N)
python3 tools/wiki-search/search.py --no-rerank "..."

# JSON output (for another script/agent to consume)
python3 tools/wiki-search/search.py --json "..."

# Force a full index rebuild of the active candidate source
python3 tools/wiki-search/search.py --reindex
```

## Candidate sources (auto-detected)

| source | dependency | when it's picked |
|---|---|---|
| `hybrid` | local Ollama daemon + `nomic-embed-text` pulled | `--candidate-source auto` (default) probes `hybrid_index.ollama_available()` — a 0.5s-timeout localhost check — and uses this if it succeeds. Today that's **Rick's Mac only**: confirmed no `ollama.service` on the VPS (2026-09-27 probe). |
| `fts_only` | none — pure SQLite FTS5, built directly from the wiki markdown | Every other host: every VPS dept, and any other Mac without Ollama. Builds in ~0.08–0.2s and ~4.7MB for the whole wiki; auto-rebuilds only when the wiki's on-disk signature (file count + max mtime) changes, not on every call. |

Both sources return the identical `(score, path, title, description, domain)`
tuple shape, so everything downstream (rerank, output formatting) is
candidate-source-agnostic.

## Rerank (on by default)

Validated on a **fresh 130-row held-out gold set never touched during
tuning** (`RESULTS_WIKI_RERANK_V2.md`): candidate pool **5**, `choice`
design, `outline_snip` state variant, threshold `0.15` — pool=5 beats
pool=10 on **every** accuracy metric (P@1 .900 vs .885, P@3 .879 vs .862,
hit@3 .923 vs .915, MRR .912 vs .899) *and* saves more tokens (58.8% vs
33.7%) *and* has the cleanest no-answer behavior (0/10 no-good-page test
queries returned a page). `fts_only` tracks `hybrid` within 1.5–2.3 points
on every headline metric at pool=5 — close enough to ship as the
dependency-light default.

Use `--no-rerank` to disable it entirely (plain candidate-source top-N,
byte-for-byte what the candidate source itself would return).

**Hard fallback, always:** if reranking is on but no OpenRouter key is
available, the Jev call errors/times out, or every candidate scores below
`--rerank-threshold`, `search.py` prints one warning line to stderr and
returns the plain candidate-source top-N unchanged. A real query with a
non-empty candidate pool never comes back empty and never crashes because
of the rerank layer — see `tests/test_wiki_search_rerank_fallback.py`.

## Jev backend + key

Reuses `skills/system-one-decisions/scripts/jev.py` for the actual
OpenRouter call, auth headers, spend cap, and decision-receipt shape —
nothing here reimplements that wire format (fleet doctrine: verify the
standard, don't duplicate). Key resolution is *exactly* jev.py's own:
`JEV_OPENROUTER_API_KEY` / `OPENROUTER_API_KEY` from the environment only.
Every VPS dept already has `JEV_OPENROUTER_API_KEY` provisioned into its
systemd env (the fleet-dedicated "fleet-jev" key, board #1505). On a Mac
without that systemd provisioning (Rick's own), `jev_bridge.py` additionally
loads `~/.config/jev/openrouter.key` into the process env *only if neither
var is already set* — this populates the exact same env var jev.py already
reads, it does not add a new resolution path.

## Receipts

One JSONL row per reranked query, matching jev.py `ask`'s own receipt shape
(`skills/system-one-decisions/references/contract.md` §5 — the fleet's
receipts contract): `state_digest` (not raw state), `question_set_version`,
`backend`, `model`, `ok`, `cost_usd`, `latency_s`/`latency_ms`, `timestamp`.
Default path: `~/.claude/agent-memory/.wiki-search-cache/receipts.jsonl`
(override with `--receipts PATH`, or `--receipts ''` to disable).

## Internal data only

The shared wiki is Bubble Invest's own internal fleet-operations knowledge
base. Per Joris's data-residency rule (Telegram msg 9715, 2026-09-25,
condensed in `skills/system-one-decisions/SKILL.md` "Backend choice"),
reading/reranking it via the OpenRouter Jev backend is cleared. **Never**
point this tool at an external-client wiki/knowledge base without
re-checking that rule first — the rule widens the door for Bubble's own
internal data, nothing else.

## Files

| file | role |
|---|---|
| `wiki_paths.py` | host-generic path resolution (`$HOME/.claude/agent-memory/shared-wiki`, cache dir) shared by every module below; both overridable via env vars for testing. |
| `fts_index.py` | dependency-light FTS5-only indexer + searcher, cached + auto-rebuilt on wiki change. |
| `hybrid_index.py` | Ollama + FTS5 hybrid indexer + searcher, ported from Rick's Mac-only `tools/wiki/wiki_search.py` prototype (untouched, still Rick's own tool). |
| `context_builder.py` | builds the compact per-candidate state (title/frontmatter/outline/snippets) fed to the reranker. |
| `rerank.py` | the two Jev question designs (`noul`, `choice`) and threshold filtering. |
| `jev_bridge.py` | thin wrapper reusing `skills/system-one-decisions/scripts/jev.py` for the actual call + receipts. |
| `search.py` | the CLI entry point: candidate-source auto-detect, rerank-or-fallback, output formatting. |

## Vendoring

Vendored into every dept by `scripts/vendor-dept-libs.sh`'s `WIKI_SEARCH_MAP`
at every service start, alongside `skills/system-one-decisions` (same
mechanism, same boot-time re-vendor discipline as every other shared
framework lib — see that script's own header comment for why).

## Provenance

Ported from `~/claude-workspaces/Rick_RnD/prototypes/jev-local/wiki-rerank/`
(board #1505 step 1): `scripts/fts_only_index.py`, `scripts/wiki_search_jev.py`,
`scripts/context_builder.py`, `scripts/rerank.py`, `scripts/jev_client.py`,
and their `tests/`. Read `RESULTS_WIKI_RERANK.md` and
`RESULTS_WIKI_RERANK_V2.md` there for the full experimental trail behind
every default in this tool.
