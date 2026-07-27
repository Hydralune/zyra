from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zyra_code_index import (
    BoundWorkspaceSource,
    CodeIndexIntegrationRuntime,
    CodeIndexRuntime,
)
from zyra_core import AgentMessage, AgentRole, MessageIntent
from zyra_memory import (
    MemoryIndexRuntime,
    MemoryLayer,
    MemoryRecord,
    RetrievalIntegrationRuntime,
    SQLiteStore,
)
from zyra_runtime import WorkerRequest
from zyra_workers import (
    CodeWorkerRuntime,
    WorkerRetrievalContextRuntime,
    WorkerRetrievalRecoveryReference,
)


class CapturingFailingQueryEngine:
    configs: list[object] = []
    calls: list[dict] = []

    def __init__(self, execution_context, config) -> None:
        del execution_context
        type(self).configs.append(config)

    def run(self, **kwargs):
        type(self).calls.append(dict(kwargs))
        raise RuntimeError("stop after observing injected retrieval context")


class PersistenceFailingCodeWorkerRuntime(CodeWorkerRuntime):
    def _persist_runtime_state(self, session_id, payload):
        del session_id, payload
        raise OSError("injected runtime-state persistence failure")


class CodeWorkerRetrievalContextMainPathTests(unittest.TestCase):
    def setUp(self) -> None:
        CapturingFailingQueryEngine.configs.clear()
        CapturingFailingQueryEngine.calls.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "lease.py").write_text(
            "def renew_lease(token):\n    return f'fenced lease {token}'\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_lease.py").write_text(
            "from src.lease import renew_lease\n\ndef test_lease():\n    assert renew_lease('x')\n",
            encoding="utf-8",
        )
        self.canonical = SQLiteStore(self.root / "canonical.sqlite3")
        self.canonical.initialize()
        self.canonical.save_memory_records(
            [
                MemoryRecord(
                    memory_id="lease-procedure",
                    run_id="run-1",
                    task_id="test-task",
                    layer=MemoryLayer.SKILL,
                    source_type="procedure",
                    source_id="lease-recovery",
                    summary="Lease recovery procedure",
                    content={"text": "renew fenced lease and execute selected regression test"},
                    keywords=["renew", "fenced", "lease", "regression"],
                    score=0.95,
                )
            ]
        )
        self.memory = RetrievalIntegrationRuntime(
            MemoryIndexRuntime(
                canonical_store=self.canonical,
                index_path=self.root / "memory-index.sqlite3",
                worker_id="context-test",
            ),
            allow_inline_worker_for_tests=True,
        )
        self.memory.admit_canonical_records("test-task", process_worker=True)
        self.code_runtime = CodeIndexRuntime(
            BoundWorkspaceSource.for_test(self.root),
            index_path=self.root / "code-index.sqlite3",
        )
        self.code = CodeIndexIntegrationRuntime(
            self.code_runtime,
            allow_inline_worker_for_tests=True,
        )
        self.code.admit_initial(process=True)
        self.retrieval = WorkerRetrievalContextRuntime(
            memory=self.memory,
            code=self.code,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_retrieval_changes_queryengine_messages_files_tests_and_delivery_state(self) -> None:
        request = WorkerRequest(
            run_id="run-1",
            task_id="test-task",
            worker_name="CodeWorkerRuntime",
            request_id="worker-request-1",
            messages=[
                AgentMessage(
                    run_id="run-1",
                    task_id="test-task",
                    sender_role=AgentRole.USER,
                    receiver_role=AgentRole.WORKER,
                    intent=MessageIntent.REQUEST,
                    content="Fix fenced lease renewal and select the regression test.",
                )
            ],
            constraints={"session_id": "session-1", "query_turns": []},
        )
        run = CodeWorkerRuntime(
            project_root=Path(__file__).resolve().parents[2],
            workspace_root=self.root,
            artifact_root=self.root / "artifacts",
            query_engine_factory=CapturingFailingQueryEngine,
            retrieval_context_runtime=self.retrieval,
        ).run(request)

        self.assertFalse(run.worker_result.ok)
        self.assertEqual(run.worker_result.error, "typescript_runtime_host_failed")
        config = CapturingFailingQueryEngine.configs[0]
        injected = tuple(config.preprocessed_messages)
        self.assertGreaterEqual(len(injected), 3)
        self.assertEqual(injected[0].sender_role, AgentRole.USER)
        scopes = {message.metadata.get("retrieval_scope") for message in injected[1:]}
        self.assertIn("task_memory", scopes)
        self.assertIn("task_workspace", scopes)
        self.assertIn("src/lease.py", config.runtime_constraints["code_index_selected_files"])
        self.assertIn("tests/test_lease.py", config.runtime_constraints["code_index_selected_tests"])
        reference = config.runtime_constraints["retrieval_recovery_reference"]
        self.assertFalse(reference["contains_index_dump"])
        self.assertFalse(reference["contains_workspace_content"])

        memory_delivery = self.memory.store.delivery(request.request_id)
        self.assertEqual(memory_delivery.state.value, "released")
        with self.code_runtime.store.connection() as connection:
            code_delivery = connection.execute(
                "SELECT state, reason FROM code_index_context_deliveries WHERE worker_request_id = ?",
                (request.request_id,),
            ).fetchone()
        self.assertEqual(code_delivery["state"], "released")
        self.assertIn("typescript_runtime_host_failed", code_delivery["reason"])

        retried = self.retrieval.prepare(request, session_id="session-1")
        self.assertEqual(
            self.memory.store.delivery(request.request_id).state.value,
            "claimed",
        )
        with self.code_runtime.store.connection() as connection:
            reclaimed = connection.execute(
                "SELECT state FROM code_index_context_deliveries WHERE worker_request_id = ?",
                (request.request_id,),
            ).fetchone()
        self.assertEqual(reclaimed["state"], "claimed")
        self.retrieval.finish(
            retried,
            committed=False,
            terminal_event_ids=("retry-terminal",),
            reason="retry_test_cleanup",
        )

    def test_disabling_retrieval_removes_injected_context_and_selection(self) -> None:
        request = WorkerRequest(
            run_id="run-1",
            task_id="test-task",
            worker_name="CodeWorkerRuntime",
            request_id="worker-request-disabled",
            messages=[
                AgentMessage(
                    run_id="run-1",
                    task_id="test-task",
                    sender_role=AgentRole.USER,
                    receiver_role=AgentRole.WORKER,
                    intent=MessageIntent.REQUEST,
                    content="Fix fenced lease renewal.",
                )
            ],
            constraints={
                "session_id": "session-disabled",
                "query_turns": [],
                "disable_retrieval_context": True,
            },
        )
        run = CodeWorkerRuntime(
            project_root=Path(__file__).resolve().parents[2],
            workspace_root=self.root,
            artifact_root=self.root / "disabled-artifacts",
            query_engine_factory=CapturingFailingQueryEngine,
            retrieval_context_runtime=self.retrieval,
        ).run(request)
        self.assertFalse(run.worker_result.ok)
        config = CapturingFailingQueryEngine.configs[-1]
        self.assertEqual(tuple(config.preprocessed_messages), tuple(request.messages))
        self.assertNotIn("code_index_selected_files", config.runtime_constraints)
        self.assertIsNone(self.memory.store.delivery(request.request_id))

    def test_mapping_message_is_normalized_before_retrieval_selection(self) -> None:
        request = WorkerRequest(
            run_id="run-1",
            task_id="test-task",
            worker_name="CodeWorkerRuntime",
            request_id="worker-request-mapping-message",
            messages=[
                {
                    "role": "user",
                    "content": "Inspect fenced lease renewal from browser context.",
                    "metadata": {
                        "browser_context_source_id": "browser-disclosure:test",
                        "external": True,
                    },
                }
            ],
            constraints={"session_id": "session-mapping-message", "query_turns": []},
        )

        context = self.retrieval.prepare(
            request,
            session_id="session-mapping-message",
        )

        self.assertTrue(context.delivery_claimed)
        self.assertIsNotNone(context.memory_execution)
        self.assertIsNotNone(context.code_selection)
        self.assertIn("src/lease.py", context.constraint_delta["code_index_selected_files"])
        self.retrieval.finish(
            context,
            committed=False,
            terminal_event_ids=("mapping-message-cleanup",),
            reason="mapping_message_test_cleanup",
        )

    def test_real_typescript_queryengine_commits_retrieval_delivery(self) -> None:
        request = WorkerRequest(
            run_id="run-1",
            task_id="test-task",
            worker_name="CodeWorkerRuntime",
            request_id="worker-request-real-typescript",
            messages=[
                AgentMessage(
                    run_id="run-1",
                    task_id="test-task",
                    sender_role=AgentRole.USER,
                    receiver_role=AgentRole.WORKER,
                    intent=MessageIntent.REQUEST,
                    content="Inspect fenced lease renewal context without changing files.",
                )
            ],
            constraints={"session_id": "session-real-typescript", "query_turns": []},
        )
        run = CodeWorkerRuntime(
            project_root=Path(__file__).resolve().parents[2],
            workspace_root=self.root,
            artifact_root=self.root / "real-typescript-artifacts",
            retrieval_context_runtime=self.retrieval,
        ).run(request)
        self.assertTrue(run.worker_result.ok, run.worker_result.error)
        self.assertIn("memory_retrieval_query_id", run.worker_result.metadata)
        self.assertIn("code_index_query_id", run.worker_result.metadata)
        self.assertTrue(
            any(event.payload.get("retrieval_context") for event in run.event_records)
        )
        memory_delivery = self.memory.store.delivery(request.request_id)
        self.assertEqual(memory_delivery.state.value, "committed")
        with self.code_runtime.store.connection() as connection:
            code_delivery = connection.execute(
                "SELECT state, terminal_event_ids_json FROM code_index_context_deliveries "
                "WHERE worker_request_id = ?",
                (request.request_id,),
            ).fetchone()
        self.assertEqual(code_delivery["state"], "committed")
        self.assertNotEqual(code_delivery["terminal_event_ids_json"], "[]")

    def test_prepare_compensates_both_journals_when_reference_build_fails(self) -> None:
        request = self._request("worker-request-reference-failure")
        original_to_dict = WorkerRetrievalRecoveryReference.to_dict

        def fail_reference_to_dict(reference):
            del reference
            raise RuntimeError("injected checkpoint reference failure")

        WorkerRetrievalRecoveryReference.to_dict = fail_reference_to_dict
        try:
            with self.assertRaisesRegex(RuntimeError, "injected checkpoint reference failure"):
                self.retrieval.prepare(request, session_id="session-reference-failure")
        finally:
            WorkerRetrievalRecoveryReference.to_dict = original_to_dict

        memory_delivery = self.memory.store.delivery(request.request_id)
        self.assertEqual(memory_delivery.state.value, "released")
        self.assertIn("retrieval_context_prepare_failed", memory_delivery.reason)
        code_delivery = self.code.journal.delivery(request.request_id)
        self.assertEqual(code_delivery["state"], "released")
        self.assertIn("retrieval_context_prepare_failed", code_delivery["reason"])

    def test_finish_attempts_both_journals_and_is_repairable(self) -> None:
        request = self._request("worker-request-finalize-repair")
        context = self.retrieval.prepare(
            request,
            session_id="session-finalize-repair",
        )
        original_complete = self.memory.store.complete_delivery

        def fail_complete(worker_request_id: str, *, terminal_event_ids):
            del worker_request_id, terminal_event_ids
            raise RuntimeError("injected memory finalize failure")

        self.memory.store.complete_delivery = fail_complete
        try:
            with self.assertRaisesRegex(RuntimeError, "memory finalize failure"):
                self.retrieval.finish(
                    context,
                    committed=True,
                    terminal_event_ids=("terminal-finalize-repair",),
                    reason="provider_runtime_completed",
                )
        finally:
            self.memory.store.complete_delivery = original_complete

        self.assertEqual(
            self.memory.store.delivery(request.request_id).state.value,
            "claimed",
        )
        self.assertEqual(
            self.code.journal.delivery(request.request_id)["state"],
            "committed",
        )
        self.retrieval.finish(
            context,
            committed=True,
            terminal_event_ids=("terminal-finalize-repair",),
            reason="provider_runtime_completed",
        )
        self.assertEqual(
            self.memory.store.delivery(request.request_id).state.value,
            "committed",
        )
        self.assertEqual(
            self.code.journal.delivery(request.request_id)["state"],
            "committed",
        )

    def test_post_query_persistence_failure_releases_both_deliveries(self) -> None:
        request = self._request("worker-request-persistence-failure")
        run = PersistenceFailingCodeWorkerRuntime(
            project_root=Path(__file__).resolve().parents[2],
            workspace_root=self.root,
            artifact_root=self.root / "persistence-failure-artifacts",
            retrieval_context_runtime=self.retrieval,
        ).run(request)

        self.assertFalse(run.worker_result.ok)
        self.assertEqual(
            run.worker_result.error,
            "code_worker_result_persistence_failed",
        )
        memory_delivery = self.memory.store.delivery(request.request_id)
        self.assertEqual(memory_delivery.state.value, "released")
        self.assertIn("result_persistence_failed", memory_delivery.reason)
        code_delivery = self.code.journal.delivery(request.request_id)
        self.assertEqual(code_delivery["state"], "released")
        self.assertIn("result_persistence_failed", code_delivery["reason"])

    def test_code_worker_fails_closed_when_delivery_finalize_needs_repair(self) -> None:
        request = self._request("worker-request-finalize-failure")
        original_complete = self.memory.store.complete_delivery

        def fail_complete(worker_request_id: str, *, terminal_event_ids):
            del worker_request_id, terminal_event_ids
            raise RuntimeError("injected final settlement failure")

        self.memory.store.complete_delivery = fail_complete
        try:
            run = CodeWorkerRuntime(
                project_root=Path(__file__).resolve().parents[2],
                workspace_root=self.root,
                artifact_root=self.root / "finalize-failure-artifacts",
                retrieval_context_runtime=self.retrieval,
            ).run(request)
        finally:
            self.memory.store.complete_delivery = original_complete

        self.assertFalse(run.worker_result.ok)
        self.assertEqual(
            run.worker_result.error,
            "retrieval_delivery_finalize_failed",
        )
        self.assertEqual(
            run.worker_result.metadata["retrieval_delivery_repair_required"],
            "true",
        )
        self.assertEqual(
            self.memory.store.delivery(request.request_id).state.value,
            "claimed",
        )
        self.assertEqual(
            self.code.journal.delivery(request.request_id)["state"],
            "committed",
        )
        self.memory.store.complete_delivery(
            request.request_id,
            terminal_event_ids=("manual-repair",),
        )
        self.assertEqual(
            self.memory.store.delivery(request.request_id).state.value,
            "committed",
        )

    @staticmethod
    def _request(request_id: str) -> WorkerRequest:
        return WorkerRequest(
            run_id="run-1",
            task_id="test-task",
            worker_name="CodeWorkerRuntime",
            request_id=request_id,
            messages=[
                AgentMessage(
                    run_id="run-1",
                    task_id="test-task",
                    sender_role=AgentRole.USER,
                    receiver_role=AgentRole.WORKER,
                    intent=MessageIntent.REQUEST,
                    content="Inspect fenced lease recovery context.",
                )
            ],
            constraints={"query_turns": []},
        )


if __name__ == "__main__":
    unittest.main()
