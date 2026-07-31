"""Zyra-owned code-index worker lifecycle.

This module reuses the cropped same-language acquire/heartbeat/pipeline/
finalize/sweep control flow from AgentScope's index worker family at commit
``b6698c5dbaa1aa916925e27402767f45e2405fa4``. Workspace revision guards,
generation candidates and atomic publication fencing replace AgentScope's
document parser/vector-store stages while preserving the mature lease-loss
and recovery lifecycle inside Zyra's canonical schemas and stores.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .job_models import (
    CodeIndexBuildJob,
    CodeIndexBuildLease,
    CodeIndexLeaseLost,
    CodeIndexPublicationFenced,
    CodeIndexWorkerOutcome,
)
from .jobs import CodeIndexBuildQueue
from .runtime import CodeIndexRuntime


CodeIndexRuntimeFactory = Callable[[CodeIndexBuildJob], CodeIndexRuntime]
CodeIndexWorkerEventSink = Callable[[str, Mapping[str, Any]], None]
CodeIndexFailpoint = Callable[[str, CodeIndexBuildLease], None]
CodeIndexSourceGuard = Callable[[CodeIndexBuildJob], None]


@dataclass(slots=True)
class _Heartbeat:
    queue: CodeIndexBuildQueue
    lease: CodeIndexBuildLease
    ttl_seconds: float
    interval_seconds: float
    stop: threading.Event
    lost: threading.Event
    error: BaseException | None = None

    def run(self) -> None:
        while not self.stop.wait(self.interval_seconds):
            try:
                self.lease = self.queue.renew(
                    self.lease,
                    lease_ttl_seconds=self.ttl_seconds,
                )
            except BaseException as error:  # noqa: BLE001 - worker must fence on any heartbeat loss.
                self.error = error
                self.lost.set()
                return


class CodeIndexWorkerRuntime:
    """Lease-guarded code-index builder and fenced publisher."""

    def __init__(
        self,
        *,
        worker_id: str,
        queue: CodeIndexBuildQueue,
        runtime_factory: CodeIndexRuntimeFactory,
        lease_ttl_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
        event_sink: CodeIndexWorkerEventSink | None = None,
        failpoint: CodeIndexFailpoint | None = None,
        source_guard: CodeIndexSourceGuard | None = None,
    ) -> None:
        worker = worker_id.strip()
        if not worker:
            raise ValueError("worker_id is required")
        self.worker_id = worker
        self.queue = queue
        self.runtime_factory = runtime_factory
        self.lease_ttl_seconds = max(0.1, float(lease_ttl_seconds))
        default_interval = max(0.05, self.lease_ttl_seconds / 3)
        self.heartbeat_interval_seconds = max(
            0.025,
            min(
                float(heartbeat_interval_seconds or default_interval),
                self.lease_ttl_seconds / 2,
            ),
        )
        self.event_sink = event_sink
        self.failpoint = failpoint
        self.source_guard = source_guard

    def process_one(self, *, workspace_id: str = "") -> CodeIndexWorkerOutcome:
        lease = self.queue.claim(
            worker_id=self.worker_id,
            workspace_id=workspace_id,
            lease_ttl_seconds=self.lease_ttl_seconds,
        )
        if lease is None:
            return CodeIndexWorkerOutcome(status="idle", worker_id=self.worker_id)
        heartbeat = _Heartbeat(
            queue=self.queue,
            lease=lease,
            ttl_seconds=self.lease_ttl_seconds,
            interval_seconds=self.heartbeat_interval_seconds,
            stop=threading.Event(),
            lost=threading.Event(),
        )
        thread = threading.Thread(
            target=heartbeat.run,
            name=f"code-index-heartbeat:{lease.job_id}",
            daemon=True,
        )
        thread.start()
        try:
            heartbeat.lease = self.queue.renew(
                heartbeat.lease,
                lease_ttl_seconds=self.lease_ttl_seconds,
            )
            self._phase("leased", heartbeat.lease)
            job = self.queue.start_build(heartbeat.lease)
            self._guard_source(job)
            self._phase("building", heartbeat.lease)
            runtime = self.runtime_factory(job)
            if runtime.source.identity.workspace_id != job.workspace_id:
                raise RuntimeError("runtime factory returned another workspace")
            if runtime.source.identity.revision != job.source_revision:
                raise RuntimeError(
                    "canonical workspace revision changed after code-index job admission"
                )
            candidate = runtime.prepare_candidate(
                generation=job.generation,
                changed_paths=job.changed_paths,
                deleted_paths=job.deleted_paths,
            )
            self._phase("candidate_built", heartbeat.lease)
            self._require_heartbeat(heartbeat)
            self._guard_source(job)
            live_lease = heartbeat.lease
            self.queue.begin_publish(live_lease)
            self._phase("publishing", live_lease)
            self._require_heartbeat(heartbeat)
            self._guard_source(job)
            result = runtime.publish_candidate(candidate, lease=heartbeat.lease)
            self._phase("published", heartbeat.lease)
            self._require_heartbeat(heartbeat)
            ready = self.queue.mark_ready(
                heartbeat.lease,
                content_digest=result.content_digest,
                counts={
                    "file_count": result.file_count,
                    "symbol_count": result.symbol_count,
                    "reference_count": result.reference_count,
                    "call_edge_count": result.call_edge_count,
                },
            )
            self._phase("ready", heartbeat.lease)
            self._emit(
                "code_index.worker.ready",
                {
                    "job_id": ready.job_id,
                    "workspace_id": ready.workspace_id,
                    "generation": ready.generation,
                    "source_revision": ready.source_revision,
                    "worker_id": self.worker_id,
                    "content_digest": result.content_digest,
                },
            )
            return CodeIndexWorkerOutcome(
                status="ready",
                worker_id=self.worker_id,
                workspace_id=ready.workspace_id,
                job_id=ready.job_id,
                generation=ready.generation,
                source_revision=ready.source_revision,
                content_digest=result.content_digest,
                file_count=result.file_count,
                symbol_count=result.symbol_count,
                reference_count=result.reference_count,
                call_edge_count=result.call_edge_count,
            )
        except (CodeIndexLeaseLost, CodeIndexPublicationFenced) as error:
            self._emit(
                "code_index.worker.fenced",
                {
                    "job_id": lease.job_id,
                    "workspace_id": lease.workspace_id,
                    "generation": lease.generation,
                    "source_revision": lease.source_revision,
                    "worker_id": self.worker_id,
                    "error_code": getattr(error, "code", "code_index_worker_fenced"),
                },
            )
            return CodeIndexWorkerOutcome(
                status="fenced",
                worker_id=self.worker_id,
                workspace_id=lease.workspace_id,
                job_id=lease.job_id,
                generation=lease.generation,
                source_revision=lease.source_revision,
                error_code=getattr(error, "code", "code_index_worker_fenced"),
                error_message=str(error),
                fenced=True,
            )
        except BaseException as error:  # noqa: BLE001 - terminal job failure is persisted.
            code = getattr(error, "code", type(error).__name__)
            try:
                self.queue.fail(
                    heartbeat.lease,
                    error_code=str(code),
                    error_message=str(error),
                )
            except CodeIndexLeaseLost:
                return CodeIndexWorkerOutcome(
                    status="fenced",
                    worker_id=self.worker_id,
                    workspace_id=lease.workspace_id,
                    job_id=lease.job_id,
                    generation=lease.generation,
                    source_revision=lease.source_revision,
                    error_code="failure_write_fenced",
                    error_message=str(error),
                    fenced=True,
                )
            self._emit(
                "code_index.worker.failed",
                {
                    "job_id": lease.job_id,
                    "workspace_id": lease.workspace_id,
                    "generation": lease.generation,
                    "source_revision": lease.source_revision,
                    "worker_id": self.worker_id,
                    "error_code": str(code),
                    "error_message": str(error)[:1_000],
                },
            )
            return CodeIndexWorkerOutcome(
                status="failed",
                worker_id=self.worker_id,
                workspace_id=lease.workspace_id,
                job_id=lease.job_id,
                generation=lease.generation,
                source_revision=lease.source_revision,
                error_code=str(code),
                error_message=str(error),
            )
        finally:
            heartbeat.stop.set()
            thread.join(timeout=max(1.0, self.heartbeat_interval_seconds * 2))

    def drain(
        self,
        *,
        maximum_jobs: int = 64,
        workspace_id: str = "",
    ) -> tuple[CodeIndexWorkerOutcome, ...]:
        if maximum_jobs < 1 or maximum_jobs > 10_000:
            raise ValueError("maximum_jobs must be between 1 and 10000")
        outcomes: list[CodeIndexWorkerOutcome] = []
        for _ in range(maximum_jobs):
            outcome = self.process_one(workspace_id=workspace_id)
            if outcome.status == "idle":
                break
            outcomes.append(outcome)
        return tuple(outcomes)

    def run_forever(
        self,
        *,
        stop_event: threading.Event,
        poll_interval_seconds: float = 0.25,
        sweep_interval_seconds: float = 5.0,
    ) -> None:
        poll = max(0.01, float(poll_interval_seconds))
        sweep_interval = max(poll, float(sweep_interval_seconds))
        next_sweep = time.monotonic()
        while not stop_event.is_set():
            now = time.monotonic()
            if now >= next_sweep:
                sweep = self.queue.sweep_expired()
                if sweep.count:
                    self._emit("code_index.sweeper.requeued", sweep.to_dict())
                next_sweep = now + sweep_interval
            outcome = self.process_one()
            if outcome.status == "idle":
                stop_event.wait(poll)

    def _phase(self, phase: str, lease: CodeIndexBuildLease) -> None:
        self._emit(
            f"code_index.worker.{phase}",
            {
                "job_id": lease.job_id,
                "workspace_id": lease.workspace_id,
                "generation": lease.generation,
                "source_revision": lease.source_revision,
                "worker_id": lease.worker_id,
                "lease_epoch": lease.epoch,
                "lease_expires_at": lease.expires_at,
            },
        )
        if self.failpoint is not None:
            self.failpoint(phase, lease)

    def _guard_source(self, job: CodeIndexBuildJob) -> None:
        if self.source_guard is not None:
            self.source_guard(job)

    @staticmethod
    def _require_heartbeat(heartbeat: _Heartbeat) -> None:
        if heartbeat.lost.is_set():
            error = heartbeat.error
            if isinstance(error, BaseException):
                raise CodeIndexLeaseLost(
                    "code_index_heartbeat_lost",
                    f"code index worker lost its lease heartbeat: {error}",
                ) from error
            raise CodeIndexLeaseLost(
                "code_index_heartbeat_lost",
                "code index worker lost its lease heartbeat",
            )

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(event_type, payload)


__all__ = ["CodeIndexWorkerRuntime"]
