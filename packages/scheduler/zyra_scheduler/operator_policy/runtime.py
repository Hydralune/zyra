from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType
from zyra_orchestration.topology_policy.contracts import (
    MechanismEvidenceReadinessReportRef,
    PolicyInputSnapshot,
    canonical_digest,
)

from .catalog import OperatorCatalog
from .selector import (
    DeterministicOperatorSelector,
    OperatorSchedulerInput,
    OperatorSelectionProposal,
    OperatorSelectionRequest,
    OperatorSelectorConfig,
)


EventSink = Callable[[EventRecord], None]
BaselineExecutor = Callable[[PolicyInputSnapshot], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class MaasReadinessResolution:
    stage: str
    status: str
    mode: str
    report_ref: str
    report_digest: str
    scheduler_input_allowed: bool
    placement_change_allowed: bool
    fallback_profile: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism_id": "maas",
            "stage": self.stage,
            "status": self.status,
            "mode": self.mode,
            "report_ref": self.report_ref,
            "report_digest": self.report_digest,
            "scheduler_input_allowed": self.scheduler_input_allowed,
            "placement_change_allowed": self.placement_change_allowed,
            "fallback_profile": self.fallback_profile,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MaasOperatorPolicyResult:
    mode: str
    readiness: MaasReadinessResolution
    proposal: OperatorSelectionProposal | None
    scheduler_input: OperatorSchedulerInput | None
    diagnostic_ranking: tuple[Mapping[str, Any], ...]
    baseline_decision: Mapping[str, Any]
    degraded: bool
    degraded_reason: str
    events: tuple[EventRecord, ...]
    placement_owner: str = "ResourceScheduler"
    lease_owner: str = "WorkerPoolFoundationRuntime"
    physical_attempt_created: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "readiness": self.readiness.to_dict(),
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "scheduler_input": (
                self.scheduler_input.to_dict() if self.scheduler_input else None
            ),
            "diagnostic_ranking": [
                dict(item) for item in self.diagnostic_ranking
            ],
            "baseline_decision": dict(self.baseline_decision),
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "placement_owner": self.placement_owner,
            "lease_owner": self.lease_owner,
            "physical_attempt_created": self.physical_attempt_created,
            "events": [
                {
                    "event_id": item.event_id,
                    "event_type": item.event_type.value,
                    "run_id": item.run_id,
                    "task_id": item.task_id,
                    "payload": dict(item.payload),
                }
                for item in self.events
            ],
        }


class MaasOperatorPolicyRuntime:
    """Readiness-bound selector runtime that never owns placement or execution."""

    def __init__(
        self,
        *,
        config: OperatorSelectorConfig | None,
        selector: DeterministicOperatorSelector | None = None,
        baseline_executor: BaselineExecutor | None = None,
        admit_event: EventSink | None = None,
        disconnected_reason: str = "",
        trusted_readiness: MaasReadinessResolution | None = None,
        require_trusted_readiness: bool = False,
    ) -> None:
        self.config = config
        self.selector = (
            selector
            if selector is not None
            else (
                DeterministicOperatorSelector(config)
                if config is not None
                else None
            )
        )
        self.baseline_executor = baseline_executor or self._default_baseline
        self.admit_event = admit_event or (lambda event: None)
        self.disconnected_reason = disconnected_reason
        self.trusted_readiness = trusted_readiness
        self.require_trusted_readiness = require_trusted_readiness

    @classmethod
    def from_repository(
        cls,
        repository_root: Path,
        *,
        config_path: Path | None = None,
        report_path: Path | None = None,
        baseline_executor: BaselineExecutor | None = None,
        admit_event: EventSink | None = None,
    ) -> "MaasOperatorPolicyRuntime":
        root = repository_root.resolve()
        selected = (
            config_path.resolve()
            if config_path is not None
            else root / "config" / "phase2" / "maas-operator-selector.json"
        )
        try:
            config = OperatorSelectorConfig.load(selected)
        except Exception as exc:  # noqa: BLE001 - explicit baseline degradation.
            return cls(
                config=None,
                baseline_executor=baseline_executor,
                admit_event=admit_event,
                disconnected_reason=getattr(
                    exc,
                    "code",
                        f"maas_config:{type(exc).__name__}",
                ),
                require_trusted_readiness=True,
            )
        selected_report = (
            report_path.resolve()
            if report_path is not None
            else root
            / "docs"
            / "reviews"
            / "evidence"
            / "P2-S04-01"
            / "MechanismEvidenceReadinessReport.json"
        )
        try:
            trusted_readiness = cls._load_readiness_report(
                selected_report,
                config=config,
            )
            disconnected_reason = ""
        except Exception as exc:  # noqa: BLE001 - report failures stay baseline.
            disconnected_reason = getattr(
                exc,
                "code",
                f"maas_readiness:{type(exc).__name__}",
            )
            trusted_readiness = MaasReadinessResolution(
                stage="input_precheck",
                status="unavailable",
                mode="baseline",
                report_ref=selected_report.as_posix(),
                report_digest="",
                scheduler_input_allowed=False,
                placement_change_allowed=False,
                fallback_profile=config.fallback_profile,
                reason=str(disconnected_reason),
            )
        return cls(
            config=config,
            baseline_executor=baseline_executor,
            admit_event=admit_event,
            disconnected_reason=str(disconnected_reason),
            trusted_readiness=trusted_readiness,
            require_trusted_readiness=True,
        )

    def execute(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        query: str,
        catalog: OperatorCatalog,
        required_capabilities: tuple[str, ...] = (),
        required_input_contract: tuple[str, ...] = (),
        required_output_contract: tuple[str, ...] = (),
        required_verifier_contracts: tuple[str, ...] = (),
        verifier_necessary: bool = False,
        enabled: bool = True,
        explicit_validation: bool = False,
    ) -> MaasOperatorPolicyResult:
        events: list[EventRecord] = []
        readiness = self._resolve_readiness(policy_input)
        if not enabled:
            return self._baseline_result(
                policy_input=policy_input,
                readiness=readiness,
                reason="maas_selector_disabled",
                events=events,
            )
        if (
            self.config is None
            or self.selector is None
            or readiness.mode == "baseline"
        ):
            return self._baseline_result(
                policy_input=policy_input,
                readiness=readiness,
                reason=(
                    self.disconnected_reason
                    or readiness.reason
                    or "maas_unavailable"
                ),
                events=events,
            )
        if readiness.mode == "validation" and not explicit_validation:
            return self._baseline_result(
                policy_input=policy_input,
                readiness=readiness,
                reason="maas_validation_not_explicit",
                events=events,
            )
        try:
            request = OperatorSelectionRequest(
                policy_input=policy_input,
                query=query,
                required_capabilities=required_capabilities,
                required_input_contract=required_input_contract,
                required_output_contract=required_output_contract,
                required_verifier_contracts=required_verifier_contracts,
                verifier_necessary=verifier_necessary,
            )
            result = self.selector.select(
                catalog=catalog,
                request=request,
                readiness_report_digest=readiness.report_digest,
            )
            self.selector.validate_proposal(
                proposal=result.proposal,
                catalog=catalog,
                policy_input=policy_input,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed to baseline.
            return self._baseline_result(
                policy_input=policy_input,
                readiness=readiness,
                reason=getattr(
                    exc,
                    "code",
                    f"maas_exception:{type(exc).__name__}",
                ),
                events=events,
            )

        scheduler_input = None
        if readiness.scheduler_input_allowed:
            scheduler_input = result.proposal.scheduler_input().bind_task(
                run_id=policy_input.run_id,
                task_id=policy_input.task_id,
            )
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_maas_" + canonical_digest(
                (
                    result.proposal.digest,
                    readiness.mode,
                    bool(scheduler_input),
                )
            )[:24],
            event_type=EventType.RESOURCE_DECISION,
            payload={
                "schema": "zyra.maas-operator-runtime-result/v1",
                "mechanism_id": "maas",
                "mode": readiness.mode,
                "readiness_stage": readiness.stage,
                "readiness_status": readiness.status,
                "readiness_report_digest": readiness.report_digest,
                "proposal_id": result.proposal.proposal_id,
                "proposal_digest": result.proposal.digest,
                "catalog_version": catalog.catalog_version,
                "catalog_digest": catalog.digest,
                "candidate_ids": [
                    item.operator_id for item in result.selected
                ],
                "expected_breadth": result.proposal.expected_breadth,
                "expected_depth": result.proposal.expected_depth,
                "scheduler_input_emitted": scheduler_input is not None,
                "diagnostic_only": readiness.mode == "diagnostic",
                "placement_change_allowed": False,
                "lease_created": False,
                "physical_attempt_created": False,
                "placement_owner": "ResourceScheduler",
                "lease_owner": "WorkerPoolFoundationRuntime",
            },
        )
        self.admit_event(event)
        events.append(event)
        return MaasOperatorPolicyResult(
            mode=readiness.mode,
            readiness=readiness,
            proposal=result.proposal,
            scheduler_input=scheduler_input,
            diagnostic_ranking=tuple(
                item.to_dict()
                for item in (
                    result.selected
                    if readiness.mode == "diagnostic"
                    else result.alternatives
                )
            ),
            baseline_decision=(
                dict(self.baseline_executor(policy_input))
                if readiness.mode == "diagnostic"
                else {}
            ),
            degraded=False,
            degraded_reason="",
            events=tuple(events),
        )

    def validate_proposal(
        self,
        *,
        proposal: OperatorSelectionProposal,
        catalog: OperatorCatalog,
        policy_input: PolicyInputSnapshot,
    ) -> None:
        if self.selector is None:
            raise ValueError("maas_selector_disconnected")
        self.selector.validate_proposal(
            proposal=proposal,
            catalog=catalog,
            policy_input=policy_input,
        )

    def _resolve_readiness(
        self,
        policy_input: PolicyInputSnapshot,
    ) -> MaasReadinessResolution:
        ref = next(
            (
                item
                for item in policy_input.readiness_refs
                if item.header.mechanism_id
                in {"maas", "maas_operator_selector"}
            ),
            None,
        )
        fallback = (
            self.config.fallback_profile
            if self.config is not None
            else "phase1_resource_scheduler_baseline"
        )
        if ref is None:
            return MaasReadinessResolution(
                stage="input_precheck",
                status="unavailable",
                mode="baseline",
                report_ref="",
                report_digest="",
                scheduler_input_allowed=False,
                placement_change_allowed=False,
                fallback_profile=fallback,
                reason="MaAS readiness reference is missing",
            )
        resolved = self._resolution_from_ref(ref, fallback=fallback)
        if not self.require_trusted_readiness:
            return resolved
        trusted = self.trusted_readiness
        if (
            trusted is None
            or trusted.status == "unavailable"
            or trusted.report_digest != resolved.report_digest
            or trusted.stage != resolved.stage
            or trusted.status != resolved.status
        ):
            return MaasReadinessResolution(
                stage=resolved.stage,
                status="unavailable",
                mode="baseline",
                report_ref=resolved.report_ref,
                report_digest=resolved.report_digest,
                scheduler_input_allowed=False,
                placement_change_allowed=False,
                fallback_profile=fallback,
                reason="policy input readiness does not match the verified MaAS report",
            )
        return trusted

    @staticmethod
    def _resolution_from_ref(
        ref: MechanismEvidenceReadinessReportRef,
        *,
        fallback: str,
    ) -> MaasReadinessResolution:
        if ref.status == "unavailable":
            return MaasReadinessResolution(
                stage=ref.readiness_stage,
                status=ref.status,
                mode="baseline",
                report_ref=ref.report_ref,
                report_digest=ref.report_digest,
                scheduler_input_allowed=False,
                placement_change_allowed=False,
                fallback_profile=fallback,
                reason="unavailable MaAS must use the baseline scheduler input",
            )
        if ref.status == "evidence_only":
            return MaasReadinessResolution(
                stage=ref.readiness_stage,
                status=ref.status,
                mode="diagnostic",
                report_ref=ref.report_ref,
                report_digest=ref.report_digest,
                scheduler_input_allowed=False,
                placement_change_allowed=False,
                fallback_profile=fallback,
                reason="evidence_only ranking cannot affect scheduler route or lease",
            )
        if ref.readiness_stage == "activation_ready":
            return MaasReadinessResolution(
                stage=ref.readiness_stage,
                status=ref.status,
                mode="default",
                report_ref=ref.report_ref,
                report_digest=ref.report_digest,
                scheduler_input_allowed=True,
                placement_change_allowed=False,
                fallback_profile=fallback,
                reason="activation-ready selector may emit candidates; placement still belongs to ResourceScheduler",
            )
        return MaasReadinessResolution(
            stage=ref.readiness_stage,
            status=ref.status,
            mode="validation",
            report_ref=ref.report_ref,
            report_digest=ref.report_digest,
            scheduler_input_allowed=True,
            placement_change_allowed=False,
            fallback_profile=fallback,
            reason="deterministic MaAS selector is limited to explicit validation until activation-ready",
        )

    def _baseline_result(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        readiness: MaasReadinessResolution,
        reason: str,
        events: list[EventRecord],
    ) -> MaasOperatorPolicyResult:
        baseline = dict(self.baseline_executor(policy_input))
        event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_maas_degraded_" + canonical_digest(
                (policy_input.digest, readiness.report_digest, reason, baseline)
            )[:24],
            event_type=EventType.RECOVERY_PLANNED,
            payload={
                "schema": "zyra.maas-operator-runtime-degraded/v1",
                "mechanism_id": "maas",
                "reason": reason,
                "fallback_profile": readiness.fallback_profile,
                "readiness_stage": readiness.stage,
                "readiness_status": readiness.status,
                "readiness_report_digest": readiness.report_digest,
                "operator_proposal_emitted": False,
                "scheduler_input_emitted": False,
                "placement_change_allowed": False,
                "lease_created": False,
                "physical_attempt_created": False,
                "baseline_decision_digest": canonical_digest(baseline),
            },
        )
        self.admit_event(event)
        events.append(event)
        return MaasOperatorPolicyResult(
            mode="baseline",
            readiness=readiness,
            proposal=None,
            scheduler_input=None,
            diagnostic_ranking=(),
            baseline_decision=baseline,
            degraded=True,
            degraded_reason=reason,
            events=tuple(events),
        )

    @classmethod
    def _load_readiness_report(
        cls,
        path: Path,
        *,
        config: OperatorSelectorConfig,
    ) -> MaasReadinessResolution:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("maas_readiness_report_missing_or_corrupt") from exc
        if not isinstance(report, Mapping):
            raise ValueError("maas_readiness_report_invalid")
        supplied_digest = str(report.get("report_digest") or "")
        unsigned = dict(report)
        unsigned.pop("report_digest", None)
        if (
            len(supplied_digest) != 64
            or canonical_digest(unsigned) != supplied_digest
        ):
            raise ValueError("maas_readiness_report_digest_mismatch")
        if report.get("schema") != "zyra.mechanism-evidence-readiness-report/v1":
            raise ValueError("maas_readiness_report_schema_unknown")
        stage = str(report.get("readiness_stage") or "")
        if stage not in {
            "input_precheck",
            "implementation_validated",
            "activation_ready",
        }:
            raise ValueError("maas_readiness_stage_unknown")
        mechanisms = report.get("mechanisms")
        maas = mechanisms.get("maas") if isinstance(mechanisms, Mapping) else None
        if not isinstance(maas, Mapping):
            raise ValueError("maas_readiness_verdict_missing")
        status = str(maas.get("status") or "")
        if status not in {"deterministic_ready", "evidence_only", "unavailable"}:
            raise ValueError("maas_readiness_status_unknown")
        if (
            maas.get("readiness_stage") != stage
            or (
                report.get("mechanism_statuses")
                if isinstance(report.get("mechanism_statuses"), Mapping)
                else {}
            ).get("maas")
            != status
        ):
            raise ValueError("maas_readiness_stage_or_status_drift")
        no_training = report.get("no_policy_training_audit")
        validation = maas.get("selector_validation")
        if (
            not isinstance(no_training, Mapping)
            or no_training.get("passed") is not True
            or no_training.get("training_sample_count") != 0
        ):
            raise ValueError("maas_readiness_no_training_audit_failed")
        if (
            status == "deterministic_ready"
            and (
                not isinstance(validation, Mapping)
                or validation.get("passed") is not True
                or validation.get("mechanism_version")
                != config.mechanism_version
                or validation.get("configuration_digest") != config.digest
                or validation.get("catalog_schema_version")
                != config.catalog_schema_version
                or validation.get("proposal_schema_version")
                != config.proposal_schema_version
                or validation.get("scheduler_input_schema_version")
                != config.scheduler_input_schema_version
                or validation.get("placement_change_allowed") is not False
                or validation.get("lease_created") is not False
                or validation.get("physical_attempt_created") is not False
            )
        ):
            raise ValueError("maas_selector_validation_incomplete")
        if (
            stage == "input_precheck"
            and report.get("supersedes_report_digest")
            != config.input_precheck_report_digest
        ):
            raise ValueError("maas_input_precheck_lineage_mismatch")
        ref = MechanismEvidenceReadinessReportRef(
            header=ContractHeader(
                contract_id=f"maas-readiness-{supplied_digest[:20]}",
                created_at=str(
                    report.get("generated_at") or "1970-01-01T00:00:00Z"
                ),
                source_event_id=f"maas-readiness-event-{supplied_digest[:20]}",
                correlation_id="maas-readiness",
                causation_id=f"maas-readiness-source-{supplied_digest[:20]}",
                mechanism_id="maas",
                mechanism_version=config.mechanism_version,
                input_version="v1",
                idempotency_key=f"maas-readiness:{supplied_digest}",
                configuration_digest=config.digest,
            ),
            report_ref=path.as_posix(),
            report_digest=supplied_digest,
            readiness_stage=stage,
            status=status,
        )
        return cls._resolution_from_ref(ref, fallback=config.fallback_profile)

    @staticmethod
    def _default_baseline(
        policy_input: PolicyInputSnapshot,
    ) -> Mapping[str, Any]:
        return {
            "profile_id": "phase1_resource_scheduler_baseline",
            "policy_input_digest": policy_input.digest,
            "task_id": policy_input.task_id,
            "graph_commit_id": policy_input.graph.commit_id,
            "operator_selector_enabled": False,
            "route_owner": "ResourceScheduler",
            "lease_owner": "WorkerPoolFoundationRuntime",
        }
