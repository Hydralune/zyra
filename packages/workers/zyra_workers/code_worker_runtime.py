from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable
from zyra_runtime import (
    JsonPermissionStore,
    ToolExecutionContext,
    WorkerRequest,
    WorkerResult,
)

from .code_worker_bridge import CodeWorkerSidecarClient
from .code_query_loop import CodeQueryLoop, CodeQueryLoopConfig, CodeQueryLoopResult, query_turns_from_constraints


@dataclass(frozen=True, slots=True)
class CodeWorkerRun:
    worker_result: WorkerResult
    event_records: list[EventRecord]


class CodeWorkerRuntime:
    """Code worker loop boundary backed by the vendored Claude Code sidecar and Zyra tools."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        sidecar_client: CodeWorkerSidecarClient | None = None,
        permission_store: JsonPermissionStore | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.sidecar_client = sidecar_client or CodeWorkerSidecarClient(self.project_root)
        self.execution_context = ToolExecutionContext.for_workspace(
            workspace_root=workspace_root,
            artifact_root=artifact_root,
            permission_store=permission_store,
        )

    def run(self, request: WorkerRequest) -> CodeWorkerRun:
        sidecar_health = self.sidecar_client.health()
        sidecar_inventory = self.sidecar_client.runtime_inventory()
        sidecar_query_contract = self.sidecar_client.query_contract()
        sidecar_session_contract = self.sidecar_client.session_contract()
        query_turns = query_turns_from_constraints(request.constraints)
        if not query_turns:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="CodeWorkerRuntime requires structured tool_plan or query_turns.",
                error="missing_tool_plan",
                metadata={
                    **_sidecar_metadata(sidecar_health),
                    **_inventory_metadata(sidecar_inventory),
                    **_contract_metadata(sidecar_query_contract),
                    **_session_contract_metadata(sidecar_session_contract),
                },
            )
            return CodeWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(request, worker_result)],
            )

        loop = CodeQueryLoop(
            self.execution_context,
            CodeQueryLoopConfig(
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
                query_contract=sidecar_query_contract,
                session_contract=sidecar_session_contract,
                max_read_only_concurrency=_positive_int(
                    request.constraints.get("max_read_only_concurrency"),
                    default=_contract_default_concurrency(sidecar_query_contract),
                ),
                emit_tool_use_summaries=request.constraints.get("emit_tool_use_summaries") is not False,
            ),
        )
        loop_result = loop.run(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            worker_request_id=request.request_id,
            turns=query_turns,
        )
        artifacts = list(loop_result.artifacts)
        step_summaries = list(loop_result.step_summaries)

        trace_artifact = self.execution_context.artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=_trace_markdown(
                request,
                sidecar_health,
                sidecar_inventory,
                sidecar_query_contract,
                sidecar_session_contract,
                step_summaries,
                loop_result,
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
                **_sidecar_metadata(sidecar_health),
                **_inventory_metadata(sidecar_inventory),
                **_contract_metadata(sidecar_query_contract),
                **_session_contract_metadata(sidecar_session_contract),
                **loop_result.metadata,
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


def _sidecar_metadata(health: dict[str, Any]) -> dict[str, str]:
    vendor = health.get("vendor") if isinstance(health.get("vendor"), dict) else {}
    return {
        "sidecar_runtime": str(health.get("runtime") or ""),
        "sidecar_worker": str(health.get("worker") or ""),
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
    sidecar_health: dict[str, Any],
    sidecar_inventory: dict[str, Any],
    sidecar_query_contract: dict[str, Any],
    sidecar_session_contract: dict[str, Any],
    step_summaries: list[str],
    loop_result: CodeQueryLoopResult,
) -> str:
    vendor = sidecar_health.get("vendor") if isinstance(sidecar_health.get("vendor"), dict) else {}
    modules = vendor.get("modules") if isinstance(vendor.get("modules"), list) else []
    tool_runtime = sidecar_inventory.get("toolRuntime") if isinstance(sidecar_inventory.get("toolRuntime"), dict) else {}
    command_runtime = (
        sidecar_inventory.get("commandRuntime") if isinstance(sidecar_inventory.get("commandRuntime"), dict) else {}
    )
    orchestration = (
        sidecar_query_contract.get("toolOrchestration")
        if isinstance(sidecar_query_contract.get("toolOrchestration"), dict)
        else {}
    )
    source_files = sidecar_query_contract.get("sourceFiles")
    if not isinstance(source_files, list):
        source_files = []
    session_source_files = sidecar_session_contract.get("sourceFiles")
    if not isinstance(session_source_files, list):
        session_source_files = []
    session_persistence = (
        sidecar_session_contract.get("transcriptPersistence")
        if isinstance(sidecar_session_contract.get("transcriptPersistence"), dict)
        else {}
    )
    session_recovery = (
        sidecar_session_contract.get("resumeRecovery")
        if isinstance(sidecar_session_contract.get("resumeRecovery"), dict)
        else {}
    )
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
            f"- query_contract_source: `{sidecar_query_contract.get('source', '')}`",
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
            "## Vendored Runtime Modules",
            "",
            *(module_lines or ["- no sidecar module snapshot available"]),
            "",
        ]
    )
