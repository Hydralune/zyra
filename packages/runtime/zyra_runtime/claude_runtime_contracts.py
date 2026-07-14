from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping

from zyra_core import now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, tool_foundation_contract_summary


OWNER_UNIT = "M1-02A"
TOOL_LOOP_OWNER_UNIT = TOOL_LOOP_FOUNDATION_OWNER_UNIT
PRIMARY_SOURCE_REPO = "claude-code-best"
PRODUCTIZED_RUNTIME_ID = "zyra-claude-code-productized-runtime"
PRODUCTIZED_CONTRACT_SOURCE = "zyra-claude-productized"


class ClaudeRuntimeSurface(StrEnum):
    QUERY_ENGINE = "query_engine"
    QUERY_LOOP = "query_loop"
    TOOL_REGISTRY = "tool_registry"
    TOOL_EXECUTOR = "tool_executor"
    TOOL_SCHEDULER = "tool_scheduler"
    TOOL_RESULT_BUDGET = "tool_result_budget"
    PERMISSION_RUNTIME = "permission_runtime"
    SESSION_LIFECYCLE = "session_lifecycle"
    SESSION_RESTORE = "session_restore"
    CONTEXT_ASSEMBLY = "context_assembly"
    CONTEXT_COMPACT = "context_compact"
    MODEL_STREAM = "model_stream"
    API_RETRY = "api_retry"
    RUNTIME_BUDGET = "runtime_budget"
    CODEWORKER_API_FOUNDATION = "codeworker_api_foundation"
    EVENT_LOG = "event_log"
    ARTIFACT_STORE = "artifact_store"
    WORKER_ENTRY = "worker_entry"


class ClaudeRuntimeDecision(StrEnum):
    ZYRA_MODULE_MIGRATED = "zyra_module_migrated"
    ADAPTER_ENCAPSULATED = "adapter_encapsulated"
    CONTRACT_ONLY = "contract_only"
    REFERENCE_ONLY = "reference_only"
    DEFERRED = "deferred"
    LEGACY_VENDOR_DEBT = "legacy_vendor_debt"


class ClaudeRuntimeStateOwner(StrEnum):
    QUERY_SESSION = "QuerySession"
    TOOL_LOOP_SCHEDULER = "ToolLoopScheduler"
    TOOL_EXECUTOR = "ToolExecutor"
    TOOL_PERMISSION_POLICY = "ToolPermissionPolicy"
    LOCAL_ARTIFACT_STORE = "LocalArtifactStore"
    CODE_WORKER_RUNTIME = "CodeWorkerRuntime"
    CLAUDE_QUERY_ENGINE = "ZyraClaudeQueryEngine"


@dataclass(frozen=True, slots=True)
class ClaudeSourceToTarget:
    source_repo: str
    source_path: str
    capability: str
    surface: ClaudeRuntimeSurface
    target_paths: tuple[str, ...]
    decision: ClaudeRuntimeDecision
    owner_unit: str = OWNER_UNIT
    primary_entrypoint: str = ""
    test_entrypoints: tuple[str, ...] = ()
    event_phases: tuple[str, ...] = ()
    artifact_kinds: tuple[str, ...] = ()
    state_owner: ClaudeRuntimeStateOwner | str = ""
    required_for_default_path: bool = True
    effective_code: bool = True
    rationale: str = ""
    upstream_signals: tuple[str, ...] = ()

    @property
    def clean_runtime_safe(self) -> bool:
        return all(not _is_source_pool_path(path) for path in self.target_paths)

    @property
    def target_count(self) -> int:
        return len(self.target_paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "capability": self.capability,
            "surface": str(self.surface),
            "target_paths": list(self.target_paths),
            "decision": str(self.decision),
            "owner_unit": self.owner_unit,
            "primary_entrypoint": self.primary_entrypoint,
            "test_entrypoints": list(self.test_entrypoints),
            "event_phases": list(self.event_phases),
            "artifact_kinds": list(self.artifact_kinds),
            "state_owner": str(self.state_owner),
            "required_for_default_path": self.required_for_default_path,
            "effective_code": self.effective_code,
            "clean_runtime_safe": self.clean_runtime_safe,
            "rationale": self.rationale,
            "upstream_signals": list(self.upstream_signals),
        }


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeContractBundle:
    runtime_id: str
    owner_unit: str
    contract_source: str
    source_repo: str
    generated_at: str
    source_to_target: tuple[ClaudeSourceToTarget, ...]
    query_contract: dict[str, Any]
    session_contract: dict[str, Any]
    tool_loop_contract: dict[str, Any]
    inventory: dict[str, Any]
    health: dict[str, Any]
    state_custody: dict[str, str]
    default_path: dict[str, Any]

    @property
    def clean_runtime_safe(self) -> bool:
        return (
            self.health.get("ok") is True
            and all(item.clean_runtime_safe for item in self.source_to_target if item.required_for_default_path)
            and self.default_path.get("requiresRootSourceRepo") is False
        )

    @property
    def primary_runtime_paths(self) -> tuple[str, ...]:
        paths: list[str] = []
        for item in self.source_to_target:
            if item.required_for_default_path:
                paths.extend(item.target_paths)
        return tuple(dict.fromkeys(paths))

    def as_sidecar_compatible(self) -> dict[str, dict[str, Any]]:
        return {
            "health": dict(self.health),
            "inventory": dict(self.inventory),
            "query_contract": dict(self.query_contract),
            "session_contract": dict(self.session_contract),
            "tool_loop_contract": dict(self.tool_loop_contract),
        }

    def metadata(self) -> dict[str, str]:
        active = sum(1 for item in self.source_to_target if item.decision == ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED)
        deferred = sum(1 for item in self.source_to_target if item.decision == ClaudeRuntimeDecision.DEFERRED)
        contract_only = sum(1 for item in self.source_to_target if item.decision == ClaudeRuntimeDecision.CONTRACT_ONLY)
        return {
            "productized_runtime_id": self.runtime_id,
            "productized_runtime_source": self.contract_source,
            "productized_runtime_owner_unit": self.owner_unit,
            "productized_runtime_clean_safe": str(self.clean_runtime_safe).lower(),
            "productized_runtime_active_migrations": str(active),
            "productized_runtime_contract_only": str(contract_only),
            "productized_runtime_deferred": str(deferred),
            "productized_runtime_default_path": str(self.default_path.get("workerRuntime") or ""),
            "productized_runtime_source_repo": self.source_repo,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "owner_unit": self.owner_unit,
            "contract_source": self.contract_source,
            "source_repo": self.source_repo,
            "generated_at": self.generated_at,
            "source_to_target": [item.to_dict() for item in self.source_to_target],
            "query_contract": to_jsonable(self.query_contract),
            "session_contract": to_jsonable(self.session_contract),
            "tool_loop_contract": to_jsonable(self.tool_loop_contract),
            "inventory": to_jsonable(self.inventory),
            "health": to_jsonable(self.health),
            "state_custody": dict(self.state_custody),
            "default_path": to_jsonable(self.default_path),
            "clean_runtime_safe": self.clean_runtime_safe,
            "primary_runtime_paths": list(self.primary_runtime_paths),
        }


@dataclass(frozen=True, slots=True)
class ClaudeCleanRuntimeProbe:
    ok: bool
    checked_at: str
    project_root: str
    source_workspace_root: str
    runtime_id: str
    default_path_exercised: bool
    sidecar_used: bool
    source_repo_required: bool
    source_pool_only_primary_sources: int
    blocked_reasons: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "project_root": self.project_root,
            "source_workspace_root": self.source_workspace_root,
            "runtime_id": self.runtime_id,
            "default_path_exercised": self.default_path_exercised,
            "sidecar_used": self.sidecar_used,
            "source_repo_required": self.source_repo_required,
            "source_pool_only_primary_sources": self.source_pool_only_primary_sources,
            "blocked_reasons": list(self.blocked_reasons),
            "metadata": dict(self.metadata),
        }


def build_productized_claude_runtime_contracts(
    *,
    project_root: str | Path | None = None,
    include_source_availability: bool = False,
    source_workspace_root: str | Path | None = None,
) -> ClaudeRuntimeContractBundle:
    project_path = Path(project_root).resolve() if project_root else None
    source_root = Path(source_workspace_root).resolve() / PRIMARY_SOURCE_REPO if source_workspace_root else None
    source_available = bool(source_root and source_root.exists())
    source_to_target = default_claude_source_to_target()
    query_contract = _query_contract(source_to_target, source_available=source_available if include_source_availability else None)
    session_contract = _session_contract(source_to_target, source_available=source_available if include_source_availability else None)
    tool_loop_contract = _tool_loop_contract(source_to_target, source_available=source_available if include_source_availability else None)
    inventory = _runtime_inventory(source_to_target, project_path=project_path)
    health = _runtime_health(source_to_target, project_path=project_path)
    state_custody = _state_custody(source_to_target)
    default_path = {
        "workerRuntime": "packages/workers/zyra_workers/code_worker_runtime.py",
        "queryInputProcessor": "packages/runtime/zyra_runtime/claude_input_processor.py",
        "contextAssemblyFoundation": "packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
        "codeWorkerSessionStore": "packages/runtime/zyra_runtime/claude_session_store.py",
        "sessionReplayRuntime": "packages/runtime/zyra_runtime/claude_session_replay_runtime.py",
        "turnLifecycleRuntime": "packages/runtime/zyra_runtime/claude_turn_lifecycle_runtime.py",
        "transcriptEventMapper": "packages/runtime/zyra_runtime/claude_transcript_event_mapper.py",
        "sessionAcceptanceRuntime": "packages/runtime/zyra_runtime/claude_session_acceptance_runtime.py",
        "sessionLifecycleRuntime": "packages/runtime/zyra_runtime/claude_session_lifecycle_state.py",
        "sessionLineageRuntime": "packages/runtime/zyra_runtime/claude_session_lineage_runtime.py",
        "sessionApiProjection": "packages/runtime/zyra_runtime/claude_session_api_projection.py",
        "queryEngine": "packages/runtime/claude-runtime/src/query-engine.ts",
        "querySession": "packages/runtime/zyra_runtime/query_session.py",
        "toolLoop": "packages/runtime/zyra_runtime/tool_loop.py",
        "toolRuntimeFoundation": "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
        "toolRuntimeContinuation": "packages/runtime/zyra_runtime/tool_runtime_continuation.py",
        "toolRuntimeCleanroom": "packages/runtime/zyra_runtime/tool_runtime_cleanroom.py",
        "toolRuntimeConcurrency": "packages/runtime/zyra_runtime/tool_runtime_concurrency.py",
        "toolRuntimeContractGate": "packages/runtime/zyra_runtime/tool_runtime_contract_gate.py",
        "toolRuntimeFailurePolicy": "packages/runtime/zyra_runtime/tool_runtime_failure_policy.py",
        "toolRuntimeOutputStore": "packages/runtime/zyra_runtime/tool_runtime_output_store.py",
        "toolRuntimeSourceCoverage": "packages/runtime/zyra_runtime/tool_runtime_source_decisions.py",
        "toolRuntimeFoundationAudit": "packages/runtime/zyra_runtime/tool_runtime_foundation_audit.py",
        "toolRuntimeFoundationPersistence": "packages/runtime/zyra_runtime/tool_runtime_foundation_persistence.py",
        "toolRuntimePermissionHandoff": "packages/runtime/zyra_runtime/tool_runtime_permission_handoff.py",
        "toolRuntimeBudgetPolicy": "packages/runtime/zyra_runtime/tool_runtime_budget_policy.py",
        "toolRuntimeStreaming": "packages/runtime/zyra_runtime/tool_runtime_streaming.py",
        "toolRuntimeSettlement": "packages/runtime/zyra_runtime/tool_runtime_settlement.py",
        "runtimeBudgetState": "packages/runtime/zyra_runtime/runtime_budget_state.py",
        "runtimeBudgetReplayRuntime": "packages/runtime/zyra_runtime/runtime_budget_replay_runtime.py",
        "compactRestoreRuntime": "packages/runtime/zyra_runtime/compact_restore_runtime.py",
        "compactRestorePolicyRuntime": "packages/runtime/zyra_runtime/compact_restore_policy_runtime.py",
        "modelStreamRuntime": "packages/runtime/zyra_runtime/model_api_runtime.py",
        "apiRetryRuntime": "packages/runtime/zyra_runtime/model_api_runtime.py",
        "apiRetryPlaybookRuntime": "packages/runtime/zyra_runtime/api_retry_playbook_runtime.py",
        "modelProviderRuntime": "packages/runtime/zyra_runtime/model_provider_runtime.py",
        "modelStreamWatchdogRuntime": "packages/runtime/zyra_runtime/model_stream_watchdog_runtime.py",
        "contextEpochRuntime": "packages/runtime/zyra_runtime/context_epoch_runtime.py",
        "codeWorkerApiFoundationRuntime": "packages/runtime/zyra_runtime/codeworker_api_foundation.py",
        "codeWorkerApiFoundationAudit": "packages/runtime/zyra_runtime/codeworker_api_foundation_audit.py",
        "compactStateProjection": "packages/runtime/zyra_runtime/codeworker_compact_state_projection.py",
        "toolExecutor": "packages/runtime/zyra_runtime/executor.py",
        "permissionRuntime": "packages/runtime/zyra_runtime/permissions.py",
        "requiresRootSourceRepo": False,
        "requiresNodeSidecar": False,
        "requiresVendorRuntime": False,
        "stateStores": sorted(set(_state_custody(source_to_target).values())),
    }
    return ClaudeRuntimeContractBundle(
        runtime_id=PRODUCTIZED_RUNTIME_ID,
        owner_unit=OWNER_UNIT,
        contract_source=PRODUCTIZED_CONTRACT_SOURCE,
        source_repo=PRIMARY_SOURCE_REPO,
        generated_at=now_iso(),
        source_to_target=source_to_target,
        query_contract=query_contract,
        session_contract=session_contract,
        tool_loop_contract=tool_loop_contract,
        inventory=inventory,
        health=health,
        state_custody=state_custody,
        default_path=default_path,
    )


def default_claude_source_to_target() -> tuple[ClaudeSourceToTarget, ...]:
    return (
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/QueryEngine.ts",
            capability="Conversation-scoped QueryEngine state, turn submission, permission-denial tracking, model/session metadata, and interrupt boundary.",
            surface=ClaudeRuntimeSurface.QUERY_ENGINE,
            target_paths=(
                "packages/runtime/claude-runtime/src/query-engine.ts",
                "packages/runtime/claude-runtime/src/session.ts",
                "packages/runtime/claude-runtime/src/protocol.ts",
                "packages/workers/zyra_workers/typescript_claude_runtime.py",
                "packages/runtime/zyra_runtime/claude_input_processor.py",
                "packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
                "packages/runtime/zyra_runtime/claude_session_store.py",
                "packages/runtime/zyra_runtime/claude_session_replay_runtime.py",
                "packages/runtime/zyra_runtime/claude_turn_lifecycle_runtime.py",
                "packages/runtime/zyra_runtime/claude_transcript_event_mapper.py",
                "packages/runtime/zyra_runtime/claude_session_acceptance_runtime.py",
                "packages/runtime/zyra_runtime/claude_session_lifecycle_state.py",
                "packages/runtime/zyra_runtime/claude_session_lineage_runtime.py",
                "packages/runtime/zyra_runtime/claude_session_api_projection.py",
                "packages/runtime/zyra_runtime/query_session.py",
                "packages/runtime/zyra_runtime/tool_runtime_continuation.py",
                "packages/workers/zyra_workers/code_worker_runtime.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            primary_entrypoint="zyra_workers.CodeWorkerRuntime.run",
            test_entrypoints=(
                "tests.integration.test_code_worker_sidecar.CodeWorkerSidecarTests.test_runtime_executes_structured_tool_plan",
                "tests.integration.test_code_worker_clean_productized_runtime.CodeWorkerCleanProductizedRuntimeTests.test_default_runtime_runs_without_source_workspace",
            ),
            event_phases=("session_started", "turn_started", "message_delta", "session_completed"),
            artifact_kinds=("structured_data", "trace"),
            state_owner=ClaudeRuntimeStateOwner.CLAUDE_QUERY_ENGINE,
            rationale="The upstream QueryEngine class is not embedded as a TS black box; its turn/session/budget responsibilities are represented by Zyra runtime primitives and event records.",
            upstream_signals=("QueryEngineConfig", "submitMessage", "mutableMessages", "permissionDenials", "interrupt"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/processUserInput.ts",
            capability="Text, slash-command, bash and structured turn input classification before QueryEngine dispatch.",
            surface=ClaudeRuntimeSurface.CONTEXT_ASSEMBLY,
            target_paths=(
                "packages/runtime/zyra_runtime/claude_input_processor.py",
                "packages/runtime/zyra_runtime/claude_session_store.py",
                "packages/runtime/zyra_runtime/claude_turn_lifecycle_runtime.py",
                "packages/workers/zyra_workers/code_worker_runtime.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit="M1-02B",
            primary_entrypoint="zyra_runtime.QueryInputProcessor.process_worker_request",
            test_entrypoints=(
                "tests.unit.test_query_session_foundation.QueryInputProcessorTests.test_classifies_text_slash_bash_and_structured_turns",
                "tests.integration.test_code_worker_query_session_foundation.CodeWorkerQuerySessionFoundationTests.test_code_worker_blocks_when_input_processor_is_disabled",
            ),
            event_phases=("query_input_processed", "query_session_seed_created"),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.QUERY_SESSION,
            rationale="Zyra classifies and records accepted input before the tool loop, mirroring Claude Code's pre-submit processUserInput boundary without calling upstream code.",
            upstream_signals=("processUserInput", "slash command routing", "bash input", "pre-submit hooks"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/query.ts",
            capability="Recursive query loop, stream request start, tool execution continuation, max-turn stop, compaction and recovery transition signals.",
            surface=ClaudeRuntimeSurface.QUERY_LOOP,
            target_paths=(
                "packages/runtime/claude-runtime/src/query-engine.ts",
                "packages/runtime/zyra_runtime/tool_runtime_continuation.py",
                "packages/workers/zyra_workers/code_query_loop.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            primary_entrypoint="@zyra/claude-runtime.ClaudeRuntimeCore.run",
            test_entrypoints=(
                "tests.integration.test_code_worker_query_session_lifecycle.CodeWorkerQuerySessionLifecycleTests.test_runtime_emits_turn_message_snapshot_and_transcript_lifecycle",
                "tests.integration.test_code_worker_tool_loop_budget.CodeWorkerToolLoopBudgetTests.test_runtime_converts_schema_error_to_failure_and_watchdog_events",
            ),
            event_phases=("stream_request_start", "tool_loop_plan", "continue", "error"),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.CLAUDE_QUERY_ENGINE,
            rationale="Zyra keeps the loop deterministic for structured worker turns while preserving Claude Code's continuation, max-turn and stop-reason semantics in QuerySession.",
            upstream_signals=("State", "transition", "maxTurns", "toolUseSummary", "context compact"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/Tool.ts",
            capability="Tool identity, input schema, read-only/concurrency/destructive flags, permission check boundary and result mapping.",
            surface=ClaudeRuntimeSurface.TOOL_REGISTRY,
            target_paths=(
                "packages/runtime/zyra_runtime/tools.py",
                "packages/runtime/zyra_runtime/tool_loop.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
                "packages/runtime/zyra_runtime/tool_runtime_continuation.py",
                "packages/runtime/zyra_runtime/tool_runtime_cleanroom.py",
                "packages/runtime/zyra_runtime/tool_runtime_concurrency.py",
                "packages/runtime/zyra_runtime/tool_runtime_contract_gate.py",
                "packages/runtime/zyra_runtime/tool_runtime_failure_policy.py",
                "packages/runtime/zyra_runtime/tool_runtime_output_store.py",
                "packages/runtime/zyra_runtime/tool_runtime_source_decisions.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_audit.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_persistence.py",
                "packages/runtime/zyra_runtime/tool_runtime_permission_handoff.py",
                "packages/runtime/zyra_runtime/tool_runtime_budget_policy.py",
                "packages/runtime/zyra_runtime/tool_runtime_streaming.py",
                "packages/runtime/zyra_runtime/tool_runtime_output_store.py",
                "packages/runtime/zyra_runtime/tool_runtime_source_decisions.py",
                "packages/runtime/zyra_runtime/tool_runtime_settlement.py",
                "packages/runtime/zyra_runtime/executor.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit=TOOL_LOOP_OWNER_UNIT,
            primary_entrypoint="zyra_runtime.default_tool_registry",
            test_entrypoints=(
                "tests.integration.test_code_worker_tool_loop_budget.CodeWorkerToolLoopBudgetTests.test_runtime_batches_read_only_tools_and_serializes_conflicting_writes",
            ),
            event_phases=("tool_call_started", "tool_call_completed"),
            artifact_kinds=("structured_data", "text", "trace"),
            state_owner=ClaudeRuntimeStateOwner.TOOL_EXECUTOR,
            rationale="Tool specs are converted to Zyra ToolSpec/ToolRegistry with schema validation and permission metadata consumed by the scheduler and executor.",
            upstream_signals=("Tool", "inputSchema", "isConcurrencySafe", "isReadOnly", "checkPermissions", "maxResultSizeChars"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/tools.ts",
            capability="Tool pool assembly, enabled-tool filtering and built-in/MCP tool separation.",
            surface=ClaudeRuntimeSurface.TOOL_REGISTRY,
            target_paths=(
                "packages/runtime/zyra_runtime/tools.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
                "packages/runtime/zyra_runtime/tool_runtime_concurrency.py",
                "packages/runtime/zyra_runtime/tool_runtime_source_decisions.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_audit.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_persistence.py",
                "packages/runtime/zyra_runtime/tool_runtime_settlement.py",
                "packages/integrations/zyra_integrations/ledger_migrations.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit=TOOL_LOOP_OWNER_UNIT,
            primary_entrypoint="zyra_runtime.default_tool_registry",
            test_entrypoints=("tests.integration.test_code_worker_sidecar.CodeWorkerSidecarTests.test_runtime_executes_structured_tool_plan",),
            event_phases=("permission_runtime_attached", "tool_loop_plan"),
            artifact_kinds=(),
            state_owner=ClaudeRuntimeStateOwner.TOOL_EXECUTOR,
            rationale="Default tool selection now lives in Zyra's registry and can be filtered by Zyra permission policy without requiring the TS tool pool.",
            upstream_signals=("getAllBaseTools", "getTools", "assembleToolPool", "filterToolsByDenyRules"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/services/tools/toolOrchestration.ts",
            capability="Partition consecutive read-only calls into concurrent batches and serialize mutating calls.",
            surface=ClaudeRuntimeSurface.TOOL_SCHEDULER,
            target_paths=(
                "packages/runtime/zyra_runtime/tool_loop.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
                "packages/runtime/zyra_runtime/tool_runtime_concurrency.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_audit.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_persistence.py",
                "packages/runtime/zyra_runtime/tool_runtime_settlement.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit=TOOL_LOOP_OWNER_UNIT,
            primary_entrypoint="zyra_runtime.ToolLoopScheduler.plan_turn",
            test_entrypoints=(
                "tests.integration.test_code_worker_tool_loop_budget.CodeWorkerToolLoopBudgetTests.test_runtime_batches_read_only_tools_and_serializes_conflicting_writes",
                "tests.integration.test_code_worker_sidecar.CodeWorkerSidecarTests.test_runtime_batches_consecutive_read_only_tools_like_claude_code",
            ),
            event_phases=("tool_loop_plan", "tool_batch_started", "tool_batch_completed"),
            artifact_kinds=(),
            state_owner=ClaudeRuntimeStateOwner.TOOL_LOOP_SCHEDULER,
            rationale="The scheduler is a Zyra-owned partitioner with schema validation and conflict protection; it no longer relies on runTools from the sidecar.",
            upstream_signals=("partitionToolCalls", "runToolsConcurrently", "runToolsSerially", "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/services/tools/toolExecution.ts",
            capability="Tool lookup, validation, permission gate, progress/result events, hook/failure classification and result mapping.",
            surface=ClaudeRuntimeSurface.TOOL_EXECUTOR,
            target_paths=(
                "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_audit.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_persistence.py",
                "packages/runtime/zyra_runtime/tool_runtime_permission_handoff.py",
                "packages/runtime/zyra_runtime/tool_runtime_failure_policy.py",
                "packages/runtime/zyra_runtime/tool_runtime_settlement.py",
                "packages/runtime/zyra_runtime/executor.py",
                "packages/runtime/zyra_runtime/permissions.py",
                "packages/runtime/zyra_runtime/tool_loop.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit=TOOL_LOOP_OWNER_UNIT,
            primary_entrypoint="zyra_runtime.ToolExecutor.execute",
            test_entrypoints=(
                "tests.integration.test_code_worker_tool_loop_budget.CodeWorkerToolLoopBudgetTests.test_runtime_converts_permission_denial_to_watchdog_signal",
            ),
            event_phases=("tool_call_started", "tool_call_completed", "tool_failure_signal", "watchdog_signal"),
            artifact_kinds=("trace", "structured_data", "file", "text"),
            state_owner=ClaudeRuntimeStateOwner.TOOL_EXECUTOR,
            rationale="Execution is owned by ToolExecutor and ToolPermissionPolicy; hook-like failure signals are emitted as watchdog events for recovery units.",
            upstream_signals=("runToolUse", "checkPermissionsAndCallTool", "PermissionResult", "tool_result", "postToolUseHooks"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/services/tools/StreamingToolExecutor.ts",
            capability="Streaming tool execution frames, progress boundaries, batch traceability, result/budget/context modifier progress and completion accounting.",
            surface=ClaudeRuntimeSurface.TOOL_EXECUTOR,
            target_paths=(
                "packages/runtime/zyra_runtime/tool_runtime_streaming.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
                "packages/runtime/zyra_runtime/tool_runtime_concurrency.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit=TOOL_LOOP_OWNER_UNIT,
            primary_entrypoint="zyra_runtime.ToolStreamingRuntime.execute_batch",
            test_entrypoints=(
                "tests.unit.test_tool_loop_budget_runtime.ToolLoopBudgetRuntimeTests.test_streaming_runtime_wraps_real_batch_execution",
                "tests.integration.test_code_worker_tool_loop_budget.CodeWorkerToolLoopFoundationRuntimeTests.test_default_worker_path_emits_tool_foundation_context_events",
            ),
            event_phases=("tool_stream_frame", "tool_streaming_report"),
            artifact_kinds=(),
            state_owner=ClaudeRuntimeStateOwner.TOOL_EXECUTOR,
            rationale="StreamingToolExecutor semantics are productized as Zyra frame traces around real ToolExecutionRuntime batches; frames are emitted on the QueryEngine event stream.",
            upstream_signals=("StreamingToolExecutor", "tool_use_progress", "tool_result_delta", "postToolUseHooks"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/utils/toolResultStorage.ts",
            capability="Per-tool and aggregate tool-result budget, persisted overflow artifacts and stable replacement metadata.",
            surface=ClaudeRuntimeSurface.TOOL_RESULT_BUDGET,
            target_paths=(
                "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_audit.py",
                "packages/runtime/zyra_runtime/tool_runtime_foundation_persistence.py",
                "packages/runtime/zyra_runtime/tool_runtime_budget_policy.py",
                "packages/runtime/zyra_runtime/tool_runtime_failure_policy.py",
                "packages/runtime/zyra_runtime/tool_runtime_output_store.py",
                "packages/runtime/zyra_runtime/tool_loop.py",
                "packages/runtime/zyra_runtime/artifacts.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit=TOOL_LOOP_OWNER_UNIT,
            primary_entrypoint="zyra_runtime.ToolResultBudgeter.apply",
            test_entrypoints=(
                "tests.integration.test_code_worker_tool_loop_budget.CodeWorkerToolLoopBudgetTests.test_runtime_externalizes_large_tool_result_and_emits_budget_watchdog_signal",
                "tests.integration.test_code_worker_sidecar.CodeWorkerSidecarTests.test_runtime_applies_tool_result_budget",
            ),
            event_phases=("tool_result_budget_exceeded", "tool_failure_signal", "watchdog_signal"),
            artifact_kinds=("structured_data",),
            state_owner=ClaudeRuntimeStateOwner.LOCAL_ARTIFACT_STORE,
            rationale="Budget decisions write Zyra artifacts and events; no session-local TS tool-results directory is needed for default execution.",
            upstream_signals=("persistToolResult", "PERSISTED_OUTPUT_TAG", "ContentReplacementState", "enforceToolResultBudget"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/utils/sessionStorage.ts",
            capability="Append-only transcript, parent UUID chain, lite metadata, resume index and session artifact ownership.",
            surface=ClaudeRuntimeSurface.SESSION_LIFECYCLE,
            target_paths=(
                "packages/runtime/zyra_runtime/query_session.py",
                "packages/runtime/zyra_runtime/claude_session_store.py",
                "packages/runtime/zyra_runtime/claude_session_replay_runtime.py",
                "packages/runtime/zyra_runtime/claude_transcript_event_mapper.py",
                "packages/runtime/zyra_runtime/claude_session_lifecycle_state.py",
                "packages/runtime/zyra_runtime/claude_session_lineage_runtime.py",
                "packages/runtime/zyra_runtime/claude_session_api_projection.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            primary_entrypoint="zyra_runtime.QuerySession.to_jsonl",
            test_entrypoints=(
                "tests.integration.test_code_worker_query_session_lifecycle.CodeWorkerQuerySessionLifecycleTests.test_runtime_emits_turn_message_snapshot_and_transcript_lifecycle",
            ),
            event_phases=("query_session_snapshot", "session_completed"),
            artifact_kinds=("trace", "structured_data"),
            state_owner=ClaudeRuntimeStateOwner.QUERY_SESSION,
            rationale="QuerySession owns transcript, parent chain, resume token and snapshots as Zyra artifacts instead of Claude's JSONL project store.",
            upstream_signals=("recordTranscript", "parentUuid", "loadTranscriptFile", "readLiteMetadata", "sessionId"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/utils/sessionRestore.ts",
            capability="Session restore, state hydration, worktree restore and agent/session metadata recovery.",
            surface=ClaudeRuntimeSurface.SESSION_RESTORE,
            target_paths=(
                "packages/runtime/zyra_runtime/query_session.py",
                "packages/runtime/zyra_runtime/claude_session_store.py",
                "packages/runtime/zyra_runtime/claude_session_replay_runtime.py",
                "packages/runtime/zyra_runtime/claude_session_acceptance_runtime.py",
                "packages/runtime/zyra_runtime/session.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            primary_entrypoint="zyra_runtime.QuerySession.restore",
            test_entrypoints=(
                "tests.integration.test_code_worker_clean_productized_runtime.CodeWorkerCleanProductizedRuntimeTests.test_session_snapshot_restores_without_sidecar",
            ),
            event_phases=("query_session_snapshot", "session_started"),
            artifact_kinds=("structured_data", "trace"),
            state_owner=ClaudeRuntimeStateOwner.QUERY_SESSION,
            rationale="Restore is based on QuerySessionSnapshot and transcript replay; the default path does not need Claude project JSONL files.",
            upstream_signals=("restoreSessionStateFromLog", "processResumedConversation", "restoreAgentFromSession", "restoreWorktreeForResume"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/utils/queryContext.ts",
            capability="System/user context assembly, tool list context and cache-safe query prefix boundary.",
            surface=ClaudeRuntimeSurface.CONTEXT_ASSEMBLY,
            target_paths=(
                "packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
                "packages/runtime/zyra_runtime/claude_input_processor.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
                "packages/runtime/zyra_runtime/session.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            primary_entrypoint="zyra_runtime.ZyraClaudeQueryEngine.build_context_window",
            test_entrypoints=(
                "tests.integration.test_code_worker_clean_productized_runtime.CodeWorkerCleanProductizedRuntimeTests.test_context_budget_changes_runtime_artifacts",
            ),
            event_phases=("message_delta", "context_compacted"),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.CLAUDE_QUERY_ENGINE,
            rationale="The structured worker path builds context from WorkerRequest, QuerySession, tool calls and result summaries inside Zyra, not via Claude system prompt helpers.",
            upstream_signals=("fetchSystemPromptParts", "buildSideQuestionFallbackParams", "ToolUseContext"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/services/compact/compact.ts",
            capability="Compact boundary, post-compact attachments, compact summary state, preserved segments and next-turn restore hints.",
            surface=ClaudeRuntimeSurface.CONTEXT_COMPACT,
            target_paths=(
                "packages/runtime/zyra_runtime/compact_restore_runtime.py",
                "packages/runtime/zyra_runtime/compact_restore_policy_runtime.py",
                "packages/runtime/zyra_runtime/runtime_budget_state.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
                "packages/runtime/zyra_runtime/query_session.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit="M1-02D",
            primary_entrypoint="zyra_runtime.CompactRestoreRuntime.build_report",
            test_entrypoints=(
                "tests.integration.test_code_worker_sidecar.CodeWorkerSidecarTests.test_runtime_compacts_query_context_budget",
                "tests.integration.test_code_worker_clean_productized_runtime.CodeWorkerCleanProductizedRuntimeTests.test_context_budget_changes_runtime_artifacts",
                "tests.integration.test_codeworker_context_compact_api_foundation.CodeWorkerContextCompactApiFoundationTests.test_default_path_emits_compact_restore_model_stream_retry_and_budget_state",
            ),
            event_phases=(
                "context_compacted",
                "compact_boundary_created",
                "compact_restore_policy",
                "next_turn_restore_contract",
                "query_session_snapshot",
            ),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.CLAUDE_QUERY_ENGINE,
            rationale="M1-02D keeps the existing deterministic compaction artifact but moves restore semantics into CompactRestoreRuntime and RuntimeBudgetState so next-turn context is owned by Zyra, not a summary-only side effect.",
            upstream_signals=("compactConversation", "buildPostCompactMessages", "createPostCompactFileAttachments", "compact_boundary", "postCompactCleanup"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/services/compact/sessionMemoryCompact.ts",
            capability="Session-memory compact that preserves message-pair invariants and restored segment metadata.",
            surface=ClaudeRuntimeSurface.CONTEXT_COMPACT,
            target_paths=(
                "packages/runtime/zyra_runtime/compact_restore_runtime.py",
                "packages/runtime/zyra_runtime/compact_restore_policy_runtime.py",
                "packages/runtime/zyra_runtime/query_session.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit="M1-02D",
            primary_entrypoint="zyra_runtime.NextTurnRestoreContract.messages",
            test_entrypoints=(
                "tests.integration.test_codeworker_context_compact_api_foundation.CodeWorkerContextCompactApiFoundationTests.test_default_path_emits_compact_restore_model_stream_retry_and_budget_state",
            ),
            event_phases=("context_restored", "compact_restore_policy", "next_turn_restore_contract"),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.QUERY_SESSION,
            rationale="Zyra represents restored tool/file/skill/MCP segments as typed RestoreSegment records and system/tool messages for the next turn.",
            upstream_signals=("sessionMemoryCompact", "message id pairing", "restored messages", "tool_result matching"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/services/api/claude.ts",
            capability="Claude API request envelope, stream frame state, usage patching and semantic API error classification.",
            surface=ClaudeRuntimeSurface.MODEL_STREAM,
            target_paths=(
                "packages/runtime/zyra_runtime/model_api_runtime.py",
                "packages/runtime/zyra_runtime/runtime_budget_state.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit="M1-02D",
            primary_entrypoint="zyra_runtime.ModelStreamRuntime.stream",
            test_entrypoints=(
                "tests.integration.test_codeworker_context_compact_api_foundation.CodeWorkerContextCompactApiFoundationTests.test_default_path_emits_compact_restore_model_stream_retry_and_budget_state",
                "tests.integration.test_codeworker_context_compact_api_foundation.CodeWorkerContextCompactApiFoundationTests.test_core_runtime_disconnects_block_codeworker_api_foundation",
            ),
            event_phases=("model_stream_frame", "model_stream_report", "runtime_budget_updated"),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.CLAUDE_QUERY_ENGINE,
            rationale="The upstream SDK/SSE path is not called as a black box; ModelStreamRuntime owns deterministic stream frames, usage patches, error kinds and budget mutation records.",
            upstream_signals=("raw SSE parser", "message_delta", "usage", "stop_reason", "prompt_too_long", "stream stall"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/services/api/retry-and-errors",
            capability="Retry/fallback policy for rate limit, unavailable model, stream stall, timeout and prompt-too-long recovery.",
            surface=ClaudeRuntimeSurface.API_RETRY,
            target_paths=(
                "packages/runtime/zyra_runtime/model_api_runtime.py",
                "packages/runtime/zyra_runtime/api_retry_playbook_runtime.py",
                "packages/runtime/zyra_runtime/runtime_budget_state.py",
                "packages/runtime/zyra_runtime/codeworker_api_foundation.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit="M1-02D",
            primary_entrypoint="zyra_runtime.ApiRetryRuntime.build_report",
            test_entrypoints=(
                "tests.integration.test_codeworker_context_compact_api_foundation.CodeWorkerContextCompactApiFoundationTests.test_rate_limit_retry_and_model_fallback_are_real_events",
            ),
            event_phases=("api_retry_report", "api_retry_playbook", "runtime_budget_updated", "codeworker_api_foundation"),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.CLAUDE_QUERY_ENGINE,
            rationale="Retry is a Zyra state transition that mutates RuntimeBudgetState and changes the foundation gate; it is not only a log entry.",
            upstream_signals=("withRetry", "rate_limit", "model_unavailable", "stream_timeout", "fallback model"),
        ),
        ClaudeSourceToTarget(
            source_repo="opencode",
            source_path="packages/opencode/src/session/*",
            capability="Session context epoch, provider usage accounting and budget custody adapted into CodeWorker runtime state.",
            surface=ClaudeRuntimeSurface.RUNTIME_BUDGET,
            target_paths=(
                "packages/runtime/zyra_runtime/runtime_budget_state.py",
                "packages/runtime/zyra_runtime/runtime_budget_replay_runtime.py",
                "packages/runtime/zyra_runtime/codeworker_compact_state_projection.py",
                "packages/runtime/zyra_runtime/codeworker_api_foundation.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
            ),
            decision=ClaudeRuntimeDecision.ADAPTER_ENCAPSULATED,
            owner_unit="M1-02D",
            primary_entrypoint="zyra_runtime.RuntimeBudgetState",
            test_entrypoints=(
                "tests.integration.test_codeworker_context_compact_api_foundation.CodeWorkerContextCompactApiFoundationTests.test_core_runtime_disconnects_block_codeworker_api_foundation",
            ),
            event_phases=(
                "runtime_budget_attached",
                "runtime_budget_updated",
                "runtime_budget_replay",
                "compact_state_projection",
                "codeworker_api_foundation",
            ),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.CLAUDE_QUERY_ENGINE,
            rationale="opencode contributes the session/provider accounting pattern, but Zyra owns the mutable RuntimeBudgetState and immutable snapshots.",
            upstream_signals=("session event", "context epoch", "provider accounting", "usage patch"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/query.ts",
            capability="CodeWorker API foundation gate connecting session/tool state, context usage, compact restore, model stream, retry and next-turn restore.",
            surface=ClaudeRuntimeSurface.CODEWORKER_API_FOUNDATION,
            target_paths=(
                "packages/runtime/zyra_runtime/codeworker_api_foundation.py",
                "packages/runtime/zyra_runtime/codeworker_api_foundation_audit.py",
                "packages/runtime/zyra_runtime/codeworker_compact_state_projection.py",
                "packages/runtime/zyra_runtime/runtime_budget_replay_runtime.py",
                "packages/runtime/zyra_runtime/compact_restore_policy_runtime.py",
                "packages/runtime/zyra_runtime/api_retry_playbook_runtime.py",
                "packages/runtime/claude-runtime/src/query-engine.ts",
                "packages/workers/zyra_workers/code_worker_runtime.py",
                "apps/api/zyra_api/main.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            owner_unit="M1-02D",
            primary_entrypoint="zyra_runtime.CodeWorkerApiFoundationRuntime.build_report",
            test_entrypoints=(
                "tests.integration.test_codeworker_context_compact_api_foundation.CodeWorkerContextCompactApiFoundationTests.test_default_path_emits_compact_restore_model_stream_retry_and_budget_state",
            ),
            event_phases=(
                "codeworker_api_foundation",
                "codeworker_api_foundation_audit",
                "compact_state_projection",
                "runtime_budget_replay",
                "compact_restore_policy",
                "api_retry_playbook",
            ),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.CODE_WORKER_RUNTIME,
            rationale="The default CodeWorker path now has an explicit foundation report and API compact-state projection instead of scattered metadata-only proof.",
            upstream_signals=("QueryEngine loop", "session/tool state", "context usage", "API stream", "compact restore"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/cli/src/utils/permissions/*",
            capability="Allow/deny/ask permission effects and prompt/store integration for tools.",
            surface=ClaudeRuntimeSurface.PERMISSION_RUNTIME,
            target_paths=(
                "packages/runtime/zyra_runtime/permissions.py",
                "packages/runtime/zyra_runtime/executor.py",
                "packages/runtime/zyra_runtime/tool_runtime_permission_handoff.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            primary_entrypoint="zyra_runtime.ToolPermissionPolicy",
            test_entrypoints=(
                "tests.integration.test_code_worker_tool_loop_budget.CodeWorkerToolLoopBudgetTests.test_runtime_converts_permission_denial_to_watchdog_signal",
                "tests.integration.test_code_worker_clean_productized_runtime.CodeWorkerCleanProductizedRuntimeTests.test_permission_semantics_fail_without_workspace_access",
            ),
            event_phases=("permission_runtime_attached", "tool_failure_signal", "watchdog_signal"),
            artifact_kinds=(),
            state_owner=ClaudeRuntimeStateOwner.TOOL_PERMISSION_POLICY,
            rationale="Permission decisions are deterministic Zyra decisions and affect ToolExecutor output; they are not frontend-only approvals.",
            upstream_signals=("allow", "deny", "ask", "PermissionResult", "useCanUseTool", "denialTracking"),
        ),
        ClaudeSourceToTarget(
            source_repo=PRIMARY_SOURCE_REPO,
            source_path="src/commands/worker-entry-and-query-dispatch",
            capability="Code worker entrypoint, request-to-query-engine dispatch, default clean runtime selection, and worker result/event bridge.",
            surface=ClaudeRuntimeSurface.WORKER_ENTRY,
            target_paths=(
                "packages/workers/zyra_workers/code_worker_runtime.py",
                "packages/runtime/zyra_runtime/claude_source_graph_crosswalk.py",
            ),
            decision=ClaudeRuntimeDecision.ZYRA_MODULE_MIGRATED,
            primary_entrypoint="zyra_workers.CodeWorkerRuntime.run",
            test_entrypoints=(
                "tests.integration.test_code_worker_clean_productized_runtime.CodeWorkerCleanProductizedRuntimeTests.test_default_runtime_runs_without_source_workspace_or_sidecar",
                "tests.integration.test_claude_productization_integration.ClaudeProductizationIntegrationTests.test_code_worker_default_path_emits_source_graph_events_and_metadata",
            ),
            event_phases=("source_graph_crosswalk_ready", "runtime_context_ready", "downstream_contracts_ready", "integration_gate_passed"),
            artifact_kinds=("trace",),
            state_owner=ClaudeRuntimeStateOwner.CODE_WORKER_RUNTIME,
            rationale="The worker entry is a Zyra-owned runtime boundary: it selects the sidecar-free contract path by default, validates source graph handoff, runs QueryEngine and persists WorkerResult/EventRecord state.",
            upstream_signals=("QueryEngine", "ToolUseContext", "session lifecycle", "control command"),
        ),
    )


def contract_metadata(bundle: ClaudeRuntimeContractBundle | None = None) -> dict[str, str]:
    active_bundle = bundle or build_productized_claude_runtime_contracts()
    return active_bundle.metadata()


def sidecar_free_contracts(project_root: str | Path | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    bundle = build_productized_claude_runtime_contracts(project_root=project_root)
    return (
        bundle.health,
        bundle.inventory,
        bundle.query_contract,
        bundle.session_contract,
        bundle.tool_loop_contract,
    )


def clean_runtime_probe(
    *,
    project_root: str | Path,
    source_workspace_root: str | Path,
    sidecar_used: bool,
    default_path_exercised: bool,
    source_pool_only_primary_sources: int = 0,
    extra_metadata: Mapping[str, str] | None = None,
) -> ClaudeCleanRuntimeProbe:
    bundle = build_productized_claude_runtime_contracts(
        project_root=project_root,
        include_source_availability=True,
        source_workspace_root=source_workspace_root,
    )
    source_repo_required = bool(bundle.default_path.get("requiresRootSourceRepo"))
    blocked: list[str] = []
    if sidecar_used:
        blocked.append("SIDECAR_USED_ON_DEFAULT_PATH")
    if source_repo_required:
        blocked.append("SOURCE_REPO_REQUIRED_ON_DEFAULT_PATH")
    if source_pool_only_primary_sources:
        blocked.append("SOURCE_POOL_PRIMARY_SOURCES_USED")
    if not default_path_exercised:
        blocked.append("DEFAULT_PATH_NOT_EXERCISED")
    if not bundle.clean_runtime_safe:
        blocked.append("CONTRACT_BUNDLE_NOT_CLEAN_SAFE")
    return ClaudeCleanRuntimeProbe(
        ok=not blocked,
        checked_at=now_iso(),
        project_root=str(Path(project_root).resolve()),
        source_workspace_root=str(Path(source_workspace_root).resolve()),
        runtime_id=bundle.runtime_id,
        default_path_exercised=default_path_exercised,
        sidecar_used=sidecar_used,
        source_repo_required=source_repo_required,
        source_pool_only_primary_sources=source_pool_only_primary_sources,
        blocked_reasons=tuple(blocked),
        metadata={**bundle.metadata(), **dict(extra_metadata or {})},
    )


def assert_clean_runtime_probe(probe: ClaudeCleanRuntimeProbe) -> None:
    if not probe.ok:
        raise AssertionError(f"Claude clean runtime probe failed: {', '.join(probe.blocked_reasons)}")


def _query_contract(source_to_target: Iterable[ClaudeSourceToTarget], *, source_available: bool | None) -> dict[str, Any]:
    source_files = [
        item.source_path
        for item in source_to_target
        if item.surface
        in {
            ClaudeRuntimeSurface.QUERY_ENGINE,
            ClaudeRuntimeSurface.QUERY_LOOP,
            ClaudeRuntimeSurface.CONTEXT_ASSEMBLY,
            ClaudeRuntimeSurface.CONTEXT_COMPACT,
            ClaudeRuntimeSurface.MODEL_STREAM,
            ClaudeRuntimeSurface.API_RETRY,
            ClaudeRuntimeSurface.RUNTIME_BUDGET,
            ClaudeRuntimeSurface.CODEWORKER_API_FOUNDATION,
        }
    ]
    target_paths = _target_paths_for(
        source_to_target,
        ClaudeRuntimeSurface.QUERY_ENGINE,
        ClaudeRuntimeSurface.QUERY_LOOP,
        ClaudeRuntimeSurface.CONTEXT_ASSEMBLY,
        ClaudeRuntimeSurface.CONTEXT_COMPACT,
        ClaudeRuntimeSurface.MODEL_STREAM,
        ClaudeRuntimeSurface.API_RETRY,
        ClaudeRuntimeSurface.RUNTIME_BUDGET,
        ClaudeRuntimeSurface.CODEWORKER_API_FOUNDATION,
    )
    contract = {
        "source": PRODUCTIZED_CONTRACT_SOURCE,
        "upstreamSource": PRIMARY_SOURCE_REPO,
        "ownerUnit": OWNER_UNIT,
        "runtimeId": PRODUCTIZED_RUNTIME_ID,
        "inventoryExists": True,
        "sourceFiles": sorted(set(source_files)),
        "targetFiles": target_paths,
        "canonicalRuntime": {
            "ownerLanguage": "typescript",
            "runtimeId": "zyra-typescript-claude-runtime",
            "queryEngine": "packages/runtime/claude-runtime/src/query-engine.ts",
            "sessionState": "packages/runtime/claude-runtime/src/session.ts",
            "toolRegistryAndScheduler": "packages/runtime/claude-runtime/src/tools.ts",
            "resultBudget": "packages/runtime/claude-runtime/src/budget.ts",
            "protocol": "packages/runtime/claude-runtime/src/protocol.ts",
            "pythonHost": "packages/workers/zyra_workers/typescript_claude_runtime.py",
            "pythonFallback": False,
        },
        "queryEngineConfigFields": [
            "workspace_root",
            "artifact_root",
            "max_turns",
            "max_tool_result_chars",
            "max_query_context_chars",
            "continue_on_error",
            "max_read_only_concurrency",
            "emit_tool_use_summaries",
            "model_name",
            "api_retry_max_attempts",
            "runtime_constraints",
        ],
        "loopStateFields": [
            "session_id",
            "resume_token",
            "turn_index",
            "tool_call_count",
            "context_entries",
            "context_chars",
            "compaction_count",
            "failure_signals",
            "stopped_reason",
        ],
        "lifecycleEvents": [
            "session_started",
            "permission_runtime_attached",
            "turn_started",
            "turn_start",
            "message_delta",
            "stream_request_start",
            "tool_loop_plan",
            "tool_batch_started",
            "tool_call_started",
            "tool_call_completed",
            "tool_result_budget_exceeded",
            "tool_failure_signal",
            "watchdog_signal",
            "tool_use_summary",
            "context_compacted",
            "compact_boundary_created",
            "compact_needed",
            "compact_restore_policy",
            "next_turn_restore_contract",
            "model_stream_frame",
            "model_stream_report",
            "model_stream_watchdog",
            "api_retry_report",
            "api_retry_playbook",
            "runtime_budget_updated",
            "runtime_budget_replay",
            "context_epoch_report",
            "codeworker_api_foundation",
            "codeworker_api_foundation_audit",
            "compact_state_projection",
            "error",
            "continue",
            "turn_completed",
            "turn_end",
            "query_session_snapshot",
            "session_completed",
        ],
        "toolOrchestration": {
            "sourcePath": "packages/runtime/claude-runtime/src/tools.ts",
            "readOnlyConcurrent": True,
            "writeSerial": True,
            "maxConcurrencyDefault": 10,
            "schemaValidation": True,
            "conflictProtection": True,
        },
        "budgets": {
            "toolResultBudget": True,
            "reactiveCompact": True,
            "contextBudget": True,
            "snapshotArtifact": True,
            "runtimeBudgetState": True,
            "runtimeBudgetReplayRuntime": True,
            "compactRestoreRuntime": True,
            "compactRestorePolicyRuntime": True,
            "nextTurnRestoreContract": True,
        },
        "modelApiRuntime": {
            "hasModelStreamRuntime": True,
            "hasApiRetryRuntime": True,
            "hasApiRetryPlaybookRuntime": True,
            "hasModelStreamWatchdogRuntime": True,
            "hasContextEpochRuntime": True,
            "hasUsagePatchState": True,
            "hasSemanticApiErrors": True,
            "hasFallbackModelRoute": True,
            "modelStreamRuntime": "packages/runtime/zyra_runtime/model_api_runtime.py",
            "apiRetryRuntime": "packages/runtime/zyra_runtime/model_api_runtime.py",
            "apiRetryPlaybookRuntime": "packages/runtime/zyra_runtime/api_retry_playbook_runtime.py",
            "modelStreamWatchdogRuntime": "packages/runtime/zyra_runtime/model_stream_watchdog_runtime.py",
            "contextEpochRuntime": "packages/runtime/zyra_runtime/context_epoch_runtime.py",
            "runtimeBudgetState": "packages/runtime/zyra_runtime/runtime_budget_state.py",
            "runtimeBudgetReplayRuntime": "packages/runtime/zyra_runtime/runtime_budget_replay_runtime.py",
        },
        "permissionRuntime": {
            "tracksPermissionDenials": True,
            "storesAskRequests": True,
            "blocksToolExecution": True,
            "eventPhase": "permission_runtime_attached",
        },
        "cleanRuntime": {
            "requiresRootSourceRepo": False,
            "requiresNodeSidecar": False,
            "requiresVendorRuntime": False,
        },
    }
    if source_available is not None:
        contract["sourceAvailableForAudit"] = source_available
    return contract


def _session_contract(source_to_target: Iterable[ClaudeSourceToTarget], *, source_available: bool | None) -> dict[str, Any]:
    source_files = [
        item.source_path
        for item in source_to_target
        if item.surface
        in {
            ClaudeRuntimeSurface.SESSION_LIFECYCLE,
            ClaudeRuntimeSurface.SESSION_RESTORE,
            ClaudeRuntimeSurface.CONTEXT_ASSEMBLY,
            ClaudeRuntimeSurface.CONTEXT_COMPACT,
            ClaudeRuntimeSurface.MODEL_STREAM,
            ClaudeRuntimeSurface.API_RETRY,
            ClaudeRuntimeSurface.RUNTIME_BUDGET,
            ClaudeRuntimeSurface.CODEWORKER_API_FOUNDATION,
        }
    ]
    contract = {
        "source": PRODUCTIZED_CONTRACT_SOURCE,
        "upstreamSource": PRIMARY_SOURCE_REPO,
        "ownerUnit": OWNER_UNIT,
        "runtimeId": PRODUCTIZED_RUNTIME_ID,
        "inventoryExists": True,
        "sourceFiles": sorted(set(source_files)),
        "targetFiles": _target_paths_for(
            source_to_target,
            ClaudeRuntimeSurface.SESSION_LIFECYCLE,
            ClaudeRuntimeSurface.SESSION_RESTORE,
            ClaudeRuntimeSurface.CONTEXT_ASSEMBLY,
            ClaudeRuntimeSurface.CONTEXT_COMPACT,
            ClaudeRuntimeSurface.MODEL_STREAM,
            ClaudeRuntimeSurface.API_RETRY,
            ClaudeRuntimeSurface.RUNTIME_BUDGET,
            ClaudeRuntimeSurface.CODEWORKER_API_FOUNDATION,
        ),
        "preQueryFoundation": {
            "ownerUnit": "M1-02B",
            "hasInputProcessor": True,
            "hasContextAssemblyRuntime": True,
            "hasCodeWorkerSessionStore": True,
            "hasSessionReplayRuntime": True,
            "hasTurnLifecycleRuntime": True,
            "hasTranscriptEventMapper": True,
            "hasSessionAcceptanceRuntime": True,
            "hasSessionLifecycleState": True,
            "hasSessionLineageRuntime": True,
            "hasSessionApiProjection": True,
            "sessionSeedRequiredForDefaultPath": True,
            "disconnectBlocksBeforeQueryEngine": True,
            "inputProcessor": "packages/runtime/zyra_runtime/claude_input_processor.py",
            "contextAssembly": "packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
            "sessionStore": "packages/runtime/zyra_runtime/claude_session_store.py",
            "sessionReplay": "packages/runtime/zyra_runtime/claude_session_replay_runtime.py",
            "turnLifecycle": "packages/runtime/zyra_runtime/claude_turn_lifecycle_runtime.py",
            "transcriptEventMapper": "packages/runtime/zyra_runtime/claude_transcript_event_mapper.py",
            "sessionAcceptance": "packages/runtime/zyra_runtime/claude_session_acceptance_runtime.py",
            "sessionLifecycleState": "packages/runtime/zyra_runtime/claude_session_lifecycle_state.py",
            "sessionLineage": "packages/runtime/zyra_runtime/claude_session_lineage_runtime.py",
            "sessionApiProjection": "packages/runtime/zyra_runtime/claude_session_api_projection.py",
        },
        "transcriptPersistence": {
            "appendOnlyJsonl": True,
            "parentUuidChain": True,
            "liteReadWindowBytes": 65536,
            "artifactBackedSnapshot": True,
            "zyraTranscriptEntryType": "SessionTranscriptEntry",
            "preQueryStore": "CodeWorkerSessionStore",
            "preQueryStoreAppendRequired": True,
            "transcriptEventMapper": "TranscriptEventMapper",
            "lifecycleStateMachine": "SessionLifecycleRuntime",
        },
        "resumeRecovery": {
            "hasChainTraversal": True,
            "hasInterruptionDetection": True,
            "hasOrphanedToolResultRecovery": True,
            "snapshotRestore": True,
            "restoreEntrypoint": "QuerySession.restore",
            "storeReplayRuntime": "CodeWorkerSessionReplayRuntime",
            "turnReplayBinding": "TurnLifecycleRuntime.project",
        },
        "streamRuntime": {
            "rawSseStateMachine": True,
            "zyraEventRecordStateMachine": True,
            "hasApiClient": True,
            "hasFilesApi": False,
            "hasQueryProfiler": True,
            "hasModelStreamRuntime": True,
            "hasApiRetryRuntime": True,
            "hasRuntimeBudgetState": True,
            "hasCompactRestoreRuntime": True,
            "retryMatrix": {
                "unattendedRetry": True,
                "deterministicWorkerRetry": True,
                "rateLimitRetry": True,
                "modelFallback": True,
                "streamStallRetry": True,
                "promptTooLongReduce": True,
            },
            "modelStreamRuntime": "ModelStreamRuntime",
            "apiRetryRuntime": "ApiRetryRuntime",
            "runtimeBudgetState": "RuntimeBudgetState",
            "compactRestoreRuntime": "CompactRestoreRuntime",
        },
        "bridgeSessionRuntime": {
            "hasSessionRunner": True,
            "hasInboundMessages": True,
            "runner": "CodeWorkerRuntime.run",
        },
        "sessionCommands": {
            "hasClearConversation": False,
            "controlCommandCompatible": True,
        },
        "zyraRuntimeMapping": {
            "querySession": "packages/runtime/zyra_runtime/query_session.py",
            "queryEngine": "packages/runtime/claude-runtime/src/query-engine.ts",
            "sessionReplay": "packages/runtime/zyra_runtime/claude_session_replay_runtime.py",
            "turnLifecycle": "packages/runtime/zyra_runtime/claude_turn_lifecycle_runtime.py",
            "transcriptEventMapper": "packages/runtime/zyra_runtime/claude_transcript_event_mapper.py",
            "sessionAcceptance": "packages/runtime/zyra_runtime/claude_session_acceptance_runtime.py",
            "sessionLifecycle": "packages/runtime/zyra_runtime/claude_session_lifecycle_state.py",
            "sessionLineage": "packages/runtime/zyra_runtime/claude_session_lineage_runtime.py",
            "sessionApiProjection": "packages/runtime/zyra_runtime/claude_session_api_projection.py",
        },
        "cleanRuntime": {
            "requiresRootSourceRepo": False,
            "requiresNodeSidecar": False,
            "requiresVendorRuntime": False,
        },
    }
    if source_available is not None:
        contract["sourceAvailableForAudit"] = source_available
    return contract


def _tool_loop_contract(source_to_target: Iterable[ClaudeSourceToTarget], *, source_available: bool | None) -> dict[str, Any]:
    source_files = [
        item.source_path
        for item in source_to_target
        if item.surface
        in {
            ClaudeRuntimeSurface.TOOL_REGISTRY,
            ClaudeRuntimeSurface.TOOL_EXECUTOR,
            ClaudeRuntimeSurface.TOOL_SCHEDULER,
            ClaudeRuntimeSurface.TOOL_RESULT_BUDGET,
            ClaudeRuntimeSurface.PERMISSION_RUNTIME,
        }
    ]
    foundation = tool_foundation_contract_summary()
    contract = {
        "source": PRODUCTIZED_CONTRACT_SOURCE,
        "upstreamSource": PRIMARY_SOURCE_REPO,
        "ownerUnit": TOOL_LOOP_OWNER_UNIT,
        "runtimeId": PRODUCTIZED_RUNTIME_ID,
        "inventoryExists": True,
        "sourceFiles": sorted(set(source_files)),
        "supplementalSources": foundation["sourceRepos"],
        "sourceDecisionLedger": foundation["sourceLedger"],
        "targetFiles": _target_paths_for(
            source_to_target,
            ClaudeRuntimeSurface.TOOL_REGISTRY,
            ClaudeRuntimeSurface.TOOL_EXECUTOR,
            ClaudeRuntimeSurface.TOOL_SCHEDULER,
            ClaudeRuntimeSurface.TOOL_RESULT_BUDGET,
            ClaudeRuntimeSurface.PERMISSION_RUNTIME,
        ),
        "toolInterface": {
            "hasInputSchema": True,
            "hasConcurrencyFlag": True,
            "hasReadOnlyFlag": True,
            "hasDestructiveFlag": True,
            "hasPermissionCheck": True,
        },
        "executionPipeline": {
            "hasPermissionGate": True,
            "hasSchemaValidation": True,
            "hasToolResultBlockMapping": True,
            "hasLargeResultExternalization": True,
            "permissionDenialIsResult": True,
            "hasExecutionReceipt": True,
            "hasToolUseContextModifiers": True,
        },
        "scheduling": {
            "readOnlyConcurrent": True,
            "writeSerial": True,
            "conflictingWritesSerial": True,
            "maxConcurrencyDefault": 10,
        },
        "resultBudget": {
            "hasMaxResultSizeChars": True,
            "hasPersistedOutputTag": True,
            "hasLargeResultExternalization": True,
            "artifactStore": "LocalArtifactStore",
            "hasTurnAggregateBudget": True,
            "opencodeToolOutputStorePort": True,
            "hermesBudgetEnvelopePort": True,
        },
        "shellRuntime": {
            "hasProcessLifecycle": True,
            "hasReadOnlyCommandValidation": True,
            "timeoutSecondsDefault": 30,
        },
        "sandboxRuntime": {
            "hasSandboxAdapter": True,
            "workspaceRootPolicy": "ToolPermissionPolicy.for_workspace",
        },
        "failureSignals": {
            "hasDenialLimits": True,
            "schemaErrorRoute": "repair_tool_arguments",
            "permissionDeniedRoute": "permission_runtime",
            "budgetExceededRoute": "artifact_externalized",
            "runtimeErrorRoute": "recovery_planner",
        },
        "zyraRuntimeMapping": {
            "toolLoop": "packages/runtime/zyra_runtime/tool_loop.py",
            "toolRegistryRuntime": "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
            "toolUseContext": "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
            "toolContinuationRuntime": "packages/runtime/zyra_runtime/tool_runtime_continuation.py",
            "toolCleanroomRuntime": "packages/runtime/zyra_runtime/tool_runtime_cleanroom.py",
            "toolConcurrencyRuntime": "packages/runtime/zyra_runtime/tool_runtime_concurrency.py",
            "toolContractGateRuntime": "packages/runtime/zyra_runtime/tool_runtime_contract_gate.py",
            "toolFailurePolicyRuntime": "packages/runtime/zyra_runtime/tool_runtime_failure_policy.py",
            "toolOutputStoreRuntime": "packages/runtime/zyra_runtime/tool_runtime_output_store.py",
            "toolSourceCoverageRuntime": "packages/runtime/zyra_runtime/tool_runtime_source_decisions.py",
            "toolExecutionRuntime": "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
            "toolResultBudgetRuntime": "packages/runtime/zyra_runtime/tool_runtime_foundation.py",
            "toolBudgetPolicyRuntime": "packages/runtime/zyra_runtime/tool_runtime_budget_policy.py",
            "toolStreamingRuntime": "packages/runtime/zyra_runtime/tool_runtime_streaming.py",
            "toolExecutor": "packages/runtime/zyra_runtime/executor.py",
            "permissionRuntime": "packages/runtime/zyra_runtime/permissions.py",
        },
        "cleanRuntime": {
            "requiresRootSourceRepo": False,
            "requiresNodeSidecar": False,
            "requiresVendorRuntime": False,
        },
    }
    if source_available is not None:
        contract["sourceAvailableForAudit"] = source_available
    return contract


def _runtime_inventory(source_to_target: Iterable[ClaudeSourceToTarget], *, project_path: Path | None) -> dict[str, Any]:
    target_paths = [path for item in source_to_target for path in item.target_paths]
    existing_targets = []
    missing_targets = []
    if project_path is not None:
        for path in sorted(set(target_paths)):
            if (project_path / path).exists():
                existing_targets.append(path)
            else:
                missing_targets.append(path)
    by_surface: dict[str, int] = {}
    by_decision: dict[str, int] = {}
    for item in source_to_target:
        by_surface[str(item.surface)] = by_surface.get(str(item.surface), 0) + 1
        by_decision[str(item.decision)] = by_decision.get(str(item.decision), 0) + 1
    return {
        "source": PRODUCTIZED_CONTRACT_SOURCE,
        "upstreamSource": PRIMARY_SOURCE_REPO,
        "ownerUnit": OWNER_UNIT,
        "runtimeId": PRODUCTIZED_RUNTIME_ID,
        "targetCount": len(set(target_paths)),
        "existingTargetCount": len(existing_targets),
        "missingTargetCount": len(missing_targets),
        "existingTargets": existing_targets,
        "missingTargets": missing_targets,
        "surfaceCounts": by_surface,
        "decisionCounts": by_decision,
        "toolRuntime": {
            "baseToolCount": 8,
            "baseToolSymbols": [
                "file_read",
                "file_write",
                "file_edit",
                "shell",
                "browser",
                "web_search",
                "artifact_write",
                "trace",
            ],
            "registry": "zyra_runtime.default_tool_registry",
            "toolRegistryRuntime": "zyra_runtime.ToolRegistryRuntime",
            "toolUseContext": "zyra_runtime.ToolUseContext",
            "toolContinuationRuntime": "zyra_runtime.ToolContinuationRuntime",
            "toolCleanroomRuntime": "zyra_runtime.ToolCleanroomRuntime",
            "toolConcurrencyRuntime": "zyra_runtime.ToolConcurrencyRuntime",
            "toolContractGateRuntime": "zyra_runtime.ToolContractGateRuntime",
            "toolFailurePolicyRuntime": "zyra_runtime.ToolFailurePolicyRuntime",
            "toolOutputStoreRuntime": "zyra_runtime.ToolOutputStoreRuntime",
            "toolSourceCoverageRuntime": "zyra_runtime.ToolSourceCoverageRuntime",
            "toolExecutionRuntime": "zyra_runtime.ToolExecutionRuntime",
            "toolResultBudgetRuntime": "zyra_runtime.ToolResultBudgetRuntime",
            "toolBudgetPolicyRuntime": "zyra_runtime.ToolBudgetPolicyRuntime",
            "toolStreamingRuntime": "zyra_runtime.ToolStreamingRuntime",
            "toolFoundationAuditRuntime": "zyra_runtime.ToolFoundationAuditRuntime",
            "toolFoundationPersistenceRuntime": "zyra_runtime.ToolFoundationPersistenceRuntime",
            "toolPermissionHandoffRuntime": "zyra_runtime.ToolPermissionHandoffRuntime",
            "toolSettlementRuntime": "zyra_runtime.ToolSettlementRuntime",
            "ownerUnit": TOOL_LOOP_OWNER_UNIT,
        },
        "commandRuntime": {
            "commandCount": 0,
            "highValueCommandPaths": [],
            "controlCommandCompatible": True,
        },
        "moduleEntrypoints": {
            "queryEngine": "@zyra/claude-runtime.ClaudeRuntimeCore",
            "queryEngineHost": "zyra_workers.TypeScriptClaudeQueryEngine",
            "querySession": "zyra_runtime.QuerySession",
            "toolLoop": "zyra_runtime.ToolLoopScheduler",
            "toolRegistryRuntime": "zyra_runtime.ToolRegistryRuntime",
            "toolContinuationRuntime": "zyra_runtime.ToolContinuationRuntime",
            "toolCleanroomRuntime": "zyra_runtime.ToolCleanroomRuntime",
            "toolConcurrencyRuntime": "zyra_runtime.ToolConcurrencyRuntime",
            "toolContractGateRuntime": "zyra_runtime.ToolContractGateRuntime",
            "toolFailurePolicyRuntime": "zyra_runtime.ToolFailurePolicyRuntime",
            "toolOutputStoreRuntime": "zyra_runtime.ToolOutputStoreRuntime",
            "toolSourceCoverageRuntime": "zyra_runtime.ToolSourceCoverageRuntime",
            "toolExecutionRuntime": "zyra_runtime.ToolExecutionRuntime",
            "toolResultBudgetRuntime": "zyra_runtime.ToolResultBudgetRuntime",
            "toolBudgetPolicyRuntime": "zyra_runtime.ToolBudgetPolicyRuntime",
            "toolStreamingRuntime": "zyra_runtime.ToolStreamingRuntime",
            "toolFoundationAuditRuntime": "zyra_runtime.ToolFoundationAuditRuntime",
            "toolFoundationPersistenceRuntime": "zyra_runtime.ToolFoundationPersistenceRuntime",
            "toolPermissionHandoffRuntime": "zyra_runtime.ToolPermissionHandoffRuntime",
            "toolSettlementRuntime": "zyra_runtime.ToolSettlementRuntime",
            "workerRuntime": "zyra_workers.CodeWorkerRuntime",
        },
        "cleanRuntime": {
            "requiresRootSourceRepo": False,
            "requiresNodeSidecar": False,
            "requiresVendorRuntime": False,
        },
    }


def _runtime_health(source_to_target: Iterable[ClaudeSourceToTarget], *, project_path: Path | None) -> dict[str, Any]:
    inventory = _runtime_inventory(source_to_target, project_path=project_path)
    missing_required = []
    if project_path is not None:
        for item in source_to_target:
            if not item.required_for_default_path:
                continue
            for target in item.target_paths:
                if not (project_path / target).exists():
                    missing_required.append(target)
    ok = not missing_required
    return {
        "ok": ok,
        "runtime": PRODUCTIZED_RUNTIME_ID,
        "worker": "CodeWorkerRuntime",
        "source": PRODUCTIZED_CONTRACT_SOURCE,
        "upstreamSource": PRIMARY_SOURCE_REPO,
        "ownerUnit": OWNER_UNIT,
        "missingRequiredTargets": sorted(set(missing_required)),
        "productizedRuntime": {
            "complete": ok,
            "effectiveLineCount": 0,
            "lineCountSource": "git-diff-numstat",
            "referenceCrosswalk": {
                "ok": True,
                "entryCount": len(tuple(source_to_target)),
                "source": "claude_runtime_contracts.default_claude_source_to_target",
            },
        },
        "vendor": {
            "complete": False,
            "requiredForMainPath": False,
            "vendorRoot": "",
        },
        "inventory": {
            "targetCount": inventory["targetCount"],
            "existingTargetCount": inventory["existingTargetCount"],
            "missingTargetCount": inventory["missingTargetCount"],
        },
        "cleanRuntime": {
            "requiresRootSourceRepo": False,
            "requiresNodeSidecar": False,
            "requiresVendorRuntime": False,
        },
    }


def _state_custody(source_to_target: Iterable[ClaudeSourceToTarget]) -> dict[str, str]:
    custody: dict[str, str] = {}
    for item in source_to_target:
        owner = str(item.state_owner)
        if not owner:
            continue
        custody[str(item.surface)] = owner
    custody.update(
        {
            "tool_permission_requests": "JsonPermissionStore",
            "tool_registry_materialization": "ToolRegistryRuntime",
            "tool_use_context": "ToolUseContext",
            "tool_execution_receipts": "ToolExecutionRuntime",
            "tool_context_modifiers": "ToolUseContext",
            "tool_foundation_audit": "ToolFoundationAuditRuntime",
            "tool_foundation_artifacts": "ToolFoundationPersistenceRuntime",
            "tool_execution_receipt_log": "ToolFoundationPersistenceRuntime",
            "tool_externalized_output_index": "ToolFoundationPersistenceRuntime",
            "tool_foundation_replay_report": "ToolFoundationPersistenceRuntime",
            "tool_permission_questions": "ToolPermissionHandoffRuntime",
            "tool_permission_replies": "JsonPermissionStore",
            "tool_budget_policy": "ToolBudgetPolicyRuntime",
            "tool_budget_ledger": "ToolBudgetPolicyRuntime",
            "tool_streaming_trace": "ToolStreamingRuntime",
            "tool_streaming_frame": "ToolStreamingRuntime",
            "tool_continuation_pair": "ToolContinuationRuntime",
            "tool_continuation_report": "ToolContinuationRuntime",
            "tool_concurrency_report": "ToolConcurrencyRuntime",
            "tool_concurrency_observation": "ToolConcurrencyRuntime",
            "tool_failure_policy": "ToolFailurePolicyRuntime",
            "tool_failure_route": "ToolFailurePolicyRuntime",
            "tool_output_store": "ToolOutputStoreRuntime",
            "tool_output_store_artifact": "LocalArtifactStore",
            "tool_source_coverage": "ToolSourceCoverageRuntime",
            "tool_contract_gate": "ToolContractGateRuntime",
            "tool_contract_gate_component": "ToolContractGateRuntime",
            "tool_cleanroom": "ToolCleanroomRuntime",
            "tool_registry_settlement": "ToolSettlementRuntime",
            "tool_receipt_settlement": "ToolSettlementRuntime",
            "tool_result_artifacts": "LocalArtifactStore",
            "query_session_snapshot": "QuerySessionSnapshot",
            "query_session_replay_plan": "SessionReplayPlan",
            "query_turn_lifecycle_projection": "TurnLifecycleProjection",
            "query_transcript_mapping": "TranscriptEventMappingReport",
            "query_session_acceptance": "SessionAcceptanceReport",
            "query_session_lifecycle_state": "SessionLifecycleStateReport",
            "query_session_lineage": "SessionLineageReport",
            "query_session_api_projection": "SessionApiProjection",
            "worker_result": "WorkerResult",
            "event_log": "EventRecord",
        }
    )
    return custody


def _target_paths_for(source_to_target: Iterable[ClaudeSourceToTarget], *surfaces: ClaudeRuntimeSurface) -> list[str]:
    selected: list[str] = []
    surface_set = set(surfaces)
    for item in source_to_target:
        if item.surface in surface_set:
            selected.extend(item.target_paths)
    return sorted(set(selected))


def _is_source_pool_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return (
        normalized.startswith("vendor/")
        or normalized.startswith("vendor-runtimes/")
        or normalized.startswith("source-pool/")
        or normalized.startswith("runtime-sources/")
        or "/productized/" in normalized
        or "/third_party/" in normalized
    )
