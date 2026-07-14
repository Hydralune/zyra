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
                    self.assertFalse(
                        (root / "subagents" / "integration" / "parent-scopes.json").exists(),
                        "retired Python parent-scope store must not be recreated",
                    )

                    fanout_payload = {
                        "request_id": "api-fanout-request-1",
                        "session_id": f"task:{task['task_id']}",
                        "shared_context": "Run two bounded child tasks.",
                        "failure_policy": "collect",
                        "maximum_concurrency": 2,
                        "idempotency_key": "api-fanout-1",
                        "items": [
                            {
                                "item_id": "api",
                                "agent_type": "general-purpose",
                                "prompt": "Inspect the API surface.",
                            },
                            {
                                "item_id": "runtime",
                                "agent_type": "general-purpose",
                                "prompt": "Inspect the runtime surface.",
                            },
                        ],
                    }
                    fanout_status, fanout = _post_error(
                        base,
                        f"/tasks/{task['task_id']}/subagents/fanout",
                        fanout_payload,
                    )
                    self.assertEqual(fanout_status, 409)
                    self.assertEqual(fanout["error"], "permission_suspended")
                    self.assertEqual(fanout["canonical_agent_owner"], "typescript")
                    self.assertFalse(fanout["python_agent_fallback"])
                    self.assertEqual(
                        _get(base, f"/tasks/{task['task_id']}/subagents")["subagents"],
                        [],
                        "permission suspension must occur before task creation",
                    )

                    custody_token = fanout["permission_session"]["session_custody_token"]
                    custody_headers = {"Authorization": f"Bearer {custody_token}"}
                    pending = _get(
                        base,
                        (
                            "/permissions/requests"
                            f"?session_id={fanout_payload['session_id']}"
                            f"&run_id={task['run_id']}"
                            f"&task_id={task['task_id']}&pending_only=true"
                        ),
                        headers=custody_headers,
                    )["requests"]["items"]
                    self.assertEqual(len(pending), 1)
                    _post(
                        base,
                        f"/permissions/requests/{pending[0]['request_id']}/resolve",
                        {
                            "session_id": fanout_payload["session_id"],
                            "run_id": task["run_id"],
                            "task_id": task["task_id"],
                            "effect": "allow",
                            "idempotency_key": "approve-api-agent-fanout",
                        },
                        headers=custody_headers,
                    )
                    completed_status, completed_fanout = _post_with_status(
                        base,
                        f"/tasks/{task['task_id']}/subagents/fanout",
                        fanout_payload,
                        headers=custody_headers,
                    )
                    self.assertEqual(completed_status, 201, completed_fanout)
                    self.assertTrue(completed_fanout["ok"], completed_fanout)
                    self.assertEqual(completed_fanout["canonical_agent_owner"], "typescript")
                    self.assertFalse(completed_fanout["python_agent_fallback"])
                    durable_children = _get(base, f"/tasks/{task['task_id']}/subagents")["subagents"]
                    agent_tool_results = [
                        event["payload"]["tool_result"]
                        for event in completed_fanout["events"]
                        if "tool_result" in event["payload"]
                    ]
                    self.assertEqual(len(durable_children), 2, agent_tool_results)
                    self.assertTrue(all(item["status"] == "completed" for item in durable_children))
                    child_session_events = [
                        event
                        for event in completed_fanout["events"]
                        if "agent_child_query_session" in event["payload"]
                    ]
                    self.assertTrue(child_session_events)
                    self.assertFalse([
                        event
                        for event in completed_fanout["events"]
                        if event["payload"].get("query_session", {}).get("session_id")
                        and event["payload"]["query_session"]["session_id"]
                        != fanout_payload["session_id"]
                    ])
                    resume_payload = {
                        "request_id": "api-agent-resume-1",
                        "expected_task_revision": durable_children[0]["revision"],
                        "resume_correlation_id": "api-agent-resume-correlation-1",
                        "restored_state": {},
                        "turns": [],
                    }
                    resume_status, resume_blocked = _post_with_status(
                        base,
                        (
                            f"/tasks/{task['task_id']}/subagents/"
                            f"{durable_children[0]['task_id']}/resume"
                        ),
                        resume_payload,
                        headers=custody_headers,
                    )
                    self.assertEqual(resume_status, 409, resume_blocked)
                    self.assertEqual(resume_blocked["error"], "permission_suspended")
                    resume_pending = _get(
                        base,
                        (
                            "/permissions/requests"
                            f"?session_id={fanout_payload['session_id']}"
                            f"&run_id={task['run_id']}"
                            f"&task_id={task['task_id']}&pending_only=true"
                        ),
                        headers=custody_headers,
                    )["requests"]["items"]
                    self.assertEqual(len(resume_pending), 1)
                    _post(
                        base,
                        f"/permissions/requests/{resume_pending[0]['request_id']}/resolve",
                        {
                            "session_id": fanout_payload["session_id"],
                            "run_id": task["run_id"],
                            "task_id": task["task_id"],
                            "effect": "allow",
                            "idempotency_key": "approve-api-agent-resume",
                        },
                        headers=custody_headers,
                    )
                    resumed_status, resumed = _post_with_status(
                        base,
                        (
                            f"/tasks/{task['task_id']}/subagents/"
                            f"{durable_children[0]['task_id']}/resume"
                        ),
                        resume_payload,
                        headers=custody_headers,
                    )
                    self.assertEqual(resumed_status, 200, resumed)
                    self.assertTrue(resumed["ok"], resumed)
                    self.assertEqual(resumed["task"]["status"], "completed")
                    self.assertGreater(resumed["task"]["revision"], durable_children[0]["revision"])
                    self.assertEqual(resumed["canonical_agent_owner"], "typescript")
                    self.assertFalse(resumed["python_agent_fallback"])
                    for path in root.rglob("*"):
                        if path.is_file():
                            self.assertNotIn(custody_token.encode("utf-8"), path.read_bytes(), str(path))

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


def _request(base: str, path: str, *, method: str, payload=None, headers=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base: str, path: str, payload, *, headers=None):
    return _request(base, path, method="POST", payload=payload, headers=headers)


def _get(base: str, path: str, *, headers=None):
    return _request(base, path, method="GET", headers=headers)


def _post_error(base: str, path: str, payload):
    try:
        _post(base, path, payload)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
    raise AssertionError("expected HTTP error")


def _post_with_status(base: str, path: str, payload, *, headers=None):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
