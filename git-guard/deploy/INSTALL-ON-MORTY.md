# INSTALL-ON-MORTY — bubble-git-guard operator runbook

**Audience:** Joris (CEO) + Rick (R&D) when deploying to Morty (Hetzner CX33).
**Pairs with:** `token-broker/deploy/INSTALL-ON-MORTY.md`.
**Doctrine:** Notion v4 line 725 — "GitHub ne fournit pas un vrai path-scope au
niveau token `contents:write`. Les paths autorisés sont donc appliqués par
wrapper local / git guard sur Morty, CI path guard, branch protection et audit
Layer 4. Les tokens limitent les repos et permissions ; les guards limitent les
chemins."

This guard is the **wrapper local / git guard sur Morty** half of that line.
The broker (Step 3b) covers repo + permission class.

---

## 0. Pre-flight

The token-broker must already be installed at `/opt/bubble-token-broker/` per
its `INSTALL-ON-MORTY.md`. The guard requires:

```bash
# Broker binary present and executable
ssh hetzner "test -x /opt/bubble-token-broker/bin/bubble-token-broker && echo OK"

# Broker policy YAML present (same file the broker consumes)
ssh hetzner "test -f /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml && echo OK"

# Python 3.10+ with PyYAML (the broker install put these there already)
ssh hetzner "python3 -c 'import yaml; print(yaml.__version__)'"
```

---

## 1. Ship the guard package to Morty

From your laptop (Mac):

```bash
# 1.1 Pack — EXCLUDE Mac AppleDouble + .DS_Store noise (QA-AUDIT-J2
# Nice-to-have #1: rsync from Mac was leaking `._*` files into /opt/).
cd /Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop
tar --exclude='._*' --exclude='.DS_Store' \
    -czf /tmp/bubble-git-guard.tar.gz git-guard/

# 1.2 Copy
scp /tmp/bubble-git-guard.tar.gz hetzner:/tmp/

# 1.3 Install
ssh hetzner bash -c "'
  sudo mkdir -p /opt/bubble-git-guard
  sudo tar xzf /tmp/bubble-git-guard.tar.gz -C /opt/bubble-git-guard --strip-components=1
  sudo chown -R root:root /opt/bubble-git-guard
  sudo chmod -R 0755 /opt/bubble-git-guard
'"

# 1.4 Wrapper script in PATH
ssh hetzner bash -c "'
  sudo mkdir -p /opt/bubble-git-guard/bin
  sudo install -m 0755 \
    /opt/bubble-git-guard/deploy/bubble-git-guard.template.sh \
    /opt/bubble-git-guard/bin/bubble-git-guard
  sudo ln -sf /opt/bubble-git-guard/bin/bubble-git-guard /usr/local/bin/bubble-git-guard
'"

# 1.5 Audit log dir (writable by the claude user that runs the loop)
ssh hetzner "sudo mkdir -p /var/log/bubble-git-guard && \
  sudo chown claude:claude /var/log/bubble-git-guard && \
  sudo chmod 0750 /var/log/bubble-git-guard"
```

---

## 2. Path-pattern reference (the policy enforces these via fnmatch globs)

Per Notion v4 §"Policies par type d'acteur" (lines 624-694):

### `runtime_write_own` (the dept's own runtime loop)

```yaml
allowed_paths:
  - outputs/**       # all layer outputs
  - queues/**        # queue items: research/, gates/, improvements/, management/
  - inbox/**         # decisions/, notifications/
```

### Structural paths — ALWAYS denied for `runtime_write_own`, ALLOWED for `settings_pr`:

```
dept.yaml
MANDATE.md
CLAUDE.md
layers/**
subagents/**
skills/**
tools/**
templates/**
policies/**
.claude/**
```

### `open_priority_pr` (Tony → child dept)

Per Notion line 621:

```yaml
target_paths:
  - queues/management/**
```

---

## 3. Smoke test (offline, no network, no broker mint)

```bash
# 3.1 Clone the fixture repo somewhere for the test
ssh hetzner bash -c "'
  sudo -u claude git clone https://github.com/vdk888/bubble-ops-fixture /tmp/guard-smoke
  cd /tmp/guard-smoke
  # Stage a couple of files that SHOULD be allowed
  echo \"smoke $(date -u +%FT%TZ)\" | sudo -u claude tee outputs/smoke-test.md >/dev/null
  sudo -u claude git add outputs/smoke-test.md
'"

# 3.2 Dry-run — must exit 0 and NOT touch the network/broker
ssh hetzner "cd /tmp/guard-smoke && sudo -u claude bubble-git-guard push \
    --dept fixture \
    --action runtime_write_own \
    --repo bubble-ops-fixture \
    --repo-dir /tmp/guard-smoke \
    --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \
    --audit-log /var/log/bubble-git-guard/audit.jsonl \
    --dry-run"
# Expect on stderr:
#   [dry-run] Would mint token (action=runtime_write_own, repo=bubble-ops-fixture) and push 1 path(s):
#     + outputs/smoke-test.md

# 3.3 Audit JSONL has a would_allow line
ssh hetzner "tail -1 /var/log/bubble-git-guard/audit.jsonl"
# Expect: {"ts":"...","actor":"ops-loop-fixture","dept":"fixture","repo":"bubble-ops-fixture","action":"runtime_write_own","status":"would_allow","paths_count":1,"token_ttl_minutes":60}
```

### Negative smoke (denial case)

```bash
# 3.4 Stage a structural file that MUST be denied
ssh hetzner bash -c "'
  cd /tmp/guard-smoke
  sudo -u claude sh -c \"echo evil >> dept.yaml && git add dept.yaml\"
'"

# 3.5 Dry-run again — must exit non-zero, name dept.yaml in the denial
ssh hetzner "cd /tmp/guard-smoke && sudo -u claude bubble-git-guard push \
    --dept fixture --action runtime_write_own --repo bubble-ops-fixture \
    --repo-dir /tmp/guard-smoke \
    --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \
    --audit-log /var/log/bubble-git-guard/audit.jsonl \
    --dry-run; echo EXIT=\$?"
# Expect EXIT=1 and stderr lists 'dept.yaml' as denied.

# 3.6 Audit JSONL has a denied line with dept.yaml in denied_paths
ssh hetzner "tail -1 /var/log/bubble-git-guard/audit.jsonl | python3 -m json.tool"

# 3.7 Reset the fixture clone for real run
ssh hetzner "cd /tmp/guard-smoke && sudo -u claude git checkout -- dept.yaml && sudo -u claude git reset HEAD dept.yaml"
```

### Token-leak sanity check

```bash
# 3.8 Audit log must NEVER contain a ghs_ token
ssh hetzner "grep -c ghs_ /var/log/bubble-git-guard/audit.jsonl || echo 'no ghs_ in audit (good)'"
# Expect: 0 or 'no ghs_ in audit (good)'
```

---

## 4. End-to-end (real broker mint + real git push)

```bash
# Requires GITHUB_APP env vars sourced from SOPS (broker install sets this up)
ssh hetzner bash -c "'
  set -euo pipefail
  ENV_FILE=/run/bubble-token-broker-test.env
  sudo SOPS_AGE_KEY_FILE=/etc/age/key.txt sops --decrypt \
    /etc/bubble/secrets.sops.env | sudo tee \"\$ENV_FILE\" >/dev/null
  sudo chmod 0600 \"\$ENV_FILE\"

  # Stage an allowed file
  cd /tmp/guard-smoke
  sudo -u claude sh -c \"echo \\\"e2e $(date -u +%FT%TZ)\\\" > outputs/e2e-test.md\"
  sudo -u claude git add outputs/e2e-test.md
  sudo -u claude git commit -m \"e2e: guard smoke\"

  # Run the guard end-to-end (sources broker env, invokes broker, runs git push)
  sudo bash -c \"set -a; source \$ENV_FILE; set +a; \
    SOPS_AGE_KEY_FILE=/etc/age/key.txt \
    cd /tmp/guard-smoke && \
    sudo -u claude --preserve-env=GITHUB_APP_ID,GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_FIXTURE,GITHUB_APP_PRIVATE_KEY_PATH \
      bubble-git-guard push \
        --dept fixture \
        --action runtime_write_own \
        --repo bubble-ops-fixture \
        --repo-dir /tmp/guard-smoke \
        --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \
        --broker /opt/bubble-token-broker/bin/bubble-token-broker \
        --audit-log /var/log/bubble-git-guard/audit.jsonl\"

  # Verify commit landed
  sudo -u claude git -C /tmp/guard-smoke log --oneline -1

  sudo shred -u \"\$ENV_FILE\"
'"

# 4.1 Verify audit ends with status:pushed (no token in line)
ssh hetzner "tail -1 /var/log/bubble-git-guard/audit.jsonl | python3 -m json.tool"
ssh hetzner "grep -c ghs_ /var/log/bubble-git-guard/audit.jsonl"   # MUST print 0
```

---

## 5. Wire into `ops-loop-fixture.service`

The `/loop` agent invokes the guard instead of bare `git push`. Edit the
autostart wrapper (see `MVP-ROADMAP §3 Step 7`):

```bash
# /home/claude/scripts/loop-autostart.sh — REPLACE the bare `git push` with:
bubble-git-guard push \
    --dept fixture \
    --action runtime_write_own \
    --repo bubble-ops-fixture \
    --repo-dir /home/claude/depts/bubble-ops-fixture \
    --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \
    --broker /opt/bubble-token-broker/bin/bubble-token-broker \
    --audit-log /var/log/bubble-git-guard/audit.jsonl
```

The /loop prompt should also be amended to invoke `bubble-git-guard` rather than
`git push` directly:

> `/loop 20m ... commit + push via bubble-git-guard.`

---

## 6. Rotation, retention, observability

- **Audit JSONL**: rotate via `/etc/logrotate.d/bubble-git-guard` (daily, gzip,
  retain 30). Same pattern as `bubble-token-broker`.
- **Grep for anomalies**:
  ```bash
  # Count deny events in the last hour (alert if > 5)
  ssh hetzner "tail -n 5000 /var/log/bubble-git-guard/audit.jsonl | \
    python3 -c \"import sys, json, datetime
  now = datetime.datetime.now(datetime.timezone.utc)
  for L in sys.stdin:
      e = json.loads(L)
      ts = datetime.datetime.fromisoformat(e['ts'].replace('Z', '+00:00'))
      if (now - ts).total_seconds() < 3600 and e['status'] == 'denied':
          print(e)\""
  ```
- **Layer 4 audit** (per Notion line 725 "audit Layer 4") consumes the same
  JSONL in its daily run and surfaces deny streaks in `outputs/<date>/4/risk-brief.md`.

---

## 7. Rollback

```bash
ssh hetzner "sudo rm /usr/local/bin/bubble-git-guard && sudo rm -rf /opt/bubble-git-guard"
# The loop will then fail-CLOSED at the next push attempt (no fallback to
# raw git push — that's by design; the wrapper script wraps the only call site).
```

---

## 8. What is NEVER logged or echoed (defense-in-depth)

Enforced in code:
1. `src/audit.py` `FORBIDDEN_FIELDS` drops fields named `token`, `access_token`,
   `pem`, `private_key`, `jwt`, `secret`.
2. `src/audit.py` raises `ValueError` if ANY value (or list item) starts with
   `ghs_` — including a sneaky `note="ghs_LEAKED"` attempt.
3. `src/guard.py` captures the broker's stdout into a LOCAL variable `_token`
   and never `print()`s it, never logs it, never puts it in the audit dict.
4. `src/guard.py` redacts the token from any `git push` stderr before
   surfacing the error message.
5. `src/guard.py` removes `GITHUB_TOKEN` from the env passed to `git push`
   so an attacker-set env var can't substitute for a broker token (fail-closed
   against PAT fallback).

Verify on Morty:
```bash
ssh hetzner "grep -rE 'print.*token|logger.*token' /opt/bubble-git-guard/src/"
# Expect: ONE false-positive line in src/guard.py mentioning 'token shape'
# (it's the error string when broker doesn't return a ghs_ prefix). No actual
# token value is ever printed.
```

---

## 9. Threat model summary

| Threat | Mitigation in the guard |
|--------|------------------------|
| Path exfiltration: stage a structural file alongside outputs | Atomicity: any single deny fails the whole batch (test_mixed_paths_partial_deny) |
| Commit + push secrets to `outputs/` | Path check is FILE-NAME based; secrets-in-content is a separate concern (pre-commit hook or CI scan — Layer 4 risk-brief flags) |
| Attacker bypasses the guard by calling `git push` directly | Service wrapper invokes guard; broker policy denies push-without-path-check on the API side too (broker also runs Policy.enforce); branch protection on `main` is the third layer |
| Token leak via audit, stderr, or env | FORBIDDEN_FIELDS + ghs_ ValueError + stderr redaction + GITHUB_TOKEN scrubbing |
| Broker exits 0 but stdout isn't a token (malformed) | Guard checks `_token.startswith("ghs_")` before invoking git; otherwise audit `mint_failed` |
| Broker MISSING from PATH | `FileNotFoundError` caught → audit `mint_failed`, no push |
| Network error on push | Captured exit code + redacted stderr → audit `push_failed` |
