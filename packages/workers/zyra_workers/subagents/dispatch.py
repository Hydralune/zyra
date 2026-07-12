from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Mapping, Protocol

from zyra_runtime import ToolRegistry, WorkerRequest, default_tool_registry

from .errors import ParentCancelled, SubagentDispatchRejected, SubagentExecutionFailed
from .models import (
    SubagentDispatchReceipt,
    SubagentDispatchRequest,
    SubagentExecutionResult,
    SubagentTaskStatus,
    UsageLedger,
)
from .parent_scope import ChildExecutionScope
from .session_assembly import ChildWorkerSessionAssembler, extract_explicit_typed_yield
from .subagent_yield import (
    SUBAGENT_YIELD_TOOL_NAME,
    SubagentYieldCollector,
    bind_subagent_yield_collector,
    subagent_yield_tool_spec,
)


class SubagentExecutionPort(Protocol):
    def execution_ref(self, request: SubagentDispatchRequest) -> str: ...

    def execute(
        self,
        request: SubagentDispatchRequest,
        *,
        cancel_check: Callable[[], bool],
        progress: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> SubagentExecutionResult: ...

    def cancel(self, execution_ref: str) -> bool: ...


class CancellationRegistry:
    """Live cooperative projection of durable task cancel flags."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._tokens: dict[str, Event] = {}

    def create(self, execution_ref: str) -> Event:
        with self._lock:
            return self._tokens.setdefault(execution_ref, Event())

    def cancel(self, execution_ref: str) -> bool:
        with self._lock:
            token = self._tokens.get(execution_ref)
            if token is None:
                return False
            token.set()
            return True

    def release(self, execution_ref: str) -> None:
        with self._lock:
            self._tokens.pop(execution_ref, None)


class CodeWorkerSubagentExecutionPort:
    """Run child tasks through the existing Zyra CodeWorkerRuntime."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        artifact_root: str | Path,
        permission_store: Any | None = None,
        permission_state_path: str | Path | None = None,
        parent_registry: ToolRegistry | None = None,
        mcp_runtime: Any | None = None,
        skill_fork_port: Any | None = None,
        session_assembler: ChildWorkerSessionAssembler | None = None,
        runtime_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.permission_store = permission_store
        self.permission_state_path = permission_state_path
        self.parent_registry = (parent_registry or default_tool_registry()).merged(
            [subagent_yield_tool_spec()], keep_existing=True
        )
        self.mcp_runtime = mcp_runtime
        self.skill_fork_port = skill_fork_port
        self.session_assembler = session_assembler or ChildWorkerSessionAssembler()
        self.runtime_factory = runtime_factory
        self.cancellations = CancellationRegistry()

    def execution_ref(self, request: SubagentDispatchRequest) -> str:
        return f"codeworker:{request.task_id}:{request.attempt}:{request.dispatch_id}"

    def execute(
        self,
        request: SubagentDispatchRequest,
        *,
        cancel_check: Callable[[], bool],
        progress: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> SubagentExecutionResult:
        execution_ref = self.execution_ref(request)
        token = self.cancellations.create(execution_ref)

        def combined_cancel() -> bool:
            return token.is_set() or cancel_check()

        if combined_cancel():
            raise ParentCancelled(request.parent_task_id)
        registry = ToolRegistry([
            spec for spec in self.parent_registry.list() if spec.name in request.tool_scope.child_tools
        ])
        if tuple(spec.name for spec in registry.list()) != request.tool_scope.child_tools:
            missing = sorted(set(request.tool_scope.child_tools) - {spec.name for spec in registry.list()})
            raise SubagentDispatchRejected("child executable registry does not match derived scope", missing=missing)
        yield_spec = registry.get(SUBAGENT_YIELD_TOOL_NAME)
        collector = SubagentYieldCollector(
            task_id=request.task_id,
            parent_task_id=request.parent_task_id,
            execution_ref=execution_ref,
            attempt=request.attempt,
        )
        dynamic_handlers: dict[str, Any] = {}
        if yield_spec is not None:
            binding = bind_subagent_yield_collector(yield_spec, collector)
            dynamic_handlers[SUBAGENT_YIELD_TOOL_NAME] = binding.handler
        raw_child_scope = request.constraints.get("signed_child_scope")
        child_scope = (
            ChildExecutionScope.from_dict(raw_child_scope)
            if isinstance(raw_child_scope, Mapping) and raw_child_scope
            else None
        )
        assembled = self.session_assembler.assemble(
            request,
            registry=registry,
            child_scope=child_scope,
            cancel_check=combined_cancel,
            typed_yield_sink=lambda raw: collector.capture(raw, causation_id="runtime-service"),
            skill_fork_port=self.skill_fork_port,
        )
        if progress:
            progress({
                "phase": "codeworker_start",
                "execution_ref": execution_ref,
                "child_session_envelope_digest": assembled.envelope.digest,
            })
        runtime = self._runtime(
            workspace_root=request.isolation.effective_cwd,
            registry=assembled.registry,
            runtime_services=assembled.runtime_services,
            dynamic_handlers=dynamic_handlers,
        )
        worker_request = assembled.worker_request
        root_task_id = assembled.envelope.root_task_id
        try:
            run = runtime.run(worker_request)
        except Exception as error:
            raise SubagentExecutionFailed(
                f"CodeWorker subagent execution raised {type(error).__name__}: {error}",
                task_id=request.task_id,
                execution_ref=execution_ref,
                error_type=type(error).__name__,
                exception_message=str(error),
            ) from error
        finally:
            self.cancellations.release(execution_ref)
        if progress:
            progress({
                "phase": "codeworker_complete",
                "execution_ref": execution_ref,
                "ok": run.worker_result.ok,
            })
        usage = UsageLedger(
            turns=int(run.worker_result.metadata.get("query_turns") or 0),
            tool_calls=int(run.worker_result.metadata.get("tool_steps") or 0),
            input_tokens=int(run.worker_result.metadata.get("input_tokens") or 0),
            output_tokens=int(run.worker_result.metadata.get("output_tokens") or 0),
            # Event envelopes include the complete runtime audit projection and
            # are not child tool output.  Charging them as result characters can
            # exceed the child budget even for a tiny typed yield.  CodeWorker's
            # owned result-budget counter is the authoritative usage signal.
            result_chars=int(run.worker_result.metadata.get("tool_runtime_result_chars") or 0),
        )
        return SubagentExecutionResult(
            task_id=request.task_id,
            execution_ref=execution_ref,
            ok=run.worker_result.ok,
            summary=run.worker_result.summary,
            artifacts=tuple(run.worker_result.artifacts),
            events=tuple({
                "event_id": item.event_id,
                "event_type": item.event_type.value,
                "created_at": item.created_at,
                "payload": copy.deepcopy(item.payload),
            } for item in run.event_records),
            usage=usage,
            error_code=str(run.worker_result.error or ""),
            error_message=str(run.worker_result.error or ""),
            metadata={
                **dict(run.worker_result.metadata),
                "typed_yield": (
                    collector.value.to_dict()
                    if collector.value is not None
                    else extract_explicit_typed_yield(run.worker_result.metadata)
                ),
                "child_session_envelope_digest": assembled.envelope.digest,
                "child_model_name": assembled.envelope.model_name,
                "child_effort": assembled.envelope.effort,
                "root_task_id": root_task_id,
                "logical_subagent_task_id": request.task_id,
                "worker_id_owned": False,
                "lease_id_owned": False,
            },
        )

    def cancel(self, execution_ref: str) -> bool:
        return self.cancellations.cancel(execution_ref)

    def _runtime(
        self,
        *,
        workspace_root: str | Path,
        registry: ToolRegistry,
        runtime_services: Mapping[str, Any],
        dynamic_handlers: Mapping[str, Any],
    ) -> Any:
        if self.runtime_factory is not None:
            return self.runtime_factory(
                project_root=self.project_root,
                workspace_root=workspace_root,
                artifact_root=self.artifact_root,
                permission_store=self.permission_store,
                permission_state_path=self.permission_state_path,
                mcp_runtime=self.mcp_runtime,
                tool_registry=registry,
                dynamic_handlers=dynamic_handlers,
                runtime_services=runtime_services,
                skill_fork_port=self.skill_fork_port,
            )
        from zyra_workers.code_worker_runtime import CodeWorkerRuntime

        return CodeWorkerRuntime(
            project_root=self.project_root,
            workspace_root=workspace_root,
            artifact_root=self.artifact_root,
            permission_store=self.permission_store,
            permission_state_path=self.permission_state_path,
            mcp_runtime=self.mcp_runtime,
            tool_registry=registry,
            dynamic_handlers=dynamic_handlers,
            runtime_services=runtime_services,
            skill_fork_port=self.skill_fork_port,
        )


class DisabledSubagentExecutionPort:
    def execution_ref(self, request: SubagentDispatchRequest) -> str:
        return f"disabled:{request.dispatch_id}"

    def execute(self, request: SubagentDispatchRequest, *, cancel_check, progress=None) -> SubagentExecutionResult:
        raise SubagentDispatchRejected("Subagent execution port is disabled", task_id=request.task_id)

    def cancel(self, execution_ref: str) -> bool:
        return False
