from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BLOCK = ROOT / "deploy" / "telegram-plugin" / "bubble-inject.block.ts"
JS_RUNTIME = shutil.which("node") or shutil.which("bun")


@pytest.mark.skipif(JS_RUNTIME is None, reason="node/bun unavailable")
def test_synthetic_maintenance_writer_drains_without_peer_identity(tmp_path: Path):
    inject = tmp_path / "inject"
    inject.write_text("synthetic maintenance wake\n")
    source = BLOCK.read_text()
    begin = "// === BUBBLE-INJECT PATCH BEGIN ==="
    end = "// === BUBBLE-INJECT PATCH END ==="
    block = source[source.index(begin) + len(begin):source.index(end)]
    block = block.replace("(err: unknown)", "(err)")
    harness = f"""
const notifications = []
const mcp = {{ notification: async (event) => {{ notifications.push(event) }} }}
const process = {{
  env: {{ TELEGRAM_STATE_DIR: {json.dumps(str(tmp_path))}, BUBBLE_INJECT_AS: 'operator-fixture' }},
  stderr: {{ write: (_text) => true }}
}}
const setInterval = (fn, _ms) => {{ fn(); return {{ unref: () => undefined }} }}
{block}
await new Promise(resolve => setTimeout(resolve, 30))
const fs = await import('node:fs')
if (fs.readFileSync({json.dumps(str(inject))}, 'utf8') !== '') throw new Error('inject not drained')
if (notifications.length !== 1) throw new Error('wrong notification count')
const event = notifications[0]
if (event.method !== 'notifications/claude/channel') throw new Error('wrong method')
if (event.params.content !== 'synthetic maintenance wake') throw new Error('wrong content')
if (event.params.meta.source !== 'bubble-inject') throw new Error('wrong source')
if (event.params.meta.user !== 'operator' || event.params.meta.user_id !== 'operator-fixture') throw new Error('wrong operator provenance')
if (event.params.meta.source === 'bubble-agent-message') throw new Error('peer identity forged')
"""
    module = tmp_path / "consumer-fixture.mjs"
    module.write_text(harness)
    result = subprocess.run(
        [JS_RUNTIME, str(module)],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
