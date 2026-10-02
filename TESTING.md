# Testing

Create a Python 3.12 virtual environment and install the exact, hashed test
closure. Invoke pip through the environment's interpreter so a copied or stale
`pip` shebang cannot select another venv:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install \
  --require-hashes \
  -r requirements/test-py312.lock
.venv/bin/python -m pip check
```

`--require-hashes` is the integrity check: an unpinned dependency or artifact
whose digest is absent from the lock fails before tests run. The lock is
generated from `requirements/test-py312.in`, which includes the source
requirements plus pytest. Refresh it only from a fresh isolated Python 3.12
environment with `pip-tools==7.6.1`, then repeat the hashed install, `pip
check`, and full non-live suite before committing it:

```bash
python3.12 -m piptools compile \
  --generate-hashes \
  --resolver=backtracking \
  --strip-extras \
  --output-file requirements/test-py312.lock \
  requirements/test-py312.in
```

The default suite is hermetic. It excludes tests marked `live`, so a clean
checkout does not need `/tmp/bubble-ops-fixture`, the sibling
`bubble-vps-platform` repository, GitHub state, or SSH access:

```bash
env -u TELEGRAM_STATE_DIR -u TELEGRAM_BOT_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
  .venv/bin/python -m pytest tests -q
```

Run the live set explicitly with `-m live`. These tests inspect the current
`vdk888/bubble-ops-fixture` outputs and gates, plus the deployed Morty VPS over
SSH; failures therefore describe external state rather than a clean-checkout
regression:

```bash
env -u TELEGRAM_STATE_DIR -u TELEGRAM_BOT_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
  .venv/bin/python -m pytest tests -m live -v
```

Use `BUBBLE_OPS_FIXTURE_ROOT` and `BUBBLE_OPS_LAYER4_DATE` to replay the Layer
4 live tests against a specific checkout and UTC date. The Morty tests require
the `hetzner` SSH alias and non-interactive access to the inspected commands.
