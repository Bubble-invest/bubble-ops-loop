# Vendored CLAUDE.md doctrine snippets (single source — #1318)

Fleet doctrine that must appear in **every** agent's `CLAUDE.md` lives here, **once**.
Each `.md` file in this directory is a canonical snippet; `scripts/vendor_claude_md.py`
renders it into a delimited, machine-managed block inside a target `CLAUDE.md`.

This closes the drift trap (#917 / #1314): the directive text still physically appears
in each agent's `CLAUDE.md` (an agent reads its own `CLAUDE.md` at session start — there
is no include mechanism), but no one hand-edits those copies. They are regenerated from
the one canonical file, exactly like the on-disk vendored libs (`vendor-dept-libs.sh`):
many copies on disk, one source of truth, drift self-caught.

## Snippets

| snippet | goes into | content |
|---|---|---|
| `operator-alignment.md` | **every** agent CLAUDE.md | the 4 approved operator-intent directives (consult-intent, reuse-access, simplify, fleet-standard). Approved Joris 2026-09-14 (#1318, #1326, #1327, #1328). |
| `session-start-reads.md` | agents missing a wiki-read block (Géraldine, Ellie) | the standard session-start wiki + memory read block. Not applied where a block already exists (directive #4: never duplicate). |

The authoritative target list is `fleet/claude-md-targets.yaml`.

## Usage

```bash
# apply a snippet to one CLAUDE.md (idempotent)
scripts/vendor_claude_md.py apply --snippet operator-alignment /path/to/CLAUDE.md

# multiple snippets at once
scripts/vendor_claude_md.py apply --snippet operator-alignment,session-start-reads /path/to/CLAUDE.md

# drift check (read-only; non-zero exit if a deployed block was reverted/edited)
scripts/vendor_claude_md.py check --snippet operator-alignment /path/to/CLAUDE.md

# whole-fleet, on a host that has the live clones checked out
scripts/vendor_claude_md.py check --manifest fleet/claude-md-targets.yaml --root /srv/agents
```

## Landing a change

1. Edit the canonical snippet here (single source).
2. Re-run the installer against each target repo and open a per-repo PR
   (each agent's `CLAUDE.md` is committed in its own repo).
3. New depts inherit `operator-alignment` automatically — `scaffold.render_claude_md_operating()`
   appends it from this same source at activation.

Global machine-level `~/.claude/CLAUDE.md` files are personal to each machine's owner and
are **not** vendored here (owner's call).
