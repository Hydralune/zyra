from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


M1_02D_TASK_API_CONTRACT_OWNER_UNIT = "M1-02D"
CODEWORKER_TASK_API_CONTRACT_RUNTIME_ID = "codeworker_task_api_contract_runtime"


class TaskApiRouteKind(StrEnum):
    POST_CODE_WORKER = "post_code_worker"
    SESSION = "session"
    TOOL_TRACE = "tool_trace"
    COMPACT_STATE = "compact_state"


class TaskApiContractStatus(StrEnum):
    READY = "ready"
    EMPTY = "empty"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    DISABLED = "disabled"


class TaskApiContractSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class TaskApiContractSurface(StrEnum):
    ROUTE = "route"
    FIELD = "field"
    PHASE = "phase"
    TASK_SCOPE = "task_scope"
    PROJECTION = "projection"
    EVENT_LOG = "event_log"


class TaskApiFieldType(StrEnum):
    ANY = "any"
    MAPPING = "mapping"
    SEQUENCE = "sequence"
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    NUMBER = "number"


@dataclass(frozen=True, slots=True)
class TaskApiFieldRequirement:
    requirement_id: str
    path: str
    field_type: TaskApiFieldType = TaskApiFieldType.ANY
    required: bool = True
    non_empty: bool = False
    truthy: bool = False
    min_items: int = 0
    description: str = ""
    severity: TaskApiContractSeverity = TaskApiContractSeverity.ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "path": self.path,
            "field_type": str(self.field_type),
            "required": self.required,
            "non_empty": self.non_empty,
            "truthy": self.truthy,
            "min_items": self.min_items,
            "description": self.description,
            "severity": str(self.severity),
        }


@dataclass(frozen=True, slots=True)
class TaskApiPhaseRequirement:
    requirement_id: str
    phase: str
    min_count: int = 1
    description: str = ""
    severity: TaskApiContractSeverity = TaskApiContractSeverity.ERROR

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "phase": self.phase,
            "min_count": self.min_count,
            "description": self.description,
            "severity": str(self.severity),
        }


@dataclass(frozen=True, slots=True)
class TaskApiRouteContract:
    route_kind: TaskApiRouteKind
    method: str
    path_template: str
    required_fields: tuple[TaskApiFieldRequirement, ...]
    required_phases: tuple[TaskApiPhaseRequirement, ...]
    source_decisions: tuple[dict[str, str], ...] = ()
    description: str = ""

    @property
    def contract_id(self) -> str:
        return f"{self.method} {self.path_template}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_kind": str(self.route_kind),
            "method": self.method,
            "path_template": self.path_template,
            "contract_id": self.contract_id,
            "description": self.description,
            "required_fields": [requirement.to_dict() for requirement in self.required_fields],
            "required_phases": [requirement.to_dict() for requirement in self.required_phases],
            "source_decisions": [dict(item) for item in self.source_decisions],
        }


@dataclass(frozen=True, slots=True)
class TaskApiFieldObservation:
    requirement_id: str
    path: str
    present: bool
    ok: bool
    field_type: TaskApiFieldType
    observed_type: str
    observed_size: int = 0
    value_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "path": self.path,
            "present": self.present,
            "ok": self.ok,
            "field_type": str(self.field_type),
            "observed_type": self.observed_type,
            "observed_size": self.observed_size,
            "value_summary": self.value_summary,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TaskApiPhaseObservation:
    requirement_id: str
    phase: str
    observed_count: int
    min_count: int
    ok: bool
    event_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "phase": self.phase,
            "observed_count": self.observed_count,
            "min_count": self.min_count,
            "ok": self.ok,
            "event_ids": list(self.event_ids),
        }


@dataclass(frozen=True, slots=True)
class TaskApiScopeObservation:
    task_id: str
    payload_task_ids: tuple[str, ...]
    event_task_ids: tuple[str, ...]
    ok: bool
    sample_scope_detected: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "payload_task_ids": list(self.payload_task_ids),
            "event_task_ids": list(self.event_task_ids),
            "ok": self.ok,
            "sample_scope_detected": self.sample_scope_detected,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TaskApiContractFinding:
    code: str
    severity: TaskApiContractSeverity
    surface: TaskApiContractSurface
    message: str
    requirement_id: str = ""
    path: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == TaskApiContractSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "requirement_id": self.requirement_id,
            "path": self.path,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class TaskApiRouteContractReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    route_kind: TaskApiRouteKind
    method: str
    path_template: str
    task_id: str
    contract: TaskApiRouteContract
    field_observations: tuple[TaskApiFieldObservation, ...]
    phase_observations: tuple[TaskApiPhaseObservation, ...]
    scope_observation: TaskApiScopeObservation
    findings: tuple[TaskApiContractFinding, ...]
    source_decisions: tuple[dict[str, str], ...]
    disabled: bool = False
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not self.disabled and not any(finding.blocking for finding in self.findings)

    @property
    def status(self) -> TaskApiContractStatus:
        if self.disabled:
            return TaskApiContractStatus.DISABLED
        if any(finding.blocking for finding in self.findings):
            return TaskApiContractStatus.BLOCKED
        if not self.field_observations and not self.phase_observations:
            return TaskApiContractStatus.EMPTY
        if self.findings:
            return TaskApiContractStatus.DEGRADED
        return TaskApiContractStatus.READY

    @property
    def field_ok_count(self) -> int:
        return sum(1 for observation in self.field_observations if observation.ok)

    @property
    def phase_ok_count(self) -> int:
        return sum(1 for observation in self.phase_observations if observation.ok)

    @property
    def blocking_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def missing_field_paths(self) -> tuple[str, ...]:
        return tuple(observation.path for observation in self.field_observations if not observation.ok)

    @property
    def missing_phase_names(self) -> tuple[str, ...]:
        return tuple(observation.phase for observation in self.phase_observations if not observation.ok)

    @property
    def route_signature(self) -> str:
        return f"{self.method} {self.path_template}"

    def summary_lines(self) -> tuple[str, ...]:
        lines = [
            f"route={self.route_signature} status={self.status} ok={str(self.ok).lower()}",
            f"fields_ok={self.field_ok_count}/{len(self.field_observations)} phases_ok={self.phase_ok_count}/{len(self.phase_observations)} scope_ok={str(self.scope_observation.ok).lower()}",
        ]
        if self.missing_field_paths:
            lines.append(f"missing_fields={','.join(self.missing_field_paths)}")
        if self.missing_phase_names:
            lines.append(f"missing_phases={','.join(self.missing_phase_names)}")
        if self.scope_observation.sample_scope_detected:
            lines.append("sample_scope_detected=true")
        return tuple(lines)

    def api_summary(self) -> dict[str, Any]:
        return {
            "route_signature": self.route_signature,
            "route_kind": str(self.route_kind),
            "status": str(self.status),
            "ok": self.ok,
            "fields_ok": self.field_ok_count,
            "fields_total": len(self.field_observations),
            "phases_ok": self.phase_ok_count,
            "phases_total": len(self.phase_observations),
            "scope_ok": self.scope_observation.ok,
            "missing_field_paths": list(self.missing_field_paths),
            "missing_phase_names": list(self.missing_phase_names),
            "summary": list(self.summary_lines()),
        }

    def metadata(self) -> dict[str, str]:
        return {
            "task_api_contract_report_id": self.report_id,
            "task_api_contract_owner_unit": self.owner_unit,
            "task_api_contract_runtime_id": self.runtime_id,
            "task_api_contract_route_kind": str(self.route_kind),
            "task_api_contract_method": self.method,
            "task_api_contract_path_template": self.path_template,
            "task_api_contract_ok": str(self.ok).lower(),
            "task_api_contract_status": str(self.status),
            "task_api_contract_disabled": str(self.disabled).lower(),
            "task_api_contract_fields": str(len(self.field_observations)),
            "task_api_contract_fields_ok": str(self.field_ok_count),
            "task_api_contract_phases": str(len(self.phase_observations)),
            "task_api_contract_phases_ok": str(self.phase_ok_count),
            "task_api_contract_scope_ok": str(self.scope_observation.ok).lower(),
            "task_api_contract_sample_scope_detected": str(self.scope_observation.sample_scope_detected).lower(),
            "task_api_contract_blocking_count": str(self.blocking_count),
            "task_api_contract_finding_count": str(len(self.findings)),
            "task_api_contract_missing_fields": ",".join(self.missing_field_paths),
            "task_api_contract_missing_phases": ",".join(self.missing_phase_names),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.task_codeworker_api_contract.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "route_kind": str(self.route_kind),
            "method": self.method,
            "path_template": self.path_template,
            "task_id": self.task_id,
            "ok": self.ok,
            "status": str(self.status),
            "disabled": self.disabled,
            "contract": self.contract.to_dict(),
            "field_observations": [observation.to_dict() for observation in self.field_observations],
            "phase_observations": [observation.to_dict() for observation in self.phase_observations],
            "scope_observation": self.scope_observation.to_dict(),
            "findings": [finding.to_dict() for finding in self.findings],
            "api_summary": self.api_summary(),
            "summary": list(self.summary_lines()),
            "contract_inventory": task_api_contract_inventory(compact=True),
            "source_decisions": [dict(item) for item in self.source_decisions],
            "metadata": self.metadata(),
            "created_at": self.created_at,
        }


class CodeWorkerTaskApiContractRuntime:
    def __init__(
        self,
        *,
        owner_unit: str = M1_02D_TASK_API_CONTRACT_OWNER_UNIT,
        runtime_id: str = CODEWORKER_TASK_API_CONTRACT_RUNTIME_ID,
        disabled: bool = False,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id
        self.disabled = disabled

    def build_report(
        self,
        *,
        route_kind: TaskApiRouteKind | str,
        task_id: str,
        payload: Mapping[str, Any],
        events: Iterable[Any] = (),
        projection: Any = None,
    ) -> TaskApiRouteContractReport:
        kind = TaskApiRouteKind(str(route_kind))
        contract = default_task_api_route_contract(kind)
        payload_map = dict(payload)
        projection_map = _projection_map(projection)
        event_list = [_event_view(event) for event in events]
        phase_index = _phase_index(event_list, projection_map)
        field_observations = tuple(
            self._observe_field(requirement, payload_map, projection_map)
            for requirement in contract.required_fields
        )
        phase_observations = tuple(
            self._observe_phase(requirement, phase_index)
            for requirement in contract.required_phases
        )
        scope_observation = _scope_observation(task_id, payload_map, projection_map, event_list)
        findings = [
            *self._field_findings(contract.required_fields, field_observations),
            *self._phase_findings(contract.required_phases, phase_observations),
            *self._scope_findings(scope_observation),
        ]
        if self.disabled:
            findings.append(
                TaskApiContractFinding(
                    code="TASK_API_CONTRACT_RUNTIME_DISABLED",
                    severity=TaskApiContractSeverity.BLOCKER,
                    surface=TaskApiContractSurface.ROUTE,
                    message="Task API contract runtime is disabled.",
                )
            )
        return TaskApiRouteContractReport(
            report_id=new_id("task_api_contract"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            route_kind=kind,
            method=contract.method,
            path_template=contract.path_template,
            task_id=task_id,
            contract=contract,
            field_observations=field_observations,
            phase_observations=phase_observations,
            scope_observation=scope_observation,
            findings=tuple(findings),
            source_decisions=default_task_api_contract_source_decisions(),
            disabled=self.disabled,
        )

    def event_for_report(
        self,
        report: TaskApiRouteContractReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        phase: str = "codeworker_task_api_contract",
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": "",
                    "worker_request_id": "",
                    "phase": phase,
                    "task_api_contract": report.to_dict(),
                }
            },
        )

    def metadata(self, report: TaskApiRouteContractReport | None = None) -> dict[str, str]:
        if report is None:
            return {
                "task_api_contract_ok": str(not self.disabled).lower(),
                "task_api_contract_status": str(
                    TaskApiContractStatus.DISABLED if self.disabled else TaskApiContractStatus.READY
                ),
                "task_api_contract_runtime_id": self.runtime_id,
            }
        return report.metadata()

    def _observe_field(
        self,
        requirement: TaskApiFieldRequirement,
        payload: Mapping[str, Any],
        projection: Mapping[str, Any],
    ) -> TaskApiFieldObservation:
        root = {"payload": payload, "projection": projection}
        present, value = _read_path(root, requirement.path)
        observed_type = _type_name(value)
        observed_size = _size(value)
        type_ok = requirement.field_type == TaskApiFieldType.ANY or _type_matches(value, requirement.field_type)
        non_empty_ok = not requirement.non_empty or observed_size > 0 or bool(value)
        min_items_ok = requirement.min_items <= 0 or observed_size >= requirement.min_items
        truthy_ok = not requirement.truthy or _truthy(value)
        present_ok = present or not requirement.required
        ok = present_ok and type_ok and non_empty_ok and min_items_ok and truthy_ok
        return TaskApiFieldObservation(
            requirement_id=requirement.requirement_id,
            path=requirement.path,
            present=present,
            ok=ok,
            field_type=requirement.field_type,
            observed_type=observed_type,
            observed_size=observed_size,
            value_summary=_summarize_value(value),
            metadata={
                "type_ok": type_ok,
                "non_empty_ok": non_empty_ok,
                "min_items_ok": min_items_ok,
                "truthy_ok": truthy_ok,
                "present_ok": present_ok,
            },
        )

    def _observe_phase(
        self,
        requirement: TaskApiPhaseRequirement,
        phase_index: Mapping[str, Sequence[str]],
    ) -> TaskApiPhaseObservation:
        event_ids = tuple(str(item) for item in phase_index.get(requirement.phase, ()))
        observed_count = len(event_ids)
        return TaskApiPhaseObservation(
            requirement_id=requirement.requirement_id,
            phase=requirement.phase,
            observed_count=observed_count,
            min_count=requirement.min_count,
            ok=observed_count >= requirement.min_count,
            event_ids=event_ids,
        )

    def _field_findings(
        self,
        requirements: Sequence[TaskApiFieldRequirement],
        observations: Sequence[TaskApiFieldObservation],
    ) -> tuple[TaskApiContractFinding, ...]:
        by_id = {observation.requirement_id: observation for observation in observations}
        findings: list[TaskApiContractFinding] = []
        for requirement in requirements:
            observation = by_id.get(requirement.requirement_id)
            if observation is None or observation.ok:
                continue
            findings.append(
                TaskApiContractFinding(
                    code="TASK_API_FIELD_CONTRACT_FAILED",
                    severity=requirement.severity,
                    surface=TaskApiContractSurface.FIELD,
                    message=requirement.description or f"Route field contract failed for {requirement.path}.",
                    requirement_id=requirement.requirement_id,
                    path=requirement.path,
                    metadata={
                        "observed_type": observation.observed_type,
                        "observed_size": str(observation.observed_size),
                        "present": str(observation.present).lower(),
                    },
                )
            )
        return tuple(findings)

    def _phase_findings(
        self,
        requirements: Sequence[TaskApiPhaseRequirement],
        observations: Sequence[TaskApiPhaseObservation],
    ) -> tuple[TaskApiContractFinding, ...]:
        by_id = {observation.requirement_id: observation for observation in observations}
        findings: list[TaskApiContractFinding] = []
        for requirement in requirements:
            observation = by_id.get(requirement.requirement_id)
            if observation is None or observation.ok:
                continue
            findings.append(
                TaskApiContractFinding(
                    code="TASK_API_PHASE_CONTRACT_FAILED",
                    severity=requirement.severity,
                    surface=TaskApiContractSurface.PHASE,
                    message=requirement.description or f"Route phase contract failed for {requirement.phase}.",
                    requirement_id=requirement.requirement_id,
                    path=requirement.phase,
                    metadata={
                        "observed_count": str(observation.observed_count),
                        "min_count": str(observation.min_count),
                    },
                )
            )
        return tuple(findings)

    def _scope_findings(self, scope: TaskApiScopeObservation) -> tuple[TaskApiContractFinding, ...]:
        findings: list[TaskApiContractFinding] = []
        if not scope.ok:
            findings.append(
                TaskApiContractFinding(
                    code="TASK_API_SCOPE_MISMATCH",
                    severity=TaskApiContractSeverity.BLOCKER,
                    surface=TaskApiContractSurface.TASK_SCOPE,
                    message="Task-scoped CodeWorker API returned payload or events outside the requested task.",
                    metadata={
                        "task_id": scope.task_id,
                        "payload_task_ids": ",".join(scope.payload_task_ids),
                        "event_task_ids": ",".join(scope.event_task_ids),
                    },
                )
            )
        if scope.sample_scope_detected:
            findings.append(
                TaskApiContractFinding(
                    code="TASK_API_SAMPLE_SCOPE_DETECTED",
                    severity=TaskApiContractSeverity.BLOCKER,
                    surface=TaskApiContractSurface.TASK_SCOPE,
                    message="Task-scoped CodeWorker API returned a sample or inventory task instead of the requested task.",
                    metadata={"task_id": scope.task_id},
                )
            )
        return tuple(findings)


def task_api_contract_metadata(report: TaskApiRouteContractReport | None) -> dict[str, str]:
    if report is None:
        return {
            "task_api_contract_ok": "false",
            "task_api_contract_status": "missing",
            "task_api_contract_report_id": "",
        }
    return report.metadata()


def default_task_api_route_contract(route_kind: TaskApiRouteKind | str) -> TaskApiRouteContract:
    kind = TaskApiRouteKind(str(route_kind))
    contracts = _route_contracts()
    return contracts[kind]


def default_task_api_contract_source_decisions() -> tuple[dict[str, str], ...]:
    return (
        {
            "source_repo": "claude-code-best",
            "source_path": "src/query.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_task_api_contract.py",
            "decision": "zyra_module_migrated",
            "capability": "session-scoped CodeWorker state must be visible through task API after query loop execution",
        },
        {
            "source_repo": "claude-code-best",
            "source_path": "src/services/compact/compact.ts",
            "target_path": "packages/runtime/zyra_runtime/codeworker_task_api_contract.py",
            "decision": "zyra_module_migrated",
            "capability": "compact and restore state are contractually required on task CodeWorker routes",
        },
        {
            "source_repo": "opencode",
            "source_path": "packages/opencode/src/session/**",
            "target_path": "packages/runtime/zyra_runtime/codeworker_task_api_contract.py",
            "decision": "adapter_encapsulated",
            "capability": "task event log phases define route-level live projection evidence",
        },
    )


def task_api_contract_inventory(*, compact: bool = False) -> dict[str, Any]:
    contracts = _route_contracts()
    route_items: list[dict[str, Any]] = []
    for kind in (
        TaskApiRouteKind.POST_CODE_WORKER,
        TaskApiRouteKind.SESSION,
        TaskApiRouteKind.TOOL_TRACE,
        TaskApiRouteKind.COMPACT_STATE,
    ):
        contract = contracts[kind]
        route_items.append(
            {
                "route_kind": str(kind),
                "method": contract.method,
                "path_template": contract.path_template,
                "field_count": len(contract.required_fields),
                "phase_count": len(contract.required_phases),
                "required_fields": []
                if compact
                else [requirement.to_dict() for requirement in contract.required_fields],
                "required_phases": []
                if compact
                else [requirement.to_dict() for requirement in contract.required_phases],
                "description": "" if compact else contract.description,
            }
        )
    return {
        "schema": "zyra.task_codeworker_api_contract_inventory.v1",
        "owner_unit": M1_02D_TASK_API_CONTRACT_OWNER_UNIT,
        "runtime_id": CODEWORKER_TASK_API_CONTRACT_RUNTIME_ID,
        "route_count": len(route_items),
        "routes": route_items,
        "source_decisions": [] if compact else [dict(item) for item in default_task_api_contract_source_decisions()],
    }


def task_api_contract_inventory_metadata() -> dict[str, str]:
    inventory = task_api_contract_inventory(compact=True)
    field_count = sum(_safe_int(route.get("field_count")) for route in inventory["routes"])
    phase_count = sum(_safe_int(route.get("phase_count")) for route in inventory["routes"])
    return {
        "task_api_contract_inventory_owner_unit": M1_02D_TASK_API_CONTRACT_OWNER_UNIT,
        "task_api_contract_inventory_runtime_id": CODEWORKER_TASK_API_CONTRACT_RUNTIME_ID,
        "task_api_contract_inventory_routes": str(inventory["route_count"]),
        "task_api_contract_inventory_fields": str(field_count),
        "task_api_contract_inventory_phases": str(phase_count),
    }


def _route_contracts() -> dict[TaskApiRouteKind, TaskApiRouteContract]:
    return {
        TaskApiRouteKind.POST_CODE_WORKER: TaskApiRouteContract(
            route_kind=TaskApiRouteKind.POST_CODE_WORKER,
            method="POST",
            path_template="/tasks/{task_id}/workers/code",
            description="Run CodeWorker for one task and return live session, compact, restore and trace projections.",
            required_fields=(
                _field("post_task_id", "payload.task.task_id", TaskApiFieldType.STRING, non_empty=True),
                _field("post_worker_result", "payload.worker_result", TaskApiFieldType.MAPPING, non_empty=True),
                _field("post_codeworker_session", "payload.codeworker_session", TaskApiFieldType.MAPPING, non_empty=True),
                _field(
                    "post_session_summary",
                    "payload.codeworker_session.session",
                    TaskApiFieldType.MAPPING,
                    non_empty=True,
                ),
                _field(
                    "post_compact_state",
                    "payload.codeworker_session.compact_state",
                    TaskApiFieldType.MAPPING,
                    non_empty=True,
                ),
                _field(
                    "post_restore_state",
                    "payload.codeworker_session.restore_state",
                    TaskApiFieldType.MAPPING,
                    non_empty=True,
                ),
                _field(
                    "post_model_api",
                    "payload.codeworker_session.model_api",
                    TaskApiFieldType.MAPPING,
                    non_empty=True,
                ),
                _field("post_tool_trace", "payload.tool_trace", TaskApiFieldType.MAPPING, non_empty=True),
                _field("post_events", "payload.events", TaskApiFieldType.SEQUENCE, min_items=1),
            ),
            required_phases=(
                _phase("post_model_stream", "model_stream_report"),
                _phase("post_compact_restore", "compact_restore_report"),
                _phase("post_restore_integration", "codeworker_restore_integration"),
                _phase("post_task_projection", "codeworker_task_api_projection", severity=TaskApiContractSeverity.WARNING),
            ),
        ),
        TaskApiRouteKind.SESSION: TaskApiRouteContract(
            route_kind=TaskApiRouteKind.SESSION,
            method="GET",
            path_template="/tasks/{task_id}/workers/code/session",
            description="Return task-scoped CodeWorker session projection from checkpoint and task events.",
            required_fields=(
                _field("session_task_id", "payload.task_id", TaskApiFieldType.STRING, non_empty=True),
                _field("session_summary", "payload.session", TaskApiFieldType.MAPPING, non_empty=True),
                _field("session_id", "payload.session.session_id", TaskApiFieldType.STRING, non_empty=True),
                _field("session_compact_state", "payload.compact_state", TaskApiFieldType.MAPPING, non_empty=True),
                _field("session_restore_state", "payload.restore_state", TaskApiFieldType.MAPPING, non_empty=True),
                _field("session_model_api", "payload.model_api", TaskApiFieldType.MAPPING, non_empty=True),
                _field("session_tool_trace", "payload.tool_trace", TaskApiFieldType.MAPPING, non_empty=True),
                _field("session_phase_counts", "payload.phase_counts", TaskApiFieldType.MAPPING, non_empty=True),
                _field("session_observations", "payload.observations", TaskApiFieldType.SEQUENCE, min_items=1),
            ),
            required_phases=(
                _phase("session_model_stream", "model_stream_report"),
                _phase("session_compact_restore", "compact_restore_report"),
                _phase("session_restore_integration", "codeworker_restore_integration"),
            ),
        ),
        TaskApiRouteKind.TOOL_TRACE: TaskApiRouteContract(
            route_kind=TaskApiRouteKind.TOOL_TRACE,
            method="GET",
            path_template="/tasks/{task_id}/workers/code/tool-trace",
            description="Return chronological model, compact, restore and tool trace for one task.",
            required_fields=(
                _field("trace_task_id", "payload.task_id", TaskApiFieldType.STRING, non_empty=True),
                _field("trace_items", "payload.items", TaskApiFieldType.SEQUENCE, min_items=1),
                _field("trace_tool_call_count", "payload.tool_call_count", TaskApiFieldType.INTEGER),
                _field("trace_model_stream_count", "payload.model_stream_count", TaskApiFieldType.INTEGER),
                _field("trace_compact_event_count", "payload.compact_event_count", TaskApiFieldType.INTEGER),
                _field("trace_restore_event_count", "payload.restore_event_count", TaskApiFieldType.INTEGER),
                _field("trace_repair", "payload.repair", TaskApiFieldType.MAPPING, non_empty=True),
            ),
            required_phases=(
                _phase("trace_model_stream", "model_stream_report"),
                _phase("trace_compact_restore", "compact_restore_report"),
                _phase("trace_restore_applied", "codeworker_restore_context_applied"),
            ),
        ),
        TaskApiRouteKind.COMPACT_STATE: TaskApiRouteContract(
            route_kind=TaskApiRouteKind.COMPACT_STATE,
            method="GET",
            path_template="/tasks/{task_id}/workers/code/compact-state",
            description="Return compact/restore/model API state and the full task projection for one task.",
            required_fields=(
                _field("compact_task_id", "payload.task_id", TaskApiFieldType.STRING, non_empty=True),
                _field("compact_session", "payload.session", TaskApiFieldType.MAPPING, non_empty=True),
                _field("compact_state", "payload.compact_state", TaskApiFieldType.MAPPING, non_empty=True),
                _field("compact_restore_state", "payload.restore_state", TaskApiFieldType.MAPPING, non_empty=True),
                _field("compact_model_api", "payload.model_api", TaskApiFieldType.MAPPING, non_empty=True),
                _field("compact_phase_counts", "payload.phase_counts", TaskApiFieldType.MAPPING, non_empty=True),
                _field("compact_projection", "payload.projection", TaskApiFieldType.MAPPING, non_empty=True),
                _field(
                    "compact_restore_contract_id",
                    "payload.compact_state.restore_contract_id",
                    TaskApiFieldType.STRING,
                    non_empty=True,
                    severity=TaskApiContractSeverity.BLOCKER,
                ),
            ),
            required_phases=(
                _phase("compact_model_stream", "model_stream_report"),
                _phase("compact_restore_report", "compact_restore_report"),
                _phase("compact_restore_integration", "codeworker_restore_integration"),
                _phase("compact_state_projection", "compact_state_projection"),
            ),
        ),
    }


def _field(
    requirement_id: str,
    path: str,
    field_type: TaskApiFieldType,
    *,
    required: bool = True,
    non_empty: bool = False,
    truthy: bool = False,
    min_items: int = 0,
    description: str = "",
    severity: TaskApiContractSeverity = TaskApiContractSeverity.ERROR,
) -> TaskApiFieldRequirement:
    return TaskApiFieldRequirement(
        requirement_id=requirement_id,
        path=path,
        field_type=field_type,
        required=required,
        non_empty=non_empty,
        truthy=truthy,
        min_items=min_items,
        description=description,
        severity=severity,
    )


def _phase(
    requirement_id: str,
    phase: str,
    *,
    min_count: int = 1,
    description: str = "",
    severity: TaskApiContractSeverity = TaskApiContractSeverity.ERROR,
) -> TaskApiPhaseRequirement:
    return TaskApiPhaseRequirement(
        requirement_id=requirement_id,
        phase=phase,
        min_count=min_count,
        description=description,
        severity=severity,
    )


def _projection_map(projection: Any) -> Mapping[str, Any]:
    if projection is None:
        return {}
    if isinstance(projection, Mapping):
        return projection
    if hasattr(projection, "to_dict"):
        data = projection.to_dict()
        return data if isinstance(data, Mapping) else {}
    data = to_jsonable(projection)
    return data if isinstance(data, Mapping) else {}


def _event_view(event: Any) -> dict[str, Any]:
    if isinstance(event, EventRecord):
        return to_jsonable(event)
    if isinstance(event, Mapping):
        return dict(event)
    data = to_jsonable(event)
    return data if isinstance(data, dict) else {}


def _phase_index(
    events: Sequence[Mapping[str, Any]],
    projection: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    phase_events: dict[str, list[str]] = {}
    for event in events:
        event_id = str(event.get("event_id") or "")
        query_session = _as_mapping(_as_mapping(event.get("payload")).get("query_session"))
        phase = str(query_session.get("phase") or "")
        if not phase:
            continue
        phase_events.setdefault(phase, []).append(event_id)
    phase_counts = _as_mapping(projection.get("phase_counts"))
    for phase, count in phase_counts.items():
        phase_key = str(phase)
        if phase_key not in phase_events:
            phase_events[phase_key] = [f"projection_count:{index}" for index in range(_safe_int(count))]
    return {phase: tuple(ids) for phase, ids in phase_events.items()}


def _scope_observation(
    task_id: str,
    payload: Mapping[str, Any],
    projection: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> TaskApiScopeObservation:
    payload_ids = set()
    for path in (
        "task_id",
        "task.task_id",
        "codeworker_session.task_id",
        "projection.task_id",
        "session.task_id",
        "tool_trace.task_id",
    ):
        present, value = _read_path(payload, path)
        if present and value:
            payload_ids.add(str(value))
    if projection:
        present, value = _read_path(projection, "task_id")
        if present and value:
            payload_ids.add(str(value))
    event_ids = {str(event.get("task_id") or "") for event in events if event.get("task_id")}
    payload_ids.discard("")
    event_ids.discard("")
    sample_scope_detected = any(_looks_like_sample_task(value) for value in payload_ids | event_ids)
    mismatched_payload = [value for value in payload_ids if task_id and value != task_id]
    mismatched_events = [value for value in event_ids if task_id and value != task_id]
    return TaskApiScopeObservation(
        task_id=task_id,
        payload_task_ids=tuple(sorted(payload_ids)),
        event_task_ids=tuple(sorted(event_ids)),
        ok=not mismatched_payload and not mismatched_events and not sample_scope_detected,
        sample_scope_detected=sample_scope_detected,
        metadata={
            "mismatched_payload_task_ids": sorted(mismatched_payload),
            "mismatched_event_task_ids": sorted(mismatched_events),
        },
    )


def _read_path(root: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    current: Any = root
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return False, None
            current = current.get(part)
            continue
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            if part == "*":
                return bool(current), current
            try:
                index = int(part)
            except ValueError:
                return False, None
            if index < 0 or index >= len(current):
                return False, None
            current = current[index]
            continue
        return False, None
    return True, current


def _type_matches(value: Any, field_type: TaskApiFieldType) -> bool:
    if field_type == TaskApiFieldType.ANY:
        return True
    if field_type == TaskApiFieldType.MAPPING:
        return isinstance(value, Mapping)
    if field_type == TaskApiFieldType.SEQUENCE:
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    if field_type == TaskApiFieldType.STRING:
        return isinstance(value, str)
    if field_type == TaskApiFieldType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == TaskApiFieldType.BOOLEAN:
        return isinstance(value, bool)
    if field_type == TaskApiFieldType.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _type_name(value: Any) -> str:
    if isinstance(value, Mapping):
        return "mapping"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "sequence"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "none"
    return type(value).__name__


def _size(value: Any) -> int:
    if isinstance(value, Mapping):
        return len(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return len(value)
    if isinstance(value, str):
        return len(value)
    return 1 if value is not None else 0


def _summarize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        keys = ",".join(str(key) for key in list(value.keys())[:8])
        return f"mapping[{len(value)}]:{keys}"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return f"sequence[{len(value)}]"
    text = str(value)
    return text[:160]


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _looks_like_sample_task(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("sample") or "inventory" in lowered or "compact-state" in lowered
