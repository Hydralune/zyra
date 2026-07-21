"""Zyra-owned retrieval-index worker lifecycle.

The acquire/heartbeat/build/finalize/sweep control flow is a cropped,
same-language integration of AgentScope's ``_index_worker.py``,
``_index_task_consumer.py`` and ``_index_sweeper.py`` at commit
``b6698c5dbaa1aa916925e27402767f45e2405fa4``. Zyra replaces AgentScope's
message bus, knowledge-document store and vector-store write with its durable
SQLite job queue, staged generation store and atomic publication fence. The
lease-loss stop condition, bounded drain, durable failure sink and stale-work
recovery remain on the production control path.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .index_jobs import IndexBuildQueue, IndexLeaseStore, SweepResult
from .retrieval_models import (
    IndexDocument,
    IndexJob,
    IndexLease,
    IndexPublication,
    LeaseLostError,
    PublicationFencedError,
)
from .retrieval_store import SQLiteRetrievalIndex


class IndexDocumentLoader(Protocol):
    def __call__(self, job: IndexJob) -> Sequence[IndexDocument]:
        ...


WorkerEventSink = Callable[[str, Mapping[str, Any]], None]
WorkerFailpoint = Callable[[str, IndexLease], None]


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    worker_id: str
    status: str
    job_id: str = ""
    scope_key: str = ""
    generation: int = 0
    document_count: int = 0
    publication: IndexPublication | None = None
    error: str = ""
    fenced: bool = False
    elapsed_ms: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "status": self.status,
            "job_id": self.job_id,
            "scope_key": self.scope_key,
            "generation": self.generation,
            "document_count": self.document_count,
            "publication": self.publication.to_dict() if self.publication else None,
            "error": self.error,
            "fenced": self.fenced,
            "elapsed_ms": self.elapsed_ms,
            "metadata": dict(self.metadata),
        }


class _HeartbeatRuntime:
    def __init__(
        self,
        leases: IndexLeaseStore,
        lease: IndexLease,
        *,
        ttl_seconds: float,
        interval_seconds: float,
        event_sink: WorkerEventSink | None = None,
    ) -> None:
        self.leases = leases
        self.lease = lease
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = interval_seconds
        self.event_sink = event_sink
        self.stop_event = threading.Event()
        self.lost_event = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"index-heartbeat:{lease.job_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(1.0, self.interval_seconds * 2.0))

    def ensure_owned(self) -> None:
        if self.lost_event.is_set():
            error = self.error or LeaseLostError("index heartbeat lost its lease")
            raise LeaseLostError(str(error))

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.lease = self.leases.heartbeat(self.lease, ttl_seconds=self.ttl_seconds)
                if self.event_sink is not None:
                    self.event_sink(
                        "index.worker.heartbeat",
                        {
                            "job_id": self.lease.job_id,
                            "scope_key": self.lease.scope_key,
                            "generation": self.lease.generation,
                            "worker_id": self.lease.worker_id,
                            "lease_epoch": self.lease.epoch,
                            "effective_transition": False,
                        },
                    )
            except BaseException as error:  # noqa: BLE001 - communicated to the pipeline thread.
                self.error = error
                self.lost_event.set()
                return


class IndexWorkerRuntime:
    """Lease-guarded build → stage → publish pipeline.

    The worker never writes the live index directly.  It builds a private
    generation, then the store validates generation, token, owner, state, and
    lease deadline inside the publication transaction.  Killing the process
    leaves only staging rows and an expiring lease.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        queue: IndexBuildQueue,
        leases: IndexLeaseStore,
        index: SQLiteRetrievalIndex,
        loader: IndexDocumentLoader,
        lease_ttl_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
        stage_batch_size: int = 128,
        event_sink: WorkerEventSink | None = None,
        failpoint: WorkerFailpoint | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        interval = heartbeat_interval_seconds
        if interval is None:
            interval = max(0.05, lease_ttl_seconds / 3.0)
        if interval <= 0 or interval >= lease_ttl_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease TTL")
        if stage_batch_size <= 0:
            raise ValueError("stage_batch_size must be positive")
        self.worker_id = worker_id
        self.queue = queue
        self.leases = leases
        self.index = index
        self.loader = loader
        self.lease_ttl_seconds = float(lease_ttl_seconds)
        self.heartbeat_interval_seconds = float(interval)
        self.stage_batch_size = int(stage_batch_size)
        self.event_sink = event_sink
        self.failpoint = failpoint

    def process_one(self, *, scope_key: str = "") -> WorkerOutcome:
        started = time.perf_counter()
        lease = self.leases.acquire(
            worker_id=self.worker_id,
            ttl_seconds=self.lease_ttl_seconds,
            scope_key=scope_key,
        )
        if lease is None:
            return WorkerOutcome(
                worker_id=self.worker_id,
                status="idle",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
            )
        heartbeat = _HeartbeatRuntime(
            self.leases,
            lease,
            ttl_seconds=self.lease_ttl_seconds,
            interval_seconds=self.heartbeat_interval_seconds,
            event_sink=self.event_sink,
        )
        heartbeat.start()
        document_count = 0
        try:
            self._reach("leased", lease)
            self.leases.start_build(lease)
            self._reach("building", lease)
            job = self.queue.require(lease.job_id)
            documents = tuple(self.loader(job))
            heartbeat.ensure_owned()
            self._validate_documents(lease, documents)
            if not documents:
                self.index.stage_documents(lease, ())
            else:
                for offset in range(0, len(documents), self.stage_batch_size):
                    heartbeat.ensure_owned()
                    batch = documents[offset : offset + self.stage_batch_size]
                    self.index.stage_document_batch(
                        lease,
                        batch,
                        replace=(offset == 0),
                    )
                    document_count += len(batch)
                    self._reach("staged_batch", lease)
            heartbeat.ensure_owned()
            self._reach("staged", lease)
            self.leases.start_publish(lease)
            self._reach("publishing", lease)
            heartbeat.ensure_owned()
            publication = self.index.publish(lease)
            self._reach("ready", lease)
            outcome = WorkerOutcome(
                worker_id=self.worker_id,
                status="ready",
                job_id=lease.job_id,
                scope_key=lease.scope_key,
                generation=lease.generation,
                document_count=document_count,
                publication=publication,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                metadata={
                    "lease_epoch": lease.epoch,
                    "stage_batch_size": self.stage_batch_size,
                    "canonical_write": False,
                },
            )
            self._emit("index.worker.completed", outcome.to_dict())
            return outcome
        except (LeaseLostError, PublicationFencedError) as error:
            outcome = WorkerOutcome(
                worker_id=self.worker_id,
                status="fenced",
                job_id=lease.job_id,
                scope_key=lease.scope_key,
                generation=lease.generation,
                document_count=document_count,
                error=f"{type(error).__name__}: {error}",
                fenced=True,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                metadata={"lease_epoch": lease.epoch},
            )
            self._emit("index.worker.fenced", outcome.to_dict())
            return outcome
        except Exception as error:  # noqa: BLE001 - durable failure sink.
            try:
                self.leases.fail(lease, error, retryable=self._retryable(error))
            except LeaseLostError:
                pass
            outcome = WorkerOutcome(
                worker_id=self.worker_id,
                status="failed",
                job_id=lease.job_id,
                scope_key=lease.scope_key,
                generation=lease.generation,
                document_count=document_count,
                error=f"{type(error).__name__}: {error}",
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                metadata={"lease_epoch": lease.epoch, "retryable": self._retryable(error)},
            )
            self._emit("index.worker.failed", outcome.to_dict())
            return outcome
        finally:
            heartbeat.stop()

    def drain(
        self,
        *,
        maximum_jobs: int = 100,
        scope_key: str = "",
    ) -> tuple[WorkerOutcome, ...]:
        outcomes: list[WorkerOutcome] = []
        for _ in range(max(0, int(maximum_jobs))):
            outcome = self.process_one(scope_key=scope_key)
            if outcome.status == "idle":
                break
            outcomes.append(outcome)
        return tuple(outcomes)

    def run_forever(
        self,
        *,
        stop_event: threading.Event,
        poll_interval_seconds: float = 0.25,
        sweep: "StaleLeaseSweeper | None" = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        while not stop_event.is_set():
            if sweep is not None:
                sweep.sweep_once()
            outcome = self.process_one()
            if outcome.status == "idle":
                stop_event.wait(poll_interval_seconds)

    def _reach(self, name: str, lease: IndexLease) -> None:
        self._emit(
            f"index.worker.{name}",
            {
                "job_id": lease.job_id,
                "scope_key": lease.scope_key,
                "generation": lease.generation,
                "worker_id": lease.worker_id,
                "lease_epoch": lease.epoch,
            },
        )
        if self.failpoint is not None:
            self.failpoint(name, lease)

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(event_type, payload)

    @staticmethod
    def _validate_documents(lease: IndexLease, documents: Sequence[IndexDocument]) -> None:
        identities: set[str] = set()
        for document in documents:
            if document.scope_key != lease.scope_key:
                raise ValueError(
                    f"loader returned document for {document.scope_key!r}; expected {lease.scope_key!r}"
                )
            if document.document_id in identities:
                raise ValueError(f"duplicate document_id returned by loader: {document.document_id}")
            identities.add(document.document_id)

    @staticmethod
    def _retryable(error: BaseException) -> bool:
        return isinstance(error, (TimeoutError, ConnectionError, OSError))


class StaleLeaseSweeper:
    def __init__(
        self,
        leases: IndexLeaseStore,
        *,
        event_sink: WorkerEventSink | None = None,
        limit: int = 100,
    ) -> None:
        self.leases = leases
        self.event_sink = event_sink
        self.limit = max(1, int(limit))

    def sweep_once(self) -> SweepResult:
        result = self.leases.sweep_expired(limit=self.limit)
        if result.count and self.event_sink is not None:
            self.event_sink("index.sweeper.requeued", result.to_dict())
        return result
