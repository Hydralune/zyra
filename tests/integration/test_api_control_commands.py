from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ApiControlCommandTests(unittest.TestCase):
    def test_change_command_creates_requirement_change_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Handle a live change.", "auto_run": False})
                task_id = created["task"]["task_id"]
                changed = _post(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {"text": "/change tighten verification criteria"},
                )

                self.assertEqual(changed["event"]["event_type"], "requirement_change")
                self.assertEqual(changed["command"]["name"], "/change")
                self.assertEqual(changed["command_result"]["runtime_status"], "stateful")
                self.assertEqual(
                    changed["task"]["metadata"]["requirement_changes"][0]["text"],
                    "tighten verification criteria",
                )
                self.assertTrue(changed["task"]["metadata"]["requirement_changes"][0]["affected_node_ids"])
                self.assertIn("replan_node_id", changed["task"]["metadata"]["requirement_changes"][0])
                self.assertEqual(changed["command_result"]["data"]["latest_decision"]["decision_type"], "requirement_change_replan")
                task_events = _get(base_url, f"/tasks/{task_id}/events")["events"]
                self.assertTrue(any(event["event_type"] == "topology_route" for event in task_events))
                self.assertTrue(any(event["event_type"] == "constraint_check" for event in task_events))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_control_surface_lists_commands_skills_tools_and_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                commands = {command["name"] for command in _get(base_url, "/commands")["commands"]}
                skills = {skill["name"] for skill in _get(base_url, "/skills")["skills"]}
                tools = {tool["name"] for tool in _get(base_url, "/tools")["tools"]}
                workers = {worker["name"] for worker in _get(base_url, "/workers")["workers"]}
                browser_actions = _get(base_url, "/workers/browser/actions")
                browser_health = _get(base_url, "/workers/browser/health")
                permission_health = _get(base_url, "/permissions/health")
                code_inventory = _get(base_url, "/workers/code/inventory")

                self.assertIn("/change", commands)
                self.assertIn("/clear", commands)
                self.assertIn("/context", commands)
                self.assertIn("/usage", commands)
                self.assertIn("/skills", commands)
                self.assertIn("/goal", commands)
                self.assertIn("/team-onboarding", commands)
                self.assertIn("web-research", skills)
                self.assertIn("browser", tools)
                self.assertIn("CodeWorkerRuntime", workers)
                self.assertIn("open_url", {action["action"] for action in browser_actions["actions"]})
                self.assertGreater(len(browser_actions["source_registered_actions"]), 10)
                self.assertTrue(browser_health["environment_configured"])
                if browser_health["importable"]:
                    self.assertEqual(browser_health["classes"]["BrowserSession"], "BrowserSession")
                else:
                    self.assertEqual(browser_health["error_type"], "ModuleNotFoundError")
                self.assertEqual(code_inventory["source"], "zyra-claude-productized")
                self.assertEqual(code_inventory["upstreamSource"], "claude-code-best")
                self.assertEqual(code_inventory["ownerUnit"], "M1-02A")
                self.assertFalse(code_inventory["cleanRuntime"]["requiresRootSourceRepo"])
                self.assertFalse(code_inventory["cleanRuntime"]["requiresNodeSidecar"])
                self.assertFalse(code_inventory["cleanRuntime"]["requiresVendorRuntime"])
                self.assertEqual(code_inventory["moduleEntrypoints"]["queryEngine"], "zyra_runtime.ZyraClaudeQueryEngine")
                self.assertEqual(code_inventory["health"]["vendor"]["complete"], False)
                self.assertFalse(code_inventory["defaultPath"]["requiresRootSourceRepo"])
                self.assertGreaterEqual(len(code_inventory["sourceToTarget"]), 10)
                self.assertNotIn("integration", permission_health["metrics"])
                self.assertTrue(
                    permission_health["metrics"]["integration_details_require_custody"]
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_skill_endpoint_records_invocation_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Record skill invocation.", "auto_run": False})
                task_id = created["task"]["task_id"]
                invoked = _post(
                    base_url,
                    f"/tasks/{task_id}/skills",
                    {
                        "skill_name": "web-research",
                        "arguments": {"query": "dynamic heterogeneous agents"},
                    },
                )
                skills_view = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/skills"})

                self.assertEqual(invoked["event"]["event_type"], "skill_invoked")
                self.assertEqual(invoked["event"]["payload"]["skill_invocation"]["skill_name"], "web-research")
                self.assertEqual(invoked["skill_result"]["runtime_status"], "recorded")
                self.assertEqual(invoked["task"]["metadata"]["skill_invocations"][0]["skill_name"], "web-research")
                self.assertIn("browser", invoked["event"]["payload"]["skill_invocation"]["allowed_tools"])
                self.assertEqual(skills_view["command_result"]["summary"], "Registered skills and recent skill invocations.")
                self.assertEqual(
                    skills_view["command_result"]["data"]["skill_invocations"][0]["skill_name"],
                    "web-research",
                )
                events = _get(base_url, f"/tasks/{task_id}/events")["events"]
                self.assertTrue(any(event["event_type"] == "skill_invoked" for event in events))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_control_command_history_records_event_only_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Track a long-running goal.", "auto_run": False})
                task_id = created["task"]["task_id"]
                recorded = _post(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {"text": "/goal keep optimizing verifier evidence"},
                )

                self.assertEqual(recorded["event"]["event_type"], "control_command")
                self.assertEqual(recorded["command"]["metadata"]["category"], "extension_team")
                self.assertEqual(recorded["command_result"]["summary"], "/goal accepted and recorded for downstream runtime handling.")
                history = recorded["task"]["metadata"]["control_commands"]
                self.assertEqual(history[0]["name"], "/goal")
                self.assertEqual(history[0]["metadata"]["runtime_status"], "event_only")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_command_results_return_runtime_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Inspect command results.", "auto_run": False})
                task_id = created["task"]["task_id"]
                helped = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/help"})
                context = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/context"})
                mcp = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/mcp"})
                permissions = _post(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {"text": "/permissions"},
                )

                self.assertIn("context_session", helped["command_result"]["data"]["groups"])
                self.assertIn("/team-onboarding", helped["command_result"]["data"]["groups"]["extension_team"])
                self.assertEqual(context["command_result"]["data"]["control_commands"], 2)
                self.assertEqual(context["command_result"]["name"], "/context")
                self.assertEqual(
                    mcp["command_result"]["summary"],
                    "MCP runtime handoff contract from Zyra source graph crosswalk.",
                )
                self.assertEqual(mcp["command_result"]["data"]["owner_slice"], "M1-03B")
                self.assertFalse(mcp["command_result"]["data"]["requires_node_sidecar"])
                self.assertFalse(mcp["command_result"]["data"]["sidecar_contracts_used"])
                self.assertTrue(mcp["command_result"]["data"]["source_graph_ok"])
                self.assertTrue(mcp["command_result"]["data"]["contracts"])
                self.assertTrue(mcp["command_result"]["data"]["source_batches"])
                permission_data = permissions["command_result"]["data"]
                self.assertTrue(permission_data["custody_required_for_details"])
                self.assertNotIn("requests", permission_data)
                self.assertNotIn("rules", permission_data)
                self.assertNotIn("session_ids", permission_data)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_clear_context_and_rewind_are_stateful_session_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Exercise session commands.", "auto_run": False})
                task_id = created["task"]["task_id"]
                cleared = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/clear start focused session"})
                after_clear_context = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/context"})
                rewound = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/rewind latest"})
                after_rewind_context = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/context"})

                self.assertEqual(cleared["command_result"]["runtime_status"], "stateful")
                self.assertEqual(cleared["command_result"]["summary"], "Visible context cleared; durable event trace retained.")
                self.assertGreater(cleared["command_result"]["data"]["cleared_visible_events"], 0)
                self.assertEqual(after_clear_context["command_result"]["data"]["snapshot_count"], 1)
                self.assertLess(
                    after_clear_context["command_result"]["data"]["visible_events"],
                    after_clear_context["command_result"]["data"]["events"],
                )
                self.assertEqual(rewound["command_result"]["summary"], "Context rewound to a prior visible window.")
                self.assertEqual(rewound["command_result"]["data"]["active_session_id"], "session_initial")
                self.assertGreaterEqual(after_rewind_context["command_result"]["data"]["snapshot_count"], 2)
                self.assertGreater(
                    after_rewind_context["command_result"]["data"]["visible_events"],
                    after_clear_context["command_result"]["data"]["visible_events"],
                )
                self.assertIn("context_session", after_rewind_context["task"]["metadata"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_compact_and_export_commands_write_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Persist compact and export artifacts.", "auto_run": False})
                task_id = created["task"]["task_id"]
                compacted = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/compact focus on verifier evidence"})
                exported = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/export"})

                self.assertEqual(compacted["command_result"]["runtime_status"], "stateful")
                self.assertEqual(compacted["command_result"]["summary"], "Context compact summary artifact written.")
                self.assertEqual(exported["command_result"]["summary"], "Run export artifact written.")
                self.assertEqual(len(exported["task"]["metadata"]["compactions"]), 1)
                self.assertEqual(len(exported["task"]["metadata"]["exports"]), 1)
                self.assertEqual(len(_get(base_url, f"/tasks/{task_id}/artifacts")["artifacts"]), 2)

                compact_artifact_id = compacted["command_result"]["data"]["artifact"]["artifact"]["artifact_id"]
                export_artifact_id = exported["command_result"]["data"]["artifact"]["artifact"]["artifact_id"]
                compact_preview = _get(base_url, f"/artifacts/{compact_artifact_id}")["artifact"]["content"]
                export_preview = _get(base_url, f"/artifacts/{export_artifact_id}")["artifact"]["content"]

                self.assertIn("Zyra Context Compact", compact_preview)
                self.assertIn("verifier evidence", compact_preview)
                self.assertIn('"events"', export_preview)
                self.assertIn('"task"', export_preview)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_memory_fabric_endpoints_ingest_compact_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Exercise M4 memory fabric.", "auto_run": False})
                task_id = created["task"]["task_id"]
                _post(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "artifact_write",
                        "arguments": {
                            "title": "Verifier evidence",
                            "content": "requirement change evidence and trajectory replay notes",
                            "kind": "markdown",
                        },
                    },
                )
                _post(base_url, f"/tasks/{task_id}/skills", {"skill_name": "trace-summary"})
                _post(base_url, f"/tasks/{task_id}/commands", {"text": "/change add replay evidence"})
                _post(base_url, f"/tasks/{task_id}/commands", {"text": "/inject node=execute transient failure"})

                ingested = _post(base_url, f"/tasks/{task_id}/memory/ingest", {})
                memory = _get(base_url, f"/tasks/{task_id}/memory?q=replay")
                compacted = _post(
                    base_url,
                    f"/tasks/{task_id}/memory/compact",
                    {"focus": "replay evidence", "tail_groups": 3},
                )
                trajectory = _get(base_url, f"/tasks/{task_id}/trajectory")
                compactions = _get(base_url, f"/tasks/{task_id}/compactions")
                events_after_trajectory = _get(base_url, f"/tasks/{task_id}/events")["events"]
                command_memory = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/memory replay"})

                self.assertGreaterEqual(ingested["layer_counts"]["working"], 1)
                self.assertGreaterEqual(ingested["layer_counts"]["episodic"], 1)
                self.assertGreaterEqual(memory["layer_counts"]["semantic"], 1)
                self.assertTrue(memory["search_results"])
                self.assertTrue(compacted["compact"]["preserved_event_ids"])
                self.assertTrue(compacted["compact"]["summarized_event_ids"])
                self.assertTrue(compacted["compact"]["artifact_ids"])
                self.assertEqual(len(compactions["compactions"]), 1)
                self.assertEqual(trajectory["frame_count"], len(events_after_trajectory))
                self.assertTrue(any(frame["requirement_change"] for frame in trajectory["frames"]))
                self.assertTrue(any(frame["failure_injection"] for frame in trajectory["frames"]))
                self.assertEqual(command_memory["command_result"]["summary"], "MemoryFabric task memory view.")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_eval_command_runs_trace_evaluator_and_records_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Evaluate trace.", "auto_run": False})
                task_id = created["task"]["task_id"]
                _post(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "artifact_write",
                        "arguments": {"title": "Evidence", "content": "trace evidence"},
                    },
                )
                evaluated = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/eval"})

                self.assertEqual(evaluated["command_result"]["runtime_status"], "stateful")
                self.assertEqual(evaluated["command_result"]["summary"], "Trace evaluation completed.")
                self.assertGreater(evaluated["command_result"]["data"]["score"], 0)
                self.assertEqual(evaluated["task"]["metadata"]["evaluations"][0]["name"], "/eval")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_tool_endpoint_executes_workspace_file_tool_and_records_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Run a file tool.", "auto_run": False})
                task_id = created["task"]["task_id"]
                executed = _post(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "file_write",
                        "arguments": {"path": "notes/result.txt", "content": "tool output"},
                    },
                )

                self.assertTrue(executed["tool_result"]["ok"])
                self.assertEqual(executed["tool_result"]["output"]["relative_path"], "notes\\result.txt")
                self.assertEqual(executed["event"]["payload"]["tool_call"]["tool_name"], "file_write")
                self.assertGreaterEqual(len(_get(base_url, f"/tasks/{task_id}/events")["events"]), 2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_tool_endpoint_executes_workspace_web_search_and_records_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            docs = workspace / "research"
            docs.mkdir(parents=True)
            (docs / "brief.md").write_text(
                "Dynamic heterogeneous agents need traceable requirement change handling.",
                encoding="utf-8",
            )
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(workspace)
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")
            os.environ["ZYRA_PERMISSION_STATE"] = str(Path(tmpdir) / "permission-state.json")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Search local research.", "auto_run": False})
                task_id = created["task"]["task_id"]
                searched = _post(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "web_search",
                        "arguments": {"query": "requirement change", "paths": ["research"]},
                    },
                )

                self.assertTrue(searched["tool_result"]["ok"])
                self.assertEqual(searched["tool_result"]["output"]["result_count"], 1)
                self.assertEqual(len(searched["task"]["artifacts"]), 1)
                artifact_id = searched["task"]["artifacts"][0]["artifact_id"]
                preview = _get(base_url, f"/artifacts/{artifact_id}")["artifact"]["content"]
                self.assertIn("Zyra Research Search Results", preview)
                self.assertIn("requirement change", preview)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_tool_endpoint_browser_extracts_inline_html_and_records_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Extract inline browser state.", "auto_run": False})
                task_id = created["task"]["task_id"]
                browsed = _post(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "browser",
                        "arguments": {
                            "action": "extract_text",
                            "html": "<html><head><title>API Browser</title></head><body><p>Runtime browser evidence.</p></body></html>",
                        },
                    },
                )

                self.assertTrue(browsed["tool_result"]["ok"])
                self.assertEqual(browsed["tool_result"]["output"]["state"]["title"], "API Browser")
                self.assertIn("Runtime browser evidence", browsed["tool_result"]["output"]["state"]["text_preview"])
                self.assertEqual(len(browsed["task"]["artifacts"]), 2)
                artifact_id = browsed["task"]["artifacts"][0]["artifact_id"]
                preview = _get(base_url, f"/artifacts/{artifact_id}")["artifact"]["content"]
                self.assertIn("API Browser", preview)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_tool_endpoint_trace_reads_task_events_and_can_write_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Read task trace.", "auto_run": False})
                task_id = created["task"]["task_id"]
                traced = _post(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "trace",
                        "arguments": {"limit": 3, "write_artifact": True},
                    },
                )

                self.assertTrue(traced["tool_result"]["ok"])
                self.assertGreaterEqual(traced["tool_result"]["output"]["event_count"], 1)
                self.assertEqual(traced["tool_result"]["output"]["returned_count"], 3)
                self.assertEqual(len(traced["task"]["artifacts"]), 1)
                artifact_id = traced["task"]["artifacts"][0]["artifact_id"]
                preview = _get(base_url, f"/artifacts/{artifact_id}")["artifact"]["content"]
                self.assertIn("event_type", preview)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_tool_endpoint_checkpoint_reads_task_state_and_can_write_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Read checkpoint.", "auto_run": False})
                task_id = created["task"]["task_id"]
                checkpoint = _post(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "checkpoint",
                        "arguments": {"include_state": True, "write_artifact": True},
                    },
                )

                self.assertTrue(checkpoint["tool_result"]["ok"])
                self.assertEqual(checkpoint["tool_result"]["output"]["summary"]["task_id"], task_id)
                self.assertIn("checkpoint", checkpoint["tool_result"]["output"])
                self.assertEqual(len(checkpoint["task"]["artifacts"]), 1)
                artifact_id = checkpoint["task"]["artifacts"][0]["artifact_id"]
                preview = _get(base_url, f"/artifacts/{artifact_id}")["artifact"]["content"]
                self.assertIn(task_id, preview)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_code_worker_endpoint_executes_tool_plan_and_records_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Run CodeWorker.", "auto_run": False})
                task_id = created["task"]["task_id"]
                executed = _post(
                    base_url,
                    f"/tasks/{task_id}/workers/code",
                    {
                        "tool_plan": [
                            {
                                "tool_name": "file_write",
                                "arguments": {"path": "worker/output.txt", "content": "from worker"},
                            },
                            {"tool_name": "file_read", "arguments": {"path": "worker/output.txt"}},
                        ],
                    },
                )

                self.assertTrue(executed["worker_result"]["ok"])
                tool_events = [event for event in executed["events"] if "tool_result" in event["payload"]]
                query_events = [event for event in executed["events"] if "query_session" in event["payload"]]
                worker_events = [event for event in executed["events"] if "worker_result" in event["payload"]]
                self.assertEqual(len(tool_events), 2)
                self.assertGreaterEqual(len(query_events), 4)
                self.assertEqual(len(worker_events), 1)
                self.assertEqual(executed["task"]["budget"]["tool_calls"], 2)
                metadata = executed["worker_result"]["metadata"]
                self.assertEqual(metadata["vendor_complete"], "false")
                self.assertEqual(metadata["sidecar_contracts_used"], "false")
                self.assertEqual(metadata["productized_runtime_clean_safe"], "true")
                self.assertEqual(metadata["productized_runtime_owner_unit"], "M1-02A")
                self.assertEqual(
                    metadata["loop"],
                    "zyra_claude_query_engine_runtime",
                )
                self.assertEqual(metadata["query_contract_source"], "zyra-claude-productized")
                self.assertEqual(metadata["query_contract_write_serial"], "true")
                self.assertEqual(metadata["tool_runtime_completed"], "2")
                self.assertEqual(metadata["query_plan_ok"], "true")
                self.assertEqual(metadata["runtime_state_ok"], "true")
                self.assertEqual(metadata["query_turns"], "1")
                self.assertEqual(metadata["context_compactions"], "0")
                self.assertEqual(metadata["query_session_consistent"], "true")
                self.assertTrue(metadata["query_session_resume_token"].startswith("codesession_"))
                self.assertIn("last_code_worker_session", executed["task"]["metadata"])
                self.assertEqual(
                    executed["task"]["metadata"]["last_code_worker_session"]["session_id"],
                    metadata["query_session_id"],
                )
                self.assertTrue((Path(tmpdir) / "workspace" / "worker" / "output.txt").exists())
                self.assertGreaterEqual(len(_get(base_url, f"/tasks/{task_id}/events")["events"]), 4)

                artifacts = _get(base_url, f"/tasks/{task_id}/artifacts")["artifacts"]
                self.assertGreaterEqual(len(artifacts), 3)
                by_title = {item["artifact"]["title"]: item["artifact"]["artifact_id"] for item in artifacts}
                trace_id = next(artifact_id for title, artifact_id in by_title.items() if "CodeWorker trace" in title)
                snapshot_id = metadata["query_session_snapshot_artifact_id"]
                transcript_id = metadata["query_session_transcript_artifact_id"]
                preview = _get(base_url, f"/artifacts/{trace_id}")["artifact"]
                snapshot = _get(base_url, f"/artifacts/{snapshot_id}")["artifact"]
                transcript = _get(base_url, f"/artifacts/{transcript_id}")["artifact"]
                self.assertIn("CodeWorker Runtime Trace", preview["content"])
                self.assertIn("Query Session Contract Sources", preview["content"])
                self.assertIn('"consistency"', snapshot["content"])
                self.assertIn('"type": "session_metadata"', transcript["content"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_browser_worker_endpoint_extracts_local_page_and_records_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            page = workspace / "page.html"
            page.write_text(
                "<html><head><title>Fixture</title></head><body><h1>Zyra Browser</h1><p>Visible browser text.</p></body></html>",
                encoding="utf-8",
            )
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(workspace)
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")
            os.environ["ZYRA_PERMISSION_STATE"] = str(Path(tmpdir) / "permission-state.json")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Run BrowserWorker.", "auto_run": False})
                task_id = created["task"]["task_id"]
                executed = _post(
                    base_url,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "browser_plan": [
                            {"action": "open_url", "arguments": {"url": page.resolve().as_uri()}},
                            {"action": "extract_text"},
                        ],
                        "constraints": {"allowed_schemes": ["file"]},
                    },
                )

                self.assertTrue(executed["worker_result"]["ok"])
                browser_events = [
                    event
                    for event in executed["events"]
                    if isinstance(event.get("payload", {}).get("browser_result"), dict)
                ]
                self.assertEqual(len(browser_events), 2)
                self.assertEqual(executed["task"]["budget"]["tool_calls"], 2)
                self.assertEqual(executed["worker_result"]["metadata"]["vendor"], "browser-use")
                self.assertIn(
                    "Visible browser text.",
                    browser_events[1]["payload"]["browser_result"]["output"]["text_preview"],
                )
                self.assertTrue(executed["permission_session"]["custody_created"])
                token = executed["permission_session"]["custody_token"]
                self.assertEqual(executed["worker_request"]["constraints"].get("permission_session_custody_token"), None)
                self.assertNotIn(
                    token,
                    Path(os.environ["ZYRA_PERMISSION_STATE"]).read_text(encoding="utf-8"),
                )
                self.assertGreaterEqual(len(_get(base_url, f"/tasks/{task_id}/events")["events"]), 4)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_browser_worker_permission_api_resumes_exact_network_action_once(self) -> None:
        class PageHandler(BaseHTTPRequestHandler):
            hit_count = 0

            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name.
                type(self).hit_count += 1
                body = b"<html><head><title>Approved</title></head><body>exact browser approval</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
            os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission-state.json")
            os.environ["ZYRA_PERMISSION_STORE"] = str(root / "legacy-permissions.json")

            from apps.api.zyra_api.main import ZyraRequestHandler

            page_server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
            page_thread = threading.Thread(target=page_server.serve_forever, daemon=True)
            page_thread.start()
            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            target_url = f"http://127.0.0.1:{page_server.server_address[1]}/approved"
            try:
                created = _post(base_url, "/tasks", {"goal": "Approve one browser request.", "auto_run": False})
                task = created["task"]
                plan = [{"action": "open_url", "arguments": {"url": target_url}}]
                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/browser",
                    {"browser_plan": plan},
                )

                self.assertEqual(first_status, 409)
                self.assertEqual(PageHandler.hit_count, 0)
                first_browser_events = [
                    event
                    for event in first["events"]
                    if isinstance(event.get("payload", {}).get("browser_result"), dict)
                ]
                self.assertEqual(
                    first_browser_events[0]["payload"]["browser_result"]["error"],
                    "permission_required",
                )
                session = first["permission_session"]
                self.assertTrue(session["custody_created"])
                token = session["custody_token"]
                identity = {
                    "session_id": session["session_id"],
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                }
                headers = {"Authorization": f"Bearer {token}"}
                pending = _get(
                    base_url,
                    (
                        "/permissions/requests"
                        f"?session_id={session['session_id']}&run_id={task['run_id']}"
                        f"&task_id={task['task_id']}&pending_only=true"
                    ),
                    headers=headers,
                )["requests"]["items"]
                self.assertEqual(len(pending), 1)
                _post(
                    base_url,
                    f"/permissions/requests/{pending[0]['request_id']}/resolve",
                    {**identity, "effect": "allow", "idempotency_key": "approve-browser-exact"},
                    headers=headers,
                )

                retry_payload = {
                    "browser_plan": plan,
                    "constraints": {
                        "permission_session_id": session["session_id"],
                        "permission_session_custody_token": token,
                    },
                }
                second_status, second = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/browser",
                    retry_payload,
                )
                self.assertEqual(second_status, 201)
                self.assertTrue(second["worker_result"]["ok"])
                self.assertEqual(PageHandler.hit_count, 1)
                self.assertNotIn("custody_token", second["permission_session"])
                self.assertEqual(
                    second["worker_request"]["constraints"]["permission_session_custody_token"],
                    "<redacted>",
                )

                replay_status, replay = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/browser",
                    retry_payload,
                )
                self.assertEqual(replay_status, 409)
                replay_browser_events = [
                    event
                    for event in replay["events"]
                    if isinstance(event.get("payload", {}).get("browser_result"), dict)
                ]
                self.assertEqual(
                    replay_browser_events[0]["payload"]["browser_result"]["error"],
                    "permission_required",
                )
                self.assertEqual(PageHandler.hit_count, 1)
                for path in root.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                page_server.shutdown()
                page_server.server_close()
                page_thread.join(timeout=5)

    def test_structured_permission_api_authorizes_only_the_exact_shell_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")
            os.environ["ZYRA_PERMISSION_STORE"] = str(Path(tmpdir) / "permissions.json")
            os.environ["ZYRA_PERMISSION_STATE"] = str(Path(tmpdir) / "permission-state.json")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Approve shell.", "auto_run": False})
                task_id = created["task"]["task_id"]
                command = f'"{sys.executable}" -c "print(789)"'
                status, blocked = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {"tool_name": "shell", "arguments": {"command": command}},
                )

                self.assertEqual(status, 409)
                request_id = blocked["tool_result"]["metadata"]["permission_request_id"]
                session = blocked["permission_session"]
                retry_identity = blocked["permission_retry_identity"]
                custody_token = session["bearer_token"]
                run_id = created["task"]["run_id"]
                query_path = (
                    "/permissions/requests"
                    f"?session_id={session['session_id']}"
                    f"&run_id={run_id}&task_id={task_id}"
                )
                query_token_status, query_token_response = _get_with_status(
                    base_url,
                    f"{query_path}&custody_token={custody_token}",
                )
                self.assertEqual(query_token_status, 401)
                self.assertEqual(
                    query_token_response["error"],
                    "permission_api_authentication_failed",
                )
                permissions = _get(
                    base_url,
                    query_path,
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertEqual(
                    permissions["requests"]["items"][0]["request_id"],
                    request_id,
                )

                malformed_status, malformed = _post_with_status(
                    base_url,
                    f"/permissions/requests/{request_id}/resolve",
                    {
                        "session_id": session["session_id"],
                        "run_id": run_id,
                        "task_id": task_id,
                        "effect": "not-a-permission-effect",
                    },
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertEqual(malformed_status, 400)
                self.assertEqual(malformed["error"], "permission_transport_response_invalid")

                secret_status, secret_response = _post_with_status(
                    base_url,
                    f"/permissions/requests/{request_id}/resolve",
                    {
                        "session_id": session["session_id"],
                        "run_id": run_id,
                        "task_id": task_id,
                        "effect": "allow",
                        "idempotency_key": "must-not-resolve-secret-echo",
                        "reason": f"operator accidentally copied {custody_token}",
                        "metadata": {"note": custody_token},
                    },
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertEqual(secret_status, 403)
                self.assertEqual(secret_response["error"], "permission_api_authorization_failed")
                secret_key_status, secret_key_response = _post_with_status(
                    base_url,
                    f"/permissions/requests/{request_id}/resolve",
                    {
                        "session_id": session["session_id"],
                        "run_id": run_id,
                        "task_id": task_id,
                        "effect": "allow",
                        "idempotency_key": "must-not-resolve-secret-key",
                        "metadata": {custody_token: "persist-me"},
                    },
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertEqual(secret_key_status, 403)
                self.assertEqual(
                    secret_key_response["error"],
                    "permission_api_authorization_failed",
                )
                self.assertNotIn(
                    custody_token,
                    Path(os.environ["ZYRA_PERMISSION_STATE"]).read_text(encoding="utf-8"),
                )

                resolved = _post(
                    base_url,
                    f"/permissions/requests/{request_id}/resolve",
                    {
                        "session_id": session["session_id"],
                        "run_id": run_id,
                        "task_id": task_id,
                        "effect": "allow",
                        "idempotency_key": "approve-shell-once",
                    },
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertEqual(
                    resolved["result"]["request"]["status"],
                    "approved",
                )

                second_status, executed = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "shell",
                        "arguments": {"command": command},
                        **retry_identity,
                    },
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertEqual(second_status, 201)
                self.assertTrue(executed["tool_result"]["ok"])
                self.assertIn("789", executed["tool_result"]["output"]["stdout"])

                forged_status, forged = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "shell",
                        "arguments": {"command": f'{sys.executable} -c "print(999)"'},
                        **retry_identity,
                    },
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertNotEqual(forged_status, 201)
                self.assertFalse(forged.get("tool_result", {}).get("ok", False))
                task_events = _get(base_url, f"/tasks/{task_id}/events")["events"]
                permission_kinds = [
                    event.get("payload", {})
                    .get("query_session", {})
                    .get("permission_runtime", {})
                    .get("kind")
                    for event in task_events
                ]
                self.assertIn("permission_request_resolved", permission_kinds)
                self.assertIn("permission_execution_grant_consumed", permission_kinds)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_permission_session_mode_api_is_custody_bound_revisioned_and_no_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")
            os.environ["ZYRA_PERMISSION_STORE"] = str(Path(tmpdir) / "legacy-permissions.json")
            os.environ["ZYRA_PERMISSION_STATE"] = str(Path(tmpdir) / "permission-state.json")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Mode control.", "auto_run": False})
                task = created["task"]
                session_id = "api-mode-session"
                open_request = urllib.request.Request(
                    f"{base_url}/permissions/sessions/open",
                    data=json.dumps(
                        {
                            "session_id": session_id,
                            "run_id": task["run_id"],
                            "task_id": task["task_id"],
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(open_request, timeout=15) as response:
                    opened = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")
                    self.assertEqual(
                        response.headers["X-Zyra-Permission-State-Owner"],
                        "PermissionStateStore",
                    )
                token = opened["session"]["bearer_token"]
                identity = {
                    "session_id": session_id,
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                }
                headers = {"Authorization": f"Bearer {token}"}

                changed = _post(
                    base_url,
                    "/permissions/mode",
                    {**identity, "mode": "dont_ask", "expected_mode_revision": 0},
                    headers=headers,
                )
                self.assertEqual(changed["result"]["metadata"]["mode"]["revision"], 1)
                status, escalated = _post_with_status(
                    base_url,
                    "/permissions/mode",
                    {**identity, "mode": "auto"},
                    headers=headers,
                )
                self.assertEqual(status, 403)
                self.assertEqual(escalated["error"], "forbidden")

                selected = _get(
                    base_url,
                    (
                        "/permissions/mode"
                        f"?session_id={session_id}&run_id={task['run_id']}"
                        f"&task_id={task['task_id']}"
                    ),
                    headers=headers,
                )
                self.assertEqual(selected["mode"], "dont_ask")
                state_text = Path(os.environ["ZYRA_PERMISSION_STATE"]).read_text(encoding="utf-8")
                self.assertNotIn(token, state_text)
                self.assertFalse(Path(os.environ["ZYRA_PERMISSION_STORE"]).exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_code_worker_permission_approval_resumes_exact_parked_call_via_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
            os.environ["ZYRA_PERMISSION_STORE"] = str(root / "legacy-permissions.json")
            os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission-state.json")

            from apps.api.zyra_api.main import ZyraRequestHandler

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Resume exact code action.", "auto_run": False})
                task = created["task"]
                session_id = "api-code-permission-session"
                command = subprocess.list2cmdline(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('api-code-approved.txt').write_text('approved', encoding='utf-8')",
                    ]
                )
                plan = [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "api-code-shell-use-1",
                        "arguments": {"command": command},
                    }
                ]
                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/code",
                    {"constraints": {"session_id": session_id, "tool_plan": plan}},
                )
                self.assertEqual(first_status, 409)
                self.assertEqual(first["worker_result"]["error"], "permission_suspended")
                session = first["permission_session"]
                token = session["session_custody_token"]
                self.assertTrue(session["session_custody_token_included"])
                self.assertEqual(first["worker_request"]["constraints"].get("session_custody_token"), None)
                self.assertFalse((root / "workspace" / "api-code-approved.txt").exists())

                identity = {
                    "session_id": session_id,
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                }
                headers = {"Authorization": f"Bearer {token}"}
                pending = _get(
                    base_url,
                    (
                        "/permissions/requests"
                        f"?session_id={session_id}&run_id={task['run_id']}"
                        f"&task_id={task['task_id']}&pending_only=true"
                    ),
                    headers=headers,
                )["requests"]["items"]
                self.assertEqual(len(pending), 1)
                request_id = pending[0]["request_id"]
                _post(
                    base_url,
                    f"/permissions/requests/{request_id}/resolve",
                    {**identity, "effect": "allow", "idempotency_key": "approve-api-code"},
                    headers=headers,
                )

                second_status, second = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/code",
                    {
                        "constraints": {
                            "session_id": session_id,
                            "session_custody_token": token,
                            "tool_plan": plan,
                        }
                    },
                )
                self.assertEqual(second_status, 201)
                self.assertTrue(second["worker_result"]["ok"])
                self.assertFalse(second["permission_session"]["session_custody_token_included"])
                self.assertEqual(
                    second["worker_request"]["constraints"]["session_custody_token"],
                    "<redacted>",
                )
                self.assertEqual(
                    (root / "workspace" / "api-code-approved.txt").read_text(encoding="utf-8"),
                    "approved",
                )
                for path in root.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def _get(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        headers=headers or {},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_with_status(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        headers=headers or {},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _post(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_with_status(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
