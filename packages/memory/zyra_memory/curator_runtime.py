from __future__ import annotations

import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType, PlanNodeStatus, TaskState, now_iso

from .curator_commit import CuratorOutboxDispatcher, MemoryCommitRuntime
from .curator_decision import CandidateModel, MemoryDecisionRuntime
from .curator_evidence import EventArtifactTraceExtractor
from .curator_models import (
    CandidateState,
    CommitDisposition,
    CuratorJob,
    CuratorJobLease,
    CuratorJobState,
    CuratorRunRequest,
    CuratorRunResult,
    CuratorTrigger,
    MemoryCommitReceipt,
    OutboxState,
    stable_id,
)
from .curator_store import (
    CuratorCandidateStore,
    CuratorLeaseLostError,
)
from .curator_typescript_port import TypeScriptCuratorStatePort
from .curator_validation import CuratorValidationPolicy, MemoryDecisionValidator


class CuratorRuntimeError(RuntimeError):
    pass


class CuratorTaskMismatchError(CuratorRuntimeError):
    pass


class CuratorNoWorkError(CuratorRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SchedulerReceipt:
    request: CuratorRunRequest
    job: CuratorJob
    created: bool
    event_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "job": self.job.to_dict(),
            "created": self.created,
            "event_id": self.event_id,
        }


@dataclass(frozen=True, slots=True)
class CuratorWorkerHealth:
    worker_id: str
    status: str
    current_job_id: str
    processed_jobs: int
    failed_jobs: int
    last_error: str
    started_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "status": self.status,
            "current_job_id": self.current_job_id,
            "processed_jobs": self.processed_jobs,
            "failed_jobs": self.failed_jobs,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


class CuratorRunScheduler:
    """Manual and task-terminal scheduling over one durable watermark queue."""

    TERMINAL_STATUSES = {
        PlanNodeStatus.COMPLETED,
        PlanNodeStatus.FAILED,
        PlanNodeStatus.CANCELLED,
    }

    def __init__(
        self,
        *,
        candidate_store: CuratorCandidateStore,
        canonical_store: Any,
        event_sink: Any | None = None,
        default_lease_seconds: float = 30.0,
        trace_ingress: Any | None = None,
    ) -> None:
        self.candidate_store = candidate_store
        self.canonical_store = canonical_store
        self.event_sink = event_sink or canonical_store
        self.default_lease_seconds = default_lease_seconds
        self.trace_ingress = trace_ingress

    def schedule_manual(
        self,
        *,
        task_id: str,
        requested_by: str,
        start_sequence: int | None = None,
        end_sequence: int | None = None,
        allow_model_assist: bool = True,
        max_candidates: int = 64,
        idempotency_key: str = "",
    ) -> SchedulerReceipt:
        state = self._require_task(task_id)
        events = self.canonical_store.task_events(task_id)
        input_watermark = self._task_input_watermark(state, events)
        request = CuratorRunRequest.build(
            run_id=state.run_id,
            task_id=state.task_id,
            trigger=CuratorTrigger.MANUAL,
            requested_by=requested_by,
            input_watermark=input_watermark,
            evidence_start=max(0, int(start_sequence or 0)),
            evidence_end=end_sequence,
            allow_model_assist=allow_model_assist,
            max_candidates=max_candidates,
            lease_seconds=self.default_lease_seconds,
            idempotency_key=idempotency_key,
            metadata={
                "task_status": str(state.status),
                "event_count_at_schedule": input_watermark,
                "scheduler": "CuratorRunScheduler",
            },
        )
        return self._enqueue(request)

    def schedule_task_end(
        self,
        *,
        task_id: str,
        requested_by: str = "task-lifecycle",
        force: bool = False,
        allow_model_assist: bool = True,
        max_candidates: int = 64,
    ) -> SchedulerReceipt | None:
        state = self._require_task(task_id)
        if not force and state.status not in self.TERMINAL_STATUSES:
            return None
        events = self.canonical_store.task_events(task_id)
        input_watermark = self._task_input_watermark(state, events)
        key = f"curator-task-end:{task_id}:{input_watermark}:{state.updated_at}"
        request = CuratorRunRequest.build(
            run_id=state.run_id,
            task_id=state.task_id,
            trigger=CuratorTrigger.TASK_END,
            requested_by=requested_by,
            input_watermark=input_watermark,
            evidence_start=self._next_evidence_start(task_id),
            evidence_end=None,
            allow_model_assist=allow_model_assist,
            max_candidates=max_candidates,
            lease_seconds=self.default_lease_seconds,
            idempotency_key=key,
            metadata={
                "task_status": str(state.status),
                "task_updated_at": state.updated_at,
                "event_count_at_schedule": input_watermark,
                "automatic": True,
            },
        )
        return self._enqueue(request)

    def schedule_recovery(
        self,
        *,
        task_id: str,
        requested_by: str,
        from_sequence: int = 0,
    ) -> SchedulerReceipt:
        state = self._require_task(task_id)
        events = self.canonical_store.task_events(task_id)
        input_watermark = self._task_input_watermark(state, events)
        request = CuratorRunRequest.build(
            run_id=state.run_id,
            task_id=state.task_id,
            trigger=CuratorTrigger.RECOVERY,
            requested_by=requested_by,
            input_watermark=input_watermark,
            evidence_start=max(0, int(from_sequence)),
            allow_model_assist=False,
            max_candidates=64,
            lease_seconds=self.default_lease_seconds,
            idempotency_key=f"curator-recovery:{task_id}:{input_watermark}:{from_sequence}",
            metadata={"recovery": True, "deterministic_only": True},
        )
        return self._enqueue(request)

    def _enqueue(self, request: CuratorRunRequest) -> SchedulerReceipt:
        job, created = self.candidate_store.enqueue_job(request)
        event_id = stable_id("event", "memory_curator_scheduled", request.request_id)
        event = EventRecord(
            event_id=event_id,
            run_id=request.run_id,
            task_id=request.task_id,
            event_type=EventType.MEMORY_CURATOR_SCHEDULED,
            payload={
                "schema": "zyra.memory-curator-schedule.v1",
                "request_id": request.request_id,
                "job_id": job.job_id,
                "trigger": request.trigger.value,
                "requested_by": request.requested_by,
                "input_watermark": request.input_watermark,
                "evidence_start": request.evidence_start,
                "evidence_end": request.evidence_end,
                "created": created,
                "idempotency_key": request.idempotency_key,
                "canonical_job_owner": "CuratorCandidateStore",
            },
        )
        if self.event_sink is not None:
            self.event_sink.append_event(event)
        return SchedulerReceipt(request=request, job=job, created=created, event_id=event_id)

    def _require_task(self, task_id: str) -> TaskState:
        state = self.canonical_store.load_task(task_id)
        if state is None:
            raise KeyError(f"task not found: {task_id}")
        return state

    def _next_evidence_start(self, task_id: str) -> int:
        jobs = self.candidate_store.jobs(
            task_id=task_id,
            states=(CuratorJobState.SUCCEEDED,),
            limit=1,
        )
        if not jobs:
            return 0
        return max(0, jobs[0].last_success_watermark)

    @staticmethod
    def _input_watermark(events: Sequence[Mapping[str, Any]]) -> int:
        operational = {
            EventType.MEMORY_CURATOR_SCHEDULED.value,
            EventType.MEMORY_CURATOR_COMMITTED.value,
            EventType.MEMORY_CURATOR_REJECTED.value,
            EventType.MEMORY_CURATOR_RECOVERED.value,
        }
        watermark = 0
        for index, event in enumerate(events):
            event_type = event.get("event_type")
            if hasattr(event_type, "value"):
                event_type = event_type.value
            if str(event_type or "") not in operational:
                watermark = index + 1
        return watermark

    def _task_input_watermark(
        self,
        state: TaskState,
        events: Sequence[Mapping[str, Any]],
    ) -> int:
        if self.trace_ingress is None:
            return self._input_watermark(events)
        return int(
            self.trace_ingress.current_watermark(
                run_id=state.run_id,
                task_id=state.task_id,
            )
        )


class LeaseHeartbeat:
    """Hermes-style background isolation with OMP ownership fencing."""

    def __init__(
        self,
        *,
        store: CuratorCandidateStore,
        lease: CuratorJobLease,
        lease_seconds: float,
        interval_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.lease = lease
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds or max(0.2, min(lease_seconds / 3.0, 10.0))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._lock = threading.Lock()

    def __enter__(self) -> LeaseHeartbeat:
        self._thread = threading.Thread(
            target=self._run,
            name=f"memory-curator-heartbeat:{self.lease.job_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2.0))
        if exc is None and self._error is not None:
            job = self.store.job(self.lease.job_id)
            if job is not None and job.terminal:
                return
            raise self._error

    @property
    def current_lease(self) -> CuratorJobLease:
        with self._lock:
            return self.lease

    def assert_healthy(self) -> CuratorJobLease:
        if self._error is not None:
            raise self._error
        return self.current_lease

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                refreshed = self.store.heartbeat_job(
                    self.current_lease,
                    lease_seconds=self.lease_seconds,
                )
            except BaseException as error:
                self._error = error
                self._stop.set()
                return
            with self._lock:
                self.lease = refreshed


class MemoryCuratorWorker:
    """Real curator worker: extract -> propose -> validate -> commit -> deliver."""

    def __init__(
        self,
        *,
        candidate_store: CuratorCandidateStore,
        canonical_store: Any,
        artifact_store: Any | None,
        index_runtime: Any | None,
        model: CandidateModel | None = None,
        validation_policy: CuratorValidationPolicy | None = None,
        worker_id: str = "",
        lease_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
        outbox_limit: int = 1000,
        typescript_port: Any | None = None,
        enable_typescript_supplement: bool = True,
        trace_ingress: Any | None = None,
    ) -> None:
        self.candidate_store = candidate_store
        self.canonical_store = canonical_store
        self.artifact_store = artifact_store
        self.index_runtime = index_runtime
        self.worker_id = worker_id or f"memory-curator:{os.getpid()}:{id(self):x}"
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.outbox_limit = outbox_limit
        self.trace_ingress = trace_ingress
        self.extractor = EventArtifactTraceExtractor(
            store=candidate_store,
            artifact_reader=artifact_store,
        )
        if enable_typescript_supplement and typescript_port is None:
            project_root = Path(__file__).resolve().parents[3]
            typescript_port = TypeScriptCuratorStatePort(project_root=project_root)
        self.typescript_port = typescript_port if enable_typescript_supplement else None
        self.decision_runtime = MemoryDecisionRuntime(
            store=candidate_store,
            model=model,
            supplementary_port=self.typescript_port,
        )
        self.validator = MemoryDecisionValidator(
            candidate_store=candidate_store,
            canonical_store=canonical_store,
            policy=validation_policy,
        )
        self.commit_runtime = MemoryCommitRuntime(
            store=candidate_store,
            canonical_store=canonical_store,
        )
        self.outbox = CuratorOutboxDispatcher(
            store=candidate_store,
            event_sink=canonical_store,
            index_sink=index_runtime,
            worker_id=f"{self.worker_id}:outbox",
        )
        timestamp = now_iso()
        self._health = CuratorWorkerHealth(
            worker_id=self.worker_id,
            status="idle",
            current_job_id="",
            processed_jobs=0,
            failed_jobs=0,
            last_error="",
            started_at=timestamp,
            updated_at=timestamp,
        )
        self._health_lock = threading.RLock()

    def process_one(self, *, task_id: str = "", job_id: str = "") -> CuratorRunResult | None:
        self.candidate_store.sweep_expired_jobs(limit=1000)
        claimed = self.candidate_store.claim_job(
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            task_id=task_id,
            job_id=job_id,
        )
        if claimed is None:
            return None
        job, lease = claimed
        self._set_health(status="working", current_job_id=job.job_id, last_error="")
        started_at = now_iso()
        heartbeat = LeaseHeartbeat(
            store=self.candidate_store,
            lease=lease,
            lease_seconds=self.lease_seconds,
            interval_seconds=self.heartbeat_interval_seconds,
        )
        try:
            with heartbeat:
                result = self._process_claim(job, heartbeat, started_at=started_at)
        except BaseException as error:
            self._settle_error(job, heartbeat.current_lease, error)
            self._set_health(
                status="degraded",
                current_job_id="",
                failed_increment=1,
                last_error=f"{type(error).__name__}: {str(error)[:500]}",
            )
            raise
        self._set_health(
            status="idle",
            current_job_id="",
            processed_increment=1,
            last_error="",
        )
        return result

    def process_job(self, job_id: str) -> CuratorRunResult:
        job = self.candidate_store.job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state not in {CuratorJobState.QUEUED, CuratorJobState.RETRY_WAIT}:
            raise CuratorRuntimeError(f"job is not claimable: {job.state.value}")
        result = self.process_one(task_id=job.request.task_id, job_id=job_id)
        if result is None or result.job_id != job_id:
            raise CuratorRuntimeError("another task job was selected unexpectedly")
        return result

    def drain(self, *, task_id: str = "", limit: int = 100) -> tuple[CuratorRunResult, ...]:
        results: list[CuratorRunResult] = []
        for _ in range(max(0, int(limit))):
            result = self.process_one(task_id=task_id)
            if result is None:
                break
            results.append(result)
        return tuple(results)

    def recover(self, *, outbox_limit: int | None = None) -> Mapping[str, Any]:
        stale_jobs = self.candidate_store.sweep_expired_jobs(limit=10_000)
        stale_outbox = self.candidate_store.sweep_expired_outbox(limit=10_000)
        outbox = self.outbox.drain(limit=outbox_limit or self.outbox_limit)
        return {
            "stale_job_ids": list(stale_jobs),
            "stale_outbox_message_ids": list(stale_outbox),
            "outbox": dict(outbox),
            "recovery_is_idempotent": True,
        }

    def health(self) -> Mapping[str, Any]:
        with self._health_lock:
            worker = self._health.to_dict()
        return {
            "worker": worker,
            "store": dict(self.candidate_store.health()),
            "index_runtime_configured": self.index_runtime is not None,
            "artifact_store_configured": self.artifact_store is not None,
            "runtime_event_ingress_configured": self.trace_ingress is not None,
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "model_can_write": False,
            "typescript_supplement": (
                dict(self.typescript_port.health())
                if self.typescript_port is not None and hasattr(self.typescript_port, "health")
                else {"available": False, "enabled": False}
            ),
        }

    def _process_claim(
        self,
        job: CuratorJob,
        heartbeat: LeaseHeartbeat,
        *,
        started_at: str,
    ) -> CuratorRunResult:
        request = job.request
        state = self.canonical_store.load_task(request.task_id)
        if state is None:
            raise KeyError(f"task not found: {request.task_id}")
        if state.run_id != request.run_id:
            raise CuratorTaskMismatchError("curator request run does not match task checkpoint")
        ingress_snapshot = None
        if self.trace_ingress is not None:
            ingress_snapshot = self.trace_ingress.prepare(
                run_id=request.run_id,
                task_id=request.task_id,
                input_watermark=request.input_watermark,
            )
            bounded_events = tuple(ingress_snapshot.extraction_events)
            if request.input_watermark != len(bounded_events):
                raise CuratorTaskMismatchError(
                    "runtime ingress does not cover the scheduled input watermark"
                )
        else:
            events = self.canonical_store.task_events(request.task_id)
            if request.input_watermark > len(events):
                raise CuratorTaskMismatchError("curator request watermark exceeds event log")
            bounded_events = events[: request.input_watermark]
        lease = heartbeat.assert_healthy()
        self.candidate_store.transition_job(
            lease,
            before=CuratorJobState.CLAIMED,
            after=CuratorJobState.EXTRACTING,
            metadata={"event_count": len(bounded_events)},
        )
        extraction = self.extractor.extract(
            run_id=request.run_id,
            task_id=request.task_id,
            events=bounded_events,
            artifacts=tuple(state.artifacts),
            start_sequence=request.evidence_start,
            end_sequence=request.evidence_end,
        )
        lease = heartbeat.assert_healthy()
        self.candidate_store.transition_job(
            lease,
            before=CuratorJobState.EXTRACTING,
            after=CuratorJobState.DECIDING,
            metadata={
                "bundle_id": extraction.bundle.bundle_id,
                "evidence_count": len(extraction.documents),
            },
        )
        generation = self.decision_runtime.decide(
            extraction,
            allow_model_assist=request.allow_model_assist,
            max_candidates=request.max_candidates,
        )
        lease = heartbeat.assert_healthy()
        self.candidate_store.transition_job(
            lease,
            before=CuratorJobState.DECIDING,
            after=CuratorJobState.VALIDATING,
            metadata={
                "candidate_count": len(generation.candidates),
                "model_status": generation.model_status,
            },
        )
        decisions = self.validator.validate_many(generation.candidates)
        lease = heartbeat.assert_healthy()
        self.candidate_store.transition_job(
            lease,
            before=CuratorJobState.VALIDATING,
            after=CuratorJobState.COMMITTING,
            metadata={
                "decision_count": len(decisions),
                "accepted_count": sum(1 for item in decisions if item.accepted),
            },
        )
        candidate_by_id = {item.candidate_id: item for item in generation.candidates}
        receipts: list[MemoryCommitReceipt] = []
        for decision in decisions:
            heartbeat.assert_healthy()
            candidate = candidate_by_id[decision.candidate_id]
            receipts.append(self.commit_runtime.commit(candidate, decision))
        heartbeat.assert_healthy()
        outbox_result = self.outbox.drain(limit=self.outbox_limit)
        committed_count = sum(
            1
            for receipt in receipts
            if receipt.disposition in {
                CommitDisposition.COMMITTED,
                CommitDisposition.ALREADY_COMMITTED,
            }
        )
        settled = self.candidate_store.succeed_job(
            heartbeat.current_lease,
            candidate_count=len(generation.candidates),
            committed_count=committed_count,
        )
        rejected = tuple(
            decision.candidate_id
            for decision in decisions
            if not decision.accepted
        )
        return CuratorRunResult(
            request_id=request.request_id,
            job_id=job.job_id,
            run_id=request.run_id,
            task_id=request.task_id,
            status=settled.state.value,
            input_watermark=request.input_watermark,
            success_watermark=settled.last_success_watermark,
            evidence_bundle_id=extraction.bundle.bundle_id,
            candidate_ids=tuple(item.candidate_id for item in generation.candidates),
            decision_ids=tuple(item.decision_id for item in decisions),
            receipts=tuple(receipts),
            rejected_candidate_ids=rejected,
            model_status=generation.model_status,
            outbox_pending=int(outbox_result.get("pending_count", 0)),
            started_at=started_at,
            finished_at=now_iso(),
            diagnostics={
                "extraction": extraction.diagnostics.to_dict(),
                "generation": generation.to_dict(),
                "outbox": dict(outbox_result),
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "candidate_store_owner": "CuratorCandidateStore",
                "model_can_write": False,
                "runtime_event_ingress": (
                    ingress_snapshot.to_dict(include_events=False)
                    if ingress_snapshot is not None
                    else {
                        "direct_spine": False,
                        "fallback_used": True,
                        "source_mode": "legacy_event_projection",
                    }
                ),
            },
        )

    def _settle_error(
        self,
        job: CuratorJob,
        lease: CuratorJobLease,
        error: BaseException,
    ) -> None:
        retryable = isinstance(error, (TimeoutError, OSError, CuratorLeaseLostError))
        try:
            self.candidate_store.fail_job(
                lease,
                error_code=type(error).__name__.casefold(),
                error_message=str(error),
                retryable=retryable,
                retry_delay_seconds=1.0,
            )
        except CuratorLeaseLostError:
            return

    def _set_health(
        self,
        *,
        status: str,
        current_job_id: str,
        processed_increment: int = 0,
        failed_increment: int = 0,
        last_error: str,
    ) -> None:
        with self._health_lock:
            self._health = CuratorWorkerHealth(
                worker_id=self._health.worker_id,
                status=status,
                current_job_id=current_job_id,
                processed_jobs=self._health.processed_jobs + processed_increment,
                failed_jobs=self._health.failed_jobs + failed_increment,
                last_error=last_error,
                started_at=self._health.started_at,
                updated_at=now_iso(),
            )


__all__ = [
    "CuratorNoWorkError",
    "CuratorRunScheduler",
    "CuratorRuntimeError",
    "CuratorTaskMismatchError",
    "CuratorWorkerHealth",
    "LeaseHeartbeat",
    "MemoryCuratorWorker",
    "SchedulerReceipt",
]
