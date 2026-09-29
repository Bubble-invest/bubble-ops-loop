"""console_client.py — in-process access to bubble-ops-console, the SAME way
console/tests/conftest.py's `client` fixture does: a FastAPI TestClient over
`console.main.create_app()`, authenticated with the console's own existing
`CONSOLE_BEARER_TOKEN` bearer header (board #997's "header-bearer API clients"
principal — console/settings.py `BEARER_TOKEN`). No new auth bypass, no new
credential — this reuses the console's own documented internal-API path
(operator-intent: verify-fleet-standard-first, never duplicate a read path).

Never prints/logs the token: it is read once from the environment and handed
straight to the TestClient's Authorization header, never placed in a return
value, a log line, or an evidence field.

Degrades to `(None, <reason>)` — never raises — when the token is missing or
the console app cannot be imported/built (e.g. run from a checkout without
FastAPI installed). Callers (collect.py) record that reason as
`console_client_error` and still run whatever checks don't need the client.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent


def build_client(bearer_token_env: str = "CONSOLE_BEARER_TOKEN") -> Tuple[Optional[object], Optional[str]]:
    """Returns (client, error). `error` is None on success. NEVER raises."""
    token = os.environ.get(bearer_token_env, "")
    if not token:
        return None, (
            f"{bearer_token_env} is not set in the environment — refusing to "
            f"call the console without the fleet's own bearer auth (no new "
            f"auth bypass). Set it via the deploy EnvironmentFile."
        )

    import sys
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    try:
        from fastapi.testclient import TestClient
        from console.main import create_app
    except Exception as exc:  # noqa: BLE001 — import-time failure is evidence, not ours to fix here
        return None, f"could not import the console app: {exc}"

    try:
        app = create_app()
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {token}"})
        return client, None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not build the console TestClient: {exc}"
