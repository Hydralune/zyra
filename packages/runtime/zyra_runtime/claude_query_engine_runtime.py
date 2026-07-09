from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, to_jsonable

from .claude_control_commands import (
    ClaudeControlCommandRuntime,
    ClaudeControlRuntimeState,
    control_metadata,
)
from .claude_context_window import (
    ClaudeContextBudget,
    ClaudeContextCompactionReason,
    ClaudeContextWindowManager,
)
from .codeworker_api_foundation import (
    CodeWorkerApiFoundationRuntime,
    codeworker_api_foundation_metadata,
)
from .codeworker_api_foundation_audit import (
    CodeWorkerApiFoundationAuditRuntime,
    codeworker_api_audit_metadata,
)
from .codeworker_compact_state_projection import (
    CodeWorkerCompactStateProjectionRuntime,
    compact_state_projection_metadata,
)
from .codeworker_context_compact_recovery_audit import (
    CodeWorkerContextCompactRecoveryAuditRuntime,
    compact_recovery_audit_metadata,
)
from .codeworker_context_restore_api_state import (
    CodeWorkerContextRestoreApiStateRuntime,
    context_restore_api_state_metadata,
)
from .codeworker_model_recovery_matrix import (
    CodeWorkerModelRecoveryMatrixRuntime,
    model_recovery_matrix_metadata,
)
from .codeworker_restore_causality import (
    CodeWorkerRestoreCausalityRuntime,
    restore_causality_metadata,
)
from .codeworker_disable_semantics import CodeWorkerDisableSemanticsRuntime, disable_semantics_metadata
from .api_retry_playbook_runtime import (
    ApiRetryPlaybookRuntime,
    api_retry_playbook_metadata,
    default_api_retry_playbook_source_decisions,
)
from .compact_restore_policy_runtime import (
    CompactRestorePolicyRuntime,
    compact_restore_policy_metadata,
    default_compact_restore_policy_source_decisions,
)
from .compact_restore_runtime import CompactRestoreRuntime, compact_restore_metadata
from .context_epoch_runtime import ContextEpochRuntime, context_epoch_metadata
from .codeworker_context_security_runtime import CodeWorkerContextSecurityRuntime, context_security_metadata
from .codeworker_restore_integration import (
    CodeWorkerRestoreIntegrationRuntime,
    RestoreContractApplication,
    restore_integration_metadata,
)
from .claude_query_plan import query_turns_from_constraints as planned_query_turns_from_constraints
from .claude_runtime_contracts import (
    PRODUCTIZED_CONTRACT_SOURCE,
    ClaudeRuntimeContractBundle,
    build_productized_claude_runtime_contracts,
)
from .claude_runtime_state import ClaudeRuntimeStateLedger
from .claude_session_lifecycle import ClaudeSessionLifecycleRuntime, session_artifact_metadata
from .claude_tool_use_runtime import ClaudeToolUseRuntime
from .executor import ToolExecutionContext, tool_result_event
from .model_api_runtime import (
    ApiRetryPolicy,
    ApiRetryRuntime,
    ModelStreamRuntime,
    api_retry_metadata,
    model_stream_metadata,
)
from .model_provider_runtime import ModelProviderCatalogRuntime, model_provider_metadata
from .model_stream_watchdog_runtime import ModelStreamWatchdogRuntime, model_stream_watchdog_metadata
from .query_session import QuerySession, QueryStreamEventType, StopReason, snapshot_checkpoint_metadata
from .runtime_budget_replay_runtime import (
    RuntimeBudgetReplayRuntime,
    default_runtime_budget_replay_source_decisions,
    runtime_budget_replay_metadata,
)
from .runtime_budget_state import RuntimeBudgetState, runtime_budget_metadata
from .tool_loop import (
    ToolFailureSignal,
    ToolLoopScheduler,
    tool_failure_signal_from_result,
    watchdog_signal_payload,
)
from .tool_runtime_foundation import (
    TOOL_LOOP_FOUNDATION_OWNER_UNIT,
    ToolExecutionReceipt,
    ToolExecutionRuntime,
    ToolRegistryRuntime,
    ToolResultBudgetRuntime,
    ToolRuntimeDisabledError,
    ToolUseContext,
)
from .tool_runtime_foundation_audit import (
    ToolFoundationAuditRuntime,
    tool_foundation_audit_metadata,
)
from .tool_runtime_foundation_persistence import (
    ToolFoundationPersistenceRuntime,
    tool_foundation_persistence_metadata,
)
from .tool_runtime_cleanroom import ToolCleanroomRuntime, tool_cleanroom_metadata
from .tool_runtime_concurrency import ToolConcurrencyRuntime, tool_concurrency_metadata
from .tool_runtime_contract_gate import ToolContractGateRuntime, tool_contract_gate_metadata
from .tool_runtime_continuation import ToolContinuationRuntime, tool_continuation_metadata
from .tool_runtime_continuation_packet import (
    ToolContinuationPacketRuntime,
    tool_continuation_packet_metadata,
)
from .tool_runtime_effect_fingerprint import (
    ToolEffectFingerprintRuntime,
    tool_effect_fingerprint_metadata,
)
from .tool_runtime_budget_chain import ToolBudgetChainRuntime, tool_budget_chain_metadata
from .tool_runtime_execution_timeline import (
    ToolExecutionTimelineRuntime,
    tool_execution_timeline_metadata,
)
from .tool_runtime_failure_policy import ToolFailurePolicyRuntime, tool_failure_policy_metadata
from .tool_runtime_integration_audit import ToolIntegrationAuditRuntime, tool_integration_metadata
from .tool_runtime_output_store import ToolOutputStoreRuntime, tool_output_store_metadata
from .tool_runtime_permission_checkpoint import (
    ToolPermissionCheckpointRuntime,
    tool_permission_checkpoint_metadata,
)
from .tool_runtime_permission_handoff import (
    ToolPermissionHandoffRuntime,
    tool_permission_handoff_metadata,
)
from .tool_runtime_permission_session import (
    ToolPermissionSessionRuntime,
    tool_permission_session_metadata,
)
from .tool_runtime_readiness_matrix import ToolReadinessMatrixRuntime, tool_readiness_matrix_metadata
from .tool_runtime_replay_state import ToolReplayStateRuntime, tool_replay_state_metadata
from .tool_runtime_result_context import ToolResultContextRuntime, tool_result_context_metadata
from .tool_runtime_result_replay_index import (
    ToolResultReplayIndexRuntime,
    tool_result_replay_index_metadata,
)
from .tool_runtime_semantic_effects import ToolSemanticEffectRuntime, tool_semantic_effect_metadata
from .tool_runtime_session_bridge import ToolSessionBridgeReport, tool_session_bridge_metadata
from .tool_runtime_source_effects import ToolSourceEffectRuntime, tool_source_effects_metadata
from .tool_runtime_source_decisions import ToolSourceCoverageRuntime, tool_source_coverage_metadata
from .tool_runtime_budget_policy import ToolBudgetPolicyRuntime, tool_budget_policy_metadata
from .tool_runtime_settlement import ToolSettlementRuntime, tool_settlement_metadata
from .tool_runtime_streaming import ToolStreamingRuntime, tool_streaming_metadata
from .tools import ToolResult


@dataclass(frozen=True, slots=True)
class ClaudeQueryEngineConfig:
    max_turns: int | None = None
    max_tool_result_chars: int = 8000
    max_query_context_chars: int = 32000
    continue_on_error: bool = False
    max_read_only_concurrency: int = 10
    emit_tool_use_summaries: bool = True
    runtime_contracts: ClaudeRuntimeContractBundle | None = None
    trace_title: str = "CodeWorker Runtime Trace"
    context_artifact_title: str = "CodeWorker context compaction"
    allow_empty_turns: bool = False
    control_commands: Sequence[Any] = field(default_factory=tuple)
    project_root: str | Path | None = None
    session_seed: Mapping[str, Any] | None = None
    context_snapshot: Mapping[str, Any] | None = None
    preprocessed_messages: Sequence[Any] = field(default_factory=tuple)
    session_foundation_metadata: Mapping[str, str] = field(default_factory=dict)
    max_turn_tool_result_chars: int | None = None
    disable_tool_registry_runtime: bool = False
    disable_tool_execution_runtime: bool = False
    disable_tool_result_budget_runtime: bool = False
    disable_tool_permission_handoff_runtime: bool = False
    disable_runtime_budget_state: bool = False
    disable_compact_restore_runtime: bool = False
    disable_model_stream_runtime: bool = False
    disable_api_retry_runtime: bool = False
    disable_codeworker_api_foundation_runtime: bool = False
    disable_context_security_runtime: bool = False
    disable_restore_integration_runtime: bool = False
    model_name: str = "zyra-local-code-model"
    model_input_token_limit: int = 200000
    model_output_token_limit: int = 8192
    api_retry_max_attempts: int = 3
    api_retry_fallback_models: Sequence[str] = field(default_factory=lambda: ("zyra-local-fallback",))
    runtime_constraints: Mapping[str, Any] = field(default_factory=dict)
    session_bridge_report: ToolSessionBridgeReport | None = None

    @property
    def contracts(self) -> ClaudeRuntimeContractBundle:
        return self.runtime_contracts or build_productized_claude_runtime_contracts()


@dataclass(frozen=True, slots=True)
class ClaudeQueryEngineResult:
    ok: bool
    event_records: list[EventRecord]
    artifacts: list[ArtifactRef]
    step_summaries: list[str]
    turn_count: int
    tool_call_count: int
    context_compaction_count: int = 0
    stopped_reason: str | None = None
    session_snapshot: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeContextEntry:
    turn_index: int
    batch_index: int
    step_index: int
    tool_name: str
    tool_call_id: str
    summary: str
    ok: bool
    chars: int

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


class ZyraClaudeQueryEngine:
    """Zyra-owned QueryEngine port for structured CodeWorker turns.

    The upstream Claude Code QueryEngine combines model streaming, tool
    orchestration, permission handling, session persistence, compact boundaries,
    and SDK result shaping. Zyra's default CodeWorker path keeps the same
    responsibilities but binds them to Zyra stores: QuerySession for transcript
    state, ToolLoopScheduler for batching, ToolExecutor for permission-gated
    tool calls, LocalArtifactStore for large outputs and snapshots, and
    EventRecord for runtime observability.
    """

    def __init__(self, context: ToolExecutionContext, config: ClaudeQueryEngineConfig | None = None) -> None:
        self.context = context
        self.config = config or ClaudeQueryEngineConfig()
        self.contracts = self.config.contracts

    def run(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        turns: Sequence[Sequence[Mapping[str, Any]]],
        request_messages: Sequence[Any] = (),
        request_metadata: Mapping[str, str] | None = None,
    ) -> ClaudeQueryEngineResult:
        normalized_turns = _normalize_turns(turns)
        if not normalized_turns and not self.config.allow_empty_turns:
            return self._missing_plan_result(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                request_messages=request_messages,
            )

        disabled_component = self._disabled_tool_foundation_component()
        if disabled_component:
            return self._tool_foundation_disabled_result(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                component=disabled_component,
            )
        registry_runtime = ToolRegistryRuntime(
            self.context.registry,
            permission_policy=self.context.permission_policy,
            disabled=self.config.disable_tool_registry_runtime,
        )
        session_seed = dict(self.config.session_seed or {})
        seed_session_id = str(session_seed.get("session_id") or "")
        session = QuerySession(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_request_id=worker_request_id,
            session_id=seed_session_id or None,
            source_contract={
                "runtime_id": self.contracts.runtime_id,
                "contract_source": self.contracts.contract_source,
                "query_contract": self.contracts.query_contract,
                "session_contract": self.contracts.session_contract,
                "tool_loop_contract": self.contracts.tool_loop_contract,
                "session_seed": session_seed,
                "context_snapshot": dict(self.config.context_snapshot or {}),
            },
            metadata={
                "contract_source": self.contracts.contract_source,
                "runtime_id": self.contracts.runtime_id,
                **{str(k): str(v) for k, v in dict(self.config.session_foundation_metadata or {}).items()},
                **dict(request_metadata or {}),
            },
        )
        materialization = registry_runtime.materialize(
            worker_request_id=worker_request_id,
            session_id=session.session_id,
            workspace_root=self.context.workspace_root,
        )
        scheduler = ToolLoopScheduler(
            materialization.to_registry(),
            max_read_only_concurrency=max(1, self.config.max_read_only_concurrency),
            source_contract=self.contracts.tool_loop_contract,
        )
        budget_runtime = ToolResultBudgetRuntime(
            max_result_chars=max(1, self.config.max_tool_result_chars),
            max_turn_chars=self.config.max_turn_tool_result_chars,
            disabled=self.config.disable_tool_result_budget_runtime,
        )
        execution_runtime = ToolExecutionRuntime(
            self.context,
            scheduler=scheduler,
            budget_runtime=budget_runtime,
            disabled=self.config.disable_tool_execution_runtime,
        )
        persistence_runtime = ToolFoundationPersistenceRuntime(self.context.artifact_store)
        settlement_runtime = ToolSettlementRuntime()
        streaming_runtime = ToolStreamingRuntime()
        continuation_runtime = ToolContinuationRuntime()
        cleanroom_runtime = ToolCleanroomRuntime()
        concurrency_runtime = ToolConcurrencyRuntime()
        contract_gate_runtime = ToolContractGateRuntime()
        continuation_packet_runtime = ToolContinuationPacketRuntime()
        effect_fingerprint_runtime = ToolEffectFingerprintRuntime()
        budget_chain_runtime = ToolBudgetChainRuntime()
        execution_timeline_runtime = ToolExecutionTimelineRuntime()
        failure_policy_runtime = ToolFailurePolicyRuntime()
        integration_audit_runtime = ToolIntegrationAuditRuntime()
        output_store_runtime = ToolOutputStoreRuntime()
        permission_checkpoint_runtime = ToolPermissionCheckpointRuntime()
        readiness_matrix_runtime = ToolReadinessMatrixRuntime()
        result_context_runtime = ToolResultContextRuntime()
        result_replay_index_runtime = ToolResultReplayIndexRuntime()
        replay_state_runtime = ToolReplayStateRuntime()
        semantic_effect_runtime = ToolSemanticEffectRuntime()
        permission_session_runtime = ToolPermissionSessionRuntime()
        source_coverage_runtime = ToolSourceCoverageRuntime()
        source_effect_runtime = ToolSourceEffectRuntime()
        budget_policy_runtime = ToolBudgetPolicyRuntime(
            tool_result_limit=max(1, self.config.max_tool_result_chars),
            turn_limit=self.config.max_turn_tool_result_chars,
            session_limit=self.config.max_query_context_chars,
        )
        runtime_budget_state = RuntimeBudgetState(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            context_limit_chars=max(1, self.config.max_query_context_chars),
            tool_result_limit_chars=max(1, self.config.max_tool_result_chars),
            model_input_token_limit=max(1, self.config.model_input_token_limit),
            model_output_token_limit=max(1, self.config.model_output_token_limit),
            retry_limit=max(0, self.config.api_retry_max_attempts),
            disabled=self.config.disable_runtime_budget_state,
            metadata={
                "source_path": "packages/runtime/zyra_runtime/runtime_budget_state.py",
                "upstream_source_path": "opencode/packages/opencode/src/session",
            },
        )
        compact_restore_runtime = CompactRestoreRuntime(disabled=self.config.disable_compact_restore_runtime)
        compact_restore_policy_runtime = CompactRestorePolicyRuntime()
        model_provider_catalog_runtime = ModelProviderCatalogRuntime()
        model_stream_runtime = ModelStreamRuntime(disabled=self.config.disable_model_stream_runtime)
        api_retry_runtime = ApiRetryRuntime(
            disabled=self.config.disable_api_retry_runtime,
            policy=ApiRetryPolicy(
                max_attempts=max(1, self.config.api_retry_max_attempts),
                fallback_models=tuple(self.config.api_retry_fallback_models),
            ),
        )
        api_retry_playbook_runtime = ApiRetryPlaybookRuntime()
        codeworker_api_foundation_runtime = CodeWorkerApiFoundationRuntime(
            disabled=self.config.disable_codeworker_api_foundation_runtime
        )
        context_epoch_runtime = ContextEpochRuntime()
        model_stream_watchdog_runtime = ModelStreamWatchdogRuntime()
        runtime_budget_replay_runtime = RuntimeBudgetReplayRuntime()
        codeworker_api_audit_runtime = CodeWorkerApiFoundationAuditRuntime()
        compact_state_projection_runtime = CodeWorkerCompactStateProjectionRuntime()
        compact_recovery_audit_runtime = CodeWorkerContextCompactRecoveryAuditRuntime()
        context_restore_api_state_runtime = CodeWorkerContextRestoreApiStateRuntime()
        model_recovery_matrix_runtime = CodeWorkerModelRecoveryMatrixRuntime()
        restore_causality_runtime = CodeWorkerRestoreCausalityRuntime()
        disable_semantics_runtime = CodeWorkerDisableSemanticsRuntime()
        context_security_runtime = CodeWorkerContextSecurityRuntime(
            disabled=self.config.disable_context_security_runtime
        )
        restore_integration_runtime = CodeWorkerRestoreIntegrationRuntime(
            security_runtime=context_security_runtime,
            disabled=self.config.disable_restore_integration_runtime,
        )

        event_records: list[EventRecord] = []
        artifacts: list[ArtifactRef] = []
        step_summaries: list[str] = []
        context_entries: list[RuntimeContextEntry] = []
        failure_signals: list[ToolFailureSignal] = []
        tool_use_context_snapshots: list[dict[str, Any]] = []
        tool_execution_receipts: list[dict[str, Any]] = []
        settlement_reports: list[Any] = []
        tool_streaming_traces: list[Any] = []
        model_provider_reports: list[Any] = []
        model_stream_reports: list[Any] = []
        api_retry_reports: list[Any] = []
        restore_applications: list[RestoreContractApplication] = []
        pending_restore_contract: Any = None
        context_window = ClaudeContextWindowManager(
            budget=ClaudeContextBudget(
                max_chars=self.config.max_query_context_chars,
                reserve_chars=0,
                min_recent_blocks=1,
            ),
            runtime_source=self.contracts.contract_source,
            runtime_id=self.contracts.runtime_id,
            session_id=session.session_id,
            request_id=worker_request_id,
        )
        context_window.seed_request_messages(request_messages)
        tool_runtime = ClaudeToolUseRuntime(
            runtime_source=self.contracts.contract_source,
            runtime_id=self.contracts.runtime_id,
            owner_unit=str(self.contracts.tool_loop_contract.get("ownerUnit") or TOOL_LOOP_FOUNDATION_OWNER_UNIT),
        )
        session_lifecycle = ClaudeSessionLifecycleRuntime(
            artifact_store=self.context.artifact_store,
            runtime_source=self.contracts.contract_source,
            runtime_id=self.contracts.runtime_id,
            owner_unit=str(self.contracts.session_contract.get("ownerUnit") or "M1-02A"),
        )
        state_ledger = ClaudeRuntimeStateLedger(
            runtime_id=self.contracts.runtime_id,
            runtime_source=self.contracts.contract_source,
            session_id=session.session_id,
        )
        state_ledger.record_session_started(session.session_id, worker_request_id=worker_request_id)
        context_chars = context_window.active_chars
        compaction_count = 0
        tool_use_summary_count = 0
        tool_budget_externalization_count = 0
        tool_failure_signal_count = 0
        tool_schema_error_count = 0
        conflict_protected_count = 0
        executed_turn_count = 0
        tool_call_count = 0
        ok = True
        stopped_reason: str | None = None
        max_turns = self.config.max_turns or len(normalized_turns)
        materialization_artifact = persistence_runtime.persist_materialization(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            materialization=materialization.to_dict(),
        )
        artifacts.append(materialization_artifact.artifact)

        self._append_lifecycle(
            event_records,
            session,
            run_id,
            task_id,
            node_id,
            worker_request_id,
            "session_started",
            {
                "runtime_id": self.contracts.runtime_id,
                "contract_source": self.contracts.contract_source,
                "max_turns": max_turns,
                "tool_result_budget_chars": self.config.max_tool_result_chars,
                "query_context_budget_chars": self.config.max_query_context_chars,
                "resume_token": session.resume_token,
            },
        )
        if session_seed:
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "query_session_seed_attached",
                {
                    "seed_session_id": str(session_seed.get("session_id") or ""),
                    "seed_status": str(session_seed.get("status") or ""),
                    "seed_ok": bool(session_seed.get("ok")),
                    "input_count": len(
                        (
                            session_seed.get("input_report", {}).get("records", [])
                            if isinstance(session_seed.get("input_report"), Mapping)
                            else []
                        )
                    ),
                    "store_path": str(
                        (
                            session_seed.get("store_receipt", {}).get("path", "")
                            if isinstance(session_seed.get("store_receipt"), Mapping)
                            else ""
                        )
                    ),
                    "resume_token": session.resume_token,
                },
            )
        if self.config.context_snapshot:
            context_snapshot = dict(self.config.context_snapshot)
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "context_snapshot_attached",
                {
                    "context_snapshot_id": str(context_snapshot.get("snapshot_id") or ""),
                    "context_fingerprint": str(context_snapshot.get("fingerprint") or ""),
                    "selected_block_count": len(context_snapshot.get("selected_blocks") or []),
                    "active_chars": str(context_snapshot.get("active_chars") or ""),
                    "resume_token": session.resume_token,
                },
            )
        self._append_lifecycle(
            event_records,
            session,
            run_id,
            task_id,
            node_id,
            worker_request_id,
            "permission_runtime_attached",
            {
                "permission": self._permission_runtime_payload(),
                "source_path": "packages/runtime/zyra_runtime/permissions.py",
                "upstream_source_path": "src/cli/src/utils/permissions/*",
                "resume_token": session.resume_token,
            },
        )
        self._append_lifecycle(
            event_records,
            session,
            run_id,
            task_id,
            node_id,
            worker_request_id,
            "tool_registry_materialized",
            {
                "runtime_id": materialization.runtime_id,
                "owner_unit": materialization.owner_unit,
                "materialization_id": materialization.materialization_id,
                "active_tool_names": list(materialization.active_tool_names),
                "active_tool_count": len(materialization.active_tools),
                "filtered_tool_names": list(materialization.filtered_tool_names),
                "ask_visible_tool_names": list(materialization.ask_visible_tool_names),
                "source_ledger_count": len(materialization.source_ledger),
                "materialization_artifact_id": materialization_artifact.artifact.artifact_id,
                "resume_token": session.resume_token,
            },
        )
        if self.config.session_bridge_report is not None:
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "tool_session_bridge_attached",
                {
                    "bridge_report_id": self.config.session_bridge_report.report_id,
                    "origin": str(self.config.session_bridge_report.origin),
                    "assistant_tool_use_count": self.config.session_bridge_report.valid_tool_use_count,
                    "opencode_tool_use_count": self.config.session_bridge_report.opencode_tool_use_count,
                    "fallback_used": str(self.config.session_bridge_report.fallback_used).lower(),
                    "resume_token": session.resume_token,
                },
            )
        runtime_budget_state.record_context_usage(
            active_chars=context_window.active_chars,
            source="query_engine_initial_context",
            metadata={"phase": "runtime_budget_attached"},
        )
        initial_budget_snapshot = runtime_budget_state.snapshot()
        event_records.append(
            runtime_budget_state.event_for_snapshot(
                initial_budget_snapshot,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                phase="runtime_budget_attached",
            )
        )
        session.record_batch_event(
            QueryStreamEventType.RUNTIME_BUDGET_UPDATED,
            metadata={
                "phase": "runtime_budget_attached",
                "runtime_budget_state": initial_budget_snapshot.to_dict(),
            },
        )

        for turn_index, turn in enumerate(normalized_turns, start=1):
            if turn_index > max_turns:
                ok = False
                stopped_reason = "max_turns_exceeded"
                session.record_error(
                    error=stopped_reason,
                    stop_reason=StopReason.MAX_TURNS_EXCEEDED,
                    metadata={"turn_index": turn_index, "max_turns": max_turns},
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "session_stopped",
                    {"turn_index": turn_index, "stopped_reason": stopped_reason, "resume_token": session.resume_token},
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "error",
                    {
                        "turn_index": turn_index,
                        "error": stopped_reason,
                        "stop_reason": str(StopReason.MAX_TURNS_EXCEEDED),
                        "resume_token": session.resume_token,
                    },
                )
                break

            executed_turn_count += 1
            turn_started_at_count = tool_call_count
            user_content = _turn_user_content(turn_index, turn)
            context_window.record_turn_prompt(
                turn_index=turn_index,
                text=user_content,
                metadata={"planned_tool_calls": len(turn)},
            )
            turn_state = session.start_turn(
                turn_index,
                user_content=user_content,
                metadata={
                    "planned_tool_calls": len(turn),
                    "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                    "upstream_source_path": "src/QueryEngine.ts",
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "turn_started",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "planned_tool_calls": len(turn),
                    "resume_token": session.resume_token,
                },
            )
            state_ledger.record_turn(turn_state.turn_id, turn_index=turn_index)
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "turn_start",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "planned_tool_calls": len(turn),
                    "resume_token": session.resume_token,
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "message_delta",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "role": "user",
                    "delta": user_content,
                    "resume_token": session.resume_token,
                },
            )
            turn_restore_model_messages: list[Mapping[str, Any]] = []
            if pending_restore_contract is not None:
                restore_application = restore_integration_runtime.apply_contract(
                    pending_restore_contract,
                    context_window=context_window,
                    budget_state=runtime_budget_state,
                    turn_index=turn_index,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    metadata={
                        "turn_id": turn_state.turn_id,
                        "resume_token": session.resume_token,
                    },
                )
                pending_restore_contract = None
                if restore_application is not None:
                    restore_applications.append(restore_application)
                    event_records.append(
                        restore_integration_runtime.event_for_application(
                            restore_application,
                            run_id=run_id,
                            task_id=task_id,
                            node_id=node_id,
                        )
                    )
                    if restore_application.security_snapshot is not None:
                        event_records.append(
                            context_security_runtime.event_for_snapshot(
                                restore_application.security_snapshot,
                                run_id=run_id,
                                task_id=task_id,
                                node_id=node_id,
                                session_id=session.session_id,
                                worker_request_id=worker_request_id,
                                phase="codeworker_restore_context_security",
                            )
                        )
                    session.record_batch_event(
                        QueryStreamEventType.CONTEXT_RESTORED,
                        turn_id=turn_state.turn_id,
                        metadata={
                            "turn_index": turn_index,
                            "restore_application": restore_application.to_dict(),
                            "restore_contract_id": restore_application.contract_id,
                            "restore_model_message_count": len(restore_application.model_messages),
                            "restore_context_block_count": len(restore_application.blocks),
                        },
                    )
                    self._append_lifecycle(
                        event_records,
                        session,
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        "codeworker_restore_context_applied",
                        {
                            "turn_index": turn_index,
                            "turn_id": turn_state.turn_id,
                            "restore_application_id": restore_application.application_id,
                            "restore_contract_id": restore_application.contract_id,
                            "restore_model_message_count": len(restore_application.model_messages),
                            "restore_context_block_count": len(restore_application.blocks),
                            "ok": restore_application.ok,
                            "resume_token": session.resume_token,
                        },
                    )
                    turn_restore_model_messages = [
                        message for message in restore_application.model_messages if isinstance(message, Mapping)
                    ]
                    if not restore_application.ok and stopped_reason is None:
                        ok = False
                        stopped_reason = "restore_integration_failed"
                        session.record_error(
                            error=stopped_reason,
                            stop_reason=StopReason.TOOL_ERROR,
                            metadata={
                                "turn_index": turn_index,
                                "restore_application": restore_application.to_dict(),
                            },
                        )
                        self._append_lifecycle(
                            event_records,
                            session,
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            "error",
                            {
                                "turn_index": turn_index,
                                "turn_id": turn_state.turn_id,
                                "error": stopped_reason,
                                "stop_reason": str(StopReason.TOOL_ERROR),
                                "restore_application_id": restore_application.application_id,
                                "resume_token": session.resume_token,
                            },
                        )
            session.start_stream_request(
                turn_id=turn_state.turn_id,
                metadata={
                    "turn_index": turn_index,
                    "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                    "upstream_source_path": "src/query.ts",
                    "contract_source": self.contracts.contract_source,
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "stream_request_start",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                    "upstream_source_path": "src/query.ts",
                    "contract_source": self.contracts.contract_source,
                    "resume_token": session.resume_token,
                },
            )
            model_provider_report = model_provider_catalog_runtime.build_report(
                requested_model=self.config.model_name,
                fallback_models=tuple(self.config.api_retry_fallback_models),
                constraints=self.config.runtime_constraints,
            )
            model_provider_reports.append(model_provider_report)
            event_records.append(
                model_provider_catalog_runtime.event_for_report(
                    model_provider_report,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    session_id=session.session_id,
                    worker_request_id=worker_request_id,
                )
            )
            session.record_batch_event(
                QueryStreamEventType.MODEL_PROVIDER_CATALOG,
                turn_id=turn_state.turn_id,
                metadata=model_provider_report.to_dict(),
            )
            model_envelope = model_stream_runtime.build_envelope(
                session_id=session.session_id,
                worker_request_id=worker_request_id,
                turn_id=turn_state.turn_id,
                turn_index=turn_index,
                model=model_provider_report.route.selected_model.model_id,
                messages=[
                    *[item for item in request_messages if isinstance(item, Mapping)],
                    *turn_restore_model_messages,
                    {"role": "user", "content": user_content},
                ],
                context_chars=context_window.active_chars,
                context_limit_chars=self.config.max_query_context_chars,
                tool_call_count=len(turn),
                metadata={
                    "source_path": "packages/runtime/zyra_runtime/model_api_runtime.py",
                    "upstream_source_path": "src/services/api/claude.ts",
                    "restore_model_message_count": str(len(turn_restore_model_messages)),
                    "restore_application_id": restore_applications[-1].application_id
                    if restore_applications and restore_applications[-1].turn_index == turn_index
                    else "",
                    "restore_contract_id": restore_applications[-1].contract_id
                    if restore_applications and restore_applications[-1].turn_index == turn_index
                    else "",
                    "restore_context_block_count": str(
                        len(restore_applications[-1].blocks)
                        if restore_applications and restore_applications[-1].turn_index == turn_index
                        else 0
                    ),
                },
            )
            model_stream_report = model_stream_runtime.stream(
                envelope=model_envelope,
                budget_state=runtime_budget_state,
                constraints=self.config.runtime_constraints,
            )
            model_stream_reports.append(model_stream_report)
            event_records.extend(
                model_stream_runtime.events_for_report(
                    model_stream_report,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                )
            )
            for frame in model_stream_report.frames:
                session.record_batch_event(
                    QueryStreamEventType.MODEL_STREAM_FRAME,
                    turn_id=turn_state.turn_id,
                    metadata={
                        "turn_index": turn_index,
                        "model_stream_report_id": model_stream_report.report_id,
                        "frame": frame.to_dict(),
                    },
                )
            session.record_batch_event(
                QueryStreamEventType.MODEL_STREAM_REPORT,
                turn_id=turn_state.turn_id,
                metadata=model_stream_report.to_dict(),
            )
            api_retry_report = api_retry_runtime.build_report(
                stream_report=model_stream_report,
                budget_state=runtime_budget_state,
                constraints=self.config.runtime_constraints,
            )
            api_retry_reports.append(api_retry_report)
            event_records.append(
                api_retry_runtime.event_for_report(
                    api_retry_report,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                )
            )
            session.record_batch_event(
                QueryStreamEventType.API_RETRY_REPORT,
                turn_id=turn_state.turn_id,
                metadata=api_retry_report.to_dict(),
            )
            budget_snapshot = runtime_budget_state.snapshot()
            event_records.append(
                runtime_budget_state.event_for_snapshot(
                    budget_snapshot,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    phase="runtime_budget_updated",
                )
            )
            session.record_batch_event(
                QueryStreamEventType.RUNTIME_BUDGET_UPDATED,
                turn_id=turn_state.turn_id,
                metadata={
                    "turn_index": turn_index,
                    "runtime_budget_state": budget_snapshot.to_dict(),
                },
            )
            session.start_assistant_message(
                turn_id=turn_state.turn_id,
                metadata={
                    "turn_index": turn_index,
                    "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                },
            )
            assistant_delta = model_stream_report.assistant_message or (
                f"Executing Zyra-owned Claude Code turn {turn_index} with {len(turn)} planned tool call(s)."
            )
            if not model_stream_report.ok and api_retry_report.recovered:
                assistant_delta = (
                    f"Recovered {model_stream_report.error_kind} through {api_retry_report.status}; "
                    f"executing turn {turn_index} with {len(turn)} planned tool call(s)."
                )
            context_window.record_assistant_delta(
                turn_index=turn_index,
                text=assistant_delta,
                metadata={
                    "planned_tool_calls": len(turn),
                    "model_stream_report_id": model_stream_report.report_id,
                    "api_retry_report_id": api_retry_report.report_id,
                },
            )
            session.append_assistant_delta(
                assistant_delta,
                metadata={
                    "turn_index": turn_index,
                    "planned_tool_calls": len(turn),
                    "model_stream_report_id": model_stream_report.report_id,
                    "api_retry_report_id": api_retry_report.report_id,
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "message_delta",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "role": "assistant",
                    "delta": assistant_delta,
                    "resume_token": session.resume_token,
                },
            )
            tool_use_context = ToolUseContext.for_turn(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                session_id=session.session_id,
                turn_id=turn_state.turn_id,
                turn_index=turn_index,
                materialization=materialization,
                source_contract=self.contracts.tool_loop_contract,
                seed_messages=[
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": assistant_delta},
                ],
            )

            tool_loop_plan = scheduler.plan_turn(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                turn_index=turn_index,
                steps=turn,
            )
            tool_schema_error_count += tool_loop_plan.schema_error_count
            conflict_protected_count += tool_loop_plan.conflict_protected_count
            tool_runtime.register_plan(tool_loop_plan)
            settlement_report = settlement_runtime.settle_plan(
                materialization=materialization,
                plan=tool_loop_plan,
                session_id=session.session_id,
                turn_id=turn_state.turn_id,
            )
            settlement_reports.append(settlement_report)
            event_records.append(
                settlement_runtime.event_for_report(
                    settlement_report,
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                )
            )
            session.record_batch_event(QueryStreamEventType.TOOL_LOOP_PLAN, turn_id=turn_state.turn_id, metadata=tool_loop_plan.to_dict())
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "tool_loop_plan",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "tool_count": len(tool_loop_plan.requests),
                    "batch_count": len(tool_loop_plan.batches),
                    "read_only_count": tool_loop_plan.read_only_count,
                    "write_count": tool_loop_plan.write_count,
                    "schema_error_count": tool_loop_plan.schema_error_count,
                    "conflict_protected_count": tool_loop_plan.conflict_protected_count,
                    "source_path": "packages/runtime/zyra_runtime/tool_loop.py",
                    "upstream_source_path": "src/services/tools/toolOrchestration.ts",
                    "resume_token": session.resume_token,
                },
            )
            if not settlement_report.ok:
                ok = False
                stopped_reason = "tool_registry_settlement_failed"
                session.record_error(
                    error=stopped_reason,
                    stop_reason=StopReason.TOOL_ERROR,
                    metadata={
                        "turn_index": turn_index,
                        "settlement_report": settlement_report.to_dict(),
                    },
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "error",
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "error": stopped_reason,
                        "stop_reason": str(StopReason.TOOL_ERROR),
                        "settlement_report_id": settlement_report.report_id,
                        "resume_token": session.resume_token,
                    },
                )
                break

            for batch in tool_loop_plan.batches:
                batch_index = batch.batch_index
                batch_requests = batch.requests
                execution_mode = str(batch.execution_mode)
                session.record_batch_event(
                    QueryStreamEventType.TOOL_BATCH_STARTED,
                    turn_id=turn_state.turn_id,
                    metadata={
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "tool_count": len(batch_requests),
                        "execution_mode": execution_mode,
                        "conflict_keys": batch.conflict_keys,
                        "conflict_protected": batch.conflict_protected,
                    },
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "tool_batch_started",
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "batch_index": batch_index,
                        "tool_count": len(batch_requests),
                        "execution_mode": execution_mode,
                        "source_path": "packages/runtime/zyra_runtime/tool_loop.py",
                        "upstream_source_path": "src/services/tools/toolOrchestration.ts",
                        "tool_names": [planned.call.tool_name for planned in batch_requests],
                        "read_only": str(all(planned.read_only for planned in batch_requests)).lower(),
                        "conflict_keys": batch.conflict_keys,
                        "conflict_protected": str(batch.conflict_protected).lower(),
                    },
                )
                for planned in batch_requests:
                    self._append_lifecycle(
                        event_records,
                        session,
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        "assistant_tool_use_accepted",
                        {
                            "turn_index": turn_index,
                            "turn_id": turn_state.turn_id,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "tool_call_id": planned.call.tool_call_id,
                            "tool_name": planned.call.tool_name,
                            "assistant_tool_use_id": planned.call.metadata.get("assistant_tool_use_id", ""),
                            "tool_session_bridge_origin": planned.call.metadata.get("tool_session_bridge_origin", ""),
                            "tool_session_bridge_format": planned.call.metadata.get("tool_session_bridge_format", ""),
                            "through_runtime": "ToolExecutionRuntime",
                            "resume_token": session.resume_token,
                        },
                    )
                    tool_runtime.mark_started(planned, batch_index=batch_index)
                    session.record_tool_call(
                        tool_call_id=planned.call.tool_call_id,
                        tool_name=planned.call.tool_name,
                        turn_id=turn_state.turn_id,
                        metadata={
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "read_only": planned.read_only,
                        },
                    )
                    self._append_lifecycle(
                        event_records,
                        session,
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        "tool_call_started",
                        {
                            "turn_index": turn_index,
                            "turn_id": turn_state.turn_id,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "tool_call_id": planned.call.tool_call_id,
                            "tool_name": planned.call.tool_name,
                            "read_only": str(planned.read_only).lower(),
                            "access_mode": str(planned.access_mode),
                            "conflict_key": planned.conflict_key,
                            "schema_error_count": len(planned.schema_errors),
                        },
                    )

                streaming_trace = streaming_runtime.execute_batch(
                    execution_runtime,
                    batch,
                    tool_context=tool_use_context,
                    max_workers=max(1, self.config.max_read_only_concurrency),
                )
                tool_streaming_traces.append(streaming_trace)
                event_records.extend(
                    streaming_runtime.events_for_trace(
                        streaming_trace,
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                    )
                )
                receipts = list(streaming_trace.receipts)
                batch_summaries: list[dict[str, Any]] = []
                bounded_results_for_batch: list[ToolResult] = []
                budget_decisions_for_batch: list[Any] = []
                for planned, receipt in zip(batch_requests, receipts, strict=True):
                    tool_execution_receipts.append(receipt.to_dict())
                    bounded_result = receipt.bounded_result
                    budget_decision = receipt.budget_decision
                    bounded_results_for_batch.append(bounded_result)
                    budget_decisions_for_batch.append(budget_decision if budget_decision.applied else None)
                    artifacts.extend(bounded_result.artifacts)
                    observed_signal: ToolFailureSignal | None = None
                    if budget_decision.applied:
                        tool_budget_externalization_count += 1
                        budget_signal = tool_failure_signal_from_result(
                            planned,
                            bounded_result,
                            budget_decision=budget_decision,
                        )
                        if budget_signal is not None:
                            observed_signal = budget_signal
                            failure_signals.append(budget_signal)
                            tool_failure_signal_count += 1
                            self._append_tool_signal_events(
                                event_records,
                                session,
                                run_id=run_id,
                                task_id=task_id,
                                node_id=node_id,
                                worker_request_id=worker_request_id,
                                session_id=session.session_id,
                                turn_id=turn_state.turn_id,
                                turn_index=turn_index,
                                batch_index=batch_index,
                                step_index=planned.step_index,
                                signal=budget_signal,
                            )
                        self._append_lifecycle(
                            event_records,
                            session,
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            "tool_result_budget_exceeded",
                            {
                                "turn_index": turn_index,
                                "turn_id": turn_state.turn_id,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "budget": budget_decision.to_dict(),
                                "resume_token": session.resume_token,
                            },
                        )

                    signal = tool_failure_signal_from_result(planned, bounded_result)
                    if signal is not None:
                        observed_signal = signal
                        failure_signals.append(signal)
                        tool_failure_signal_count += 1
                        self._append_tool_signal_events(
                            event_records,
                            session,
                            run_id=run_id,
                            task_id=task_id,
                            node_id=node_id,
                            worker_request_id=worker_request_id,
                            session_id=session.session_id,
                            turn_id=turn_state.turn_id,
                            turn_index=turn_index,
                            batch_index=batch_index,
                            step_index=planned.step_index,
                            signal=signal,
                        )

                    result_block = tool_runtime.record_result(
                        planned,
                        bounded_result,
                        batch_index=batch_index,
                        budget_decision=budget_decision if budget_decision.applied else None,
                        failure_signal=observed_signal,
                    )
                    for application in receipt.modifier_applications:
                        self._append_lifecycle(
                            event_records,
                            session,
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            "tool_context_modifier_applied",
                            {
                                "turn_index": turn_index,
                                "turn_id": turn_state.turn_id,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.tool_name,
                                "modifier": application,
                                "resume_token": session.resume_token,
                            },
                        )
                    state_ledger.record_tool_result(
                        tool_call_id=planned.call.tool_call_id,
                        tool_name=planned.call.tool_name,
                        ok=bounded_result.ok,
                        artifact_ids=[artifact.artifact_id for artifact in bounded_result.artifacts],
                        turn_id=turn_state.turn_id,
                        error=bounded_result.error,
                    )
                    session.record_tool_result(
                        tool_call_id=planned.call.tool_call_id,
                        tool_name=planned.call.tool_name,
                        summary=bounded_result.summary,
                        ok=bounded_result.ok,
                        error=bounded_result.error,
                        artifacts=[artifact.artifact_id for artifact in bounded_result.artifacts],
                        turn_id=turn_state.turn_id,
                        metadata={
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "permission_effect": str(bounded_result.metadata.get("permission_effect") or ""),
                            "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
                            "tool_result_block_id": result_block.block_id,
                            "tool_result_block_kind": str(result_block.kind),
                        },
                    )
                    event_records.append(tool_result_event(planned.call, bounded_result))
                    self._append_lifecycle(
                        event_records,
                        session,
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        "tool_call_completed",
                        {
                            "turn_index": turn_index,
                            "turn_id": turn_state.turn_id,
                            "batch_index": batch_index,
                            "step_index": planned.step_index,
                            "tool_call_id": planned.call.tool_call_id,
                            "tool_name": planned.call.tool_name,
                            "ok": bounded_result.ok,
                            "error": bounded_result.error,
                            "artifact_ids": [artifact.artifact_id for artifact in bounded_result.artifacts],
                            "permission_effect": str(bounded_result.metadata.get("permission_effect") or ""),
                            "resume_token": session.resume_token,
                        },
                    )

                    tool_call_count += 1
                    result_chars = _result_chars(bounded_result)
                    summary_row = {
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "step_index": planned.step_index,
                        "tool_call_id": planned.call.tool_call_id,
                        "tool_name": planned.call.tool_name,
                        "ok": bounded_result.ok,
                        "summary": bounded_result.summary,
                        "error": bounded_result.error,
                    }
                    batch_summaries.append(summary_row)
                    step_summaries.append(_format_step_summary(summary_row, execution_mode))
                    context_entries.append(
                        RuntimeContextEntry(
                            turn_index=turn_index,
                            batch_index=batch_index,
                            step_index=planned.step_index,
                            tool_name=planned.call.tool_name,
                            tool_call_id=planned.call.tool_call_id,
                            summary=bounded_result.summary,
                            ok=bounded_result.ok,
                            chars=result_chars,
                        )
                    )
                    context_window.record_tool_result(
                        planned=planned,
                        result=bounded_result,
                        result_chars=result_chars,
                        turn_index=turn_index,
                        batch_index=batch_index,
                        metadata={"tool_result_block_id": result_block.block_id},
                    )
                    context_chars = context_window.active_chars

                    if context_window.active_chars > self.config.max_query_context_chars:
                        compaction = context_window.maybe_compact(
                            artifact_store=self.context.artifact_store,
                            run_id=run_id,
                            task_id=task_id,
                            producer_node_id=node_id,
                            reason=ClaudeContextCompactionReason.BUDGET_EXCEEDED,
                        )
                        if compaction.applied and compaction.artifact is not None:
                            artifact = compaction.artifact
                            artifacts.append(artifact)
                            compaction_count += 1
                            session.record_context_compaction(
                                artifact_id=artifact.artifact_id,
                                metadata={
                                    "turn_index": turn_index,
                                    "batch_index": batch_index,
                                    "step_index": planned.step_index,
                                    "context_chars": compaction.before_chars,
                                    "after_chars": compaction.after_chars,
                                    "budget_chars": self.config.max_query_context_chars,
                                    "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
                                    "compacted_block_ids": compaction.compacted_block_ids,
                                },
                            )
                            self._append_lifecycle(
                                event_records,
                                session,
                                run_id,
                                task_id,
                                node_id,
                                worker_request_id,
                                "context_compacted",
                                {
                                    "turn_index": turn_index,
                                    "turn_id": turn_state.turn_id,
                                    "artifact_id": artifact.artifact_id,
                                    "context_chars": compaction.before_chars,
                                    "after_chars": compaction.after_chars,
                                    "budget_chars": self.config.max_query_context_chars,
                                    "resume_token": session.resume_token,
                                },
                            )
                            context_entries = []
                            context_chars = context_window.active_chars
                            state_ledger.record_context_compaction(
                                artifact=artifact,
                                before_chars=compaction.before_chars,
                                after_chars=compaction.after_chars,
                            )
                            interim_tool_result_context = result_context_runtime.build_report(
                                session_id=session.session_id,
                                worker_request_id=worker_request_id,
                                receipts=tool_execution_receipts,
                                context_snapshots=[
                                    *tool_use_context_snapshots,
                                    tool_use_context.to_dict(include_messages=False),
                                ],
                                output_store_snapshot=None,
                                session_bridge_report=self.config.session_bridge_report,
                            )
                            interim_compact_restore_report = compact_restore_runtime.build_report(
                                context_window_snapshot=context_window.snapshot(include_text=False),
                                budget_state=runtime_budget_state,
                                tool_result_context_report=interim_tool_result_context,
                                budget_chain_report=None,
                                constraints=self.config.runtime_constraints,
                                resume_token=session.resume_token,
                            )
                            pending_restore_contract = interim_compact_restore_report.restore_contract
                            event_records.append(
                                compact_restore_runtime.event_for_report(
                                    interim_compact_restore_report,
                                    run_id=run_id,
                                    task_id=task_id,
                                    node_id=node_id,
                                    phase="compact_restore_contract_pending",
                                )
                            )
                            session.record_batch_event(
                                QueryStreamEventType.CONTEXT_RESTORED,
                                turn_id=turn_state.turn_id,
                                metadata={
                                    "turn_index": turn_index,
                                    "phase": "compact_restore_contract_pending",
                                    "compact_restore_report_id": interim_compact_restore_report.report_id,
                                    "next_turn_restore_contract": interim_compact_restore_report.restore_contract.to_dict()
                                    if interim_compact_restore_report.restore_contract is not None
                                    else {},
                                },
                            )
                            self._append_lifecycle(
                                event_records,
                                session,
                                run_id,
                                task_id,
                                node_id,
                                worker_request_id,
                                "compact_restore_contract_pending",
                                {
                                    "turn_index": turn_index,
                                    "turn_id": turn_state.turn_id,
                                    "compact_restore_report_id": interim_compact_restore_report.report_id,
                                    "restore_contract_id": interim_compact_restore_report.restore_contract_id,
                                    "restore_segment_count": str(
                                        interim_compact_restore_report.restore_contract.restore_segment_count
                                        if interim_compact_restore_report.restore_contract is not None
                                        else 0
                                    ),
                                    "resume_token": session.resume_token,
                                },
                            )

                    if not bounded_result.ok and not self.config.continue_on_error and stopped_reason is None:
                        ok = False
                        stopped_reason = bounded_result.error or "tool_step_failed"
                        session.record_error(
                            error=stopped_reason,
                            stop_reason=StopReason.TOOL_ERROR,
                            metadata={
                                "turn_index": turn_index,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                            },
                        )
                        self._append_lifecycle(
                            event_records,
                            session,
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            "error",
                            {
                                "turn_index": turn_index,
                                "turn_id": turn_state.turn_id,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "error": stopped_reason,
                                "stop_reason": str(StopReason.TOOL_ERROR),
                                "resume_token": session.resume_token,
                            },
                        )
                    elif not bounded_result.ok and self.config.continue_on_error:
                        session.record_continue(
                            reason=StopReason.CONTINUE_REQUESTED,
                            error=bounded_result.error or "tool_step_failed",
                            metadata={
                                "turn_index": turn_index,
                                "batch_index": batch_index,
                                "step_index": planned.step_index,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                            },
                        )
                        self._append_lifecycle(
                            event_records,
                            session,
                            run_id,
                            task_id,
                            node_id,
                            worker_request_id,
                            "continue",
                            {
                                "turn_index": turn_index,
                                "turn_id": turn_state.turn_id,
                                "tool_call_id": planned.call.tool_call_id,
                                "tool_name": planned.call.tool_name,
                                "reason": str(StopReason.CONTINUE_REQUESTED),
                                "error": bounded_result.error,
                                "resume_token": session.resume_token,
                            },
                        )

                batch_digest = tool_runtime.record_batch_digest(
                    batch,
                    bounded_results_for_batch,
                    budget_decisions=budget_decisions_for_batch,
                )
                session.record_batch_event(
                    QueryStreamEventType.TOOL_BATCH_COMPLETED,
                    turn_id=turn_state.turn_id,
                    metadata={
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "tool_use_digest": batch_digest.to_dict(),
                    },
                )
                if self.config.emit_tool_use_summaries:
                    tool_use_summary_count += 1
                    session.record_batch_event(
                        QueryStreamEventType.TOOL_USE_SUMMARY,
                        turn_id=turn_state.turn_id,
                        metadata={
                            "turn_index": turn_index,
                            "batch_index": batch_index,
                            "execution_mode": execution_mode,
                            "tool_count": len(batch_requests),
                            "summary": batch_summaries,
                        },
                    )
                    self._append_lifecycle(
                        event_records,
                        session,
                        run_id,
                        task_id,
                        node_id,
                        worker_request_id,
                        "tool_use_summary",
                        {
                            "turn_index": turn_index,
                            "turn_id": turn_state.turn_id,
                            "batch_index": batch_index,
                            "execution_mode": execution_mode,
                            "tool_count": len(batch_requests),
                            "source_path": "packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                            "upstream_source_path": "src/query.ts",
                            "summary": batch_summaries,
                            "resume_token": session.resume_token,
                        },
                    )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "tool_batch_completed",
                    {
                        "turn_index": turn_index,
                        "turn_id": turn_state.turn_id,
                        "batch_index": batch_index,
                        "tool_count": len(batch_requests),
                        "execution_mode": execution_mode,
                        "ok": all(item["ok"] for item in batch_summaries),
                        "stopped_reason": stopped_reason,
                        "resume_token": session.resume_token,
                    },
                )
                session.record_batch_event(
                    QueryStreamEventType.TOOL_BATCH_COMPLETED,
                    turn_id=turn_state.turn_id,
                    metadata={
                        "turn_index": turn_index,
                        "batch_index": batch_index,
                        "tool_count": len(batch_requests),
                        "execution_mode": execution_mode,
                        "ok": all(item["ok"] for item in batch_summaries),
                        "stopped_reason": stopped_reason,
                    },
                )
                if not ok and not self.config.continue_on_error:
                    break

            expected_receipt_ids = {request.call.tool_call_id for request in tool_loop_plan.requests}
            turn_receipts = [
                receipt
                for receipt in tool_execution_receipts
                if str(
                    (
                        receipt.get("request", {}).get("tool_call_id")
                        if isinstance(receipt.get("request"), Mapping)
                        else ""
                    )
                    or ""
                )
                in expected_receipt_ids
            ]
            if settlement_reports and settlement_reports[-1].turn_index == turn_index:
                settled_report = settlement_runtime.settle_receipts(
                    settlement_reports[-1],
                    plan=tool_loop_plan,
                    receipts=turn_receipts,
                )
                settlement_reports[-1] = settled_report
                event_records.append(
                    settlement_runtime.event_for_report(
                        settled_report,
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                    )
                )
            session.end_turn(
                ok=ok or self.config.continue_on_error,
                stop_reason=StopReason.END_TURN if ok or self.config.continue_on_error else StopReason.TOOL_ERROR,
                error=stopped_reason,
                metadata={
                    "turn_index": turn_index,
                    "tool_calls": tool_call_count - turn_started_at_count,
                    "stopped_reason": stopped_reason,
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "turn_completed",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "ok": ok,
                    "tool_calls": tool_call_count - turn_started_at_count,
                    "stopped_reason": stopped_reason,
                    "resume_token": session.resume_token,
                },
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "turn_end",
                {
                    "turn_index": turn_index,
                    "turn_id": turn_state.turn_id,
                    "ok": ok or self.config.continue_on_error,
                    "tool_calls": tool_call_count - turn_started_at_count,
                    "stopped_reason": stopped_reason,
                    "resume_token": session.resume_token,
                },
            )
            state_ledger.record_turn(turn_state.turn_id, turn_index=turn_index, completed=True, ok=ok)
            tool_use_context_snapshots.append(tool_use_context.to_dict(include_messages=False))
            if not ok and not self.config.continue_on_error:
                break

        session.complete_session(
            ok=ok,
            stop_reason=StopReason.SESSION_COMPLETED if ok else StopReason.TOOL_ERROR,
            metadata={
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "tool_budget_externalization_count": tool_budget_externalization_count,
                "tool_failure_signal_count": tool_failure_signal_count,
                "tool_schema_error_count": tool_schema_error_count,
                "conflict_protected_count": conflict_protected_count,
                "stopped_reason": stopped_reason,
                "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
            },
        )
        budget_policy_report = budget_policy_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            receipts=tool_execution_receipts,
            context_snapshots=tool_use_context_snapshots,
        )
        tool_streaming_report = streaming_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            traces=tool_streaming_traces,
        )
        tool_continuation_report = continuation_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            receipts=tool_execution_receipts,
            context_snapshots=tool_use_context_snapshots,
        )
        tool_concurrency_report = concurrency_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            traces=tool_streaming_traces,
        )
        tool_failure_policy_report = failure_policy_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            receipts=tool_execution_receipts,
            failure_signals=[signal.to_dict() for signal in failure_signals],
        )
        tool_output_store_snapshot = output_store_runtime.build_snapshot(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            receipts=tool_execution_receipts,
        )
        tool_output_store_artifact = output_store_runtime.write_snapshot(
            self.context.artifact_store,
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            snapshot=tool_output_store_snapshot,
        )
        artifacts.append(tool_output_store_artifact.artifact)
        tool_result_context_report = result_context_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            receipts=tool_execution_receipts,
            context_snapshots=tool_use_context_snapshots,
            output_store_snapshot=tool_output_store_snapshot,
            session_bridge_report=self.config.session_bridge_report,
        )
        event_records.extend(
            result_context_runtime.message_events(
                tool_result_context_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            result_context_runtime.event_for_report(
                tool_result_context_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        runtime_budget_state.ingest_tool_result_context_report(tool_result_context_report)
        force_final_compact = (
            self.config.runtime_constraints.get("force_compact_restore") is True
            or self.config.runtime_constraints.get("force_context_compact") is True
        )
        if force_final_compact or context_window.active_chars > self.config.max_query_context_chars:
            final_compaction = context_window.maybe_compact(
                artifact_store=self.context.artifact_store,
                run_id=run_id,
                task_id=task_id,
                producer_node_id=node_id,
                reason=ClaudeContextCompactionReason.MANUAL_COMPACT
                if force_final_compact
                else ClaudeContextCompactionReason.BUDGET_EXCEEDED,
                force=force_final_compact,
            )
            if final_compaction.applied and final_compaction.artifact is not None:
                artifacts.append(final_compaction.artifact)
                compaction_count += 1
                session.record_context_compaction(
                    artifact_id=final_compaction.artifact.artifact_id,
                    metadata={
                        "context_chars": final_compaction.before_chars,
                        "after_chars": final_compaction.after_chars,
                        "budget_chars": self.config.max_query_context_chars,
                        "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
                        "compacted_block_ids": final_compaction.compacted_block_ids,
                        "final_compact_restore_pass": True,
                    },
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "context_compacted",
                    {
                        "artifact_id": final_compaction.artifact.artifact_id,
                        "context_chars": final_compaction.before_chars,
                        "after_chars": final_compaction.after_chars,
                        "budget_chars": self.config.max_query_context_chars,
                        "final_compact_restore_pass": "true",
                        "resume_token": session.resume_token,
                    },
                )
                state_ledger.record_context_compaction(
                    artifact=final_compaction.artifact,
                    before_chars=final_compaction.before_chars,
                    after_chars=final_compaction.after_chars,
                )
        compact_restore_report = compact_restore_runtime.build_report(
            context_window_snapshot=context_window.snapshot(include_text=False),
            budget_state=runtime_budget_state,
            tool_result_context_report=tool_result_context_report,
            budget_chain_report=None,
            constraints=self.config.runtime_constraints,
            resume_token=session.resume_token,
        )
        event_records.append(
            compact_restore_runtime.event_for_report(
                compact_restore_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        boundary_event = compact_restore_runtime.boundary_event(
            compact_restore_report,
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
        )
        if boundary_event is not None:
            event_records.append(boundary_event)
            session.record_batch_event(
                QueryStreamEventType.COMPACT_BOUNDARY,
                metadata={
                    "compact_restore_report_id": compact_restore_report.report_id,
                    "compact_boundary": compact_restore_report.boundary.to_dict()
                    if compact_restore_report.boundary is not None
                    else {},
                    "compact_needed": compact_restore_report.compact_needed,
                },
            )
        restore_event = compact_restore_runtime.restore_event(
            compact_restore_report,
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
        )
        if restore_event is not None:
            event_records.append(restore_event)
            session.record_batch_event(
                QueryStreamEventType.CONTEXT_RESTORED,
                metadata={
                    "compact_restore_report_id": compact_restore_report.report_id,
                    "next_turn_restore_contract": compact_restore_report.restore_contract.to_dict()
                    if compact_restore_report.restore_contract is not None
                    else {},
                },
            )
        restore_integration_report = restore_integration_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            applications=restore_applications,
            pending_contracts=[
                pending_restore_contract,
                compact_restore_report.restore_contract
                if compact_restore_report.restore_contract is not None
                and not any(
                    application.contract_id == compact_restore_report.restore_contract.contract_id
                    for application in restore_applications
                )
                else None,
            ],
        )
        event_records.append(
            restore_integration_runtime.event_for_report(
                restore_integration_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.CONTEXT_RESTORED,
            metadata={
                "phase": "codeworker_restore_integration",
                "restore_integration": restore_integration_report.to_dict(),
            },
        )
        latest_context_security_snapshot = (
            restore_applications[-1].security_snapshot if restore_applications else None
        )
        compact_restore_policy_report = compact_restore_policy_runtime.build_report(
            compact_restore=compact_restore_report,
            budget_snapshot=runtime_budget_state.snapshot(),
        )
        event_records.append(
            compact_restore_policy_runtime.event_for_report(
                compact_restore_policy_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.COMPACT_RESTORE_POLICY,
            metadata=compact_restore_policy_report.to_dict(),
        )
        latest_model_stream_report = model_stream_reports[-1] if model_stream_reports else model_stream_runtime.stream(
            envelope=model_stream_runtime.build_envelope(
                session_id=session.session_id,
                worker_request_id=worker_request_id,
                turn_id="no-turn",
                turn_index=0,
                model=self.config.model_name,
                messages=[{"role": "user", "content": ""}],
                context_chars=context_window.active_chars,
                context_limit_chars=self.config.max_query_context_chars,
                tool_call_count=0,
            ),
            budget_state=runtime_budget_state,
            constraints=self.config.runtime_constraints,
        )
        latest_api_retry_report = api_retry_reports[-1] if api_retry_reports else api_retry_runtime.build_report(
            stream_report=latest_model_stream_report,
            budget_state=runtime_budget_state,
            constraints=self.config.runtime_constraints,
        )
        codeworker_api_foundation_report = codeworker_api_foundation_runtime.build_report(
            budget_state=runtime_budget_state,
            compact_restore=compact_restore_report,
            model_stream=latest_model_stream_report,
            api_retry=latest_api_retry_report,
            tool_result_context_report=tool_result_context_report,
            session_snapshot=None,
        )
        event_records.append(
            codeworker_api_foundation_runtime.event_for_report(
                codeworker_api_foundation_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.CODEWORKER_API_FOUNDATION,
            metadata=codeworker_api_foundation_report.to_dict(),
        )
        model_stream_watchdog_report = model_stream_watchdog_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            stream_reports=model_stream_reports,
            retry_reports=api_retry_reports,
        )
        event_records.append(
            model_stream_watchdog_runtime.event_for_report(
                model_stream_watchdog_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.extend(
            model_stream_watchdog_runtime.signal_events(
                model_stream_watchdog_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.MODEL_STREAM_WATCHDOG,
            metadata=model_stream_watchdog_report.to_dict(),
        )
        api_retry_playbook_report = api_retry_playbook_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            stream_reports=model_stream_reports,
            retry_reports=api_retry_reports,
            budget_snapshot=runtime_budget_state.snapshot(),
            provider_report=model_provider_reports[-1] if model_provider_reports else None,
        )
        event_records.append(
            api_retry_playbook_runtime.event_for_report(
                api_retry_playbook_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.API_RETRY_PLAYBOOK,
            metadata=api_retry_playbook_report.to_dict(),
        )
        context_epoch_report = context_epoch_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            event_records=event_records,
            budget_snapshot=runtime_budget_state.snapshot(),
            compact_restore=compact_restore_report,
            model_stream_reports=model_stream_reports,
            api_retry_reports=api_retry_reports,
            provider_report=model_provider_reports[-1] if model_provider_reports else None,
            foundation_report=codeworker_api_foundation_report,
        )
        event_records.append(
            context_epoch_runtime.event_for_report(
                context_epoch_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.CONTEXT_EPOCH_REPORT,
            metadata=context_epoch_report.to_dict(),
        )
        runtime_budget_replay_report = runtime_budget_replay_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            budget_snapshot=runtime_budget_state.snapshot(),
            compact_restore=compact_restore_report,
            compact_policy=compact_restore_policy_report,
            api_retry_playbook=api_retry_playbook_report,
            event_records=event_records,
            source_decisions=(
                *default_runtime_budget_replay_source_decisions(),
                *default_compact_restore_policy_source_decisions(),
                *default_api_retry_playbook_source_decisions(),
            ),
        )
        event_records.append(
            runtime_budget_replay_runtime.event_for_report(
                runtime_budget_replay_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.RUNTIME_BUDGET_REPLAY,
            metadata=runtime_budget_replay_report.to_dict(),
        )
        codeworker_api_audit_report = codeworker_api_audit_runtime.build_report(
            foundation_report=codeworker_api_foundation_report,
            event_records=event_records,
            metadata={
                **runtime_budget_metadata(runtime_budget_state),
                **runtime_budget_replay_metadata(runtime_budget_replay_report),
                **compact_restore_metadata(compact_restore_report),
                **restore_integration_metadata(restore_integration_report),
                **context_security_metadata(latest_context_security_snapshot),
                **compact_restore_policy_metadata(compact_restore_policy_report),
                **context_epoch_metadata(context_epoch_report),
                **model_provider_metadata(model_provider_reports[-1] if model_provider_reports else None),
                **model_stream_metadata(model_stream_reports[-1] if model_stream_reports else None),
                **model_stream_watchdog_metadata(model_stream_watchdog_report),
                **api_retry_metadata(api_retry_reports[-1] if api_retry_reports else None),
                **api_retry_playbook_metadata(api_retry_playbook_report),
                **codeworker_api_foundation_metadata(codeworker_api_foundation_report),
            },
        )
        event_records.append(
            codeworker_api_audit_runtime.event_for_report(
                codeworker_api_audit_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        compact_state_projection_report = compact_state_projection_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            metadata={
                **runtime_budget_metadata(runtime_budget_state),
                **runtime_budget_replay_metadata(runtime_budget_replay_report),
                **compact_restore_metadata(compact_restore_report),
                **restore_integration_metadata(restore_integration_report),
                **context_security_metadata(latest_context_security_snapshot),
                **compact_restore_policy_metadata(compact_restore_policy_report),
                **context_epoch_metadata(context_epoch_report),
                **model_provider_metadata(model_provider_reports[-1] if model_provider_reports else None),
                **model_stream_metadata(model_stream_reports[-1] if model_stream_reports else None),
                **model_stream_watchdog_metadata(model_stream_watchdog_report),
                **api_retry_metadata(api_retry_reports[-1] if api_retry_reports else None),
                **api_retry_playbook_metadata(api_retry_playbook_report),
                **codeworker_api_foundation_metadata(codeworker_api_foundation_report),
                **codeworker_api_audit_metadata(codeworker_api_audit_report),
            },
            event_records=event_records,
        )
        event_records.append(
            compact_state_projection_runtime.event_for_report(
                compact_state_projection_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.COMPACT_STATE_PROJECTION,
            metadata=compact_state_projection_report.to_dict(),
        )
        compact_recovery_audit_report = compact_recovery_audit_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            compact_restore_report=compact_restore_report,
            restore_integration_report=restore_integration_report,
            model_stream_reports=model_stream_reports,
            api_retry_reports=api_retry_reports,
            event_records=event_records,
            metadata={
                **runtime_budget_metadata(runtime_budget_state),
                **compact_restore_metadata(compact_restore_report),
                **restore_integration_metadata(restore_integration_report),
                **context_security_metadata(latest_context_security_snapshot),
                **model_stream_metadata(model_stream_reports[-1] if model_stream_reports else None),
                **api_retry_metadata(api_retry_reports[-1] if api_retry_reports else None),
            },
        )
        event_records.append(
            compact_recovery_audit_runtime.event_for_report(
                compact_recovery_audit_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.COMPACT_STATE_PROJECTION,
            metadata={
                "phase": "codeworker_compact_recovery_audit",
                "compact_recovery_audit": compact_recovery_audit_report.to_dict(),
            },
        )
        model_recovery_matrix_report = model_recovery_matrix_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            model_stream_reports=model_stream_reports,
            api_retry_reports=api_retry_reports,
        )
        event_records.append(
            model_recovery_matrix_runtime.event_for_report(
                model_recovery_matrix_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.MODEL_STREAM_WATCHDOG,
            metadata={
                "phase": "codeworker_model_recovery_matrix",
                "model_recovery_matrix": model_recovery_matrix_report.to_dict(),
            },
        )
        restore_causality_report = restore_causality_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            event_records=event_records,
        )
        event_records.append(
            restore_causality_runtime.event_for_report(
                restore_causality_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.COMPACT_STATE_PROJECTION,
            metadata={
                "phase": "codeworker_restore_causality",
                "restore_causality": restore_causality_report.to_dict(),
            },
        )
        context_restore_api_state_report = context_restore_api_state_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            compact_restore_report=compact_restore_report,
            restore_integration_report=restore_integration_report,
            context_security_snapshot=latest_context_security_snapshot,
            model_stream_reports=model_stream_reports,
            event_records=event_records,
        )
        event_records.append(
            context_restore_api_state_runtime.event_for_report(
                context_restore_api_state_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.COMPACT_STATE_PROJECTION,
            metadata={
                "phase": "codeworker_context_restore_api_state",
                "context_restore_api_state": context_restore_api_state_report.to_dict(),
            },
        )
        disable_semantics_report = disable_semantics_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            constraints=self.config.runtime_constraints,
            metadata={
                **runtime_budget_metadata(runtime_budget_state),
                **compact_restore_metadata(compact_restore_report),
                **restore_integration_metadata(restore_integration_report),
                **context_security_metadata(latest_context_security_snapshot),
                **model_stream_metadata(model_stream_reports[-1] if model_stream_reports else None),
                **api_retry_metadata(api_retry_reports[-1] if api_retry_reports else None),
                **codeworker_api_foundation_metadata(codeworker_api_foundation_report),
                **compact_recovery_audit_metadata(compact_recovery_audit_report),
                **model_recovery_matrix_metadata(model_recovery_matrix_report),
                **restore_causality_metadata(restore_causality_report),
                **context_restore_api_state_metadata(context_restore_api_state_report),
            },
            event_records=event_records,
            worker_ok=ok,
        )
        event_records.append(
            disable_semantics_runtime.event_for_report(
                disable_semantics_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        session.record_batch_event(
            QueryStreamEventType.COMPACT_STATE_PROJECTION,
            metadata={
                "phase": "codeworker_disable_semantics",
                "disable_semantics": disable_semantics_report.to_dict(),
            },
        )
        tool_source_coverage_report = source_coverage_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
        )
        tool_cleanroom_report = cleanroom_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            target_paths=[
                "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
                "packages/runtime/zyra_runtime/tool_runtime_streaming.py",
                "packages/runtime/zyra_runtime/tool_runtime_continuation.py",
                "packages/runtime/zyra_runtime/tool_runtime_output_store.py",
            ],
        )
        session_snapshot = session.snapshot_payload(
            include_transcript=True,
            metadata={
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "tool_budget_externalization_count": tool_budget_externalization_count,
                "tool_failure_signal_count": tool_failure_signal_count,
                "tool_schema_error_count": tool_schema_error_count,
                "conflict_protected_count": conflict_protected_count,
                "failure_signals": [signal.to_dict() for signal in failure_signals],
                "stopped_reason": stopped_reason,
                "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
                "tool_registry_materialization": materialization.to_dict(),
                "tool_use_contexts": tool_use_context_snapshots,
                "tool_execution_receipts": tool_execution_receipts,
                "tool_settlement_reports": [report.to_dict() for report in settlement_reports],
                "tool_budget_policy_report": budget_policy_report.to_dict(),
                "tool_streaming_report": tool_streaming_report.to_dict(),
                "tool_continuation_report": tool_continuation_report.to_dict(),
                "tool_concurrency_report": tool_concurrency_report.to_dict(),
                "tool_failure_policy_report": tool_failure_policy_report.to_dict(),
                "tool_output_store": tool_output_store_artifact.to_dict(),
                "tool_result_context_report": tool_result_context_report.to_dict(),
                "runtime_budget_state": runtime_budget_state.snapshot().to_dict(),
                "runtime_budget_replay_report": runtime_budget_replay_report.to_dict(),
                "compact_restore_report": compact_restore_report.to_dict(),
                "restore_integration_report": restore_integration_report.to_dict(),
                "context_security_snapshot": latest_context_security_snapshot.to_dict()
                if latest_context_security_snapshot is not None
                else None,
                "restore_applications": [application.to_dict() for application in restore_applications],
                "compact_restore_policy_report": compact_restore_policy_report.to_dict(),
                "context_epoch_report": context_epoch_report.to_dict(),
                "model_provider_reports": [report.to_dict() for report in model_provider_reports],
                "model_stream_reports": [report.to_dict() for report in model_stream_reports],
                "model_stream_watchdog_report": model_stream_watchdog_report.to_dict(),
                "api_retry_reports": [report.to_dict() for report in api_retry_reports],
                "api_retry_playbook_report": api_retry_playbook_report.to_dict(),
                "codeworker_api_foundation_report": codeworker_api_foundation_report.to_dict(),
                "codeworker_api_audit_report": codeworker_api_audit_report.to_dict(),
                "compact_state_projection_report": compact_state_projection_report.to_dict(),
                "compact_recovery_audit_report": compact_recovery_audit_report.to_dict(),
                "model_recovery_matrix_report": model_recovery_matrix_report.to_dict(),
                "restore_causality_report": restore_causality_report.to_dict(),
                "context_restore_api_state_report": context_restore_api_state_report.to_dict(),
                "disable_semantics_report": disable_semantics_report.to_dict(),
                "tool_source_coverage_report": tool_source_coverage_report.to_dict(),
                "tool_cleanroom_report": tool_cleanroom_report.to_dict(),
                "tool_session_bridge_report": self.config.session_bridge_report.to_dict()
                if self.config.session_bridge_report is not None
                else None,
            },
        )
        artifact_set = session_lifecycle.materialize_session_artifacts(
            session,
            run_id=run_id,
            task_id=task_id,
            producer_node_id=node_id,
            metadata={
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "tool_budget_externalization_count": tool_budget_externalization_count,
                "tool_failure_signal_count": tool_failure_signal_count,
                "tool_schema_error_count": tool_schema_error_count,
                "conflict_protected_count": conflict_protected_count,
                "failure_signals": [signal.to_dict() for signal in failure_signals],
                "stopped_reason": stopped_reason,
                "context_window": context_window.snapshot(include_text=False),
                "tool_runtime": tool_runtime.snapshot(),
                "tool_registry_materialization": materialization.to_dict(),
                "tool_use_contexts": tool_use_context_snapshots,
                "tool_execution_receipts": tool_execution_receipts,
                "tool_settlement_reports": [report.to_dict() for report in settlement_reports],
                "tool_budget_policy_report": budget_policy_report.to_dict(),
                "tool_streaming_report": tool_streaming_report.to_dict(),
                "tool_continuation_report": tool_continuation_report.to_dict(),
                "tool_concurrency_report": tool_concurrency_report.to_dict(),
                "tool_failure_policy_report": tool_failure_policy_report.to_dict(),
                "tool_output_store": tool_output_store_artifact.to_dict(),
                "tool_result_context_report": tool_result_context_report.to_dict(),
                "runtime_budget_state": runtime_budget_state.snapshot().to_dict(),
                "runtime_budget_replay_report": runtime_budget_replay_report.to_dict(),
                "compact_restore_report": compact_restore_report.to_dict(),
                "restore_integration_report": restore_integration_report.to_dict(),
                "context_security_snapshot": latest_context_security_snapshot.to_dict()
                if latest_context_security_snapshot is not None
                else None,
                "restore_applications": [application.to_dict() for application in restore_applications],
                "compact_restore_policy_report": compact_restore_policy_report.to_dict(),
                "context_epoch_report": context_epoch_report.to_dict(),
                "model_provider_reports": [report.to_dict() for report in model_provider_reports],
                "model_stream_reports": [report.to_dict() for report in model_stream_reports],
                "model_stream_watchdog_report": model_stream_watchdog_report.to_dict(),
                "api_retry_reports": [report.to_dict() for report in api_retry_reports],
                "api_retry_playbook_report": api_retry_playbook_report.to_dict(),
                "codeworker_api_foundation_report": codeworker_api_foundation_report.to_dict(),
                "codeworker_api_audit_report": codeworker_api_audit_report.to_dict(),
                "compact_state_projection_report": compact_state_projection_report.to_dict(),
                "compact_recovery_audit_report": compact_recovery_audit_report.to_dict(),
                "model_recovery_matrix_report": model_recovery_matrix_report.to_dict(),
                "restore_causality_report": restore_causality_report.to_dict(),
                "context_restore_api_state_report": context_restore_api_state_report.to_dict(),
                "disable_semantics_report": disable_semantics_report.to_dict(),
                "tool_source_coverage_report": tool_source_coverage_report.to_dict(),
                "tool_cleanroom_report": tool_cleanroom_report.to_dict(),
                "tool_session_bridge_report": self.config.session_bridge_report.to_dict()
                if self.config.session_bridge_report is not None
                else None,
            },
        )
        snapshot_artifact = artifact_set.snapshot_artifact
        transcript_artifact = artifact_set.transcript_artifact
        artifacts.extend(artifact_set.artifacts)
        state_ledger.record_session_artifacts(
            snapshot_artifact=snapshot_artifact,
            transcript_artifact=transcript_artifact,
            resume_artifact=artifact_set.resume_plan_artifact,
        )
        self._append_lifecycle(
            event_records,
            session,
            run_id,
            task_id,
            node_id,
            worker_request_id,
            "query_session_snapshot",
            {
                "snapshot_artifact_id": snapshot_artifact.artifact_id,
                "transcript_artifact_id": transcript_artifact.artifact_id,
                "resume_plan_artifact_id": artifact_set.resume_plan_artifact.artifact_id if artifact_set.resume_plan_artifact else "",
                "resume_token": session.resume_token,
                "consistency": session_snapshot.get("consistency", {}),
                "stats": session_snapshot.get("stats", {}),
            },
        )
        self._append_lifecycle(
            event_records,
            session,
            run_id,
            task_id,
            node_id,
            worker_request_id,
            "session_completed",
            {
                "ok": ok,
                "tool_call_count": tool_call_count,
                "turn_count": executed_turn_count,
                "context_compaction_count": compaction_count,
                "tool_budget_externalization_count": tool_budget_externalization_count,
                "tool_failure_signal_count": tool_failure_signal_count,
                "stopped_reason": stopped_reason,
                "resume_token": session.resume_token,
            },
        )
        tool_foundation_audit = ToolFoundationAuditRuntime().build_report(
            materialization=materialization.to_dict(),
            context_snapshots=tool_use_context_snapshots,
            receipt_snapshots=tool_execution_receipts,
            event_records=event_records,
            expected_tool_calls=tool_call_count,
        )
        event_records.append(
            ToolFoundationAuditRuntime().event_for_report(
                tool_foundation_audit,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                worker_request_id=worker_request_id,
                session_id=session.session_id,
            )
        )
        permission_handoff = ToolPermissionHandoffRuntime(permission_store=self.context.permission_store).build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            receipts=tool_execution_receipts,
            context_snapshots=tool_use_context_snapshots,
        )
        event_records.append(
            ToolPermissionHandoffRuntime(permission_store=self.context.permission_store).event_for_report(
                permission_handoff,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_permission_session_report = permission_session_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            handoff_report=permission_handoff,
            require_store_link=self.context.permission_store is not None,
        )
        event_records.extend(
            permission_session_runtime.record_events(
                tool_permission_session_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            permission_session_runtime.event_for_report(
                tool_permission_session_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_execution_timeline_report = execution_timeline_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            traces=tool_streaming_traces,
            receipts=tool_execution_receipts,
            event_records=event_records,
            session_bridge_report=self.config.session_bridge_report,
        )
        event_records.append(
            execution_timeline_runtime.event_for_report(
                tool_execution_timeline_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_budget_chain_report = budget_chain_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            receipts=tool_execution_receipts,
            context_snapshots=tool_use_context_snapshots,
            output_store_snapshot=tool_output_store_snapshot,
            result_context_report=tool_result_context_report,
        )
        event_records.append(
            budget_chain_runtime.event_for_report(
                tool_budget_chain_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_permission_checkpoint_report = permission_checkpoint_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            permission_store=self.context.permission_store,
            handoff_report=permission_handoff,
            permission_session_report=tool_permission_session_report,
            receipts=tool_execution_receipts,
        )
        event_records.append(
            permission_checkpoint_runtime.event_for_report(
                tool_permission_checkpoint_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_continuation_packet_report = continuation_packet_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            result_context_report=tool_result_context_report,
            budget_chain_report=tool_budget_chain_report,
            permission_checkpoint_report=tool_permission_checkpoint_report,
            timeline_report=tool_execution_timeline_report,
            session_bridge_report=self.config.session_bridge_report,
        )
        event_records.append(
            continuation_packet_runtime.event_for_report(
                tool_continuation_packet_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_replay_state_report = replay_state_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            materialization=materialization.to_dict(),
            settlement_reports=settlement_reports,
            receipts=tool_execution_receipts,
            event_records=event_records,
            timeline_report=tool_execution_timeline_report,
            result_context_report=tool_result_context_report,
        )
        event_records.append(
            replay_state_runtime.event_for_report(
                tool_replay_state_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_semantic_effect_report = semantic_effect_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            workspace_root=self.context.workspace_root,
            receipts=tool_execution_receipts,
            context_snapshots=tool_use_context_snapshots,
            result_context_report=tool_result_context_report,
            permission_session_report=tool_permission_session_report,
            session_bridge_report=self.config.session_bridge_report,
            timeline_report=tool_execution_timeline_report,
        )
        event_records.append(
            semantic_effect_runtime.event_for_report(
                tool_semantic_effect_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_result_replay_index_report = result_replay_index_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            receipts=tool_execution_receipts,
            output_store_snapshot=tool_output_store_snapshot,
            result_context_report=tool_result_context_report,
            budget_chain_report=tool_budget_chain_report,
            continuation_packet_report=tool_continuation_packet_report,
        )
        event_records.append(
            result_replay_index_runtime.event_for_report(
                tool_result_replay_index_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_source_effect_report = source_effect_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            materialization=materialization.to_dict(),
            receipts=tool_execution_receipts,
            session_bridge_report=self.config.session_bridge_report,
            timeline_report=tool_execution_timeline_report,
            budget_chain_report=tool_budget_chain_report,
            permission_checkpoint_report=tool_permission_checkpoint_report,
            result_replay_index_report=tool_result_replay_index_report,
            semantic_effect_report=tool_semantic_effect_report,
        )
        event_records.append(
            source_effect_runtime.event_for_report(
                tool_source_effect_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_readiness_matrix_report = readiness_matrix_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            disabled_components=(),
            reports={
                "registry": materialization.to_dict(),
                "execution": {
                    "ok": bool(tool_execution_receipts) or tool_call_count == 0,
                    "status": "ready" if bool(tool_execution_receipts) or tool_call_count == 0 else "empty",
                    "tool_call_count": len(tool_execution_receipts),
                },
                "result_budget": budget_policy_report,
                "permission_handoff": permission_handoff,
                "session_bridge": self.config.session_bridge_report,
                "result_context": tool_result_context_report,
                "execution_timeline": tool_execution_timeline_report,
                "budget_chain": tool_budget_chain_report,
                "permission_checkpoint": tool_permission_checkpoint_report,
                "continuation_packet": tool_continuation_packet_report,
                "result_replay_index": tool_result_replay_index_report,
                "source_effects": tool_source_effect_report,
                "semantic_effects": tool_semantic_effect_report,
            },
        )
        event_records.append(
            readiness_matrix_runtime.event_for_report(
                tool_readiness_matrix_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_effect_fingerprint_report = effect_fingerprint_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            receipts=tool_execution_receipts,
            reports={
                "result_context": tool_result_context_report,
                "budget_chain": tool_budget_chain_report,
                "permission_checkpoint": tool_permission_checkpoint_report,
                "continuation_packet": tool_continuation_packet_report,
                "replay_index": tool_result_replay_index_report,
                "source_effects": tool_source_effect_report,
                "readiness_matrix": tool_readiness_matrix_report,
                "semantic_effects": tool_semantic_effect_report,
            },
        )
        event_records.append(
            effect_fingerprint_runtime.event_for_report(
                tool_effect_fingerprint_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_integration_report = integration_audit_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            session_bridge_report=self.config.session_bridge_report,
            result_context_report=tool_result_context_report,
            permission_session_report=tool_permission_session_report,
            receipts=tool_execution_receipts,
            streaming_report=tool_streaming_report,
            concurrency_report=tool_concurrency_report,
            event_records=event_records,
            disabled_components=(),
        )
        event_records.append(
            integration_audit_runtime.event_for_report(
                tool_integration_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            budget_policy_runtime.event_for_report(
                budget_policy_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            streaming_runtime.event_for_report(
                tool_streaming_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            continuation_runtime.event_for_report(
                tool_continuation_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            concurrency_runtime.event_for_report(
                tool_concurrency_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            failure_policy_runtime.event_for_report(
                tool_failure_policy_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            output_store_runtime.event_for_artifact(
                tool_output_store_artifact,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            source_coverage_runtime.event_for_report(
                tool_source_coverage_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        event_records.append(
            cleanroom_runtime.event_for_report(
                tool_cleanroom_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_foundation_artifacts = persistence_runtime.persist_final_state(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            materialization=materialization.to_dict(),
            receipts=tool_execution_receipts,
            context_snapshots=tool_use_context_snapshots,
            audit_report=tool_foundation_audit,
        )
        artifacts.extend(tool_foundation_artifacts.artifacts)
        event_records.append(
            persistence_runtime.event_for_artifacts(
                tool_foundation_artifacts,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_contract_gate_report = contract_gate_runtime.build_report(
            session_id=session.session_id,
            worker_request_id=worker_request_id,
            reports={
                "materialization": materialization.to_dict(),
                "audit": tool_foundation_audit,
                "persistence": tool_foundation_artifacts,
                "permission_handoff": permission_handoff,
                "permission_session": tool_permission_session_report,
                "execution_timeline": tool_execution_timeline_report,
                "budget_policy": budget_policy_report,
                "budget_chain": tool_budget_chain_report,
                "permission_checkpoint": tool_permission_checkpoint_report,
                "continuation_packet": tool_continuation_packet_report,
                "replay_state": tool_replay_state_report,
                "semantic_effect": tool_semantic_effect_report,
                "result_replay_index": tool_result_replay_index_report,
                "source_effects": tool_source_effect_report,
                "readiness_matrix": tool_readiness_matrix_report,
                "effect_fingerprint": tool_effect_fingerprint_report,
                "streaming": tool_streaming_report,
                "continuation": tool_continuation_report,
                "concurrency": tool_concurrency_report,
                "failure_policy": tool_failure_policy_report,
                "output_store": tool_output_store_artifact,
                "result_context": tool_result_context_report,
                "source_coverage": tool_source_coverage_report,
                "cleanroom": tool_cleanroom_report,
                "integration": tool_integration_report,
                "settlement": {
                    "ok": all(report.ok for report in settlement_reports),
                    "status": "pass" if all(report.ok for report in settlement_reports) else "blocked",
                    "report_count": len(settlement_reports),
                },
            },
        )
        event_records.append(
            contract_gate_runtime.event_for_report(
                tool_contract_gate_report,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
            )
        )
        tool_runtime_gate_failures = _runtime_gate_failures(
            {
                "tool_foundation_audit": tool_foundation_audit,
                "tool_permission_handoff": permission_handoff,
                "tool_permission_session": tool_permission_session_report,
                "tool_execution_timeline": tool_execution_timeline_report,
                "tool_budget_chain": tool_budget_chain_report,
                "tool_permission_checkpoint": tool_permission_checkpoint_report,
                "tool_continuation_packet": tool_continuation_packet_report,
                "tool_replay_state": tool_replay_state_report,
                "tool_semantic_effects": tool_semantic_effect_report,
                "tool_result_replay_index": tool_result_replay_index_report,
                "tool_source_effects": tool_source_effect_report,
                "tool_readiness_matrix": tool_readiness_matrix_report,
                "tool_effect_fingerprint": tool_effect_fingerprint_report,
                "tool_integration": tool_integration_report,
                "tool_contract_gate": tool_contract_gate_report,
                "codeworker_api_foundation": codeworker_api_foundation_report,
                "context_epoch": context_epoch_report,
                "runtime_budget_replay": runtime_budget_replay_report,
                "compact_restore_policy": compact_restore_policy_report,
                "model_stream_watchdog": model_stream_watchdog_report,
                "api_retry_playbook": api_retry_playbook_report,
                "codeworker_api_audit": codeworker_api_audit_report,
                "compact_state_projection": compact_state_projection_report,
            }
        )
        if tool_runtime_gate_failures:
            ok = False
            stopped_reason = stopped_reason or "tool_runtime_gate_failed"
            gate_payload = {
                "error": stopped_reason,
                "blocking_reports": list(tool_runtime_gate_failures),
                "blocking_report_count": len(tool_runtime_gate_failures),
                "resume_token": session.resume_token,
            }
            session.status = "failed"
            session.metadata["tool_runtime_gate_failed"] = True
            session.metadata["tool_runtime_gate_failures"] = list(tool_runtime_gate_failures)
            session.record_error(
                error=stopped_reason,
                stop_reason=StopReason.TOOL_ERROR,
                metadata=gate_payload,
            )
            self._append_lifecycle(
                event_records,
                session,
                run_id,
                task_id,
                node_id,
                worker_request_id,
                "tool_runtime_gate_failed",
                gate_payload,
            )
        metadata = self._metadata(
            session_snapshot=session_snapshot,
            max_turns=max_turns,
            tool_budget_externalization_count=tool_budget_externalization_count,
            tool_failure_signal_count=tool_failure_signal_count,
            tool_schema_error_count=tool_schema_error_count,
            conflict_protected_count=conflict_protected_count,
            compaction_count=compaction_count,
            tool_use_summary_count=tool_use_summary_count,
        )
        metadata.update({str(k): str(v) for k, v in dict(self.config.session_foundation_metadata or {}).items()})
        metadata.update(session_artifact_metadata(artifact_set))
        metadata.update(context_window.metadata())
        metadata.update(tool_runtime.metadata())
        metadata.update(materialization.metadata())
        metadata.update(_tool_use_context_metadata(tool_use_context_snapshots))
        metadata.update(tool_foundation_audit_metadata(tool_foundation_audit))
        metadata.update(tool_session_bridge_metadata(self.config.session_bridge_report))
        metadata.update(tool_permission_handoff_metadata(permission_handoff))
        metadata.update(tool_permission_session_metadata(tool_permission_session_report))
        metadata.update(tool_execution_timeline_metadata(tool_execution_timeline_report))
        metadata.update(tool_budget_policy_metadata(budget_policy_report))
        metadata.update(tool_budget_chain_metadata(tool_budget_chain_report))
        metadata.update(tool_permission_checkpoint_metadata(tool_permission_checkpoint_report))
        metadata.update(tool_continuation_packet_metadata(tool_continuation_packet_report))
        metadata.update(tool_replay_state_metadata(tool_replay_state_report))
        metadata.update(tool_semantic_effect_metadata(tool_semantic_effect_report))
        metadata.update(tool_result_replay_index_metadata(tool_result_replay_index_report))
        metadata.update(tool_source_effects_metadata(tool_source_effect_report))
        metadata.update(tool_readiness_matrix_metadata(tool_readiness_matrix_report))
        metadata.update(tool_effect_fingerprint_metadata(tool_effect_fingerprint_report))
        metadata.update(tool_streaming_metadata(tool_streaming_report))
        metadata.update(tool_continuation_metadata(tool_continuation_report))
        metadata.update(tool_concurrency_metadata(tool_concurrency_report))
        metadata.update(tool_failure_policy_metadata(tool_failure_policy_report))
        metadata.update(tool_output_store_metadata(tool_output_store_artifact))
        metadata.update(tool_result_context_metadata(tool_result_context_report))
        metadata.update(runtime_budget_metadata(runtime_budget_state))
        metadata.update(runtime_budget_replay_metadata(runtime_budget_replay_report))
        metadata.update(compact_restore_metadata(compact_restore_report))
        metadata.update(restore_integration_metadata(restore_integration_report))
        metadata.update(context_security_metadata(latest_context_security_snapshot))
        metadata.update(compact_restore_policy_metadata(compact_restore_policy_report))
        metadata.update(context_epoch_metadata(context_epoch_report))
        metadata.update(model_provider_metadata(model_provider_reports[-1] if model_provider_reports else None))
        metadata.update(model_stream_metadata(model_stream_reports[-1] if model_stream_reports else None))
        metadata.update(model_stream_watchdog_metadata(model_stream_watchdog_report))
        metadata.update(api_retry_metadata(api_retry_reports[-1] if api_retry_reports else None))
        metadata.update(api_retry_playbook_metadata(api_retry_playbook_report))
        metadata.update(codeworker_api_foundation_metadata(codeworker_api_foundation_report))
        metadata.update(codeworker_api_audit_metadata(codeworker_api_audit_report))
        metadata.update(compact_state_projection_metadata(compact_state_projection_report))
        metadata.update(compact_recovery_audit_metadata(compact_recovery_audit_report))
        metadata.update(model_recovery_matrix_metadata(model_recovery_matrix_report))
        metadata.update(restore_causality_metadata(restore_causality_report))
        metadata.update(context_restore_api_state_metadata(context_restore_api_state_report))
        metadata.update(disable_semantics_metadata(disable_semantics_report))
        metadata.update(tool_source_coverage_metadata(tool_source_coverage_report))
        metadata.update(tool_cleanroom_metadata(tool_cleanroom_report))
        metadata.update(tool_integration_metadata(tool_integration_report))
        metadata.update(tool_contract_gate_metadata(tool_contract_gate_report))
        metadata.update(tool_foundation_persistence_metadata(tool_foundation_artifacts))
        metadata.update(tool_settlement_metadata(settlement_reports))
        metadata.update(session_lifecycle.metadata())
        metadata["tool_runtime_gate_ok"] = str(not tool_runtime_gate_failures).lower()
        metadata["tool_runtime_gate_failures"] = ",".join(tool_runtime_gate_failures)
        metadata["tool_runtime_gate_failure_count"] = str(len(tool_runtime_gate_failures))
        control_report = None
        if self.config.control_commands:
            control_runtime = ClaudeControlCommandRuntime(
                ClaudeControlRuntimeState(
                    project_root=Path(self.config.project_root or self.context.workspace_root),
                    workspace_root=self.context.workspace_root,
                    artifact_store=self.context.artifact_store,
                    runtime_source=self.contracts.contract_source,
                    runtime_id=self.contracts.runtime_id,
                    context_window=context_window,
                    tool_runtime=tool_runtime,
                    session_lifecycle=session_lifecycle,
                    permission_store=self.context.permission_store,
                    event_reader=self.context.event_reader,
                    checkpoint_reader=self.context.checkpoint_reader,
                    metadata={
                        "session_id": session.session_id,
                        "worker_request_id": worker_request_id,
                    },
                )
            )
            control_report = control_runtime.run_commands(
                list(self.config.control_commands),
                run_id=run_id,
                task_id=task_id,
                producer_node_id=node_id,
            )
            artifacts.extend(control_report.artifacts)
            for control_result in control_report.results:
                state_ledger.record_control_result(
                    command_id=control_result.command_id,
                    command_name=str(control_result.name),
                    ok=control_result.ok,
                    artifact=control_result.artifact,
                )
                context_window.record_control_result(
                    command_name=str(control_result.name),
                    text=control_result.summary,
                    ok=control_result.ok,
                    metadata={"command_id": control_result.command_id},
                )
                self._append_lifecycle(
                    event_records,
                    session,
                    run_id,
                    task_id,
                    node_id,
                    worker_request_id,
                    "control_command",
                    {
                        "command_id": control_result.command_id,
                        "command_name": str(control_result.name),
                        "status": str(control_result.status),
                        "ok": control_result.ok,
                        "artifact_id": control_result.artifact.artifact_id if control_result.artifact else "",
                    },
                )
        metadata.update(control_metadata(control_report))
        metadata.update(state_ledger.metadata())
        return ClaudeQueryEngineResult(
            ok=ok,
            event_records=event_records,
            artifacts=artifacts,
            step_summaries=step_summaries,
            turn_count=executed_turn_count,
            tool_call_count=tool_call_count,
            context_compaction_count=compaction_count,
            stopped_reason=stopped_reason,
            session_snapshot=session_snapshot,
            metadata=metadata,
        )

    def _missing_plan_result(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        request_messages: Sequence[Any],
    ) -> ClaudeQueryEngineResult:
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": "",
                    "worker_request_id": worker_request_id,
                    "phase": "missing_tool_plan",
                    "runtime_id": self.contracts.runtime_id,
                    "message_count": len(request_messages),
                }
            },
        )
        return ClaudeQueryEngineResult(
            ok=False,
            event_records=[event],
            artifacts=[],
            step_summaries=[],
            turn_count=0,
            tool_call_count=0,
            stopped_reason="missing_tool_plan",
            metadata={
                **self.contracts.metadata(),
                "loop": "zyra_claude_query_engine_runtime",
                "query_contract_source": self.contracts.contract_source,
                "query_turns": "0",
                "tool_steps": "0",
            },
        )

    def _disabled_tool_foundation_component(self) -> str:
        if self.config.disable_tool_registry_runtime:
            return "ToolRegistryRuntime"
        if self.config.disable_tool_execution_runtime:
            return "ToolExecutionRuntime"
        if self.config.disable_tool_result_budget_runtime:
            return "ToolResultBudgetRuntime"
        if self.config.disable_tool_permission_handoff_runtime:
            return "ToolPermissionHandoffRuntime"
        return ""

    def _tool_foundation_disabled_result(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        component: str,
    ) -> ClaudeQueryEngineResult:
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": "",
                    "worker_request_id": worker_request_id,
                    "phase": "tool_loop_foundation_disabled",
                    "runtime_id": self.contracts.runtime_id,
                    "owner_unit": TOOL_LOOP_FOUNDATION_OWNER_UNIT,
                    "component": component,
                }
            },
        )
        return ClaudeQueryEngineResult(
            ok=False,
            event_records=[event],
            artifacts=[],
            step_summaries=[],
            turn_count=0,
            tool_call_count=0,
            stopped_reason="tool_loop_foundation_disabled",
            metadata={
                **self.contracts.metadata(),
                "loop": "zyra_claude_query_engine_runtime",
                "query_contract_source": self.contracts.contract_source,
                "query_turns": "0",
                "tool_steps": "0",
                "tool_foundation_owner_unit": TOOL_LOOP_FOUNDATION_OWNER_UNIT,
                "tool_foundation_disabled_component": component,
                "tool_loop_contract_owner_unit": str(self.contracts.tool_loop_contract.get("ownerUnit") or ""),
            },
        )

    def _execute_batch(
        self,
        execution_runtime: ToolExecutionRuntime,
        batch: Any,
        tool_use_context: ToolUseContext,
    ) -> list[ToolExecutionReceipt]:
        try:
            return execution_runtime.execute_batch(
                batch,
                tool_context=tool_use_context,
                max_workers=max(1, self.config.max_read_only_concurrency),
            )
        except ToolRuntimeDisabledError:
            raise

    def _write_context_compaction_artifact(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session: QuerySession,
        context_entries: Sequence[RuntimeContextEntry],
        context_chars: int,
    ) -> ArtifactRef:
        payload = {
            "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
            "runtime_id": self.contracts.runtime_id,
            "session_id": session.session_id,
            "resume_token": session.resume_token,
            "budget_chars": self.config.max_query_context_chars,
            "context_chars": context_chars,
            "entries": [entry.to_dict() for entry in context_entries],
        }
        return self.context.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            title=self.config.context_artifact_title,
            kind=ArtifactKind.TRACE,
            extension=".json",
            producer_node_id=node_id,
        )

    def _append_lifecycle(
        self,
        event_records: list[EventRecord],
        session: QuerySession,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        phase: str,
        payload: Mapping[str, Any],
    ) -> None:
        event_records.append(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "session_id": session.session_id,
                        "worker_request_id": worker_request_id,
                        "phase": phase,
                        **dict(payload),
                    }
                },
            )
        )

    def _append_tool_signal_events(
        self,
        event_records: list[EventRecord],
        session: QuerySession,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        worker_request_id: str,
        session_id: str,
        turn_id: str,
        turn_index: int,
        batch_index: int,
        step_index: int,
        signal: ToolFailureSignal,
    ) -> None:
        payload = {
            "turn_index": turn_index,
            "turn_id": turn_id,
            "batch_index": batch_index,
            "step_index": step_index,
            "signal": signal.to_dict(),
        }
        session.record_batch_event(QueryStreamEventType.TOOL_FAILURE_SIGNAL, turn_id=turn_id, metadata=payload)
        event_records.append(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "session_id": session_id,
                        "worker_request_id": worker_request_id,
                        "phase": "tool_failure_signal",
                        **payload,
                    }
                },
            )
        )
        watchdog_payload = {
            "turn_index": turn_index,
            "turn_id": turn_id,
            "batch_index": batch_index,
            "step_index": step_index,
            **watchdog_signal_payload(signal),
        }
        session.record_batch_event(QueryStreamEventType.WATCHDOG_SIGNAL, turn_id=turn_id, metadata=watchdog_payload)
        event_records.append(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "session_id": session_id,
                        "worker_request_id": worker_request_id,
                        "phase": "watchdog_signal",
                        **watchdog_payload,
                    }
                },
            )
        )

    def _permission_runtime_payload(self) -> dict[str, Any]:
        return {
            "permission_policy": type(self.context.permission_policy).__name__,
            "permission_store": type(self.context.permission_store).__name__ if self.context.permission_store else "",
            "default_effect": str(getattr(self.context.permission_policy, "default_effect", "")),
            "workspace_root": str(self.context.workspace_root),
            "runtime_source": PRODUCTIZED_CONTRACT_SOURCE,
            "state_owner": "ToolPermissionPolicy",
        }

    def _metadata(
        self,
        *,
        session_snapshot: Mapping[str, Any],
        max_turns: int,
        tool_budget_externalization_count: int,
        tool_failure_signal_count: int,
        tool_schema_error_count: int,
        conflict_protected_count: int,
        compaction_count: int,
        tool_use_summary_count: int,
    ) -> dict[str, str]:
        session_metadata = snapshot_checkpoint_metadata(session_snapshot)
        orchestration = self.contracts.query_contract.get("toolOrchestration")
        if not isinstance(orchestration, dict):
            orchestration = {}
        return {
            **self.contracts.metadata(),
            "loop": "zyra_claude_query_engine_runtime",
            "query_session_id": session_metadata["query_session_id"],
            "query_session_resume_token": session_metadata["query_session_resume_token"],
            **session_metadata,
            "max_turns": str(max_turns),
            "tool_result_budget_chars": str(self.config.max_tool_result_chars),
            "tool_result_externalizations": str(tool_budget_externalization_count),
            "tool_failure_signals": str(tool_failure_signal_count),
            "tool_schema_errors": str(tool_schema_error_count),
            "tool_conflict_protected": str(conflict_protected_count),
            "query_context_budget_chars": str(self.config.max_query_context_chars),
            "context_compactions": str(compaction_count),
            "query_engine_contract_source": self.contracts.contract_source,
            "query_contract_source": self.contracts.contract_source,
            "session_contract_source": self.contracts.contract_source,
            "tool_loop_contract_source": self.contracts.contract_source,
            "tool_loop_contract_owner_unit": str(self.contracts.tool_loop_contract.get("ownerUnit") or ""),
            "tool_loop_contract_inventory_exists": str(self.contracts.tool_loop_contract.get("inventoryExists") is True).lower(),
            "tool_orchestration_read_only_concurrent": str(orchestration.get("readOnlyConcurrent") is True).lower(),
            "tool_orchestration_write_serial": str(orchestration.get("writeSerial") is True).lower(),
            "max_read_only_concurrency": str(self.config.max_read_only_concurrency),
            "tool_use_summaries": str(tool_use_summary_count),
            "sidecar_contracts_used": "false",
        }


def query_turns_from_constraints(constraints: Mapping[str, Any]) -> list[list[dict[str, Any]]]:
    return planned_query_turns_from_constraints(constraints)


def _normalize_turns(turns: Sequence[Sequence[Mapping[str, Any]]]) -> list[list[dict[str, Any]]]:
    normalized: list[list[dict[str, Any]]] = []
    for turn in turns:
        steps = [dict(step) for step in turn if isinstance(step, Mapping)]
        if steps:
            normalized.append(steps)
    return normalized


def _turn_user_content(turn_index: int, turn: Sequence[Mapping[str, Any]]) -> str:
    explicit_prompts = [
        str(step.get("prompt") or step.get("user_message") or "")
        for step in turn
        if isinstance(step, Mapping) and (step.get("prompt") or step.get("user_message"))
    ]
    if explicit_prompts:
        return "\n".join(explicit_prompts)
    tool_names = [str(step.get("tool_name") or step.get("tool") or "unknown_tool") for step in turn if isinstance(step, Mapping)]
    return f"Structured CodeWorker query turn {turn_index}: execute {', '.join(tool_names) or 'no tools'}."


def _request_message_chars(messages: Sequence[Any]) -> int:
    total = 0
    for message in messages:
        total += len(json.dumps(to_jsonable(message), ensure_ascii=False, sort_keys=True))
    return total


def _result_chars(result: ToolResult) -> int:
    return len(json.dumps(to_jsonable(result.output), ensure_ascii=False, sort_keys=True))


def _tool_use_context_metadata(snapshots: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    modifier_count = 0
    permission_count = 0
    budget_count = 0
    artifact_count = 0
    result_chars = 0
    for snapshot in snapshots:
        modifier_count += len(snapshot.get("modifier_log") or [])
        permission_count += len(snapshot.get("permission_handoffs") or [])
        budget_count += len(snapshot.get("budget_ledger") or [])
        artifact_count += len(snapshot.get("artifact_refs") or [])
        try:
            result_chars += int(snapshot.get("tool_result_chars") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "tool_foundation_owner_unit": TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        "tool_use_context_turns": str(len(snapshots)),
        "tool_use_context_modifiers": str(modifier_count),
        "tool_use_context_permission_handoffs": str(permission_count),
        "tool_use_context_budget_entries": str(budget_count),
        "tool_use_context_artifact_refs": str(artifact_count),
        "tool_use_context_result_chars": str(result_chars),
    }


def _runtime_gate_failures(reports: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    for name, report in reports.items():
        if report is None:
            failures.append(name)
            continue
        ok_value = getattr(report, "ok", None)
        if ok_value is None and isinstance(report, Mapping):
            ok_value = report.get("ok")
        if ok_value is None and hasattr(report, "to_dict"):
            payload = report.to_dict()
            ok_value = payload.get("ok") if isinstance(payload, Mapping) else None
        if ok_value is False:
            failures.append(name)
            continue
        status = getattr(report, "status", None)
        if status is None and isinstance(report, Mapping):
            status = report.get("status")
        status_text = str(status or "").split(".")[-1].lower()
        if ok_value is None and status_text in {"blocked", "fail", "failed"}:
            failures.append(name)
    return failures


def _format_step_summary(summary: Mapping[str, Any], execution_mode: str) -> str:
    ok = "ok" if summary.get("ok") else f"error={summary.get('error') or 'tool_error'}"
    return (
        f"- turn {summary.get('turn_index')} batch {summary.get('batch_index')} "
        f"step {summary.get('step_index')} `{summary.get('tool_name')}` "
        f"({execution_mode}): {ok}; {summary.get('summary')}"
    )
