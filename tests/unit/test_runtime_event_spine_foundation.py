from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
for package_path in (ROOT / "packages" / "core", ROOT / "packages" / "runtime"):
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_runtime.runtime_events import (  # noqa: E402
    RuntimeEventCustodyAudit,
    RuntimeEventQuery,
    RuntimeEventSpineBridge,
)


class RuntimeEventSpineFoundationTests(unittest.TestCase):
    def test_python_port_exercises_query_projection_delivery_and_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bridge = RuntimeEventSpineBridge.create(
                database_path=root / "events.sqlite3",
                artifact_root=root / "artifacts",
                workspace_root=ROOT,
            )
            try:
                receipt = bridge.append_draft(
                    {
                        "event_id": "legacy-root",
                        "run_id": "run-1",
                        "task_id": "task-1",
                        "node_id": "root",
                        "event_type": "task_created",
                        "created_at": "2026-07-19T00:00:00.000Z",
                        "payload": {"goal": "prove the RPC contract"},
                    }
                )
                self.assertEqual(receipt.event.global_sequence, 1)

                page = bridge.query(
                    RuntimeEventQuery(after_sequence=0, task_id="task-1")
                )
                self.assertEqual([event.event_id for event in page.events], ["legacy-root"])
                self.assertEqual(page.next_sequence, 1)
                self.assertEqual(page.high_watermark, 1)
                self.assertEqual(bridge.get_event("legacy-root").event_id, "legacy-root")

                aggregate_id = "run:run-1:task:task-1"
                projection = bridge.get_projection("session", aggregate_id)
                self.assertIsNotNone(projection)
                self.assertEqual(projection.key, aggregate_id)
                self.assertEqual(projection.state["lastEventId"], "legacy-root")

                health = bridge.health()
                self.assertTrue(health.ok)
                self.assertEqual(health.high_watermark, 1)
                leases = bridge.lease(
                    consumer_id="builtin-resource-scheduler",
                    limit=1,
                )
                self.assertEqual(len(leases), 1)
                self.assertEqual(leases[0].event.event_id, "legacy-root")
                self.assertTrue(
                    bridge.acknowledge(
                        delivery_id=leases[0].delivery_id,
                        lease_token=leases[0].lease_token,
                    )
                )
                self.assertTrue(bridge.replay("legacy-root").duplicate)

                custody = RuntimeEventCustodyAudit(
                    workspace_root=root,
                    database_path=root / "events.sqlite3",
                ).inspect()
                self.assertTrue(custody.passed, custody.to_jsonable())
            finally:
                bridge.close()

    def test_global_cursor_does_not_drop_sequence_zero_from_multiple_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bridge = RuntimeEventSpineBridge.create(
                database_path=root / "events.sqlite3",
                artifact_root=root / "artifacts",
                workspace_root=ROOT,
            )
            try:
                for suffix in ("a", "b"):
                    bridge.append_draft(
                        {
                            "event_id": f"event-{suffix}",
                            "run_id": f"run-{suffix}",
                            "task_id": f"task-{suffix}",
                            "node_id": "root",
                            "event_type": "task_created",
                            "created_at": f"2026-07-19T00:00:0{1 if suffix == 'a' else 2}.000Z",
                            "payload": {"goal": suffix},
                        }
                    )
                page = bridge.query(RuntimeEventQuery(after_sequence=0, limit=10))
                self.assertEqual(
                    [(event.aggregate_sequence, event.global_sequence) for event in page.events],
                    [(0, 1), (0, 2)],
                )
            finally:
                bridge.close()


if __name__ == "__main__":
    unittest.main()
