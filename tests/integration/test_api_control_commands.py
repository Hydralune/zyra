from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
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
                self.assertTrue(browser_health["importable"])
                self.assertEqual(browser_health["classes"]["BrowserSession"], "BrowserSession")
                self.assertEqual(code_inventory["source"], "claude-code-best")
                self.assertGreater(code_inventory["commandRuntime"]["commandCount"], 20)
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

                self.assertIn("context_session", helped["command_result"]["data"]["groups"])
                self.assertIn("/team-onboarding", helped["command_result"]["data"]["groups"]["extension_team"])
                self.assertEqual(context["command_result"]["data"]["control_commands"], 2)
                self.assertEqual(context["command_result"]["name"], "/context")
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

    @unittest.skipIf(shutil.which("node") is None, "node is required for code-worker sidecar")
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
                self.assertEqual(executed["worker_result"]["metadata"]["vendor_complete"], "true")
                self.assertEqual(
                    executed["worker_result"]["metadata"]["loop"],
                    "claude_code_query_engine_contract_loop",
                )
                self.assertEqual(executed["worker_result"]["metadata"]["query_contract_source"], "claude-code-best")
                self.assertEqual(executed["worker_result"]["metadata"]["query_contract_write_serial"], "true")
                self.assertEqual(executed["worker_result"]["metadata"]["query_turns"], "1")
                self.assertEqual(executed["worker_result"]["metadata"]["context_compactions"], "0")
                self.assertEqual(executed["worker_result"]["metadata"]["query_session_consistent"], "true")
                self.assertTrue(executed["worker_result"]["metadata"]["query_session_resume_token"].startswith("codesession_"))
                self.assertIn("last_code_worker_session", executed["task"]["metadata"])
                self.assertEqual(
                    executed["task"]["metadata"]["last_code_worker_session"]["session_id"],
                    executed["worker_result"]["metadata"]["query_session_id"],
                )
                self.assertTrue((Path(tmpdir) / "workspace" / "worker" / "output.txt").exists())
                self.assertGreaterEqual(len(_get(base_url, f"/tasks/{task_id}/events")["events"]), 4)

                artifacts = _get(base_url, f"/tasks/{task_id}/artifacts")["artifacts"]
                self.assertGreaterEqual(len(artifacts), 3)
                by_title = {item["artifact"]["title"]: item["artifact"]["artifact_id"] for item in artifacts}
                trace_id = next(artifact_id for title, artifact_id in by_title.items() if "CodeWorker trace" in title)
                snapshot_id = executed["worker_result"]["metadata"]["query_session_snapshot_artifact_id"]
                transcript_id = executed["worker_result"]["metadata"]["query_session_transcript_artifact_id"]
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
                self.assertEqual(len(executed["events"]), 3)
                self.assertEqual(executed["task"]["budget"]["tool_calls"], 2)
                self.assertEqual(executed["worker_result"]["metadata"]["vendor"], "browser-use")
                self.assertIn(
                    "Visible browser text.",
                    executed["events"][1]["payload"]["browser_result"]["output"]["text_preview"],
                )
                self.assertGreaterEqual(len(_get(base_url, f"/tasks/{task_id}/events")["events"]), 4)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_permission_api_records_and_resolves_shell_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")
            os.environ["ZYRA_PERMISSION_STORE"] = str(Path(tmpdir) / "permissions.json")

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
                permissions = _get(base_url, "/permissions")
                self.assertEqual(permissions["requests"][0]["request_id"], request_id)

                resolved = _post(
                    base_url,
                    f"/permissions/requests/{request_id}/resolve",
                    {"status": "approved", "create_rule": True},
                )
                self.assertEqual(resolved["request"]["status"], "approved")

                allowed = _post(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {"tool_name": "shell", "arguments": {"command": command}},
                )
                self.assertTrue(allowed["tool_result"]["ok"])
                self.assertIn("789", allowed["tool_result"]["output"]["stdout"])
                self.assertEqual(_get(base_url, "/permissions")["rules"][0]["effect"], "allow")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_with_status(base_url: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
