from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from zyra_core import EventRecord, EventType, now_iso

from .claude_runtime_context_ports import RuntimeContextAssemblyReport
from .claude_runtime_contracts import ClaudeRuntimeContractBundle
from .claude_source_graph_crosswalk import ClaudeProductizationIntegrationReport, EventContractPhase, RuntimePortKind
from .workers import WorkerRequest


class StateCustodyStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    BLOCKED = "blocked"


class StateCustodySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class StateCustodyRisk(StrEnum):
    NONE = "none"
    MISSING_OWNER = "missing_owner"
    MISSING_RUNTIME_BINDING = "missing_runtime_binding"
    MISSING_REQUIRED_TARGET = "missing_required_target"
    SOURCE_POOL_PATH = "source_pool_path"
    EVENT_PHASE_GAP = "event_phase_gap"
    OBSERVABILITY_GAP = "observability_gap"


class StateCustodySource(StrEnum):
    CONTRACT_BUNDLE = "contract_bundle"
    SOURCE_GRAPH_PORT = "source_graph_port"
    RUNTIME_CONTEXT_BINDING = "runtime_context_binding"
    SOURCE_TO_TARGET = "source_to_target"
    SYNTHETIC_REQUIRED_STATE = "synthetic_required_state"


@dataclass(frozen=True, slots=True)
class StateCustodyRequirement:
    requirement_id: str
    state_key: str
    required_owner: str
    required: bool
    runtime_port_kind: RuntimePortKind | str
    expected_events: tuple[EventContractPhase | str, ...]
    expected_targets: tuple[str, ...]
    producer: str
    consumer: str
    source: StateCustodySource
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "state_key": self.state_key,
            "required_owner": self.required_owner,
            "required": self.required,
            "runtime_port_kind": str(self.runtime_port_kind),
            "expected_events": [str(event) for event in self.expected_events],
            "expected_targets": list(self.expected_targets),
            "producer": self.producer,
            "consumer": self.consumer,
            "source": str(self.source),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class StateCustodyObservation:
    requirement_id: str
    state_key: str
    required_owner: str
    observed_owner: str
    required: bool
    runtime_port_kind: RuntimePortKind | str
    runtime_binding_id: str
    runtime_binding_active: bool
    runtime_binding_status: str
    observed_events: tuple[str, ...]
    expected_events: tuple[str, ...]
    expected_targets: tuple[str, ...]
    existing_targets: tuple[str, ...]
    missing_targets: tuple[str, ...]
    source: StateCustodySource
    status: StateCustodyStatus
    risk: StateCustodyRisk
    message: str

    @property
    def blocking(self) -> bool:
        return self.status == StateCustodyStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "state_key": self.state_key,
            "required_owner": self.required_owner,
            "observed_owner": self.observed_owner,
            "required": self.required,
            "runtime_port_kind": str(self.runtime_port_kind),
            "runtime_binding_id": self.runtime_binding_id,
            "runtime_binding_active": self.runtime_binding_active,
            "runtime_binding_status": self.runtime_binding_status,
            "observed_events": list(self.observed_events),
            "expected_events": list(self.expected_events),
            "expected_targets": list(self.expected_targets),
            "existing_targets": list(self.existing_targets),
            "missing_targets": list(self.missing_targets),
            "source": str(self.source),
            "status": str(self.status),
            "risk": str(self.risk),
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class StateCustodyTransition:
    transition_id: str
    state_key: str
    from_owner: str
    to_owner: str
    event_phase: str
    producer: str
    consumer: str
    persistent_store: str
    event_payload_key: str
    status: StateCustodyStatus
    rationale: str

    @property
    def blocking(self) -> bool:
        return self.status == StateCustodyStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "state_key": self.state_key,
            "from_owner": self.from_owner,
            "to_owner": self.to_owner,
            "event_phase": self.event_phase,
            "producer": self.producer,
            "consumer": self.consumer,
            "persistent_store": self.persistent_store,
            "event_payload_key": self.event_payload_key,
            "status": str(self.status),
            "rationale": self.rationale,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class StateCustodyFinding:
    severity: StateCustodySeverity
    code: str
    message: str
    state_key: str
    requirement_id: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == StateCustodySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "state_key": self.state_key,
            "requirement_id": self.requirement_id,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class StateCustodyRuntimeReport:
    ok: bool
    checked_at: str
    owner_slice: str
    observations: tuple[StateCustodyObservation, ...]
    transitions: tuple[StateCustodyTransition, ...]
    findings: tuple[StateCustodyFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[StateCustodyFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[StateCustodyFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == StateCustodySeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    @property
    def observed_state_keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(row.state_key for row in self.observations))

    @property
    def active_runtime_bindings(self) -> int:
        return sum(1 for row in self.observations if row.runtime_binding_active)

    def metadata(self) -> dict[str, str]:
        return {
            "state_custody_runtime_ok": str(self.ok).lower(),
            "state_custody_runtime_observations": str(len(self.observations)),
            "state_custody_runtime_transitions": str(len(self.transitions)),
            "state_custody_runtime_blockers": str(len(self.blockers)),
            "state_custody_runtime_warnings": str(len(self.warnings)),
            "state_custody_runtime_first_blocker": self.first_blocker_code,
            "state_custody_runtime_active_bindings": str(self.active_runtime_bindings),
            "state_custody_runtime_owner_slice": self.owner_slice,
        }

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": "state_custody_runtime",
            "ok": self.ok,
            "owner_slice": self.owner_slice,
            "observations": len(self.observations),
            "transitions": len(self.transitions),
            "active_runtime_bindings": self.active_runtime_bindings,
            "state_keys": list(self.observed_state_keys),
            "blocking_error": self.first_blocker_code,
            "blockers": [finding.to_dict() for finding in self.blockers],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "owner_slice": self.owner_slice,
            "observations": [row.to_dict() for row in self.observations],
            "transitions": [row.to_dict() for row in self.transitions],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


def build_state_custody_runtime_report(
    *,
    project_root: str | Path,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_contracts: ClaudeRuntimeContractBundle,
    runtime_context_report: RuntimeContextAssemblyReport | None = None,
) -> StateCustodyRuntimeReport:
    project_path = Path(project_root).resolve()
    requirements = default_state_custody_requirements(integration_report, runtime_contracts)
    observations = tuple(
        _observe_requirement(
            requirement,
            project_root=project_path,
            runtime_contracts=runtime_contracts,
            runtime_context_report=runtime_context_report,
        )
        for requirement in requirements
    )
    transitions = tuple(_transitions_from_observation(row) for row in observations)
    findings = tuple(_state_custody_findings(observations, transitions))
    return StateCustodyRuntimeReport(
        ok=not any(finding.blocking for finding in findings),
        checked_at=now_iso(),
        owner_slice=integration_report.crosswalk.owner_slice,
        observations=observations,
        transitions=transitions,
        findings=findings,
    )


def default_state_custody_requirements(
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_contracts: ClaudeRuntimeContractBundle,
) -> tuple[StateCustodyRequirement, ...]:
    requirements: list[StateCustodyRequirement] = []
    bundle_custody = dict(runtime_contracts.state_custody)
    for index, port in enumerate(integration_report.crosswalk.runtime_context_ports, start=1):
        expected_targets = _targets_for_state_key(port.kind, runtime_contracts)
        requirements.append(
            StateCustodyRequirement(
                requirement_id=f"state.port.{index:02d}.{port.kind}",
                state_key=str(port.kind),
                required_owner=port.state_owner or bundle_custody.get(str(port.kind), ""),
                required=port.required_for_runtime_shell,
                runtime_port_kind=port.kind,
                expected_events=port.event_phases,
                expected_targets=expected_targets,
                producer=port.producer,
                consumer=port.consumer,
                source=StateCustodySource.SOURCE_GRAPH_PORT,
                rationale="Every RuntimeContext port has a Zyra-owned state owner and default-path binding.",
            )
        )
    synthetic = _synthetic_state_requirements(bundle_custody, runtime_contracts)
    return tuple([*requirements, *synthetic])


def state_custody_runtime_event(request: WorkerRequest, report: StateCustodyRuntimeReport) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.CONSTRAINT_CHECK,
        payload={"claude_state_custody_runtime": report.event_payload()},
    )


def state_custody_runtime_markdown(report: StateCustodyRuntimeReport) -> str:
    lines = [
        "## State Custody Runtime",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- observations: `{len(report.observations)}`",
        f"- transitions: `{len(report.transitions)}`",
        f"- active_runtime_bindings: `{report.active_runtime_bindings}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Observations",
        "",
    ]
    for row in report.observations:
        lines.append(
            f"- `{row.state_key}` owner=`{row.observed_owner}` "
            f"binding=`{row.runtime_binding_status}` status=`{row.status}`"
        )
    if report.blockers:
        lines.extend(["", "### State Custody Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    if report.warnings:
        lines.extend(["", "### State Custody Warnings", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.warnings[:12])
    return "\n".join(lines) + "\n"


def assert_state_custody_runtime_ready(report: StateCustodyRuntimeReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"State custody runtime failed: {blockers}")


def _observe_requirement(
    requirement: StateCustodyRequirement,
    *,
    project_root: Path,
    runtime_contracts: ClaudeRuntimeContractBundle,
    runtime_context_report: RuntimeContextAssemblyReport | None,
) -> StateCustodyObservation:
    binding = _binding_for_kind(runtime_context_report, requirement.runtime_port_kind)
    bundle_owner = runtime_contracts.state_custody.get(requirement.state_key, "")
    observed_owner = _first_non_empty(
        binding.state_owner if binding is not None else "",
        bundle_owner,
        requirement.required_owner,
    )
    observed_events = tuple(str(event) for event in (binding.event_phases if binding is not None else ()))
    expected_events = tuple(str(event) for event in requirement.expected_events)
    existing_targets, missing_targets = _target_existence(project_root, requirement.expected_targets)
    active = bool(binding and binding.active_in_default_path)
    binding_status = str(binding.status) if binding is not None else "not_bound"
    risk = _observation_risk(
        requirement=requirement,
        observed_owner=observed_owner,
        active=active,
        binding_status=binding_status,
        observed_events=observed_events,
        expected_events=expected_events,
        missing_targets=missing_targets,
    )
    status = _status_for_risk(requirement.required, risk)
    return StateCustodyObservation(
        requirement_id=requirement.requirement_id,
        state_key=requirement.state_key,
        required_owner=requirement.required_owner,
        observed_owner=observed_owner,
        required=requirement.required,
        runtime_port_kind=requirement.runtime_port_kind,
        runtime_binding_id=binding.binding_id if binding is not None else "",
        runtime_binding_active=active,
        runtime_binding_status=binding_status,
        observed_events=observed_events,
        expected_events=expected_events,
        expected_targets=requirement.expected_targets,
        existing_targets=existing_targets,
        missing_targets=missing_targets,
        source=requirement.source,
        status=status,
        risk=risk,
        message=_message_for_risk(requirement, risk, observed_owner, binding_status),
    )


def _transitions_from_observation(row: StateCustodyObservation) -> StateCustodyTransition:
    event_phase = row.observed_events[0] if row.observed_events else (row.expected_events[0] if row.expected_events else "")
    payload_key = row.state_key.replace("-", "_").replace(".", "_")
    persistent_store = _persistent_store_for_state(row.state_key, row.observed_owner)
    status = StateCustodyStatus.PASSING
    rationale = "State can be reconstructed from RuntimeContext binding and event payload."
    if row.blocking:
        status = StateCustodyStatus.BLOCKED
        rationale = row.message
    elif not event_phase:
        status = StateCustodyStatus.WARNING
        rationale = "No event phase is attached to this state transition."
    return StateCustodyTransition(
        transition_id=f"transition.{row.requirement_id}",
        state_key=row.state_key,
        from_owner=row.observed_owner or row.required_owner,
        to_owner=row.observed_owner or row.required_owner,
        event_phase=event_phase,
        producer="RuntimeContextAssemblyReport",
        consumer="CodeWorkerRuntime",
        persistent_store=persistent_store,
        event_payload_key=payload_key,
        status=status,
        rationale=rationale,
    )


def _state_custody_findings(
    observations: Iterable[StateCustodyObservation],
    transitions: Iterable[StateCustodyTransition],
) -> list[StateCustodyFinding]:
    findings: list[StateCustodyFinding] = []
    for row in observations:
        if row.status == StateCustodyStatus.BLOCKED:
            findings.append(
                StateCustodyFinding(
                    severity=StateCustodySeverity.BLOCKER,
                    code=f"state_custody_{row.risk}",
                    message=row.message,
                    state_key=row.state_key,
                    requirement_id=row.requirement_id,
                )
            )
        elif row.status == StateCustodyStatus.WARNING:
            findings.append(
                StateCustodyFinding(
                    severity=StateCustodySeverity.WARNING,
                    code=f"state_custody_{row.risk}",
                    message=row.message,
                    state_key=row.state_key,
                    requirement_id=row.requirement_id,
                )
            )
    for transition in transitions:
        if transition.status == StateCustodyStatus.WARNING:
            findings.append(
                StateCustodyFinding(
                    severity=StateCustodySeverity.WARNING,
                    code="state_custody_transition_warning",
                    message=transition.rationale,
                    state_key=transition.state_key,
                    requirement_id=transition.transition_id,
                )
            )
    return findings


def _synthetic_state_requirements(
    bundle_custody: dict[str, str],
    runtime_contracts: ClaudeRuntimeContractBundle,
) -> tuple[StateCustodyRequirement, ...]:
    synthetic_keys = (
        ("tool_permission_requests", "JsonPermissionStore", RuntimePortKind.PERMISSION_MODE),
        ("tool_result_artifacts", "LocalArtifactStore", RuntimePortKind.ARTIFACT_STORE),
        ("query_session_snapshot", "QuerySessionSnapshot", RuntimePortKind.SESSION_LIFECYCLE),
        ("worker_result", "WorkerResult", RuntimePortKind.SESSION_LIFECYCLE),
        ("event_log", "EventRecord", RuntimePortKind.EVENT_SINK),
    )
    requirements: list[StateCustodyRequirement] = []
    for index, (state_key, default_owner, port_kind) in enumerate(synthetic_keys, start=1):
        requirements.append(
            StateCustodyRequirement(
                requirement_id=f"state.synthetic.{index:02d}.{state_key}",
                state_key=state_key,
                required_owner=bundle_custody.get(state_key, default_owner),
                required=True,
                runtime_port_kind=port_kind,
                expected_events=_synthetic_events_for_key(state_key),
                expected_targets=_targets_for_state_key(state_key, runtime_contracts),
                producer=bundle_custody.get(state_key, default_owner),
                consumer="CodeWorkerRuntime",
                source=StateCustodySource.SYNTHETIC_REQUIRED_STATE,
                rationale="Synthetic state is required by the Zyra runtime even when it is not a Claude RuntimeContext port.",
            )
        )
    return tuple(requirements)


def _synthetic_events_for_key(state_key: str) -> tuple[EventContractPhase, ...]:
    if state_key == "tool_permission_requests":
        return (EventContractPhase.PERMISSION_DECIDED,)
    if state_key == "tool_result_artifacts":
        return (EventContractPhase.TOOL_RESULT_RECORDED,)
    if state_key == "query_session_snapshot":
        return (EventContractPhase.SESSION_INIT, EventContractPhase.COMPACT_RESTORE_POINT)
    if state_key == "worker_result":
        return (EventContractPhase.TOOL_RESULT_RECORDED,)
    if state_key == "event_log":
        return (EventContractPhase.SOURCE_GRAPH_CROSSWALK_READY,)
    return ()


def _targets_for_state_key(
    state_key: RuntimePortKind | str,
    runtime_contracts: ClaudeRuntimeContractBundle,
) -> tuple[str, ...]:
    key = str(state_key)
    if key in {"session_lifecycle", "query_session_snapshot", "worker_result"}:
        return _paths_matching(runtime_contracts, "session")
    if key in {"tool_registry", "tool_executor", "tool_result_budget", "tool_permission_requests"}:
        return _paths_matching(runtime_contracts, "tool")
    if key in {"permission_mode"}:
        return _paths_matching(runtime_contracts, "permission")
    if key in {"workspace_cwd", "artifact_store", "tool_result_artifacts"}:
        return _paths_matching(runtime_contracts, "artifact")
    if key in {"event_log"}:
        return _paths_matching(runtime_contracts, "event")
    if key in {"source_graph", "state_custody"}:
        return (
            "packages/runtime/zyra_runtime/claude_source_graph_crosswalk.py",
            "packages/runtime/zyra_runtime/claude_state_custody_runtime.py",
        )
    return tuple(runtime_contracts.primary_runtime_paths[:2])


def _paths_matching(runtime_contracts: ClaudeRuntimeContractBundle, token: str) -> tuple[str, ...]:
    selected: list[str] = []
    for item in runtime_contracts.source_to_target:
        haystack = " ".join([str(item.surface), item.capability, *item.target_paths]).lower()
        if token in haystack:
            selected.extend(item.target_paths)
    return tuple(dict.fromkeys(selected))


def _binding_for_kind(runtime_context_report: RuntimeContextAssemblyReport | None, kind: RuntimePortKind | str) -> Any | None:
    if runtime_context_report is None:
        return None
    kind_value = str(kind)
    for binding in runtime_context_report.runtime_bindings:
        if str(binding.port_kind) == kind_value:
            return binding
    return None


def _observation_risk(
    *,
    requirement: StateCustodyRequirement,
    observed_owner: str,
    active: bool,
    binding_status: str,
    observed_events: tuple[str, ...],
    expected_events: tuple[str, ...],
    missing_targets: tuple[str, ...],
) -> StateCustodyRisk:
    if requirement.required and not observed_owner:
        return StateCustodyRisk.MISSING_OWNER
    if requirement.required and not active and requirement.source == StateCustodySource.SOURCE_GRAPH_PORT:
        return StateCustodyRisk.MISSING_RUNTIME_BINDING
    if requirement.required and binding_status == "blocked":
        return StateCustodyRisk.MISSING_RUNTIME_BINDING
    if requirement.required and missing_targets and not all(_target_is_future_handoff(path) for path in missing_targets):
        return StateCustodyRisk.MISSING_REQUIRED_TARGET
    if any(_path_contains_source_pool(path) for path in requirement.expected_targets):
        return StateCustodyRisk.SOURCE_POOL_PATH
    if requirement.required and expected_events and not set(expected_events).intersection(observed_events or expected_events):
        return StateCustodyRisk.EVENT_PHASE_GAP
    if not active and requirement.required:
        return StateCustodyRisk.OBSERVABILITY_GAP
    return StateCustodyRisk.NONE


def _status_for_risk(required: bool, risk: StateCustodyRisk) -> StateCustodyStatus:
    if risk == StateCustodyRisk.NONE:
        return StateCustodyStatus.PASSING
    if required and risk in {
        StateCustodyRisk.MISSING_OWNER,
        StateCustodyRisk.MISSING_RUNTIME_BINDING,
        StateCustodyRisk.MISSING_REQUIRED_TARGET,
        StateCustodyRisk.SOURCE_POOL_PATH,
    }:
        return StateCustodyStatus.BLOCKED
    return StateCustodyStatus.WARNING


def _message_for_risk(
    requirement: StateCustodyRequirement,
    risk: StateCustodyRisk,
    observed_owner: str,
    binding_status: str,
) -> str:
    if risk == StateCustodyRisk.NONE:
        return f"State {requirement.state_key} is owned by {observed_owner or requirement.required_owner}."
    if risk == StateCustodyRisk.MISSING_OWNER:
        return f"State {requirement.state_key} has no Zyra-owned custody owner."
    if risk == StateCustodyRisk.MISSING_RUNTIME_BINDING:
        return f"State {requirement.state_key} has runtime binding status {binding_status}."
    if risk == StateCustodyRisk.MISSING_REQUIRED_TARGET:
        return f"State {requirement.state_key} depends on missing required target paths."
    if risk == StateCustodyRisk.SOURCE_POOL_PATH:
        return f"State {requirement.state_key} points at vendor/source-pool-like targets."
    if risk == StateCustodyRisk.EVENT_PHASE_GAP:
        return f"State {requirement.state_key} does not expose the expected event phase."
    return f"State {requirement.state_key} has an observability gap."


def _target_existence(project_root: Path, targets: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    existing: list[str] = []
    missing: list[str] = []
    for target in targets:
        if not target:
            continue
        if (project_root / target).exists():
            existing.append(target)
        else:
            missing.append(target)
    return tuple(existing), tuple(missing)


def _target_is_future_handoff(path: str) -> bool:
    normalized = path.replace("\\", "/")
    future_tokens = (
        "claude_permission",
        "claude_mcp",
        "claude_skill",
        "claude_agent",
        "claude_scheduler",
        "claude_recovery",
    )
    return any(token in normalized for token in future_tokens)


def _path_contains_source_pool(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return any(
        marker in normalized
        for marker in (
            "vendor/",
            "vendor-runtimes/",
            "source-pool/",
            "runtime-sources/",
            "third_party/",
            "/productized/",
        )
    )


def _persistent_store_for_state(state_key: str, owner: str) -> str:
    lowered = f"{state_key} {owner}".lower()
    if "permission" in lowered:
        return "JsonPermissionStore"
    if "artifact" in lowered or "tool_result" in lowered:
        return "LocalArtifactStore"
    if "event" in lowered:
        return "EventRecord"
    if "session" in lowered or "worker_result" in lowered:
        return "QuerySessionSnapshot"
    return owner or "RuntimeContextAssemblyReport"


def _first_non_empty(*values: str) -> str:
    for value in values:
        if value:
            return str(value)
    return ""
