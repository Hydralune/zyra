from __future__ import annotations

import importlib
import gc
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from zyra_code_index import (
    BoundWorkspaceSource,
    CodeIndexIntegrationRuntime,
    CodeIndexRuntime,
)
from zyra_core import (
    AgentMessage,
    AgentRole,
    ArtifactKind,
    EventRecord,
    EventType,
    MessageIntent,
    PlanNodeStatus,
)
from zyra_memory import (
    CuratorConsumer,
    CuratorDeliveryConflictError,
    CuratorDeliveryState,
    CuratorIntegrationStore,
    CuratorOutcomeKind,
    RetrievalIntegrationRuntime,
)
from zyra_runtime import WorkerRequest
from zyra_workers import (
    MemoryCuratorOperation,
    MemoryCuratorWorkerRequest,
    WorkerRetrievalContextRuntime,
)


def _fresh_api_module() -> Any:
    module_name = "apps.api.zyra_api.main"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


class _Harness:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = {
            "ZYRA_SQLITE_PATH": str(self.root / "canonical.sqlite3"),
            "ZYRA_MEMORY_INDEX_PATH": str(self.root / "memory-index.sqlite3"),
            "ZYRA_EVENT_LOG": str(self.root / "events.jsonl"),
            "ZYRA_ARTIFACT_ROOT": str(self.root / "artifacts"),
            "ZYRA_WORKSPACE_ROOT": str(self.root / "workspace"),
        }
        self.previous = {
            name: os.environ.get(name) for name in self.environment
        }
        os.environ.update(self.environment)
        self.module = _fresh_api_module()
        self.module.reset_memory_curator_runtime()
        self.module.reset_runtime_event_spine_bridge()
        self.state, created = self.module.make_task_created_event(
            "Preserve deterministic lease recovery and browser verification evidence."
        )
        self.store = self.module.get_store()
        self.store.initialize()
        self.store.save_checkpoint(self.state)
        self.module.persist_events(self.store, [created])
        self.artifact_store = self.module.LocalArtifactStore(
            self.module.artifact_root_path()
        )
        self.artifact = self.artifact_store.write_text(
            run_id=self.state.run_id,
            task_id=self.state.task_id,
            content=(
                "Verified fenced lease takeover, exact resume, browser selector "
                "recovery, and the focused regression test."
            ),
            title="Curator integration evidence",
            kind=ArtifactKind.REPORT,
            extension=".md",
            producer_node_id=self.state.root_node_id,
        )
        self.state.artifacts.append(self.artifact)
        self.store.save_checkpoint(self.state)

    @property
    def runtime(self):
        return self.module.get_memory_curator_runtime(self.store)

    def add_rich_trace(self) -> None:
        events = [
            EventRecord(
                run_id=self.state.run_id,
                task_id=self.state.task_id,
                node_id=self.state.root_node_id,
                event_type=EventType.REQUIREMENT_CHANGE,
                payload={
                    "requirement_id": "REQ-CURATOR-INTEGRATION",
                    "summary": "Retain verified fenced lease takeover behavior.",
                    "source": "user",
                },
            ),
            EventRecord(
                run_id=self.state.run_id,
                task_id=self.state.task_id,
                node_id=self.state.root_node_id,
                event_type=EventType.COMMAND_SUCCEEDED,
                payload={
                    "tool_name": "lease_verifier",
                    "tool_call_id": "integration-tool-success-1",
                    "command": "python -m pytest tests/recovery -q",
                    "status": "succeeded",
                    "output": "12 passed",
                    "artifact_ids": [self.artifact.artifact_id],
                    "workspace_transaction_id": "workspace-tx-success-1",
                    "files_changed": ["packages/runtime/lease.py"],
                },
            ),
            EventRecord(
                run_id=self.state.run_id,
                task_id=self.state.task_id,
                node_id=self.state.root_node_id,
                event_type=EventType.COMMAND_SUCCEEDED,
                payload={
                    "tool_name": "lease_verifier",
                    "tool_call_id": "integration-tool-success-2",
                    "command": "python -m pytest tests/recovery -q",
                    "status": "succeeded",
                    "output": "12 passed",
                    "artifact_ids": [self.artifact.artifact_id],
                    "workspace_transaction_id": "workspace-tx-success-2",
                    "files_changed": ["tests/recovery/test_lease.py"],
                },
            ),
            EventRecord(
                run_id=self.state.run_id,
                task_id=self.state.task_id,
                node_id=self.state.root_node_id,
                event_type=EventType.COMMAND_FAILED,
                payload={
                    "tool_name": "pytest",
                    "tool_call_id": "integration-tool-failure-1",
                    "command": "python -m pytest tests/lease_epoch_failure -q",
                    "status": "failed",
                    "error": "LeaseEpochConflict: stale ownership token",
                    "stderr": "LeaseEpochConflict at lease.py:42",
                    "exit_code": 1,
                },
            ),
            EventRecord(
                run_id=self.state.run_id,
                task_id=self.state.task_id,
                node_id=self.state.root_node_id,
                event_type=EventType.COMMAND_FAILED,
                payload={
                    "tool_name": "pytest",
                    "tool_call_id": "integration-tool-failure-2",
                    "command": "python -m pytest tests/lease_epoch_failure -q",
                    "status": "failed",
                    "error": "LeaseEpochConflict: stale ownership token",
                    "stderr": "LeaseEpochConflict at lease.py:42",
                    "exit_code": 1,
                },
            ),
            EventRecord(
                run_id=self.state.run_id,
                task_id=self.state.task_id,
                node_id=self.state.root_node_id,
                event_type=EventType.BROWSER_RUNTIME_DIAGNOSTIC,
                payload={
                    "browser_session_id": "browser-session-curator",
                    "target_id": "page-curator",
                    "url": "https://example.test/recovery",
                    "action": "click",
                    "selector": "button[data-retry]",
                    "status": "succeeded",
                    "artifact_ids": [self.artifact.artifact_id],
                    "diagnostic": {
                        "watchdog": "healthy",
                        "selected_target": "page-curator",
                    },
                },
            ),
        ]
        self.module.persist_events(self.store, events)

    def run_manual(self, *, idempotency_key: str = ""):
        return self.runtime.execute(
            MemoryCuratorWorkerRequest(
                operation=MemoryCuratorOperation.SCHEDULE_MANUAL,
                task_id=self.state.task_id,
                requested_by="integration-test",
                allow_model_assist=True,
                max_candidates=128,
                idempotency_key=idempotency_key,
                process_immediately=True,
            )
        )

    def close(self) -> None:
        self.module.reset_memory_curator_runtime()
        self.module.reset_runtime_event_spine_bridge()
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        gc.collect()
        for attempt in range(20):
            try:
                self.temporary.cleanup()
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
            else:
                break


class MemoryCuratorWorkerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _Harness()

    def tearDown(self) -> None:
        self.harness.close()

    def test_direct_05c_trace_projects_skill_failure_and_recall_contracts(self) -> None:
        self.harness.add_rich_trace()
        response = self.harness.run_manual()

        self.assertEqual(response.status, "succeeded")
        self.assertIsNotNone(response.integration)
        integration = dict(response.integration or {})
        self.assertTrue(integration["ok"])
        self.assertGreater(response.result.committed_count, 0)
        self.assertEqual(response.result.model_status, "unavailable")
        ingress = response.result.diagnostics["runtime_event_ingress"]
        self.assertTrue(ingress["direct_spine"])
        self.assertFalse(ingress["fallback_used"])
        self.assertEqual(
            ingress["diagnostics"]["compatibility_only_event_count"],
            0,
        )
        self.assertGreaterEqual(
            ingress["batch"]["runtime_event_count"],
            7,
        )
        evidence_kinds = set(ingress["batch"]["evidence_kinds"])
        self.assertIn("tool_result", evidence_kinds)
        self.assertIn("browser_trace", evidence_kinds)
        self.assertIn("failure", evidence_kinds)

        app = self.harness.runtime.integration_application
        outcomes = app.store.outcomes(
            task_id=self.harness.state.task_id,
            limit=10_000,
        )
        kinds = {item.kind for item in outcomes}
        self.assertIn(CuratorOutcomeKind.CANDIDATE, kinds)
        self.assertIn(CuratorOutcomeKind.ACCEPTED, kinds)
        self.assertIn(CuratorOutcomeKind.COMMITTED, kinds)
        self.assertIn(CuratorOutcomeKind.INDEX_PUBLISHED, kinds)
        self.assertIn(CuratorOutcomeKind.SKILL_CANDIDATE, kinds)
        self.assertIn(CuratorOutcomeKind.FAILURE_PATTERN, kinds)
        self.assertIn(CuratorOutcomeKind.COMPRESSED, kinds)
        canonical = [item for item in outcomes if item.canonical_memory_changed]
        self.assertTrue(canonical)
        self.assertTrue(all(item.deterministic_validation for item in canonical))
        self.assertTrue(all(item.commit_receipt_id for item in canonical))

        proofs = app.store.context_proofs(
            task_id=self.harness.state.task_id,
            verified_only=True,
            limit=10_000,
        )
        self.assertTrue(proofs)
        self.assertTrue(all(proof.memory_id in proof.retrieved_memory_ids for proof in proofs))
        self.assertTrue(all(not proof.contains_index_dump for proof in proofs))
        context_receipts = app.store.deliveries(
            task_id=self.harness.state.task_id,
            consumer=CuratorConsumer.RETRIEVAL_CONTEXT,
            limit=10_000,
        )
        self.assertEqual(
            {item.state for item in context_receipts},
            {CuratorDeliveryState.ACKNOWLEDGED},
        )

        skill_report = app.dispatch_consumer(
            CuratorConsumer.SKILL_MEMORY,
            task_id=self.harness.state.task_id,
            limit=100,
        )
        fault_report = app.dispatch_consumer(
            CuratorConsumer.FAULT_OBSERVER,
            task_id=self.harness.state.task_id,
            limit=100,
        )
        recovery_report = app.dispatch_consumer(
            CuratorConsumer.RECOVERY_PLANNER,
            task_id=self.harness.state.task_id,
            limit=100,
        )
        self.assertTrue(skill_report.ok)
        self.assertTrue(fault_report.ok)
        self.assertTrue(recovery_report.ok)
        self.assertGreater(skill_report.acknowledged_count, 0)
        self.assertGreater(fault_report.acknowledged_count, 0)
        self.assertGreater(recovery_report.acknowledged_count, 0)
        skill_receipts = [
            item.receipt
            for item in app.store.deliveries(
                task_id=self.harness.state.task_id,
                consumer=CuratorConsumer.SKILL_MEMORY,
                limit=100,
            )
        ]
        self.assertEqual(
            {item["next_owner"] for item in skill_receipts},
            {"M1-06C SkillMemoryRuntime"},
        )
        recovery_receipts = [
            item.receipt
            for item in app.store.deliveries(
                task_id=self.harness.state.task_id,
                consumer=CuratorConsumer.RECOVERY_PLANNER,
                limit=100,
            )
        ]
        self.assertEqual(
            {item["next_owner"] for item in recovery_receipts},
            {"M1-07C RecoveryPlannerRuntime"},
        )

        event_types = {
            str(item["event_type"])
            for item in self.harness.store.task_events(self.harness.state.task_id)
        }
        self.assertIn("memory_curator_candidate", event_types)
        self.assertIn("memory_curator_accepted", event_types)
        self.assertIn("memory_curator_committed", event_types)
        self.assertIn("memory_curator_index_published", event_types)

    def test_secret_bearing_tool_result_is_rejected_without_canonical_write(self) -> None:
        self.harness.module.persist_events(
            self.harness.store,
            [
                EventRecord(
                    run_id=self.harness.state.run_id,
                    task_id=self.harness.state.task_id,
                    node_id=self.harness.state.root_node_id,
                    event_type=EventType.COMMAND_SUCCEEDED,
                    payload={
                        "tool_name": "credential_probe",
                        "tool_call_id": "integration-tool-secret-1",
                        "command": "python scripts/check_provider.py",
                        "status": "succeeded",
                        "output": "provider responded but the credential must not enter memory",
                        "api_key": "sk-notarealcredential00000000",
                    },
                )
            ],
        )

        response = self.harness.run_manual()

        self.assertEqual(response.status, "succeeded")
        self.assertEqual(response.result.committed_count, 0)
        outcomes = self.harness.runtime.integration_store.outcomes(
            task_id=self.harness.state.task_id,
            limit=10_000,
        )
        self.assertIn(
            CuratorOutcomeKind.REJECTED,
            {item.kind for item in outcomes},
        )
        self.assertFalse(any(item.canonical_memory_changed for item in outcomes))
        self.assertEqual(
            self.harness.store.task_memory_records(self.harness.state.task_id),
            [],
        )

    def test_committed_curator_memory_reaches_real_worker_retrieval_context(self) -> None:
        self.harness.add_rich_trace()
        response = self.harness.run_manual()
        self.assertGreater(response.result.committed_count, 0)

        app = self.harness.runtime.integration_application
        workspace = Path(self.harness.environment["ZYRA_WORKSPACE_ROOT"])
        (workspace / "src").mkdir(parents=True, exist_ok=True)
        (workspace / "src" / "lease.py").write_text(
            "def reclaim(token):\n    return f'fenced lease {token}'\n",
            encoding="utf-8",
        )
        code_runtime = CodeIndexRuntime(
            BoundWorkspaceSource.for_test(workspace),
            index_path=self.harness.root / "worker-code-index.sqlite3",
        )
        code = CodeIndexIntegrationRuntime(
            code_runtime,
            run_id=self.harness.state.run_id,
            task_id=self.harness.state.task_id,
            allow_inline_worker_for_tests=True,
        )
        code.admit_initial(process=True)
        retrieval = WorkerRetrievalContextRuntime(
            memory=RetrievalIntegrationRuntime(
                app.memory_index,
                allow_inline_worker_for_tests=True,
            ),
            code=code,
        )
        request = WorkerRequest(
            run_id=self.harness.state.run_id,
            task_id=self.harness.state.task_id,
            worker_name="CodeWorkerRuntime",
            request_id="worker-after-curator",
            node_id=self.harness.state.root_node_id,
            messages=[
                AgentMessage(
                    run_id=self.harness.state.run_id,
                    task_id=self.harness.state.task_id,
                    node_id=self.harness.state.root_node_id,
                    sender_role=AgentRole.USER,
                    receiver_role=AgentRole.WORKER,
                    intent=MessageIntent.REQUEST,
                    content="Apply the verified fenced lease takeover and recovery procedure.",
                )
            ],
        )

        context = retrieval.prepare(request, session_id="session-after-curator")
        memory_messages = [
            item
            for item in context.messages
            if item.metadata.get("retrieval_scope") == "task_memory"
        ]
        self.assertTrue(memory_messages)
        self.assertIn("fenced lease", memory_messages[0].content.casefold())
        canonical_ids = {
            item.memory_id
            for item in self.harness.store.task_memory_records(
                self.harness.state.task_id
            )
        }
        retrieved_ids = {
            ref.document_id
            for ref in context.memory_execution.snapshot.source_refs
        }
        self.assertTrue(canonical_ids.intersection(retrieved_ids))
        retrieval.finish(
            context,
            committed=True,
            terminal_event_ids=("worker-after-curator-terminal",),
            reason="worker_consumed_curated_memory",
        )
        self.assertEqual(
            retrieval.memory.store.delivery(request.request_id).state.value,
            "committed",
        )

    def test_terminal_task_automatic_path_uses_new_runtime_events(self) -> None:
        self.harness.add_rich_trace()
        self.harness.state.status = PlanNodeStatus.COMPLETED
        self.harness.store.save_checkpoint(self.harness.state)
        self.harness.module.persist_events(
            self.harness.store,
            [
                EventRecord(
                    run_id=self.harness.state.run_id,
                    task_id=self.harness.state.task_id,
                    node_id=self.harness.state.root_node_id,
                    event_type=EventType.EVALUATION,
                    payload={
                        "status": "passed",
                        "summary": "Terminal task verification passed.",
                        "artifact_ids": [self.harness.artifact.artifact_id],
                    },
                )
            ],
        )

        response = self.harness.runtime.execute(
            MemoryCuratorWorkerRequest(
                operation=MemoryCuratorOperation.SCHEDULE_TASK_END,
                task_id=self.harness.state.task_id,
                requested_by="task-lifecycle",
                process_immediately=True,
            )
        )

        self.assertEqual(response.status, "succeeded")
        self.assertEqual(response.scheduled.request.trigger.value, "task_end")
        self.assertTrue(response.integration["ok"])
        self.assertTrue(
            response.result.diagnostics["runtime_event_ingress"]["direct_spine"]
        )
        run = self.harness.runtime.integration_store.integration_run_for_job(
            response.result.job_id
        )
        self.assertEqual(run.state.value, "succeeded")
        self.assertGreater(run.canonical_commit_count, 0)
        self.assertGreater(run.index_publication_count, 0)

    def test_integration_replay_is_idempotent_and_does_not_duplicate_memory(self) -> None:
        self.harness.add_rich_trace()
        response = self.harness.run_manual(idempotency_key="integration-replay")
        app = self.harness.runtime.integration_application
        before_memory = tuple(
            record.memory_id
            for record in self.harness.store.task_memory_records(
                self.harness.state.task_id
            )
        )
        before_outcomes = app.store.outcomes(
            task_id=self.harness.state.task_id,
            limit=100_000,
        )
        before_deliveries = app.store.deliveries(
            task_id=self.harness.state.task_id,
            limit=100_000,
        )

        replay = app.integrate(response.result)

        after_memory = tuple(
            record.memory_id
            for record in self.harness.store.task_memory_records(
                self.harness.state.task_id
            )
        )
        after_outcomes = app.store.outcomes(
            task_id=self.harness.state.task_id,
            limit=100_000,
        )
        after_deliveries = app.store.deliveries(
            task_id=self.harness.state.task_id,
            limit=100_000,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(before_memory, after_memory)
        self.assertEqual(
            {item.outcome_id for item in before_outcomes},
            {item.outcome_id for item in after_outcomes},
        )
        self.assertEqual(
            {item.delivery_id for item in before_deliveries},
            {item.delivery_id for item in after_deliveries},
        )
        self.assertTrue(app.store.consistency_report(task_id=self.harness.state.task_id)["ok"])

    def test_validator_disconnect_fails_closed(self) -> None:
        self.harness.add_rich_trace()
        runtime = self.harness.runtime
        scheduled = runtime.scheduler.schedule_manual(
            task_id=self.harness.state.task_id,
            requested_by="validator-disable-probe",
            idempotency_key="validator-disabled",
        )

        class DisabledValidator:
            def validate_many(self, candidates):
                del candidates
                raise RuntimeError("validator deliberately disconnected")

        original_validator = runtime.worker.validator
        runtime.worker.validator = DisabledValidator()
        try:
            with self.assertRaisesRegex(RuntimeError, "validator deliberately disconnected"):
                runtime.integration_application.process_job(scheduled.job.job_id)
        finally:
            runtime.worker.validator = original_validator
        self.assertEqual(
            self.harness.store.task_memory_records(self.harness.state.task_id),
            [],
        )
        self.assertFalse(
            runtime.integration_store.outcomes(
                task_id=self.harness.state.task_id,
                limit=100,
            )
        )

    def test_committer_disconnect_fails_closed(self) -> None:
        self.harness.add_rich_trace()
        runtime = self.harness.runtime
        scheduled = runtime.scheduler.schedule_manual(
            task_id=self.harness.state.task_id,
            requested_by="committer-disable-probe",
            idempotency_key="committer-disabled",
        )

        class DisabledCommitter:
            def commit(self, candidate, decision):
                del candidate, decision
                raise RuntimeError("committer deliberately disconnected")

        original_committer = runtime.worker.commit_runtime
        runtime.worker.commit_runtime = DisabledCommitter()
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "committer deliberately disconnected",
            ):
                runtime.integration_application.process_job(scheduled.job.job_id)
        finally:
            runtime.worker.commit_runtime = original_committer
        self.assertEqual(
            self.harness.store.task_memory_records(self.harness.state.task_id),
            [],
        )
        self.assertFalse(
            runtime.integration_store.outcomes(
                task_id=self.harness.state.task_id,
                limit=100,
            )
        )
        event_types = {
            str(item["event_type"])
            for item in self.harness.store.task_events(self.harness.state.task_id)
        }
        self.assertNotIn("memory_curator_committed", event_types)
        self.assertNotIn("memory_curator_index_published", event_types)

    def test_downstream_lease_expiry_reclaims_and_fences_stale_owner(self) -> None:
        self.harness.add_rich_trace()
        self.harness.run_manual()
        app = self.harness.runtime.integration_application
        pending = app.store.deliveries(
            task_id=self.harness.state.task_id,
            consumer=CuratorConsumer.SKILL_MEMORY,
            states=(CuratorDeliveryState.PENDING,),
            limit=100,
        )
        self.assertTrue(pending)
        clock = [time.time()]
        store = CuratorIntegrationStore(
            self.harness.store.path,
            clock=lambda: clock[0],
        )
        first = store.claim_delivery(
            consumer=CuratorConsumer.SKILL_MEMORY,
            worker_id="skill-worker-a",
            delivery_id=pending[0].delivery_id,
            lease_seconds=5.0,
        )
        self.assertIsNotNone(first)
        first_delivery, first_lease = first
        self.assertEqual(first_delivery.state, CuratorDeliveryState.CLAIMED)
        clock[0] += 6.0
        swept = store.sweep_expired_deliveries(
            consumer=CuratorConsumer.SKILL_MEMORY,
            retry_delay_seconds=0.0,
        )
        self.assertIn(first_delivery.delivery_id, swept)
        second = store.claim_delivery(
            consumer=CuratorConsumer.SKILL_MEMORY,
            worker_id="skill-worker-b",
            delivery_id=pending[0].delivery_id,
            lease_seconds=5.0,
        )
        self.assertIsNotNone(second)
        second_delivery, second_lease = second
        self.assertGreater(second_lease.lease_epoch, first_lease.lease_epoch)
        with self.assertRaises(CuratorDeliveryConflictError):
            store.acknowledge_delivery(
                first_lease,
                receipt={
                    "delivery_id": first_delivery.delivery_id,
                    "outcome_id": first_delivery.outcome_id,
                    "consumer": CuratorConsumer.SKILL_MEMORY.value,
                },
            )
        acknowledged = store.acknowledge_delivery(
            second_lease,
            receipt={
                "delivery_id": second_delivery.delivery_id,
                "outcome_id": second_delivery.outcome_id,
                "consumer": CuratorConsumer.SKILL_MEMORY.value,
                "worker": "skill-worker-b",
            },
            partition_key=self.harness.state.task_id,
        )
        self.assertEqual(acknowledged.state, CuratorDeliveryState.ACKNOWLEDGED)
        checkpoint = store.consumer_checkpoint(
            CuratorConsumer.SKILL_MEMORY,
            partition_key=self.harness.state.task_id,
        )
        self.assertEqual(checkpoint["last_delivery_id"], acknowledged.delivery_id)
        self.assertEqual(checkpoint["acknowledged_count"], 1)


if __name__ == "__main__":
    unittest.main()
