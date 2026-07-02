# deploy/telegram-plugin — /loop boot re-arm source-of-truth

The telegram channel plugin re-arms a dept's `/loop` on poller startup by
injecting ONE synthetic "boot" turn straight into Claude via the same MCP
channel notification a real inbound message uses — bypassing Telegram entirely
(a bot's own outbound message never returns as an inbound update, which is why
the retired `bubble-loop-reinit.sh` could not do this).

The plugin lives in a VOLATILE cache that is re-extracted on plugin update:

    /home/claude/.claude/plugins/cache/claude-plugins-official/telegram/<ver>/

so the source-of-truth is tracked HERE and re-applied by
`scripts/install-boot-rearm.sh` after every deploy / plugin update.

## Files

| File | Role |
|------|------|
| `boot_rearm.ts` | Exact copy of the plugin's `boot_rearm.ts`. Exports `bootRearmNotification(env)` → notification payload when `OPS_LOOP_BOOT_REARM==="1"`, else `null`. Reads `OPS_LOOP_BOOT_REARM` + `OPS_LOOP_DEPT`. |
| `server.ts.boot-rearm.patch` | Unified diff (`patch -p0`) that wires `server.ts`: adds the `boot_rearm.ts` import + a `bootRearmFired` flag, and calls `bootRearmNotification` at the END of `bot.start()`'s `onStart` callback (after `setMyCommands(...)`). |

## Install / re-apply

    bash scripts/install-boot-rearm.sh            # idempotent
    bash scripts/install-boot-rearm.sh --dry-run  # show what it would do

Then restart the dept services (Rick controls restarts) so the patched plugin
code is loaded.

## Regenerating the patch (when the plugin's server.ts drifts)

If `install-boot-rearm.sh` exits 3 ("patch does NOT apply"), the plugin's
`server.ts` changed across a version bump. Rebuild the patch:

1. Copy the NEW pristine `server.ts` from the plugin cache to `pristine.ts`.
2. Copy it again to `patched.ts` and apply the 3 edits (see the patch for the
   exact text):
   - after `import { join, extname, sep } from 'path'`:
     `import { bootRearmNotification } from './boot_rearm.ts'` + `let bootRearmFired = false`
   - at the END of the `onStart` callback (after `setMyCommands(...).catch(() => {})`):
     the `if (!bootRearmFired) { ... }` block that calls `mcp.notification(rearm)`.
3. `diff -u --label server.ts --label server.ts pristine.ts patched.ts > server.ts.boot-rearm.patch`
4. Verify: `cp pristine.ts X/server.ts && cp boot_rearm.ts X/ && (cd X && patch -p0 --dry-run < server.ts.boot-rearm.patch)`
5. Validate the build from WITHIN the plugin dir (so deps resolve):
   `bun build server.ts --target=node --outdir=/tmp/check` must exit 0.
6. `bash tests/test_boot_rearm_install.sh` must stay green.

## Tests

    bash tests/test_boot_rearm_install.sh

## Note — the LIVE boot-rearm path is the inject file, not this MCP notification

The `bootRearmNotification`/`server.ts.boot-rearm.patch` mechanism documented
above fires too early (during session init) to reliably land, per the comment
at `deploy/templates/ops-loop-dept.service.template` (search "MCP notification
in onStart fires too early"). The mechanism actually in production is a second
`ExecStartPost` in that same template: it sleeps 8s (letting the poller start
watching the inject file) then appends the boot turn to
`${TELEGRAM_STATE_DIR}/inject`. Both files are kept in the repo — this one as
the documented source-of-truth for the plugin-side wiring, the template's
`ExecStartPost` as the thing that actually fires in production — but treat the
inject-file text in the template as authoritative when the two ever disagree.

Board card #461 (child of #456) extended that SAME inject-file boot turn to
also re-arm any durable dept cron declared in `config/crons.yaml`, not just
`/loop` itself — see `docs/durable-cron-manifest.md` +
`schemas-draft/crons-manifest.schema.yaml`.
