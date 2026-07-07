from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

from zyra_core import EventRecord, EventType, now_iso

from .claude_runtime_context_ports import RuntimeContextAssemblyReport
from .claude_runtime_contracts import ClaudeRuntimeContractBundle
from .claude_source_graph_crosswalk import ClaudeProductizationIntegrationReport
from .workers import WorkerRequest


class ApiInventoryContractStatus(StrEnum):
    PASSING = "passing"
    WARNING = "warning"
    BLOCKED = "blocked"


class ApiInventoryContractSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class ApiInventoryContractRisk(StrEnum):
    NONE = "none"
    ROUTE_TARGET_MISSING = "route_target_missing"
    PAYLOAD_FIELD_MISSING = "payload_field_missing"
    PAYLOAD_FIELD_EMPTY = "payload_field_empty"
    RUNTIME_CONTEXT_NOT_BOUND = "runtime_context_not_bound"
    SOURCE_GRAPH_NOT_READY = "source_graph_not_ready"
    DOWNSTREAM_CONTRACT_GAP = "downstream_contract_gap"
    EVENT_CONTRACT_GAP = "event_contract_gap"
    VENDOR_FIELD_EXPOSED = "vendor_field_exposed"


class ApiInventoryPayloadKind(StrEnum):
    HEALTH = "health"
    DEFAULT_PATH = "default_path"
    SOURCE_TO_TARGET = "source_to_target"
    SOURCE_GRAPH = "source_graph"
    RUNTIME_CONTEXT = "runtime_context"
    EVENT_CONTRACTS = "event_contracts"
    DOWNSTREAM_CONTRACTS = "downstream_contracts"
    INTEGRATION_REPORT = "integration_report"
    SOURCE_GRAPH_AUDIT = "source_graph_audit"
    STATE_CUSTODY = "state_custody"
    API_CONTRACT = "api_contract"


@dataclass(frozen=True, slots=True)
class ApiInventoryFieldContract:
    field_id: str
    payload_key: str
    kind: ApiInventoryPayloadKind
    required: bool
    min_items: int
    source: str
    owner_slice: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "payload_key": self.payload_key,
            "kind": str(self.kind),
            "required": self.required,
            "min_items": self.min_items,
            "source": self.source,
            "owner_slice": self.owner_slice,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ApiInventoryFieldObservation:
    field_id: str
    payload_key: str
    kind: ApiInventoryPayloadKind
    required: bool
    present: bool
    item_count: int
    observed_type: str
    source: str
    status: ApiInventoryContractStatus
    risk: ApiInventoryContractRisk
    message: str

    @property
    def blocking(self) -> bool:
        return self.status == ApiInventoryContractStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "payload_key": self.payload_key,
            "kind": str(self.kind),
            "required": self.required,
            "present": self.present,
            "item_count": self.item_count,
            "observed_type": self.observed_type,
            "source": self.source,
            "status": str(self.status),
            "risk": str(self.risk),
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class ApiInventoryRouteContract:
    route: str
    method: str
    handler: str
    target_path: str
    required_for_console: bool
    payload_fields: tuple[str, ...]
    status: ApiInventoryContractStatus
    message: str

    @property
    def blocking(self) -> bool:
        return self.required_for_console and self.status == ApiInventoryContractStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "method": self.method,
            "handler": self.handler,
            "target_path": self.target_path,
            "required_for_console": self.required_for_console,
            "payload_fields": list(self.payload_fields),
            "status": str(self.status),
            "message": self.message,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class ApiInventoryContractFinding:
    severity: ApiInventoryContractSeverity
    code: str
    message: str
    subject: str

    @property
    def blocking(self) -> bool:
        return self.severity == ApiInventoryContractSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "subject": self.subject,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class ApiInventoryContractReport:
    ok: bool
    checked_at: str
    owner_slice: str
    route: ApiInventoryRouteContract
    fields: tuple[ApiInventoryFieldObservation, ...]
    findings: tuple[ApiInventoryContractFinding, ...] = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple[ApiInventoryContractFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[ApiInventoryContractFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == ApiInventoryContractSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    @property
    def present_required_fields(self) -> int:
        return sum(1 for field in self.fields if field.required and field.present)

    @property
    def required_field_count(self) -> int:
        return sum(1 for field in self.fields if field.required)

    def metadata(self) -> dict[str, str]:
        return {
            "api_inventory_contract_ok": str(self.ok).lower(),
            "api_inventory_contract_route": self.route.route,
            "api_inventory_contract_required_fields": str(self.required_field_count),
            "api_inventory_contract_present_required_fields": str(self.present_required_fields),
            "api_inventory_contract_blockers": str(len(self.blockers)),
            "api_inventory_contract_warnings": str(len(self.warnings)),
            "api_inventory_contract_first_blocker": self.first_blocker_code,
            "api_inventory_contract_owner_slice": self.owner_slice,
        }

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": "api_inventory_contract",
            "ok": self.ok,
            "route": self.route.to_dict(),
            "required_fields": self.required_field_count,
            "present_required_fields": self.present_required_fields,
            "blocking_error": self.first_blocker_code,
            "blockers": [finding.to_dict() for finding in self.blockers],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "owner_slice": self.owner_slice,
            "route": self.route.to_dict(),
            "fields": [field.to_dict() for field in self.fields],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


def build_api_inventory_contract_report(
    *,
    project_root: str | Path,
    contracts: ClaudeRuntimeContractBundle,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport | None = None,
    payload: Mapping[str, Any] | None = None,
) -> ApiInventoryContractReport:
    project_path = Path(project_root).resolve()
    field_contracts = default_api_inventory_field_contracts(integration_report)
    route = _route_contract(project_path, field_contracts)
    synthetic_payload = dict(payload) if payload is not None else _synthetic_inventory_payload(
        contracts=contracts,
        integration_report=integration_report,
        runtime_context_report=runtime_context_report,
    )
    field_rows = tuple(_observe_field(contract, synthetic_payload) for contract in field_contracts)
    semantic_rows = tuple(
        _semantic_contract_observations(
            integration_report=integration_report,
            runtime_context_report=runtime_context_report,
            payload=synthetic_payload,
            owner_slice=integration_report.crosswalk.owner_slice,
        )
    )
    fields = tuple([*field_rows, *semantic_rows])
    findings = tuple(_api_inventory_findings(route, fields))
    return ApiInventoryContractReport(
        ok=not any(finding.blocking for finding in findings),
        checked_at=now_iso(),
        owner_slice=integration_report.crosswalk.owner_slice,
        route=route,
        fields=fields,
        findings=findings,
    )


def default_api_inventory_field_contracts(
    integration_report: ClaudeProductizationIntegrationReport,
) -> tuple[ApiInventoryFieldContract, ...]:
    owner = integration_report.crosswalk.owner_slice
    return (
        ApiInventoryFieldContract(
            field_id="inventory.health",
            payload_key="health",
            kind=ApiInventoryPayloadKind.HEALTH,
            required=True,
            min_items=1,
            source="ClaudeRuntimeContractBundle.health",
            owner_slice=owner,
            rationale="The console needs runtime health before offering CodeWorker controls.",
        ),
        ApiInventoryFieldContract(
            field_id="inventory.default_path",
            payload_key="defaultPath",
            kind=ApiInventoryPayloadKind.DEFAULT_PATH,
            required=True,
            min_items=1,
            source="ClaudeRuntimeContractBundle.default_path",
            owner_slice=owner,
            rationale="The route must expose that default execution is Zyra-owned and sidecar-free.",
        ),
        ApiInventoryFieldContract(
            field_id="inventory.source_to_target",
            payload_key="sourceToTarget",
            kind=ApiInventoryPayloadKind.SOURCE_TO_TARGET,
            required=True,
            min_items=1,
            source="ClaudeRuntimeContractBundle.source_to_target",
            owner_slice=owner,
            rationale="Source-to-target decisions remain visible to audit and console surfaces.",
        ),
        ApiInventoryFieldContract(
            field_id="inventory.source_graph",
            payload_key="sourceGraph",
            kind=ApiInventoryPayloadKind.SOURCE_GRAPH,
            required=True,
            min_items=1,
            source="ClaudeSourceGraphCrosswalk.source_to_target_payload",
            owner_slice=owner,
            rationale="The productized source graph must be available without the root source repository.",
        ),
        ApiInventoryFieldContract(
            field_id="inventory.runtime_context",
            payload_key="runtimeContext",
            kind=ApiInventoryPayloadKind.RUNTIME_CONTEXT,
            required=True,
            min_items=1,
            source="ClaudeSourceGraphCrosswalk.runtime_context_payload",
            owner_slice=owner,
            rationale="RuntimeContext ports must be visible for downstream permission/MCP/skill units.",
        ),
        ApiInventoryFieldContract(
            field_id="inventory.event_contracts",
            payload_key="eventContracts",
            kind=ApiInventoryPayloadKind.EVENT_CONTRACTS,
            required=True,
            min_items=1,
            source="ClaudeSourceGraphCrosswalk.event_contract_payload",
            owner_slice=owner,
            rationale="Event contracts provide the console and later units with causal trace boundaries.",
        ),
        ApiInventoryFieldContract(
            field_id="inventory.downstream_contracts",
            payload_key="downstreamContracts",
            kind=ApiInventoryPayloadKind.DOWNSTREAM_CONTRACTS,
            required=True,
            min_items=1,
            source="ClaudeSourceGraphCrosswalk.downstream_payload",
            owner_slice=owner,
            rationale="The endpoint must publish handoff owners for M1-02B and later units.",
        ),
        ApiInventoryFieldContract(
            field_id="inventory.integration",
            payload_key="integration",
            kind=ApiInventoryPayloadKind.INTEGRATION_REPORT,
            required=True,
            min_items=1,
            source="ClaudeProductizationIntegrationReport.to_dict",
            owner_slice=owner,
            rationale="The source graph integration report is the route-level readiness contract.",
        ),
        ApiInventoryFieldContract(
            field_id="inventory.source_graph_audit",
            payload_key="sourceGraphAudit",
            kind=ApiInventoryPayloadKind.SOURCE_GRAPH_AUDIT,
            required=True,
            min_items=1,
            source="SourceGraphAuditReport.to_dict",
            owner_slice=owner,
            rationale="The route must include dynamic audit results, not only static inventory.",
        ),
    )


def api_inventory_contract_event(request: WorkerRequest, report: ApiInventoryContractReport) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.CONSTRAINT_CHECK,
        payload={"claude_api_inventory_contract": report.event_payload()},
    )


def api_inventory_contract_markdown(report: ApiInventoryContractReport) -> str:
    lines = [
        "## API Inventory Contract",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- route: `{report.route.route}`",
        f"- required_fields: `{report.required_field_count}`",
        f"- present_required_fields: `{report.present_required_fields}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Fields",
        "",
    ]
    for field in report.fields:
        lines.append(
            f"- `{field.payload_key}` kind=`{field.kind}` present=`{str(field.present).lower()}` "
            f"items=`{field.item_count}` status=`{field.status}`"
        )
    if report.blockers:
        lines.extend(["", "### API Contract Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    return "\n".join(lines) + "\n"


def assert_api_inventory_contract_ready(report: ApiInventoryContractReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"API inventory contract failed: {blockers}")


def _route_contract(project_root: Path, fields: Iterable[ApiInventoryFieldContract]) -> ApiInventoryRouteContract:
    target_path = "apps/api/zyra_api/main.py"
    exists = (project_root / target_path).exists()
    return ApiInventoryRouteContract(
        route="/workers/code/inventory",
        method="GET",
        handler="ZyraRequestHandler.do_GET",
        target_path=target_path,
        required_for_console=True,
        payload_fields=tuple(field.payload_key for field in fields),
        status=ApiInventoryContractStatus.PASSING if exists else ApiInventoryContractStatus.BLOCKED,
        message="Route target exists." if exists else "Route target apps/api/zyra_api/main.py is missing.",
    )


def _synthetic_inventory_payload(
    *,
    contracts: ClaudeRuntimeContractBundle,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport | None,
) -> dict[str, Any]:
    runtime_context = integration_report.crosswalk.runtime_context_payload()
    if runtime_context_report is not None:
        runtime_context = {
            **runtime_context,
            "assembly": runtime_context_report.to_dict(),
            "assembly_ok": runtime_context_report.ok,
        }
    return {
        **dict(contracts.inventory),
        "health": contracts.health,
        "defaultPath": contracts.default_path,
        "sourceToTarget": [item.to_dict() for item in contracts.source_to_target],
        "sourceGraph": integration_report.crosswalk.source_to_target_payload(),
        "runtimeContext": runtime_context,
        "eventContracts": integration_report.crosswalk.event_contract_payload(),
        "downstreamContracts": integration_report.crosswalk.downstream_payload(),
        "integration": integration_report.to_dict(),
        "sourceGraphAudit": {"ok": integration_report.ok, "contract_id": integration_report.crosswalk.contract_id},
    }


def _observe_field(
    contract: ApiInventoryFieldContract,
    payload: Mapping[str, Any],
) -> ApiInventoryFieldObservation:
    present = contract.payload_key in payload
    value = payload.get(contract.payload_key)
    observed_type = type(value).__name__ if present else "missing"
    item_count = _payload_item_count(value)
    risk = _field_risk(contract, present, item_count, value)
    status = _field_status(contract.required, risk)
    return ApiInventoryFieldObservation(
        field_id=contract.field_id,
        payload_key=contract.payload_key,
        kind=contract.kind,
        required=contract.required,
        present=present,
        item_count=item_count,
        observed_type=observed_type,
        source=contract.source,
        status=status,
        risk=risk,
        message=_field_message(contract, risk, item_count),
    )


def _semantic_contract_observations(
    *,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_context_report: RuntimeContextAssemblyReport | None,
    payload: Mapping[str, Any],
    owner_slice: str,
) -> Iterable[ApiInventoryFieldObservation]:
    yield _semantic_observation(
        field_id="inventory.semantic.source_graph_ok",
        key="sourceGraph",
        kind=ApiInventoryPayloadKind.SOURCE_GRAPH,
        ok=integration_report.ok and integration_report.crosswalk.source_pool_target_count == 0,
        risk=ApiInventoryContractRisk.SOURCE_GRAPH_NOT_READY,
        source="ClaudeProductizationIntegrationReport",
        owner_slice=owner_slice,
        message="Source graph is ready and contains no source-pool targets.",
    )
    yield _semantic_observation(
        field_id="inventory.semantic.runtime_context_bound",
        key="runtimeContext",
        kind=ApiInventoryPayloadKind.RUNTIME_CONTEXT,
        ok=bool(runtime_context_report and runtime_context_report.ok and runtime_context_report.active_runtime_bindings),
        risk=ApiInventoryContractRisk.RUNTIME_CONTEXT_NOT_BOUND,
        source="RuntimeContextAssemblyReport",
        owner_slice=owner_slice,
        message="RuntimeContext assembly is bound for the inventory route.",
    )
    downstream_payload = payload.get("downstreamContracts") if isinstance(payload, Mapping) else {}
    downstream_owners = downstream_payload.get("owners", []) if isinstance(downstream_payload, Mapping) else []
    yield _semantic_observation(
        field_id="inventory.semantic.downstream_owners",
        key="downstreamContracts",
        kind=ApiInventoryPayloadKind.DOWNSTREAM_CONTRACTS,
        ok="M1-02B" in downstream_owners and "M1-03A" in downstream_owners,
        risk=ApiInventoryContractRisk.DOWNSTREAM_CONTRACT_GAP,
        source="ClaudeSourceGraphCrosswalk.downstream_payload",
        owner_slice=owner_slice,
        message="Downstream owners include permission/MCP and memory/scheduler handoff slices.",
    )
    event_payload = payload.get("eventContracts") if isinstance(payload, Mapping) else {}
    event_count = int(event_payload.get("event_contract_count", 0)) if isinstance(event_payload, Mapping) else 0
    yield _semantic_observation(
        field_id="inventory.semantic.event_contract_count",
        key="eventContracts",
        kind=ApiInventoryPayloadKind.EVENT_CONTRACTS,
        ok=event_count >= 20,
        risk=ApiInventoryContractRisk.EVENT_CONTRACT_GAP,
        source="ClaudeSourceGraphCrosswalk.event_contract_payload",
        owner_slice=owner_slice,
        message="Event contract payload exposes enough phases for runtime and console causality.",
    )


def _semantic_observation(
    *,
    field_id: str,
    key: str,
    kind: ApiInventoryPayloadKind,
    ok: bool,
    risk: ApiInventoryContractRisk,
    source: str,
    owner_slice: str,
    message: str,
) -> ApiInventoryFieldObservation:
    return ApiInventoryFieldObservation(
        field_id=field_id,
        payload_key=key,
        kind=kind,
        required=True,
        present=ok,
        item_count=1 if ok else 0,
        observed_type="semantic",
        source=f"{source}:{owner_slice}",
        status=ApiInventoryContractStatus.PASSING if ok else ApiInventoryContractStatus.BLOCKED,
        risk=ApiInventoryContractRisk.NONE if ok else risk,
        message=message if ok else f"{message} Expected semantic readiness but observed a gap.",
    )


def _api_inventory_findings(
    route: ApiInventoryRouteContract,
    fields: Iterable[ApiInventoryFieldObservation],
) -> list[ApiInventoryContractFinding]:
    findings: list[ApiInventoryContractFinding] = []
    if route.blocking:
        findings.append(
            ApiInventoryContractFinding(
                severity=ApiInventoryContractSeverity.BLOCKER,
                code=f"api_inventory_{ApiInventoryContractRisk.ROUTE_TARGET_MISSING}",
                message=route.message,
                subject=route.route,
            )
        )
    for field in fields:
        if field.status == ApiInventoryContractStatus.BLOCKED:
            findings.append(
                ApiInventoryContractFinding(
                    severity=ApiInventoryContractSeverity.BLOCKER,
                    code=f"api_inventory_{field.risk}",
                    message=field.message,
                    subject=field.payload_key,
                )
            )
        elif field.status == ApiInventoryContractStatus.WARNING:
            findings.append(
                ApiInventoryContractFinding(
                    severity=ApiInventoryContractSeverity.WARNING,
                    code=f"api_inventory_{field.risk}",
                    message=field.message,
                    subject=field.payload_key,
                )
            )
    return findings


def _field_risk(
    contract: ApiInventoryFieldContract,
    present: bool,
    item_count: int,
    value: Any,
) -> ApiInventoryContractRisk:
    if not present:
        return ApiInventoryContractRisk.PAYLOAD_FIELD_MISSING
    if contract.min_items and item_count < contract.min_items:
        return ApiInventoryContractRisk.PAYLOAD_FIELD_EMPTY
    if _contains_vendor_runtime_marker(value):
        return ApiInventoryContractRisk.VENDOR_FIELD_EXPOSED
    return ApiInventoryContractRisk.NONE


def _field_status(required: bool, risk: ApiInventoryContractRisk) -> ApiInventoryContractStatus:
    if risk == ApiInventoryContractRisk.NONE:
        return ApiInventoryContractStatus.PASSING
    if required and risk in {
        ApiInventoryContractRisk.PAYLOAD_FIELD_MISSING,
        ApiInventoryContractRisk.PAYLOAD_FIELD_EMPTY,
        ApiInventoryContractRisk.RUNTIME_CONTEXT_NOT_BOUND,
        ApiInventoryContractRisk.SOURCE_GRAPH_NOT_READY,
        ApiInventoryContractRisk.DOWNSTREAM_CONTRACT_GAP,
        ApiInventoryContractRisk.EVENT_CONTRACT_GAP,
        ApiInventoryContractRisk.VENDOR_FIELD_EXPOSED,
    }:
        return ApiInventoryContractStatus.BLOCKED
    return ApiInventoryContractStatus.WARNING


def _field_message(contract: ApiInventoryFieldContract, risk: ApiInventoryContractRisk, item_count: int) -> str:
    if risk == ApiInventoryContractRisk.NONE:
        return f"Payload field {contract.payload_key} satisfies {contract.field_id}."
    if risk == ApiInventoryContractRisk.PAYLOAD_FIELD_MISSING:
        return f"Required inventory payload field {contract.payload_key} is missing."
    if risk == ApiInventoryContractRisk.PAYLOAD_FIELD_EMPTY:
        return f"Inventory payload field {contract.payload_key} has {item_count} item(s), expected {contract.min_items}."
    if risk == ApiInventoryContractRisk.VENDOR_FIELD_EXPOSED:
        return f"Inventory payload field {contract.payload_key} exposes vendor/source-pool markers."
    return f"Inventory payload field {contract.payload_key} failed semantic contract {contract.field_id}."


def _payload_item_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, Mapping):
        return len(value)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, str):
        return 1 if value else 0
    return 1


def _contains_vendor_runtime_marker(value: Any) -> bool:
    return _contains_vendor_runtime_marker_at_key("", value)


def _contains_vendor_runtime_marker_at_key(key: str, value: Any) -> bool:
    markers = ("../claude-code-best", "../browser-use", "../openhands", "..\\claude-code-best", "..\\browser-use")
    if isinstance(value, str):
        if key and not _path_like_key(key):
            return False
        normalized = value.replace("\\", "/").lower()
        return any(marker in normalized for marker in markers)
    if isinstance(value, Mapping):
        return any(_contains_vendor_runtime_marker_at_key(str(item_key), item) for item_key, item in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_vendor_runtime_marker_at_key(key, item) for item in value)
    return False


def _path_like_key(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("target", "root", "uri", "entrypoint", "workspace"))
