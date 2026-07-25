from __future__ import annotations

import importlib
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.integration.test_api_control_commands import (
    _get,
    _post,
    _post_with_status,
)


class CommandInputQueueControlIntegrationTests(unittest.TestCase):
    def test_busy_command_queue_restore_cancel_retry_and_event_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
            os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission-state.json")
            os.environ["ZYRA_COMMAND_RUNTIME_STATE"] = str(root / "commands")

            main = importlib.import_module("apps.api.zyra_api.main")
            main = importlib.reload(main)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                main.ZyraRequestHandler,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                task = _post(
                    base_url,
                    "/tasks",
                    {
                        "goal": "Exercise real command queue control.",
                        "auto_run": False,
                    },
                )["task"]
                task_id = task["task_id"]
                run_id = task["run_id"]
                session_id = f"task:{task_id}"
                dispatcher = main.get_control_dispatcher()
                self.assertTrue(
                    dispatcher.mutation_guard.reserve(
                        session_id,
                        "test-busy-holder",
                    )
                )

                first_status, first = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {
                        "text": "/change replace the failed edge worker",
                        "arguments": {
                            "instruction": "replace the failed edge worker",
                        },
                        "request_id": "request_queue_01",
                        "command_id": "cmd_queue_01",
                        "idempotency_key": "command-queue-control-01",
                        "actor_id": "integration-user",
                        "session_id": session_id,
                        "priority": "now",
                        "delivery_mode": "steer",
                    },
                )
                self.assertEqual(first_status, 202)
                self.assertEqual(first["command_result"]["status"], "queued")
                queue_id = first["command_result"]["result"]["followup_queue_id"]
                self.assertTrue(queue_id)

                restored = _get(
                    base_url,
                    (
                        f"/tasks/{task_id}/command-queue"
                        f"?session_id={session_id}&include_terminal=true"
                    ),
                )
                self.assertEqual(restored["canonical_owner"], "PromptQueueRuntime")
                self.assertEqual(restored["projection_owner"], "CanonicalProjectionStore")
                self.assertEqual(len(restored["entries"]), 1)
                queued = restored["entries"][0]
                self.assertEqual(queued["queue_id"], queue_id)
                self.assertEqual(queued["status"], "queued")
                self.assertEqual(queued["priority"], "now")
                self.assertNotIn("claim_token", queued)

                cancel_status, cancelled = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands/request_queue_01/cancel",
                    {
                        "reason": "operator cancelled queued change",
                        "idempotency_key": "command-queue-control-01-cancel",
                    },
                )
                self.assertEqual(cancel_status, 200)
                self.assertEqual(
                    cancelled["command_result"]["status"],
                    "cancelled",
                )
                replay_status, replay = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands/request_queue_01/cancel",
                    {
                        "reason": "operator cancelled queued change",
                        "idempotency_key": "command-queue-control-01-cancel",
                    },
                )
                self.assertEqual(replay_status, 200)
                self.assertEqual(
                    replay["command_result"]["command_id"],
                    "cmd_queue_01",
                )

                after_cancel = _get(
                    base_url,
                    (
                        f"/tasks/{task_id}/command-queue"
                        f"?session_id={session_id}&include_terminal=true"
                    ),
                )
                self.assertEqual(
                    after_cancel["entries"][0]["status"],
                    "cancelled",
                )

                retry_status, retry = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {
                        "text": "/change replace the failed edge worker",
                        "arguments": {
                            "instruction": "replace the failed edge worker",
                        },
                        "request_id": "request_queue_02",
                        "command_id": "cmd_queue_02",
                        "idempotency_key": "command-queue-control-02",
                        "actor_id": "integration-user",
                        "session_id": session_id,
                        "priority": "next",
                        "delivery_mode": "enqueue",
                        "retry_of_request_id": "request_queue_01",
                    },
                )
                self.assertEqual(retry_status, 202)
                self.assertEqual(retry["command_result"]["status"], "queued")
                self.assertNotEqual(
                    retry["command_result"]["command_id"],
                    first["command_result"]["command_id"],
                )
                after_retry = _get(
                    base_url,
                    (
                        f"/tasks/{task_id}/command-queue"
                        f"?session_id={session_id}&include_terminal=true"
                    ),
                )
                retry_entry = next(
                    entry
                    for entry in after_retry["entries"]
                    if entry["payload"]["request"]["request_id"]
                    == "request_queue_02"
                )
                self.assertEqual(
                    retry_entry["payload"]["request"]["metadata"][
                        "retry_of_request_id"
                    ],
                    "request_queue_01",
                )

                events = _get(
                    base_url,
                    f"/tasks/{task_id}/events?limit=500",
                )["events"]
                lifecycle = [
                    event
                    for event in events
                    if event.get("payload", {}).get("command_id")
                    in {"cmd_queue_01", "cmd_queue_02"}
                ]
                phases = {
                    (
                        event["payload"]["command_id"],
                        event["payload"]["phase"],
                    )
                    for event in lifecycle
                }
                self.assertIn(("cmd_queue_01", "received"), phases)
                self.assertIn(("cmd_queue_01", "queued"), phases)
                self.assertIn(("cmd_queue_01", "cancelled"), phases)
                self.assertIn(("cmd_queue_02", "queued"), phases)
                self.assertTrue(
                    all(event["task_id"] == task_id for event in lifecycle)
                )
                self.assertTrue(
                    all(event["run_id"] == run_id for event in lifecycle)
                )

                dispatcher.mutation_guard.release(
                    session_id,
                    "test-busy-holder",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_read_only_command_receipt_and_side_question_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
            os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission-state.json")
            os.environ["ZYRA_COMMAND_RUNTIME_STATE"] = str(root / "commands")

            main = importlib.import_module("apps.api.zyra_api.main")
            main = importlib.reload(main)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                main.ZyraRequestHandler,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                task = _post(
                    base_url,
                    "/tasks",
                    {
                        "goal": "Inspect commands and ask one side question.",
                        "auto_run": False,
                    },
                )["task"]
                task_id = task["task_id"]
                session_id = f"task:{task_id}"
                status_code, status = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {
                        "text": "/status",
                        "request_id": "request_status_01",
                        "command_id": "cmd_status_01",
                        "idempotency_key": "command-status-control-01",
                        "session_id": session_id,
                    },
                )
                self.assertEqual(status_code, 201)
                self.assertTrue(status["command_result"]["ok"])
                self.assertEqual(
                    status["command_result"]["name"],
                    "/status",
                )
                self.assertFalse(status["event_only_stateful_fallback"])

                dispatcher = main.get_control_dispatcher()
                dispatcher.disabled = True
                disabled_code, disabled = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {
                        "text": "/status",
                        "request_id": "request_status_disabled",
                        "command_id": "cmd_status_disabled",
                        "idempotency_key": "command-status-disabled-01",
                        "session_id": session_id,
                    },
                )
                self.assertEqual(disabled_code, 503)
                self.assertEqual(
                    disabled["error"],
                    "control_runtime_unavailable",
                )
                self.assertFalse(disabled["fallback"])
                dispatcher.disabled = False

                malformed_code, malformed = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {
                        "text": "/status",
                        "request_id": "request_status_malformed",
                        "command_id": "cmd_status_malformed",
                        "idempotency_key": "command-status-malformed-01",
                        "session_id": session_id,
                        "delivery_mode": "chat-fallback",
                    },
                )
                self.assertEqual(malformed_code, 400)
                self.assertEqual(
                    malformed["error"],
                    "unknown_or_invalid_command",
                )

                before = _get(base_url, f"/tasks/{task_id}")
                before_events = _get(
                    base_url,
                    f"/tasks/{task_id}/events?limit=500",
                )["events"]
                btw_code, btw = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {
                        "text": "/btw why is this task pending?",
                        "arguments": {
                            "question": "why is this task pending?",
                        },
                        "request_id": "request_btw_01",
                        "command_id": "cmd_btw_01",
                        "idempotency_key": "command-btw-control-01",
                        "session_id": session_id,
                    },
                )
                self.assertIn(btw_code, {201, 409})
                result = btw["command_result"]
                if btw_code == 201:
                    self.assertTrue(result["ok"])
                    self.assertEqual(result["name"], "/btw")
                    self.assertFalse(
                        result["data"].get("tools_used", False)
                    )
                after = _get(base_url, f"/tasks/{task_id}")
                after_events = _get(
                    base_url,
                    f"/tasks/{task_id}/events?limit=500",
                )["events"]
                self.assertEqual(
                    before["task"]["metadata"].get("messages"),
                    after["task"]["metadata"].get("messages"),
                )
                btw_events = [
                    event
                    for event in after_events[len(before_events):]
                    if event.get("payload", {}).get("command_id")
                    == "cmd_btw_01"
                ]
                self.assertTrue(btw_events)
                self.assertTrue(
                    all(
                        event["payload"].get("command_id")
                        == "cmd_btw_01"
                        for event in btw_events
                    )
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
