---
name: pre-publish-scan
description: >-
  Systematic pre-open-source LEAK GATE for any Bubble repo. Run BEFORE making
  (or while a repo is) public, and at each step of an open-sourcing effort, to
  catch operator PII (Joris/Jade Telegram IDs), internal infra (Tailscale
  FQDN/IP, Mac paths), internal Notion IDs, secret-map docs, and raw secret
  prefixes — in the working tree AND git history. Use whenever you're about to
  publish, push to a public repo, open-source a skill/plugin/repo, run a launch,
  or whenever Joris says "is this clean to share / can this go public / scan for
  leaks". This is the GATE: a repo does not go public until this exits clean.
---

# pre-publish-scan — the systematic leak gate

Joris (2026-06-22): "make the security scan systematic at each step." Before ANY
Bubble repo goes public — or at each step of an open-sourcing push — run this.

## Run it
```bash
# working tree only (fast):
deploy/bin/pre-publish-scan.sh <repo-dir>
# working tree + ALL git history (a value purged from HEAD can still be public in history):
deploy/bin/pre-publish-scan.sh <repo-dir> --history
```
Exit 0 = CLEAN (safe to publish). Exit 2 = findings (BLOCK publish).

## What it catches (the VOIE3 leak taxonomy)
- operator Telegram IDs (Joris/Jade — the specific IDs live in the scanner patterns, not here)
- internal Tailscale FQDN + CGNAT 100.64/10 IPs
- personal paths / usernames (operator home dirs, personal SSH targets)
- internal Notion DB/page UUIDs
- raw secret prefixes (sk-ant, ghp_/ghs_, AKIA, BEGIN PRIVATE KEY, age1…)
- secret-MAP docs (any doc that enumerates where secrets live)

**PII controls (client/personal data)** — structured PII a public repo must never carry,
reusing Bubble Shield's recognizers: emails, IBAN, ISIN, SIRET/SIREN, FR numéro de sécu,
FR phone, credit-card numbers. Allowlist synthetic fixtures via `BUBBLE_PII_ALLOW` (pipe-
separated literals). NOTE: names & postal addresses are the hard low-recall part — grep can't
catch them reliably; for a hard pre-publish name/address check, run the repo's text through
**bubble-shield** (the NER PII anonymizer) as a complementary gate.

Values are NEVER printed — findings show file:line + class + «match redacted».

## After findings
- Working tree → replace with `{{PLACEHOLDER}}` or read from env. Re-scan.
- History → working-tree fixes are NOT enough; use git-filter-repo (see
  agent-memory `reference_git_history_secret_purge`), then re-scan with --history.

## Make it systematic
Wire this into: the launch runbook (gate before publishing each repo), a
pre-push hook on repos slated for OSS, and the rnd_loop (scan any repo before
recommending it go public). The gate is non-optional — clean exit or no publish.
