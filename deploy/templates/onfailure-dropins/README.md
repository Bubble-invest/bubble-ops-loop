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

Composition note: `bubble-deploy-full`/`bubble-deploy-infra` only alert once
`bubble-deploy.sh` actually exits nonzero on a real failure — WS-A's deploy-script hardening
adds that exit-code discipline. Wiring `OnFailure=` here is correct regardless (it's inert
until the script raises), so this ships independently.
