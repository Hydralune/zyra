from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from zyra_commands.runtime import (
    CallableCommandSourceProvider,
    CommandRegistryCoordinator,
    CommandSourceKind,
    ControlFrameStore,
    SourceRefreshStatus,
    default_control_command_registry,
)
from zyra_runtime import ToolCall, ToolRegistry, default_tool_registry
from zyra_workers.subagents import (
    AgentContextMode,
    AgentExecutionMode,
    AgentToolParentContext,
    AgentToolRuntime,
    ChildScopeDeriver,
    ChildScopeRequest,
    DeliveryIntent,
    DeliveryTopologyViolation,
    DurableDeliveryStore,
    ExecutionIdentity,
    ExecutionPhase,
    ExecutionReceiptStore,
    FanoutFailurePolicy,
    FanoutItem,
    FanoutRequest,
    FanoutStatus,
    FanoutStore,
    LogicalFanoutRuntime,
    LogicalTaskEdge,
    ParentScopeBuilder,
    ParentScopeStore,
    ParentScopeViolation,
    PermissionMode,
    ResumeCapsuleRuntime,
    ResumeCapsuleStore,
    SubagentTranscriptStore,
    TranscriptEntryKind,
    TypedYield,
    TypedYieldStore,
    TypedYieldValidationError,
    YieldContract,
    YieldKind,
)


class _FakeSubagentRuntime:
    def __init__(self) -> None:
        self.requests = []

    def spawn(self, request):
        self.requests.append(request)
        record = SimpleNamespace(
            task_id=request.task_id,
            parent_task_id=request.parent_task_id,
            status=SimpleNamespace(value="running"),
            metadata={"child_session_id": f"subagent:{request.task_id}"},
            handoff=None,
            usage=SimpleNamespace(to_dict=lambda: {"turns": 0}),
            execution_ref="",
            error_message="",
            error_code="",
        )
        return SimpleNamespace(record=record, background=True, replayed=False, ok=True)


class _FanoutChildRuntime:
    def __init__(self) -> None:
        self.spawned = []

    def spawn(self, request):
        self.spawned.append(request.task_id)
        typed = TypedYield(
            yield_id=f"yield-{request.task_id}",
            task_id=request.task_id,
            parent_task_id="task-parent",
            execution_ref=f"worker:{request.task_id}",
            attempt=1,
            sequence=1,
            kind=YieldKind.TERMINAL,
            summary=f"completed {request.task_id}",
            data={"item": request.task_id, "ok": True},
        )
        record = SimpleNamespace(task_id=request.task_id, execution_ref=f"worker:{request.task_id}", attempt=1)
        lifecycle = SimpleNamespace(
            execution_result=SimpleNamespace(metadata={"typed_yield": typed.to_dict()}),
        )
        return SimpleNamespace(record=record, lifecycle=lifecycle, background=False)

    def wait(self, task_id, timeout=None):
        raise AssertionError("synchronous child must not call wait")

    def cancel(self, task_id, *, reason="operator_cancelled"):
        return None

    def get(self, task_id):
        return None


class _SlowFanoutChildRuntime(_FanoutChildRuntime):
    def spawn(self, request):
        time.sleep(0.08)
        return super().spawn(request)


class SubagentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _scope(self, registry: ToolRegistry | None = None):
        store = ParentScopeStore(self.root / "parent-scopes.json")
        return ParentScopeBuilder(store).build(
            run_id="run-1",
            parent_task_id="task-parent",
            parent_session_id="session-1",
            session_revision=3,
            tool_registry=registry or default_tool_registry(),
            tool_generation=7,
            permission_mode=PermissionMode.DEFAULT,
            permission_revision=11,
            permission_rules=(),
            mcp_catalog=({"server_id": "docs", "state": "connected", "generation": 2},),
            model_allowlist=("model-a", "model-b"),
            effective_model="model-a",
            workspace_root=self.root,
            writable_roots=(self.root,),
            readable_roots=(self.root,),
            network_allowed=False,
            skill_refs=("review",),
            hook_refs=("pre-tool",),
            context_epoch=5,
            compact_boundary_id="compact-1",
        )

    def test_signed_parent_scope_rejects_every_authority_expansion(self) -> None:
        parent = self._scope()
        deriver = ChildScopeDeriver()
        with self.assertRaises(ParentScopeViolation) as captured:
            deriver.derive(parent, ChildScopeRequest(
                requested_tools=("file_read", "unknown_tool"),
                requested_mcp_servers=("docs", "unknown-server"),
                requested_permission_mode=PermissionMode.BYPASS,
                requested_model="model-z",
                requested_writable_paths=(str(self.root.parent),),
                requested_network=True,
                requested_skill_refs=("unknown-skill",),
                requested_hook_refs=("unknown-hook",),
                expected_session_revision=2,
                expected_permission_revision=10,
                expected_tool_generation=6,
                expected_mcp_generations={"docs": 1},
            ))
        codes = {item.code for item in captured.exception.findings}
        self.assertTrue({
            "child_tool_not_in_parent",
            "child_mcp_not_in_parent",
            "child_permission_expansion",
            "child_model_not_allowed",
            "child_network_expansion",
        }.issubset(codes))

    def test_agent_tool_ignores_caller_parent_ceiling_and_binds_signed_scope(self) -> None:
        parent = self._scope()
        runtime = _FakeSubagentRuntime()
        tool = AgentToolRuntime(runtime, AgentToolParentContext(
            run_id="run-1",
            task_id="task-parent",
            session_id="session-1",
            worker_request_id="worker-1",
            workspace_root=str(self.root),
            parent_scope=parent,
            context_payload={"artifact_refs": [{"artifact_id": "artifact-1"}]},
            root_task_id="task-parent",
        ))
        binding = tool.bind(default_tool_registry())
        self.assertIsNotNone(binding.registry.get("Agent"))
        result = binding.handlers["Agent"](ToolCall(
            run_id="run-1",
            task_id="task-parent",
            tool_name="Agent",
            arguments={
                "prompt": "Inspect the runtime boundary",
                "execution_mode": "background",
                "tools": ["file_read"],
                "mcp_servers": ["docs"],
                "model_name": "model-a",
                "artifact_refs": ["artifact-1"],
            },
        ))
        self.assertTrue(result.ok)
        request = runtime.requests[0]
        self.assertEqual(request.parent_tools, parent.tool_names)
        self.assertEqual(request.available_mcp_servers, parent.mcp_servers)
        self.assertEqual(request.parent_scope_snapshot_id, parent.snapshot_id)
        self.assertEqual(request.metadata["expected_parent_session_revision"], 3)
        self.assertNotIn("parent_tools", request.constraints)

    def test_agent_tool_cross_task_call_is_rejected(self) -> None:
        parent = self._scope()
        runtime = _FakeSubagentRuntime()
        binding = AgentToolRuntime(runtime, AgentToolParentContext(
            run_id="run-1",
            task_id="task-parent",
            session_id="session-1",
            worker_request_id="worker-1",
            workspace_root=str(self.root),
            parent_scope=parent,
            context_payload={},
            root_task_id="task-parent",
        )).bind(default_tool_registry())
        result = binding.handlers["Task"](ToolCall(
            run_id="run-1",
            task_id="other-task",
            tool_name="Task",
            arguments={"prompt": "escape"},
        ))
        self.assertFalse(result.ok)
        self.assertEqual(runtime.requests, [])

    def test_execution_receipt_claim_is_idempotent_and_restart_parks_unsafe_effect(self) -> None:
        store = ExecutionReceiptStore(self.root / "receipts.json")
        identity = ExecutionIdentity(
            run_id="run-1",
            parent_task_id="task-parent",
            task_id="task-child",
            parent_session_id="session-1",
            request_digest="sha256:request",
            idempotency_key="spawn-1",
        )
        first = store.claim(identity, execution_ref="worker:child")
        replay = store.claim(identity, execution_ref="worker:child")
        self.assertTrue(first.created)
        self.assertTrue(replay.replay)
        self.assertEqual(first.receipt.receipt_id, replay.receipt.receipt_id)
        store.transition(first.receipt.receipt_id, ExecutionPhase.DISPATCHED, attempt_token=first.attempt_token)
        store.transition(first.receipt.receipt_id, ExecutionPhase.STARTED, attempt_token=first.attempt_token)
        store.start_effect(
            first.receipt.receipt_id,
            attempt_token=first.attempt_token,
            effect_identity={"tool_call_id": "call-1", "tool": "file_write"},
        )
        recovered = store.reconcile_after_restart()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].phase, ExecutionPhase.PARKED)
        self.assertTrue(recovered[0].effect_may_have_happened)
        late = store.quarantine_late_result(
            first.receipt.receipt_id,
            attempt=1,
            execution_ref="worker:child",
            result={"ok": True, "effect": "arrived after restart fence"},
            reason="late_after_park",
        )
        self.assertEqual(late.current_phase, ExecutionPhase.PARKED)
        self.assertEqual(store.get(first.receipt.receipt_id).late_result_count, 1)
        self.assertEqual(len(store.late_results(task_id="task-child")), 1)

        cancel_claim = store.claim(ExecutionIdentity(
            run_id="run-1",
            parent_task_id="task-parent",
            task_id="task-cancel",
            parent_session_id="session-1",
            request_digest="sha256:cancel",
            idempotency_key="spawn-cancel",
        ), execution_ref="worker:cancel")
        cancelled = store.cancel(
            cancel_claim.receipt.receipt_id,
            reason="parent_cancelled",
            attempt_token=cancel_claim.attempt_token,
        )
        cancelled_again = store.cancel(
            cancel_claim.receipt.receipt_id,
            reason="duplicate_parent_cancel",
            attempt_token=cancel_claim.attempt_token,
        )
        self.assertEqual(cancelled.phase, ExecutionPhase.CANCELLED)
        self.assertEqual(cancelled_again.revision, cancelled.revision)

    def test_typed_yield_requires_terminal_and_validates_schema(self) -> None:
        store = TypedYieldStore(self.root / "typed-yields.json")
        contract = YieldContract(
            contract_id="contract-1",
            schema={
                "type": "object",
                "required": ["answer"],
                "additionalProperties": False,
                "properties": {"answer": {"type": "string"}},
            },
        )
        store.begin(task_id="task-child", contract=contract, execution_ref="worker:1", attempt=1)
        partial = TypedYield(
            yield_id="yield-1",
            task_id="task-child",
            parent_task_id="task-parent",
            execution_ref="worker:1",
            attempt=1,
            sequence=1,
            kind=YieldKind.PARTIAL,
            summary="working",
            data={"answer": "draft"},
        )
        store.append(partial, idempotency_key="yield-partial")
        with self.assertRaises(TypedYieldValidationError):
            store.require_terminal("task-child")
        terminal = TypedYield(
            yield_id="yield-2",
            task_id="task-child",
            parent_task_id="task-parent",
            execution_ref="worker:1",
            attempt=1,
            sequence=2,
            kind=YieldKind.TERMINAL,
            summary="done",
            data={"answer": "final"},
        )
        assembly = store.append(terminal, idempotency_key="yield-terminal")
        self.assertTrue(assembly.completed)
        self.assertEqual(store.require_terminal("task-child").assembled_data, {"answer": "final"})

    def test_delivery_store_forbids_sibling_and_free_form_routes(self) -> None:
        store = DurableDeliveryStore(self.root / "deliveries.json")
        first = store.bind_edge(LogicalTaskEdge(
            parent_task_id="parent",
            child_task_id="child-a",
            run_id="run-1",
            parent_session_id="parent-session",
            child_session_id="child-a-session",
        ))
        store.bind_edge(LogicalTaskEdge(
            parent_task_id="parent",
            child_task_id="child-b",
            run_id="run-1",
            parent_session_id="parent-session",
            child_session_id="child-b-session",
        ))
        with self.assertRaises(DeliveryTopologyViolation):
            store.enqueue(
                edge_id=first.edge_id,
                sender_task_id="child-a",
                target_task_id="child-b",
                intent=DeliveryIntent.TYPED_YIELD,
                summary="sibling escape",
                payload={"answer": 1},
                idempotency_key="sibling-1",
            )
        accepted = store.enqueue(
            edge_id=first.edge_id,
            sender_task_id="child-a",
            target_task_id="parent",
            intent=DeliveryIntent.TYPED_YIELD,
            summary="bounded result",
            payload={"answer": 1},
            idempotency_key="parent-1",
        )
        self.assertEqual(accepted.target_task_id, "parent")

    def test_same_parent_fanout_two_children_progress_typed_yield_and_fanin(self) -> None:
        child_runtime = _FanoutChildRuntime()
        events = []
        deliveries = []
        runtime = LogicalFanoutRuntime(
            FanoutStore(self.root / "fanout.json"),
            child_runtime,
            TypedYieldStore(self.root / "fanout-yields.json"),
            ExecutionReceiptStore(self.root / "fanout-receipts.json"),
            event_sink=events.append,
            delivery_sink=deliveries.append,
            spawn_request_factory=lambda record, child: SimpleNamespace(task_id=child.task_id),
        )
        request = FanoutRequest(
            run_id="run-1",
            parent_task_id="task-parent",
            parent_session_id="session-1",
            shared_context="Inspect two independent surfaces.",
            items=(
                FanoutItem("item-a", "api", "explore", "Inspect API"),
                FanoutItem("item-b", "runtime", "explore", "Inspect runtime"),
            ),
            idempotency_key="fanout-1",
            execution_mode=AgentExecutionMode.FOREGROUND,
            failure_policy=FanoutFailurePolicy.REQUIRE_ALL,
            maximum_concurrency=2,
            yield_contract=YieldContract(
                contract_id="fanout-contract",
                schema={
                    "type": "object",
                    "required": ["item", "ok"],
                    "properties": {"item": {"type": "string"}, "ok": {"type": "boolean"}},
                },
            ),
        )
        result = runtime.start(request, causation_id="tool-call-parent")
        self.assertEqual(result.record.status, FanoutStatus.COMPLETED)
        self.assertEqual(set(child_runtime.spawned), {item.task_id for item in result.record.children})
        self.assertEqual(result.record.aggregate["success_count"], 2)
        self.assertEqual(len(result.record.aggregate["results"]), 2)
        self.assertGreaterEqual(len(events), 5)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["group_id"], result.record.group_id)

    def test_foreground_fanout_promotes_to_durable_background_and_finishes(self) -> None:
        child_runtime = _SlowFanoutChildRuntime()
        store = FanoutStore(self.root / "promoted-fanout.json")
        runtime = LogicalFanoutRuntime(
            store,
            child_runtime,
            TypedYieldStore(self.root / "promoted-yields.json"),
            ExecutionReceiptStore(self.root / "promoted-receipts.json"),
            spawn_request_factory=lambda record, child: SimpleNamespace(task_id=child.task_id),
        )
        result = runtime.start(FanoutRequest(
            run_id="run-1",
            parent_task_id="task-parent",
            parent_session_id="session-1",
            shared_context="Promote slow foreground work.",
            items=(
                FanoutItem("slow-a", "slow-a", "explore", "Inspect A"),
                FanoutItem("slow-b", "slow-b", "explore", "Inspect B"),
            ),
            idempotency_key="fanout-promote-1",
            execution_mode=AgentExecutionMode.FOREGROUND,
            maximum_concurrency=2,
            promotion_after_ms=10,
            yield_contract=YieldContract(
                contract_id="promoted-contract",
                schema={
                    "type": "object",
                    "required": ["item", "ok"],
                    "properties": {"item": {"type": "string"}, "ok": {"type": "boolean"}},
                },
            ),
        ))
        self.assertTrue(result.detached)
        self.assertTrue(result.handle)
        self.assertEqual(result.record.execution_mode, AgentExecutionMode.BACKGROUND)
        completed = runtime.wait(result.record.group_id, timeout=2)
        self.assertEqual(completed.status, FanoutStatus.COMPLETED)
        self.assertEqual(completed.aggregate["success_count"], 2)

    def test_dynamic_registry_keeps_last_good_on_malformed_reload(self) -> None:
        registry = default_control_command_registry()
        coordinator = CommandRegistryCoordinator(registry, self.root / "command-sources.json")
        values = [{"name": "review-runtime", "description": "Review the runtime"}]
        provider = CallableCommandSourceProvider(
            "project:test",
            CommandSourceKind.PROJECT,
            lambda: values,
            revision_loader=lambda: "1",
        )
        coordinator.register(provider)
        applied = coordinator.refresh("project:test")
        self.assertEqual(applied.status, SourceRefreshStatus.APPLIED)
        self.assertIsNotNone(registry.get("/review-runtime"))
        values[:] = [{"description": "missing name"}]
        failed = coordinator.refresh("project:test")
        self.assertEqual(failed.status, SourceRefreshStatus.FAILED_LAST_GOOD_RETAINED)
        self.assertIsNotNone(registry.get("/review-runtime"))

    def test_evicted_transcript_materializes_from_signed_resume_capsule(self) -> None:
        transcript = SubagentTranscriptStore(self.root / "sidechains")
        transcript.initialize("task-child", context_payload={"objective": "inspect"}, metadata={})
        transcript.append(
            "task-child",
            TranscriptEntryKind.CONTINUATION,
            {"summary": "continue with verification", "message_id": "message-1", "intent": "continue"},
        )
        runtime = ResumeCapsuleRuntime(
            ResumeCapsuleStore(self.root / "capsules.json"),
            transcript,
        )
        context = SimpleNamespace(
            metadata={},
            artifact_refs=("artifact-1",),
            evidence_refs=("evidence-1",),
            context_epoch=3,
            compact_boundary_id="compact-1",
            content_replacement_refs=(),
            invoked_skill_refs=("skill-1",),
        )
        record = SimpleNamespace(
            task_id="task-child",
            parent_task_id="task-parent",
            run_id="run-1",
            parent_session_id="session-1",
            metadata={"child_session_id": "child-session-1"},
            execution_ref="worker:child",
            attempt=1,
            agent_type="verify",
            definition_id="definition-1",
            context_snapshot=context,
            tool_scope=SimpleNamespace(digest="sha256:tools"),
            permission=SimpleNamespace(to_dict=lambda: {"child_mode": "default"}),
        )
        capsule = runtime.create(
            task_record=record,
            execution_receipt={"receipt_id": "receipt-1", "execution_ref": "worker:child", "attempt": 1},
            pending_messages=({"message_id": "pending-1", "intent": "clarify"},),
        )
        evicted = runtime.evict("task-child", capsule_id=capsule.capsule_id)
        self.assertFalse(transcript.path("task-child").exists())
        self.assertEqual(evicted.state.value, "evicted")
        material = runtime.materialize(capsule.capsule_id, idempotency_key="resume-1")
        replay = runtime.materialize(capsule.capsule_id, idempotency_key="resume-1")
        self.assertEqual(material.capsule.capsule_id, replay.capsule.capsule_id)
        self.assertGreaterEqual(len(material.messages), 2)
        self.assertEqual(material.context["context_epoch"], 3)

    def test_control_frame_store_dedupes_and_recovers_dispatched_effect(self) -> None:
        path = self.root / "control-frames.json"
        store = ControlFrameStore(path)
        envelope = {
            "protocol_version": 1,
            "stream_id": "stream-1",
            "message_id": "message-1",
            "correlation_id": "correlation-1",
            "sequence": 1,
            "type": "control_request",
            "payload": {"request": {"request_id": "request-1"}},
        }
        frame = store.receive(envelope)
        replay = store.receive(envelope)
        self.assertEqual(frame.frame_id, replay.frame_id)
        store.mark(frame.frame_id, store.get(frame.frame_id).status.VALIDATED)
        store.mark(frame.frame_id, store.get(frame.frame_id).status.DISPATCHED, request_id="request-1")
        restarted = ControlFrameStore(path)
        recovered = restarted.recover()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status.value, "rejected")
        self.assertEqual(recovered[0].error_code, "control_effect_outcome_unknown_after_restart")


if __name__ == "__main__":
    unittest.main()
