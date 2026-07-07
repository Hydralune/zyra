from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable

from zyra_core import EventRecord, EventType, now_iso

from .claude_runtime_context_ports import RuntimeContextAssemblyReport
from .claude_source_graph_crosswalk import (
    ClaudeProductizationIntegrationReport,
    EventContract,
    EventContractPhase,
)
from .workers import WorkerRequest


class EventContractStatus(StrEnum):
    READY = "ready"
    DECLARED_ONLY = "declared_only"
    WARNING = "warning"
    BLOCKED = "blocked"


class EventContractSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class EventContractRisk(StrEnum):
    DUPLICATE_PHASE = "duplicate_phase"
    MISSING_OWNER = "missing_owner"
    MISSING_PAYLOAD_KEY = "missing_payload_key"
    MISSING_FIELDS = "missing_fields"
    REQUIRED_EVENT_NOT_DECLARED = "required_event_not_declared"
    CURRENT_EVENT_NOT_OBSERVED = "current_event_not_observed"
    UNKNOWN_RUNTIME_EVENT = "unknown_runtime_event"


@dataclass(frozen=True, slots=True)
class EventPayloadField:
    field_path: str
    required: bool
    source_phase: EventContractPhase
    producer: str
    consumer: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_path": self.field_path,
            "required": self.required,
            "source_phase": str(self.source_phase),
            "producer": self.producer,
            "consumer": self.consumer,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class EventContractSchema:
    phase: EventContractPhase
    payload_key: str
    owner_slice: str
    producer: str
    consumer: str
    fields: tuple[EventPayloadField, ...]
    currently_emitted: bool
    downstream_owner: str = ""

    @property
    def schema_id(self) -> str:
        return f"{self.payload_key}.{self.phase}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "phase": str(self.phase),
            "payload_key": self.payload_key,
            "owner_slice": self.owner_slice,
            "producer": self.producer,
            "consumer": self.consumer,
            "fields": [field.to_dict() for field in self.fields],
            "currently_emitted": self.currently_emitted,
            "downstream_owner": self.downstream_owner,
        }


@dataclass(frozen=True, slots=True)
class EventObservation:
    phase: EventContractPhase | str
    payload_key: str
    source: str
    observed: bool
    event_type: EventType | str = EventType.CONSTRAINT_CHECK
    sample_fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "payload_key": self.payload_key,
            "source": self.source,
            "observed": self.observed,
            "event_type": str(self.event_type),
            "sample_fields": list(self.sample_fields),
        }


@dataclass(frozen=True, slots=True)
class EventOwnerCoverage:
    owner_slice: str
    declared_phases: tuple[str, ...]
    observed_phases: tuple[str, ...]
    current_event_count: int
    downstream_event_count: int
    status: EventContractStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_slice": self.owner_slice,
            "declared_phases": list(self.declared_phases),
            "observed_phases": list(self.observed_phases),
            "current_event_count": self.current_event_count,
            "downstream_event_count": self.downstream_event_count,
            "status": str(self.status),
        }


@dataclass(frozen=True, slots=True)
class EventContractFinding:
    severity: EventContractSeverity
    code: str
    message: str
    risk: EventContractRisk | str
    phase: str = ""
    owner_slice: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == EventContractSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "risk": str(self.risk),
            "phase": self.phase,
            "owner_slice": self.owner_slice,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class EventContractRuntimeReport:
    ok: bool
    checked_at: str
    contract_id: str
    owner_slice: str
    schemas: tuple[EventContractSchema, ...]
    observations: tuple[EventObservation, ...]
    owner_coverage: tuple[EventOwnerCoverage, ...]
    findings: tuple[EventContractFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[EventContractFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[EventContractFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == EventContractSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    def metadata(self) -> dict[str, str]:
        observed = sum(1 for observation in self.observations if observation.observed)
        current = sum(1 for schema in self.schemas if schema.currently_emitted)
        downstream = len(self.schemas) - current
        return {
            "event_contract_runtime_ok": str(self.ok).lower(),
            "event_contract_runtime_schemas": str(len(self.schemas)),
            "event_contract_runtime_observations": str(len(self.observations)),
            "event_contract_runtime_observed": str(observed),
            "event_contract_runtime_current_events": str(current),
            "event_contract_runtime_downstream_events": str(downstream),
            "event_contract_runtime_owner_count": str(len(self.owner_coverage)),
            "event_contract_runtime_blockers": str(len(self.blockers)),
            "event_contract_runtime_warnings": str(len(self.warnings)),
            "event_contract_runtime_first_blocker": self.first_blocker_code,
        }

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": "event_contract_runtime",
            "ok": self.ok,
            "contract_id": self.contract_id,
            "owner_slice": self.owner_slice,
            "schema_count": len(self.schemas),
            "observation_count": len(self.observations),
            "owner_count": len(self.owner_coverage),
            "blocking_error": self.first_blocker_code,
            "blockers": [finding.to_dict() for finding in self.blockers],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "contract_id": self.contract_id,
            "owner_slice": self.owner_slice,
            "schemas": [schema.to_dict() for schema in self.schemas],
            "observations": [observation.to_dict() for observation in self.observations],
            "owner_coverage": [coverage.to_dict() for coverage in self.owner_coverage],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


def build_event_contract_runtime_report(
    *,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport | None = None,
) -> EventContractRuntimeReport:
    schemas = tuple(_schema_from_contract(contract) for contract in integration_report.crosswalk.event_contracts)
    observations = tuple(_observations_from_runtime_context(schemas, runtime_context_report))
    owner_coverage = tuple(_owner_coverage(schemas, observations))
    findings = [
        *_schema_findings(schemas),
        *_observation_findings(schemas, observations, runtime_context_report=runtime_context_report),
        *_owner_coverage_findings(owner_coverage),
        *_required_phase_findings(schemas),
    ]
    ok = integration_report.ok and not any(finding.blocking for finding in findings)
    return EventContractRuntimeReport(
        ok=ok,
        checked_at=now_iso(),
        contract_id=integration_report.crosswalk.contract_id,
        owner_slice=integration_report.crosswalk.owner_slice,
        schemas=schemas,
        observations=observations,
        owner_coverage=owner_coverage,
        findings=tuple(findings),
    )


def event_contract_runtime_event(request: WorkerRequest, report: EventContractRuntimeReport) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.CONSTRAINT_CHECK,
        payload={"claude_event_contract_runtime": report.event_payload()},
    )


def event_contract_runtime_markdown(report: EventContractRuntimeReport) -> str:
    lines = [
        "## Event Contract Runtime",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- schemas: `{len(report.schemas)}`",
        f"- observations: `{len(report.observations)}`",
        f"- owner_coverage: `{len(report.owner_coverage)}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Owners",
        "",
    ]
    for coverage in report.owner_coverage:
        lines.append(
            f"- `{coverage.owner_slice}` status=`{coverage.status}` declared=`{len(coverage.declared_phases)}` observed=`{len(coverage.observed_phases)}`"
        )
    if report.blockers:
        lines.extend(["", "### Event Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    if report.warnings:
        lines.extend(["", "### Event Warnings", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.warnings[:12])
    return "\n".join(lines) + "\n"


def assert_event_contract_runtime_ready(report: EventContractRuntimeReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"Event contract runtime report is not ready: {blockers}")


def _schema_from_contract(contract: EventContract) -> EventContractSchema:
    fields = tuple(
        EventPayloadField(
            field_path=f"{contract.payload_key}.{field}",
            required=True,
            source_phase=contract.phase,
            producer=contract.producer,
            consumer=contract.consumer,
            description=f"Required by {contract.phase} event contract.",
        )
        for field in contract.required_fields
    )
    return EventContractSchema(
        phase=contract.phase,
        payload_key=contract.payload_key,
        owner_slice=contract.owner_slice,
        producer=contract.producer,
        consumer=contract.consumer,
        fields=fields,
        currently_emitted=contract.currently_emitted,
        downstream_owner=contract.downstream_owner,
    )


def _observations_from_runtime_context(
    schemas: Iterable[EventContractSchema],
    runtime_context_report: RuntimeContextAssemblyReport | None,
) -> list[EventObservation]:
    observed_phases = _runtime_context_phases(runtime_context_report)
    observations: list[EventObservation] = []
    for schema in schemas:
        observations.append(
            EventObservation(
                phase=schema.phase,
                payload_key=schema.payload_key,
                source="RuntimeContextAssemblyReport" if runtime_context_report else "declared_crosswalk",
                observed=str(schema.phase) in observed_phases or runtime_context_report is None,
                sample_fields=tuple(field.field_path for field in schema.fields[:8]),
            )
        )
    return observations


def _owner_coverage(
    schemas: Iterable[EventContractSchema],
    observations: Iterable[EventObservation],
) -> list[EventOwnerCoverage]:
    observation_map = {str(observation.phase): observation for observation in observations}
    by_owner: dict[str, list[EventContractSchema]] = {}
    for schema in schemas:
        by_owner.setdefault(schema.owner_slice, []).append(schema)
    coverage_rows: list[EventOwnerCoverage] = []
    for owner, owner_schemas in sorted(by_owner.items()):
        declared = tuple(str(schema.phase) for schema in owner_schemas)
        observed = tuple(
            str(schema.phase)
            for schema in owner_schemas
            if observation_map.get(str(schema.phase)) and observation_map[str(schema.phase)].observed
        )
        current_count = sum(1 for schema in owner_schemas if schema.currently_emitted)
        downstream_count = len(owner_schemas) - current_count
        if current_count and not observed:
            status = EventContractStatus.WARNING
        elif current_count:
            status = EventContractStatus.READY
        else:
            status = EventContractStatus.DECLARED_ONLY
        coverage_rows.append(
            EventOwnerCoverage(
                owner_slice=owner,
                declared_phases=declared,
                observed_phases=observed,
                current_event_count=current_count,
                downstream_event_count=downstream_count,
                status=status,
            )
        )
    return coverage_rows


def _schema_findings(schemas: Iterable[EventContractSchema]) -> list[EventContractFinding]:
    findings: list[EventContractFinding] = []
    seen_phases: set[str] = set()
    duplicate_phases: set[str] = set()
    for schema in schemas:
        phase = str(schema.phase)
        if phase in seen_phases:
            duplicate_phases.add(phase)
        seen_phases.add(phase)
        if not schema.owner_slice:
            findings.append(
                EventContractFinding(
                    severity=EventContractSeverity.BLOCKER,
                    code="event_contract_owner_missing",
                    message=f"Event contract {phase} has no owner slice.",
                    risk=EventContractRisk.MISSING_OWNER,
                    phase=phase,
                )
            )
        if not schema.payload_key:
            findings.append(
                EventContractFinding(
                    severity=EventContractSeverity.BLOCKER,
                    code="event_contract_payload_key_missing",
                    message=f"Event contract {phase} has no payload key.",
                    risk=EventContractRisk.MISSING_PAYLOAD_KEY,
                    phase=phase,
                    owner_slice=schema.owner_slice,
                )
            )
        if not schema.fields:
            findings.append(
                EventContractFinding(
                    severity=EventContractSeverity.WARNING,
                    code="event_contract_required_fields_missing",
                    message=f"Event contract {phase} has no required fields.",
                    risk=EventContractRisk.MISSING_FIELDS,
                    phase=phase,
                    owner_slice=schema.owner_slice,
                )
            )
    for phase in sorted(duplicate_phases):
        findings.append(
            EventContractFinding(
                severity=EventContractSeverity.BLOCKER,
                code="event_contract_duplicate_phase",
                message=f"Event contract phase {phase} is declared more than once.",
                risk=EventContractRisk.DUPLICATE_PHASE,
                phase=phase,
            )
        )
    return findings


def _observation_findings(
    schemas: Iterable[EventContractSchema],
    observations: Iterable[EventObservation],
    *,
    runtime_context_report: RuntimeContextAssemblyReport | None,
) -> list[EventContractFinding]:
    if runtime_context_report is None:
        return []
    observation_map = {str(observation.phase): observation for observation in observations}
    findings: list[EventContractFinding] = []
    for schema in schemas:
        observation = observation_map.get(str(schema.phase))
        if schema.currently_emitted and (observation is None or not observation.observed):
            findings.append(
                EventContractFinding(
                    severity=EventContractSeverity.WARNING,
                    code="event_contract_current_event_not_observed",
                    message=f"Current event contract {schema.phase} was not observed in RuntimeContext assembly.",
                    risk=EventContractRisk.CURRENT_EVENT_NOT_OBSERVED,
                    phase=str(schema.phase),
                    owner_slice=schema.owner_slice,
                )
            )
    return findings


def _owner_coverage_findings(coverage_rows: Iterable[EventOwnerCoverage]) -> list[EventContractFinding]:
    findings: list[EventContractFinding] = []
    for coverage in coverage_rows:
        if not coverage.declared_phases:
            findings.append(
                EventContractFinding(
                    severity=EventContractSeverity.BLOCKER,
                    code="event_owner_has_no_declared_phases",
                    message=f"Owner {coverage.owner_slice} has no event contract phases.",
                    risk=EventContractRisk.REQUIRED_EVENT_NOT_DECLARED,
                    owner_slice=coverage.owner_slice,
                )
            )
    return findings


def _required_phase_findings(schemas: Iterable[EventContractSchema]) -> list[EventContractFinding]:
    required = {
        EventContractPhase.SOURCE_GRAPH_CROSSWALK_READY,
        EventContractPhase.RUNTIME_CONTEXT_READY,
        EventContractPhase.DOWNSTREAM_CONTRACTS_READY,
        EventContractPhase.INTEGRATION_GATE_PASSED,
        EventContractPhase.TOOL_USE_REQUESTED,
        EventContractPhase.TOOL_RESULT_RECORDED,
        EventContractPhase.PERMISSION_REQUESTED,
        EventContractPhase.PERMISSION_DECIDED,
        EventContractPhase.CONTEXT_COMPACTED,
        EventContractPhase.CONTROL_COMMAND_RECEIVED,
        EventContractPhase.CONTROL_COMMAND_APPLIED,
    }
    actual = {schema.phase for schema in schemas}
    return [
        EventContractFinding(
            severity=EventContractSeverity.BLOCKER,
            code="event_contract_required_phase_missing",
            message=f"Required event contract phase {phase} is missing.",
            risk=EventContractRisk.REQUIRED_EVENT_NOT_DECLARED,
            phase=str(phase),
        )
        for phase in sorted(required - actual, key=str)
    ]


def _runtime_context_phases(report: RuntimeContextAssemblyReport | None) -> set[str]:
    if report is None:
        return set()
    phases: set[str] = {str(EventContractPhase.RUNTIME_CONTEXT_READY)}
    for binding in report.runtime_bindings:
        phases.update(str(phase) for phase in binding.event_phases)
    for binding in report.tool_use_bindings:
        phases.update(str(phase) for phase in binding.event_phases)
    return phases
