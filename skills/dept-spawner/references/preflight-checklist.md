# Preflight — collect these BEFORE running any spawn step

A spawn started without these decided stalls mid-way. Resolve all of them first, then write
them into an `ONBOARDING-ANSWERS.md` (so the human can approve the agent's Phase-3 proposals
fast rather than compose from scratch — this is how Geraldine's spawn was kept quick).

## Phase-0 decisions

| Decision | Example (Geraldine) | Notes |
|---|---|---|
| Dept slug | `accountant` | lowercase, regex-safe; becomes `bubble-ops-<slug>` |
| Display name | `Geraldine` | the agent's human name |
| Telegram bot | created via BotFather | handle < 32 chars; BotFather names are globally unique |
| Level | `ops` (leaf) or `management` (aggregator like Tony) | affects layer wiring |
| Mandate scope | a SCOPE-v1.md written before bootstrap | the agent proposes the full mandate in Phase 3; this is the seed |
| Domain tooling | dougs-devis (Geraldine), trading skills (Ben) | wired Phase 5, post-live |
| Creds strategy | which Claude/Notion creds to copy; SOPS plan | human-handled |
| GitHub org | `Bubble-invest` | not a personal account |

## Known footguns to plan around

- **Empty-repo check bug:** `bootstrap-dept.sh` tests `default_branch==null`, but modern GitHub
  sets `default_branch=main` even on 0-commit repos → the empty-repo detection fails. If the
  human pre-created the org repo via the UI, pass `--accept-existing-empty-repo`. (Upstream fix
  to the lab copy is still pending — worth a card.)
- **GitHub App coverage:** if the `bubble-ops-bot` App install is `repository_selection=ALL` on
  the org, new repos are auto-covered (no callback). Otherwise the human clicks "add repo" in the
  App UI to trigger the setup callback (this bit Ben + Maya; Geraldine was auto-covered).
- **Watchdog not rendered:** `deploy-to-morty.sh` does NOT render the Telegram watchdog — slug-swap
  the 4 artifacts from maya (Phase 2).
- **agents/ prefix:** clone to `/home/claude/agents/bubble-ops-<slug>` WITH the prefix — the
  fleet backup cron auto-discovers depts by it.
