# Decision-signing key — boot-time decrypt for the cockpit

Board #1498, follow-up to board #1476 / PR #492's "Key generation + install
runbook". `scripts/lib/decision_signing.py` (PR #492) signs/verifies every
cockpit gate decision with an Ed25519 keypair. The PUBLIC key is safe to
commit (`scripts/keys/decision-signing-ed25519.pub.pem`, this PR). The
PRIVATE key must only ever exist, in plaintext, in a root-owned tmpfs file
readable by the console's own uid — this directory provisions that.

**Board #1498 step 1 is done** (see the card): an Ed25519 keypair was
generated ON the VPS as root; the private key is SOPS-encrypted at
`/srv/bubble-secrets/decision-signing-ed25519.private-key.sops.pem`
(root:root, 0440), same age recipients as
`github-app-cockpit-approver.private-key.sops.pem`. This PR ships the
installer that decrypts it into place — it does **not** touch that file or
any key material.

## Pieces

- `bubble-cockpit-decision-key-decrypt.sh` — root-owned; SOPS-decrypts the
  private key to `/run/bubble-cockpit-decision-key/key` (tmpfs, atomic
  write via `$DEST.tmp.$$` + `mv -f`, `root:bubble-console 0640`). Fails
  CLOSED with a clear stderr message (no key material logged) if the SOPS
  source file is missing or decrypt produces no output. The relevant
  degrade path downstream is
  `scripts/lib/decision_signing.py::load_private_key()` raising
  `FileNotFoundError`, which `console/services/github_reader.py::_sign_decision()`
  already catches and writes the decision **unsigned**
  (`DECISION_SIGNATURES=warn`, the intended migration-period behaviour —
  this was never meant to be a write-time hard failure, see PR #492's body).
- `bubble-cockpit-decision-key.service` — a root **oneshot** systemd unit,
  `RemainAfterExit=yes`, that runs the decrypt script once. Unlike
  `console/deploy/cockpit-approver/`'s App-token minter, this key is
  **static** (no expiry, no rotation schedule) — so there is deliberately
  **no repeating timer**, only a boot-time run:
  - `Before=bubble-ops-console.service` + `WantedBy=multi-user.target`
    orders the decrypt before the console on a fresh boot (both units are
    pulled in by `multi-user.target`; `Before=` is pure ordering, not a
    hard dependency — a failure here never blocks the console from
    starting, matching the fail-open/degrade design above).
  - `RuntimeDirectory=bubble-cockpit-decision-key` +
    `RuntimeDirectoryMode=0750` + `Group=bubble-console` gives the
    directory `root:bubble-console 0750` automatically, before the script
    even runs.
  - `RuntimeDirectoryPreserve=yes` is **required**, not cosmetic: systemd's
    default (`no`) deletes a unit's `RuntimeDirectory` the moment that unit
    is considered "stopped" — which, for a `Type=oneshot` service, is
    immediately after `ExecStart` exits. Without `yes`, the key would be
    wiped from `/run` seconds after being written. `yes` keeps it until the
    machine actually reboots, so a **console restart never races this
    unit** — restarting `bubble-ops-console.service` only touches its own
    process, never this unit's separately-owned `RuntimeDirectory`.
- `install-decision-signing-key.sh` — idempotent installer, **render-only
  by default** (prints what it would do, writes nothing); pass `--activate`
  to actually install the files + `daemon-reload` + `enable --now` the
  unit (which both arms it for future boots and runs the decrypt
  immediately). See the script's own header for the full contract.

## One-time operator steps (run as root on the VPS — joris-cx33)

1. **Nothing to place** — the SOPS-encrypted private key already exists
   (board #1498 step 1, done). If you're doing this on a different box or
   re-provisioning after a key rotation, drop it at
   `/srv/bubble-secrets/decision-signing-ed25519.private-key.sops.pem`
   (root:root, 0440) via the usual `auth` skill / `morty-sops-add-key`
   flow first — the key must never transit chat.
2. **After this PR merges**, `/opt/bubble-ops-loop` picks up the new files
   automatically via the existing `bubble-deploy-infra.timer`
   (`bubble-deploy.sh --infra-only`, ~every 15min) — no manual sync step.
3. **Run the installer, for real:**
   ```bash
   bash /opt/bubble-ops-loop/console/deploy/decision-signing-key/install-decision-signing-key.sh --activate
   ```
   (idempotent; safe to re-run on every deploy — `install` only rewrites a
   destination file when its content differs.)
4. **Smoke-test** (same shape as `cockpit-approver`'s):
   ```bash
   test -s /run/bubble-cockpit-decision-key/key                              # the oneshot decrypted something
   sudo -u bubble-console test -r /run/bubble-cockpit-decision-key/key       # console can read it
   sudo -u claude test -r /run/bubble-cockpit-decision-key/key && echo BAD-READABLE || echo "OK - claude denied"
   ```
5. **Restart the console** so its running process picks up signing:
   ```bash
   systemctl restart bubble-ops-console.service
   ```
6. **End-to-end verify:** make (or wait for) the next cockpit gate decision
   — `console/services/github_reader.py::write_gate_decision()` should now
   sign it (it already tries to on every write; step 5 just removes the
   "key not provisioned" reason it was falling back to unsigned). Confirm
   with `scripts/lib/decision_signing.py::verify_decision()` against the
   written `inbox/decisions/<gate_id>.yaml` + its gate file — `verified`
   should be `True`. `DECISION_SIGNATURES` stays unset (`warn`) fleet-wide
   either way; this step only starts producing real signatures, it does
   not flip enforcement (see "Not in scope" below).

## Not in scope for this PR

- `DECISION_SIGNATURES` stays **unset** (`warn`, unenforced) everywhere.
  Flipping any dept to `enforce` is a separate, per-dept decision once its
  executor has adopted `verify_decision()` (see PR #492's follow-up list).
- No executor wiring (`bubble-ops-tony`, `bubble-ops-ben`, etc.) — separate
  per-dept PRs, per PR #492's body.
- No change to `bubble-ops-console.service` itself or its drop-ins.
- No key rotation tooling — this key is static; a future rotation would
  bump `DEFAULT_KEY_ID` in `decision_signing.py` and re-run this installer
  against a newly-dropped SOPS file, out of scope here.

## Why a boot-time oneshot, not a timer (unlike cockpit-approver)

`console/deploy/cockpit-approver/`'s token is a GitHub App **installation
token**, ~1h-lived, so it needs re-minting every ~45min via a timer. This
key is a **static Ed25519 keypair** with no expiry — decrypting it once at
boot (or once via `--activate`) is sufficient; a repeating timer would just
re-decrypt byte-identical plaintext on a schedule for no benefit. The
`RuntimeDirectoryPreserve=yes` + separate-unit-lifetime design above is
what stands in for "still there after a console restart" that a timer
would otherwise provide incidentally.
