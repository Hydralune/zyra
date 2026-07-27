from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from .admission import LiveRunAdmission, verify_campaign_run_uniqueness
from .canonical import BenchmarkValidationError, identity, invalid, utc_now
from .deployment import DeploymentEvidenceVerifier
from .faults import FaultCoverageVerifier
from .matrix import cell_for, verify_campaign_plan
from .metrics import (
    MetricCatalog,
    MetricExtractor,
    verify_campaign_metric_completeness,
)
from .models import BenchmarkCell, Campaign, CellPhase, CellResult, RawSample
from .statistics import StatisticalEvaluator
from .store import BenchmarkStore
from .verifiers import DeterministicDomainVerifier


class LiveRunPort(Protocol):
    def execute(
        self,
        *,
        campaign: Campaign,
        cell: BenchmarkCell,
        cancel_requested: Any,
    ) -> Mapping[str, Any]:
        ...


class UnboundLiveRunPort:
    def execute(self, **_: Any) -> Mapping[str, Any]:
        raise invalid(
            "benchmark_live_run_port_unbound",
            "Formal live benchmark has no execution owner bound.",
            phase="execution",
        )


class LiveBenchmarkRuntime:
    def __init__(
        self,
        *,
        store_root: str | Path,
        live_port: LiveRunPort | None = None,
        maximum_workers: int = 2,
        metric_catalog: MetricCatalog | None = None,
    ) -> None:
        if maximum_workers < 1 or maximum_workers > 32:
            raise ValueError("maximum_workers must be between 1 and 32")
        self.store = BenchmarkStore(store_root)
        self.live_port = live_port or UnboundLiveRunPort()
        self.maximum_workers = maximum_workers
        self.admission = LiveRunAdmission()
        self.deployment = DeploymentEvidenceVerifier()
        self.faults = FaultCoverageVerifier()
        self.verifier = DeterministicDomainVerifier()
        self.metric_catalog = metric_catalog or MetricCatalog()
        self.metrics = MetricExtractor(self.metric_catalog)
        self.statistics = StatisticalEvaluator(self.metric_catalog)
        self._cancel = threading.Event()

    def create(self, campaign: Campaign) -> dict[str, Any]:
        plan_receipt = verify_campaign_plan(campaign)
        state = self.store.create(campaign.to_dict())
        return {
            "schema": "zyra.live-benchmark-create/v1",
            "campaign_id": campaign.campaign_id,
            "campaign_digest": campaign.campaign_digest,
            "plan_receipt": plan_receipt,
            "state": state,
        }

    def run(
        self,
        campaign: Campaign,
        *,
        worker_prefix: str = "benchmark-worker",
    ) -> dict[str, Any]:
        selected_prefix = identity(worker_prefix, "worker prefix")
        self._cancel.clear()
        state = self.store.state(campaign.campaign_id)
        if state["phase"] == "planned":
            self.store.transition(campaign.campaign_id, "running")
        elif state["phase"] != "running":
            raise invalid(
                "benchmark_runtime_phase_invalid",
                "Campaign cannot run from its current phase.",
                phase="execution",
                detail={"phase": state["phase"]},
            )
        self.store.recover_expired_leases(campaign.campaign_id)
        pending = self.store.pending_cells(campaign.campaign_id)
        failures: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.maximum_workers,
            thread_name_prefix="zyra-live-benchmark",
        ) as executor:
            futures = {}
            for index, cell_id in enumerate(pending, start=1):
                worker_id = f"{selected_prefix}-{((index - 1) % self.maximum_workers) + 1}"
                future = executor.submit(
                    self._execute_cell,
                    campaign,
                    cell_id,
                    worker_id,
                )
                futures[future] = cell_id
            for future in concurrent.futures.as_completed(futures):
                cell_id = futures[future]
                try:
                    future.result()
                except BaseException as error:
                    failures.append(failure_value(cell_id, error))
                    self._cancel.set()
        if failures:
            self.store.transition(
                campaign.campaign_id,
                "failed",
                reason="one or more formal benchmark cells failed",
            )
            raise invalid(
                "benchmark_campaign_execution_failed",
                "Formal benchmark campaign has failed cells.",
                phase="execution",
                detail={"failures": failures},
            )
        return self.evaluate(campaign)

    def submit(
        self,
        campaign: Campaign,
        cell_id: str,
        receipt: Mapping[str, Any],
        *,
        worker_id: str = "external-live-owner",
    ) -> CellResult:
        cell = cell_for(campaign, cell_id)
        state = self.store.state(campaign.campaign_id)
        if state["phase"] == "planned":
            self.store.transition(campaign.campaign_id, "running")
        lease = self.store.lease(
            campaign.campaign_id,
            cell.cell_id,
            worker_id=worker_id,
        )
        self.store.start_cell(
            campaign.campaign_id,
            cell.cell_id,
            lease_id=lease["lease_id"],
            worker_id=worker_id,
        )
        try:
            result = self._admit(campaign, cell, receipt)
            self.store.complete_cell(
                campaign.campaign_id,
                cell.cell_id,
                lease_id=lease["lease_id"],
                worker_id=worker_id,
                result=result.to_dict(),
            )
            return result
        except BaseException as error:
            self.store.fail_cell(
                campaign.campaign_id,
                cell.cell_id,
                lease_id=lease["lease_id"],
                worker_id=worker_id,
                failure=failure_value(cell.cell_id, error),
                retryable=False,
            )
            raise

    def evaluate(self, campaign: Campaign) -> dict[str, Any]:
        state = self.store.state(campaign.campaign_id)
        incomplete = [
            cell_id
            for cell_id, value in state["cell_states"].items()
            if value["phase"] != CellPhase.ADMITTED.value
        ]
        if incomplete:
            raise invalid(
                "benchmark_campaign_incomplete",
                "Formal benchmark cannot be evaluated before all cells pass.",
                phase="evaluation",
                detail={"cell_ids": sorted(incomplete)},
            )
        if state["phase"] == "running":
            self.store.transition(campaign.campaign_id, "verifying")
        raw_results = self.store.results(campaign.campaign_id)
        results = tuple(result_from_dict(item, campaign) for item in raw_results)
        admissions = [item.admission_receipt for item in results]
        uniqueness = verify_campaign_run_uniqueness(admissions)
        samples = tuple(sample for result in results for sample in result.samples)
        completeness = verify_campaign_metric_completeness(
            samples,
            expected_cell_ids=[item.cell_id for item in campaign.cells],
            catalog=self.metric_catalog,
        )
        statistics = self.statistics.evaluate(samples)
        long_runs = [
            result.run_id
            for result in results
            if result.admission_receipt["semantic_step_receipt"][
                "effective_step_count"
            ]
            >= campaign.required_long_run_steps
        ]
        if not long_runs:
            raise invalid(
                "benchmark_long_run_threshold_not_met",
                "No formal run contains at least 2,000 effective transitions.",
                phase="evaluation",
            )
        self.store.transition(campaign.campaign_id, "reporting")
        return {
            "schema": "zyra.live-benchmark-evaluation/v1",
            "valid": True,
            "campaign_id": campaign.campaign_id,
            "result_count": len(results),
            "sample_count": len(samples),
            "long_run_ids": sorted(long_runs),
            "uniqueness_receipt": uniqueness,
            "metric_completeness_receipt": completeness,
            "statistical_evaluation": statistics,
            "results": results,
            "samples": samples,
            "evaluated_at": utc_now(),
        }

    def cancel(self, campaign_id: str, *, reason: str) -> dict[str, Any]:
        self._cancel.set()
        return self.store.request_cancel(campaign_id, reason=reason)

    def _execute_cell(
        self,
        campaign: Campaign,
        cell_id: str,
        worker_id: str,
    ) -> None:
        if self._cancel.is_set():
            return
        cell = cell_for(campaign, cell_id)
        lease = self.store.lease(
            campaign.campaign_id,
            cell.cell_id,
            worker_id=worker_id,
        )
        self.store.start_cell(
            campaign.campaign_id,
            cell.cell_id,
            lease_id=lease["lease_id"],
            worker_id=worker_id,
        )
        try:
            receipt = self.live_port.execute(
                campaign=campaign,
                cell=cell,
                cancel_requested=self._cancel.is_set,
            )
            result = self._admit(campaign, cell, receipt)
            self.store.complete_cell(
                campaign.campaign_id,
                cell.cell_id,
                lease_id=lease["lease_id"],
                worker_id=worker_id,
                result=result.to_dict(),
            )
        except BaseException as error:
            self.store.fail_cell(
                campaign.campaign_id,
                cell.cell_id,
                lease_id=lease["lease_id"],
                worker_id=worker_id,
                failure=failure_value(cell.cell_id, error),
                retryable=False,
            )
            raise

    def _admit(
        self,
        campaign: Campaign,
        cell: BenchmarkCell,
        receipt: Mapping[str, Any],
    ) -> CellResult:
        started_at = utc_now()
        admission = self.admission.admit(campaign, cell, receipt)
        deployment = self.deployment.verify(
            receipt["deployment"],
            run_id=str(receipt["run_id"]),
        )
        faults = self.faults.verify(
            receipt["faults"],
            run_id=str(receipt["run_id"]),
        )
        verifier = self.verifier.verify(
            receipt["verification"],
            domain=cell.domain,
            run_id=str(receipt["run_id"]),
        )
        metric_source = receipt["metrics"]
        samples = self.metrics.extract(
            campaign_id=campaign.campaign_id,
            cell=cell,
            run_id=str(receipt["run_id"]),
            receipt=metric_source,
            evidence_digest=admission["receipt_digest"],
        )
        return CellResult(
            cell=cell,
            run_id=str(receipt["run_id"]),
            phase=CellPhase.ADMITTED,
            live_receipt=dict(receipt),
            admission_receipt=admission,
            verifier_receipt=verifier,
            deployment_receipt=deployment,
            fault_receipt=faults,
            samples=samples,
            started_at=started_at,
            completed_at=utc_now(),
        )


def failure_value(cell_id: str, error: BaseException) -> dict[str, Any]:
    if isinstance(error, BenchmarkValidationError):
        value = error.to_dict()
    else:
        value = {
            "code": "benchmark-unexpected-error",
            "message": str(error),
            "phase": "execution",
            "detail": {"error_type": type(error).__name__},
        }
    value["cell_id"] = cell_id
    value["failed_at"] = utc_now()
    return value


def result_from_dict(value: Mapping[str, Any], campaign: Campaign) -> CellResult:
    cell_value = value.get("cell") or {}
    cell = cell_for(campaign, str(cell_value.get("cell_id") or ""))
    samples: list[RawSample] = []
    from .models import DomainKind, SampleStatus

    for item in value.get("samples") or []:
        samples.append(
            RawSample(
                sample_id=str(item["sample_id"]),
                campaign_id=str(item["campaign_id"]),
                cell_id=str(item["cell_id"]),
                run_id=str(item["run_id"]),
                domain=DomainKind(str(item["domain"])),
                variant_id=str(item["variant_id"]),
                repetition=int(item["repetition"]),
                seed=int(item["seed"]),
                metric_id=str(item["metric_id"]),
                value=(None if item.get("value") is None else float(item["value"])),
                unit=str(item["unit"]),
                status=SampleStatus(str(item["status"])),
                observed_at=str(item["observed_at"]),
                evidence_digest=str(item["evidence_digest"]),
                dimensions=dict(item.get("dimensions") or {}),
                reason=str(item.get("reason") or ""),
            )
        )
    return CellResult(
        cell=cell,
        run_id=str(value["run_id"]),
        phase=CellPhase(str(value["phase"])),
        live_receipt=dict(value["live_receipt"]),
        admission_receipt=dict(value["admission_receipt"]),
        verifier_receipt=dict(value["verifier_receipt"]),
        deployment_receipt=dict(value["deployment_receipt"]),
        fault_receipt=dict(value["fault_receipt"]),
        samples=tuple(samples),
        started_at=str(value["started_at"]),
        completed_at=str(value["completed_at"]),
        failure=dict(value.get("failure") or {}),
    )
