# OnFailure drop-ins

Each `<unit>.service.d/override.conf` here wires `OnFailure=cron-failure-alert@%n.service`
onto an EXISTING unit, without owning or duplicating the base unit file. Installed via
`scripts/install-onfailure-dropins.sh`, which copies each `<unit>.service.d/` directory into
`/etc/systemd/system/<unit>.service.d/` and reloads systemd.

Requires `cron-failure-alert@.service` + `/home/claude/scripts/cron-failure-alert.sh` to
already be present on the box (deployed by hand today — see board follow-up to bring that
under `deploy/templates/` too).

Units wired here (today alert on nothing):
- bubble-deploy-full.service
- bubble-deploy-infra.service
- bubble-board-token-refresh.service
- bubble-ops-contents-token-refresh.service
- bubble-restic-backup.service

The loop FLOOR — the fleet safety net (#1313):
- `loop-layer1@.service` — templated; one drop-in covers every dept instance (`@maya`, …) + future depts
- `loop-layer2@.service` — templated
- `loop-layer3@.service` — templated
- `loop-layer4@.service` — templated (covers `@maya`, `@ben`, `@tony`, …)
- `loop-layer@.service` — templated; the legacy/manual GENERIC-mode floor (no forced layer).
  Added in #1313 step 2 (code-review finding: this template — still documented as supported
  in `scripts/loop-backup.sh`'s own header — was missed from the original rollout). DORMANT
  in production as of 2026-09-14: no live `loop-layer@<slug>` instances exist.
- `loop-backup.service` — plain per-instance; the timer this legacy path runs under
  (`loop-backup.timer`) is also DORMANT (disabled) as of 2026-09-14, but wiring it now closes
  the gap before it can ever fire silently again.
- `morty-agentic-audit.service` — plain per-instance
- `telegram-watchdog-tony.service` — plain per-instance

Note on templated units: a template-level drop-in `loop-layerN@.service.d/override.conf`
applies to **all** instances of that template, and `%n` expands to the full instance name
(e.g. `loop-layer4@maya.service`), so `OnFailure=cron-failure-alert@%n.service` becomes a
nested-`@` instance (`cron-failure-alert@loop-layer4@maya.service.service`) — which systemd
handles fine. Proven in production by `cloud-wiki-compile@.service.d/onfailure.conf`, which
uses the identical template-level `%n` wiring on `cloud-wiki-compile@compile`.

`morty-agentic-audit.service.d` already carries `headless.conf` + `timeout.conf` on the box;
`override.conf` is additive (systemd merges all `*.conf` in the drop-in dir) and does not
touch them — the installer only writes/updates `override.conf`.

Composition note: `bubble-deploy-full`/`bubble-deploy-infra` only alert once
`bubble-deploy.sh` actually exits nonzero on a real failure — WS-A's deploy-script hardening
adds that exit-code discipline. Wiring `OnFailure=` here is correct regardless (it's inert
until the script raises), so this ships independently.

Floor sequencing note (#1313): this PR wires OnFailure onto the floor **only** — it does
NOT change what the floor does. In particular, `loop-backup.sh` still exits nonzero on a
deliberate deferral (e.g. "dept stale, wake channel dead"). That deferral-vs-failure
exit-code fix is deliberately a SEPARATE follow-up (step 2, tracked separately), because
making the floor exit 0 on deferral BEFORE alerting exists would convert a silent failure
into a silent success — strictly worse. Alerting-first is safe on its own: until step 2
lands, a deferral fires the Telegram alert (correct — a 3-day-stale dept nobody can wake is
worth paging on); after step 2, a deferral becomes its own non-failure signal instead.
