from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from .bundle import EvidenceBundleBuilder, EvidenceBundleVerifier
from .canonical import (
    bounded_integer,
    bounded_text,
    canonicalize,
    digest,
    utc_now,
)
from .errors import ExperimentError, conflict, invalid, unavailable
from .matrix import (
    VariantCatalog,
    assert_same_conditions,
    build_envelope,
    plan_cells,
    verify_cell_plan,
    verify_envelope,
)
from .metrics import MetricCatalog, MetricExtractor
from .models import (
    CellPhase,
    ExperimentPhase,
    ExperimentRun,
    RawMetricSample,
)
from .reporting import ExperimentReportBuilder
from .requirements import RequirementEvidenceMapper
from .source import EvidenceArchiveLoader, SourceArchive, SourceEvidenceVerifier
from .source_roles import SourceRoleExitAuditor
from .statistics import (
    DistributionAggregator,
    aggregation_digest,
    compare_summaries,
    verify_summary,
)
from .store import ExperimentStore
from .verification import (
    ExternalEvidenceVerifier,
    MatrixEffectVerifier,
    WorkloadObservationVerifier,
    verify_run_completion,
)
from .workload import (
    EvidenceBackedWorkloadRuntime,
    VariantExecutionPort,
    WorkloadObservation,
)


class ExperimentMatrixRuntime:
    def __init__(
        self,
        *,
        project_root: str | Path,
        store: ExperimentStore,
        artifact_root: str | Path,
        allowed_source_roots: tuple[str | Path, ...] = (),
        execution: VariantExecutionPort | None = None,
        maximum_workers: int = 2,
        enable_ablation_verifier: bool = True,
        enable_metric_aggregator: bool = True,
        enable_evidence_verifier: bool = True,
        auto_reconcile: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.store = store
        self.artifact_root = Path(artifact_root).resolve(strict=False)
        self.source_loader = EvidenceArchiveLoader(
            allowed_roots=allowed_source_roots
            or (
                self.project_root,
                self.artifact_root,
            )
        )
        self.execution = execution or EvidenceBackedWorkloadRuntime()
        self.enable_ablation_verifier = enable_ablation_verifier
        self.enable_metric_aggregator = enable_metric_aggregator
        self.enable_evidence_verifier = enable_evidence_verifier
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, min(32, maximum_workers)),
            thread_name_prefix="zyra-experiment",
        )
        self._futures: dict[str, Future[None]] = {}
        self._source_cache: dict[str, SourceArchive] = {}
        self._lock = threading.RLock()
        self._closed = False
        if auto_reconcile:
            self.reconcile()

    def create(self, request: Mapping[str, Any]) -> ExperimentRun:
        self._require_open()
        if request.get("enabled") is False:
            raise unavailable(
                "experiment_runtime_disabled",
                "Experiment runtime is disabled; no report fallback is available.",
            )
        source_path = bounded_text(
            request.get("source_archive_path"),
            "source evidence archive path",
            maximum_bytes=32 * 1024,
        )
        source = self.source_loader.load(source_path)
        source_receipt = SourceEvidenceVerifier().require_valid(source)
        envelope_request = dict(request.get("envelope") or {})
        envelope_request.setdefault("source_evidence_digest", source.archive_digest)
        envelope_request.setdefault("task_input_digest", source.input_digest)
        envelope_request.setdefault("task_input_bytes", 1)
        envelope_request.setdefault("task_domain", source.domain)
        envelope = build_envelope(envelope_request)
        if envelope.source_evidence_digest != source.archive_digest:
            raise invalid(
                "experiment_source_envelope_digest_mismatch",
                "Envelope source digest does not match the admitted archive.",
                phase="admission",
            )
        if envelope.task_input_digest != source.input_digest:
            raise invalid(
                "experiment_input_envelope_digest_mismatch",
                "Envelope input digest does not match the live source.",
                phase="admission",
            )
        catalog = VariantCatalog()
        catalog_receipt = catalog.require_formal_matrix()
        envelope_receipt = verify_envelope(envelope)
        cells = plan_cells(envelope, catalog)
        plan_receipt = verify_cell_plan(
            cells,
            envelope=envelope,
            catalog=catalog,
        )
        external_receipt = ExternalEvidenceVerifier(
            project_root=self.project_root
        ).verify(request.get("external_evidence"))
        run = ExperimentRun.create(
            title=bounded_text(
                request.get("title") or f"{source.domain} controlled experiment",
                "experiment title",
                maximum_bytes=4096,
            ),
            repetitions=len(envelope.seed_plan),
            envelope=envelope,
            variants=catalog.list(),
            cells=cells,
            requested_by=str(request.get("requested_by") or "api"),
            metadata={
                "source_archive_path": str(Path(source_path).resolve(strict=False)),
                "external_evidence": external_receipt["entries"],
                "external_evidence_receipt_digest": external_receipt["receipt_digest"],
                "screenshot_index": canonicalize(
                    request.get("screenshot_index") or {}
                ),
                "sealed": True,
                "browser_connection_required": False,
                "authenticated_provider_cli_allowed": False,
                "external_model_request_allowed": False,
            },
        )
        self.store.create(run)
        admitted = run.evolve(phase=ExperimentPhase.ADMITTED)
        admitted = self.store.save(
            admitted,
            expected_revision=run.revision,
            reason="experiment.admitted",
            detail={
                "source_receipt_digest": source_receipt["receipt_digest"],
                "cell_plan_digest": plan_receipt["cell_plan_digest"],
            },
        )
        for kind, receipt in (
            ("source_admission", source_receipt),
            ("variant_catalog", catalog_receipt),
            ("envelope", envelope_receipt),
            ("cell_plan", plan_receipt),
            ("external_evidence", external_receipt),
        ):
            self.store.append_receipt(
                run.experiment_id,
                kind=kind,
                payload=receipt,
            )
        self.store.record_source(
            run.experiment_id,
            archive_id=source.archive_id,
            archive_digest=source.archive_digest,
            member_digests=source.member_digests,
            source_path=source.source_path,
            summary=source.to_dict(include_events=False),
        )
        with self._lock:
            self._source_cache[run.experiment_id] = source
        return admitted

    def start(
        self,
        experiment_id: str,
        *,
        wait: bool = False,
        timeout: float | None = None,
    ) -> ExperimentRun:
        self._require_open()
        run = self.store.require(experiment_id)
        if run.phase is ExperimentPhase.SUCCEEDED:
            return run
        if run.phase not in {ExperimentPhase.ADMITTED, ExperimentPhase.FAILED}:
            raise conflict(
                "experiment_start_phase_invalid",
                "Experiment can start only after admission or failure.",
                detail={"phase": run.phase.value},
            )
        queued = run.evolve(
            phase=ExperimentPhase.QUEUED,
            failure=None,
            cancel_requested=False,
            completed_at="",
        )
        queued = self.store.save(
            queued,
            expected_revision=run.revision,
            reason="experiment.queued",
            detail={"cell_count": len(run.cells)},
        )
        with self._lock:
            future = self._futures.get(experiment_id)
            if future is None or future.done():
                self._futures[experiment_id] = self._executor.submit(
                    self._run,
                    experiment_id,
                )
            future = self._futures[experiment_id]
        if wait:
            future.result(timeout=timeout)
            return self.store.require(experiment_id)
        return queued

    def cancel(
        self,
        experiment_id: str,
        *,
        reason: str,
        actor_id: str,
    ) -> ExperimentRun:
        run = self.store.require(experiment_id)
        if run.terminal:
            return run
        if str(run.metadata.get("sealed") or "").casefold() == "true" or (
            run.metadata.get("sealed") is True
        ):
            raise conflict(
                "experiment_sealed_cancel_rejected",
                "Sealed formal experiment cannot be cancelled by an operator.",
                phase="policy",
                detail={"actor_id": actor_id, "reason": reason},
            )
        cancelled = run.evolve(
            cancel_requested=True,
            phase=ExperimentPhase.CANCELLED,
            completed_at=utc_now(),
        )
        return self.store.save(
            cancelled,
            expected_revision=run.revision,
            reason="experiment.cancelled",
            detail={"actor_id": actor_id, "reason": reason},
        )

    def archive(self, experiment_id: str, *, reason: str) -> ExperimentRun:
        run = self.store.require(experiment_id)
        if not run.terminal:
            raise conflict(
                "experiment_archive_active",
                "Active experiment cannot be archived.",
            )
        if run.phase is ExperimentPhase.ARCHIVED:
            return run
        archived = run.evolve(
            phase=ExperimentPhase.ARCHIVED,
            archive_reason=str(reason or "Archived from experiment control plane."),
        )
        return self.store.save(
            archived,
            expected_revision=run.revision,
            reason="experiment.archived",
        )

    def status(self, experiment_id: str) -> dict[str, Any]:
        run = self.store.require(experiment_id)
        with self._lock:
            future = self._futures.get(experiment_id)
            worker_active = bool(future and not future.done())
        samples = self.store.samples(experiment_id, limit=10_000_000)
        return {
            "schema": "zyra.experiment-status/v1",
            "run": run.to_dict(),
            "transitions": list(self.store.transitions(experiment_id)),
            "receipts": list(self.store.receipts(experiment_id)),
            "bundles": list(self.store.bundles(experiment_id)),
            "sample_count": len(samples),
            "sample_status_counts": dict(
                sorted(Counter(item.status.value for item in samples).items())
            ),
            "worker_active": worker_active,
            "browser_connection_required": False,
            "status_digest": digest(
                {
                    "run": run.to_dict(),
                    "sample_digests": [item.sample_digest for item in samples],
                    "worker_active": worker_active,
                }
            ),
        }

    def list(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        runs = self.store.list(
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        return {
            "schema": "zyra.experiment-run-page/v1",
            "offset": offset,
            "limit": limit,
            "runs": [item.to_dict() for item in runs],
            "store": self.store.integrity(),
        }

    def raw_samples(
        self,
        experiment_id: str,
        *,
        metric: str = "",
        variant_id: str = "",
        cell_id: str = "",
        status: str = "",
        limit: int = 1000,
        offset: int = 0,
    ) -> dict[str, Any]:
        samples = self.store.samples(
            experiment_id,
            metric=metric,
            variant_id=variant_id,
            cell_id=cell_id,
            status=status,
            limit=limit,
            offset=offset,
        )
        return {
            "schema": "zyra.experiment-raw-sample-page/v1",
            "experiment_id": experiment_id,
            "offset": offset,
            "limit": limit,
            "filters": {
                "metric": metric,
                "variant_id": variant_id,
                "cell_id": cell_id,
                "status": status,
            },
            "samples": [item.to_dict() for item in samples],
            "page_digest": digest([item.to_dict() for item in samples]),
        }

    def report(self, experiment_id: str) -> dict[str, Any]:
        run = self.store.require(experiment_id)
        if not run.report:
            raise conflict(
                "experiment_report_not_ready",
                "Experiment report is not ready.",
                phase="report",
            )
        ExperimentReportBuilder().verify(run.report)
        return dict(run.report)

    def bundle(self, experiment_id: str) -> dict[str, Any]:
        run = self.store.require(experiment_id)
        bundles = self.store.bundles(experiment_id)
        if not bundles:
            raise conflict(
                "experiment_bundle_not_ready",
                "Experiment evidence bundle is not ready.",
                phase="bundle",
            )
        latest = dict(bundles[0])
        if self.enable_evidence_verifier:
            latest["verification"] = EvidenceBundleVerifier().require_valid(
                latest["bundle_path"],
                expected_manifest_digest=latest["manifest_digest"],
            )
        else:
            raise unavailable(
                "experiment_evidence_verifier_disabled",
                "Evidence verifier is disabled; bundle cannot be served.",
                phase="bundle",
            )
        return latest

    def verify(self, experiment_id: str) -> dict[str, Any]:
        run = self.store.require(experiment_id)
        receipt_kinds = tuple(
            str(item.get("kind") or "")
            for item in self._receipts_with_kinds(experiment_id)
        )
        completion = verify_run_completion(run, receipt_kinds=receipt_kinds)
        bundle = self.bundle(experiment_id)
        receipt = {
            "schema": "zyra.experiment-reverification/v1",
            "valid": completion["valid"]
            and bundle["verification"]["valid"]
            and ExperimentReportBuilder().verify(run.report or {})["valid"],
            "experiment_id": experiment_id,
            "completion": completion,
            "bundle": bundle["verification"],
            "report": ExperimentReportBuilder().verify(run.report or {}),
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        self.store.append_receipt(
            experiment_id,
            kind="reverification",
            payload=receipt,
        )
        return receipt

    def reconcile(self) -> tuple[ExperimentRun, ...]:
        reconciled: list[ExperimentRun] = []
        for run in self.store.list(include_archived=False, limit=10_000):
            if run.phase in {
                ExperimentPhase.QUEUED,
                ExperimentPhase.RUNNING,
                ExperimentPhase.AGGREGATING,
                ExperimentPhase.VERIFYING,
            }:
                failed = run.evolve(
                    phase=ExperimentPhase.FAILED,
                    completed_at=utc_now(),
                    failure={
                        "schema": "zyra.experiment-error/v1",
                        "error": "experiment_interrupted",
                        "message": (
                            "Experiment process stopped before durable completion; "
                            "restart creates a fresh deterministic execution."
                        ),
                        "retryable": True,
                        "fallback": False,
                    },
                )
                reconciled.append(
                    self.store.save(
                        failed,
                        expected_revision=run.revision,
                        reason="experiment.interrupted",
                    )
                )
        return tuple(reconciled)

    def close(self, *, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _run(self, experiment_id: str) -> None:
        try:
            queued = self.store.require(experiment_id)
            running = queued.evolve(
                phase=ExperimentPhase.RUNNING,
                started_at=utc_now(),
            )
            running = self.store.save(
                running,
                expected_revision=queued.revision,
                reason="experiment.running",
            )
            source = self._source(experiment_id)
            catalog = VariantCatalog(running.variants)
            metric_catalog = MetricCatalog(minimum_samples=running.repetitions)
            extractor = MetricExtractor(metric_catalog)
            observation_verifier = WorkloadObservationVerifier()
            observations: list[WorkloadObservation] = []
            all_samples: list[RawMetricSample] = []
            for original_cell in tuple(running.cells):
                cell_clock = time.perf_counter()
                timing_marks: dict[str, float] = {}

                def mark(name: str) -> None:
                    timing_marks[name] = round(
                        (time.perf_counter() - cell_clock) * 1000,
                        3,
                    )

                current = self.store.require(experiment_id)
                if current.cancel_requested:
                    raise invalid(
                        "experiment_cancelled",
                        "Experiment cancellation was requested.",
                        phase="execution",
                    )
                cell = current.cell(original_cell.cell_id)
                started_cell = cell.evolve(
                    phase=CellPhase.RUNNING,
                    started_at=utc_now(),
                    failure=None,
                )
                current = current.replace_cell(started_cell)
                current = self.store.save(
                    current,
                    expected_revision=current.revision - 1,
                    reason="experiment.cell.running",
                    detail={
                        "cell_id": cell.cell_id,
                        "variant_id": cell.variant_id,
                        "repetition": cell.repetition,
                    },
                )
                variant = catalog.require(cell.variant_id)
                conditions = assert_same_conditions(
                    current.envelope,
                    {
                        "envelope_digest": cell.envelope_digest,
                        "budget_digest": current.envelope.budget.budget_digest,
                        "hardware_digest": current.envelope.hardware.hardware_digest,
                        "provider_digest": digest(
                            current.envelope.provider.to_dict()
                        ),
                        "verifier_digest": (
                            current.envelope.verifier.verifier_digest
                        ),
                        "failure_schedule_digest": (
                            current.envelope.failure_schedule.schedule_digest
                        ),
                    },
                )
                self.store.append_receipt(
                    experiment_id,
                    kind="cell_conditions",
                    payload=conditions,
                    cell_id=cell.cell_id,
                )
                cancel_state = {
                    "checked_at": 0.0,
                    "requested": current.cancel_requested,
                }

                def cancel_requested() -> bool:
                    if cancel_state["requested"]:
                        return True
                    now = time.monotonic()
                    if now - float(cancel_state["checked_at"]) >= 0.1:
                        cancel_state["checked_at"] = now
                        cancel_state["requested"] = self.store.require(
                            experiment_id
                        ).cancel_requested
                    return bool(cancel_state["requested"])

                observation = self.execution.execute(
                    experiment_id=experiment_id,
                    cell_id=cell.cell_id,
                    variant=variant,
                    repetition=cell.repetition,
                    seed=cell.seed,
                    envelope=current.envelope,
                    source=source,
                    cancel_requested=cancel_requested,
                )
                mark("workload_complete_ms")
                observation_receipt = observation_verifier.verify(
                    observation=observation,
                    variant=variant,
                    source=source,
                    expected_envelope_digest=current.envelope.envelope_digest,
                    expected_repetition=cell.repetition,
                    expected_seed=cell.seed,
                )
                mark("observation_verified_ms")
                samples = extractor.extract(
                    observation,
                    source,
                    experiment_id=experiment_id,
                    cell_id=cell.cell_id,
                )
                mark("metrics_extracted_ms")
                self.store.append_samples(experiment_id, samples)
                mark("metrics_persisted_ms")
                for receipt in observation.execution_receipts:
                    self.store.append_receipt(
                        experiment_id,
                        kind="cell_execution",
                        payload=receipt,
                        cell_id=cell.cell_id,
                    )
                self.store.append_receipt(
                    experiment_id,
                    kind="cell_observation",
                    payload=self._observation_receipt(
                        observation,
                        observation_receipt=observation_receipt,
                    ),
                    cell_id=cell.cell_id,
                )
                self.store.append_receipt(
                    experiment_id,
                    kind="cell_verification",
                    payload=observation_receipt,
                    cell_id=cell.cell_id,
                )
                mark("receipts_persisted_ms")
                current = self.store.require(experiment_id)
                persisted_cell = current.cell(cell.cell_id)
                completed_cell = persisted_cell.evolve(
                    phase=CellPhase.SUCCEEDED,
                    completed_at=observation.completed_at,
                    scenario_run_id=observation.scenario_run_id,
                    owner_run_id=observation.owner_run_id,
                    task_id=observation.task_id,
                    observation_digest=observation.observation_digest,
                    sample_ids=tuple(item.sample_id for item in samples),
                    verification_receipt=observation_receipt,
                    failure=None,
                )
                current = current.replace_cell(completed_cell)
                self.store.save(
                    current,
                    expected_revision=current.revision - 1,
                    reason="experiment.cell.succeeded",
                    detail={
                        "cell_id": cell.cell_id,
                        "observation_digest": observation.observation_digest,
                        "sample_count": len(samples),
                    },
                )
                mark("cell_committed_ms")
                performance_receipt = {
                    "schema": "zyra.experiment-cell-performance/v1",
                    "valid": True,
                    "cell_id": cell.cell_id,
                    "variant_id": cell.variant_id,
                    "repetition": cell.repetition,
                    "source_event_count": len(source.events),
                    "timing_marks": timing_marks,
                    "total_ms": timing_marks["cell_committed_ms"],
                    "complexity_claim": (
                        "O(E * (W + log W)); causation lookup is indexed"
                    ),
                    "measured_at": utc_now(),
                }
                performance_receipt["receipt_digest"] = digest(
                    performance_receipt
                )
                self.store.append_receipt(
                    experiment_id,
                    kind="cell_performance",
                    payload=performance_receipt,
                    cell_id=cell.cell_id,
                )
                observations.append(observation)
                all_samples.extend(samples)
            current = self.store.require(experiment_id)
            aggregating = current.evolve(phase=ExperimentPhase.AGGREGATING)
            aggregating = self.store.save(
                aggregating,
                expected_revision=current.revision,
                reason="experiment.aggregating",
            )
            if not self.enable_ablation_verifier:
                raise unavailable(
                    "experiment_ablation_verifier_disabled",
                    "Ablation verifier is disabled; experiment cannot complete.",
                    phase="verification",
                )
            matrix_receipt = MatrixEffectVerifier().verify(
                run=aggregating,
                observations=observations,
                samples=all_samples,
            )
            self.store.append_receipt(
                experiment_id,
                kind="matrix_effect",
                payload=matrix_receipt,
            )
            if not self.enable_metric_aggregator:
                raise unavailable(
                    "experiment_metric_aggregator_disabled",
                    "Metric aggregator is disabled; no report fallback is available.",
                    phase="aggregation",
                )
            aggregator = DistributionAggregator(
                confidence_level=0.95,
                bootstrap_resamples=2000,
                include_anomalies=True,
            )
            summaries = []
            summary_receipts = []
            for definition in metric_catalog.list():
                for variant in aggregating.variants:
                    summary = aggregator.summarize(
                        definition,
                        all_samples,
                        variant_id=variant.variant_id,
                        seed=int(
                            digest(
                                {
                                    "experiment_id": experiment_id,
                                    "metric": definition.metric,
                                    "variant": variant.variant_id,
                                }
                            )[:12],
                            16,
                        ),
                    )
                    summaries.append(summary)
                    summary_receipts.append(
                        verify_summary(summary, definition)
                    )
            summary_index = {
                (item.metric, item.variant_id): item for item in summaries
            }
            comparisons = []
            for definition in metric_catalog.list():
                for variant in aggregating.variants:
                    if variant.variant_id == "dynamic_heterogeneous_swarm":
                        continue
                    comparisons.append(
                        compare_summaries(
                            definition,
                            summary_index[
                                (
                                    definition.metric,
                                    "dynamic_heterogeneous_swarm",
                                )
                            ],
                            summary_index[(definition.metric, variant.variant_id)],
                        )
                    )
            aggregate_receipt = {
                "schema": "zyra.experiment-aggregation-verification/v1",
                "valid": all(item["valid"] for item in summary_receipts),
                "summary_count": len(summaries),
                "comparison_count": len(comparisons),
                "raw_sample_count": len(all_samples),
                "aggregation_digest": aggregation_digest(
                    summaries,
                    comparisons,
                ),
                "summary_receipt_digests": [
                    item["receipt_digest"] for item in summary_receipts
                ],
                "verified_at": utc_now(),
            }
            aggregate_receipt["receipt_digest"] = digest(aggregate_receipt)
            self.store.append_receipt(
                experiment_id,
                kind="aggregation",
                payload=aggregate_receipt,
            )
            current = self.store.require(experiment_id)
            verifying = current.evolve(phase=ExperimentPhase.VERIFYING)
            verifying = self.store.save(
                verifying,
                expected_revision=current.revision,
                reason="experiment.verifying",
            )
            source_role_audit = SourceRoleExitAuditor(
                project_root=self.project_root
            ).require_valid()
            self.store.append_receipt(
                experiment_id,
                kind="source_role_audit",
                payload=source_role_audit,
            )
            evidence_paths = self._evidence_paths(verifying)
            external = dict(verifying.metadata.get("external_evidence") or {})
            requirement_mapper = RequirementEvidenceMapper()
            requirement_rows = requirement_mapper.map(
                run=verifying,
                summaries=summaries,
                comparisons=comparisons,
                evidence_paths=evidence_paths,
                external_evidence=external,
            )
            requirement_receipt = requirement_mapper.verify(
                requirement_rows,
                require_all=True,
            )
            self.store.append_receipt(
                experiment_id,
                kind="requirements",
                payload=requirement_receipt,
            )
            verification_receipts = (
                matrix_receipt,
                aggregate_receipt,
                requirement_receipt,
                source_role_audit,
                *summary_receipts,
            )
            report_builder = ExperimentReportBuilder()
            report = report_builder.build(
                run=verifying,
                source=source,
                samples=all_samples,
                summaries=summaries,
                comparisons=comparisons,
                requirements=requirement_rows,
                source_role_audit=source_role_audit,
                verification_receipts=verification_receipts,
                external_evidence=external,
            )
            report_receipt = report_builder.verify(report)
            self.store.append_receipt(
                experiment_id,
                kind="report",
                payload=report_receipt,
            )
            if not self.enable_evidence_verifier:
                raise unavailable(
                    "experiment_evidence_verifier_disabled",
                    "Evidence verifier is disabled; M2 exit cannot complete.",
                    phase="bundle",
                )
            bundle_result = EvidenceBundleBuilder(
                artifact_root=self.artifact_root
            ).build(
                run=verifying,
                source=source,
                report=report,
                samples=all_samples,
                summaries=summaries,
                comparisons=comparisons,
                requirements=requirement_rows,
                source_role_audit=source_role_audit,
                verification_receipts=(
                    *verification_receipts,
                    report_receipt,
                ),
                screenshot_index=(
                    verifying.metadata.get("screenshot_index")
                    if isinstance(
                        verifying.metadata.get("screenshot_index"),
                        Mapping,
                    )
                    else {}
                ),
                additional_members={
                    "external/verified-evidence.json": (
                        __import__("json").dumps(
                            external,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        )
                        + "\n"
                    )
                },
            )
            bundle_receipt = {
                "schema": "zyra.experiment-bundle-commit-receipt/v1",
                "valid": bundle_result["verification"]["valid"],
                "bundle_id": bundle_result["bundle_id"],
                "bundle_sha256": bundle_result["sha256"],
                "bundle_size": bundle_result["size"],
                "manifest_digest": bundle_result["manifest"]["manifest_digest"],
                "root_digest": bundle_result["manifest"]["root_digest"],
                "verified_at": utc_now(),
            }
            bundle_receipt["receipt_digest"] = digest(bundle_receipt)
            self.store.append_receipt(
                experiment_id,
                kind="bundle",
                payload=bundle_receipt,
            )
            self.store.record_bundle(
                experiment_id,
                bundle_id=bundle_result["bundle_id"],
                manifest_digest=bundle_result["manifest"]["manifest_digest"],
                bundle_path=bundle_result["path"],
                bundle_sha256=bundle_result["sha256"],
                bundle_size=bundle_result["size"],
                manifest=bundle_result["manifest"],
                verification=bundle_result["verification"],
            )
            current = self.store.require(experiment_id)
            succeeded = current.evolve(
                phase=ExperimentPhase.SUCCEEDED,
                completed_at=utc_now(),
                report=report,
                bundle_manifest=bundle_result["manifest"],
                verification_receipt={
                    "schema": "zyra.experiment-exit-readiness/v1",
                    "valid": True,
                    "matrix_effect_receipt": matrix_receipt["receipt_digest"],
                    "aggregation_receipt": aggregate_receipt["receipt_digest"],
                    "requirement_receipt": requirement_receipt["receipt_digest"],
                    "report_receipt": report_receipt["receipt_digest"],
                    "bundle_receipt": bundle_receipt["receipt_digest"],
                    "human_intervention_count": 0,
                    "authenticated_provider_cli_invoked": False,
                    "external_model_request_made": False,
                },
                failure=None,
            )
            succeeded = self.store.save(
                succeeded,
                expected_revision=current.revision,
                reason="experiment.succeeded",
                detail={
                    "report_digest": report["report_digest"],
                    "bundle_manifest_digest": (
                        bundle_result["manifest"]["manifest_digest"]
                    ),
                    "raw_sample_count": len(all_samples),
                },
            )
            completion = verify_run_completion(
                succeeded,
                receipt_kinds=tuple(
                    str(item.get("kind") or "")
                    for item in self._receipts_with_kinds(experiment_id)
                ),
            )
            self.store.append_receipt(
                experiment_id,
                kind="completion",
                payload=completion,
            )
        except BaseException as error:
            self._record_failure(experiment_id, error)

    def _source(self, experiment_id: str) -> SourceArchive:
        with self._lock:
            source = self._source_cache.get(experiment_id)
        if source is not None:
            return source
        record = self.store.source(experiment_id)
        source = self.source_loader.load(record["source_path"])
        if source.archive_digest != record["archive_digest"]:
            raise invalid(
                "experiment_source_changed",
                "Source archive changed after admission.",
                phase="source_admission",
            )
        with self._lock:
            self._source_cache[experiment_id] = source
        return source

    def _record_failure(self, experiment_id: str, error: BaseException) -> None:
        try:
            current = self.store.require(experiment_id)
            if current.phase in {
                ExperimentPhase.SUCCEEDED,
                ExperimentPhase.CANCELLED,
                ExperimentPhase.ARCHIVED,
            }:
                return
            failure = (
                error.response()
                if isinstance(error, ExperimentError)
                else {
                    "schema": "zyra.experiment-error/v1",
                    "ok": False,
                    "error": "experiment_execution_failed",
                    "message": str(error),
                    "phase": "execution",
                    "retryable": False,
                    "fallback": False,
                }
            )
            cells = tuple(
                (
                    item.evolve(
                        phase=CellPhase.FAILED,
                        completed_at=utc_now(),
                        failure=failure,
                    )
                    if item.phase in {CellPhase.RUNNING, CellPhase.QUEUED}
                    else item
                )
                for item in current.cells
            )
            failed = current.evolve(
                phase=ExperimentPhase.FAILED,
                completed_at=utc_now(),
                cells=cells,
                failure=failure,
            )
            self.store.save(
                failed,
                expected_revision=current.revision,
                reason="experiment.failed",
                detail={"error": failure.get("error")},
            )
            self.store.append_receipt(
                experiment_id,
                kind="failure",
                payload={
                    "schema": "zyra.experiment-failure-receipt/v1",
                    "valid": False,
                    "failure": failure,
                    "recorded_at": utc_now(),
                },
            )
        except BaseException:
            return

    def _observation_receipt(
        self,
        observation: WorkloadObservation,
        *,
        observation_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist a checksum-bound projection instead of duplicating live archives.

        The canonical source archive and final evidence bundle retain raw events.
        Persisting every derived route/message for every repetition would multiply
        a 7,000-event live run into hundreds of megabytes without adding evidence.
        """

        value = {
            "schema": "zyra.experiment-workload-observation-commit/v1",
            "valid": observation_receipt.get("valid") is True,
            "observation_id": observation.observation_id,
            "observation_digest": observation_receipt["observation_digest"],
            "variant_id": observation.variant_id,
            "repetition": observation.repetition,
            "seed": observation.seed,
            "envelope_digest": observation.envelope_digest,
            "source_archive_digest": observation.source_archive_digest,
            "scenario_run_id": observation.scenario_run_id,
            "owner_run_id": observation.owner_run_id,
            "task_id": observation.task_id,
            "state_digest": observation.state_digest,
            "started_at": observation.started_at,
            "completed_at": observation.completed_at,
            "counts": {
                "processed_events": len(observation.processed_event_ids),
                "routes": len(observation.routes),
                "messages": len(observation.messages),
                "recoveries": len(observation.recoveries),
                "memory_writes": observation.memory_writes,
                "memory_hits": observation.memory_hits,
                "compact_operations": observation.compact_operations,
                "restore_operations": observation.restore_operations,
                "topology_mutations": observation.topology_mutations,
                "useful_messages": observation.useful_messages,
                "broadcast_deliveries": observation.broadcast_deliveries,
                "unresolved_faults": len(observation.unresolved_fault_ids),
                "artifacts": len(observation.artifact_ids),
            },
            "topology": {
                "nodes": list(observation.topology_nodes),
                "edges": [list(item) for item in observation.topology_edges],
            },
            "event_sequence": {
                "first_event_id": (
                    observation.processed_event_ids[0]
                    if observation.processed_event_ids
                    else ""
                ),
                "last_event_id": (
                    observation.processed_event_ids[-1]
                    if observation.processed_event_ids
                    else ""
                ),
                "sequence_digest": digest(observation.processed_event_ids),
            },
            "execution_receipt_digests": [
                str(item.get("receipt_digest") or "")
                for item in observation.execution_receipts
            ],
            "verification_receipt_digest": observation_receipt["receipt_digest"],
            "quality_score": observation.quality_score,
            "artifact_drift_ratio": observation.artifact_drift_ratio,
            "token_units": observation.token_units,
            "wall_time_ms": observation.wall_time_ms,
            "cost_microunits": observation.cost_microunits,
            "human_intervention_count": observation.human_intervention_count,
            "claims": canonicalize(observation.claims),
            "raw_canonical_events_location": (
                "evidence bundle: sources/canonical-events.jsonl"
            ),
            "derived_raw_metrics_location": (
                "evidence bundle: metrics/raw-samples.jsonl"
            ),
        }
        value["commit_digest"] = digest(value)
        return value

    def _evidence_paths(self, run: ExperimentRun) -> dict[str, tuple[str, ...]]:
        prefix = f"bundle://{run.experiment_id}/"
        common = {
            "source_archive": (prefix + "sources/live-source-manifest.json",),
            "matrix": (prefix + "configuration/variants.json",),
            "report": (prefix + "report/final-report.json",),
            "artifact": (prefix + "sources/artifact-manifest.json",),
            "memory": (prefix + "metrics/distribution-summaries.json",),
            "metric": (
                prefix + "metrics/raw-samples.jsonl",
                prefix + "metrics/distribution-summaries.json",
            ),
            "comparison": (prefix + "metrics/variant-comparisons.json",),
            "topology": (
                prefix + "sources/canonical-events.jsonl",
                prefix + "metrics/variant-comparisons.json",
            ),
            "communication": (prefix + "metrics/variant-comparisons.json",),
            "placement": (
                prefix + "sources/owner-receipts.json",
                prefix + "metrics/distribution-summaries.json",
            ),
            "prior_verified_dispatch": (
                "external://M1-08-and-M1-exit-review",
            ),
            "fault": (prefix + "sources/canonical-events.jsonl",),
            "recovery": (
                prefix + "sources/owner-receipts.json",
                prefix + "metrics/variant-comparisons.json",
            ),
            "canonical_event": (prefix + "sources/canonical-events.jsonl",),
            "span": (prefix + "report/reviewer-navigation.json",),
            "mutation": (prefix + "sources/canonical-events.jsonl",),
            "checkpoint": (prefix + "sources/owner-receipts.json",),
            "portfolio": ("external://M2-S05-02-dual-domain-live",),
            "scenario": (prefix + "sources/live-source-manifest.json",),
            "assumption": (prefix + "report/final-report.json",),
            "workbench": (prefix + "report/reviewer-navigation.json",),
            "navigation": (prefix + "report/reviewer-navigation.json",),
            "bundle": (prefix + "manifest.json",),
            "ablation": (prefix + "metrics/variant-comparisons.json",),
            "pseudocode": (prefix + "report/algorithm-entries.json",),
            "complexity": (prefix + "report/algorithm-entries.json",),
            "source_role": (prefix + "sources/source-role-audit.json",),
            "p50_p95": (prefix + "metrics/distribution-summaries.json",),
            "provider_receipt": (prefix + "sources/owner-receipts.json",),
            "route": (prefix + "sources/canonical-events.jsonl",),
        }
        return common

    def _receipts_with_kinds(self, experiment_id: str) -> tuple[dict[str, Any], ...]:
        # The public receipt payload intentionally does not embed the store kind.
        # Reconstruct kinds from the transition-independent SQLite projection.
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT kind, payload_json
                FROM experiment_receipts
                WHERE experiment_id=?
                ORDER BY sequence
                """,
                (experiment_id,),
            ).fetchall()
        import json

        return tuple(
            {"kind": str(row["kind"]), **json.loads(str(row["payload_json"]))}
            for row in rows
        )

    def _require_open(self) -> None:
        if self._closed:
            raise unavailable(
                "experiment_runtime_closed",
                "Experiment runtime is closed.",
            )
