from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import ArtifactKind, EventRecord, EventType, new_id, to_jsonable
from zyra_runtime import (
    CodeWorkerSessionFoundationRuntime,
    CodeWorkerSessionStore,
    ContextAssemblyBudget,
    ContextAssemblyRuntime,
    ClaudeQueryEngineConfig,
    SessionFoundationAuditor,
    JsonPermissionStore,
    QueryInputProcessor,
    ToolExecutionContext,
    WorkerRequest,
    WorkerResult,
    ZyraClaudeQueryEngine,
    assemble_claude_runtime_context,
    build_worker_execution_gate_inputs,
    build_worker_execution_gate_report,
    build_claude_productization_integration_report,
    build_claude_source_graph_audit,
    build_productized_claude_runtime_contracts,
    claude_productization_integration_events,
    claude_runtime_context_assembly_events,
    claude_source_graph_audit_event,
    query_plan_metadata_from_constraints,
    query_turns_from_constraints as productized_query_turns_from_constraints,
    foundation_audit_event,
    foundation_audit_metadata,
    render_foundation_audit_markdown,
    seed_failure_result_metadata,
    session_seed_metadata,
    runtime_context_assembly_markdown,
    source_graph_audit_markdown,
    source_graph_crosswalk_markdown,
    worker_execution_gate_event,
    worker_execution_gate_markdown,
)

from .code_worker_bridge import CodeWorkerSidecarClient


@dataclass(frozen=True, slots=True)
class CodeWorkerRun:
    worker_result: WorkerResult
    event_records: list[EventRecord]


class CodeWorkerRuntime:
    """Code worker loop boundary backed by Zyra-owned Claude Code runtime ports."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        sidecar_client: CodeWorkerSidecarClient | None = None,
        permission_store: JsonPermissionStore | None = None,
        query_engine_factory: Any | None = ZyraClaudeQueryEngine,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.sidecar_client = sidecar_client or CodeWorkerSidecarClient(self.project_root)
        self.runtime_contracts = build_productized_claude_runtime_contracts(project_root=self.project_root)
        self.query_engine_factory = query_engine_factory
        self.execution_context = ToolExecutionContext.for_workspace(
            workspace_root=workspace_root,
            artifact_root=artifact_root,
            permission_store=permission_store,
        )

    def run(self, request: WorkerRequest) -> CodeWorkerRun:
        if request.constraints.get("disable_productized_runtime") is True or self.query_engine_factory is None:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime productized QueryEngine runtime is disconnected.",
                error="productized_query_engine_runtime_disabled",
                metadata={
                    **self.runtime_contracts.metadata(),
                    "sidecar_contracts_used": "false",
                    "query_turns": "0",
                    "tool_steps": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(request, worker_result)],
            )

        use_sidecar_contracts = request.constraints.get("use_sidecar_contracts") is True
        integration_report = build_claude_productization_integration_report(
            project_root=self.project_root,
            runtime_contracts=self.runtime_contracts,
            request_constraints=request.constraints,
            sidecar_contracts_used=use_sidecar_contracts,
        )
        integration_events = claude_productization_integration_events(request, integration_report)
        if not integration_report.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before tool execution because Claude source graph integration is blocked.",
                error=integration_report.blocking_error or "claude_productization_integration_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[*integration_events, _worker_result_event(request, worker_result)],
            )
        tool_specs = self.execution_context.registry.list()
        tool_names = tuple(tool.name for tool in tool_specs)
        read_only_tool_names = tuple(tool.name for tool in tool_specs if tool.metadata.get("read_only") == "true")
        mutating_tool_names = tuple(tool.name for tool in tool_specs if tool.metadata.get("read_only") != "true")
        runtime_context_report = assemble_claude_runtime_context(
            request=request,
            integration_report=integration_report,
            runtime_contracts=self.runtime_contracts,
            project_root=self.project_root,
            workspace_root=self.execution_context.workspace_root,
            artifact_root=self.execution_context.artifact_store.root,
            tool_names=tool_names,
            read_only_tool_names=read_only_tool_names,
            mutating_tool_names=mutating_tool_names,
            permission_mode=str(request.constraints.get("permission_mode") or "workspace"),
        )
        runtime_context_events = claude_runtime_context_assembly_events(request, runtime_context_report)
        if not runtime_context_report.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before tool execution because RuntimeContext assembly is blocked.",
                error=runtime_context_report.first_blocker_code or "claude_runtime_context_assembly_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    _worker_result_event(request, worker_result),
                ],
            )
        source_graph_audit = build_claude_source_graph_audit(
            project_root=self.project_root,
            integration_report=integration_report,
            runtime_contracts=self.runtime_contracts,
            runtime_context_report=runtime_context_report,
        )
        source_graph_audit_events = [claude_source_graph_audit_event(request, source_graph_audit)]
        if not source_graph_audit.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before tool execution because source graph audit is blocked.",
                error=source_graph_audit.first_blocker_code or "claude_source_graph_audit_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    _worker_result_event(request, worker_result),
                ],
            )

        worker_gate = build_worker_execution_gate_report(
            build_worker_execution_gate_inputs(
                request=request,
                project_root=self.project_root,
                workspace_root=self.execution_context.workspace_root,
                artifact_root=self.execution_context.artifact_store.root,
                runtime_contracts=self.runtime_contracts,
                integration_report=integration_report,
                runtime_context_report=runtime_context_report,
                source_graph_audit=source_graph_audit,
                tool_specs=tool_specs,
                query_engine_available=self.query_engine_factory is not None,
                sidecar_contracts_used=use_sidecar_contracts,
            )
        )
        worker_gate_event = worker_execution_gate_event(request, worker_gate)
        if not worker_gate.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before QueryEngine because worker execution gate is blocked.",
                error=worker_gate.first_blocker_code or "claude_worker_execution_gate_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    **worker_gate.metadata(),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                    "query_turns": "0",
                    "tool_steps": "0",
                    "context_compactions": "0",
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    worker_gate_event,
                    _worker_result_event(request, worker_result),
                ],
            )

        if use_sidecar_contracts:
            runtime_health = self.sidecar_client.health()
            runtime_inventory = self.sidecar_client.runtime_inventory()
            query_contract = self.sidecar_client.query_contract()
            session_contract = self.sidecar_client.session_contract()
            tool_loop_contract = self.sidecar_client.tool_loop_contract()
        else:
            runtime_health = self.runtime_contracts.health
            runtime_inventory = self.runtime_contracts.inventory
            query_contract = self.runtime_contracts.query_contract
            session_contract = self.runtime_contracts.session_contract
            tool_loop_contract = self.runtime_contracts.tool_loop_contract

        query_turns = productized_query_turns_from_constraints(request.constraints)
        query_plan_metadata = query_plan_metadata_from_constraints(request.constraints)
        session_store = CodeWorkerSessionStore(self.execution_context.artifact_store.root)
        input_processor = QueryInputProcessor(
            max_input_chars=_positive_int(
                request.constraints.get("max_query_input_chars"),
                default=64000,
            )
        )
        input_report = input_processor.process_worker_request(
            request,
            disabled=request.constraints.get("disable_query_input_processor") is True,
        )
        context_runtime = ContextAssemblyRuntime(
            budget=ContextAssemblyBudget(
                max_chars=_positive_int(
                    request.constraints.get("query_context_budget_chars"),
                    default=32000,
                ),
                reserve_chars=_positive_int(
                    request.constraints.get("query_context_reserve_chars"),
                    default=4000,
                ),
            )
        )
        seed_session_id = str(request.constraints.get("session_id") or new_id("codesession"))
        context_snapshot = context_runtime.assemble(
            request=request,
            session_id=seed_session_id,
            input_records=input_report.records,
            tool_specs=tool_specs,
            project_root=self.project_root,
            workspace_root=self.execution_context.workspace_root,
            artifact_root=self.execution_context.artifact_store.root,
            runtime_contracts=self.runtime_contracts,
            integration_report=integration_report,
            runtime_context_report=runtime_context_report,
            source_graph_audit=source_graph_audit,
            permission_mode=str(request.constraints.get("permission_mode") or "workspace"),
            disabled=request.constraints.get("disable_context_assembly") is True,
        )
        session_foundation = CodeWorkerSessionFoundationRuntime(store=session_store)
        session_seed = session_foundation.build_seed(
            request=request,
            input_report=input_report,
            context_snapshot=context_snapshot,
            session_id=seed_session_id,
            disabled_store=request.constraints.get("disable_code_worker_session_store") is True,
        )
        session_seed_events = session_foundation.seed_events(session_seed)
        foundation_auditor = SessionFoundationAuditor()
        foundation_audit = foundation_auditor.audit_seed(
            session_seed,
            store=session_store,
            events=session_seed_events,
        )
        foundation_audit_record = foundation_audit_event(foundation_audit)
        if not session_seed.ok or not foundation_audit.ok:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime stopped before QueryEngine because query session foundation is blocked.",
                error="code_worker_session_foundation_failed",
                metadata={
                    **self.runtime_contracts.metadata(),
                    **integration_report.metadata(),
                    **runtime_context_report.metadata(),
                    **source_graph_audit.metadata(),
                    **worker_gate.metadata(),
                    **query_plan_metadata,
                    **seed_failure_result_metadata(session_seed, error="code_worker_session_foundation_failed"),
                    **foundation_audit_metadata(foundation_audit),
                    "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[
                    *integration_events,
                    *runtime_context_events,
                    *source_graph_audit_events,
                    worker_gate_event,
                    *session_seed_events,
                    foundation_audit_record,
                    _worker_result_event(request, worker_result),
                ],
            )
        engine = self.query_engine_factory(
            self.execution_context,
            ClaudeQueryEngineConfig(
                max_turns=_optional_int(request.constraints.get("max_turns")),
                max_tool_result_chars=_positive_int(
                    request.constraints.get("tool_result_budget_chars"),
                    default=8000,
                ),
                max_query_context_chars=_positive_int(
                    request.constraints.get("query_context_budget_chars"),
                    default=32000,
                ),
                continue_on_error=request.constraints.get("continue_on_error") is True,
                runtime_contracts=self.runtime_contracts,
                max_read_only_concurrency=_positive_int(
                    request.constraints.get("max_read_only_concurrency"),
                    default=_contract_default_concurrency(query_contract),
                ),
                emit_tool_use_summaries=request.constraints.get("emit_tool_use_summaries") is not False,
                control_commands=request.constraints.get("control_commands") or (),
                project_root=self.project_root,
                session_seed=session_seed.to_dict(include_text=False),
                context_snapshot=context_snapshot.to_dict(include_text=True),
                preprocessed_messages=session_seed.request_messages(),
                session_foundation_metadata=session_seed.metadata_values(),
            ),
        )
        loop_result = engine.run(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            worker_request_id=request.request_id,
            turns=query_turns,
            request_messages=[*session_seed.request_messages(), *request.messages],
            request_metadata={**request.metadata, **session_seed.metadata_values()},
        )
        session_store.mark_query_engine_attached(
            session_id=session_seed.session_id,
            worker_request_id=request.request_id,
            run_id=request.run_id,
            task_id=request.task_id,
            query_session_id=str(loop_result.metadata.get("query_session_id") or session_seed.session_id),
            resume_token=str(loop_result.metadata.get("query_session_resume_token") or ""),
        )
        foundation_audit = foundation_auditor.audit_seed(
            session_seed,
            store=session_store,
            events=[*session_seed_events, *loop_result.event_records],
        )
        foundation_audit_record = foundation_audit_event(foundation_audit)
        artifacts = list(loop_result.artifacts)
        step_summaries = list(loop_result.step_summaries)

        trace_artifact = self.execution_context.artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=_trace_markdown(
                request,
                runtime_health,
                runtime_inventory,
                query_contract,
                session_contract,
                tool_loop_contract,
                integration_report,
                runtime_context_report,
                source_graph_audit,
                worker_gate,
                step_summaries,
                loop_result,
                sidecar_contracts_used=use_sidecar_contracts,
            ),
            title=f"CodeWorker trace {request.request_id}",
            kind=ArtifactKind.TRACE,
            extension=".md",
            producer_node_id=request.node_id,
        )
        artifacts.append(trace_artifact)

        summary = "CodeWorkerRuntime completed structured tool plan."
        if not loop_result.ok:
            summary = "CodeWorkerRuntime stopped on a failed tool step."

        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=loop_result.ok,
            summary=summary,
            artifacts=artifacts,
            events=[to_jsonable(event) for event in [*session_seed_events, foundation_audit_record, *loop_result.event_records]],
            error=None if loop_result.ok else loop_result.stopped_reason or "tool_step_failed",
            metadata={
                **self.runtime_contracts.metadata(),
                **_sidecar_metadata(runtime_health, used=use_sidecar_contracts),
                **_inventory_metadata(runtime_inventory),
                **_contract_metadata(query_contract),
                **_session_contract_metadata(session_contract),
                **_tool_loop_contract_metadata(tool_loop_contract),
                **integration_report.metadata(),
                **runtime_context_report.metadata(),
                **source_graph_audit.metadata(),
                **worker_gate.metadata(),
                **query_plan_metadata,
                **session_seed_metadata(session_seed),
                **foundation_audit_metadata(foundation_audit),
                **loop_result.metadata,
                "sidecar_contracts_used": str(use_sidecar_contracts).lower(),
                "query_turns": str(loop_result.turn_count),
                "tool_steps": str(loop_result.tool_call_count),
                "context_compactions": str(loop_result.context_compaction_count),
                "trace_artifact_id": trace_artifact.artifact_id,
                "query_session_checkpoint_ready": str(bool(loop_result.session_snapshot)).lower(),
            },
        )
        return CodeWorkerRun(
            worker_result=worker_result,
            event_records=[
                *integration_events,
                *runtime_context_events,
                *source_graph_audit_events,
                worker_gate_event,
                *session_seed_events,
                foundation_audit_record,
                *loop_result.event_records,
                _worker_result_event(request, worker_result),
            ],
        )


def _worker_result_event(request: WorkerRequest, result: WorkerResult) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "worker_request": to_jsonable(request),
            "worker_result": to_jsonable(result),
        },
    )


def _sidecar_metadata(health: dict[str, Any], *, used: bool) -> dict[str, str]:
    vendor = health.get("vendor") if isinstance(health.get("vendor"), dict) else {}
    return {
        "sidecar_runtime": str(health.get("runtime") or "") if used else "",
        "sidecar_worker": str(health.get("worker") or "") if used else "",
        "sidecar_contracts_used": str(used).lower(),
        "vendor_complete": str(vendor.get("complete") is True).lower(),
    }


def _inventory_metadata(inventory: dict[str, Any]) -> dict[str, str]:
    tool_runtime = inventory.get("toolRuntime") if isinstance(inventory.get("toolRuntime"), dict) else {}
    command_runtime = inventory.get("commandRuntime") if isinstance(inventory.get("commandRuntime"), dict) else {}
    return {
        "inventory_source": str(inventory.get("source") or ""),
        "inventory_base_tool_count": str(tool_runtime.get("baseToolCount") or 0),
        "inventory_command_count": str(command_runtime.get("commandCount") or 0),
    }


def _contract_metadata(contract: dict[str, Any]) -> dict[str, str]:
    orchestration = contract.get("toolOrchestration") if isinstance(contract.get("toolOrchestration"), dict) else {}
    budgets = contract.get("budgets") if isinstance(contract.get("budgets"), dict) else {}
    lifecycle_events = contract.get("lifecycleEvents") if isinstance(contract.get("lifecycleEvents"), list) else []
    return {
        "query_contract_source": str(contract.get("source") or ""),
        "query_contract_lifecycle_count": str(len(lifecycle_events)),
        "query_contract_read_only_concurrent": str(orchestration.get("readOnlyConcurrent") is True).lower(),
        "query_contract_write_serial": str(orchestration.get("writeSerial") is True).lower(),
        "query_contract_tool_result_budget": str(budgets.get("toolResultBudget") is True).lower(),
        "query_contract_reactive_compact": str(budgets.get("reactiveCompact") is True).lower(),
    }


def _session_contract_metadata(contract: dict[str, Any]) -> dict[str, str]:
    persistence = contract.get("transcriptPersistence") if isinstance(contract.get("transcriptPersistence"), dict) else {}
    recovery = contract.get("resumeRecovery") if isinstance(contract.get("resumeRecovery"), dict) else {}
    stream = contract.get("streamRuntime") if isinstance(contract.get("streamRuntime"), dict) else {}
    bridge = contract.get("bridgeSessionRuntime") if isinstance(contract.get("bridgeSessionRuntime"), dict) else {}
    foundation = contract.get("preQueryFoundation") if isinstance(contract.get("preQueryFoundation"), dict) else {}
    retry_matrix = stream.get("retryMatrix") if isinstance(stream.get("retryMatrix"), dict) else {}
    return {
        "session_contract_source": str(contract.get("source") or ""),
        "session_contract_owner_unit": str(contract.get("ownerUnit") or ""),
        "session_contract_inventory_exists": str(contract.get("inventoryExists") is True).lower(),
        "session_contract_foundation_owner_unit": str(foundation.get("ownerUnit") or ""),
        "session_contract_has_input_processor": str(foundation.get("hasInputProcessor") is True).lower(),
        "session_contract_has_context_assembly": str(foundation.get("hasContextAssemblyRuntime") is True).lower(),
        "session_contract_has_code_worker_session_store": str(foundation.get("hasCodeWorkerSessionStore") is True).lower(),
        "session_contract_seed_required": str(foundation.get("sessionSeedRequiredForDefaultPath") is True).lower(),
        "session_contract_append_only_jsonl": str(persistence.get("appendOnlyJsonl") is True).lower(),
        "session_contract_parent_uuid_chain": str(persistence.get("parentUuidChain") is True).lower(),
        "session_contract_lite_read_window": str(persistence.get("liteReadWindowBytes") or ""),
        "session_contract_resume_chain": str(recovery.get("hasChainTraversal") is True).lower(),
        "session_contract_interruption_detection": str(recovery.get("hasInterruptionDetection") is True).lower(),
        "session_contract_raw_sse": str(stream.get("rawSseStateMachine") is True).lower(),
        "session_contract_unattended_retry": str(retry_matrix.get("unattendedRetry") is True).lower(),
        "session_contract_bridge_runner": str(bridge.get("hasSessionRunner") is True).lower(),
    }


def _tool_loop_contract_metadata(contract: dict[str, Any]) -> dict[str, str]:
    interface = contract.get("toolInterface") if isinstance(contract.get("toolInterface"), dict) else {}
    execution = contract.get("executionPipeline") if isinstance(contract.get("executionPipeline"), dict) else {}
    scheduling = contract.get("scheduling") if isinstance(contract.get("scheduling"), dict) else {}
    budget = contract.get("resultBudget") if isinstance(contract.get("resultBudget"), dict) else {}
    shell = contract.get("shellRuntime") if isinstance(contract.get("shellRuntime"), dict) else {}
    sandbox = contract.get("sandboxRuntime") if isinstance(contract.get("sandboxRuntime"), dict) else {}
    failures = contract.get("failureSignals") if isinstance(contract.get("failureSignals"), dict) else {}
    source_files = contract.get("sourceFiles") if isinstance(contract.get("sourceFiles"), list) else []
    return {
        "tool_loop_contract_source": str(contract.get("source") or ""),
        "tool_loop_contract_owner_unit": str(contract.get("ownerUnit") or ""),
        "tool_loop_contract_inventory_exists": str(contract.get("inventoryExists") is True).lower(),
        "tool_loop_contract_source_count": str(len(source_files)),
        "tool_loop_contract_has_input_schema": str(interface.get("hasInputSchema") is True).lower(),
        "tool_loop_contract_permission_gate": str(execution.get("hasPermissionGate") is True).lower(),
        "tool_loop_contract_result_mapping": str(execution.get("hasToolResultBlockMapping") is True).lower(),
        "tool_loop_contract_read_only_concurrent": str(scheduling.get("readOnlyConcurrent") is True).lower(),
        "tool_loop_contract_write_serial": str(scheduling.get("writeSerial") is True).lower(),
        "tool_loop_contract_budget_externalization": str(budget.get("hasLargeResultExternalization") is True or budget.get("hasPersistedOutputTag") is True).lower(),
        "tool_loop_contract_shell_lifecycle": str(shell.get("hasProcessLifecycle") is True).lower(),
        "tool_loop_contract_sandbox_adapter": str(sandbox.get("hasSandboxAdapter") is True).lower(),
        "tool_loop_contract_denial_limits": str(failures.get("hasDenialLimits") is True).lower(),
    }


def _contract_default_concurrency(contract: dict[str, Any]) -> int:
    orchestration = contract.get("toolOrchestration") if isinstance(contract.get("toolOrchestration"), dict) else {}
    return _positive_int(orchestration.get("maxConcurrencyDefault"), default=10)


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _trace_markdown(
    request: WorkerRequest,
    runtime_health: dict[str, Any],
    runtime_inventory: dict[str, Any],
    query_contract: dict[str, Any],
    session_contract: dict[str, Any],
    tool_loop_contract: dict[str, Any],
    integration_report: Any,
    runtime_context_report: Any,
    source_graph_audit: Any,
    worker_gate: Any,
    step_summaries: list[str],
    loop_result: Any,
    *,
    sidecar_contracts_used: bool,
) -> str:
    vendor = runtime_health.get("vendor") if isinstance(runtime_health.get("vendor"), dict) else {}
    modules = vendor.get("modules") if isinstance(vendor.get("modules"), list) else []
    tool_runtime = runtime_inventory.get("toolRuntime") if isinstance(runtime_inventory.get("toolRuntime"), dict) else {}
    command_runtime = (
        runtime_inventory.get("commandRuntime") if isinstance(runtime_inventory.get("commandRuntime"), dict) else {}
    )
    orchestration = (
        query_contract.get("toolOrchestration")
        if isinstance(query_contract.get("toolOrchestration"), dict)
        else {}
    )
    source_files = query_contract.get("sourceFiles")
    if not isinstance(source_files, list):
        source_files = []
    session_source_files = session_contract.get("sourceFiles")
    if not isinstance(session_source_files, list):
        session_source_files = []
    session_persistence = (
        session_contract.get("transcriptPersistence")
        if isinstance(session_contract.get("transcriptPersistence"), dict)
        else {}
    )
    session_recovery = (
        session_contract.get("resumeRecovery")
        if isinstance(session_contract.get("resumeRecovery"), dict)
        else {}
    )
    session_foundation = (
        session_contract.get("preQueryFoundation")
        if isinstance(session_contract.get("preQueryFoundation"), dict)
        else {}
    )
    tool_loop_scheduling = (
        tool_loop_contract.get("scheduling")
        if isinstance(tool_loop_contract.get("scheduling"), dict)
        else {}
    )
    tool_loop_budget = (
        tool_loop_contract.get("resultBudget")
        if isinstance(tool_loop_contract.get("resultBudget"), dict)
        else {}
    )
    tool_loop_shell = (
        tool_loop_contract.get("shellRuntime")
        if isinstance(tool_loop_contract.get("shellRuntime"), dict)
        else {}
    )
    tool_loop_sandbox = (
        tool_loop_contract.get("sandboxRuntime")
        if isinstance(tool_loop_contract.get("sandboxRuntime"), dict)
        else {}
    )
    tool_loop_source_files = tool_loop_contract.get("sourceFiles")
    if not isinstance(tool_loop_source_files, list):
        tool_loop_source_files = []
    high_value_commands = command_runtime.get("highValueCommandPaths")
    if not isinstance(high_value_commands, list):
        high_value_commands = []
    module_lines = [
        f"- {module.get('name')}: {'complete' if module.get('complete') else 'missing'}"
        for module in modules
        if isinstance(module, dict)
    ]
    return "\n".join(
        [
            "# CodeWorker Runtime Trace",
            "",
            f"- request_id: `{request.request_id}`",
            f"- worker_name: `{request.worker_name}`",
            f"- ok: `{str(loop_result.ok).lower()}`",
            f"- query_loop: `{loop_result.metadata.get('loop', '')}`",
            f"- runtime_source: `{runtime_health.get('source', '')}`",
            f"- sidecar_contracts_used: `{str(sidecar_contracts_used).lower()}`",
            f"- query_contract_source: `{query_contract.get('source', '')}`",
            f"- tool_orchestration_source: `{orchestration.get('sourcePath', '')}`",
            f"- read_only_concurrent: `{str(orchestration.get('readOnlyConcurrent') is True).lower()}`",
            f"- write_serial: `{str(orchestration.get('writeSerial') is True).lower()}`",
            f"- max_read_only_concurrency: `{loop_result.metadata.get('max_read_only_concurrency', '')}`",
            f"- query_turns: `{loop_result.turn_count}`",
            f"- tool_call_count: `{loop_result.tool_call_count}`",
            f"- context_compaction_count: `{loop_result.context_compaction_count}`",
            f"- stopped_reason: `{loop_result.stopped_reason or ''}`",
            f"- tool_result_budget_chars: `{loop_result.metadata.get('tool_result_budget_chars', '')}`",
            f"- query_context_budget_chars: `{loop_result.metadata.get('query_context_budget_chars', '')}`",
            f"- query_session_id: `{loop_result.metadata.get('query_session_id', '')}`",
            f"- query_session_resume_token: `{loop_result.metadata.get('query_session_resume_token', '')}`",
            f"- query_session_snapshot_artifact_id: `{loop_result.metadata.get('query_session_snapshot_artifact_id', '')}`",
            f"- query_session_transcript_artifact_id: `{loop_result.metadata.get('query_session_transcript_artifact_id', '')}`",
            f"- code_worker_session_seed_ok: `{loop_result.metadata.get('code_worker_session_seed_ok', '')}`",
            f"- context_assembly_snapshot_id: `{loop_result.metadata.get('context_assembly_snapshot_id', '')}`",
            f"- query_input_count: `{loop_result.metadata.get('query_input_count', '')}`",
            f"- session_seed_required: `{str(session_foundation.get('sessionSeedRequiredForDefaultPath') is True).lower()}`",
            f"- session_append_only_jsonl: `{str(session_persistence.get('appendOnlyJsonl') is True).lower()}`",
            f"- session_parent_uuid_chain: `{str(session_persistence.get('parentUuidChain') is True).lower()}`",
            f"- session_resume_chain: `{str(session_recovery.get('hasChainTraversal') is True).lower()}`",
            f"- tool_loop_contract_source: `{tool_loop_contract.get('source', '')}`",
            f"- tool_loop_contract_owner_unit: `{tool_loop_contract.get('ownerUnit', '')}`",
            f"- tool_loop_inventory_exists: `{str(tool_loop_contract.get('inventoryExists') is True).lower()}`",
            f"- tool_loop_read_only_concurrent: `{str(tool_loop_scheduling.get('readOnlyConcurrent') is True).lower()}`",
            f"- tool_loop_write_serial: `{str(tool_loop_scheduling.get('writeSerial') is True).lower()}`",
            f"- tool_loop_budget_externalization: `{str(tool_loop_budget.get('hasPersistedOutputTag') is True or tool_loop_budget.get('hasMaxResultSizeChars') is True).lower()}`",
            f"- tool_loop_shell_lifecycle: `{str(tool_loop_shell.get('hasProcessLifecycle') is True).lower()}`",
            f"- tool_loop_sandbox_adapter: `{str(tool_loop_sandbox.get('hasSandboxAdapter') is True).lower()}`",
            f"- tool_failure_signal_count: `{loop_result.metadata.get('tool_failure_signals', '')}`",
            f"- vendored_runtime_complete: `{str(vendor.get('complete') is True).lower()}`",
            f"- inventory_base_tool_count: `{tool_runtime.get('baseToolCount', 0)}`",
            f"- inventory_command_count: `{command_runtime.get('commandCount', 0)}`",
            "",
            "## Tool Steps",
            "",
            *(step_summaries or ["- no tool steps executed"]),
            "",
            "## Claude Code Runtime Inventory",
            "",
            *(f"- command: `{command}`" for command in high_value_commands[:24]),
            "",
            "## QueryEngine Contract Sources",
            "",
            *(f"- `{source_file}`" for source_file in source_files[:24]),
            "",
            "## Query Session Contract Sources",
            "",
            *(f"- `{source_file}`" for source_file in session_source_files[:32]),
            "",
            "## Tool Loop Budget Contract Sources",
            "",
            *(f"- `{source_file}`" for source_file in tool_loop_source_files[:32]),
            "",
            source_graph_crosswalk_markdown(integration_report).rstrip(),
            "",
            runtime_context_assembly_markdown(runtime_context_report).rstrip(),
            "",
            source_graph_audit_markdown(source_graph_audit).rstrip(),
            "",
            worker_execution_gate_markdown(worker_gate).rstrip(),
            "",
            "## Vendored Runtime Modules",
            "",
            *(module_lines or ["- no sidecar module snapshot available"]),
            "",
        ]
    )
