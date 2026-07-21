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
from unittest.mock import patch

from zyra_core import PlanNodeStatus


def _fresh_api_module() -> Any:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


class MemoryCuratorApiMainPathTests(unittest.TestCase):
    def test_workspace_reset_rebinds_curator_to_live_event_spine(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
                "ZYRA_MEMORY_INDEX_PATH": str(root / "memory-index.sqlite3"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_WORKSPACE_ROOT": str(root / "workspace"),
            }
            previous = {name: os.environ.get(name) for name in environment}
            os.environ.update(environment)
            module = _fresh_api_module()
            try:
                first = module.get_memory_curator_runtime(module.get_store())
                first_bridge = first.worker.trace_ingress.bridge
                module.reset_workspace_manager()

                second = module.get_memory_curator_runtime(module.get_store())
                second_bridge = second.worker.trace_ingress.bridge
                self.assertIsNot(second, first)
                self.assertIsNot(second_bridge, first_bridge)
                self.assertIs(second_bridge, module.get_runtime_event_spine_bridge())
            finally:
                module.reset_workspace_manager()
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_real_api_trace_commits_memory_event_and_retrieval_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "ZYRA_SQLITE_PATH": str(root / "api.sqlite3"),
                "ZYRA_MEMORY_INDEX_PATH": str(root / "memory-index.sqlite3"),
                "ZYRA_EVENT_LOG": str(root / "events.jsonl"),
                "ZYRA_ARTIFACT_ROOT": str(root / "artifacts"),
                "ZYRA_WORKSPACE_ROOT": str(root / "workspace"),
            }
            previous = {name: os.environ.get(name) for name in environment}
            os.environ.update(environment)
            module = _fresh_api_module()
            server = ThreadingHTTPServer(("127.0.0.1", 0), module.ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                created = _post(
                    base_url,
                    "/tasks",
                    {
                        "goal": "Preserve recovery lease fencing evidence.",
                        "auto_run": False,
                    },
                )
                task_id = created["task"]["task_id"]
                state = module.get_store().load_task(task_id)
                artifact_store = module.LocalArtifactStore(module.artifact_root_path())
                artifact = artifact_store.write_text(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    content="Lease takeover and exact-resume verification passed.",
                    title="Curator API evidence",
                    kind=module.ArtifactKind.REPORT,
                    extension=".md",
                    producer_node_id=state.root_node_id,
                )
                state.artifacts.append(artifact)
                store = module.get_store()
                store.save_checkpoint(state)
                store.append_events(
                    [
                        module.EventRecord(
                            run_id=state.run_id,
                            task_id=state.task_id,
                            node_id=state.root_node_id,
                            event_type=module.EventType.REQUIREMENT_CHANGE,
                            payload={
                                "requirement_id": "REQ-CURATOR-API",
                                "summary": "Retain verified lease takeover behavior.",
                                "source": "user",
                            },
                        ),
                        module.EventRecord(
                            run_id=state.run_id,
                            task_id=state.task_id,
                            node_id=state.root_node_id,
                            event_type=module.EventType.COMMAND_SUCCEEDED,
                            payload={
                                "tool_name": "pytest",
                                "tool_call_id": "api-tool-1",
                                "command": "python -m pytest tests/recovery -q",
                                "status": "succeeded",
                                "output": "8 passed",
                                "artifact_ids": [artifact.artifact_id],
                            },
                        ),
                    ]
                )

                curated = _post(
                    base_url,
                    f"/tasks/{task_id}/memory/curator",
                    {
                        "operation": "schedule_manual",
                        "requested_by": "integration-test",
                        "allow_model_assist": True,
                        "process_immediately": True,
                    },
                )
                self.assertEqual(curated["status"], "succeeded")
                self.assertGreater(curated["result"]["committed_count"], 0)
                self.assertEqual(curated["result"]["model_status"], "unavailable")
                self.assertEqual(curated["result"]["outbox_pending"], 0)
                self.assertTrue(
                    curated["result"]["diagnostics"]["generation"]["diagnostics"][
                        "supplementary_consolidation"
                    ]["typescript_active"]
                )

                status = _get(base_url, f"/tasks/{task_id}/memory/curator")
                self.assertTrue(status["candidates"])
                self.assertTrue(status["jobs"])
                self.assertFalse(status["model_can_write"])
                self.assertTrue(status["candidate_store_is_separate"])
                self.assertEqual(status["canonical_memory_owner"], "SQLiteStore.memory_records")
                self.assertEqual(
                    {item["state"] for item in status["outbox"]},
                    {"delivered"},
                )

                memory = _get(base_url, f"/tasks/{task_id}/memory?q=lease%20takeover")
                self.assertTrue(memory["search_results"])
                self.assertEqual(memory["retrieval_runtime"], "MemoryIndexRuntime")
                self.assertFalse(memory["legacy_substring_fallback"])
                event_types = [item["event_type"] for item in store.task_events(task_id)]
                self.assertIn("memory_curator_scheduled", event_types)
                self.assertIn("memory_curator_committed", event_types)

                recovered = _post(
                    base_url,
                    f"/tasks/{task_id}/memory/curator/recover",
                    {"operation": "recover"},
                )
                self.assertEqual(recovered["status"], "recovered")
                self.assertEqual(recovered["data"]["outbox"]["pending_count"], 0)

                scheduled_only = _post(
                    base_url,
                    f"/tasks/{task_id}/memory/curator",
                    {
                        "operation": "schedule_manual",
                        "requested_by": "integration-test-background",
                        "idempotency_key": "integration-background-schedule",
                    },
                )
                self.assertEqual(scheduled_only["status"], "scheduled")
                self.assertIsNone(scheduled_only["result"])
                self.assertEqual(scheduled_only["scheduled"]["job"]["state"], "queued")

                state.status = PlanNodeStatus.COMPLETED
                store.save_checkpoint(state)
                with patch.object(
                    module,
                    "get_memory_curator_runtime",
                    side_effect=RuntimeError("curator unavailable"),
                ):
                    isolated = module.curate_terminal_task(store, state)
                self.assertEqual(isolated["status"], "degraded")
                self.assertTrue(isolated["data"]["task_lifecycle_preserved"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                module.reset_memory_curator_runtime()
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
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
