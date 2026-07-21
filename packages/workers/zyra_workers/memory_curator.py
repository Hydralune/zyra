from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from zyra_memory import MemoryIndexRuntime
from zyra_memory.curator_models import CuratorRunResult
from zyra_memory.curator_runtime import (
    CuratorRunScheduler,
    MemoryCuratorWorker,
    SchedulerReceipt,
)
from zyra_memory.curator_store import CuratorCandidateStore


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "status": self.status,
            "scheduled": self.scheduled.to_dict() if self.scheduled else None,
            "result": self.result.to_dict() if self.result else None,
            "data": dict(self.data),
        }


class MemoryCuratorWorkerRuntime:
    """Worker-facing request/result port used by API and task lifecycle hooks."""

    def __init__(
        self,
        *,
        scheduler: CuratorRunScheduler,
        worker: MemoryCuratorWorker,
    ) -> None:
        self.scheduler = scheduler
        self.worker = worker

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
            result = (
                self.worker.process_job(scheduled.job.job_id)
                if value.process_immediately
                else None
            )
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status=result.status if result else "scheduled",
                scheduled=scheduled,
                result=result,
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
            result = (
                self.worker.process_job(scheduled.job.job_id)
                if value.process_immediately
                else None
            )
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status=result.status if result else "scheduled",
                scheduled=scheduled,
                result=result,
            )
        if value.operation is MemoryCuratorOperation.RUN_NEXT:
            result = self.worker.process_one(task_id=value.task_id)
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status=result.status if result else "idle",
                result=result,
            )
        if value.operation is MemoryCuratorOperation.RUN_JOB:
            result = self.worker.process_job(value.job_id)
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status=result.status,
                result=result,
            )
        if value.operation is MemoryCuratorOperation.RECOVER:
            data = self.worker.recover()
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status="recovered",
                data=data,
            )
        if value.operation is MemoryCuratorOperation.HEALTH:
            return MemoryCuratorWorkerResponse(
                operation=value.operation,
                status="ok",
                data=self.worker.health(),
            )
        raise ValueError(f"unsupported memory curator operation: {value.operation.value}")


def build_memory_curator_runtime(
    *,
    canonical_store: Any,
    artifact_store: Any | None,
    memory_index: MemoryIndexRuntime | None,
    worker_id: str = "",
    model: Any | None = None,
) -> MemoryCuratorWorkerRuntime:
    path = getattr(canonical_store, "path", None)
    if path is None:
        raise ValueError("memory curator requires a path-backed canonical store")
    candidate_store = CuratorCandidateStore(Path(path))
    scheduler = CuratorRunScheduler(
        candidate_store=candidate_store,
        canonical_store=canonical_store,
        event_sink=canonical_store,
    )
    worker = MemoryCuratorWorker(
        candidate_store=candidate_store,
        canonical_store=canonical_store,
        artifact_store=artifact_store,
        index_runtime=memory_index,
        model=model,
        worker_id=worker_id,
    )
    return MemoryCuratorWorkerRuntime(scheduler=scheduler, worker=worker)


__all__ = [
    "MemoryCuratorOperation",
    "MemoryCuratorWorkerRequest",
    "MemoryCuratorWorkerResponse",
    "MemoryCuratorWorkerRuntime",
    "build_memory_curator_runtime",
]
