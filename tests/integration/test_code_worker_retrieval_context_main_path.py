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
from zyra_workers import CodeWorkerRuntime, WorkerRetrievalContextRuntime


class CapturingFailingQueryEngine:
    configs: list[object] = []
    calls: list[dict] = []

    def __init__(self, execution_context, config) -> None:
        del execution_context
        type(self).configs.append(config)

    def run(self, **kwargs):
        type(self).calls.append(dict(kwargs))
        raise RuntimeError("stop after observing injected retrieval context")


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


if __name__ == "__main__":
    unittest.main()
