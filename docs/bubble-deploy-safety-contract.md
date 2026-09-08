# VPS deploy safety contract

`scripts/bubble-deploy.sh` advances reviewed `origin/main` only when the target checkout is an exact, clean `main` branch with no local commits. Git runs as the checkout directory owner, so the deployer does not modify global `safe.directory` trust.

The current layout is `/opt/bubble-ops-loop` plus `/srv/agents/<slug>` and `bubble-agent@<slug>.service`. `/home/claude/agents/bubble-ops-<slug>` and `ops-loop-<slug>.service` remain discovery fallbacks for hosts that have not migrated. Department discovery requires its paired primary unit to be loaded, so abandoned clones and aliases are not treated as live departments.

The deployer leaves dirty, ahead, detached, and non-main checkouts byte-for-byte in place and exits 2 so systemd reports that operator review is required. It never stashes or resets them. An active, activating, reloading, or deactivating primary agent owns its own pull, so the deployer reports that deferral without changing the checkout. Inactive and operator-stopped agents may receive a clean fast-forward, but the deployer does not start them.

The script never stops, starts, restarts, resets, stashes, rolls back, or rewrites Git history. A failed fast-forward stays visible for review; it is not followed by a destructive rollback.

Tests:

```bash
bash tests/test_1122_bubble_deploy_rollback_dataloss.sh
bash tests/test_1124_bubble_deploy_safe_ff.sh
```
