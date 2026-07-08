from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import EventRecord, EventType, create_task_state  # noqa: E402
from zyra_runtime import (  # noqa: E402
    JsonPermissionStore,
    ToolBatchExecutionMode,
    ToolContextModifier,
    ToolContextModifierKind,
    ToolBudgetPolicyRuntime,
    ToolBudgetPressure,
    ToolContinuationMode,
    ToolContinuationRuntime,
    ToolContinuationStatus,
    ToolCleanroomRuntime,
    ToolCleanroomStatus,
    ToolConcurrencyMode,
    ToolConcurrencyRuntime,
    ToolContractGateRuntime,
    ToolContractGateStatus,
    ToolExecutionContext,
    ToolExecutionRuntime,
    ToolFailurePolicyRuntime,
    ToolFailureRoute,
    ToolOutputStoreRuntime,
    ToolOutputStoreStatus,
    ToolFoundationAuditRuntime,
    ToolFailureKind,
    ToolFoundationPersistenceRuntime,
    ToolFoundationReplayStatus,
    ToolPermissionHandoffRuntime,
    ToolPermissionReply,
    ToolPermissionReplyEffect,
    ToolSettlementRuntime,
    ToolSettlementStatus,
    ToolStreamFrameKind,
    ToolStreamingRuntime,
    ToolSourceCoverageRuntime,
    ToolSourceCoverageStatus,
    ToolLoopScheduler,
    ToolRegistry,
    ToolRegistryFilterDecision,
    ToolRegistryRuntime,
    ToolResult,
    ToolResultBudgetRuntime,
    ToolRuntimeDisabledError,
    ToolFoundationAuditStatus,
    ToolSpec,
    ToolUseContext,
    ToolResultBudgeter,
    default_tool_registry,
    tool_runtime_source_to_target_rows,
    tool_failure_signal_from_result,
)


class ToolLoopBudgetRuntimeTests(unittest.TestCase):
    def test_scheduler_batches_read_only_tools_and_conflict_protects_repeated_writes(self) -> None:
        state = create_task_state("Plan tool batches.")
        scheduler = ToolLoopScheduler(default_tool_registry(), max_read_only_concurrency=4)

        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[
                {"tool_name": "file_read", "arguments": {"path": "a.txt"}},
                {"tool_name": "file_read", "arguments": {"path": "b.txt"}},
                {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "one"}},
                {"tool_name": "file_write", "arguments": {"path": "same.txt", "content": "two"}},
            ],
        )

        self.assertEqual(len(plan.batches), 3)
        self.assertEqual(plan.batches[0].execution_mode, ToolBatchExecutionMode.CONCURRENT_READ_ONLY)
        self.assertEqual([request.tool_name for request in plan.batches[0].requests], ["file_read", "file_read"])
        self.assertEqual(plan.batches[1].execution_mode, ToolBatchExecutionMode.SERIAL_NON_READ_ONLY)
        self.assertEqual(plan.batches[2].execution_mode, ToolBatchExecutionMode.SERIAL_NON_READ_ONLY)
        self.assertTrue(plan.batches[2].conflict_protected)
        self.assertEqual(plan.conflict_protected_count, 1)
        self.assertEqual(plan.read_only_count, 2)
        self.assertEqual(plan.write_count, 2)

    def test_scheduler_validates_tool_schema_before_execution(self) -> None:
        state = create_task_state("Validate schema.")
        scheduler = ToolLoopScheduler(default_tool_registry())

        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[
                {"tool_name": "file_write", "arguments": {"path": "missing-content.txt"}},
                {"tool_name": "shell", "arguments": {"command": "echo ok", "timeout_seconds": "fast"}},
                {"tool_name": "missing_tool", "arguments": {}},
            ],
        )

        self.assertEqual(plan.schema_error_count, 3)
        error_fields = [error.field for request in plan.requests for error in request.schema_errors]
        self.assertIn("content", error_fields)
        self.assertIn("timeout_seconds", error_fields)
        self.assertIn("tool_name", error_fields)
        schema_result = scheduler.schema_error_result(plan.requests[0])
        self.assertFalse(schema_result.ok)
        self.assertEqual(schema_result.error, "schema_error")
        self.assertEqual(schema_result.metadata["failure_kind"], "schema_error")

    def test_budgeter_externalizes_large_tool_result_as_structured_artifact(self) -> None:
        state = create_task_state("Budget tool result.")
        scheduler = ToolLoopScheduler(default_tool_registry())
        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            context = ToolExecutionContext.for_workspace(
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            result = ToolResult(
                tool_call_id=plan.requests[0].call.tool_call_id,
                ok=True,
                summary="Read large.txt",
                output={"content": "x" * 500, "path": "large.txt"},
            )

            bounded, decision = ToolResultBudgeter(max_chars=80).apply(
                request=plan.requests[0],
                result=result,
                artifact_store=context.artifact_store,
            )

            self.assertTrue(decision.applied)
            self.assertTrue(bounded.output["truncated"])
            self.assertEqual(bounded.output["full_output_artifact_id"], decision.artifact_id)
            self.assertEqual(bounded.metadata["tool_result_budget_applied"], "true")
            artifact_path = Path(bounded.artifacts[-1].uri)
            self.assertTrue(artifact_path.exists())
            self.assertEqual(json.loads(artifact_path.read_text(encoding="utf-8"))["content"], "x" * 500)

    def test_failure_signal_maps_budget_schema_timeout_and_runtime_failures(self) -> None:
        state = create_task_state("Map tool failures.")
        scheduler = ToolLoopScheduler(default_tool_registry())
        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[
                {"tool_name": "file_write", "arguments": {"path": "missing-content.txt"}},
                {"tool_name": "shell", "arguments": {"command": "sleep 10", "approved": True}},
            ],
        )
        schema_signal = tool_failure_signal_from_result(
            plan.requests[0],
            scheduler.schema_error_result(plan.requests[0]),
        )
        timeout_signal = tool_failure_signal_from_result(
            plan.requests[1],
            ToolResult(
                tool_call_id=plan.requests[1].call.tool_call_id,
                ok=False,
                summary="shell timed out after 1 second(s)",
                error="tool_timeout",
            ),
        )
        runtime_signal = tool_failure_signal_from_result(
            plan.requests[1],
            ToolResult(
                tool_call_id=plan.requests[1].call.tool_call_id,
                ok=False,
                summary="shell failed",
                error="RuntimeError",
            ),
        )

        self.assertIsNotNone(schema_signal)
        self.assertIsNotNone(timeout_signal)
        self.assertIsNotNone(runtime_signal)
        self.assertEqual(schema_signal.kind, ToolFailureKind.SCHEMA_ERROR)
        self.assertEqual(schema_signal.watchdog_route, "repair_tool_arguments")
        self.assertFalse(schema_signal.retryable)
        self.assertEqual(timeout_signal.kind, ToolFailureKind.TIMEOUT)
        self.assertEqual(timeout_signal.watchdog_route, "retry_or_background")
        self.assertTrue(timeout_signal.retryable)
        self.assertEqual(runtime_signal.kind, ToolFailureKind.RUNTIME_ERROR)
        self.assertEqual(runtime_signal.watchdog_route, "recovery_planner")

    def test_registry_runtime_materializes_stable_permission_filtered_tool_pool(self) -> None:
        registry = ToolRegistry(
            [
                ToolSpec(
                    "mcp_dynamic",
                    "dynamic tool",
                    "mcp server",
                    metadata={"access_mode": "read_only", "read_only": "true", "concurrency_safe": "true"},
                ),
                ToolSpec(
                    "blocked",
                    "blocked tool",
                    "claude-code-best",
                    metadata={"access_mode": "read_only", "registry_filter_effect": "deny"},
                ),
                ToolSpec(
                    "alpha",
                    "base tool",
                    "claude-code-best",
                    metadata={"access_mode": "read_only", "read_only": "true", "concurrency_safe": "true"},
                ),
            ]
        )

        materialization = ToolRegistryRuntime(registry).materialize(
            worker_request_id="worker-test",
            session_id="session-test",
            workspace_root=ROOT,
        )

        self.assertEqual(materialization.active_tool_names, ("alpha", "mcp_dynamic"))
        self.assertEqual(materialization.filtered_tool_names, ("blocked",))
        blocked_entry = next(entry for entry in materialization.entries if entry.name == "blocked")
        self.assertEqual(blocked_entry.filter_decision, ToolRegistryFilterDecision.FILTERED_DENY)
        source_repos = {row.source_repo for row in tool_runtime_source_to_target_rows()}
        self.assertIn("opencode", source_repos)
        self.assertIn("hermes-agent", source_repos)

    def test_foundation_audit_requires_reachable_registry_receipts_and_context(self) -> None:
        materialization = ToolRegistryRuntime(default_tool_registry()).materialize(
            worker_request_id="worker-test",
            session_id="session-test",
            workspace_root=ROOT,
        )
        events = [
            _query_event("tool_registry_materialized"),
            _query_event("tool_call_started"),
            _query_event("tool_context_modifier_applied"),
            _query_event("tool_call_completed"),
            EventRecord(run_id="run-test", task_id="task-test", node_id=None, event_type=EventType.AGENT_MESSAGE, payload={"tool_result": {}}),
        ]
        report = ToolFoundationAuditRuntime().build_report(
            materialization=materialization.to_dict(),
            context_snapshots=[
                {
                    "modifier_log": [{"kind": "message_append"}],
                    "permission_handoffs": [],
                    "budget_ledger": [],
                    "artifact_refs": [],
                    "read_file_state": {"a.txt": {}},
                    "content_replacements": {},
                    "tool_result_chars": 12,
                }
            ],
            receipt_snapshots=[
                {
                    "raw_result": {"ok": True},
                    "bounded_result": {"ok": True},
                    "budget_decision": {"applied": False},
                    "failure_signal": None,
                    "context_modifiers": [{"kind": "message_append"}],
                    "executor_name": "ToolExecutor",
                }
            ],
            event_records=events,
            expected_tool_calls=1,
        )

        self.assertTrue(report.ok)
        self.assertEqual(report.status, ToolFoundationAuditStatus.PASS)
        self.assertIn("opencode", report.metadata()["tool_foundation_audit_source_repos"])

    def test_foundation_persistence_writes_and_replays_receipts_context_and_output_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Persist tool foundation state.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "large.txt").write_text("payload-" * 80, encoding="utf-8")
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            materialization = ToolRegistryRuntime(execution_context.registry).materialize(
                worker_request_id="worker-test",
                session_id="session-test",
                workspace_root=workspace,
            )
            scheduler = ToolLoopScheduler(materialization.to_registry())
            plan = scheduler.plan_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                turn_index=1,
                steps=[{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
            )
            tool_context = ToolUseContext.for_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                session_id="session-test",
                turn_id="turn-test",
                turn_index=1,
                materialization=materialization,
            )
            runtime = ToolExecutionRuntime(
                execution_context,
                scheduler=scheduler,
                budget_runtime=ToolResultBudgetRuntime(max_result_chars=120),
            )
            receipts = runtime.execute_batch(plan.batches[0], tool_context=tool_context, max_workers=1)
            receipt_snapshots = [receipt.to_dict() for receipt in receipts]
            context_snapshots = [tool_context.to_dict(include_messages=False)]
            audit_report = ToolFoundationAuditRuntime().build_report(
                materialization=materialization.to_dict(),
                context_snapshots=context_snapshots,
                receipt_snapshots=receipt_snapshots,
                event_records=[
                    _query_event("tool_registry_materialized"),
                    _query_event("tool_call_started"),
                    _query_event("tool_context_modifier_applied"),
                    _query_event("tool_call_completed"),
                    EventRecord(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        node_id=state.root_node_id,
                        event_type=EventType.AGENT_MESSAGE,
                        payload={"tool_result": {}},
                    ),
                ],
                expected_tool_calls=1,
            )
            persistence = ToolFoundationPersistenceRuntime(execution_context.artifact_store)

            artifact_set = persistence.persist_final_state(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                session_id="session-test",
                worker_request_id="worker-test",
                materialization=materialization.to_dict(),
                receipts=receipt_snapshots,
                context_snapshots=context_snapshots,
                audit_report=audit_report,
            )

            self.assertEqual(artifact_set.persisted_count, 5)
            self.assertIsNotNone(artifact_set.receipt_log_artifact)
            self.assertIsNotNone(artifact_set.externalized_output_index_artifact)
            replayed = persistence.replay_receipt_log(artifact_set.receipt_log_artifact.artifact)
            self.assertEqual(len(replayed), 1)
            output_index = persistence.replay_json_artifact(artifact_set.externalized_output_index_artifact.artifact)
            self.assertEqual(output_index["externalized_output_count"], 1)
            replay_report = persistence.replay_json_artifact(artifact_set.replay_report_artifact.artifact)
            self.assertEqual(replay_report["replay_report"]["status"], ToolFoundationReplayStatus.READY)

    def test_settlement_runtime_blocks_missing_execution_receipts(self) -> None:
        state = create_task_state("Settle tool registry.")
        materialization = ToolRegistryRuntime(default_tool_registry()).materialize(
            worker_request_id="worker-test",
            session_id="session-test",
            workspace_root=ROOT,
        )
        scheduler = ToolLoopScheduler(materialization.to_registry())
        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[{"tool_name": "file_read", "arguments": {"path": "a.txt"}}],
        )
        settlement = ToolSettlementRuntime()

        report = settlement.settle_plan(
            materialization=materialization,
            plan=plan,
            session_id="session-test",
            turn_id="turn-test",
        )
        receipt_report = settlement.settle_receipts(report, plan=plan, receipts=[])

        self.assertTrue(report.ok)
        self.assertEqual(report.status, ToolSettlementStatus.SETTLED)
        self.assertFalse(receipt_report.ok)
        self.assertEqual(receipt_report.status, ToolSettlementStatus.BLOCKED)
        self.assertIn("TOOL_SETTLEMENT_RECEIPT_MISSING", receipt_report.blocking_codes)

    def test_tool_use_context_flushes_concurrent_modifiers_after_batch(self) -> None:
        materialization = ToolRegistryRuntime(default_tool_registry()).materialize(
            worker_request_id="worker-test",
            session_id="session-test",
            workspace_root=ROOT,
        )
        context = ToolUseContext.for_turn(
            run_id="run-test",
            task_id="task-test",
            node_id=None,
            worker_request_id="worker-test",
            session_id="session-test",
            turn_id="turn-test",
            turn_index=1,
            materialization=materialization,
        )
        context.queue_modifier(
            ToolContextModifier(
                kind=ToolContextModifierKind.CONTENT_REPLACEMENT,
                tool_call_id="tool-b",
                key="same.txt",
                value={"content": "b"},
                phase="after_mutating_tool",
            )
        )
        context.queue_modifier(
            ToolContextModifier(
                kind=ToolContextModifierKind.CONTENT_REPLACEMENT,
                tool_call_id="tool-a",
                key="same.txt",
                value={"content": "a"},
                phase="after_mutating_tool",
            )
        )

        applied = context.flush_concurrent_modifiers()

        self.assertEqual([item["tool_call_id"] for item in applied], ["tool-a", "tool-b"])
        self.assertEqual(applied[-1]["status"], "applied_with_conflict")
        self.assertEqual(context.content_replacements["same.txt"]["content"], "b")
        self.assertEqual(context.metadata()["tool_use_context_modifiers"], "2")

    def test_execution_runtime_records_permission_handoff_before_shell_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Permission handoff.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            permission_store = JsonPermissionStore(Path(tmpdir) / "permissions.json")
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                permission_store=permission_store,
            )
            scheduler = ToolLoopScheduler(execution_context.registry)
            plan = scheduler.plan_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                turn_index=1,
                steps=[{"tool_name": "shell", "arguments": {"command": "echo should-not-run"}}],
            )
            materialization = ToolRegistryRuntime(execution_context.registry, permission_policy=execution_context.permission_policy).materialize(
                worker_request_id="worker-test",
                session_id="session-test",
                workspace_root=workspace,
            )
            tool_context = ToolUseContext.for_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                session_id="session-test",
                turn_id="turn-test",
                turn_index=1,
                materialization=materialization,
            )
            runtime = ToolExecutionRuntime(
                execution_context,
                scheduler=scheduler,
                budget_runtime=ToolResultBudgetRuntime(max_result_chars=8000),
            )

            receipts = runtime.execute_batch(plan.batches[0], tool_context=tool_context, max_workers=1)

            self.assertEqual(len(receipts), 1)
            self.assertFalse(receipts[0].bounded_result.ok)
            self.assertEqual(receipts[0].bounded_result.error, "permission_required")
            self.assertEqual(len(permission_store.list_requests()), 1)
            self.assertEqual(len(tool_context.permission_handoffs), 1)
            self.assertEqual(receipts[0].modifier_applications[-1]["kind"], "permission_handoff")

    def test_permission_handoff_runtime_projects_and_resolves_ask_questions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Resolve permission handoff.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            permission_store = JsonPermissionStore(Path(tmpdir) / "permissions.json")
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
                permission_store=permission_store,
            )
            materialization = ToolRegistryRuntime(
                execution_context.registry,
                permission_policy=execution_context.permission_policy,
            ).materialize(
                worker_request_id="worker-test",
                session_id="session-test",
                workspace_root=workspace,
            )
            scheduler = ToolLoopScheduler(materialization.to_registry())
            plan = scheduler.plan_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                turn_index=1,
                steps=[{"tool_name": "shell", "arguments": {"command": "echo needs-approval"}}],
            )
            tool_context = ToolUseContext.for_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                session_id="session-test",
                turn_id="turn-test",
                turn_index=1,
                materialization=materialization,
            )
            receipts = ToolExecutionRuntime(
                execution_context,
                scheduler=scheduler,
                budget_runtime=ToolResultBudgetRuntime(max_result_chars=8000),
            ).execute_batch(plan.batches[0], tool_context=tool_context, max_workers=1)
            handoff_runtime = ToolPermissionHandoffRuntime(permission_store=permission_store)
            report = handoff_runtime.build_report(
                session_id="session-test",
                worker_request_id="worker-test",
                receipts=[receipt.to_dict() for receipt in receipts],
                context_snapshots=[tool_context.to_dict(include_messages=False)],
            )

            self.assertEqual(report.pending_count, 1)
            self.assertEqual(report.questions[0].tool_name, "shell")
            resolution = handoff_runtime.resolve(
                report,
                ToolPermissionReply(
                    question_id=report.questions[0].question_id,
                    effect=ToolPermissionReplyEffect.APPROVE_ALWAYS,
                ),
            )
            resolved_report = handoff_runtime.report_with_resolutions(report, [resolution])

            self.assertTrue(resolution.ok)
            self.assertEqual(resolution.store_status, "approved")
            self.assertEqual(resolved_report.pending_count, 0)
            self.assertEqual(len(permission_store.list_rules()), 1)

    def test_budget_runtime_externalizes_turn_aggregate_overflow(self) -> None:
        state = create_task_state("Aggregate budget.")
        scheduler = ToolLoopScheduler(default_tool_registry())
        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
        )
        materialization = ToolRegistryRuntime(default_tool_registry()).materialize(
            worker_request_id="worker-test",
            session_id="session-test",
            workspace_root=ROOT,
        )
        tool_context = ToolUseContext.for_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            session_id="session-test",
            turn_id="turn-test",
            turn_index=1,
            materialization=materialization,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            result = ToolResult(
                tool_call_id=plan.requests[0].call.tool_call_id,
                ok=True,
                summary="Read large.txt",
                output={"content": "x" * 500, "path": "large.txt"},
            )

            receipt = ToolResultBudgetRuntime(max_result_chars=1000, max_turn_chars=120).apply(
                request=plan.requests[0],
                result=result,
                context=tool_context,
                artifact_store=execution_context.artifact_store,
            )

            self.assertTrue(receipt.decision.applied)
            self.assertEqual(receipt.decision.reason, "turn_tool_result_budget_exceeded")
            self.assertTrue(receipt.bounded_result.output["turn_budget_overflow"])
            self.assertTrue(Path(receipt.bounded_result.artifacts[-1].uri).exists())

    def test_budget_policy_runtime_builds_ledger_from_real_receipts(self) -> None:
        state = create_task_state("Policy budget.")
        scheduler = ToolLoopScheduler(default_tool_registry())
        plan = scheduler.plan_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            turn_index=1,
            steps=[{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
        )
        materialization = ToolRegistryRuntime(default_tool_registry()).materialize(
            worker_request_id="worker-test",
            session_id="session-test",
            workspace_root=ROOT,
        )
        tool_context = ToolUseContext.for_turn(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_request_id="worker-test",
            session_id="session-test",
            turn_id="turn-test",
            turn_index=1,
            materialization=materialization,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=Path(tmpdir) / "workspace",
                artifact_root=Path(tmpdir) / "artifacts",
            )
            result = ToolResult(
                tool_call_id=plan.requests[0].call.tool_call_id,
                ok=True,
                summary="Read large.txt",
                output={"content": "x" * 500, "path": "large.txt"},
            )
            receipt = ToolResultBudgetRuntime(max_result_chars=120, max_turn_chars=180).apply(
                request=plan.requests[0],
                result=result,
                context=tool_context,
                artifact_store=execution_context.artifact_store,
            )

            report = ToolBudgetPolicyRuntime(
                tool_result_limit=120,
                turn_limit=180,
                session_limit=240,
            ).build_report(
                session_id="session-test",
                worker_request_id="worker-test",
                receipts=[receipt.to_dict()],
                context_snapshots=[tool_context.to_dict(include_messages=False)],
            )

            self.assertTrue(report.ok)
            self.assertEqual(report.externalized_count, 3)
            self.assertEqual(report.highest_pressure, ToolBudgetPressure.EXCEEDED)
            self.assertEqual(report.metadata()["tool_budget_policy_ok"], "true")
            self.assertGreaterEqual(len(report.ledger), 3)

    def test_streaming_runtime_wraps_real_batch_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Stream tool execution.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "stream.txt").write_text("stream payload", encoding="utf-8")
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            scheduler = ToolLoopScheduler(execution_context.registry)
            plan = scheduler.plan_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                turn_index=1,
                steps=[{"tool_name": "file_read", "arguments": {"path": "stream.txt"}}],
            )
            materialization = ToolRegistryRuntime(execution_context.registry).materialize(
                worker_request_id="worker-test",
                session_id="session-test",
                workspace_root=workspace,
            )
            tool_context = ToolUseContext.for_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                session_id="session-test",
                turn_id="turn-test",
                turn_index=1,
                materialization=materialization,
            )
            streaming = ToolStreamingRuntime()

            trace = streaming.execute_batch(
                ToolExecutionRuntime(
                    execution_context,
                    scheduler=scheduler,
                    budget_runtime=ToolResultBudgetRuntime(max_result_chars=8000),
                ),
                plan.batches[0],
                tool_context=tool_context,
                max_workers=1,
            )
            report = streaming.build_report(
                session_id="session-test",
                worker_request_id="worker-test",
                traces=[trace],
            )

            kinds = {frame.kind for frame in trace.frames}
            self.assertTrue(trace.ok)
            self.assertEqual(len(trace.receipts), 1)
            self.assertIn(ToolStreamFrameKind.TOOL_DISPATCHED, kinds)
            self.assertIn(ToolStreamFrameKind.TOOL_COMPLETED, kinds)
            self.assertIn(ToolStreamFrameKind.CONTEXT_MODIFIER_APPLIED, kinds)
            self.assertTrue(report.ok)
            self.assertGreaterEqual(report.frame_count, 5)
            self.assertEqual(len(streaming.events_for_trace(trace, run_id=state.run_id, task_id=state.task_id, node_id=None)), trace.frame_count)

    def test_continuation_runtime_pairs_receipts_with_context_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Continuation pairing.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "pair.txt").write_text("pair payload", encoding="utf-8")
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            scheduler = ToolLoopScheduler(execution_context.registry)
            plan = scheduler.plan_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                turn_index=1,
                steps=[{"tool_name": "file_read", "arguments": {"path": "pair.txt"}}],
            )
            materialization = ToolRegistryRuntime(execution_context.registry).materialize(
                worker_request_id="worker-test",
                session_id="session-test",
                workspace_root=workspace,
            )
            tool_context = ToolUseContext.for_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                session_id="session-test",
                turn_id="turn-test",
                turn_index=1,
                materialization=materialization,
            )
            trace = ToolStreamingRuntime().execute_batch(
                ToolExecutionRuntime(
                    execution_context,
                    scheduler=scheduler,
                    budget_runtime=ToolResultBudgetRuntime(max_result_chars=8000),
                ),
                plan.batches[0],
                tool_context=tool_context,
                max_workers=1,
            )

            report = ToolContinuationRuntime().build_report(
                session_id="session-test",
                worker_request_id="worker-test",
                receipts=[receipt.to_dict() for receipt in trace.receipts],
                context_snapshots=[tool_context.to_dict(include_messages=True)],
            )

            self.assertTrue(report.ok)
            self.assertEqual(report.status, ToolContinuationStatus.READY)
            self.assertEqual(report.pair_count, 1)
            self.assertEqual(report.pairs[0].mode, ToolContinuationMode.INLINE_RESULT)
            self.assertTrue(report.pairs[0].result_message_appended)
            self.assertEqual(report.metadata()["tool_continuation_ready"], "1")

    def test_concurrency_runtime_validates_real_read_only_batch_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Concurrency report.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "a.txt").write_text("a", encoding="utf-8")
            (workspace / "b.txt").write_text("b", encoding="utf-8")
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            scheduler = ToolLoopScheduler(execution_context.registry, max_read_only_concurrency=4)
            plan = scheduler.plan_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                turn_index=1,
                steps=[
                    {"tool_name": "file_read", "arguments": {"path": "a.txt"}},
                    {"tool_name": "file_read", "arguments": {"path": "b.txt"}},
                ],
            )
            materialization = ToolRegistryRuntime(execution_context.registry).materialize(
                worker_request_id="worker-test",
                session_id="session-test",
                workspace_root=workspace,
            )
            tool_context = ToolUseContext.for_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                session_id="session-test",
                turn_id="turn-test",
                turn_index=1,
                materialization=materialization,
            )
            trace = ToolStreamingRuntime().execute_batch(
                ToolExecutionRuntime(
                    execution_context,
                    scheduler=scheduler,
                    budget_runtime=ToolResultBudgetRuntime(max_result_chars=8000),
                ),
                plan.batches[0],
                tool_context=tool_context,
                max_workers=4,
            )

            report = ToolConcurrencyRuntime().build_report(
                session_id="session-test",
                worker_request_id="worker-test",
                traces=[trace],
            )

            self.assertTrue(report.ok)
            self.assertEqual(report.concurrent_batch_count, 1)
            self.assertEqual(report.observations[0].mode, ToolConcurrencyMode.CONCURRENT_READ_ONLY)
            self.assertEqual(report.observations[0].read_only_count, 2)

    def test_failure_policy_routes_budget_externalization_from_real_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Failure policy.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "large.txt").write_text("x" * 500, encoding="utf-8")
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            scheduler = ToolLoopScheduler(execution_context.registry)
            plan = scheduler.plan_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                turn_index=1,
                steps=[{"tool_name": "file_read", "arguments": {"path": "large.txt"}}],
            )
            materialization = ToolRegistryRuntime(execution_context.registry).materialize(
                worker_request_id="worker-test",
                session_id="session-test",
                workspace_root=workspace,
            )
            tool_context = ToolUseContext.for_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                session_id="session-test",
                turn_id="turn-test",
                turn_index=1,
                materialization=materialization,
            )
            trace = ToolStreamingRuntime().execute_batch(
                ToolExecutionRuntime(
                    execution_context,
                    scheduler=scheduler,
                    budget_runtime=ToolResultBudgetRuntime(max_result_chars=80),
                ),
                plan.batches[0],
                tool_context=tool_context,
                max_workers=1,
            )

            report = ToolFailurePolicyRuntime().build_report(
                session_id="session-test",
                worker_request_id="worker-test",
                receipts=[receipt.to_dict() for receipt in trace.receipts],
                failure_signals=[
                    receipt.failure_signal.to_dict()
                    for receipt in trace.receipts
                    if receipt.failure_signal is not None
                ],
            )

            self.assertTrue(report.ok)
            self.assertEqual(report.budget_count, 1)
            self.assertEqual(report.decisions[0].route, ToolFailureRoute.ARTIFACT_EXTERNALIZED)

    def test_output_store_persists_real_tool_receipt_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = create_task_state("Output store.")
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            (workspace / "store.txt").write_text("stored payload", encoding="utf-8")
            execution_context = ToolExecutionContext.for_workspace(
                workspace_root=workspace,
                artifact_root=Path(tmpdir) / "artifacts",
            )
            scheduler = ToolLoopScheduler(execution_context.registry)
            plan = scheduler.plan_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                turn_index=1,
                steps=[{"tool_name": "file_read", "arguments": {"path": "store.txt"}}],
            )
            materialization = ToolRegistryRuntime(execution_context.registry).materialize(
                worker_request_id="worker-test",
                session_id="session-test",
                workspace_root=workspace,
            )
            tool_context = ToolUseContext.for_turn(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                worker_request_id="worker-test",
                session_id="session-test",
                turn_id="turn-test",
                turn_index=1,
                materialization=materialization,
            )
            trace = ToolStreamingRuntime().execute_batch(
                ToolExecutionRuntime(
                    execution_context,
                    scheduler=scheduler,
                    budget_runtime=ToolResultBudgetRuntime(max_result_chars=8000),
                ),
                plan.batches[0],
                tool_context=tool_context,
                max_workers=1,
            )
            runtime = ToolOutputStoreRuntime()
            snapshot = runtime.build_snapshot(
                session_id="session-test",
                worker_request_id="worker-test",
                receipts=[receipt.to_dict() for receipt in trace.receipts],
            )
            stored = runtime.write_snapshot(
                execution_context.artifact_store,
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                snapshot=snapshot,
            )

            self.assertEqual(snapshot.status, ToolOutputStoreStatus.READY)
            self.assertEqual(snapshot.entry_count, 1)
            self.assertTrue(stored.artifact)
            self.assertTrue(Path(stored.artifact.uri).exists())
            self.assertEqual(runtime.resolve_entry(snapshot, trace.receipts[0].request.call.tool_call_id).tool_name, "file_read")

    def test_source_coverage_runtime_requires_claude_opencode_and_hermes_rows(self) -> None:
        report = ToolSourceCoverageRuntime().build_report(
            session_id="session-test",
            worker_request_id="worker-test",
        )

        self.assertTrue(report.ok)
        self.assertGreaterEqual(report.repo_count, 3)
        self.assertEqual(report.matched_count, report.requirement_count)
        self.assertIn("opencode", report.metadata()["tool_source_coverage_repos"])
        self.assertIn("hermes-agent", report.metadata()["tool_source_coverage_repos"])

    def test_source_coverage_runtime_blocks_explicitly_empty_source_rows(self) -> None:
        report = ToolSourceCoverageRuntime().build_report(
            session_id="session-test",
            worker_request_id="worker-test",
            source_rows=[],
        )

        self.assertFalse(report.ok)
        self.assertEqual(report.status, ToolSourceCoverageStatus.BLOCKED)
        self.assertEqual(report.matched_count, 0)
        self.assertIn("TOOL_SOURCE_REQUIRED_REPO_MISSING", {finding.code for finding in report.findings})

    def test_contract_gate_blocks_missing_required_component(self) -> None:
        runtime = ToolContractGateRuntime(required_components=("streaming", "continuation"))

        report = runtime.build_report(
            session_id="session-test",
            worker_request_id="worker-test",
            reports={
                "streaming": {"ok": True, "status": "pass", "report_id": "streaming-test"},
                "continuation": {"ok": False, "status": "blocked", "report_id": "continuation-test"},
            },
        )

        self.assertFalse(report.ok)
        self.assertEqual(report.status, ToolContractGateStatus.BLOCKED)
        self.assertEqual(report.blocking_count, 1)
        self.assertEqual(report.metadata()["tool_contract_gate_blockers"], "1")

    def test_cleanroom_runtime_blocks_vendor_and_relative_source_paths(self) -> None:
        runtime = ToolCleanroomRuntime()

        clean = runtime.build_report(
            session_id="session-test",
            worker_request_id="worker-test",
            target_paths=["packages/runtime/zyra_runtime/tool_runtime_foundation.py"],
        )
        clean_without_default_rows = runtime.build_report(
            session_id="session-test",
            worker_request_id="worker-test",
            target_paths=["packages/runtime/zyra_runtime/tool_runtime_foundation.py"],
            source_rows=[],
        )
        blocked = runtime.build_report(
            session_id="session-test",
            worker_request_id="worker-test",
            target_paths=["../claude-code-best/src/tools.ts", "vendor/tool_runtime.py"],
            source_rows=[],
        )

        self.assertEqual(clean.status, ToolCleanroomStatus.PASS)
        self.assertGreater(clean.check_count, 1)
        self.assertEqual(clean_without_default_rows.check_count, 1)
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.status, ToolCleanroomStatus.BLOCKED)

    def test_foundation_runtime_disable_is_not_a_noop(self) -> None:
        with self.assertRaises(ToolRuntimeDisabledError):
            ToolRegistryRuntime(default_tool_registry(), disabled=True).materialize(
                worker_request_id="worker-test",
                session_id="session-test",
                workspace_root=ROOT,
            )


def _query_event(phase: str) -> EventRecord:
    return EventRecord(
        run_id="run-test",
        task_id="task-test",
        node_id=None,
        event_type=EventType.AGENT_MESSAGE,
        payload={"query_session": {"phase": phase, "session_id": "session-test", "worker_request_id": "worker-test"}},
    )


if __name__ == "__main__":
    unittest.main()
