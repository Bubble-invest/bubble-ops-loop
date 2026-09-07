"""Independent consumer checks against the actual installed JavaScript block."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("watcher", HERE / "install_inject_watcher.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
spec2 = importlib.util.spec_from_file_location("transport", HERE / "agent_message.py")
m = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(m)


@unittest.skipUnless(shutil.which("node"), "Node required for consumer runtime tests")
class WatcherTests(unittest.TestCase):
    def exercise(self, mutation=None, reject=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            inbox = root / "inbox"
            inbox.mkdir(mode=0o700)
            legacy = root / "inject"
            legacy.write_text("stale human-authority message\n")
            message = {"from": "ben", "to": "miranda", "id": "00000000-0000-4000-8000-000000000001",
                       "in_reply_to": None, "text": "Review please"}
            m.receive(json.dumps(message).encode(), "ben", "miranda", str(inbox))
            queued = inbox / (message["id"] + ".json")
            if mutation:
                event = json.loads(queued.read_text())
                event.update(mutation)
                queued.write_text(json.dumps(event))
            route = {"inbox": str(inbox), "sender": "ben", "recipient": "miranda", "chat_id": "123"}
            prelude = """
let tick; const sent=[];
globalThis.setInterval=(callback)=>{tick=callback;return {unref(){}}};
const mcp={notification:async event=>{ __REJECT__ ; sent.push(event)}};
""".replace("__REJECT__", "throw Error('simulated notification failure')" if reject else "")
            script = root / "watch.mjs"
            script.write_text(prelude + w.TEMPLATE.replace("__ROUTE__", json.dumps(route)) + """
tick(); await new Promise(setImmediate);
tick(); await new Promise(setImmediate);
console.log(JSON.stringify(sent));
""")
            result = subprocess.run(["node", str(script)], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            sent = json.loads(result.stdout)
            self.assertEqual(legacy.read_text(), "stale human-authority message\n")
            if reject or mutation:
                self.assertEqual(sent, [])
                self.assertTrue(queued.exists())
            else:
                self.assertEqual(len(sent), 1)
                self.assertFalse(queued.exists())
                self.assertTrue(queued.with_suffix(".delivered").exists())
                self.assertEqual(sent[0]["params"]["meta"]["user"], "ben")
                self.assertEqual(sent[0]["params"]["meta"]["source"], "bubble-agent-message")

    def test_delivery_metadata_and_no_duplicate_tick(self):
        self.exercise()

    def test_wrong_sender_rejected_and_legacy_untouched(self):
        self.exercise({"from": "joris"})

    def test_failed_notification_keeps_queued_event(self):
        self.exercise(reject=True)

    def test_installer_idempotence_and_route_drift_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            server = root / "server.ts"
            original = w.ANCHOR + "\n// next line\n"
            server.write_text(original)
            self.assertIn("installed", w.install(server, root / "inbox", "ben", "miranda", "123"))
            content = server.read_text()
            self.assertEqual(w.install(server, root / "inbox", "ben", "miranda", "123"), "already installed")
            self.assertEqual(server.read_text(), content)
            with self.assertRaises(ValueError):
                w.install(server, root / "other", "ben", "miranda", "123")
            self.assertEqual((root / "server.ts.bak-agent-message").read_text(), original)


if __name__ == "__main__":
    unittest.main()
