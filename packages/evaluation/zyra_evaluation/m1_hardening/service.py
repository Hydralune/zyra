from __future__ import annotations

import os
import threading
import time
import traceback
from collections import Counter, defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .autonomy import SealedAutonomyGate
from .catalog import M1CapabilityCatalog, M1CustodyCatalog
from .contracts import (
    EvidencePointer,
    Finding,
    GateResult,
    GateStatus,
    HardeningContext,
    HardeningReport,
    Severity,
    audit_id,
    utc_now,
)
from .coverage import SourceToTargetCoverageReport
from .cross_cutting import CrossCuttingGateSuite
from .custody import M1StateCustodyMap
from .dependency import M1InternalizationGate
from .deployment import ExecutionTierGate, ProviderControlPlaneGate
from .disable import DisableModuleProbe, ProbeHandle
from .entropy import LowEntropyGate
from .evidence_graph import CausalEvidenceGraphGate
from .langgraph import LangGraphBoundaryGate
from .line_audit import EffectiveLineAuditor
from .progress import LongHorizonProgressLedger
from .probe_catalog import default_disable_probes
from .reporting import (
    EvidenceLinkAuditor,
    GatePolicy,
    GatePolicyEngine,
    ReportDisposition,
    default_gate_policies,
)
from .scenario import FOUNDATION_SCENARIO_ID, M1MainPathScenarioSuite, ScenarioRun
from .store import HardeningReportStore, ReportRecord


class HardeningServiceError(RuntimeError):
    pass


class GateDependencyError(HardeningServiceError):
    pass


class AuditCancelled(HardeningServiceError):
    pass


@dataclass(frozen=True, slots=True)
class AuditOptions:
    baseline_commit: str
    final_completion: bool = False
    persist: bool = True
    run_dynamic_graph_probes: bool = True
    run_disable_probes: bool = False
    selected_disable_probe_ids: tuple[str, ...] = ()
    minimum_effective_lines: int = 9000
    line_audit_head: str = "HEAD"
    include_line_audit: bool = True
    include_cross_cutting: bool = True
    include_scenario: bool = True
    gate_timeout_seconds: float = 180.0
    audit_lease_seconds: float = 1800.0
    sealed_policy: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.baseline_commit.strip():
            raise ValueError("baseline commit is required")
        if self.minimum_effective_lines < 0:
            raise ValueError("minimum effective lines cannot be negative")
        if self.gate_timeout_seconds <= 0:
            raise ValueError("gate timeout must be positive")
        if self.audit_lease_seconds <= 0:
            raise ValueError("audit lease must be positive")
        if self.final_completion and not self.include_line_audit:
            raise ValueError("final completion cannot disable the effective line audit")


@dataclass(frozen=True, slots=True)
class GateExecutionReceipt:
    gate_id: str
    started_at: str
    completed_at: str
    duration_ms: int
    thread_name: str
    timed_out: bool
    crashed: bool
    dependency_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "thread_name": self.thread_name,
            "timed_out": self.timed_out,
            "crashed": self.crashed,
            "dependency_ids": list(self.dependency_ids),
        }


@dataclass(slots=True)
class AuditOutcome:
    report: HardeningReport
    disposition: ReportDisposition
    evidence_audit: Mapping[str, Any]
    receipts: tuple[GateExecutionReceipt, ...]
    record: ReportRecord | None = None
    scenario_run: ScenarioRun | None = None

    @property
    def accepted(self) -> bool:
        return self.disposition.accepted

    def to_dict(self, *, include_report: bool = True, include_scenario: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "accepted": self.accepted,
            "disposition": self.disposition.to_dict(),
            "evidence_link_audit": dict(self.evidence_audit),
            "execution_receipts": [item.to_dict() for item in self.receipts],
            "record": self.record.to_dict() if self.record else None,
        }
        if include_report:
            value["report"] = self.report.to_dict()
        if include_scenario and self.scenario_run:
            value["scenario_run"] = self.scenario_run.to_dict()
        return value


@dataclass(frozen=True, slots=True)
class GateDefinition:
    gate_id: str
    execute: Callable[[], GateResult]
    dependencies: tuple[str, ...] = ()
    timeout_seconds: float = 0.0


class GateExecutionGraph:
    def __init__(self, *, default_timeout_seconds: float = 180.0) -> None:
        if default_timeout_seconds <= 0:
            raise ValueError("default timeout must be positive")
        self.default_timeout = default_timeout_seconds
        self._definitions: dict[str, GateDefinition] = {}
        self._cancelled = threading.Event()

    def add(self, definition: GateDefinition) -> None:
        gate_id = definition.gate_id.strip()
        if not gate_id:
            raise ValueError("gate definition requires an id")
        if gate_id in self._definitions:
            raise GateDependencyError(f"duplicate gate definition: {gate_id}")
        self._definitions[gate_id] = definition

    def cancel(self) -> None:
        self._cancelled.set()

    def execute(self) -> tuple[list[GateResult], list[GateExecutionReceipt]]:
        order = self._topological_order()
        results: dict[str, GateResult] = {}
        receipts: list[GateExecutionReceipt] = []
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="m1-hardening") as executor:
            for gate_id in order:
                if self._cancelled.is_set():
                    raise AuditCancelled("hardening audit was cancelled")
                definition = self._definitions[gate_id]
                blocked_dependencies = [
                    dependency
                    for dependency in definition.dependencies
                    if dependency not in results or results[dependency].status is GateStatus.BLOCKED
                ]
                if blocked_dependencies:
                    result = self._dependency_failure(gate_id, blocked_dependencies)
                    results[gate_id] = result
                    now = utc_now()
                    receipts.append(
                        GateExecutionReceipt(
                            gate_id=gate_id,
                            started_at=now,
                            completed_at=now,
                            duration_ms=0,
                            thread_name="dependency-gate",
                            timed_out=False,
                            crashed=False,
                            dependency_ids=definition.dependencies,
                        )
                    )
                    continue
                result, receipt = self._run_one(executor, definition)
                results[gate_id] = result
                receipts.append(receipt)
        return [results[gate_id] for gate_id in order], receipts

    def _run_one(
        self,
        executor: ThreadPoolExecutor,
        definition: GateDefinition,
    ) -> tuple[GateResult, GateExecutionReceipt]:
        started_wall = time.monotonic()
        started_at = utc_now()
        timeout = definition.timeout_seconds or self.default_timeout
        future: Future[GateResult] = executor.submit(definition.execute)
        timed_out = False
        crashed = False
        try:
            result = future.result(timeout=timeout)
            if not isinstance(result, GateResult):
                raise TypeError(f"gate returned {type(result).__name__}, expected GateResult")
            if result.gate_id != definition.gate_id:
                result.add(
                    Finding(
                        code="hardening.gate_identity_mismatch",
                        severity=Severity.BLOCKER,
                        summary="Gate implementation returned a different canonical id.",
                        detail=f"registered={definition.gate_id}; returned={result.gate_id}",
                    )
                )
                result.gate_id = definition.gate_id
                result.finish()
        except TimeoutError:
            timed_out = True
            future.cancel()
            result = self._execution_failure(
                definition.gate_id,
                "hardening.gate_timed_out",
                f"gate exceeded {timeout:.3f} seconds",
            )
        except Exception as error:
            crashed = True
            result = self._execution_failure(
                definition.gate_id,
                "hardening.gate_crashed",
                f"{type(error).__name__}: {error}",
                trace=traceback.format_exc(),
            )
        duration_ms = max(0, int((time.monotonic() - started_wall) * 1000))
        receipt = GateExecutionReceipt(
            gate_id=definition.gate_id,
            started_at=started_at,
            completed_at=utc_now(),
            duration_ms=duration_ms,
            thread_name="m1-hardening_0",
            timed_out=timed_out,
            crashed=crashed,
            dependency_ids=definition.dependencies,
        )
        result.metrics.setdefault("hardening_execution", receipt.to_dict())
        return result, receipt

    def _topological_order(self) -> list[str]:
        for definition in self._definitions.values():
            missing = set(definition.dependencies) - set(self._definitions)
            if missing:
                raise GateDependencyError(
                    f"gate {definition.gate_id} has missing dependencies: {', '.join(sorted(missing))}"
                )
        indegree = {gate_id: 0 for gate_id in self._definitions}
        dependants: dict[str, list[str]] = defaultdict(list)
        for gate_id, definition in self._definitions.items():
            indegree[gate_id] = len(definition.dependencies)
            for dependency in definition.dependencies:
                dependants[dependency].append(gate_id)
        queue = deque(sorted(gate_id for gate_id, degree in indegree.items() if degree == 0))
        order: list[str] = []
        while queue:
            gate_id = queue.popleft()
            order.append(gate_id)
            for dependant in sorted(dependants.get(gate_id, ())):
                indegree[dependant] -= 1
                if indegree[dependant] == 0:
                    queue.append(dependant)
        if len(order) != len(self._definitions):
            cycle = sorted(gate_id for gate_id, degree in indegree.items() if degree > 0)
            raise GateDependencyError("gate dependency cycle: " + " -> ".join(cycle))
        return order

    @staticmethod
    def _dependency_failure(gate_id: str, dependencies: Sequence[str]) -> GateResult:
        result = GateResult(
            gate_id=gate_id,
            status=GateStatus.NOT_RUN,
            summary="Gate skipped because a hard dependency was blocked.",
        )
        result.add(
            Finding(
                code="hardening.gate_dependency_blocked",
                severity=Severity.BLOCKER,
                summary="A required hardening dependency did not complete safely.",
                detail=", ".join(dependencies),
            )
        )
        return result.finish()

    @staticmethod
    def _execution_failure(gate_id: str, code: str, detail: str, *, trace: str = "") -> GateResult:
        result = GateResult(
            gate_id=gate_id,
            status=GateStatus.NOT_RUN,
            summary="Hardening gate execution failed closed.",
        )
        result.add(
            Finding(
                code=code,
                severity=Severity.BLOCKER,
                summary="The audit runtime could not produce trustworthy gate evidence.",
                detail=detail,
                metadata={"traceback": trace[-8000:] if trace else ""},
            )
        )
        return result.finish()


class M1HardeningService:
    def __init__(
        self,
        project_root: str | Path,
        *,
        source_workspace: str | Path | None = None,
        artifact_root: str | Path | None = None,
        policies: Iterable[GatePolicy] | None = None,
    ) -> None:
        self.root = Path(project_root).resolve()
        self.source_workspace = Path(source_workspace).resolve() if source_workspace else self.root.parent
        self.artifact_root = Path(artifact_root).resolve() if artifact_root else self.root / ".tmp" / "m1-hardening"
        self.catalog = M1CapabilityCatalog(self.root, source_workspace=self.source_workspace)
        self.custody_catalog = M1CustodyCatalog()
        self.scenarios = M1MainPathScenarioSuite()
        self.disable_probes = DisableModuleProbe()
        for probe in default_disable_probes(self.artifact_root):
            self.disable_probes.register(probe)
        self.policies = tuple(policies or default_gate_policies())
        self.policy = GatePolicyEngine(self.policies)
        self.evidence_auditor = EvidenceLinkAuditor()
        self.store = HardeningReportStore(self.artifact_root / "reports")
        self._active_graph: GateExecutionGraph | None = None
        self._mutex = threading.RLock()

    def register_disable_probe(self, probe: ProbeHandle) -> None:
        self.disable_probes.register(probe)

    def cancel(self) -> None:
        with self._mutex:
            if self._active_graph:
                self._active_graph.cancel()

    def audit(
        self,
        context: HardeningContext,
        options: AuditOptions,
        *,
        scenario_gate: GateResult | None = None,
        scenario_run: ScenarioRun | None = None,
    ) -> AuditOutcome:
        options.validate()
        self._validate_context(context)
        scope = f"{context.task.get('task_id') if context.task else 'repository'}:{options.final_completion}"
        with self.store.audit_lease(scope, owner=f"pid-{os.getpid()}", ttl_seconds=options.audit_lease_seconds):
            graph = self._build_graph(context, options, scenario_gate=scenario_gate)
            with self._mutex:
                if self._active_graph is not None:
                    raise HardeningServiceError("service already has an active audit")
                self._active_graph = graph
            try:
                gates, receipts = graph.execute()
            finally:
                with self._mutex:
                    self._active_graph = None
            if scenario_run is not None:
                self._attach_scenario_disable_evidence(gates, scenario_run.disable_evidence)
            report = self._report(context, options, gates, receipts, scenario_run=scenario_run)
            policy = self.policy
            if not options.include_line_audit:
                policy = GatePolicyEngine(
                    item for item in self.policies if item.gate_id != "effective-line-audit"
                )
            disposition = policy.evaluate(report, final_completion=options.final_completion)
            evidence_audit = self.evidence_auditor.evaluate(report)
            report.metadata.update(
                {
                    "disposition": disposition.to_dict(),
                    "evidence_link_audit": dict(evidence_audit),
                    "execution_receipts": [item.to_dict() for item in receipts],
                }
            )
            record = None
            if options.persist:
                record = self.store.persist(
                    report,
                    disposition=disposition,
                    evidence_audit=evidence_audit,
                )
            return AuditOutcome(
                report=report,
                disposition=disposition,
                evidence_audit=evidence_audit,
                receipts=tuple(receipts),
                record=record,
                scenario_run=scenario_run,
            )

    def run_http_foundation(
        self,
        base_url: str,
        options: AuditOptions,
        *,
        goal: str = "Exercise M1 query, permission, control, trace, and disable hardening.",
        timeout_seconds: float = 90.0,
    ) -> AuditOutcome:
        run, scenario_gate = self.scenarios.run_http_foundation(
            base_url,
            goal=goal,
            timeout_seconds=timeout_seconds,
        )
        context = HardeningContext(
            project_root=self.root,
            workspace_root=self.source_workspace,
            artifact_root=self.artifact_root,
            task=run.task_after,
            events=run.events_after,
            environment={"base_url": base_url},
            options={"scenario_id": run.scenario_id},
        )
        return self.audit(
            context,
            options,
            scenario_gate=scenario_gate,
            scenario_run=run,
        )

    def evaluate_existing_task(
        self,
        task: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        options: AuditOptions,
        *,
        responses: Mapping[str, Mapping[str, Any]] | None = None,
        disable_evidence: Mapping[str, Any] | None = None,
    ) -> AuditOutcome:
        scenario_gate = self.scenarios.evaluate_existing_task(
            task,
            events,
            responses=responses,
            disable_evidence=disable_evidence,
        )
        context = HardeningContext(
            project_root=self.root,
            workspace_root=self.source_workspace,
            artifact_root=self.artifact_root,
            task=task,
            events=events,
            options={"scenario_id": FOUNDATION_SCENARIO_ID},
        )
        return self.audit(context, options, scenario_gate=scenario_gate)

    def repository_status(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m1-hardening-service-status/v1",
            "project_root": str(self.root),
            "source_workspace": str(self.source_workspace),
            "artifact_root": str(self.artifact_root),
            "capability_catalog": self.catalog.source_summary(),
            "scenario_ids": list(self.scenarios.registered_scenario_ids()),
            "disable_probe_ids": list(self.disable_probes.registered_ids()),
            "report_store": self.store.status(),
        }

    def _build_graph(
        self,
        context: HardeningContext,
        options: AuditOptions,
        *,
        scenario_gate: GateResult | None,
    ) -> GateExecutionGraph:
        root = context.resolved_project_root()
        artifact_root = context.resolved_artifact_root()
        events = tuple(context.events)
        task = context.task or {}
        graph = GateExecutionGraph(default_timeout_seconds=options.gate_timeout_seconds)
        items = self.catalog.items()
        custody_entries = self.custody_catalog.entries()
        graph.add(
            GateDefinition(
                "source-to-target-coverage",
                lambda: SourceToTargetCoverageReport(root, source_workspace=self.source_workspace).evaluate(
                    items,
                    required_capabilities=self.catalog.required_capabilities(),
                    known_scenarios=self.scenarios.registered_scenario_ids(),
                    known_disable_probes=self.catalog.known_disable_probe_ids(),
                ),
            )
        )
        graph.add(
            GateDefinition(
                "m1-internalization",
                lambda: M1InternalizationGate(root, source_workspace=self.source_workspace).evaluate(),
                dependencies=("source-to-target-coverage",),
            )
        )
        graph.add(
            GateDefinition(
                "m1-state-custody",
                lambda: M1StateCustodyMap(root).evaluate(
                    custody_entries,
                    required_families=self.custody_catalog.required_families(),
                    runtime_required_families=(
                        ("task_session", "runtime_event", "permission", "workspace")
                        if scenario_gate is not None
                        else ()
                    ),
                    task=task,
                    events=events,
                ),
                dependencies=("source-to-target-coverage",),
            )
        )
        graph.add(
            GateDefinition(
                "langgraph-boundary",
                lambda: LangGraphBoundaryGate(root, artifact_root=artifact_root).evaluate(
                    run_dynamic_probes=options.run_dynamic_graph_probes
                ),
                dependencies=("m1-internalization",),
            )
        )
        if options.include_scenario:
            graph.add(
                GateDefinition(
                    f"scenario:{FOUNDATION_SCENARIO_ID}",
                    lambda: scenario_gate or self.scenarios.evaluate_existing_task(task, events),
                    dependencies=("m1-state-custody",),
                )
            )
        graph.add(
            GateDefinition(
                "long-horizon-progress",
                lambda: LongHorizonProgressLedger().evaluate(
                    events,
                    run_id=str(task.get("run_id") or ""),
                    final_completion=options.final_completion,
                ),
            )
        )
        graph.add(
            GateDefinition(
                "causal-evidence",
                lambda: CausalEvidenceGraphGate().evaluate(
                    events,
                    task=task,
                    final_completion=options.final_completion,
                ),
                dependencies=("m1-state-custody",),
            )
        )
        graph.add(
            GateDefinition(
                "low-entropy",
                lambda: LowEntropyGate().evaluate(events=events, final_completion=options.final_completion),
            )
        )
        graph.add(
            GateDefinition(
                "sealed-autonomy",
                lambda: SealedAutonomyGate().evaluate(
                    events,
                    declared_policy=options.sealed_policy,
                    final_completion=options.final_completion,
                ),
            )
        )
        graph.add(
            GateDefinition(
                "execution-tiers",
                lambda: ExecutionTierGate().evaluate(events=events, final_completion=options.final_completion),
            )
        )
        graph.add(
            GateDefinition(
                "provider-control-plane",
                lambda: ProviderControlPlaneGate().evaluate(events=events, final_completion=options.final_completion),
            )
        )
        if options.include_cross_cutting:
            suite = CrossCuttingGateSuite(root)
            for gate_id, evaluator in (
                ("patch-git", suite.patch_git.evaluate),
                ("deny-policy", suite.deny_policy.evaluate),
                ("secrets-prompt-injection", suite.secrets.evaluate),
                ("code-index", suite.code_index.evaluate),
            ):
                graph.add(
                    GateDefinition(
                        gate_id,
                        lambda evaluator=evaluator: evaluator(
                            events,
                            final_completion=options.final_completion,
                        ),
                    )
                )
        graph.add(
            GateDefinition(
                "disable-module-probe",
                lambda: self.disable_probes.evaluate(
                    required_capabilities=(
                        ()
                        if not options.run_disable_probes
                        else (
                            self.catalog.required_capabilities()
                            if options.final_completion
                            else self.disable_probes.registered_capabilities()
                        )
                    ),
                    selected_probe_ids=options.selected_disable_probe_ids,
                    execute=options.run_disable_probes,
                ),
                dependencies=("m1-internalization",),
            )
        )
        if options.include_line_audit:
            graph.add(
                GateDefinition(
                    "effective-line-audit",
                    lambda: EffectiveLineAuditor(root).evaluate(
                        options.baseline_commit,
                        head=options.line_audit_head,
                        minimum_effective_production=options.minimum_effective_lines,
                    ),
                )
            )
        return graph

    @staticmethod
    def _attach_scenario_disable_evidence(
        gates: Sequence[GateResult],
        evidence: Mapping[str, Any],
    ) -> None:
        """Join the live CodeWorker disconnect result to the probe gate."""

        gate = next((item for item in gates if item.gate_id == "disable-module-probe"), None)
        if gate is None:
            return
        payload = dict(evidence)
        valid = (
            payload.get("baseline_ok") is True
            and payload.get("disabled_ok") is not True
            and bool(payload.get("error_code"))
            and payload.get("fallback_masked") is not True
        )
        gate.metrics["live_scenario_disable"] = payload
        gate.evidence.append(
            EvidencePointer(
                kind="disable_probe",
                location=str(payload.get("probe_id") or "query-engine-typescript-runtime"),
                summary="Live HTTP CodeWorker owner disconnect produced an explicit unmasked failure.",
                metadata={"valid": valid, **payload},
            )
        )
        if not valid:
            gate.add(
                Finding(
                    code="disable.live_scenario_invalid",
                    severity=Severity.BLOCKER,
                    summary="Live CodeWorker disconnect evidence was missing, masked, or non-failing.",
                )
            )
        gate.finish(default_partial=not valid)

    def _report(
        self,
        context: HardeningContext,
        options: AuditOptions,
        gates: Sequence[GateResult],
        receipts: Sequence[GateExecutionReceipt],
        *,
        scenario_run: ScenarioRun | None,
    ) -> HardeningReport:
        task = context.task or {}
        task_id = str(task.get("task_id") or (scenario_run.task_id if scenario_run else ""))
        run_id = str(task.get("run_id") or (scenario_run.run_id if scenario_run else ""))
        scenario_id = str(context.options.get("scenario_id") or (scenario_run.scenario_id if scenario_run else ""))
        return HardeningReport(
            report_id=audit_id("m1-hardening"),
            baseline_commit=options.baseline_commit,
            project_root=str(context.resolved_project_root()),
            gates=list(gates),
            scenario_id=scenario_id,
            task_id=task_id,
            run_id=run_id,
            metadata={
                "schema": "zyra.m1-hardening-service/v1",
                "final_completion": options.final_completion,
                "created_at": utc_now(),
                "process_id": os.getpid(),
                "gate_status_counts": dict(Counter(gate.status.value for gate in gates)),
                "execution_receipts": [item.to_dict() for item in receipts],
                "phase_boundary": {
                    "slice": "M1-S08-01",
                    "parent_closed": False,
                    "real_edge_cloud_required_later": True,
                    "two_live_providers_required_later": True,
                    "sealed_2000_transitions_required_later": True,
                },
            },
        )

    def _validate_context(self, context: HardeningContext) -> None:
        if context.resolved_project_root() != self.root:
            raise ValueError("hardening context project root differs from service root")
        try:
            context.resolved_artifact_root().relative_to(self.root)
        except ValueError as error:
            raise ValueError("hardening artifacts must remain inside the Zyra repository") from error
        if not (self.root / "packages").is_dir() or not (self.root / "apps").is_dir():
            raise ValueError("project root does not look like the Zyra repository")
