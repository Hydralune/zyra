from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from zyra_commands.runtime import (
    CommandConcurrency,
    CommandMutationScope,
    CommandOrigin,
    CommandSource,
    CommandSourceKind,
    CommandStatus,
    ControlCommandDescriptor,
    ControlCommandRegistry,
    ControlCommandRequest,
    ControlErrorCode,
    ControlRequestStore,
    ControlResult,
    PromptQueueEntry,
    PromptQueueRuntime,
    QueueEntryKind,
    QueuePriority,
    RuntimeControlContext,
    RuntimeControlDispatcher,
    SideQuestionContextSnapshot,
    SideQuestionProviderResult,
    SideQuestionRuntime,
    SideQuestionUsage,
    command_source_audit,
    default_control_command_registry,
)
from zyra_core import EventType
from zyra_runtime import ToolCall, ToolExecutionContext, ToolExecutor, default_tool_registry
from zyra_skills.fork_scope import _selector_matches
from zyra_skills.models import ToolSelector
from zyra_skills.policy import ToolUseIdentity
from zyra_workers.subagents import (
    AgentContextMode,
    AgentExecutionMode,
    LogicalWorkspaceIsolationPort,
    PermissionMode,
    SubagentBudgetReservationStore,
    SubagentControlAction,
    SubagentControlRequest,
    SubagentControlRuntime,
    SubagentExecutionResult,
    SubagentRuntime,
    SubagentRuntimeConfig,
    SubagentSpawnRequest,
    UsageBudget,
    UsageLedger,
    audit_subagent_sources,
)


class FakeExecutionPort:
    def __init__(self, *, ok: bool = True, wait: threading.Event | None = None) -> None:
        self.ok = ok
        self.wait = wait
        self.requests = []
        self.cancelled = []
        self.started = threading.Event()

    def execution_ref(self, request):
        return f"fake:{request.task_id}:{request.dispatch_id}"

    def execute(self, request, *, cancel_check, progress=None):
        self.requests.append(request)
        self.started.set()
        if progress:
            progress({"phase": "fake_started", "tool_name": "file_read"})
        if self.wait is not None:
            self.wait.wait(timeout=2)
        if cancel_check():
            raise RuntimeError("cancelled")
        return SubagentExecutionResult(
            task_id=request.task_id,
            execution_ref=self.execution_ref(request),
            ok=self.ok,
            summary="bounded child result" if self.ok else "child failed",
            usage=UsageLedger(turns=1, tool_calls=1, input_tokens=20, output_tokens=10, result_chars=20),
            error_code="" if self.ok else "fake_failure",
            error_message="" if self.ok else "fake failure",
            metadata={"fixture": False, "semantic_execution": True},
        )

    def cancel(self, execution_ref):
        self.cancelled.append(execution_ref)
        return True


class ToolRequestingSideProvider:
    def ask(self, question, snapshot, *, tools, max_turns, cache_write):
        self.received = {
            "tools": tools,
            "max_turns": max_turns,
            "cache_write": cache_write,
        }
        return SideQuestionProviderResult(
            answer="should be rejected",
            usage=SideQuestionUsage(tool_calls=1),
            tool_requests=({"name": "file_read"},),
        )


class SubagentCommandFoundationTests(unittest.TestCase):
    def test_dynamic_registry_audits_collision_and_alias(self) -> None:
        registry = default_control_command_registry()
        lower = ControlCommandDescriptor(
            canonical_name="/status",
            description="project status",
            source=CommandSource(CommandSourceKind.PROJECT, "project"),
            handler_id="project.status",
        )
        snapshot = registry.replace_source("project", [lower])
        generation = registry.generation
        registry.replace_source("project", [lower])
        self.assertEqual(generation, registry.generation)
        self.assertEqual("task.status", registry.require("/status").handler_id)
        self.assertTrue(any(item.name == "/status" for item in snapshot.collisions))
        self.assertEqual("/change", registry.require("/需求变更").canonical_name)

    def test_dispatcher_persists_idempotent_read_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = default_control_command_registry()
            store = ControlRequestStore(root / "requests.json")
            events = []
            dispatcher = RuntimeControlDispatcher(
                registry=registry,
                request_store=store,
                prompt_queue=PromptQueueRuntime(root / "queue.json"),
            )
            context = RuntimeControlContext(
                handlers={"task.status": lambda request, descriptor, context: ControlResult(display_text="live", data={"request": request.request_id})},
                event_sink=events.append,
            )
            request = ControlCommandRequest(
                run_id="run",
                task_id="task",
                session_id="session",
                canonical_name="/status",
                idempotency_key="stable-status",
                registry_generation=registry.generation,
            )
            first = dispatcher.submit(request, context)
            second = dispatcher.submit(request, context)
            self.assertTrue(first.ok)
            self.assertEqual(first.to_dict(), second.to_dict())
            self.assertEqual(CommandStatus.SUCCEEDED, store.get(request.request_id).status)
            self.assertTrue((root / "requests.json").is_file())
            event_types = {item.event_type for item in events}
            self.assertIn(EventType.COMMAND_REQUESTED, event_types)
            self.assertIn(EventType.COMMAND_SUCCEEDED, event_types)

    def test_dispatcher_rejects_stale_registry_and_missing_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = default_control_command_registry()
            dispatcher = RuntimeControlDispatcher(
                registry=registry,
                request_store=ControlRequestStore(root / "requests.json"),
                prompt_queue=PromptQueueRuntime(root / "queue.json"),
            )
            stale = ControlCommandRequest(
                run_id="run", task_id="task", session_id="session", canonical_name="/status",
                registry_generation=999,
            )
            response = dispatcher.submit(stale, RuntimeControlContext())
            self.assertEqual(ControlErrorCode.REGISTRY_STALE, response.error.code)

            request = ControlCommandRequest(
                run_id="run", task_id="task", session_id="session", canonical_name="/compact",
                registry_generation=registry.generation,
            )
            response = dispatcher.submit(
                request,
                RuntimeControlContext(permission_authorize=lambda request, descriptor: True),
            )
            self.assertEqual(ControlErrorCode.STATE_OWNER_UNAVAILABLE, response.error.code)
            self.assertFalse(response.ok)
            self.assertFalse(response.metadata["event_only_fallback"])

    def test_mutating_command_checkpoints_and_changes_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = default_control_command_registry()
            revision = {"value": 4}
            dispatcher = RuntimeControlDispatcher(
                registry=registry,
                request_store=ControlRequestStore(root / "requests.json"),
                prompt_queue=PromptQueueRuntime(root / "queue.json"),
            )

            def handler(request, descriptor, context):
                revision["value"] += 1
                return ControlResult(display_text="compacted", data={"changed": True})

            request = ControlCommandRequest(
                run_id="run", task_id="task", session_id="session", canonical_name="/compact",
                registry_generation=registry.generation, expected_session_revision=4,
            )
            response = dispatcher.submit(request, RuntimeControlContext(
                handlers={"session.compact": handler},
                session_revision=lambda session: revision["value"],
                checkpoint=lambda request, descriptor: "checkpoint:before",
                permission_authorize=lambda request, descriptor: True,
            ))
            self.assertTrue(response.ok)
            self.assertEqual(4, response.revision_before)
            self.assertEqual(5, response.revision_after)
            self.assertEqual("checkpoint:before", response.result.checkpoint_ref)

    def test_prompt_queue_isolates_main_and_child_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            queue = PromptQueueRuntime(Path(temp) / "queue.json")
            main = queue.enqueue(PromptQueueEntry(
                session_id="session", kind=QueueEntryKind.PROMPT, payload={"text": "main"},
                priority=QueuePriority.NEXT, origin=CommandOrigin.API, idempotency_key="main",
            ))
            child = queue.enqueue(PromptQueueEntry(
                session_id="session", kind=QueueEntryKind.PROMPT, payload={"text": "child"},
                priority=QueuePriority.NOW, origin=CommandOrigin.API,
                target_subagent_task_id="child-1", idempotency_key="child",
            ))
            main_claim = queue.reserve_next(session_id="session")
            child_claim = queue.reserve_next(session_id="session", target_subagent_task_id="child-1")
            self.assertEqual(main.queue_id, main_claim.queue_id)
            self.assertEqual(child.queue_id, child_claim.queue_id)
            self.assertNotEqual(main_claim.claim_token, child_claim.claim_token)

    def test_side_question_is_tool_free_and_does_not_mutate_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            events = []
            messages = [{"message_id": "m1", "content": "main"}]
            snapshot = SideQuestionContextSnapshot(
                parent_session_id="session", parent_session_revision=3, context_epoch=1,
                compact_boundary_id="compact", message_ids=("m1",), system_prompt_digest="s",
                user_context_digest="u", model="test", thinking="off", cache_prefix_digest="cache",
                metadata={"context_summary": "main remains stable"},
            )
            runtime = SideQuestionRuntime(Path(temp) / "side", event_sink=events.append)
            result = runtime.ask(
                run_id="run", task_id="task", question="What is current status?", snapshot=snapshot,
                parent_messages_before=messages, parent_messages_after=lambda: messages,
            )
            self.assertTrue(result.ok)
            self.assertFalse(result.parent_messages_mutated)
            self.assertFalse(result.main_replan_triggered)
            self.assertTrue(Path(result.transcript_ref).is_file())
            self.assertEqual({EventType.SIDE_QUESTION}, {item.event_type for item in events})

    def test_side_question_records_tool_violation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            provider = ToolRequestingSideProvider()
            events = []
            snapshot = SideQuestionContextSnapshot(
                parent_session_id="session", parent_session_revision=1, context_epoch=0,
                compact_boundary_id="", message_ids=(), system_prompt_digest="s",
                user_context_digest="u", model="test", thinking="off", cache_prefix_digest="",
            )
            result = SideQuestionRuntime(Path(temp), provider=provider, event_sink=events.append).ask(
                run_id="run", task_id="task", question="read a file", snapshot=snapshot,
                parent_messages_before=[], parent_messages_after=lambda: [],
            )
            self.assertFalse(result.ok)
            self.assertTrue(result.tool_violation)
            self.assertEqual((), provider.received["tools"])
            self.assertEqual(1, provider.received["max_turns"])
            self.assertFalse(provider.received["cache_write"])
            self.assertTrue(any(item.payload.get("phase") == "tool_denied" for item in events))

    def test_subagent_foreground_lifecycle_has_structured_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            events = []
            execution = FakeExecutionPort()
            runtime = SubagentRuntime(
                SubagentRuntimeConfig.from_paths(state_root=root / "state", workspace_root=workspace, artifact_root=root / "artifacts"),
                execution_port=execution,
                isolation_port=LogicalWorkspaceIsolationPort(),
                parent_registry=default_tool_registry(),
                event_sink=events.append,
            )
            request = SubagentSpawnRequest(
                run_id="run", parent_task_id="parent", parent_session_id="parent-session",
                parent_worker_request_id="worker-request", agent_type="explore", prompt="Inspect only the bounded scope.",
                parent_tools=tuple(item.name for item in default_tool_registry().list()),
                parent_permission_mode=PermissionMode.DEFAULT,
                requested_tools=("file_read", "trace"),
                context_mode=AgentContextMode.ISOLATED,
                execution_mode=AgentExecutionMode.FOREGROUND,
                workspace_root=str(workspace),
                context_payload={"messages": [{"role": "user", "content": "parent scratchpad must not leak"}]},
                constraints={"parent_budget": UsageBudget(max_children=2).to_dict()},
                task_id="child-1",
                metadata={"root_task_id": "root-task"},
            )
            result = runtime.spawn(request)
            self.assertTrue(result.ok)
            self.assertEqual("completed", result.lifecycle.record.status.value)
            self.assertEqual(("file_read", "trace"), result.lifecycle.record.tool_scope.child_tools)
            self.assertFalse(result.lifecycle.record.permission.exact_grants_inherited)
            self.assertIsNotNone(result.lifecycle.handoff)
            self.assertEqual("child-1", result.lifecycle.handoff.task_id)
            self.assertTrue(Path(result.lifecycle.handoff.transcript_ref).is_file())
            event_types = {item.event_type for item in events}
            self.assertIn(EventType.SUBAGENT_DISPATCHED, event_types)
            self.assertIn(EventType.SUBAGENT_COMPLETED, event_types)
            persisted = json.loads((root / "state" / "tasks.json").read_text(encoding="utf-8"))
            for task in persisted["tasks"]:
                self.assertNotIn("worker_id", task)
                self.assertNotIn("lease_id", task)
                self.assertNotIn("capacity", task)
                self.assertNotIn("heartbeat", task)

    def test_subagent_permission_and_tools_cannot_expand(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            runtime = SubagentRuntime(
                SubagentRuntimeConfig.from_paths(state_root=root / "state", workspace_root=workspace, artifact_root=root / "artifacts"),
                execution_port=FakeExecutionPort(), isolation_port=LogicalWorkspaceIsolationPort(),
                parent_registry=default_tool_registry(),
            )
            request = SubagentSpawnRequest(
                run_id="run", parent_task_id="parent", parent_session_id="session", parent_worker_request_id="worker",
                agent_type="explore", prompt="try expansion", parent_tools=("file_read",),
                parent_permission_mode=PermissionMode.DEFAULT, requested_tools=("file_read", "file_write"),
                requested_permission_mode=PermissionMode.BYPASS, workspace_root=str(workspace), task_id="child",
            )
            with self.assertRaises(Exception):
                runtime.spawn(request)

    def test_background_subagent_cancel_is_durable_and_cooperative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()
            release = threading.Event()
            execution = FakeExecutionPort(wait=release)
            runtime = SubagentRuntime(
                SubagentRuntimeConfig.from_paths(state_root=root / "state", workspace_root=workspace, artifact_root=root / "artifacts"),
                execution_port=execution,
                isolation_port=LogicalWorkspaceIsolationPort(),
                parent_registry=default_tool_registry(),
            )
            spawned = runtime.spawn(SubagentSpawnRequest(
                run_id="run", parent_task_id="parent", parent_session_id="session", parent_worker_request_id="worker",
                agent_type="explore", prompt="wait for cancellation",
                parent_tools=tuple(item.name for item in default_tool_registry().list()),
                requested_tools=("file_read",), execution_mode=AgentExecutionMode.BACKGROUND,
                workspace_root=str(workspace), task_id="background-child",
            ))
            self.assertTrue(spawned.background)
            self.assertTrue(execution.started.wait(timeout=2))
            running = runtime.task_store.get("background-child")
            execution_ref = running.execution_ref
            control_request = SubagentControlRequest(
                root_task_id="parent",
                subagent_task_id="background-child",
                action=SubagentControlAction.MESSAGE,
                arguments={"message": "report progress at the next safe boundary"},
                idempotency_key="message-background-once",
                expected_task_revision=running.revision,
            )
            message = runtime.control_runtime.execute(control_request)
            replayed_message = runtime.control_runtime.execute(control_request)
            restarted_message = SubagentControlRuntime(
                runtime, root / "state" / "controls.json"
            ).execute(control_request)
            self.assertTrue(message.ok)
            self.assertEqual(message.to_dict(), replayed_message.to_dict())
            self.assertEqual(message.to_dict(), restarted_message.to_dict())
            self.assertEqual(1, len(runtime.task_store.get("background-child").pending_messages))
            cancelled = runtime.cancel_for_parent("parent", reason="parent_cancelled")
            release.set()
            runtime.wait("background-child", timeout=3)
            final = runtime.task_store.get("background-child")
            self.assertTrue(cancelled)
            self.assertEqual("cancelled", final.status.value)
            self.assertEqual(execution_ref, final.execution_ref)

    def test_shared_budget_reservation_prevents_oversell(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = SubagentBudgetReservationStore(Path(temp) / "budget.json")
            store.set_parent_limit("parent", UsageBudget(max_turns=10, max_tool_calls=10, max_children=2))
            store.reserve(parent_task_id="parent", child_task_id="a", requested=UsageBudget(max_turns=7, max_tool_calls=6, max_children=1), idempotency_key="a")
            with self.assertRaises(Exception):
                store.reserve(parent_task_id="parent", child_task_id="b", requested=UsageBudget(max_turns=7, max_tool_calls=6, max_children=1), idempotency_key="b")

    def test_tool_executor_cooperatively_cancels_before_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = ToolExecutionContext.for_workspace(
                root / "workspace", root / "artifacts",
                runtime_services={"cancellation_check": lambda: True},
            )
            call = ToolCall(
                run_id="run", task_id="task", tool_name="file_write",
                arguments={"path": "blocked.txt", "content": "must not exist"},
            )
            result = ToolExecutor(context).execute(call)
            self.assertFalse(result.ok)
            self.assertEqual("parent_cancelled", result.error)
            self.assertFalse((root / "workspace" / "blocked.txt").exists())

    def test_fork_selector_missing_path_or_domain_fails_closed(self) -> None:
        identity = ToolUseIdentity(namespace="tool", name="web_search", operation="execute")
        path_selector = ToolSelector(namespace="tool", name="*", path_prefixes=("src/",))
        domain_selector = ToolSelector(namespace="tool", name="*", domains=("example.com",))
        self.assertFalse(_selector_matches(path_selector, identity))
        self.assertFalse(_selector_matches(domain_selector, identity))

    def test_source_audits_record_real_migration_dispositions(self) -> None:
        commands = command_source_audit()
        subagents = audit_subagent_sources(Path(__file__).resolve().parents[2]).to_dict()
        self.assertFalse(commands["event_only_stateful_fallback_allowed"])
        self.assertGreaterEqual(commands["counts"]["zyra_module_migrated"], 3)
        self.assertTrue(subagents["decisions"])
        self.assertTrue(all(item["target_module"] for item in subagents["decisions"]))

    def test_disabling_each_core_owner_breaks_real_task_or_control_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir()

            def request(task_id: str) -> SubagentSpawnRequest:
                return SubagentSpawnRequest(
                    run_id="run", parent_task_id="parent", parent_session_id="session",
                    parent_worker_request_id="worker", agent_type="explore", prompt="must fail",
                    parent_tools=tuple(item.name for item in default_tool_registry().list()),
                    requested_tools=("file_read",), workspace_root=str(workspace), task_id=task_id,
                )

            disabled_runtime = SubagentRuntime(
                SubagentRuntimeConfig.from_paths(
                    state_root=root / "disabled-runtime", workspace_root=workspace,
                    artifact_root=root / "artifacts", disabled=True,
                ),
                execution_port=FakeExecutionPort(), isolation_port=LogicalWorkspaceIsolationPort(),
                parent_registry=default_tool_registry(),
            )
            with self.assertRaises(RuntimeError):
                disabled_runtime.spawn(request("disabled-runtime"))

            disabled_isolation = SubagentRuntime(
                SubagentRuntimeConfig.from_paths(
                    state_root=root / "disabled-isolation", workspace_root=workspace, artifact_root=root / "artifacts",
                ),
                execution_port=FakeExecutionPort(), isolation_port=LogicalWorkspaceIsolationPort(disabled=True),
                parent_registry=default_tool_registry(),
            )
            with self.assertRaises(RuntimeError):
                disabled_isolation.spawn(request("disabled-isolation"))

            disabled_lifecycle = SubagentRuntime(
                SubagentRuntimeConfig.from_paths(
                    state_root=root / "disabled-lifecycle", workspace_root=workspace, artifact_root=root / "artifacts",
                ),
                execution_port=FakeExecutionPort(), isolation_port=LogicalWorkspaceIsolationPort(),
                parent_registry=default_tool_registry(),
            )
            disabled_lifecycle.lifecycle.disabled = True
            with self.assertRaises(RuntimeError):
                disabled_lifecycle.spawn(request("disabled-lifecycle"))

            registry = ControlCommandRegistry(disabled=True)
            with self.assertRaises(RuntimeError):
                registry.list()

            active_registry = default_control_command_registry()
            dispatcher = RuntimeControlDispatcher(
                registry=active_registry,
                request_store=ControlRequestStore(root / "disabled-dispatcher.json"),
                prompt_queue=PromptQueueRuntime(root / "disabled-queue.json"),
                disabled=True,
            )
            with self.assertRaises(RuntimeError):
                dispatcher.submit(ControlCommandRequest(
                    run_id="run", task_id="task", session_id="session", canonical_name="/status",
                    registry_generation=active_registry.generation,
                ), RuntimeControlContext())


if __name__ == "__main__":
    unittest.main()
