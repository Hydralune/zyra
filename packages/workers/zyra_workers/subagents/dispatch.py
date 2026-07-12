from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Mapping, Protocol

from zyra_core import AgentMessage, AgentRole, MessageIntent
from zyra_runtime import ToolRegistry, WorkerRequest, default_tool_registry

from .errors import ParentCancelled, SubagentDispatchRejected, SubagentExecutionFailed
from .models import (
    SubagentDispatchReceipt,
    SubagentDispatchRequest,
    SubagentExecutionResult,
    SubagentTaskStatus,
    UsageLedger,
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
        runtime_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.permission_store = permission_store
        self.permission_state_path = permission_state_path
        self.parent_registry = parent_registry or default_tool_registry()
        self.mcp_runtime = mcp_runtime
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
        if progress:
            progress({"phase": "codeworker_start", "execution_ref": execution_ref})
        runtime = self._runtime(
            workspace_root=request.isolation.effective_cwd,
            registry=registry,
            cancel_check=combined_cancel,
        )
        root_task_id = str(request.metadata.get("root_task_id") or request.parent_task_id)
        message = AgentMessage(
            run_id=request.run_id,
            task_id=root_task_id,
            sender_role=AgentRole.PLANNER,
            receiver_role=AgentRole.CODE_WORKER,
            intent=MessageIntent.EXECUTE,
            content=request.prompt,
            summary="Bounded subagent dispatch",
            artifact_refs=[],
            metadata={
                "subagent_task_id": request.task_id,
                "parent_task_id": request.parent_task_id,
                "context_snapshot_id": request.context.snapshot_id,
                "tool_scope_digest": request.tool_scope.digest,
                "permission_digest": request.permission.digest,
            },
        )
        constraints = {
            **copy.deepcopy(request.constraints),
            "session_id": str(request.metadata.get("child_session_id") or f"subagent:{request.task_id}"),
            "subagent_task_id": request.task_id,
            "parent_task_id": request.parent_task_id,
            "subagent_depth": request.context.depth,
            "max_turns": request.budget.max_turns,
            "max_tool_calls": request.budget.max_tool_calls,
            "permission_mode": request.permission.child_mode.value,
            "allowed_mcp_servers": list(request.permission.mcp_servers),
            "child_tool_scope": request.tool_scope.to_dict(),
            "disable_skill_tool_projection": not bool(
                {"skill", "list_skills", "read_skill_resource"}.intersection(request.tool_scope.child_tools)
            ),
            "invoked_skill_refs": [copy.deepcopy(item) for item in request.context.invoked_skill_refs],
            "content_replacement_refs": list(request.context.content_replacement_refs),
        }
        worker_request = WorkerRequest(
            run_id=request.run_id,
            task_id=root_task_id,
            worker_name=request.worker_name,
            messages=[message],
            node_id=str(request.metadata.get("node_id") or "") or None,
            constraints=constraints,
            metadata={
                "logical_subagent_task_id": request.task_id,
                "dispatch_id": request.dispatch_id,
                "execution_ref": execution_ref,
                "agent_definition_id": request.agent_definition_id,
            },
        )
        try:
            run = runtime.run(worker_request)
        except Exception as error:
            raise SubagentExecutionFailed(
                "CodeWorker subagent execution raised an exception",
                task_id=request.task_id,
                execution_ref=execution_ref,
                error_type=type(error).__name__,
                message=str(error),
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
            result_chars=sum(len(str(item.payload)) for item in run.event_records),
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
                "root_task_id": root_task_id,
                "logical_subagent_task_id": request.task_id,
                "worker_id_owned": False,
                "lease_id_owned": False,
            },
        )

    def cancel(self, execution_ref: str) -> bool:
        return self.cancellations.cancel(execution_ref)

    def _runtime(self, *, workspace_root: str | Path, registry: ToolRegistry, cancel_check: Callable[[], bool]) -> Any:
        if self.runtime_factory is not None:
            return self.runtime_factory(
                project_root=self.project_root,
                workspace_root=workspace_root,
                artifact_root=self.artifact_root,
                permission_store=self.permission_store,
                permission_state_path=self.permission_state_path,
                mcp_runtime=self.mcp_runtime,
                tool_registry=registry,
                runtime_services={"cancel_check": cancel_check},
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
            runtime_services={"cancel_check": cancel_check},
        )


class DisabledSubagentExecutionPort:
    def execution_ref(self, request: SubagentDispatchRequest) -> str:
        return f"disabled:{request.dispatch_id}"

    def execute(self, request: SubagentDispatchRequest, *, cancel_check, progress=None) -> SubagentExecutionResult:
        raise SubagentDispatchRejected("Subagent execution port is disabled", task_id=request.task_id)

    def cancel(self, execution_ref: str) -> bool:
        return False
