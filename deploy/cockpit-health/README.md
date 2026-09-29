# cockpit-health — deploy

Hourly evidence-collect + first-pass-judge for the cockpit/dashboards (board
card origin: Joris tg 10023-10029 — "is there a mission to detect those bugs
(cockpit /cost page broken, Ben exposure page stale again)? how do we make it
so you find and fix them on your own?"). Design: **tools as evidence, agent
as judgment** (`~/.claude/agent-memory/shared-wiki/shared/systems/
cron-judgment-vs-tools.md`). See `../../scripts/cockpit_health/` for the code
and `../../scripts/cockpit_health/__init__.py` for the module map.

## What this is NOT

This is the **framework** — the collector, the judge runner, the systemd
templates. Rick's own recurring **mission** (per-tick review of what shadow
mode surfaces, the weekly deep walk, the false-negative measurement bar, and
when to flip `COCKPIT_HEALTH_SHADOW=0`) lives in the companion PR on
`bubble-rnd-workspace`: `missions/cockpit-health.md`. Deploy this framework
first; the mission is what actually *acts* on what it finds.

## Deploy steps

1. **Pull the infra clone.** `scripts/cockpit_health/` ships inside this repo
   (`bubble-ops-loop`) — the existing `bubble-deploy-infra.timer` /
   `bubble-deploy.sh --infra-only` already keeps `/opt/bubble-ops-loop` (the
   root-owned, read-only clone `bubble-ops-console.service` runs from) in
   sync every 15 min. No new deploy path — merging to `main` is sufficient;
   nothing here needs a bespoke install script.

2. **Install the units.**
   ```bash
   sudo install -m 0644 deploy/templates/cockpit-health.service /etc/systemd/system/
   sudo install -m 0644 deploy/templates/cockpit-health.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now cockpit-health.timer
   ```

3. **Secrets — reuse, don't duplicate.** `cockpit-health.service` runs as the
   EXISTING `bubble-console` uid and reuses `bubble-ops-console.service`'s
   already-decrypted `CONSOLE_BEARER_TOKEN` + `GH_TOKEN` from
   `/run/bubble-ops-console/env` (`ConditionPathExists` gates the unit on
   that file already existing this boot — no secrets are decrypted twice, no
   new credential is minted for either of those two). Nothing to provision
   here.

4. **The one genuinely new credential — `JEV_OPENROUTER_API_KEY`
   (`needs:human`).** Hosted Jev via OpenRouter (`typesafe/jev-1.13`,
   cleared for internal Bubble data per Joris 2026-09-25 — see
   `skills/system-one-decisions/scripts/jev.py`'s
   `RECOMMENDED_DEFAULT_BACKEND_FOR_INTERNAL_DATA`) is the ONE credential
   this framework needs that nothing else on the box already holds. Provision
   it the same way every other fleet dept secret is provisioned (the
   existing SOPS-encrypted-secret pattern — `morty-sops-add-key` /
   `bubble-rotate-dept-secret`, per the fleet's own secret-provisioning
   mechanics; NOT a new mechanism), writing it to a `secrets.sops.env`-style
   file that decrypts to `/run/cockpit-health/env` before this unit starts
   (mirror `bubble-ops-console.service`'s own `ExecStartPre` SOPS-decrypt
   pattern, or reuse whichever existing drop-in decrypts secrets for this
   host already — do not hand-write a plaintext key file). This is
   Joris/Rick's call at actual deploy time, not something this PR does —
   **the framework works correctly without it**: `judge.py`'s
   `build_jev_caller()` degrades to "Jev call failed" per page, and the
   collector's own hard-inconsistency flags still drive every finding (the
   asymmetric rule's whole point — Jev only ever *adds* escalations, it is
   never required for the framework to be useful).

5. **Verify.**
   ```bash
   sudo systemctl start cockpit-health.service   # one-shot manual run
   sudo journalctl -u cockpit-health.service -n 50
   sudo -u bubble-console cat /var/lib/cockpit-health/evidence/evidence_latest.json | python3 -m json.tool | head -50
   sudo -u bubble-console tail -5 /var/lib/cockpit-health/shadow-log.jsonl   # shadow mode (default)
   ```

## Shadow mode (default ON)

`COCKPIT_HEALTH_SHADOW=1` by default in the checked-in unit template —
suspicious verdicts are appended to `/var/lib/cockpit-health/shadow-log.jsonl`
only, never posted to the real board. This follows
`system-one-decisions/SKILL.md`'s "Mandatory rollout discipline" (shadow ->
gold set -> calibrate -> gate, never a hard cutover) so Jev's real false-
negative rate on THIS fleet's own cockpit pages gets measured before it can
page anyone. Flip it with a systemd drop-in once Rick's mission
(`bubble-rnd-workspace/missions/cockpit-health.md`) says the calibration bar
is met — never by hand-editing the checked-in template:

```bash
sudo mkdir -p /etc/systemd/system/cockpit-health.service.d
printf '[Service]\nEnvironment=COCKPIT_HEALTH_SHADOW=0\n' | \
  sudo tee /etc/systemd/system/cockpit-health.service.d/live.conf
sudo systemctl daemon-reload
```

## Dedupe

A suspicious page gets a STABLE title (`judge._stable_title()`), so
`tools/kanban/emit_kanban_item.sh`'s own emit-key dedup collapses repeat runs
into one card; if a card is already open for that page, `judge.py` posts a
`gh issue comment` update instead of a second card.
