# Security Audit Log
**Last Full Audit**: 2026-05-22 | **Critical Open**: 1 | **Stack**: Python/FastAPI, SOPS/age, systemd, Tailscale

## Open Vulnerabilities
| ID | Severity | Category | Location | Status | Found |
|----|----------|----------|----------|--------|-------|
| SEC-001 | CRITICAL | Secret Leak in Session Transcript | morty+fixture agent JSONL transcripts (2026-05-21) | OPEN — credential rotation pending | 2026-05-21 |

## Details: SEC-001
During Layer 2 smoke test, test command used wrong argument ordering:
`sops --decrypt /etc/bubble/secrets.sops.env --output /tmp/test.env`
sops.real treated `--output` as a POSITIONAL argument (bug in test, not in wrapper),
causing full plaintext of secrets.sops.env to print to terminal stdout.
Exposed credentials:
- TELEGRAM_BOT_TOKEN (morty agent)
- CLAUDE_CODE_OAUTH_TOKEN
- TAILSCALE_AUTHKEY
- PHONEHOME_TOKEN
- GITHUB_TOKEN (PAT)
- GITHUB_APP_ID / INSTALLATION_IDs / CLIENT_ID
- FIXTURE_TELEGRAM_BOT_TOKEN
- CONSOLE_BEARER_TOKEN
- MAYA_TELEGRAM_BOT_TOKEN
These were visible in this Claude session transcript. All must be rotated.

## Security Configuration
- SOPS wrapper (Layer 2): /usr/local/bin/sops (bash, 755, root:root, 7268 bytes) — blocks stdout decrypt
- SOPS real binary: /usr/local/bin/sops.real (ELF, 755, root:root, 45011128 bytes)
- SOPS rollback: /usr/local/bin/sops-rollback.sh (bash, 755, root:root, 637 bytes)
- AGE key: /etc/age/key.txt (root:root, 0400)
- Secrets file: /etc/bubble/secrets.sops.env (root:root, 0440)
- TLS: Tailscale serve (handled by Tailscale)
- Transcript leak scanner (Layer 3): /home/claude/scripts/transcript-leak-scan.sh (bash, 755, claude:claude, 7268 bytes)
- Layer 3 timer: transcript-leak-scan.timer — active, enabled, next elapse 2026-05-23 06:31 UTC
- Layer 3 logs: /var/log/bubble-security/transcript-leak-scan-<date>.log

## 3-Layer Hardening Status (2026-05-22)
- Layer 1: DONE — SOPS stdout blocked by wrapper (idempotent, rollback available)
- Layer 2: DONE — Wrapper live, proof-of-correctness: blocked `sudo sops --decrypt > stdout`
- Layer 3: DONE — transcript-leak-scan.sh deployed + timer enabled; dry-run caught SEC-001 files; test-fixture alert fired correctly with redacted output

## Wrapper Allowlist
Services permitted to call sops without --output (stdout-redirect pattern):
- bubble-ops-console.service (uses --output, should be lowest-risk)
- claude-agent-morty.service (uses --output)
- ops-loop-fixture.service (uses shell > redirect for PEM file)

## Server Access
- Production: ssh hetzner ({{VPS_HOST}}, Hetzner CX33)
- Services: bubble-ops-console.service, claude-agent-morty.service, ops-loop-fixture.service

## False Positives (Don't Re-flag)
- Layer 3 dry-run 2026-05-22: 2 findings are SEC-001 (known, pending rotation) — not new discoveries
