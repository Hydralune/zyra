from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_api_module() -> Any:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


class TopologyInteractionControlTests(unittest.TestCase):
    def test_control_commands_mutate_canonical_owner_resume_exact_checkpoint_and_deny_sealed(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            root = Path(tmpdir)
            environment = {
                "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
                "ZYRA_RECOVERY_SQLITE_PATH": str(root / "recovery.sqlite3"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_TOOL_WORKSPACE": str(root / "workspace"),
                "ZYRA_WORKSPACE_ROOT": str(root / "managed-workspaces"),
                "ZYRA_PERMISSION_STATE": str(root / "permission-state.json"),
                "ZYRA_PROVIDER_CONTROL_STATE": str(root / "provider-control.sqlite3"),
                "ZYRA_DISABLE_RECOVERY_RUNTIME": "false",
            }
            previous = {name: os.environ.get(name) for name in environment}
            os.environ.update(environment)
            module = _fresh_api_module()
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                module.ZyraRequestHandler,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(
                    base_url,
                    "/tasks",
                    {
                        "goal": "Control a dynamic topology from the live console.",
                        "auto_run": False,
                    },
                )
                task = created["task"]
                task_id = task["task_id"]
                run_id = task["run_id"]
                session_id = f"task:{task_id}"

                changed_status, changed = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {
                        "text": "/change require verifier evidence before route commit",
                        "arguments": {
                            "requirement": "require verifier evidence before route commit",
                            "node_id": task["root_node_id"],
                            "change_scope": "local_update_state",
                            "fields": {"state": "needs_revision"},
                        },
                        "request_id": "controlreq_topology_change_001",
                        "command_id": "cmd_topology_change_001",
                        "idempotency_key": "topology-control-change-once",
                        "actor_id": "topology-console",
                        "sealed": False,
                        "competition_mode": "interactive",
                    },
                )
                self.assertEqual(changed_status, 201, changed)
                self.assertTrue(changed["command_result"]["ok"])
                self.assertEqual(changed["command"]["name"], "/change")
                self.assertFalse(changed["intervention_counted"])
                self.assertEqual(
                    changed["task"]["metadata"]["requirement_changes"][-1]["text"],
                    "require verifier evidence before route commit",
                )
                self.assertTrue(
                    changed["task"]["metadata"]["requirement_changes"][-1][
                        "affected_node_ids"
                    ]
                )

                first_checkpoint = self._checkpoint(
                    base_url,
                    task_id=task_id,
                    run_id=run_id,
                    session_id=session_id,
                    ordinal=1,
                )
                second_checkpoint = self._checkpoint(
                    base_url,
                    task_id=task_id,
                    run_id=run_id,
                    session_id=session_id,
                    ordinal=2,
                )

                rewind_status, rewound = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {
                        "text": f"/rewind {first_checkpoint}",
                        "arguments": {
                            "checkpoint_ref": first_checkpoint,
                            "target": first_checkpoint,
                        },
                        "request_id": "controlreq_topology_rewind_001",
                        "command_id": "cmd_topology_rewind_001",
                        "idempotency_key": "topology-control-rewind-once",
                        "session_id": session_id,
                        "actor_id": "topology-console",
                        "sealed": False,
                        "competition_mode": "interactive",
                    },
                )
                self.assertEqual(rewind_status, 201, rewound)
                self.assertTrue(rewound["command_result"]["ok"])
                rewind_effect = rewound["command_result"]["data"]["transaction"][
                    "effect"
                ]
                self.assertEqual(rewind_effect["checkpoint_ref"], first_checkpoint)
                self.assertTrue(rewind_effect["metadata"]["exact_resume"])
                self.assertEqual(
                    rewind_effect["metadata"]["state_owner"],
                    "RecoveryApplication.resume_checkpoint",
                )

                resume_status, resumed = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    {
                        "text": f"/resume {session_id}",
                        "arguments": {
                            "target_session_id": session_id,
                            "checkpoint_ref": second_checkpoint,
                        },
                        "request_id": "controlreq_topology_resume_001",
                        "command_id": "cmd_topology_resume_001",
                        "idempotency_key": "topology-control-resume-once",
                        "session_id": session_id,
                        "actor_id": "topology-console",
                        "sealed": False,
                        "competition_mode": "interactive",
                    },
                )
                self.assertEqual(resume_status, 201, resumed)
                self.assertTrue(resumed["command_result"]["ok"])
                resume_effect = resumed["command_result"]["data"]["transaction"][
                    "effect"
                ]
                self.assertEqual(resume_effect["checkpoint_ref"], second_checkpoint)
                self.assertTrue(resume_effect["metadata"]["same_session"])

                denied_payload = {
                    "text": "/change replace the active worker route manually",
                    "arguments": {
                        "requirement": "replace the active worker route manually",
                        "node_id": task["root_node_id"],
                    },
                    "request_id": "controlreq_topology_sealed_001",
                    "command_id": "cmd_topology_sealed_001",
                    "idempotency_key": "topology-control-sealed-denial-once",
                    "actor_id": "sealed-benchmark-observer",
                    "sealed": True,
                    "competition_mode": "sealed_autonomous",
                }
                denied_status, denied = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    denied_payload,
                )
                self.assertEqual(denied_status, 409, denied)
                self.assertFalse(denied["command_result"]["ok"])
                self.assertEqual(
                    denied["command_result"]["error"]["code"],
                    "permission_denied",
                )
                self.assertTrue(denied["intervention_counted"])
                self.assertEqual(denied["operator_intervention_attempt_count"], 1)
                self.assertEqual(denied["human_intervention_count"], 0)

                replay_status, replay = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/commands",
                    denied_payload,
                )
                self.assertEqual(replay_status, 409, replay)
                state = module.get_store().load_task(task_id)
                self.assertIsNotNone(state)
                self.assertEqual(
                    state.metadata["operator_intervention_attempt_count"],
                    1,
                )
                self.assertEqual(state.metadata["human_intervention_count"], 0)
                self.assertEqual(
                    len(state.metadata["operator_intervention_ledger"]),
                    1,
                )
                self.assertGreaterEqual(
                    len(state.metadata.get("control_mutations") or ()),
                    3,
                )
                events = _get(base_url, f"/tasks/{task_id}/events")["events"]
                time_travel_events = [
                    event
                    for event in events
                    if event["event_type"] == "topology_route"
                    and (
                        event.get("payload", {})
                        .get("recovery_runtime", {})
                        .get("phase")
                        == "checkpoint_resumed"
                    )
                ]
                self.assertEqual(len(time_travel_events), 2)
                self.assertEqual(
                    {
                        event["payload"]["recovery_runtime"]["action"]
                        for event in time_travel_events
                    },
                    {"rewind", "resume"},
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def _checkpoint(
        self,
        base_url: str,
        *,
        task_id: str,
        run_id: str,
        session_id: str,
        ordinal: int,
    ) -> str:
        status, body = _post_with_status(
            base_url,
            f"/tasks/{task_id}/recovery/checkpoints",
            {
                "run_id": run_id,
                "task_id": task_id,
                "session_id": session_id,
                "workflow_signature": "workflow-topology-control-v1",
                "graph_signature": f"graph-topology-control-v{ordinal}",
                "topology_signature": f"topology-control-v{ordinal}",
                "owner_refs": {"task": task_id, "session": session_id},
                "version_refs": {"task": ordinal, "session": ordinal},
                "state_payload": {
                    "progress": ordinal,
                    "active_node_id": f"node-{ordinal}",
                },
                "completed_step_ids": [f"step-{ordinal}"],
            },
        )
        self.assertEqual(status, 201, body)
        return str(body["checkpoint"]["checkpoint_id"])


def _get(base_url: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url}{path}", method="GET")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(
    base_url: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    status, body = _post_with_status(base_url, path, payload)
    if status >= 400:
        raise AssertionError(body)
    return body


def _post_with_status(
    base_url: str,
    path: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
