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


class RecoveryPlannerRoutingMemoryApiTests(unittest.TestCase):
    def test_real_api_recovery_mutates_task_persists_memory_and_disables_fail_closed(self) -> None:
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
            server = ThreadingHTTPServer(("127.0.0.1", 0), module.ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {
                    "goal": "Recover a stalled long-horizon task without a human.",
                    "auto_run": False,
                })
                task = created["task"]
                task_id = task["task_id"]
                run_id = task["run_id"]
                canonical_session_id = task["metadata"]["query_session_id"]

                status, recovered = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/recovery/signals",
                    {
                        "source": "api_runtime",
                        "source_kind": "stream_stall",
                        "refs": {
                            "run_id": run_id,
                            "task_id": task_id,
                            "session_id": canonical_session_id,
                            "request_id": "request-stalled-1",
                            "provider_id": "provider-stalled-1",
                        },
                        "summary": "provider stream produced no frame before the deadline",
                        "retryable": True,
                        "idempotency_key": "recover-stall-once",
                    },
                )
                self.assertEqual(status, 202, recovered)
                self.assertTrue(recovered["ok"])
                self.assertEqual(recovered["plan"]["decision"]["selected"]["action"], "retry")
                self.assertTrue(recovered["execution"]["outcome"]["success"])
                self.assertTrue(recovered["execution"]["receipts"][0]["changed_execution"])

                state = module.get_store().load_task(task_id)
                self.assertIsNotNone(state)
                recovery_projection = state.metadata["recovery_runtime"]
                self.assertEqual(recovery_projection["last_mutation"]["action"], "retry")
                self.assertEqual(recovery_projection["retry_generation"], 1)

                view = _get(base_url, f"/tasks/{task_id}/recovery")
                self.assertEqual(len(view["signals"]), 1)
                self.assertEqual(len(view["outcomes"]), 1)
                self.assertEqual(len(view["routing_memory"]), 1)
                self.assertTrue(view["runtime_audit"]["ok"])
                self.assertTrue(any(
                    item["entity_kind"] == "routing_memory"
                    for item in view["journal"]
                ))

                checkpoint_head = view["checkpoint_head"]
                self.assertEqual(checkpoint_head["commit_revision"], 1)
                checkpoint_status, checkpoint_body = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/recovery/checkpoints",
                    {
                        "run_id": run_id,
                        "task_id": task_id,
                        "session_id": checkpoint_head["session_id"],
                        "workflow_signature": checkpoint_head["workflow_signature"],
                        "graph_signature": checkpoint_head["graph_signature"],
                        "topology_signature": checkpoint_head["topology_signature"],
                        "owner_refs": {"task": task_id, "session": checkpoint_head["session_id"]},
                        "version_refs": {"task": 1, "session": 1},
                        "state_payload": {"progress": 4, "artifacts": ["artifact-a"]},
                        "completed_step_ids": ["step-4"],
                        "expected_revision": checkpoint_head["commit_revision"],
                    },
                )
                self.assertEqual(checkpoint_status, 201, checkpoint_body)
                checkpoint = checkpoint_body["checkpoint"]
                self.assertEqual(checkpoint["commit_revision"], 2)

                delta_status, delta = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/recovery/deltas",
                    {
                        "task_id": task_id,
                        "owner": "integration-branch",
                        "branch_id": "integration-branch-1",
                        "operations": [
                            {"operation": "increment", "key": "progress", "value": 1},
                            {"operation": "append_unique", "key": "artifacts", "value": "artifact-b"},
                        ],
                    },
                )
                self.assertEqual(delta_status, 200)
                self.assertTrue(delta["committed"])
                self.assertEqual(delta["checkpoint"]["state_payload"]["progress"], 5)
                self.assertEqual(delta["checkpoint"]["state_payload"]["artifacts"], ["artifact-a", "artifact-b"])

                after_delta = _get(base_url, f"/tasks/{task_id}/recovery")
                self.assertEqual(after_delta["checkpoint_head"]["commit_revision"], 3)
                self.assertTrue(after_delta["runtime_audit"]["ok"])

                replan_status, replanned = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/recovery/signals",
                    {
                        "source": "control_runtime",
                        "source_kind": "requirement_changed",
                        "refs": {
                            "run_id": run_id,
                            "task_id": task_id,
                            "session_id": canonical_session_id,
                            "node_id": task["root_node_id"],
                            "request_id": "requirement-change-1",
                        },
                        "summary": "acceptance criteria changed while the task was active",
                        "context": {"checkpoint_state": {"candidate_step_ids": ["step-4", "step-5"]}},
                    },
                )
                replan_state = module.get_store().load_task(task_id)
                self.assertEqual(
                    replan_status,
                    202,
                    {
                        "response": replanned,
                        "failed_continuations": {
                            key: value
                            for key, value in replan_state.metadata.get(
                                "recovery_continuation_fences", {}
                            ).items()
                            if value.get("phase") == "failed"
                        },
                    },
                )
                self.assertEqual(replanned["plan"]["decision"]["selected"]["action"], "replan")
                self.assertGreaterEqual(replanned["plan"]["provenance"]["memory_evidence_count"], 1)
                self.assertTrue(replanned["execution"]["outcome"]["success"])
                self.assertEqual(replanned["execution"]["receipts"][0]["route_decision"]["changes"][0]["owner"], "GraphStateCustody")
                self.assertEqual(replanned["execution"]["receipts"][1]["checkpoint_receipt"]["bypassed_step_ids"], ["step-4"])
                self.assertIn("dynamic_graph_ref", module.get_store().load_task(task_id).metadata)

                after_replan = _get(base_url, f"/tasks/{task_id}/recovery")
                self.assertEqual(len(after_replan["route_decisions"]), 1)
                self.assertEqual(len(after_replan["routing_memory"]), 2)
                self.assertTrue(after_replan["runtime_audit"]["ok"])

                os.environ["ZYRA_DISABLE_RECOVERY_RUNTIME"] = "true"
                disabled_status, disabled = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/recovery/signals",
                    {
                        "source": "api_runtime",
                        "source_kind": "stream_stall",
                        "refs": {"run_id": run_id, "task_id": task_id, "request_id": "request-disabled"},
                        "summary": "disabled runtime must not fall back",
                    },
                )
                self.assertEqual(disabled_status, 503)
                self.assertEqual(disabled["error"], "recovery_runtime_disabled")
                self.assertFalse(disabled.get("fallback", False))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
                module.reset_recovery_runtime_api()
                module.reset_provider_control_client()
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_integrated_observation_causal_feedback_and_restart_routes(self) -> None:
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
                "ZYRA_DISABLE_RECOVERY_CLASSIFIER": "false",
            }
            previous = {name: os.environ.get(name) for name in environment}
            os.environ.update(environment)
            module = _fresh_api_module()
            server = ThreadingHTTPServer(("127.0.0.1", 0), module.ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(base_url, "/tasks", {
                    "goal": "Exercise integrated recovery observation and durable restart.",
                    "auto_run": False,
                })
                task = created["task"]
                task_id = task["task_id"]
                run_id = task["run_id"]
                observation = {
                    "domain": "api",
                    "owner": "QueryEngine",
                    "owner_revision": "query-session:4",
                    "span_id": "span-stream-stall-1",
                    "event_ids": [created["events"][0]["event_id"]],
                    "observation": {
                        "source": "api_runtime",
                        "source_kind": "stream_stall",
                        "error_kind": "stream_stall",
                        "refs": {
                            "run_id": run_id,
                            "task_id": task_id,
                            "session_id": "session-integrated",
                            "request_id": "request-integrated-1",
                            "provider_id": "provider-integrated-a",
                        },
                        "summary": "stream produced no observable output",
                        "attempt_count": 1,
                    },
                    "apply": True,
                    "idempotency_key": "integrated-observation-1",
                }
                status, integrated = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/recovery/observations",
                    observation,
                )
                self.assertEqual(status, 202, integrated)
                self.assertTrue(integrated["success"])
                self.assertGreaterEqual(len(integrated["fused_state"]["families"]), 3)
                self.assertTrue(integrated["feedback_influence"]["accepted"])
                self.assertTrue(integrated["feedback_influence"]["changed_later_decision"])
                self.assertTrue(integrated["causal_trace"]["complete"])
                continued_state = module.get_store().load_task(task_id)
                self.assertIsNotNone(continued_state)
                self.assertEqual(
                    str(continued_state.status),
                    "completed",
                    integrated["attempts"][0]["result"]["execution"]["applied_proof"],
                )
                self.assertTrue(continued_state.artifacts)
                continuation_state = continued_state.metadata["recovery_continuation"]
                owner_receipt = continuation_state["owner_receipt"]
                self.assertTrue(owner_receipt["canonical_ref"]["execution_event_ids"])
                self.assertFalse(owner_receipt["canonical_ref"]["event_only"])
                self.assertTrue(owner_receipt["canonical_ref"]["worker_dispatch_consumed"])
                kinds = set(integrated["causal_trace"]["fact_kinds"])
                self.assertTrue({
                    "recovery_signal",
                    "recovery_plan",
                    "action_receipt",
                    "outcome",
                    "continuation",
                    "memory_update",
                }.issubset(kinds))
                plan_id = integrated["attempts"][-1]["plan_id"]
                plan_view = _get(base_url, f"/recovery/plans/{plan_id}")
                self.assertEqual(plan_view["causal_trace"]["plan_id"], plan_id)
                self.assertFalse(plan_view["restart_candidate"]["ready"])
                component_view = _get(base_url, "/recovery/components")
                self.assertTrue(component_view["ready"])
                self.assertFalse(component_view["legacy_fallback"])

                planned_payload = {
                    **observation,
                    "observation": {
                        **observation["observation"],
                        "refs": {
                            **observation["observation"]["refs"],
                            "request_id": "request-integrated-restart",
                        },
                        "summary": "persist a recovery plan before simulated restart",
                    },
                    "span_id": "span-stream-stall-restart",
                    "event_ids": [],
                    "apply": False,
                    "idempotency_key": "integrated-observation-restart",
                }
                planned_status, planned = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/recovery/observations",
                    planned_payload,
                )
                self.assertEqual(planned_status, 200, planned)
                planned_id = planned["attempts"][-1]["plan_id"]
                module.reset_recovery_runtime_api()
                restart_status, restarted = _post_with_status(
                    base_url,
                    f"/recovery/plans/{planned_id}/restart",
                    {"task_id": task_id},
                )
                restart_state = module.get_store().load_task(task_id)
                self.assertEqual(
                    restart_status,
                    200,
                    {
                        "response": restarted,
                        "continuation_fences": {
                            key: value
                            for key, value in restart_state.metadata.get(
                                "recovery_continuation_fences", {}
                            ).items()
                            if value.get("phase") == "failed"
                        },
                    },
                )
                self.assertTrue(restarted["success"])
                self.assertEqual(restarted["candidate"]["plan_id"], planned_id)
                self.assertTrue(restarted["result"]["applied_proof"]["applied"])
                integrated_view = _get(base_url, f"/tasks/{task_id}/recovery")
                self.assertTrue(integrated_view["runtime_audit"]["ok"], integrated_view["runtime_audit"])
                self.assertEqual(integrated_view["runtime_audit"]["blocker_count"], 0)

                os.environ["ZYRA_DISABLE_RECOVERY_CLASSIFIER"] = "true"
                disabled_status, disabled = _post_with_status(
                    base_url,
                    f"/tasks/{task_id}/recovery/observations",
                    {
                        **observation,
                        "observation": {
                            **observation["observation"],
                            "refs": {
                                **observation["observation"]["refs"],
                                "request_id": "request-disabled-component",
                            },
                        },
                        "idempotency_key": "disabled-component-observation",
                    },
                )
                self.assertEqual(disabled_status, 503)
                self.assertEqual(disabled["error"], "recovery_component_disabled")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
                module.reset_recovery_runtime_api()
                module.reset_provider_control_client()
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value


def _get(base_url: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url}{path}", method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    status, body = _post_with_status(base_url, path, payload)
    if status >= 400:
        raise AssertionError(f"unexpected HTTP {status}: {body}")
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
