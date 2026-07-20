from __future__ import annotations

import json
import importlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_api_handler() -> type[BaseHTTPRequestHandler]:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        module = importlib.reload(sys.modules[module_name])
    else:
        module = importlib.import_module(module_name)
    return module.ZyraRequestHandler


class ApiControlCommandTests(unittest.TestCase):
    def test_skill_update_api_suspends_then_resumes_exact_local_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            bundle = workspace / ".zyra" / "skill-imports" / "api-release" / "api-helper"
            bundle.mkdir(parents=True)
            (bundle / "SKILL.md").write_text(
                "---\nschema: zyra.skill/v1\nname: api-helper\ndescription: API update helper.\nversion: 1.0.0\nuser-invocable: true\nmodel-invocable: true\ninvocation: {\"mode\":\"inline\",\"max-skill-depth\":0}\nallowed-tools: []\n---\nAPI helper body.\n",
                encoding="utf-8",
            )
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(workspace)
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
            os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission-state.json")

            ZyraRequestHandler = _fresh_api_handler()

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                task = _post(base_url, "/tasks", {"goal": "Install a trusted skill bundle.", "auto_run": False})["task"]
                payload = {
                    "action": "install",
                    "channel": "project",
                    "bundle_name": "api-release",
                    "update_id": "api-update-1",
                }
                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/skill-updates",
                    payload,
                )
                self.assertEqual(first_status, 409)
                self.assertEqual(first["error"], "skill_plugin_update_pending")
                self.assertEqual(first["update_id"], "api-update-1")
                session = first["permission_session"]
                self.assertTrue(session["created"])
                session_id = session["session_id"]
                token = session["bearer_token"]
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
                _post(
                    base_url,
                    f"/permissions/requests/{pending[0]['request_id']}/resolve",
                    {
                        "session_id": session_id,
                        "run_id": task["run_id"],
                        "task_id": task["task_id"],
                        "effect": "allow",
                        "idempotency_key": "approve-api-update-1",
                    },
                    headers=headers,
                )
                second_status, second = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/skill-updates",
                    payload,
                    headers=headers,
                )
                self.assertEqual(second_status, 200)
                self.assertEqual(second["skill_update"]["state"]["status"], "committed")
                self.assertTrue((workspace / ".zyra" / "skills" / "api-helper" / "SKILL.md").is_file())
                self.assertTrue(any(event["payload"].get("phase") == "skill_update_committed" for event in second["events"]))
                self.assertNotIn(token, json.dumps(second))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_change_command_creates_requirement_change_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                commands = {command["name"] for command in _get(base_url, "/commands")["commands"]}
                skills = {skill["name"] for skill in _get(base_url, "/skills")["skills"]}
                searched = _get(base_url, "/skills?q=verify+implementation+evidence")
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
                self.assertIn("verification", {skill["name"] for skill in searched["skills"]})
                self.assertTrue(searched["search"]["local_only"])
                self.assertEqual(searched["search"]["remote_search_status"], "deferred_upstream_stub")
                self.assertFalse(searched["body_loaded"])
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
                self.assertEqual(
                    code_inventory["moduleEntrypoints"]["queryEngine"],
                    "@zyra/claude-runtime.ClaudeRuntimeCore",
                )
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

    def test_skill_endpoint_loads_versioned_runtime_and_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(workspace)

            ZyraRequestHandler = _fresh_api_handler()

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Record skill invocation.", "auto_run": False})
                task_id = created["task"]["task_id"]
                workspace_id = created["task"]["metadata"]["workspace_ref"]["workspace_id"]
                _get(
                    base_url,
                    f"/workspaces/{workspace_id}/files?path=skill-input.txt&read=true&encoding=utf-8",
                )
                _post(
                    base_url,
                    f"/workspaces/{workspace_id}/files",
                    {
                        "path": "skill-input.txt",
                        "encoding": "utf-8",
                        "content": "skill worker context",
                    },
                )
                invoked = _post(
                    base_url,
                    f"/tasks/{task_id}/skills",
                    {
                        "skill_name": "web-research",
                        "arguments": {"query": "dynamic heterogeneous agents"},
                    },
                )
                skills_view = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/skills"})

                self.assertGreaterEqual(len(invoked["events"]), 5)
                self.assertTrue(all(event["event_type"] == "skill_invoked" for event in invoked["events"]))
                self.assertEqual(invoked["skill"]["metadata"]["name"], "web-research")
                self.assertEqual(invoked["skill"]["provenance"]["source_kind"], "builtin")
                self.assertTrue(invoked["skill"]["version_ref"]["content_digest"])
                self.assertEqual(invoked["skill_result"]["runtime_status"], "fork_pending")
                self.assertFalse(invoked["skill_result"]["body_returned"])
                self.assertFalse(invoked["skill_result"]["body_in_checkpoint"])
                projection = invoked["task"]["metadata"]["skill_invocation_projection"]
                self.assertEqual(projection["qualified_name"], "builtin:web-research")
                self.assertFalse(projection["body_in_checkpoint"])
                self.assertEqual(projection["permission_owner"], "M1-03A")
                self.assertIn(
                    "states",
                    invoked["task"]["metadata"]["skill_runtime_state"]["state_snapshot"],
                )
                self.assertEqual(
                    invoked["task"]["metadata"]["skill_session_context"]["invoked_skill_refs"],
                    [],
                )
                self.assertTrue(
                    invoked["task"]["metadata"]["skill_runtime_state"]["state_snapshot"]["fork_handoff"]["requests"]
                )
                self.assertNotIn(
                    "message_deltas",
                    invoked["task"]["metadata"]["skill_session_context"],
                )
                invocation_id = invoked["skill_result"]["invocation"]["state"]["invocation_id"]
                causal_event_id = next(
                    event["event_id"]
                    for event in invoked["events"]
                    if event.get("payload", {}).get("skill_runtime", {}).get("invocation_id")
                    == invocation_id
                )
                completed = _post(
                    base_url,
                    f"/tasks/{task_id}/skills/{invocation_id}/complete",
                    {
                        "event_ids": [causal_event_id],
                    },
                )
                self.assertEqual(completed["skill_state"]["status"], "completed")
                self.assertEqual(completed["session_checkpoint"]["active_invocation_ids"], [])
                self.assertEqual(completed["outcome_projection"]["outcome_refs"], [])
                self.assertTrue(completed["outcome_projection"]["evidence_refs"])
                completed_context = completed["task"]["metadata"]["skill_session_context"]
                self.assertFalse(completed_context["permission_hook_active"])
                self.assertEqual(completed_context["permission_hook_id"], "")
                self.assertEqual(
                    completed["task"]["metadata"]["skill_invocation_projection"]["status"],
                    "completed",
                )
                worker_status, worker = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/code",
                    {
                        "tool_plan": [
                            {
                                "tool_name": "file_read",
                                "arguments": {"path": "skill-input.txt"},
                            }
                        ]
                    },
                )
                self.assertEqual(worker_status, 201, worker)
                skill_messages = [
                    message
                    for message in worker["worker_request"]["messages"]
                    if message.get("metadata", {}).get("skill_invocation_id") == invocation_id
                ]
                self.assertEqual(skill_messages, [])
                self.assertEqual(
                    completed["session_checkpoint"]["active_invocation_ids"],
                    [],
                )
                self.assertEqual(
                    skills_view["command_result"]["summary"],
                    "Versioned skills and the task-scoped invocation projection.",
                )
                self.assertEqual(
                    skills_view["command_result"]["data"]["skill_invocation"]["qualified_name"],
                    "builtin:web-research",
                )
                events = _get(base_url, f"/tasks/{task_id}/events")["events"]
                self.assertTrue(any(event["event_type"] == "skill_invoked" for event in events))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_skill_api_disable_flag_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_SKILL_RUNTIME_DISABLED"] = "true"

            ZyraRequestHandler = _fresh_api_handler()

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Disabled skill runtime.", "auto_run": False})
                status, response = _post_with_status(
                    base_url,
                    f"/tasks/{created['task']['task_id']}/skills",
                    {"skill_name": "verification"},
                )

                self.assertEqual(status, 404)
                self.assertEqual(response["error"], "skill_registry_disabled")
            finally:
                os.environ.pop("ZYRA_SKILL_RUNTIME_DISABLED", None)
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_goal_command_mutates_canonical_task_instead_of_event_only_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            ZyraRequestHandler = _fresh_api_handler()

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

                self.assertIsNone(recorded["event"])
                self.assertEqual(recorded["command"]["category"], "task")
                self.assertEqual(recorded["command_result"]["summary"], "Root task objective updated.")
                self.assertEqual(recorded["task"]["user_goal"], "keep optimizing verifier evidence")
                history = recorded["task"]["metadata"]["control_mutations"]
                self.assertEqual(history[0]["command"], "/goal")
                self.assertFalse(recorded["event_only_stateful_fallback"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_command_results_return_runtime_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            ZyraRequestHandler = _fresh_api_handler()

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
                    "MCP runtime status",
                )
                self.assertEqual(mcp["command_result"]["data"]["owner_slice"], "M1-S03B-02")
                self.assertFalse(mcp["command_result"]["data"]["requires_node_sidecar"])
                self.assertFalse(mcp["command_result"]["data"]["sidecar_contracts_used"])
                self.assertTrue(mcp["command_result"]["data"]["enabled"])
                self.assertEqual(
                    mcp["command_result"]["data"]["state_owner"],
                    "McpClientRuntime",
                )
                self.assertTrue(mcp["command_result"]["data"]["control"]["ok"])
                self.assertEqual(
                    mcp["command_result"]["data"]["control"]["action"],
                    "status",
                )
                permission_data = permissions["command_result"]["data"]
                self.assertTrue(permission_data["custody_required_for_details"])
                self.assertNotIn("requests", permission_data)
                self.assertNotIn("rules", permission_data)
                self.assertNotIn("session_ids", permission_data)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_clear_uses_canonical_session_owner_and_rewind_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            ZyraRequestHandler = _fresh_api_handler()

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Exercise session commands.", "auto_run": False})
                task_id = created["task"]["task_id"]
                clear_status, cleared = _post_with_status(base_url, f"/tasks/{task_id}/commands", {"text": "/clear start focused session"})
                rewind_status, rewound = _post_with_status(base_url, f"/tasks/{task_id}/commands", {"text": "/rewind latest"})

                self.assertEqual(clear_status, 201)
                self.assertEqual(rewind_status, 409)
                self.assertTrue(cleared["command_result"]["ok"])
                self.assertFalse(rewound["command_result"]["ok"])
                self.assertIn(rewound["command_result"]["error"]["code"], {"permission_denied", "state_owner_unavailable"})
                self.assertFalse(cleared["event_only_stateful_fallback"])
                self.assertTrue(
                    cleared["command_result"]["data"]["transaction"]["effect"]["metadata"]["same_session_new_epoch"]
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_compact_and_export_commands_write_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

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

    def test_code_worker_endpoint_suspends_unapproved_workspace_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(Path(tmpdir) / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")

            ZyraRequestHandler = _fresh_api_handler()

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Run CodeWorker.", "auto_run": False})
                task_id = created["task"]["task_id"]
                status, blocked = _post_with_status(
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

                self.assertEqual(status, 409, blocked)
                self.assertEqual(blocked["worker_result"]["error"], "permission_suspended")
                metadata = blocked["worker_result"]["metadata"]
                self.assertEqual(metadata["canonical_runtime_owner"], "typescript")
                self.assertEqual(metadata["loop"], "zyra_typescript_query_engine_runtime")
                self.assertEqual(metadata["tool_runtime_completed"], "1")
                self.assertEqual(metadata["query_plan_ok"], "true")
                self.assertIn("permission_session", blocked)
                self.assertTrue(blocked["permission_session"]["session_custody_token_included"])
                self.assertFalse((Path(tmpdir) / "workspace" / "worker" / "output.txt").exists())
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

            ZyraRequestHandler = _fresh_api_handler()

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Run BrowserWorker.", "auto_run": False})
                task_id = created["task"]["task_id"]
                workspace_id = created["task"]["metadata"]["workspace_ref"]["workspace_id"]
                _get(
                    base_url,
                    f"/workspaces/{workspace_id}/files?path=page.html&read=true&encoding=utf-8",
                )
                _post(
                    base_url,
                    f"/workspaces/{workspace_id}/files",
                    {
                        "path": "page.html",
                        "encoding": "utf-8",
                        "content": page.read_text(encoding="utf-8"),
                    },
                )
                executed = _post(
                    base_url,
                    f"/tasks/{task_id}/workers/browser",
                    {
                        "browser_plan": [
                            {"action": "open_url", "arguments": {"url": "workspace:///page.html"}},
                            {"action": "extract_text"},
                        ],
                        "constraints": {"browser_backend": "static", "allowed_schemes": ["workspace"]},
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

            ZyraRequestHandler = _fresh_api_handler()

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
                    {"browser_plan": plan, "constraints": {"browser_backend": "static"}},
                )

                self.assertEqual(first_status, 409, first)
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
                        "browser_backend": "static",
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

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

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

            ZyraRequestHandler = _fresh_api_handler()

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
                self.assertEqual(first_status, 409, first)
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
                workspace_id = created["task"]["metadata"]["workspace_ref"]["workspace_id"]
                workspace_file = _get(
                    base_url,
                    f"/workspaces/{workspace_id}/files?path=api-code-approved.txt&read=true&encoding=utf-8",
                )
                self.assertEqual(workspace_file["content"], "approved")
                self.assertFalse((root / "workspace" / "api-code-approved.txt").exists())
                for path in root.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_code_worker_permission_approval_recovers_lost_terminal_ack_via_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
            os.environ["ZYRA_PERMISSION_STORE"] = str(root / "legacy-permissions.json")
            os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission-state.json")

            ZyraRequestHandler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(
                    base_url,
                    "/tasks",
                    {"goal": "Recover an approved effect after terminal ACK loss.", "auto_run": False},
                )
                task = created["task"]
                session_id = "api-code-permission-lost-ack-session"
                command = subprocess.list2cmdline(
                    [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "Path('approved-once.txt').open('a', encoding='utf-8').write('once')"
                        ),
                    ]
                )
                plan = [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "api-code-lost-ack-shell-1",
                        "arguments": {"command": command},
                    }
                ]
                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/code",
                    {"constraints": {"session_id": session_id, "tool_plan": plan}},
                )
                self.assertEqual(first_status, 409, first)
                self.assertEqual(first["worker_result"]["error"], "permission_suspended")
                token = first["permission_session"]["session_custody_token"]
                headers = {"Authorization": f"Bearer {token}"}
                identity = {
                    "session_id": session_id,
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                }
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
                _post(
                    base_url,
                    f"/permissions/requests/{pending[0]['request_id']}/resolve",
                    {**identity, "effect": "allow", "idempotency_key": "approve-lost-ack"},
                    headers=headers,
                )

                fault_status, faulted = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/code",
                    {
                        "constraints": {
                            "session_id": session_id,
                            "session_custody_token": token,
                            "tool_plan": plan,
                            "typescript_fault_injection": "terminal_result_ack_lost",
                        }
                    },
                )
                self.assertEqual(fault_status, 409, faulted)
                self.assertEqual(
                    faulted["worker_result"]["metadata"]["typescript_runtime_error"],
                    "typescript_runtime_fault_injected",
                )

                recovered_status, recovered = _post_with_status(
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
                self.assertEqual(recovered_status, 201, recovered)
                self.assertTrue(recovered["worker_result"]["ok"])
                self.assertTrue(recovered["route_contract"]["ok"])
                self.assertEqual(
                    recovered["worker_result"]["metadata"]["terminal_result_recovered"],
                    "true",
                )
                self.assertEqual(
                    recovered["worker_result"]["metadata"]["runtime_transport"],
                    "durable-terminal-receipt",
                )
                workspace_id = task["metadata"]["workspace_ref"]["workspace_id"]
                workspace_file = _get(
                    base_url,
                    (
                        f"/workspaces/{workspace_id}/files"
                        "?path=approved-once.txt&read=true&encoding=utf-8"
                    ),
                )
                self.assertEqual(workspace_file["content"], "once")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_code_worker_permission_batch_fences_partial_effects_and_denial_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
            os.environ["ZYRA_PERMISSION_STORE"] = str(root / "legacy-permissions.json")
            os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission-state.json")

            ZyraRequestHandler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(
                    base_url,
                    "/tasks",
                    {"goal": "Fence two approval-gated code effects.", "auto_run": False},
                )
                task = created["task"]
                session_id = "api-code-permission-batch-session"
                workspace_id = task["metadata"]["workspace_ref"]["workspace_id"]

                def write_command(path: str, content: str) -> str:
                    return subprocess.list2cmdline(
                        [
                            sys.executable,
                            "-c",
                            (
                                "from pathlib import Path; "
                                f"Path({path!r}).open('a', encoding='utf-8').write({content!r})"
                            ),
                        ]
                    )

                plan = [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "api-code-batch-shell-1",
                        "arguments": {"command": write_command("batch-first.txt", "first")},
                    },
                    {
                        "tool_name": "shell",
                        "tool_call_id": "api-code-batch-shell-2",
                        "arguments": {"command": write_command("batch-second.txt", "second")},
                    },
                ]
                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/code",
                    {"constraints": {"session_id": session_id, "tool_plan": plan}},
                )
                self.assertEqual(first_status, 409, first)
                self.assertEqual(first["worker_result"]["error"], "permission_suspended")
                self.assertEqual(first["worker_result"]["metadata"]["runtime_process_epoch"], "1")
                token = first["permission_session"]["session_custody_token"]
                headers = {"Authorization": f"Bearer {token}"}
                identity = {
                    "session_id": session_id,
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                }
                query_path = (
                    "/permissions/requests"
                    f"?session_id={session_id}&run_id={task['run_id']}"
                    f"&task_id={task['task_id']}&pending_only=true"
                )
                pending = _get(base_url, query_path, headers=headers)["requests"]["items"]
                self.assertEqual(len(pending), 2)
                for path in ("batch-first.txt", "batch-second.txt"):
                    absent = _get(
                        base_url,
                        f"/workspaces/{workspace_id}/files?path={path}&read=true&encoding=utf-8",
                    )
                    self.assertFalse(absent["read"]["record"]["metadata"]["exists"])

                _post(
                    base_url,
                    f"/permissions/requests/{pending[0]['request_id']}/resolve",
                    {**identity, "effect": "allow", "idempotency_key": "approve-batch-first"},
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
                self.assertEqual(second_status, 409, second)
                self.assertEqual(second["worker_result"]["error"], "permission_suspended")
                self.assertEqual(second["worker_result"]["metadata"]["runtime_process_epoch"], "2")
                remaining = _get(base_url, query_path, headers=headers)["requests"]["items"]
                self.assertEqual(len(remaining), 1)
                for path in ("batch-first.txt", "batch-second.txt"):
                    absent = _get(
                        base_url,
                        f"/workspaces/{workspace_id}/files?path={path}&read=true&encoding=utf-8",
                    )
                    self.assertFalse(absent["read"]["record"]["metadata"]["exists"])

                _post(
                    base_url,
                    f"/permissions/requests/{remaining[0]['request_id']}/resolve",
                    {**identity, "effect": "allow", "idempotency_key": "approve-batch-second"},
                    headers=headers,
                )
                third_status, third = _post_with_status(
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
                self.assertEqual(
                    third_status,
                    201,
                    {
                        "worker_error": third.get("worker_result", {}).get("error"),
                        "runtime_error_message": third.get("worker_result", {})
                        .get("metadata", {})
                        .get("typescript_runtime_error_message"),
                        "phases": [
                            event.get("payload", {}).get("query_session", {}).get("phase")
                            for event in third.get("events", [])
                        ],
                    },
                )
                self.assertTrue(third["worker_result"]["ok"])
                self.assertEqual(third["worker_result"]["metadata"]["runtime_process_epoch"], "3")
                for path, content in (("batch-first.txt", "first"), ("batch-second.txt", "second")):
                    workspace_file = _get(
                        base_url,
                        f"/workspaces/{workspace_id}/files?path={path}&read=true&encoding=utf-8",
                    )
                    self.assertEqual(workspace_file["content"], content)

                denied_task = _post(
                    base_url,
                    "/tasks",
                    {"goal": "Deny an approval-gated code effect.", "auto_run": False},
                )["task"]
                denied_session_id = "api-code-permission-denied-session"
                denied_plan = [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "api-code-denied-shell-1",
                        "arguments": {"command": write_command("must-not-exist.txt", "denied")},
                    }
                ]
                denied_status, denied = _post_with_status(
                    base_url,
                    f"/tasks/{denied_task['task_id']}/workers/code",
                    {"constraints": {"session_id": denied_session_id, "tool_plan": denied_plan}},
                )
                self.assertEqual(denied_status, 409, denied)
                denied_token = denied["permission_session"]["session_custody_token"]
                denied_headers = {"Authorization": f"Bearer {denied_token}"}
                denied_identity = {
                    "session_id": denied_session_id,
                    "run_id": denied_task["run_id"],
                    "task_id": denied_task["task_id"],
                }
                denied_pending = _get(
                    base_url,
                    (
                        "/permissions/requests"
                        f"?session_id={denied_session_id}&run_id={denied_task['run_id']}"
                        f"&task_id={denied_task['task_id']}&pending_only=true"
                    ),
                    headers=denied_headers,
                )["requests"]["items"]
                self.assertEqual(len(denied_pending), 1)
                _post(
                    base_url,
                    f"/permissions/requests/{denied_pending[0]['request_id']}/resolve",
                    {**denied_identity, "effect": "deny", "idempotency_key": "deny-code-effect"},
                    headers=denied_headers,
                )
                retry_status, retry = _post_with_status(
                    base_url,
                    f"/tasks/{denied_task['task_id']}/workers/code",
                    {
                        "constraints": {
                            "session_id": denied_session_id,
                            "session_custody_token": denied_token,
                            "tool_plan": denied_plan,
                        }
                    },
                )
                self.assertEqual(retry_status, 409, retry)
                self.assertEqual(retry["worker_result"]["error"], "permission_denied")
                denied_workspace_id = denied_task["metadata"]["workspace_ref"]["workspace_id"]
                absent = _get(
                    base_url,
                    (
                        f"/workspaces/{denied_workspace_id}/files"
                        "?path=must-not-exist.txt&read=true&encoding=utf-8"
                    ),
                )
                self.assertFalse(absent["read"]["record"]["metadata"]["exists"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_code_worker_expired_approval_stays_fail_closed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
            os.environ["ZYRA_PERMISSION_STORE"] = str(root / "legacy-permissions.json")
            os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission-state.json")

            ZyraRequestHandler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                task = _post(
                    base_url,
                    "/tasks",
                    {"goal": "Expire a code approval without executing it.", "auto_run": False},
                )["task"]
                session_id = "api-code-permission-expiry-session"
                command = subprocess.list2cmdline(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; Path('expired-must-not-exist.txt').write_text('bad')",
                    ]
                )
                plan = [
                    {
                        "tool_name": "shell",
                        "tool_call_id": "api-code-expired-shell-1",
                        "arguments": {"command": command},
                    }
                ]
                constraints = {
                    "session_id": session_id,
                    "tool_plan": plan,
                    "permission_approval_ttl_seconds": 1,
                }
                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/code",
                    {"constraints": constraints},
                )
                self.assertEqual(first_status, 409, first)
                self.assertEqual(first["worker_result"]["error"], "permission_suspended")
                token = first["permission_session"]["session_custody_token"]
                headers = {"Authorization": f"Bearer {token}"}
                identity = {
                    "session_id": session_id,
                    "run_id": task["run_id"],
                    "task_id": task["task_id"],
                }
                query_path = (
                    "/permissions/requests"
                    f"?session_id={session_id}&run_id={task['run_id']}"
                    f"&task_id={task['task_id']}&pending_only=true"
                )
                pending = _get(base_url, query_path, headers=headers)["requests"]["items"]
                self.assertEqual(len(pending), 1)
                time.sleep(1.2)
                expire_status, expired = _post_with_status(
                    base_url,
                    "/permissions/requests/expire",
                    identity,
                    headers=headers,
                )
                self.assertEqual(expire_status, 200, expired)
                self.assertEqual(expired["receipt"]["transport"]["expired_count"], 1)
                self.assertEqual(
                    _get(base_url, query_path, headers=headers)["requests"]["items"],
                    [],
                )

                retry_status, retry = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/code",
                    {
                        "constraints": {
                            **constraints,
                            "session_custody_token": token,
                        }
                    },
                )
                self.assertEqual(retry_status, 409, retry)
                self.assertEqual(retry["worker_result"]["error"], "permission_suspended")
                self.assertEqual(retry["worker_result"]["metadata"]["runtime_process_epoch"], "2")
                workspace_id = task["metadata"]["workspace_ref"]["workspace_id"]
                absent = _get(
                    base_url,
                    (
                        f"/workspaces/{workspace_id}/files"
                        "?path=expired-must-not-exist.txt&read=true&encoding=utf-8"
                    ),
                )
                self.assertFalse(absent["read"]["record"]["metadata"]["exists"])
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
    with urllib.request.urlopen(request, timeout=60) as response:
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
        with urllib.request.urlopen(request, timeout=60) as response:
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
    with urllib.request.urlopen(request, timeout=60) as response:
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
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
