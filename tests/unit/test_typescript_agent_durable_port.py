from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zyra_workers.subagents.typescript_port import TypeScriptAgentDurablePort


class TypeScriptAgentDurablePortTests(unittest.TestCase):
    def test_revisioned_lifecycle_persists_without_python_query_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            events = []
            port = TypeScriptAgentDurablePort(
                root / "state",
                workspace_root=workspace,
                event_sink=events.append,
            )
            created = port.handle(
                {
                    "action": "create",
                    "task_id": "child-1",
                    "idempotency_key": "spawn-1",
                    "record": self._record(workspace),
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(created["accepted"], created["error"])
            self.assertEqual(created["status"], "ready")
            self.assertEqual(created["revision"], 2)
            self.assertFalse(created["python_logical_fallback"])

            dispatched = port.handle(
                {
                    "action": "dispatch",
                    "task_id": "child-1",
                    "expected_revision": 2,
                    "execution_ref": "typescript-query-engine:child-1:1",
                    "dispatch_request": {
                        "definition_digest": "definition-1",
                        "permission_ceiling_digest": "permission-1",
                    },
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertEqual(dispatched["status"], "dispatched")
            running = port.handle(
                {
                    "action": "running",
                    "task_id": "child-1",
                    "expected_revision": 3,
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertEqual(running["status"], "running")
            completed = port.handle(
                {
                    "action": "complete",
                    "task_id": "child-1",
                    "expected_revision": 4,
                    "result_digest": "result-1",
                    "result": {
                        "ok": True,
                        "summary": "complete",
                        "usage": {"turns": 1, "tool_calls": 1},
                    },
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(completed["accepted"], completed["error"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["revision"], 5)
            self.assertEqual(len(events), 4)

            restored = TypeScriptAgentDurablePort(
                root / "state",
                workspace_root=workspace,
            ).snapshot(parent_task_id="parent-1")
            self.assertEqual(restored["terminal_task_ids"], ["child-1"])
            self.assertEqual(
                restored["tasks"][0]["metadata"]["result_commit_owner"],
                "typescript-agent-runtime",
            )

    def test_stale_control_and_workspace_escape_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            port = TypeScriptAgentDurablePort(root / "state", workspace_root=workspace)
            record = self._record(workspace)
            record["isolation_request"]["workspace_root"] = str(root.parent)
            escaped = port.handle(
                {
                    "action": "create",
                    "task_id": "child-1",
                    "idempotency_key": "spawn-1",
                    "record": record,
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertFalse(escaped["accepted"])
            self.assertIn("escapes", escaped["error"])

            created = port.handle(
                {
                    "action": "create",
                    "task_id": "child-2",
                    "idempotency_key": "spawn-2",
                    "record": self._record(workspace, task_id="child-2"),
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(created["accepted"], created["error"])
            wrong_session = port.handle(
                {"action": "load", "task_id": "child-2"},
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="another-session",
            )
            self.assertFalse(wrong_session["accepted"])
            self.assertIn("session authority", wrong_session["error"])
            stale = port.handle(
                {
                    "action": "cancel",
                    "task_id": "child-2",
                    "expected_revision": 1,
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertFalse(stale["accepted"])
            self.assertIn("revision", stale["error"])

            dispatched = port.handle(
                {
                    "action": "dispatch",
                    "task_id": "child-2",
                    "expected_revision": created["revision"],
                    "execution_ref": "typescript-query-engine:child-2:1",
                    "dispatch_request": {},
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(dispatched["accepted"], dispatched["error"])
            running = port.handle(
                {
                    "action": "running",
                    "task_id": "child-2",
                    "expected_revision": dispatched["revision"],
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(running["accepted"], running["error"])
            competing_port = TypeScriptAgentDurablePort(root / "state", workspace_root=workspace)
            cancelled = competing_port.handle(
                {
                    "action": "cancel",
                    "task_id": "child-2",
                    "expected_revision": running["revision"],
                    "reason": "concurrent parent cancellation",
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(cancelled["accepted"], cancelled["error"])
            late = port.handle(
                {
                    "action": "complete",
                    "task_id": "child-2",
                    "expected_revision": running["revision"],
                    "result_digest": "late-result",
                    "result": {"ok": True, "usage": {}},
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertFalse(late["accepted"])
            self.assertEqual(competing_port.get_task("child-2").status.value, "cancelled")

    @staticmethod
    def _record(workspace: Path, *, task_id: str = "child-1") -> dict:
        return {
            "run_id": "run-1",
            "task_id": task_id,
            "parent_task_id": "parent-1",
            "parent_session_id": "session-1",
            "agent_type": "general-purpose",
            "definition_id": "definition-1",
            "status": "created",
            "context_snapshot": {
                "snapshot_id": f"context-{task_id}",
                "parent_session_id": "session-1",
                "parent_task_id": "parent-1",
                "parent_worker_request_id": "worker-1",
                "mode": "isolated",
                "ancestry": [],
                "depth": 1,
            },
            "tool_scope": {
                "parent_tools": ["file_read"],
                "child_tools": ["file_read"],
                "denied_tools": [],
                "required_tools": [],
                "dynamic_tool_identities": {},
                "digest": "scope-1",
            },
            "permission": {
                "parent_mode": "default",
                "child_mode": "default",
                "monotonic": True,
                "digest": "permission-1",
            },
            "budget": {
                "max_turns": 4,
                "max_tool_calls": 8,
                "max_input_tokens": 1000,
                "max_output_tokens": 1000,
                "max_result_chars": 10000,
                "max_wall_time_ms": 10000,
                "max_children": 2,
                "max_depth": 2,
            },
            "isolation_request": {
                "run_id": "run-1",
                "task_id": task_id,
                "parent_task_id": "parent-1",
                "kind": "workspace",
                "workspace_root": str(workspace),
                "requested_cwd": "",
                "writable_paths": [],
                "read_only_paths": [],
                "network_allowed": False,
                "cleanup_required": True,
                "request_id": f"isolation-{task_id}",
            },
            "execution_mode": "foreground",
            "prompt_digest": "prompt-1",
            "revision": 0,
            "metadata": {
                "canonical_logical_owner": "typescript",
                "python_logical_fallback": False,
            },
        }


if __name__ == "__main__":
    unittest.main()
