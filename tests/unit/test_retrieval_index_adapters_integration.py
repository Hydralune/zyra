from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zyra_memory import (
    MemoryFilterQuery,
    MemoryIndexRuntime,
    MemoryIndexWorkerProcessSupervisor,
    MemoryLayer,
    MemoryRecord,
    RetrievalConformanceSuite,
    RetrievalConsumer,
    RetrievalIntegrationError,
    RetrievalIntegrationRuntime,
    SQLiteStore,
)


class RetrievalIndexIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.canonical = SQLiteStore(self.root / "canonical.sqlite3")
        self.canonical.initialize()
        self.canonical.save_memory_records(
            [
                self._record(
                    "failure-memory",
                    "Lease recovery procedure",
                    "worker killed retry lease fence recovery",
                    layer=MemoryLayer.EPISODIC,
                    source_type="failure_event",
                    source_id="failure-1",
                    metadata={"failure_kind": "worker_killed", "session_id": "session-1"},
                ),
                self._record(
                    "skill-memory",
                    "Deploy verification procedure",
                    "deploy smoke test rollback verification",
                    layer=MemoryLayer.SKILL,
                    source_type="skill_invocation",
                    source_id="deploy",
                    metadata={"skill_name": "deploy", "session_id": "session-1"},
                ),
            ]
        )
        self.index = self.root / "derived.sqlite3"
        self.runtime = MemoryIndexRuntime(
            canonical_store=self.canonical,
            index_path=self.index,
            worker_id="integration-inline",
            lease_ttl_seconds=0.5,
            heartbeat_interval_seconds=0.05,
        )
        self.integration = RetrievalIntegrationRuntime(
            self.runtime,
            allow_inline_worker_for_tests=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_admission_filter_comparison_checkpoint_and_conformance(self) -> None:
        first = self.integration.admit_canonical_records("task-1", process_worker=True)
        duplicate = self.integration.admit_canonical_records("task-1", process_worker=True)
        self.assertTrue(first.published)
        self.assertTrue(duplicate.published)
        self.assertEqual(duplicate.disposition, "already_published")

        execution = self.integration.execute(
            MemoryFilterQuery(
                task_id="task-1",
                text="killed retry fence",
                consumer=RetrievalConsumer.FAILURE_RECOVERY,
                failure_kinds=("worker_killed",),
                session_id="session-1",
                maximum_results=10,
                request_id="failure-query",
            )
        )
        self.assertEqual(
            [record.memory_id for record in execution.hydrated.records],
            ["failure-memory"],
        )
        comparison = self.integration.compare(
            MemoryFilterQuery(
                task_id="task-1",
                text="deploy verification",
                consumer=RetrievalConsumer.EVALUATION,
                maximum_results=10,
                request_id="comparison",
            )
        )
        self.assertEqual({arm.mode.value for arm in comparison.arms}, {"lexical_only", "fts_mmr", "polyphonic"})
        checkpoint = self.integration.checkpoint_ref("task-1").to_dict()
        self.assertFalse(checkpoint["contains_index_dump"])
        self.assertFalse(checkpoint["contains_canonical_records"])

        suite = RetrievalConformanceSuite(self.integration)
        report = suite.run(
            task_id="task-1",
            query="deploy verification",
            session_id="session-1",
            skill_name="deploy",
            concurrency=4,
        )
        suite.assert_passed(report)
        self.assertTrue(report.passed)

    def test_productized_process_publishes_and_restart_reads_same_generation(self) -> None:
        supervisor = MemoryIndexWorkerProcessSupervisor(
            canonical_db=self.canonical.path,
            index_db=self.index,
            project_root=Path(__file__).resolve().parents[2],
            lease_ttl_seconds=2.0,
            heartbeat_interval_seconds=0.2,
            timeout_seconds=30.0,
        )
        integration = RetrievalIntegrationRuntime(
            self.runtime,
            worker_supervisor=supervisor,
        )
        admission = integration.admit_canonical_records("task-1", process_worker=True)
        self.assertTrue(admission.published)
        self.assertIsNotNone(admission.worker_receipt)
        self.assertTrue(admission.worker_receipt.ok)
        self.assertGreater(admission.worker_receipt.pid, 0)

        restarted = RetrievalIntegrationRuntime(
            MemoryIndexRuntime(
                canonical_store=self.canonical,
                index_path=self.index,
                worker_id="after-restart",
            ),
            worker_supervisor=supervisor,
        )
        result = restarted.execute(
            MemoryFilterQuery(
                task_id="task-1",
                text="rollback verification",
                consumer=RetrievalConsumer.CODE_WORKER_CONTEXT,
            )
        )
        self.assertEqual([record.memory_id for record in result.hydrated.records], ["skill-memory"])

    def test_disabling_worker_stalls_new_revision_without_inline_fallback(self) -> None:
        self.integration.admit_canonical_records("task-1", process_worker=True)
        self.canonical.save_memory_records(
            [
                *self.canonical.task_memory_records("task-1"),
                self._record(
                    "new-memory",
                    "New procedure",
                    "novel recovery token",
                    layer=MemoryLayer.SEMANTIC,
                    source_type="procedure",
                    source_id="procedure-2",
                ),
            ]
        )
        disabled = RetrievalIntegrationRuntime(
            self.runtime,
            worker_enabled=False,
            allow_inline_worker_for_tests=True,
        )
        with self.assertRaises(RetrievalIntegrationError) as captured:
            disabled.admit_canonical_records("task-1", process_worker=True)
        self.assertEqual(captured.exception.code, "index_worker_disabled")
        probe = disabled.disable_probes("task-1", "novel recovery")
        self.assertTrue(probe["worker_disabled"]["queue_stalled"])
        self.assertFalse(probe["worker_disabled"]["inline_rebuild"])

    @staticmethod
    def _record(
        memory_id: str,
        summary: str,
        text: str,
        *,
        layer: MemoryLayer,
        source_type: str,
        source_id: str,
        metadata: dict | None = None,
    ) -> MemoryRecord:
        return MemoryRecord(
            memory_id=memory_id,
            run_id="run-1",
            task_id="task-1",
            layer=layer,
            source_type=source_type,
            source_id=source_id,
            summary=summary,
            content={"text": text},
            keywords=text.split(),
            score=0.9,
            metadata=metadata or {},
        )


if __name__ == "__main__":
    unittest.main()
