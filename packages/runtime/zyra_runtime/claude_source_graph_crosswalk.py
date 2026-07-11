from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, now_iso, to_jsonable

from .claude_runtime_contracts import (
    ClaudeRuntimeContractBundle,
    ClaudeRuntimeDecision,
    ClaudeRuntimeSurface,
)
from .workers import WorkerRequest


OWNER_SLICE = "M1-02A-02"
PARENT_UNIT = "M1-02A"
PRIMARY_SOURCE_REPO = "claude-code-best"
CONTRACT_ID = "zyra-claude-source-graph-crosswalk"
CONTRACT_VERSION = "2026-07-07"
CONTRACT_SOURCE = "zyra-owned-source-graph-crosswalk"


class ClaudeSourceGraphBatch(StrEnum):
    QUERY_SESSION_CONTEXT = "batch-01-query-session-context"
    QUERY_TOOL_LOOP = "batch-02-query-tool-loop"
    PERMISSION_RUNTIME_HOOKS = "batch-03-permission-runtime-hooks"
    COMPACT_CONTEXT_RESTORE = "batch-04-compact-context-restore"
    MCP_RUNTIME_TOOLS_AUTH = "batch-05-mcp-runtime-tools-auth"
    SKILL_PLUGIN_HOOKS = "batch-06-skill-plugin-hooks"
    AGENT_SUBAGENT_TASK_ISOLATION = "batch-07-agent-subagent-task-isolation"
    API_STREAMING_RETRY_CLIENT = "batch-08-api-streaming-retry-client"
    TUI_CLI_COMMANDS_CONTROL = "batch-09-tui-cli-commands-control"


class CrosswalkDecision(StrEnum):
    ZYRA_MODULE_MIGRATED = "zyra_module_migrated"
    ACTIVE_RUNTIME_CONTRACT = "active_runtime_contract"
    DOWNSTREAM_HANDOFF = "downstream_handoff"
    REFERENCE_ONLY = "reference_only"
    LEGACY_VENDOR_DEBT = "legacy_vendor_debt"
    BLOCKED = "blocked"


class CrosswalkFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class RuntimePortKind(StrEnum):
    SESSION_LIFECYCLE = "session_lifecycle"
    WORKSPACE_CWD = "workspace_cwd"
    TOOL_REGISTRY = "tool_registry"
    TOOL_EXECUTOR = "tool_executor"
    TOOL_RESULT_BUDGET = "tool_result_budget"
    PERMISSION_MODE = "permission_mode"
    CONTEXT_WINDOW = "context_window"
    COMPACT_RESTORE = "compact_restore"
    MCP_CLIENTS = "mcp_clients"
    SKILL_REGISTRY = "skill_registry"
    PLUGIN_REGISTRY = "plugin_registry"
    SUBAGENT_DISPATCH = "subagent_dispatch"
    CONTROL_COMMANDS = "control_commands"
    MODEL_STREAM = "model_stream"
    EVENT_SINK = "event_sink"
    ARTIFACT_STORE = "artifact_store"
    SOURCE_GRAPH = "source_graph"
    STATE_CUSTODY = "state_custody"


class RuntimePortStatus(StrEnum):
    ACTIVE = "active"
    CONTRACT_READY = "contract_ready"
    DEFERRED_TO_OWNER = "deferred_to_owner"
    DISABLED = "disabled"


class EventContractPhase(StrEnum):
    SESSION_INIT = "session_init"
    MESSAGE_RECEIVED = "message_received"
    TOOL_USE_REQUESTED = "tool_use_requested"
    TOOL_RESULT_RECORDED = "tool_result_recorded"
    PERMISSION_REQUESTED = "permission_requested"
    PERMISSION_DECIDED = "permission_decided"
    CONTEXT_COMPACTED = "context_compacted"
    COMPACT_RESTORE_POINT = "compact_restore_point"
    MCP_SERVER_REGISTERED = "mcp_server_registered"
    MCP_TOOL_PROJECTED = "mcp_tool_projected"
    SKILL_INVOKED = "skill_invoked"
    PLUGIN_HOOK_FIRED = "plugin_hook_fired"
    SUBAGENT_TASK_STARTED = "subagent_task_started"
    SUBAGENT_TASK_FINISHED = "subagent_task_finished"
    CONTROL_COMMAND_RECEIVED = "control_command_received"
    CONTROL_COMMAND_APPLIED = "control_command_applied"
    MODEL_STREAM_DELTA = "model_stream_delta"
    RETRY_DECISION = "retry_decision"
    SOURCE_GRAPH_CROSSWALK_READY = "source_graph_crosswalk_ready"
    RUNTIME_CONTEXT_READY = "runtime_context_ready"
    DOWNSTREAM_CONTRACTS_READY = "downstream_contracts_ready"
    INTEGRATION_GATE_PASSED = "integration_gate_passed"
    INTEGRATION_GATE_BLOCKED = "integration_gate_blocked"


class SourceGraphOwnerStatus(StrEnum):
    CURRENT_SLICE = "current_slice"
    DOWNSTREAM_READY_CONTRACT = "downstream_ready_contract"
    DOWNSTREAM_DEFERRED = "downstream_deferred"


@dataclass(frozen=True, slots=True)
class SourceGraphDocumentRef:
    batch: ClaudeSourceGraphBatch
    document_path: str
    title: str
    source_summary: str
    required_signals: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch": str(self.batch),
            "document_path": self.document_path,
            "title": self.title,
            "source_summary": self.source_summary,
            "required_signals": list(self.required_signals),
        }


@dataclass(frozen=True, slots=True)
class UpstreamSourceRef:
    repo: str
    source_path: str
    symbol_or_region: str
    migration_note: str
    required_for_current_slice: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "source_path": self.source_path,
            "symbol_or_region": self.symbol_or_region,
            "migration_note": self.migration_note,
            "required_for_current_slice": self.required_for_current_slice,
        }


@dataclass(frozen=True, slots=True)
class ZyraTargetRef:
    path: str
    symbol_or_entrypoint: str
    owner_slice: str
    runtime_role: str
    decision: CrosswalkDecision
    current_slice_required: bool = False
    import_symbol: str = ""
    main_path_required: bool = False

    @property
    def is_source_pool(self) -> bool:
        normalized = self.path.replace("\\", "/")
        return any(marker in normalized for marker in ("/vendor/", "/vendor-runtimes/", "/source-pool/", "/runtime-sources/"))

    @property
    def is_product_module(self) -> bool:
        normalized = self.path.replace("\\", "/")
        return normalized.startswith(("packages/", "apps/", "skills/", "scripts/"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "symbol_or_entrypoint": self.symbol_or_entrypoint,
            "owner_slice": self.owner_slice,
            "runtime_role": self.runtime_role,
            "decision": str(self.decision),
            "current_slice_required": self.current_slice_required,
            "import_symbol": self.import_symbol,
            "main_path_required": self.main_path_required,
            "is_source_pool": self.is_source_pool,
            "is_product_module": self.is_product_module,
        }


@dataclass(frozen=True, slots=True)
class RuntimeContextPortContract:
    port_id: str
    kind: RuntimePortKind
    owner_slice: str
    status: RuntimePortStatus
    producer: str
    consumer: str
    state_owner: str
    contract_fields: tuple[str, ...]
    event_phases: tuple[EventContractPhase, ...]
    failure_code: str
    required_for_runtime_shell: bool = False
    downstream_owner: str = ""

    def disabled_copy(self) -> "RuntimeContextPortContract":
        return RuntimeContextPortContract(
            port_id=self.port_id,
            kind=self.kind,
            owner_slice=self.owner_slice,
            status=RuntimePortStatus.DISABLED,
            producer=self.producer,
            consumer=self.consumer,
            state_owner=self.state_owner,
            contract_fields=self.contract_fields,
            event_phases=self.event_phases,
            failure_code=self.failure_code,
            required_for_runtime_shell=self.required_for_runtime_shell,
            downstream_owner=self.downstream_owner,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "port_id": self.port_id,
            "kind": str(self.kind),
            "owner_slice": self.owner_slice,
            "status": str(self.status),
            "producer": self.producer,
            "consumer": self.consumer,
            "state_owner": self.state_owner,
            "contract_fields": list(self.contract_fields),
            "event_phases": [str(phase) for phase in self.event_phases],
            "failure_code": self.failure_code,
            "required_for_runtime_shell": self.required_for_runtime_shell,
            "downstream_owner": self.downstream_owner,
        }


@dataclass(frozen=True, slots=True)
class ToolUseContextPortContract:
    port_id: str
    tool_surface: str
    registry_entrypoint: str
    permission_surface: str
    budget_surface: str
    result_surface: str
    current_slice_contract: str
    downstream_owner: str
    event_phases: tuple[EventContractPhase, ...]
    required_for_runtime_shell: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "port_id": self.port_id,
            "tool_surface": self.tool_surface,
            "registry_entrypoint": self.registry_entrypoint,
            "permission_surface": self.permission_surface,
            "budget_surface": self.budget_surface,
            "result_surface": self.result_surface,
            "current_slice_contract": self.current_slice_contract,
            "downstream_owner": self.downstream_owner,
            "event_phases": [str(phase) for phase in self.event_phases],
            "required_for_runtime_shell": self.required_for_runtime_shell,
        }


@dataclass(frozen=True, slots=True)
class EventContract:
    phase: EventContractPhase
    owner_slice: str
    payload_key: str
    source_batch: ClaudeSourceGraphBatch
    producer: str
    consumer: str
    required_fields: tuple[str, ...]
    currently_emitted: bool
    downstream_owner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": str(self.phase),
            "owner_slice": self.owner_slice,
            "payload_key": self.payload_key,
            "source_batch": str(self.source_batch),
            "producer": self.producer,
            "consumer": self.consumer,
            "required_fields": list(self.required_fields),
            "currently_emitted": self.currently_emitted,
            "downstream_owner": self.downstream_owner,
        }


@dataclass(frozen=True, slots=True)
class DownstreamContract:
    contract_id: str
    owner_slice: str
    target_unit: str
    capability: str
    source_batches: tuple[ClaudeSourceGraphBatch, ...]
    required_ports: tuple[RuntimePortKind, ...]
    required_events: tuple[EventContractPhase, ...]
    required_targets: tuple[str, ...]
    acceptance_test_entrypoints: tuple[str, ...]
    handoff_status: SourceGraphOwnerStatus

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "owner_slice": self.owner_slice,
            "target_unit": self.target_unit,
            "capability": self.capability,
            "source_batches": [str(batch) for batch in self.source_batches],
            "required_ports": [str(port) for port in self.required_ports],
            "required_events": [str(phase) for phase in self.required_events],
            "required_targets": list(self.required_targets),
            "acceptance_test_entrypoints": list(self.acceptance_test_entrypoints),
            "handoff_status": str(self.handoff_status),
        }


@dataclass(frozen=True, slots=True)
class SourceGraphBatchContract:
    batch: ClaudeSourceGraphBatch
    document: SourceGraphDocumentRef
    source_refs: tuple[UpstreamSourceRef, ...]
    zyra_targets: tuple[ZyraTargetRef, ...]
    runtime_ports: tuple[RuntimePortKind, ...]
    event_phases: tuple[EventContractPhase, ...]
    owner_slice: str
    downstream_slices: tuple[str, ...]
    decision: CrosswalkDecision
    main_path_role: str
    acceptance_test_entrypoints: tuple[str, ...]
    owner_status: SourceGraphOwnerStatus

    @property
    def current_slice_targets(self) -> tuple[ZyraTargetRef, ...]:
        return tuple(target for target in self.zyra_targets if target.current_slice_required)

    @property
    def main_path_targets(self) -> tuple[ZyraTargetRef, ...]:
        return tuple(target for target in self.zyra_targets if target.main_path_required)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch": str(self.batch),
            "document": self.document.to_dict(),
            "source_refs": [source.to_dict() for source in self.source_refs],
            "zyra_targets": [target.to_dict() for target in self.zyra_targets],
            "runtime_ports": [str(port) for port in self.runtime_ports],
            "event_phases": [str(phase) for phase in self.event_phases],
            "owner_slice": self.owner_slice,
            "downstream_slices": list(self.downstream_slices),
            "decision": str(self.decision),
            "main_path_role": self.main_path_role,
            "acceptance_test_entrypoints": list(self.acceptance_test_entrypoints),
            "owner_status": str(self.owner_status),
        }


@dataclass(frozen=True, slots=True)
class CrosswalkFinding:
    severity: CrosswalkFindingSeverity
    code: str
    message: str
    batch: ClaudeSourceGraphBatch | None = None
    target_path: str = ""
    port_id: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == CrosswalkFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "batch": str(self.batch) if self.batch else "",
            "target_path": self.target_path,
            "port_id": self.port_id,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class CrosswalkValidationReport:
    ok: bool
    checked_at: str
    contract_id: str
    contract_version: str
    owner_slice: str
    batch_count: int
    current_slice_target_count: int
    main_path_target_count: int
    runtime_context_port_count: int
    tool_use_context_port_count: int
    downstream_contract_count: int
    event_contract_count: int
    findings: tuple[CrosswalkFinding, ...] = ()

    @property
    def blockers(self) -> tuple[CrosswalkFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def warnings(self) -> tuple[CrosswalkFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == CrosswalkFindingSeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    def metadata(self) -> dict[str, str]:
        return {
            "source_graph_crosswalk_ok": str(self.ok).lower(),
            "source_graph_contract_id": self.contract_id,
            "source_graph_contract_version": self.contract_version,
            "source_graph_owner_slice": self.owner_slice,
            "source_graph_batch_count": str(self.batch_count),
            "source_graph_current_slice_targets": str(self.current_slice_target_count),
            "source_graph_main_path_targets": str(self.main_path_target_count),
            "runtime_context_port_count": str(self.runtime_context_port_count),
            "tool_use_context_port_count": str(self.tool_use_context_port_count),
            "downstream_contract_count": str(self.downstream_contract_count),
            "event_contract_count": str(self.event_contract_count),
            "source_graph_blocker_count": str(len(self.blockers)),
            "source_graph_warning_count": str(len(self.warnings)),
            "source_graph_first_blocker": self.first_blocker_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "owner_slice": self.owner_slice,
            "batch_count": self.batch_count,
            "current_slice_target_count": self.current_slice_target_count,
            "main_path_target_count": self.main_path_target_count,
            "runtime_context_port_count": self.runtime_context_port_count,
            "tool_use_context_port_count": self.tool_use_context_port_count,
            "downstream_contract_count": self.downstream_contract_count,
            "event_contract_count": self.event_contract_count,
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


@dataclass(frozen=True, slots=True)
class ClaudeSourceGraphCrosswalk:
    contract_id: str
    contract_version: str
    owner_slice: str
    parent_unit: str
    source_repo: str
    generated_at: str
    batches: tuple[SourceGraphBatchContract, ...]
    runtime_context_ports: tuple[RuntimeContextPortContract, ...]
    tool_use_context_ports: tuple[ToolUseContextPortContract, ...]
    event_contracts: tuple[EventContract, ...]
    downstream_contracts: tuple[DownstreamContract, ...]

    @property
    def batch_ids(self) -> tuple[str, ...]:
        return tuple(str(batch.batch) for batch in self.batches)

    @property
    def current_slice_targets(self) -> tuple[ZyraTargetRef, ...]:
        targets: list[ZyraTargetRef] = []
        for batch in self.batches:
            targets.extend(batch.current_slice_targets)
        return tuple(_unique_targets(targets))

    @property
    def main_path_targets(self) -> tuple[ZyraTargetRef, ...]:
        targets: list[ZyraTargetRef] = []
        for batch in self.batches:
            targets.extend(batch.main_path_targets)
        return tuple(_unique_targets(targets))

    @property
    def source_pool_target_count(self) -> int:
        return sum(1 for batch in self.batches for target in batch.zyra_targets if target.is_source_pool)

    def validate(
        self,
        *,
        project_root: str | Path | None = None,
        disabled_batches: Iterable[str] = (),
        disabled_ports: Iterable[str] = (),
        disable_source_graph: bool = False,
        disable_downstream_contracts: bool = False,
        require_current_targets_exist: bool = True,
        runtime_contracts: ClaudeRuntimeContractBundle | None = None,
    ) -> CrosswalkValidationReport:
        project_path = Path(project_root).resolve() if project_root else None
        disabled_batch_values = set(_ordered_normalized_values(disabled_batches))
        disabled_port_values = _ordered_normalized_values(disabled_ports)
        findings: list[CrosswalkFinding] = []

        if disable_source_graph:
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="source_graph_crosswalk_disabled",
                    message="Claude source graph crosswalk was disabled before CodeWorker runtime startup.",
                )
            )

        expected_batches = {str(batch) for batch in ClaudeSourceGraphBatch}
        actual_batches = set(self.batch_ids)
        for missing in sorted(expected_batches - actual_batches):
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="source_graph_batch_missing",
                    message=f"Missing source graph batch {missing}.",
                )
            )
        for extra in sorted(actual_batches - expected_batches):
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.WARNING,
                    code="source_graph_batch_unknown",
                    message=f"Unknown source graph batch {extra}.",
                )
            )

        for batch in self.batches:
            batch_value = str(batch.batch)
            if batch_value in disabled_batch_values or batch.batch.name.lower() in disabled_batch_values:
                findings.append(
                    CrosswalkFinding(
                        severity=CrosswalkFindingSeverity.BLOCKER,
                        code="source_graph_batch_disabled",
                        message=f"Source graph batch {batch_value} was disabled by runtime constraints.",
                        batch=batch.batch,
                    )
                )
            if not batch.source_refs:
                findings.append(
                    CrosswalkFinding(
                        severity=CrosswalkFindingSeverity.BLOCKER,
                        code="source_graph_batch_has_no_sources",
                        message=f"Source graph batch {batch_value} has no upstream source refs.",
                        batch=batch.batch,
                    )
                )
            if not batch.zyra_targets:
                findings.append(
                    CrosswalkFinding(
                        severity=CrosswalkFindingSeverity.BLOCKER,
                        code="source_graph_batch_has_no_targets",
                        message=f"Source graph batch {batch_value} has no Zyra target refs.",
                        batch=batch.batch,
                    )
                )
            if not batch.runtime_ports:
                findings.append(
                    CrosswalkFinding(
                        severity=CrosswalkFindingSeverity.WARNING,
                        code="source_graph_batch_has_no_runtime_ports",
                        message=f"Source graph batch {batch_value} has no runtime context ports.",
                        batch=batch.batch,
                    )
                )
            if not batch.event_phases:
                findings.append(
                    CrosswalkFinding(
                        severity=CrosswalkFindingSeverity.WARNING,
                        code="source_graph_batch_has_no_event_phases",
                        message=f"Source graph batch {batch_value} has no event contract phases.",
                        batch=batch.batch,
                    )
                )
            for target in batch.zyra_targets:
                findings.extend(_validate_target_ref(target, batch=batch.batch, project_root=project_path, require_exists=require_current_targets_exist))

        if self.source_pool_target_count:
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="source_pool_target_in_crosswalk",
                    message="Source graph crosswalk contains vendor/source-pool target paths; these cannot prove internalization.",
                )
            )

        port_by_id = {port.port_id: port for port in self.runtime_context_ports}
        port_by_kind = {str(port.kind): port for port in self.runtime_context_ports}
        for raw_port in disabled_port_values:
            port = port_by_id.get(raw_port) or port_by_kind.get(raw_port)
            if port is None:
                findings.append(
                    CrosswalkFinding(
                        severity=CrosswalkFindingSeverity.WARNING,
                        code="runtime_context_unknown_disabled_port",
                        message=f"Runtime constraints disabled unknown context port {raw_port}.",
                        port_id=raw_port,
                    )
                )
                continue
            severity = CrosswalkFindingSeverity.BLOCKER if port.required_for_runtime_shell else CrosswalkFindingSeverity.WARNING
            findings.append(
                CrosswalkFinding(
                    severity=severity,
                    code=port.failure_code,
                    message=f"Runtime context port {port.port_id} is disabled.",
                    port_id=port.port_id,
                )
            )

        if not self.runtime_context_ports:
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="runtime_context_ports_missing",
                    message="No RuntimeContext ports were defined for the CodeWorker shell.",
                )
            )

        required_runtime_ports = [port for port in self.runtime_context_ports if port.required_for_runtime_shell]
        if not required_runtime_ports:
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="runtime_context_required_ports_missing",
                    message="No required RuntimeContext ports were marked for CodeWorker shell assembly.",
                )
            )

        if not self.tool_use_context_ports:
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="tool_use_context_ports_missing",
                    message="No ToolUseContext ports were defined for tool loop handoff.",
                )
            )

        if disable_downstream_contracts:
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="downstream_contracts_disabled",
                    message="Downstream source-to-target contracts were disabled; 02B/02C/02D/03A handoff cannot be proven.",
                )
            )
        if not self.downstream_contracts:
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="downstream_contracts_missing",
                    message="No downstream contracts were exposed for later M1 slices.",
                )
            )

        if runtime_contracts is not None:
            findings.extend(_validate_foundation_contract_alignment(self, runtime_contracts))

        ok = not any(finding.blocking for finding in findings)
        return CrosswalkValidationReport(
            ok=ok,
            checked_at=now_iso(),
            contract_id=self.contract_id,
            contract_version=self.contract_version,
            owner_slice=self.owner_slice,
            batch_count=len(self.batches),
            current_slice_target_count=len(self.current_slice_targets),
            main_path_target_count=len(self.main_path_targets),
            runtime_context_port_count=len(self.runtime_context_ports),
            tool_use_context_port_count=len(self.tool_use_context_ports),
            downstream_contract_count=len(self.downstream_contracts),
            event_contract_count=len(self.event_contracts),
            findings=tuple(findings),
        )

    def downstream_payload(self) -> dict[str, Any]:
        by_owner: dict[str, list[dict[str, Any]]] = {}
        for contract in self.downstream_contracts:
            by_owner.setdefault(contract.owner_slice, []).append(contract.to_dict())
        return {
            "contract_count": len(self.downstream_contracts),
            "owners": by_owner,
            "contracts": [contract.to_dict() for contract in self.downstream_contracts],
        }

    def runtime_context_payload(self) -> dict[str, Any]:
        return {
            "ports": [port.to_dict() for port in self.runtime_context_ports],
            "tool_use_context_ports": [port.to_dict() for port in self.tool_use_context_ports],
            "required_runtime_shell_ports": [
                port.port_id for port in self.runtime_context_ports if port.required_for_runtime_shell
            ],
            "active_ports": [
                port.port_id for port in self.runtime_context_ports if port.status == RuntimePortStatus.ACTIVE
            ],
            "contract_ready_ports": [
                port.port_id for port in self.runtime_context_ports if port.status == RuntimePortStatus.CONTRACT_READY
            ],
        }

    def event_contract_payload(self) -> dict[str, Any]:
        return {
            "event_contract_count": len(self.event_contracts),
            "events": [contract.to_dict() for contract in self.event_contracts],
            "currently_emitted": [
                str(contract.phase) for contract in self.event_contracts if contract.currently_emitted
            ],
            "downstream_events": [
                str(contract.phase) for contract in self.event_contracts if not contract.currently_emitted
            ],
        }

    def source_to_target_payload(self) -> dict[str, Any]:
        return {
            "batches": [batch.to_dict() for batch in self.batches],
            "current_slice_targets": [target.to_dict() for target in self.current_slice_targets],
            "main_path_targets": [target.to_dict() for target in self.main_path_targets],
            "source_pool_target_count": self.source_pool_target_count,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "source_graph_contract_id": self.contract_id,
            "source_graph_contract_version": self.contract_version,
            "source_graph_owner_slice": self.owner_slice,
            "source_graph_parent_unit": self.parent_unit,
            "source_graph_source_repo": self.source_repo,
            "source_graph_batch_count": str(len(self.batches)),
            "source_graph_runtime_context_ports": str(len(self.runtime_context_ports)),
            "source_graph_tool_use_context_ports": str(len(self.tool_use_context_ports)),
            "source_graph_event_contracts": str(len(self.event_contracts)),
            "source_graph_downstream_contracts": str(len(self.downstream_contracts)),
            "source_graph_source_pool_targets": str(self.source_pool_target_count),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "owner_slice": self.owner_slice,
            "parent_unit": self.parent_unit,
            "source_repo": self.source_repo,
            "generated_at": self.generated_at,
            "batches": [batch.to_dict() for batch in self.batches],
            "runtime_context": self.runtime_context_payload(),
            "event_contracts": self.event_contract_payload(),
            "downstream_contracts": self.downstream_payload(),
            "source_to_target": self.source_to_target_payload(),
            "metadata": self.metadata(),
        }


@dataclass(frozen=True, slots=True)
class ClaudeProductizationIntegrationReport:
    ok: bool
    checked_at: str
    crosswalk: ClaudeSourceGraphCrosswalk
    validation: CrosswalkValidationReport
    runtime_contract_metadata: dict[str, str]
    request_constraints: dict[str, Any] = field(default_factory=dict)
    sidecar_contracts_used: bool = False

    @property
    def blocking_error(self) -> str:
        if self.ok:
            return ""
        return self.validation.first_blocker_code or "claude_productization_integration_failed"

    def metadata(self) -> dict[str, str]:
        return {
            **self.crosswalk.metadata(),
            **self.validation.metadata(),
            "claude_productization_integration_ok": str(self.ok).lower(),
            "claude_productization_integration_error": self.blocking_error,
            "claude_integration_sidecar_excluded_from_completion": str(self.sidecar_contracts_used).lower(),
            "claude_integration_request_constraint_count": str(len(self.request_constraints)),
            **{f"foundation_{key}": value for key, value in self.runtime_contract_metadata.items()},
        }

    def event_payload(self, phase: EventContractPhase) -> dict[str, Any]:
        return {
            "phase": str(phase),
            "ok": self.ok,
            "contract_id": self.crosswalk.contract_id,
            "contract_version": self.crosswalk.contract_version,
            "owner_slice": self.crosswalk.owner_slice,
            "source_repo": self.crosswalk.source_repo,
            "batch_count": len(self.crosswalk.batches),
            "runtime_context_port_count": len(self.crosswalk.runtime_context_ports),
            "tool_use_context_port_count": len(self.crosswalk.tool_use_context_ports),
            "downstream_contract_count": len(self.crosswalk.downstream_contracts),
            "event_contract_count": len(self.crosswalk.event_contracts),
            "blocking_error": self.blocking_error,
            "blockers": [finding.to_dict() for finding in self.validation.blockers],
            "sidecar_contracts_used": self.sidecar_contracts_used,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "crosswalk": self.crosswalk.to_dict(),
            "validation": self.validation.to_dict(),
            "runtime_contract_metadata": dict(self.runtime_contract_metadata),
            "request_constraints": to_jsonable(self.request_constraints),
            "sidecar_contracts_used": self.sidecar_contracts_used,
            "blocking_error": self.blocking_error,
            "metadata": self.metadata(),
        }


def default_claude_source_graph_crosswalk() -> ClaudeSourceGraphCrosswalk:
    return ClaudeSourceGraphCrosswalk(
        contract_id=CONTRACT_ID,
        contract_version=CONTRACT_VERSION,
        owner_slice=OWNER_SLICE,
        parent_unit=PARENT_UNIT,
        source_repo=PRIMARY_SOURCE_REPO,
        generated_at=now_iso(),
        batches=_default_batch_contracts(),
        runtime_context_ports=_default_runtime_context_ports(),
        tool_use_context_ports=_default_tool_use_context_ports(),
        event_contracts=_default_event_contracts(),
        downstream_contracts=_default_downstream_contracts(),
    )


def build_claude_productization_integration_report(
    *,
    project_root: str | Path | None = None,
    runtime_contracts: ClaudeRuntimeContractBundle | None = None,
    request_constraints: Mapping[str, Any] | None = None,
    sidecar_contracts_used: bool = False,
) -> ClaudeProductizationIntegrationReport:
    constraints = dict(request_constraints or {})
    crosswalk = default_claude_source_graph_crosswalk()
    disabled_ports = _disabled_ports_from_constraints(constraints)
    disabled_batches = _list_constraint(constraints.get("disabled_source_graph_batches"))
    if constraints.get("disable_tool_use_context_port") is True:
        disabled_ports.extend(
            [
                RuntimePortKind.TOOL_REGISTRY.value,
                RuntimePortKind.TOOL_EXECUTOR.value,
                RuntimePortKind.TOOL_RESULT_BUDGET.value,
            ]
        )
    if constraints.get("disable_runtime_context_port") is True:
        disabled_ports.extend(
            [
                RuntimePortKind.SESSION_LIFECYCLE.value,
                RuntimePortKind.WORKSPACE_CWD.value,
                RuntimePortKind.SOURCE_GRAPH.value,
                RuntimePortKind.EVENT_SINK.value,
            ]
        )
    validation = crosswalk.validate(
        project_root=project_root,
        disabled_batches=disabled_batches,
        disabled_ports=disabled_ports,
        disable_source_graph=constraints.get("disable_source_graph_crosswalk") is True,
        disable_downstream_contracts=constraints.get("disable_downstream_contracts") is True,
        require_current_targets_exist=True,
        runtime_contracts=runtime_contracts,
    )
    return ClaudeProductizationIntegrationReport(
        ok=validation.ok,
        checked_at=validation.checked_at,
        crosswalk=crosswalk,
        validation=validation,
        runtime_contract_metadata=runtime_contracts.metadata() if runtime_contracts else {},
        request_constraints=constraints,
        sidecar_contracts_used=sidecar_contracts_used,
    )


def claude_productization_integration_events(
    request: WorkerRequest,
    report: ClaudeProductizationIntegrationReport,
) -> list[EventRecord]:
    phases = [
        EventContractPhase.SOURCE_GRAPH_CROSSWALK_READY,
        EventContractPhase.RUNTIME_CONTEXT_READY,
        EventContractPhase.DOWNSTREAM_CONTRACTS_READY,
        EventContractPhase.INTEGRATION_GATE_PASSED if report.ok else EventContractPhase.INTEGRATION_GATE_BLOCKED,
    ]
    return [
        EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=EventType.CONSTRAINT_CHECK,
            payload={"claude_productization_integration": report.event_payload(phase)},
        )
        for phase in phases
    ]


def assert_claude_productization_integration_ready(report: ClaudeProductizationIntegrationReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.validation.blockers)
    raise AssertionError(f"Claude productization integration is not ready: {blockers}")


def source_graph_crosswalk_markdown(report: ClaudeProductizationIntegrationReport) -> str:
    validation = report.validation
    blockers = validation.blockers
    warnings = validation.warnings
    lines = [
        "## Claude Source Graph Crosswalk",
        "",
        f"- contract_id: `{report.crosswalk.contract_id}`",
        f"- owner_slice: `{report.crosswalk.owner_slice}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- batch_count: `{validation.batch_count}`",
        f"- runtime_context_ports: `{validation.runtime_context_port_count}`",
        f"- tool_use_context_ports: `{validation.tool_use_context_port_count}`",
        f"- downstream_contracts: `{validation.downstream_contract_count}`",
        f"- event_contracts: `{validation.event_contract_count}`",
        f"- blocking_error: `{report.blocking_error}`",
        "",
        "### Source Graph Batches",
        "",
    ]
    for batch in report.crosswalk.batches:
        lines.append(f"- `{batch.batch}` -> `{batch.owner_slice}` ({batch.main_path_role})")
    lines.extend(["", "### Runtime Context Ports", ""])
    for port in report.crosswalk.runtime_context_ports:
        marker = "required" if port.required_for_runtime_shell else "handoff"
        lines.append(f"- `{port.port_id}` `{port.kind}` `{port.status}` {marker}")
    lines.extend(["", "### Downstream Contracts", ""])
    for contract in report.crosswalk.downstream_contracts:
        lines.append(f"- `{contract.contract_id}` -> `{contract.owner_slice}` {contract.capability}")
    if blockers:
        lines.extend(["", "### Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in blockers)
    if warnings:
        lines.extend(["", "### Warnings", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in warnings)
    return "\n".join(lines) + "\n"


def _default_batch_contracts() -> tuple[SourceGraphBatchContract, ...]:
    return (
        SourceGraphBatchContract(
            batch=ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT,
            document=SourceGraphDocumentRef(
                batch=ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT,
                document_path="docs/milestones/M1-runtime-memory-scheduler-fault/claude-code-source-graph/batch-01-query-session-context.md",
                title="Query/session/context lifecycle",
                source_summary="QueryEngine session creation, context collection, transcript state, restore points.",
                required_signals=("QueryEngine", "session lifecycle", "context assembly", "resume/restore"),
            ),
            source_refs=(
                _source("src/query.ts", "QueryEngine loop and session bootstrap", "Ported into Zyra QueryEngine shell.", True),
                _source("src/context.ts", "context assembly and compaction inputs", "Split into context window and session lifecycle ports.", True),
                _source("src/services/session.ts", "session ids, transcript append, resume chain", "Mapped to QuerySession and ClaudeSessionLifecycleRuntime.", True),
            ),
            zyra_targets=(
                _target("packages/runtime/zyra_runtime/claude_query_engine_runtime.py", "ZyraClaudeQueryEngine", "current CodeWorker runtime shell", True, "ZyraClaudeQueryEngine", True),
                _target("packages/runtime/zyra_runtime/claude_session_lifecycle.py", "ClaudeSessionLifecycleRuntime", "session checkpoint and restore contract", True, "ClaudeSessionLifecycleRuntime", True),
                _target("packages/runtime/zyra_runtime/claude_context_window.py", "ClaudeContextWindowManager", "context budget and compact boundary", True, "ClaudeContextWindowManager", True),
                _target("packages/runtime/zyra_runtime/query_session.py", "QuerySession", "Zyra-owned transcript and turn state", True, "QuerySession", True),
                _target("packages/workers/zyra_workers/code_worker_runtime.py", "CodeWorkerRuntime.run", "worker entry and event bridge", True, "CodeWorkerRuntime", True),
                _target("packages/runtime/zyra_runtime/claude_source_graph_crosswalk.py", "ClaudeSourceGraphCrosswalk", "02A-02 source graph handoff", True, "ClaudeSourceGraphCrosswalk", True),
                _target("packages/runtime/zyra_runtime/claude_runtime_context_ports.py", "assemble_claude_runtime_context", "02A-02 RuntimeContext and ToolUseContext assembly", True, "assemble_claude_runtime_context", True),
                _target("packages/runtime/zyra_runtime/claude_source_graph_audit.py", "build_claude_source_graph_audit", "02A-02 anti-fake audit gate", True, "build_claude_source_graph_audit", True),
                _target("packages/runtime/zyra_runtime/claude_downstream_handoff_runtime.py", "build_downstream_handoff_report", "02A-02 downstream handoff package builder", True, "build_downstream_handoff_report", True),
                _target("packages/runtime/zyra_runtime/claude_event_contract_runtime.py", "build_event_contract_runtime_report", "02A-02 event contract schema and observation runtime", True, "build_event_contract_runtime_report", True),
                _target("packages/runtime/zyra_runtime/claude_productization_review_runtime.py", "build_productization_review_report", "02A-02 clean boundary/state custody/reachability review", True, "build_productization_review_report", True),
                _target("packages/runtime/zyra_runtime/claude_state_custody_runtime.py", "build_state_custody_runtime_report", "02A-02 runtime state custody matrix", True, "build_state_custody_runtime_report", True),
                _target("packages/runtime/zyra_runtime/claude_api_inventory_contract_runtime.py", "build_api_inventory_contract_report", "02A-02 API inventory contract validator", True, "build_api_inventory_contract_report", True),
                _target("packages/runtime/zyra_runtime/claude_worker_execution_gate_runtime.py", "build_worker_execution_gate_report", "02A-02 worker execution gate before QueryEngine", True, "build_worker_execution_gate_report", True),
                _future_target("packages/runtime/zyra_runtime/claude_context_assembly_runtime.py", "ContextAssemblyRuntime", "M1-02B context expansion and selection", "M1-02B"),
            ),
            runtime_ports=(
                RuntimePortKind.SESSION_LIFECYCLE,
                RuntimePortKind.WORKSPACE_CWD,
                RuntimePortKind.CONTEXT_WINDOW,
                RuntimePortKind.COMPACT_RESTORE,
                RuntimePortKind.EVENT_SINK,
                RuntimePortKind.ARTIFACT_STORE,
                RuntimePortKind.SOURCE_GRAPH,
            ),
            event_phases=(
                EventContractPhase.SESSION_INIT,
                EventContractPhase.MESSAGE_RECEIVED,
                EventContractPhase.CONTEXT_COMPACTED,
                EventContractPhase.COMPACT_RESTORE_POINT,
                EventContractPhase.SOURCE_GRAPH_CROSSWALK_READY,
                EventContractPhase.RUNTIME_CONTEXT_READY,
            ),
            owner_slice=OWNER_SLICE,
            downstream_slices=("M1-02B", "M1-02D"),
            decision=CrosswalkDecision.ZYRA_MODULE_MIGRATED,
            main_path_role="default CodeWorker shell assembles session/context/source-graph contracts before tool execution",
            acceptance_test_entrypoints=(
                "tests/integration/test_code_worker_clean_productized_runtime.py",
                "tests/integration/test_claude_productization_integration.py",
            ),
            owner_status=SourceGraphOwnerStatus.CURRENT_SLICE,
        ),
        SourceGraphBatchContract(
            batch=ClaudeSourceGraphBatch.QUERY_TOOL_LOOP,
            document=SourceGraphDocumentRef(
                batch=ClaudeSourceGraphBatch.QUERY_TOOL_LOOP,
                document_path="docs/milestones/M1-runtime-memory-scheduler-fault/claude-code-source-graph/batch-02-query-tool-loop.md",
                title="Tool registry, execution loop, result budget",
                source_summary="ToolUseContext, tool choice handling, concurrent read scheduling, serialized writes, result truncation.",
                required_signals=("tool registry", "tool use context", "read concurrency", "write serialization", "result budget"),
            ),
            source_refs=(
                _source("src/tools.ts", "tool registry and schema projection", "Mapped to ToolRegistry and default_tool_registry.", True),
                _source("src/query.ts", "tool-use loop and tool_result blocks", "Mapped to ZyraClaudeQueryEngine and ToolLoopScheduler.", True),
                _source("src/toolUseContext.ts", "ToolUseContext object", "Mapped to RuntimeContext/ToolUseContext ports.", True),
            ),
            zyra_targets=(
                _target("packages/runtime/zyra_runtime/tools.py", "ToolRegistry", "Zyra-owned tool registry", True, "ToolRegistry", True),
                _target("packages/runtime/zyra_runtime/tool_loop.py", "ToolLoopScheduler", "read/write tool scheduling and budget", True, "ToolLoopScheduler", True),
                _target("packages/runtime/zyra_runtime/claude_tool_use_runtime.py", "ClaudeToolUseRuntime", "semantic tool-use envelope", True, "ClaudeToolUseRuntime", True),
                _target("packages/runtime/zyra_runtime/claude_query_engine_runtime.py", "ZyraClaudeQueryEngine.run", "tool loop entry in QueryEngine shell", True, "ZyraClaudeQueryEngine", True),
                _future_target("packages/runtime/zyra_runtime/claude_tool_registry_runtime.py", "ToolRegistryRuntime", "M1-02C tool registry hardening", "M1-02C"),
                _future_target("packages/runtime/zyra_runtime/claude_tool_result_budget_runtime.py", "ToolResultBudgetRuntime", "M1-02C budget and result artifact policy", "M1-02C"),
            ),
            runtime_ports=(
                RuntimePortKind.TOOL_REGISTRY,
                RuntimePortKind.TOOL_EXECUTOR,
                RuntimePortKind.TOOL_RESULT_BUDGET,
                RuntimePortKind.PERMISSION_MODE,
                RuntimePortKind.EVENT_SINK,
                RuntimePortKind.ARTIFACT_STORE,
            ),
            event_phases=(
                EventContractPhase.TOOL_USE_REQUESTED,
                EventContractPhase.TOOL_RESULT_RECORDED,
                EventContractPhase.PERMISSION_REQUESTED,
                EventContractPhase.PERMISSION_DECIDED,
            ),
            owner_slice=OWNER_SLICE,
            downstream_slices=("M1-02C", "M1-03A"),
            decision=CrosswalkDecision.ACTIVE_RUNTIME_CONTRACT,
            main_path_role="default CodeWorker shell exposes tool-use context and validates result-budget handoff",
            acceptance_test_entrypoints=(
                "tests/integration/test_code_worker_clean_productized_runtime.py",
                "tests/integration/test_claude_productization_integration.py",
            ),
            owner_status=SourceGraphOwnerStatus.CURRENT_SLICE,
        ),
        SourceGraphBatchContract(
            batch=ClaudeSourceGraphBatch.PERMISSION_RUNTIME_HOOKS,
            document=SourceGraphDocumentRef(
                batch=ClaudeSourceGraphBatch.PERMISSION_RUNTIME_HOOKS,
                document_path="docs/milestones/M1-runtime-memory-scheduler-fault/claude-code-source-graph/batch-03-permission-runtime-hooks.md",
                title="Permission runtime, hooks, classifier/user approval paths",
                source_summary="Allow/deny/ask rules, permission prompts, hook checks, classifier decisions, denial accounting.",
                required_signals=("permission mode", "hook gate", "user approval", "classifier", "denial limit"),
            ),
            source_refs=(
                _source("src/hooks/toolPermission", "tool permission hooks and request queue", "02A-02 exposes port; 03A owns full migration."),
                _source("src/utils/permissions", "allow/deny/ask matching", "Current ToolPermissionPolicy owns minimal semantics."),
                _source("src/query.ts", "permission interrupt in tool loop", "Mapped to query events and watchdog route."),
            ),
            zyra_targets=(
                _target("packages/runtime/zyra_runtime/permissions.py", "ToolPermissionPolicy", "current permission policy and request model", True, "ToolPermissionPolicy", True),
                _target("packages/runtime/zyra_runtime/tool_loop.py", "ToolFailureKind.PERMISSION_DENIED", "semantic denial route", True, "ToolFailureKind", True),
                _future_target("packages/runtime/zyra_runtime/claude_permission_runtime.py", "ToolPermissionRuntime", "M1-03A allow/deny/ask + hook/classifier runtime", "M1-03A"),
                _future_target("packages/runtime/zyra_runtime/claude_permission_queue.py", "PermissionRequestQueue", "M1-03A approval queue", "M1-03A"),
            ),
            runtime_ports=(
                RuntimePortKind.PERMISSION_MODE,
                RuntimePortKind.EVENT_SINK,
                RuntimePortKind.STATE_CUSTODY,
            ),
            event_phases=(
                EventContractPhase.PERMISSION_REQUESTED,
                EventContractPhase.PERMISSION_DECIDED,
            ),
            owner_slice="M1-03A",
            downstream_slices=("M1-03A", "M1-03D"),
            decision=CrosswalkDecision.DOWNSTREAM_HANDOFF,
            main_path_role="02A-02 validates permission port is present; M1-03A replaces minimal policy with full runtime",
            acceptance_test_entrypoints=(
                "tests/integration/test_code_worker_clean_productized_runtime.py::test_permission_semantics_fail_without_workspace_access",
                "tests/integration/test_claude_productization_integration.py",
            ),
            owner_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        SourceGraphBatchContract(
            batch=ClaudeSourceGraphBatch.COMPACT_CONTEXT_RESTORE,
            document=SourceGraphDocumentRef(
                batch=ClaudeSourceGraphBatch.COMPACT_CONTEXT_RESTORE,
                document_path="docs/milestones/M1-runtime-memory-scheduler-fault/claude-code-source-graph/batch-04-compact-context-restore.md",
                title="Auto compact, reactive compact, restore",
                source_summary="Context budget boundaries, compact trigger, post-compact restore, transcript continuity.",
                required_signals=("auto compact", "reactive compact", "restore", "budget state"),
            ),
            source_refs=(
                _source("src/context.ts", "context window and compact triggers", "Current context window runtime owns compact signal."),
                _source("src/query.ts", "reactive compact in query loop", "Mapped to QueryEngine context budget path."),
                _source("src/services/session.ts", "post-compact resume chain", "Mapped to QuerySession snapshot and restore."),
            ),
            zyra_targets=(
                _target("packages/runtime/zyra_runtime/claude_context_window.py", "ClaudeContextWindowManager", "context compaction and budget state", True, "ClaudeContextWindowManager", True),
                _target("packages/runtime/zyra_runtime/claude_session_lifecycle.py", "ClaudeSessionRestoreReport", "restore proof and resume plan", True, "ClaudeSessionRestoreReport", True),
                _target("packages/runtime/zyra_runtime/claude_query_engine_runtime.py", "context compaction event path", "default QueryEngine compact emission", True, "ZyraClaudeQueryEngine", True),
                _future_target("packages/runtime/zyra_runtime/claude_compact_restore_runtime.py", "CompactRestoreRuntime", "M1-02D compact/restore scheduler", "M1-02D"),
                _future_target("packages/memory/zyra_memory/skill_memory.py", "SkillMemoryRuntime", "M1-06C compact memory handoff", "M1-06C"),
            ),
            runtime_ports=(
                RuntimePortKind.CONTEXT_WINDOW,
                RuntimePortKind.COMPACT_RESTORE,
                RuntimePortKind.ARTIFACT_STORE,
                RuntimePortKind.STATE_CUSTODY,
            ),
            event_phases=(
                EventContractPhase.CONTEXT_COMPACTED,
                EventContractPhase.COMPACT_RESTORE_POINT,
            ),
            owner_slice="M1-02D",
            downstream_slices=("M1-02D", "M1-06C"),
            decision=CrosswalkDecision.DOWNSTREAM_HANDOFF,
            main_path_role="02A-02 exposes compact/restore port; 02D owns full restoration policy",
            acceptance_test_entrypoints=(
                "tests/integration/test_code_worker_clean_productized_runtime.py::test_context_budget_changes_runtime_artifacts",
                "tests/integration/test_claude_productization_integration.py",
            ),
            owner_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        SourceGraphBatchContract(
            batch=ClaudeSourceGraphBatch.MCP_RUNTIME_TOOLS_AUTH,
            document=SourceGraphDocumentRef(
                batch=ClaudeSourceGraphBatch.MCP_RUNTIME_TOOLS_AUTH,
                document_path="docs/milestones/M1-runtime-memory-scheduler-fault/claude-code-source-graph/batch-05-mcp-runtime-tools-auth.md",
                title="MCP clients, resources, tools, auth",
                source_summary="MCP server lifecycle, tool/resource/prompt projection, auth and elicitation boundaries.",
                required_signals=("mcp client", "tool projection", "resource", "prompt", "auth", "elicitation"),
            ),
            source_refs=(
                _source("src/services/mcp/client.ts", "MCP client connection and auth lifecycle", "M1-03B internalizes the lifecycle in Zyra MCP modules."),
                _source("src/tools.ts", "MCP tool projection into registry", "M1-03B owns projection runtime."),
                _source("src/commands/mcp", "MCP control commands", "M1-03B owns HTTP/live command state; M1-03D may extend CLI/TUI interaction."),
            ),
            zyra_targets=(
                _target(
                    "packages/integrations/zyra_integrations/mcp/runtime.py",
                    "McpClientRuntime",
                    "M1-03B MCP server/client composition and session state",
                    False,
                    "McpClientRuntime",
                    True,
                    owner_slice="M1-03B",
                    decision=CrosswalkDecision.ACTIVE_RUNTIME_CONTRACT,
                ),
                _target(
                    "packages/integrations/zyra_integrations/mcp/projection.py",
                    "McpToolProjectionRuntime",
                    "M1-03B MCP tool registry and permission projection",
                    False,
                    "McpToolProjectionRuntime",
                    True,
                    owner_slice="M1-03B",
                    decision=CrosswalkDecision.ACTIVE_RUNTIME_CONTRACT,
                ),
                _target(
                    "apps/api/zyra_api/mcp_api.py",
                    "McpApiFacade",
                    "M1-03B live MCP HTTP control plane",
                    False,
                    "McpApiFacade",
                    True,
                    owner_slice="M1-03B",
                    decision=CrosswalkDecision.ACTIVE_RUNTIME_CONTRACT,
                ),
                _future_target("packages/commands/zyra_commands/mcp.py", "McpControlCommands", "M1-03D MCP control command binding", "M1-03D"),
                _target("packages/runtime/zyra_runtime/claude_source_graph_crosswalk.py", "RuntimePortKind.MCP_CLIENTS", "02A-02 MCP port handoff", True, "RuntimePortKind", False),
            ),
            runtime_ports=(RuntimePortKind.MCP_CLIENTS, RuntimePortKind.TOOL_REGISTRY, RuntimePortKind.CONTROL_COMMANDS),
            event_phases=(EventContractPhase.MCP_SERVER_REGISTERED, EventContractPhase.MCP_TOOL_PROJECTED),
            owner_slice="M1-03B",
            downstream_slices=("M1-03B", "M1-03D"),
            decision=CrosswalkDecision.DOWNSTREAM_HANDOFF,
            main_path_role="02A-02 exposes the port; M1-03B now binds it to CodeWorker, 03A permission, 02D restore and live API state",
            acceptance_test_entrypoints=(
                "tests/integration/test_mcp_codeworker_permission_integration.py",
                "tests/integration/test_mcp_api_control_restore.py",
            ),
            owner_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        SourceGraphBatchContract(
            batch=ClaudeSourceGraphBatch.SKILL_PLUGIN_HOOKS,
            document=SourceGraphDocumentRef(
                batch=ClaudeSourceGraphBatch.SKILL_PLUGIN_HOOKS,
                document_path="docs/milestones/M1-runtime-memory-scheduler-fault/claude-code-source-graph/batch-06-skill-plugin-hooks.md",
                title="Skills, plugins, hooks, markdown skill execution",
                source_summary="SkillTool, markdown skills, plugin manifests/cache, hook execution and command discovery.",
                required_signals=("SkillTool", "Markdown skills", "plugin", "hook", "marketplace/cache"),
            ),
            source_refs=(
                _source("src/tools/SkillTool.ts", "SkillTool execution and skill context", "M1-03C owns skill runtime."),
                _source("src/services/plugin", "plugin manifests and cache", "M1-03C owns plugin runtime."),
                _source("src/hooks", "hook execution surfaces", "M1-03A/03C share hook routing."),
            ),
            zyra_targets=(
                _future_target("packages/skills/zyra_skills/claude_skill_runtime.py", "SkillRuntime", "M1-03C Markdown skill runtime", "M1-03C"),
                _future_target("packages/skills/zyra_skills/plugin_runtime.py", "PluginRuntime", "M1-03C plugin cache and manifest runtime", "M1-03C"),
                _future_target("packages/memory/zyra_memory/skill_memory.py", "SkillMemoryRuntime", "M1-06C skill memory bridge", "M1-06C"),
                _target("packages/runtime/zyra_runtime/claude_source_graph_crosswalk.py", "RuntimePortKind.SKILL_REGISTRY", "02A-02 skill/plugin port handoff", True, "RuntimePortKind", False),
            ),
            runtime_ports=(RuntimePortKind.SKILL_REGISTRY, RuntimePortKind.PLUGIN_REGISTRY, RuntimePortKind.EVENT_SINK),
            event_phases=(EventContractPhase.SKILL_INVOKED, EventContractPhase.PLUGIN_HOOK_FIRED),
            owner_slice="M1-03C",
            downstream_slices=("M1-03C", "M1-06C"),
            decision=CrosswalkDecision.DOWNSTREAM_HANDOFF,
            main_path_role="02A-02 records skill/plugin ports; 03C owns execution semantics",
            acceptance_test_entrypoints=("tests/integration/test_claude_productization_integration.py",),
            owner_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        SourceGraphBatchContract(
            batch=ClaudeSourceGraphBatch.AGENT_SUBAGENT_TASK_ISOLATION,
            document=SourceGraphDocumentRef(
                batch=ClaudeSourceGraphBatch.AGENT_SUBAGENT_TASK_ISOLATION,
                document_path="docs/milestones/M1-runtime-memory-scheduler-fault/claude-code-source-graph/batch-07-agent-subagent-task-isolation.md",
                title="AgentTool, subagents, tasks, worktree isolation",
                source_summary="AgentTool, subagent prompt/task lifecycle, background task tracking, worktree/remote isolation.",
                required_signals=("AgentTool", "subagent", "task isolation", "background task", "worktree"),
            ),
            source_refs=(
                _source("src/tools/AgentTool.ts", "subagent task dispatch", "M1-03D owns AgentTool runtime."),
                _source("src/services/backgroundTasks", "background task state", "M1-07A owns lifecycle runtime."),
                _source("src/services/worktree", "worktree/remote isolation", "M1-07C owns workspace manager."),
            ),
            zyra_targets=(
                _future_target("packages/runtime/zyra_runtime/claude_subagent_runtime.py", "SubagentRuntime", "M1-03D AgentTool and task dispatch", "M1-03D"),
                _future_target("packages/scheduler/zyra_scheduler/worker_lifecycle.py", "WorkerLifecycleRuntime", "M1-07A background task lifecycle", "M1-07A"),
                _future_target("packages/runtime/zyra_runtime/workspace_manager.py", "WorkspaceManager", "M1-07C workspace/worktree isolation", "M1-07C"),
                _target("packages/runtime/zyra_runtime/claude_source_graph_crosswalk.py", "RuntimePortKind.SUBAGENT_DISPATCH", "02A-02 subagent port handoff", True, "RuntimePortKind", False),
            ),
            runtime_ports=(RuntimePortKind.SUBAGENT_DISPATCH, RuntimePortKind.WORKSPACE_CWD, RuntimePortKind.EVENT_SINK),
            event_phases=(EventContractPhase.SUBAGENT_TASK_STARTED, EventContractPhase.SUBAGENT_TASK_FINISHED),
            owner_slice="M1-03D",
            downstream_slices=("M1-03D", "M1-07A", "M1-07C"),
            decision=CrosswalkDecision.DOWNSTREAM_HANDOFF,
            main_path_role="02A-02 exposes subagent dispatch contract; 03D/07A/07C own behavior",
            acceptance_test_entrypoints=("tests/integration/test_claude_productization_integration.py",),
            owner_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        SourceGraphBatchContract(
            batch=ClaudeSourceGraphBatch.API_STREAMING_RETRY_CLIENT,
            document=SourceGraphDocumentRef(
                batch=ClaudeSourceGraphBatch.API_STREAMING_RETRY_CLIENT,
                document_path="docs/milestones/M1-runtime-memory-scheduler-fault/claude-code-source-graph/batch-08-api-streaming-retry-client.md",
                title="API streaming, retry, model client",
                source_summary="Raw SSE/state-machine streaming, retry matrix, fallback, budget/error propagation.",
                required_signals=("streaming", "retry", "failover", "budget", "raw SSE"),
            ),
            source_refs=(
                _source("src/api.ts", "model client request/stream handling", "M1-02D owns stream runtime."),
                _source("src/query.ts", "retry and error propagation", "M1-05D owns backend failover."),
                _source("src/services/cost.ts", "budget updates", "M1-02D/05D own budget integration."),
            ),
            zyra_targets=(
                _future_target("packages/runtime/zyra_runtime/claude_model_stream_runtime.py", "ModelStreamRuntime", "M1-02D model stream state", "M1-02D"),
                _future_target("packages/runtime/zyra_runtime/runtime_budget_state.py", "RuntimeBudgetState", "M1-02D budget bridge", "M1-02D"),
                _future_target("packages/runtime/zyra_runtime/backend_failover.py", "BackendFailoverRuntime", "M1-05D retry/failover path", "M1-05D"),
                _target("packages/runtime/zyra_runtime/claude_runtime_contracts.py", "session_contract.streamRuntime", "02A-01 contract imported into 02A-02 crosswalk", True, "ClaudeRuntimeContractBundle", False),
            ),
            runtime_ports=(RuntimePortKind.MODEL_STREAM, RuntimePortKind.STATE_CUSTODY, RuntimePortKind.EVENT_SINK),
            event_phases=(EventContractPhase.MODEL_STREAM_DELTA, EventContractPhase.RETRY_DECISION),
            owner_slice="M1-02D",
            downstream_slices=("M1-02D", "M1-05D", "M1-07C"),
            decision=CrosswalkDecision.DOWNSTREAM_HANDOFF,
            main_path_role="02A-02 carries stream/retry contract metadata; 02D/05D own runtime",
            acceptance_test_entrypoints=("tests/integration/test_claude_productization_integration.py",),
            owner_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        SourceGraphBatchContract(
            batch=ClaudeSourceGraphBatch.TUI_CLI_COMMANDS_CONTROL,
            document=SourceGraphDocumentRef(
                batch=ClaudeSourceGraphBatch.TUI_CLI_COMMANDS_CONTROL,
                document_path="docs/milestones/M1-runtime-memory-scheduler-fault/claude-code-source-graph/batch-09-tui-cli-commands-control.md",
                title="TUI/CLI commands and runtime control",
                source_summary="context, compact, cost, diff, doctor, mcp, memory, permissions, resume, skills, tasks commands.",
                required_signals=("control commands", "prompt queue", "runtime dispatcher", "TUI panels"),
            ),
            source_refs=(
                _source("src/commands.ts", "command registry and route metadata", "Mapped to ClaudeControlCommandRuntime and downstream control registry."),
                _source("src/coordinator", "coordinator mode/control handoff", "M1-03D owns runtime control dispatcher."),
                _source("src/tui", "TUI panels and stream state", "M2 owns frontend productization."),
            ),
            zyra_targets=(
                _target("packages/runtime/zyra_runtime/claude_control_commands.py", "ClaudeControlCommandRuntime", "current runtime command report path", True, "ClaudeControlCommandRuntime", True),
                _target("packages/commands/zyra_commands", "default_command_registry", "existing slash command registry", True, "default_command_registry", False),
                _target("apps/api/zyra_api/main.py", "/workers/code/inventory", "02A-02 API inventory and CodeWorker task entry", True, "ZyraRequestHandler", True),
                _future_target("packages/runtime/zyra_runtime/control_command_registry.py", "ControlCommandRegistry", "M1-03D runtime command registry", "M1-03D"),
                _future_target("packages/runtime/zyra_runtime/prompt_queue_runtime.py", "PromptQueueRuntime", "M1-05C prompt queue control", "M1-05C"),
                _future_target("apps/web/src", "Runtime control panels", "M2 console and event stream UX", "M2"),
            ),
            runtime_ports=(RuntimePortKind.CONTROL_COMMANDS, RuntimePortKind.EVENT_SINK, RuntimePortKind.ARTIFACT_STORE),
            event_phases=(EventContractPhase.CONTROL_COMMAND_RECEIVED, EventContractPhase.CONTROL_COMMAND_APPLIED),
            owner_slice="M1-03D",
            downstream_slices=("M1-03D", "M1-05C", "M2"),
            decision=CrosswalkDecision.ACTIVE_RUNTIME_CONTRACT,
            main_path_role="current CodeWorker emits command events and artifacts; downstream slices harden registry/UI",
            acceptance_test_entrypoints=(
                "tests/integration/test_code_worker_clean_productized_runtime.py::test_control_commands_and_semantic_runtime_enter_default_path",
                "tests/integration/test_claude_productization_integration.py",
            ),
            owner_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
    )


def _default_runtime_context_ports() -> tuple[RuntimeContextPortContract, ...]:
    return (
        RuntimeContextPortContract(
            port_id="session_lifecycle",
            kind=RuntimePortKind.SESSION_LIFECYCLE,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="ClaudeSessionLifecycleRuntime",
            consumer="CodeWorkerRuntime",
            state_owner="QuerySession",
            contract_fields=("session_id", "turn_id", "resume_token", "transcript_uri", "snapshot_artifact_id"),
            event_phases=(EventContractPhase.SESSION_INIT, EventContractPhase.COMPACT_RESTORE_POINT),
            failure_code="runtime_context_session_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="workspace_cwd",
            kind=RuntimePortKind.WORKSPACE_CWD,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="ToolExecutionContext.for_workspace",
            consumer="ToolExecutor",
            state_owner="ToolExecutionContext",
            contract_fields=("workspace_root", "artifact_root", "permission_store"),
            event_phases=(EventContractPhase.TOOL_USE_REQUESTED, EventContractPhase.TOOL_RESULT_RECORDED),
            failure_code="runtime_context_workspace_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="tool_registry",
            kind=RuntimePortKind.TOOL_REGISTRY,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="default_tool_registry",
            consumer="ZyraClaudeQueryEngine",
            state_owner="ToolRegistry",
            contract_fields=("tool_name", "input_schema", "access_mode", "execution_mode"),
            event_phases=(EventContractPhase.TOOL_USE_REQUESTED,),
            failure_code="runtime_context_tool_registry_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="tool_executor",
            kind=RuntimePortKind.TOOL_EXECUTOR,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="ToolExecutor",
            consumer="ZyraClaudeQueryEngine",
            state_owner="ToolExecutor",
            contract_fields=("tool_call_id", "tool_name", "arguments", "permission_decision", "result"),
            event_phases=(EventContractPhase.TOOL_USE_REQUESTED, EventContractPhase.TOOL_RESULT_RECORDED),
            failure_code="runtime_context_tool_executor_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="tool_result_budget",
            kind=RuntimePortKind.TOOL_RESULT_BUDGET,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="ToolResultBudgeter",
            consumer="ZyraClaudeQueryEngine",
            state_owner="ToolLoopScheduler",
            contract_fields=("max_result_chars", "externalized_artifact_id", "truncated", "budget_signal"),
            event_phases=(EventContractPhase.TOOL_RESULT_RECORDED,),
            failure_code="runtime_context_tool_budget_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="permission_mode",
            kind=RuntimePortKind.PERMISSION_MODE,
            owner_slice="M1-03A",
            status=RuntimePortStatus.CONTRACT_READY,
            producer="ToolPermissionPolicy",
            consumer="ToolExecutor",
            state_owner="JsonPermissionStore",
            contract_fields=("permission_mode", "operation", "effect", "request_id", "decision"),
            event_phases=(EventContractPhase.PERMISSION_REQUESTED, EventContractPhase.PERMISSION_DECIDED),
            failure_code="runtime_context_permission_port_disabled",
            required_for_runtime_shell=True,
            downstream_owner="M1-03A",
        ),
        RuntimeContextPortContract(
            port_id="context_window",
            kind=RuntimePortKind.CONTEXT_WINDOW,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="ClaudeContextWindowManager",
            consumer="ZyraClaudeQueryEngine",
            state_owner="ClaudeContextWindowManager",
            contract_fields=("budget_chars", "selected_blocks", "compaction_id", "restore_plan"),
            event_phases=(EventContractPhase.CONTEXT_COMPACTED,),
            failure_code="runtime_context_context_window_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="compact_restore",
            kind=RuntimePortKind.COMPACT_RESTORE,
            owner_slice="M1-02D",
            status=RuntimePortStatus.CONTRACT_READY,
            producer="ClaudeSessionLifecycleRuntime",
            consumer="CompactRestoreRuntime",
            state_owner="QuerySession",
            contract_fields=("resume_token", "snapshot_artifact_id", "compact_artifact_id", "restore_status"),
            event_phases=(EventContractPhase.CONTEXT_COMPACTED, EventContractPhase.COMPACT_RESTORE_POINT),
            failure_code="runtime_context_compact_restore_port_disabled",
            required_for_runtime_shell=False,
            downstream_owner="M1-02D",
        ),
        RuntimeContextPortContract(
            port_id="mcp_clients",
            kind=RuntimePortKind.MCP_CLIENTS,
            owner_slice="M1-03B",
            status=RuntimePortStatus.CONTRACT_READY,
            producer="McpRuntimeStore",
            consumer="ToolRegistryRuntime",
            state_owner="McpRuntimeStore",
            contract_fields=("server_id", "transport", "auth_state", "projected_tools", "resources"),
            event_phases=(EventContractPhase.MCP_SERVER_REGISTERED, EventContractPhase.MCP_TOOL_PROJECTED),
            failure_code="runtime_context_mcp_port_disabled",
            required_for_runtime_shell=False,
            downstream_owner="M1-03B",
        ),
        RuntimeContextPortContract(
            port_id="skill_registry",
            kind=RuntimePortKind.SKILL_REGISTRY,
            owner_slice="M1-03C",
            status=RuntimePortStatus.CONTRACT_READY,
            producer="SkillRuntime",
            consumer="ToolRegistryRuntime",
            state_owner="SkillRuntime",
            contract_fields=("skill_id", "skill_path", "activation_context", "tool_call_id"),
            event_phases=(EventContractPhase.SKILL_INVOKED,),
            failure_code="runtime_context_skill_port_disabled",
            required_for_runtime_shell=False,
            downstream_owner="M1-03C",
        ),
        RuntimeContextPortContract(
            port_id="plugin_registry",
            kind=RuntimePortKind.PLUGIN_REGISTRY,
            owner_slice="M1-03C",
            status=RuntimePortStatus.CONTRACT_READY,
            producer="PluginRuntime",
            consumer="SkillRuntime",
            state_owner="PluginRuntime",
            contract_fields=("plugin_id", "manifest", "cache_key", "hook_bindings"),
            event_phases=(EventContractPhase.PLUGIN_HOOK_FIRED,),
            failure_code="runtime_context_plugin_port_disabled",
            required_for_runtime_shell=False,
            downstream_owner="M1-03C",
        ),
        RuntimeContextPortContract(
            port_id="subagent_dispatch",
            kind=RuntimePortKind.SUBAGENT_DISPATCH,
            owner_slice="M1-03D",
            status=RuntimePortStatus.CONTRACT_READY,
            producer="SubagentRuntime",
            consumer="CodeWorkerRuntime",
            state_owner="SubagentRuntime",
            contract_fields=("agent_id", "task_id", "workspace_id", "status", "result_artifact_id"),
            event_phases=(EventContractPhase.SUBAGENT_TASK_STARTED, EventContractPhase.SUBAGENT_TASK_FINISHED),
            failure_code="runtime_context_subagent_port_disabled",
            required_for_runtime_shell=False,
            downstream_owner="M1-03D",
        ),
        RuntimeContextPortContract(
            port_id="control_commands",
            kind=RuntimePortKind.CONTROL_COMMANDS,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="ClaudeControlCommandRuntime",
            consumer="CodeWorkerRuntime",
            state_owner="ClaudeControlRuntimeState",
            contract_fields=("command_id", "name", "arguments", "status", "artifact_policy"),
            event_phases=(EventContractPhase.CONTROL_COMMAND_RECEIVED, EventContractPhase.CONTROL_COMMAND_APPLIED),
            failure_code="runtime_context_control_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="model_stream",
            kind=RuntimePortKind.MODEL_STREAM,
            owner_slice="M1-02D",
            status=RuntimePortStatus.CONTRACT_READY,
            producer="ModelStreamRuntime",
            consumer="ZyraClaudeQueryEngine",
            state_owner="RuntimeBudgetState",
            contract_fields=("stream_id", "delta", "retry_count", "backend", "budget_state"),
            event_phases=(EventContractPhase.MODEL_STREAM_DELTA, EventContractPhase.RETRY_DECISION),
            failure_code="runtime_context_model_stream_port_disabled",
            required_for_runtime_shell=False,
            downstream_owner="M1-02D",
        ),
        RuntimeContextPortContract(
            port_id="event_sink",
            kind=RuntimePortKind.EVENT_SINK,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="CodeWorkerRuntime",
            consumer="SQLiteStore/event_log",
            state_owner="EventRecord",
            contract_fields=("run_id", "task_id", "node_id", "event_type", "payload"),
            event_phases=(EventContractPhase.SOURCE_GRAPH_CROSSWALK_READY, EventContractPhase.INTEGRATION_GATE_PASSED),
            failure_code="runtime_context_event_sink_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="artifact_store",
            kind=RuntimePortKind.ARTIFACT_STORE,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="LocalArtifactStore",
            consumer="CodeWorkerRuntime",
            state_owner="LocalArtifactStore",
            contract_fields=("artifact_id", "kind", "uri", "title", "producer_node_id"),
            event_phases=(EventContractPhase.TOOL_RESULT_RECORDED, EventContractPhase.CONTROL_COMMAND_APPLIED),
            failure_code="runtime_context_artifact_store_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="source_graph",
            kind=RuntimePortKind.SOURCE_GRAPH,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="ClaudeSourceGraphCrosswalk",
            consumer="CodeWorkerRuntime",
            state_owner="ClaudeSourceGraphCrosswalk",
            contract_fields=("batch_id", "source_ref", "target_path", "owner_slice", "handoff_status"),
            event_phases=(
                EventContractPhase.SOURCE_GRAPH_CROSSWALK_READY,
                EventContractPhase.RUNTIME_CONTEXT_READY,
                EventContractPhase.DOWNSTREAM_CONTRACTS_READY,
            ),
            failure_code="runtime_context_source_graph_port_disabled",
            required_for_runtime_shell=True,
        ),
        RuntimeContextPortContract(
            port_id="state_custody",
            kind=RuntimePortKind.STATE_CUSTODY,
            owner_slice=OWNER_SLICE,
            status=RuntimePortStatus.ACTIVE,
            producer="ClaudeRuntimeStateLedger",
            consumer="CodeWorkerRuntime",
            state_owner="ClaudeRuntimeStateLedger",
            contract_fields=("scope", "mutation_kind", "owner", "artifact_id", "causality"),
            event_phases=(EventContractPhase.INTEGRATION_GATE_PASSED,),
            failure_code="runtime_context_state_custody_port_disabled",
            required_for_runtime_shell=True,
        ),
    )


def _default_tool_use_context_ports() -> tuple[ToolUseContextPortContract, ...]:
    return (
        ToolUseContextPortContract(
            port_id="tool_use_context.default",
            tool_surface="ToolCall -> ToolLoopScheduler -> ToolExecutor",
            registry_entrypoint="default_tool_registry",
            permission_surface="ToolPermissionPolicy.check",
            budget_surface="ToolResultBudgeter.apply",
            result_surface="tool_result_event/query_session tool_result phase",
            current_slice_contract="CodeWorkerRuntime injects ToolExecutionContext and QueryEngine config before tool execution.",
            downstream_owner="M1-02C/M1-03A",
            event_phases=(
                EventContractPhase.TOOL_USE_REQUESTED,
                EventContractPhase.PERMISSION_REQUESTED,
                EventContractPhase.PERMISSION_DECIDED,
                EventContractPhase.TOOL_RESULT_RECORDED,
            ),
        ),
        ToolUseContextPortContract(
            port_id="tool_use_context.mcp_projection",
            tool_surface="MCP tool projection -> ToolRegistryRuntime",
            registry_entrypoint="McpToolProjectionRuntime",
            permission_surface="ToolPermissionRuntime/MCP auth",
            budget_surface="ToolResultBudgetRuntime",
            result_surface="MCP tool result artifact or inline result",
            current_slice_contract="02A-02 exposes source graph contract only; no MCP sidecar is counted as completion.",
            downstream_owner="M1-03B",
            event_phases=(EventContractPhase.MCP_SERVER_REGISTERED, EventContractPhase.MCP_TOOL_PROJECTED),
            required_for_runtime_shell=False,
        ),
        ToolUseContextPortContract(
            port_id="tool_use_context.skill_agent",
            tool_surface="SkillTool/AgentTool projection -> ToolRegistryRuntime",
            registry_entrypoint="SkillRuntime/SubagentRuntime",
            permission_surface="ToolPermissionRuntime",
            budget_surface="ToolResultBudgetRuntime",
            result_surface="skill/subagent artifact and task event",
            current_slice_contract="02A-02 records SkillTool/AgentTool handoff as downstream runtime ports.",
            downstream_owner="M1-03C/M1-03D",
            event_phases=(
                EventContractPhase.SKILL_INVOKED,
                EventContractPhase.SUBAGENT_TASK_STARTED,
                EventContractPhase.SUBAGENT_TASK_FINISHED,
            ),
            required_for_runtime_shell=False,
        ),
    )


def _default_event_contracts() -> tuple[EventContract, ...]:
    return (
        _event(EventContractPhase.SESSION_INIT, OWNER_SLICE, "query_session", ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT, "ZyraClaudeQueryEngine", "event log", ("session_id", "phase"), True),
        _event(EventContractPhase.MESSAGE_RECEIVED, OWNER_SLICE, "query_session", ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT, "ZyraClaudeQueryEngine", "event log", ("message_id", "role", "phase"), True),
        _event(EventContractPhase.TOOL_USE_REQUESTED, OWNER_SLICE, "query_session", ClaudeSourceGraphBatch.QUERY_TOOL_LOOP, "ToolLoopScheduler", "event log", ("tool_call_id", "tool_name", "phase"), True),
        _event(EventContractPhase.TOOL_RESULT_RECORDED, OWNER_SLICE, "query_session", ClaudeSourceGraphBatch.QUERY_TOOL_LOOP, "ToolExecutor", "event log", ("tool_call_id", "ok", "phase"), True),
        _event(EventContractPhase.PERMISSION_REQUESTED, "M1-03A", "query_session", ClaudeSourceGraphBatch.PERMISSION_RUNTIME_HOOKS, "ToolPermissionPolicy", "PermissionRequestQueue", ("operation", "path", "request_id"), True, "M1-03A"),
        _event(EventContractPhase.PERMISSION_DECIDED, "M1-03A", "query_session", ClaudeSourceGraphBatch.PERMISSION_RUNTIME_HOOKS, "ToolPermissionPolicy", "event log", ("effect", "reason", "phase"), True, "M1-03A"),
        _event(EventContractPhase.CONTEXT_COMPACTED, OWNER_SLICE, "query_session", ClaudeSourceGraphBatch.COMPACT_CONTEXT_RESTORE, "ClaudeContextWindowManager", "event log", ("artifact_id", "budget_chars", "phase"), True),
        _event(EventContractPhase.COMPACT_RESTORE_POINT, "M1-02D", "query_session", ClaudeSourceGraphBatch.COMPACT_CONTEXT_RESTORE, "ClaudeSessionLifecycleRuntime", "CompactRestoreRuntime", ("resume_token", "snapshot_artifact_id"), True, "M1-02D"),
        _event(EventContractPhase.MCP_SERVER_REGISTERED, "M1-03B", "mcp_runtime", ClaudeSourceGraphBatch.MCP_RUNTIME_TOOLS_AUTH, "McpRuntimeStore", "event log", ("server_id", "transport", "auth_state"), False, "M1-03B"),
        _event(EventContractPhase.MCP_TOOL_PROJECTED, "M1-03B", "mcp_runtime", ClaudeSourceGraphBatch.MCP_RUNTIME_TOOLS_AUTH, "McpToolProjectionRuntime", "ToolRegistryRuntime", ("server_id", "tool_name", "schema"), False, "M1-03B"),
        _event(EventContractPhase.SKILL_INVOKED, "M1-03C", "skill_runtime", ClaudeSourceGraphBatch.SKILL_PLUGIN_HOOKS, "SkillRuntime", "event log", ("skill_id", "tool_call_id", "artifact_id"), False, "M1-03C"),
        _event(EventContractPhase.PLUGIN_HOOK_FIRED, "M1-03C", "plugin_runtime", ClaudeSourceGraphBatch.SKILL_PLUGIN_HOOKS, "PluginRuntime", "event log", ("plugin_id", "hook_name", "result"), False, "M1-03C"),
        _event(EventContractPhase.SUBAGENT_TASK_STARTED, "M1-03D", "subagent_runtime", ClaudeSourceGraphBatch.AGENT_SUBAGENT_TASK_ISOLATION, "SubagentRuntime", "event log", ("agent_id", "task_id", "workspace_id"), False, "M1-03D"),
        _event(EventContractPhase.SUBAGENT_TASK_FINISHED, "M1-03D", "subagent_runtime", ClaudeSourceGraphBatch.AGENT_SUBAGENT_TASK_ISOLATION, "SubagentRuntime", "event log", ("agent_id", "task_id", "status"), False, "M1-03D"),
        _event(EventContractPhase.CONTROL_COMMAND_RECEIVED, OWNER_SLICE, "query_session", ClaudeSourceGraphBatch.TUI_CLI_COMMANDS_CONTROL, "ClaudeControlCommandRuntime", "event log", ("command_id", "name", "phase"), True),
        _event(EventContractPhase.CONTROL_COMMAND_APPLIED, OWNER_SLICE, "query_session", ClaudeSourceGraphBatch.TUI_CLI_COMMANDS_CONTROL, "ClaudeControlCommandRuntime", "artifact store", ("command_id", "status", "artifact_id"), True),
        _event(EventContractPhase.MODEL_STREAM_DELTA, "M1-02D", "model_stream", ClaudeSourceGraphBatch.API_STREAMING_RETRY_CLIENT, "ModelStreamRuntime", "event log", ("stream_id", "delta", "budget_state"), False, "M1-02D"),
        _event(EventContractPhase.RETRY_DECISION, "M1-05D", "model_stream", ClaudeSourceGraphBatch.API_STREAMING_RETRY_CLIENT, "BackendFailoverRuntime", "event log", ("backend", "attempt", "decision"), False, "M1-05D"),
        _event(EventContractPhase.SOURCE_GRAPH_CROSSWALK_READY, OWNER_SLICE, "claude_productization_integration", ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT, "ClaudeSourceGraphCrosswalk", "CodeWorkerRuntime", ("contract_id", "batch_count", "ok"), True),
        _event(EventContractPhase.RUNTIME_CONTEXT_READY, OWNER_SLICE, "claude_productization_integration", ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT, "ClaudeSourceGraphCrosswalk", "CodeWorkerRuntime", ("runtime_context_port_count", "ok"), True),
        _event(EventContractPhase.DOWNSTREAM_CONTRACTS_READY, OWNER_SLICE, "claude_productization_integration", ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT, "ClaudeSourceGraphCrosswalk", "CodeWorkerRuntime", ("downstream_contract_count", "ok"), True),
        _event(EventContractPhase.INTEGRATION_GATE_PASSED, OWNER_SLICE, "claude_productization_integration", ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT, "ClaudeSourceGraphCrosswalk", "CodeWorkerRuntime", ("contract_id", "ok"), True),
        _event(EventContractPhase.INTEGRATION_GATE_BLOCKED, OWNER_SLICE, "claude_productization_integration", ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT, "ClaudeSourceGraphCrosswalk", "CodeWorkerRuntime", ("contract_id", "blocking_error"), True),
    )


def _default_downstream_contracts() -> tuple[DownstreamContract, ...]:
    return (
        DownstreamContract(
            contract_id="M1-02B.query-session-lifecycle",
            owner_slice="M1-02B",
            target_unit="unit-02b-query-session-lifecycle",
            capability="CodeWorkerSessionStore, ContextAssemblyRuntime, resume/restore contract",
            source_batches=(ClaudeSourceGraphBatch.QUERY_SESSION_CONTEXT,),
            required_ports=(RuntimePortKind.SESSION_LIFECYCLE, RuntimePortKind.CONTEXT_WINDOW, RuntimePortKind.EVENT_SINK),
            required_events=(EventContractPhase.SESSION_INIT, EventContractPhase.MESSAGE_RECEIVED, EventContractPhase.COMPACT_RESTORE_POINT),
            required_targets=("packages/runtime/zyra_runtime/claude_context_assembly_runtime.py",),
            acceptance_test_entrypoints=("tests/integration/test_claude_query_session_lifecycle.py",),
            handoff_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        DownstreamContract(
            contract_id="M1-02C.tool-loop-budget",
            owner_slice="M1-02C",
            target_unit="unit-02c-tool-loop-budget",
            capability="ToolRegistryRuntime, ToolExecutionRuntime, ToolResultBudgetRuntime",
            source_batches=(ClaudeSourceGraphBatch.QUERY_TOOL_LOOP,),
            required_ports=(RuntimePortKind.TOOL_REGISTRY, RuntimePortKind.TOOL_EXECUTOR, RuntimePortKind.TOOL_RESULT_BUDGET),
            required_events=(EventContractPhase.TOOL_USE_REQUESTED, EventContractPhase.TOOL_RESULT_RECORDED),
            required_targets=(
                "packages/runtime/zyra_runtime/claude_tool_registry_runtime.py",
                "packages/runtime/zyra_runtime/claude_tool_result_budget_runtime.py",
            ),
            acceptance_test_entrypoints=("tests/integration/test_claude_tool_loop_budget.py",),
            handoff_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        DownstreamContract(
            contract_id="M1-02D.compact-stream-budget",
            owner_slice="M1-02D",
            target_unit="unit-02d-compact-stream-budget",
            capability="CompactRestoreRuntime, ModelStreamRuntime, RuntimeBudgetState",
            source_batches=(ClaudeSourceGraphBatch.COMPACT_CONTEXT_RESTORE, ClaudeSourceGraphBatch.API_STREAMING_RETRY_CLIENT),
            required_ports=(RuntimePortKind.COMPACT_RESTORE, RuntimePortKind.MODEL_STREAM, RuntimePortKind.STATE_CUSTODY),
            required_events=(EventContractPhase.CONTEXT_COMPACTED, EventContractPhase.MODEL_STREAM_DELTA, EventContractPhase.RETRY_DECISION),
            required_targets=(
                "packages/runtime/zyra_runtime/claude_compact_restore_runtime.py",
                "packages/runtime/zyra_runtime/claude_model_stream_runtime.py",
                "packages/runtime/zyra_runtime/runtime_budget_state.py",
            ),
            acceptance_test_entrypoints=("tests/integration/test_claude_compact_stream_budget.py",),
            handoff_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        DownstreamContract(
            contract_id="M1-03A.permission-runtime",
            owner_slice="M1-03A",
            target_unit="unit-03a-permission-runtime",
            capability="ToolPermissionRuntime, PermissionRequestQueue, hook/classifier/user approval path",
            source_batches=(ClaudeSourceGraphBatch.PERMISSION_RUNTIME_HOOKS,),
            required_ports=(RuntimePortKind.PERMISSION_MODE, RuntimePortKind.EVENT_SINK),
            required_events=(EventContractPhase.PERMISSION_REQUESTED, EventContractPhase.PERMISSION_DECIDED),
            required_targets=(
                "packages/runtime/zyra_runtime/claude_permission_runtime.py",
                "packages/runtime/zyra_runtime/claude_permission_queue.py",
            ),
            acceptance_test_entrypoints=("tests/integration/test_claude_permission_runtime.py",),
            handoff_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        DownstreamContract(
            contract_id="M1-03B.mcp-runtime",
            owner_slice="M1-03B",
            target_unit="unit-03b-mcp-client-runtime",
            capability="McpClientRuntime, McpToolProjectionRuntime, auth/resource/prompt/elicitation/task/restore lifecycle",
            source_batches=(ClaudeSourceGraphBatch.MCP_RUNTIME_TOOLS_AUTH,),
            required_ports=(RuntimePortKind.MCP_CLIENTS, RuntimePortKind.TOOL_REGISTRY, RuntimePortKind.PERMISSION_MODE),
            required_events=(EventContractPhase.MCP_SERVER_REGISTERED, EventContractPhase.MCP_TOOL_PROJECTED),
            required_targets=(
                "packages/integrations/zyra_integrations/mcp/runtime.py",
                "packages/integrations/zyra_integrations/mcp/projection.py",
                "apps/api/zyra_api/mcp_api.py",
            ),
            acceptance_test_entrypoints=(
                "tests/integration/test_mcp_codeworker_permission_integration.py",
                "tests/integration/test_mcp_api_control_restore.py",
            ),
            handoff_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        DownstreamContract(
            contract_id="M1-03C.skill-plugin-runtime",
            owner_slice="M1-03C",
            target_unit="unit-03c-skill-plugin-runtime",
            capability="SkillRuntime, PluginRuntime, Markdown SkillTool, hook/cache lifecycle",
            source_batches=(ClaudeSourceGraphBatch.SKILL_PLUGIN_HOOKS,),
            required_ports=(RuntimePortKind.SKILL_REGISTRY, RuntimePortKind.PLUGIN_REGISTRY, RuntimePortKind.TOOL_REGISTRY),
            required_events=(EventContractPhase.SKILL_INVOKED, EventContractPhase.PLUGIN_HOOK_FIRED),
            required_targets=(
                "packages/skills/zyra_skills/claude_skill_runtime.py",
                "packages/skills/zyra_skills/plugin_runtime.py",
            ),
            acceptance_test_entrypoints=("tests/integration/test_claude_skill_plugin_runtime.py",),
            handoff_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        DownstreamContract(
            contract_id="M1-03D.control-agent-runtime",
            owner_slice="M1-03D",
            target_unit="unit-03d-control-agent-runtime",
            capability="ControlCommandRegistry, SubagentRuntime, runtime dispatcher",
            source_batches=(ClaudeSourceGraphBatch.AGENT_SUBAGENT_TASK_ISOLATION, ClaudeSourceGraphBatch.TUI_CLI_COMMANDS_CONTROL),
            required_ports=(RuntimePortKind.CONTROL_COMMANDS, RuntimePortKind.SUBAGENT_DISPATCH, RuntimePortKind.WORKSPACE_CWD),
            required_events=(
                EventContractPhase.CONTROL_COMMAND_RECEIVED,
                EventContractPhase.CONTROL_COMMAND_APPLIED,
                EventContractPhase.SUBAGENT_TASK_STARTED,
                EventContractPhase.SUBAGENT_TASK_FINISHED,
            ),
            required_targets=(
                "packages/runtime/zyra_runtime/control_command_registry.py",
                "packages/runtime/zyra_runtime/claude_subagent_runtime.py",
            ),
            acceptance_test_entrypoints=("tests/integration/test_claude_control_agent_runtime.py",),
            handoff_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
        DownstreamContract(
            contract_id="M2.console-runtime-control",
            owner_slice="M2",
            target_unit="frontend-runtime-console",
            capability="event stream, artifacts/diff/browser/terminal/permission/control panels",
            source_batches=(ClaudeSourceGraphBatch.TUI_CLI_COMMANDS_CONTROL, ClaudeSourceGraphBatch.API_STREAMING_RETRY_CLIENT),
            required_ports=(RuntimePortKind.CONTROL_COMMANDS, RuntimePortKind.EVENT_SINK, RuntimePortKind.ARTIFACT_STORE),
            required_events=(EventContractPhase.CONTROL_COMMAND_RECEIVED, EventContractPhase.MODEL_STREAM_DELTA),
            required_targets=("apps/web/src",),
            acceptance_test_entrypoints=("tests/integration/test_runtime_console_api.py",),
            handoff_status=SourceGraphOwnerStatus.DOWNSTREAM_READY_CONTRACT,
        ),
    )


def _source(source_path: str, symbol_or_region: str, migration_note: str, required: bool = False) -> UpstreamSourceRef:
    return UpstreamSourceRef(
        repo=PRIMARY_SOURCE_REPO,
        source_path=source_path,
        symbol_or_region=symbol_or_region,
        migration_note=migration_note,
        required_for_current_slice=required,
    )


def _target(
    path: str,
    symbol_or_entrypoint: str,
    runtime_role: str,
    current_slice_required: bool,
    import_symbol: str,
    main_path_required: bool,
    *,
    owner_slice: str = OWNER_SLICE,
    decision: CrosswalkDecision = CrosswalkDecision.ZYRA_MODULE_MIGRATED,
) -> ZyraTargetRef:
    return ZyraTargetRef(
        path=path,
        symbol_or_entrypoint=symbol_or_entrypoint,
        owner_slice=owner_slice,
        runtime_role=runtime_role,
        decision=decision,
        current_slice_required=current_slice_required,
        import_symbol=import_symbol,
        main_path_required=main_path_required,
    )


def _future_target(path: str, symbol_or_entrypoint: str, runtime_role: str, owner_slice: str) -> ZyraTargetRef:
    return ZyraTargetRef(
        path=path,
        symbol_or_entrypoint=symbol_or_entrypoint,
        owner_slice=owner_slice,
        runtime_role=runtime_role,
        decision=CrosswalkDecision.DOWNSTREAM_HANDOFF,
        current_slice_required=False,
        import_symbol="",
        main_path_required=False,
    )


def _event(
    phase: EventContractPhase,
    owner_slice: str,
    payload_key: str,
    source_batch: ClaudeSourceGraphBatch,
    producer: str,
    consumer: str,
    required_fields: tuple[str, ...],
    currently_emitted: bool,
    downstream_owner: str = "",
) -> EventContract:
    return EventContract(
        phase=phase,
        owner_slice=owner_slice,
        payload_key=payload_key,
        source_batch=source_batch,
        producer=producer,
        consumer=consumer,
        required_fields=required_fields,
        currently_emitted=currently_emitted,
        downstream_owner=downstream_owner,
    )


def _validate_target_ref(
    target: ZyraTargetRef,
    *,
    batch: ClaudeSourceGraphBatch,
    project_root: Path | None,
    require_exists: bool,
) -> list[CrosswalkFinding]:
    findings: list[CrosswalkFinding] = []
    if target.is_source_pool:
        findings.append(
            CrosswalkFinding(
                severity=CrosswalkFindingSeverity.BLOCKER,
                code="source_pool_target_path",
                message=f"Target path {target.path} is source-pool/vendor-like and cannot count as internalized code.",
                batch=batch,
                target_path=target.path,
            )
        )
    if not target.is_product_module:
        findings.append(
            CrosswalkFinding(
                severity=CrosswalkFindingSeverity.WARNING,
                code="target_not_product_module",
                message=f"Target path {target.path} is outside apps/packages/skills/scripts.",
                batch=batch,
                target_path=target.path,
            )
        )
    if target.current_slice_required and require_exists and project_root is not None:
        target_path = project_root / target.path
        if not target_path.exists():
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="current_slice_target_missing",
                    message=f"Required current-slice target {target.path} does not exist.",
                    batch=batch,
                    target_path=target.path,
                )
            )
    return findings


def _validate_foundation_contract_alignment(
    crosswalk: ClaudeSourceGraphCrosswalk,
    runtime_contracts: ClaudeRuntimeContractBundle,
) -> list[CrosswalkFinding]:
    findings: list[CrosswalkFinding] = []
    if runtime_contracts.clean_runtime_safe is not True:
        findings.append(
            CrosswalkFinding(
                severity=CrosswalkFindingSeverity.BLOCKER,
                code="foundation_runtime_not_clean_safe",
                message="02A-01 runtime contracts are not clean-runtime safe.",
            )
        )
    required_surfaces = {
        ClaudeRuntimeSurface.QUERY_ENGINE,
        ClaudeRuntimeSurface.QUERY_LOOP,
        ClaudeRuntimeSurface.TOOL_REGISTRY,
        ClaudeRuntimeSurface.TOOL_EXECUTOR,
        ClaudeRuntimeSurface.TOOL_RESULT_BUDGET,
        ClaudeRuntimeSurface.PERMISSION_RUNTIME,
        ClaudeRuntimeSurface.SESSION_LIFECYCLE,
        ClaudeRuntimeSurface.CONTEXT_ASSEMBLY,
        ClaudeRuntimeSurface.WORKER_ENTRY,
    }
    actual_surfaces = {item.surface for item in runtime_contracts.source_to_target}
    for missing in sorted(str(surface) for surface in required_surfaces - actual_surfaces):
        findings.append(
            CrosswalkFinding(
                severity=CrosswalkFindingSeverity.BLOCKER,
                code="foundation_surface_missing",
                message=f"02A-01 contract bundle is missing runtime surface {missing}.",
            )
        )
    migrated_or_contract = {
        ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
        ClaudeRuntimeDecision.ADAPTER_ENCAPSULATED,
        ClaudeRuntimeDecision.CONTRACT_ONLY,
    }
    default_path_refs = [item for item in runtime_contracts.source_to_target if item.required_for_default_path]
    if not default_path_refs:
        findings.append(
            CrosswalkFinding(
                severity=CrosswalkFindingSeverity.BLOCKER,
                code="foundation_default_path_refs_missing",
                message="02A-01 contract bundle has no required default-path source-to-target refs.",
            )
        )
    for item in default_path_refs:
        if item.decision not in migrated_or_contract:
            findings.append(
                CrosswalkFinding(
                    severity=CrosswalkFindingSeverity.BLOCKER,
                    code="foundation_default_path_not_internalized",
                    message=f"Default path ref {item.capability} is marked {item.decision}.",
                )
            )
    current_paths = {target.path for target in crosswalk.current_slice_targets}
    foundation_paths = set(runtime_contracts.primary_runtime_paths)
    missing_alignment = {
        "packages/workers/zyra_workers/code_worker_runtime.py",
        "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
        "packages/runtime/zyra_runtime/claude_tool_use_runtime.py",
    } - (current_paths | foundation_paths)
    for missing in sorted(missing_alignment):
        findings.append(
            CrosswalkFinding(
                severity=CrosswalkFindingSeverity.BLOCKER,
                code="foundation_crosswalk_alignment_missing",
                message=f"Current slice crosswalk and foundation bundle do not expose {missing}.",
                target_path=missing,
            )
        )
    return findings


def _disabled_ports_from_constraints(constraints: Mapping[str, Any]) -> list[str]:
    ports = _list_constraint(constraints.get("disabled_runtime_context_ports"))
    ports.extend(_list_constraint(constraints.get("disabled_runtime_ports")))
    single = constraints.get("disabled_runtime_context_port")
    if single:
        ports.extend(_list_constraint(single))
    return ports


def _list_constraint(value: Any) -> list[str]:
    if value is None or value is False:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _normalize_enum_value(value: Any) -> str:
    raw = str(value).strip()
    if "." in raw:
        raw = raw.rsplit(".", 1)[-1]
    return raw


def _ordered_normalized_values(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = _normalize_enum_value(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _unique_targets(targets: Iterable[ZyraTargetRef]) -> list[ZyraTargetRef]:
    seen: set[str] = set()
    unique: list[ZyraTargetRef] = []
    for target in targets:
        key = f"{target.path}::{target.symbol_or_entrypoint}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique
