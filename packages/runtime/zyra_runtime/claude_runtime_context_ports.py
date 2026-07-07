from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

from zyra_core import EventRecord, EventType, now_iso, to_jsonable

from .claude_runtime_contracts import ClaudeRuntimeContractBundle
from .claude_source_graph_crosswalk import (
    ClaudeProductizationIntegrationReport,
    EventContractPhase,
    RuntimeContextPortContract,
    RuntimePortKind,
    RuntimePortStatus,
    ToolUseContextPortContract,
)
from .workers import WorkerRequest


class RuntimeContextAssemblyStatus(StrEnum):
    READY = "ready"
    CONTRACT_ONLY = "contract_only"
    BLOCKED = "blocked"


class RuntimeContextBindingKind(StrEnum):
    RUNTIME_PORT = "runtime_port"
    TOOL_USE_CONTEXT = "tool_use_context"
    DOWNSTREAM_HANDOFF = "downstream_handoff"
    EVENT_CONTRACT = "event_contract"


class RuntimeContextBindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class RuntimeContextBinding:
    binding_id: str
    kind: RuntimeContextBindingKind
    port_id: str
    port_kind: RuntimePortKind | str
    status: RuntimeContextAssemblyStatus
    owner_slice: str
    producer: str
    consumer: str
    value_ref: str
    state_owner: str
    payload_fields: tuple[str, ...]
    event_phases: tuple[EventContractPhase, ...]
    required_for_runtime_shell: bool
    active_in_default_path: bool
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status == RuntimeContextAssemblyStatus.BLOCKED and self.required_for_runtime_shell

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "kind": str(self.kind),
            "port_id": self.port_id,
            "port_kind": str(self.port_kind),
            "status": str(self.status),
            "owner_slice": self.owner_slice,
            "producer": self.producer,
            "consumer": self.consumer,
            "value_ref": self.value_ref,
            "state_owner": self.state_owner,
            "payload_fields": list(self.payload_fields),
            "event_phases": [str(phase) for phase in self.event_phases],
            "required_for_runtime_shell": self.required_for_runtime_shell,
            "active_in_default_path": self.active_in_default_path,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolUseContextBinding:
    binding_id: str
    port_id: str
    registry_entrypoint: str
    permission_surface: str
    budget_surface: str
    result_surface: str
    active_tool_names: tuple[str, ...]
    mutating_tool_names: tuple[str, ...]
    read_only_tool_names: tuple[str, ...]
    downstream_owner: str
    event_phases: tuple[EventContractPhase, ...]
    status: RuntimeContextAssemblyStatus
    failure_reason: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == RuntimeContextAssemblyStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "port_id": self.port_id,
            "registry_entrypoint": self.registry_entrypoint,
            "permission_surface": self.permission_surface,
            "budget_surface": self.budget_surface,
            "result_surface": self.result_surface,
            "active_tool_names": list(self.active_tool_names),
            "mutating_tool_names": list(self.mutating_tool_names),
            "read_only_tool_names": list(self.read_only_tool_names),
            "downstream_owner": self.downstream_owner,
            "event_phases": [str(phase) for phase in self.event_phases],
            "status": str(self.status),
            "failure_reason": self.failure_reason,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class RuntimeContextAssemblyFinding:
    severity: RuntimeContextBindingSeverity
    code: str
    message: str
    port_id: str = ""
    binding_id: str = ""

    @property
    def blocking(self) -> bool:
        return self.severity == RuntimeContextBindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "code": self.code,
            "message": self.message,
            "port_id": self.port_id,
            "binding_id": self.binding_id,
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class RuntimeContextAssemblyReport:
    ok: bool
    checked_at: str
    request_id: str
    run_id: str
    task_id: str
    worker_name: str
    project_root: str
    workspace_root: str
    artifact_root: str
    source_graph_contract_id: str
    runtime_bindings: tuple[RuntimeContextBinding, ...]
    tool_use_bindings: tuple[ToolUseContextBinding, ...]
    findings: tuple[RuntimeContextAssemblyFinding, ...] = ()
    metadata_extra: dict[str, str] = field(default_factory=dict)

    @property
    def blockers(self) -> tuple[RuntimeContextAssemblyFinding, ...]:
        return tuple(finding for finding in self.findings if finding.blocking)

    @property
    def active_runtime_bindings(self) -> tuple[RuntimeContextBinding, ...]:
        return tuple(binding for binding in self.runtime_bindings if binding.active_in_default_path)

    @property
    def contract_only_bindings(self) -> tuple[RuntimeContextBinding, ...]:
        return tuple(binding for binding in self.runtime_bindings if binding.status == RuntimeContextAssemblyStatus.CONTRACT_ONLY)

    @property
    def first_blocker_code(self) -> str:
        return self.blockers[0].code if self.blockers else ""

    def metadata(self) -> dict[str, str]:
        return {
            "runtime_context_assembly_ok": str(self.ok).lower(),
            "runtime_context_assembly_blockers": str(len(self.blockers)),
            "runtime_context_assembly_first_blocker": self.first_blocker_code,
            "runtime_context_binding_count": str(len(self.runtime_bindings)),
            "runtime_context_active_binding_count": str(len(self.active_runtime_bindings)),
            "runtime_context_contract_only_binding_count": str(len(self.contract_only_bindings)),
            "tool_use_context_binding_count": str(len(self.tool_use_bindings)),
            "tool_use_context_blocking_bindings": str(sum(1 for binding in self.tool_use_bindings if binding.blocking)),
            "runtime_context_project_root": self.project_root,
            "runtime_context_workspace_root": self.workspace_root,
            "runtime_context_artifact_root": self.artifact_root,
            "runtime_context_source_graph_contract_id": self.source_graph_contract_id,
            **dict(self.metadata_extra),
        }

    def event_payload(self, phase: EventContractPhase) -> dict[str, Any]:
        return {
            "phase": str(phase),
            "ok": self.ok,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_name": self.worker_name,
            "source_graph_contract_id": self.source_graph_contract_id,
            "runtime_binding_count": len(self.runtime_bindings),
            "active_runtime_binding_count": len(self.active_runtime_bindings),
            "tool_use_binding_count": len(self.tool_use_bindings),
            "blocking_error": self.first_blocker_code,
            "blockers": [finding.to_dict() for finding in self.blockers],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_name": self.worker_name,
            "project_root": self.project_root,
            "workspace_root": self.workspace_root,
            "artifact_root": self.artifact_root,
            "source_graph_contract_id": self.source_graph_contract_id,
            "runtime_bindings": [binding.to_dict() for binding in self.runtime_bindings],
            "tool_use_bindings": [binding.to_dict() for binding in self.tool_use_bindings],
            "findings": [finding.to_dict() for finding in self.findings],
            "metadata": self.metadata(),
        }


def assemble_claude_runtime_context(
    *,
    request: WorkerRequest,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_contracts: ClaudeRuntimeContractBundle,
    project_root: str | Path,
    workspace_root: str | Path,
    artifact_root: str | Path,
    tool_names: Iterable[str],
    read_only_tool_names: Iterable[str] = (),
    mutating_tool_names: Iterable[str] = (),
    permission_mode: str = "workspace",
) -> RuntimeContextAssemblyReport:
    project_path = Path(project_root).resolve()
    workspace_path = Path(workspace_root).resolve()
    artifact_path = Path(artifact_root).resolve()
    tool_name_tuple = tuple(dict.fromkeys(str(name) for name in tool_names))
    read_only_tuple = tuple(dict.fromkeys(str(name) for name in read_only_tool_names))
    mutating_tuple = tuple(dict.fromkeys(str(name) for name in mutating_tool_names))
    runtime_bindings = tuple(
        _binding_from_port(
            port,
            request=request,
            integration_report=integration_report,
            runtime_contracts=runtime_contracts,
            project_root=project_path,
            workspace_root=workspace_path,
            artifact_root=artifact_path,
            tool_names=tool_name_tuple,
            permission_mode=permission_mode,
        )
        for port in integration_report.crosswalk.runtime_context_ports
    )
    tool_use_bindings = tuple(
        _tool_use_binding_from_port(
            port,
            active_tool_names=tool_name_tuple,
            read_only_tool_names=read_only_tuple,
            mutating_tool_names=mutating_tuple,
        )
        for port in integration_report.crosswalk.tool_use_context_ports
    )
    findings = [
        *_runtime_binding_findings(runtime_bindings),
        *_tool_use_binding_findings(tool_use_bindings),
        *_path_findings(project_path, workspace_path, artifact_path),
    ]
    ok = integration_report.ok and not any(finding.blocking for finding in findings)
    return RuntimeContextAssemblyReport(
        ok=ok,
        checked_at=now_iso(),
        request_id=request.request_id,
        run_id=request.run_id,
        task_id=request.task_id,
        worker_name=request.worker_name,
        project_root=str(project_path),
        workspace_root=str(workspace_path),
        artifact_root=str(artifact_path),
        source_graph_contract_id=integration_report.crosswalk.contract_id,
        runtime_bindings=runtime_bindings,
        tool_use_bindings=tool_use_bindings,
        findings=tuple(findings),
        metadata_extra={
            "runtime_context_tool_names": str(len(tool_name_tuple)),
            "runtime_context_read_only_tools": str(len(read_only_tuple)),
            "runtime_context_mutating_tools": str(len(mutating_tuple)),
            "runtime_context_permission_mode": permission_mode,
            "runtime_context_foundation_clean_safe": str(runtime_contracts.clean_runtime_safe).lower(),
        },
    )


def claude_runtime_context_assembly_events(
    request: WorkerRequest,
    report: RuntimeContextAssemblyReport,
) -> list[EventRecord]:
    phase = EventContractPhase.RUNTIME_CONTEXT_READY if report.ok else EventContractPhase.INTEGRATION_GATE_BLOCKED
    return [
        EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=EventType.CONSTRAINT_CHECK,
            payload={"claude_runtime_context": report.event_payload(phase)},
        )
    ]


def runtime_context_assembly_markdown(report: RuntimeContextAssemblyReport) -> str:
    lines = [
        "## Runtime Context Assembly",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- source_graph_contract_id: `{report.source_graph_contract_id}`",
        f"- runtime_bindings: `{len(report.runtime_bindings)}`",
        f"- active_runtime_bindings: `{len(report.active_runtime_bindings)}`",
        f"- tool_use_bindings: `{len(report.tool_use_bindings)}`",
        f"- blocking_error: `{report.first_blocker_code}`",
        "",
        "### Required Runtime Bindings",
        "",
    ]
    for binding in report.runtime_bindings:
        if not binding.required_for_runtime_shell:
            continue
        lines.append(
            f"- `{binding.port_id}` `{binding.status}` producer=`{binding.producer}` consumer=`{binding.consumer}` value=`{binding.value_ref}`"
        )
    lines.extend(["", "### ToolUseContext Bindings", ""])
    for binding in report.tool_use_bindings:
        lines.append(
            f"- `{binding.port_id}` `{binding.status}` tools=`{len(binding.active_tool_names)}` downstream=`{binding.downstream_owner}`"
        )
    if report.blockers:
        lines.extend(["", "### Assembly Blockers", ""])
        lines.extend(f"- `{finding.code}` {finding.message}" for finding in report.blockers)
    return "\n".join(lines) + "\n"


def assert_runtime_context_assembly_ready(report: RuntimeContextAssemblyReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.blockers)
    raise AssertionError(f"Claude runtime context assembly is not ready: {blockers}")


def _binding_from_port(
    port: RuntimeContextPortContract,
    *,
    request: WorkerRequest,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_contracts: ClaudeRuntimeContractBundle,
    project_root: Path,
    workspace_root: Path,
    artifact_root: Path,
    tool_names: tuple[str, ...],
    permission_mode: str,
) -> RuntimeContextBinding:
    value_ref, metadata = _value_ref_for_port(
        port,
        request=request,
        integration_report=integration_report,
        runtime_contracts=runtime_contracts,
        project_root=project_root,
        workspace_root=workspace_root,
        artifact_root=artifact_root,
        tool_names=tool_names,
        permission_mode=permission_mode,
    )
    active = port.status == RuntimePortStatus.ACTIVE or port.required_for_runtime_shell
    status = RuntimeContextAssemblyStatus.READY if active else RuntimeContextAssemblyStatus.CONTRACT_ONLY
    if port.status == RuntimePortStatus.DISABLED:
        status = RuntimeContextAssemblyStatus.BLOCKED
    return RuntimeContextBinding(
        binding_id=f"runtime_context.{port.port_id}",
        kind=RuntimeContextBindingKind.RUNTIME_PORT if active else RuntimeContextBindingKind.DOWNSTREAM_HANDOFF,
        port_id=port.port_id,
        port_kind=port.kind,
        status=status,
        owner_slice=port.owner_slice,
        producer=port.producer,
        consumer=port.consumer,
        value_ref=value_ref,
        state_owner=port.state_owner,
        payload_fields=port.contract_fields,
        event_phases=port.event_phases,
        required_for_runtime_shell=port.required_for_runtime_shell,
        active_in_default_path=active,
        metadata=metadata,
    )


def _tool_use_binding_from_port(
    port: ToolUseContextPortContract,
    *,
    active_tool_names: tuple[str, ...],
    read_only_tool_names: tuple[str, ...],
    mutating_tool_names: tuple[str, ...],
) -> ToolUseContextBinding:
    if port.required_for_runtime_shell and not active_tool_names:
        status = RuntimeContextAssemblyStatus.BLOCKED
        failure_reason = "tool_use_context_has_no_active_tools"
    elif port.required_for_runtime_shell:
        status = RuntimeContextAssemblyStatus.READY
        failure_reason = ""
    else:
        status = RuntimeContextAssemblyStatus.CONTRACT_ONLY
        failure_reason = ""
    return ToolUseContextBinding(
        binding_id=f"tool_use_context.{port.port_id}",
        port_id=port.port_id,
        registry_entrypoint=port.registry_entrypoint,
        permission_surface=port.permission_surface,
        budget_surface=port.budget_surface,
        result_surface=port.result_surface,
        active_tool_names=active_tool_names,
        mutating_tool_names=mutating_tool_names,
        read_only_tool_names=read_only_tool_names,
        downstream_owner=port.downstream_owner,
        event_phases=port.event_phases,
        status=status,
        failure_reason=failure_reason,
    )


def _value_ref_for_port(
    port: RuntimeContextPortContract,
    *,
    request: WorkerRequest,
    integration_report: ClaudeProductizationIntegrationReport,
    runtime_contracts: ClaudeRuntimeContractBundle,
    project_root: Path,
    workspace_root: Path,
    artifact_root: Path,
    tool_names: tuple[str, ...],
    permission_mode: str,
) -> tuple[str, dict[str, str]]:
    if port.kind == RuntimePortKind.SESSION_LIFECYCLE:
        return (
            f"{request.run_id}/{request.task_id}/{request.request_id}",
            {
                "run_id": request.run_id,
                "task_id": request.task_id,
                "request_id": request.request_id,
                "message_count": str(len(request.messages)),
            },
        )
    if port.kind == RuntimePortKind.WORKSPACE_CWD:
        return (
            str(workspace_root),
            {
                "workspace_root": str(workspace_root),
                "artifact_root": str(artifact_root),
            },
        )
    if port.kind == RuntimePortKind.TOOL_REGISTRY:
        return (
            "zyra_runtime.default_tool_registry",
            {"tool_count": str(len(tool_names)), "tools": ",".join(tool_names[:16])},
        )
    if port.kind == RuntimePortKind.TOOL_EXECUTOR:
        return (
            "zyra_runtime.ToolExecutor",
            {"tool_count": str(len(tool_names)), "permission_mode": permission_mode},
        )
    if port.kind == RuntimePortKind.TOOL_RESULT_BUDGET:
        return (
            str(runtime_contracts.tool_loop_contract.get("resultBudget", {}).get("artifactStore") or "LocalArtifactStore"),
            {
                "budget_source": str(runtime_contracts.tool_loop_contract.get("source") or ""),
                "max_result_budget": str(runtime_contracts.query_contract.get("budgets", {}).get("maxToolResultChars") or ""),
            },
        )
    if port.kind == RuntimePortKind.PERMISSION_MODE:
        return (
            permission_mode,
            {"permission_mode": permission_mode, "policy": "ToolPermissionPolicy"},
        )
    if port.kind == RuntimePortKind.CONTEXT_WINDOW:
        return (
            "ClaudeContextWindowManager",
            {
                "query_context_budget": str(runtime_contracts.query_contract.get("budgets", {}).get("maxQueryContextChars") or ""),
                "reactive_compact": str(runtime_contracts.query_contract.get("budgets", {}).get("reactiveCompact") is True).lower(),
            },
        )
    if port.kind == RuntimePortKind.COMPACT_RESTORE:
        return (
            "CompactRestoreRuntime.contract",
            {"downstream_owner": port.downstream_owner, "session_contract_source": str(runtime_contracts.session_contract.get("source") or "")},
        )
    if port.kind == RuntimePortKind.MCP_CLIENTS:
        return ("McpRuntimeStore.contract", {"downstream_owner": port.downstream_owner})
    if port.kind == RuntimePortKind.SKILL_REGISTRY:
        return ("SkillRuntime.contract", {"downstream_owner": port.downstream_owner})
    if port.kind == RuntimePortKind.PLUGIN_REGISTRY:
        return ("PluginRuntime.contract", {"downstream_owner": port.downstream_owner})
    if port.kind == RuntimePortKind.SUBAGENT_DISPATCH:
        return ("SubagentRuntime.contract", {"downstream_owner": port.downstream_owner})
    if port.kind == RuntimePortKind.CONTROL_COMMANDS:
        return (
            "ClaudeControlCommandRuntime",
            {"requested_control_commands": str(len(request.constraints.get("control_commands") or ()))},
        )
    if port.kind == RuntimePortKind.MODEL_STREAM:
        return ("ModelStreamRuntime.contract", {"downstream_owner": port.downstream_owner})
    if port.kind == RuntimePortKind.EVENT_SINK:
        return ("zyra_core.EventRecord", {"event_type": EventType.CONSTRAINT_CHECK.value})
    if port.kind == RuntimePortKind.ARTIFACT_STORE:
        return (str(artifact_root), {"artifact_root": str(artifact_root)})
    if port.kind == RuntimePortKind.SOURCE_GRAPH:
        return (
            integration_report.crosswalk.contract_id,
            {
                "batch_count": str(len(integration_report.crosswalk.batches)),
                "downstream_contracts": str(len(integration_report.crosswalk.downstream_contracts)),
            },
        )
    if port.kind == RuntimePortKind.STATE_CUSTODY:
        return ("ClaudeRuntimeStateLedger", {"project_root": str(project_root)})
    return (port.producer, {})


def _runtime_binding_findings(bindings: Iterable[RuntimeContextBinding]) -> list[RuntimeContextAssemblyFinding]:
    findings: list[RuntimeContextAssemblyFinding] = []
    seen_ports: set[str] = set()
    for binding in bindings:
        if binding.port_id in seen_ports:
            findings.append(
                RuntimeContextAssemblyFinding(
                    severity=RuntimeContextBindingSeverity.BLOCKER,
                    code="runtime_context_duplicate_port_binding",
                    message=f"RuntimeContext port {binding.port_id} was bound more than once.",
                    port_id=binding.port_id,
                    binding_id=binding.binding_id,
                )
            )
        seen_ports.add(binding.port_id)
        if binding.required_for_runtime_shell and not binding.value_ref:
            findings.append(
                RuntimeContextAssemblyFinding(
                    severity=RuntimeContextBindingSeverity.BLOCKER,
                    code="runtime_context_required_binding_missing_value",
                    message=f"Required RuntimeContext binding {binding.port_id} has no value reference.",
                    port_id=binding.port_id,
                    binding_id=binding.binding_id,
                )
            )
        if binding.blocking:
            findings.append(
                RuntimeContextAssemblyFinding(
                    severity=RuntimeContextBindingSeverity.BLOCKER,
                    code=f"{binding.port_id}_binding_blocked",
                    message=f"Required RuntimeContext binding {binding.port_id} is blocked.",
                    port_id=binding.port_id,
                    binding_id=binding.binding_id,
                )
            )
        if binding.required_for_runtime_shell and binding.status == RuntimeContextAssemblyStatus.CONTRACT_ONLY:
            findings.append(
                RuntimeContextAssemblyFinding(
                    severity=RuntimeContextBindingSeverity.BLOCKER,
                    code="runtime_context_required_binding_contract_only",
                    message=f"Required RuntimeContext binding {binding.port_id} is contract-only.",
                    port_id=binding.port_id,
                    binding_id=binding.binding_id,
                )
            )
    required = {
        RuntimePortKind.SESSION_LIFECYCLE.value,
        RuntimePortKind.WORKSPACE_CWD.value,
        RuntimePortKind.TOOL_REGISTRY.value,
        RuntimePortKind.TOOL_EXECUTOR.value,
        RuntimePortKind.TOOL_RESULT_BUDGET.value,
        RuntimePortKind.PERMISSION_MODE.value,
        RuntimePortKind.CONTEXT_WINDOW.value,
        RuntimePortKind.CONTROL_COMMANDS.value,
        RuntimePortKind.EVENT_SINK.value,
        RuntimePortKind.ARTIFACT_STORE.value,
        RuntimePortKind.SOURCE_GRAPH.value,
        RuntimePortKind.STATE_CUSTODY.value,
    }
    actual = {str(binding.port_kind) for binding in bindings if binding.required_for_runtime_shell}
    for missing in sorted(required - actual):
        findings.append(
            RuntimeContextAssemblyFinding(
                severity=RuntimeContextBindingSeverity.BLOCKER,
                code="runtime_context_required_port_not_bound",
                message=f"Required RuntimeContext port kind {missing} was not bound.",
                port_id=missing,
            )
        )
    return findings


def _tool_use_binding_findings(bindings: Iterable[ToolUseContextBinding]) -> list[RuntimeContextAssemblyFinding]:
    findings: list[RuntimeContextAssemblyFinding] = []
    for binding in bindings:
        if binding.blocking:
            findings.append(
                RuntimeContextAssemblyFinding(
                    severity=RuntimeContextBindingSeverity.BLOCKER,
                    code=binding.failure_reason or "tool_use_context_binding_blocked",
                    message=f"ToolUseContext binding {binding.port_id} is blocked.",
                    port_id=binding.port_id,
                    binding_id=binding.binding_id,
                )
            )
        if binding.status == RuntimeContextAssemblyStatus.READY and not binding.registry_entrypoint:
            findings.append(
                RuntimeContextAssemblyFinding(
                    severity=RuntimeContextBindingSeverity.BLOCKER,
                    code="tool_use_context_registry_missing",
                    message=f"ToolUseContext binding {binding.port_id} has no registry entrypoint.",
                    port_id=binding.port_id,
                    binding_id=binding.binding_id,
                )
            )
        if binding.status == RuntimeContextAssemblyStatus.READY and not binding.permission_surface:
            findings.append(
                RuntimeContextAssemblyFinding(
                    severity=RuntimeContextBindingSeverity.BLOCKER,
                    code="tool_use_context_permission_surface_missing",
                    message=f"ToolUseContext binding {binding.port_id} has no permission surface.",
                    port_id=binding.port_id,
                    binding_id=binding.binding_id,
                )
            )
    return findings


def _path_findings(project_root: Path, workspace_root: Path, artifact_root: Path) -> list[RuntimeContextAssemblyFinding]:
    findings: list[RuntimeContextAssemblyFinding] = []
    if not project_root.exists():
        findings.append(
            RuntimeContextAssemblyFinding(
                severity=RuntimeContextBindingSeverity.BLOCKER,
                code="runtime_context_project_root_missing",
                message=f"Project root {project_root} does not exist.",
            )
        )
    if _path_contains_source_pool(project_root):
        findings.append(
            RuntimeContextAssemblyFinding(
                severity=RuntimeContextBindingSeverity.BLOCKER,
                code="runtime_context_project_root_is_source_pool",
                message=f"Project root {project_root} points at a source-pool/vendor-like path.",
            )
        )
    if _path_contains_source_pool(workspace_root):
        findings.append(
            RuntimeContextAssemblyFinding(
                severity=RuntimeContextBindingSeverity.BLOCKER,
                code="runtime_context_workspace_is_source_pool",
                message=f"Workspace root {workspace_root} points at a source-pool/vendor-like path.",
            )
        )
    if _path_contains_source_pool(artifact_root):
        findings.append(
            RuntimeContextAssemblyFinding(
                severity=RuntimeContextBindingSeverity.BLOCKER,
                code="runtime_context_artifact_root_is_source_pool",
                message=f"Artifact root {artifact_root} points at a source-pool/vendor-like path.",
            )
        )
    return findings


def _path_contains_source_pool(path: Path) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    return any(
        marker in normalized
        for marker in (
            "/vendor/",
            "/vendor-runtimes/",
            "/source-pool/",
            "/runtime-sources/",
        )
    )
