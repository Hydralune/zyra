from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class InternalizationLedgerApiTests(unittest.TestCase):
    def test_ledger_query_audit_and_event_log_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_INTEGRATION_LEDGER"] = str(Path(tmpdir) / "internalization_ledger.json")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                listed = _get(base_url, "/ledger", {"source_repo": "claude-code-best", "limit": "2"})
                self.assertEqual(len(listed["entries"]), 2)
                self.assertGreaterEqual(listed["summary"]["total_entries"], 800)

                ledger_id = listed["entries"][0]["ledger_id"]
                shown = _get(base_url, f"/integrations/ledger/{ledger_id}")
                self.assertEqual(shown["entry"]["ledger_id"], ledger_id)

                audit = _get(base_url, "/ledger/audit", {"severity": "error"})
                self.assertTrue(audit["ok"])
                self.assertEqual(audit["error_count"], 0)
                self.assertEqual(audit["filtered_finding_count"], 0)

                posted = _post(base_url, "/integrations/ledger/audit", {"strict": True, "write_event": True})
                self.assertTrue(posted["audit"]["ok"])
                self.assertTrue(posted["audit"]["event_written"])
                self.assertEqual(posted["event"]["payload"]["integration_ledger_audit"]["trigger"], "api")

                events = _get(base_url, "/events", {"limit": "5"})
                audit_events = [
                    event
                    for event in events["events"]
                    if "integration_ledger_audit" in event.get("payload", {})
                ]
                self.assertTrue(audit_events)
                self.assertEqual(audit_events[-1]["event_type"], "system_notice")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def _get(base_url: str, path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    suffix = ""
    if query:
        suffix = "?" + urllib.parse.urlencode(query)
    with urllib.request.urlopen(f"{base_url}{path}{suffix}", timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
