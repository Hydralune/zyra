from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping

from zyra_core import now_iso

from .claude_runtime_context_ports import RuntimeContextAssemblyReport
from .claude_source_graph_crosswalk import (
    ClaudeProductizationIntegrationReport,
    ClaudeSourceGraphBatch,
    DownstreamContract,
    EventContractPhase,
    RuntimePortKind,
    SourceGraphOwnerStatus,
)


class HandoffReadinessStatus(StrEnum):
    READY = "ready"
    WARNING = "warning"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


class HandoffFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class HandoffRiskKind(StrEnum):
    MISSING_RUNTIME_PORT = "missing_runtime_port"
    MISSING_EVENT_CONTRACT = "missing_event_contract"
    MISSING_ACCEPTANCE_TEST = "missing_acceptance_test"
    OWNER_NOT_DECLARED = "owner_not_declared"
    SOURCE_BATCH_NOT_COVERED = "source_batch_not_covered"
    TARGET_PATH_NOT_DECLARED = "target_path_not_declared"
    UPSTREAM_SOURCE_POOL_LEAK = "upstream_source_pool_leak"
    DOWNSTREAM_DEFERRED = "downstream_deferred"


class HandoffDependencyKind(StrEnum):
    SOURCE_BATCH = "source_batch"
    RUNTIME_PORT = "runtime_port"
    EVENT_CONTRACT = "event_contract"
    TARGET_MODULE = "target_module"
    ACCEPTANCE_TEST = "acceptance_test"


@dataclass(frozen=True, slots=True)
class HandoffDependency:
    dependency_id: str
    kind: HandoffDependencyKind
    value: str
    owner_slice: str
    required: bool = True
    observed: bool = False
    source: str = ""

    @property
    def missing(self) -> bool:
        return self.required and not self.observed

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency_id": self.dependency_id,
            "kind": str(self.kind),
            "value": self.value,
            "owner_slice": self.owner_slice,
            "required": self.required,
            "observed": self.observed,
            "missing": self.missing,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class HandoffAcceptanceGate:
    gate_id: str
    owner_slice: str
    target_unit: str
    description: str
    required_tests: tuple[str, ...]
    required_events: tuple[str, ...]
    required_ports: tuple[str, ...]
    status: HandoffReadinessStatus
    rationale: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == HandoffReadinessStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "owner_slice": self.owner_slice,
            "target_unit": self.target_unit,
            "description": self.description,
            "required_tests": list(self.required_tests),
            "required_events": list(self.required_events),
            "required_ports": list(self.required_ports),
            "status": str(self.status),
            "rationale": self.rationale,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class HandoffOwnerPackage:
    owner_slice: str
    target_units: tuple[str, ...]
    capabilities: tuple[str, ...]
    source_batches: tuple[str, ...]
    runtime_ports: tuple[str, ...]
    event_contracts: tuple[str, ...]
    target_paths: tuple[str, ...]
    acceptance_tests: tuple[str, ...]
    gates: tuple[HandoffAcceptanceGate, ...]
    dependencies: tuple[HandoffDependency, ...]
    status: HandoffReadinessStatus
    downstream_contract_ids: tuple[str, ...]

    @property
    def blocker_count(self) -> int:
        return sum(1 for gate in self.gates if gate.blocking) + sum(1 for dependency in self.dependencies if dependency.missing)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_slice": self.owner_slice,
            "target_units": list(self.target_units),
            "capabilities": list(self.capabilities),
            "source_batches": list(self.source_batches),
            "runtime_ports": list(self.runtime_ports),
            "event_contracts": list(self.event_contracts),
            "target_paths": list(self.target_paths),
            "acceptance_tests": list(self.acceptance_tests),
            "gates": [gate.to_dict() for gate in self.gates],
            "dependencies": [dependency.to_dict() for dependency in self.dependencies],
            "status": str(self.status),
            "downstream_contract_ids": list(self.downstream_contract_ids),
            "blocker_count": self.blocker_count,
        }


@dataclass(frozen=True, slots=True)
class HandoffDependencyEdge:
    edge_id: str
    from_owner: str
    to_owner: str
    dependency_kind: HandoffDependencyKind
    value: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "from_owner": self.from_owner,
            "to_owner": self.to_owner,
            "dependency_kind": str(self.dependency_kind),
            "value": self.value,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class HandoffFinding:
    severity: HandoffFindingSeverity
    code: str
    message: str
    owner_slice: str = ""
    risk_kind: HandoffRiskKind | str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == HandoffFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "owner_slice": self.owner_slice,
            "risk_kind": str(self.risk_kind),
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class DownstreamHandoffReport:
    ok: bool
    checked_at: str
    contract_id: str
    owner_slice: str
    owner_packages: tuple[HandoffOwnerPackage, ...]
    dependency_edges: tuple[HandoffDependencyEdge, ...]
    findings: tuple[HandoffFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[HandoffFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[HandoffFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == HandoffFindingSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    @property
    def owner_count(self) -> int:
        return len(self.owner_packages)

    def owner(self, owner_slice: str) -> HandoffOwnerPackage | None:
        for package in self.owner_packages:
            if package.owner_slice == owner_slice:
                return package
        return None

    def metadata(self) -> dict[str, str]:
        return {
            "downstream_handoff_ok": str(self.ok).lower(),
            "downstream_handoff_owner_count": str(len(self.owner_packages)),
            "downstream_handoff_edge_count": str(len(self.dependency_edges)),
            "downstream_handoff_blockers": str(len(self.blockers)),
            "downstream_handoff_warnings": str(len(self.warnings)),
            "downstream_handoff_first_blocker": self.first_blocker_code,
            "downstream_handoff_contract_id": self.contract_id,
            "downstream_handoff_owner_slice": self.owner_slice,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "contract_id": self.contract_id,
            "owner_slice": self.owner_slice,
            "owner_packages": [package.to_dict() for package in self.owner_packages],
            "dependency_edges": [edge.to_dict() for edge in self.dependency_edges],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


def build_downstream_handoff_report(
    *,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport | None = None,
) -> DownstreamHandoffReport:
    crosswalk = integration_report.crosswalk
    observed_ports = _observed_runtime_ports(runtime_context_report) | {
        str(port.kind) for port in crosswalk.runtime_context_ports
    }
    observed_events = _observed_event_phases(runtime_context_report) | {
        str(contract.phase) for contract in crosswalk.event_contracts
    }
    packages = tuple(
        _owner_package(
            owner,
            contracts,
            observed_ports=observed_ports,
            observed_events=observed_events,
            source_batches={batch.batch for batch in crosswalk.batches},
        )
        for owner, contracts in sorted(_contracts_by_owner(crosswalk.downstream_contracts).items())
    )
    edges = _dependency_edges(packages)
    findings = [
        *_owner_findings(packages),
        *_edge_findings(edges),
        *_required_owner_findings(packages),
        *_integration_findings(integration_report),
    ]
    ok = integration_report.ok and not any(finding.blocking for finding in findings)
    return DownstreamHandoffReport(
        ok=ok,
        checked_at=now_iso(),
        contract_id=crosswalk.contract_id,
        owner_slice=crosswalk.owner_slice,
        owner_packages=packages,
        dependency_edges=edges,
        findings=tuple(findings),
    )


def downstream_handoff_markdown(report: DownstreamHandoffReport) -> str:
    lines = [
        "## Downstream Handoff Runtime",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- contract_id: `{report.contract_id}`",
        f"- owner_packages: `{len(report.owner_packages)}`",
        f"- dependency_edges: `{len(report.dependency_edges)}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Owner Packages",
        "",
    ]
    for package in report.owner_packages:
        lines.append(
            f"- `{package.owner_slice}` status=`{package.status}` units=`{len(package.target_units)}` ports=`{len(package.runtime_ports)}` tests=`{len(package.acceptance_tests)}`"
        )
    if report.dependency_edges:
        lines.extend(["", "### Dependency Edges", ""])
        for edge in report.dependency_edges[:24]:
            lines.append(f"- `{edge.from_owner}` -> `{edge.to_owner}` `{edge.value}`")
    if report.blockers:
        lines.extend(["", "### Handoff Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    return "\n".join(lines) + "\n"


def assert_downstream_handoff_ready(report: DownstreamHandoffReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"Downstream handoff report is not ready: {blockers}")


def _contracts_by_owner(contracts: Iterable[DownstreamContract]) -> dict[str, list[DownstreamContract]]:
    by_owner: dict[str, list[DownstreamContract]] = {}
    for contract in contracts:
        by_owner.setdefault(contract.owner_slice, []).append(contract)
    return by_owner


def _owner_package(
    owner_slice: str,
    contracts: list[DownstreamContract],
    *,
    observed_ports: set[str],
    observed_events: set[str],
    source_batches: set[ClaudeSourceGraphBatch],
) -> HandoffOwnerPackage:
    target_units = _unique(contract.target_unit for contract in contracts)
    capabilities = _unique(contract.capability for contract in contracts)
    batches = _unique(str(batch) for contract in contracts for batch in contract.source_batches)
    ports = _unique(str(port) for contract in contracts for port in contract.required_ports)
    events = _unique(str(event) for contract in contracts for event in contract.required_events)
    targets = _unique(path for contract in contracts for path in contract.required_targets)
    tests = _unique(test for contract in contracts for test in contract.acceptance_test_entrypoints)
    gates = tuple(
        HandoffAcceptanceGate(
            gate_id=f"{contract.contract_id}.acceptance",
            owner_slice=owner_slice,
            target_unit=contract.target_unit,
            description=contract.capability,
            required_tests=contract.acceptance_test_entrypoints,
            required_events=tuple(str(event) for event in contract.required_events),
            required_ports=tuple(str(port) for port in contract.required_ports),
            status=_gate_status(contract),
            rationale="Gate is ready when required ports, events and behavior tests are declared for the downstream owner.",
        )
        for contract in contracts
    )
    dependencies = tuple(
        [
            *(
                HandoffDependency(
                    dependency_id=f"{owner_slice}.batch.{batch}",
                    kind=HandoffDependencyKind.SOURCE_BATCH,
                    value=str(batch),
                    owner_slice=owner_slice,
                    observed=_batch_observed(batch, source_batches),
                    source="ClaudeSourceGraphCrosswalk.batches",
                )
                for batch in batches
            ),
            *(
                HandoffDependency(
                    dependency_id=f"{owner_slice}.port.{port}",
                    kind=HandoffDependencyKind.RUNTIME_PORT,
                    value=port,
                    owner_slice=owner_slice,
                    observed=port in observed_ports or observed_ports == set(),
                    source="RuntimeContextAssemblyReport.runtime_bindings",
                )
                for port in ports
            ),
            *(
                HandoffDependency(
                    dependency_id=f"{owner_slice}.event.{event}",
                    kind=HandoffDependencyKind.EVENT_CONTRACT,
                    value=event,
                    owner_slice=owner_slice,
                    observed=event in observed_events or observed_events == set(),
                    source="RuntimeContextAssemblyReport.runtime_bindings/event_phases",
                )
                for event in events
            ),
            *(
                HandoffDependency(
                    dependency_id=f"{owner_slice}.target.{index}",
                    kind=HandoffDependencyKind.TARGET_MODULE,
                    value=target,
                    owner_slice=owner_slice,
                    required=False,
                    observed=bool(target),
                    source="DownstreamContract.required_targets",
                )
                for index, target in enumerate(targets, start=1)
            ),
            *(
                HandoffDependency(
                    dependency_id=f"{owner_slice}.test.{index}",
                    kind=HandoffDependencyKind.ACCEPTANCE_TEST,
                    value=test,
                    owner_slice=owner_slice,
                    required=True,
                    observed=bool(test),
                    source="DownstreamContract.acceptance_test_entrypoints",
                )
                for index, test in enumerate(tests, start=1)
            ),
        ]
    )
    status = _package_status(gates, dependencies)
    return HandoffOwnerPackage(
        owner_slice=owner_slice,
        target_units=target_units,
        capabilities=capabilities,
        source_batches=batches,
        runtime_ports=ports,
        event_contracts=events,
        target_paths=targets,
        acceptance_tests=tests,
        gates=gates,
        dependencies=dependencies,
        status=status,
        downstream_contract_ids=tuple(contract.contract_id for contract in contracts),
    )


def _gate_status(contract: DownstreamContract) -> HandoffReadinessStatus:
    if contract.handoff_status == SourceGraphOwnerStatus.DOWNSTREAM_DEFERRED:
        return HandoffReadinessStatus.DEFERRED
    if not contract.required_ports or not contract.required_events or not contract.acceptance_test_entrypoints:
        return HandoffReadinessStatus.BLOCKED
    if not contract.required_targets:
        return HandoffReadinessStatus.WARNING
    return HandoffReadinessStatus.READY


def _package_status(
    gates: Iterable[HandoffAcceptanceGate],
    dependencies: Iterable[HandoffDependency],
) -> HandoffReadinessStatus:
    gate_tuple = tuple(gates)
    dependency_tuple = tuple(dependencies)
    if any(gate.status == HandoffReadinessStatus.BLOCKED for gate in gate_tuple):
        return HandoffReadinessStatus.BLOCKED
    if any(dependency.missing for dependency in dependency_tuple):
        return HandoffReadinessStatus.BLOCKED
    if any(gate.status == HandoffReadinessStatus.DEFERRED for gate in gate_tuple):
        return HandoffReadinessStatus.DEFERRED
    if any(gate.status == HandoffReadinessStatus.WARNING for gate in gate_tuple):
        return HandoffReadinessStatus.WARNING
    return HandoffReadinessStatus.READY


def _dependency_edges(packages: Iterable[HandoffOwnerPackage]) -> tuple[HandoffDependencyEdge, ...]:
    package_tuple = tuple(packages)
    owner_by_port: dict[str, str] = {}
    owner_by_event: dict[str, str] = {}
    for package in package_tuple:
        for port in package.runtime_ports:
            owner_by_port.setdefault(port, package.owner_slice)
        for event in package.event_contracts:
            owner_by_event.setdefault(event, package.owner_slice)
    edges: list[HandoffDependencyEdge] = []
    for package in package_tuple:
        for dependency in package.dependencies:
            if dependency.kind == HandoffDependencyKind.RUNTIME_PORT:
                producer = owner_by_port.get(dependency.value)
                if producer and producer != package.owner_slice:
                    edges.append(
                        HandoffDependencyEdge(
                            edge_id=f"{producer}->{package.owner_slice}:{dependency.value}",
                            from_owner=producer,
                            to_owner=package.owner_slice,
                            dependency_kind=dependency.kind,
                            value=dependency.value,
                            reason="Shared runtime port dependency",
                        )
                    )
            if dependency.kind == HandoffDependencyKind.EVENT_CONTRACT:
                producer = owner_by_event.get(dependency.value)
                if producer and producer != package.owner_slice:
                    edges.append(
                        HandoffDependencyEdge(
                            edge_id=f"{producer}->{package.owner_slice}:{dependency.value}",
                            from_owner=producer,
                            to_owner=package.owner_slice,
                            dependency_kind=dependency.kind,
                            value=dependency.value,
                            reason="Shared event contract dependency",
                        )
                    )
    return tuple(_unique_edges(edges))


def _owner_findings(packages: Iterable[HandoffOwnerPackage]) -> list[HandoffFinding]:
    findings: list[HandoffFinding] = []
    for package in packages:
        if package.status == HandoffReadinessStatus.BLOCKED:
            findings.append(
                HandoffFinding(
                    severity=HandoffFindingSeverity.BLOCKER,
                    code="downstream_handoff_owner_blocked",
                    message=f"Downstream owner {package.owner_slice} has blocked gates or missing dependencies.",
                    owner_slice=package.owner_slice,
                    risk_kind=HandoffRiskKind.MISSING_RUNTIME_PORT,
                )
            )
        if not package.acceptance_tests:
            findings.append(
                HandoffFinding(
                    severity=HandoffFindingSeverity.BLOCKER,
                    code="downstream_handoff_acceptance_tests_missing",
                    message=f"Downstream owner {package.owner_slice} has no declared acceptance test entrypoint.",
                    owner_slice=package.owner_slice,
                    risk_kind=HandoffRiskKind.MISSING_ACCEPTANCE_TEST,
                )
            )
        if not package.runtime_ports:
            findings.append(
                HandoffFinding(
                    severity=HandoffFindingSeverity.BLOCKER,
                    code="downstream_handoff_runtime_ports_missing",
                    message=f"Downstream owner {package.owner_slice} has no runtime ports.",
                    owner_slice=package.owner_slice,
                    risk_kind=HandoffRiskKind.MISSING_RUNTIME_PORT,
                )
            )
        if not package.event_contracts:
            findings.append(
                HandoffFinding(
                    severity=HandoffFindingSeverity.BLOCKER,
                    code="downstream_handoff_events_missing",
                    message=f"Downstream owner {package.owner_slice} has no event contracts.",
                    owner_slice=package.owner_slice,
                    risk_kind=HandoffRiskKind.MISSING_EVENT_CONTRACT,
                )
            )
    return findings


def _edge_findings(edges: Iterable[HandoffDependencyEdge]) -> list[HandoffFinding]:
    edge_tuple = tuple(edges)
    duplicate_ids = _duplicates(edge.edge_id for edge in edge_tuple)
    return [
        HandoffFinding(
            severity=HandoffFindingSeverity.WARNING,
            code="downstream_handoff_duplicate_dependency_edge",
            message=f"Duplicate dependency edge {edge_id} was collapsed.",
            risk_kind=HandoffRiskKind.OWNER_NOT_DECLARED,
        )
        for edge_id in duplicate_ids
    ]


def _required_owner_findings(packages: Iterable[HandoffOwnerPackage]) -> list[HandoffFinding]:
    required = {"M1-02B", "M1-02C", "M1-02D", "M1-03A", "M1-03B", "M1-03C", "M1-03D", "M2"}
    actual = {package.owner_slice for package in packages}
    return [
        HandoffFinding(
            severity=HandoffFindingSeverity.BLOCKER,
            code="downstream_handoff_required_owner_missing",
            message=f"Required downstream owner {owner} is missing from the handoff package set.",
            owner_slice=owner,
            risk_kind=HandoffRiskKind.OWNER_NOT_DECLARED,
        )
        for owner in sorted(required - actual)
    ]


def _integration_findings(report: ClaudeProductizationIntegrationReport) -> list[HandoffFinding]:
    if report.ok:
        return []
    return [
        HandoffFinding(
            severity=HandoffFindingSeverity.BLOCKER,
            code=report.blocking_error or "source_graph_integration_blocked",
            message="Source graph integration is blocked, so downstream handoff cannot be trusted.",
            owner_slice=report.crosswalk.owner_slice,
            risk_kind=HandoffRiskKind.SOURCE_BATCH_NOT_COVERED,
        )
    ]


def _observed_runtime_ports(report: RuntimeContextAssemblyReport | None) -> set[str]:
    if report is None:
        return set()
    return {str(binding.port_kind) for binding in report.runtime_bindings}


def _observed_event_phases(report: RuntimeContextAssemblyReport | None) -> set[str]:
    if report is None:
        return set()
    observed: set[str] = set()
    for binding in report.runtime_bindings:
        observed.update(str(phase) for phase in binding.event_phases)
    for binding in report.tool_use_bindings:
        observed.update(str(phase) for phase in binding.event_phases)
    return observed


def _batch_observed(batch: str, source_batches: set[ClaudeSourceGraphBatch]) -> bool:
    return any(str(source_batch) == batch for source_batch in source_batches)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _unique_edges(edges: Iterable[HandoffDependencyEdge]) -> list[HandoffDependencyEdge]:
    seen: set[str] = set()
    unique: list[HandoffDependencyEdge] = []
    for edge in edges:
        if edge.edge_id in seen:
            continue
        seen.add(edge.edge_id)
        unique.append(edge)
    return unique


def _duplicates(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))
