from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from zyra_code_index import (
    BoundWorkspaceSource,
    CodeIndexBuildQueue,
    CodeIndexConformanceSuite,
    CodeIndexConsumer,
    CodeIndexIntegrationRuntime,
    CodeIndexJobOperation,
    CodeIndexJobState,
    CodeIndexPublicationFenced,
    CodeIndexQuery,
    CodeIndexRuntime,
)


class CodeIndexIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "recovery.py").write_text(
            "def recover_lease(value):\n    return f'lease recovery {value}'\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_recovery.py").write_text(
            "from src.recovery import recover_lease\n\ndef test_recovery():\n    assert recover_lease('x')\n",
            encoding="utf-8",
        )
        self.source = BoundWorkspaceSource.for_test(self.root, binding_revision=1)
        self.runtime = CodeIndexRuntime(
            self.source,
            index_path=self.root / "state" / "code.sqlite3",
        )
        self.transactions: dict[str, object] = {}

        class Resolver:
            def require_transaction(inner_self, transaction_id: str):
                return self.transactions[transaction_id]

            def list_transactions(inner_self, workspace_id: str):
                return tuple(
                    item
                    for item in self.transactions.values()
                    if item.workspace_id == workspace_id
                )

        self.integration = CodeIndexIntegrationRuntime(
            self.runtime,
            transaction_resolver=Resolver(),
            allow_inline_worker_for_tests=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _query(self, text: str, request_id: str = "query") -> CodeIndexQuery:
        return CodeIndexQuery(
            task_id="test-task",
            text=text,
            consumer=CodeIndexConsumer.CODE_WORKER_CONTEXT,
            worker_request_id=request_id,
            changed_paths=("src/recovery.py",),
            request_id=request_id,
        )

    def test_initial_patch_selection_test_routing_and_delivery(self) -> None:
        initial = self.integration.admit_initial(process=True)
        duplicate = self.integration.admit_initial(process=True)
        self.assertTrue(initial.published)
        self.assertTrue(duplicate.published)
        self.assertEqual(initial.job.job_id, duplicate.job.job_id)

        selection = self.integration.select(self._query("recover lease"))
        self.assertIn("src/recovery.py", selection.selected_files)
        self.assertIn("tests/test_recovery.py", selection.selected_tests)
        claim = self.integration.journal.claim_delivery(selection, message_id="message-1")
        self.assertEqual(claim["state"], "claimed")
        committed = self.integration.journal.finish_delivery(
            selection.request.worker_request_id,
            committed=True,
            terminal_event_ids=("event-terminal",),
            reason="provider_runtime_completed",
        )
        self.assertEqual(committed["state"], "committed")

        target = self.root / "src" / "recovery.py"
        target.write_text(
            target.read_text(encoding="utf-8").replace("lease recovery", "fenced retry recovery"),
            encoding="utf-8",
        )
        transaction = SimpleNamespace(
            transaction_id="transaction-1",
            workspace_id="test-workspace",
            phase="committed",
            path_results=(
                SimpleNamespace(
                    logical_path="src/recovery.py",
                    operation="update",
                    after_hash="after-hash",
                ),
            ),
        )
        self.transactions[transaction.transaction_id] = transaction
        patch = self.integration.admit_patch_transaction(transaction.transaction_id, process=True)
        self.assertTrue(patch.published)
        self.assertGreater(patch.job.generation, initial.job.generation)
        changed = self.integration.select(self._query("fenced retry", "query-after-patch"))
        self.assertIn("src/recovery.py", changed.selected_files)

    def test_new_generation_fences_an_old_candidate_before_atomic_publish(self) -> None:
        queue = CodeIndexBuildQueue(self.runtime.store)
        old = queue.enqueue(
            self.source.identity,
            operation=CodeIndexJobOperation.REBUILD,
            idempotency_key="old-generation",
        )
        lease = queue.claim(
            worker_id="worker-old",
            workspace_id=self.source.identity.workspace_id,
            lease_ttl_seconds=10,
        )
        self.assertIsNotNone(lease)
        queue.start_build(lease)
        self.runtime.prepare_candidate(generation=old.generation)

        newer = queue.enqueue(
            self.source.identity,
            operation=CodeIndexJobOperation.PATCH,
            transaction_id="newer-patch",
            changed_paths=("src/recovery.py",),
            idempotency_key="new-generation",
        )
        self.assertGreater(newer.generation, old.generation)
        with self.assertRaises(CodeIndexPublicationFenced):
            queue.begin_publish(lease)
        self.assertEqual(self.runtime.store.workspace_state("test-workspace"), {})

    def test_expired_lease_is_staled_and_requeued_with_a_new_generation(self) -> None:
        queue = CodeIndexBuildQueue(self.runtime.store)
        admitted = queue.enqueue(
            self.source.identity,
            operation=CodeIndexJobOperation.REBUILD,
            idempotency_key="expiry-job",
        )
        lease = queue.claim(
            worker_id="doomed-worker",
            workspace_id=self.source.identity.workspace_id,
            lease_ttl_seconds=0.1,
        )
        self.assertIsNotNone(lease)
        time.sleep(0.15)
        sweep = queue.sweep_expired()
        self.assertEqual(sweep.count, 1)
        stale = queue.require(admitted.job_id)
        self.assertEqual(stale.state, CodeIndexJobState.STALE)
        replacement = queue.require(sweep.requeued_job_ids[0])
        self.assertGreater(replacement.generation, admitted.generation)
        outcome = self.integration.worker.process_one(workspace_id="test-workspace")
        self.assertEqual(outcome.status, "ready")

    def test_conformance_and_destructive_rebuild_preserve_selection(self) -> None:
        self.integration.admit_initial(process=True)
        suite = CodeIndexConformanceSuite(self.integration)
        report = suite.run(query="recover lease", concurrency=4, destructive_rebuild=True)
        suite.assert_passed(report)
        self.assertTrue(report.passed)
        reference = self.integration.select(self._query("recover lease", "recovery-ref")).recovery_reference()
        self.assertFalse(reference["index_dump_embedded"])
        self.assertFalse(reference["workspace_content_embedded"])


if __name__ == "__main__":
    unittest.main()
