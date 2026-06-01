# INSTALL-ON-MORTY — bubble-token-broker operator runbook

**Audience:** {{OPERATOR}} (CEO) + Rick (R&D) when deploying to Morty (Hetzner CX33).
**Status:** v1 = oneshot CLI. No daemon. Each `git push` workflow invokes the
CLI, consumes the token, the process exits, and the token is gone.

---

## 0. Pre-flight checks

These should already be true on Morty (audited 2026-05-19). Verify if unsure:

```bash
# SOPS + age key present?
ls -l /etc/age/key.txt                          # expect: -r-------- root root
ls -l /etc/bubble/secrets.sops.env              # expect: SOPS-encrypted env
ls -l /srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem

# The env file MUST contain (decrypt to confirm; root only):
sudo SOPS_AGE_KEY_FILE=/etc/age/key.txt sops --decrypt /etc/bubble/secrets.sops.env \
    | grep -E '^GITHUB_APP_(ID|INSTALLATION_ID_BUBBLE_OPS_FIXTURE|CLIENT_ID|PRIVATE_KEY_PATH)='
# Expected output (values redacted):
#   GITHUB_APP_ID=3782718
#   GITHUB_APP_INSTALLATION_ID_BUBBLE_OPS_FIXTURE=134075326
#   GITHUB_APP_CLIENT_ID=Iv23cteHxkkWlFqSloaa
#   GITHUB_APP_PRIVATE_KEY_PATH=/srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem
```

---

## 1. Ship the broker package to Morty

From your laptop (Mac):

```bash
# 1.1 Pack — EXCLUDE Mac AppleDouble + .DS_Store noise (QA-AUDIT-J2
# Nice-to-have #1: rsync from Mac was leaking `._*` files into /opt/).
cd /Users/joris/claude-workspaces/Rick_RnD/projects/bubble-ops-loop
tar --exclude='._*' --exclude='.DS_Store' \
    -czf /tmp/bubble-token-broker.tar.gz token-broker/

# 1.2 Copy to Morty (hetzner SSH alias must be configured)
scp /tmp/bubble-token-broker.tar.gz hetzner:/tmp/

# 1.3 Install on Morty
ssh hetzner bash -c "'
  sudo mkdir -p /opt/bubble-token-broker
  sudo tar xzf /tmp/bubble-token-broker.tar.gz -C /opt/bubble-token-broker --strip-components=1
  sudo chown -R root:root /opt/bubble-token-broker
  sudo chmod -R 0755 /opt/bubble-token-broker
'"

# 1.4 Python deps (system Python 3.12 on Ubuntu 24.04 on Morty)
ssh hetzner "sudo apt-get update && sudo apt-get install -y python3-cryptography python3-requests python3-yaml"

# 1.5 Wrapper script
ssh hetzner bash -c "'
  sudo tee /opt/bubble-token-broker/bin/bubble-token-broker >/dev/null << \"EOF\"
#!/usr/bin/env bash
exec python3 -m src.cli \"\$@\"
EOF
  sudo chmod +x /opt/bubble-token-broker/bin/bubble-token-broker
  sudo mkdir -p /opt/bubble-token-broker/bin
'"
```

Note: `python3 -m src.cli` is run from `/opt/bubble-token-broker/` as cwd —
the wrapper should `cd` first:

```bash
ssh hetzner bash -c "'
  sudo tee /opt/bubble-token-broker/bin/bubble-token-broker >/dev/null << \"EOF\"
#!/usr/bin/env bash
cd /opt/bubble-token-broker
exec python3 -m src.cli \"\$@\"
EOF
  sudo chmod +x /opt/bubble-token-broker/bin/bubble-token-broker
'"
```

---

## 2. Confirm SOPS decrypt works in the broker's invocation context

```bash
# 2.1 Sanity: broker can shell-out to sops and get plaintext PEM
ssh hetzner "sudo SOPS_AGE_KEY_FILE=/etc/age/key.txt \
  sops --decrypt --input-type binary --output-type binary \
  /srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem | wc -c"
# Expect: ~1700 bytes (PKCS#8 RSA-2048 PEM). NEVER print the content itself.
```

If `wc -c` returns ~1700 bytes, decryption is healthy.

---

## 3. Smoke test the broker (dry-run, offline)

```bash
# 3.1 Policy-only check — no PEM, no API, no token leaves anywhere
ssh hetzner "/opt/bubble-token-broker/bin/bubble-token-broker check \
  --dept fixture --action runtime_write_own \
  --repo bubble-ops-fixture \
  --paths outputs/2026-05-20/1/summary.md \
  --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml"
# Expect: ALLOWED: actor=ops-loop-fixture repo=bubble-ops-fixture action=runtime_write_own

# 3.2 Mock-GitHub mint — exercises the JWT mint + audit path WITHOUT calling GitHub
ssh hetzner "sudo SOPS_AGE_KEY_FILE=/etc/age/key.txt \
  /opt/bubble-token-broker/bin/bubble-token-broker mint \
    --dept fixture --action runtime_read --repo bubble-ops-fixture \
    --app-id 3782718 --installation-id 134075326 \
    --pem-path /srv/bubble-secrets/github-app-bubble-ops-bot.private-key.sops.pem \
    --policy /opt/bubble-token-broker/deploy/policies/fixture-policy.yaml \
    --audit-log /tmp/broker-smoke.jsonl \
    --mock-github" \
  | head -c 30 ; echo

# Expect on stdout: ghs_MOCK<40 hex chars>  (truncated to first 30 chars by head)
# Expect in /tmp/broker-smoke.jsonl: one JSON line with status:"issued",
#   NO 'token' field, NO ghs_ value.

ssh hetzner "cat /tmp/broker-smoke.jsonl"
ssh hetzner "grep -c ghs_ /tmp/broker-smoke.jsonl"   # MUST print 0
```

---

## 4. Real mint (live GitHub, end-to-end)

```bash
# 4.1 Decrypt env file into a tmpfs-backed file, source it, mint, drop file.
ssh hetzner bash -c "'
  set -euo pipefail
  ENV_FILE=/run/bubble-token-broker-test.env
  sudo SOPS_AGE_KEY_FILE=/etc/age/key.txt sops --decrypt \
    /etc/bubble/secrets.sops.env | sudo tee \"\$ENV_FILE\" >/dev/null
  sudo chmod 0600 \"\$ENV_FILE\"

  # Mint, get token, do NOT echo it
  TOKEN=\$(sudo bash -c \"set -a; source \$ENV_FILE; set +a; \
    SOPS_AGE_KEY_FILE=/etc/age/key.txt \
    /opt/bubble-token-broker/bin/bubble-token-broker mint \
      --dept fixture --action runtime_read --repo bubble-ops-fixture \
      --audit-log /var/log/bubble-token-broker/audit.jsonl\")

  # Smoke check: token starts with ghs_ and is ~40 chars; never echo content
  echo \"token length = \${#TOKEN}\"
  case \"\$TOKEN\" in ghs_*) echo OK ;; *) echo BAD; exit 1 ;; esac

  # Use it for ONE git push (or ls-remote), then drop
  git ls-remote https://x-access-token:\$TOKEN@github.com/vdk888/bubble-ops-fixture.git >/dev/null
  echo ls-remote OK

  # Shred env file
  sudo shred -u \"\$ENV_FILE\"
'"
```

If `ls-remote OK` prints, the broker is live.

---

## 5. Where audit goes

- Default: `~/.local/state/bubble-token-broker/audit.jsonl` (for the invoking user).
- Recommended for the `claude` user / production:
  `/var/log/bubble-token-broker/audit.jsonl` (pass via `--audit-log`).
- Create the dir once:
  ```bash
  ssh hetzner "sudo mkdir -p /var/log/bubble-token-broker && \
    sudo chown claude:claude /var/log/bubble-token-broker && \
    sudo chmod 0750 /var/log/bubble-token-broker"
  ```
- Each line is structured JSON (see Notion v4 lines 605-614 for schema).
- Rotate via `logrotate.d/bubble-token-broker` (daily, gzip, retain 30).

---

## 6. Wiring into `ops-loop-fixture.service` (the consumer)

The `/loop` agent in `ops-loop-fixture` calls the broker just before each
`git push`. Pattern (inside the loop's shell wrapper):

```bash
# inside ops-loop-fixture loop body
TOKEN=$(/opt/bubble-token-broker/bin/bubble-token-broker mint \
  --dept fixture --action runtime_write_own --repo bubble-ops-fixture \
  --paths "$STAGED_PATHS" \
  --audit-log /var/log/bubble-token-broker/audit.jsonl)
case "$TOKEN" in
  ghs_*) ;;
  *) echo "broker mint failed: $TOKEN" >&2; exit 1 ;;
esac
# Inject only into the single `git push` invocation, never export globally.
GIT_ASKPASS=/dev/null git -c \
  "http.https://github.com/.extraheader=AUTHORIZATION: bearer $TOKEN" \
  push origin HEAD
unset TOKEN
```

The Morty git guard (Step 3c, separate component) verifies `$STAGED_PATHS`
is a subset of the runtime_write_own allow-list BEFORE the broker is called.

---

## 7. Rollback

If the broker misbehaves:

```bash
ssh hetzner "sudo rm -rf /opt/bubble-token-broker"
# ops-loop-fixture.service will fail-closed at its next push attempt
# (no fallback to a long-lived PAT — that's by design per Notion v4).
```

---

## 8. What is NEVER logged (defense-in-depth)

The broker enforces these in code (`src/audit.py`):
1. Any audit field literally named `token`, `access_token`, `pem`, `private_key`, `jwt`, `secret` is **dropped before serialization** (`FORBIDDEN_FIELDS` frozenset).
2. Any audit value that starts with `ghs_` **raises ValueError** before reaching disk.
3. The PEM bytes pass through `_pem_provider()` → `serialization.load_pem_private_key()` only; no `open()` / `write()` of PEM anywhere in `src/`.

Verify with:
```bash
ssh hetzner "grep -rE 'print.*pem|print.*token|logger.*token|logger.*pem' /opt/bubble-token-broker/src/"
# Expect: empty output.
```
