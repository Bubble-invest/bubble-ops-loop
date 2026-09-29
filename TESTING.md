# Testing

Create a Python 3.12 virtual environment and install the test dependencies:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r scripts/requirements.txt -r console/requirements.txt pytest
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
