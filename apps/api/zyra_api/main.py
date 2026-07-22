from __future__ import annotations

import hashlib
import json
import os
import threading
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
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
    PROJECT_ROOT / "packages" / "code_index",
]

# Dynamic task identifiers make these routes invisible to literal path
# comparisons.  This manifest is kept beside the handlers so submission and
# reachability audits can verify the public surface without importing the API.
ZYRA_DYNAMIC_API_ROUTES = (
    ("POST", "/tasks/{task_id}/workers/browser"),
    ("GET", "/tasks/{task_id}/memory/curator"),
    ("POST", "/tasks/{task_id}/memory/curator"),
    ("POST", "/tasks/{task_id}/memory/curator/task-end"),
    ("POST", "/tasks/{task_id}/memory/curator/recover"),
    ("GET", "/tasks/{task_id}/memory/procedures"),
    ("POST", "/tasks/{task_id}/memory/procedures/mine"),
    ("POST", "/tasks/{task_id}/memory/procedures/routing"),
    ("POST", "/tasks/{task_id}/memory/procedures/recovery"),
    ("POST", "/tasks/{task_id}/memory/procedures/context"),
    ("GET", "/tasks/{task_id}/faults"),
    ("POST", "/tasks/{task_id}/faults/inject"),
    ("POST", "/tasks/{task_id}/faults/observers"),
    ("POST", "/tasks/{task_id}/faults/sources/bind"),
    ("POST", "/tasks/{task_id}/faults/sources/observe"),
    ("POST", "/tasks/{task_id}/faults/runtime-events"),
    ("POST", "/tasks/{task_id}/faults/observations"),
    ("POST", "/tasks/{task_id}/faults/handoffs/dispatch"),
)

for package_path in PACKAGE_PATHS:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import (
    AgentMessage,
    AgentRole,
    ArtifactKind,
    ArtifactRef,
    ControlCommand,
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
from zyra_memory import (
    CompactPolicy,
    MemoryFabric,
    MemoryIndexRuntime,
    MemoryIndexWorkerProcessSupervisor,
    ProcedureContractError,
    ReusableProcedureRuntime,
    ReusableProcedureStore,
    RetrievalIntegrationRuntime,
    SQLiteStore,
)
from zyra_code_index import (
    CodeIndexApiService,
    CodeIndexError,
    CodeIndexIntegrationRuntime,
    CodeIndexOperation,
    CodeIndexRuntimeRegistry,
    CodeIndexServiceError,
    CodeIndexWorkerProcessSupervisor,
)
from zyra_orchestration import GraphExecutionContext, cancel_task_graph, ensure_default_graph, run_task_graph
from zyra_symbolic import apply_failure_injection, apply_requirement_change
from zyra_scheduler import (
    ResourceScheduler,
    WorkerPool,
    backend_registry_path,
    cancel_pending_dispatches,
    source_to_target_ledger,
)
from zyra_scheduler.fault_runtime import (
    FaultApiError,
    FaultRuntimeApiService,
    FaultRuntimeApplication,
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
    WatchdogControlCommandRuntime,
    default_control_command_registry,
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
import threading as _runtime_event_threading
from pathlib import Path as _RuntimeEventPath

from zyra_runtime.runtime_events import (
    RuntimeEventApiFacade,
    RuntimeEventProcessError,
    get_runtime_event_spine,
    release_runtime_event_spine,
)

_RUNTIME_EVENT_SPINE_LOCK = _runtime_event_threading.RLock()
_RUNTIME_EVENT_SPINE = None
_RUNTIME_EVENT_SPINE_KEY = None


def get_runtime_event_spine_bridge():
    """Return the unique TypeScript event spine bound to the API database."""

    global _RUNTIME_EVENT_SPINE, _RUNTIME_EVENT_SPINE_KEY
    database = _RuntimeEventPath(sqlite_path()).expanduser().resolve()
    artifact_root = database.parent / "runtime-event-artifacts"
    key = (str(database), str(artifact_root))
    with _RUNTIME_EVENT_SPINE_LOCK:
        if _RUNTIME_EVENT_SPINE is None or _RUNTIME_EVENT_SPINE_KEY != key:
            if _RUNTIME_EVENT_SPINE is not None:
                release_runtime_event_spine(_RUNTIME_EVENT_SPINE)
            _RUNTIME_EVENT_SPINE = get_runtime_event_spine(
                database_path=database,
                artifact_root=artifact_root,
            )
            _RUNTIME_EVENT_SPINE_KEY = key
        return _RUNTIME_EVENT_SPINE


def get_runtime_event_api() -> RuntimeEventApiFacade:
    return RuntimeEventApiFacade(get_runtime_event_spine_bridge())


def reset_runtime_event_spine_bridge() -> None:
    """Close and forget the API-owned event sidecar and its SQLite handle."""

    global _RUNTIME_EVENT_SPINE, _RUNTIME_EVENT_SPINE_KEY
    with _RUNTIME_EVENT_SPINE_LOCK:
        bridge = _RUNTIME_EVENT_SPINE
        _RUNTIME_EVENT_SPINE = None
        _RUNTIME_EVENT_SPINE_KEY = None
        if bridge is not None:
            # Keep the API lock through registry eviction and close. Otherwise
            # a concurrent getter can reacquire the still-registered bridge
            # between clearing this cache and release, then receive an object
            # that this reset immediately closes.
            release_runtime_event_spine(bridge)


from zyra_runtime import (
    ContextSessionRuntime,
    JsonPermissionStore,
    LocalArtifactStore,
    PermissionEffect,
    ToolCall,
    ToolExecutionContext,
    ToolLoopScheduler,
    WorkerRequest,
    control_event_from_command,
    default_tool_registry,
    default_worker_descriptors,
    tool_result_event,
)
from zyra_runtime.claude_session_api_projection import SessionApiProjectionBuilder
from zyra_runtime.claude_session_lifecycle_state import SessionLifecycleRuntime
from zyra_runtime.claude_session_lineage_runtime import SessionLineageRuntime
from zyra_runtime.codeworker_task_api_contract import (
    CodeWorkerTaskApiContractRuntime,
    TaskApiRouteKind,
)
from zyra_runtime.codeworker_task_api_projection import CodeWorkerTaskApiProjectionRuntime
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
from zyra_workers import (
    BrowserRuntimeConfig,
    BrowserRuntimeRegistry,
    BrowserContextScope,
    BrowserContextTaskIntegrationRuntime,
    BrowserContextApiProjectionRuntime,
    BrowserWorkerRuntime,
    CodeWorkerRuntime,
    WorkerRetrievalContextRuntime,
    MemoryCuratorOperation,
    MemoryCuratorWorkerRequest,
    MemoryCuratorWorkerRuntime,
    build_memory_curator_runtime,
    browser_use_health_summary,
    default_browser_action_registry,
    inspect_browser_use_runtime,
)
from zyra_workers.subagents.typescript_port import TypeScriptAgentDurablePort
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
from zyra_integrations.e02_ports import TypeScriptE02ApiPort, TypeScriptE02PortError

if __package__:
    from .mcp_api import (
        McpApiFacade,
    )
    from .provider_backend_api import ProviderBackendApi, reset_provider_control_client
    from .worker_pool_api import WorkerPoolApiService
else:  # pragma: no cover - direct development script entry.
    from mcp_api import (
        McpApiFacade,
    )
    from provider_backend_api import ProviderBackendApi, reset_provider_control_client
    from worker_pool_api import WorkerPoolApiService

from zyra_orchestration.graph_custody import GraphStateCustody, GraphStateStore
from zyra_scheduler.worker_pool import (
    BackendRegistryHealthAdapter,
    ExecutionOutcome as PhysicalExecutionOutcome,
    ResourceVector as PhysicalResourceVector,
    WorkerPoolFoundationRuntime,
    DispatchMode as PhysicalDispatchMode,
    WorkerLocation as PhysicalWorkerLocation,
    ControlKind,
    WorkerPoolError,
    WorkerPoolErrorCode,
    SchedulerDispatchContext as PhysicalSchedulerDispatchContext,
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


def worker_pool_path() -> Path:
    configured_value = os.environ.get("ZYRA_WORKER_POOL_STORE", "").strip()
    if configured_value:
        configured = Path(configured_value)
        return configured if configured.is_absolute() else PROJECT_ROOT / configured
    canonical = sqlite_path()
    return canonical.with_name(f"{canonical.stem}.worker-pool.sqlite3")


def graph_state_path() -> Path:
    configured_value = os.environ.get("ZYRA_GRAPH_STATE_STORE", "").strip()
    if configured_value:
        configured = Path(configured_value)
        return configured if configured.is_absolute() else PROJECT_ROOT / configured
    canonical = sqlite_path()
    return canonical.with_name(f"{canonical.stem}.graph-state.sqlite3")


_WORKER_POOL_LOCK = threading.RLock()
_WORKER_POOL_RUNTIME: WorkerPoolFoundationRuntime | None = None
_WORKER_POOL_API: WorkerPoolApiService | None = None
_WORKER_POOL_KEY: tuple[str, str, str] | None = None


def get_worker_pool_api() -> WorkerPoolApiService:
    global _WORKER_POOL_RUNTIME, _WORKER_POOL_API, _WORKER_POOL_KEY
    pool_path = worker_pool_path().resolve()
    graph_path = graph_state_path().resolve()
    backend_path = backend_registry_path(artifact_root_path()).resolve()
    key = (str(pool_path), str(graph_path), str(backend_path))
    with _WORKER_POOL_LOCK:
        if _WORKER_POOL_API is None or _WORKER_POOL_KEY != key:
            if _WORKER_POOL_API is not None:
                _WORKER_POOL_API.close()
            configured = os.environ.get("ZYRA_WORKER_POOL_SECRET", "").encode("utf-8")
            secret = (
                hashlib.sha256(configured).digest()
                if configured
                else hashlib.sha256(f"zyra-worker-pool:{pool_path}".encode("utf-8")).digest()
            )
            _WORKER_POOL_RUNTIME = WorkerPoolFoundationRuntime(
                pool_path,
                attestation_secret=secret,
                default_lease_ttl_seconds=60.0,
            )
            graph_store = GraphStateStore(graph_path)
            graph_store.initialize()
            _WORKER_POOL_API = WorkerPoolApiService(
                _WORKER_POOL_RUNTIME,
                GraphStateCustody(graph_store),
                backend_health=BackendRegistryHealthAdapter(backend_path),
            )
            _WORKER_POOL_KEY = key
        return _WORKER_POOL_API


def reset_worker_pool_api() -> None:
    """Forget API-owned worker/graph composition roots between workspace lifecycles."""

    global _WORKER_POOL_RUNTIME, _WORKER_POOL_API, _WORKER_POOL_KEY
    with _WORKER_POOL_LOCK:
        if _WORKER_POOL_API is not None:
            _WORKER_POOL_API.close()
        _WORKER_POOL_API = None
        _WORKER_POOL_RUNTIME = None
        _WORKER_POOL_KEY = None


def fault_runtime_path() -> Path:
    configured_value = os.environ.get("ZYRA_FAULT_RUNTIME_STORE", "").strip()
    if configured_value:
        configured = Path(configured_value)
        return configured if configured.is_absolute() else PROJECT_ROOT / configured
    canonical = sqlite_path()
    return canonical.with_name(f"{canonical.stem}.fault-runtime.sqlite3")


_FAULT_RUNTIME_LOCK = threading.RLock()
_FAULT_RUNTIME_API: FaultRuntimeApiService | None = None
_FAULT_RUNTIME_KEY: tuple[str, str, str] | None = None


def get_fault_runtime_api(store: SQLiteStore | None = None) -> FaultRuntimeApiService:
    global _FAULT_RUNTIME_API, _FAULT_RUNTIME_KEY
    canonical = store or get_store()
    fault_path = fault_runtime_path().resolve()
    backend_path = backend_registry_path(artifact_root_path()).resolve()
    key = (str(Path(canonical.path).resolve()), str(fault_path), str(backend_path))
    with _FAULT_RUNTIME_LOCK:
        if (
            _FAULT_RUNTIME_API is not None
            and _FAULT_RUNTIME_KEY == key
            and not _FAULT_RUNTIME_API.closed
        ):
            return _FAULT_RUNTIME_API
        if _FAULT_RUNTIME_API is not None and not _FAULT_RUNTIME_API.closed:
            _FAULT_RUNTIME_API.close()
        application = FaultRuntimeApplication(
            fault_path,
            event_sink=lambda event: persist_events(canonical, [event]),
            memory=_memory_fabric(canonical),
            scheduler_health=BackendRegistryHealthAdapter(backend_path),
            task_state_resolver=canonical.load_task,
        )
        _FAULT_RUNTIME_API = FaultRuntimeApiService(application)
        _FAULT_RUNTIME_KEY = key
        return _FAULT_RUNTIME_API


def reset_fault_runtime_api() -> None:
    global _FAULT_RUNTIME_API, _FAULT_RUNTIME_KEY
    with _FAULT_RUNTIME_LOCK:
        if _FAULT_RUNTIME_API is not None and not _FAULT_RUNTIME_API.closed:
            _FAULT_RUNTIME_API.close()
        _FAULT_RUNTIME_API = None
        _FAULT_RUNTIME_KEY = None


def memory_index_path() -> Path:
    configured_value = os.environ.get("ZYRA_MEMORY_INDEX_PATH", "").strip()
    if configured_value:
        configured = Path(configured_value)
        if configured.is_absolute():
            return configured
        return PROJECT_ROOT / configured
    canonical = sqlite_path()
    return canonical.with_name(f"{canonical.stem}.memory-index.sqlite3")


def code_index_root_path() -> Path:
    configured_value = os.environ.get("ZYRA_CODE_INDEX_ROOT", "").strip()
    if configured_value:
        configured = Path(configured_value)
        if configured.is_absolute():
            return configured
        return PROJECT_ROOT / configured
    canonical = sqlite_path()
    return canonical.with_name(f"{canonical.stem}-code-index")


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
_CODE_INDEX_SERVICE_LOCK = threading.RLock()
_CODE_INDEX_SERVICE_INSTANCE: CodeIndexApiService | None = None
_CODE_INDEX_SERVICE_KEY: tuple[int, str, bool] | None = None


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
        for event in events:
            workspace_event = event.payload.get("workspace_event", {})
            if str(workspace_event.get("event_type") or "") != "workspace.patch.committed":
                continue
            try:
                _admit_code_index_patch_event(store, workspace_event)
            except Exception as error:  # noqa: BLE001 - canonical patch is already committed.
                persist_events(
                    store,
                    [
                        EventRecord(
                            run_id=event.run_id,
                            task_id=event.task_id,
                            event_type=EventType.SYSTEM_NOTICE,
                            payload={
                                "code_index": {
                                    "schema": "zyra.code-index-event.v1",
                                    "phase": "patch_admission_failed",
                                    "workspace_id": str(workspace_event.get("workspace_id") or ""),
                                    "transaction_id": str(
                                        (workspace_event.get("metadata") or {}).get("transaction_id")
                                        or ""
                                    ),
                                    "error_code": str(getattr(error, "code", type(error).__name__)),
                                    "message": str(error)[:1_000],
                                    "canonical_patch_committed": True,
                                    "fallback": False,
                                    "reconcile_on_next_worker_context": True,
                                }
                            },
                        )
                    ],
                )
    return events


def _admit_code_index_patch_event(store: SQLiteStore, workspace_event: Mapping[str, Any]) -> None:
    metadata = workspace_event.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    transaction_id = str(metadata.get("transaction_id") or "").strip()
    workspace_id = str(workspace_event.get("workspace_id") or "").strip()
    if not transaction_id or not workspace_id:
        raise ValueError("workspace patch event requires transaction_id and workspace_id")
    manager = get_workspace_manager()
    runtime = get_code_index_service().registry.runtime_for_snapshot(workspace_id)
    supervisor = CodeIndexWorkerProcessSupervisor(
        identity=runtime.source.identity,
        index_db=runtime.store.path,
        workspace_state_root=manager.config.state_root,
        workspace_data_root=manager.config.data_root,
        project_root=PROJECT_ROOT,
    )
    integration = CodeIndexIntegrationRuntime(
        runtime,
        transaction_resolver=manager.integration_store,
        process_worker=lambda selected_workspace, maximum_jobs: supervisor.drain(
            maximum_jobs=maximum_jobs
        ).outcomes,
        event_sink=lambda event: persist_events(store, [event]),
        enabled=os.environ.get("ZYRA_CODE_INDEX_DISABLED", "").strip().casefold()
        not in {"1", "true", "yes", "on"},
    )
    integration.admit_patch_transaction(
        transaction_id,
        process=True,
        causation_id=str(workspace_event.get("causation_id") or transaction_id),
    )


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
    # Workspace events are persisted by the API-owned runtime event spine.
    # The 06B procedure/curator runtime is bound to both that workspace and the
    # same canonical SQLite path.  Drop it before closing the sidecar so a
    # workspace lifecycle reset cannot retain a stale bridge or database owner.
    reset_memory_curator_runtime()
    reset_worker_pool_api()
    reset_runtime_event_spine_bridge()


def get_code_index_service() -> CodeIndexApiService:
    global _CODE_INDEX_SERVICE_INSTANCE, _CODE_INDEX_SERVICE_KEY
    manager = get_workspace_manager()
    root = code_index_root_path()
    disabled = os.environ.get("ZYRA_CODE_INDEX_DISABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    key = (id(manager), str(root.resolve()), disabled)
    with _CODE_INDEX_SERVICE_LOCK:
        if _CODE_INDEX_SERVICE_INSTANCE is None or _CODE_INDEX_SERVICE_KEY != key:
            _CODE_INDEX_SERVICE_INSTANCE = CodeIndexApiService(
                CodeIndexRuntimeRegistry(manager, index_root=root),
                enabled=not disabled,
            )
            _CODE_INDEX_SERVICE_KEY = key
        return _CODE_INDEX_SERVICE_INSTANCE


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
_MCP_RUNTIME_INSTANCE: TypeScriptE02ApiPort | None = None
_MCP_RUNTIME_KEY: tuple[str, str, str] | None = None


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


def get_mcp_runtime() -> TypeScriptE02ApiPort:
    """Return the process-live transport to TypeScript-owned E02 state."""

    global _MCP_RUNTIME_INSTANCE, _MCP_RUNTIME_KEY
    key = (
        str(mcp_state_path().resolve()),
        str(artifact_root_path().resolve()),
        str(tool_workspace_path().resolve()),
    )
    with _MCP_RUNTIME_LOCK:
        if _MCP_RUNTIME_INSTANCE is None or _MCP_RUNTIME_KEY != key:
            if _MCP_RUNTIME_INSTANCE is not None:
                _MCP_RUNTIME_INSTANCE.close()
            _MCP_RUNTIME_INSTANCE = TypeScriptE02ApiPort(
                project_root=PROJECT_ROOT,
                workspace_root=key[2],
                state_path=key[0],
                artifact_root=key[1],
                permission_mode=str(os.environ.get("ZYRA_E02_API_PERMISSION_MODE") or "default"),
                sealed_autonomous=_truthy(
                    os.environ.get("ZYRA_E02_API_SEALED_AUTONOMOUS"),
                    default=False,
                ),
            )
            _MCP_RUNTIME_KEY = key
        return _MCP_RUNTIME_INSTANCE


def reset_mcp_runtime(runtime: TypeScriptE02ApiPort | None = None) -> None:
    """Test/development reset of the physical port, never of logical state."""

    global _MCP_RUNTIME_INSTANCE, _MCP_RUNTIME_KEY
    with _MCP_RUNTIME_LOCK:
        if _MCP_RUNTIME_INSTANCE is not None and _MCP_RUNTIME_INSTANCE is not runtime:
            _MCP_RUNTIME_INSTANCE.close()
        _MCP_RUNTIME_INSTANCE = runtime
        _MCP_RUNTIME_KEY = (
            (
                str(mcp_state_path().resolve()),
                str(artifact_root_path().resolve()),
                str(tool_workspace_path().resolve()),
            )
            if runtime is not None
            else None
        )


_E02_READ_COMMANDS = frozenset(
    {
        "e02-health",
        "help",
        "mcp",
        "permissions",
        "plugins",
        "skills",
        "tools",
    }
)
_E02_MUTATING_COMMANDS = frozenset({"e02-reload"})


def _e02_command_route(text: str) -> tuple[str, str] | None:
    """Classify only the command names whose parser and dispatch moved to TS."""

    stripped = str(text or "").strip()
    if not stripped.startswith("/"):
        return None
    name = stripped[1:].partition(" ")[0].strip().lower()
    if name in _E02_READ_COMMANDS:
        return name, "read"
    if name in _E02_MUTATING_COMMANDS:
        return name, "execute"
    return None


def get_runtime_command_registry() -> Any:
    """Retained control registry; E02 commands remain TypeScript projections."""

    return get_control_command_registry()


_CONTROL_RUNTIME_LOCK = threading.RLock()
_CONTROL_REGISTRY: Any | None = None
_CONTROL_SOURCE_COORDINATOR: CommandRegistryCoordinator | None = None
_CONTROL_DISPATCHER: RuntimeControlDispatcher | None = None
_STRUCTURED_CONTROL_HUB: StructuredControlHub | None = None
_CONTROL_RUNTIME_KEY: str | None = None
_TYPESCRIPT_AGENT_PORT_LOCK = threading.RLock()
_TYPESCRIPT_AGENT_PORT: TypeScriptAgentDurablePort | None = None
_TYPESCRIPT_AGENT_PORT_KEY: str | None = None
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
        e02_permission_port=get_mcp_runtime(),
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
        # MCP prompts stay visible through the TypeScript command projection;
        # they are intentionally not registered as Python-dispatchable commands.
        return ()

    coordinator.register(CallableCommandSourceProvider(
        "mcp-prompts",
        CommandSourceKind.MCP,
        mcp_commands,
        revision_loader=lambda: str(
            get_mcp_runtime().snapshot(("mcp",)).get("snapshotHash") or "typescript"
        ),
    ))

    def skill_commands() -> tuple[dict[str, Any], ...]:
        return ()

    coordinator.register(CallableCommandSourceProvider(
        "skill-registry",
        CommandSourceKind.SKILL,
        skill_commands,
        revision_loader=lambda: str(
            get_mcp_runtime().snapshot(("skills",)).get("snapshotHash") or "typescript"
        ),
    ))

    def plugin_commands() -> tuple[dict[str, Any], ...]:
        return ()

    coordinator.register(CallableCommandSourceProvider(
        "plugin-cache",
        CommandSourceKind.PLUGIN,
        plugin_commands,
        revision_loader=lambda: str(
            get_mcp_runtime().snapshot(("plugins",)).get("snapshotHash") or "typescript"
        ),
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


def get_typescript_agent_port() -> TypeScriptAgentDurablePort:
    """Return the TypeScript Agent runtime's durable/physical port.

    Logical AgentTool decisions and child QueryEngine execution are owned by
    packages/runtime/claude-runtime/src/agents. This accessor cannot spawn or
    run a Python child loop.
    """

    global _TYPESCRIPT_AGENT_PORT, _TYPESCRIPT_AGENT_PORT_KEY
    root = subagent_state_path().resolve()
    workspace_root = workspace_manager_config().data_root.resolve()
    key = f"{root}|{workspace_root}"
    with _TYPESCRIPT_AGENT_PORT_LOCK:
        if _TYPESCRIPT_AGENT_PORT is None or _TYPESCRIPT_AGENT_PORT_KEY != key:
            _TYPESCRIPT_AGENT_PORT = TypeScriptAgentDurablePort(
                root,
                # E03 effects may only target manager-owned task workspaces;
                # the legacy tool workspace is a sibling and is not their
                # containment authority.
                workspace_root=workspace_root,
                event_sink=_commit_runtime_event,
            )
            _TYPESCRIPT_AGENT_PORT_KEY = key
        return _TYPESCRIPT_AGENT_PORT


def reset_subagent_runtime() -> None:
    global _TYPESCRIPT_AGENT_PORT, _TYPESCRIPT_AGENT_PORT_KEY
    with _TYPESCRIPT_AGENT_PORT_LOCK:
        _TYPESCRIPT_AGENT_PORT = None
        _TYPESCRIPT_AGENT_PORT_KEY = None


def _authorize_typescript_agent_physical_execution(
    state: Any,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    parent_session_id: str,
) -> Mapping[str, Any] | tuple[Mapping[str, Any], ...] | None:
    """Re-read the canonical physical attempt immediately before E03 runs."""

    if tool_name not in {"Agent", "agent_resume"}:
        return None
    gate = get_worker_pool_api().integration.execution_gate
    if tool_name == "Agent":
        projection = arguments.get("physical_dispatch")
        if isinstance(projection, Mapping):
            task_id = str(arguments.get("task_id") or projection.get("task_id") or "")
            return gate.authorize_projection(
                projection,
                expected_task_id=task_id,
                expected_run_id=state.run_id,
                expected_session_id=parent_session_id,
                operation="execute_typescript_agent_task",
            )
        raw_requests = arguments.get("requests")
        if not isinstance(raw_requests, list):
            raise WorkerPoolError(
                WorkerPoolErrorCode.EXECUTION_REJECTED,
                "TypeScript Agent execution requires a physical dispatch lease",
                operation="execute_typescript_agent_task",
                task_id=state.task_id,
            )
        projections: list[Mapping[str, Any]] = []
        for item in raw_requests:
            if not isinstance(item, Mapping) or not isinstance(item.get("physical_dispatch"), Mapping):
                raise WorkerPoolError(
                    WorkerPoolErrorCode.EXECUTION_REJECTED,
                    "every TypeScript Agent fanout item requires a physical dispatch lease",
                    operation="execute_typescript_agent_fanout",
                    task_id=state.task_id,
                )
            projections.append(item["physical_dispatch"])
        return gate.authorize_many(
            projections,
            expected_run_id=state.run_id,
            expected_session_id=parent_session_id,
            operation="execute_typescript_agent_fanout",
        )
    task_id = str(arguments.get("task_id") or "")
    return gate.authorize_task(
        task_id,
        expected_run_id=state.run_id,
        expected_session_id=parent_session_id,
        operation="resume_typescript_agent_task",
    )


def _run_typescript_agent_request(
    state: Any,
    *,
    arguments: dict[str, Any],
    request_id: str,
    tool_name: str = "Agent",
    session_id: str = "",
    session_custody_token: str = "",
) -> Any:
    parent_session_id = session_id or str(
        state.metadata.get("query_session_id") or f"task:{state.task_id}"
    )
    workspace_manager = get_workspace_manager()
    workspace_access = workspace_manager.acquire_for_worker(
        task_id=state.task_id,
        session_id="",
        worker_id="CodeWorkerRuntime",
    )
    worker_workspace_root = workspace_manager.internal_task_root(workspace_access)
    _authorize_typescript_agent_physical_execution(
        state,
        tool_name=tool_name,
        arguments=arguments,
        parent_session_id=parent_session_id,
    )
    bounded_arguments = {
        **arguments,
        # The E03 physical isolation port is bound to this task workspace.  A
        # caller-supplied/project-root value must never escape that boundary.
        "workspace_root": str(worker_workspace_root),
    }
    constraints = {
        "query_turns": [[{
            "tool_name": tool_name,
            "step_id": f"agent-api:{request_id}",
            "tool_call_id": f"agent-api:{request_id}",
            "arguments": bounded_arguments,
        }]],
        "typescriptAgentStatePath": str(subagent_state_path()),
        "query_context_budget_chars": 32000,
        "tool_result_budget_chars": 120000,
        "session_id": parent_session_id,
        "workspace_ref": workspace_access.to_public_dict(),
    }
    if session_custody_token:
        constraints["session_custody_token"] = session_custody_token
    request = WorkerRequest(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=state.root_node_id,
        worker_name="CodeWorkerRuntime",
        request_id=request_id,
        constraints=constraints,
        metadata={
            "origin": "typescript-agent-api",
            "canonical_agent_owner": "typescript",
        },
    )
    canonical_store = SQLiteStore(sqlite_path())
    canonical_store.initialize()
    retrieval_context = _worker_retrieval_context(
        canonical_store,
        task_id=state.task_id,
        workspace_manager=workspace_manager,
        workspace_access=workspace_access,
    )
    return CodeWorkerRuntime(
        project_root=PROJECT_ROOT,
        workspace_root=worker_workspace_root,
        artifact_root=artifact_root_path(),
        permission_store=get_permission_store(),
        permission_state_path=permission_state_path(),
        tool_registry=default_tool_registry(),
        runtime_services={
            "workspace_edit_port": WorkspaceEditPort(
                workspace_manager,
                workspace_access,
                worker_id="CodeWorkerRuntime",
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                artifact_store=LocalArtifactStore(artifact_root_path()),
            ),
            "workspace_isolation_runtime": WorkspaceIsolationRuntime(
                workspace_manager,
                artifact_store=LocalArtifactStore(artifact_root_path()),
            ),
            "workspace_gateway_required": True,
            "typescript_agent_state_path": str(subagent_state_path()),
        },
        retrieval_context_runtime=retrieval_context,
    ).run(request)


def _acquire_subagent_physical_dispatch(
    state: Any,
    *,
    task_id: str,
    owner_session_id: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Acquire and start the canonical 07A physical attempt before E03 runs.

    The first mapping is persisted by the TypeScript E03 task as a read-only
    dispatch projection.  It intentionally excludes the fence token; only the
    Python WorkerPoolStore uses that token to settle the canonical lease.
    """

    pool_api = get_worker_pool_api()
    raw_workspace_ref = state.metadata.get("workspace_ref")
    if isinstance(raw_workspace_ref, Mapping):
        nested_workspace = raw_workspace_ref.get("workspace")
        workspace_ref = str(
            raw_workspace_ref.get("workspace_id")
            or (
                nested_workspace.get("workspace_id")
                if isinstance(nested_workspace, Mapping)
                else ""
            )
            or f"workspace:{state.task_id}"
        )
    else:
        workspace_ref = str(raw_workspace_ref or f"workspace:{state.task_id}")
    pool_api.ensure_default_local_worker()
    graph_id = pool_api.ensure_task_graph(state)
    graph = pool_api.graph_custody.current(graph_id)
    runtime_node = pool_api.integration.add_runtime_node_for_requirement(
        graph_id=graph_id,
        logical_task_id=task_id,
        role="subagent_worker",
        capabilities=("agent_task", "code_execution", "artifact_return"),
        connect_from=(state.root_node_id,) if state.root_node_id in graph.node_map else (),
        workspace_ref=workspace_ref,
        causation_id=f"subagent-admission:{task_id}",
        node_id=f"runtime-subagent:{task_id}",
        metadata={"parent_task_id": state.task_id, "omp_session_owner": owner_session_id},
    ) if f"runtime-subagent:{task_id}" not in graph.node_map else {
        "version_ref": pool_api.topology.version_ref(graph_id).to_dict()
    }
    graph_ref = pool_api.topology.version_ref(graph_id)
    latest_attempt = pool_api.pool.store.latest_attempt(task_id)
    active_binding = pool_api.integration.repository.latest_binding(task_id)
    if active_binding is not None:
        active_lease = pool_api.pool.store.get_lease(active_binding.lease_id)
        if (
            not active_binding.terminal
            and active_lease is not None
            and not active_lease.terminal
            and not active_lease.expired_at()
        ):
            binding = active_binding
            lease = active_lease
            worker = pool_api.pool.store.require_worker(binding.worker_id)
            manifest = pool_api.pool.store.latest_manifest(worker.worker_id)
            if manifest is None:
                raise RuntimeError(f"worker {worker.worker_id} has no capability manifest")
            reused = True
        else:
            if (
                active_lease is not None
                and not active_lease.terminal
                and active_lease.expired_at()
            ):
                pool_api.pool.leases.expire(
                    active_lease.lease_id,
                    reason="subagent approval exceeded integrated physical admission lease",
                )
                pool_api.integration.reconcile(run_id=state.run_id)
            active_binding = None
    if active_binding is None:
        attempt_number = 1 if latest_attempt is None else latest_attempt.attempt_number + 1
        scheduler_context = PhysicalSchedulerDispatchContext(
            task_id=task_id,
            run_id=state.run_id,
            owner_session_id=owner_session_id,
            logical_attempt=attempt_number,
            logical_task_revision=0,
            graph_id=graph_id,
            graph_revision=graph_ref.revision,
            graph_node_id=f"runtime-subagent:{task_id}",
            workspace_ref=workspace_ref,
            gateway_ref="local-sandbox-gateway",
            backend_route_id="local-code-worker",
            required_capabilities=("agent_task",),
            locations=(PhysicalWorkerLocation.LOCAL,),
            resources=PhysicalResourceVector(process_slots=1, memory_mb=64),
            execution_mode=PhysicalDispatchMode.BACKGROUND,
            preferred_worker_ids=("local-code-worker",),
            lease_ttl_seconds=15 * 60.0,
            checkpoint_ref=str(state.metadata.get("checkpoint_id") or ""),
            idempotency_key=f"{idempotency_key}:integration:{attempt_number}",
            causation_id=f"subagent-admission:{task_id}",
            correlation_id=owner_session_id,
            metadata={
                "parent_task_id": state.task_id,
                "graph_node_id": f"runtime-subagent:{task_id}",
                "logical_owner": "typescript.E03AgentControlCoordinator",
                "projection_owner": "typescript.OmpWorkerDispatchRuntime",
                "logical_task_not_duplicated": True,
                "runtime_node_commit": runtime_node,
            },
        )
        raw_memory_signals = state.metadata.get("memory_signals") or ()
        memory_signals = tuple(item for item in raw_memory_signals if isinstance(item, Mapping))
        _scheduler_plan, admission = pool_api.integration.scheduler.admit(
            scheduler_context,
            memory_signals=memory_signals,
        )
        binding = admission.binding
        lease = pool_api.pool.store.require_lease(binding.lease_id)
        worker = pool_api.pool.store.require_worker(binding.worker_id)
        manifest = pool_api.pool.store.latest_manifest(worker.worker_id)
        if manifest is None:
            raise RuntimeError(f"worker {worker.worker_id} has no capability manifest")
        reused = admission.reused
    started = pool_api.integration.start(
        binding.binding_id,
        fence_token=lease.fence_token,
        backend_dispatch_id=f"typescript-e03:{task_id}:{binding.attempt_number}",
    )
    binding = started.binding
    unsigned_projection = {
        "schema": "zyra.worker-pool-dispatch/v1",
        "required": True,
        "canonical_owner": "python.WorkerPoolStore",
        "projection_owner": "typescript.OmpWorkerDispatchRuntime",
        "task_id": task_id,
        "attempt_id": binding.attempt_id,
        "attempt": binding.attempt_number,
        "lease_id": lease.lease_id,
        "worker_id": worker.worker_id,
        "backend_id": lease.backend_id,
        "fence_epoch": lease.fence_epoch,
        "manifest_digest": manifest.digest,
        "concurrency_limit": max(1, min(128, manifest.resource_capacity.process_slots or 1)),
        "lease_state": lease.state.value,
        "logical_task_not_duplicated": True,
        "integration_binding_id": binding.binding_id,
        "graph_ref": binding.foreign_refs.graph.to_dict(),
        "workspace_ref": binding.foreign_refs.workspace.to_dict(),
        "gateway_ref": binding.foreign_refs.gateway.to_dict(),
        "route_ref": binding.foreign_refs.backend_route.to_dict(),
    }
    projection = {
        **unsigned_projection,
        "projection_digest": hashlib.sha256(
            json.dumps(
                unsigned_projection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    public = {
        "task_id": task_id,
        "attempt_id": binding.attempt_id,
        "attempt": binding.attempt_number,
        "lease_id": lease.lease_id,
        "worker_id": worker.worker_id,
        "backend_id": lease.backend_id,
        "fence_epoch": lease.fence_epoch,
        "manifest_digest": manifest.digest,
        "lease_state": lease.state.value,
        "typescript_dispatch_gate": "typescript.OmpWorkerDispatchRuntime",
        "logical_task_not_duplicated": True,
        "reused": reused,
        "integration_binding_id": binding.binding_id,
        "graph_ref": binding.foreign_refs.graph.to_dict(),
    }
    return projection, public


def _settle_subagent_physical_dispatch(
    projection: Mapping[str, Any],
    *,
    status: str,
    summary: str,
) -> Mapping[str, Any] | None:
    """Commit one canonical execution receipt after the E03 task settles."""

    terminal = {"completed", "failed", "cancelled", "killed"}
    if status not in terminal:
        return None
    pool_api = get_worker_pool_api()
    pool = pool_api.pool
    lease_id = str(projection.get("lease_id") or "")
    lease = pool.store.get_lease(lease_id)
    if lease is None:
        raise RuntimeError(f"physical worker lease {lease_id} disappeared")
    if lease.terminal:
        existing = next(
            (
                item
                for item in pool.store.receipts_for_task(str(projection.get("task_id") or ""))
                if item.attempt_id == str(projection.get("attempt_id") or "")
            ),
            None,
        )
        return existing.to_dict() if existing is not None else None
    outcome = {
        "completed": PhysicalExecutionOutcome.SUCCEEDED,
        "cancelled": PhysicalExecutionOutcome.CANCELLED,
        "killed": PhysicalExecutionOutcome.CANCELLED,
        "failed": PhysicalExecutionOutcome.FAILED,
    }[status]
    binding_id = str(projection.get("integration_binding_id") or "")
    if not binding_id:
        raise RuntimeError("physical dispatch projection has no integration binding id")
    integrated = pool_api.integration.complete(
        binding_id,
        fence_token=lease.fence_token,
        outcome=outcome,
        summary=summary or f"TypeScript E03 task {status}",
        backend_receipt_ref=f"typescript-e03:{projection.get('task_id')}:{projection.get('attempt')}",
        result_payload={
            "logical_owner": "typescript.E03AgentControlCoordinator",
            "dispatch_gate": "typescript.OmpWorkerDispatchRuntime",
            "projection_digest": str(projection.get("projection_digest") or ""),
        },
    )
    return {
        **dict(integrated.execution_receipt),
        "integration_binding": integrated.binding.to_dict(),
        "typed_yield": integrated.typed_yield.to_dict() if integrated.typed_yield else None,
    }


def _agent_permission_session(run: Any) -> dict[str, Any]:
    return {
        **run.private_api_session_envelope(),
        "schema": "zyra.permission-session-api-envelope.v1",
        "cacheable": False,
        "must_not_persist": True,
        "presentation": "one_time_if_created",
    }


def _agent_api_authority_error(payload: dict[str, Any]) -> str:
    if payload.get("available_mcp_servers"):
        return "client cannot declare the parent MCP ceiling"
    if str(payload.get("permission_mode") or payload.get("requested_permission_mode") or "") in {
        "bypass",
        "auto",
    }:
        return "client cannot expand the parent permission ceiling"
    requested = {
        str(item)
        for item in payload.get("requested_tools") or ()
        if str(item)
    }
    available = {item.name for item in default_tool_registry().list()}
    unknown = sorted(requested - available)
    if unknown:
        return "client requested tools outside the parent catalog: " + ", ".join(unknown)
    return ""


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


def _task_skill_worker_messages(
    state: Any,
    *,
    worker_request_id: str = "",
) -> tuple[list[AgentMessage], None]:
    """Skills are assembled inside the TypeScript E02 query context."""

    del state, worker_request_id
    return [], None
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
        workspace_runtime_resolver=_graph_workspace_runtime_binding,
    )


def _graph_workspace_runtime_binding(
    state: Any,
    node: Any,
    worker_name: str,
) -> tuple[Path, dict[str, Any]]:
    """Bind graph workers to the task's canonical managed workspace.

    The graph layer owns worker selection, while the workspace package owns
    leases, mutation fencing, and path custody.  Resolve the private capability
    at execution time so a resumed task reacquires the current owner epoch
    instead of falling back to the process-wide development workspace.
    """

    manager = get_workspace_manager()
    access = manager.acquire_for_worker(
        task_id=state.task_id,
        session_id="",
        worker_id=worker_name,
    )
    workspace_root = manager.internal_task_root(access)
    services: dict[str, Any] = {
        "workspace_edit_port": WorkspaceEditPort(
            manager,
            access,
            worker_id=worker_name,
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=node.node_id,
            artifact_store=LocalArtifactStore(artifact_root_path()),
        ),
        "workspace_gateway_required": True,
        "runtime_event_bridge": get_runtime_event_spine_bridge(),
    }
    if worker_name == "CodeWorkerRuntime":
        services.update(
            {
                "workspace_isolation_runtime": WorkspaceIsolationRuntime(
                    manager,
                    artifact_store=LocalArtifactStore(artifact_root_path()),
                ),
                "typescript_agent_state_path": str(subagent_state_path()),
            }
        )
    return workspace_root, services


def get_permission_store() -> JsonPermissionStore:
    return JsonPermissionStore(permission_store_path())


def _memory_fabric(store: SQLiteStore) -> MemoryFabric:
    artifacts = LocalArtifactStore(artifact_root_path())
    return MemoryFabric(
        store=store,
        artifact_store=artifacts,
        index_runtime=MemoryIndexRuntime(
            canonical_store=store,
            index_path=memory_index_path(),
            artifact_store=artifacts,
            worker_id=f"api-memory-index:{os.getpid()}",
        ),
    )


_MEMORY_CURATOR_LOCK = threading.RLock()
_MEMORY_CURATOR_INSTANCE: MemoryCuratorWorkerRuntime | None = None
_MEMORY_CURATOR_KEY: tuple[str, str, str] | None = None
_REUSABLE_PROCEDURE_LOCK = threading.RLock()
_REUSABLE_PROCEDURE_INSTANCE: ReusableProcedureRuntime | None = None
_REUSABLE_PROCEDURE_KEY: tuple[str, int] | None = None


def get_memory_curator_runtime(store: SQLiteStore | None = None) -> MemoryCuratorWorkerRuntime:
    """Return the API-owned curator bound to canonical memory/artifact/index paths."""

    global _MEMORY_CURATOR_INSTANCE, _MEMORY_CURATOR_KEY
    canonical = store or get_store()
    key = (
        str(Path(canonical.path).resolve()),
        str(memory_index_path().resolve()),
        str(artifact_root_path().resolve()),
    )
    with _MEMORY_CURATOR_LOCK:
        if _MEMORY_CURATOR_INSTANCE is None or _MEMORY_CURATOR_KEY != key:
            artifacts = LocalArtifactStore(artifact_root_path())
            memory_index = MemoryIndexRuntime(
                canonical_store=canonical,
                index_path=memory_index_path(),
                artifact_store=artifacts,
                worker_id=f"api-memory-curator-index:{os.getpid()}",
            )
            _MEMORY_CURATOR_INSTANCE = build_memory_curator_runtime(
                canonical_store=canonical,
                artifact_store=artifacts,
                memory_index=memory_index,
                worker_id=f"api-memory-curator:{os.getpid()}",
                runtime_event_bridge=get_runtime_event_spine_bridge(),
                allow_legacy_event_fallback=False,
                auto_dispatch=True,
            )
            _MEMORY_CURATOR_KEY = key
        return _MEMORY_CURATOR_INSTANCE


def reset_memory_curator_runtime() -> None:
    global _MEMORY_CURATOR_INSTANCE, _MEMORY_CURATOR_KEY
    global _REUSABLE_PROCEDURE_INSTANCE, _REUSABLE_PROCEDURE_KEY
    with _MEMORY_CURATOR_LOCK:
        _MEMORY_CURATOR_INSTANCE = None
        _MEMORY_CURATOR_KEY = None
    with _REUSABLE_PROCEDURE_LOCK:
        _REUSABLE_PROCEDURE_INSTANCE = None
        _REUSABLE_PROCEDURE_KEY = None


def get_reusable_procedure_runtime(
    store: SQLiteStore | None = None,
) -> ReusableProcedureRuntime:
    """Bind procedure mining to the canonical 06B store and MemoryFabric DB."""

    global _REUSABLE_PROCEDURE_INSTANCE, _REUSABLE_PROCEDURE_KEY
    canonical = store or get_store()
    curator = get_memory_curator_runtime(canonical)
    integration_store = curator.integration_store
    if integration_store is None:
        raise RuntimeError(
            "memory curator integration store is required for procedure mining"
        )
    key = (str(Path(canonical.path).resolve()), id(integration_store))
    with _REUSABLE_PROCEDURE_LOCK:
        if (
            _REUSABLE_PROCEDURE_INSTANCE is None
            or _REUSABLE_PROCEDURE_KEY != key
        ):
            _REUSABLE_PROCEDURE_INSTANCE = ReusableProcedureRuntime(
                canonical_store=canonical,
                curator_store=integration_store,
                procedure_store=ReusableProcedureStore(canonical.path),
                event_sink=lambda event: persist_events(canonical, [event]),
            )
            _REUSABLE_PROCEDURE_KEY = key
        return _REUSABLE_PROCEDURE_INSTANCE


def curate_terminal_task(store: SQLiteStore, state: Any) -> dict[str, Any] | None:
    """Schedule terminal curation without coupling primary task success to the worker."""

    if str(state.status) not in {"completed", "failed", "cancelled"}:
        return None
    try:
        response = get_memory_curator_runtime(store).execute(
            MemoryCuratorWorkerRequest(
                operation=MemoryCuratorOperation.SCHEDULE_TASK_END,
                task_id=state.task_id,
                requested_by="task-lifecycle",
                allow_model_assist=True,
                max_candidates=64,
                process_immediately=False,
                metadata={"task_status": str(state.status)},
            )
        )
        return response.to_dict()
    except Exception as error:  # noqa: BLE001 - curator failure must not fail the task lifecycle.
        return {
            "operation": MemoryCuratorOperation.SCHEDULE_TASK_END.value,
            "status": "degraded",
            "scheduled": None,
            "result": None,
            "data": {
                "error": type(error).__name__,
                "message": str(error)[:500],
                "task_lifecycle_preserved": True,
            },
        }


def _worker_retrieval_context(
    store: SQLiteStore,
    *,
    task_id: str,
    workspace_manager: WorkspaceManagerRuntime,
    workspace_access: Any,
) -> WorkerRetrievalContextRuntime:
    """Synchronize derived indexes and bind them to the next CodeWorker run."""

    artifacts = LocalArtifactStore(artifact_root_path())
    memory_runtime = MemoryIndexRuntime(
        canonical_store=store,
        index_path=memory_index_path(),
        artifact_store=artifacts,
        worker_id=f"api-memory-index:{os.getpid()}",
    )
    memory_supervisor = MemoryIndexWorkerProcessSupervisor(
        canonical_db=store.path,
        index_db=memory_index_path(),
        project_root=PROJECT_ROOT,
    )
    memory = RetrievalIntegrationRuntime(
        memory_runtime,
        worker_supervisor=memory_supervisor,
        event_sink=lambda event: persist_events(store, [event]),
        enabled=os.environ.get("ZYRA_RETRIEVAL_INDEX_DISABLED", "").strip().casefold()
        not in {"1", "true", "yes", "on"},
    )
    memory.admit_canonical_records(
        task_id,
        event_id=f"worker-context:{task_id}",
        causation_id=f"worker-context:{task_id}",
        process_worker=True,
    )

    code_runtime = get_code_index_service().registry.runtime_for_access(workspace_access)
    if code_runtime.source.identity.workspace_id != str(workspace_access.workspace_id):
        raise RuntimeError("code index registry returned another task workspace")
    config = workspace_manager.config
    code_supervisor = CodeIndexWorkerProcessSupervisor(
        identity=code_runtime.source.identity,
        index_db=code_runtime.store.path,
        workspace_state_root=config.state_root,
        workspace_data_root=config.data_root,
        project_root=PROJECT_ROOT,
    )
    code = CodeIndexIntegrationRuntime(
        code_runtime,
        transaction_resolver=workspace_manager.integration_store,
        process_worker=lambda workspace_id, maximum_jobs: code_supervisor.drain(
            maximum_jobs=maximum_jobs
        ).outcomes,
        event_sink=lambda event: persist_events(store, [event]),
        enabled=os.environ.get("ZYRA_CODE_INDEX_DISABLED", "").strip().casefold()
        not in {"1", "true", "yes", "on"},
    )
    code.admit_initial(process=True, causation_id=f"worker-context:{task_id}")
    # Reconcile durable 05B patch transactions even if an in-process event was
    # lost.  The transaction is dereferenced from WorkspaceIntegrationStore;
    # no event payload is trusted as file truth.
    for transaction in workspace_manager.integration_store.list_transactions(
        code_runtime.source.identity.workspace_id
    ):
        transaction_id = str(getattr(transaction, "transaction_id", ""))
        phase = str(getattr(transaction, "phase", "")).casefold()
        if (
            transaction_id
            and "commit" in phase
            and not code_runtime.store.has_invalidation(
                code_runtime.source.identity.workspace_id,
                transaction_id,
            )
        ):
            code.admit_patch_transaction(
                transaction_id,
                process=True,
                causation_id=transaction_id,
            )
    return WorkerRetrievalContextRuntime(
        memory=memory,
        code=code,
        procedures=get_reusable_procedure_runtime(store),
    )


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
    # The TypeScript spine owns sequence/idempotency and derives the legacy
    # `events` compatibility rows from the committed fact.  The JSONL trajectory is
    # appended only after canonical admission so a failed canonical append can
    # never manufacture a competing fact.
    bridge = get_runtime_event_spine_bridge()
    for event in events:
        # Legacy EventLog batches were never atomic.  Project each successful
        # canonical commit immediately so a later rejected item cannot leave
        # an already-committed fact missing from the compatibility trajectory.
        bridge.append_legacy_events([event])
        append_jsonl_event(event, event_log_path())


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

    def setup(self) -> None:
        super().setup()
        server = self.server
        with _RUNTIME_EVENT_SPINE_LOCK:
            drain_condition = getattr(server, "_zyra_request_drain_condition", None)
            if drain_condition is None:
                drain_condition = threading.Condition()
                setattr(server, "_zyra_request_drain_condition", drain_condition)
                setattr(server, "_zyra_active_request_count", 0)
            with drain_condition:
                active = int(getattr(server, "_zyra_active_request_count", 0))
                setattr(server, "_zyra_active_request_count", active + 1)
            if getattr(server, "_zyra_runtime_event_close_bound", False):
                return
            original_server_close = server.server_close

            def close_with_runtime_event_spine() -> None:
                try:
                    original_server_close()
                finally:
                    # ThreadingHTTPServer uses daemon request threads. Drain
                    # them explicitly before closing shared sidecars so a
                    # timed-out client cannot make an in-flight canonical
                    # event append fail with a closed process port.
                    deadline = time.monotonic() + 30.0
                    with drain_condition:
                        while int(getattr(server, "_zyra_active_request_count", 0)) > 0:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            drain_condition.wait(timeout=remaining)
                    try:
                        reset_runtime_event_spine_bridge()
                    finally:
                        reset_provider_control_client()

            server.server_close = close_with_runtime_event_spine  # type: ignore[method-assign]
            setattr(server, "_zyra_runtime_event_close_bound", True)

    def finish(self) -> None:
        server = self.server
        try:
            super().finish()
        finally:
            drain_condition = getattr(server, "_zyra_request_drain_condition", None)
            if drain_condition is not None:
                with drain_condition:
                    active = int(getattr(server, "_zyra_active_request_count", 0))
                    setattr(server, "_zyra_active_request_count", max(0, active - 1))
                    drain_condition.notify_all()

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

    def _require_permission_task_identity(
        self,
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
            session_id = str(parameters.get("session_id") or "")
            authority = None
            if parts != ["permissions", "health"] and session_id:
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
            if parts == ["permissions", "health"]:
                view = "summary"
                operation = PermissionApiOperation.HEALTH
            elif parts == ["permissions"]:
                view = "summary"
                operation = PermissionApiOperation.REQUEST_QUERY
            elif parts == ["permissions", "requests"]:
                view = "requests"
                operation = PermissionApiOperation.REQUEST_QUERY
            elif len(parts) == 3 and parts[:2] == ["permissions", "requests"]:
                view = "request"
                operation = PermissionApiOperation.REQUEST_GET
            elif parts == ["permissions", "rules"]:
                view = "rules"
                operation = PermissionApiOperation.RULE_QUERY
            elif parts == ["permissions", "mode"]:
                view = "mode"
                operation = PermissionApiOperation.MODE_GET
            elif parts == ["permissions", "decisions"]:
                view = "decisions"
                operation = PermissionApiOperation.DECISION_QUERY
            else:
                return False
            projection = get_mcp_runtime().permission_get(
                view=view,
                request_id=(parts[2] if view == "request" else str(parameters.get("request_id") or "")),
                status=str(parameters.get("status") or ""),
                limit=_bounded_permission_limit(parameters.get("limit")),
            )
            transport_requests: list[dict[str, Any]] = []
            if authority is not None and view in {"summary", "requests", "request"}:
                transport_parameters = dict(parameters)
                if view == "request":
                    transport_parameters["request_id"] = parts[2]
                transport_status = str(transport_parameters.get("status") or "")
                if transport_status in {"created", "delivered", "resolved"}:
                    # E02 projects approval-envelope lifecycle as ``status``;
                    # the transport store models the same values as ``phase``.
                    transport_parameters.pop("status", None)
                    transport_parameters["phase"] = transport_status
                transport_response = facade.query_requests(authority, transport_parameters)
                transport_page = transport_response.body.get("requests", {})
                if isinstance(transport_page, dict):
                    transport_requests = [
                        dict(item)
                        for item in transport_page.get("items", [])
                        if isinstance(item, dict)
                    ]
            projected_requests = [
                dict(item)
                for item in projection.get("requests", [])
                if isinstance(item, dict)
            ]
            visible_requests = _merge_permission_request_projections(
                transport_requests,
                projected_requests,
            )
            body = {
                "schema": "zyra.permission-api.v2",
                "ok": True,
                "operation": operation.value,
                "state_owner": "typescript.PermissionCoordinator",
                "canonical_entrypoint": "E02CapabilityCoordinator.resumePermission",
                "python_decision_fallback": False,
                "session_id": authority.session_id if authority is not None else "",
                "health": projection.get("health", {}),
            }
            if view == "summary":
                visible_requests = visible_requests if authority is not None else []
                visible_rules = projection.get("rules", []) if authority is not None else []
                visible_decisions = projection.get("decisions", []) if authority is not None else []
                body.update(
                    {
                        "requests": {"items": visible_requests, "total": len(visible_requests)},
                        "rules": visible_rules,
                        "mode": projection.get("mode", {}),
                        "decisions": {"items": visible_decisions, "total": len(visible_decisions)},
                        "state_listing_requires_session_custody": not bool(authority),
                    }
                )
            elif view == "requests":
                body["requests"] = {"items": visible_requests, "total": len(visible_requests)}
            elif view == "request":
                body["request"] = (visible_requests or [None])[0]
            elif view == "rules":
                body["rules"] = projection.get("rules", [])
            elif view == "mode":
                body["mode"] = projection.get("mode", {})
            elif view == "decisions":
                body["decisions"] = {"items": projection.get("decisions", []), "total": len(projection.get("decisions", []))}
            response = PermissionApiResponse(
                status=HTTPStatus.OK,
                operation=operation,
                body=body,
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Pragma": "no-cache",
                    "X-Zyra-Permission-State-Owner": "typescript.PermissionCoordinator",
                    "X-Zyra-Python-Decision-Fallback": "false",
                },
            )
        except TypeScriptE02PortError as error:
            response = PermissionApiResponse(
                status=(HTTPStatus.NOT_FOUND if "not_found" in error.code else HTTPStatus.CONFLICT),
                operation=operation,
                body={
                    "schema": "zyra.permission-api.v2",
                    "ok": False,
                    "error": error.code,
                    "message": str(error),
                    "detail": error.detail,
                    "state_owner": "typescript.PermissionCoordinator",
                    "python_decision_fallback": False,
                },
                headers={"Cache-Control": "no-store, max-age=0"},
            )
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
                receipt: dict[str, Any]
                response_events: tuple[EventRecord, ...] = ()
                status = HTTPStatus.OK
                if parts == ["permissions", "requests"]:
                    operation = PermissionApiOperation.REQUEST_CREATE
                    status = HTTPStatus.CONFLICT
                    receipt = {
                        "error": "typescript_permission_evaluation_required",
                        "message": "Permission requests are created only by TypeScript tool enforcement.",
                    }
                elif parts == ["permissions", "requests", "expire"]:
                    operation = PermissionApiOperation.REQUEST_EXPIRE
                    transport_response = facade.expire_requests(authority)
                    response_events = transport_response.events
                    receipt = {
                        "typescript": get_mcp_runtime().permission_expire(),
                        "transport": dict(transport_response.body),
                    }
                elif len(parts) == 4 and parts[:2] == ["permissions", "requests"]:
                    request_id = parts[2]
                    action = parts[3]
                    transport_request = facade.control_plane.state_store.get_request(request_id)
                    if action == "deliver":
                        operation = PermissionApiOperation.REQUEST_DELIVER
                        if transport_request is not None:
                            transport_response = facade.deliver_request(authority, request_id, payload)
                            response_events = transport_response.events
                            receipt = dict(transport_response.body.get("result") or {})
                        else:
                            projection = get_mcp_runtime().permission_get(
                                view="request",
                                request_id=request_id,
                            )
                            receipt = {
                                "request": (projection.get("requests") or [None])[0],
                                "transport": "e02-api-poll",
                                "logical_state_changed": False,
                            }
                    elif action == "resolve":
                        operation = PermissionApiOperation.REQUEST_RESOLVE
                        if transport_request is not None:
                            transport_response = facade.resolve_request(authority, request_id, payload)
                            status = HTTPStatus(transport_response.status)
                            response_events = transport_response.events
                            receipt = dict(transport_response.body.get("result") or {})
                        else:
                            receipt = get_mcp_runtime().permission_respond(
                                request_id,
                                str(payload.get("effect") or ""),
                                responder=authority.actor_id,
                                response_id=str(payload.get("response_id") or payload.get("idempotency_key") or ""),
                                metadata={
                                    "authority_id": authority.authority_id,
                                    "channel": str(authority.channel),
                                    "custody_verified": True,
                                },
                            )
                    elif action == "cancel":
                        operation = PermissionApiOperation.REQUEST_CANCEL
                        if transport_request is not None:
                            transport_response = facade.cancel_request(authority, request_id, payload)
                            response_events = transport_response.events
                            receipt = dict(transport_response.body.get("result") or {})
                        else:
                            receipt = get_mcp_runtime().permission_cancel(
                                request_id,
                                reason=str(payload.get("reason") or "cancelled by operator"),
                            )
                    elif action == "abort":
                        operation = PermissionApiOperation.REQUEST_ABORT
                        if transport_request is not None:
                            transport_response = facade.abort_request(authority, request_id, payload)
                            response_events = transport_response.events
                            receipt = dict(transport_response.body.get("result") or {})
                        else:
                            receipt = get_mcp_runtime().permission_cancel(
                                request_id,
                                reason=str(payload.get("reason") or "aborted by operator"),
                            )
                    elif action == "retry":
                        operation = PermissionApiOperation.REQUEST_RETRY
                        status = HTTPStatus.CONFLICT
                        receipt = {
                            "error": "typescript_exact_retry_required",
                            "message": "Retry the original tool call with the permit_id returned by the TypeScript decision receipt.",
                            "request_id": request_id,
                        }
                    else:
                        return False
                elif parts == ["permissions", "rules"]:
                    operation = PermissionApiOperation.RULE_CREATE
                    receipt = get_mcp_runtime().permission_policy(
                        {
                            "action": "replace_rules",
                            "rules": payload.get("rules"),
                            "expected_revision": payload.get("expected_revision"),
                            "actor_id": authority.actor_id,
                        }
                    )
                elif (
                    len(parts) == 4
                    and parts[:2] == ["permissions", "rules"]
                    and parts[3] == "remove"
                ):
                    operation = PermissionApiOperation.RULE_REMOVE
                    receipt = get_mcp_runtime().permission_policy(
                        {
                            "action": "remove_rule",
                            "rule_id": parts[2],
                            "expected_revision": payload.get("expected_revision"),
                            "actor_id": authority.actor_id,
                        }
                    )
                elif parts == ["permissions", "mode"]:
                    operation = PermissionApiOperation.MODE_UPDATE
                    receipt = get_mcp_runtime().permission_policy(
                        {
                            "action": "mode",
                            "mode": payload.get("mode"),
                            "expected_revision": payload.get("expected_revision"),
                            "actor_id": authority.actor_id,
                            "reason": str(payload.get("reason") or "permission API mode update"),
                        }
                    )
                else:
                    return False
                response = PermissionApiResponse(
                    status=status,
                    operation=operation,
                    body={
                        "schema": "zyra.permission-api.v2",
                        "ok": 200 <= int(status) < 300,
                        "operation": operation.value,
                        "state_owner": "typescript.PermissionCoordinator",
                        "canonical_entrypoint": "E02CapabilityCoordinator.resumePermission",
                        "python_decision_fallback": False,
                        "session_id": authority.session_id,
                        "receipt": receipt,
                        **({"error": receipt.get("error"), "message": receipt.get("message")} if receipt.get("error") else {}),
                    },
                    events=response_events,
                    headers={
                        "Cache-Control": "no-store, max-age=0",
                        "Pragma": "no-cache",
                        "X-Zyra-Permission-State-Owner": "typescript.PermissionCoordinator",
                        "X-Zyra-Python-Decision-Fallback": "false",
                    },
                )
        except TypeScriptE02PortError as error:
            response = PermissionApiResponse(
                status=(
                    HTTPStatus.NOT_FOUND
                    if "not_found" in error.code
                    else HTTPStatus.FORBIDDEN
                    if "permission" in error.code or "forbidden" in error.code
                    else HTTPStatus.CONFLICT
                ),
                operation=operation,
                body={
                    "schema": "zyra.permission-api.v2",
                    "ok": False,
                    "operation": operation.value,
                    "error": error.code,
                    "message": str(error),
                    "detail": error.detail,
                    "state_owner": "typescript.PermissionCoordinator",
                    "python_decision_fallback": False,
                },
                headers={"Cache-Control": "no-store, max-age=0"},
            )
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

        worker_pool_response = get_worker_pool_api().route_get(
            tuple(parts),
            _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
        )
        if worker_pool_response is not None:
            self._send_json(
                worker_pool_response.status,
                dict(worker_pool_response.body),
                headers=dict(worker_pool_response.headers),
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "faults":
            fault_task = store.load_task(parts[1])
            fault_api = get_fault_runtime_api(store)
            fault_response = fault_api.route_get(
                tuple(parts),
                task_state=fault_task,
            )
            self._send_json(
                fault_response.status,
                dict(fault_response.body),
                headers=dict(fault_response.headers),
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "code-index":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            try:
                response = get_code_index_service().status(
                    state.task_id,
                    request_id=self.headers.get("X-Request-Id", ""),
                )
            except CodeIndexServiceError as error:
                self._send_json(error.status, error.to_dict())
                return
            except WorkspaceError as error:
                mapped = workspace_error_response(error)
                self._send_json(mapped.status, mapped.body, headers=dict(mapped.headers))
                return
            except (CodeIndexError, ValueError) as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "code_index_status_failed", "message": str(error), "fallback": False},
                )
                return
            self._send_json(response.status, response.to_dict())
            return

        provider_backend_response = ProviderBackendApi(
            project_root=PROJECT_ROOT,
            artifact_root=artifact_root_path(),
        ).handle_get(
            parts,
            _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
        )
        if provider_backend_response is not None:
            self._send_json(
                provider_backend_response.status,
                provider_backend_response.body,
                headers=dict(provider_backend_response.headers),
            )
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

        if parts == ["runtime-events"]:
            try:
                result = get_runtime_event_api().list_events(
                    _flatten_query(parse_qs(parsed.query, keep_blank_values=True))
                )
            except (ValueError, RuntimeEventProcessError) as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE
                    if isinstance(error, RuntimeEventProcessError)
                    else HTTPStatus.BAD_REQUEST,
                    {
                        "error": getattr(error, "code", "invalid_runtime_event_query"),
                        "message": str(error),
                    },
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if parts == ["runtime-events", "health"]:
            try:
                result = get_runtime_event_api().health()
            except RuntimeEventProcessError as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": error.code, "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if parts == ["runtime-events", "metrics"]:
            try:
                result = get_runtime_event_api().metrics()
            except RuntimeEventProcessError as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": error.code, "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if parts == ["runtime-events", "baselines"]:
            try:
                result = get_runtime_event_api().baselines()
            except RuntimeEventProcessError as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": error.code, "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if parts == ["runtime-events", "reconcile"]:
            try:
                result = get_runtime_event_api().reconciliation(
                    _flatten_query(parse_qs(parsed.query, keep_blank_values=True))
                )
            except (ValueError, RuntimeEventProcessError) as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE
                    if isinstance(error, RuntimeEventProcessError)
                    else HTTPStatus.BAD_REQUEST,
                    {"error": getattr(error, "code", "runtime_reconciliation_failed"), "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if len(parts) == 2 and parts[0] == "runtime-event-artifacts":
            try:
                result = get_runtime_event_api().read_artifact(
                    parts[1],
                    _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
                )
            except (ValueError, RuntimeEventProcessError) as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE
                    if isinstance(error, RuntimeEventProcessError)
                    else HTTPStatus.BAD_REQUEST,
                    {"error": getattr(error, "code", "runtime_artifact_read_failed"), "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if len(parts) == 2 and parts[0] == "runtime-events":
            try:
                result = get_runtime_event_api().get_event(parts[1])
            except RuntimeEventProcessError as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": error.code, "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if len(parts) == 3 and parts[0] == "runtime-events" and parts[2] == "causal-chain":
            query = _flatten_query(parse_qs(parsed.query, keep_blank_values=True))
            try:
                result = get_runtime_event_api().causal_chain(
                    parts[1],
                    max_depth=_positive_int(str(query.get("max_depth", "256")), default=256),
                )
            except (ValueError, RuntimeEventProcessError) as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE
                    if isinstance(error, RuntimeEventProcessError)
                    else HTTPStatus.BAD_REQUEST,
                    {"error": getattr(error, "code", "invalid_causal_chain_query"), "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "runtime-events":
            try:
                result = get_runtime_event_api().list_task_events(
                    parts[1],
                    _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
                )
            except (ValueError, RuntimeEventProcessError) as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE
                    if isinstance(error, RuntimeEventProcessError)
                    else HTTPStatus.BAD_REQUEST,
                    {"error": getattr(error, "code", "invalid_runtime_event_query"), "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "runtime-event-history":
            try:
                result = get_runtime_event_api().get_task_history(
                    parts[1],
                    _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
                )
            except (ValueError, RuntimeEventProcessError) as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE
                    if isinstance(error, RuntimeEventProcessError)
                    else HTTPStatus.BAD_REQUEST,
                    {"error": getattr(error, "code", "invalid_runtime_event_history_query"), "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "runtime-event-projection":
            try:
                result = get_runtime_event_api().get_task_projection(parts[1])
            except RuntimeEventProcessError as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": error.code, "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "runtime-event-stream":
            try:
                result = get_runtime_event_api().get_task_projection_stream(
                    parts[1],
                    _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
                )
            except (ValueError, RuntimeEventProcessError) as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE
                    if isinstance(error, RuntimeEventProcessError)
                    else HTTPStatus.BAD_REQUEST,
                    {"error": getattr(error, "code", "runtime_projection_stream_failed"), "message": str(error)},
                )
                return
            self._send_json(result.status, dict(result.body), headers=dict(result.headers))
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
            query = parse_qs(parsed.query)
            search_text = str((query.get("q") or query.get("query") or [""])[0]).strip()
            projection = get_mcp_runtime().skills(query=search_text)
            projection.setdefault("canonical_entrypoint", "E02CapabilityCoordinator.execute")
            projection.setdefault("python_registry_enabled", False)
            self._send_json(HTTPStatus.OK, projection, headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Zyra-Skill-State-Owner": "SkillCoordinator",
                "X-Zyra-Python-Decision-Fallback": "false",
            })
            return

        if parts == ["tools"]:
            projection = get_mcp_runtime().tools()
            projection.setdefault("canonical_entrypoint", "E02CapabilityCoordinator.execute")
            projection.setdefault("python_registry_enabled", False)
            self._send_json(HTTPStatus.OK, projection, headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Zyra-Tool-Registry-Owner": "E02CapabilityCoordinator",
                "X-Zyra-Python-Decision-Fallback": "false",
            })
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

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "memory" and parts[3] == "curator":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            runtime = get_memory_curator_runtime(store)
            candidate_store = runtime.worker.candidate_store
            integration = (
                runtime.integration_application.status(task_id=state.task_id)
                if runtime.integration_application is not None
                else None
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "task_id": state.task_id,
                    "run_id": state.run_id,
                    "health": runtime.worker.health(),
                    "jobs": [item.to_dict() for item in candidate_store.jobs(task_id=state.task_id, limit=100)],
                    "candidates": [item.to_dict() for item in candidate_store.candidates(task_id=state.task_id, limit=1000)],
                    "outbox": [item.to_dict() for item in candidate_store.outbox_messages(task_id=state.task_id, limit=1000)],
                    "canonical_memory_owner": "SQLiteStore.memory_records",
                    "candidate_store_is_separate": True,
                    "model_can_write": False,
                    "integration": integration,
                    "direct_runtime_event_input": runtime.integration_application is not None,
                },
                headers={"Cache-Control": "no-store, max-age=0"},
            )
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "memory" and parts[3] == "procedures":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            try:
                runtime = get_reusable_procedure_runtime(store)
                status = runtime.status(state.task_id)
                procedures = runtime.export_for_typescript(state.task_id, limit=1000)
            except (ProcedureContractError, RuntimeError, ValueError) as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": getattr(error, "code", "procedure_status_failed"),
                        "message": str(error),
                        "task_id": state.task_id,
                    },
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "protocol": "zyra.reusable-procedure-api/v1",
                    "task_id": state.task_id,
                    "run_id": state.run_id,
                    "status": status.to_dict(),
                    "procedures": list(procedures),
                    "source_owner": "06B CuratorIntegrationStore",
                    "procedure_owner": "06C ReusableProcedureStore",
                    "skill_execution_owner": "03C SkillCoordinator",
                    "model_can_activate": False,
                },
                headers={"Cache-Control": "no-store, max-age=0"},
            )
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
            runtime = get_typescript_agent_port()
            tasks = runtime.records(parent_task_id=state.task_id)
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

        worker_pool_task = (
            store.load_task(parts[1])
            if len(parts) >= 2 and parts[0] == "tasks"
            else None
        )
        worker_pool_response = get_worker_pool_api().route_post(
            tuple(parts),
            payload,
            task_state=worker_pool_task,
        )
        if worker_pool_response is not None:
            self._send_json(
                worker_pool_response.status,
                dict(worker_pool_response.body),
                headers=dict(worker_pool_response.headers),
            )
            return

        if (
            len(parts) in {4, 5}
            and parts[0] == "tasks"
            and parts[2] == "faults"
            and "/".join(parts[3:])
            in {
                "inject",
                "observers",
                "sources/bind",
                "sources/observe",
                "runtime-events",
                "observations",
                "handoffs/dispatch",
            }
        ):
            fault_api = get_fault_runtime_api(store)
            fault_response = fault_api.route_post(
                tuple(parts),
                payload,
                task_state=worker_pool_task,
                requested_by=str(payload.get("actor_id") or "api-user"),
            )
            if worker_pool_task is not None and fault_response.status < HTTPStatus.BAD_REQUEST:
                store.save_checkpoint(worker_pool_task)
            self._send_json(
                fault_response.status,
                dict(fault_response.body),
                headers=dict(fault_response.headers),
            )
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "code-index":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            operations = {
                "rebuild": CodeIndexOperation.REBUILD,
                "search": CodeIndexOperation.SEARCH,
                "symbols": CodeIndexOperation.SYMBOLS,
                "context": CodeIndexOperation.CONTEXT,
                "select-tests": CodeIndexOperation.SELECT_TESTS,
                "invalidate": CodeIndexOperation.INVALIDATE,
                "reconcile": CodeIndexOperation.RECONCILE,
            }
            operation = operations.get(parts[3])
            if operation is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "code_index_operation_not_found", "operation": parts[3]},
                )
                return
            try:
                response = get_code_index_service().execute(
                    state.task_id,
                    operation,
                    payload,
                    request_id=self.headers.get("X-Request-Id", ""),
                    causation_id=str(payload.get("causation_id") or ""),
                )
            except CodeIndexServiceError as error:
                self._send_json(error.status, error.to_dict())
                return
            except WorkspaceError as error:
                mapped = workspace_error_response(error)
                self._send_json(mapped.status, mapped.body, headers=dict(mapped.headers))
                return
            except (CodeIndexError, ValueError) as error:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "code_index_operation_failed", "message": str(error), "fallback": False},
                )
                return
            self._send_json(response.status, response.to_dict())
            return

        provider_backend_response = ProviderBackendApi(
            project_root=PROJECT_ROOT,
            artifact_root=artifact_root_path(),
        ).handle_post(parts, payload)
        if provider_backend_response is not None:
            self._send_json(
                provider_backend_response.status,
                provider_backend_response.body,
                headers=dict(provider_backend_response.headers),
            )
            return

        mcp_runtime = get_mcp_runtime()
        mcp_response = McpApiFacade(mcp_runtime).handle_post(
            parts,
            payload,
            self._permission_actor_id(),
        )
        if mcp_response is not None:
            status, body, headers = mcp_response
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
            graph_events = ensure_default_graph(state)
            events = [
                created_event,
                *graph_events,
                *drain_workspace_events(state.task_id),
            ]
            pool_api = get_worker_pool_api()
            pool_journal = pool_api.pool.store.journal(limit=10000)
            pool_sequence = pool_journal[-1].sequence if pool_journal else 0
            try:
                pool_api.acquire_for_task(
                    state,
                    payload=(
                        payload.get("worker_pool")
                        if isinstance(payload.get("worker_pool"), dict)
                        else {}
                    ),
                )
            except Exception as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": "worker_pool_acquisition_failed",
                        "message": str(error),
                        "task_id": state.task_id,
                        "fallback": False,
                    },
                )
                return
            if auto_run:
                events.extend(run_task_graph(state, execution_context=graph_execution_context()))
                pool_api.finalize_task(
                    state,
                    success=str(state.status) == "completed",
                    summary=f"default task graph finished with status {state.status}",
                )
            events.extend(
                event
                for event in pool_api.pool.events.project_after(pool_api.pool.store, pool_sequence)
                if event.run_id == state.run_id and event.task_id == state.task_id
            )
            persist_events(store, events)
            store.save_checkpoint(state)
            curator = curate_terminal_task(store, state) if auto_run else None
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "task": to_jsonable(state),
                    "events": [to_jsonable(event) for event in events],
                    "memory_curator": curator,
                },
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "run":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            pool_api = get_worker_pool_api()
            pool_journal = pool_api.pool.store.journal(limit=10000)
            pool_sequence = pool_journal[-1].sequence if pool_journal else 0
            pool_api.ensure_task_lease(state, payload={})
            events = run_task_graph(state, execution_context=graph_execution_context())
            pool_api.finalize_task(
                state,
                success=str(state.status) == "completed",
                summary=f"task run finished with status {state.status}",
            )
            events.extend(
                event
                for event in pool_api.pool.events.project_after(pool_api.pool.store, pool_sequence)
                if event.run_id == state.run_id and event.task_id == state.task_id
            )
            persist_events(store, events)
            store.save_checkpoint(state)
            curator = curate_terminal_task(store, state)
            self._send_json(
                HTTPStatus.OK,
                {
                    "task": to_jsonable(state),
                    "events": [to_jsonable(event) for event in events],
                    "memory_curator": curator,
                },
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            reason = str(payload.get("reason") or "Cancelled by control API.")
            backend_cancel = cancel_pending_dispatches(
                store_path=backend_registry_path(artifact_root_path()),
                run_id=state.run_id,
                task_id=state.task_id,
                reason=reason,
                requested_by="task-control-api",
                idempotency_key=f"task-cancel:{state.run_id}:{state.task_id}:{reason}",
            )
            events = cancel_task_graph(state, reason=reason)
            pool_cancel = get_worker_pool_api().integration.control.submit_and_apply(
                ControlKind.CANCEL,
                claim_owner="task-control-api",
                actor_id="task-control-api",
                reason=reason,
                idempotency_key=(
                    f"task-cancel:{state.task_id}:"
                    + hashlib.sha256(reason.encode("utf-8")).hexdigest()[:16]
                ),
                task_id=state.task_id,
                run_id=state.run_id,
            )
            agent_port = get_typescript_agent_port()
            cancelled_subagents = []
            cancelled_physical_children: list[dict[str, Any]] = []
            cancelled_physical_task_ids: set[str] = set()
            subagent_cancel_errors: list[dict[str, str]] = []
            for child in agent_port.records(parent_task_id=state.task_id):
                if child.status.terminal:
                    continue
                cancel_request_id = new_id("parent-agent-cancel")
                cancel_run = _run_typescript_agent_request(
                    state,
                    tool_name="agent_cancel",
                    arguments={
                        "task_id": child.task_id,
                        "expected_revision": child.revision,
                        "reason": reason,
                        "idempotency_key": f"parent-cancel:{state.task_id}:{child.task_id}:{child.revision}",
                    },
                    request_id=cancel_request_id,
                    session_id=child.parent_session_id,
                    session_custody_token=extract_bearer_token(self.headers, payload),
                )
                events.extend(cancel_run.event_records)
                if cancel_run.worker_result.ok:
                    cancelled_subagents.append(agent_port.get_task(child.task_id))
                    physical = get_worker_pool_api().integration.control.submit_and_apply(
                        ControlKind.CANCEL,
                        claim_owner="task-control-api",
                        actor_id="task-control-api",
                        reason=reason,
                        idempotency_key=(
                            f"parent-cancel-physical:{state.task_id}:"
                            f"{child.task_id}:{child.revision}"
                        ),
                        task_id=child.task_id,
                        run_id=state.run_id,
                    )
                    if physical.phase.value != "applied":
                        subagent_cancel_errors.append({
                            "task_id": child.task_id,
                            "error": physical.error or "physical_child_lease_cancel_failed",
                        })
                    else:
                        cancelled_physical_children.append(physical.to_dict())
                        cancelled_physical_task_ids.add(child.task_id)
                else:
                    subagent_cancel_errors.append({
                        "task_id": child.task_id,
                        "error": str(cancel_run.worker_result.error or "typescript_agent_cancel_rejected"),
                    })
            # Admission deliberately precedes E03 execution. A permission-
            # suspended or crashed child can therefore own a physical binding
            # before the TypeScript logical registry contains a task record.
            # Sweep parent-correlated bindings so those leases cannot survive a
            # parent cancel merely because logical creation never committed.
            for binding in get_worker_pool_api().integration.repository.list_bindings(
                run_id=state.run_id
            ):
                if binding.terminal or binding.task_id in cancelled_physical_task_ids:
                    continue
                if str(binding.metadata.get("parent_task_id") or "") != state.task_id:
                    continue
                physical = get_worker_pool_api().integration.control.submit_and_apply(
                    ControlKind.CANCEL,
                    claim_owner="task-control-api",
                    actor_id="task-control-api",
                    reason=reason,
                    idempotency_key=(
                        f"parent-cancel-orphan-physical:{state.task_id}:"
                        f"{binding.binding_id}:{binding.version}"
                    ),
                    task_id=binding.task_id,
                    run_id=state.run_id,
                    binding_id=binding.binding_id,
                )
                if physical.phase.value != "applied":
                    subagent_cancel_errors.append({
                        "task_id": binding.task_id,
                        "error": physical.error or "orphan_physical_child_lease_cancel_failed",
                    })
                else:
                    cancelled_physical_children.append(physical.to_dict())
                    cancelled_physical_task_ids.add(binding.task_id)
            persist_events(store, events)
            store.save_checkpoint(state)
            curator = curate_terminal_task(store, state)
            self._send_json(
                HTTPStatus.OK,
                {
                    "task": to_jsonable(state),
                    "events": [to_jsonable(event) for event in events],
                    "cancelled_subagents": [item.safe_dict() for item in cancelled_subagents],
                    "cancelled_physical_children": cancelled_physical_children,
                    "subagent_cancel_errors": subagent_cancel_errors,
                    "canonical_agent_owner": "typescript",
                    "backend_dispatch_control": backend_cancel.to_dict(),
                    "worker_pool_control": {
                        **pool_cancel.to_dict(),
                        **dict(pool_cancel.effect.get("cancellation") or {}),
                    },
                    "memory_curator": curator,
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
            authority_error = _agent_api_authority_error(payload)
            if authority_error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "subagent_fanout_rejected", "message": authority_error},
                )
                return
            request_id = str(payload.get("request_id") or new_id("agentfanout"))
            owner_session_id = str(
                payload.get("session_id")
                or state.metadata.get("query_session_id")
                or f"task:{state.task_id}"
            )
            prepared_requests: list[dict[str, Any]] = []
            physical_dispatches: list[dict[str, Any]] = []
            physical_workers: list[dict[str, Any]] = []
            try:
                for index, item in enumerate(raw_items):
                    if not isinstance(item, dict):
                        continue
                    child = dict(item)
                    child_task_id = str(child.get("task_id") or new_id(f"agenttask-{index + 1}"))
                    child["task_id"] = child_task_id
                    dispatch, public = _acquire_subagent_physical_dispatch(
                        state,
                        task_id=child_task_id,
                        owner_session_id=owner_session_id,
                        idempotency_key=f"{request_id}:{index}",
                    )
                    child["physical_dispatch"] = dispatch
                    prepared_requests.append(child)
                    physical_dispatches.append(dispatch)
                    physical_workers.append(public)
            except Exception as error:
                for projection in physical_dispatches:
                    lease = get_worker_pool_api().pool.store.get_lease(str(projection.get("lease_id") or ""))
                    if lease is not None and not lease.terminal:
                        get_worker_pool_api().pool.leases.cancel(
                            lease.lease_id,
                            reason="fanout physical admission failed",
                        )
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "subagent_worker_pool_acquisition_failed", "message": str(error)},
                )
                return
            arguments = {
                "prompt": str(payload.get("shared_context") or "fanout"),
                "requests": [
                    {
                        **dict(item),
                        "prompt": (
                            str(payload.get("shared_context") or "")
                            + "\n\n"
                            + str(item.get("prompt") or "")
                        ).strip(),
                    }
                    for item in prepared_requests
                ],
                "failure_mode": str(payload.get("failure_policy") or "collect"),
                "maximum_concurrency": max(
                    1,
                    min(len(prepared_requests), int(payload.get("maximum_concurrency") or 4)),
                ),
                "idempotency_key": str(payload.get("idempotency_key") or request_id),
                "budget": {"max_children": max(2, int(payload.get("maximum_concurrency") or 4))},
            }
            try:
                run = _run_typescript_agent_request(
                    state,
                    arguments=arguments,
                    request_id=request_id,
                    session_id=owner_session_id,
                    session_custody_token=extract_bearer_token(self.headers, payload),
                )
            except Exception as error:
                failed_receipts = [
                    _settle_subagent_physical_dispatch(
                        projection,
                        status="failed",
                        summary=str(error),
                    )
                    for projection in physical_dispatches
                ]
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": "typescript_subagent_fanout_execution_failed",
                        "message": str(error),
                        "physical_workers": physical_workers,
                        "physical_receipts": failed_receipts,
                    },
                )
                return
            persist_events(store, run.event_records)
            records_by_id = {
                item.task_id: item
                for item in get_typescript_agent_port().records(parent_task_id=state.task_id)
            }
            physical_receipts = []
            for projection in physical_dispatches:
                selected = records_by_id.get(str(projection.get("task_id") or ""))
                selected_status = (
                    selected.status.value
                    if selected is not None
                    else (
                        "running"
                        if str(run.worker_result.error or "") == "permission_suspended"
                        else ("failed" if not run.worker_result.ok else "running")
                    )
                )
                receipt = _settle_subagent_physical_dispatch(
                    projection,
                    status=selected_status,
                    summary=(
                        str(selected.payload.get("error") or selected_status)
                        if selected is not None
                        else str(run.worker_result.error or selected_status)
                    ),
                )
                physical_receipts.append(receipt)
            status = HTTPStatus.CREATED if run.worker_result.ok else HTTPStatus.CONFLICT
            self._send_json(status, {
                "ok": run.worker_result.ok,
                "error": run.worker_result.error,
                "worker_result": to_jsonable(run.worker_result),
                "events": [to_jsonable(item) for item in run.event_records],
                "request_id": request_id,
                "permission_session": _agent_permission_session(run),
                "canonical_agent_owner": "typescript",
                "python_agent_fallback": False,
                "physical_workers": physical_workers,
                "physical_receipts": physical_receipts,
            }, headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "subagents":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            authority_error = _agent_api_authority_error(payload)
            if authority_error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "subagent_spawn_rejected", "message": authority_error},
                )
                return
            request_id = str(payload.get("request_id") or new_id("agentspawn"))
            task_id = str(payload.get("subagent_task_id") or new_id("agenttask"))
            owner_session_id = str(
                payload.get("session_id")
                or state.metadata.get("query_session_id")
                or f"task:{state.task_id}"
            )
            try:
                physical_dispatch, physical_worker = _acquire_subagent_physical_dispatch(
                    state,
                    task_id=task_id,
                    owner_session_id=owner_session_id,
                    idempotency_key=str(payload.get("idempotency_key") or request_id),
                )
            except Exception as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "subagent_worker_pool_acquisition_failed", "message": str(error)},
                )
                return
            arguments = {
                "prompt": str(payload.get("prompt") or ""),
                "agent_type": str(payload.get("agent_type") or "general-purpose"),
                "task_id": task_id,
                "idempotency_key": str(payload.get("idempotency_key") or request_id),
                "background": str(payload.get("execution_mode") or "background") == "background",
                "context_mode": str(payload.get("context_mode") or "isolated"),
                "tools": list(payload.get("requested_tools") or ()),
                "messages": list(payload.get("messages") or ()),
                "context": {
                    "artifact_refs": [item.artifact_id for item in state.artifacts],
                    "evidence_refs": list(payload.get("evidence_refs") or ()),
                    "context_epoch": int(state.metadata.get("context_epoch") or 0),
                    "compact_boundary_id": str(state.metadata.get("compact_boundary_id") or ""),
                },
                "physical_dispatch": physical_dispatch,
            }
            try:
                run = _run_typescript_agent_request(
                    state,
                    arguments=arguments,
                    request_id=request_id,
                    session_id=owner_session_id,
                    session_custody_token=extract_bearer_token(self.headers, payload),
                )
            except Exception as error:
                physical_receipt = _settle_subagent_physical_dispatch(
                    physical_dispatch,
                    status="failed",
                    summary=str(error),
                )
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": "typescript_subagent_execution_failed",
                        "message": str(error),
                        "physical_worker": physical_worker,
                        "physical_receipt": physical_receipt,
                    },
                )
                return
            persist_events(store, run.event_records)
            records = get_typescript_agent_port().records(parent_task_id=state.task_id)
            selected = next((item for item in records if item.task_id == task_id), None)
            selected_status = (
                selected.status.value
                if selected is not None
                else (
                    "running"
                    if str(run.worker_result.error or "") == "permission_suspended"
                    else ("failed" if not run.worker_result.ok else "running")
                )
            )
            physical_receipt = _settle_subagent_physical_dispatch(
                physical_dispatch,
                status=selected_status,
                summary=(
                    str(selected.payload.get("error") or selected_status)
                    if selected is not None
                    else str(run.worker_result.error or selected_status)
                ),
            )
            status = HTTPStatus.CREATED if run.worker_result.ok else HTTPStatus.CONFLICT
            self._send_json(status, {
                "ok": run.worker_result.ok,
                "error": run.worker_result.error,
                "record": selected.safe_dict() if selected is not None else None,
                "worker_result": to_jsonable(run.worker_result),
                "events": [to_jsonable(item) for item in run.event_records],
                "request_id": request_id,
                "subagent_task_id": task_id,
                "permission_session": _agent_permission_session(run),
                "canonical_agent_owner": "typescript",
                "python_agent_fallback": False,
                "physical_worker": physical_worker,
                "physical_receipt": physical_receipt,
            }, headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})
            return

        if len(parts) == 5 and parts[0] == "tasks" and parts[2] == "subagents":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            runtime = get_typescript_agent_port()
            try:
                record = runtime.get_task(parts[3])
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "subagent_not_found"})
                return
            if record.parent_task_id != state.task_id:
                self._send_json(HTTPStatus.CONFLICT, {"error": "subagent_parent_mismatch"})
                return
            action = parts[4]
            if action not in {"cancel", "message", "resume", "status", "background"}:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "subagent_control_not_found"})
                return
            if action == "status":
                self._send_json(HTTPStatus.OK, {
                    "ok": True,
                    "task": record.safe_dict(),
                    "canonical_agent_owner": "typescript",
                    "durable_owner": "SubagentTaskStore",
                })
                return
            if action == "background":
                self._send_json(HTTPStatus.CONFLICT, {
                    "ok": False,
                    "error": "agent_execution_mode_owned_by_typescript",
                })
                return
            expected = payload.get("expected_task_revision")
            if not isinstance(expected, int):
                self._send_json(HTTPStatus.CONFLICT, {
                    "ok": False,
                    "error": "expected_task_revision_required",
                })
                return
            arguments = {
                "task_id": record.task_id,
                "expected_revision": expected,
            }
            if action == "cancel":
                arguments["reason"] = str(payload.get("reason") or "api_cancel")
            elif action == "message":
                arguments.update({
                    "message_id": str(payload.get("message_id") or new_id("agentmsg")),
                    "message": str(payload.get("message") or payload.get("summary") or ""),
                    "intent": str(payload.get("intent") or "message"),
                    "state_delta": dict(payload.get("state_delta") or {}),
                })
            else:
                correlation_id = str(payload.get("resume_correlation_id") or "")
                if not correlation_id:
                    self._send_json(HTTPStatus.CONFLICT, {"error": "resume_correlation_id_required"})
                    return
                arguments.update({
                    "resume_correlation_id": correlation_id,
                    "restored_state": dict(payload.get("restored_state") or {}),
                    "turns": list(payload.get("turns") or ()),
                })
            request_id = str(payload.get("request_id") or new_id(f"agent{action}"))
            physical_control = None
            if action == "resume":
                physical_binding = get_worker_pool_api().integration.repository.latest_binding(
                    record.task_id
                )
                if physical_binding is not None and physical_binding.phase.value == "parked":
                    revived = get_worker_pool_api().integration.control.submit_and_apply(
                        ControlKind.REVIVE,
                        claim_owner="subagent-control-api",
                        actor_id="subagent-control-api",
                        reason="logical subagent resume requires physical dispatch revive",
                        idempotency_key=f"subagent-revive:{record.task_id}:{expected}",
                        task_id=record.task_id,
                        run_id=state.run_id,
                        binding_id=physical_binding.binding_id,
                    )
                    physical_control = revived.to_dict()
                    if revived.phase.value != "applied":
                        self._send_json(
                            HTTPStatus.CONFLICT,
                            {
                                "ok": False,
                                "error": "subagent_physical_revive_failed",
                                "message": revived.error,
                                "physical_control": physical_control,
                            },
                        )
                        return
            run = _run_typescript_agent_request(
                state,
                tool_name={
                    "cancel": "agent_cancel",
                    "message": "agent_message",
                    "resume": "agent_resume",
                }[action],
                arguments=arguments,
                request_id=request_id,
                session_id=record.parent_session_id,
                session_custody_token=extract_bearer_token(self.headers, payload),
            )
            persist_events(store, run.event_records)
            updated = runtime.get_task(record.task_id)
            if action == "cancel" and run.worker_result.ok:
                physical_control = get_worker_pool_api().integration.control.submit_and_apply(
                    ControlKind.CANCEL,
                    claim_owner="subagent-control-api",
                    actor_id="subagent-control-api",
                    reason=str(payload.get("reason") or "api_cancel"),
                    idempotency_key=f"subagent-cancel:{record.task_id}:{expected}",
                    task_id=record.task_id,
                    run_id=state.run_id,
                ).to_dict()
            self._send_json(
                HTTPStatus.OK if run.worker_result.ok else HTTPStatus.CONFLICT,
                {
                    "ok": run.worker_result.ok,
                    "error": run.worker_result.error,
                    "task": updated.safe_dict(),
                    "worker_result": to_jsonable(run.worker_result),
                    "events": [to_jsonable(item) for item in run.event_records],
                    "request_id": request_id,
                    "permission_session": _agent_permission_session(run),
                    "canonical_agent_owner": "typescript",
                    "python_agent_fallback": False,
                    "physical_worker_control": physical_control,
                },
                headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
            )
            return

        if len(parts) >= 4 and parts[0] == "tasks" and parts[2] == "memory" and parts[3] == "procedures":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            operation = (
                parts[4]
                if len(parts) == 5
                else str(payload.get("operation") or "mine")
            )
            try:
                runtime = get_reusable_procedure_runtime(store)
                if operation == "mine":
                    raw_outcome_ids = payload.get("outcome_ids", ())
                    if not isinstance(raw_outcome_ids, (list, tuple)):
                        raise ProcedureContractError(
                            "procedure_outcome_ids_invalid",
                            "outcome_ids must be a list of curator outcome ids",
                        )
                    results = runtime.mine_task(
                        state.task_id,
                        outcome_ids=tuple(
                            str(item)
                            for item in raw_outcome_ids
                            if str(item).strip()
                        ),
                        limit=int(payload.get("limit", 1000)),
                    )
                    body = {
                        "protocol": "zyra.reusable-procedure-api/v1",
                        "operation": operation,
                        "task_id": state.task_id,
                        "results": [item.to_dict() for item in results],
                        "status": runtime.status(state.task_id).to_dict(),
                    }
                    response_status = HTTPStatus.CREATED if results else HTTPStatus.OK
                elif operation in {"routing", "recovery", "context"}:
                    query_result = getattr(runtime, operation)(state.task_id, payload)
                    body = {
                        "protocol": "zyra.reusable-procedure-api/v1",
                        "operation": operation,
                        "task_id": state.task_id,
                        "result": query_result.to_dict(),
                        "model_can_activate": False,
                        "static_document_can_activate": False,
                    }
                    response_status = HTTPStatus.OK
                else:
                    raise ProcedureContractError(
                        "procedure_operation_unknown",
                        f"unsupported procedure operation: {operation}",
                    )
            except (ProcedureContractError, RuntimeError, ValueError, KeyError) as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": getattr(error, "code", "procedure_operation_failed"),
                        "message": str(error),
                        "operation": operation,
                        "task_id": state.task_id,
                    },
                )
                return
            self._send_json(
                response_status,
                body,
                headers={"Cache-Control": "no-store, max-age=0"},
            )
            return

        if len(parts) >= 4 and parts[0] == "tasks" and parts[2] == "memory" and parts[3] == "curator":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            operation = str(payload.get("operation") or "schedule_manual")
            if len(parts) == 5:
                operation = {
                    "task-end": "schedule_task_end",
                    "recover": "recover",
                    "run-next": "run_next",
                    "run-job": "run_job",
                }.get(parts[4], operation)
            try:
                request = MemoryCuratorWorkerRequest.from_dict(
                    {
                        **payload,
                        "operation": operation,
                        "task_id": state.task_id,
                        "requested_by": str(payload.get("requested_by") or "memory-curator-api"),
                    }
                )
                response = get_memory_curator_runtime(store).execute(request)
            except (ValueError, RuntimeError, KeyError) as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": getattr(error, "code", "memory_curator_failed"),
                        "message": str(error),
                        "task_id": state.task_id,
                    },
                )
                return
            self._send_json(
                HTTPStatus.CREATED if response.scheduled is not None else HTTPStatus.OK,
                response.to_dict(),
                headers={"Cache-Control": "no-store, max-age=0"},
            )
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
            e02_route = _e02_command_route(text)
            if e02_route is not None:
                command_name, operation = e02_route
                tool_call_id = str(payload.get("tool_call_id") or new_id("e02command"))
                try:
                    execution = get_mcp_runtime().execute(
                        "command",
                        {"input": text},
                        identity={
                            "tool_call_id": tool_call_id,
                            "command_name": command_name,
                            "operation": operation,
                            "permit_id": str(payload.get("permit_id") or ""),
                            "actor_id": str(payload.get("actor_id") or "api-user"),
                            "correlation_id": str(payload.get("correlation_id") or tool_call_id),
                        },
                    )
                except TypeScriptE02PortError as error:
                    status = HTTPStatus.FORBIDDEN if "permission" in error.code else HTTPStatus.CONFLICT
                    self._send_json(
                        status,
                        {
                            "ok": False,
                            "error": error.code,
                            "message": str(error),
                            "detail": error.detail,
                            "tool_call_id": tool_call_id,
                            "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                            "canonical_command_owner": "typescript.CommandCoordinator",
                            "python_parser_fallback": False,
                            "python_dispatch_fallback": False,
                        },
                        headers={"Cache-Control": "no-store, max-age=0"},
                    )
                    return
                receipt = dict(execution.get("receipt") or {})
                capability_result = dict(receipt.get("result") or {})
                command_output = dict(capability_result.get("output") or {})
                invocation = dict(command_output.get("invocation") or {})
                invocation_status = str(invocation.get("status") or "failed")
                if invocation_status not in {"completed", "pending_approval", "denied"}:
                    self._send_json(
                        HTTPStatus.CONFLICT,
                        {
                            "ok": False,
                            "error": "typescript_command_receipt_invalid",
                            "message": "TypeScript command execution returned no terminal invocation receipt.",
                            "receipt": receipt,
                            "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                            "python_dispatch_fallback": False,
                        },
                        headers={"Cache-Control": "no-store, max-age=0"},
                    )
                    return
                status = (
                    HTTPStatus.CREATED
                    if invocation_status == "completed"
                    else HTTPStatus.ACCEPTED
                    if invocation_status == "pending_approval"
                    else HTTPStatus.FORBIDDEN
                )
                command_result = {
                    **invocation,
                    "name": f"/{command_name}",
                    "summary": str(capability_result.get("summary") or f"Command {command_name} {invocation_status}"),
                    "data": invocation.get("output"),
                    "runtime_status": "typescript_stateful",
                    "receipt": receipt,
                    "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                    "canonical_command_owner": "typescript.CommandCoordinator",
                    "python_parser_fallback": False,
                    "python_dispatch_fallback": False,
                }
                self._send_json(
                    status,
                    {
                        "task": to_jsonable(state),
                        "control_request": {
                            "input": text,
                            "canonical_name": f"/{command_name}",
                            "tool_call_id": tool_call_id,
                            "transport_only": True,
                        },
                        "command": {
                            "name": f"/{command_name}",
                            "operation": operation,
                            "canonical_owner": "typescript.CommandCoordinator",
                        },
                        "command_result": command_result,
                        "event": receipt.get("commit"),
                        "event_only_stateful_fallback": False,
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
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
            tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
            arguments = payload.get("arguments")
            if not tool_name or not isinstance(arguments, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_tool_request", "message": "tool_name and object arguments are required"},
                )
                return
            tool_call_id = str(payload.get("tool_call_id") or new_id("toolcall"))
            try:
                execution = get_mcp_runtime().execute(
                    tool_name,
                    arguments,
                    identity={
                        "tool_call_id": tool_call_id,
                        "namespace": str(payload.get("namespace") or ""),
                        "server_id": str(payload.get("server_id") or ""),
                        "operation": str(payload.get("operation") or "api.execute"),
                        "actor_id": self._permission_actor_id(),
                        "correlation_id": str(payload.get("correlation_id") or tool_call_id),
                        "task_id": state.task_id,
                        "run_id": state.run_id,
                    },
                )
            except TypeScriptE02PortError as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "error": error.code,
                        "message": str(error),
                        "detail": error.detail,
                        "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                        "python_execution_fallback": False,
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "ok": True,
                    "task_id": state.task_id,
                    "run_id": state.run_id,
                    "tool_call_id": tool_call_id,
                    "execution": execution,
                    "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                    "python_execution_fallback": False,
                },
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "X-Zyra-Permission-State-Owner": "PermissionCoordinator",
                    "X-Zyra-Capability-State-Owner": "E02CapabilityCoordinator",
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
            restored_skill_memory = state.metadata.get(
                "skill_memory_browser_context_projection"
            )
            if (
                "skill_memory_restore" not in constraints
                and isinstance(restored_skill_memory, dict)
                and restored_skill_memory
            ):
                constraints["skill_memory_restore"] = dict(restored_skill_memory)
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
                        "skill_memory_context": prior.get("skill_memory_context", {}),
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
            skill_memory_checkpoint = state.metadata.get(
                "browser_skill_memory_context_runtime_state"
            )
            skill_memory_checkpoint = (
                skill_memory_checkpoint
                if isinstance(skill_memory_checkpoint, dict)
                else None
            )
            try:
                _runtime, browser_worker = get_browser_runtime_services(
                    task_id=state.task_id,
                    session_id=session_id,
                    worker_id="BrowserWorker",
                )
                run_result = browser_worker.run(
                    request,
                    browser_context_checkpoint=checkpoint,
                    skill_memory_context_checkpoint=skill_memory_checkpoint,
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
            if run_result.skill_memory_context_checkpoint and not browser_action_pending:
                state.metadata["browser_skill_memory_context_runtime_state"] = dict(
                    run_result.skill_memory_context_checkpoint
                )
            state.metadata["browser_skill_memory_context"] = dict(
                run_result.skill_memory_context_projection
            )
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
                    "skill_memory_context": run_result.skill_memory_context_projection,
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
                    "skill_memory_context": run_result.skill_memory_context_projection,
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
        """Invoke SkillTool through the persistent TypeScript E02 owner."""

        with _task_lock(task_id):
            state = store.load_task(task_id)
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            skill_name = str(payload.get("skill_name") or payload.get("skill") or "").strip()
            if not skill_name:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "skill_name_required"})
                return
            arguments = payload.get("arguments")
            resources = payload.get("resources")
            tool_call_id = str(
                payload.get("tool_call_id")
                or payload.get("tool_use_id")
                or payload.get("invocation_id")
                or new_id("skillcall")
            )
            try:
                execution = get_mcp_runtime().execute(
                    "skill",
                    {
                        "skill": skill_name,
                        "arguments": dict(arguments) if isinstance(arguments, dict) else {},
                        "resources": list(resources)
                        if isinstance(resources, list) and all(isinstance(item, str) for item in resources)
                        else [],
                    },
                    identity={
                        "tool_call_id": tool_call_id,
                        "namespace": "skill",
                        "operation": "invoke",
                        "actor_id": self._permission_actor_id(),
                        "correlation_id": str(payload.get("correlation_id") or tool_call_id),
                        "task_id": state.task_id,
                        "run_id": state.run_id,
                    },
                )
            except TypeScriptE02PortError as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "error": error.code,
                        "message": str(error),
                        "detail": error.detail,
                        "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                        "python_skill_fallback": False,
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
            state.metadata["skill_invocation_projection"] = {
                "canonical_owner": "typescript.SkillCoordinator",
                "tool_call_id": tool_call_id,
                "skill": skill_name,
                "snapshot_hash": execution.get("snapshot_hash"),
            }
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "ok": True,
                    "task": to_jsonable(state),
                    "execution": execution,
                    "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                    "python_skill_fallback": False,
                },
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "X-Zyra-Skill-State-Owner": "SkillCoordinator",
                    "X-Zyra-Permission-State-Owner": "PermissionCoordinator",
                },
            )

    def _execute_skill_update_post(
        self,
        store: SQLiteStore,
        task_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Rescan skill roots through the TypeScript atomic reload path."""

        state = store.load_task(task_id)
        if state is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
            return
        tool_call_id = str(payload.get("tool_call_id") or new_id("skillreload"))
        try:
            execution = get_mcp_runtime().execute(
                "reload_skills",
                {},
                identity={
                    "tool_call_id": tool_call_id,
                    "namespace": "skill",
                    "operation": "reload",
                    "actor_id": self._permission_actor_id(),
                    "correlation_id": str(payload.get("correlation_id") or tool_call_id),
                },
            )
        except TypeScriptE02PortError as error:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "ok": False,
                    "error": error.code,
                    "message": str(error),
                    "detail": error.detail,
                    "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                    "python_skill_fallback": False,
                },
                headers={"Cache-Control": "no-store, max-age=0"},
            )
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "task_id": state.task_id,
                "execution": execution,
                "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                "python_skill_fallback": False,
            },
            headers={"Cache-Control": "no-store, max-age=0"},
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
        """Reject the removed Python invocation journal instead of shadowing TS."""

        del payload
        if store.load_task(task_id) is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
            return
        self._send_json(
            HTTPStatus.CONFLICT,
            {
                "ok": False,
                "error": "typescript_skill_lifecycle_owned",
                "message": "Skill completion/cancellation is committed by SkillCoordinator during execution.",
                "task_id": task_id,
                "invocation_id": invocation_id,
                "action": action,
                "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                "python_skill_fallback": False,
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
            worker_request_id = new_id("workerreq")
            worker_messages, _skill_disclosure_batch = _task_skill_worker_messages(
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
                metadata={
                    "skill_context_owner": "typescript.SkillCoordinator",
                    "canonical_permission_owner": "typescript",
                    "python_decision_fallback": False,
                },
            )
            try:
                retrieval_context = _worker_retrieval_context(
                    store,
                    task_id=state.task_id,
                    workspace_manager=workspace_manager,
                    workspace_access=workspace_access,
                )
                run_result = CodeWorkerRuntime(
                    project_root=PROJECT_ROOT,
                    workspace_root=worker_workspace_root,
                    artifact_root=artifact_root_path(),
                    permission_store=get_permission_store(),
                    permission_state_path=permission_state_path(),
                    tool_registry=default_tool_registry(),
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
                    retrieval_context_runtime=retrieval_context,
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
                "skill_task_ingest": {
                    "canonical_owner": "typescript.SkillCoordinator",
                    "python_skill_dispatch": False,
                },
                "browser_context_delivery": browser_context_batch.to_dict(),
                "browser_context_provider_selection": browser_context_selection.to_dict(),
                "browser_context": _BROWSER_CONTEXT_TASK_INTEGRATION.public_projection(
                    browser_context_checkpoint
                ),
            }
            contract_runtime = CodeWorkerTaskApiContractRuntime()
            contract_events = _code_worker_route_contract_events(
                store,
                task_id=state.task_id,
                session_id=codeworker_api_projection.session.session_id,
                worker_request_id=codeworker_api_projection.session.worker_request_id,
                current_events=response_payload["events"],
                terminal_result_recovered=(
                    str(
                        run_result.worker_result.metadata.get(
                            "terminal_result_recovered",
                            "false",
                        )
                    ).lower()
                    == "true"
                ),
            )
            route_contract = contract_runtime.build_report(
                route_kind=TaskApiRouteKind.POST_CODE_WORKER,
                task_id=state.task_id,
                payload=response_payload,
                events=contract_events,
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


def _merge_permission_request_projections(
    *sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge E02 state with CodeWorker's policy-free approval transport view."""

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for source in sources:
        for item in source:
            request_id = str(item.get("request_id") or "")
            if not request_id or request_id in merged:
                continue
            order.append(request_id)
            merged[request_id] = dict(item)
    return [merged[request_id] for request_id in order]


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
            "permission_authority": "retained non-E02 task-control allowlist",
            "e02_command_dispatch": "typescript-only",
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
        # E02 command families are never evaluated or dispatched here: their
        # API route enters the TypeScript CommandCoordinator above.  Alternate
        # legacy transports therefore fail closed instead of becoming a shadow
        # parser/permission path.
        if descriptor.handler_id in {
            "mcp.control",
            "mcp.prompt",
            "permission.control",
            "permission.plan",
            "skill.command",
            "skill_plugin.command",
            "skill_plugin.hooks",
        }:
            return False
        get_permission_control_plane().state_store.snapshot()
        raw = str(request.arguments.get("raw") or "").strip().lower()
        if descriptor.handler_id == "provider.model" and raw in {"", "status", "list", "show"}:
            return True
        return descriptor.permission_action in {
            "task.goal",
            "context.compact",
            "session.clear",
            "artifact.write",
            "task.change",
            "task.inject",
            "watchdog.control",
            "artifact.export",
            "task.evaluate",
        }

    handlers: dict[str, Any] = {}

    def read_projection(request: ControlCommandRequest, descriptor: Any, _context: Any) -> ControlResult:
        command = ControlCommand(
            run_id=state.run_id,
            task_id=state.task_id,
            name=request.canonical_name,
            arguments=dict(request.arguments),
            command_id=request.command_id,
            metadata={
                "category": str(getattr(descriptor, "category", "projection")),
                "handler_id": str(getattr(descriptor, "handler_id", "")),
                "runtime_status": "projection",
                "canonical_command_registry_owner": "typescript",
            },
        )
        event = control_event_from_command(command, node_id=state.root_node_id)
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
            metadata={"python_parser_enabled": False, "event_only_stateful_fallback": False},
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
        snapshot = get_typescript_agent_port().snapshot()
        tasks = [
            item
            for item in snapshot["tasks"]
            if item.get("parent_task_id") == state.task_id
        ]
        return ControlResult(
            display_text=f"{len(tasks)} logical subagent tasks.",
            data={**snapshot, "tasks": tasks},
            metadata={"physical_worker_state_owned": False},
        )

    handlers["subagent.inspect"] = subagent_inspect

    def mcp_owner(*, action: str, arguments: Any, request: Any) -> dict[str, Any]:
        if action not in {"status", "health", "servers", "server", "catalog", "tools", "resources", "prompts", "tasks", "elicitations"}:
            raise RuntimeError("mutating MCP control requires E02CapabilityCoordinator.execute")
        target = str(arguments.get("target") or "").strip()
        route = {
            "status": "health",
            "health": "health",
            "servers": "servers",
            "server": "servers",
            "catalog": "catalog",
            "tools": "tools",
            "resources": "resources",
            "prompts": "prompts",
            "tasks": "health",
            "elicitations": "elicitations",
        }[action]
        parts = ["mcp", route, *([target] if action == "server" and target else [])]
        payload = get_mcp_runtime().mcp_get(parts).get("body", {})
        return {
            "ok": True,
            "summary": "TypeScript MCP capability projection.",
            **(dict(payload) if isinstance(payload, dict) else {}),
            "runtime_status": "live",
            "canonical_owner": "typescript.McpRuntimeCoordinator",
            "canonical_entrypoint": "E02CapabilityCoordinator.execute",
            "python_dispatch": False,
        }

    def permission_owner(*, action: str, arguments: Any, request: Any) -> dict[str, Any]:
        if action not in {"inspect", "status", "list", "rules", "requests", "decisions", "mode"}:
            raise RuntimeError("mutating permission control requires E02CapabilityCoordinator.execute")
        projection = get_mcp_runtime().snapshot(("permission",))
        return {
            "ok": True,
            "summary": "TypeScript permission runtime projection.",
            "action": action,
            "permission": projection.get("permission", {}),
            "canonical_owner": "typescript.PermissionCoordinator",
            "python_decision_fallback": False,
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
            raise RuntimeError("plugin mutation requires E02CapabilityCoordinator.execute")
        snapshot = get_mcp_runtime().plugins()
        return {
            "ok": True,
            "summary": "TypeScript plugin capability state.",
            **snapshot,
            "canonical_owner": "typescript.PluginCoordinator",
            "python_dispatch": False,
        }

    def subagent_owner(*, action: str, arguments: Any, request: Any) -> dict[str, Any]:
        task_id = str(arguments.get("task_id") or "")
        if action not in {"status", "inspect"}:
            raise RuntimeError("mutating subagent control requires an exact SubagentTaskStore authorization")
        record = get_typescript_agent_port().get_task(task_id)
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

    def legacy_real_mutation(request: ControlCommandRequest, descriptor: Any, _context: Any) -> ControlResult:
        command = ControlCommand(
            run_id=state.run_id,
            task_id=state.task_id,
            name=request.canonical_name,
            arguments=dict(request.arguments),
            command_id=request.command_id,
            metadata={
                "category": str(getattr(descriptor, "category", "task-control")),
                "handler_id": str(getattr(descriptor, "handler_id", "")),
                "runtime_status": "stateful",
                "canonical_command_registry_owner": "typescript",
                "event_hint": str(getattr(descriptor, "event_hint", "control_command")),
            },
        )
        event = control_event_from_command(command, node_id=state.root_node_id)
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

    def fault_inject(request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        fault_api = get_fault_runtime_api(store)
        command = WatchdogControlCommandRuntime(
            fault_api,
            state,
            actor_id=str(request.metadata.get("actor_id") or "control-user"),
        )
        command_receipt = command.dispatch(
            request.canonical_name,
            request.arguments,
            idempotency_key=request.idempotency_key or request.request_id,
        )
        receipt = dict(command_receipt.data)
        state.metadata.setdefault("control_mutations", []).append({
            "request_id": request.request_id,
            "command": request.canonical_name,
            "injection_id": receipt.get("request", {}).get("injection_id", ""),
            "signal_id": (receipt.get("signal") or {}).get("signal_id", ""),
            "same_run": True,
        })
        store.save_checkpoint(state)
        return ControlResult(
            display_text="Same-run fault boundary injected and projected.",
            data=dict(receipt),
            metadata={
                "runtime_status": "stateful",
                "canonical_fault_owner": "python.FaultStateStore",
                "legacy_symbolic_inject": False,
                "mutation_receipt": command_receipt.to_dict(),
            },
        )

    def watchdog_control(request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        fault_api = get_fault_runtime_api(store)
        command = WatchdogControlCommandRuntime(
            fault_api,
            state,
            actor_id=str(request.metadata.get("actor_id") or "control-user"),
        )
        receipt = command.dispatch(
            request.canonical_name,
            request.arguments,
            idempotency_key=request.idempotency_key or request.request_id,
        )
        state.metadata.setdefault("control_mutations", []).append({
            "request_id": request.request_id,
            "command": request.canonical_name,
            "action": receipt.action,
            "mutations": list(receipt.mutations),
        })
        store.save_checkpoint(state)
        return ControlResult(
            display_text=f"Watchdog action {receipt.action} completed.",
            data=receipt.to_dict(),
            metadata={
                "runtime_status": "stateful",
                "canonical_fault_owner": "python.FaultStateStore",
                "recovery_plan_owner": "M1-S07C",
            },
        )

    def requirement_change(request: ControlCommandRequest, descriptor: Any, _context: Any) -> ControlResult:
        command = ControlCommand(
            run_id=state.run_id,
            task_id=state.task_id,
            name=request.canonical_name,
            arguments=dict(request.arguments),
            command_id=request.command_id,
            metadata={
                "category": str(getattr(descriptor, "category", "task-control")),
                "handler_id": str(getattr(descriptor, "handler_id", "task.change")),
                "runtime_status": "stateful",
                "canonical_command_registry_owner": "typescript",
                "event_hint": "requirement_change",
            },
        )
        event = control_event_from_command(command, node_id=state.root_node_id)
        persist_events(store, [event])
        fault_api = get_fault_runtime_api(store)
        isolation = fault_api.runtime.integration.apply_requirement_change(state, event)
        state.metadata.setdefault("control_mutations", []).append({
            "request_id": request.request_id,
            "command": request.canonical_name,
            "event_id": event.event_id,
            "replan_node_id": isolation.get("replan_node_id", ""),
            "requirement_changed_is_fault": False,
        })
        store.save_checkpoint(state)
        legacy = _command_result_for_event(state, event, store)
        legacy_data = dict(legacy.get("data") or {})
        legacy_data["fault_isolation"] = dict(isolation)
        fault_api.release_idle_connection()
        return ControlResult(
            display_text="Requirement change routed through ConstraintKeeper and TopologyRouter.",
            data={**legacy_data, "control_event": to_jsonable(event)},
            metadata={
                "runtime_status": "stateful",
                "route_owner": "ConstraintKeeper/TopologyRouter",
                "requirement_changed_is_fault": False,
            },
        )

    handlers["task.change"] = requirement_change
    handlers["task.inject"] = fault_inject
    handlers["watchdog.control"] = watchdog_control
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
            f"- logical subagents: `{len(get_typescript_agent_port().records(parent_task_id=state.task_id))}`",
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
    fault_api = get_fault_runtime_api(store)
    try:
        snapshot = dict(fault_api.runtime.snapshot(task_id=state.task_id))
    finally:
        fault_api.release_idle_connection()
    fault_state = dict(snapshot.get("fault_state") or {})
    return {
        "task_id": state.task_id,
        "run_id": state.run_id,
        "recovery_plans": list(state.metadata.get("recovery_plans", [])),
        "last_recovery_plan": state.metadata.get("last_recovery_plan"),
        "failure_injections": list(state.metadata.get("failure_injections", [])),
        "signals": list(fault_state.get("signals") or []),
        "recovery_handoffs": list(fault_state.get("recovery_handoffs") or []),
        "recovery_handoff_deliveries": list(fault_state.get("recovery_handoff_deliveries") or []),
        "integration": dict(snapshot.get("integration") or {}),
        "legacy_free_text_scan": False,
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
        projection = get_mcp_runtime().tools()
        result["summary"] = "TypeScript-owned capability tools."
        result["data"] = {
            **projection,
            "canonical_entrypoint": "E02CapabilityCoordinator.execute",
            "python_registry_enabled": False,
        }
    elif name == "/permissions":
        projection = get_mcp_runtime().snapshot(("permission",))
        result["summary"] = "TypeScript-owned permission runtime summary."
        result["data"] = {
            **projection,
            "state_owner": "typescript.PermissionCoordinator",
            "python_decision_fallback": False,
        }
    elif name == "/help":
        projection = get_mcp_runtime().commands()
        result["summary"] = "TypeScript-owned command registry."
        result["data"] = {
            **projection,
            "canonical_entrypoint": "E02CapabilityCoordinator.execute",
            "python_parser_enabled": False,
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
        tokens = [token for token in raw.strip().split() if token]
        route = tokens[0].lower() if tokens else "health"
        route = {
            "status": "health",
            "server": "servers",
        }.get(route, route)
        if route not in {"health", "config", "servers", "catalog", "tools", "resources", "prompts", "elicitations"}:
            route = "health"
        parts = ["mcp", route]
        if route == "servers" and len(tokens) > 1:
            parts.append(tokens[1])
        projection = get_mcp_runtime().mcp_get(parts)
        status = int(projection.get("status") or HTTPStatus.OK)
        body = dict(projection.get("body") or {})
        result["ok"] = status < HTTPStatus.BAD_REQUEST
        result["summary"] = "TypeScript-owned MCP capability projection."
        result["runtime_status"] = "live" if result["ok"] else "blocked"
        result["data"] = {
            **body,
            "runtime_status": result["runtime_status"],
            "state_owner": "typescript.McpRuntimeCoordinator",
            "permission_owner": "typescript.PermissionCoordinator",
            "canonical_entrypoint": "E02CapabilityCoordinator.execute",
            "python_dispatch": False,
        }
    elif name == "/skills":
        projection = get_mcp_runtime().skills()
        result["summary"] = "TypeScript-owned skills and invocation state."
        result["data"] = {
            **projection,
            "skill_invocation": dict(state.metadata.get("skill_invocation_projection") or {}),
            "body_in_projection": False,
            "state_owner": "typescript.SkillCoordinator",
            "permission_owner": "typescript.PermissionCoordinator",
            "canonical_entrypoint": "E02CapabilityCoordinator.execute",
            "python_dispatch": False,
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


def _code_worker_route_contract_events(
    store: SQLiteStore,
    *,
    task_id: str,
    session_id: str,
    worker_request_id: str,
    current_events: list[dict[str, Any]],
    terminal_result_recovered: bool,
) -> list[dict[str, Any]]:
    """Recover prior route phases only for the exact durable terminal binding."""

    current = [dict(event) for event in current_events if isinstance(event, dict)]
    if not terminal_result_recovered or not session_id or not worker_request_id:
        return current

    recovered: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    for event in store.task_events(task_id):
        payload = event.get("payload")
        query_session = payload.get("query_session") if isinstance(payload, dict) else None
        if not isinstance(query_session, dict):
            continue
        if (
            str(query_session.get("session_id") or "") != session_id
            or str(query_session.get("worker_request_id") or "") != worker_request_id
        ):
            continue
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen_event_ids:
            continue
        if event_id:
            seen_event_ids.add(event_id)
        recovered.append(dict(event))

    for event in current:
        event_id = str(event.get("event_id") or "")
        if event_id and event_id in seen_event_ids:
            continue
        if event_id:
            seen_event_ids.add(event_id)
        recovered.append(event)
    return recovered


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
    skill_memory_browser_exports = [
        event.payload["query_session"].get("browser_projection", {})
        for event in session_events
        if event.payload.get("query_session", {}).get("phase")
        == "skill_memory_browser_context_exported"
        and isinstance(
            event.payload.get("query_session", {}).get("browser_projection"),
            dict,
        )
    ]
    latest_skill_memory_browser_context = (
        dict(skill_memory_browser_exports[-1])
        if skill_memory_browser_exports
        else {}
    )
    if latest_skill_memory_browser_context:
        state.metadata["skill_memory_browser_context_projection"] = dict(
            latest_skill_memory_browser_context
        )
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
        "worker_request_id": str(
            metadata.get("logical_worker_request_id")
            or run_result.worker_result.request_id
        ),
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
        "skill_memory_browser_context": latest_skill_memory_browser_context,
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
