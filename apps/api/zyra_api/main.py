from __future__ import annotations

import hashlib
import json
import os
import threading
import sys
from dataclasses import replace
from types import SimpleNamespace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_PATHS = [
    PROJECT_ROOT / "packages" / "core",
    PROJECT_ROOT / "packages" / "orchestration",
    PROJECT_ROOT / "packages" / "memory",
    PROJECT_ROOT / "packages" / "commands",
    PROJECT_ROOT / "packages" / "skills",
    PROJECT_ROOT / "packages" / "runtime",
    PROJECT_ROOT / "packages" / "integrations",
    PROJECT_ROOT / "packages" / "workers",
    PROJECT_ROOT / "packages" / "symbolic",
    PROJECT_ROOT / "packages" / "scheduler",
    PROJECT_ROOT / "packages" / "evaluation",
    PROJECT_ROOT / "packages" / "workspace",
]
for package_path in PACKAGE_PATHS:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import (
    AgentMessage,
    AgentRole,
    ArtifactKind,
    ArtifactRef,
    EventRecord,
    EventType,
    MessageIntent,
    create_task_state,
    new_id,
    now_iso,
    to_jsonable,
)
from zyra_core.event_log import append_event as append_jsonl_event
from zyra_workspace import (
    WorkspaceApiService,
    WorkspaceEditPort,
    WorkspaceError,
    WorkspaceIsolationRuntime,
    WorkspaceKind,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
    workspace_error_response,
)
from zyra_memory import CompactPolicy, MemoryFabric, SQLiteStore
from zyra_orchestration import GraphExecutionContext, cancel_task_graph, ensure_default_graph, run_task_graph
from zyra_symbolic import apply_failure_injection, apply_requirement_change
from zyra_scheduler import (
    ResourceScheduler,
    RuntimeWatchdog,
    WorkerPool,
    source_to_target_ledger,
)
from zyra_commands import (
    CommandOrigin,
    ControlCommandRequest,
    ControlRequestStore,
    ControlResult,
    PromptQueueRuntime,
    RuntimeControlContext,
    RuntimeControlDispatcher,
    SideQuestionContextSnapshot,
    SideQuestionRuntime,
    StructuredControlIO,
    StructuredControlHub,
    ControlFrameStore,
    StructuredEnvelope,
    default_command_registry,
    default_control_command_registry,
    parse_slash_command,
)
from zyra_commands.runtime import (
    CallbackSessionOwner,
    CanonicalOwnerHandlerSet,
    CallableCommandSourceProvider,
    CommandOwnerServices,
    CommandRegistryCoordinator,
    CommandSourceKind,
    SessionAction,
    SessionControlRuntime,
    SessionControlStore,
    deterministic_owner_authorization,
    snapshot_from_state,
)
from zyra_runtime import (
    ContextAssemblyRuntime,
    ContextSessionRuntime,
    CodeWorkerTaskApiContractRuntime,
    CodeWorkerTaskApiProjectionRuntime,
    CodeWorkerSessionFoundationRuntime,
    CodeWorkerSessionReplayRuntime,
    CodeWorkerSessionStore,
    JsonPermissionStore,
    LocalArtifactStore,
    PermissionEffect,
    QueryInputProcessor,
    QuerySessionIntegrationRuntime,
    SessionAcceptanceRuntime,
    SessionApiProjectionBuilder,
    SessionFoundationAuditor,
    SessionLifecycleRuntime,
    SessionLineageRuntime,
    ToolCall,
    ToolExecutionContext,
    ToolExecutionRuntime,
    ToolLoopScheduler,
    ToolPermissionRuntime,
    ToolRegistryRuntime,
    ToolResultBudgetRuntime,
    ToolUseContext,
    TaskApiRouteKind,
    WorkerRequest,
    assemble_claude_runtime_context,
    build_api_inventory_contract_report,
    build_claude_productization_integration_report,
    build_claude_source_graph_audit,
    build_productized_claude_runtime_contracts,
    compact_state_projection_from_metadata,
    control_event_from_command,
    default_tool_registry,
    default_worker_descriptors,
    foundation_audit_event,
    tool_result_event,
    TurnLifecycleRuntime,
)
from zyra_runtime.permission.canonical import arguments_digest, build_tool_identity
from zyra_runtime.permission.api import (
    PermissionApiAuthenticationError,
    PermissionApiFacade,
    PermissionApiNotFound,
    PermissionApiOperation,
    PermissionApiResponse,
    PermissionCustodyEnvelope,
    extract_bearer_token,
    permission_api_error_response,
)
from zyra_runtime.permission.control_plane import (
    PermissionControlPlane,
    project_permission_request,
    project_permission_rule,
)
from zyra_runtime.permission.custody import (
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyStore,
)
from zyra_runtime.permission.models import (
    PermissionEffect as RuntimePermissionEffect,
    PermissionEvaluationRequest,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
)
from zyra_skills import (
    SkillCommandSafetyClassifier,
    SkillInvocationRequest,
    SkillInvocationMode,
    SkillIntegrationHealthProbe,
    SkillOutcomeCommitRequest,
    SkillOutcomeCommitRuntime,
    InMemorySkillOutcomeEvidencePort,
    SkillPermissionDenied,
    SkillPermissionPending,
    SkillRuntime,
    SkillRuntimeConfig,
    SkillRuntimeHealthProbe,
    SkillSearchIndex,
    SkillTaskIntegrationRuntime,
    SkillUpdateControlRuntime,
    SkillWorkerDisclosureBatch,
    ToolPermissionRuntimeSkillGateway,
    compact_reference_from_dict,
    default_skill_registry,
    default_skill_runtime,
    utc_now,
)
from zyra_workers import (
    AgentContextMode,
    AgentExecutionMode,
    AgentToolParentContext,
    AgentToolRuntime,
    FanoutFailurePolicy,
    FanoutItem,
    FanoutRequest,
    FanoutStore,
    LogicalFanoutRuntime,
    BrowserRuntimeConfig,
    BrowserRuntimeRegistry,
    BrowserContextScope,
    BrowserContextTaskIntegrationRuntime,
    BrowserContextApiProjectionRuntime,
    BrowserWorkerRuntime,
    CodeWorkerRuntime,
    CodeWorkerSubagentExecutionPort,
    LogicalWorkspaceIsolationPort,
    ParentScopeBuilder,
    PermissionMode,
    SubagentRuntime,
    SubagentRuntimeConfig,
    SubagentControlAction,
    SubagentControlRequest,
    SubagentSpawnRequest,
    YieldContract,
    browser_use_health_summary,
    default_browser_action_registry,
    inspect_browser_use_runtime,
)
from zyra_evaluation import evaluate_task_trace
from zyra_integrations import (
    LedgerAdvanceRequest,
    LedgerSelector,
    LedgerWorkflow,
    AtomicLedgerStore,
    InternalizationLedgerEntry,
    InternalizationLedgerAuditor,
    acceptance_payload,
    build_accounting_report,
    build_acceptance_report,
    build_clean_boundary_report,
    build_cleanroom_report,
    build_evidence_graph_report,
    build_full_ledger_report,
    build_line_bucket_report,
    build_line_count_report,
    build_mutation_consistency_report,
    build_policy_matrix_report,
    build_reachability_report,
    build_schema_contract_report,
    build_semantic_effect_report,
    build_selection_report,
    build_state_custody_report,
    build_snapshot,
    build_test_quality_report,
    build_unit_readiness_report,
    build_unit_review_report,
    boundary_payload,
    cleanroom_payload,
    evidence_graph_payload,
    event_record_from_audit,
    event_record_from_mutation,
    line_count_payload,
    line_bucket_payload,
    list_snapshots,
    load_project_ledger,
    load_seed_ledger,
    minimum_effective_lines_for_unit,
    mutation_consistency_payload,
    parse_query as parse_ledger_query,
    persistence_payload,
    policy_matrix_payload,
    project_ledger_path,
    reachability_payload,
    save_snapshot_to_default_dir,
    save_project_ledger,
    schema_contract_payload,
    semantic_payload,
    state_custody_payload,
    test_quality_payload,
    unit_review_payload,
    validate_entry_for_persistence,
)
from zyra_integrations.mcp.control import McpControlContext, McpControlRuntime
from zyra_integrations.mcp.event_commit import (
    CallableMcpEventSink,
    CallableMcpEventStorePort,
    McpEventCommitter,
    McpEventStoreCommitResult,
)
from zyra_integrations.mcp.runtime import McpClientRuntime
from zyra_commands.mcp_control import McpCommandAdapter

if __package__:
    from .mcp_api import (
        McpApiAuthorizationError,
        McpApiFacade,
        McpMutationAuthorization,
        McpMutationRequest,
    )
else:  # pragma: no cover - direct development script entry.
    from mcp_api import (
        McpApiAuthorizationError,
        McpApiFacade,
        McpMutationAuthorization,
        McpMutationRequest,
    )


def event_log_path() -> Path:
    configured = Path(os.environ.get("ZYRA_EVENT_LOG", "tmp/events.jsonl"))
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


def sqlite_path() -> Path:
    configured = Path(os.environ.get("ZYRA_SQLITE_PATH", "tmp/zyra.sqlite3"))
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


def tool_workspace_path() -> Path:
    """Legacy non-task tooling root.

    CodeWorkerRuntime and BrowserWorker no longer use this path for task work;
    they receive an epoch-fenced task mount from WorkspaceManagerRuntime.
    """

    configured = Path(os.environ.get("ZYRA_TOOL_WORKSPACE", "tmp/workspace"))
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


_WORKSPACE_RUNTIME_LOCK = threading.RLock()
_WORKSPACE_RUNTIME_INSTANCE: WorkspaceManagerRuntime | None = None
_WORKSPACE_RUNTIME_KEY: tuple[str, str, bool] | None = None
_WORKSPACE_EVENT_LOCK = threading.RLock()
_WORKSPACE_PENDING_EVENTS: dict[str, list[EventRecord]] = {}


def workspace_manager_config() -> WorkspaceManagerConfig:
    # The default is isolated beside the configured legacy tool path so test,
    # local, and packaged instances do not share workspace state accidentally.
    # It is not a fallback workspace: task workers only receive manager-owned
    # mounts below this separate service root.
    return WorkspaceManagerConfig.from_environment(
        base_root=tool_workspace_path().parent / ".zyra-workspace-manager"
    )


def _queue_workspace_event(event_type: str, payload: Any) -> None:
    value = dict(payload or {})
    task_id = str(value.get("task_id") or "")
    run_id = str(value.get("run_id") or "")
    if not task_id or not run_id:
        raise ValueError("workspace events require canonical run/task identity")
    event = EventRecord(
        run_id=run_id,
        task_id=task_id,
        event_type=EventType.SYSTEM_NOTICE,
        node_id=None,
        payload={
            "workspace_event": {
                "event_type": event_type,
                **value,
            }
        },
    )
    with _WORKSPACE_EVENT_LOCK:
        _WORKSPACE_PENDING_EVENTS.setdefault(task_id, []).append(event)


def drain_workspace_events(task_id: str) -> list[EventRecord]:
    with _WORKSPACE_EVENT_LOCK:
        return list(_WORKSPACE_PENDING_EVENTS.pop(task_id, ()))


def persist_workspace_events(
    store: SQLiteStore,
    *,
    task_id: str = "",
    workspace_id: str = "",
) -> list[EventRecord]:
    selected_task_id = task_id
    if not selected_task_id and workspace_id:
        binding = get_workspace_manager().store.get_binding(workspace_id)
        selected_task_id = binding.task_id if binding is not None else ""
    events = drain_workspace_events(selected_task_id) if selected_task_id else []
    if events:
        persist_events(store, events)
    return events


def get_workspace_manager() -> WorkspaceManagerRuntime:
    global _WORKSPACE_RUNTIME_INSTANCE, _WORKSPACE_RUNTIME_KEY
    config = workspace_manager_config()
    key = (str(config.state_root), str(config.data_root), config.local_enabled)
    with _WORKSPACE_RUNTIME_LOCK:
        if _WORKSPACE_RUNTIME_INSTANCE is None or _WORKSPACE_RUNTIME_KEY != key:
            _WORKSPACE_RUNTIME_INSTANCE = WorkspaceManagerRuntime(
                config,
                event_sink=_queue_workspace_event,
            )
            _WORKSPACE_RUNTIME_INSTANCE.recover_on_startup()
            _WORKSPACE_RUNTIME_KEY = key
        return _WORKSPACE_RUNTIME_INSTANCE


def reset_workspace_manager(runtime: WorkspaceManagerRuntime | None = None) -> None:
    global _WORKSPACE_RUNTIME_INSTANCE, _WORKSPACE_RUNTIME_KEY
    with _WORKSPACE_RUNTIME_LOCK:
        _WORKSPACE_RUNTIME_INSTANCE = runtime
        if runtime is None:
            _WORKSPACE_RUNTIME_KEY = None
        else:
            _WORKSPACE_RUNTIME_KEY = (
                str(runtime.config.state_root),
                str(runtime.config.data_root),
                runtime.config.local_enabled,
            )
    with _WORKSPACE_EVENT_LOCK:
        _WORKSPACE_PENDING_EVENTS.clear()


def task_workspace_root(*, task_id: str, session_id: str, worker_id: str) -> Path:
    manager = get_workspace_manager()
    # A worker's query/permission session can rotate while the task workspace
    # remains the same canonical binding.  Resolve by task here; the binding's
    # persisted session identity is still returned by the workspace API and is
    # never overwritten by a per-request worker session.
    access = manager.acquire_for_worker(
        task_id=task_id,
        session_id="",
        worker_id=worker_id,
    )
    return manager.internal_task_root(access)


def artifact_root_path() -> Path:
    configured = Path(os.environ.get("ZYRA_ARTIFACT_ROOT", "tmp/artifacts"))
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


def permission_store_path() -> Path:
    configured = Path(os.environ.get("ZYRA_PERMISSION_STORE", "tmp/permissions.json"))
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


def permission_state_path() -> Path:
    """Return the sole authoritative permission state path.

    ``tmp/permissions.json`` is retained only for the pre-M1 compatibility
    projection used by unrelated historical graph code.  Every interactive
    permission operation and every execution guard in this API shares this
    state owner.
    """

    configured = Path(
        os.environ.get(
            "ZYRA_PERMISSION_STATE",
            str(artifact_root_path() / ".permission" / "state.json"),
        )
    )
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


def mcp_state_path() -> Path:
    configured = Path(
        os.environ.get(
            "ZYRA_MCP_STATE",
            str(artifact_root_path() / ".mcp" / "state.json"),
        )
    )
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


_MCP_RUNTIME_LOCK = threading.RLock()
_MCP_RUNTIME_INSTANCE: McpClientRuntime | None = None
_MCP_RUNTIME_KEY: tuple[str, str] | None = None


def _commit_mcp_runtime_event(event: EventRecord) -> None:
    if not str(event.run_id or "") or not str(event.task_id or ""):
        raise ValueError("MCP event has no canonical run/task partition")
    store = get_store()

    def partition_revision(run_id: str, task_id: str) -> int:
        return sum(
            1
            for value in store.task_events(task_id)
            if str(value.get("run_id") or "") == run_id
        )

    def event_fingerprints(
        run_id: str,
        task_id: str,
        event_ids: Any,
    ) -> dict[str, str]:
        selected = set(str(value) for value in event_ids)
        result: dict[str, str] = {}
        for value in store.task_events(task_id):
            if str(value.get("run_id") or "") != run_id:
                continue
            event_id = str(value.get("event_id") or value.get("id") or "")
            if event_id not in selected:
                continue
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            result[event_id] = "sha256:" + hashlib.sha256(encoded).hexdigest()
        return result

    def commit_events(
        run_id: str,
        task_id: str,
        events: Any,
        *,
        expected_revision: int,
        idempotency_key: str,
        atomic: bool,
    ) -> McpEventStoreCommitResult:
        del idempotency_key, atomic
        current = partition_revision(run_id, task_id)
        if current != expected_revision:
            return McpEventStoreCommitResult(
                committed=False,
                partition_revision=current,
                retryable=True,
                conflict=True,
                error_code="event_partition_revision_conflict",
            )
        persist_events(store, list(events))
        return McpEventStoreCommitResult(
            committed=True,
            partition_revision=partition_revision(run_id, task_id),
            committed_event_ids=tuple(str(value.event_id) for value in events),
            metadata={"state_owner": "SQLiteStore", "atomic_adapter": True},
        )

    port = CallableMcpEventStorePort(
        partition_revision=partition_revision,
        event_fingerprints=event_fingerprints,
        commit_events=commit_events,
    )
    sink = CallableMcpEventSink(McpEventCommitter(port))
    sink.append(event)


def get_mcp_runtime() -> McpClientRuntime:
    """Return the process-live MCP runtime with durable Zyra state custody."""

    global _MCP_RUNTIME_INSTANCE, _MCP_RUNTIME_KEY
    key = (str(mcp_state_path().resolve()), str(artifact_root_path().resolve()))
    with _MCP_RUNTIME_LOCK:
        if _MCP_RUNTIME_INSTANCE is None or _MCP_RUNTIME_KEY != key:
            if _MCP_RUNTIME_INSTANCE is not None:
                _MCP_RUNTIME_INSTANCE.connection_runtime.close_all()
            _MCP_RUNTIME_INSTANCE = McpClientRuntime.from_paths(
                state_path=key[0],
                artifact_root=key[1],
                event_sink=_commit_mcp_runtime_event,
            )
            _MCP_RUNTIME_KEY = key
        return _MCP_RUNTIME_INSTANCE


def reset_mcp_runtime(runtime: McpClientRuntime | None = None) -> None:
    """Test/development reset without changing the production state owner."""

    global _MCP_RUNTIME_INSTANCE, _MCP_RUNTIME_KEY
    with _MCP_RUNTIME_LOCK:
        if _MCP_RUNTIME_INSTANCE is not None and _MCP_RUNTIME_INSTANCE is not runtime:
            _MCP_RUNTIME_INSTANCE.connection_runtime.close_all()
        _MCP_RUNTIME_INSTANCE = runtime
        _MCP_RUNTIME_KEY = (
            (str(mcp_state_path().resolve()), str(artifact_root_path().resolve()))
            if runtime is not None
            else None
        )


def get_mcp_command_adapter() -> McpCommandAdapter:
    runtime = get_mcp_runtime()
    return McpCommandAdapter(
        runtime.control_runtime,
        prompt_source=runtime,
    )


def get_runtime_command_registry() -> Any:
    return get_mcp_command_adapter().registry(default_command_registry())


_CONTROL_RUNTIME_LOCK = threading.RLock()
_CONTROL_REGISTRY: Any | None = None
_CONTROL_SOURCE_COORDINATOR: CommandRegistryCoordinator | None = None
_CONTROL_DISPATCHER: RuntimeControlDispatcher | None = None
_STRUCTURED_CONTROL_HUB: StructuredControlHub | None = None
_CONTROL_RUNTIME_KEY: str | None = None
_SUBAGENT_RUNTIME_LOCK = threading.RLock()
_SUBAGENT_RUNTIME: SubagentRuntime | None = None
_SUBAGENT_RUNTIME_KEY: str | None = None
_FANOUT_RUNTIME_INSTANCES: dict[str, LogicalFanoutRuntime] = {}
_BROWSER_RUNTIME_LOCK = threading.RLock()
_BROWSER_RUNTIME_REGISTRY = BrowserRuntimeRegistry()
_BROWSER_CONTEXT_TASK_INTEGRATION = BrowserContextTaskIntegrationRuntime()


def browser_runtime_config() -> BrowserRuntimeConfig:
    return BrowserRuntimeConfig(
        state_root=artifact_root_path() / ".browser-session" / "state",
        runtime_root=PROJECT_ROOT / "tmp" / "browser-session-runtime",
        artifact_root=artifact_root_path(),
        request_timeout_seconds=float(os.environ.get("ZYRA_BROWSER_REQUEST_TIMEOUT", "15")),
        connect_timeout_seconds=float(os.environ.get("ZYRA_BROWSER_CONNECT_TIMEOUT", "15")),
    )


def get_browser_runtime() -> Any:
    """Return the process-live 04A browser resource owner.

    Canonical task/session identity remains in SQLite/02B.  This registry owns
    only Chrome, CDP, target/focus, event-bus and reconnect resources whose
    process lifetime must span sequential HTTP worker requests.
    """

    with _BROWSER_RUNTIME_LOCK:
        return _BROWSER_RUNTIME_REGISTRY.get_or_create(browser_runtime_config())


def get_browser_runtime_services(
    *,
    task_id: str = "",
    session_id: str = "",
    worker_id: str = "BrowserWorker",
) -> tuple[Any, BrowserWorkerRuntime]:
    """Bind API requests to one registry runtime and its shared services."""

    runtime = get_browser_runtime()
    workspace_edit_port = None
    workspace_gateway_required = False
    if task_id:
        manager = get_workspace_manager()
        workspace_access = manager.acquire_for_worker(
            task_id=task_id,
            session_id=session_id,
            worker_id=worker_id,
        )
        workspace_root = manager.internal_task_root(workspace_access)
        binding = manager.store.require_binding(workspace_access.workspace_id)
        workspace_edit_port = WorkspaceEditPort(
            manager,
            workspace_access,
            worker_id=worker_id,
            run_id=binding.run_id,
            task_id=binding.task_id,
            artifact_store=LocalArtifactStore(artifact_root_path()),
        )
        workspace_gateway_required = True
    else:
        workspace_root = tool_workspace_path()
    worker = BrowserWorkerRuntime(
        project_root=PROJECT_ROOT,
        workspace_root=workspace_root,
        artifact_root=artifact_root_path(),
        permission_state_path=permission_state_path(),
        browser_session_runtime=runtime,
        browser_runtime_registry=_BROWSER_RUNTIME_REGISTRY,
        workspace_edit_port=workspace_edit_port,
        workspace_gateway_required=workspace_gateway_required,
    )
    return runtime, worker


def _browser_projection_payload(projection: Any) -> dict[str, Any]:
    if isinstance(projection, dict):
        return dict(projection)
    to_dict = getattr(projection, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, dict):
            return dict(value)
        return {"value": to_jsonable(value)}
    value = to_jsonable(projection)
    if isinstance(value, dict):
        return value
    return {"value": value}


def reset_browser_runtime(*, stop: bool = True) -> None:
    """Explicit test/development reset; never silently replace a live owner."""

    with _BROWSER_RUNTIME_LOCK:
        _BROWSER_RUNTIME_REGISTRY.shutdown(force=stop)


def control_state_path() -> Path:
    configured = Path(os.environ.get("ZYRA_CONTROL_STATE", str(artifact_root_path() / ".control")))
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def subagent_state_path() -> Path:
    configured = Path(os.environ.get("ZYRA_SUBAGENT_STATE", str(artifact_root_path() / ".subagents")))
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def get_control_command_registry() -> Any:
    global _CONTROL_REGISTRY, _CONTROL_SOURCE_COORDINATOR
    with _CONTROL_RUNTIME_LOCK:
        if _CONTROL_REGISTRY is None:
            _CONTROL_REGISTRY = default_control_command_registry()
            _CONTROL_SOURCE_COORDINATOR = _build_command_source_coordinator(_CONTROL_REGISTRY)
            _CONTROL_SOURCE_COORDINATOR.refresh_all()
        return _CONTROL_REGISTRY


def get_command_source_coordinator() -> CommandRegistryCoordinator:
    get_control_command_registry()
    if _CONTROL_SOURCE_COORDINATOR is None:
        raise RuntimeError("command source coordinator was not initialized")
    return _CONTROL_SOURCE_COORDINATOR


def _build_command_source_coordinator(registry: Any) -> CommandRegistryCoordinator:
    coordinator = CommandRegistryCoordinator(
        registry,
        control_state_path() / "command-sources.json",
        event_sink=_commit_runtime_event,
    )

    def mcp_commands() -> tuple[Any, ...]:
        return tuple(get_mcp_runtime().prompt_commands())

    coordinator.register(CallableCommandSourceProvider(
        "mcp-prompts",
        CommandSourceKind.MCP,
        mcp_commands,
        revision_loader=lambda: str(get_mcp_runtime().diagnostics().get("catalog_generation") or "live"),
    ))

    def skill_commands() -> tuple[dict[str, Any], ...]:
        runtime = default_skill_runtime()
        runtime.reload_if_changed()
        values = []
        for skill in runtime.list():
            if not bool(getattr(skill, "user_invocable", False)):
                continue
            values.append({
                "name": f"/skill:{skill.qualified_name}",
                "description": skill.description or f"Invoke skill {skill.qualified_name}.",
                "handler_id": "skill.command",
                "metadata": {
                    "skill_ref": skill.qualified_name,
                    "content_digest": skill.content_digest,
                },
            })
        return tuple(values)

    coordinator.register(CallableCommandSourceProvider(
        "skill-registry",
        CommandSourceKind.SKILL,
        skill_commands,
        revision_loader=lambda: str(default_skill_runtime().registry.snapshot().generation),
    ))

    def plugin_commands() -> tuple[dict[str, Any], ...]:
        snapshot = default_skill_runtime().plugin_runtime.snapshot()
        values: list[dict[str, Any]] = []
        for plugin_id, plugin in sorted(snapshot.plugins.items()):
            for command in plugin.get("commands") or ():
                raw = dict(command) if isinstance(command, dict) else {}
                name = str(raw.get("name") or "").strip().lstrip("/")
                if not name:
                    continue
                values.append({
                    "name": f"/{plugin_id}:{name}",
                    "description": str(raw.get("description") or f"Invoke plugin command {name}."),
                    "handler_id": "skill_plugin.command",
                    "metadata": {"plugin_id": plugin_id, "plugin_command": name},
                })
        return tuple(values)

    coordinator.register(CallableCommandSourceProvider(
        "plugin-cache",
        CommandSourceKind.PLUGIN,
        plugin_commands,
        revision_loader=lambda: str(default_skill_runtime().plugin_runtime.snapshot().generation),
    ))

    project_commands = PROJECT_ROOT / ".zyra" / "commands.json"

    def project_workflows() -> tuple[dict[str, Any], ...]:
        if not project_commands.exists():
            return ()
        value = json.loads(project_commands.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError(".zyra/commands.json must contain an array")
        return tuple(dict(item) for item in value if isinstance(item, dict))

    coordinator.register(CallableCommandSourceProvider(
        "project-workflows",
        CommandSourceKind.PROJECT,
        project_workflows,
        revision_loader=lambda: str(project_commands.stat().st_mtime_ns) if project_commands.exists() else "absent",
    ))
    return coordinator


def _commit_runtime_event(event: EventRecord) -> None:
    persist_events(get_store(), [event])


def get_control_dispatcher() -> RuntimeControlDispatcher:
    global _CONTROL_DISPATCHER, _CONTROL_RUNTIME_KEY
    root = control_state_path().resolve()
    key = str(root)
    with _CONTROL_RUNTIME_LOCK:
        if _CONTROL_DISPATCHER is None or _CONTROL_RUNTIME_KEY != key:
            root.mkdir(parents=True, exist_ok=True)
            _CONTROL_DISPATCHER = RuntimeControlDispatcher(
                registry=get_control_command_registry(),
                request_store=ControlRequestStore(root / "requests.json"),
                prompt_queue=PromptQueueRuntime(root / "prompt-queue.json"),
            )
            _CONTROL_RUNTIME_KEY = key
        return _CONTROL_DISPATCHER


def get_structured_control_hub() -> StructuredControlHub:
    global _STRUCTURED_CONTROL_HUB
    with _CONTROL_RUNTIME_LOCK:
        if _STRUCTURED_CONTROL_HUB is None:
            frame_store = ControlFrameStore(control_state_path() / "structured" / "control-frames.json")
            frame_store.recover()

            def resolve_context(run_id: str, task_id: str, session_id: str) -> RuntimeControlContext:
                state_store = get_store()
                task = state_store.load_task(task_id)
                if task is None or task.run_id != run_id:
                    raise RuntimeError("structured control task identity is unavailable")
                expected_session = str(task.metadata.get("query_session_id") or f"task:{task.task_id}")
                if session_id != expected_session:
                    raise RuntimeError("structured control session identity mismatch")
                return _control_context_for_task(task, state_store)

            _STRUCTURED_CONTROL_HUB = StructuredControlHub(
                frame_store,
                get_control_dispatcher(),
                resolve_context,
                registry_snapshot=lambda: get_control_command_registry().snapshot().to_dict(),
            )
        return _STRUCTURED_CONTROL_HUB


def reset_control_runtime() -> None:
    global _CONTROL_DISPATCHER, _CONTROL_RUNTIME_KEY, _CONTROL_REGISTRY, _CONTROL_SOURCE_COORDINATOR, _STRUCTURED_CONTROL_HUB
    with _CONTROL_RUNTIME_LOCK:
        _CONTROL_DISPATCHER = None
        _CONTROL_RUNTIME_KEY = None
        _CONTROL_REGISTRY = None
        _CONTROL_SOURCE_COORDINATOR = None
        _STRUCTURED_CONTROL_HUB = None


def get_subagent_runtime() -> SubagentRuntime:
    global _SUBAGENT_RUNTIME, _SUBAGENT_RUNTIME_KEY
    root = subagent_state_path().resolve()
    key = str(root)
    with _SUBAGENT_RUNTIME_LOCK:
        if _SUBAGENT_RUNTIME is None or _SUBAGENT_RUNTIME_KEY != key:
            parent_registry = default_tool_registry()
            execution = CodeWorkerSubagentExecutionPort(
                project_root=PROJECT_ROOT,
                artifact_root=artifact_root_path(),
                permission_store=get_permission_store(),
                permission_state_path=permission_state_path(),
                parent_registry=parent_registry,
                mcp_runtime=get_mcp_runtime(),
            )
            config = SubagentRuntimeConfig.from_paths(
                    state_root=root,
                    workspace_root=tool_workspace_path(),
                    artifact_root=artifact_root_path(),
                )
            _SUBAGENT_RUNTIME = SubagentRuntime(
                replace(config, require_signed_parent_scope=True),
                execution_port=execution,
                isolation_port=LogicalWorkspaceIsolationPort(),
                parent_registry=execution.parent_registry,
                event_sink=_commit_runtime_event,
            )
            _SUBAGENT_RUNTIME_KEY = key
        return _SUBAGENT_RUNTIME


def _issue_parent_subagent_scope(state: Any, *, session_id: str) -> Any:
    """Capture the parent ceiling from canonical process owners, never HTTP input."""

    runtime = get_subagent_runtime()
    permission_plane = get_permission_control_plane()
    permission_state = permission_plane.state_store.read_state()
    permission_metadata = permission_state.get("metadata", {}) if isinstance(permission_state, dict) else {}
    integration = permission_metadata.get("permission_integration", {}) if isinstance(permission_metadata, dict) else {}
    permission_revision = int(integration.get("revision") or permission_state.get("revision") or 0) if isinstance(integration, dict) else 0
    try:
        parent_permission_mode = PermissionMode(permission_plane.session_mode(session_id, fallback="default"))
    except ValueError:
        parent_permission_mode = PermissionMode.DEFAULT
    try:
        permission_rules = tuple(get_permission_store().list_rules())
    except Exception:
        # Absence of optional overlay rules narrows the snapshot; it never
        # authorizes a client-declared replacement.
        permission_rules = ()
    mcp_runtime = get_mcp_runtime()
    try:
        mcp_catalog = tuple(mcp_runtime.config_store.list_servers(include_inactive=True).values())
    except Exception:
        mcp_catalog = ()
    state_metadata = state.metadata if isinstance(state.metadata, dict) else {}
    session_revision = int(
        state_metadata.get("session_revision")
        or state_metadata.get("query_session_revision")
        or state_metadata.get("revision")
        or 0
    )
    effective_model = str(state_metadata.get("model_name") or state_metadata.get("model") or "zyra-local-code-model")
    configured_models = state_metadata.get("model_allowlist")
    model_allowlist = (
        tuple(str(item) for item in configured_models if str(item))
        if isinstance(configured_models, (list, tuple))
        else (effective_model,)
    )
    workspace = tool_workspace_path().resolve()
    builder = ParentScopeBuilder(runtime.integration.parent_scopes)
    return builder.build(
        run_id=state.run_id,
        parent_task_id=state.task_id,
        parent_session_id=session_id,
        session_revision=session_revision,
        tool_registry=runtime.parent_registry,
        tool_generation=int(state_metadata.get("tool_registry_generation") or 0),
        permission_mode=parent_permission_mode,
        permission_revision=permission_revision,
        permission_rules=permission_rules,
        mcp_catalog=mcp_catalog,
        model_allowlist=model_allowlist,
        effective_model=effective_model,
        workspace_root=workspace,
        writable_roots=(workspace,),
        readable_roots=(workspace,),
        network_allowed=_truthy(os.environ.get("ZYRA_SUBAGENT_NETWORK_ALLOWED"), default=False),
        skill_refs=tuple(state_metadata.get("skill_invocation_refs") or ()),
        hook_refs=tuple(state_metadata.get("active_hook_refs") or ()),
        context_epoch=int(state_metadata.get("context_epoch") or 0),
        compact_boundary_id=str(state_metadata.get("compact_boundary_id") or ""),
        metadata={"origin": "api-parent-scope", "state_owner": "task+permission+mcp+model"},
    )


def reset_subagent_runtime() -> None:
    global _SUBAGENT_RUNTIME, _SUBAGENT_RUNTIME_KEY, _FANOUT_RUNTIME_INSTANCES
    with _SUBAGENT_RUNTIME_LOCK:
        _SUBAGENT_RUNTIME = None
        _SUBAGENT_RUNTIME_KEY = None
        _FANOUT_RUNTIME_INSTANCES = {}


def _fanout_runtime_for(
    state: Any,
    *,
    parent_scope: Any,
    context_payload: dict[str, Any],
) -> LogicalFanoutRuntime:
    subagents = get_subagent_runtime()
    key = f"{state.task_id}:{parent_scope.snapshot_id}"

    def spawn_request(record: Any, child: Any) -> SubagentSpawnRequest:
        item = child.item
        constraints = {
            **dict(item.constraints),
            "typed_yield_required": True,
            "typed_yield_schema": dict((record.request.yield_contract or YieldContract(
                contract_id=f"fanout:{record.group_id}", schema={"type": "object"}
            )).schema),
            "model_name": item.requested_model or parent_scope.effective_model,
            "writable_paths": list(item.requested_writable_paths),
            "read_only_paths": list(item.requested_readable_paths),
        }
        return SubagentSpawnRequest(
            run_id=state.run_id,
            parent_task_id=state.task_id,
            parent_session_id=record.request.parent_session_id,
            parent_worker_request_id=f"fanout:{record.group_id}",
            agent_type=item.agent_type,
            prompt=(record.request.shared_context + "\n\n" + item.prompt).strip(),
            parent_tools=parent_scope.tool_names,
            parent_permission_mode=parent_scope.permission_mode,
            requested_tools=item.requested_tools,
            requested_permission_mode=None,
            available_mcp_servers=parent_scope.mcp_servers,
            requested_mcp_servers=item.requested_mcp_servers,
            context_mode=AgentContextMode.ISOLATED,
            execution_mode=AgentExecutionMode.FOREGROUND,
            workspace_root=parent_scope.workspace_root,
            requested_cwd="",
            parent_scope_snapshot_id=parent_scope.snapshot_id,
            context_payload=dict(context_payload),
            constraints=constraints,
            idempotency_key=f"{record.request.idempotency_key}:{item.item_id}",
            task_id=child.task_id,
            metadata={
                "root_task_id": state.task_id,
                "node_id": state.root_node_id,
                "origin": "fanout-api",
                "fanout_group_id": record.group_id,
                "fanout_item_id": item.item_id,
                "parent_scope_snapshot_id": parent_scope.snapshot_id,
                "expected_parent_session_revision": parent_scope.session_revision,
                "expected_parent_permission_revision": parent_scope.permission_revision,
                "expected_parent_tool_generation": parent_scope.tool_generation,
                "expected_parent_mcp_generations": dict(parent_scope.mcp_catalog_generations),
            },
        )

    runtime = LogicalFanoutRuntime(
        FanoutStore(subagent_state_path() / "fanout.json"),
        subagents,
        subagents.integration.typed_yields,
        subagents.integration.execution_receipts,
        event_sink=_commit_runtime_event,
        spawn_request_factory=spawn_request,
    )
    _FANOUT_RUNTIME_INSTANCES[key] = runtime
    return runtime


def get_permission_control_plane() -> PermissionControlPlane:
    return PermissionControlPlane.from_path(
        permission_state_path(),
        allow_standing_rules=_truthy(
            os.environ.get("ZYRA_PERMISSION_ALLOW_STANDING_RULES"),
            default=False,
        ),
        allow_mode_updates=True,
    )


def permission_workspace_root(task_id: str = "") -> Path:
    """Resolve the internal root used in permission-custody fingerprints."""

    if not task_id:
        return tool_workspace_path()
    manager = get_workspace_manager()
    binding = manager.store.find_binding(task_id=task_id, workspace_kind="task")
    if binding is None:
        return tool_workspace_path()
    manager.backend.open(binding)
    return manager.backend.mount_root(binding, WorkspaceKind.TASK)


def permission_session_workspace_root(*, task_id: str = "", session_id: str = "") -> Path:
    """Use the server-owned custody binding when a session already exists."""

    if session_id:
        state = get_permission_control_plane().state_store.read_state()
        metadata = state.get("metadata") if isinstance(state, dict) else None
        custody = metadata.get("session_custody") if isinstance(metadata, dict) else None
        records = custody.get("records") if isinstance(custody, dict) else None
        record = records.get(session_id) if isinstance(records, dict) else None
        binding = record.get("binding") if isinstance(record, dict) else None
        if isinstance(binding, dict):
            bound_task_id = str(binding.get("task_id") or "")
            bound_root = str(binding.get("workspace_root") or "")
            if bound_root and (not task_id or bound_task_id == task_id):
                return Path(bound_root).resolve()
    return permission_workspace_root(task_id)


def get_permission_api_facade(*, task_id: str = "", session_id: str = "") -> PermissionApiFacade:
    return PermissionApiFacade(
        get_permission_control_plane(),
        workspace_root=permission_session_workspace_root(
            task_id=task_id,
            session_id=session_id,
        ),
        service_token=os.environ.get("ZYRA_PERMISSION_SERVICE_TOKEN", ""),
        expose_custody_token_in_body=True,
    )


def _install_task_skill_permission_ceiling(
    *,
    state: Any,
    permission_runtime: ToolPermissionRuntime,
    permission_session_id: str,
    workspace_root: str | Path,
) -> tuple[SkillRuntime | None, str]:
    """Restore task-owned 03C state into the active 03A hook adapter."""

    snapshot = state.metadata.get("skill_runtime_state")
    if not isinstance(snapshot, dict):
        return None, ""
    inner = snapshot.get("state_snapshot") if isinstance(snapshot.get("state_snapshot"), dict) else snapshot
    if not inner.get("states"):
        return None, ""
    runtime = SkillRuntime(
        SkillRuntimeConfig.for_project(
            PROJECT_ROOT,
            workspace_root=workspace_root,
            include_user_skills=False,
            disabled=_truthy(os.environ.get("ZYRA_SKILL_RUNTIME_DISABLED"), default=False),
        ),
        state_snapshot=snapshot,
    )
    runtime.bootstrap()
    hook_id = runtime.install_permission_hook(
        permission_runtime.evaluator.hook_adapter,
        permission_session_id=permission_session_id,
    )
    return runtime, hook_id


def _task_skill_worker_messages(
    state: Any,
    *,
    worker_request_id: str = "",
) -> tuple[list[AgentMessage], SkillWorkerDisclosureBatch | None]:
    """Materialize exact skill revisions into the next real worker request."""

    checkpoint = state.metadata.get("skill_runtime_state")
    context = state.metadata.get("skill_session_context")
    if not isinstance(checkpoint, dict) or not isinstance(context, dict):
        return [], None
    raw_references = context.get("invoked_skill_refs")
    if not isinstance(raw_references, list):
        return [], None
    runtime = SkillRuntime(
        SkillRuntimeConfig.for_project(
            PROJECT_ROOT,
            workspace_root=tool_workspace_path(),
            include_user_skills=False,
            disabled=_truthy(os.environ.get("ZYRA_SKILL_RUNTIME_DISABLED"), default=False),
        ),
        state_snapshot=checkpoint,
    )
    runtime.bootstrap()
    bridge = SkillTaskIntegrationRuntime()
    batch = bridge.prepare_disclosures(
        metadata=state.metadata,
        runtime=runtime,
        run_id=state.run_id,
        task_id=state.task_id,
        worker_request_id=worker_request_id,
    )
    messages: list[AgentMessage] = []
    for disclosure in batch.disclosures:
        projection = disclosure.to_message_projection()
        messages.append(
            AgentMessage(
                run_id=state.run_id,
                task_id=state.task_id,
                sender_role=AgentRole.USER,
                receiver_role=AgentRole.WORKER,
                intent=MessageIntent.REQUEST,
                content=str(projection["content"]),
                summary=str(projection["summary"]),
                message_budget_chars=int(projection["message_budget_chars"]),
                metadata=dict(projection["metadata"]),
            )
        )
    return messages, None if batch.empty else batch


def _execute_guarded_api_tool(
    *,
    state: Any,
    node_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    context: ToolExecutionContext,
    session_id: str,
    worker_request_id: str,
    tool_call_id: str,
    custody_token: str = "",
    external_session_exists: bool = False,
) -> tuple[
    ToolCall,
    Any,
    tuple[EventRecord, ...],
    PermissionCustodyEnvelope,
]:
    """Execute one API tool through the same deterministic permission guard.

    The legacy permission JSON remains a compatibility projection for older
    graph context.  It is never accepted as execution authority; structured
    resolve/retry and execution grants share ``PermissionStateStore``.
    Exact, one-use rules below represent only low-risk actions explicitly
    initiated through this interactive API route; shell/network stay on ASK.
    """

    control_plane = get_permission_control_plane()
    custody_receipt = control_plane.custody_store.claim(
        PermissionSessionCustodyBinding(
            session_id=session_id,
            run_id=state.run_id,
            task_id=state.task_id,
            workspace_root=str(context.workspace_root),
        ),
        presented_token=custody_token,
        external_session_exists=external_session_exists,
    )
    custody_envelope = PermissionCustodyEnvelope.from_receipt(custody_receipt)
    materialization = ToolRegistryRuntime(context.registry).materialize(
        worker_request_id=worker_request_id,
        session_id=session_id,
        workspace_root=context.workspace_root,
    )
    scheduler = ToolLoopScheduler(materialization.to_registry())
    plan = scheduler.plan_turn(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=node_id,
        worker_request_id=worker_request_id,
        turn_index=1,
        steps=[
            {
                "tool_name": tool_name,
                "arguments": dict(arguments),
                "tool_call_id": tool_call_id,
                "metadata": {
                    "permission_session_custody_fingerprint": (
                        custody_receipt.custody_fingerprint
                    ),
                    "api_exact_retry": str(external_session_exists).lower(),
                },
            }
        ],
    )
    scheduled = plan.requests[0]
    permission_runtime = ToolPermissionRuntime.for_session(
        session_id=session_id,
        state_path=permission_state_path(),
        workspace_root=context.workspace_root,
        custody_fingerprint=custody_receipt.custody_fingerprint,
    )
    skill_policy_runtime, skill_policy_hook_id = _install_task_skill_permission_ceiling(
        state=state,
        permission_runtime=permission_runtime,
        permission_session_id=session_id,
        workspace_root=context.workspace_root,
    )
    if _api_low_risk_explicit_action(context, scheduled.call):
        permission_runtime.rule_store.add(
            PermissionRuleRecord(
                rule_id=f"api-exact-{scheduled.call.tool_call_id}",
                effect=RuntimePermissionEffect.ALLOW,
                source=PermissionRuleSource.COMMAND,
                scope=PermissionScope(
                    PermissionScopeKind.ACTION,
                    session_id=session_id,
                    task_id=state.task_id,
                    run_id=state.run_id,
                    workspace_root=str(context.workspace_root),
                    tool_namespace="builtin",
                    tool_name=scheduled.tool_name,
                    argument_digest=arguments_digest(scheduled.arguments),
                ),
                namespace_pattern="builtin",
                tool_pattern=scheduled.tool_name,
                reason="exact low-risk action explicitly invoked through the task API",
                max_uses=1,
                metadata={
                    "authority": "interactive_task_api",
                    "projection_only_legacy_store": True,
                },
            )
        )
    tool_context = ToolUseContext.for_turn(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=node_id,
        worker_request_id=worker_request_id,
        session_id=session_id,
        turn_id=f"api-turn:{scheduled.call.tool_call_id}",
        turn_index=1,
        materialization=materialization,
    )
    receipt = ToolExecutionRuntime(
        context,
        scheduler=scheduler,
        budget_runtime=ToolResultBudgetRuntime(max_result_chars=context.max_inline_chars),
        permission_runtime=permission_runtime,
    ).execute_batch(
        plan.batches[0],
        tool_context=tool_context,
        max_workers=1,
    )[0]
    return (
        receipt.request.call,
        receipt.bounded_result,
        receipt.permission_events,
        custody_envelope,
    )


def _api_low_risk_explicit_action(context: ToolExecutionContext, call: ToolCall) -> bool:
    arguments = call.arguments
    tool_name = call.tool_name
    root = context.workspace_root.resolve()

    def workspace_path(value: Any) -> Path | None:
        if value is None:
            return None
        candidate = Path(str(value))
        target = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        try:
            target.relative_to(root)
        except (OSError, ValueError):
            return None
        return target

    if tool_name == "file_write":
        target = workspace_path(arguments.get("path"))
        return bool(
            target is not None
            and len(str(arguments.get("content") or "")) <= 2_000_000
            and context.permission_policy.decide_write(target).effect is PermissionEffect.ALLOW
        )
    if tool_name == "file_edit":
        target = workspace_path(arguments.get("path"))
        return bool(
            target is not None
            and target.is_file()
            and arguments.get("old") is not None
            and context.permission_policy.decide_read(target).effect is PermissionEffect.ALLOW
            and context.permission_policy.decide_write(target).effect is PermissionEffect.ALLOW
        )
    if tool_name == "browser":
        return bool(arguments.get("html")) and not bool(
            arguments.get("url") or arguments.get("allow_network")
        )
    if tool_name == "web_search":
        if arguments.get("url") or arguments.get("allow_network"):
            return False
        paths = arguments.get("paths")
        values = paths if isinstance(paths, list) and paths else ["."]
        return all(workspace_path(value) is not None for value in values)
    if tool_name in {"trace", "checkpoint", "artifact_write"}:
        return True
    return False


def get_store() -> SQLiteStore:
    store = SQLiteStore(sqlite_path())
    store.initialize()
    return store


def graph_execution_context() -> GraphExecutionContext:
    return GraphExecutionContext.from_paths(
        project_root=PROJECT_ROOT,
        workspace_root=tool_workspace_path(),
        artifact_root=artifact_root_path(),
        permission_store_path=permission_store_path(),
    )


def get_permission_store() -> JsonPermissionStore:
    return JsonPermissionStore(permission_store_path())


def _memory_fabric(store: SQLiteStore) -> MemoryFabric:
    return MemoryFabric(store=store, artifact_store=LocalArtifactStore(artifact_root_path()))


def make_task_created_event(user_goal: str) -> tuple[Any, EventRecord]:
    state = create_task_state(user_goal=user_goal)
    event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.TASK_CREATED,
        node_id=state.root_node_id,
        payload={"task": to_jsonable(state)},
    )
    return state, event


def persist_events(store: SQLiteStore, events: list[EventRecord]) -> None:
    for event in events:
        append_jsonl_event(event, event_log_path())
    store.append_events(events)


class JsonRequestError(ValueError):
    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


_TASK_LOCKS_GUARD = threading.Lock()
_TASK_LOCKS: dict[str, threading.RLock] = {}


def _task_lock(task_id: str) -> threading.RLock:
    with _TASK_LOCKS_GUARD:
        return _TASK_LOCKS.setdefault(task_id, threading.RLock())


def _task_node_ids(state: Any) -> set[str]:
    node_ids: set[str] = set()
    root_node_id = str(getattr(state, "root_node_id", "") or "").strip()
    if root_node_id:
        node_ids.add(root_node_id)

    def collect(value: Any, *, node_map: bool = False) -> None:
        if isinstance(value, dict):
            if node_map:
                for key in value:
                    candidate = str(key or "").strip()
                    if candidate:
                        node_ids.add(candidate)
            for key, item in value.items():
                if key in {"node_id", "root_node_id"} and not isinstance(item, (dict, list, tuple, set)):
                    candidate = str(item or "").strip()
                    if candidate:
                        node_ids.add(candidate)
                if key in {"nodes", "node_map"}:
                    collect(item, node_map=isinstance(item, dict))
                elif isinstance(item, (dict, list, tuple, set)):
                    collect(item)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)

    serialized = to_jsonable(state)
    if isinstance(serialized, dict):
        collect(serialized)
    return node_ids


class ZyraRequestHandler(BaseHTTPRequestHandler):
    server_version = "ZyraDevAPI/0.2"

    def _permission_actor_id(self) -> str:
        # The current development API has no end-user authentication layer.
        # Stamp a deployment-owned actor instead of trusting request JSON.
        return str(os.environ.get("ZYRA_PERMISSION_API_ACTOR", "api-operator")).strip() or "api-operator"

    def _permission_authority(
        self,
        facade: PermissionApiFacade,
        values: dict[str, Any],
        *,
        session_id: str = "",
        allow_payload_token: bool = True,
    ) -> Any:
        selected_session = str(session_id or values.get("session_id") or "").strip()
        run_id = str(values.get("run_id") or "").strip()
        task_id = str(values.get("task_id") or "").strip()
        if not selected_session or not run_id or not task_id:
            raise ValueError("session_id, run_id, and task_id are required")
        return facade.authority(
            session_id=selected_session,
            run_id=run_id,
            task_id=task_id,
            custody_token=extract_bearer_token(
                self.headers,
                values if allow_payload_token else None,
            ),
            actor_id=self._permission_actor_id(),
        )

    def _authorize_mcp_mutation(
        self,
        request: McpMutationRequest,
        *,
        payload: dict[str, Any],
        store: SQLiteStore,
    ) -> McpMutationAuthorization:
        """Authorize and atomically consume one exact 03A execution grant.

        The custody bearer proves ownership of the permission session; it is
        not itself an MCP grant.  ``ToolPermissionRuntime`` binds the grant to
        action, server, canonical argument digest, task/run/session, and the
        caller-supplied stable tool-use identity, then consumes it before the
        facade invokes any MCP runtime mutation.
        """

        session_id = str(payload.get("session_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        task_id = str(payload.get("task_id") or "").strip()
        worker_request_id = str(payload.get("worker_request_id") or "").strip()
        tool_use_id = str(payload.get("tool_use_id") or "").strip()
        if not all((session_id, run_id, task_id, worker_request_id, tool_use_id)):
            raise McpApiAuthorizationError(
                "mcp_permission_identity_required",
                retryable=False,
            )
        # Execution grants are private capabilities consumed inside this
        # method.  Accepting a caller-presented grant id/token would create a
        # second authority path and permit confused-deputy replay.
        if payload.get("permission_grant_id") or payload.get("permission_grant_token"):
            raise McpApiAuthorizationError(
                "mcp_presented_grant_rejected",
                retryable=False,
            )

        facade = get_permission_api_facade()
        try:
            authority = self._permission_authority(
                facade,
                payload,
                session_id=session_id,
                allow_payload_token=False,
            )
            self._require_permission_task_identity(
                store,
                task_id=task_id,
                run_id=run_id,
            )
        except Exception as error:  # noqa: BLE001 - never expose custody details.
            raise McpApiAuthorizationError(
                "mcp_permission_authority_invalid",
                retryable=False,
            ) from error

        workspace = Path(authority.workspace_root).resolve()
        permission_runtime = ToolPermissionRuntime.for_session(
            session_id=session_id,
            state_path=permission_state_path(),
            workspace_root=workspace,
            custody_fingerprint=authority.custody_fingerprint,
        )
        task_state = store.load_task(task_id)
        if task_state is None:
            raise McpApiAuthorizationError("mcp_permission_task_not_found", retryable=False)
        _install_task_skill_permission_ceiling(
            state=task_state,
            permission_runtime=permission_runtime,
            permission_session_id=session_id,
            workspace_root=workspace,
        )
        evaluation = PermissionEvaluationRequest(
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            tool_use_id=tool_use_id,
            node_id=str(payload.get("node_id") or "") or None,
            tool_identity=build_tool_identity(
                request.action,
                namespace="mcp-control",
                server_id=request.server_id,
                version="v1",
            ),
            arguments=dict(request.arguments),
            operation="mcp_control_mutation",
            workspace_root=str(workspace),
            principal_id=authority.principal_id or authority.actor_id,
            interactive=True,
            requires_interaction=True,
            risk_tags=("mcp_control_mutation", "external_runtime_state"),
            attributes={
                "mcp_action": request.action,
                "mcp_server_id": request.server_id,
                "authority_id": authority.authority_id,
            },
            metadata={
                "owner_unit": "M1-03B",
                "permission_custody": "M1-03A.PermissionSessionCustodyStore",
                "grant_custody": "ToolPermissionRuntime.ExecutionGrantStore",
                "http_facade": "McpApiFacade",
            },
        )
        guarded = permission_runtime.guard(evaluation)
        permission_events = list(guarded.events)
        if permission_events:
            persist_events(store, permission_events)
        evidence = {
            "effect": str(guarded.effect),
            "decision_id": guarded.decision.decision_id,
            "request_id": guarded.decision.request_id,
            "request_fingerprint": guarded.request.request_fingerprint,
            "arguments_digest": guarded.request.arguments_digest,
            "custody_fingerprint": authority.custody_fingerprint,
            "restored_approval": guarded.restored_approval,
            "pending_request": (
                guarded.pending_request.to_dict() if guarded.pending_request is not None else None
            ),
            "events": [to_jsonable(event) for event in permission_events],
            "exact_one_shot": True,
        }
        if guarded.effect is RuntimePermissionEffect.ASK:
            raise McpApiAuthorizationError(
                "mcp_permission_pending",
                status=HTTPStatus.CONFLICT,
                permission=evidence,
            )
        if guarded.effect is not RuntimePermissionEffect.ALLOW or guarded.execution_grant is None:
            raise McpApiAuthorizationError(
                "mcp_permission_denied",
                status=HTTPStatus.FORBIDDEN,
                retryable=False,
                permission=evidence,
            )

        grant = guarded.execution_grant
        call = ToolCall(
            run_id=run_id,
            task_id=task_id,
            node_id=str(payload.get("node_id") or "") or None,
            tool_name=request.action,
            arguments=dict(request.arguments),
            tool_call_id=tool_use_id,
            metadata={
                "tool_namespace": "mcp-control",
                "server_id": request.server_id,
            },
        )
        consumed = permission_runtime.validate_and_consume(
            call,
            grant,
            SimpleNamespace(workspace_root=workspace, registry=None),
        )
        consumption_events = list(permission_runtime.drain_execution_events(tool_use_id))
        if consumption_events:
            persist_events(store, consumption_events)
        all_events = [*permission_events, *consumption_events]
        if not consumed:
            raise McpApiAuthorizationError(
                "mcp_permission_grant_rejected",
                retryable=False,
                permission={
                    **evidence,
                    "grant_id": grant.grant_id,
                    "events": [to_jsonable(event) for event in all_events],
                },
            )
        return McpMutationAuthorization(
            allowed=True,
            decision_id=guarded.decision.decision_id,
            request_id=guarded.decision.request_id,
            grant_id=grant.grant_id,
            custody_fingerprint=authority.custody_fingerprint,
            reason_code=guarded.decision.reason_code,
            events=tuple(to_jsonable(event) for event in all_events),
            metadata={
                "permission_runtime": "ToolPermissionRuntime",
                "control_plane": "PermissionControlPlane",
                "grant_consumed_before_mutation": True,
                "request_fingerprint": guarded.request.request_fingerprint,
            },
        )

    @staticmethod
    def _require_permission_task_identity(
        store: SQLiteStore,
        *,
        task_id: str,
        run_id: str,
    ) -> Any:
        state = store.load_task(str(task_id or ""))
        if state is None:
            raise PermissionApiNotFound("permission task was not found")
        if str(state.run_id) != str(run_id or ""):
            raise ValueError("permission run_id does not match task custody")
        return state

    def _send_permission_response(
        self,
        store: SQLiteStore,
        response: PermissionApiResponse,
    ) -> None:
        if response.events:
            persist_events(store, list(response.events))
        self._send_json(response.status, response.body, headers=response.headers)

    def _handle_permission_get(
        self,
        *,
        parsed: Any,
        parts: list[str],
        store: SQLiteStore,
    ) -> bool:
        if not parts or parts[0] != "permissions":
            return False
        parameters = _flatten_query(parse_qs(parsed.query, keep_blank_values=True))
        facade = get_permission_api_facade(
            task_id=str(parameters.get("task_id") or ""),
            session_id=str(parameters.get("session_id") or ""),
        )
        operation = PermissionApiOperation.HEALTH
        try:
            if parts == ["permissions", "health"]:
                response = facade.health()
            elif parts == ["permissions"] and not parameters.get("session_id"):
                health = facade.health()
                response = PermissionApiResponse(
                    status=HTTPStatus.OK,
                    operation=PermissionApiOperation.HEALTH,
                    body={
                        **health.body,
                        "compatibility_projection": {
                            "legacy_json_store_authority": False,
                            "state_listing_requires_session_custody": True,
                            "structured_paths": [
                                "/permissions/requests",
                                "/permissions/rules",
                                "/permissions/mode",
                                "/permissions/decisions",
                            ],
                        },
                    },
                    headers=health.headers,
                )
            else:
                if any(
                    key in parameters
                    for key in (
                        "custody_token",
                        "permission_session_custody_token",
                        "session_custody_token",
                    )
                ):
                    raise PermissionApiAuthenticationError(
                        "GET permission custody must use the Authorization header"
                    )
                self._require_permission_task_identity(
                    store,
                    task_id=str(parameters.get("task_id") or ""),
                    run_id=str(parameters.get("run_id") or ""),
                )
                authority = self._permission_authority(
                    facade,
                    parameters,
                    allow_payload_token=False,
                )
                if parts == ["permissions"]:
                    operation = PermissionApiOperation.REQUEST_QUERY
                    requests = facade.query_requests(authority, parameters)
                    rules = facade.query_rules(authority)
                    mode = facade.get_mode(authority)
                    response = PermissionApiResponse(
                        status=HTTPStatus.OK,
                        operation=operation,
                        body={
                            "schema": "zyra.permission-api.v1",
                            "ok": True,
                            "operation": operation.value,
                            "state_owner": "PermissionStateStore",
                            "legacy_json_store_authority": False,
                            "requests": requests.body.get("requests", {}),
                            "rules": rules.body.get("rules", []),
                            "mode": mode.body.get("mode", "default"),
                            "session_id": authority.session_id,
                        },
                        headers=requests.headers,
                    )
                elif parts == ["permissions", "requests"]:
                    operation = PermissionApiOperation.REQUEST_QUERY
                    response = facade.query_requests(authority, parameters)
                elif len(parts) == 3 and parts[:2] == ["permissions", "requests"]:
                    operation = PermissionApiOperation.REQUEST_GET
                    response = facade.get_request(authority, parts[2])
                elif parts == ["permissions", "rules"]:
                    operation = PermissionApiOperation.RULE_QUERY
                    response = facade.query_rules(authority)
                elif parts == ["permissions", "mode"]:
                    operation = PermissionApiOperation.MODE_GET
                    response = facade.get_mode(authority)
                elif parts == ["permissions", "decisions"]:
                    operation = PermissionApiOperation.DECISION_QUERY
                    response = facade.query_decisions(
                        authority,
                        limit=_bounded_permission_limit(parameters.get("limit")),
                    )
                else:
                    return False
        except Exception as error:  # noqa: BLE001 - mapped to a redacted permission response.
            response = permission_api_error_response(operation, error)
        self._send_permission_response(store, response)
        return True

    def _handle_permission_post(
        self,
        *,
        parts: list[str],
        payload: dict[str, Any],
        store: SQLiteStore,
    ) -> bool:
        if not parts or parts[0] != "permissions":
            return False
        facade = get_permission_api_facade(
            task_id=str(payload.get("task_id") or ""),
            session_id=str(payload.get("session_id") or ""),
        )
        operation = PermissionApiOperation.REQUEST_CREATE
        try:
            if parts == ["permissions", "sessions", "open"]:
                operation = PermissionApiOperation.SESSION_OPEN
                self._require_permission_task_identity(
                    store,
                    task_id=str(payload.get("task_id") or ""),
                    run_id=str(payload.get("run_id") or ""),
                )
                response = facade.open_session(
                    session_id=str(payload.get("session_id") or ""),
                    run_id=str(payload.get("run_id") or ""),
                    task_id=str(payload.get("task_id") or ""),
                    presented_token=extract_bearer_token(self.headers, payload),
                    external_session_exists=_truthy(
                        payload.get("external_session_exists"),
                        default=False,
                    ),
                )
            elif (
                len(parts) == 4
                and parts[:2] == ["permissions", "sessions"]
                and parts[3] == "resume"
            ):
                operation = PermissionApiOperation.SESSION_RESUME
                self._require_permission_task_identity(
                    store,
                    task_id=str(payload.get("task_id") or ""),
                    run_id=str(payload.get("run_id") or ""),
                )
                response = facade.resume_session(
                    session_id=parts[2],
                    run_id=str(payload.get("run_id") or ""),
                    task_id=str(payload.get("task_id") or ""),
                    custody_token=extract_bearer_token(self.headers, payload),
                    actor_id=self._permission_actor_id(),
                )
            else:
                self._require_permission_task_identity(
                    store,
                    task_id=str(payload.get("task_id") or ""),
                    run_id=str(payload.get("run_id") or ""),
                )
                authority = self._permission_authority(facade, payload)
                node_id = None if payload.get("node_id") is None else str(payload.get("node_id"))
                if parts == ["permissions", "requests"]:
                    operation = PermissionApiOperation.REQUEST_CREATE
                    response = facade.create_request(
                        authority,
                        payload,
                        service_token=str(self.headers.get("X-Zyra-Service-Token") or ""),
                    )
                elif parts == ["permissions", "requests", "expire"]:
                    operation = PermissionApiOperation.REQUEST_EXPIRE
                    response = facade.expire_requests(authority, node_id=node_id)
                elif len(parts) == 4 and parts[:2] == ["permissions", "requests"]:
                    request_id = parts[2]
                    action = parts[3]
                    if action == "deliver":
                        operation = PermissionApiOperation.REQUEST_DELIVER
                        response = facade.deliver_request(
                            authority,
                            request_id,
                            payload,
                            node_id=node_id,
                        )
                    elif action == "resolve":
                        operation = PermissionApiOperation.REQUEST_RESOLVE
                        response = facade.resolve_request(
                            authority,
                            request_id,
                            payload,
                            node_id=node_id,
                        )
                    elif action == "cancel":
                        operation = PermissionApiOperation.REQUEST_CANCEL
                        response = facade.cancel_request(
                            authority,
                            request_id,
                            payload,
                            node_id=node_id,
                        )
                    elif action == "abort":
                        operation = PermissionApiOperation.REQUEST_ABORT
                        response = facade.abort_request(
                            authority,
                            request_id,
                            payload,
                            node_id=node_id,
                        )
                    elif action == "retry":
                        operation = PermissionApiOperation.REQUEST_RETRY
                        response = facade.prepare_retry(
                            authority,
                            request_id,
                            payload,
                            node_id=node_id,
                        )
                    else:
                        return False
                elif parts == ["permissions", "rules"]:
                    operation = PermissionApiOperation.RULE_CREATE
                    response = facade.create_rule(authority, payload, node_id=node_id)
                elif (
                    len(parts) == 4
                    and parts[:2] == ["permissions", "rules"]
                    and parts[3] == "remove"
                ):
                    operation = PermissionApiOperation.RULE_REMOVE
                    response = facade.remove_rule(authority, parts[2], node_id=node_id)
                elif parts == ["permissions", "mode"]:
                    operation = PermissionApiOperation.MODE_UPDATE
                    response = facade.update_mode(authority, payload, node_id=node_id)
                else:
                    return False
        except Exception as error:  # noqa: BLE001 - mapped to a redacted permission response.
            response = permission_api_error_response(operation, error)
        self._send_permission_response(store, response)
        return True

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = _path_parts(parsed.path)
        store = get_store()

        if self._handle_permission_get(parsed=parsed, parts=parts, store=store):
            return

        mcp_response = McpApiFacade(get_mcp_runtime()).handle_get(
            parts,
            _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
        )
        if mcp_response is not None:
            status, body, headers = mcp_response
            self._send_json(status, body, headers=headers)
            return

        try:
            workspace_response = WorkspaceApiService(get_workspace_manager()).route_get(
                tuple(parts),
                _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
            )
        except WorkspaceError as error:
            workspace_response = workspace_error_response(error)
        if workspace_response is not None:
            workspace_id = parts[1] if len(parts) > 1 and parts[0] == "workspaces" else ""
            persist_workspace_events(store, workspace_id=workspace_id)
            self._send_json(
                workspace_response.status,
                workspace_response.body,
                headers=dict(workspace_response.headers),
            )
            return

        if parts == ["health"]:
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "zyra-api",
                    "phase": "m5-resource-scheduler-fault-recovery",
                    "event_log": str(event_log_path()),
                    "sqlite": str(sqlite_path()),
                    "tool_workspace": str(tool_workspace_path()),
                    "artifact_root": str(artifact_root_path()),
                    "permission_store": str(permission_store_path()),
                    "workspace": get_workspace_manager().health(),
                },
            )
            return

        if (
            len(parts) == 3
            and parts[0] == "tasks"
            and parts[2] == "browser-observability"
        ):
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "task_not_found"},
                )
                return
            query = parse_qs(parsed.query)
            view = str((query.get("view") or ["summary"])[0]).strip() or "summary"
            browser_session_id = str(
                (query.get("browser_session_id") or [""])[0]
            ).strip()
            worker_request_id = str(
                (query.get("worker_request_id") or [""])[0]
            ).strip()
            limit = _positive_int(
                (query.get("limit") or ["100"])[0],
                default=100,
            )
            after_sequence = max(
                0,
                int((query.get("after_sequence") or ["0"])[0] or 0),
            )
            try:
                _runtime, browser_worker = get_browser_runtime_services()
                projection = browser_worker.browser_observability_application.query(
                    task_id=state.task_id,
                    browser_session_id=browser_session_id,
                    worker_request_id=worker_request_id,
                    view=view,
                    limit=min(limit, 5_000),
                    after_sequence=after_sequence,
                )
            except (TypeError, ValueError) as error:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "invalid_browser_observability_query",
                        "message": str(error),
                    },
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "task_id": state.task_id,
                    "browser_observability": projection,
                },
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Pragma": "no-cache",
                },
            )
            return

        if parts == ["schema", "sample-task"]:
            state, event = make_task_created_event("Inspect a long-horizon task.")
            graph_events = ensure_default_graph(state)
            self._send_json(
                HTTPStatus.OK,
                {
                    "task": to_jsonable(state),
                    "events": [to_jsonable(item) for item in [event, *graph_events]],
                },
            )
            return

        if parts == ["events"]:
            query = parse_qs(parsed.query)
            limit = _positive_int(query.get("limit", ["100"])[0], default=100)
            self._send_json(HTTPStatus.OK, {"events": store.all_events(limit=limit)})
            return

        if parts == ["commands"]:
            refresh_receipts = get_command_source_coordinator().refresh_all()
            snapshot = get_control_command_registry().snapshot()
            self._send_json(
                HTTPStatus.OK,
                {
                    "commands": [command.to_dict() for command in snapshot.descriptors],
                    "registry": snapshot.to_dict(),
                    "sources": get_command_source_coordinator().snapshot(),
                    "refresh": [item.to_dict() for item in refresh_receipts],
                },
            )
            return

        if parts == ["skills"]:
            skill_runtime = default_skill_runtime()
            query = parse_qs(parsed.query)
            reload_status = skill_runtime.reload_if_changed()
            search_text = str((query.get("q") or query.get("query") or [""])[0]).strip()
            listing_session = str((query.get("session_id") or [""])[0]).strip()
            listing_agent = str((query.get("agent_id") or ["CodeWorkerRuntime"])[0]).strip()
            search_result = (
                SkillSearchIndex(skill_runtime.registry).search(search_text)
                if search_text
                else None
            )
            listing_projection = (
                skill_runtime.listing_projection(
                    session_id=listing_session,
                    agent_id=listing_agent,
                )
                if listing_session and search_result is None
                else None
            )
            if search_result is not None:
                skills = list(search_result.hits)
            elif listing_projection is not None:
                skills = list(listing_projection.entries)
            else:
                skills = list(skill_runtime.list())
            self._send_json(
                HTTPStatus.OK,
                {
                    "skills": [to_jsonable(skill) for skill in skills],
                    "search": to_jsonable(search_result) if search_result is not None else None,
                    "listing_projection": (
                        listing_projection.to_dict() if listing_projection is not None else None
                    ),
                    "reload": reload_status.to_dict(),
                    "registry": skill_runtime.registry.snapshot().to_dict(),
                    "health": SkillRuntimeHealthProbe().probe(skill_runtime).to_dict(),
                    "integration_health": SkillIntegrationHealthProbe().probe(
                        product_root=PROJECT_ROOT,
                        workspace_root=tool_workspace_path(),
                        runtime=skill_runtime,
                    ).to_dict(),
                    "progressive_disclosure": True,
                    "body_loaded": False,
                },
            )
            return

        if parts == ["tools"]:
            base_context = ToolExecutionContext.for_workspace(
                workspace_root=tool_workspace_path(),
                artifact_root=artifact_root_path(),
                permission_store=get_permission_store(),
            )
            mcp_projection = get_mcp_runtime().worker_projection(
                base_context,
                run_id="api-tools-catalog",
                task_id="api-tools-catalog",
                node_id=None,
                session_id="api-tools-catalog",
                worker_request_id="api-tools-catalog",
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "tools": [to_jsonable(tool) for tool in mcp_projection.context.registry.list()],
                    "mcp": mcp_projection.safe_dict(),
                },
            )
            return

        if parts == ["workers"]:
            self._send_json(
                HTTPStatus.OK,
                {
                    "workers": [to_jsonable(worker) for worker in default_worker_descriptors()],
                    "manifests": [to_jsonable(manifest) for manifest in WorkerPool().manifests()],
                },
            )
            return

        if parts == ["scheduler", "manifests"]:
            self._send_json(
                HTTPStatus.OK,
                {
                    "manifests": [to_jsonable(manifest) for manifest in WorkerPool().manifests()],
                    "source_to_target_ledger": source_to_target_ledger(),
                },
            )
            return

        if parts == ["scheduler", "health"]:
            self._send_json(
                HTTPStatus.OK,
                {
                    "health": [to_jsonable(item) for item in WorkerPool().health_snapshot(events=store.all_events(limit=200))],
                    "source_to_target_ledger": source_to_target_ledger(),
                },
            )
            return

        if _is_ledger_list_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            query = parse_ledger_query(query_params)
            include_audit = _truthy(_optional_query_value(parse_qs(parsed.query), "include_findings"))
            if _uses_advanced_ledger_selector(query_params):
                selection = build_selection_report(ledger, LedgerSelector.from_params(query_params), project_root=PROJECT_ROOT)
                payload = {
                    "ledger_path": str(project_ledger_path(PROJECT_ROOT)),
                    "summary": ledger.summary().to_dict(),
                    "entries": selection.entries,
                    "selection": selection.to_dict(),
                }
            else:
                payload = {
                    "ledger_path": str(project_ledger_path(PROJECT_ROOT)),
                    "summary": ledger.summary().to_dict(),
                    "entries": [entry.to_dict() for entry in ledger.query(query)],
                }
            if include_audit:
                payload["audit"] = InternalizationLedgerAuditor(PROJECT_ROOT, strict=True).audit(ledger).to_dict()
            self._send_json(HTTPStatus.OK, payload)
            return

        if _is_ledger_audit_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            strict = _truthy(query_params.get("strict"), default=True)
            report = InternalizationLedgerAuditor(PROJECT_ROOT, strict=strict).audit(ledger)
            filtered = _filtered_audit_payload(report, query_params)
            self._send_json(HTTPStatus.OK, filtered)
            return

        if _is_ledger_readiness_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            line_count = _optional_line_count_report(query_params)
            audit = InternalizationLedgerAuditor(PROJECT_ROOT, strict=True).audit(ledger)
            self._send_json(
                HTTPStatus.OK,
                build_unit_readiness_report(
                    PROJECT_ROOT,
                    ledger,
                    owner_unit=owner_unit,
                    audit_report=audit,
                    line_count_report=line_count,
                ).to_dict(),
            )
            return

        if _is_ledger_report_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            self._send_json(
                HTTPStatus.OK,
                build_full_ledger_report(
                    PROJECT_ROOT,
                    ledger,
                    owner_unit=owner_unit,
                    line_count_report=_optional_line_count_report(query_params),
                ),
            )
            return

        if _is_ledger_accounting_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            include_entries = not _truthy(query_params.get("no_entries"))
            self._send_json(
                HTTPStatus.OK,
                build_accounting_report(
                    PROJECT_ROOT,
                    ledger,
                    owner_unit=owner_unit,
                    include_entries=include_entries,
                ).to_dict(),
            )
            return

        if _is_ledger_linecount_path(parts):
            query_params = _flatten_query(parse_qs(parsed.query))
            base = query_params.get("base") or ""
            if not base:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_base", "message": "linecount requires ?base=<commit>"})
                return
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            minimum = _int_or_default(query_params.get("minimum_effective_lines"), minimum_effective_lines_for_unit(owner_unit))
            report = build_line_count_report(
                PROJECT_ROOT,
                base=base,
                head=query_params.get("head") or "HEAD",
                cached=_truthy(query_params.get("cached")),
                minimum_effective_lines=minimum,
            )
            self._send_json(HTTPStatus.OK, line_count_payload(report))
            return

        if _is_ledger_buckets_path(parts):
            query_params = _flatten_query(parse_qs(parsed.query))
            base = query_params.get("base") or ""
            if not base:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_base", "message": "buckets requires ?base=<commit>"})
                return
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            minimum = _int_or_default(query_params.get("minimum_effective_lines"), minimum_effective_lines_for_unit(owner_unit))
            report = build_line_bucket_report(
                PROJECT_ROOT,
                base=base,
                head=query_params.get("head") or "HEAD",
                cached=_truthy(query_params.get("cached")),
                minimum_effective_lines=minimum,
            )
            self._send_json(HTTPStatus.OK, line_bucket_payload(report))
            return

        if _is_ledger_boundary_path(parts):
            query_params = _flatten_query(parse_qs(parsed.query))
            report = build_clean_boundary_report(
                PROJECT_ROOT,
                include_tests=not _truthy(query_params.get("no_tests")),
                include_cache=_truthy(query_params.get("include_cache")),
                scan_roots=_split_csv(query_params.get("roots") or ""),
            )
            self._send_json(HTTPStatus.OK, boundary_payload(report))
            return

        if _is_ledger_reachability_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            strict_audit = _truthy(query_params.get("strict_audit"))
            report = build_reachability_report(
                PROJECT_ROOT,
                ledger,
                owner_unit=owner_unit,
                include_entries=not _truthy(query_params.get("no_entries")),
                strict_audit=strict_audit,
            )
            self._send_json(HTTPStatus.OK, reachability_payload(report))
            return

        if _is_ledger_acceptance_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or "M1-01A"
            line_count = _optional_line_count_report(query_params)
            strict_audit = _truthy(query_params.get("strict_audit"))
            audit = InternalizationLedgerAuditor(PROJECT_ROOT, strict=strict_audit).audit(ledger)
            boundary = build_clean_boundary_report(
                PROJECT_ROOT,
                include_tests=not _truthy(query_params.get("no_tests")),
                include_cache=_truthy(query_params.get("include_cache")),
                scan_roots=_split_csv(query_params.get("roots") or ""),
            )
            reachability = build_reachability_report(
                PROJECT_ROOT,
                ledger,
                owner_unit=owner_unit,
                include_entries=not _truthy(query_params.get("no_entries")),
                strict_audit=strict_audit,
            )
            report = build_acceptance_report(
                PROJECT_ROOT,
                ledger,
                owner_unit=owner_unit,
                audit_report=audit,
                line_count_report=line_count,
                reachability_report=reachability,
                boundary_report=boundary,
                include_entries=not _truthy(query_params.get("no_entries")),
            )
            self._send_json(HTTPStatus.OK, acceptance_payload(report))
            return

        if _is_ledger_persistence_path(parts):
            self._send_json(HTTPStatus.OK, persistence_payload(AtomicLedgerStore(PROJECT_ROOT)))
            return

        if _is_ledger_cleanroom_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            source_root_value = query_params.get("source_root") or ""
            source_root = Path(source_root_value).resolve() if source_root_value else None
            report = build_cleanroom_report(
                PROJECT_ROOT,
                ledger,
                source_root=source_root,
                include_source_scan=not _truthy(query_params.get("no_source_scan")),
                scan_roots=_split_csv(query_params.get("roots") or ""),
            )
            self._send_json(HTTPStatus.OK, cleanroom_payload(report))
            return

        if _is_ledger_semantic_effects_path(parts):
            self._send_json(HTTPStatus.OK, semantic_payload(build_semantic_effect_report(PROJECT_ROOT)))
            return

        if _is_ledger_test_quality_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            report = build_test_quality_report(
                PROJECT_ROOT,
                ledger,
                owner_unit=owner_unit,
                include_entries=not _truthy(query_params.get("no_entries")),
            )
            self._send_json(HTTPStatus.OK, test_quality_payload(report))
            return

        if _is_ledger_schema_contract_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            report = build_schema_contract_report(
                ledger,
                owner_unit=owner_unit,
                include_entries=not _truthy(query_params.get("no_entries")),
            )
            self._send_json(HTTPStatus.OK, schema_contract_payload(report))
            return

        if _is_ledger_evidence_graph_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            report = build_evidence_graph_report(
                PROJECT_ROOT,
                ledger,
                owner_unit=owner_unit,
                include_nodes=not _truthy(query_params.get("no_nodes")),
            )
            self._send_json(HTTPStatus.OK, evidence_graph_payload(report))
            return

        if _is_ledger_state_custody_path(parts):
            self._send_json(HTTPStatus.OK, state_custody_payload(build_state_custody_report(PROJECT_ROOT)))
            return

        if _is_ledger_mutation_consistency_path(parts):
            self._send_json(HTTPStatus.OK, mutation_consistency_payload(build_mutation_consistency_report(PROJECT_ROOT)))
            return

        if _is_ledger_policy_matrix_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
            report = build_policy_matrix_report(
                ledger,
                owner_unit=owner_unit,
                include_decisions=not _truthy(query_params.get("no_decisions")),
            )
            self._send_json(HTTPStatus.OK, policy_matrix_payload(report))
            return

        if _is_ledger_unit_review_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            query_params = _flatten_query(parse_qs(parsed.query))
            owner_unit = query_params.get("owner_unit") or query_params.get("unit") or "M1-01A"
            minimum = _int_or_default(query_params.get("minimum_effective_lines"), 10000)
            report = build_unit_review_report(
                PROJECT_ROOT,
                ledger,
                owner_unit=owner_unit,
                base_commit=query_params.get("base") or "",
                cached=_truthy(query_params.get("cached")),
                minimum_effective_lines=minimum,
                include_reports=not _truthy(query_params.get("no_reports")),
                boundary_roots=_split_csv(query_params.get("boundary_roots") or query_params.get("roots") or "") or None,
            )
            self._send_json(HTTPStatus.OK, unit_review_payload(report))
            return

        if _is_ledger_snapshots_path(parts):
            self._send_json(HTTPStatus.OK, {"snapshots": list_snapshots(PROJECT_ROOT)})
            return

        ledger_id = _ledger_entry_id_from_path(parts)
        if ledger_id:
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            entry = ledger.get(ledger_id)
            if entry is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "ledger_entry_not_found", "ledger_id": ledger_id})
                return
            self._send_json(HTTPStatus.OK, {"entry": entry.to_dict()})
            return

        if parts == ["workers", "code", "inventory"]:
            contracts = build_productized_claude_runtime_contracts(project_root=PROJECT_ROOT)
            integration = build_claude_productization_integration_report(
                project_root=PROJECT_ROOT,
                runtime_contracts=contracts,
            )
            inventory_tools = default_tool_registry().list()
            inventory_runtime_context = assemble_claude_runtime_context(
                request=WorkerRequest(
                    run_id="inventory",
                    task_id="code-worker-runtime-inventory",
                    worker_name="CodeWorkerRuntime",
                    constraints={"permission_mode": "workspace"},
                    metadata={"source": "workers/code/inventory"},
                ),
                integration_report=integration,
                runtime_contracts=contracts,
                project_root=PROJECT_ROOT,
                workspace_root=tool_workspace_path(),
                artifact_root=artifact_root_path(),
                tool_names=tuple(tool.name for tool in inventory_tools),
                read_only_tool_names=tuple(
                    tool.name for tool in inventory_tools if tool.metadata.get("read_only") == "true"
                ),
                mutating_tool_names=tuple(
                    tool.name for tool in inventory_tools if tool.metadata.get("read_only") != "true"
                ),
                permission_mode="workspace",
            )
            source_graph_audit = build_claude_source_graph_audit(
                project_root=PROJECT_ROOT,
                integration_report=integration,
                runtime_contracts=contracts,
                runtime_context_report=inventory_runtime_context,
            )
            payload = dict(contracts.inventory)
            payload["health"] = contracts.health
            payload["defaultPath"] = contracts.default_path
            payload["sessionFoundation"] = contracts.session_contract.get("preQueryFoundation", {})
            payload["sourceToTarget"] = [item.to_dict() for item in contracts.source_to_target]
            payload["sourceGraph"] = integration.crosswalk.source_to_target_payload()
            payload["runtimeContext"] = integration.crosswalk.runtime_context_payload()
            payload["eventContracts"] = integration.crosswalk.event_contract_payload()
            payload["downstreamContracts"] = integration.crosswalk.downstream_payload()
            payload["integration"] = integration.to_dict()
            payload["sourceGraphAudit"] = source_graph_audit.to_dict()
            payload["stateCustodyRuntime"] = source_graph_audit.state_custody_runtime_report.to_dict()
            payload["sessionLineage"] = SessionLineageRuntime().build_report(
                project_root=PROJECT_ROOT,
                contracts=contracts,
            ).to_dict()
            api_inventory_contract = build_api_inventory_contract_report(
                project_root=PROJECT_ROOT,
                contracts=contracts,
                integration_report=integration,
                runtime_context_report=inventory_runtime_context,
                payload=payload,
            )
            payload["apiInventoryContract"] = api_inventory_contract.to_dict()
            self._send_json(HTTPStatus.OK, payload)
            return

        if parts == ["workers", "code", "compact-state"]:
            query = parse_qs(parsed.query)
            task_id = str(query.get("task_id", [""])[0]).strip()
            if not task_id:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "task_id_required", "message": "task_id is required for compact-state projection."},
                )
                return
            with _task_lock(task_id):
                state = store.load_task(task_id)
                if state is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                    return
                task_events = store.task_events(task_id)
                projection = CodeWorkerTaskApiProjectionRuntime().build_session_projection(
                    task_id=task_id,
                    state=to_jsonable(state),
                    events=task_events,
                )
                payload = {
                    "task_id": task_id,
                    "run_id": state.run_id,
                    "session": projection.session.to_dict(),
                    "compact_state": projection.compact_state.to_dict(),
                    "restore_state": projection.restore_state.to_dict(),
                    "model_api": projection.model_api.to_dict(),
                    "phase_counts": projection.phase_counts,
                    "projection": projection.to_dict(),
                }
                route_contract = CodeWorkerTaskApiContractRuntime().build_report(
                    route_kind=TaskApiRouteKind.COMPACT_STATE,
                    task_id=task_id,
                    payload=payload,
                    events=task_events,
                    projection=projection,
                )
                payload["route_contract"] = route_contract.to_dict()
                self._send_json(HTTPStatus.OK if route_contract.ok else HTTPStatus.CONFLICT, payload)
            return

        if parts == ["workers", "code", "session-foundation"]:
            contracts = build_productized_claude_runtime_contracts(project_root=PROJECT_ROOT)
            tools = default_tool_registry().list()
            query = parse_qs(parsed.query)
            request = WorkerRequest(
                run_id="inventory",
                task_id="code-worker-session-foundation",
                worker_name="CodeWorkerRuntime",
                constraints={
                    "raw_input": "Inspect CodeWorker session foundation.",
                    "query_turns": [[{"tool_name": "trace", "arguments": {"limit": 1}}]],
                    "permission_mode": "workspace",
                },
                metadata={"source": "workers/code/session-foundation"},
            )
            api_session_id = str(query.get("session_id", [""])[0] or f"codesession_inventory_{request.request_id}")
            input_report = QueryInputProcessor().process_worker_request(request)
            context_snapshot = ContextAssemblyRuntime().assemble(
                request=request,
                session_id=api_session_id,
                input_records=input_report.records,
                tool_specs=tools,
                project_root=PROJECT_ROOT,
                workspace_root=tool_workspace_path(),
                artifact_root=artifact_root_path(),
                runtime_contracts=contracts,
                permission_mode="workspace",
            )
            session_store = CodeWorkerSessionStore(artifact_root_path() / "code-worker-session-foundation-api")
            session_foundation = CodeWorkerSessionFoundationRuntime(store=session_store)
            session_seed = session_foundation.build_seed(
                request=request,
                input_report=input_report,
                context_snapshot=context_snapshot,
                session_id=api_session_id,
            )
            session_seed_events = session_foundation.seed_events(session_seed)
            replay_constraints = {
                key: values[0]
                for key, values in query.items()
                if key in {"resume_session_id", "resume_code_worker_session_id", "resume_worker_request_id", "resume_after_sequence", "resume_limit"}
                and values
            }
            replay_plan = CodeWorkerSessionReplayRuntime(session_store).build_plan_from_constraints(replay_constraints)
            replay_events = []
            if replay_plan is not None:
                replay_events.append(
                    CodeWorkerSessionReplayRuntime(session_store).event_for_plan(
                        replay_plan,
                        run_id=request.run_id,
                        task_id=request.task_id,
                        node_id=request.node_id,
                    )
                )
            turn_lifecycle_runtime = TurnLifecycleRuntime()
            turn_lifecycle = turn_lifecycle_runtime.project(
                session_id=session_seed.session_id,
                worker_request_id=request.request_id,
                input_records=input_report.records,
                query_turns=request.constraints.get("query_turns", []),
                context_snapshot=context_snapshot,
                replay_plan=replay_plan,
            )
            turn_lifecycle_event = turn_lifecycle_runtime.event_for_projection(
                turn_lifecycle,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            foundation_audit = SessionFoundationAuditor().audit_seed(
                session_seed,
                store=session_store,
                events=[*session_seed_events, *replay_events, turn_lifecycle_event],
            )
            foundation_audit_record = foundation_audit_event(foundation_audit)
            acceptance_runtime = SessionAcceptanceRuntime()
            acceptance_report = acceptance_runtime.evaluate(
                seed=session_seed,
                foundation_audit=foundation_audit,
                turn_lifecycle=turn_lifecycle,
                replay_plan=replay_plan,
                transcript_mapping=None,
                require_transcript=False,
            )
            acceptance_event = acceptance_runtime.event_for_report(
                acceptance_report,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            lifecycle_report = SessionLifecycleRuntime().build_report(
                [*session_seed_events, *replay_events, turn_lifecycle_event, foundation_audit_record, acceptance_event],
                session_id=session_seed.session_id,
                worker_request_id=request.request_id,
                require_query_engine=False,
                require_transcript=False,
            )
            lineage_report = SessionLineageRuntime().build_report(project_root=PROJECT_ROOT, contracts=contracts)
            projection = SessionApiProjectionBuilder().build(
                contracts=contracts,
                request=request,
                input_report=input_report,
                context_snapshot=context_snapshot,
                replay_plan=replay_plan,
                turn_lifecycle=turn_lifecycle,
                acceptance_report=acceptance_report,
                lifecycle_report=lifecycle_report,
                lineage_report=lineage_report,
            )
            self._send_json(HTTPStatus.OK, projection.to_dict())
            return

        if parts == ["workers", "code", "session-integration"]:
            contracts = build_productized_claude_runtime_contracts(project_root=PROJECT_ROOT)
            tools = default_tool_registry().list()
            query = parse_qs(parsed.query)
            api_session_id = str(query.get("session_id", [""])[0] or "")
            query_turns = [[{"tool_name": "trace", "arguments": {"limit": 1}}]]
            constraints = {
                "raw_input": str(query.get("q", ["Inspect CodeWorker query session integration."])[0]),
                "query_turns": query_turns,
                "permission_mode": str(query.get("permission_mode", ["workspace"])[0] or "workspace"),
            }
            if api_session_id:
                constraints["session_id"] = api_session_id
            for key in (
                "resume_session_id",
                "resume_code_worker_session_id",
                "resume_worker_request_id",
                "resume_after_sequence",
                "resume_limit",
                "cancel_session",
                "cancel_reason",
                "interrupt_session",
                "interrupt_reason",
                "checkpoint_session",
                "checkpoint_query_entry",
                "expected_context_fingerprint",
                "disable_query_entry_packet",
                "disable_query_entry_store",
                "disable_query_session_checkpoint",
            ):
                if query.get(key):
                    constraints[key] = query[key][0]
            request = WorkerRequest(
                run_id="inventory",
                task_id="code-worker-session-integration",
                worker_name="CodeWorkerRuntime",
                constraints=constraints,
                metadata={"source": "workers/code/session-integration"},
            )
            api_session_id = str(constraints.get("session_id") or f"codesession_integration_{request.request_id}")
            input_report = QueryInputProcessor().process_worker_request(request)
            context_snapshot = ContextAssemblyRuntime().assemble(
                request=request,
                session_id=api_session_id,
                input_records=input_report.records,
                tool_specs=tools,
                project_root=PROJECT_ROOT,
                workspace_root=tool_workspace_path(),
                artifact_root=artifact_root_path(),
                runtime_contracts=contracts,
                permission_mode=str(constraints.get("permission_mode") or "workspace"),
            )
            session_store = CodeWorkerSessionStore(artifact_root_path() / "code-worker-session-integration-api")
            session_foundation = CodeWorkerSessionFoundationRuntime(store=session_store)
            session_seed = session_foundation.build_seed(
                request=request,
                input_report=input_report,
                context_snapshot=context_snapshot,
                session_id=api_session_id,
            )
            session_seed_events = session_foundation.seed_events(session_seed)
            replay_plan = CodeWorkerSessionReplayRuntime(session_store).build_plan_from_constraints(constraints)
            replay_events = []
            if replay_plan is not None:
                replay_events.append(
                    CodeWorkerSessionReplayRuntime(session_store).event_for_plan(
                        replay_plan,
                        run_id=request.run_id,
                        task_id=request.task_id,
                        node_id=request.node_id,
                    )
                )
            turn_lifecycle_runtime = TurnLifecycleRuntime()
            turn_lifecycle = turn_lifecycle_runtime.project(
                session_id=session_seed.session_id,
                worker_request_id=request.request_id,
                input_records=input_report.records,
                query_turns=query_turns,
                context_snapshot=context_snapshot,
                replay_plan=replay_plan,
            )
            turn_lifecycle_event = turn_lifecycle_runtime.event_for_projection(
                turn_lifecycle,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            foundation_audit = SessionFoundationAuditor().audit_seed(
                session_seed,
                store=session_store,
                events=[*session_seed_events, *replay_events, turn_lifecycle_event],
            )
            foundation_audit_record = foundation_audit_event(foundation_audit)
            acceptance_runtime = SessionAcceptanceRuntime()
            acceptance_report = acceptance_runtime.evaluate(
                seed=session_seed,
                foundation_audit=foundation_audit,
                turn_lifecycle=turn_lifecycle,
                replay_plan=replay_plan,
                transcript_mapping=None,
                require_transcript=False,
            )
            acceptance_event = acceptance_runtime.event_for_report(
                acceptance_report,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            lifecycle_report = SessionLifecycleRuntime().build_report(
                [*session_seed_events, *replay_events, turn_lifecycle_event, foundation_audit_record, acceptance_event],
                session_id=session_seed.session_id,
                worker_request_id=request.request_id,
                require_query_engine=False,
                require_transcript=False,
            )
            integration_runtime = QuerySessionIntegrationRuntime(
                store=session_store,
                artifact_store=LocalArtifactStore(artifact_root_path()),
            )
            integration_report = integration_runtime.prepare(
                request=request,
                seed=session_seed,
                input_report=input_report,
                context_snapshot=context_snapshot,
                tool_specs=tools,
                query_turns=query_turns,
                turn_lifecycle=turn_lifecycle,
                foundation_audit=foundation_audit,
                acceptance_report=acceptance_report,
                lifecycle_report=lifecycle_report,
                replay_plan=replay_plan,
            )
            integration_events = integration_runtime.events_for_report(
                integration_report,
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": integration_report.ok,
                    "report": integration_report.to_dict(include_text=False),
                    "packet": integration_report.packet.to_dict(include_text=False),
                    "events": [to_jsonable(event) for event in integration_events],
                    "messagePreview": [message.to_model_message() for message in integration_report.packet.messages[:4]],
                },
            )
            return

        if parts == ["workers", "browser", "actions"]:
            self._send_json(HTTPStatus.OK, default_browser_action_registry(PROJECT_ROOT).describe())
            return

        if parts == ["workers", "browser", "health"]:
            runtime, worker = get_browser_runtime_services()
            compatibility = browser_use_health_summary(inspect_browser_use_runtime(PROJECT_ROOT))
            diagnostics = _BROWSER_RUNTIME_REGISTRY.diagnostics(
                browser_runtime_config(),
                action_runtime=worker.browser_session_application,
                lease_store=worker.browser_session_application.receipt_store,
            )
            projection = _BROWSER_RUNTIME_REGISTRY.projection(
                browser_runtime_config(),
                lease_store=worker.browser_session_application.receipt_store,
            )
            diagnostic_payload = _browser_projection_payload(diagnostics)
            integration_diagnostics = diagnostic_payload.get("integration_audit")
            integration_ok = bool(
                isinstance(integration_diagnostics, dict)
                and integration_diagnostics.get(
                    "ok",
                    integration_diagnostics.get("ready", False),
                )
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    **compatibility,
                    "ok": bool(diagnostic_payload.get("ok"))
                    and integration_ok
                    and not bool(diagnostic_payload.get("blocking_findings")),
                    "runtime": runtime.snapshot(),
                    "diagnostics": diagnostic_payload,
                    "projection": _browser_projection_payload(projection),
                    "compatibility": compatibility,
                    "state_owner": "JsonBrowserStateStore",
                    "canonical_task_owner": "SQLiteStore",
                    "permission_owner": "PermissionStateStore",
                    "source_repository_dependency": False,
                },
            )
            return

        if parts == ["workers", "browser", "sessions"]:
            runtime, worker = get_browser_runtime_services()
            projection = _BROWSER_RUNTIME_REGISTRY.projection(
                browser_runtime_config(),
                lease_store=worker.browser_session_application.receipt_store,
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "sessions": [to_jsonable(item) for item in runtime.list_sessions()],
                    "registry": _BROWSER_RUNTIME_REGISTRY.snapshot(),
                    "projection": _browser_projection_payload(projection),
                    "state_owner": "JsonBrowserStateStore",
                },
            )
            return

        if len(parts) == 4 and parts[:3] == ["workers", "browser", "sessions"]:
            runtime, worker = get_browser_runtime_services()
            try:
                session = runtime.get_session(parts[3])
                diagnostic = runtime.diagnose(parts[3])
                projection = _BROWSER_RUNTIME_REGISTRY.projection(
                    browser_runtime_config(),
                    session_id=parts[3],
                    lease_store=worker.browser_session_application.receipt_store,
                )
            except Exception as error:  # noqa: BLE001 - typed browser lookup boundary.
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": type(error).__name__, "message": str(error)},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "session": to_jsonable(session),
                    "diagnostic": to_jsonable(diagnostic),
                    "projection": _browser_projection_payload(projection),
                    "state_owner": "JsonBrowserStateStore",
                },
            )
            return

        if parts == ["artifacts"]:
            query = parse_qs(parsed.query)
            task_id = _optional_query_value(query, "task_id")
            artifact_refs = _artifact_refs_from_store(store, task_id=task_id)
            if artifact_refs is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found", "task_id": task_id})
                return
            catalog = LocalArtifactStore(artifact_root_path())
            self._send_json(
                HTTPStatus.OK,
                {
                    "artifact_root": str(catalog.root),
                    "task_id": task_id,
                    "artifacts": [_artifact_entry(catalog, artifact) for artifact in artifact_refs],
                },
            )
            return

        if len(parts) == 2 and parts[0] == "artifacts":
            artifact = _find_artifact_ref(store, parts[1])
            if artifact is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "artifact_not_found", "artifact_id": parts[1]})
                return
            catalog = LocalArtifactStore(artifact_root_path())
            try:
                preview = catalog.read_preview(artifact)
            except ValueError as error:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "artifact_outside_store", "message": str(error), "artifact": to_jsonable(artifact)},
                )
                return
            self._send_json(HTTPStatus.OK, {"artifact": preview})
            return

        if parts == ["tasks"]:
            self._send_json(HTTPStatus.OK, {"tasks": store.list_tasks()})
            return

        if len(parts) == 2 and parts[0] == "tasks":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            self._send_json(HTTPStatus.OK, {"task": to_jsonable(state)})
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "events":
            if store.load_task(parts[1]) is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            self._send_json(HTTPStatus.OK, {"events": store.task_events(parts[1])})
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "browser-context":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            session_id = str(state.metadata.get("query_session_id") or f"task:{state.task_id}")
            scope = BrowserContextScope(
                run_id=state.run_id,
                task_id=state.task_id,
                session_id=session_id,
            )
            try:
                checkpoint = _BROWSER_CONTEXT_TASK_INTEGRATION.checkpoint_from_metadata(
                    state.metadata,
                    scope=scope,
                )
            except Exception as error:  # noqa: BLE001 - projection route returns typed corruption.
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": getattr(error, "code", "browser_context_checkpoint_invalid"),
                        "message": str(error),
                        "details": to_jsonable(getattr(error, "details", {})),
                    },
                )
                return
            projection = BrowserContextApiProjectionRuntime().build(
                checkpoint,
                events=store.task_events(parts[1]),
                task_artifact_ids=tuple(item.artifact_id for item in state.artifacts),
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "task_id": state.task_id,
                    "run_id": state.run_id,
                    "browser_context": _BROWSER_CONTEXT_TASK_INTEGRATION.public_projection(checkpoint),
                    "projection": projection.to_dict(),
                    "memory_candidates": list(checkpoint.memory_candidates),
                    "history_messages": list(checkpoint.history_messages[-64:]),
                },
                headers={"Cache-Control": "no-store, max-age=0"},
            )
            return

        if len(parts) == 5 and parts[0] == "tasks" and parts[2] == "workers" and parts[3] == "code":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            task_events = store.task_events(parts[1])
            projection = CodeWorkerTaskApiProjectionRuntime().build_session_projection(
                task_id=parts[1],
                state=to_jsonable(state),
                events=task_events,
            )
            contract_runtime = CodeWorkerTaskApiContractRuntime()
            if parts[4] == "session":
                payload = projection.to_dict()
                route_contract = contract_runtime.build_report(
                    route_kind=TaskApiRouteKind.SESSION,
                    task_id=parts[1],
                    payload=payload,
                    events=task_events,
                    projection=projection,
                )
                payload["route_contract"] = route_contract.to_dict()
                self._send_json(HTTPStatus.OK if route_contract.ok else HTTPStatus.CONFLICT, payload)
                return
            if parts[4] == "tool-trace":
                payload = projection.tool_trace.to_dict()
                route_contract = contract_runtime.build_report(
                    route_kind=TaskApiRouteKind.TOOL_TRACE,
                    task_id=parts[1],
                    payload=payload,
                    events=task_events,
                    projection=projection,
                )
                payload["route_contract"] = route_contract.to_dict()
                self._send_json(HTTPStatus.OK if route_contract.ok else HTTPStatus.CONFLICT, payload)
                return
            if parts[4] == "compact-state":
                payload = {
                    "task_id": parts[1],
                    "run_id": state.run_id,
                    "session": projection.session.to_dict(),
                    "compact_state": projection.compact_state.to_dict(),
                    "restore_state": projection.restore_state.to_dict(),
                    "model_api": projection.model_api.to_dict(),
                    "phase_counts": projection.phase_counts,
                    "projection": projection.to_dict(),
                }
                route_contract = contract_runtime.build_report(
                    route_kind=TaskApiRouteKind.COMPACT_STATE,
                    task_id=parts[1],
                    payload=payload,
                    events=task_events,
                    projection=projection,
                )
                payload["route_contract"] = route_contract.to_dict()
                self._send_json(HTTPStatus.OK if route_contract.ok else HTTPStatus.CONFLICT, payload)
                return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "memory":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            query = parse_qs(parsed.query)
            memory = _memory_fabric(store).memory_view(
                state,
                store.task_events(parts[1]),
                query=_optional_query_value(query, "q") or "",
                limit=_positive_int(query.get("limit", ["12"])[0], default=12),
            )
            self._send_json(HTTPStatus.OK, memory["data"])
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "trajectory":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            frames = _memory_fabric(store).replay_trajectory(state, store.task_events(parts[1]))
            self._send_json(
                HTTPStatus.OK,
                {
                    "task_id": parts[1],
                    "run_id": state.run_id,
                    "frame_count": len(frames),
                    "frames": [to_jsonable(frame) for frame in frames],
                },
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "compactions":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "task_id": parts[1],
                    "run_id": state.run_id,
                    "compactions": store.task_compactions(parts[1]),
                },
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "scheduler":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            self._send_json(HTTPStatus.OK, _scheduler_task_view(state, store))
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "recovery":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            self._send_json(HTTPStatus.OK, _recovery_task_view(state, store))
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "artifacts":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            catalog = LocalArtifactStore(artifact_root_path())
            self._send_json(
                HTTPStatus.OK,
                {
                    "artifact_root": str(catalog.root),
                    "task_id": parts[1],
                    "artifacts": [_artifact_entry(catalog, artifact) for artifact in state.artifacts],
                },
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "subagents":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            runtime = get_subagent_runtime()
            tasks = runtime.task_store.list(parent_task_id=state.task_id)
            self._send_json(
                HTTPStatus.OK,
                {
                    "schema": "zyra.subagent-api/v1",
                    "task_id": state.task_id,
                    "subagents": [item.safe_dict() for item in tasks],
                    "active_count": sum(1 for item in tasks if not item.status.terminal),
                    "physical_worker_state_owned": False,
                },
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": parsed.path})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = _path_parts(parsed.path)
        store = get_store()
        try:
            payload = self._read_json_body()
        except JsonRequestError as error:
            self._send_json(error.status, {"error": error.code, "message": error.message})
            return

        if self._handle_permission_post(parts=parts, payload=payload, store=store):
            return

        mcp_runtime = get_mcp_runtime()
        mcp_response = McpApiFacade(
            mcp_runtime,
            mutation_authorizer=lambda request: self._authorize_mcp_mutation(
                request,
                payload=payload,
                store=store,
            ),
        ).handle_post(
            parts,
            payload,
            self._permission_actor_id(),
        )
        if mcp_response is not None:
            status, body, headers = mcp_response
            run_id = str(payload.get("run_id") or "")
            task_id = str(payload.get("task_id") or "")
            mcp_events = (
                list(mcp_runtime.drain_events(run_id=run_id, task_id=task_id))
                if run_id and task_id
                else []
            )
            if mcp_events:
                persist_events(store, mcp_events)
                body = {**body, "events": [to_jsonable(event) for event in mcp_events]}
            self._send_json(status, body, headers=headers)
            return

        try:
            workspace_response = WorkspaceApiService(get_workspace_manager()).route_post(
                tuple(parts),
                payload,
            )
        except WorkspaceError as error:
            workspace_response = workspace_error_response(error)
        if workspace_response is not None:
            workspace_id = parts[1] if len(parts) > 1 and parts[0] == "workspaces" else ""
            events = persist_workspace_events(store, workspace_id=workspace_id)
            body = dict(workspace_response.body)
            if events:
                body["events"] = [to_jsonable(event) for event in events]
            self._send_json(
                workspace_response.status,
                body,
                headers=dict(workspace_response.headers),
            )
            return

        if parts == ["tasks"]:
            user_goal = str(payload.get("goal") or "Unspecified long-horizon task")
            auto_run = payload.get("auto_run", True) is not False
            state, created_event = make_task_created_event(user_goal)
            session_id = str(payload.get("session_id") or f"task:{state.task_id}")
            state.metadata["query_session_id"] = session_id
            try:
                workspace_result = get_workspace_manager().create_for_task(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    session_id=session_id,
                    worker_id="task-runtime",
                    idempotency_key=str(
                        self.headers.get("Idempotency-Key")
                        or payload.get("idempotency_key")
                        or f"task-create:{state.task_id}"
                    ),
                    causation_id=created_event.event_id,
                )
            except WorkspaceError as error:
                drain_workspace_events(state.task_id)
                response = workspace_error_response(error)
                self._send_json(response.status, response.body, headers=dict(response.headers))
                return
            state.metadata["workspace_ref"] = workspace_result.projection.to_dict()
            events = [
                created_event,
                *ensure_default_graph(state),
                *drain_workspace_events(state.task_id),
            ]
            if auto_run:
                events.extend(run_task_graph(state, execution_context=graph_execution_context()))
            persist_events(store, events)
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "task": to_jsonable(state),
                    "events": [to_jsonable(event) for event in events],
                },
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "run":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            events = run_task_graph(state, execution_context=graph_execution_context())
            persist_events(store, events)
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.OK,
                {"task": to_jsonable(state), "events": [to_jsonable(event) for event in events]},
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            reason = str(payload.get("reason") or "Cancelled by control API.")
            events = cancel_task_graph(state, reason=reason)
            cancelled_subagents = get_subagent_runtime().cancel_for_parent(state.task_id, reason=reason)
            persist_events(store, events)
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.OK,
                {
                    "task": to_jsonable(state),
                    "events": [to_jsonable(event) for event in events],
                    "cancelled_subagents": [item.safe_dict() for item in cancelled_subagents],
                },
            )
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "subagents" and parts[3] == "fanout":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            raw_items = payload.get("items")
            if not isinstance(raw_items, list) or len(raw_items) < 2:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "fanout_requires_two_or_more_items"})
                return
            session_id = str(payload.get("session_id") or state.metadata.get("query_session_id") or f"task:{state.task_id}")
            parent_scope = _issue_parent_subagent_scope(state, session_id=session_id)
            context_payload = {
                "messages": [],
                "artifact_refs": [to_jsonable(item) for item in state.artifacts],
                "evidence_refs": list(payload.get("evidence_refs") or ()),
                "invoked_skill_refs": list(state.metadata.get("skill_invocation_refs") or ()),
                "context_epoch": int(state.metadata.get("context_epoch") or 0),
                "compact_boundary_id": str(state.metadata.get("compact_boundary_id") or ""),
                "rendered_system_prompt": str(state.metadata.get("rendered_system_prompt") or ""),
                "parent_permission_rule_ids": list(parent_scope.permission_rule_ids),
                "parent_permission_denials": list(parent_scope.deny_rule_ids),
            }
            schema = payload.get("yield_schema") if isinstance(payload.get("yield_schema"), dict) else {
                "type": "object",
            }
            request = FanoutRequest(
                run_id=state.run_id,
                parent_task_id=state.task_id,
                parent_session_id=session_id,
                shared_context=str(payload.get("shared_context") or ""),
                items=tuple(FanoutItem.from_dict(item) for item in raw_items if isinstance(item, dict)),
                idempotency_key=str(payload.get("idempotency_key") or new_id("fanoutrequest")),
                execution_mode=AgentExecutionMode(str(payload.get("execution_mode") or "foreground")),
                failure_policy=FanoutFailurePolicy(str(payload.get("failure_policy") or "require_all")),
                maximum_concurrency=max(1, int(payload.get("maximum_concurrency") or 4)),
                promotion_after_ms=max(0, int(payload.get("promotion_after_ms") or 0)),
                yield_contract=YieldContract(
                    contract_id=str(payload.get("yield_contract_id") or new_id("yieldcontract")),
                    schema=schema,
                ),
                parent_scope_snapshot_id=parent_scope.snapshot_id,
                metadata={"origin": "api", "node_id": state.root_node_id},
            )
            try:
                result = _fanout_runtime_for(
                    state,
                    parent_scope=parent_scope,
                    context_payload=context_payload,
                ).start(request, causation_id=str(payload.get("causation_id") or "api-fanout"))
            except (ValueError, RuntimeError) as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "subagent_fanout_rejected", "message": str(error), "type": type(error).__name__},
                )
                return
            self._send_json(HTTPStatus.ACCEPTED if result.detached else HTTPStatus.CREATED, result.to_dict())
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "subagents":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            execution_mode = AgentExecutionMode(str(payload.get("execution_mode") or "background"))
            context_mode = AgentContextMode(str(payload.get("context_mode") or "isolated"))
            session_id = str(payload.get("session_id") or state.metadata.get("query_session_id") or f"task:{state.task_id}")
            parent_scope = _issue_parent_subagent_scope(state, session_id=session_id)
            request = SubagentSpawnRequest(
                run_id=state.run_id,
                parent_task_id=state.task_id,
                parent_session_id=session_id,
                parent_worker_request_id=str(payload.get("parent_worker_request_id") or "api-control"),
                agent_type=str(payload.get("agent_type") or "general-purpose"),
                prompt=str(payload.get("prompt") or ""),
                parent_tools=parent_scope.tool_names,
                parent_permission_mode=parent_scope.permission_mode,
                requested_tools=tuple(payload.get("requested_tools") or ()),
                requested_permission_mode=(
                    PermissionMode(str(payload["requested_permission_mode"]))
                    if payload.get("requested_permission_mode")
                    else None
                ),
                available_mcp_servers=parent_scope.mcp_servers,
                requested_mcp_servers=tuple(payload.get("requested_mcp_servers") or ()),
                context_mode=context_mode,
                execution_mode=execution_mode,
                workspace_root=str(tool_workspace_path()),
                requested_cwd=str(payload.get("requested_cwd") or ""),
                context_payload={
                    "messages": list(payload.get("messages") or ()),
                    "artifact_refs": [to_jsonable(item) for item in state.artifacts],
                    "evidence_refs": list(payload.get("evidence_refs") or ()),
                    "invoked_skill_refs": list(state.metadata.get("skill_invocation_refs") or ()),
                    "context_epoch": int(state.metadata.get("context_epoch") or 0),
                    "compact_boundary_id": str(state.metadata.get("compact_boundary_id") or ""),
                    "rendered_system_prompt": str(state.metadata.get("rendered_system_prompt") or ""),
                    "parent_permission_rule_ids": list(parent_scope.permission_rule_ids),
                    "parent_permission_denials": list(parent_scope.deny_rule_ids),
                },
                constraints=dict(payload.get("constraints") or {}),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                task_id=str(payload.get("subagent_task_id") or new_id("subagenttask")),
                parent_scope_snapshot_id=parent_scope.snapshot_id,
                metadata={
                    "root_task_id": state.task_id,
                    "node_id": state.root_node_id,
                    "origin": "api",
                    "parent_scope_snapshot_id": parent_scope.snapshot_id,
                    "expected_parent_session_revision": parent_scope.session_revision,
                    "expected_parent_permission_revision": parent_scope.permission_revision,
                    "expected_parent_tool_generation": parent_scope.tool_generation,
                    "expected_parent_mcp_generations": dict(parent_scope.mcp_catalog_generations),
                },
            )
            try:
                result = get_subagent_runtime().spawn(request)
            except (ValueError, RuntimeError) as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "subagent_spawn_rejected", "message": str(error), "type": type(error).__name__},
                )
                return
            self._send_json(HTTPStatus.ACCEPTED if result.background else HTTPStatus.CREATED, result.to_dict())
            return

        if len(parts) == 5 and parts[0] == "tasks" and parts[2] == "subagents":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            runtime = get_subagent_runtime()
            try:
                record = runtime.task_store.get(parts[3])
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "subagent_not_found"})
                return
            if record.parent_task_id != state.task_id:
                self._send_json(HTTPStatus.CONFLICT, {"error": "subagent_parent_mismatch"})
                return
            if parts[4] == "cancel":
                action = SubagentControlAction.CANCEL
            elif parts[4] == "background":
                action = SubagentControlAction.BACKGROUND
            elif parts[4] == "message":
                action = SubagentControlAction.MESSAGE
            elif parts[4] == "status":
                action = SubagentControlAction.STATUS
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "subagent_control_not_found"})
                return
            response = runtime.control_runtime.execute(SubagentControlRequest(
                root_task_id=state.task_id,
                subagent_task_id=record.task_id,
                action=action,
                arguments=dict(payload),
                request_id=str(payload.get("request_id") or new_id("subcontrol")),
                idempotency_key=str(payload.get("idempotency_key") or ""),
                expected_task_revision=(int(payload["expected_task_revision"]) if payload.get("expected_task_revision") is not None else None),
            ))
            self._send_json(HTTPStatus.OK if response.ok else HTTPStatus.CONFLICT, response.to_dict())
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "memory" and parts[3] == "ingest":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            snapshot = _memory_fabric(store).refresh_task_memory(state, store.task_events(parts[1]), persist=True)
            self._send_json(
                HTTPStatus.OK,
                {
                    "task_id": parts[1],
                    "run_id": state.run_id,
                    "record_count": len(snapshot.records),
                    "layer_counts": snapshot.layer_counts(),
                    "source_modules": snapshot.metadata.get("source_modules", {}),
                },
            )
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "memory" and parts[3] == "compact":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            result = _memory_fabric(store).compact_context(
                state,
                store.task_events(parts[1]),
                focus=str(payload.get("focus") or payload.get("raw") or ""),
                policy=_compact_policy_from_payload(payload),
                persist=True,
            )
            _attach_artifacts(state, result.artifacts)
            _record_compaction_metadata(state, result, source_event_id=str(payload.get("source_event_id") or "api"))
            event = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.SYSTEM_NOTICE,
                node_id=state.root_node_id,
                payload={
                    "memory_compact": {
                        "compact_id": result.compact_id,
                        "focus": result.focus,
                        "artifact_ids": list(result.artifact_ids),
                        "preserved_event_count": len(result.preserved_event_ids),
                        "summarized_event_count": len(result.summarized_event_ids),
                        "memory_record_count": len(result.memory_ids),
                        "compression_ratio": result.compression_ratio,
                    }
                },
            )
            persist_events(store, [event])
            state.updated_at = event.created_at
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "task": to_jsonable(state),
                    "compact": to_jsonable(result),
                    "event": to_jsonable(event),
                    "artifacts": [_artifact_entry(LocalArtifactStore(artifact_root_path()), artifact) for artifact in result.artifacts],
                },
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "commands":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            text = str(payload.get("text") or "")
            control_request = _control_command_request_from_text(state, text, payload)
            if control_request is None:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "unknown_or_invalid_command", "text": text},
                )
                return
            response = get_control_dispatcher().submit(
                control_request,
                _control_context_for_task(state, store),
            )
            store.save_checkpoint(state)
            status = HTTPStatus.CREATED if response.ok else HTTPStatus.ACCEPTED if response.status.value == "queued" else HTTPStatus.CONFLICT
            descriptor = get_control_command_registry().require(control_request.canonical_name)
            compatibility_result = {
                **response.to_dict(),
                "name": control_request.canonical_name,
                "summary": response.summary,
                "data": response.result.data,
                "runtime_status": (
                    str(response.result.metadata.get("runtime_status") or "stateful")
                    if descriptor.mutation_scope.value != "read_only"
                    else "read_only"
                ),
            }
            control_event = response.result.data.get("control_event") if isinstance(response.result.data, dict) else None
            self._send_json(
                status,
                {
                    "task": to_jsonable(state),
                    "control_request": control_request.to_dict(),
                    "command": descriptor.to_dict(),
                    "command_result": compatibility_result,
                    "event": control_event,
                    "event_only_stateful_fallback": False,
                },
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "control-frames":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            raw_envelope = payload.get("envelope") if isinstance(payload.get("envelope"), dict) else payload
            try:
                if raw_envelope.get("protocol_version") == 1 or raw_envelope.get("version") == 1:
                    response_envelope = get_structured_control_hub().handle(raw_envelope)
                    structured_state = get_structured_control_hub().store.snapshot()
                    protocol_schema = "zyra.structured-control-stream/v1"
                else:
                    # Compatibility path for the 03D-01 envelope.  New callers
                    # use the process-live hub above so cancel and sequence
                    # ownership survive individual HTTP requests and restarts.
                    envelope = StructuredEnvelope.from_dict(raw_envelope)
                    structured = StructuredControlIO(
                        control_state_path() / "structured" / f"{state.task_id}.json",
                        dispatcher=lambda request: get_control_dispatcher().submit(
                            request,
                            _control_context_for_task(state, store),
                        ),
                        cancel_callback=lambda request_id: bool(
                            get_control_dispatcher().cancel(
                                request_id,
                                reason="structured_control_cancelled",
                                context=_control_context_for_task(state, store),
                            )
                        ),
                    )
                    legacy_response = structured.handle(envelope)
                    response_envelope = legacy_response.to_dict() if legacy_response else None
                    structured_state = structured.snapshot()
                    protocol_schema = "zyra.structured-control-api/v1"
            except (KeyError, ValueError, RuntimeError) as error:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "structured_control_invalid", "message": str(error), "type": type(error).__name__},
                )
                return
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "schema": protocol_schema,
                    "envelope": response_envelope,
                    "state": structured_state,
                    "event_only_stateful_fallback": False,
                },
            )
            return

        if _is_ledger_audit_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            strict = _truthy(payload.get("strict"), default=True)
            write_event = _truthy(payload.get("write_event"), default=True)
            report = InternalizationLedgerAuditor(PROJECT_ROOT, strict=strict).audit(ledger)
            event_payload: dict[str, Any] | None = None
            if write_event:
                event = event_record_from_audit(report, trigger="api")
                persist_events(store, [event])
                report.event_written = True
                event_payload = to_jsonable(event)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "audit": report.to_dict(),
                    "event": event_payload,
                    "ledger_path": str(project_ledger_path(PROJECT_ROOT)),
                },
            )
            return

        if _is_ledger_seed_path(parts):
            ledger = load_seed_ledger()
            save_project_ledger(PROJECT_ROOT, ledger)
            event_payload = None
            if _truthy(payload.get("write_event"), default=True):
                event = event_record_from_audit(
                    InternalizationLedgerAuditor(PROJECT_ROOT, strict=False).audit(ledger),
                    trigger="api_seed",
                )
                persist_events(store, [event])
                event_payload = to_jsonable(event)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "ledger_path": str(project_ledger_path(PROJECT_ROOT)),
                    "summary": ledger.summary().to_dict(),
                    "event": event_payload,
                },
            )
            return

        if _is_ledger_entries_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            entry_payload = payload.get("entry") if isinstance(payload.get("entry"), dict) else payload
            try:
                entry = InternalizationLedgerEntry.from_dict(entry_payload)
            except Exception as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_ledger_entry", "message": str(error)})
                return
            validation = validate_entry_for_persistence(PROJECT_ROOT, ledger, entry, strict=True)
            if not validation.ok:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "invalid_ledger_entry",
                        "validation": validation.to_dict(),
                        "ledger_path": str(project_ledger_path(PROJECT_ROOT)),
                    },
                )
                return
            mutation = ledger.upsert(entry)
            revision = AtomicLedgerStore(PROJECT_ROOT).save_atomic(
                ledger,
                actor=str(payload.get("actor") or "api"),
                run_id=str(payload.get("run_id") or "api-ledger-entry"),
                mutations=[mutation],
            )
            event_payload = None
            if _truthy(payload.get("write_event"), default=True):
                event = event_record_from_mutation(mutation, trigger="api")
                persist_events(store, [event])
                event_payload = to_jsonable(event)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "entry": entry.to_dict(),
                    "mutation": to_jsonable(mutation),
                    "revision": revision.to_dict(),
                    "event": event_payload,
                    "ledger_path": str(project_ledger_path(PROJECT_ROOT)),
                },
            )
            return

        advance_ledger_id = _ledger_advance_id_from_path(parts)
        if advance_ledger_id:
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            request = LedgerAdvanceRequest.from_dict(
                {
                    **payload,
                    "ledger_id": advance_ledger_id,
                    "actor": payload.get("actor") or "api",
                    "write_event": False,
                }
            )
            workflow = LedgerWorkflow(PROJECT_ROOT, ledger, event_log_path=event_log_path())
            result = workflow.advance(request)
            event_payload = None
            revision = None
            if result.mutation is not None:
                revision = AtomicLedgerStore(PROJECT_ROOT).save_atomic(
                    ledger,
                    actor=str(payload.get("actor") or "api"),
                    run_id=str(payload.get("run_id") or "api-ledger-advance"),
                    mutations=[result.mutation],
                )
                if _truthy(payload.get("write_event"), default=True):
                    event = event_record_from_mutation(result.mutation, trigger="api_advance")
                    event.payload.setdefault("integration_ledger_update", {})["policy"] = result.policy.to_dict()
                    event.payload.setdefault("integration_ledger_update", {})["audit_after"] = (
                        {
                            "ok": result.audit.ok,
                            "error_count": result.audit.error_count,
                            "blocker_count": result.audit.blocker_count,
                            "warning_count": result.audit.warning_count,
                        }
                        if result.audit
                        else {}
                    )
                    persist_events(store, [event])
                    event_payload = to_jsonable(event)
            response_payload = result.to_dict()
            response_payload["event"] = event_payload
            response_payload["revision"] = revision.to_dict() if revision else None
            self._send_json(HTTPStatus.CREATED if result.ok else HTTPStatus.BAD_REQUEST, response_payload)
            return

        if _is_ledger_snapshots_path(parts):
            ledger = load_project_ledger(PROJECT_ROOT, bootstrap=True)
            snapshot = build_snapshot(
                PROJECT_ROOT,
                ledger,
                label=str(payload.get("label") or ""),
                owner_unit=str(payload.get("owner_unit") or payload.get("unit") or ""),
                base_commit=str(payload.get("base") or ""),
                include_entries=not _truthy(payload.get("no_entries"), default=False),
                metadata={"trigger": "api"},
            )
            path = save_snapshot_to_default_dir(PROJECT_ROOT, snapshot)
            self._send_json(HTTPStatus.CREATED, {"snapshot": snapshot.to_dict(), "path": str(path)})
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "skills":
            self._execute_skill_runtime_post(store, parts[1], payload)
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "skill-updates":
            self._execute_skill_update_post(store, parts[1], payload)
            return

        if (
            len(parts) == 5
            and parts[0] == "tasks"
            and parts[2] == "skills"
            and parts[4] in {"complete", "cancel"}
        ):
            self._execute_skill_lifecycle_post(
                store,
                parts[1],
                invocation_id=parts[3],
                action=parts[4],
                payload=payload,
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "tools":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            tool_name = str(payload.get("tool_name") or payload.get("tool") or "")
            arguments = payload.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            node_id = str(payload.get("node_id") or state.root_node_id)
            context = ToolExecutionContext.for_workspace(
                workspace_root=tool_workspace_path(),
                artifact_root=artifact_root_path(),
                permission_store=get_permission_store(),
                event_reader=store.task_events,
                checkpoint_reader=lambda task_id: _checkpoint_json(store, task_id),
            )
            context = get_mcp_runtime().worker_projection(
                context,
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=node_id,
                session_id=str(payload.get("session_id") or ""),
                worker_request_id=str(payload.get("worker_request_id") or ""),
            ).context
            existing_permission_session = str(
                payload.get("permission_session_id") or ""
            ).strip()
            permission_session_id = existing_permission_session or (
                f"api-tool:{state.task_id}:{new_id('permsession')}"
            )
            permission_tool_use_id = str(
                payload.get("permission_tool_use_id")
                or payload.get("tool_call_id")
                or ""
            ).strip()
            permission_worker_request_id = str(
                payload.get("permission_worker_request_id") or ""
            ).strip()
            if existing_permission_session and (
                not permission_tool_use_id or not permission_worker_request_id
            ):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "permission_retry_identity_required",
                        "message": (
                            "permission_tool_use_id and permission_worker_request_id "
                            "are required when resuming an existing permission session"
                        ),
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
            permission_tool_use_id = permission_tool_use_id or new_id("toolcall")
            permission_worker_request_id = permission_worker_request_id or new_id(
                "api-tool-request"
            )
            try:
                call, result, permission_events, custody_envelope = _execute_guarded_api_tool(
                    state=state,
                    node_id=node_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    context=context,
                    session_id=permission_session_id,
                    worker_request_id=permission_worker_request_id,
                    tool_call_id=permission_tool_use_id,
                    custody_token=extract_bearer_token(self.headers, payload),
                    external_session_exists=bool(existing_permission_session),
                )
            except Exception as error:  # noqa: BLE001 - return redacted permission error.
                response = permission_api_error_response(
                    PermissionApiOperation.SESSION_RESUME,
                    error,
                )
                self._send_permission_response(store, response)
                return
            event = tool_result_event(call, result)
            if result.artifacts:
                state.artifacts.extend(result.artifacts)
            state.budget.tool_calls += 1
            state.updated_at = event.created_at
            persist_events(store, [*permission_events, event])
            store.save_checkpoint(state)
            status = HTTPStatus.CREATED if result.ok else HTTPStatus.CONFLICT
            self._send_json(
                status,
                {
                    "task": to_jsonable(state),
                    "tool_call": to_jsonable(call),
                    "tool_result": to_jsonable(result),
                    "event": to_jsonable(event),
                    "permission_events": [to_jsonable(item) for item in permission_events],
                    "permission_session": (
                        custody_envelope.private_dict()
                        if custody_envelope.created
                        else custody_envelope.public_dict()
                    ),
                    "permission_retry_identity": {
                        "permission_session_id": permission_session_id,
                        "permission_worker_request_id": permission_worker_request_id,
                        "permission_tool_use_id": call.tool_call_id,
                        "arguments_digest": arguments_digest(call.arguments),
                        "raw_arguments_included": False,
                    },
                },
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Pragma": "no-cache",
                    "X-Zyra-Permission-State-Owner": "PermissionStateStore",
                },
            )
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "workers" and parts[3] == "code":
            self._execute_code_worker_post(store, parts[1], payload)
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "workers" and parts[3] == "browser":
            self._execute_browser_worker_post(store, parts[1], payload)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": parsed.path})

    def _execute_browser_worker_post(
        self,
        store: SQLiteStore,
        task_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Run browser state/action/context as one task-checkpoint transaction."""

        with _task_lock(task_id):
            state = store.load_task(task_id)
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            constraints = payload.get("constraints")
            constraints = dict(constraints) if isinstance(constraints, dict) else {}
            if "browser_plan" in payload and "browser_plan" not in constraints:
                constraints["browser_plan"] = payload["browser_plan"]
            session_id = str(
                constraints.get("session_id")
                or constraints.get("canonical_session_id")
                or payload.get("session_id")
                or state.metadata.get("query_session_id")
                or f"task:{state.task_id}"
            )
            constraints["session_id"] = session_id
            constraints["canonical_session_id"] = session_id
            state.metadata["query_session_id"] = session_id
            node_id = str(payload.get("node_id") or state.root_node_id)
            if node_id not in _task_node_ids(state):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_task_node", "task_id": task_id, "node_id": node_id},
                )
                return

            idempotency_key = str(
                self.headers.get("Idempotency-Key")
                or payload.get("idempotency_key")
                or ""
            ).strip()
            if len(idempotency_key) > 200:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_idempotency_key", "message": "Idempotency-Key exceeds 200 characters."},
                )
                return
            fingerprint_constraints = dict(constraints)
            for custody_key in (
                "permission_session_id",
                "permission_session_custody_id",
                "permission_session_custody_token",
                "permission_session_custody_fingerprint",
            ):
                fingerprint_constraints.pop(custody_key, None)
            request_fingerprint_payload = json.dumps(
                {
                    "task_id": task_id,
                    "node_id": node_id,
                    "session_id": session_id,
                    "constraints": fingerprint_constraints,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            request_fingerprint = "sha256:" + hashlib.sha256(request_fingerprint_payload).hexdigest()
            idempotency_records = state.metadata.setdefault("browser_worker_idempotency", {})
            if not isinstance(idempotency_records, dict):
                idempotency_records = {}
                state.metadata["browser_worker_idempotency"] = idempotency_records
            prior = idempotency_records.get(idempotency_key) if idempotency_key else None
            if isinstance(prior, dict):
                if str(prior.get("request_fingerprint") or "") != request_fingerprint:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {
                            "error": "browser_worker_idempotency_conflict",
                            "idempotency_key": idempotency_key,
                            "request_fingerprint": request_fingerprint,
                        },
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "task": to_jsonable(state),
                        "idempotent_replay": True,
                        "idempotency_key": idempotency_key,
                        "browser_context": prior.get("browser_context", {}),
                        "browser_observability": prior.get("browser_observability", {}),
                        "worker_result": prior.get("worker_result", {}),
                        "event_ids": prior.get("event_ids", []),
                        "permission_session": {
                            "created": False,
                            "custody_token": "",
                            "cacheable": False,
                            "must_not_persist": True,
                        },
                    },
                    headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
                )
                return

            pending_records = state.metadata.setdefault("browser_worker_pending_idempotency", {})
            if not isinstance(pending_records, dict):
                pending_records = {}
                state.metadata["browser_worker_pending_idempotency"] = pending_records
            prior_pending = pending_records.get(idempotency_key) if idempotency_key else None
            if isinstance(prior_pending, dict):
                if str(prior_pending.get("request_fingerprint") or "") != request_fingerprint:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {
                            "error": "browser_worker_pending_idempotency_conflict",
                            "idempotency_key": idempotency_key,
                            "request_fingerprint": request_fingerprint,
                        },
                    )
                    return
                pending_request_id = str(prior_pending.get("worker_request_id") or "")
                if not pending_request_id:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {"error": "browser_worker_pending_identity_missing", "idempotency_key": idempotency_key},
                    )
                    return
            else:
                pending_request_id = ""

            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=node_id,
                worker_name="BrowserWorker",
                constraints=constraints,
                request_id=pending_request_id or new_id("browser-worker-request"),
                metadata={"browser_context_owner": "M1-02D.ClaudeContextWindowManager"},
            )
            checkpoint = state.metadata.get("browser_context_runtime_state")
            checkpoint = checkpoint if isinstance(checkpoint, dict) else None
            try:
                _runtime, browser_worker = get_browser_runtime_services(
                    task_id=state.task_id,
                    session_id=session_id,
                    worker_id="BrowserWorker",
                )
                run_result = browser_worker.run(
                    request,
                    browser_context_checkpoint=checkpoint,
                )
            except WorkspaceError as error:
                response = workspace_error_response(error)
                self._send_json(response.status, response.body, headers=dict(response.headers))
                return
            except Exception as error:  # noqa: BLE001 - no partial checkpoint is persisted.
                persist_workspace_events(store, task_id=state.task_id)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "error": "browser_worker_failed",
                        "message": str(error),
                        "exception_type": type(error).__name__,
                    },
                )
                return

            browser_action_pending = (
                str(run_result.worker_result.metadata.get("browser_permission_pending") or "").lower()
                == "true"
            )
            if run_result.browser_context_checkpoint and not browser_action_pending:
                restored = _BROWSER_CONTEXT_TASK_INTEGRATION.checkpoint_from_metadata(
                    {"browser_context_runtime_state": run_result.browser_context_checkpoint},
                    scope=BrowserContextScope(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        session_id=session_id,
                    ),
                )
                _BROWSER_CONTEXT_TASK_INTEGRATION.persist_checkpoint(state.metadata, restored)
            state.metadata["browser_observability"] = dict(
                run_result.browser_observability_projection
            )
            _attach_artifacts(state, list(run_result.worker_result.artifacts))
            state.budget.tool_calls += sum(
                1
                for event in run_result.event_records
                if "browser_result" in event.payload or "browser_action" in event.payload
            )
            if run_result.event_records:
                state.updated_at = run_result.event_records[-1].created_at
            if idempotency_key and browser_action_pending:
                pending_records[idempotency_key] = {
                    "request_fingerprint": request_fingerprint,
                    "worker_request_id": request.request_id,
                    "browser_session_id": run_result.worker_result.metadata.get("browser_session_id", ""),
                    "permission_request_id": run_result.worker_result.metadata.get(
                        "browser_pending_permission_request_id", ""
                    ),
                    "permission_tool_use_id": run_result.worker_result.metadata.get(
                        "browser_pending_permission_tool_use_id", ""
                    ),
                    "checkpoint_id": run_result.worker_result.metadata.get(
                        "browser_pending_checkpoint_id", ""
                    ),
                    "updated_at": now_iso(),
                }
            elif idempotency_key:
                pending_records.pop(idempotency_key, None)
                idempotency_records[idempotency_key] = {
                    "request_fingerprint": request_fingerprint,
                    "worker_request_id": request.request_id,
                    "worker_result": to_jsonable(run_result.worker_result),
                    "browser_context": run_result.browser_context_projection,
                    "browser_observability": run_result.browser_observability_projection,
                    "event_ids": [event.event_id for event in run_result.event_records],
                    "created_at": now_iso(),
                }
                if len(idempotency_records) > 128:
                    oldest = sorted(
                        idempotency_records,
                        key=lambda key: str(idempotency_records[key].get("created_at") or ""),
                    )[:-128]
                    for key in oldest:
                        idempotency_records.pop(key, None)
            workspace_events = drain_workspace_events(state.task_id)
            persist_events(store, [*workspace_events, *run_result.event_records])
            run_result = replace(
                run_result,
                browser_observability_projection=(
                    browser_worker.browser_observability_application.acknowledge_events(
                        run_result.browser_observability_projection,
                        committed_event_ids=tuple(
                            event.event_id for event in run_result.event_records
                        ),
                    )
                ),
            )
            state.metadata["browser_observability"] = dict(
                run_result.browser_observability_projection
            )
            if idempotency_key and idempotency_key in idempotency_records:
                idempotency_records[idempotency_key]["browser_observability"] = dict(
                    run_result.browser_observability_projection
                )
            store.save_checkpoint(state)
            run_result = replace(
                run_result,
                browser_observability_projection=(
                    browser_worker.browser_observability_application.acknowledge_checkpoint(
                        run_result.browser_observability_projection
                    )
                ),
            )
            status = (
                HTTPStatus.ACCEPTED
                if browser_action_pending
                else HTTPStatus.CREATED
                if run_result.worker_result.ok
                else HTTPStatus.CONFLICT
            )
            permission_session = _browser_permission_session_envelope(run_result)
            self._send_json(
                status,
                {
                    "task": to_jsonable(state),
                    "worker_request": _worker_request_projection(request),
                    "worker_result": to_jsonable(run_result.worker_result),
                    "events": [
                        to_jsonable(event)
                        for event in [*workspace_events, *run_result.event_records]
                    ],
                    "browser_context": run_result.browser_context_projection,
                    "browser_observability": run_result.browser_observability_projection,
                    "idempotency_key": idempotency_key,
                    "permission_session": permission_session,
                    "browser_action_continuation": {
                        "pending": browser_action_pending,
                        "checkpoint_id": run_result.worker_result.metadata.get(
                            "browser_pending_checkpoint_id", ""
                        ),
                        "permission_request_id": run_result.worker_result.metadata.get(
                            "browser_pending_permission_request_id", ""
                        ),
                        "permission_tool_use_id": run_result.worker_result.metadata.get(
                            "browser_pending_permission_tool_use_id", ""
                        ),
                        "resume_method": "POST same endpoint with same Idempotency-Key after permission resolution",
                    },
                },
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Pragma": "no-cache",
                    "X-Zyra-Permission-State-Owner": "PermissionStateStore",
                    "X-Zyra-Context-State-Owner": "ClaudeContextWindowManager/M1-02D",
                },
            )

    def _execute_skill_runtime_post(
        self,
        store: SQLiteStore,
        task_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Invoke a versioned skill through the 03C -> 03A main path.

        Skill state is embedded in the existing task/session checkpoint.  The
        registry and revision loader remain 03C-owned; the permission runtime
        remains the only decision owner.  An exact rule is created only for
        immutable Zyra builtins explicitly invoked through this API.  It does
        not authorize any downstream tool; the skill policy hook only denies
        tools outside the skill ceiling and every admitted tool still needs a
        separate 03A one-use grant at execution time.
        """

        with _task_lock(task_id):
            state = store.load_task(task_id)
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            skill_name = str(payload.get("skill_name") or payload.get("skill") or "").strip()
            if not skill_name:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "skill_name_required"},
                )
                return
            node_id = str(payload.get("node_id") or state.root_node_id)
            if node_id not in _task_node_ids(state):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_task_node", "task_id": task_id, "node_id": node_id},
                )
                return
            arguments = payload.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            resources = payload.get("resources")
            if not isinstance(resources, list) or not all(isinstance(item, str) for item in resources):
                resources = []
            session_id = str(payload.get("session_id") or "").strip() or f"skill:{task_id}"
            worker_request_id = str(payload.get("worker_request_id") or "").strip() or new_id(
                "skill-worker-request"
            )

            invocation_id = str(payload.get("invocation_id") or "").strip() or new_id("skillinv")
            tool_use_id = str(payload.get("tool_use_id") or payload.get("tool_call_id") or "").strip() or invocation_id
            idempotency_key = str(
                self.headers.get("Idempotency-Key")
                or payload.get("idempotency_key")
                or ""
            ).strip()
            if len(idempotency_key) > 200:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_idempotency_key", "message": "Idempotency-Key exceeds 200 characters."},
                )
                return

            control_plane = get_permission_control_plane()
            try:
                custody_receipt = control_plane.custody_store.claim(
                    PermissionSessionCustodyBinding(
                        session_id=session_id,
                        run_id=state.run_id,
                        task_id=state.task_id,
                        workspace_root=str(tool_workspace_path()),
                    ),
                    presented_token=extract_bearer_token(self.headers, payload),
                    external_session_exists=bool(payload.get("session_id")),
                )
            except Exception as error:  # noqa: BLE001 - custody details must remain private.
                response = permission_api_error_response(
                    PermissionApiOperation.SESSION_RESUME,
                    error,
                )
                self._send_permission_response(store, response)
                return
            permission_runtime = ToolPermissionRuntime.for_session(
                session_id=session_id,
                state_path=permission_state_path(),
                workspace_root=tool_workspace_path(),
                custody_fingerprint=custody_receipt.custody_fingerprint,
            )
            state_snapshot = state.metadata.get("skill_runtime_state")
            config = SkillRuntimeConfig.for_project(
                PROJECT_ROOT,
                workspace_root=tool_workspace_path(),
                include_user_skills=False,
                disabled=_truthy(os.environ.get("ZYRA_SKILL_RUNTIME_DISABLED"), default=False),
            )
            runtime = SkillRuntime(
                config,
                permission_port=ToolPermissionRuntimeSkillGateway(
                    permission_runtime,
                    workspace_root=str(tool_workspace_path()),
                ),
                state_snapshot=state_snapshot if isinstance(state_snapshot, dict) else None,
            )
            try:
                runtime.bootstrap()
                revision = runtime.registry.resolve(
                    skill_name,
                    require_user_invocable=True,
                )
            except Exception as error:  # noqa: BLE001 - skill errors provide safe structured detail.
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": str(getattr(error, "code", "skill_not_found")),
                        "message": str(error),
                        "skill_name": skill_name,
                        "detail": dict(getattr(error, "detail", {}) or {}),
                    },
                )
                return
            request = SkillInvocationRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                session_id=session_id,
                agent_id=str(payload.get("agent_id") or "CodeWorkerRuntime"),
                skill_name=skill_name,
                arguments=dict(arguments),
                node_id=node_id,
                worker_request_id=worker_request_id,
                parent_tool_use_id=tool_use_id,
                context_refs=tuple(str(item) for item in payload.get("context_refs") or ()),
                attachment_refs=tuple(str(item) for item in payload.get("attachment_refs") or ()),
                skill_depth=int(payload.get("skill_depth") or 0),
                idempotency_key=idempotency_key,
                interactive=True,
                headless=False,
                invocation_id=invocation_id,
            )
            active_ref = runtime.registry.snapshot().active_by_qualified_name.get(
                revision.qualified_name,
                "",
            )
            admission = SkillCommandSafetyClassifier().classify(
                request,
                revision,
                requested_resources=tuple(resources),
                active_ref=active_ref,
            )
            if not admission.valid:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "skill_admission_invalid", "admission": admission.to_dict()},
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
            permission_arguments = {
                "skill_ref": revision.version_ref.immutable_ref,
                "arguments": dict(arguments),
                "invocation_mode": str(revision.metadata.invocation.mode),
            }
            if admission.bootstrap_allow:
                permission_runtime.rule_store.add(
                    PermissionRuleRecord(
                        rule_id=f"api-skill-exact-{invocation_id}",
                        effect=RuntimePermissionEffect.ALLOW,
                        source=PermissionRuleSource.COMMAND,
                        scope=PermissionScope(
                            PermissionScopeKind.ACTION,
                            session_id=session_id,
                            task_id=state.task_id,
                            run_id=state.run_id,
                            workspace_root=str(tool_workspace_path()),
                            tool_namespace="skill",
                            tool_name=revision.metadata.name,
                            server_id=revision.provenance.source_namespace,
                            argument_digest=arguments_digest(permission_arguments),
                        ),
                        namespace_pattern="skill",
                        tool_pattern=revision.metadata.name,
                        server_pattern=revision.provenance.source_namespace,
                        operation_pattern="skill_context_expansion",
                        reason="exact immutable builtin skill explicitly invoked through task API",
                        max_uses=1,
                        metadata={
                            "authority": "interactive_task_api",
                            "downstream_tools_authorized": False,
                            "owner_unit": "M1-03C",
                            "admission_digest": admission.request_digest,
                        },
                    )
                )
            try:
                plan = runtime.invoke(
                    request,
                    load_resources=tuple(resources),
                    require_user_invocable=True,
                )
            except (SkillPermissionPending, SkillPermissionDenied) as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": str(getattr(error, "code", "skill_permission_denied")),
                        "message": str(error),
                        "detail": dict(getattr(error, "detail", {}) or {}),
                        "permission_session": PermissionCustodyEnvelope.from_receipt(
                            custody_receipt
                        ).private_dict(),
                    },
                    headers={
                        "Cache-Control": "no-store, max-age=0",
                        "X-Zyra-Permission-State-Owner": "PermissionStateStore",
                    },
                )
                return
            except Exception as error:  # noqa: BLE001 - report fail-closed skill contract errors.
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": str(getattr(error, "code", "skill_invocation_failed")),
                        "message": str(error),
                        "detail": dict(getattr(error, "detail", {}) or {}),
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return

            records = list(runtime.invocation_runtime.event_records(plan.state.invocation_id))
            persist_events(store, records)
            policy_hook_id = runtime.install_permission_hook(
                permission_runtime.evaluator.hook_adapter,
                permission_session_id=session_id,
            )
            session_mutation = runtime.session_bridge.mutation_for_plan(
                plan,
                runtime.invocation_runtime.events(plan.state.invocation_id),
            )
            session_checkpoint = runtime.session_bridge.checkpoint(
                session_id=session_id,
                agent_id=request.agent_id,
                runtime_state_snapshot=runtime.state_snapshot(),
            )
            state.metadata["skill_runtime_state"] = session_checkpoint.to_dict()
            state.metadata["skill_session_context"] = {
                "owner": "02B/02D session aggregate via M1-03C SkillSessionBridge",
                "latest_mutation": {
                    "invocation_id": session_mutation.invocation_id,
                    "mutation_digest": session_mutation.mutation_digest,
                    "policy_snapshot": dict(session_mutation.policy_snapshot),
                    "state": dict(session_mutation.state),
                },
                "message_delta_refs": list(plan.state.message_delta_refs),
                "attachment_refs": list(plan.state.attachment_refs),
                "invoked_skill_refs": [
                    reference.to_dict() for reference in session_checkpoint.compact_references
                ],
                "permission_hook_id": policy_hook_id,
                "body_in_checkpoint": False,
            }
            state.metadata["skill_registry_generation"] = runtime.registry.generation
            state.metadata["skill_invocation_projection"] = {
                "invocation_id": plan.state.invocation_id,
                "qualified_name": plan.revision.qualified_name,
                "version_ref": plan.revision.version_ref.to_dict(),
                "status": str(plan.state.status),
                "policy_snapshot_digest": plan.policy_snapshot.policy_digest,
                "attachment_refs": [item.immutable_ref for item in plan.attachments],
                "body_in_checkpoint": False,
                "permission_owner": "M1-03A",
                "skill_owner": "M1-03C",
            }
            state.updated_at = plan.state.updated_at
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "task": to_jsonable(state),
                    "skill": plan.revision.to_dict(),
                    "events": [to_jsonable(record) for record in records],
                    "skill_result": {
                        "ok": True,
                        "summary": (
                            f"Skill {plan.revision.qualified_name} loaded as immutable revision "
                            f"{plan.revision.version_ref.content_digest[:12]}."
                        ),
                        "runtime_status": str(plan.state.status),
                        "invocation": plan.to_dict(include_body=False),
                        "message_deltas": [message.to_dict() for message in plan.messages],
                        "body_returned": (
                            plan.revision.metadata.invocation.mode
                            is SkillInvocationMode.INLINE
                        ),
                        "body_in_checkpoint": False,
                        "admission": admission.to_dict(),
                        "session_mutation": session_mutation.to_dict(),
                        "session_checkpoint": session_checkpoint.to_dict(),
                        "permission_hook_id": policy_hook_id,
                    },
                    "permission_session": PermissionCustodyEnvelope.from_receipt(
                        custody_receipt
                    ).private_dict(),
                },
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Pragma": "no-cache",
                    "X-Zyra-Permission-State-Owner": "PermissionStateStore",
                    "X-Zyra-Skill-State-Owner": "SkillInvocationStateStore",
                },
            )

    def _execute_skill_update_post(
        self,
        store: SQLiteStore,
        task_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Run a trusted-local install/update/control request through 03A."""

        with _task_lock(task_id):
            state = store.load_task(task_id)
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            session_id = str(payload.get("session_id") or f"skill-update:{task_id}")
            custody = None
            try:
                custody = get_permission_control_plane().custody_store.claim(
                    PermissionSessionCustodyBinding(
                        session_id=session_id,
                        run_id=state.run_id,
                        task_id=state.task_id,
                        workspace_root=str(tool_workspace_path()),
                    ),
                    presented_token=extract_bearer_token(self.headers, payload),
                    external_session_exists=bool(payload.get("session_id")),
                )
                permission_runtime = ToolPermissionRuntime.for_session(
                    session_id=session_id,
                    state_path=permission_state_path(),
                    workspace_root=tool_workspace_path(),
                    custody_fingerprint=custody.custody_fingerprint,
                )
                checkpoint = state.metadata.get("skill_runtime_state")
                skill_runtime = SkillRuntime(
                    SkillRuntimeConfig.for_project(
                        PROJECT_ROOT,
                        workspace_root=tool_workspace_path(),
                        include_user_skills=False,
                    ),
                    state_snapshot=checkpoint if isinstance(checkpoint, dict) else None,
                )
                skill_runtime.bootstrap()
                update_control = SkillUpdateControlRuntime(
                    product_root=PROJECT_ROOT,
                    workspace_root=tool_workspace_path(),
                    permission_runtime=permission_runtime,
                    skill_runtime=skill_runtime,
                    state_snapshot=(
                        state.metadata.get("skill_update_runtime_state")
                        if isinstance(state.metadata.get("skill_update_runtime_state"), dict)
                        else None
                    ),
                )
                receipt = update_control.execute(
                    payload,
                    run_id=state.run_id,
                    task_id=state.task_id,
                    session_id=session_id,
                    task_metadata=state.metadata,
                )
                state.metadata["skill_runtime_state"] = (
                    update_control.skill_runtime.state_snapshot()
                )
                skill_context = state.metadata.get("skill_session_context")
                if isinstance(skill_context, dict) and receipt.state.invalidated_invocations:
                    invalidated = set(receipt.state.invalidated_invocations)
                    skill_context["invoked_skill_refs"] = [
                        item
                        for item in skill_context.get("invoked_skill_refs") or ()
                        if isinstance(item, dict)
                        and str(item.get("invocation_id") or "") not in invalidated
                    ]
            except Exception as error:  # noqa: BLE001 - structured fail-closed control response.
                code = str(getattr(error, "code", "skill_update_failed"))
                if "skill_update_runtime_state" in state.metadata:
                    store.save_checkpoint(state)
                status = (
                    HTTPStatus.CONFLICT
                    if code.endswith(("pending", "denied"))
                    else HTTPStatus.BAD_REQUEST
                )
                self._send_json(
                    status,
                    {
                        "error": code,
                        "message": str(error),
                        "detail": dict(getattr(error, "detail", {}) or {}),
                        "permission_session": (
                            PermissionCustodyEnvelope.from_receipt(custody).private_dict()
                            if custody is not None and custody.created
                            else PermissionCustodyEnvelope.from_receipt(custody).public_dict()
                            if custody is not None
                            else None
                        ),
                        "update_id": str(
                            payload.get("update_id")
                            or dict(getattr(error, "detail", {}) or {}).get("update_id")
                            or ""
                        ),
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
            events = [
                EventRecord(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    event_type=EventType.SKILL_INVOKED,
                    payload=value,
                )
                for value in receipt.event_payloads
            ]
            events.append(
                EventRecord(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    event_type=EventType.SKILL_INVOKED,
                    payload={
                        "phase": "skill_update_committed",
                        "owner_unit": "M1-03C",
                        "control": receipt.to_dict(),
                        "body_persisted": False,
                    },
                )
            )
            persist_events(store, events)
            state.updated_at = events[-1].created_at
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.OK,
                {
                    "task": to_jsonable(state),
                    "skill_update": receipt.to_dict(),
                    "events": [to_jsonable(event) for event in events],
                    "permission_session": PermissionCustodyEnvelope.from_receipt(
                        custody
                    ).private_dict(),
                },
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "X-Zyra-Permission-State-Owner": "PermissionStateStore",
                    "X-Zyra-Skill-Update-State-Owner": "SkillUpdateRuntime",
                },
            )

    def _execute_skill_lifecycle_post(
        self,
        store: SQLiteStore,
        task_id: str,
        *,
        invocation_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        with _task_lock(task_id):
            state = store.load_task(task_id)
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            checkpoint = state.metadata.get("skill_runtime_state")
            if not isinstance(checkpoint, dict):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "skill_runtime_state_not_found"})
                return
            runtime = SkillRuntime(
                SkillRuntimeConfig.for_project(
                    PROJECT_ROOT,
                    workspace_root=tool_workspace_path(),
                    include_user_skills=False,
                    disabled=_truthy(os.environ.get("ZYRA_SKILL_RUNTIME_DISABLED"), default=False),
                ),
                state_snapshot=checkpoint,
            )
            try:
                runtime.bootstrap()
                current = runtime.state_store.get(invocation_id)
                if current.task_id != task_id:
                    raise ValueError("skill invocation belongs to another task")
                if action == "cancel":
                    terminal = runtime.invocation_runtime.cancel(
                        invocation_id,
                        reason=str(payload.get("reason") or "cancelled by task API"),
                    )
                    outcome_commit = None
                else:
                    task_events = [to_jsonable(item) for item in store.task_events(task_id)]
                    task_artifacts = [to_jsonable(item) for item in state.artifacts]
                    evidence_port = InMemorySkillOutcomeEvidencePort(
                        events=(item for item in task_events if isinstance(item, dict)),
                        artifacts=(item for item in task_artifacts if isinstance(item, dict)),
                    )
                    known_event_ids = {
                        str(item.get("event_id") or "")
                        for item in task_events
                        if isinstance(item, dict)
                    }
                    supplied_evidence = tuple(
                        str(item) for item in payload.get("evidence_refs") or ()
                    )
                    event_ids = [str(item) for item in payload.get("event_ids") or ()]
                    explicit_refs = [str(item) for item in payload.get("explicit_refs") or ()]
                    for reference in supplied_evidence:
                        candidate = reference.rstrip("/").rsplit("/", 1)[-1]
                        if candidate in known_event_ids:
                            event_ids.append(candidate)
                        else:
                            explicit_refs.append(reference)
                    explicit_refs.extend(
                        str(item) for item in payload.get("outcome_refs") or ()
                    )
                    artifact_ids = [str(item) for item in payload.get("artifact_ids") or ()]
                    artifact_ids.extend(
                        str(item).rstrip("/").rsplit("/", 1)[-1]
                        for item in payload.get("artifact_refs") or ()
                    )
                    if not event_ids and not artifact_ids and not explicit_refs:
                        event_ids.extend(
                            str(item.get("event_id"))
                            for item in task_events
                            if isinstance(item, dict)
                            and isinstance(item.get("payload"), dict)
                            and str(
                                item["payload"].get("invocation_id")
                                or (
                                    item["payload"].get("skill_runtime", {}).get("invocation_id")
                                    if isinstance(item["payload"].get("skill_runtime"), dict)
                                    else ""
                                )
                                or ""
                            )
                            == invocation_id
                        )
                    outcome_commit = SkillOutcomeCommitRuntime(
                        skill_runtime=runtime,
                        evidence_port=evidence_port,
                    ).commit(
                        SkillOutcomeCommitRequest(
                            commit_id=str(payload.get("commit_id") or new_id("skillcommit")),
                            invocation_id=invocation_id,
                            run_id=current.run_id,
                            task_id=current.task_id,
                            session_id=current.session_id,
                            worker_request_id=str(payload.get("worker_request_id") or ""),
                            child_task_id=str(payload.get("child_task_id") or ""),
                            event_ids=tuple(dict.fromkeys(event_ids)),
                            artifact_ids=tuple(dict.fromkeys(artifact_ids)),
                            explicit_refs=tuple(dict.fromkeys(explicit_refs)),
                            expected_state_revision=current.revision,
                        )
                    )
                    terminal = runtime.state_store.get(invocation_id)
            except Exception as error:  # noqa: BLE001 - fail-closed lifecycle response.
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": str(getattr(error, "code", "skill_lifecycle_failed")),
                        "message": str(error),
                    },
                )
                return
            session_checkpoint = runtime.session_bridge.checkpoint(
                session_id=terminal.session_id,
                agent_id=terminal.agent_id,
                runtime_state_snapshot=runtime.state_snapshot(),
            )
            state.metadata["skill_runtime_state"] = session_checkpoint.to_dict()
            context = state.metadata.setdefault("skill_session_context", {})
            context["invoked_skill_refs"] = [
                reference.to_dict() for reference in session_checkpoint.compact_references
            ]
            context["latest_terminal_state"] = terminal.to_dict()
            context["permission_hook_id"] = ""
            context["permission_hook_active"] = False
            outcome = (
                outcome_commit.outcome_projection.to_dict()
                if action == "complete" and outcome_commit is not None
                else None
            )
            if outcome_commit is not None:
                context["latest_outcome_commit"] = outcome_commit.to_dict()
            projection = state.metadata.get("skill_invocation_projection")
            if isinstance(projection, dict) and projection.get("invocation_id") == invocation_id:
                projection["status"] = str(terminal.status)
                projection["outcome_projection"] = outcome
                projection["permission_hook_active"] = False
            event = EventRecord(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                event_type=EventType.SKILL_INVOKED,
                payload={
                    "phase": f"skill_{action}",
                    "invocation_id": invocation_id,
                    "status": str(terminal.status),
                    "outcome_projection": outcome,
                    "owner_unit": "M1-03C",
                },
            )
            persist_events(store, (event,))
            state.updated_at = terminal.updated_at
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.OK,
                {
                    "task": to_jsonable(state),
                    "skill_state": terminal.to_dict(),
                    "outcome_projection": outcome,
                    "event": to_jsonable(event),
                    "session_checkpoint": session_checkpoint.to_dict(),
                },
                headers={"Cache-Control": "no-store, max-age=0"},
            )

    def _execute_code_worker_post(self, store: SQLiteStore, task_id: str, payload: dict[str, Any]) -> None:
        with _task_lock(task_id):
            state = store.load_task(task_id)
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            idempotency_key = str(self.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip()
            if len(idempotency_key) > 200:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_idempotency_key", "message": "Idempotency-Key exceeds 200 characters."},
                )
                return
            used_keys = state.metadata.setdefault("code_worker_idempotency_keys", [])
            if idempotency_key and idempotency_key in used_keys:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "duplicate_idempotency_key", "idempotency_key": idempotency_key},
                )
                return
            node_id = str(payload.get("node_id") or state.root_node_id)
            if node_id not in _task_node_ids(state):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_task_node", "task_id": task_id, "node_id": node_id},
                )
                return
            constraints = payload.get("constraints")
            if not isinstance(constraints, dict):
                constraints = {}
            else:
                constraints = dict(constraints)
            for key, value in payload.items():
                if key in {"constraints", "node_id", "idempotency_key"}:
                    continue
                constraints.setdefault(key, value)
            skill_checkpoint = state.metadata.get("skill_runtime_state")
            skill_context = state.metadata.get("skill_session_context")
            if isinstance(skill_checkpoint, dict) and isinstance(skill_context, dict):
                for key, value in SkillTaskIntegrationRuntime().worker_constraints(
                    state.metadata
                ).items():
                    constraints.setdefault(key, value)
            worker_request_id = new_id("workerreq")
            worker_messages, skill_disclosure_batch = _task_skill_worker_messages(
                state,
                worker_request_id=worker_request_id,
            )
            parent_session_id = str(
                constraints.get("session_id")
                or state.metadata.get("query_session_id")
                or f"task:{state.task_id}"
            )
            constraints["session_id"] = parent_session_id
            state.metadata["query_session_id"] = parent_session_id
            try:
                workspace_manager = get_workspace_manager()
                workspace_access = workspace_manager.acquire_for_worker(
                    task_id=state.task_id,
                    # The CodeWorker query session can be replaced for an
                    # approval/resume turn; task workspace ownership does not
                    # move with that conversational session.
                    session_id="",
                    worker_id="CodeWorkerRuntime",
                )
                worker_workspace_root = workspace_manager.internal_task_root(workspace_access)
            except WorkspaceError as error:
                response = workspace_error_response(error)
                self._send_json(response.status, response.body, headers=dict(response.headers))
                return
            constraints["workspace_ref"] = workspace_access.to_public_dict()
            state.metadata["workspace_ref"] = workspace_manager.project(
                workspace_access.workspace_id
            ).to_dict()
            browser_context_scope = BrowserContextScope(
                run_id=state.run_id,
                task_id=state.task_id,
                session_id=parent_session_id,
            )
            browser_context_batch = _BROWSER_CONTEXT_TASK_INTEGRATION.prepare_delivery(
                state.metadata,
                scope=browser_context_scope,
                consumer_worker_request_id=worker_request_id,
                max_items=1,
                max_chars=int(constraints.get("browser_context_delivery_max_chars") or 24000),
            )
            if browser_context_batch.messages:
                worker_messages = tuple((*worker_messages, *browser_context_batch.messages))
            # Persist the claim before provider dispatch. Exact retry binds an
            # indeterminate claim to the same worker request rather than
            # delivering it concurrently to a new request.
            store.save_checkpoint(state)
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=node_id,
                worker_name="CodeWorkerRuntime",
                messages=worker_messages,
                request_id=worker_request_id,
                constraints=constraints,
                metadata={"skill_context_owner": "M1-03C.SkillSessionBridge"},
            )
            parent_scope = _issue_parent_subagent_scope(state, session_id=parent_session_id)
            agent_tool = AgentToolRuntime(
                get_subagent_runtime(),
                AgentToolParentContext(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    session_id=parent_session_id,
                    worker_request_id=worker_request_id,
                    workspace_root=str(worker_workspace_root),
                    parent_scope=parent_scope,
                    context_payload={
                        "messages": [],
                        "artifact_refs": [to_jsonable(item) for item in state.artifacts],
                        "evidence_refs": list(state.metadata.get("evidence_refs") or ()),
                        "invoked_skill_refs": list(state.metadata.get("skill_invocation_refs") or ()),
                        "context_epoch": int(state.metadata.get("context_epoch") or 0),
                        "compact_boundary_id": str(state.metadata.get("compact_boundary_id") or ""),
                        "rendered_system_prompt": str(state.metadata.get("rendered_system_prompt") or ""),
                    },
                    root_task_id=state.task_id,
                    node_id=node_id,
                ),
            )
            agent_tool_binding = agent_tool.bind(default_tool_registry())
            try:
                run_result = CodeWorkerRuntime(
                    project_root=PROJECT_ROOT,
                    workspace_root=worker_workspace_root,
                    artifact_root=artifact_root_path(),
                    permission_store=get_permission_store(),
                    permission_state_path=permission_state_path(),
                    mcp_runtime=get_mcp_runtime(),
                    tool_registry=agent_tool_binding.registry,
                    dynamic_handlers=agent_tool_binding.handlers,
                    runtime_services={
                        "workspace_edit_port": WorkspaceEditPort(
                            workspace_manager,
                            workspace_access,
                            worker_id="CodeWorkerRuntime",
                            run_id=state.run_id,
                            task_id=state.task_id,
                            node_id=node_id,
                            artifact_store=LocalArtifactStore(artifact_root_path()),
                        ),
                        "workspace_isolation_runtime": WorkspaceIsolationRuntime(
                            workspace_manager,
                            artifact_store=LocalArtifactStore(artifact_root_path()),
                        ),
                        "workspace_gateway_required": True,
                    },
                ).run(request)
            except Exception as error:  # noqa: BLE001 - keep internal exception details out of API responses.
                persist_workspace_events(store, task_id=state.task_id)
                _BROWSER_CONTEXT_TASK_INTEGRATION.release_delivery(
                    state.metadata,
                    batch=browser_context_batch,
                    reason=f"CodeWorker raised {type(error).__name__} after context claim",
                    indeterminate=bool(browser_context_batch.source_ids),
                )
                store.save_checkpoint(state)
                if skill_disclosure_batch is not None:
                    SkillTaskIntegrationRuntime().abort_disclosures(
                        metadata=state.metadata,
                        batch=skill_disclosure_batch,
                        reason=f"worker raised {type(error).__name__}",
                    )
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "error": "code_worker_failed",
                        "message": "CodeWorker execution failed.",
                        "exception_type": type(error).__name__,
                    },
                )
                return

            _attach_artifacts(state, list(run_result.worker_result.artifacts))
            browser_context_selection = _BROWSER_CONTEXT_TASK_INTEGRATION.verify_provider_selection(
                browser_context_batch,
                run_result.event_records,
            )
            browser_context_delivery_ok = browser_context_batch.empty or browser_context_selection.valid
            if browser_context_delivery_ok:
                browser_context_checkpoint = _BROWSER_CONTEXT_TASK_INTEGRATION.commit_delivery(
                    state.metadata,
                    batch=browser_context_batch,
                    worker_event_ids=tuple(event.event_id for event in run_result.event_records),
                )
            else:
                browser_context_checkpoint = _BROWSER_CONTEXT_TASK_INTEGRATION.release_delivery(
                    state.metadata,
                    batch=browser_context_batch,
                    reason="02D provider envelope did not select the claimed browser disclosure exactly once",
                    indeterminate=False,
                )
            browser_context_event = _BROWSER_CONTEXT_TASK_INTEGRATION.event_for_delivery(
                browser_context_batch,
                node_id=node_id,
                operation=(
                    "consumed"
                    if browser_context_batch.source_ids and browser_context_delivery_ok
                    else "provider_selection_failed"
                    if browser_context_batch.source_ids
                    else "no_pending_context"
                ),
                worker_event_ids=tuple(event.event_id for event in run_result.event_records),
                reason="" if browser_context_delivery_ok else ";".join(browser_context_selection.findings),
            )
            if browser_context_batch.source_ids and browser_context_delivery_ok:
                try:
                    _runtime, browser_worker = get_browser_runtime_services()
                    projection_store = browser_worker.browser_message_state_application.turn_store
                    for disclosure_id in browser_context_batch.disclosure_ids:
                        projection_store.mark_consumed(disclosure_id)
                except Exception:
                    # The turn projection is explicitly a reconstructable read
                    # model. Canonical consumption remains in TaskState.
                    pass
            skill_task_bridge = SkillTaskIntegrationRuntime()
            skill_ingest_receipt = skill_task_bridge.ingest_worker_events(
                metadata=state.metadata,
                events=run_result.event_records,
                run_id=state.run_id,
                task_id=state.task_id,
                worker_request_id=worker_request_id,
            )
            if skill_disclosure_batch is not None:
                skill_task_bridge.commit_disclosures(
                    metadata=state.metadata,
                    batch=skill_disclosure_batch,
                    worker_event_ids=tuple(
                        event.event_id for event in run_result.event_records
                    ),
                )
            state.budget.tool_calls += sum(1 for event in run_result.event_records if "tool_result" in event.payload)
            if run_result.event_records:
                state.updated_at = run_result.event_records[-1].created_at
            _record_code_worker_session_metadata(state, run_result)
            projection_runtime = CodeWorkerTaskApiProjectionRuntime()
            codeworker_api_projection = projection_runtime.build_session_projection(
                task_id=state.task_id,
                state=to_jsonable(state),
                events=[to_jsonable(event) for event in run_result.event_records],
            )
            projection_event = projection_runtime.event_for_projection(
                codeworker_api_projection,
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=node_id,
            )
            projected_events = [*run_result.event_records, browser_context_event, projection_event]
            state.metadata["last_code_worker_api_projection"] = codeworker_api_projection.to_dict()
            response_payload = {
                "task": to_jsonable(state),
                "worker_request": _worker_request_projection(request),
                "worker_result": to_jsonable(run_result.worker_result),
                "codeworker_session": codeworker_api_projection.to_dict(),
                "compact_state": codeworker_api_projection.compact_state.to_dict(),
                "tool_trace": codeworker_api_projection.tool_trace.to_dict(),
                "events": [to_jsonable(event) for event in projected_events],
                "skill_task_ingest": skill_ingest_receipt.to_dict(),
                "browser_context_delivery": browser_context_batch.to_dict(),
                "browser_context_provider_selection": browser_context_selection.to_dict(),
                "browser_context": _BROWSER_CONTEXT_TASK_INTEGRATION.public_projection(
                    browser_context_checkpoint
                ),
            }
            contract_runtime = CodeWorkerTaskApiContractRuntime()
            route_contract = contract_runtime.build_report(
                route_kind=TaskApiRouteKind.POST_CODE_WORKER,
                task_id=state.task_id,
                payload=response_payload,
                events=response_payload["events"],
                projection=codeworker_api_projection,
            )
            contract_event = contract_runtime.event_for_report(
                route_contract,
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=node_id,
                session_id=codeworker_api_projection.session.session_id,
                worker_request_id=codeworker_api_projection.session.worker_request_id,
            )
            persisted_events = [
                *drain_workspace_events(state.task_id),
                *projected_events,
                contract_event,
            ]
            response_payload["events"] = [to_jsonable(event) for event in persisted_events]
            response_payload["route_contract"] = route_contract.to_dict()
            state.metadata["last_code_worker_api_route_contract"] = route_contract.to_dict()
            if idempotency_key:
                used_keys.append(idempotency_key)
                del used_keys[:-128]
            state.updated_at = contract_event.created_at
            response_payload["task"] = to_jsonable(state)
            persist_events(store, persisted_events)
            store.save_checkpoint(state)
            status = (
                HTTPStatus.CREATED
                if run_result.worker_result.ok and route_contract.ok and browser_context_delivery_ok
                else HTTPStatus.CONFLICT
            )
            permission_session = {
                **run_result.private_api_session_envelope(),
                "schema": "zyra.permission-session-api-envelope.v1",
                "cacheable": False,
                "must_not_persist": True,
                "presentation": "one_time_if_created",
            }
            self._send_json(
                status,
                {**response_payload, "permission_session": permission_session},
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Pragma": "no-cache",
                    "X-Zyra-Permission-State-Owner": "PermissionStateStore",
                },
            )

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise JsonRequestError(HTTPStatus.BAD_REQUEST, "unsupported_transfer_encoding", "Chunked request bodies are not supported.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError) as error:
            raise JsonRequestError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Content-Length must be an integer.") from error
        if length < 0:
            raise JsonRequestError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Content-Length cannot be negative.")
        if length <= 0:
            return {}
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json" and not content_type.endswith("+json"):
            raise JsonRequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported_media_type", "Content-Type must be application/json.")
        try:
            max_bytes = max(1024, int(os.environ.get("ZYRA_MAX_JSON_BODY_BYTES", "2097152")))
        except ValueError:
            max_bytes = 2097152
        if length > max_bytes:
            raise JsonRequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_body_too_large", f"JSON body exceeds {max_bytes} bytes.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise JsonRequestError(HTTPStatus.BAD_REQUEST, "incomplete_request_body", "Request body ended before Content-Length bytes were read.")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise JsonRequestError(HTTPStatus.BAD_REQUEST, "invalid_utf8", "JSON body must be valid UTF-8.") from error
        try:
            body = json.loads(text)
        except json.JSONDecodeError as error:
            raise JsonRequestError(HTTPStatus.BAD_REQUEST, "invalid_json", "Request body is not valid JSON.") from error
        if not isinstance(body, dict):
            raise JsonRequestError(HTTPStatus.BAD_REQUEST, "json_object_required", "JSON body must be an object.")
        return body

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in dict(headers or {}).items():
            normalized = str(key).strip()
            if not normalized or "\r" in normalized or "\n" in normalized:
                continue
            rendered = str(value)
            if "\r" in rendered or "\n" in rendered:
                continue
            self.send_header(normalized, rendered)
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self) -> None:
        origin = str(self.headers.get("Origin") or "").strip()
        allowed_origins = {
            item.strip()
            for item in os.environ.get(
                "ZYRA_CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173",
            ).split(",")
            if item.strip() and item.strip() != "*"
        }
        if origin and origin in allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Idempotency-Key, Authorization, X-Zyra-Service-Token",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def run(host: str | None = None, port: int | None = None) -> None:
    bind_host = host or os.environ.get("ZYRA_API_HOST", "127.0.0.1")
    bind_port = port or int(os.environ.get("ZYRA_API_PORT", "8000"))
    get_store()
    server = ThreadingHTTPServer((bind_host, bind_port), ZyraRequestHandler)
    print(f"Zyra API listening on http://{bind_host}:{bind_port}")
    print(f"Event log: {event_log_path()}")
    print(f"SQLite: {sqlite_path()}")
    server.serve_forever()


def _path_parts(path: str) -> list[str]:
    return [part for part in path.strip("/").split("/") if part]


def _bounded_permission_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 100
    return min(1000, max(1, parsed))


def _is_ledger_list_path(parts: list[str]) -> bool:
    return parts == ["ledger"] or parts == ["integrations", "ledger"]


def _is_ledger_audit_path(parts: list[str]) -> bool:
    return parts == ["ledger", "audit"] or parts == ["integrations", "ledger", "audit"]


def _is_ledger_readiness_path(parts: list[str]) -> bool:
    return parts == ["ledger", "readiness"] or parts == ["integrations", "ledger", "readiness"]


def _is_ledger_report_path(parts: list[str]) -> bool:
    return parts == ["ledger", "report"] or parts == ["integrations", "ledger", "report"]


def _is_ledger_accounting_path(parts: list[str]) -> bool:
    return parts == ["ledger", "accounting"] or parts == ["integrations", "ledger", "accounting"]


def _is_ledger_linecount_path(parts: list[str]) -> bool:
    return parts == ["ledger", "linecount"] or parts == ["integrations", "ledger", "linecount"]


def _is_ledger_buckets_path(parts: list[str]) -> bool:
    return parts == ["ledger", "buckets"] or parts == ["integrations", "ledger", "buckets"]


def _is_ledger_boundary_path(parts: list[str]) -> bool:
    return parts == ["ledger", "boundary"] or parts == ["integrations", "ledger", "boundary"]


def _is_ledger_reachability_path(parts: list[str]) -> bool:
    return parts == ["ledger", "reachability"] or parts == ["integrations", "ledger", "reachability"]


def _is_ledger_acceptance_path(parts: list[str]) -> bool:
    return parts == ["ledger", "acceptance"] or parts == ["integrations", "ledger", "acceptance"]


def _is_ledger_persistence_path(parts: list[str]) -> bool:
    return parts == ["ledger", "persistence"] or parts == ["integrations", "ledger", "persistence"]


def _is_ledger_cleanroom_path(parts: list[str]) -> bool:
    return parts == ["ledger", "cleanroom"] or parts == ["integrations", "ledger", "cleanroom"]


def _is_ledger_semantic_effects_path(parts: list[str]) -> bool:
    return parts == ["ledger", "semantic-effects"] or parts == ["integrations", "ledger", "semantic-effects"]


def _is_ledger_test_quality_path(parts: list[str]) -> bool:
    return parts == ["ledger", "test-quality"] or parts == ["integrations", "ledger", "test-quality"]


def _is_ledger_schema_contract_path(parts: list[str]) -> bool:
    return parts == ["ledger", "schema-contract"] or parts == ["integrations", "ledger", "schema-contract"]


def _is_ledger_evidence_graph_path(parts: list[str]) -> bool:
    return parts == ["ledger", "evidence-graph"] or parts == ["integrations", "ledger", "evidence-graph"]


def _is_ledger_state_custody_path(parts: list[str]) -> bool:
    return parts == ["ledger", "state-custody"] or parts == ["integrations", "ledger", "state-custody"]


def _is_ledger_mutation_consistency_path(parts: list[str]) -> bool:
    return parts == ["ledger", "mutation-consistency"] or parts == ["integrations", "ledger", "mutation-consistency"]


def _is_ledger_policy_matrix_path(parts: list[str]) -> bool:
    return parts == ["ledger", "policy-matrix"] or parts == ["integrations", "ledger", "policy-matrix"]


def _is_ledger_unit_review_path(parts: list[str]) -> bool:
    return parts == ["ledger", "unit-review"] or parts == ["integrations", "ledger", "unit-review"]


def _is_ledger_snapshots_path(parts: list[str]) -> bool:
    return parts == ["ledger", "snapshots"] or parts == ["integrations", "ledger", "snapshots"]


def _is_ledger_seed_path(parts: list[str]) -> bool:
    return parts == ["ledger", "seed"] or parts == ["integrations", "ledger", "seed"]


def _is_ledger_entries_path(parts: list[str]) -> bool:
    return parts == ["ledger", "entries"] or parts == ["integrations", "ledger", "entries"]


def _ledger_entry_id_from_path(parts: list[str]) -> str:
    reserved = {
        "acceptance",
        "audit",
        "boundary",
        "buckets",
        "cleanroom",
        "entries",
        "evidence-graph",
        "linecount",
        "mutation-consistency",
        "persistence",
        "policy-matrix",
        "reachability",
        "readiness",
        "report",
        "schema-contract",
        "semantic-effects",
        "seed",
        "snapshots",
        "state-custody",
        "test-quality",
        "unit-review",
    }
    if len(parts) == 2 and parts[0] == "ledger" and parts[1] not in reserved:
        return parts[1]
    if len(parts) == 3 and parts[0] == "integrations" and parts[1] == "ledger" and parts[2] not in reserved:
        return parts[2]
    return ""


def _ledger_advance_id_from_path(parts: list[str]) -> str:
    if len(parts) == 3 and parts[0] == "ledger" and parts[2] == "advance":
        return parts[1]
    if len(parts) == 4 and parts[0] == "integrations" and parts[1] == "ledger" and parts[3] == "advance":
        return parts[2]
    return ""


def _flatten_query(query: dict[str, list[str]]) -> dict[str, str]:
    return {key: values[-1] for key, values in query.items() if values}


def _split_csv(value: str) -> list[str] | None:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "strict"}


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_line_count_report(query_params: dict[str, str]) -> Any:
    base = query_params.get("base") or ""
    if not base:
        return None
    owner_unit = query_params.get("owner_unit") or query_params.get("unit") or ""
    minimum = _int_or_default(query_params.get("minimum_effective_lines"), minimum_effective_lines_for_unit(owner_unit))
    return build_line_count_report(
        PROJECT_ROOT,
        base=base,
        head=query_params.get("head") or "HEAD",
        cached=_truthy(query_params.get("cached")),
        minimum_effective_lines=minimum,
    )


def _uses_advanced_ledger_selector(query_params: dict[str, str]) -> bool:
    advanced_keys = {
        "runtime_module",
        "runtime_module_contains",
        "runtime_command",
        "runtime_command_contains",
        "runtime_protocol",
        "test_kind",
        "test_path",
        "test_path_contains",
        "test_command",
        "test_command_contains",
        "api_route",
        "api_route_contains",
        "event_type",
        "control_command",
        "surface",
        "worker_runtime",
        "ui_panel",
        "artifact_kind",
        "license_status",
        "target_prefix",
        "target_verdict",
        "target_exists",
        "has_blockers",
        "has_risk_notes",
        "has_replacement_plan",
        "dependency",
        "downstream_unit",
        "metadata_key",
        "text",
    }
    return any(key in query_params for key in advanced_keys)


def _filtered_audit_payload(report: Any, query_params: dict[str, str]) -> dict[str, Any]:
    payload = report.to_dict()
    severity = query_params.get("severity", "")
    source_repo = query_params.get("source_repo", "") or query_params.get("repo", "")
    owner_unit = query_params.get("owner_unit", "") or query_params.get("unit", "")
    code = query_params.get("code", "")
    if any([severity, source_repo, owner_unit, code]):
        findings = []
        for finding in report.findings:
            if severity and str(finding.severity) != severity:
                continue
            if source_repo and finding.source_repo != source_repo:
                continue
            if owner_unit and finding.owner_unit != owner_unit:
                continue
            if code and str(finding.code) != code:
                continue
            findings.append(finding.to_dict())
        payload["findings"] = findings
        payload["filtered_finding_count"] = len(findings)
    return payload


def _positive_int(value: str, default: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _optional_query_value(query: dict[str, list[str]], name: str) -> str | None:
    values = query.get(name)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _artifact_refs_from_store(store: SQLiteStore, *, task_id: str | None = None) -> list[ArtifactRef] | None:
    if task_id is not None:
        state = store.load_task(task_id)
        return None if state is None else list(state.artifacts)

    artifacts: list[ArtifactRef] = []
    for task in store.list_tasks():
        state = store.load_task(str(task["task_id"]))
        if state is not None:
            artifacts.extend(state.artifacts)
    return artifacts


def _find_artifact_ref(store: SQLiteStore, artifact_id: str) -> ArtifactRef | None:
    for artifact in _artifact_refs_from_store(store) or []:
        if artifact.artifact_id == artifact_id:
            return artifact
    return None


def _checkpoint_json(store: SQLiteStore, task_id: str) -> dict[str, Any] | None:
    state = store.load_task(task_id)
    return None if state is None else to_jsonable(state)


def _worker_request_projection(request: WorkerRequest) -> dict[str, Any]:
    payload = to_jsonable(request)
    constraints = payload.get("constraints")
    if isinstance(constraints, dict):
        payload["constraints"] = PermissionSessionCustodyStore.redact_constraints(
            constraints
        )
    return payload


def _browser_permission_session_envelope(run_result: Any) -> dict[str, Any]:
    """Build the API-only BrowserWorker custody envelope.

    The bearer capability lives only on ``BrowserWorkerRun`` and is added after
    task/event/checkpoint persistence.  Worker metadata carries public custody
    identifiers and fingerprints only, so the token cannot leak into durable
    task state or the canonical event log.
    """

    metadata = getattr(getattr(run_result, "worker_result", None), "metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    created = str(metadata.get("permission_session_custody_created") or "").lower() == "true"
    envelope: dict[str, Any] = {
        "schema": "zyra.permission-session-api-envelope.v1",
        "session_id": str(metadata.get("permission_runtime_session_id") or ""),
        "custody_id": str(metadata.get("permission_session_custody_id") or ""),
        "custody_fingerprint": str(
            metadata.get("permission_session_custody_fingerprint") or ""
        ),
        "custody_created": created,
        "custody_verified": (
            str(metadata.get("permission_session_custody_verified") or "").lower() == "true"
        ),
        "cacheable": False,
        "must_not_persist": True,
        "presentation": "one_time_if_created",
    }
    token = str(getattr(run_result, "permission_session_custody_token", "") or "")
    if created and token:
        envelope["custody_token"] = token
    return envelope


def _artifact_entry(catalog: LocalArtifactStore, artifact: ArtifactRef) -> dict[str, Any]:
    try:
        return catalog.describe(artifact)
    except ValueError as error:
        return {
            "artifact": to_jsonable(artifact),
            "relative_path": artifact.metadata.get("relative_path", ""),
            "exists": False,
            "is_file": False,
            "size_bytes": 0,
            "content_type": "application/octet-stream",
            "previewable": False,
            "error": str(error),
        }


def _scheduler_task_view(state: Any, store: SQLiteStore) -> dict[str, Any]:
    events = store.task_events(state.task_id)
    memory_records = store.task_memory_records(state.task_id)
    scheduler = ResourceScheduler()
    execute_node = _stage_node(state, "execute")
    preview = scheduler.decide(
        state,
        node=execute_node,
        events=events,
        memory_records=memory_records,
    )
    decisions = list(state.metadata.get("resource_decisions", []))
    return {
        "task_id": state.task_id,
        "run_id": state.run_id,
        "manifests": [to_jsonable(manifest) for manifest in WorkerPool().manifests()],
        "health": [to_jsonable(item) for item in WorkerPool().health_snapshot(state=state, events=events)],
        "latest_resource_decision": state.metadata.get("last_resource_decision"),
        "resource_decisions": decisions,
        "preview_decision": to_jsonable(preview),
        "source_to_target_ledger": source_to_target_ledger(),
        "event_counts": _event_counts(events),
        "memory_record_count": len(memory_records),
    }


def _control_command_request_from_text(state: Any, text: str, payload: dict[str, Any]) -> ControlCommandRequest | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    name, _, raw = stripped.partition(" ")
    registry = get_control_command_registry()
    descriptor = registry.get(name)
    if descriptor is None:
        return None
    arguments = dict(payload.get("arguments") or {})
    arguments.setdefault("raw", raw.strip())
    arguments.setdefault("argv", [item for item in raw.split() if item])
    if descriptor.handler_id == "side_question.ask":
        arguments.setdefault("question", raw.strip())
    session_id = str(
        payload.get("session_id")
        or state.metadata.get("query_session_id")
        or state.metadata.get("session_id")
        or f"task:{state.task_id}"
    )
    return ControlCommandRequest(
        run_id=state.run_id,
        task_id=state.task_id,
        session_id=session_id,
        canonical_name=descriptor.canonical_name,
        arguments=arguments,
        request_id=str(payload.get("request_id") or new_id("controlreq")),
        command_id=str(payload.get("command_id") or new_id("cmd")),
        idempotency_key=str(payload.get("idempotency_key") or ""),
        target_subagent_task_id=str(payload.get("target_subagent_task_id") or ""),
        origin=CommandOrigin.API,
        registry_generation=registry.generation,
        expected_session_revision=(
            int(payload["expected_session_revision"])
            if payload.get("expected_session_revision") is not None
            else None
        ),
        metadata={
            "actor_id": str(payload.get("actor_id") or "api-user"),
            "permission_authority": "PermissionStateStore deterministic control allowlist",
        },
    )


def _control_context_for_task(state: Any, store: SQLiteStore) -> RuntimeControlContext:
    def revision(_session_id: str) -> int:
        return len(store.task_events(state.task_id)) + len(state.metadata.get("control_mutations") or ())

    def checkpoint(request: ControlCommandRequest, descriptor: Any) -> str:
        store.save_checkpoint(state)
        return f"sqlite-task:{state.task_id}:{revision(request.session_id)}:{descriptor.mutation_scope.value}"

    def permission_authorize(request: ControlCommandRequest, descriptor: Any) -> bool:
        # Interactive command permission remains under the 03A state owner.
        # Foundation exposes only a deterministic low-risk allowlist; commands
        # that would change permission, provider, MCP, plugin or session custody
        # fail closed until their canonical owner supplies an exact handler/grant.
        get_permission_control_plane().state_store.snapshot()
        raw = str(request.arguments.get("raw") or "").strip().lower()
        if descriptor.handler_id in {"mcp.control", "permission.control", "provider.model"} and raw in {"", "status", "list", "show"}:
            return True
        return descriptor.permission_action in {
            "task.goal",
            "context.compact",
            "session.clear",
            "artifact.write",
            "task.change",
            "task.inject",
            "artifact.export",
            "task.evaluate",
        }

    handlers: dict[str, Any] = {}

    def read_projection(request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        legacy = parse_slash_command(
            f"{request.canonical_name} {request.arguments.get('raw', '')}".strip(),
            run_id=state.run_id,
            task_id=state.task_id,
            registry=get_runtime_command_registry(),
        )
        if legacy is None:
            raise RuntimeError(f"read projection is unavailable for {request.canonical_name}")
        event = control_event_from_command(legacy.control_command, node_id=state.root_node_id)
        projected = _command_result_for_event(state, event, store)
        projected_data = dict(projected.get("data") or {})
        if request.canonical_name == "/context":
            projected_data["control_commands"] = sum(
                1 for item in store.task_events(state.task_id)
                if item.get("event_type") in {"command_requested", "control_command"}
            )
        return ControlResult(
            display_text=str(projected.get("summary") or request.canonical_name),
            data=projected_data,
            metadata={"legacy_parser_only": True, "event_only_stateful_fallback": False},
        )

    for handler_id in {
        "session.context",
        "usage.cost",
        "usage.inspect",
        "runtime.doctor",
        "task.status",
        "task.graph",
        "task.trace",
        "scheduler.inspect",
        "artifact.list",
        "skill.list",
        "tool.list",
        "memory.inspect",
    }:
        handlers[handler_id] = read_projection

    def registry_help(_request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        get_command_source_coordinator().refresh_all()
        snapshot = get_control_command_registry().snapshot()
        groups: dict[str, list[str]] = {}
        for item in snapshot.descriptors:
            groups.setdefault(item.category, []).append(item.canonical_name)
        # Compatibility group aliases remain projections only; the dynamic
        # registry categories above are the canonical source of truth.
        groups.setdefault("context_session", ["/context", "/compact", "/btw"])
        groups.setdefault("extension_team", ["/goal", "/team-onboarding", "/agents"])
        return ControlResult(
            display_text=f"{len(snapshot.descriptors)} commands available.",
            data={**snapshot.to_dict(), "groups": groups},
        )

    handlers["registry.help"] = registry_help

    def subagent_inspect(_request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        snapshot = get_subagent_runtime().snapshot()
        tasks = [item for item in snapshot.tasks if item.get("parent_task_id") == state.task_id]
        return ControlResult(
            display_text=f"{len(tasks)} logical subagent tasks.",
            data={**snapshot.to_dict(), "tasks": tasks},
            metadata={"physical_worker_state_owned": False},
        )

    handlers["subagent.inspect"] = subagent_inspect

    def mcp_owner(*, action: str, arguments: Any, request: Any) -> dict[str, Any]:
        if action not in {"status", "health", "servers", "server", "catalog", "tools", "resources", "prompts", "tasks", "elicitations"}:
            raise RuntimeError("mutating MCP control requires an exact McpClientRuntime authorization")
        target = str(arguments.get("target") or "").strip()
        raw = " ".join(item for item in (action, target) if item)
        control_result = get_mcp_command_adapter().execute(
            "/mcp",
            raw,
            context=McpControlContext(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=str(state.root_node_id or ""),
                session_id=str(state.metadata.get("code_worker_session_id") or ""),
                worker_request_id=str(request.request_id),
                tool_use_id=str(request.command_id),
                actor_id=str(request.metadata.get("actor_id") or "control-command"),
                cause_event_id=str(request.request_id),
            ),
        )
        payload = control_result.safe_dict()
        return {
            "ok": control_result.ok,
            "summary": control_result.summary,
            **dict(control_result.data),
            "control": payload,
            "runtime_status": "live" if control_result.ok else "blocked",
            "owner_slice": "M1-S03B-02",
            "requires_node_sidecar": False,
            "sidecar_contracts_used": False,
        }

    def permission_owner(*, action: str, arguments: Any, request: Any) -> dict[str, Any]:
        if action not in {"inspect", "status", "list", "rules", "requests", "decisions", "mode"}:
            raise RuntimeError("mutating permission control requires an exact PermissionStateStore authorization")
        plane = get_permission_control_plane()
        permission_requests = [
            item
            for item in plane.state_store.list_requests()
            if item.task_id == state.task_id and item.run_id == state.run_id
        ]
        status_counts: dict[str, int] = {}
        for item in permission_requests:
            status = item.status.value
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "ok": True,
            "summary": "Permission runtime summary; session details require custody.",
            "action": action,
            "legacy_json_store_authority": False,
            "request_count": len(permission_requests),
            "request_status_counts": status_counts,
            "session_count": len({item.session_id for item in permission_requests}),
            "custody_required_for_details": True,
            "structured_routes": [
                "/permissions/requests",
                "/permissions/rules",
                "/permissions/mode",
                "/permissions/decisions",
            ],
        }

    def model_owner(*, action: str, arguments: Any, request: Any) -> dict[str, Any]:
        if action != "inspect":
            raise RuntimeError("model mutation requires an exact session-owner authorization")
        return {
            "ok": True,
            "summary": "Current session model.",
            "model": str(state.metadata.get("model") or os.environ.get("ZYRA_MODEL", "unconfigured")),
            "effort": str(state.metadata.get("effort") or "default"),
            "session_revision": revision(request.session_id),
        }

    def plugin_owner(*, action: str, arguments: Any, request: Any) -> dict[str, Any]:
        if action not in {"inspect", "status", "list", "hooks", "plugins"}:
            raise RuntimeError("plugin mutation requires an exact SkillPluginRuntime authorization")
        snapshot = default_skill_runtime().plugin_runtime.snapshot()
        return {
            "ok": True,
            "summary": "Plugin capability state.",
            "generation": snapshot.generation,
            "plugins": snapshot.plugins,
            "errors": list(snapshot.errors),
        }

    def subagent_owner(*, action: str, arguments: Any, request: Any) -> dict[str, Any]:
        task_id = str(arguments.get("task_id") or "")
        if action not in {"status", "inspect"}:
            raise RuntimeError("mutating subagent control requires an exact SubagentTaskStore authorization")
        record = get_subagent_runtime().task_store.get(task_id)
        if record is None:
            raise RuntimeError(f"logical subagent task not found: {task_id}")
        if record.parent_task_id != state.task_id:
            raise RuntimeError("logical subagent does not belong to this parent task")
        return {"ok": True, "summary": "Logical subagent state.", "task": record.safe_dict()}

    def session_snapshot(*, run_id: str, task_id: str, session_id: str) -> Any:
        if run_id != state.run_id or task_id != state.task_id:
            raise RuntimeError("canonical session owner identity mismatch")
        effective_session = str(state.metadata.get("query_session_id") or f"task:{state.task_id}")
        if session_id != effective_session:
            raise RuntimeError("canonical session owner does not own the requested session")
        session_revision = int(state.metadata.get("session_control_revision") or revision(session_id))
        epoch = int(state.metadata.get("session_epoch") or 0)
        session_state = {
            "model": str(state.metadata.get("model") or ""),
            "effort": str(state.metadata.get("effort") or ""),
            "thinking": str(state.metadata.get("thinking") or ""),
            "context_epoch": int(state.metadata.get("context_epoch") or 0),
            "compact_boundary_id": str(state.metadata.get("compact_boundary_id") or ""),
            "active": True,
            "message_count": len(state.metadata.get("main_messages") or ()),
        }
        return snapshot_from_state(
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            revision=session_revision,
            epoch=epoch,
            state=session_state,
            checkpoint_ref=f"sqlite-session:{task_id}:{session_revision}:{epoch}",
            transcript=tuple(state.metadata.get("main_messages") or ()),
            metadata={"owner": "SQLiteStore", "owner_unit": "M1-S03D-02"},
        )

    def session_mutation(request: Any, before: Any) -> dict[str, Any]:
        if request.action is not SessionAction.CLEAR:
            raise RuntimeError(f"session action is not connected to this canonical owner: {request.action.value}")
        messages = list(state.metadata.get("main_messages") or ())
        checkpoint = {
            "checkpoint_ref": before.checkpoint_ref,
            "session_id": before.session_id,
            "revision": before.revision,
            "epoch": before.epoch,
            "messages": messages,
            "context_epoch": int(state.metadata.get("context_epoch") or 0),
            "compact_boundary_id": str(state.metadata.get("compact_boundary_id") or ""),
        }
        state.metadata.setdefault("session_control_checkpoints", []).append(checkpoint)
        state.metadata["main_messages"] = []
        state.metadata["session_epoch"] = before.epoch + 1
        state.metadata["context_epoch"] = int(state.metadata.get("context_epoch") or 0) + 1
        state.metadata["compact_boundary_id"] = ""
        state.metadata["session_control_revision"] = before.revision + 1
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            event_type=EventType.COMMAND_SUCCEEDED,
            payload={
                "schema": "zyra.session-clear/v1",
                "request_id": request.request_id,
                "session_id": before.session_id,
                "before_checkpoint": before.checkpoint_ref,
                "before_message_count": len(messages),
                "after_epoch": before.epoch + 1,
            },
        )
        persist_events(store, [event])
        store.save_checkpoint(state)
        return {
            "changed": True,
            "session_id": before.session_id,
            "revision": before.revision + 1,
            "checkpoint_ref": f"sqlite-session:{state.task_id}:{before.revision + 1}:{before.epoch + 1}",
            "result": {"cleared_messages": len(messages), "checkpoint": checkpoint},
            "event_ids": [event.event_id],
            "metadata": {"same_session_new_epoch": True, "state_owner": "SQLiteStore"},
        }

    session_runtime = SessionControlRuntime(
        SessionControlStore(control_state_path() / "sessions" / f"{state.task_id}.json"),
        CallbackSessionOwner(session_snapshot, session_mutation),
    )

    def owner_authorizer(owner: str, action: str, request: Any, arguments: Any) -> Any:
        granted = owner == "CanonicalSessionStore" and action == SessionAction.CLEAR.value and request.canonical_name == "/clear"
        return deterministic_owner_authorization(
            owner=owner,
            action=action,
            request=request,
            actor_id=str(request.metadata.get("actor_id") or "api-user"),
            authority={
                "source": "RuntimeControlDispatcher.permission_authorize",
                "permission_action": "session.clear",
                "request_id": request.request_id,
            },
            granted=granted,
            reason="explicit /clear is a checkpoint-before-reset canonical session action" if granted else "owner action not granted",
        )

    owner_handlers = CanonicalOwnerHandlerSet(CommandOwnerServices(
        session=session_runtime,
        mcp=mcp_owner,
        permission=permission_owner,
        model=model_owner,
        plugin=plugin_owner,
        subagent=subagent_owner,
        authorizer=owner_authorizer,
        registry_refresh=lambda: {
            "receipts": [item.to_dict() for item in get_command_source_coordinator().refresh_all()],
            "snapshot": get_command_source_coordinator().snapshot(),
        },
    )).handlers()
    for handler_id in {
        "session.clear",
        "mcp.control",
        "permission.control",
        "provider.model",
        "skill_plugin.hooks",
        "subagent.control",
    }:
        handlers[handler_id] = owner_handlers[handler_id]

    def compact_context(request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        result = _memory_fabric(store).compact_context(
            state,
            store.task_events(state.task_id),
            focus=str(request.arguments.get("raw") or ""),
            persist=True,
        )
        _attach_artifacts(state, result.artifacts)
        _record_compaction_metadata(state, result, source_event_id=request.request_id)
        state.metadata.setdefault("control_mutations", []).append({
            "request_id": request.request_id,
            "command": request.canonical_name,
            "compact_id": result.compact_id,
        })
        store.save_checkpoint(state)
        return ControlResult(
            display_text="Context compact summary artifact written.",
            data={
                "compact": to_jsonable(result),
                "artifact": (
                    _artifact_entry(LocalArtifactStore(artifact_root_path()), result.artifacts[-1])
                    if result.artifacts else None
                ),
            },
            artifact_refs=tuple(result.artifacts),
        )

    handlers["session.compact"] = compact_context

    def task_goal(request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        value = str(request.arguments.get("goal") or request.arguments.get("raw") or "").strip()
        if not value:
            return ControlResult(display_text="Current task objective.", data={"goal": state.user_goal})
        previous = state.user_goal
        state.user_goal = value
        state.metadata.setdefault("control_mutations", []).append({
            "request_id": request.request_id,
            "command": request.canonical_name,
            "before": previous,
            "after": value,
        })
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            event_type=EventType.REQUIREMENT_CHANGE,
            payload={
                "schema": "zyra.control-goal/v1",
                "request_id": request.request_id,
                "previous_goal": previous,
                "goal": value,
            },
        )
        persist_events(store, [event])
        store.save_checkpoint(state)
        return ControlResult(
            display_text="Root task objective updated.",
            data={"previous_goal": previous, "goal": value, "event_id": event.event_id},
        )

    handlers["task.goal"] = task_goal

    def legacy_real_mutation(request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        legacy = parse_slash_command(
            f"{request.canonical_name} {request.arguments.get('raw', '')}".strip(),
            run_id=state.run_id,
            task_id=state.task_id,
            registry=get_runtime_command_registry(),
        )
        if legacy is None:
            raise RuntimeError(f"canonical mutation owner is unavailable for {request.canonical_name}")
        event = control_event_from_command(legacy.control_command, node_id=state.root_node_id)
        applied = _apply_control_event_to_state(state, event)
        persist_events(store, [event, *applied])
        projected = _command_result_for_event(state, event, store)
        state.metadata.setdefault("control_mutations", []).append({
            "request_id": request.request_id,
            "command": request.canonical_name,
            "event_id": event.event_id,
        })
        store.save_checkpoint(state)
        return ControlResult(
            display_text=str(projected.get("summary") or request.canonical_name),
            data={**dict(projected.get("data") or {}), "control_event": to_jsonable(event)},
            metadata={"runtime_status": str(projected.get("runtime_status") or "stateful")},
        )

    handlers["task.change"] = legacy_real_mutation
    handlers["task.inject"] = legacy_real_mutation
    handlers["artifact.export"] = legacy_real_mutation
    handlers["task.evaluate"] = legacy_real_mutation

    def onboarding(request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        events = store.task_events(state.task_id)
        content = "\n".join([
            f"# Team onboarding: {state.user_goal}",
            "",
            f"- task_id: `{state.task_id}`",
            f"- run_id: `{state.run_id}`",
            f"- status: `{state.status}`",
            f"- plan nodes: `{len(state.plan_nodes)}`",
            f"- canonical events: `{len(events)}`",
            f"- logical subagents: `{len(get_subagent_runtime().task_store.list(parent_task_id=state.task_id))}`",
            "",
            "This artifact is generated from live Zyra task state; it is not a static acknowledgement.",
        ])
        artifact = LocalArtifactStore(artifact_root_path()).write_text(
            run_id=state.run_id,
            task_id=state.task_id,
            content=content,
            title="Team onboarding",
            kind=ArtifactKind.DOCUMENT,
            extension=".md",
            producer_node_id=state.root_node_id,
        )
        _attach_artifacts(state, [artifact])
        state.metadata.setdefault("control_mutations", []).append({
            "request_id": request.request_id,
            "command": request.canonical_name,
            "artifact_id": artifact.artifact_id,
        })
        store.save_checkpoint(state)
        return ControlResult(
            display_text="Team onboarding artifact written.",
            data={"artifact": _artifact_entry(LocalArtifactStore(artifact_root_path()), artifact)},
            artifact_refs=(artifact,),
        )

    handlers["task.onboarding"] = onboarding

    def side_question(request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        events = store.task_events(state.task_id)
        messages_before = list(state.metadata.get("main_messages") or ())
        snapshot = SideQuestionContextSnapshot(
            parent_session_id=request.session_id,
            parent_session_revision=revision(request.session_id),
            context_epoch=int(state.metadata.get("context_epoch") or 0),
            compact_boundary_id=str(state.metadata.get("compact_boundary_id") or ""),
            message_ids=tuple(str(item.get("message_id") or "") for item in messages_before if isinstance(item, dict)),
            system_prompt_digest=f"sha256:{hashlib.sha256(str(state.user_goal).encode('utf-8')).hexdigest()}",
            user_context_digest=f"sha256:{hashlib.sha256(json.dumps(events[-20:], sort_keys=True).encode('utf-8')).hexdigest()}",
            model=str(state.metadata.get("model") or os.environ.get("ZYRA_MODEL", "unconfigured")),
            thinking=str(state.metadata.get("thinking") or "default"),
            cache_prefix_digest=str(state.metadata.get("cache_prefix_digest") or ""),
            metadata={"context_summary": f"Task {state.task_id} is {state.status}; goal: {state.user_goal}"},
        )
        runtime = SideQuestionRuntime(
            control_state_path() / "side-questions",
            artifact_store=LocalArtifactStore(artifact_root_path()),
            event_sink=_commit_runtime_event,
        )
        result = runtime.ask(
            run_id=state.run_id,
            task_id=state.task_id,
            question=str(request.arguments.get("question") or request.arguments.get("raw") or ""),
            snapshot=snapshot,
            parent_messages_before=messages_before,
            parent_messages_after=lambda: list(state.metadata.get("main_messages") or ()),
        )
        if not result.ok:
            raise RuntimeError("side question tool or parent-mutation invariant failed")
        return ControlResult(
            display_text=result.answer,
            data=result.to_dict(),
            usage=result.usage.to_dict(),
            metadata={"main_replan_triggered": False, "parent_messages_mutated": False},
        )

    handlers["side_question.ask"] = side_question
    return RuntimeControlContext(
        handlers=handlers,
        session_revision=revision,
        checkpoint=checkpoint,
        permission_authorize=permission_authorize,
        event_sink=_commit_runtime_event,
        metadata={"root_task_id": state.task_id, "state_owner": "SQLiteStore"},
    )


def _recovery_task_view(state: Any, store: SQLiteStore) -> dict[str, Any]:
    events = store.task_events(state.task_id)
    signals = RuntimeWatchdog().scan_events(state, events)
    return {
        "task_id": state.task_id,
        "run_id": state.run_id,
        "recovery_plans": list(state.metadata.get("recovery_plans", [])),
        "last_recovery_plan": state.metadata.get("last_recovery_plan"),
        "failure_injections": list(state.metadata.get("failure_injections", [])),
        "signals": [to_jsonable(signal) for signal in signals],
        "event_counts": _event_counts(events),
    }


def _stage_node(state: Any, stage: str) -> Any | None:
    for node in state.plan_nodes.values():
        if node.metadata.get("stage") == stage:
            return node
    return None


def _event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("event_type") or "unknown")
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _command_result_for_event(state: Any, event: EventRecord, store: SQLiteStore) -> dict[str, Any]:
    command = event.payload.get("command")
    if not isinstance(command, dict):
        return {"ok": False, "summary": "Control event has no command payload.", "data": {}}
    name = str(command.get("name") or "")
    metadata = command.get("metadata") if isinstance(command.get("metadata"), dict) else {}
    category = str(metadata.get("category") or "")
    runtime_status = str(metadata.get("runtime_status") or "event_only")
    result = {
        "ok": True,
        "name": name,
        "category": category,
        "runtime_status": runtime_status,
        "summary": f"{name} recorded as a control command.",
        "data": {},
    }

    if name == "/status":
        result["summary"] = "Current task status."
        result["data"] = {
            "task_id": state.task_id,
            "run_id": state.run_id,
            "status": str(state.status),
            "plan_nodes": len(state.plan_nodes),
            "artifacts": len(state.artifacts),
            "tool_calls": state.budget.tool_calls,
            "updated_at": state.updated_at,
        }
    elif name == "/graph":
        result["summary"] = "Current task graph."
        result["data"] = {"plan_nodes": [to_jsonable(node) for node in state.plan_nodes.values()]}
    elif name == "/trace":
        events = store.task_events(state.task_id)
        result["summary"] = "Recent task events."
        result["data"] = {"events": events[-20:], "event_count": len(events)}
    elif name == "/artifacts":
        catalog = LocalArtifactStore(artifact_root_path())
        result["summary"] = "Task artifacts."
        result["data"] = {"artifacts": [_artifact_entry(catalog, artifact) for artifact in state.artifacts]}
    elif name == "/tools":
        base_context = ToolExecutionContext.for_workspace(
            workspace_root=tool_workspace_path(),
            artifact_root=artifact_root_path(),
            permission_store=get_permission_store(),
        )
        projection = get_mcp_runtime().worker_projection(
            base_context,
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
        )
        result["summary"] = "Registered tools."
        result["data"] = {
            "tools": [to_jsonable(tool) for tool in projection.context.registry.list()],
            "mcp": projection.safe_dict(),
        }
    elif name == "/permissions":
        control_plane = get_permission_control_plane()
        permission_requests = [
            request
            for request in control_plane.state_store.list_requests()
            if request.task_id == state.task_id and request.run_id == state.run_id
        ]
        status_counts: dict[str, int] = {}
        for request in permission_requests:
            status = request.status.value
            status_counts[status] = status_counts.get(status, 0) + 1
        result["summary"] = "Permission runtime summary; session details require custody."
        result["data"] = {
            "state_owner": "PermissionStateStore",
            "legacy_json_store_authority": False,
            "request_count": len(permission_requests),
            "request_status_counts": status_counts,
            "session_count": len({request.session_id for request in permission_requests}),
            "custody_required_for_details": True,
            "structured_routes": [
                "/permissions/requests",
                "/permissions/rules",
                "/permissions/mode",
                "/permissions/decisions",
            ],
        }
    elif name == "/help":
        commands = [to_jsonable(command_spec) for command_spec in default_command_registry().list()]
        result["summary"] = "Available slash commands."
        result["data"] = {
            "commands": commands,
            "groups": _command_groups(commands),
        }
    elif name in {"/cost", "/usage"}:
        result["summary"] = "Task resource usage."
        result["data"] = {
            "budget": to_jsonable(state.budget),
            "scheduler": {
                "resource_decision_count": len(state.metadata.get("resource_decisions", [])),
                "recovery_plan_count": len(state.metadata.get("recovery_plans", [])),
                "latest_resource_decision": state.metadata.get("last_resource_decision"),
            },
        }
    elif name == "/context":
        events = store.task_events(state.task_id)
        session_runtime = ContextSessionRuntime(events)
        memory_snapshot = _memory_fabric(store).refresh_task_memory(state, events, persist=True)
        result["summary"] = "Context and checkpoint pressure summary."
        result["data"] = session_runtime.summarize(state)
        result["data"]["memory"] = {
            "record_count": len(memory_snapshot.records),
            "layer_counts": memory_snapshot.layer_counts(),
        }
    elif name == "/agents":
        result["summary"] = "Registered worker descriptors."
        result["data"] = {
            "workers": [to_jsonable(worker) for worker in default_worker_descriptors()],
            "manifests": [to_jsonable(manifest) for manifest in WorkerPool().manifests()],
            "health": [to_jsonable(item) for item in WorkerPool().health_snapshot(state=state, events=store.task_events(state.task_id))],
        }
    elif name == "/scheduler":
        result["summary"] = "M5 resource scheduler, worker manifests, and recovery state."
        result["data"] = _scheduler_task_view(state, store)
    elif name == "/mcp" or str(metadata.get("command_kind") or "") == "mcp_prompt":
        arguments = command.get("arguments")
        raw = str(arguments.get("raw") or "") if isinstance(arguments, dict) else ""
        control_result = get_mcp_command_adapter().execute(
            name,
            raw,
            context=McpControlContext(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=str(state.root_node_id or ""),
                session_id=str(state.metadata.get("code_worker_session_id") or ""),
                worker_request_id=str(event.event_id),
                tool_use_id=str(event.event_id),
                actor_id="control-command",
                cause_event_id=str(event.event_id),
            ),
        )
        control_payload = control_result.safe_dict()
        result["ok"] = control_result.ok
        result["summary"] = control_result.summary
        result["runtime_status"] = "live" if control_result.ok else "blocked"
        result["data"] = {
            **dict(control_result.data),
            "control": control_payload,
            "runtime_status": result["runtime_status"],
            "owner_slice": "M1-S03B-02",
            "state_owner": "McpClientRuntime",
            "permission_owner": "ToolPermissionRuntime",
            "requires_node_sidecar": False,
            "sidecar_contracts_used": False,
        }
    elif name == "/skills":
        skill_runtime = default_skill_runtime()
        result["summary"] = "Versioned skills and the task-scoped invocation projection."
        result["data"] = {
            "skills": [to_jsonable(skill) for skill in skill_runtime.list()],
            "registry": skill_runtime.registry.snapshot().to_dict(),
            "skill_invocation": dict(state.metadata.get("skill_invocation_projection") or {}),
            "runtime_state": dict(state.metadata.get("skill_runtime_state") or {}),
            "body_in_projection": False,
            "owner_slice": "M1-S03C-01",
            "state_owner": "SkillInvocationStateStore",
            "permission_owner": "ToolPermissionRuntime",
        }
    elif name == "/doctor":
        result["summary"] = "Development runtime health checks."
        result["data"] = {
            "project_root_exists": PROJECT_ROOT.exists(),
            "vendor_claude_code_best_exists": (PROJECT_ROOT / "vendor" / "claude-code-best").exists(),
            "vendor_browser_use_exists": (PROJECT_ROOT / "vendor" / "browser-use").exists(),
            "browser_use_runtime": browser_use_health_summary(inspect_browser_use_runtime(PROJECT_ROOT)),
            "tool_workspace": str(tool_workspace_path()),
            "artifact_root": str(artifact_root_path()),
            "permission_store": str(permission_store_path()),
            "scheduler_health": [to_jsonable(item) for item in WorkerPool().health_snapshot(state=state, events=store.task_events(state.task_id))],
            "scheduler_ledger_entries": source_to_target_ledger(),
        }
    elif name == "/model":
        result["summary"] = "Configured model/provider environment."
        result["data"] = {
            "provider": os.environ.get("ZYRA_MODEL_PROVIDER", "local-or-unconfigured"),
            "model": os.environ.get("ZYRA_MODEL", "unconfigured"),
        }
    elif name == "/bashes":
        result["summary"] = "No background shell task registry is active yet."
        result["data"] = {"background_tasks": []}
    elif name == "/compact":
        result.update(_compact_task_context(state, event, store))
    elif name == "/export":
        result.update(_export_task_run(state, event, store))
    elif name == "/clear":
        result.update(ContextSessionRuntime(store.task_events(state.task_id)).clear(state, event))
    elif name == "/rewind":
        result.update(
            ContextSessionRuntime(store.task_events(state.task_id)).rewind(
                state,
                event,
                target=str(event.payload.get("raw") or ""),
            )
        )
    elif name == "/resume":
        result.update(
            ContextSessionRuntime(store.task_events(state.task_id)).resume(
                state,
                event,
                target=str(event.payload.get("raw") or ""),
            )
        )
    elif name == "/memory":
        memory = _memory_fabric(store).memory_view(
            state,
            store.task_events(state.task_id),
            query=str(event.payload.get("raw") or ""),
        )
        result.update(memory)
    elif name in {"/verify", "/eval"}:
        evaluation = evaluate_task_trace(to_jsonable(state), store.task_events(state.task_id))
        evaluations = state.metadata.setdefault("evaluations", [])
        evaluations.append(
            {
                "event_id": event.event_id,
                "name": name,
                "score": evaluation["score"],
                "created_at": event.created_at,
            }
        )
        result["summary"] = "Trace evaluation completed."
        result["data"] = evaluation
    elif name == "/change":
        latest = _latest_metadata_item(state, "requirement_changes")
        result["summary"] = "/change associated affected PlanNodes and created a local replan route."
        result["data"] = {
            "event_id": event.event_id,
            "raw": event.payload.get("raw", ""),
            "requirement_change": latest,
            "latest_decision": to_jsonable(state.decisions[-1]) if state.decisions else None,
            "latest_resource_decision": state.metadata.get("last_resource_decision"),
        }
    elif name == "/inject":
        latest = _latest_metadata_item(state, "failure_injections")
        result["summary"] = "/inject produced a structured failure recovery route."
        result["data"] = {
            "event_id": event.event_id,
            "raw": event.payload.get("raw", ""),
            "failure_injection": latest,
            "latest_decision": to_jsonable(state.decisions[-1]) if state.decisions else None,
            "latest_resource_decision": state.metadata.get("last_resource_decision"),
            "latest_recovery_plan": state.metadata.get("last_recovery_plan"),
        }
    elif name in {"/init", "/hooks", "/plan", "/goal", "/team-onboarding"}:
        result["summary"] = f"{name} accepted and recorded for downstream runtime handling."
        result["data"] = {
            "event_id": event.event_id,
            "raw": event.payload.get("raw", ""),
        }

    return result


def _compact_task_context(state: Any, event: EventRecord, store: SQLiteStore) -> dict[str, Any]:
    events = store.task_events(state.task_id)
    focus = str(event.payload.get("raw") or "").strip()
    result = _memory_fabric(store).compact_context(
        state,
        events,
        focus=focus,
        source_event_id=event.event_id,
        persist=True,
    )
    _attach_artifacts(state, result.artifacts)
    _record_compaction_metadata(state, result, source_event_id=event.event_id)
    return {
        "summary": "Context compact summary artifact written.",
        "data": {
            "artifact": (
                _artifact_entry(LocalArtifactStore(artifact_root_path()), result.artifacts[-1])
                if result.artifacts
                else None
            ),
            "compact": to_jsonable(result),
            "event_count": len(events),
            "focus": focus,
        },
    }


def _attach_artifacts(state: Any, artifacts: list[ArtifactRef]) -> None:
    existing_ids = {artifact.artifact_id for artifact in state.artifacts}
    for artifact in artifacts:
        if artifact.artifact_id not in existing_ids:
            state.artifacts.append(artifact)
            existing_ids.add(artifact.artifact_id)


def _record_compaction_metadata(state: Any, result: Any, *, source_event_id: str) -> None:
    compactions = state.metadata.setdefault("compactions", [])
    compactions.append(
        {
            "event_id": source_event_id,
            "compact_id": result.compact_id,
            "artifact_ids": list(result.artifact_ids),
            "artifact_id": result.artifact_ids[-1] if result.artifact_ids else "",
            "focus": result.focus,
            "event_count": len(result.preserved_event_ids) + len(result.summarized_event_ids),
            "preserved_event_count": len(result.preserved_event_ids),
            "summarized_event_count": len(result.summarized_event_ids),
            "memory_record_count": len(result.memory_ids),
            "compression_ratio": result.compression_ratio,
            "created_at": result.created_at,
            "source": "MemoryFabric",
        }
    )


def _record_code_worker_session_metadata(state: Any, run_result: Any) -> None:
    metadata = dict(getattr(run_result.worker_result, "metadata", {}) or {})
    session_id = metadata.get("query_session_id")
    if not session_id:
        return
    session_events = [
        event
        for event in getattr(run_result, "event_records", [])
        if isinstance(getattr(event, "payload", None), dict) and "query_session" in event.payload
    ]
    snapshot_events = [
        event.payload["query_session"]
        for event in session_events
        if event.payload.get("query_session", {}).get("phase") == "query_session_snapshot"
    ]
    latest_snapshot = snapshot_events[-1] if snapshot_events else {}
    record = {
        "session_id": session_id,
        "resume_token": metadata.get("query_session_resume_token", ""),
        "snapshot_artifact_id": metadata.get("query_session_snapshot_artifact_id", ""),
        "transcript_artifact_id": metadata.get("query_session_transcript_artifact_id", ""),
        "trace_artifact_id": metadata.get("trace_artifact_id", ""),
        "turn_count": metadata.get("query_session_turns", metadata.get("query_turns", "0")),
        "message_count": metadata.get("query_session_messages", "0"),
        "transcript_entry_count": metadata.get("query_session_transcript_entries", "0"),
        "consistent": metadata.get("query_session_consistent", "false"),
        "leaf_uuid": metadata.get("query_session_leaf_uuid", ""),
        "worker_request_id": run_result.worker_result.request_id,
        "event_ids": [event.event_id for event in session_events],
        "snapshot_event": latest_snapshot,
        "compact_restore": {
            "ok": metadata.get("compact_restore_ok", ""),
            "status": metadata.get("compact_restore_status", ""),
            "boundary_id": metadata.get("compact_restore_boundary_id", ""),
            "restore_contract_id": metadata.get("compact_restore_contract_id", ""),
            "segments": metadata.get("compact_restore_segments", ""),
            "preserved_segments": metadata.get("compact_restore_preserved_segments", ""),
        },
        "restore_integration": {
            "ok": metadata.get("restore_integration_ok", ""),
            "status": metadata.get("restore_integration_status", ""),
            "applications": metadata.get("restore_integration_applications", ""),
            "model_messages": metadata.get("restore_integration_model_messages", ""),
            "context_blocks": metadata.get("restore_integration_context_blocks", ""),
            "latest_contract_id": metadata.get("restore_integration_latest_contract_id", ""),
            "untrusted_messages": metadata.get("restore_integration_untrusted_messages", ""),
            "redacted_messages": metadata.get("restore_integration_redacted_messages", ""),
        },
        "context_security": {
            "ok": metadata.get("context_security_ok", ""),
            "status": metadata.get("context_security_status", ""),
            "snapshot_id": metadata.get("context_security_snapshot_id", ""),
            "verdicts": metadata.get("context_security_verdicts", ""),
            "redactions": metadata.get("context_security_redactions", ""),
            "untrusted": metadata.get("context_security_untrusted", ""),
        },
        "model_api": {
            "model_stream_status": metadata.get("model_stream_status", ""),
            "model_stream_error_kind": metadata.get("model_stream_error_kind", ""),
            "api_retry_status": metadata.get("api_retry_status", ""),
            "api_retry_fallback_used": metadata.get("api_retry_fallback_used", ""),
            "api_retry_final_model": metadata.get("api_retry_final_model", ""),
            "model_stream_watchdog_status": metadata.get("model_stream_watchdog_status", ""),
        },
        "source": "CodeWorkerRuntime",
    }
    sessions = state.metadata.setdefault("code_worker_sessions", [])
    sessions.append(record)
    state.metadata["last_code_worker_session"] = record


def _compact_policy_from_payload(payload: dict[str, Any]) -> CompactPolicy:
    return CompactPolicy(
        max_active_tokens=_bounded_int(payload.get("max_active_tokens"), default=8000, minimum=1000, maximum=200000),
        tail_groups=_bounded_int(payload.get("tail_groups"), default=8, minimum=1, maximum=100),
        max_inline_chars=_bounded_int(payload.get("max_inline_chars"), default=3000, minimum=500, maximum=50000),
    )


def _export_task_run(state: Any, event: EventRecord, store: SQLiteStore) -> dict[str, Any]:
    events = store.task_events(state.task_id)
    export_payload = {
        "task": to_jsonable(state),
        "events": events,
        "control_commands": state.metadata.get("control_commands", []),
        "source_event_id": event.event_id,
    }
    artifact = LocalArtifactStore(artifact_root_path()).write_text(
        run_id=state.run_id,
        task_id=state.task_id,
        content=json.dumps(export_payload, ensure_ascii=False, indent=2),
        title="Run export",
        kind=ArtifactKind.STRUCTURED_DATA,
        extension=".json",
        producer_node_id=event.node_id,
    )
    state.artifacts.append(artifact)
    exports = state.metadata.setdefault("exports", [])
    exports.append(
        {
            "event_id": event.event_id,
            "artifact_id": artifact.artifact_id,
            "event_count": len(events),
            "created_at": event.created_at,
        }
    )
    return {
        "summary": "Run export artifact written.",
        "data": {
            "artifact": _artifact_entry(LocalArtifactStore(artifact_root_path()), artifact),
            "event_count": len(events),
        },
    }


def _command_groups(commands: list[dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for command in commands:
        metadata = command.get("metadata") if isinstance(command.get("metadata"), dict) else {}
        category = str(metadata.get("category") or "uncategorized")
        groups.setdefault(category, []).append(str(command.get("name") or ""))
    return groups


def _latest_metadata_item(state: Any, key: str) -> dict[str, Any]:
    items = state.metadata.get(key)
    if isinstance(items, list) and items and isinstance(items[-1], dict):
        return dict(items[-1])
    return {}


def _apply_control_event_to_state(state: Any, event: EventRecord) -> list[EventRecord]:
    command = event.payload.get("command")
    if isinstance(command, dict):
        controls = state.metadata.setdefault("control_commands", [])
        controls.append(
            {
                "event_id": event.event_id,
                "command_id": command.get("command_id", ""),
                "name": command.get("name", ""),
                "arguments": command.get("arguments", {}),
                "metadata": command.get("metadata", {}),
                "created_at": event.created_at,
            }
        )
    applied_events: list[EventRecord] = []
    if event.event_type == EventType.REQUIREMENT_CHANGE:
        applied_events.extend(apply_requirement_change(state, event))
    elif event.event_type == EventType.FAILURE_INJECTED:
        applied_events.extend(apply_failure_injection(state, event))
    state.updated_at = event.created_at
    return applied_events


if __name__ == "__main__":
    run()
