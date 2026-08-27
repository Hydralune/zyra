from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path

from zyra_workers.subagents.typescript_port import (
    TypeScriptAgentDurablePort,
    _digest,
    typescript_agent_authority_state_root,
)


class TypeScriptAgentDurablePortTests(unittest.TestCase):
    def test_authority_state_root_is_stable_per_session_and_isolates_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "state"
            initial = typescript_agent_authority_state_root(
                root,
                run_id="run-1",
                parent_task_id="task-1",
                parent_session_id="session-1",
            )
            replay = typescript_agent_authority_state_root(
                root,
                run_id="run-1",
                parent_task_id="task-1",
                parent_session_id="session-1",
            )
            recovery = typescript_agent_authority_state_root(
                root,
                run_id="run-1",
                parent_task_id="task-1",
                parent_session_id="session-1:recovery:1",
            )

            self.assertEqual(initial, replay)
            self.assertNotEqual(initial, recovery)
            self.assertEqual(initial.parent, root.resolve() / "authorities")

    def test_effect_receipt_reconciles_after_restart_without_redispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            state = root / "state"
            first_port = TypeScriptAgentDurablePort(
                state,
                workspace_root=workspace,
            )
            first = first_port.handle(
                {
                    "action": "e03.effect",
                    "task_id": "child-1",
                    "effect_request": self._effect_request(
                        effect_id="effect-before-crash",
                        lease_id="lease-before-crash",
                    ),
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(first["accepted"], first["error"])
            first_receipt = first["effect_receipt"]
            self.assertFalse(first_receipt["replayed"])

            restarted = TypeScriptAgentDurablePort(
                state,
                workspace_root=workspace,
            )
            replay = restarted.handle(
                {
                    "action": "e03.effect",
                    "task_id": "child-1",
                    "effect_request": self._effect_request(
                        effect_id="effect-after-restart",
                        lease_id="lease-after-restart",
                    ),
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(replay["accepted"], replay["error"])
            replay_receipt = replay["effect_receipt"]
            self.assertTrue(replay_receipt["replayed"])
            self.assertEqual(replay_receipt["receiptId"], first_receipt["receiptId"])
            self.assertEqual(replay_receipt["completedAt"], first_receipt["completedAt"])
            self.assertEqual(replay_receipt["effectId"], "effect-after-restart")
            self.assertEqual(
                replay_receipt["requestDigest"],
                first_receipt["requestDigest"],
            )

            conflict = restarted.handle(
                {
                    "action": "e03.effect",
                    "task_id": "child-1",
                    "effect_request": self._effect_request(
                        effect_id="effect-conflict",
                        lease_id="lease-conflict",
                        payload={"value": "changed"},
                    ),
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertFalse(conflict["accepted"])
            self.assertIn("semantic content conflict", conflict["error"])

    def test_cas_persists_only_typescript_supplied_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            port = TypeScriptAgentDurablePort(root / "state", workspace_root=workspace)
            snapshot = self._empty_snapshot(revision=1)
            committed = port.handle(
                {
                    "action": "e03.cas",
                    "task_id": "parent-1",
                    "expected_registry_revision": 0,
                    "registry_snapshot": snapshot,
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(committed["accepted"], committed["error"])
            self.assertEqual(committed["revision"], 1)
            self.assertEqual(
                committed["canonical_logical_owner"],
                "typescript.E03AgentControlCoordinator",
            )
            self.assertFalse(committed["python_logical_fallback"])

            stale = port.handle(
                {
                    "action": "e03.cas",
                    "task_id": "parent-1",
                    "expected_registry_revision": 0,
                    "registry_snapshot": self._empty_snapshot(revision=1),
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(stale["accepted"])
            self.assertTrue(stale["replayed"])

            restored = port.handle(
                {"action": "e03.restore", "task_id": "parent-1"},
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(restored["accepted"], restored["error"])
            self.assertEqual(restored["snapshot"], snapshot)

    def test_python_port_rejects_legacy_logical_control_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            port = TypeScriptAgentDurablePort(root / "state", workspace_root=workspace)
            for action in ("create", "dispatch", "running", "complete", "cancel"):
                result = port.handle(
                    {"action": action, "task_id": "child-1"},
                    run_id="run-1",
                    parent_task_id="parent-1",
                    parent_session_id="session-1",
                )
                self.assertFalse(result["accepted"], action)
                self.assertIn("unsupported E03 physical-port action", result["error"])
                self.assertFalse(result["python_logical_fallback"])

    def test_worktree_physical_receipt_distinguishes_create_and_safe_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
            (workspace / "tracked.txt").write_text("e04\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Zyra E04",
                    "-c",
                    "user.email=e04@zyra.invalid",
                    "commit",
                    "-m",
                    "e04 worktree fixture",
                ],
                cwd=workspace,
                check=True,
                capture_output=True,
            )
            port = TypeScriptAgentDurablePort(root / "state", workspace_root=workspace)
            payload = {
                "mode": "worktree",
                "workspaceRoot": str(workspace),
                "baseRevision": "HEAD",
                "taskId": "e04-worktree-child",
                "requestId": "e04-worktree-request",
                "branchName": "zyra/e04-worktree-child",
                "allowDirtyBaseline": False,
                "allowNestedRepository": False,
                "reuseExisting": True,
                "createIfMissing": True,
            }
            created = port.handle(
                {
                    "action": "e03.effect",
                    "task_id": "e04-worktree-child",
                    "effect_request": self._effect_request(
                        effect_id="e04-worktree-create",
                        lease_id="e04-worktree-lease",
                        request_id="e04-worktree-create-request",
                        task_id="e04-worktree-child",
                        idempotency_key="e04-worktree-create",
                        effect_kind="workspace",
                        operation="prepare_workspace_isolation",
                        payload=payload,
                    ),
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(created["accepted"], created["error"])
            created_result = created["effect_receipt"]["result"]
            self.assertEqual(created_result["workspace_disposition"], "created")
            self.assertEqual(created_result["backend"], "git-worktree")
            self.assertEqual(created_result["worktree_head"], created_result["resulting_revision"])

            reused = port.handle(
                {
                    "action": "e03.effect",
                    "task_id": "e04-worktree-child",
                    "effect_request": self._effect_request(
                        effect_id="e04-worktree-reuse",
                        lease_id="e04-worktree-lease",
                        request_id="e04-worktree-reuse-request",
                        task_id="e04-worktree-child",
                        idempotency_key="e04-worktree-reuse",
                        effect_kind="workspace",
                        operation="prepare_workspace_isolation",
                        payload=payload,
                    ),
                },
                run_id="run-1",
                parent_task_id="parent-1",
                parent_session_id="session-1",
            )
            self.assertTrue(reused["accepted"], reused["error"])
            reused_result = reused["effect_receipt"]["result"]
            self.assertEqual(reused_result["workspace_disposition"], "reused")
            self.assertEqual(reused_result["workspace_path"], created_result["workspace_path"])
            self.assertEqual(reused_result["worktree_head"], created_result["worktree_head"])

    @staticmethod
    def _effect_request(
        *,
        effect_id: str,
        lease_id: str,
        request_id: str = "request-1",
        task_id: str = "child-1",
        idempotency_key: str = "idempotency-1",
        effect_kind: str = "persist",
        operation: str = "persist_agent_task_create",
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        unsigned: dict[str, object] = {
            "effectId": effect_id,
            "requestId": request_id,
            "taskId": task_id,
            "leaseId": lease_id,
            "expectedRevision": 0,
            "effectKind": effect_kind,
            "operation": operation,
            "payload": payload or {"value": "stable"},
            "idempotencyKey": idempotency_key,
            "preparedAt": "2026-07-18T00:00:00.000Z",
        }
        return {**unsigned, "digest": _digest(unsigned)}

    @staticmethod
    def _empty_snapshot(*, revision: int) -> dict[str, object]:
        unsigned: dict[str, object] = {
            "schemaVersion": "3.0",
            "revision": revision,
            "tasks": {},
            "requests": {},
            "effects": {},
            "definitions": {},
            "writerLeases": {},
            "createdAt": "2026-07-18T00:00:00.000Z",
            "updatedAt": "2026-07-18T00:00:00.000Z",
        }
        return {**unsigned, "checksum": _digest(unsigned)}


if __name__ == "__main__":
    unittest.main()
