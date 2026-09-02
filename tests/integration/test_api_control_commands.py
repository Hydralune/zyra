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
            workspace.mkdir()
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
                _get(base_url, "/skills")
                bundle = workspace / ".zyra" / "skills" / "api-helper"
                bundle.mkdir(parents=True)
                (bundle / "SKILL.md").write_text(
                    "---\nid: api-helper\nname: api-helper\ndescription: API update helper.\ntools: {\"allowed\":[],\"denied\":[],\"namespaces\":[],\"mcp_servers\":[],\"read_only\":true,\"inherit_parent\":true,\"maximum_calls\":0,\"maximum_parallel\":1,\"require_approval\":[]}\nexecution: {\"mode\":\"inline\",\"timeout_ms\":30000,\"maximum_turns\":1,\"sandbox\":\"workspace_read\",\"allow_network\":false,\"persist_transcript\":true,\"persist_artifacts\":false}\nenabled: true\n---\nAPI helper body.\n",
                    encoding="utf-8",
                )
                session_id = "api-skill-update-session"
                session = _post(
                    base_url,
                    "/permissions/sessions/open",
                    {
                        "session_id": session_id,
                        "run_id": task["run_id"],
                        "task_id": task["task_id"],
                    },
                )["session"]
                token = session["bearer_token"]
                headers = {"Authorization": f"Bearer {token}"}
                payload = {
                    "tool_call_id": "api-update-1",
                }
                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/skill-updates",
                    payload,
                    headers=headers,
                )
                self.assertEqual(first_status, 409)
                self.assertEqual(first["error"], "permission_approval_required")
                self.assertEqual(first["tool_call_id"], "api-update-1")
                self.assertEqual(first["canonical_entrypoint"], "E02CapabilityCoordinator.execute")
                self.assertFalse(first["python_skill_fallback"])
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
                resolved = _post(
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
                permit_id = resolved["receipt"]["permit_id"]
                second_status, second = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/skill-updates",
                    {**payload, "permit_id": permit_id},
                    headers=headers,
                )
                self.assertEqual(second_status, 200)
                self.assertTrue(second["ok"])
                self.assertEqual(second["execution"]["receipt"]["owner"], "typescript-skill")
                self.assertEqual(second["execution"]["receipt"]["permitId"], permit_id)
                registry = _get(base_url, "/skills")["registry"]
                latest = registry["revisions"][-1]
                self.assertIn("api-helper", {item["name"] for item in latest["skills"]})
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
                self.assertIn("list_skills", skills)
                self.assertIn("search_skills", skills)
                self.assertEqual(searched["skills"], [])
                self.assertEqual(
                    searched["canonical_entrypoint"],
                    "E02CapabilityCoordinator.execute",
                )
                self.assertFalse(searched["python_registry_enabled"])
                self.assertIn("e02_health", tools)
                self.assertIn("agent_list", tools)
                self.assertIn("CodeWorkerRuntime", workers)
                self.assertIn("open_url", {action["action"] for action in browser_actions["actions"]})
                self.assertGreater(len(browser_actions["source_registered_actions"]), 10)
                self.assertTrue(browser_health["environment_configured"])
                if browser_health["importable"]:
                    self.assertEqual(browser_health["classes"]["BrowserSession"], "BrowserSession")
                else:
                    self.assertEqual(browser_health["error_type"], "ModuleNotFoundError")
                self.assertEqual(code_inventory["source"], "zyra-typescript-runtime")
                self.assertEqual(code_inventory["upstreamSource"], "claude-code-best")
                self.assertEqual(code_inventory["canonicalOwner"], "typescript")
                self.assertFalse(code_inventory["requiresRootSourceRepo"])
                self.assertFalse(code_inventory["requiresVendorRuntime"])
                self.assertFalse(code_inventory["requiresLegacyInspectionSidecar"])
                self.assertEqual(
                    code_inventory["moduleEntrypoints"]["queryEngine"],
                    "ClaudeRuntimeCore",
                )
                self.assertEqual(code_inventory["health"]["vendor"]["complete"], False)
                self.assertTrue(code_inventory["defaultRoute"])
                self.assertFalse(code_inventory["fallbackUsed"])
                self.assertTrue(code_inventory["processConfiguration"]["cleanDefault"])
                self.assertEqual(
                    permission_health["state_owner"],
                    "typescript.PermissionCoordinator",
                )
                self.assertFalse(permission_health["python_decision_fallback"])
                self.assertTrue(
                    permission_health["state_listing_requires_session_custody"]
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
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(Path(tmpdir) / "artifacts")
            os.environ["ZYRA_PERMISSION_STATE"] = str(Path(tmpdir) / "permission-state.json")

            ZyraRequestHandler = _fresh_api_handler()

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {"goal": "Record skill invocation.", "auto_run": False})
                task_id = created["task"]["task_id"]
                skills = _get(base_url, "/skills")
                selected_skill = skills["registry"]["revisions"][-1]["skills"][0]["name"]
                session_id = "api-skill-invocation-session"
                session = _post(
                    base_url,
                    "/permissions/sessions/open",
                    {
                        "session_id": session_id,
                        "run_id": created["task"]["run_id"],
                        "task_id": task_id,
                    },
                )["session"]
                token = session["bearer_token"]
                headers = {"Authorization": f"Bearer {token}"}
                invocation_payload = {
                    "skill_name": selected_skill,
                    "arguments": {},
                    "tool_call_id": "api-skill-invocation-1",
                    "session_id": session_id,
                }
                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/skills",
                    invocation_payload,
                    headers=headers,
                )
                self.assertEqual(first_status, 409, first)
                self.assertEqual(first["error"], "permission_approval_required")
                self.assertEqual(first["tool_call_id"], "api-skill-invocation-1")
                pending = _get(
                    base_url,
                    (
                        "/permissions/requests"
                        f"?session_id={session_id}&run_id={created['task']['run_id']}"
                        f"&task_id={task_id}&pending_only=true"
                    ),
                    headers=headers,
                )["requests"]["items"]
                self.assertEqual(len(pending), 1)
                resolved = _post(
                    base_url,
                    f"/permissions/requests/{pending[0]['request_id']}/resolve",
                    {
                        "session_id": session_id,
                        "run_id": created["task"]["run_id"],
                        "task_id": task_id,
                        "effect": "allow",
                        "idempotency_key": "approve-api-skill-invocation",
                    },
                    headers=headers,
                )
                status, invoked = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/skills",
                    invocation_payload,
                    headers=headers,
                )
                self.assertEqual(status, 201, invoked)
                skills_view = _post(base_url, f"/tasks/{task_id}/commands", {"text": "/skills"})

                self.assertTrue(invoked["ok"])
                self.assertEqual(invoked["canonical_entrypoint"], "E02CapabilityCoordinator.execute")
                self.assertFalse(invoked["python_skill_fallback"])
                receipt = invoked["execution"]["receipt"]
                self.assertEqual(receipt["owner"], "typescript-skill")
                self.assertTrue(
                    receipt["permitId"].startswith("e02-capability-permit-"),
                    receipt,
                )
                self.assertFalse(receipt["replayed"])
                self.assertTrue(receipt["result"]["ok"])
                self.assertEqual(receipt["result"]["output"]["invocation"]["status"], "completed")
                projection = invoked["task"]["metadata"]["skill_invocation_projection"]
                self.assertEqual(projection["canonical_owner"], "typescript.SkillCoordinator")
                self.assertEqual(projection["tool_call_id"], "api-skill-invocation-1")
                self.assertEqual(projection["skill"], selected_skill)
                self.assertEqual(projection["snapshot_hash"], invoked["execution"]["snapshot_hash"])
                skill_data = skills_view["command_result"]["data"]
                self.assertEqual(skills_view["command_result"]["status"], "completed")
                self.assertEqual(skills_view["command_result"]["receipt"]["owner"], "typescript-command")
                self.assertFalse(skill_data["body_in_projection"])
                visible_skills = skill_data["registry"]["revisions"][-1]["skills"]
                self.assertIn(selected_skill, {item["name"] for item in visible_skills})
                self.assertNotIn(token, json.dumps(invoked))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_retired_python_skill_disable_flag_cannot_shadow_typescript_owner(self) -> None:
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

                self.assertEqual(status, 409)
                self.assertEqual(response["error"], "permission_approval_required")
                self.assertEqual(response["canonical_entrypoint"], "E02CapabilityCoordinator.execute")
                self.assertFalse(response["python_skill_fallback"])
                self.assertTrue(response["tool_call_id"])
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

                help_result = helped["command_result"]
                help_names = {item["name"] for item in help_result["data"]["commands"]}
                self.assertIn("help", help_names)
                self.assertIn("skills", help_names)
                self.assertIn("tools", help_names)
                self.assertEqual(help_result["status"], "completed")
                self.assertEqual(help_result["receipt"]["owner"], "typescript-command")
                self.assertEqual(help_result["canonical_entrypoint"], "E02CapabilityCoordinator.execute")
                self.assertEqual(context["command_result"]["data"]["control_commands"], 1)
                self.assertEqual(context["command_result"]["name"], "/context")
                mcp_result = mcp["command_result"]
                self.assertEqual(mcp_result["status"], "completed")
                self.assertEqual(mcp_result["permission"]["effect"], "allow")
                self.assertEqual(mcp_result["receipt"]["owner"], "typescript-command")
                self.assertEqual(mcp_result["canonical_command_owner"], "typescript.CommandCoordinator")
                self.assertFalse(mcp_result["python_parser_fallback"])
                self.assertFalse(mcp_result["python_dispatch_fallback"])
                permission_data = permissions["command_result"]["data"]
                self.assertEqual(permission_data["canonical_owner"], "typescript.PermissionCoordinator")
                self.assertFalse(permission_data["python_decision_fallback"])
                self.assertEqual(permission_data["health"]["canonical_owner"], "typescript")
                self.assertNotIn("requests", permission_data)
                self.assertNotIn("rules", permission_data)
                self.assertNotIn("session_ids", permission_data)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_doctor_reports_legacy_source_pools_as_retired_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["ZYRA_SQLITE_PATH"] = str(Path(tmpdir) / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(Path(tmpdir) / "events.jsonl")

            ZyraRequestHandler = _fresh_api_handler()
            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(
                    base_url,
                    "/tasks",
                    {"goal": "Inspect source retirement.", "auto_run": False},
                )
                task_id = created["task"]["task_id"]
                doctor = _post(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {"text": "/doctor"},
                )

                source_pools = doctor["command_result"]["data"]["legacy_source_pools"]
                self.assertEqual(source_pools["status"], "retired")
                self.assertEqual(source_pools["availability"], "not_applicable")
                self.assertFalse(source_pools["filesystem_required"])
                self.assertFalse(source_pools["fallback_available"])
                self.assertEqual(
                    {source["name"] for source in source_pools["sources"]},
                    {"claude-code-best", "browser-use"},
                )
                self.assertNotIn(
                    "vendor_claude_code_best_exists",
                    doctor["command_result"]["data"],
                )
                self.assertNotIn(
                    "vendor_browser_use_exists",
                    doctor["command_result"]["data"],
                )
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
                rename_status, renamed = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {"text": "/rename Fix CLI session state"},
                )
                sessions = _get(base_url, "/sessions")
                clear_status, cleared = _post_with_status(base_url, f"/tasks/{task_id}/commands", {"text": "/clear start focused session"})
                rewind_status, rewound = _post_with_status(base_url, f"/tasks/{task_id}/commands", {"text": "/rewind latest"})

                self.assertEqual(rename_status, 201)
                self.assertTrue(renamed["command_result"]["ok"])
                self.assertEqual(
                    renamed["command_result"]["data"]["transaction"]["effect"]["result"]["title"],
                    "Fix CLI session state",
                )
                self.assertEqual(sessions["sessions"][0]["title"], "Fix CLI session state")
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
                artifact_run = _run_code_tool(
                    base_url,
                    task_id,
                    "artifact_write",
                    {
                        "title": "Verifier evidence",
                        "content": "requirement change evidence and trajectory replay notes",
                        "kind": "markdown",
                    },
                )
                self.assertTrue(_completed_tool_result(artifact_run, "artifact_write")["ok"])
                _post(base_url, f"/tasks/{task_id}/commands", {"text": "/skills"})
                _post(base_url, f"/tasks/{task_id}/commands", {"text": "/change add replay evidence"})
                _post(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {"text": "/inject worker_lost worker_id=worker-control-test"},
                )

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
                artifact_run = _run_code_tool(
                    base_url,
                    task_id,
                    "artifact_write",
                    {"title": "Evidence", "content": "trace evidence"},
                )
                self.assertTrue(_completed_tool_result(artifact_run, "artifact_write")["ok"])
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
                route_status, rejected = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/tools",
                    {
                        "tool_name": "file_write",
                        "arguments": {"path": "notes/result.txt", "content": "tool output"},
                    },
                )
                self.assertEqual(route_status, 409)
                self.assertEqual(rejected["error"], "e02_route_not_found")
                self.assertEqual(rejected["canonical_entrypoint"], "E02CapabilityCoordinator.execute")
                self.assertFalse(rejected["python_execution_fallback"])
                executed = _run_code_tool(
                    base_url,
                    task_id,
                    "file_write",
                    {"path": "notes/result.txt", "content": "tool output"},
                )
                tool_result = _completed_tool_result(executed, "file_write")
                self.assertTrue(tool_result["ok"])
                self.assertEqual(tool_result["output"]["path"], "notes/result.txt")
                self.assertEqual(tool_result["metadata"]["sandbox_gateway_routed"], "true")
                workspace_id = created["task"]["metadata"]["workspace_ref"]["workspace_id"]
                written = _get(
                    base_url,
                    f"/workspaces/{workspace_id}/files?path=notes/result.txt&read=true&encoding=utf-8",
                )
                self.assertEqual(written["content"], "tool output")
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
                workspace_id = created["task"]["metadata"]["workspace_ref"]["workspace_id"]
                _post(
                    base_url,
                    f"/workspaces/{workspace_id}/files",
                    {
                        "path": "research/brief.md",
                        "encoding": "utf-8",
                        "content": "Dynamic heterogeneous agents need traceable requirement change handling.",
                    },
                )
                searched = _run_code_tool(
                    base_url,
                    task_id,
                    "web_search",
                    {"query": "requirement change", "paths": ["research"]},
                )
                tool_result = _completed_tool_result(searched, "web_search")
                self.assertTrue(tool_result["ok"])
                self.assertEqual(tool_result["output"]["result_count"], 1)
                artifact_id = tool_result["output"]["artifact_id"]
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
                browsed = _run_code_tool(
                    base_url,
                    task_id,
                    "browser",
                    {
                        "action": "extract_text",
                        "html": "<html><head><title>API Browser</title></head><body><p>Runtime browser evidence.</p></body></html>",
                    },
                )
                tool_result = _completed_tool_result(browsed, "browser")
                self.assertTrue(tool_result["ok"])
                self.assertEqual(tool_result["output"]["state"]["title"], "API Browser")
                self.assertIn("Runtime browser evidence", tool_result["output"]["state"]["text_preview"])
                self.assertEqual(len(tool_result["artifacts"]), 2)
                artifact_id = tool_result["artifacts"][0]["artifact_id"]
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
                traced = _run_code_tool(
                    base_url,
                    task_id,
                    "trace",
                    {"limit": 3, "write_artifact": True},
                )
                tool_result = _completed_tool_result(traced, "trace")
                self.assertTrue(tool_result["ok"])
                self.assertGreaterEqual(tool_result["output"]["event_count"], 1)
                self.assertEqual(tool_result["output"]["returned_count"], 3)
                self.assertEqual(len(tool_result["artifacts"]), 1)
                artifact_id = tool_result["artifacts"][0]["artifact_id"]
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
                checkpoint = _run_code_tool(
                    base_url,
                    task_id,
                    "checkpoint",
                    {"include_state": True, "write_artifact": True},
                )
                tool_result = _completed_tool_result(checkpoint, "checkpoint")
                self.assertTrue(tool_result["ok"])
                self.assertTrue(tool_result["output"]["truncated"])
                self.assertEqual(tool_result["metadata"]["tool_result_budget_applied"], "true")
                checkpoint_artifacts = [
                    artifact
                    for artifact in tool_result["artifacts"]
                    if artifact["title"] == f"checkpoint:{task_id}"
                ]
                self.assertEqual(len(checkpoint_artifacts), 1)
                artifact_id = checkpoint_artifacts[0]["artifact_id"]
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
                self.assertEqual(metadata["query_plan_ok"], "false")
                self.assertEqual(metadata["query_session_consistent"], "true")
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
                self.assertEqual(
                    executed["worker_result"]["metadata"]["source_identity"],
                    "browser-use",
                )
                self.assertEqual(
                    executed["worker_result"]["metadata"]["source_status"],
                    "retired",
                )
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
                idempotency_key = "api-browser-exact-approval"
                network_constraints = {
                    "browser_backend": "static",
                    "allowed_schemes": ["http"],
                    "allowed_domains": ["127.0.0.1"],
                    "allow_private_network": True,
                    "allow_loopback_network": True,
                }
                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/browser",
                    {
                        "browser_plan": plan,
                        "constraints": network_constraints,
                        "idempotency_key": idempotency_key,
                    },
                )

                self.assertEqual(
                    first_status,
                    202,
                    {
                        "worker_error": first.get("worker_result", {}).get("error"),
                        "worker_metadata": first.get("worker_result", {}).get("metadata"),
                        "event_payload_keys": [
                            sorted(event.get("payload", {})) for event in first.get("events", [])
                        ],
                        "browser_results": [
                            {
                                "error": event.get("payload", {}).get("browser_result", {}).get("error"),
                                "output_keys": sorted(
                                    event.get("payload", {}).get("browser_result", {}).get("output", {})
                                ),
                                "event_count": len(
                                    event.get("payload", {}).get("browser_result", {}).get("events", [])
                                ),
                            }
                            for event in first.get("events", [])
                            if isinstance(event.get("payload", {}).get("browser_result"), dict)
                        ],
                    },
                )
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
                    "idempotency_key": idempotency_key,
                    "constraints": {
                        **network_constraints,
                        "permission_session_id": session["session_id"],
                        "permission_session_custody_token": token,
                    },
                }
                second_status, second = _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/browser",
                    retry_payload,
                )
                self.assertEqual(
                    second_status,
                    201,
                    {
                        "worker_error": second.get("worker_result", {}).get("error"),
                        "worker_metadata": second.get("worker_result", {}).get("metadata"),
                        "browser_results": [
                            {
                                "error": event.get("payload", {}).get("browser_result", {}).get("error"),
                                "summary": event.get("payload", {}).get("browser_result", {}).get("summary"),
                                "output": event.get("payload", {}).get("browser_result", {}).get("output"),
                                "effect": event.get("payload", {})
                                .get("browser_result", {})
                                .get("output", {})
                                .get("permission_decision", {})
                                .get("effect"),
                            }
                            for event in second.get("events", [])
                            if isinstance(event.get("payload", {}).get("browser_result"), dict)
                        ],
                    },
                )
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
                self.assertEqual(replay_status, 200)
                self.assertTrue(replay["idempotent_replay"])
                self.assertTrue(replay["worker_result"]["ok"])
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
                session_id = "structured-shell-permission-session"
                command = f'"{sys.executable}" -c "print(789)"'
                plan = [[{
                    "tool_name": "shell",
                    "tool_call_id": "structured-shell-retry-1",
                    "arguments": {"command": command},
                }]]
                status, blocked = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/code",
                    {"constraints": {"session_id": session_id, "query_turns": plan}},
                )

                self.assertEqual(status, 409)
                self.assertEqual(blocked["worker_result"]["error"], "permission_suspended")
                session = blocked["permission_session"]
                custody_token = session["session_custody_token"]
                self.assertTrue(session["session_custody_token_included"])
                run_id = created["task"]["run_id"]
                query_path = (
                    "/permissions/requests"
                    f"?session_id={session_id}"
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
                request_id = permissions["requests"]["items"][0]["request_id"]
                self.assertEqual(
                    permissions["requests"]["items"][0]["request_id"],
                    request_id,
                )

                malformed_status, malformed = _post_with_status(
                    base_url,
                    f"/permissions/requests/{request_id}/resolve",
                    {
                        "session_id": session_id,
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
                        "session_id": session_id,
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
                        "session_id": session_id,
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
                        "session_id": session_id,
                        "run_id": run_id,
                        "task_id": task_id,
                        "effect": "allow",
                        "idempotency_key": "approve-shell-once",
                    },
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertEqual(
                    resolved["receipt"]["request"]["status"],
                    "approved",
                )

                second_status, executed = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/code",
                    {
                        "constraints": {
                            "session_id": session_id,
                            "session_custody_token": custody_token,
                            "query_turns": plan,
                        },
                    },
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertEqual(second_status, 201)
                executed_result = _completed_tool_result(executed, "shell")
                self.assertTrue(executed_result["ok"])
                self.assertIn("789", executed_result["output"]["stdout"])

                forged_status, forged = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/workers/code",
                    {
                        "constraints": {
                            "session_id": session_id,
                            "session_custody_token": custody_token,
                            "query_turns": [[{
                                "tool_name": "shell",
                                "tool_call_id": "structured-shell-retry-1",
                                "arguments": {"command": f'{sys.executable} -c "print(999)"'},
                            }]],
                        },
                    },
                    headers={"Authorization": f"Bearer {custody_token}"},
                )
                self.assertNotEqual(
                    forged_status,
                    201,
                    {
                        "worker_error": forged.get("worker_result", {}).get("error"),
                        "tool_results": [
                            event.get("payload", {}).get("query_session", {}).get("tool_result")
                            for event in forged.get("events", [])
                            if event.get("payload", {}).get("query_session", {}).get("phase")
                            == "tool_call_completed"
                        ],
                    },
                )
                self.assertFalse(forged["worker_result"]["ok"])
                task_events = _get(base_url, f"/tasks/{task_id}/events")["events"]
                permission_kinds = [
                    event.get("payload", {})
                    .get("query_session", {})
                    .get("permission_runtime", {})
                    .get("kind")
                    for event in task_events
                ]
                self.assertIn("permission_request_resolved", permission_kinds)
                self.assertTrue(
                    executed_result["metadata"][
                        "sandbox_gateway_permission_consumption_id"
                    ],
                    executed_result,
                )
                self.assertEqual(
                    executed["worker_result"]["metadata"]["e02_port_consumed"],
                    "1",
                )
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
                transition = changed["receipt"]["transition"]
                self.assertEqual(transition["from"], "default")
                self.assertEqual(transition["to"], "dontAsk")
                self.assertEqual(transition["revisionBefore"], 0)
                self.assertEqual(transition["revisionAfter"], 1)
                self.assertEqual(changed["state_owner"], "typescript.PermissionCoordinator")
                self.assertFalse(changed["python_decision_fallback"])
                status, stale = _post_with_status(
                    base_url,
                    "/permissions/mode",
                    {**identity, "mode": "auto", "expected_mode_revision": 0},
                    headers=headers,
                )
                self.assertEqual(status, 409)
                self.assertIn("revision conflict", stale["message"])

                selected = _get(
                    base_url,
                    (
                        "/permissions/mode"
                        f"?session_id={session_id}&run_id={task['run_id']}"
                        f"&task_id={task['task_id']}"
                    ),
                    headers=headers,
                )
                self.assertEqual(selected["mode"]["mode"], "dontAsk")
                self.assertEqual(selected["mode"]["revision"], 1)
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


def _run_code_tool(
    base_url: str,
    task_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    status, response = _post_with_status(
        base_url,
        f"/tasks/{task_id}/workers/code",
        {
            "constraints": {
                "query_turns": [[{
                    "tool_name": tool_name,
                    "tool_call_id": f"api-{tool_name}-1",
                    "arguments": arguments,
                }]],
                "e02PermissionPolicy": {"default_effect": "allow"},
                "permission_interactive": False,
                "permission_headless": True,
            },
        },
    )
    if status != 201:
        phases = []
        for event in response.get("events", []):
            query_session = event.get("payload", {}).get("query_session", {})
            if not isinstance(query_session, dict):
                continue
            phases.append(
                {
                    "phase": query_session.get("phase"),
                    "tool_name": query_session.get("tool_name"),
                    "error": (query_session.get("tool_result") or {}).get("error")
                    if isinstance(query_session.get("tool_result"), dict)
                    else None,
                    "summary": (query_session.get("tool_result") or {}).get("summary")
                    if isinstance(query_session.get("tool_result"), dict)
                    else None,
                    "reason": (query_session.get("tool_result") or {})
                    .get("metadata", {})
                    .get("sandbox_gateway_reason")
                    if isinstance(query_session.get("tool_result"), dict)
                    else None,
                }
            )
        raise AssertionError(
            f"CodeWorker {tool_name} returned HTTP {status}: "
            f"worker_error={response.get('worker_result', {}).get('error')!r}, "
            f"phases={phases!r}"
        )
    return response


def _completed_tool_result(response: dict[str, Any], tool_name: str) -> dict[str, Any]:
    for event in response.get("events", []):
        query_session = event.get("payload", {}).get("query_session", {})
        if (
            query_session.get("phase") == "tool_call_completed"
            and query_session.get("tool_name") == tool_name
            and isinstance(query_session.get("tool_result"), dict)
        ):
            return query_session["tool_result"]
    raise AssertionError(f"CodeWorker response has no completed {tool_name} result")


def _raise_with_body(error: HTTPError, method: str, url: str) -> None:
    """Re-raise an HTTP error that still carries the server's own explanation.

    ``urllib`` discards the response body, so a structured API failure reaches
    pytest as a bare ``HTTP Error 409: Conflict`` and says nothing about which
    contract was violated.  Diagnosing one of these cost a full reproduction
    harness; the body is the only place the error code lives.
    """

    try:
        body = error.read().decode("utf-8")
    except Exception:  # noqa: BLE001 - the original error must survive
        body = "<unreadable>"
    raise AssertionError(
        f"{method} {url} -> HTTP {error.code}: {body}"
    ) from error


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
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        _raise_with_body(error, "GET", f"{base_url}{path}")
        raise


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
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        _raise_with_body(error, "POST", f"{base_url}{path}")
        raise


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
