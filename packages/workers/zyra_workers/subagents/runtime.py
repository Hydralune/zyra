from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock, Thread
from typing import Any, Callable, Mapping, Sequence

from zyra_core import EventRecord, new_id, now_iso
from zyra_runtime import LocalArtifactStore, ToolRegistry, default_tool_registry

from .budget import SubagentBudgetReservationStore
from .context import ParentContextInput, SubagentContextFactory
from .continuation import ContinuationRequest, SubagentContinuationRuntime
from .control import SubagentControlRuntime
from .definitions import AgentDefinitionRegistry, default_agent_definition_registry
from .digests import digest_object, stable_id
from .dispatch import SubagentExecutionPort
from .errors import (
    AgentDefinitionNotFound,
    SubagentBudgetExceeded,
    SubagentCycleDetected,
    SubagentDisabled,
)
from .handoff import SubagentHandoffRuntime
from .isolation import SubagentIsolationRequestPort
from .lifecycle import AgentTaskLifecycleRuntime, LifecycleResult
from .models import (
    AgentExecutionMode,
    IsolationCleanupStatus,
    PermissionMode,
    SubagentDispatchRequest,
    SubagentIsolationRequest,
    SubagentRuntimeSnapshot,
    SubagentSpawnRequest,
    SubagentTaskRecord,
    SubagentTaskStatus,
    TranscriptEntryKind,
    UsageBudget,
    UsageLedger,
)
from .task_store import SubagentTaskStore
from .tool_scope import ChildToolScopeRuntime, PermissionDerivationRuntime
from .transcript import SubagentTranscriptStore


@dataclass(frozen=True, slots=True)
class SubagentRuntimeConfig:
    state_root: str
    workspace_root: str
    artifact_root: str
    maximum_active_children_per_parent: int = 4
    default_parent_budget: UsageBudget = field(
        default_factory=lambda: UsageBudget(
            max_turns=64,
            max_tool_calls=256,
            max_input_tokens=256_000,
            max_output_tokens=64_000,
            max_result_chars=1_000_000,
            max_wall_time_ms=3_600_000,
            max_children=8,
            max_depth=4,
        )
    )
    disabled: bool = False

    @classmethod
    def from_paths(
        cls,
        *,
        state_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        disabled: bool = False,
    ) -> "SubagentRuntimeConfig":
        return cls(
            state_root=str(Path(state_root).resolve()),
            workspace_root=str(Path(workspace_root).resolve()),
            artifact_root=str(Path(artifact_root).resolve()),
            disabled=disabled,
        )


@dataclass(frozen=True, slots=True)
class SubagentSpawnResult:
    record: SubagentTaskRecord
    lifecycle: LifecycleResult | None
    background: bool
    accepted_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        if self.background:
            return self.record.status in {SubagentTaskStatus.READY, SubagentTaskStatus.DISPATCHED, SubagentTaskStatus.RUNNING}
        return bool(self.lifecycle and self.lifecycle.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "record": self.record.safe_dict(),
            "lifecycle": self.lifecycle.to_dict() if self.lifecycle else None,
            "background": self.background,
            "accepted_at": self.accepted_at,
        }


class SubagentRuntime:
    """Zyra-owned logical subagent runtime and sole 03D task owner."""

    def __init__(
        self,
        config: SubagentRuntimeConfig,
        *,
        execution_port: SubagentExecutionPort,
        isolation_port: SubagentIsolationRequestPort,
        parent_registry: ToolRegistry | None = None,
        definition_registry: AgentDefinitionRegistry | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        task_store: SubagentTaskStore | None = None,
        transcript_store: SubagentTranscriptStore | None = None,
        budget_store: SubagentBudgetReservationStore | None = None,
        lifecycle_runtime: AgentTaskLifecycleRuntime | None = None,
    ) -> None:
        self.config = config
        state_root = Path(config.state_root).resolve()
        state_root.mkdir(parents=True, exist_ok=True)
        self.parent_registry = parent_registry or default_tool_registry()
        self.definition_registry = definition_registry or default_agent_definition_registry()
        self.task_store = task_store or SubagentTaskStore(state_root / "tasks.json")
        self.transcript_store = transcript_store or SubagentTranscriptStore(state_root / "sidechains")
        self.budget_store = budget_store or SubagentBudgetReservationStore(state_root / "budgets.json")
        self.context_factory = SubagentContextFactory()
        self.tool_scope_runtime = ChildToolScopeRuntime()
        self.permission_runtime = PermissionDerivationRuntime()
        self.isolation_port = isolation_port
        self.execution_port = execution_port
        self.artifact_store = LocalArtifactStore(config.artifact_root)
        self.handoff_runtime = SubagentHandoffRuntime(self.artifact_store)
        self.continuation_runtime = SubagentContinuationRuntime(
            task_store=self.task_store,
            transcript_store=self.transcript_store,
            context_factory=self.context_factory,
        )
        self.control_runtime = SubagentControlRuntime(self, state_root / "controls.json")
        self.event_sink = event_sink
        self.lifecycle = lifecycle_runtime or AgentTaskLifecycleRuntime(
            task_store=self.task_store,
            transcript_store=self.transcript_store,
            budget_store=self.budget_store,
            isolation_port=self.isolation_port,
            execution_port=self.execution_port,
            handoff_runtime=self.handoff_runtime,
            event_sink=event_sink,
        )
        self._lock = RLock()
        self._threads: dict[str, Thread] = {}
        self._background_results: dict[str, LifecycleResult] = {}
        self._cancel_requested: set[str] = set()

    def spawn(self, request: SubagentSpawnRequest) -> SubagentSpawnResult:
        self._require_enabled()
        definition = self.definition_registry.get(request.agent_type)
        if definition is None or not definition.enabled:
            raise AgentDefinitionNotFound(request.agent_type)
        active_children = self.task_store.list(parent_task_id=request.parent_task_id, include_terminal=False)
        child_limit = min(
            self.config.maximum_active_children_per_parent,
            definition.budget.max_children or self.config.maximum_active_children_per_parent,
        )
        if len(active_children) >= child_limit:
            raise SubagentBudgetExceeded("active_children", len(active_children) + 1, child_limit)

        declared_parent_tools = set(request.parent_tools)
        parent_registry = ToolRegistry([
            spec for spec in self.parent_registry.list() if spec.name in declared_parent_tools
        ])
        resolution = self.tool_scope_runtime.resolve(
            parent_registry,
            definition,
            requested_tools=request.requested_tools,
            required_tools=tuple(request.constraints.get("required_tools") or ()),
            allowed_mcp_servers=request.requested_mcp_servers or definition.mcp_servers,
        )
        if not resolution.ok:
            codes = [item.code for item in resolution.findings if item.blocking]
            raise SubagentCycleDetected(tuple(codes), request.agent_type)
        permission = self.permission_runtime.derive(
            parent_mode=request.parent_permission_mode,
            requested_mode=request.requested_permission_mode,
            definition_mode=definition.permission_mode,
            parent_rule_ids=tuple(request.context_payload.get("parent_permission_rule_ids") or ()),
            parent_denials=tuple(request.context_payload.get("parent_permission_denials") or ()),
            available_mcp_servers=request.available_mcp_servers,
            requested_mcp_servers=request.requested_mcp_servers,
            definition_mcp_servers=definition.mcp_servers,
        )
        objective_digest = digest_object({
            "prompt": request.prompt,
            "artifact_refs": request.context_payload.get("artifact_refs") or (),
            "evidence_refs": request.context_payload.get("evidence_refs") or (),
        })
        ancestry = tuple(request.context_payload.get("ancestry") or ())
        parent_context = ParentContextInput(
            parent_session_id=request.parent_session_id,
            parent_task_id=request.parent_task_id,
            parent_worker_request_id=request.parent_worker_request_id,
            messages=tuple(request.context_payload.get("messages") or ()),
            artifacts=tuple(request.context_payload.get("artifact_refs") or ()),
            evidence=tuple(request.context_payload.get("evidence_refs") or ()),
            invoked_skill_refs=tuple(request.context_payload.get("invoked_skill_refs") or ()),
            content_replacement_refs=tuple(request.context_payload.get("content_replacement_refs") or ()),
            context_epoch=int(request.context_payload.get("context_epoch") or 0),
            compact_boundary_id=str(request.context_payload.get("compact_boundary_id") or ""),
            rendered_system_prompt=str(request.context_payload.get("rendered_system_prompt") or ""),
            ancestry=ancestry,
            metadata={
                **dict(request.context_payload.get("metadata") or {}),
                "cycle_keys": list(request.context_payload.get("cycle_keys") or ()),
            },
        )
        context = self.context_factory.create(
            parent_context,
            child_task_id=request.task_id,
            agent_type=request.agent_type,
            objective_digest=objective_digest,
            mode=request.context_mode,
            maximum_depth=definition.budget.max_depth,
            directive=request.prompt,
        )
        workspace_root = request.workspace_root or self.config.workspace_root
        isolation_request = SubagentIsolationRequest(
            run_id=request.run_id,
            task_id=request.task_id,
            parent_task_id=request.parent_task_id,
            kind=definition.isolation,
            workspace_root=workspace_root,
            requested_cwd=request.requested_cwd,
            writable_paths=tuple(request.constraints.get("writable_paths") or ()),
            read_only_paths=tuple(request.constraints.get("read_only_paths") or ()),
            network_allowed=bool(request.constraints.get("network_allowed", False)),
            cleanup_required=True,
            metadata={
                "agent_type": request.agent_type,
                "definition_id": definition.definition_id,
                "downstream_physical_owner": "M1-05A",
            },
        )
        isolation_manifest = self.isolation_port.prepare(isolation_request)
        parent_budget = UsageBudget.from_dict(
            request.constraints.get("parent_budget")
            if isinstance(request.constraints.get("parent_budget"), Mapping)
            else self.config.default_parent_budget.to_dict()
        )
        self.budget_store.set_parent_limit(request.parent_task_id, parent_budget)
        child_budget = parent_budget.narrowed_by(definition.budget)
        reservation = self.budget_store.reserve(
            parent_task_id=request.parent_task_id,
            child_task_id=request.task_id,
            requested=child_budget,
            idempotency_key=f"budget:{request.idempotency_key}",
        )
        child_session_id = stable_id("childsession", request.parent_session_id, request.task_id)
        record = SubagentTaskRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            parent_task_id=request.parent_task_id,
            parent_session_id=request.parent_session_id,
            agent_type=request.agent_type,
            definition_id=definition.definition_id,
            status=SubagentTaskStatus.CREATED,
            context_snapshot=context,
            tool_scope=resolution.scope,
            permission=permission,
            budget=child_budget,
            isolation_request=isolation_request,
            execution_mode=(AgentExecutionMode.BACKGROUND if definition.background else request.execution_mode),
            prompt_digest=digest_object(request.prompt),
            metadata={
                **copy.deepcopy(request.metadata),
                "root_task_id": str(request.metadata.get("root_task_id") or request.parent_task_id),
                "child_session_id": child_session_id,
                "definition_generation": self.definition_registry.generation,
                "parent_tool_generation": resolution.parent_generation,
                "reservation_id": reservation.reservation_id,
                "isolation_manifest": isolation_manifest.safe_dict(),
                "physical_worker_state_owned": False,
                "worker_lease_state_owned": False,
                "workspace_lifecycle_owned": False,
            },
        )
        try:
            ready, _ = self.lifecycle.prepare(
                record,
                idempotency_key=request.idempotency_key,
                context_payload=context.to_dict(),
            )
            self.transcript_store.append(
                request.task_id,
                TranscriptEntryKind.USER,
                {
                    "content": request.prompt,
                    "self_contained": request.context_mode.value == "isolated",
                    "objective_digest": objective_digest,
                },
            )
            dispatch = SubagentDispatchRequest(
                task_id=request.task_id,
                run_id=request.run_id,
                parent_task_id=request.parent_task_id,
                worker_name="CodeWorkerRuntime",
                agent_definition_id=definition.definition_id,
                prompt=request.prompt,
                tool_scope=resolution.scope,
                permission=permission,
                context=context,
                isolation=isolation_manifest,
                budget=child_budget,
                execution_mode=ready.execution_mode,
                attempt=max(1, ready.attempt + 1),
                idempotency_key=request.idempotency_key,
                constraints={
                    **copy.deepcopy(request.constraints),
                    "max_turns": child_budget.max_turns,
                    "max_tool_calls": child_budget.max_tool_calls,
                    "agent_system_prompt": definition.system_prompt,
                    "agent_skills": list(definition.skills),
                    "agent_hooks": list(definition.hooks),
                    "agent_memory_scope": definition.memory_scope,
                    "model": definition.model,
                    "effort": definition.effort,
                },
                metadata={
                    "root_task_id": str(record.metadata["root_task_id"]),
                    "child_session_id": child_session_id,
                    "node_id": str(request.metadata.get("node_id") or ""),
                    "definition_generation": self.definition_registry.generation,
                },
            )
            if ready.execution_mode == AgentExecutionMode.BACKGROUND:
                thread = Thread(
                    target=self._run_background,
                    args=(dispatch, reservation.reservation_id),
                    name=f"zyra-subagent-{request.task_id}",
                    daemon=True,
                )
                with self._lock:
                    self._threads[request.task_id] = thread
                thread.start()
                return SubagentSpawnResult(
                    record=self.task_store.get(request.task_id),
                    lifecycle=None,
                    background=True,
                )
            lifecycle = self.lifecycle.execute(
                dispatch,
                reservation_id=reservation.reservation_id,
                cancellation_check=lambda: self._is_cancelled(request.task_id, request.parent_task_id),
            )
            return SubagentSpawnResult(
                record=lifecycle.record,
                lifecycle=lifecycle,
                background=False,
            )
        except Exception:
            try:
                self.budget_store.release(reservation.reservation_id, reason="spawn_failed_before_terminal")
            except Exception:
                pass
            # If the task never reached lifecycle execution, release the
            # logical isolation lease here. Missing cleanup is fail-closed.
            current = None
            try:
                current = self.task_store.get(request.task_id)
            except Exception:
                pass
            if current is None or current.status in {SubagentTaskStatus.CREATED, SubagentTaskStatus.VALIDATING, SubagentTaskStatus.READY}:
                self.isolation_port.cleanup(isolation_manifest)
            raise

    def get(self, task_id: str) -> SubagentTaskRecord:
        return self.task_store.get(task_id)

    def list(self, *, parent_task_id: str | None = None, include_terminal: bool = True) -> tuple[SubagentTaskRecord, ...]:
        return self.task_store.list(parent_task_id=parent_task_id, include_terminal=include_terminal)

    def wait(self, task_id: str, timeout: float | None = None) -> LifecycleResult | None:
        with self._lock:
            thread = self._threads.get(task_id)
        if thread:
            thread.join(timeout=timeout)
        with self._lock:
            return self._background_results.get(task_id)

    def cancel(self, task_id: str, *, reason: str = "operator_cancelled") -> tuple[SubagentTaskRecord, ...]:
        self._require_enabled()
        with self._lock:
            self._cancel_requested.add(task_id)
            for descendant in self.task_store.descendants(task_id):
                self._cancel_requested.add(descendant.task_id)
        return self.lifecycle.cancel(task_id, reason=reason, cascade=True)

    def send_message(self, request: ContinuationRequest):
        self._require_enabled()
        return self.continuation_runtime.send(request)

    def cancel_for_parent(self, parent_task_id: str, *, reason: str = "parent_cancelled") -> tuple[SubagentTaskRecord, ...]:
        self._require_enabled()
        cancelled: list[SubagentTaskRecord] = []
        roots = self.task_store.list(parent_task_id=parent_task_id, include_terminal=False)
        for record in roots:
            cancelled.extend(self.cancel(record.task_id, reason=reason))
        return tuple(cancelled)

    def promote_to_background(self, task_id: str) -> SubagentTaskRecord:
        """Change logical mode without starting a second execution."""

        record = self.task_store.get(task_id)
        if record.status not in {SubagentTaskStatus.DISPATCHED, SubagentTaskStatus.RUNNING, SubagentTaskStatus.WAITING}:
            raise ValueError(f"task {task_id} cannot be promoted from {record.status}")

        def update(item: SubagentTaskRecord) -> None:
            item.execution_mode = AgentExecutionMode.BACKGROUND
            item.metadata["promoted_to_background"] = True
            item.metadata["promotion_execution_ref"] = item.execution_ref

        updated, _ = self.task_store.mutate(task_id, "foreground_promoted", update)
        if updated.execution_ref != record.execution_ref:
            raise RuntimeError("foreground promotion changed execution_ref")
        return updated

    def snapshot(self) -> SubagentRuntimeSnapshot:
        tasks = self.task_store.list()
        active = tuple(item.task_id for item in tasks if not item.status.terminal)
        terminal = tuple(item.task_id for item in tasks if item.status.terminal)
        payload = [item.safe_dict() for item in tasks]
        return SubagentRuntimeSnapshot(
            tasks=tuple(payload),
            active_task_ids=active,
            terminal_task_ids=terminal,
            definition_generation=self.definition_registry.generation,
            state_digest=digest_object(payload),
        )

    def background_result(self, task_id: str) -> LifecycleResult | None:
        with self._lock:
            return self._background_results.get(task_id)

    def _run_background(self, dispatch: SubagentDispatchRequest, reservation_id: str) -> None:
        try:
            result = self.lifecycle.execute(
                dispatch,
                reservation_id=reservation_id,
                cancellation_check=lambda: self._is_cancelled(dispatch.task_id, dispatch.parent_task_id),
            )
            with self._lock:
                self._background_results[dispatch.task_id] = result
        finally:
            with self._lock:
                self._threads.pop(dispatch.task_id, None)

    def _is_cancelled(self, task_id: str, parent_task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancel_requested or parent_task_id in self._cancel_requested

    def _require_enabled(self) -> None:
        if self.config.disabled:
            raise SubagentDisabled("SubagentRuntime")
