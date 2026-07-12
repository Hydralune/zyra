from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


class SubagentCommandsFoundationApiTests(unittest.TestCase):
    def test_commands_use_durable_dispatch_and_goal_is_real_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            os.environ.update({
                "ZYRA_SQLITE_PATH": str(root / "state.sqlite3"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_PERMISSION_STATE": str(root / "permission.json"),
                "ZYRA_CONTROL_STATE": str(root / "control"),
                "ZYRA_SUBAGENT_STATE": str(root / "subagents"),
            })
            from apps.api.zyra_api import main

            main.reset_control_runtime()
            main.reset_subagent_runtime()
            server = ThreadingHTTPServer(("127.0.0.1", 0), main.ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                task = _post(base, "/tasks", {"goal": "old objective", "auto_run": False})["task"]
                status = _post(base, f"/tasks/{task['task_id']}/commands", {
                    "text": "/status", "idempotency_key": "status-once",
                })
                changed = _post(base, f"/tasks/{task['task_id']}/commands", {
                    "text": "/goal new canonical objective", "idempotency_key": "goal-once",
                })
                replay = _post(base, f"/tasks/{task['task_id']}/commands", {
                    "text": "/goal new canonical objective", "idempotency_key": "goal-once",
                })
                side = _post(base, f"/tasks/{task['task_id']}/commands", {
                    "text": "/btw what is the current objective?",
                })
                view = _get(base, f"/tasks/{task['task_id']}")
                subagents = _get(base, f"/tasks/{task['task_id']}/subagents")
                generation = _get(base, "/commands")["registry"]["generation"]
                frame = _post(base, f"/tasks/{task['task_id']}/control-frames", {
                    "protocol_version": "zyra.structured-io/v1",
                    "message_type": "control_request",
                    "message_id": "structured-status-1",
                    "payload": {
                        "protocol_version": "zyra.control/v1",
                        "request_id": "structured-control-1",
                        "command_id": "structured-command-1",
                        "run_id": task["run_id"],
                        "task_id": task["task_id"],
                        "session_id": f"task:{task['task_id']}",
                        "canonical_name": "/status",
                        "registry_generation": generation,
                    },
                })

                self.assertTrue(status["command_result"]["ok"])
                self.assertTrue(changed["command_result"]["ok"])
                self.assertEqual("new canonical objective", changed["task"]["user_goal"])
                self.assertEqual(changed["command_result"]["request_id"], replay["command_result"]["request_id"])
                self.assertEqual("new canonical objective", view["task"]["user_goal"])
                self.assertTrue(side["command_result"]["ok"])
                self.assertFalse(side["command_result"]["data"]["parent_messages_mutated"])
                self.assertFalse(side["command_result"]["data"]["main_replan_triggered"])
                self.assertEqual([], subagents["subagents"])
                self.assertFalse(subagents["physical_worker_state_owned"])
                self.assertEqual("control_response", frame["envelope"]["message_type"])
                self.assertEqual("succeeded", frame["envelope"]["payload"]["status"])
                self.assertIn("structured-control-1", frame["state"]["resolved"])
                request_state = json.loads((root / "control" / "requests.json").read_text(encoding="utf-8"))
                self.assertGreaterEqual(len(request_state["records"]), 3)
                self.assertTrue(all(item["status"] == "succeeded" for item in request_state["records"]))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                main.reset_control_runtime()
                main.reset_subagent_runtime()

    def test_stateful_command_without_owner_fails_instead_of_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            os.environ.update({
                "ZYRA_SQLITE_PATH": str(root / "state.sqlite3"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_PERMISSION_STATE": str(root / "permission.json"),
                "ZYRA_CONTROL_STATE": str(root / "control"),
            })
            from apps.api.zyra_api import main

            main.reset_control_runtime()
            server = ThreadingHTTPServer(("127.0.0.1", 0), main.ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                task = _post(base, "/tasks", {"goal": "fail closed", "auto_run": False})["task"]
                code, response = _post_status(base, f"/tasks/{task['task_id']}/commands", {"text": "/hooks reload"})
                self.assertEqual(409, code)
                self.assertFalse(response["command_result"]["ok"])
                self.assertIn(
                    response["command_result"]["error"]["code"],
                    {"permission_denied", "state_owner_unavailable"},
                )
                self.assertFalse(response["event_only_stateful_fallback"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                main.reset_control_runtime()


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base: str, path: str, payload: dict) -> dict:
    code, result = _post_status(base, path, payload)
    if code >= 400:
        raise AssertionError(result)
    return result


def _post_status(base: str, path: str, payload: dict) -> tuple[int, dict]:
    from urllib.error import HTTPError

    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
