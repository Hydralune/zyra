from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, now_iso
from zyra_memory import MemoryIndexRuntime
from zyra_memory.curator_context import CuratorRecallVerifier
from zyra_memory.curator_delivery import (
    AuditContractConsumer,
    CompactRuntimeContractConsumer,
    CuratorDeliveryDrainReport,
    CuratorDownstreamDispatcher,
    FaultObserverContractConsumer,
    RecoveryPlannerContractConsumer,
    RetrievalContextConsumer,
    SkillMemoryContractConsumer,
)
from zyra_memory.curator_integration_models import (
    CuratorConsumer,
    CuratorFailureContract,
    CuratorInputBatch,
    CuratorIntegrationRun,
    CuratorIntegrationRunState,
    CuratorOutcome,
    CuratorProjectionReport,
)
from zyra_memory.curator_integration_store import CuratorIntegrationStore
from zyra_memory.curator_models import CuratorRunResult, stable_id
from zyra_memory.curator_outcomes import (
    CuratorOutcomeProjector,
    CuratorProjectionAudit,
    CuratorProjectionAuditor,
)
from zyra_memory.curator_runtime import MemoryCuratorWorker

from .memory_curator_ingress import RuntimeEventCuratorIngress


class MemoryCuratorIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = str(code).strip() or "memory_curator_integration_failed"
        self.retryable = bool(retryable)


@dataclass(frozen=True, slots=True)
class IntegratedCuratorResult:
    curator_result: CuratorRunResult
    projection: CuratorProjectionReport
    audit: CuratorProjectionAudit
    delivery_reports: Mapping[str, Mapping[str, Any]]
    input_batch: CuratorInputBatch
    event_ids: tuple[str, ...]
    replayed: bool = False
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.projection.ok and self.audit.ok

    @property
    def integration_run_id(self) -> str:
        return self.projection.integration_run.integration_run_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "curator_result": self.curator_result.to_dict(),
            "projection": self.projection.to_dict(),
            "audit": self.audit.to_dict(),
            "delivery_reports": {
                key: dict(value) for key, value in self.delivery_reports.items()
            },
            "input_batch": self.input_batch.to_dict(),
            "event_ids": list(self.event_ids),
            "replayed": self.replayed,
            "diagnostics": dict(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class CuratorIntegrationRecoveryReport:
    worker: Mapping[str, Any]
    downstream: Mapping[str, Any]
    resumed_integration_run_ids: tuple[str, ...]
    failed_integration_run_ids: tuple[str, ...]
    stale_delivery_ids: tuple[str, ...]
    consistency: Mapping[str, Any]

    @property
    def ok(self) -> bool:
        return not self.failed_integration_run_ids and bool(self.consistency.get("ok"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "worker": dict(self.worker),
            "downstream": dict(self.downstream),
            "resumed_integration_run_ids": list(self.resumed_integration_run_ids),
            "failed_integration_run_ids": list(self.failed_integration_run_ids),
            "stale_delivery_ids": list(self.stale_delivery_ids),
            "consistency": dict(self.consistency),
            "recovery_is_idempotent": True,
        }


class MemoryCuratorIntegrationApplication:
    """06B main path from 05C trace refs to later worker/recovery context.

    ``MemoryCuratorWorker`` remains the primary candidate/validation/commit
    control flow.  This application starts only after that worker returns a
    durable result: it reconstructs phases from the candidate store, publishes
    immutable outcomes and fans them out.  It therefore cannot rescue a
    disabled validator or committer with a report-only fallback.
    """

    AUTO_CONSUMERS = (
        CuratorConsumer.RETRIEVAL_CONTEXT,
        CuratorConsumer.COMPACT_RUNTIME,
        CuratorConsumer.AUDIT,
    )

    DEFERRED_CONSUMERS = (
        CuratorConsumer.SKILL_MEMORY,
        CuratorConsumer.FAULT_OBSERVER,
        CuratorConsumer.RECOVERY_PLANNER,
    )

    def __init__(
        self,
        *,
        worker: MemoryCuratorWorker,
        ingress: RuntimeEventCuratorIngress,
        store: CuratorIntegrationStore,
        memory_index: MemoryIndexRuntime,
        event_sink: Any | None = None,
        worker_id: str = "memory-curator-integration",
        auto_dispatch: bool = True,
    ) -> None:
        self.worker = worker
        self.ingress = ingress
        self.store = store
        self.memory_index = memory_index
        self.event_sink = event_sink
        self.worker_id = str(worker_id).strip() or "memory-curator-integration"
        self.auto_dispatch = bool(auto_dispatch)
        self.projector = CuratorOutcomeProjector(worker.candidate_store)
        self.auditor = CuratorProjectionAuditor()
        self.recall = CuratorRecallVerifier(
            index_runtime=memory_index,
            integration_store=store,
        )
        self.dispatcher = CuratorDownstreamDispatcher(
            store=store,
            handlers=(
                RetrievalContextConsumer(self.recall),
                CompactRuntimeContractConsumer(),
                SkillMemoryContractConsumer(),
                FaultObserverContractConsumer(),
                RecoveryPlannerContractConsumer(),
                AuditContractConsumer(),
            ),
            worker_id=f"{self.worker_id}:downstream",
        )

    def process_one(
        self,
        *,
        task_id: str = "",
        job_id: str = "",
    ) -> IntegratedCuratorResult | None:
        result = self.worker.process_one(task_id=task_id, job_id=job_id)
        if result is None:
            return None
        return self.integrate(result)

    def process_job(self, job_id: str) -> IntegratedCuratorResult:
        result = self.worker.process_job(job_id)
        return self.integrate(result)

    def drain(
        self,
        *,
        task_id: str = "",
        limit: int = 100,
    ) -> tuple[IntegratedCuratorResult, ...]:
        output: list[IntegratedCuratorResult] = []
        for _ in range(max(0, int(limit))):
            result = self.process_one(task_id=task_id)
            if result is None:
                break
            output.append(result)
        return tuple(output)

    def integrate(self, result: CuratorRunResult) -> IntegratedCuratorResult:
        if result.status != "succeeded":
            raise MemoryCuratorIntegrationError(
                "curator_not_succeeded",
                f"cannot integrate curator job in state {result.status}",
            )
        existing = self.store.integration_run_for_job(result.job_id)
        if existing is not None and existing.state is CuratorIntegrationRunState.SUCCEEDED:
            return self._replay_result(result, existing)
        input_batch = self._input_batch(result)
        integration_run = existing or CuratorIntegrationRun.start(
            result=result,
            input_batch=input_batch,
            metadata={
                "application": "MemoryCuratorIntegrationApplication/1",
                "input_owner": "RuntimeEventSpineBridge",
                "candidate_owner": "CuratorCandidateStore",
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "derived_index_owner": "MemoryIndexRuntime",
                "model_can_write": False,
                "curator_result": result.to_dict(),
            },
        )
        if existing is None:
            self.store.save_integration_run(integration_run)
        event_ids: list[str] = []
        try:
            draft = self.projector.project(result)
            audit = self.auditor.audit(draft)
            if not audit.ok:
                raise MemoryCuratorIntegrationError(
                    "projection_audit_failed",
                    "; ".join(audit.findings),
                )
            if integration_run.state is CuratorIntegrationRunState.STARTED:
                integration_run = self._ensure_state(
                    integration_run,
                    CuratorIntegrationRunState.CURATOR_SUCCEEDED,
                    result=result,
                )
            if integration_run.state is CuratorIntegrationRunState.CURATOR_SUCCEEDED:
                integration_run = self._ensure_state(
                    integration_run,
                    CuratorIntegrationRunState.PROJECTING,
                    result=result,
                )
            if integration_run.state is CuratorIntegrationRunState.PROJECTING:
                (
                    integration_run,
                    outcomes,
                    failures,
                    deliveries,
                    duplicate_outcomes,
                ) = self.store.publish_projection(
                    integration_run=integration_run,
                    outcomes=draft.outcomes,
                    failures=draft.failures,
                )
            elif integration_run.state in {
                CuratorIntegrationRunState.PUBLISHED,
                CuratorIntegrationRunState.CONTEXT_VERIFIED,
            }:
                outcomes = self.store.outcomes(
                    job_id=result.job_id,
                    limit=100_000,
                )
                failures = self.store.failures(
                    job_id=result.job_id,
                    limit=100_000,
                )
                deliveries = tuple(
                    {
                        delivery.delivery_id: delivery
                        for outcome in outcomes
                        for delivery in self.store.deliveries(
                            outcome_id=outcome.outcome_id,
                            limit=1000,
                        )
                    }.values()
                )
                duplicate_outcomes = tuple(
                    outcome.outcome_id for outcome in outcomes
                )
            else:
                raise MemoryCuratorIntegrationError(
                    "integration_state_not_resumable",
                    f"cannot resume integration from {integration_run.state.value}",
                )
            # Outcome events use deterministic ids and are safe to replay.  Emit
            # them after both the initial publish and the PUBLISHED resume path
            # so a transient event-sink failure cannot create a permanent gap.
            event_ids.extend(
                self._emit_outcome_events(
                    integration_run=integration_run,
                    outcomes=outcomes,
                    failures=failures,
                )
            )
            delivery_reports = self._dispatch(
                task_id=result.task_id,
                enabled=self.auto_dispatch,
            )
            context_proofs = self.store.context_proofs(
                task_id=result.task_id,
                verified_only=True,
                limit=100_000,
            )
            run_proof_ids = tuple(
                proof.proof_id
                for proof in context_proofs
                if proof.outcome_id in {outcome.outcome_id for outcome in outcomes}
            )
            canonical_outcomes = tuple(
                outcome for outcome in outcomes if outcome.canonical_memory_changed
            )
            if canonical_outcomes and self.auto_dispatch and not run_proof_ids:
                raise MemoryCuratorIntegrationError(
                    "context_proof_missing",
                    "canonical curator outcome did not change 06A retrieval context",
                    retryable=True,
                )
            if (
                run_proof_ids
                and integration_run.state is CuratorIntegrationRunState.PUBLISHED
            ):
                integration_run = integration_run.transition(
                    CuratorIntegrationRunState.CONTEXT_VERIFIED,
                    context_proof_ids=run_proof_ids,
                    metadata={
                        "context_verified": True,
                        "context_proof_count": len(run_proof_ids),
                    },
                )
                self.store.save_integration_run(
                    integration_run,
                    expected_state=CuratorIntegrationRunState.PUBLISHED,
                )
            completion_source_state = integration_run.state
            integration_run = integration_run.transition(
                CuratorIntegrationRunState.SUCCEEDED,
                outcome_ids=[outcome.outcome_id for outcome in outcomes],
                failure_ids=[failure.failure_id for failure in failures],
                delivery_ids=[delivery.delivery_id for delivery in deliveries],
                context_proof_ids=run_proof_ids,
                model_status=result.model_status,
                canonical_commit_count=sum(
                    1 for outcome in outcomes if outcome.canonical_memory_changed
                ),
                index_publication_count=sum(
                    1 for outcome in outcomes if outcome.index_published
                ),
                metadata={
                    "projection_audit": audit.to_dict(),
                    "delivery_reports": delivery_reports,
                    "duplicate_outcome_ids": list(duplicate_outcomes),
                    "integration_completed": True,
                },
            )
            # Publish the deterministic completion event before committing the
            # terminal state.  If the sink is temporarily unavailable, the
            # durable run remains resumable and recovery can retry the event;
            # a SUCCEEDED run must never hide a missing completion event.
            completed_event_id = self._emit_completed_event(integration_run, audit)
            self.store.save_integration_run(
                integration_run,
                expected_state=completion_source_state,
            )
            if completed_event_id:
                event_ids.append(completed_event_id)
        except BaseException as error:
            self._record_failure(integration_run, result, error)
            raise
        report = CuratorProjectionReport(
            integration_run=integration_run,
            outcomes=tuple(outcomes),
            failures=tuple(failures),
            deliveries=tuple(deliveries),
            context_proofs=tuple(
                proof for proof in context_proofs if proof.proof_id in run_proof_ids
            ),
            duplicate_outcome_ids=tuple(duplicate_outcomes),
            warnings=draft.warnings,
        ).validated()
        return IntegratedCuratorResult(
            curator_result=result,
            projection=report,
            audit=audit,
            delivery_reports=delivery_reports,
            input_batch=input_batch,
            event_ids=tuple(event_ids),
            diagnostics={
                "main_path": (
                    "RuntimeEventSpineBridge -> MemoryCuratorWorker -> "
                    "MemoryDecisionValidator -> MemoryCommitRuntime -> "
                    "CuratorIntegrationStore -> MemoryIndexRuntime -> worker context"
                ),
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "candidate_owner": "CuratorCandidateStore",
                "outcome_owner": "CuratorIntegrationStore",
                "model_can_write": False,
                "direct_runtime_event_input": bool(
                    input_batch.metadata.get("direct_spine", False)
                ),
                "deferred_consumers": [
                    consumer.value for consumer in self.DEFERRED_CONSUMERS
                ],
            },
        )

    def _input_batch(self, result: CuratorRunResult) -> CuratorInputBatch:
        diagnostics = result.diagnostics
        ingress = diagnostics.get("runtime_event_ingress")
        if isinstance(ingress, Mapping):
            batch_value = ingress.get("batch")
            if isinstance(batch_value, Mapping):
                batch_id = str(batch_value.get("batch_id") or "")
                if batch_id:
                    batch = self.store.input_batch(batch_id)
                    if batch is not None:
                        return batch
        cursor = self.store.input_cursor(result.task_id)
        batch_id = str(cursor.get("batch_id") or "")
        if batch_id:
            batch = self.store.input_batch(batch_id)
            if batch is not None:
                return batch
        snapshot = self.ingress.prepare(
            run_id=result.run_id,
            task_id=result.task_id,
            input_watermark=result.input_watermark,
        )
        return snapshot.batch

    def _ensure_state(
        self,
        run: CuratorIntegrationRun,
        target: CuratorIntegrationRunState,
        *,
        result: CuratorRunResult,
    ) -> CuratorIntegrationRun:
        if run.state is target:
            return run
        if run.state is CuratorIntegrationRunState.FAILED:
            raise MemoryCuratorIntegrationError(
                "integration_previously_failed",
                run.error_message or run.error_code,
            )
        next_run = run.transition(
            target,
            model_status=result.model_status,
            metadata={
                "curator_status": result.status,
                "curator_success_watermark": result.success_watermark,
                "curator_outbox_pending": result.outbox_pending,
            },
        )
        self.store.save_integration_run(next_run, expected_state=run.state)
        return next_run

    def _dispatch(
        self,
        *,
        task_id: str,
        enabled: bool,
    ) -> Mapping[str, Mapping[str, Any]]:
        reports: dict[str, Mapping[str, Any]] = {}
        if not enabled:
            return reports
        for consumer in self.AUTO_CONSUMERS:
            report = self.dispatcher.drain(
                consumer,
                task_id=task_id,
                limit=10_000,
            )
            reports[consumer.value] = report.to_dict()
            if not report.ok:
                raise MemoryCuratorIntegrationError(
                    "downstream_dispatch_failed",
                    f"{consumer.value} has dead or non-retryable delivery",
                    retryable=False,
                )
        return reports

    def dispatch_consumer(
        self,
        consumer: CuratorConsumer,
        *,
        task_id: str = "",
        limit: int = 100,
    ) -> CuratorDeliveryDrainReport:
        return self.dispatcher.drain(
            consumer,
            task_id=task_id,
            limit=limit,
        )

    def recover(self, *, limit: int = 10_000) -> CuratorIntegrationRecoveryReport:
        worker_recovery = self.worker.recover(outbox_limit=limit)
        stale = self.store.sweep_expired_deliveries(limit=limit)
        downstream = self.dispatcher.recover(limit=limit)
        resumed: list[str] = []
        failed: list[str] = []
        active = self.store.integration_runs(
            states=(
                CuratorIntegrationRunState.STARTED,
                CuratorIntegrationRunState.CURATOR_SUCCEEDED,
                CuratorIntegrationRunState.PROJECTING,
                CuratorIntegrationRunState.PUBLISHED,
                CuratorIntegrationRunState.CONTEXT_VERIFIED,
            ),
            limit=limit,
        )
        for run in active:
            job = self.worker.candidate_store.job(run.curator_job_id)
            if job is None or job.state.value != "succeeded":
                failed.append(run.integration_run_id)
                continue
            result = self._result_from_durable_job(run.curator_job_id)
            if result is None:
                failed.append(run.integration_run_id)
                continue
            try:
                self.integrate(result)
            except BaseException:
                failed.append(run.integration_run_id)
            else:
                resumed.append(run.integration_run_id)
        consistency = self.store.consistency_report()
        return CuratorIntegrationRecoveryReport(
            worker=worker_recovery,
            downstream=downstream,
            resumed_integration_run_ids=tuple(resumed),
            failed_integration_run_ids=tuple(failed),
            stale_delivery_ids=tuple(stale),
            consistency=consistency,
        )

    def status(self, *, task_id: str = "") -> Mapping[str, Any]:
        return {
            "task_id": task_id,
            "worker": self.worker.health(),
            "integration_store": self.store.health(task_id=task_id),
            "dispatcher": self.dispatcher.health(task_id=task_id),
            "runs": [
                run.to_dict()
                for run in self.store.integration_runs(task_id=task_id, limit=100)
            ],
            "outcomes": [
                outcome.to_dict()
                for outcome in self.store.outcomes(task_id=task_id, limit=1000)
            ],
            "failures": [
                failure.to_dict()
                for failure in self.store.failures(task_id=task_id, limit=1000)
            ],
            "deliveries": [
                delivery.to_dict()
                for delivery in self.store.deliveries(task_id=task_id, limit=1000)
            ],
            "context_proofs": [
                proof.to_dict()
                for proof in self.store.context_proofs(task_id=task_id, limit=1000)
            ],
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "projection_store_is_canonical_memory": False,
            "input_owner": "RuntimeEventSpineBridge",
            "model_can_write": False,
        }

    def _replay_result(
        self,
        result: CuratorRunResult,
        run: CuratorIntegrationRun,
    ) -> IntegratedCuratorResult:
        input_batch = self.store.input_batch(run.input_batch_id)
        if input_batch is None:
            raise MemoryCuratorIntegrationError(
                "input_batch_missing",
                "completed integration run lost its input batch",
            )
        outcomes = self.store.outcomes(job_id=result.job_id, limit=100_000)
        failures = self.store.failures(job_id=result.job_id, limit=100_000)
        deliveries = tuple(
            delivery
            for outcome in outcomes
            for delivery in self.store.deliveries(
                outcome_id=outcome.outcome_id,
                limit=1000,
            )
        )
        proofs = tuple(
            proof
            for outcome in outcomes
            for proof in self.store.context_proofs(
                outcome_id=outcome.outcome_id,
                limit=1000,
            )
        )
        draft = self.projector.project(result)
        audit = self.auditor.audit(draft)
        event_ids = list(
            self._emit_outcome_events(
                integration_run=run,
                outcomes=outcomes,
                failures=failures,
            )
        )
        event_ids.append(self._emit_completed_event(run, audit))
        report = CuratorProjectionReport(
            integration_run=run,
            outcomes=outcomes,
            failures=failures,
            deliveries=tuple(
                {delivery.delivery_id: delivery for delivery in deliveries}.values()
            ),
            context_proofs=tuple(
                {proof.proof_id: proof for proof in proofs}.values()
            ),
            duplicate_outcome_ids=tuple(outcome.outcome_id for outcome in outcomes),
            replayed_delivery_ids=tuple(delivery.delivery_id for delivery in deliveries),
            warnings=("idempotent_integration_replay",),
        ).validated()
        return IntegratedCuratorResult(
            curator_result=result,
            projection=report,
            audit=audit,
            delivery_reports={},
            input_batch=input_batch,
            event_ids=tuple(item for item in event_ids if item),
            replayed=True,
            diagnostics={
                "idempotent_replay": True,
                "canonical_memory_owner": "SQLiteStore.memory_records",
            },
        )

    def _record_failure(
        self,
        run: CuratorIntegrationRun,
        result: CuratorRunResult,
        error: BaseException,
    ) -> None:
        current = self.store.integration_run(run.integration_run_id) or run
        if current.terminal:
            return
        retryable = bool(getattr(error, "retryable", False))
        if retryable:
            deferred = replace(
                current,
                updated_at=now_iso(),
                error_code="",
                error_message="",
                metadata={
                    **dict(current.metadata),
                    "last_retryable_error": {
                        "code": (
                            error.code
                            if isinstance(error, MemoryCuratorIntegrationError)
                            else type(error).__name__.casefold()
                        ),
                        "message": str(error)[:2000],
                        "failure_type": type(error).__name__,
                    },
                    "retryable": True,
                    "recovery_required": True,
                    "canonical_task_lifecycle_preserved": True,
                },
            ).validated()
            self.store.save_integration_run(
                deferred,
                expected_state=current.state,
            )
            return
        code = (
            error.code
            if isinstance(error, MemoryCuratorIntegrationError)
            else type(error).__name__.casefold()
        )
        failed = current.transition(
            CuratorIntegrationRunState.FAILED,
            error_code=code,
            error_message=str(error),
            metadata={
                "failure_type": type(error).__name__,
                "retryable": False,
                "canonical_task_lifecycle_preserved": True,
            },
        )
        try:
            self.store.save_integration_run(failed, expected_state=current.state)
        except BaseException:
            return

    def _emit_outcome_events(
        self,
        *,
        integration_run: CuratorIntegrationRun,
        outcomes: Sequence[CuratorOutcome],
        failures: Sequence[CuratorFailureContract],
    ) -> tuple[str, ...]:
        if self.event_sink is None:
            return ()
        event_ids: list[str] = []
        for outcome in outcomes:
            event_type = self._event_type(outcome)
            event_id = stable_id(
                "event",
                event_type.value if hasattr(event_type, "value") else str(event_type),
                outcome.outcome_id,
            )
            event = EventRecord(
                event_id=event_id,
                run_id=outcome.run_id,
                task_id=outcome.task_id,
                event_type=event_type,
                payload={
                    "schema": "zyra.memory-curator-outcome-event/v1",
                    "integration_run_id": integration_run.integration_run_id,
                    "outcome": outcome.to_dict(),
                    "canonical_memory_owner": "SQLiteStore.memory_records",
                    "model_can_write": False,
                },
            )
            self._append_projection_event(event)
            event_ids.append(event_id)
        for failure in failures:
            event_id = stable_id(
                "event",
                "memory_curator_rejected",
                failure.failure_id,
            )
            event = EventRecord(
                event_id=event_id,
                run_id=failure.run_id,
                task_id=failure.task_id,
                event_type=EventType.MEMORY_CURATOR_REJECTED,
                payload={
                    "schema": "zyra.memory-curator-failure-event/v1",
                    "integration_run_id": integration_run.integration_run_id,
                    "failure": failure.to_dict(),
                    "canonical_memory_changed": False,
                },
            )
            self._append_projection_event(event)
            event_ids.append(event_id)
        return tuple(event_ids)

    def _append_projection_event(self, event: EventRecord) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink.append_event(event)
        except Exception as error:
            raise MemoryCuratorIntegrationError(
                "outcome_event_delivery_failed",
                f"curator outcome event delivery failed: {type(error).__name__}: {error}",
                retryable=True,
            ) from error

    @staticmethod
    def _event_type(outcome: CuratorOutcome) -> EventType:
        if outcome.kind.value == "candidate":
            return getattr(EventType, "MEMORY_CURATOR_CANDIDATE", EventType.SYSTEM_NOTICE)
        if outcome.kind.value == "accepted":
            return getattr(EventType, "MEMORY_CURATOR_ACCEPTED", EventType.SYSTEM_NOTICE)
        if outcome.kind.value == "index_published":
            return getattr(EventType, "MEMORY_CURATOR_INDEX_PUBLISHED", EventType.SYSTEM_NOTICE)
        if outcome.canonical_memory_changed:
            return EventType.MEMORY_CURATOR_COMMITTED
        return EventType.MEMORY_CURATOR_REJECTED

    def _emit_completed_event(
        self,
        run: CuratorIntegrationRun,
        audit: CuratorProjectionAudit,
    ) -> str:
        event_id = stable_id(
            "event",
            "memory_curator_integration_completed",
            run.integration_run_id,
        )
        if self.event_sink is None:
            return ""
        event = EventRecord(
            event_id=event_id,
            run_id=run.run_id,
            task_id=run.task_id,
            event_type=EventType.SYSTEM_NOTICE,
            payload={
                "schema": "zyra.memory-curator-integration-event/v1",
                "phase": "integration_completed",
                "integration_run": run.to_dict(),
                "audit": audit.to_dict(),
                "canonical_memory_owner": "SQLiteStore.memory_records",
            },
        )
        self._append_projection_event(event)
        return event_id

    def _result_from_durable_job(self, job_id: str) -> CuratorRunResult | None:
        run = self.store.integration_run_for_job(job_id)
        if run is None:
            return None
        value = run.metadata.get("curator_result")
        if not isinstance(value, Mapping):
            return None
        result = CuratorRunResult.from_dict(value)
        if result.job_id != job_id or result.task_id != run.task_id:
            return None
        return result


__all__ = [
    "CuratorIntegrationRecoveryReport",
    "IntegratedCuratorResult",
    "MemoryCuratorIntegrationApplication",
    "MemoryCuratorIntegrationError",
]
