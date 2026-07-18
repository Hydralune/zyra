from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zyra_workers.subagents.typescript_port import (
    TypeScriptAgentDurablePort,
    _digest,
)


class TypeScriptAgentDurablePortTests(unittest.TestCase):
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

    @staticmethod
    def _effect_request(
        *,
        effect_id: str,
        lease_id: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        unsigned: dict[str, object] = {
            "effectId": effect_id,
            "requestId": "request-1",
            "taskId": "child-1",
            "leaseId": lease_id,
            "expectedRevision": 0,
            "effectKind": "persist",
            "operation": "persist_agent_task_create",
            "payload": payload or {"value": "stable"},
            "idempotencyKey": "idempotency-1",
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
