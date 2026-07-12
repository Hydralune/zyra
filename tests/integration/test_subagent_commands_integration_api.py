from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


class SubagentCommandsIntegrationApiTests(unittest.TestCase):
    def test_signed_spawn_ceiling_and_durable_control_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workspace").mkdir()
            environment = {
                "ZYRA_SQLITE_PATH": str(root / "state.sqlite"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_PERMISSION_STATE": str(root / "permission.json"),
                "ZYRA_CONTROL_STATE": str(root / "control"),
                "ZYRA_SUBAGENT_STATE": str(root / "subagents"),
                "ZYRA_MCP_STATE": str(root / "mcp.json"),
            }
            with patch.dict(os.environ, environment, clear=False):
                from apps.api.zyra_api import main

                main.reset_control_runtime()
                main.reset_subagent_runtime()
                main.reset_mcp_runtime()
                server = ThreadingHTTPServer(("127.0.0.1", 0), main.ZyraRequestHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_address[1]}"
                try:
                    task = _post(base, "/tasks", {"goal": "audit signed child scope", "auto_run": False})["task"]
                    cleared = _post(base, f"/tasks/{task['task_id']}/commands", {
                        "text": "/clear",
                        "idempotency_key": "clear-session-once",
                    })
                    self.assertTrue(cleared["command_result"]["ok"])
                    self.assertEqual(cleared["task"]["metadata"]["session_epoch"], 1)
                    self.assertEqual(
                        cleared["command_result"]["data"]["owner"],
                        "CanonicalSessionStore",
                    )
                    rejected_status, rejected = _post_error(base, f"/tasks/{task['task_id']}/subagents", {
                        "prompt": "try to expand authority",
                        "execution_mode": "background",
                        "available_mcp_servers": ["caller-invented-parent"],
                        "requested_mcp_servers": ["caller-invented-parent"],
                        "permission_mode": "bypass",
                        "requested_permission_mode": "bypass",
                        "requested_tools": ["caller_invented_tool"],
                    })
                    self.assertEqual(rejected_status, 409)
                    self.assertEqual(rejected["error"], "subagent_spawn_rejected")
                    self.assertNotIn("caller-invented-parent", rejected.get("message", "").split("available"))
                    scope_state = json.loads(
                        (root / "subagents" / "integration" / "parent-scopes.json").read_text(encoding="utf-8")
                    )
                    snapshots = scope_state["snapshots"]
                    self.assertEqual(len(snapshots), 1)
                    scope = snapshots[0]
                    self.assertEqual(scope["parent_task_id"], task["task_id"])
                    self.assertEqual(scope["metadata"]["client_declared_ceiling"], False)
                    self.assertNotIn("caller_invented_tool", [item["name"] for item in scope["tools"]])
                    self.assertNotIn("caller-invented-parent", scope["mcp_servers"])

                    fanout = _post(base, f"/tasks/{task['task_id']}/subagents/fanout", {
                        "shared_context": "Return one typed result per independent item.",
                        "execution_mode": "foreground",
                        "failure_policy": "require_all",
                        "maximum_concurrency": 2,
                        "idempotency_key": "api-fanout-1",
                        "yield_schema": {
                            "type": "object",
                            "required": ["surface", "ok"],
                            "additionalProperties": False,
                            "properties": {
                                "surface": {"type": "string"},
                                "ok": {"type": "boolean"},
                            },
                        },
                        "items": [
                            {
                                "item_id": "api",
                                "name": "api",
                                "agent_type": "general-purpose",
                                "prompt": "Yield the API result.",
                                "requested_tools": ["SubagentYield"],
                                "constraints": {"tool_plan": [{
                                    "tool_name": "SubagentYield",
                                    "arguments": {"summary": "api complete", "data": {"surface": "api", "ok": True}},
                                }]},
                            },
                            {
                                "item_id": "runtime",
                                "name": "runtime",
                                "agent_type": "general-purpose",
                                "prompt": "Yield the runtime result.",
                                "requested_tools": ["SubagentYield"],
                                "constraints": {"tool_plan": [{
                                    "tool_name": "SubagentYield",
                                    "arguments": {"summary": "runtime complete", "data": {"surface": "runtime", "ok": True}},
                                }]},
                            },
                        ],
                    })
                    self.assertEqual(fanout["record"]["status"], "completed", fanout)
                    self.assertEqual(fanout["record"]["aggregate"]["success_count"], 2)
                    self.assertEqual(len(fanout["record"]["aggregate"]["results"]), 2)

                    session_id = f"task:{task['task_id']}"
                    initialized = _post(base, f"/tasks/{task['task_id']}/control-frames", {
                        "protocol_version": 1,
                        "stream_id": "stream-api-1",
                        "message_id": "initialize-1",
                        "correlation_id": "initialize-1",
                        "sequence": 1,
                        "type": "initialize",
                        "payload": {
                            "client_id": "integration-test",
                            "run_id": task["run_id"],
                            "task_id": task["task_id"],
                            "session_id": session_id,
                        },
                    })
                    self.assertEqual(initialized["envelope"]["status"], "resolved")
                    generation = _get(base, "/commands")["registry"]["generation"]
                    request_frame = {
                        "protocol_version": 1,
                        "stream_id": "stream-api-1",
                        "message_id": "status-1",
                        "correlation_id": "status-1",
                        "sequence": 2,
                        "type": "control_request",
                        "payload": {
                            "request": {
                                "protocol_version": "zyra.control/v1",
                                "request_id": "stream-request-1",
                                "command_id": "stream-command-1",
                                "run_id": task["run_id"],
                                "task_id": task["task_id"],
                                "session_id": session_id,
                                "canonical_name": "/status",
                                "registry_generation": generation,
                                "origin": "api",
                                "arguments": {},
                            }
                        },
                    }
                    response = _post(base, f"/tasks/{task['task_id']}/control-frames", request_frame)
                    replay = _post(base, f"/tasks/{task['task_id']}/control-frames", request_frame)
                    self.assertEqual(response["envelope"]["frame_id"], replay["envelope"]["frame_id"])
                    self.assertTrue(response["envelope"]["payload"]["ok"])
                    durable = json.loads(
                        (root / "control" / "structured" / "control-frames.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(durable["schema"], "zyra.control-frame-store/v1")
                    self.assertEqual(len(durable["frames"]), 2)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                    main.reset_control_runtime()
                    main.reset_subagent_runtime()
                    main.reset_mcp_runtime()


def _request(base: str, path: str, *, method: str, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base: str, path: str, payload):
    return _request(base, path, method="POST", payload=payload)


def _get(base: str, path: str):
    return _request(base, path, method="GET")


def _post_error(base: str, path: str, payload):
    try:
        _post(base, path, payload)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
    raise AssertionError("expected HTTP error")


if __name__ == "__main__":
    unittest.main()
