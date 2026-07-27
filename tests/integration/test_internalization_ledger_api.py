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
HTTP_TIMEOUT_SECONDS = 120
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class InternalizationLedgerApiTests(unittest.TestCase):
    def test_ledger_query_audit_and_event_log_api(self) -> None:
        env_keys = [
            "ZYRA_SQLITE_PATH",
            "ZYRA_EVENT_LOG",
            "ZYRA_INTEGRATION_LEDGER",
            "ZYRA_TOOL_WORKSPACE",
            "ZYRA_ARTIFACT_ROOT",
        ]
        previous_env = {key: os.environ.get(key) for key in env_keys}
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
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

                listed = _get(base_url, "/ledger", {"source_repo": "claude-code-best", "limit": "2"})
                self.assertEqual(len(listed["entries"]), 2)
                self.assertGreaterEqual(listed["summary"]["total_entries"], 800)

                advanced_list = _get(base_url, "/ledger", {"target_verdict": "effective", "limit": "2"})
                self.assertIn("selection", advanced_list)
                self.assertGreaterEqual(advanced_list["selection"]["total_matches"], 1)

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

                readiness = _get(base_url, "/ledger/readiness", {"unit": "M1-01A"})
                self.assertEqual(readiness["owner_unit"], "M1-01A")

                report = _get(base_url, "/ledger/report", {"unit": "M1-01A"})
                self.assertIn("unit_matrix", report)
                self.assertIn("coverage", report)
                self.assertIn("accounting", report)

                accounting = _get(base_url, "/ledger/accounting", {"unit": "M1-01A", "no_entries": "1"})
                self.assertEqual(accounting["summary"]["owner_unit"], "M1-01A")
                self.assertIn("source_accounts", accounting)
                self.assertIn("unit_accounts", accounting)

                missing_linecount = _get_error(base_url, "/ledger/linecount")
                self.assertEqual(missing_linecount["error"], "missing_base")

                snapshot = _post(base_url, "/ledger/snapshots", {"label": "api-test", "owner_unit": "M1-01A"})
                self.assertTrue(Path(snapshot["path"]).exists())

                snapshots = _get(base_url, "/ledger/snapshots")
                self.assertTrue(snapshots["snapshots"])

                events = _get(base_url, "/events", {"limit": "5"})
                audit_events = [
                    event
                    for event in events["events"]
                    if "integration_ledger_audit" in event.get("payload", {})
                ]
                self.assertTrue(audit_events)
                self.assertEqual(audit_events[-1]["event_type"], "system_notice")

                invalid = _post_error(
                    base_url,
                    "/ledger/entries",
                    {
                        "ledger_id": "invalid-entry",
                        "source_repo": "claude-code-best",
                        "source_path": "src/QueryEngine.ts",
                        "capability_name": "invalid",
                        "capability_summary": "missing target and tests",
                        "target_bindings": [],
                        "migration_strategy": "adapter",
                        "main_path_status": "api_connected",
                        "lifecycle": "active",
                    },
                )
                self.assertEqual(invalid["error"], "invalid_ledger_entry")
                self.assertFalse(invalid["validation"]["ok"])

                updated_list = _get(base_url, "/ledger", {"q": "invalid", "limit": "10"})
                self.assertFalse(any(entry["ledger_id"] == "invalid-entry" for entry in updated_list["entries"]))

                advance_target = listed["entries"][0]["ledger_id"]
                advanced = _post(
                    base_url,
                    f"/ledger/{advance_target}/advance",
                    {
                        "lifecycle": "in_progress",
                        "reason": "api integration test moves planned entry",
                        "actor": "api-test",
                    },
                )
                self.assertTrue(advanced["ok"])
                self.assertIsNotNone(advanced["event"])
                events_after_advance = _get(base_url, "/events", {"limit": "20"})
                mutation_events = [
                    event
                    for event in events_after_advance["events"]
                    if "integration_ledger_update" in event.get("payload", {})
                ]
                self.assertTrue(mutation_events)
                self.assertEqual(mutation_events[-1]["payload"]["integration_ledger_update"]["ledger_id"], advance_target)

                reachability = _get(base_url, "/ledger/reachability", {"unit": "M1-01A", "no_entries": "1"})
                self.assertIn("route_count", reachability)

                boundary = _get(base_url, "/ledger/boundary", {"no_tests": "1", "roots": "packages"})
                self.assertIn("summary", boundary)

                acceptance = _get(base_url, "/ledger/acceptance", {"unit": "M1-01A", "no_entries": "1", "roots": "packages"})
                self.assertEqual(acceptance["owner_unit"], "M1-01A")

                persistence = _get(base_url, "/ledger/persistence")
                self.assertIn("revision", persistence)

                cleanroom = _get(base_url, "/ledger/cleanroom", {"no_source_scan": "1", "roots": "packages"})
                self.assertIn("commands_to_run", cleanroom)
                self.assertIn("copy_plan", cleanroom)

                semantic = _get(base_url, "/ledger/semantic-effects")
                self.assertEqual(semantic["total_probes"], 4)
                self.assertTrue(semantic["ok"])

                test_quality = _get(base_url, "/ledger/test-quality", {"unit": "M1-01A", "no_entries": "1"})
                self.assertIn("checked_test_entries", test_quality)
                self.assertIn("warning_findings", test_quality)

                schema = _get(base_url, "/ledger/schema-contract", {"unit": "M1-01A", "no_entries": "1"})
                self.assertIn("contract_version", schema)
                self.assertIn("field_contracts", schema)

                graph = _get(base_url, "/ledger/evidence-graph", {"unit": "M1-01A", "no_nodes": "1"})
                self.assertIn("target_impacts", graph)
                self.assertIn("summary", graph)

                custody = _get(base_url, "/ledger/state-custody")
                self.assertIn("claims", custody)
                self.assertIn("module_signals", custody)

                mutation = _get(base_url, "/ledger/mutation-consistency")
                self.assertTrue(mutation["ok"])
                self.assertIn("probes", mutation)

                policy = _get(base_url, "/ledger/policy-matrix", {"unit": "M1-01A", "no_decisions": "1"})
                self.assertIn("coverage", policy)
                self.assertIn("summary", policy)

                review = _get(base_url, "/ledger/unit-review", {"unit": "M1-01A", "minimum_effective_lines": "0", "no_reports": "1"})
                self.assertIn("objectives", review)
                self.assertIn("evidence", review)
            finally:
                if "server" in locals():
                    server.shutdown()
                    server.server_close()
                if "thread" in locals():
                    thread.join(timeout=5)
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


def _get(base_url: str, path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    suffix = ""
    if query:
        suffix = "?" + urllib.parse.urlencode(query)
    with urllib.request.urlopen(
        f"{base_url}{path}{suffix}",
        timeout=HTTP_TIMEOUT_SECONDS,
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_error(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS)
    except Exception as error:
        response = getattr(error, "fp", None)
        if response is None:
            raise
        return json.loads(response.read().decode("utf-8"))
    raise AssertionError("expected HTTP error")


def _get_error(base_url: str, path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    suffix = ""
    if query:
        suffix = "?" + urllib.parse.urlencode(query)
    try:
        urllib.request.urlopen(f"{base_url}{path}{suffix}", timeout=30)
    except Exception as error:
        response = getattr(error, "fp", None)
        if response is None:
            raise
        return json.loads(response.read().decode("utf-8"))
    raise AssertionError("expected HTTP error")


if __name__ == "__main__":
    unittest.main()
