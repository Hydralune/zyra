from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .benchmark import LongHorizonBenchmarkGate
from .cleanroom import CleanroomCommand, CleanroomReceipt, CleanroomVerifier, default_cleanroom_commands
from .contracts import EvidencePointer, Finding, GateResult, GateStatus, HardeningContext, Severity, utc_now
from .cross_scenario import CrossScenarioConsistencyGate, TopologyAdversarialGate
from .evidence_admission import (
    EvidenceAdmissionController,
    EvidenceEnvelope,
    EvidenceEnvelopeFactory,
    EvidenceKind,
    EvidenceOrigin,
)
from .exit_gate import ExitBundleBuilder, ExitPolicy, LineEvidence, M1ExitBundle
from .handoff import (
    HandoffBuilder,
    HandoffGate,
    HandoffStore,
    M2HandoffContract,
    SourceChainHandoff,
    default_handoff_surfaces,
)
from .integration_contracts import ScenarioEvidence, stable_digest
from .integration_scenarios import (
    M1IntegrationScenarioSuite,
    ScenarioRuntimeOptions,
    scenario_catalog_gate,
)
from .live_evidence import LiveEvidenceSuite
from .owner_matrix import OwnerMatrix
from .owner_probes import (
    OwnerProbeCatalog,
    RuntimeResetRegistry,
    ScenarioDisconnectCoordinator,
)
from .service import AuditOptions, AuditOutcome, M1HardeningService


@dataclass(frozen=True, slots=True)
class IntegrationOptions:
    baseline_commit: str
    implementation_commit: str = ""
    evidence_commit: str = ""
    scenario_ids: tuple[str, ...] = ()
    execute_scenarios: bool = True
    execute_disconnects: bool = False
    final_completion: bool = False
    run_cleanroom: bool = False
    cleanroom_commands: tuple[CleanroomCommand, ...] = ()
    scenario_timeout_seconds: float = 120.0
    benchmark_run_id: str = ""
    benchmark_events: tuple[Mapping[str, Any], ...] = ()
    sealed_policy: Mapping[str, Any] = field(default_factory=dict)
    line_evidence: tuple[Mapping[str, Any], ...] = ()
    tier_observations: tuple[Mapping[str, Any], ...] = ()
    provider_observations: tuple[Mapping[str, Any], ...] = ()
    evidence_envelopes: tuple[Mapping[str, Any], ...] = ()
    source_chains: tuple[SourceChainHandoff, ...] = ()
    unresolved_requirements: tuple[str, ...] = ()
    persist: bool = True

    def validate(self) -> None:
        if not self.baseline_commit:
            raise ValueError("integration baseline commit is required")
        if self.scenario_timeout_seconds <= 0:
            raise ValueError("scenario timeout must be positive")
        if self.final_completion:
            if not self.implementation_commit:
                raise ValueError("final integration completion requires an implementation commit")
            if not self.execute_scenarios:
                raise ValueError("final integration completion must execute scenarios")
            if not self.execute_disconnects:
                raise ValueError("final integration completion must execute disconnects")
            if not self.run_cleanroom:
                raise ValueError("final integration completion must execute cleanroom")


@dataclass(slots=True)
class IntegrationOutcome:
    run_id: str
    started_at: str
    completed_at: str
    baseline_commit: str
    implementation_commit: str
    scenario_evidence: list[ScenarioEvidence]
    gates: list[GateResult]
    base_audits: list[AuditOutcome]
    cleanroom_receipt: CleanroomReceipt | None
    handoff: M2HandoffContract | None
    exit_bundle: M1ExitBundle | None
    artifact_paths: list[str]
    limitations: list[str]

    @property
    def accepted(self) -> bool:
        return bool(self.gates) and all(gate.ok for gate in self.gates) and not self.limitations

    def gate(self, gate_id: str) -> GateResult | None:
        return next((gate for gate in self.gates if gate.gate_id == gate_id), None)

    def to_dict(self, *, include_scenario_events: bool = False) -> dict[str, Any]:
        scenarios: list[dict[str, Any]] = []
        for item in self.scenario_evidence:
            value = item.to_dict()
            if not include_scenario_events:
                value["events"] = [
                    {
                        "event_id": event.get("event_id"),
                        "event_type": event.get("event_type"),
                        "run_id": event.get("run_id"),
                        "task_id": event.get("task_id"),
                        "causation_id": event.get("causation_id"),
                    }
                    for event in item.events
                ]
            scenarios.append(value)
        result = {
            "schema": "zyra.m1-integration-outcome/v1",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "baseline_commit": self.baseline_commit,
            "implementation_commit": self.implementation_commit,
            "accepted": self.accepted,
            "scenario_evidence": scenarios,
            "gates": [gate.to_dict() for gate in self.gates],
            "base_audits": [audit.to_dict(include_report=False, include_scenario=False) for audit in self.base_audits],
            "cleanroom_receipt": self.cleanroom_receipt.to_dict() if self.cleanroom_receipt else None,
            "handoff": self.handoff.to_dict() if self.handoff else None,
            "exit_bundle": self.exit_bundle.to_dict() if self.exit_bundle else None,
            "artifact_paths": list(self.artifact_paths),
            "limitations": list(self.limitations),
        }
        result["content_digest"] = stable_digest(result)
        return result


class IntegrationArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def persist(self, outcome: IntegrationOutcome) -> Path:
        payload = outcome.to_dict(include_scenario_events=True)
        target = self.path_for(outcome.run_id)
        with self._lock:
            if target.exists():
                existing = json.loads(target.read_text(encoding="utf-8"))
                if existing.get("content_digest") == payload.get("content_digest"):
                    return target
                raise RuntimeError(f"integration outcome already exists with different content: {outcome.run_id}")
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
            temporary.replace(target)
        return target

    def path_for(self, run_id: str) -> Path:
        if not re_safe_id(run_id):
            raise ValueError("invalid integration run id")
        return self.root / f"{run_id}.json"

    def list(self) -> list[Mapping[str, Any]]:
        values: list[Mapping[str, Any]] = []
        with self._lock:
            for path in sorted(self.root.glob("*.json"), reverse=True):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                values.append(
                    {
                        "run_id": value.get("run_id"),
                        "started_at": value.get("started_at"),
                        "completed_at": value.get("completed_at"),
                        "accepted": value.get("accepted"),
                        "implementation_commit": value.get("implementation_commit"),
                        "content_digest": value.get("content_digest"),
                        "path": path.name,
                    }
                )
        return values

    def load(self, run_id: str) -> Mapping[str, Any]:
        if not re_safe_id(run_id):
            raise ValueError("invalid integration run id")
        target = self.root / f"{run_id}.json"
        value = json.loads(target.read_text(encoding="utf-8"))
        expected = str(value.get("content_digest") or "")
        content = dict(value)
        content.pop("content_digest", None)
        if stable_digest(content) != expected:
            raise ValueError("integration outcome digest mismatch")
        return value


class M1IntegrationService:
    def __init__(
        self,
        project_root: str | Path,
        *,
        source_workspace: str | Path | None = None,
        artifact_root: str | Path | None = None,
        foundation_service: M1HardeningService | None = None,
        reset_registry: RuntimeResetRegistry | None = None,
    ) -> None:
        self.root = Path(project_root).resolve()
        self.source_workspace = (
            Path(source_workspace).resolve()
            if source_workspace
            else self.root / "provenance"
        )
        self.artifact_root = Path(artifact_root).resolve() if artifact_root else self.root / ".tmp" / "m1-hardening"
        self.foundation = foundation_service or M1HardeningService(
            self.root,
            source_workspace=self.source_workspace,
            artifact_root=self.artifact_root,
        )
        self.scenarios = M1IntegrationScenarioSuite()
        self.owner_probes = OwnerProbeCatalog()
        self.reset_registry = reset_registry or RuntimeResetRegistry()
        self.owner_matrix = OwnerMatrix(self.root)
        self.live = LiveEvidenceSuite()
        self.benchmark = LongHorizonBenchmarkGate()
        self.cleanroom = CleanroomVerifier(self.root)
        self.handoff_store = HandoffStore(self.artifact_root / "handoff")
        self.store = IntegrationArtifactStore(self.artifact_root / "integration")
        self._mutex = threading.Lock()

    def status(self) -> Mapping[str, Any]:
        return {
            "schema": "zyra.m1-integration-service-status/v1",
            "project_root": str(self.root),
            "scenario_ids": list(self.scenarios.scenario_ids()),
            "owner_probe_ids": list(self.owner_probes.probe_ids()),
            "owner_capabilities": list(self.owner_probes.capabilities()),
            "owner_probe_contract_issues": list(self.owner_probes.validate()),
            "runs": self.store.list(),
        }

    def execute(
        self,
        base_url: str,
        options: IntegrationOptions,
    ) -> IntegrationOutcome:
        options.validate()
        if not self._mutex.acquire(blocking=False):
            raise RuntimeError("M1 integration service already has an active run")
        started_at = utc_now()
        run_id = f"m1-integration-{stable_digest({'time': started_at, 'base': options.baseline_commit})[:20]}"
        limitations: list[str] = []
        gates: list[GateResult] = [scenario_catalog_gate()]
        scenario_evidence: list[ScenarioEvidence] = []
        base_audits: list[AuditOutcome] = []
        cleanroom_receipt: CleanroomReceipt | None = None
        handoff: M2HandoffContract | None = None
        exit_bundle: M1ExitBundle | None = None
        artifact_paths: list[str] = []
        try:
            if options.execute_scenarios:
                coordinator = ScenarioDisconnectCoordinator(
                    self.owner_probes,
                    reset_registry=self.reset_registry,
                )
                scenario_evidence, scenario_gate = self.scenarios.execute_http(
                    base_url,
                    scenario_ids=options.scenario_ids,
                    options=ScenarioRuntimeOptions(
                        timeout_seconds=options.scenario_timeout_seconds,
                        execute_disconnects=options.execute_disconnects,
                        final_completion=options.final_completion,
                    ),
                    disconnect_executor=coordinator.execute if options.execute_disconnects else None,
                )
                gates.append(scenario_gate)
                base_audits.extend(self._run_foundation_audits(scenario_evidence, options))
                gates.extend(self._aggregate_foundation_gates(base_audits))
            else:
                limitations.append("integration scenarios were not executed")
                gates.append(self.scenarios.evaluate_evidence((), final_completion=options.final_completion))

            executed_probe_ids, executed_capabilities = self._executed_probes(scenario_evidence)
            gates.append(
                self._disconnect_gate(
                    scenario_evidence,
                    final_completion=options.final_completion,
                )
            )
            owner_gate = self.owner_matrix.evaluate(
                registered_probe_ids=self.owner_probes.probe_ids(),
                scenario_ids=self.scenarios.scenario_ids(),
                executed_probe_ids=executed_probe_ids,
                final_completion=options.final_completion,
            )
            gates.append(owner_gate)

            tier_gate, provider_gate, live_gate = self.live.evaluate(
                tier_observations=options.tier_observations,
                provider_observations=options.provider_observations,
                final_completion=options.final_completion,
            )
            gates.extend((tier_gate, provider_gate, live_gate))

            benchmark_events = (
                list(options.benchmark_events)
                if options.benchmark_events
                else self._benchmark_events(scenario_evidence, options.benchmark_run_id)
            )
            benchmark_run_id = options.benchmark_run_id or self._single_run_id(benchmark_events)
            gates.append(
                CrossScenarioConsistencyGate().evaluate(
                    scenario_evidence,
                    final_completion=options.final_completion,
                    supporting_gates=tuple(gates),
                )
            )
            gates.append(
                TopologyAdversarialGate().evaluate(
                    benchmark_events,
                    final_completion=options.final_completion,
                )
            )
            benchmark_gate = self.benchmark.evaluate(
                benchmark_events,
                run_id=benchmark_run_id,
                declared_policy=options.sealed_policy,
                final_completion=options.final_completion,
            )
            gates.append(benchmark_gate)

            if options.run_cleanroom:
                cleanroom_receipt, cleanroom_gate = self.cleanroom.verify(
                    target_commit=options.implementation_commit or "HEAD",
                    commands=options.cleanroom_commands or default_cleanroom_commands(),
                    require_clean_source=options.final_completion,
                )
                gates.append(cleanroom_gate)
            else:
                gates.append(self._not_run_gate("m1-cleanroom", "Exact-commit cleanroom was not executed."))

            handoff = self._build_handoff(
                options,
                gates,
                scenario_evidence,
                cleanroom_receipt,
                base_audits,
                run_id,
            )
            handoff_gate = HandoffGate().evaluate(handoff, final_completion=options.final_completion)
            gates.append(handoff_gate)
            line_gate = self._line_evidence_gate(
                options.line_evidence,
                final_completion=options.final_completion,
            )
            gates.append(line_gate)
            automatic_envelopes = self._automatic_evidence_envelopes(
                integration_run_id=run_id,
                benchmark_run_id=benchmark_run_id,
                options=options,
                scenarios=scenario_evidence,
                benchmark_events=benchmark_events,
                benchmark_gate=benchmark_gate,
                cleanroom=cleanroom_receipt,
                handoff=handoff,
                base_audits=base_audits,
            )
            _, admission_gate = EvidenceAdmissionController().admit(
                (*options.evidence_envelopes, *automatic_envelopes),
                expected_target_commit=options.implementation_commit,
                final_completion=options.final_completion,
            )
            gates.append(admission_gate)
            if options.persist:
                artifact_paths.append(str(self.handoff_store.persist(handoff)))

            exit_bundle = self._build_exit_bundle(
                options,
                gates,
                scenario_evidence,
                cleanroom_receipt,
                handoff,
                executed_capabilities,
                base_audits,
            )
            gates.append(ExitPolicy().evaluate(exit_bundle))

            outcome = IntegrationOutcome(
                run_id=run_id,
                started_at=started_at,
                completed_at=utc_now(),
                baseline_commit=options.baseline_commit,
                implementation_commit=options.implementation_commit,
                scenario_evidence=scenario_evidence,
                gates=gates,
                base_audits=base_audits,
                cleanroom_receipt=cleanroom_receipt,
                handoff=handoff,
                exit_bundle=exit_bundle,
                artifact_paths=artifact_paths,
                limitations=limitations,
            )
            if options.persist:
                # All deterministic receipt paths must be present before
                # either artifact computes a content digest.  This keeps the
                # API response, integration record and release report bound
                # to one identical final outcome.
                path = self.store.path_for(outcome.run_id)
                release_root = self.artifact_root / "release"
                release_id = f"release-{outcome.run_id}"
                outcome.artifact_paths.extend(
                    (
                        str(path),
                        str((release_root / f"{release_id}.json").resolve()),
                        str((release_root / f"{release_id}.md").resolve()),
                    )
                )
                # Deferred import avoids a module cycle: release reporting
                # consumes IntegrationOutcome as its canonical source.
                from .release_reporting import ReleaseReportBuilder, ReleaseReportStore

                report = ReleaseReportBuilder().build(outcome)
                ReleaseReportStore(release_root).persist(report)
                self.store.persist(outcome)
            return outcome
        finally:
            self._mutex.release()

    def _run_foundation_audits(
        self,
        evidence: Sequence[ScenarioEvidence],
        options: IntegrationOptions,
    ) -> list[AuditOutcome]:
        outcomes: list[AuditOutcome] = []
        for scenario in evidence:
            task = {
                "task_id": scenario.task_id,
                "run_id": scenario.run_id,
                "revision": scenario.final_revision,
                "artifacts": scenario.artifacts,
                "metadata": {
                    "integration_scenario_id": scenario.scenario_id,
                    "integration_evidence_digest": scenario.digest(),
                },
            }
            context = HardeningContext(
                project_root=self.root,
                workspace_root=self.source_workspace,
                artifact_root=self.artifact_root,
                task=task,
                events=scenario.events,
                options={"scenario_id": scenario.scenario_id, "integration": True},
            )
            audit_options = AuditOptions(
                baseline_commit=options.baseline_commit,
                final_completion=False,
                persist=options.persist,
                run_dynamic_graph_probes=True,
                run_disable_probes=False,
                minimum_effective_lines=0,
                include_line_audit=False,
                include_scenario=False,
                sealed_policy=options.sealed_policy,
            )
            outcomes.append(self.foundation.audit(context, audit_options))
        return outcomes

    @staticmethod
    def _aggregate_foundation_gates(audits: Sequence[AuditOutcome]) -> list[GateResult]:
        grouped: dict[str, list[tuple[AuditOutcome, GateResult]]] = {}
        excluded = {"disable-module-probe", "effective-line-audit"}
        for audit in audits:
            for gate in audit.report.gates:
                if gate.gate_id.startswith("scenario:") or gate.gate_id in excluded:
                    continue
                grouped.setdefault(gate.gate_id, []).append((audit, gate))
        aggregates: list[GateResult] = []
        for gate_id, executions in sorted(grouped.items()):
            result = GateResult(
                gate_id=gate_id,
                status=GateStatus.NOT_RUN,
                summary=f"Aggregate of {len(executions)} executed foundation audits.",
            )
            for index, (audit, child) in enumerate(executions):
                for finding in child.findings:
                    if finding.severity.failing:
                        result.add(
                            Finding(
                                code=f"integration.foundation.{finding.code}",
                                severity=finding.severity,
                                summary=finding.summary,
                                detail=f"audit[{index}]: {finding.detail}",
                                capability=finding.capability,
                                location=finding.location,
                                metadata=dict(finding.metadata),
                            )
                        )
                result.evidence.extend(child.evidence)
                result.evidence.append(
                    EvidencePointer(
                        kind="foundation_audit_execution",
                        location=audit.report.report_id,
                        summary=(
                            f"{gate_id} executed for integration scenario "
                            f"{audit.report.scenario_id or audit.report.task_id}"
                        ),
                        revision=audit.report.generated_at,
                        causation_id=audit.report.run_id,
                        metadata={
                            "child_status": child.status.value,
                            "metrics_digest": stable_digest(child.metrics),
                            "task_id": audit.report.task_id,
                        },
                    )
                )
            result.metrics.update(
                {
                    "audit_count": len(executions),
                    "child_status": [child.status.value for _, child in executions],
                    "metrics_digests": [
                        stable_digest(child.metrics) for _, child in executions
                    ],
                }
            )
            aggregates.append(result.finish())
        return aggregates

    def _disconnect_gate(
        self,
        scenarios: Sequence[ScenarioEvidence],
        *,
        final_completion: bool,
    ) -> GateResult:
        result = GateResult(
            gate_id="disable-module-probe",
            status=GateStatus.NOT_RUN,
            summary="Complete scenario-executed canonical owner disconnect matrix.",
        )
        receipts: list[Mapping[str, Any]] = []
        for scenario in scenarios:
            receipts.extend(scenario.disconnect_evidence)
        passed_capabilities: set[str] = set()
        seen_probes: set[str] = set()
        for receipt in receipts:
            probe_id = str(receipt.get("probe_id") or "")
            capability = str(receipt.get("capability") or "")
            if probe_id in seen_probes:
                result.add(
                    Finding(
                        code="disable.integration_probe_duplicate",
                        severity=Severity.ERROR,
                        summary="Integration scenarios repeated one canonical disconnect probe.",
                        detail=probe_id,
                    )
                )
            seen_probes.add(probe_id)
            passed = (
                str(receipt.get("status") or "").lower() == "passed"
                and receipt.get("expected_failure_observed") is True
                and receipt.get("fallback_masked") is not True
            )
            if passed and capability:
                passed_capabilities.add(capability)
                result.evidence.append(
                    EvidencePointer(
                        kind="owner_disconnect_receipt",
                        location=probe_id,
                        summary=f"{capability} failed closed and restored without fallback masking.",
                        revision=str((receipt.get("restore_receipt") or {}).get("reset_epoch") or ""),
                        causation_id=str(receipt.get("causation_id") or ""),
                        metadata={
                            "status": "passed",
                            "expected_failure_observed": True,
                            "material_difference": receipt.get("material_difference") is True,
                        },
                    )
                )
            else:
                result.add(
                    Finding(
                        code="disable.integration_probe_failed",
                        severity=Severity.BLOCKER,
                        summary="Scenario owner disconnect did not fail closed and restore safely.",
                        detail=f"{probe_id}/{capability}",
                    )
                )
        if final_completion:
            for capability in sorted(set(self.owner_probes.capabilities()) - passed_capabilities):
                result.add(
                    Finding(
                        code="disable.integration_capability_missing",
                        severity=Severity.BLOCKER,
                        summary="Final M1 disconnect matrix lacks an executed canonical owner.",
                        capability=capability,
                    )
                )
        result.metrics.update(
            {
                "registered_count": len(self.owner_probes.probe_ids()),
                "executed_count": len(receipts),
                "passed_capability_count": len(passed_capabilities),
                "passed_capabilities": sorted(passed_capabilities),
                "executions": [dict(item) for item in receipts],
            }
        )
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _line_evidence_gate(
        values: Sequence[Mapping[str, Any]],
        *,
        final_completion: bool,
    ) -> GateResult:
        result = GateResult(
            gate_id="effective-line-audit",
            status=GateStatus.NOT_RUN,
            summary="Conservative effective production-line evidence for both M1-08 slices.",
        )
        lines = [LineEvidence.from_mapping(value) for value in values]
        by_slice = {item.slice_id: item for item in lines}
        required = {"M1-S08-01": 9_000, "M1-S08-02": 7_000}
        for slice_id, minimum in required.items():
            item = by_slice.get(slice_id)
            if item is None:
                result.add(
                    Finding(
                        code="lines.slice_missing",
                        severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                        summary="Effective-line audit lacks one required M1-08 slice.",
                        detail=slice_id,
                    )
                )
                continue
            if item.minimum_required != minimum or not item.passes:
                result.add(
                    Finding(
                        code="lines.slice_failed",
                        severity=Severity.BLOCKER,
                        summary="Slice does not meet its frozen conservative effective-line floor.",
                        detail=(
                            f"{slice_id}: effective={item.effective_production}; "
                            f"minimum={minimum}; declared={item.minimum_required}"
                        ),
                    )
                )
            result.evidence.append(
                EvidencePointer(
                    kind="effective_line_audit",
                    location=slice_id,
                    summary=f"{item.effective_production}/{minimum} effective production lines",
                    revision=item.target_commit,
                    metadata={"auditor_digest": item.auditor_digest},
                )
            )
        parent = sum(item.effective_production for item in lines)
        if parent < 16_000:
            result.add(
                Finding(
                    code="lines.parent_failed",
                    severity=Severity.BLOCKER,
                    summary="M1-08 cumulative effective production lines are below 16,000.",
                    detail=str(parent),
                )
            )
        result.metrics.update(
            {
                "slice_count": len(lines),
                "parent_effective_production": parent,
                "slices": [item.to_dict() for item in lines],
            }
        )
        return result.finish(default_partial=not final_completion)

    def _build_handoff(
        self,
        options: IntegrationOptions,
        gates: Sequence[GateResult],
        evidence: Sequence[ScenarioEvidence],
        cleanroom: CleanroomReceipt | None,
        base_audits: Sequence[AuditOutcome],
        report_id: str,
    ) -> M2HandoffContract:
        evidence_refs = tuple(item.scenario_id + ":" + item.digest() for item in evidence if item.passed)
        scenario_gate = next((gate for gate in gates if gate.gate_id == "m1-integration-scenarios"), None)
        scenario_status = (
            dict(scenario_gate.metrics.get("scenario_status") or {}) if scenario_gate else {}
        )
        custody_entries: list[Mapping[str, Any]] = []
        for audit in base_audits:
            custody = audit.report.gate("m1-state-custody")
            if custody:
                for entry in custody.metrics.get("entries") or ():
                    if isinstance(entry, Mapping):
                        custody_entries.append(dict(entry))
        custody_by_family: dict[str, Mapping[str, Any]] = {}
        for entry in custody_entries:
            family = str(entry.get("state_family") or "")
            if family:
                custody_by_family.setdefault(family, entry)
        return HandoffBuilder().build(
            baseline_commit=options.baseline_commit,
            target_commit=options.implementation_commit,
            surfaces=default_handoff_surfaces(evidence_refs),
            source_chains=options.source_chains or default_source_chain_handoffs(),
            state_custody=list(custody_by_family.values()),
            scenario_status=scenario_status,
            gates=gates,
            cleanroom_digest=cleanroom.to_dict()["content_digest"] if cleanroom else "",
            report_id=report_id,
            metadata={"migration_mode": "audit_and_hardening_only"},
        )

    @staticmethod
    def _automatic_evidence_envelopes(
        *,
        integration_run_id: str,
        benchmark_run_id: str,
        options: IntegrationOptions,
        scenarios: Sequence[ScenarioEvidence],
        benchmark_events: Sequence[Mapping[str, Any]],
        benchmark_gate: GateResult,
        cleanroom: CleanroomReceipt | None,
        handoff: M2HandoffContract,
        base_audits: Sequence[AuditOutcome],
    ) -> tuple[EvidenceEnvelope, ...]:
        if not options.final_completion:
            return ()
        factory = EvidenceEnvelopeFactory()
        contents: dict[EvidenceKind, Mapping[str, Any]] = {}
        if scenarios:
            contents[EvidenceKind.SCENARIO] = {
                "scenario_count": len(scenarios),
                "passed_count": sum(item.passed for item in scenarios),
                "scenario_digests": {
                    item.scenario_id: item.digest() for item in scenarios
                },
            }
            disconnects = [
                {
                    "probe_id": str(value.get("probe_id") or ""),
                    "capability": str(value.get("capability") or ""),
                    "status": str(value.get("status") or ""),
                    "expected_failure_observed": value.get("expected_failure_observed") is True,
                    "fallback_masked": value.get("fallback_masked") is True,
                }
                for item in scenarios
                for value in item.disconnect_evidence
            ]
            if disconnects:
                contents[EvidenceKind.DISCONNECT] = {
                    "execution_count": len(disconnects),
                    "executions": disconnects,
                    "content_digest": stable_digest(disconnects),
                }
        if benchmark_events:
            contents[EvidenceKind.CANONICAL_EVENT] = {
                "benchmark_run_id": benchmark_run_id,
                "event_count": len(benchmark_events),
                "first_event_id": str(benchmark_events[0].get("event_id") or ""),
                "last_event_id": str(benchmark_events[-1].get("event_id") or ""),
                "event_stream_digest": stable_digest(benchmark_events),
            }
        custody_digests: list[str] = []
        coverage_digests: list[str] = []
        for audit in base_audits:
            custody = audit.report.gate("m1-state-custody")
            coverage = audit.report.gate("source-to-target-coverage")
            if custody:
                custody_digests.append(stable_digest(custody.metrics))
            if coverage:
                coverage_digests.append(stable_digest(coverage.metrics))
        if custody_digests:
            contents[EvidenceKind.STATE_CUSTODY] = {
                "audit_count": len(custody_digests),
                "gate_metric_digests": sorted(custody_digests),
                "aggregate_digest": stable_digest(sorted(custody_digests)),
            }
        if coverage_digests:
            contents[EvidenceKind.SOURCE_COVERAGE] = {
                "audit_count": len(coverage_digests),
                "gate_metric_digests": sorted(coverage_digests),
                "aggregate_digest": stable_digest(sorted(coverage_digests)),
            }
        if options.line_evidence:
            contents[EvidenceKind.LINE_AUDIT] = {
                "slice_count": len(options.line_evidence),
                "line_evidence": [dict(item) for item in options.line_evidence],
                "aggregate_digest": stable_digest(options.line_evidence),
            }
        if cleanroom is not None:
            cleanroom_value = cleanroom.to_dict()
            contents[EvidenceKind.CLEANROOM] = {
                "target_commit": cleanroom.target_commit,
                "receipt_digest": cleanroom_value.get("content_digest"),
                "command_count": len(cleanroom.commands),
                "passed": all(item.ok for item in cleanroom.commands),
            }
        if options.tier_observations:
            contents[EvidenceKind.EXECUTION_TIER] = {
                "observation_count": len(options.tier_observations),
                "tiers": [
                    str(item.get("tier") or "") for item in options.tier_observations
                ],
                "observations_digest": stable_digest(options.tier_observations),
            }
        if options.provider_observations:
            contents[EvidenceKind.PROVIDER_WIRE] = {
                "observation_count": len(options.provider_observations),
                "providers": [
                    str(item.get("provider_id") or "")
                    for item in options.provider_observations
                ],
                "dialects": [
                    str(item.get("dialect") or "")
                    for item in options.provider_observations
                ],
                "observations_digest": stable_digest(options.provider_observations),
            }
        if benchmark_gate.ok:
            contents[EvidenceKind.BENCHMARK] = {
                "benchmark_run_id": benchmark_run_id,
                "status": benchmark_gate.status.value,
                "effective_action_count": benchmark_gate.metrics.get(
                    "effective_action_count"
                ),
                "effective_transition_count": benchmark_gate.metrics.get(
                    "effective_transition_count"
                ),
                "snapshot_digest": benchmark_gate.metrics.get("snapshot_digest"),
                "child_status": dict(benchmark_gate.metrics.get("child_status") or {}),
            }
        handoff_value = handoff.to_dict()
        contents[EvidenceKind.HANDOFF] = {
            "report_id": handoff.report_id,
            "content_digest": handoff_value.get("content_digest"),
            "surface_count": len(handoff.surfaces),
            "source_chain_count": len(handoff.source_chains),
        }
        envelopes: list[EvidenceEnvelope] = []
        for kind in EvidenceKind:
            content = contents.get(kind)
            if content is None:
                continue
            runtime_partition = kind in {
                EvidenceKind.SCENARIO,
                EvidenceKind.DISCONNECT,
                EvidenceKind.CANONICAL_EVENT,
                EvidenceKind.BENCHMARK,
            }
            envelopes.append(
                factory.create(
                    evidence_id=f"m1:{integration_run_id}:{kind.value}",
                    kind=kind,
                    origin=(
                        EvidenceOrigin.LIVE_RUNTIME
                        if kind
                        in {
                            EvidenceKind.SCENARIO,
                            EvidenceKind.DISCONNECT,
                            EvidenceKind.CANONICAL_EVENT,
                            EvidenceKind.EXECUTION_TIER,
                            EvidenceKind.PROVIDER_WIRE,
                            EvidenceKind.BENCHMARK,
                        }
                        else EvidenceOrigin.LOCAL_AUDITOR
                    ),
                    producer=f"zyra.m1-hardening.{kind.value}",
                    content=content,
                    baseline_commit=options.baseline_commit,
                    target_commit=options.implementation_commit,
                    run_id=integration_run_id if runtime_partition else "",
                    task_id="m1-integration" if runtime_partition else "",
                    metadata={
                        "automatic_final_admission": True,
                        "benchmark_run_id": benchmark_run_id,
                    },
                )
            )
        return tuple(envelopes)

    def _build_exit_bundle(
        self,
        options: IntegrationOptions,
        gates: Sequence[GateResult],
        evidence: Sequence[ScenarioEvidence],
        cleanroom: CleanroomReceipt | None,
        handoff: M2HandoffContract,
        executed_capabilities: Sequence[str],
        base_audits: Sequence[AuditOutcome],
    ) -> M1ExitBundle:
        scenario_gate = next((gate for gate in gates if gate.gate_id == "m1-integration-scenarios"), None)
        scenario_status = dict(scenario_gate.metrics.get("scenario_status") or {}) if scenario_gate else {}
        custody_digests: list[str] = []
        coverage_digests: list[str] = []
        for audit in base_audits:
            custody = audit.report.gate("m1-state-custody")
            coverage = audit.report.gate("source-to-target-coverage")
            if custody:
                custody_digests.append(stable_digest(custody.metrics))
            if coverage:
                coverage_digests.append(stable_digest(coverage.metrics))
        return ExitBundleBuilder().build(
            baseline_commit=options.baseline_commit,
            implementation_commit=options.implementation_commit,
            evidence_commit=options.evidence_commit,
            line_evidence=options.line_evidence,
            gates=gates,
            scenario_status=scenario_status,
            executed_disable_capabilities=executed_capabilities,
            cleanroom_commit=cleanroom.target_commit if cleanroom else "",
            cleanroom_digest=cleanroom.to_dict()["content_digest"] if cleanroom else "",
            state_custody_digest=stable_digest(sorted(custody_digests)) if custody_digests else "",
            source_coverage_digest=stable_digest(sorted(coverage_digests)) if coverage_digests else "",
            handoff_digest=handoff.to_dict()["content_digest"],
            unresolved_requirements=options.unresolved_requirements,
            metadata={"scenario_evidence_digests": [item.digest() for item in evidence]},
        )

    @staticmethod
    def _executed_probes(
        evidence: Sequence[ScenarioEvidence],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        probe_ids: set[str] = set()
        capabilities: set[str] = set()
        for scenario in evidence:
            for item in scenario.disconnect_evidence:
                if str(item.get("status") or "").lower() != "passed":
                    continue
                probe_id = str(item.get("probe_id") or "")
                capability = str(item.get("capability") or "")
                if probe_id:
                    probe_ids.add(probe_id)
                if capability:
                    capabilities.add(capability)
        return tuple(sorted(probe_ids)), tuple(sorted(capabilities))

    @staticmethod
    def _benchmark_events(
        evidence: Sequence[ScenarioEvidence],
        run_id: str,
    ) -> list[Mapping[str, Any]]:
        values: list[Mapping[str, Any]] = []
        for scenario in evidence:
            if run_id and scenario.run_id != run_id:
                continue
            values.extend(scenario.events)
        return values

    @staticmethod
    def _single_run_id(events: Sequence[Mapping[str, Any]]) -> str:
        values = {
            str(event.get("run_id") or (event.get("payload") or {}).get("run_id") or "")
            for event in events
        }
        values.discard("")
        return next(iter(values)) if len(values) == 1 else ""

    @staticmethod
    def _not_run_gate(gate_id: str, limitation: str) -> GateResult:
        result = GateResult(gate_id=gate_id, status=GateStatus.NOT_RUN, summary=limitation)
        result.add(
            Finding(
                code="integration.gate_not_executed",
                severity=Severity.BLOCKER,
                summary=limitation,
            )
        )
        return result.finish()


def default_source_chain_handoffs() -> tuple[SourceChainHandoff, ...]:
    return (
        SourceChainHandoff(
            chain_id="claude-runtime-main-path",
            source_repository="claude-code-best",
            source_paths=("src/query*", "src/tools/**", "src/services/**", "src/tasks/**"),
            source_role="primary",
            source_language="TypeScript/TSX",
            target_paths=("packages/runtime/claude-runtime", "apps/code-worker", "packages/integrations/claude-mcp"),
            target_language="TypeScript",
            canonical_owner="QueryEngine/CodeWorkerRuntime",
            state_family="task_session",
            main_path_surfaces=("task-query-worker-api", "permission-control-api"),
            test_paths=("tests/integration/test_claude_productization_integration.py",),
            disable_probe_ids=("disable-query-session", "disable-tool-loop", "disable-permission-runtime"),
            residual_dependencies=(),
            final_decision="active_real",
        ),
        SourceChainHandoff(
            chain_id="opencode-event-provider",
            source_repository="opencode",
            source_paths=("packages/opencode/src/session/**", "packages/opencode/src/provider/**"),
            source_role="primary",
            source_language="TypeScript",
            target_paths=("packages/runtime/runtime-event-spine", "packages/runtime/provider-control-plane"),
            target_language="TypeScript",
            canonical_owner="RuntimeEventSpine/ProviderControlPlane",
            state_family="runtime_event",
            main_path_surfaces=("runtime-event-stream-api",),
            test_paths=("tests/integration/test_e02_typescript_api_cutover.py",),
            disable_probe_ids=("disable-runtime-event-spine", "disable-provider-control-plane"),
            residual_dependencies=(),
            final_decision="active_real",
        ),
        SourceChainHandoff(
            chain_id="omp-selected-supplements",
            source_repository="oh-my-pi",
            source_paths=("TaskTool/PAL", "Mnemopi", "provider fallback", "Hashline"),
            source_role="supplementary",
            source_language="TypeScript/Rust",
            target_paths=("packages/runtime/claude-runtime", "packages/memory", "packages/runtime/provider-control-plane", "packages/workspace"),
            target_language="TypeScript/Python/Rust",
            canonical_owner="existing Zyra task/memory/provider/patch owners",
            state_family="task_session",
            main_path_surfaces=("task-query-worker-api", "task-artifact-surface"),
            test_paths=("tests/integration",),
            disable_probe_ids=("disable-tool-loop", "disable-memory-retrieval", "disable-provider-control-plane"),
            residual_dependencies=(),
            final_decision="active_real",
        ),
        SourceChainHandoff(
            chain_id="langgraph-narrow-checkpoint",
            source_repository="langgraph",
            source_paths=("libs/checkpoint/**", "selected loop/checkpoint semantics"),
            source_role="conformance",
            source_language="Python",
            target_paths=(),
            target_language="Python",
            canonical_owner="GraphStateStore/RecoveryPlanStore",
            state_family="graph_topology",
            main_path_surfaces=(),
            test_paths=("tests/unit/test_graph_state_custody.py",),
            disable_probe_ids=(),
            residual_dependencies=(),
            final_decision="conformance_only",
        ),
        SourceChainHandoff(
            chain_id="openclaw-forward-exclusion",
            source_repository="OpenClaw",
            source_paths=(),
            source_role="excluded",
            source_language="TypeScript",
            target_paths=(),
            target_language="none",
            canonical_owner="",
            state_family="none",
            main_path_surfaces=(),
            test_paths=(),
            disable_probe_ids=(),
            residual_dependencies=(),
            final_decision="excluded_forward_only",
        ),
    )


def re_safe_id(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,120}", value))
