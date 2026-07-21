from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from zyra_memory import MemoryIndexRuntime
from zyra_memory.curator_integration_store import CuratorIntegrationStore
from zyra_memory.curator_models import CuratorRunResult
from zyra_memory.curator_runtime import (
    CuratorRunScheduler,
    MemoryCuratorWorker,
    SchedulerReceipt,
)
from zyra_memory.curator_store import CuratorCandidateStore

from .memory_curator_ingress import RuntimeEventCuratorIngress
from .memory_curator_integration import MemoryCuratorIntegrationApplication


class MemoryCuratorOperation(StrEnum):
    SCHEDULE_MANUAL = "schedule_manual"
    SCHEDULE_TASK_END = "schedule_task_end"
    RUN_NEXT = "run_next"
    RUN_JOB = "run_job"
    RECOVER = "recover"
    HEALTH = "health"


@dataclass(frozen=True, slots=True)
class MemoryCuratorWorkerRequest:
    operation: MemoryCuratorOperation
    task_id: str = ""
    job_id: str = ""
    requested_by: str = "api"
    start_sequence: int | None = None
    end_sequence: int | None = None
    allow_model_assist: bool = True
    max_candidates: int = 64
    idempotency_key: str = ""
    process_immediately: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> MemoryCuratorWorkerRequest:
        if self.operation in {
            MemoryCuratorOperation.SCHEDULE_MANUAL,
            MemoryCuratorOperation.SCHEDULE_TASK_END,
            MemoryCuratorOperation.RUN_NEXT,
        } and not self.task_id.strip():
            raise ValueError("memory curator request requires task_id")
        if self.operation is MemoryCuratorOperation.RUN_JOB and not self.job_id.strip():
            raise ValueError("memory curator run_job request requires job_id")
        if self.start_sequence is not None and self.start_sequence < 0:
            raise ValueError("memory curator start_sequence must be non-negative")
        if (
            self.end_sequence is not None
            and self.start_sequence is not None
            and self.end_sequence < self.start_sequence
        ):
            raise ValueError("memory curator end_sequence precedes start_sequence")
        if not 1 <= self.max_candidates <= 1000:
            raise ValueError("memory curator max_candidates is out of range")
        return self

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MemoryCuratorWorkerRequest:
        return cls(
            operation=MemoryCuratorOperation(str(value.get("operation") or "")),
            task_id=str(value.get("task_id") or ""),
            job_id=str(value.get("job_id") or ""),
            requested_by=str(value.get("requested_by") or "api"),
            start_sequence=(
                int(value["start_sequence"])
                if value.get("start_sequence") is not None
                else None
            ),
            end_sequence=(
                int(value["end_sequence"])
                if value.get("end_sequence") is not None
                else None
            ),
            allow_model_assist=bool(value.get("allow_model_assist", True)),
            max_candidates=int(value.get("max_candidates", 64)),
            idempotency_key=str(value.get("idempotency_key") or ""),
            process_immediately=bool(value.get("process_immediately", False)),
            metadata=(
                dict(value.get("metadata"))
                if isinstance(value.get("metadata"), Mapping)
                else {}
            ),
        ).validated()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "requested_by": self.requested_by,
            "start_sequence": self.start_sequence,
            "end_sequence": self.end_sequence,
            "allow_model_assist": self.allow_model_assist,
            "max_candidates": self.max_candidates,
            "idempotency_key": self.idempotency_key,
            "process_immediately": self.process_immediately,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class MemoryCuratorWorkerResponse:
    operation: MemoryCuratorOperation
    status: str
    scheduled: SchedulerReceipt | None = None
    result: CuratorRunResult | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    integration: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "status": self.status,
            "scheduled": self.scheduled.to_dict() if self.scheduled else None,
            "result": self.result.to_dict() if self.result else None,
            "data": dict(self.data),
            "integration": dict(self.integration) if self.integration else None,
        }


class MemoryCuratorWorkerRuntime:
    """Worker-facing request/result port used by API and task lifecycle hooks."""

    def __init__(
        self,
        *,
        scheduler: CuratorRunScheduler,
        worker: MemoryCuratorWorker,
        integration_application: MemoryCuratorIntegrationApplication | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.worker = worker
        self.integration_application = integration_application

    @property
    def integration_store(self) -> CuratorIntegrationStore | None:
        return (
            self.integration_application.store
            if self.integration_application is not None
            else None
        )

    def _process_job(
        self,
        job_id: str,
    ) -> tuple[CuratorRunResult, Mapping[str, Any] | None]:
        if self.integration_application is None:
            return self.worker.process_job(job_id), None
        integrated = self.integration_application.process_job(job_id)
        return integrated.curator_result, integrated.to_dict()

    def _process_one(
        self,
        *,
        task_id: str,
    ) -> tuple[CuratorRunResult | None, Mapping[str, Any] | None]:
        if self.integration_application is None:
            return self.worker.process_one(task_id=task_id), None
        integrated = self.integration_application.process_one(task_id=task_id)
        if integrated is None:
            return None, None
        return integrated.curator_result, integrated.to_dict()

    def execute(self, request: MemoryCuratorWorkerRequest) -> MemoryCuratorWorkerResponse:
        value = request.validated()
        if value.operation is MemoryCuratorOperation.SCHEDULE_MANUAL:
            scheduled = self.scheduler.schedule_manual(
                task_id=value.task_id,
                requested_by=value.requested_by,
                start_sequence=value.start_sequence,
                end_sequence=value.end_sequence,
                allow_model_assist=value.allow_model_assist,
                max_candidates=value.max_candidates,
                idempotency_key=value.idempotency_key,
            )
            result = None
            integration = None
            if value.process_immediately:
                result, integration = self._process_job(scheduled.job.job_id)
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status=result.status if result else "scheduled",
                scheduled=scheduled,
                result=result,
                integration=integration,
            )
        if value.operation is MemoryCuratorOperation.SCHEDULE_TASK_END:
            scheduled = self.scheduler.schedule_task_end(
                task_id=value.task_id,
                requested_by=value.requested_by,
                allow_model_assist=value.allow_model_assist,
                max_candidates=value.max_candidates,
            )
            if scheduled is None:
                return MemoryCuratorWorkerResponse(
                    operation=value.operation,
                    status="not_terminal",
                    data={"scheduled": False},
                )
            result = None
            integration = None
            if value.process_immediately:
                result, integration = self._process_job(scheduled.job.job_id)
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status=result.status if result else "scheduled",
                scheduled=scheduled,
                result=result,
                integration=integration,
            )
        if value.operation is MemoryCuratorOperation.RUN_NEXT:
            result, integration = self._process_one(task_id=value.task_id)
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status=result.status if result else "idle",
                result=result,
                integration=integration,
            )
        if value.operation is MemoryCuratorOperation.RUN_JOB:
            result, integration = self._process_job(value.job_id)
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status=result.status,
                result=result,
                integration=integration,
            )
        if value.operation is MemoryCuratorOperation.RECOVER:
            if self.integration_application is not None:
                integration_recovery = self.integration_application.recover()
                data = {
                    **dict(integration_recovery.worker),
                    "integration": integration_recovery.to_dict(),
                }
            else:
                data = self.worker.recover()
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status="recovered",
                data=data,
            )
        if value.operation is MemoryCuratorOperation.HEALTH:
            health = (
                self.integration_application.status(task_id=value.task_id)
                if self.integration_application is not None
                else self.worker.health()
            )
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status="ok",
                data=health,
            )
        raise ValueError(f"unsupported memory curator operation: {value.operation.value}")


def build_memory_curator_runtime(
    *,
    canonical_store: Any,
    artifact_store: Any | None,
    memory_index: MemoryIndexRuntime | None,
    worker_id: str = "",
    model: Any | None = None,
    runtime_event_bridge: Any | None = None,
    allow_legacy_event_fallback: bool = False,
    auto_dispatch: bool = True,
) -> MemoryCuratorWorkerRuntime:
    path = getattr(canonical_store, "path", None)
    if path is None:
        raise ValueError("memory curator requires a path-backed canonical store")
    candidate_store = CuratorCandidateStore(Path(path))
    integration_store = CuratorIntegrationStore(Path(path))
    trace_ingress = (
        RuntimeEventCuratorIngress(
            bridge=runtime_event_bridge,
            canonical_store=canonical_store,
            integration_store=integration_store,
            allow_legacy_fallback=allow_legacy_event_fallback,
        )
        if runtime_event_bridge is not None
        else None
    )
    scheduler = CuratorRunScheduler(
        candidate_store=candidate_store,
        canonical_store=canonical_store,
        event_sink=canonical_store,
        trace_ingress=trace_ingress,
    )
    worker = MemoryCuratorWorker(
        candidate_store=candidate_store,
        canonical_store=canonical_store,
        artifact_store=artifact_store,
        index_runtime=memory_index,
        model=model,
        worker_id=worker_id,
        trace_ingress=trace_ingress,
    )
    integration_application = (
        MemoryCuratorIntegrationApplication(
            worker=worker,
            ingress=trace_ingress,
            store=integration_store,
            memory_index=memory_index,
            event_sink=canonical_store,
            worker_id=f"{worker_id or 'memory-curator'}:integration",
            auto_dispatch=auto_dispatch,
        )
        if trace_ingress is not None and memory_index is not None
        else None
    )
    return MemoryCuratorWorkerRuntime(
        scheduler=scheduler,
        worker=worker,
        integration_application=integration_application,
    )


__all__ = [
    "MemoryCuratorOperation",
    "MemoryCuratorWorkerRequest",
    "MemoryCuratorWorkerResponse",
    "MemoryCuratorWorkerRuntime",
    "build_memory_curator_runtime",
]
