from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable
from zyra_runtime import (
    ClaudeQueryEngineConfig,
    JsonPermissionStore,
    ToolExecutionContext,
    WorkerRequest,
    WorkerResult,
    ZyraClaudeQueryEngine,
    build_productized_claude_runtime_contracts,
    query_plan_metadata_from_constraints,
    query_turns_from_constraints as productized_query_turns_from_constraints,
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
            ),
        )
        loop_result = engine.run(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            worker_request_id=request.request_id,
            turns=query_turns,
            request_messages=request.messages,
            request_metadata=request.metadata,
        )
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
            events=[to_jsonable(event) for event in loop_result.event_records],
            error=None if loop_result.ok else loop_result.stopped_reason or "tool_step_failed",
            metadata={
                **self.runtime_contracts.metadata(),
                **_sidecar_metadata(runtime_health, used=use_sidecar_contracts),
                **_inventory_metadata(runtime_inventory),
                **_contract_metadata(query_contract),
                **_session_contract_metadata(session_contract),
                **_tool_loop_contract_metadata(tool_loop_contract),
                **query_plan_metadata,
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
            event_records=[*loop_result.event_records, _worker_result_event(request, worker_result)],
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
    retry_matrix = stream.get("retryMatrix") if isinstance(stream.get("retryMatrix"), dict) else {}
    return {
        "session_contract_source": str(contract.get("source") or ""),
        "session_contract_owner_unit": str(contract.get("ownerUnit") or ""),
        "session_contract_inventory_exists": str(contract.get("inventoryExists") is True).lower(),
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
            "## Vendored Runtime Modules",
            "",
            *(module_lines or ["- no sidecar module snapshot available"]),
            "",
        ]
    )
