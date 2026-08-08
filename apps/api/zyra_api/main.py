from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import threading
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

from .typed_transport import (
    API_VERSION,
    API_VERSION_HEADER,
    REQUEST_ID_HEADER,
    ReceiptReplay,
    ReceiptReservation,
    TypedReceiptStore,
    TypedRequestContext,
    TypedTransportError,
    error_context as typed_error_context,
    paginate_tasks,
    runtime_readiness_payload,
    typed_request_context,
)
from .event_stream_ingress import (
    EventIngressApiFacade,
    EventIngressError,
    sse_headers,
)
from .session_api import paginate_sessions, session_detail

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
    ("GET", "/sessions"),
    ("GET", "/sessions/{session_id}"),
    ("GET", "/experiments/registry"),
    ("GET", "/experiments/runs"),
    ("GET", "/experiments/runs/{experiment_id}"),
    ("GET", "/experiments/runs/{experiment_id}/report"),
    ("GET", "/experiments/runs/{experiment_id}/samples"),
    ("GET", "/experiments/runs/{experiment_id}/bundle"),
    ("GET", "/experiments/runs/{experiment_id}/source"),
    ("GET", "/experiments/runs/{experiment_id}/requirements"),
    ("POST", "/experiments/runs"),
    ("POST", "/experiments/runs/{experiment_id}/start"),
    ("POST", "/experiments/runs/{experiment_id}/cancel"),
    ("POST", "/experiments/runs/{experiment_id}/archive"),
    ("POST", "/experiments/runs/{experiment_id}/verify"),
    ("GET", "/scenarios/registry"),
    ("GET", "/scenarios/runs"),
    ("GET", "/scenarios/runs/{scenario_run_id}"),
    ("GET", "/scenarios/runs/{scenario_run_id}/evidence"),
    ("POST", "/scenarios/runs"),
    ("POST", "/scenarios/runs/{scenario_run_id}/start"),
    ("POST", "/scenarios/runs/{scenario_run_id}/cancel"),
    ("POST", "/scenarios/runs/{scenario_run_id}/archive"),
    ("POST", "/scenarios/runs/{scenario_run_id}/verify"),
    ("POST", "/tasks/{task_id}/workers/browser"),
    ("GET", "/tasks/{task_id}/loopx"),
    ("POST", "/tasks/{task_id}/loopx/commands"),
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
    ("GET", "/tasks/{task_id}/event-ingress/capabilities"),
    ("GET", "/tasks/{task_id}/event-ingress/snapshot"),
    ("GET", "/tasks/{task_id}/event-ingress/delta"),
    ("GET", "/tasks/{task_id}/event-ingress/sse"),
    ("GET", "/tasks/{task_id}/artifacts"),
    ("GET", "/tasks/{task_id}/artifacts/{artifact_id}"),
    ("GET", "/tasks/{task_id}/artifacts/{artifact_id}/content"),
    ("GET", "/tasks/{task_id}/artifacts/{artifact_id}/download"),
    ("GET", "/tasks/{task_id}/artifacts/{artifact_id}/receipts"),
    ("GET", "/tasks/{task_id}/diff-reviews/{artifact_id}"),
    ("GET", "/tasks/{task_id}/diff-reviews/{artifact_id}/files/{file_id}/hunks"),
    ("GET", "/tasks/{task_id}/diff-reviews/{artifact_id}/files/{file_id}/content"),
    ("POST", "/tasks/{task_id}/diff-reviews/{artifact_id}/comments"),
    ("POST", "/tasks/{task_id}/diff-reviews/{artifact_id}/apply"),
    ("POST", "/tasks/{task_id}/diff-reviews/transactions/{transaction_id}/rollback"),
    ("GET", "/tasks/{task_id}/terminals"),
    ("GET", "/tasks/{task_id}/terminals/{terminal_id}"),
    ("GET", "/tasks/{task_id}/terminals/{terminal_id}/connect"),
    ("POST", "/tasks/{task_id}/terminals"),
    ("POST", "/tasks/{task_id}/terminals/{terminal_id}/ticket"),
    ("POST", "/tasks/{task_id}/terminals/{terminal_id}/kill"),
    ("POST", "/tasks/{task_id}/faults/observations"),
    ("POST", "/tasks/{task_id}/faults/handoffs/dispatch"),
    ("GET", "/tasks/{task_id}/recovery"),
    ("POST", "/tasks/{task_id}/recovery/signals"),
    ("POST", "/tasks/{task_id}/recovery/fault-handoff"),
    ("POST", "/tasks/{task_id}/recovery/worker-handoff"),
    ("POST", "/tasks/{task_id}/recovery/checkpoints"),
    ("POST", "/tasks/{task_id}/recovery/checkpoints/{checkpoint_id}/resume"),
    ("POST", "/tasks/{task_id}/recovery/deltas"),
    ("GET", "/recovery/plans/{plan_id}"),
    ("POST", "/recovery/plans/{plan_id}/resume-waiting"),
    ("GET", "/hardening/m1/status"),
    ("GET", "/hardening/m1/reports"),
    ("GET", "/hardening/m1/reports/{report_id}"),
    ("GET", "/hardening/m1/chain"),
    ("POST", "/hardening/m1/audit"),
    ("POST", "/hardening/m1/reports/{report_id}/verify"),
    ("POST", "/tasks/{task_id}/hardening/m1/foundation"),
    ("GET", "/hardening/m1/integration/status"),
    ("GET", "/hardening/m1/integration/runs"),
    ("GET", "/hardening/m1/integration/runs/{run_id}"),
    ("POST", "/hardening/m1/integration"),
    ("GET", "/deployment/status"),
    ("GET", "/deployment/profiles"),
    ("GET", "/deployment/events"),
    ("GET", "/deployment/semantic-health/latest"),
    ("POST", "/deployment/doctor"),
    ("POST", "/deployment/semantic-health"),
    ("POST", "/deployment/dispatch"),
    ("POST", "/deployment/faults/{profile}"),
    ("POST", "/deployment/exercise"),
    ("POST", "/deployment/shutdown"),
    ("GET", "/policy/evidence"),
    ("GET", "/policy/metrics/specs"),
    ("GET", "/policy/metrics/reports/{report_id}"),
)

for package_path in PACKAGE_PATHS:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from .artifact_api import (
    ArtifactApiError,
    ArtifactCatalogQuery,
    ArtifactCatalogService,
    ArtifactReadAudit,
    ArtifactReadQuery,
    find_task_artifact,
)
from .diff_review_api import (
    DiffReviewApiError,
    DiffReviewApiService,
    DiffReviewRegistry,
)
from .terminal_api import TerminalApiService
from .scenario_api import get_scenario_runner_api, reset_scenario_runner_api
from .experiment_api import get_experiment_api, reset_experiment_api
from .deployment_api import get_deployment_api, reset_deployment_api
from .policy_api import get_policy_metric_api, reset_policy_metric_api

from zyra_core import (
    AgentMessage,
    AgentRole,
    ArtifactKind,
    ArtifactRef,
    ControlCommand,
    EventRecord,
    EventType,
    MessageIntent,
    PlanNodeStatus,
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
from zyra_orchestration.goal_contracts import (
    direct_response_contract,
    goal_delivery_contract,
    goal_contract_matches_projection,
    validate_direct_response,
    validate_goal_delivery,
)
from zyra_orchestration.deployment import DeploymentProfile
from zyra_orchestration.topology_policy.production import (
    Phase2StrongestProductionBridge,
)
from zyra_orchestration.topology_policy import (
    PhysicalDispatchReceipt,
    StableArtifactRef,
    canonical_digest,
)
from zyra_symbolic import ConstraintKeeper, apply_failure_injection, apply_requirement_change
from zyra_scheduler import (
    PhysicalDispatchCallPort,
    PhysicalDispatchEvidenceStore,
    PhysicalDispatchTask,
    ResourceLocation,
    ResourceScheduler,
    WorkerBackendKind,
    WorkerManifest,
    WorkerPool,
    backend_registry_path,
    build_final_verifier_decision,
    cancel_pending_dispatches,
    source_to_target_ledger,
)
from zyra_scheduler.fault_runtime import (
    FaultApiError,
    FaultRuntimeApiService,
    FaultRuntimeApplication,
)
from zyra_scheduler.recovery_runtime import (
    AppliedOutcomeVerifier,
    CallbackContinuationOwner,
    CallbackProjectionPort,
    CallbackRecoveryEventSink,
    CallbackRecoveryIntegrationEventSink,
    CanonicalOwnerCallbacks,
    CanonicalTaskStateRuntime,
    CheckpointCommitRuntime,
    CheckpointResumeBridge,
    DeterministicCommitRuntime,
    ExactRecoveryRuntime,
    LayeredRouteRuntime,
    MemoryAwareRouteRuntime,
    RecoveryCausalTraceRuntime,
    RecoveryComponentControl,
    RecoveryContinuationRuntime,
    RecoveryFeedbackIntegrationRuntime,
    RecoveryIngressRuntime,
    RecoveryIntegrationRuntime,
    RecoveryActionRuntime,
    RecoveryApplication,
    RecoveryContextRuntime,
    RecoveryDecisionRuntime,
    RecoveryOwnerRuntime,
    RecoveryPlanStore,
    RecoveryRestartRuntime,
    RecoverySemanticRuntime,
    RecoverySignalClassifier,
    RecoveryStateFusionRuntime,
    RoutingMemoryFeedback,
    CallbackRoutingMemorySink,
)
from zyra_commands import (
    CommandOrigin,
    ControlCommandRequest,
    ControlRequestStore,
    ControlResult,
    PromptQueueRuntime,
    QueuePriority,
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
    RuntimeEventContractError,
    RuntimeEventProcessError,
    TypeScriptRuntimeEventPort,
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
                workspace_root=PROJECT_ROOT,
            )
            _RUNTIME_EVENT_SPINE_KEY = key
        return _RUNTIME_EVENT_SPINE


def get_runtime_event_api() -> RuntimeEventApiFacade:
    return RuntimeEventApiFacade(get_runtime_event_spine_bridge())


def get_event_ingress_api() -> EventIngressApiFacade:
    """Return a read-only browser ingress facade over the canonical spine."""

    return EventIngressApiFacade(get_runtime_event_spine_bridge())


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
from zyra_runtime.provider_control_plane import (
    ProviderRouteBindingRuntime,
    provider_database_path,
)
from zyra_workers.terminal import (
    TerminalBinding,
    TerminalCreateRequest,
    TerminalError,
    TerminalPermission,
    TerminalSessionRegistry,
    TerminalSpill,
    TerminalStateStore,
    TerminalTicketAuthority,
    TerminalWebSocketHandler,
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
    PermissionMode,
    ToolIdentity as RuntimePermissionToolIdentity,
)
from zyra_runtime.productization.composition import (
    RuntimeActivationSet,
    RuntimeOwnerComposition,
    activation_result,
)
from zyra_runtime.productization.contracts import RuntimeDomain
from zyra_runtime.productization.bootstrap import (
    BootstrapError,
    ProductBootstrapRuntime,
    get_product_bootstrap,
    reset_product_bootstrap,
)
from zyra_runtime.productization.configuration import (
    ResolvedConfiguration,
    load_runtime_configuration,
)
from zyra_runtime.sandbox_gateway.state_store import GatewayStateStore
from zyra_workers import (
    BrowserRuntimeConfig,
    BrowserRuntimeRegistry,
    BrowserContextScope,
    BrowserContextTaskIntegrationRuntime,
    BrowserContextApiProjectionRuntime,
    BrowserWorkerRuntime,
    CodeWorkerSidecarClient,
    CodeWorkerRuntime,
    WorkerRetrievalContextRuntime,
    MemoryCuratorOperation,
    MemoryCuratorWorkerRequest,
    MemoryCuratorWorkerRuntime,
    EdgeWorkerGatewayRuntime,
    EdgeWorkerProcessConnector,
    EdgeWorkerRegistrationRuntime,
    build_memory_curator_runtime,
    browser_use_health_summary,
    default_browser_action_registry,
    inspect_browser_use_runtime,
)
from zyra_workers.edge_pool import IntegratedEdgeExecutionAdapter
from zyra_workers.subagents.typescript_port import TypeScriptAgentDurablePort
from zyra_evaluation import evaluate_task_trace
from zyra_evaluation.m1_hardening.api import M1HardeningApi
from zyra_evaluation.m1_hardening.integration_service import M1IntegrationService
from zyra_evaluation.m1_hardening.owner_probes import RuntimeResetRegistry
from zyra_evaluation.m1_hardening.service import M1HardeningService
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
    browser_use_source_identity,
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
    claude_code_source_identity,
)
from zyra_integrations.e02_ports import (
    TypeScriptE02ApiPort,
    TypeScriptE02PortError,
    materialize_bundled_skills,
)
from zyra_integrations.loopx import (
    LoopXControlRuntime,
    task_goal_id,
)
from zyra_integrations.loopx.bridge import (
    LoopXBridgeObservability,
    LoopXDispatcher,
    LoopXOutbox,
    LoopXRuntimeStateAdapter,
    LoopXSingleWriter,
)
from zyra_integrations.loopx.runtime import (
    LOOPX_SOURCE_COMMIT,
    LOOPX_SOURCE_TREE_COMMIT,
    LOOPX_VERSION,
    LoopXRuntimeResolver,
)

from .mcp_api import McpApiFacade
from .provider_backend_api import (
    ProviderBackendApi,
    get_provider_control_client,
    reset_provider_control_client,
)
from .worker_pool_api import WorkerPoolApiService
from .recovery_api import RecoveryRuntimeApiService

from zyra_orchestration.graph_custody import GraphStateCustody, GraphStateStore
from zyra_orchestration.topology_policy import policy_contract_schema_catalog
from zyra_scheduler.worker_pool import (
    BackendCapability as PhysicalBackendCapability,
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
from zyra_scheduler.backend_registry import (
    BackendRegistryActionDispatchPort,
    BackendDefinition,
    BackendKind,
    BackendLocation,
    BackendRegistry,
    BackendResourceLimits,
    BackendRegistryStore,
    BackendSelectionRequest,
    WorkspacePolicy,
    ensure_default_backends,
)


def runtime_configuration() -> ResolvedConfiguration:
    return load_runtime_configuration(PROJECT_ROOT)


def event_log_path() -> Path:
    return runtime_configuration().path("state.event_log")


def sqlite_path() -> Path:
    return runtime_configuration().path("state.database")


def worker_pool_path() -> Path:
    return runtime_configuration().path("state.worker_pool")


def graph_state_path() -> Path:
    return runtime_configuration().path("state.graph")


_WORKER_POOL_LOCK = threading.RLock()
_WORKER_POOL_RUNTIME: WorkerPoolFoundationRuntime | None = None
_WORKER_POOL_API: WorkerPoolApiService | None = None
_WORKER_POOL_KEY: tuple[str, str, str] | None = None
_API_EDGE_CONNECTOR: EdgeWorkerProcessConnector | None = None
_API_EDGE_REGISTRATION: EdgeWorkerRegistrationRuntime | None = None
_API_EDGE_ADAPTER: IntegratedEdgeExecutionAdapter | None = None
_API_EDGE_KEY: tuple[int, str] | None = None


def _worker_pool_secret(pool_path: Path) -> bytes:
    configured = os.environ.get("ZYRA_WORKER_POOL_SECRET", "").encode("utf-8")
    return (
        hashlib.sha256(configured).digest()
        if configured
        else hashlib.sha256(f"zyra-worker-pool:{pool_path}".encode("utf-8")).digest()
    )


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
            secret = _worker_pool_secret(pool_path)
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
                artifact_store=LocalArtifactStore(artifact_root_path()),
            )
            _WORKER_POOL_KEY = key
        return _WORKER_POOL_API


def ensure_api_edge_worker(
    pool_api: WorkerPoolApiService | None = None,
) -> tuple[str, IntegratedEdgeExecutionAdapter]:
    """Attach one authenticated child-process edge worker to the API pool.

    Edge execution is opt-in per fanout child.  The runtime is still part of
    the production composition root: registration, heartbeat, lease,
    gateway action, artifact and settlement all use the canonical 07A/05B
    owners.  Disabling the connector never falls back to the local worker.
    """

    global _API_EDGE_CONNECTOR, _API_EDGE_REGISTRATION, _API_EDGE_ADAPTER, _API_EDGE_KEY
    if os.environ.get("ZYRA_EDGE_POOL_DISABLED") == "1":
        raise WorkerPoolError(
            WorkerPoolErrorCode.EDGE_CONNECTOR_DISABLED,
            "API edge worker is disabled and local fallback is forbidden",
            operation="ensure_api_edge_worker",
        )
    selected = pool_api or get_worker_pool_api()
    host = os.environ.get("ZYRA_EDGE_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    key = (id(selected), host)
    with _WORKER_POOL_LOCK:
        if (
            _API_EDGE_KEY == key
            and _API_EDGE_CONNECTOR is not None
            and _API_EDGE_CONNECTOR.running
            and _API_EDGE_ADAPTER is not None
        ):
            return _API_EDGE_CONNECTOR.worker_id, _API_EDGE_ADAPTER
        if _API_EDGE_REGISTRATION is not None:
            _API_EDGE_REGISTRATION.stop()
        pool_path = worker_pool_path().resolve()
        secret = _worker_pool_secret(pool_path)
        connector = EdgeWorkerProcessConnector(
            worker_id="api-edge-worker",
            secret=secret,
            package_roots=(
                PROJECT_ROOT / "packages" / "core",
                PROJECT_ROOT / "packages" / "scheduler",
                PROJECT_ROOT / "packages" / "workers",
            ),
            host=host,
            startup_timeout_seconds=15,
            request_timeout_seconds=30,
        )
        registration = EdgeWorkerRegistrationRuntime(
            connector,
            selected.pool.lifecycle,
            selected.pool.heartbeats,
            attestor=selected.pool.attestor,
        )
        endpoint = registration.register(
            backend=PhysicalBackendCapability(
                backend_id="api-edge-sandbox-gateway",
                backend_kind="sandbox_gateway",
                enabled=True,
                healthy=True,
                capabilities=(
                    "agent_task",
                    "code_execution",
                    "artifact_return",
                    "edge_execution",
                ),
                tool_ids=("edge.json_transform",),
                constraints={
                    "sealed_capable": True,
                    "gateway_owner": "SandboxGatewayRuntime",
                },
                labels={"dispatch_location": "edge"},
            ),
            resources=PhysicalResourceVector(
                cpu_cores=1,
                memory_mb=256,
                disk_mb=512,
                network_mbps=50,
                process_slots=2,
            ),
            capabilities=(
                "agent_task",
                "code_execution",
                "artifact_return",
                "edge_execution",
            ),
            tool_ids=("edge.json_transform",),
            replace_generation=True,
        )
        registry_store = BackendRegistryStore(
            backend_registry_path(artifact_root_path())
        )
        try:
            registry = BackendRegistry(registry_store)
            ensure_default_backends(registry)
            registry.register(
                BackendDefinition(
                    backend_id="api-edge-sandbox-gateway",
                    display_name="Authenticated API Edge Worker",
                    kind=BackendKind.LOCAL_PROCESS,
                    location=BackendLocation.EDGE,
                    runtime_worker="typescript.E03AgentControlCoordinator",
                    capabilities=(
                        "agent_task",
                        "code_execution",
                        "artifact_return",
                        "edge_execution",
                    ),
                    workspace_policy=WorkspacePolicy(
                        scope="artifact",
                        artifact_only=True,
                        require_existing=False,
                        require_writable=True,
                        isolation="independent-edge-process",
                    ),
                    limits=BackendResourceLimits(
                        maximum_concurrency=2,
                        turn_timeout_seconds=120,
                        connect_timeout_seconds=15,
                    ),
                    priority=100,
                    metadata={
                        "execution_mode": "authenticated_tcp_child_process",
                        "real_edge_dispatch": True,
                        "authenticated_endpoint": endpoint.endpoint,
                        "worker_id": connector.worker_id,
                    },
                )
            )
        finally:
            registry_store.close()
        gateway = EdgeWorkerGatewayRuntime(
            connector,
            selected.pool.leases,
            artifact_root=artifact_root_path() / "edge-worker",
        )
        adapter = IntegratedEdgeExecutionAdapter(gateway, registration)
        selected.integration.edge_execution = adapter
        selected.integration.control.execution_cancellation = adapter
        _API_EDGE_CONNECTOR = connector
        _API_EDGE_REGISTRATION = registration
        _API_EDGE_ADAPTER = adapter
        _API_EDGE_KEY = key
        return connector.worker_id, adapter


def reset_worker_pool_api() -> None:
    """Forget API-owned worker/graph composition roots between workspace lifecycles."""

    global _WORKER_POOL_RUNTIME, _WORKER_POOL_API, _WORKER_POOL_KEY
    global _API_EDGE_CONNECTOR, _API_EDGE_REGISTRATION, _API_EDGE_ADAPTER, _API_EDGE_KEY
    with _WORKER_POOL_LOCK:
        if _API_EDGE_REGISTRATION is not None:
            _API_EDGE_REGISTRATION.stop()
        _API_EDGE_CONNECTOR = None
        _API_EDGE_REGISTRATION = None
        _API_EDGE_ADAPTER = None
        _API_EDGE_KEY = None
        if _WORKER_POOL_API is not None:
            _WORKER_POOL_API.close()
        _WORKER_POOL_API = None
        _WORKER_POOL_RUNTIME = None
        _WORKER_POOL_KEY = None


def fault_runtime_path() -> Path:
    return runtime_configuration().path("state.fault")


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


def recovery_runtime_path() -> Path:
    return runtime_configuration().path("state.recovery")


_RECOVERY_RUNTIME_LOCK = threading.RLock()
_RECOVERY_RUNTIME_API: RecoveryRuntimeApiService | None = None
_RECOVERY_RUNTIME_KEY: tuple[str, str, str, str] | None = None


def _recovery_owner_callbacks(store: SQLiteStore) -> CanonicalOwnerCallbacks:
    def require_state(request: Mapping[str, Any]):
        task_id = str(request.get("task_id") or "")
        state = store.load_task(task_id)
        if state is None:
            raise ValueError(f"task state not found: {task_id}")
        if str(request.get("run_id") or state.run_id) != state.run_id:
            raise ValueError("recovery owner request run identity mismatch")
        return state

    def worker_successor(request: Mapping[str, Any]) -> Mapping[str, Any]:
        def same_failure_boundary(left: Any, right: Any) -> bool:
            left_identity = str(left.process_identity or "")
            right_identity = str(right.process_identity or "")
            if left_identity and right_identity:
                return left_identity == right_identity
            left_endpoint = str(left.endpoint or "")
            right_endpoint = str(right.endpoint or "")
            return bool(
                left_endpoint
                and right_endpoint
                and left_endpoint == right_endpoint
            )

        state = require_state(request)
        before = dict(state.metadata.get("worker_pool") or {})
        worker_api = get_worker_pool_api()
        idempotency_key = str(request.get("idempotency_key") or "")
        prior_lease_id = str(before.get("lease_id") or "")
        prior = (
            worker_api.pool.store.get_lease(prior_lease_id)
            if prior_lease_id
            else None
        )

        def replay_successor(
            lease: Any,
            route: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            prior_attempt = worker_api.pool.store.require_attempt(
                lease.attempt_id
            )
            replay_before = dict(route.get("before") or {})
            if not replay_before and prior_attempt.parent_attempt_id:
                parent_lease = worker_api.pool.store.lease_for_attempt(
                    prior_attempt.parent_attempt_id
                )
                if parent_lease is not None:
                    replay_before = {
                        "attempt_id": parent_lease.attempt_id,
                        "lease_id": parent_lease.lease_id,
                        "worker_id": parent_lease.worker_id,
                        "backend_id": parent_lease.backend_id,
                    }
            replay_after = dict(route.get("after") or {})
            if not replay_after and before.get("lease_id") == lease.lease_id:
                replay_after = dict(before)
            if lease.terminal and before.get("lease_id") == lease.lease_id:
                worker_api.reconcile_task_graph_binding(
                    state,
                    reason=(
                        "replayed successor lease became terminal after its "
                        "TaskState checkpoint"
                    ),
                    actor_id="recovery-worker-successor",
                    causation_id=(
                        f"recovery-successor-terminal-replay:{lease.lease_id}"
                    ),
                )
                store.save_checkpoint(state)
            return {
                "accepted": not lease.terminal,
                "changed": False,
                "before": replay_before,
                "after": replay_after,
                "canonical_ref": {
                    "attempt_id": lease.attempt_id,
                    "lease_id": lease.lease_id,
                    "fence_epoch": lease.fence_epoch,
                    "worker_id": lease.worker_id,
                },
                "receipt_id": lease.lease_id,
                "message": (
                    "WorkerPoolFoundationRuntime replayed the committed "
                    "successor lease"
                ),
                "metadata": {
                    "replayed": True,
                    "terminal": lease.terminal,
                },
            }

        route = dict(state.metadata.get("recovery_worker_route") or {})
        route_lease = worker_api.pool.store.get_lease(
            str(route.get("lease_id") or "")
        )
        if (
            idempotency_key
            and route.get("idempotency_key") == idempotency_key
            and route_lease is not None
        ):
            return replay_successor(route_lease, route)
        if (
            idempotency_key
            and prior is not None
            and prior.idempotency_key == idempotency_key
        ):
            return replay_successor(prior, route)
        prior_worker = (
            worker_api.pool.store.get_worker(str(before.get("worker_id") or ""))
            if before.get("worker_id")
            else None
        )
        excluded = {
            str(item) for item in request.get("excluded_refs") or () if str(item)
        }
        if before.get("worker_id"):
            excluded.add(str(before["worker_id"]))
        if prior_worker is not None:
            for candidate in worker_api.pool.store.list_workers():
                if same_failure_boundary(prior_worker, candidate):
                    excluded.add(candidate.worker_id)
        route_worker_id = str(route.get("preferred_worker_id") or "")
        route_boundary = dict(route.get("successor_failure_boundary") or {})
        route_process_identity = str(
            route_boundary.get("process_identity") or ""
        )
        route_endpoint = str(route_boundary.get("endpoint") or "")
        route_worker = (
            worker_api.pool.store.get_worker(route_worker_id)
            if route_worker_id
            else None
        )
        if route_worker is not None and (
            (
                route_process_identity
                and route_worker.process_identity == route_process_identity
            )
            or (
                not route_process_identity
                and route_endpoint
                and route_worker.endpoint == route_endpoint
            )
        ):
            excluded.add(route_worker_id)
        if route_process_identity or route_endpoint:
            for candidate in worker_api.pool.store.list_workers():
                if (
                    route_process_identity
                    and candidate.process_identity == route_process_identity
                ) or (
                    not route_process_identity
                    and route_endpoint
                    and candidate.endpoint == route_endpoint
                ):
                    excluded.add(candidate.worker_id)

        def persist_successor(
            *,
            lease: Any,
            attempt: Any,
            worker: Any,
            original_before: Mapping[str, Any],
            after_projection: Mapping[str, Any],
            recovered: bool,
            projection_applied: bool = True,
        ) -> Mapping[str, Any]:
            if (
                worker.worker_id in excluded
                or (
                    prior_worker is not None
                    and same_failure_boundary(prior_worker, worker)
                )
            ):
                if not lease.terminal:
                    worker_api.pool.leases.cancel(
                        lease.lease_id,
                        reason=(
                            "recovery successor reused the prior failure "
                            "boundary"
                        ),
                    )
                worker_api.cancel_task_graph_binding(
                    state,
                    reason=(
                        "recovery successor reused the prior failure boundary"
                    ),
                    actor_id="recovery-worker-successor",
                    causation_id=(
                        f"recovery-successor-rejected:{lease.lease_id}"
                    ),
                )
                raise RuntimeError(
                    "recovery successor must cross a physical failure boundary"
                )
            runtime_hints = dict(state.metadata.get("runtime_hints") or {})
            worker_manifest = worker_api.pool.store.latest_manifest(
                worker.worker_id
            )
            delivery_contract = dict(
                state.metadata.get("delivery_contract") or {}
            )
            provider_capable = bool(
                worker_manifest is not None
                and "provider-reasoning" in worker_manifest.capabilities
            )
            if (
                delivery_contract.get("provider_reasoning_required") is not True
                or provider_capable
            ):
                runtime_hints["preferred_worker"] = worker.worker_id
            else:
                # The successor lease still crosses and fences the failed
                # physical boundary, but it cannot override the provider-first
                # MaAS candidate contract for the continuation.
                runtime_hints.pop("preferred_worker", None)
            runtime_hints["avoid_workers"] = sorted(excluded)
            state.metadata["runtime_hints"] = runtime_hints
            route_value = {
                "owner": "WorkerPoolFoundationRuntime",
                "plan_id": str(request.get("plan_id") or ""),
                "preferred_worker_id": worker.worker_id,
                "avoided_worker_ids": sorted(excluded),
                "lease_id": lease.lease_id,
                "attempt_id": attempt.attempt_id,
                "idempotency_key": idempotency_key,
                "before": dict(original_before),
                "after": dict(after_projection),
                "prior_failure_boundary": {
                    "process_identity": (
                        prior_worker.process_identity if prior_worker else ""
                    ),
                    "endpoint": prior_worker.endpoint if prior_worker else "",
                },
                "successor_failure_boundary": {
                    "process_identity": worker.process_identity,
                    "endpoint": worker.endpoint,
                },
                "recovered_before_task_checkpoint": recovered,
                "terminal": lease.terminal,
                "accepted": not lease.terminal,
                "projection_applied": projection_applied,
            }
            state.metadata["recovery_worker_route"] = route_value
            store.save_checkpoint(state)
            return {
                "accepted": not lease.terminal,
                "changed": (
                    not lease.terminal
                    and original_before.get("lease_id")
                    != after_projection.get("lease_id")
                ),
                "before": dict(original_before),
                "after": dict(after_projection),
                "canonical_ref": {
                    "attempt_id": attempt.attempt_id,
                    "lease_id": lease.lease_id,
                    "fence_epoch": lease.fence_epoch,
                    "worker_id": worker.worker_id,
                },
                "receipt_id": lease.lease_id,
                "message": (
                    "WorkerPoolFoundationRuntime reconciled a terminal "
                    "successor committed before the task checkpoint"
                    if lease.terminal
                    else "WorkerPoolFoundationRuntime recovered the committed "
                    "successor lease"
                    if recovered
                    else "WorkerPoolFoundationRuntime allocated a successor lease"
                ),
                "metadata": {
                    "replayed": recovered,
                    "terminal": lease.terminal,
                },
            }

        recovered = worker_api.recover_task_acquisition(
            state,
            idempotency_key=idempotency_key,
        )
        if recovered is not None:
            return persist_successor(
                lease=recovered["lease"],
                attempt=recovered["attempt"],
                worker=recovered["worker"],
                original_before=recovered["before"],
                after_projection=recovered["after"],
                recovered=True,
                projection_applied=bool(recovered["projection_applied"]),
            )
        if prior_lease_id:
            if prior is not None and not prior.terminal:
                worker_api.pool.leases.cancel(
                    prior.lease_id,
                    reason=f"recovery successor requested by {request.get('plan_id')}",
                )
            if prior is not None:
                worker_api.cancel_task_graph_binding(
                    state,
                    reason=(
                        "recovery successor superseded the prior physical "
                        f"binding for {request.get('plan_id')}"
                    ),
                    actor_id="recovery-worker-successor",
                    causation_id=(
                        f"recovery-successor:{request.get('plan_id')}:"
                        f"{prior.lease_id}"
                    ),
                )
        successor_locations = sorted(
            {
                candidate.location.value
                for candidate in worker_api.pool.store.list_workers()
                if candidate.accepting_leases
                and candidate.worker_id not in excluded
            }
        )
        preferred_successor_ids = tuple(
            sorted(
                candidate.worker_id
                for candidate in worker_api.pool.store.list_workers()
                if prior_worker is not None
                and candidate.accepting_leases
                and candidate.worker_id not in excluded
                and candidate.endpoint == prior_worker.endpoint
                and not same_failure_boundary(prior_worker, candidate)
            )
        )
        if not successor_locations:
            raise RuntimeError(
                "recovery successor has no distinct physical failure boundary"
            )
        acquisition = worker_api.acquire_for_task(
            state,
            payload={
                "excluded_worker_ids": sorted(excluded),
                "required_capabilities": list(
                    (request.get("constraints") or {}).get("worker", {}).get(
                        "required_capabilities"
                    )
                    or ("agent_task",)
                ),
                "locations": successor_locations,
                # A managed deployment keeps its endpoint stable across a
                # process-generation restart.  Prefer the newly attested
                # generation at that endpoint before falling back to another
                # healthy failure boundary.
                "preferred_worker_ids": list(preferred_successor_ids),
                "idempotency_key": idempotency_key,
                "ttl_seconds": 3600.0,
            },
        )
        after = dict(state.metadata.get("worker_pool") or {})
        return persist_successor(
            lease=acquisition.lease,
            attempt=acquisition.attempt,
            worker=acquisition.worker,
            original_before=before,
            after_projection=after,
            recovered=False,
            projection_applied=True,
        )

    def backend_successor(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        before = dict(state.metadata.get("backend_route") or state.metadata.get("worker_pool") or {})
        provider = dict(state.metadata.get("provider_route") or {})
        workspace = task_workspace_root(
            task_id=state.task_id,
            session_id=str(state.metadata.get("query_session_id") or f"task:{state.task_id}"),
            worker_id=str(before.get("worker_id") or "recovery-worker"),
        )
        registry_store = BackendRegistryStore(backend_registry_path(artifact_root_path()))
        try:
            registry = BackendRegistry(registry_store)
            ensure_default_backends(registry)
            locations = tuple(
                BackendLocation(str(item))
                for item in (request.get("constraints") or {}).get("allowed_locations") or ()
            )
            excluded_backend_ids = {
                *[str(item) for item in request.get("excluded_refs") or () if str(item)],
                *([str(before.get("backend_id"))] if before.get("backend_id") else []),
            }
            sealed_terminal_exclusion = _task_is_sealed_control(state, request)
            if sealed_terminal_exclusion:
                excluded_backend_ids.update(registry.terminal_backend_ids())
            selection = BackendSelectionRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                runtime_worker="CodeWorkerRuntime",
                preferred_backend_id=None,
                required_capabilities=("code-change",),
                allowed_locations=locations,
                excluded_backend_ids=tuple(sorted(excluded_backend_ids)),
                workspace_root=str(workspace),
                artifact_root=str(artifact_root_path()),
                provider_route_id=str(provider.get("route_id") or "") or None,
                provider_route_checksum=str(provider.get("checksum") or ""),
                provider_catalog_revision=int(provider.get("catalog_revision") or 0),
                provider_credential_version=int(provider.get("credential_version") or 0),
                provider_credential_fingerprint=str(provider.get("credential_fingerprint") or ""),
                provider_transport_id=str(provider.get("transport_id") or ""),
                m0_execution_ref=str(state.metadata.get("m0_execution_ref") or ""),
                turn_id=str(state.metadata.get("turn_id") or f"recovery:{request.get('plan_id')}"),
                metadata={
                    "recovery_plan_id": str(request.get("plan_id") or ""),
                    "sealed_terminal_exclusion": sealed_terminal_exclusion,
                    "excluded_backend_ids": tuple(sorted(excluded_backend_ids)),
                },
            )
            lease = registry.acquire(selection)
            after = lease.to_dict()
            return {
                "accepted": True,
                "changed": before.get("backend_id") != lease.backend_id,
                "before": before,
                "after": after,
                "canonical_ref": {
                    "backend_lease_id": lease.lease_id,
                    "backend_id": lease.backend_id,
                    "registry_revision": lease.registry_revision,
                },
                "receipt_id": lease.lease_id,
                "message": "BackendRegistry allocated a successor backend lease",
            }
        finally:
            registry_store.close()

    def provider_successor(request: Mapping[str, Any], *, degrade: bool = False) -> Mapping[str, Any]:
        state = require_state(request)
        before = dict(state.metadata.get("provider_route") or {})
        provider_api = ProviderBackendApi(
            project_root=PROJECT_ROOT,
            artifact_root=artifact_root_path(),
            provider_database=runtime_configuration().path("state.provider"),
        )
        constraints = dict((request.get("constraints") or {}).get("provider") or {})
        excluded = [str(item) for item in request.get("excluded_refs") or () if str(item)]
        provider_ids = [
            str(item) for item in constraints.get("provider_ids") or ()
            if str(item) and str(item) not in excluded
        ]
        model_ids = [str(item) for item in constraints.get("degrade_model_ids") or () if str(item)] if degrade else [
            str(item) for item in constraints.get("model_ids") or () if str(item)
        ]
        response = provider_api.handle_post(
            ("providers", "routes"),
            {
                "request": {
                    "runId": state.run_id,
                    "taskId": state.task_id,
                    "nodeId": state.root_node_id,
                    "sessionId": str(state.metadata.get("query_session_id") or f"task:{state.task_id}"),
                    "turnId": str(state.metadata.get("turn_id") or f"recovery:{request.get('plan_id')}"),
                    "purpose": "reason",
                    "preferredProviderId": None,
                    "preferredModelId": None,
                    "routeHint": "recovery-degrade" if degrade else "recovery-failover",
                    "constraints": {
                        "providerIds": provider_ids,
                        "modelIds": model_ids,
                        "requiredInput": list(constraints.get("required_input") or ("text",)),
                        "requiredOutput": list(constraints.get("required_output") or ("text",)),
                        "requireTools": bool(constraints.get("require_tools", True)),
                        "requireStreaming": bool(constraints.get("require_streaming", False)),
                        "minimumContextWindow": int(constraints.get("minimum_context_window") or 0),
                        "maximumInputPricePerMillion": constraints.get("maximum_input_price_per_million"),
                        "maximumOutputPricePerMillion": constraints.get("maximum_output_price_per_million"),
                        "excludedCredentialIds": list(constraints.get("excluded_credential_ids") or ()),
                        "requiredScopes": list(constraints.get("required_scopes") or ()),
                    },
                    "metadata": {
                        "recovery_plan_id": str(request.get("plan_id") or ""),
                        "excluded_provider_ids": excluded,
                    },
                },
                "previous_route_id": str(before.get("routeId") or before.get("route_id") or "") or None,
            },
        )
        if response is None or int(response.status) >= 400:
            body = {} if response is None else dict(response.body)
            return {
                "accepted": False,
                "changed": False,
                "before": before,
                "after": before,
                "error_code": str(body.get("error") or "provider_route_unavailable"),
                "message": str(body.get("message") or "ProviderControlPlane rejected route acquisition"),
            }
        after = dict(response.body.get("result") or {})
        return {
            "accepted": True,
            "changed": before.get("routeId") != after.get("routeId"),
            "before": before,
            "after": {
                **after,
                "route_id": after.get("routeId", ""),
                "provider_id": after.get("providerId", ""),
                "model_id": after.get("modelId", ""),
                "credential_id": after.get("credentialId", ""),
                "transport_id": after.get("transportId", ""),
            },
            "canonical_ref": {
                "route_id": after.get("routeId", ""),
                "checksum": after.get("checksum", ""),
                "catalog_revision": after.get("catalogRevision", 0),
            },
            "receipt_id": str(after.get("routeId") or ""),
            "message": "ProviderControlPlane allocated a recovery route",
        }

    def graph_replan(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        worker_api = get_worker_pool_api()
        graph_id_value = worker_api.ensure_task_graph(state)
        before = worker_api.topology.version_ref(graph_id_value).to_dict()
        prior_requirement_revision = str(
            state.metadata.get("requirement_revision")
            or "requirements:initial"
        )
        requirement_revision = (
            "requirements:recovery:"
            + str(request.get("signal_id") or request.get("plan_id") or "unknown")
        )
        branch = worker_api.graph_custody.branch(
            graph_id_value,
            branch_id=f"recovery:{request.get('plan_id')}",
            actor_id="RecoveryDecisionRuntime",
            causation_id=str(request.get("signal_id") or ""),
            idempotency_key=str(request.get("idempotency_key") or ""),
            metadata={"recovery_action": "replan"},
        )
        branch.set_metadata("recovery_replan", {
            "plan_id": str(request.get("plan_id") or ""),
            "signal_id": str(request.get("signal_id") or ""),
            "reason": str(request.get("reason") or "recovery replan"),
            "prior_requirement_revision": prior_requirement_revision,
            "requirement_revision": requirement_revision,
            "requested_at": now_iso(),
        })
        committed = worker_api.graph_custody.commit(branch.build())
        if not committed.receipt.committed:
            return {
                "accepted": False,
                "before": before,
                "after": before,
                "error_code": "graph_replan_conflict",
                "message": "GraphStateCustody rejected recovery replan",
                "metadata": {"receipt": committed.receipt.to_dict()},
            }
        after = worker_api.topology.version_ref(graph_id_value).to_dict()
        state.metadata["dynamic_graph_ref"] = after
        prior_execution = {
            "requirement_revision": prior_requirement_revision,
            "executed_operator_refs": list(
                state.metadata.get("phase2_executed_operator_refs") or ()
            ),
            "execution_layers": list(
                state.metadata.get("phase2_operator_execution_layers") or ()
            ),
        }
        if prior_execution["executed_operator_refs"] or prior_execution["execution_layers"]:
            history = list(
                state.metadata.get("phase2_requirement_execution_history") or ()
            )
            history.append(prior_execution)
            state.metadata["phase2_requirement_execution_history"] = history[-16:]
        state.metadata.pop("phase2_executed_operator_refs", None)
        state.metadata.pop("phase2_operator_execution_layers", None)
        state.metadata.pop("phase2_adaptive_depth_pass", None)
        state.metadata["requirement_revision"] = requirement_revision
        state.metadata["requirement_change_ref"] = {
            "owner": "GraphStateCustody",
            "commit_id": committed.receipt.commit_id,
            "signal_id": str(request.get("signal_id") or ""),
            "prior_requirement_revision": prior_requirement_revision,
            "requirement_revision": requirement_revision,
        }
        store.save_checkpoint(state)
        return {
            "accepted": True,
            "changed": before != after,
            "before": before,
            "after": after,
            "canonical_ref": after,
            "receipt_id": committed.receipt.commit_id,
            "message": "GraphStateCustody committed a recovery replan mutation",
        }

    def retry_request(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        root = state.plan_nodes.get(state.root_node_id)
        before = {
            "task_status": str(state.status),
            "root_status": str(root.status) if root is not None else "",
            "retry_generation": int((state.metadata.get("recovery_runtime") or {}).get("retry_generation") or 0),
        }
        if root is not None and root.status in {PlanNodeStatus.FAILED, PlanNodeStatus.BLOCKED}:
            root.status = PlanNodeStatus.PENDING
            root.updated_at = now_iso()
        if state.status in {PlanNodeStatus.FAILED, PlanNodeStatus.BLOCKED}:
            state.status = PlanNodeStatus.PENDING
        after = {
            **before,
            "task_status": str(state.status),
            "root_status": str(root.status) if root else "",
            "retry_generation": int(before["retry_generation"]) + 1,
        }
        store.save_checkpoint(state)
        return {
            "accepted": True,
            "changed": True,
            "before": before,
            "after": after,
            "receipt_id": str(request.get("request_digest") or ""),
            "message": "QueryEngine task projection was reopened for a bounded retry",
            "metadata": {"retry_delay_ms": (request.get("metadata") or {}).get("retry_delay_ms", 0)},
        }

    def permission_request(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        context = dict(request.get("context") or {})
        permission_state = dict(context.get("permission_state") or {})
        session_id = str(state.metadata.get("query_session_id") or permission_state.get("session_id") or f"task:{state.task_id}")
        facade = get_permission_api_facade(task_id=state.task_id, session_id=session_id)
        token = str(permission_state.get("custody_token") or "")
        if not token:
            opened = facade.open_session(
                session_id=session_id,
                run_id=state.run_id,
                task_id=state.task_id,
                external_session_exists=False,
            )
            token = str((opened.body.get("session") or {}).get("bearer_token") or "")
        authority = facade.authority(
            session_id=session_id,
            run_id=state.run_id,
            task_id=state.task_id,
            custody_token=token,
            actor_id="recovery-runtime",
        )
        tool_name = str(permission_state.get("tool_name") or "recovery-action")
        response = facade.create_request(
            authority,
            {
                "run_id": state.run_id,
                "task_id": state.task_id,
                "session_id": session_id,
                "tool_use_id": str(permission_state.get("tool_call_id") or request.get("signal_id") or request.get("plan_id")),
                "tool_identity": {
                    "namespace": str(permission_state.get("tool_namespace") or "recovery"),
                    "name": tool_name,
                    "server_id": str(permission_state.get("server_id") or ""),
                },
                "arguments": dict(permission_state.get("arguments") or {}),
                "worker_request_id": str(permission_state.get("worker_request_id") or "recovery-runtime"),
                "node_id": state.root_node_id,
                "mode": PermissionMode.DEFAULT.value,
                "workspace_root": str(permission_session_workspace_root(task_id=state.task_id, session_id=session_id)),
                "interactive": True,
                "requires_interaction": True,
                "reason_code": "recovery.permission_required",
                "reason": str(request.get("reason") or "recovery requires permission"),
            },
            service_token=os.environ.get("ZYRA_PERMISSION_SERVICE_TOKEN", ""),
        )
        persist_events(store, list(response.events))
        result = dict(response.body.get("result") or {})
        request_projection = dict(result.get("request") or result)
        request_id = str(request_projection.get("request_id") or "")
        return {
            "accepted": bool(request_id),
            "changed": bool(request_id),
            "before": {"pending": False},
            "after": {"pending": True, "request_id": request_id, "session_id": session_id},
            "canonical_ref": {"permission_request_id": request_id},
            "receipt_id": request_id,
            "message": "PermissionControlPlane created an interactive recovery request",
            "metadata": {"deferred": True, "custody_token_persisted": False},
        }

    def mcp_authenticate(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        signal = dict(request.get("plan") or {})
        server_id = str(
            (signal.get("signal") or {}).get("refs", {}).get("mcp_server_id")
            or (request.get("context") or {}).get("metadata", {}).get("mcp_server_id")
            or ""
        )
        if not server_id:
            return {
                "accepted": False,
                "error_code": "mcp_server_id_missing",
                "message": "MCP authentication requires a structured server id",
            }
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            event_type=EventType.MCP_AUTH_CHANGED,
            payload={
                "server_id": server_id,
                "state": "needs_auth",
                "control_action": "authenticate",
                "recovery_plan_id": str(request.get("plan_id") or ""),
                "credential_material_in_event": False,
            },
        )
        persist_events(store, [event])
        return {
            "accepted": True,
            "changed": True,
            "before": {"server_id": server_id, "state": "needs_auth"},
            "after": {"server_id": server_id, "state": "authenticating", "event_id": event.event_id},
            "canonical_ref": {"event_id": event.event_id, "server_id": server_id},
            "receipt_id": event.event_id,
            "message": "MCP authentication control path was opened",
            "metadata": {"deferred": True, "credential_material_persisted": False},
        }

    def compact_context(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        before = len(store.task_compactions(state.task_id))
        result = _memory_fabric(store).compact_context(
            state,
            store.task_events(state.task_id),
            focus=str(request.get("reason") or "recovery prompt-too-long compaction"),
            source_event_id=str(request.get("signal_id") or ""),
            persist=True,
        )
        after = len(store.task_compactions(state.task_id))
        return {
            "accepted": True,
            "changed": after > before,
            "before": {"compaction_count": before},
            "after": {
                "compaction_count": after,
                "compact_id": result.compact_id,
                "artifact_ids": list(result.artifact_ids),
                "compression_ratio": result.compression_ratio,
            },
            "canonical_ref": {"compact_id": result.compact_id, "artifact_ids": list(result.artifact_ids)},
            "receipt_id": result.compact_id,
            "message": "MemoryFabric compacted the canonical task context",
        }

    def abort_task(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        before = {"status": str(state.status)}
        state.status = PlanNodeStatus.FAILED
        root = state.plan_nodes.get(state.root_node_id)
        if root is not None and root.status not in {PlanNodeStatus.COMPLETED, PlanNodeStatus.CANCELLED}:
            root.status = PlanNodeStatus.FAILED
            root.updated_at = now_iso()
        store.save_checkpoint(state)
        return {
            "accepted": True,
            "changed": before["status"] != str(state.status),
            "before": before,
            "after": {"status": str(state.status)},
            "receipt_id": str(request.get("request_digest") or ""),
            "message": "TaskState entered a terminal failed state",
        }

    def session_resume(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "phase": "session_resume_recovery",
                    "session_id": str(request.get("session_id") or ""),
                    "checkpoint_id": str(request.get("checkpoint_id") or ""),
                    "worker_request_id": str(state.metadata.get("worker_request_id") or ""),
                    "ok": True,
                }
            },
        )
        persist_events(store, [event])
        return {
            "accepted": True,
            "changed": True,
            "before": {"resume_event_id": ""},
            "after": {"resume_event_id": event.event_id},
            "canonical_ref": {"event_id": event.event_id},
            "receipt_id": event.event_id,
        }

    def compact_restore(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        snapshot = _memory_fabric(store).refresh_task_memory(
            state,
            store.task_events(state.task_id),
            persist=True,
        )
        return {
            "accepted": True,
            "changed": bool(snapshot.records),
            "before": {},
            "after": {"memory_record_count": len(snapshot.records)},
            "canonical_ref": {"memory_ids": [item.memory_id for item in snapshot.records]},
            "receipt_id": new_id("compact_restore"),
        }

    def worker_rebind(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        before = dict(state.metadata.get("worker_pool") or {})
        acquisition = get_worker_pool_api().ensure_task_lease(
            state,
            payload={"idempotency_key": str(request.get("idempotency_key") or "")},
        )
        after = dict(state.metadata.get("worker_pool") or {})
        store.save_checkpoint(state)
        return {
            "accepted": True,
            "changed": before != after,
            "before": before,
            "after": after,
            "canonical_ref": after,
            "receipt_id": str(after.get("lease_id") or new_id("worker_rebind")),
            "metadata": {"existing_lease_reused": acquisition is None},
        }

    def graph_rebind(request: Mapping[str, Any]) -> Mapping[str, Any]:
        state = require_state(request)
        graph_id_value = get_worker_pool_api().ensure_task_graph(state)
        snapshot = get_worker_pool_api().graph_custody.current(graph_id_value)
        ref = get_worker_pool_api().topology.version_ref(graph_id_value).to_dict()
        store.save_checkpoint(state)
        return {
            "accepted": True,
            "changed": False,
            "before": ref,
            "after": ref,
            "canonical_ref": ref,
            "receipt_id": snapshot.commit_id,
        }

    def permission_snapshot(signal: Any) -> Mapping[str, Any]:
        state = get_permission_control_plane().state_store.read_state()
        requests = state.get("requests") if isinstance(state, Mapping) else {}
        task_requests = [
            value for value in (requests or {}).values()
            if isinstance(value, Mapping) and str(value.get("task_id") or "") == signal.refs.task_id
        ]
        return {
            "revision": int(state.get("revision") or 0),
            "pending_count": sum(str(item.get("status") or "") == "pending" for item in task_requests),
            "request_ids": [str(item.get("request_id") or "") for item in task_requests],
            "secret_values_exposed": False,
        }

    def worker_snapshot(signal: Any) -> Mapping[str, Any]:
        state = store.load_task(signal.refs.task_id)
        projection = dict(state.metadata.get("worker_pool") or {}) if state is not None else {}
        worker_id = str(projection.get("worker_id") or "")
        health = None
        if worker_id:
            try:
                health = get_worker_pool_api().pool.heartbeats.assess(worker_id).to_dict()
            except Exception:
                health = None
        return {**projection, "health": health, "available": state is not None}

    def backend_snapshot(signal: Any) -> Mapping[str, Any]:
        state = store.load_task(signal.refs.task_id)
        return {
            **(dict(state.metadata.get("backend_route") or {}) if state is not None else {}),
            "available": state is not None,
        }

    def provider_snapshot(signal: Any) -> Mapping[str, Any]:
        state = store.load_task(signal.refs.task_id)
        projection = dict(state.metadata.get("provider_route") or {}) if state is not None else {}
        response = ProviderBackendApi(
            project_root=PROJECT_ROOT,
            artifact_root=artifact_root_path(),
            provider_database=runtime_configuration().path("state.provider"),
        ).handle_get(("providers", "routes"), {"run_id": signal.refs.run_id, "task_id": signal.refs.task_id})
        routes = [] if response is None or int(response.status) >= 400 else response.body.get("result", [])
        return {**projection, "routes": routes, "available": response is not None and int(response.status) < 400}

    return CanonicalOwnerCallbacks(
        worker_successor=worker_successor,
        backend_successor=backend_successor,
        provider_successor=provider_successor,
        model_degrade=lambda request: provider_successor(request, degrade=True),
        graph_replan=graph_replan,
        retry_request=retry_request,
        permission_request=permission_request,
        mcp_authenticate=mcp_authenticate,
        compact_context=compact_context,
        abort_task=abort_task,
        session_resume=session_resume,
        compact_restore=compact_restore,
        worker_rebind=worker_rebind,
        graph_rebind=graph_rebind,
        permission_snapshot=permission_snapshot,
        worker_snapshot=worker_snapshot,
        backend_snapshot=backend_snapshot,
        provider_snapshot=provider_snapshot,
    )


_RECOVERY_EXECUTION_CONTINUATION_ACTIONS = frozenset({
    "retry",
    "reroute",
    "switch_backend",
    "switch_provider",
    "degrade_model",
    "replan",
    "compact",
    "resume_checkpoint",
})


def _recovery_continuation_request_digest(request: Mapping[str, Any]) -> str:
    payload = {
        "run_id": str(request.get("run_id") or ""),
        "task_id": str(request.get("task_id") or ""),
        "plan_id": str(request.get("plan_id") or ""),
        "signal_id": str(request.get("signal_id") or ""),
        "action": str(request.get("action") or ""),
        "action_receipt_ids": list(request.get("action_receipt_ids") or ()),
        "route_decision_id": str(request.get("route_decision_id") or ""),
        "checkpoint_id": str(request.get("checkpoint_id") or ""),
        "context_digest": str(request.get("context_digest") or ""),
    }
    encoded = json.dumps(to_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reopen_task_for_recovery_continuation(
    state: Any,
    action: str,
    *,
    plan_id: str = "",
) -> bool:
    """Reopen only the graph stages whose execution must consume a recovery result."""

    if action not in _RECOVERY_EXECUTION_CONTINUATION_ACTIONS:
        return False
    if state.status == PlanNodeStatus.CANCELLED:
        raise RuntimeError("cancelled tasks cannot dispatch a recovery continuation")
    # Every physical continuation needs a fresh ResourceScheduler decision
    # and lease.  Reusing a completed task's prior route would retain a
    # released placement binding and fail the execution fence before any new
    # canonical attempt can start.
    stages = {"route", "execute", "verify", "finalize"}
    prior_execution = {
        "plan_id": plan_id,
        "action": action,
        "requirement_revision": str(
            state.metadata.get("requirement_revision")
            or "requirements:initial"
        ),
        "executed_operator_refs": list(
            state.metadata.get("phase2_executed_operator_refs") or ()
        ),
        "execution_layers": list(
            state.metadata.get("phase2_operator_execution_layers") or ()
        ),
    }
    if prior_execution["executed_operator_refs"] or prior_execution["execution_layers"]:
        history = list(
            state.metadata.get("phase2_recovery_execution_history") or ()
        )
        history.append(prior_execution)
        state.metadata["phase2_recovery_execution_history"] = history[-16:]
    for key in (
        "phase2_executed_operator_refs",
        "phase2_operator_execution_layers",
        "phase2_adaptive_depth_pass",
        "phase2_final_verifier_scope",
        "phase2_final_verifier_decision",
        "operator_placement_binding",
        "worker_pool_receipt",
        "physical_execution_failure_receipt",
    ):
        state.metadata.pop(key, None)
    changed = state.status != PlanNodeStatus.PENDING
    state.status = PlanNodeStatus.PENDING
    for node in state.plan_nodes.values():
        if str(node.metadata.get("stage") or "") not in stages:
            continue
        if node.status in {PlanNodeStatus.CANCELLED, PlanNodeStatus.SUPERSEDED}:
            continue
        changed = changed or node.status != PlanNodeStatus.PENDING
        node.status = PlanNodeStatus.PENDING
        node.updated_at = now_iso()
    state.updated_at = now_iso()
    return changed


def _observe_delivery_contract_paths(
    state: Any,
    paths: Sequence[str],
) -> dict[str, str]:
    """Digest the delivery-contract paths inside the task workspace.

    Used to resolve an unknown physical dispatch outcome: if the paths a side
    effect would touch are byte-identical before and after a lost node, the
    operator never wrote and one re-dispatch is safe.  Absent files are a
    stable observation, not an error.
    """

    workspace_ref = dict(getattr(state, "metadata", {}).get("workspace_ref") or {})
    workspace_id = str(workspace_ref.get("workspace_id") or "")
    if not workspace_id:
        return {}
    service = WorkspaceApiService(get_workspace_manager())
    observed: dict[str, str] = {}
    for path in paths:
        selected = str(path).strip()
        if not selected:
            continue
        try:
            response = service.read_file(
                workspace_id,
                {"path": selected, "mount": "task", "encoding": "base64"},
            )
        except WorkspaceError:
            observed[selected] = "absent"
            continue
        body = dict(response.body or {})
        observed[selected] = hashlib.sha256(
            str(body.get("content") or "").encode("utf-8")
        ).hexdigest()
    return observed


def _fence_pending_task_reservation(
    pool_api: WorkerPoolApiService,
    state: Any,
    *,
    reason: str,
) -> str:
    """Fence an API-process reservation before binding a real deployment worker."""

    projection = state.metadata.get("worker_pool")
    if not isinstance(projection, Mapping):
        return ""
    lease_id = str(projection.get("lease_id") or "")
    lease = pool_api.pool.store.get_lease(lease_id) if lease_id else None
    if lease is None or lease.terminal:
        pool_api.reconcile_task_graph_binding(
            state,
            reason=reason,
            actor_id="task-reservation-fence",
            causation_id=f"task-reservation-fence-replay:{lease_id or 'missing'}",
        )
        return ""
    # This lease is an unstarted API reservation, not a cancelled user
    # action.  Expiry both advances the fence epoch and records that the
    # reservation was superseded without misattributing an autonomous
    # continuation to a rejected manual cancellation.
    pool_api.pool.leases.expire(lease_id, reason=reason)
    pool_api.reconcile_task_graph_binding(
        state,
        reason=reason,
        actor_id="task-reservation-fence",
        causation_id=f"task-reservation-fence:{lease_id}",
    )
    return lease_id


def _recovery_execution_projection(state: Any) -> dict[str, Any]:
    execute_nodes = [
        node
        for node in state.plan_nodes.values()
        if str(node.metadata.get("stage") or "") == "execute"
    ]
    execute = execute_nodes[-1] if execute_nodes else None
    raw_backend_dispatch = (
        dict(execute.metadata.get("backend_dispatch") or {})
        if execute is not None and isinstance(execute.metadata.get("backend_dispatch"), Mapping)
        else {}
    )
    raw_envelope = dict(raw_backend_dispatch.get("final_envelope") or {})
    raw_route_ref = dict(raw_backend_dispatch.get("provider_route_ref") or {})
    raw_worker_receipt = dict(state.metadata.get("worker_pool_receipt") or {})
    raw_physical_dispatch = dict(
        raw_worker_receipt.get("physical_dispatch_receipt") or {}
    )
    raw_physical_validation = dict(
        raw_worker_receipt.get("physical_dispatch_validation") or {}
    )
    raw_physical_payload = dict(
        raw_physical_dispatch.get("payload") or {}
    )
    raw_operator_binding = dict(
        state.metadata.get("operator_placement_binding") or {}
    )
    backend_dispatch = {
        "final_envelope": {
            key: raw_envelope.get(key)
            for key in (
                "envelope_id",
                "backend_id",
                "backend_lease_id",
                "provider_route_id",
                "provider_transport_id",
                "runtime_worker",
            )
            if raw_envelope.get(key) not in (None, "")
        },
        "provider_route_ref": {
            key: raw_route_ref.get(key)
            for key in ("route_id", "transport_id", "catalog_revision")
            if raw_route_ref.get(key) not in (None, "")
        },
        "state_owner": str(raw_backend_dispatch.get("state_owner") or ""),
    }
    return {
        "status": str(state.status),
        "artifact_count": len(state.artifacts),
        "executed_operator_refs": list(
            state.metadata.get("phase2_executed_operator_refs") or ()
        ),
        "operator_execution_layers": [
            {
                key: item.get(key)
                for key in (
                    "layer_index",
                    "operator_ref",
                    "resource_decision_id",
                    "worker_id",
                    "domain_result_kind",
                    "output_contract_fulfilled",
                )
            }
            for item in state.metadata.get(
                "phase2_operator_execution_layers"
            )
            or ()
            if isinstance(item, Mapping)
        ],
        "runtime_hints": dict(state.metadata.get("runtime_hints") or {}),
        "recovery_worker_route": dict(
            state.metadata.get("recovery_worker_route") or {}
        ),
        "execute_node_id": str(getattr(execute, "node_id", "")),
        "execute_status": str(getattr(execute, "status", "")),
        "assigned_worker_id": str(getattr(execute, "assigned_worker_id", "") or ""),
        "worker_error": str((getattr(execute, "metadata", {}) or {}).get("worker_error") or ""),
        "result_summary": str((getattr(execute, "metadata", {}) or {}).get("result_summary") or ""),
        "backend_dispatch": backend_dispatch,
        "physical_dispatch": {
            "schema_version": str(
                raw_physical_dispatch.get("schema_version") or ""
            ),
            "digest": str(raw_physical_dispatch.get("digest") or ""),
            "placement_decision_id": str(
                raw_physical_payload.get("placement_decision_id") or ""
            ),
            "lease_id": str(raw_physical_payload.get("lease_id") or ""),
            "physical_attempt_id": str(
                raw_physical_payload.get("physical_attempt_id") or ""
            ),
            "call_receipt": dict(
                raw_physical_payload.get("call_receipt") or {}
            ),
            "real_gate_closed": (
                raw_physical_validation.get("real_gate_closed") is True
            ),
        },
        "physical_execution_failure": dict(
            state.metadata.get("physical_execution_failure_receipt") or {}
        ),
        "operator_placement_binding": raw_operator_binding,
        "worker_pool": dict(state.metadata.get("worker_pool") or {}),
        "backend_route": dict(state.metadata.get("backend_route") or {}),
        "provider_route": dict(state.metadata.get("provider_route") or {}),
    }


def _recovery_continuation_owners(
    store: SQLiteStore,
) -> dict[str, CallbackContinuationOwner]:
    """Bind recovery continuation to a fenced graph/worker dispatch, not an event ACK."""

    def port(key: str, owner: str) -> CallbackContinuationOwner:
        def continue_execution(request: Mapping[str, Any]) -> Mapping[str, Any]:
            task_id = str(request.get("task_id") or "")
            run_id = str(request.get("run_id") or "")
            action = str(request.get("action") or "")
            idempotency_key = str(request.get("idempotency_key") or "").strip()
            request_digest = _recovery_continuation_request_digest(request)
            if not task_id or not run_id or not idempotency_key:
                return {
                    "accepted": False,
                    "changed": False,
                    "error_code": "continuation_identity_missing",
                    "message": "recovery continuation requires run, task and idempotency identity",
                }
            with _task_lock(task_id):
                state = store.load_task(task_id)
                if state is None:
                    return {
                        "accepted": False,
                        "changed": False,
                        "error_code": "task_not_found",
                        "message": f"continuation task does not exist: {task_id}",
                    }
                if str(state.run_id) != run_id:
                    return {
                        "accepted": False,
                        "changed": False,
                        "error_code": "run_identity_mismatch",
                        "message": "continuation owner rejected cross-run dispatch",
                    }
                before = _recovery_execution_projection(state)
                if action not in _RECOVERY_EXECUTION_CONTINUATION_ACTIONS:
                    action_receipts = [str(item) for item in request.get("action_receipt_ids") or () if str(item)]
                    changed = action in {"ask_permission", "authenticate_mcp", "abort"} and bool(action_receipts)
                    return {
                        "accepted": changed,
                        "changed": changed,
                        "before": before,
                        "after": _recovery_execution_projection(state),
                        "canonical_ref": {
                            "owner": owner,
                            "action_receipt_ids": action_receipts,
                            "event_only": False,
                            "worker_dispatch_consumed": False,
                        },
                        "receipt_id": action_receipts[-1] if action_receipts else "",
                        "message": f"{owner} retained the canonical blocked or terminal state",
                        "metadata": {"tool_dispatch_allowed": False, "event_only": False},
                    }

                fences = dict(state.metadata.get("recovery_continuation_fences") or {})
                existing = fences.get(idempotency_key)
                if isinstance(existing, Mapping):
                    if str(existing.get("request_digest") or "") != request_digest:
                        return {
                            "accepted": False,
                            "changed": False,
                            "error_code": "continuation_idempotency_conflict",
                            "message": "continuation idempotency key changed request content",
                        }
                    if str(existing.get("phase") or "") == "committed" and isinstance(existing.get("receipt"), Mapping):
                        replay = dict(existing["receipt"])
                        replay["metadata"] = {**dict(replay.get("metadata") or {}), "replayed": True}
                        return replay
                    return {
                        "accepted": False,
                        "changed": False,
                        "error_code": "continuation_dispatch_indeterminate",
                        "message": "a prepared recovery dispatch cannot be replayed without its committed receipt",
                    }

                _reopen_task_for_recovery_continuation(
                    state,
                    action,
                    plan_id=str(request.get("plan_id") or ""),
                )
                # A continuation may reopen a completed task whose prior
                # QueryEngine session still exists.  Reusing either the signal
                # session or ``runtime_hints.session_id`` without its private
                # custody token is an ownership violation.  Give every plan a
                # deterministic fresh session instead; idempotent replay of
                # the same plan retains the same fenced identity.
                recovery_session_id = (
                    f"query:{run_id}:{task_id}:recovery:"
                    f"{str(request.get('plan_id') or request_digest[:24])}"
                )
                runtime_hints = dict(state.metadata.get("runtime_hints") or {})
                runtime_hints["session_id"] = recovery_session_id
                state.metadata["runtime_hints"] = runtime_hints
                state.metadata["recovery_continuation_session"] = {
                    "session_id": recovery_session_id,
                    "plan_id": str(request.get("plan_id") or ""),
                    "action": action,
                    "custody_mode": "new_fenced_session",
                    "persisted_custody_token": False,
                }
                fences[idempotency_key] = {
                    "phase": "prepared",
                    "request_digest": request_digest,
                    "plan_id": str(request.get("plan_id") or ""),
                    "action": action,
                    "prepared_at": now_iso(),
                }
                state.metadata["recovery_continuation_fences"] = dict(list(fences.items())[-64:])
                store.save_checkpoint(state)

                pool_api = get_worker_pool_api()
                pool_journal = pool_api.pool.store.journal(limit=10000)
                pool_sequence = pool_journal[-1].sequence if pool_journal else 0
                events: list[EventRecord] = []
                execution_events: tuple[EventRecord, ...] = ()
                try:
                    _fence_pending_task_reservation(
                        pool_api,
                        state,
                        reason=(
                            "superseded by recovery continuation physical dispatch "
                            f"{idempotency_key}"
                        ),
                    )
                    events = run_task_graph(state, execution_context=graph_execution_context())
                    execution_events = tuple(events)
                    pool_api.finalize_task(
                        state,
                        success=state.status == PlanNodeStatus.COMPLETED,
                        summary=f"recovery continuation finished with status {state.status}",
                    )
                    events.extend(
                        event
                        for event in pool_api.pool.events.project_after(pool_api.pool.store, pool_sequence)
                        if event.run_id == run_id and event.task_id == task_id
                    )
                    after = _recovery_execution_projection(state)
                    worker_dispatch = dict(after.get("backend_dispatch") or {})
                    physical_dispatch = dict(
                        after.get("physical_dispatch") or {}
                    )
                    operator_binding = dict(
                        after.get("operator_placement_binding") or {}
                    )
                    legacy_dispatch_consumed = bool(
                        worker_dispatch.get("final_envelope")
                    )
                    physical_dispatch_consumed = bool(
                        physical_dispatch.get("schema_version")
                        == "zyra.physical-dispatch-receipt/v2"
                        and physical_dispatch.get("digest")
                        and physical_dispatch.get("real_gate_closed") is True
                        and physical_dispatch.get("placement_decision_id")
                        == operator_binding.get("resource_decision_id")
                        and physical_dispatch.get("lease_id")
                        == operator_binding.get("lease_id")
                        and physical_dispatch.get("physical_attempt_id")
                        == operator_binding.get("attempt_id")
                    )
                    consumed = (
                        legacy_dispatch_consumed
                        or physical_dispatch_consumed
                    )
                    if not events or not consumed or state.status != PlanNodeStatus.COMPLETED:
                        raise RuntimeError(
                            "recovery continuation did not complete a canonical physical dispatch: "
                            f"event_count={len(events)}, dispatch_consumed={consumed}, "
                            f"task_status={state.status}"
                        )
                    persist_events(store, events)
                    event_ids = [event.event_id for event in events]
                    dispatch_id = event_ids[-1]
                    canonical_ref = {
                        "event_id": dispatch_id,
                        "execution_event_ids": event_ids,
                        "owner": owner,
                        "event_only": False,
                        "worker_dispatch_consumed": True,
                        "canonical_physical_dispatch_consumed": (
                            physical_dispatch_consumed
                        ),
                        "worker_id": str(after.get("assigned_worker_id") or ""),
                        "worker_lease_id": str((after.get("worker_pool") or {}).get("lease_id") or ""),
                        "backend_id": str(
                            ((worker_dispatch.get("final_envelope") or {}).get("backend_id"))
                            or operator_binding.get("backend_id")
                            or ((after.get("backend_route") or {}).get("backend_id"))
                            or ""
                        ),
                        "provider_route_id": str(
                            ((worker_dispatch.get("provider_route_ref") or {}).get("route_id"))
                            or ((after.get("provider_route") or {}).get("route_id"))
                            or ""
                        ),
                        "session_id": recovery_session_id,
                        "physical_dispatch_receipt_digest": str(
                            physical_dispatch.get("digest") or ""
                        ),
                    }
                    receipt = {
                        "accepted": True,
                        "changed": before != after,
                        "dispatch_id": dispatch_id,
                        "before": before,
                        "after": after,
                        "canonical_ref": canonical_ref,
                        "receipt_id": dispatch_id,
                        "message": f"{owner} completed the recovery continuation through the task graph",
                        "metadata": {
                            "execution_event_count": len(event_ids),
                            "tool_dispatch_allowed": True,
                            "event_only": False,
                            "replayed": False,
                        },
                    }
                    fences = dict(state.metadata.get("recovery_continuation_fences") or {})
                    fences[idempotency_key] = {
                        "phase": "committed",
                        "request_digest": request_digest,
                        "plan_id": str(request.get("plan_id") or ""),
                        "action": action,
                        "committed_at": now_iso(),
                        "receipt": receipt,
                    }
                    state.metadata["recovery_continuation_fences"] = dict(list(fences.items())[-64:])
                    store.save_checkpoint(state)
                    return receipt
                except Exception as error:
                    failed_state = store.load_task(task_id) or state
                    failed_fences = dict(failed_state.metadata.get("recovery_continuation_fences") or {})
                    # Pool journal projections are appended after graph events
                    # and can otherwise displace the physical failure from a
                    # bounded diagnostic tail.  Prefer the canonical graph
                    # execution slice whenever it was returned.
                    diagnostic_events = execution_events or tuple(events)
                    event_tail = [
                        {
                            "event_id": event.event_id,
                            "event_type": str(event.event_type),
                            "node_id": str(event.node_id or ""),
                            "transition": str(event.payload.get("transition") or ""),
                            "summary": str(event.payload.get("summary") or "")[:300],
                            "error": str(event.payload.get("error") or ""),
                            "message": str(event.payload.get("message") or "")[:300],
                        }
                        for event in diagnostic_events[-12:]
                    ]
                    failure_projection = _recovery_execution_projection(state)
                    failed_fences[idempotency_key] = {
                        "phase": "failed",
                        "request_digest": request_digest,
                        "plan_id": str(request.get("plan_id") or ""),
                        "action": action,
                        "failed_at": now_iso(),
                        "error_type": type(error).__name__,
                        "error_message": str(error)[:2000],
                        "execution_projection": failure_projection,
                        "event_tail": event_tail,
                    }
                    failed_state.metadata["recovery_continuation_fences"] = dict(list(failed_fences.items())[-64:])
                    store.save_checkpoint(failed_state)
                    return {
                        "accepted": False,
                        "changed": False,
                        "before": before,
                        "after": _recovery_execution_projection(failed_state),
                        "error_code": "continuation_dispatch_failed",
                        "message": (
                            f"{str(error)[:1400]}; "
                            f"event_tail={json.dumps(event_tail, sort_keys=True)[:500]}"
                        ),
                        "metadata": {
                            "event_only": False,
                            "replay_forbidden": True,
                            "execution_projection": failure_projection,
                            "event_tail": event_tail,
                        },
                    }

        return CallbackContinuationOwner(owner, continue_execution)

    return {
        "query": port("query", "QueryEngine"),
        "scheduler": port("scheduler", "ResourceScheduler"),
        "graph": port("graph", "GraphStateCustody"),
        "permission": port("permission", "PermissionControlPlane"),
        "mcp": port("mcp", "McpControlRuntime"),
        "task": port("task", "TaskState"),
    }


def _recovery_projection_ports(
    state_runtime: CanonicalTaskStateRuntime,
) -> dict[str, CallbackProjectionPort]:
    """Read action receipts back through the canonical TaskState projection."""

    def snapshot(plan: Any, receipt: Any) -> Mapping[str, Any]:
        state = state_runtime.require(plan.signal.refs.task_id)
        if str(state.run_id) != plan.signal.refs.run_id:
            raise ValueError("recovery projection crosses run custody")
        recovery = dict(state.metadata.get("recovery_runtime") or {})
        mutations = [
            dict(item)
            for item in recovery.get("mutation_history") or (recovery.get("last_mutation") or {},)
            if isinstance(item, Mapping)
        ]
        mutation = next(
            (
                item
                for item in reversed(mutations)
                if str(item.get("receipt_id") or "") == receipt.external_receipt_ref
            ),
            {},
        )
        if not mutation:
            raise ValueError(
                "canonical TaskState does not reference the applied owner receipt: "
                f"action={receipt.action.value}, owner={receipt.owner}, ref={receipt.external_receipt_ref}"
            )
        if str(mutation.get("action") or "") != receipt.action.value:
            raise ValueError("canonical TaskState action differs from the applied receipt")
        return {
            "run_id": str(state.run_id),
            "task_id": str(state.task_id),
            "revision": str(recovery.get("revision") or ""),
            "projection": {
                "active_plan_id": str(recovery.get("active_plan_id") or ""),
                "receipt_id": str(mutation.get("receipt_id") or ""),
                "action": str(mutation.get("action") or ""),
                "owner": str(mutation.get("owner") or ""),
                "changed": bool(mutation.get("changed")),
                "canonical_ref": dict(mutation.get("canonical_ref") or {}),
            },
        }

    return {
        action: CallbackProjectionPort("TaskState", snapshot)
        for action in (
            "retry",
            "compact",
            "ask_permission",
            "authenticate_mcp",
            "abort",
        )
    }


def _persist_recovery_proof_event(
    store: SQLiteStore,
    event_type: str,
    proof: Any,
) -> Mapping[str, Any]:
    value = proof.to_dict() if hasattr(proof, "to_dict") else dict(proof)
    event = EventRecord(
        run_id=str(value["run_id"]),
        task_id=str(value["task_id"]),
        node_id=None,
        event_type=EventType.TOPOLOGY_ROUTE,
        payload={
            "recovery_runtime": {
                "phase": event_type,
                "proof": value,
                "opaque_external_owner": False,
            }
        },
    )
    persist_events(store, [event])
    return {"event_id": event.event_id, "proof_id": str(value.get("proof_id") or "")}


def get_recovery_runtime_api(store: SQLiteStore | None = None) -> RecoveryRuntimeApiService:
    global _RECOVERY_RUNTIME_API, _RECOVERY_RUNTIME_KEY
    canonical = store or get_store()
    recovery_path = recovery_runtime_path().resolve()
    key = (
        str(Path(canonical.path).resolve()),
        str(recovery_path),
        str(worker_pool_path().resolve()),
        str(backend_registry_path(artifact_root_path()).resolve()),
    )
    with _RECOVERY_RUNTIME_LOCK:
        if _RECOVERY_RUNTIME_API is not None and _RECOVERY_RUNTIME_KEY == key:
            return _RECOVERY_RUNTIME_API
        recovery_store = RecoveryPlanStore(recovery_path)
        recovery_store.initialize()
        components = RecoveryComponentControl()
        feedback = RoutingMemoryFeedback(
            recovery_store,
            sinks=(CallbackRoutingMemorySink(lambda record: _persist_recovery_memory(canonical, record)),),
        )
        task_state = CanonicalTaskStateRuntime(canonical)
        owner_runtime = RecoveryOwnerRuntime(task_state, _recovery_owner_callbacks(canonical))
        route_owners = owner_runtime.route_registry()
        base_routes = LayeredRouteRuntime(recovery_store, route_owners)
        routes = MemoryAwareRouteRuntime(base_routes, feedback, components=components)
        base_context = RecoveryContextRuntime(
            recovery_store,
            feedback,
            snapshot_ports=owner_runtime.snapshot_ports(),
            route_owners=route_owners,
        )
        state_fusion = RecoveryStateFusionRuntime(
            base_context,
            components=components,
        )
        resume_owners = owner_runtime.resume_owners()
        resume = CheckpointResumeBridge(
            recovery_store,
            session_owner=resume_owners.get("session"),
            compact_owner=resume_owners.get("compact"),
            worker_owner=resume_owners.get("worker"),
            graph_owner=resume_owners.get("graph"),
        )
        continuation = RecoveryContinuationRuntime(
            canonical,
            owners=_recovery_continuation_owners(canonical),
            components=components,
            event_sink=lambda event_type, payload: _persist_recovery_event(canonical, event_type, payload),
        )
        verifier = AppliedOutcomeVerifier(
            routes,
            continuation,
            projection_ports=_recovery_projection_ports(task_state),
            components=components,
            proof_sink=lambda proof: _persist_recovery_proof_event(canonical, "applied_outcome_proof", proof),
        )
        semantics = RecoverySemanticRuntime()
        actions = RecoveryActionRuntime(
            recovery_store,
            routes,
            feedback,
            ports=owner_runtime.action_registry(),
            resume_bridge=resume,
            outcome_verifier=verifier,
            semantic_gate=semantics,
            executor_id=f"api-recovery:{os.getpid()}",
        )
        checkpoints = CheckpointCommitRuntime(recovery_store)
        application = RecoveryApplication(
            recovery_store,
            classifier := RecoverySignalClassifier(),
            RecoveryDecisionRuntime(recovery_store),
            actions,
            routes,
            feedback,
            checkpoints,
            DeterministicCommitRuntime(recovery_store),
            context_resolver=state_fusion,
            resume_bridge=resume,
            event_sinks=(CallbackRecoveryEventSink(lambda event_type, payload: _persist_recovery_event(canonical, event_type, payload)),),
            enabled=lambda: not _truthy(os.environ.get("ZYRA_DISABLE_RECOVERY_RUNTIME"), default=False),
        )
        exact = ExactRecoveryRuntime(
            recovery_store,
            checkpoints,
            resume,
            components=components,
        )
        restart = RecoveryRestartRuntime(
            recovery_store,
            actions,
            state_fusion,
            components=components,
            executor_id=f"api-recovery-restart:{os.getpid()}",
        )
        feedback_integration = RecoveryFeedbackIntegrationRuntime(
            recovery_store,
            feedback,
            routes,
            proof_sink=lambda proof: _persist_recovery_proof_event(canonical, "feedback_influence_proof", proof),
        )
        causality = RecoveryCausalTraceRuntime(
            recovery_store,
            event_sink=lambda trace: _persist_recovery_proof_event(canonical, "causal_trace", trace),
        )
        integration = RecoveryIntegrationRuntime(
            recovery_store,
            application,
            RecoveryIngressRuntime(classifier, components=components),
            state_fusion,
            routes,
            exact,
            restart,
            feedback_integration,
            causality,
            components=components,
            event_sinks=(CallbackRecoveryIntegrationEventSink(
                lambda event_type, payload: _persist_recovery_event(canonical, event_type, payload)
            ),),
            enabled=lambda: not _truthy(os.environ.get("ZYRA_DISABLE_RECOVERY_RUNTIME"), default=False),
        )
        _RECOVERY_RUNTIME_API = RecoveryRuntimeApiService(application, integration=integration)
        _RECOVERY_RUNTIME_KEY = key
        return _RECOVERY_RUNTIME_API


def reset_recovery_runtime_api() -> None:
    global _RECOVERY_RUNTIME_API, _RECOVERY_RUNTIME_KEY
    with _RECOVERY_RUNTIME_LOCK:
        _RECOVERY_RUNTIME_API = None
        _RECOVERY_RUNTIME_KEY = None


def _persist_recovery_event(
    store: SQLiteStore,
    event_type: str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    mapped = EventType.RECOVERY_PLANNED if event_type == "recovery_planned" else EventType.TOPOLOGY_ROUTE
    event = EventRecord(
        run_id=str(payload["run_id"]),
        task_id=str(payload["task_id"]),
        node_id=None,
        event_type=mapped,
        payload={"recovery_runtime": {"phase": event_type, **dict(payload)}},
    )
    persist_events(store, [event])
    return {"event_id": event.event_id, "event_type": event.event_type.value}


def _persist_recovery_memory(store: SQLiteStore, record: Any) -> Mapping[str, Any]:
    event = EventRecord(
        run_id=record.run_id,
        task_id=record.task_id,
        event_type=EventType.TOPOLOGY_ROUTE,
        payload={"routing_memory": record.to_dict()},
    )
    persist_events(store, [event])
    state = store.load_task(record.task_id)
    if state is not None:
        snapshot = _memory_fabric(store).refresh_task_memory(
            state,
            store.task_events(record.task_id),
            persist=True,
        )
        memory_count = len(snapshot.records)
    else:
        memory_count = 0
    return {"event_id": event.event_id, "memory_record_count": memory_count}


def codeworker_fault_observation_sink(
    store: SQLiteStore,
    state: Any,
):
    """Bind the real CodeWorker frame boundary to the canonical 07B runtime."""

    def ingest(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        service = get_fault_runtime_api(store)
        response = service.route_post(
            ("tasks", state.task_id, "faults", "runtime-events"),
            payload,
            task_state=state,
            requested_by="typescript-codeworker-watchdog",
        )
        if response is None or int(response.status) >= 400:
            body = {} if response is None else dict(response.body)
            raise RuntimeError(
                str(body.get("message") or body.get("error") or "fault runtime rejected CodeWorker observation")
            )
        projection = service.runtime.writer.task_projection(state.task_id)
        current = dict(state.metadata.get("fault_runtime") or {})
        current.update(dict(projection))
        state.metadata["fault_runtime"] = current
        return dict(response.body)

    return ingest


def memory_index_path() -> Path:
    return runtime_configuration().path("state.memory_index")


def code_index_root_path() -> Path:
    return runtime_configuration().path("state.code_index")


def tool_workspace_path() -> Path:
    """Legacy non-task tooling root.

    CodeWorkerRuntime and BrowserWorker no longer use this path for task work;
    they receive an epoch-fenced task mount from WorkspaceManagerRuntime.
    """

    return runtime_configuration().path("state.workspace")


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
    reset_diff_review_api()
    reset_terminal_api()
    reset_runtime_owner_composition()


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


def reset_code_index_service() -> None:
    global _CODE_INDEX_SERVICE_INSTANCE, _CODE_INDEX_SERVICE_KEY
    with _CODE_INDEX_SERVICE_LOCK:
        _CODE_INDEX_SERVICE_INSTANCE = None
        _CODE_INDEX_SERVICE_KEY = None


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
    return runtime_configuration().path("state.artifacts")


_ARTIFACT_READ_AUDIT = ArtifactReadAudit(maximum=16_384)


def artifact_catalog_service() -> ArtifactCatalogService:
    return ArtifactCatalogService(
        store=LocalArtifactStore(artifact_root_path()),
        audit=_ARTIFACT_READ_AUDIT,
    )


_DIFF_REVIEW_API_LOCK = threading.RLock()
_DIFF_REVIEW_API_INSTANCE: DiffReviewApiService | None = None
_DIFF_REVIEW_API_KEY: tuple[int, str, bool] | None = None


def get_diff_review_api() -> DiffReviewApiService:
    global _DIFF_REVIEW_API_INSTANCE, _DIFF_REVIEW_API_KEY
    manager = get_workspace_manager()
    disabled = os.environ.get("ZYRA_DIFF_REVIEW_DISABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    root = str(artifact_root_path().resolve())
    key = (id(manager), root, disabled)
    with _DIFF_REVIEW_API_LOCK:
        if _DIFF_REVIEW_API_INSTANCE is None or _DIFF_REVIEW_API_KEY != key:
            artifact_store = LocalArtifactStore(artifact_root_path())
            _DIFF_REVIEW_API_INSTANCE = DiffReviewApiService(
                artifact_service=ArtifactCatalogService(
                    store=artifact_store,
                    audit=_ARTIFACT_READ_AUDIT,
                ),
                workspace_manager=manager,
                permission_port=get_mcp_runtime(),
                registry=DiffReviewRegistry(maximum_sessions=128),
                artifact_store=artifact_store,
                enabled=not disabled,
            )
            _DIFF_REVIEW_API_KEY = key
        return _DIFF_REVIEW_API_INSTANCE


def reset_diff_review_api() -> None:
    global _DIFF_REVIEW_API_INSTANCE, _DIFF_REVIEW_API_KEY
    with _DIFF_REVIEW_API_LOCK:
        if _DIFF_REVIEW_API_INSTANCE is not None:
            _DIFF_REVIEW_API_INSTANCE.registry.clear()
        _DIFF_REVIEW_API_INSTANCE = None
        _DIFF_REVIEW_API_KEY = None


_TERMINAL_API_LOCK = threading.RLock()
_TERMINAL_API_INSTANCE: TerminalApiService | None = None
_TERMINAL_API_KEY: tuple[int, str, str, bool] | None = None


def terminal_state_path() -> Path:
    return runtime_configuration().path("state.terminal")


def _terminal_workspace(request: TerminalCreateRequest) -> tuple[str, int, Path]:
    manager = get_workspace_manager()
    access = manager.acquire_for_worker(
        task_id=request.task_id,
        session_id="",
        worker_id=request.worker_id,
    )
    binding = manager.store.require_binding(access.workspace_id)
    root = manager.internal_task_root(access).resolve()
    logical = request.cwd.strip() or "."
    selected = Path(logical)
    if selected.is_absolute():
        raise TerminalError(
            "terminal_cwd_absolute",
            "Terminal cwd must be relative to the task workspace.",
            status=400,
        )
    target = (root / selected).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise TerminalError(
            "terminal_cwd_escape",
            "Terminal cwd escapes the task workspace.",
            status=403,
        ) from error
    if not target.is_dir():
        raise TerminalError(
            "terminal_cwd_not_found",
            "Terminal cwd is not an existing workspace directory.",
            status=404,
        )
    return binding.workspace_id, binding.binding_revision, target


def _terminal_permission(
    action: str,
    request: TerminalCreateRequest | Any,
) -> TerminalPermission:
    tool_call_id = str(request.tool_call_id or "")
    material = {
        "run_id": request.run_id,
        "task_id": request.task_id,
        "session_id": request.session_id,
        "session_revision": 0,
        "worker_request_id": (
            str(getattr(request, "command_id", "") or "") or tool_call_id
        ),
        "tool_call_id": tool_call_id,
        "tool_name": "terminal.pty",
        "namespace": "builtin",
        "server_id": "",
        "operation": action,
        "workspace_root": ".",
        "arguments": {
            "action": action,
            "terminal_id": str(getattr(request, "terminal_id", "") or ""),
            "command": (
                str(getattr(request, "command", "") or "")[:4_096]
                if action == "create"
                else ""
            ),
            "cwd": str(getattr(request, "cwd", "") or ""),
            "rows": int(getattr(request, "rows", 0) or 0),
            "cols": int(getattr(request, "cols", 0) or 0),
            "reason": str(getattr(request, "reason", "") or "")[:1_024],
        },
        "metadata": {
            "canonical_terminal_owner": (
                "zyra_workers.terminal.TerminalSessionRegistry"
            ),
            "canonical_permission_owner": "typescript.PermissionCoordinator",
            "python_decision_fallback": False,
            "span_id": request.span_id,
            "actor_id": request.actor_id,
            "competition_mode": request.competition_mode,
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": action in {"create", "input", "kill"},
                "openWorldHint": False,
                "idempotentHint": action == "kill",
            },
        },
        "await_approval_delivery": True,
    }
    permit_id = str(getattr(request, "permission_permit_id", "") or "")
    if permit_id:
        material["permit_id"] = permit_id
    port = get_mcp_runtime()
    claimed = dict(port.permission_claim(material))
    response = (
        claimed
        if claimed.get("claimed") is True
        else dict(port.permission_enforce(material))
    )
    decision = (
        dict(response.get("decision") or {})
        if isinstance(response.get("decision"), Mapping)
        else {}
    )
    owner = str(
        response.get("canonical_owner")
        or response.get("canonicalOwner")
        or decision.get("canonical_owner")
        or decision.get("canonicalOwner")
        or ""
    )
    if "typescript" not in owner.casefold():
        raise TerminalError(
            "terminal_permission_owner_invalid",
            "Terminal permission response is not TypeScript-owned.",
            status=503,
        )
    effect = str(decision.get("effect") or response.get("effect") or "").casefold()
    if effect not in {"allow", "ask", "deny"}:
        raise TerminalError(
            "terminal_permission_effect_invalid",
            "Terminal permission owner returned an invalid effect.",
            status=503,
        )
    return TerminalPermission(
        effect=effect,
        decision_id=str(
            decision.get("decision_id")
            or decision.get("decisionId")
            or new_id("terminal_permission")
        ),
        request_id=str(
            decision.get("request_id")
            or decision.get("requestId")
            or response.get("request_id")
            or ""
        ),
        permit_id=str(
            response.get("permit_id")
            or decision.get("permit_id")
            or permit_id
            or ""
        ),
        reason_code=str(
            decision.get("reason_code")
            or decision.get("reasonCode")
            or f"permission.{effect}"
        ),
        reason=str(
            decision.get("reason")
            or response.get("message")
            or f"Terminal {action} permission resolved as {effect}."
        ),
    )


def _terminal_event(payload: Mapping[str, Any]) -> str:
    binding = payload.get("binding")
    if not isinstance(binding, Mapping):
        raise TerminalError(
            "terminal_event_binding_invalid",
            "Terminal event has no canonical binding.",
            status=500,
        )
    event_name = str(payload.get("event_type") or "")
    event_type = (
        EventType.TERMINAL_OUTPUT
        if event_name == "terminal.output"
        else EventType.TERMINAL_CONTROL
        if event_name in {"terminal.input", "terminal.resize", "terminal.killed"}
        else EventType.TERMINAL_SESSION_LIFECYCLE
    )
    event = EventRecord(
        run_id=str(binding.get("run_id") or ""),
        task_id=str(binding.get("task_id") or ""),
        event_type=event_type,
        payload=dict(payload),
    )
    persist_events(get_store(), [event])
    return event.event_id


def _terminal_spill_sink(binding: TerminalBinding) -> Any:
    artifact_store = LocalArtifactStore(artifact_root_path())

    def spill(
        data: bytes,
        first_cursor: int,
        next_cursor: int,
        binary: bool,
        redacted: bool,
    ) -> TerminalSpill:
        artifact = artifact_store.write_bytes(
            run_id=binding.run_id,
            task_id=binding.task_id,
            content=data,
            title=(
                f"Terminal {binding.terminal_id} output "
                f"{first_cursor}-{next_cursor}"
            ),
            kind=ArtifactKind.TRACE,
            extension=".bin" if binary else ".log",
            metadata={
                "contract": "zyra.terminal-spill.v1",
                "terminal_id": binding.terminal_id,
                "first_cursor": first_cursor,
                "next_cursor": next_cursor,
                "binary": binary,
                "redacted": redacted,
                "producer_span_id": binding.span_id,
                "producer_tool_call_id": binding.tool_call_id,
                "producer_worker_id": binding.worker_id,
                "security_label": "internal",
            },
        )
        store = get_store()
        state = store.load_task(binding.task_id)
        if state is None or state.run_id != binding.run_id:
            raise TerminalError(
                "terminal_spill_task_missing",
                "Terminal spill cannot attach to its canonical task.",
                status=500,
            )
        _attach_artifacts(state, [artifact])
        store.save_checkpoint(state)
        return TerminalSpill(
            artifact_id=artifact.artifact_id,
            revision=str(artifact.metadata.get("revision") or ""),
            media_type=str(
                artifact.metadata.get("media_type")
                or ("application/octet-stream" if binary else "text/plain")
            ),
            sha256=str(artifact.metadata.get("sha256") or ""),
            byte_length=next_cursor - first_cursor,
            first_cursor=first_cursor,
            next_cursor=next_cursor,
            binary=binary,
            redacted=redacted,
        )

    return spill


def get_terminal_api() -> TerminalApiService:
    global _TERMINAL_API_INSTANCE, _TERMINAL_API_KEY
    manager = get_workspace_manager()
    state_path = terminal_state_path().resolve()
    disabled = os.environ.get("ZYRA_TERMINAL_DISABLED", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    key = (
        id(manager),
        str(state_path),
        str(artifact_root_path().resolve()),
        disabled,
    )
    with _TERMINAL_API_LOCK:
        if _TERMINAL_API_INSTANCE is None or _TERMINAL_API_KEY != key:
            if _TERMINAL_API_INSTANCE is not None:
                _TERMINAL_API_INSTANCE.registry.shutdown()
            registry = TerminalSessionRegistry(
                state_store=TerminalStateStore(state_path),
                workspace_resolver=_terminal_workspace,
                permission_port=_terminal_permission,
                event_sink=_terminal_event,
                spill_sink_factory=_terminal_spill_sink,
                ticket_authority=TerminalTicketAuthority.random(
                    ttl_seconds=float(
                        os.environ.get("ZYRA_TERMINAL_TICKET_TTL", "20")
                    )
                ),
                maximum_sessions=max(
                    1,
                    min(
                        1_024,
                        int(os.environ.get("ZYRA_TERMINAL_MAX_SESSIONS", "128")),
                    ),
                ),
                enabled=not disabled,
            )
            _TERMINAL_API_INSTANCE = TerminalApiService(registry)
            _TERMINAL_API_KEY = key
        return _TERMINAL_API_INSTANCE


def reset_terminal_api() -> None:
    global _TERMINAL_API_INSTANCE, _TERMINAL_API_KEY
    with _TERMINAL_API_LOCK:
        if _TERMINAL_API_INSTANCE is not None:
            _TERMINAL_API_INSTANCE.registry.shutdown()
        _TERMINAL_API_INSTANCE = None
        _TERMINAL_API_KEY = None


_M1_HARDENING_API_LOCK = threading.RLock()
_M1_HARDENING_API_INSTANCE: M1HardeningApi | None = None
_M1_HARDENING_API_KEY: tuple[str, str] | None = None


def m1_owner_probe_reset_registry() -> RuntimeResetRegistry:
    """Bind owner-disable probes to the API composition roots they invalidate."""

    registry = RuntimeResetRegistry()
    stateless = lambda: None
    callbacks = {
        "codeworker": stateless,
        "mcp": reset_mcp_runtime,
        "workspace": reset_workspace_manager,
        "sandbox-gateway": stateless,
        "runtime-event-spine": reset_runtime_event_spine_bridge,
        "provider-control-plane": reset_provider_control_client,
        "memory": reset_memory_curator_runtime,
        "code-index": reset_code_index_service,
        "skill-memory": stateless,
        "worker-pool": reset_worker_pool_api,
        "fault-runtime": reset_fault_runtime_api,
        "recovery": reset_recovery_runtime_api,
        "scheduler": reset_recovery_runtime_api,
        "graph-custody": reset_worker_pool_api,
    }
    for component, callback in callbacks.items():
        registry.register(component, callback)
    return registry


def get_m1_hardening_api() -> M1HardeningApi:
    global _M1_HARDENING_API_INSTANCE, _M1_HARDENING_API_KEY
    hardening_root = (artifact_root_path() / "m1-hardening").resolve()
    key = (str(PROJECT_ROOT.resolve()), str(hardening_root))
    with _M1_HARDENING_API_LOCK:
        if _M1_HARDENING_API_INSTANCE is None or _M1_HARDENING_API_KEY != key:
            service = M1HardeningService(
                PROJECT_ROOT,
                source_workspace=PROJECT_ROOT / "provenance",
                artifact_root=hardening_root,
            )
            _M1_HARDENING_API_INSTANCE = M1HardeningApi(
                service,
                default_baseline="8065bac109a3bed9ba01e0e92392fec4d05bfca3",
                integration_service=M1IntegrationService(
                    PROJECT_ROOT,
                    source_workspace=PROJECT_ROOT / "provenance",
                    artifact_root=hardening_root,
                    foundation_service=service,
                    reset_registry=m1_owner_probe_reset_registry(),
                ),
            )
            _M1_HARDENING_API_KEY = key
        return _M1_HARDENING_API_INSTANCE


def reset_m1_hardening_api() -> None:
    global _M1_HARDENING_API_INSTANCE, _M1_HARDENING_API_KEY
    with _M1_HARDENING_API_LOCK:
        if _M1_HARDENING_API_INSTANCE is not None:
            _M1_HARDENING_API_INSTANCE.service.cancel()
        _M1_HARDENING_API_INSTANCE = None
        _M1_HARDENING_API_KEY = None


def _hardening_task_payload(store: SQLiteStore, task_id: str) -> Mapping[str, Any] | None:
    state = store.load_task(task_id)
    if state is None:
        return None
    value = to_jsonable(state)
    return value if isinstance(value, Mapping) else None


def permission_store_path() -> Path:
    return runtime_configuration().path("state.permission_compat")


def permission_state_path() -> Path:
    """Return the sole authoritative permission state path.

    ``tmp/permissions.json`` is retained only for the pre-M1 compatibility
    projection used by unrelated historical graph code.  Every interactive
    permission operation and every execution guard in this API shares this
    state owner.
    """

    return runtime_configuration().path("state.permission")


def mcp_state_path() -> Path:
    return runtime_configuration().path("state.mcp")


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
_E02_MCP_MUTATIONS = frozenset(
    {"enable", "disable", "reconnect", "refresh", "auth-refresh", "elicit"}
)
_E02_SKILL_MUTATIONS = frozenset({"update", "invoke"})


def _e02_command_route(text: str) -> tuple[str, str] | None:
    """Classify only the command names whose parser and dispatch moved to TS."""

    stripped = str(text or "").strip()
    if not stripped.startswith("/"):
        return None
    name = stripped[1:].partition(" ")[0].strip().lower()
    raw_arguments = stripped[1:].partition(" ")[2].strip()
    action = raw_arguments.partition(" ")[0].strip().lower()
    if name == "mcp" and action in _E02_MCP_MUTATIONS:
        return name, "execute"
    if name == "skills" and action in _E02_SKILL_MUTATIONS:
        return name, "execute"
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
_LOOPX_CONTROL_RUNTIME: LoopXControlRuntime | None = None
_LOOPX_CONTROL_KEY: tuple[str, str, str] | None = None
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
    gateway_constraints: Mapping[str, Any] | None = None,
) -> tuple[Any, BrowserWorkerRuntime]:
    """Bind API requests to one registry runtime and its shared services."""

    runtime = get_browser_runtime()
    workspace_edit_port = None
    workspace_gateway_required = False
    if task_id:
        manager = get_workspace_manager()
        # Browser session identity belongs to BrowserRuntimeRegistry, not to
        # WorkspaceBindingStore.  A task workspace is created against the
        # task/query session and must remain shared across Chrome reconnects,
        # popups, replacement process epochs, and viewer-selected browser
        # sessions.  Passing the browser session here would incorrectly turn
        # it into a second workspace owner and reject an otherwise valid task
        # with workspace_not_found.
        workspace_access = manager.acquire_for_worker(
            task_id=task_id,
            session_id="",
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
    gateway_constraints = dict(gateway_constraints or {})
    allowed_schemes = {
        str(item).strip().casefold()
        for item in gateway_constraints.get("allowed_schemes", ())
        if str(item).strip()
    }
    allowed_hosts = tuple(
        str(item).strip()
        for item in gateway_constraints.get("allowed_domains", ())
        if str(item).strip()
    )
    worker = BrowserWorkerRuntime(
        project_root=PROJECT_ROOT,
        workspace_root=workspace_root,
        artifact_root=artifact_root_path(),
        permission_state_path=permission_state_path(),
        browser_session_runtime=runtime,
        browser_runtime_registry=_BROWSER_RUNTIME_REGISTRY,
        workspace_edit_port=workspace_edit_port,
        workspace_gateway_required=workspace_gateway_required,
        sandbox_gateway_services={
            "sandbox_gateway_allowed_hosts": allowed_hosts,
            "sandbox_gateway_allow_public_http": bool(
                "http" in allowed_schemes and allowed_hosts
            ),
            "sandbox_gateway_allow_private_network": bool(
                gateway_constraints.get("allow_private_network") and allowed_hosts
            ),
            "sandbox_gateway_allow_loopback_network": bool(
                gateway_constraints.get("allow_loopback_network") and allowed_hosts
            ),
        },
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


def _task_is_sealed_browser_viewer_control(
    state: Any,
    payload: Mapping[str, Any],
    control: Mapping[str, Any],
) -> bool:
    return _task_is_sealed_control(state, payload, control)


def _task_is_sealed_control(
    state: Any,
    payload: Mapping[str, Any],
    control: Mapping[str, Any] | None = None,
) -> bool:
    metadata = state.metadata if isinstance(getattr(state, "metadata", None), dict) else {}
    control = control or {}
    mode = str(
        control.get("competition_mode")
        or payload.get("competition_mode")
        or metadata.get("competition_mode")
        or metadata.get("execution_mode")
        or ""
    ).strip().casefold()
    return bool(
        control.get("sealed")
        or payload.get("sealed")
        or metadata.get("sealed")
        or metadata.get("sealed_autonomous")
        or metadata.get("formal_benchmark")
        or "sealed" in mode
    )


def _task_control_competition_mode(
    state: Any,
    payload: Mapping[str, Any],
) -> str:
    if _task_is_sealed_control(state, payload):
        return "sealed_autonomous"
    metadata = state.metadata if isinstance(getattr(state, "metadata", None), dict) else {}
    return str(
        payload.get("competition_mode")
        or metadata.get("competition_mode")
        or metadata.get("execution_mode")
        or "interactive"
    )


def _browser_viewer_control_request(
    state: Any,
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    raw = payload.get("browser_viewer_control")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("browser_viewer_control must be an object")
    control = dict(raw)
    if str(control.get("schema") or "") != "zyra.browser-viewer.control.v1":
        raise ValueError("browser_viewer_control schema is unsupported")
    action = str(control.get("action") or "").strip().casefold().replace("_", "-")
    if action not in {"navigate", "stop", "retry", "inspect"}:
        raise ValueError("browser_viewer_control action is unsupported")
    command_id = str(control.get("command_id") or "").strip()
    request_id = str(control.get("request_id") or "").strip()
    actor_id = str(control.get("actor_id") or "zyra-web-browser").strip()
    reason = str(control.get("reason") or "").strip()
    browser_session_id = str(
        control.get("browser_session_id")
        or payload.get("session_id")
        or state.metadata.get("query_session_id")
        or f"task:{state.task_id}"
    ).strip()
    if not command_id or not request_id or not actor_id or not reason or not browser_session_id:
        raise ValueError("browser_viewer_control identity and reason are required")
    if any(len(value) > 512 for value in (command_id, request_id, actor_id, browser_session_id)):
        raise ValueError("browser_viewer_control identity exceeds 512 characters")
    if len(reason.encode("utf-8")) > 8 * 1024:
        raise ValueError("browser_viewer_control reason exceeds 8 KiB")
    url = str(control.get("url") or "").strip()
    if action == "navigate":
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("browser_viewer_control navigate requires an http or https URL")
        if parsed.username or parsed.password:
            raise ValueError("browser_viewer_control URL credentials are forbidden")
    retry_action = control.get("retry_action")
    if action == "retry":
        if not isinstance(retry_action, Mapping):
            raise ValueError("browser_viewer_control retry requires one prior action")
        retry_name = str(retry_action.get("action") or "").strip()
        retry_arguments = retry_action.get("arguments")
        if not retry_name or not isinstance(retry_arguments, Mapping):
            raise ValueError("browser_viewer_control retry action is invalid")
        if len(retry_arguments) > 128:
            raise ValueError("browser_viewer_control retry arguments exceed 128 entries")
        retry_action = {
            "action": retry_name,
            "arguments": dict(retry_arguments),
            "retry_of_action_id": str(retry_action.get("retry_of_action_id") or "")[:512],
            "expected_argument_digest": str(
                retry_action.get("expected_argument_digest") or ""
            )[:512],
        }
    expected_generation = control.get("expected_generation")
    if expected_generation not in (None, ""):
        expected_generation = int(expected_generation)
        if expected_generation < 0:
            raise ValueError("browser_viewer_control expected_generation cannot be negative")
    else:
        expected_generation = None
    expected_task_revision = control.get("expected_task_revision")
    if expected_task_revision not in (None, ""):
        expected_task_revision = int(expected_task_revision)
        if expected_task_revision < 0:
            raise ValueError("browser_viewer_control expected_task_revision cannot be negative")
    else:
        expected_task_revision = None
    normalized = {
        "schema": "zyra.browser-viewer.control.v1",
        "action": action,
        "command_id": command_id,
        "request_id": request_id,
        "actor_id": actor_id,
        "reason": reason,
        "browser_session_id": browser_session_id,
        "worker_request_id": str(control.get("worker_request_id") or "").strip()[:512],
        "action_id": str(control.get("action_id") or "").strip()[:512],
        "url": url,
        "retry_action": retry_action,
        "expected_generation": expected_generation,
        "expected_task_revision": expected_task_revision,
        "sealed": _task_is_sealed_browser_viewer_control(state, payload, control),
    }
    normalized["fingerprint"] = "sha256:" + hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return normalized


def _browser_viewer_execution_constraints(
    control: Mapping[str, Any],
) -> dict[str, Any]:
    action = str(control["action"])
    session_id = str(control["browser_session_id"])
    constraints: dict[str, Any] = {
        "session_id": session_id,
        "canonical_session_id": session_id,
        "browser_viewer_control_id": str(control["command_id"]),
        "browser_viewer_control_request_id": str(control["request_id"]),
        "browser_viewer_control_action": action,
        "browser_viewer_control_actor_id": str(control["actor_id"]),
        "browser_viewer_control_reason": str(control["reason"]),
        "browser_viewer_control_fingerprint": str(control["fingerprint"]),
    }
    if control.get("expected_generation") is not None:
        constraints["expected_generation"] = int(control["expected_generation"])
    if action == "navigate":
        constraints["browser_plan"] = [
            {
                "action": "open_url",
                "arguments": {"url": str(control["url"])},
            }
        ]
    elif action == "retry":
        retry = control.get("retry_action")
        if not isinstance(retry, Mapping):
            raise ValueError("browser viewer retry payload disappeared after admission")
        constraints["browser_plan"] = [
            {
                "action": str(retry["action"]),
                "arguments": dict(retry["arguments"]),
                "metadata": {
                    "retry_of_action_id": str(retry.get("retry_of_action_id") or ""),
                    "expected_argument_digest": str(
                        retry.get("expected_argument_digest") or ""
                    ),
                    "bounded_retry": True,
                },
            }
        ]
    elif action == "stop":
        constraints["browser_lifecycle_command"] = "stop"
        constraints["browser_lifecycle_reason"] = str(control["reason"])
    elif action == "inspect":
        constraints["browser_lifecycle_command"] = "diagnose"
    return constraints


def _browser_viewer_receipt(
    control: Mapping[str, Any],
    *,
    status: str,
    event_ids: tuple[str, ...] = (),
    mutation_ids: tuple[str, ...] = (),
    artifact_ids: tuple[str, ...] = (),
    worker_request_id: str = "",
    lifecycle_receipt_id: str = "",
    permission_request_id: str = "",
    error_code: str = "",
    error_message: str = "",
    intervention_counted: bool = False,
    human_intervention_count: int = 0,
    manual_mutation_applied: bool = False,
    approval_wait_entered: bool = False,
    automatic_recovery_action: str = "",
    replayed: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "zyra.browser-viewer.control.v1",
        "action": str(control["action"]),
        "command_id": str(control["command_id"]),
        "request_id": str(control["request_id"]),
        "idempotency_fingerprint": str(control["fingerprint"]),
        "status": status,
        "task_id": str(control.get("task_id") or ""),
        "run_id": str(control.get("run_id") or ""),
        "browser_session_id": str(control["browser_session_id"]),
        "worker_request_id": worker_request_id or str(control.get("worker_request_id") or ""),
        "action_id": str(control.get("action_id") or ""),
        "event_ids": list(event_ids),
        "mutation_ids": list(mutation_ids),
        "artifact_ids": list(artifact_ids),
        "lifecycle_receipt_id": lifecycle_receipt_id,
        "permission_request_id": permission_request_id,
        "error_code": error_code,
        "error_message": error_message,
        "retryable": status in {"timed_out", "stale"},
        "sealed": bool(control.get("sealed")),
        "intervention_counted": intervention_counted,
        "human_intervention_count": max(0, int(human_intervention_count)),
        "manual_mutation_applied": manual_mutation_applied,
        "approval_wait_entered": approval_wait_entered,
        "automatic_recovery_action": automatic_recovery_action,
        "replayed": replayed,
        "completed_at": now_iso(),
    }


def _tool_checkpoint_reader(store: SQLiteStore) -> Callable[[str], dict[str, Any] | None]:
    """Expose canonical task checkpoints to physical CodeWorker tools."""

    def read(task_id: str) -> dict[str, Any] | None:
        state = store.load_task(task_id)
        return to_jsonable(state) if state is not None else None

    return read


def _browser_pending_permission_metadata(run_result: Any) -> dict[str, str]:
    """Recover a pending browser permission from the physical action result.

    BrowserWorker normally projects these fields from its productized plan
    coordinator.  A permission request can also originate at the final
    browser-session effect boundary, so the API must recognize that canonical
    E02 request instead of caching the recoverable result as a terminal 409.
    """

    metadata = dict(getattr(run_result.worker_result, "metadata", {}) or {})
    if str(metadata.get("browser_permission_pending") or "").lower() == "true":
        return {
            "browser_permission_pending": "true",
            "browser_pending_checkpoint_id": str(
                metadata.get("browser_pending_checkpoint_id") or ""
            ),
            "browser_pending_permission_request_id": str(
                metadata.get("browser_pending_permission_request_id") or ""
            ),
            "browser_pending_permission_tool_use_id": str(
                metadata.get("browser_pending_permission_tool_use_id") or ""
            ),
        }

    for event in getattr(run_result, "event_records", ()):
        payload = getattr(event, "payload", {})
        if not isinstance(payload, Mapping):
            continue
        action = payload.get("browser_action")
        result = payload.get("browser_result")
        if not isinstance(result, Mapping) and isinstance(action, Mapping):
            result = action.get("browser_result")
        if not isinstance(result, Mapping) or result.get("error") != "permission_required":
            continue
        output = result.get("output")
        decision = output.get("permission_decision") if isinstance(output, Mapping) else None
        pending = output.get("pending_request") if isinstance(output, Mapping) else None
        if not isinstance(pending, Mapping) and isinstance(decision, Mapping):
            pending = decision.get("pending_request")
        if not isinstance(pending, Mapping) or pending.get("status") != "pending":
            continue
        return {
            "browser_permission_pending": "true",
            "browser_pending_checkpoint_id": "",
            "browser_pending_permission_request_id": str(pending.get("request_id") or ""),
            "browser_pending_permission_tool_use_id": str(pending.get("tool_use_id") or ""),
        }
    return {}


def reset_browser_runtime(*, stop: bool = True) -> None:
    """Explicit test/development reset; never silently replace a live owner."""

    with _BROWSER_RUNTIME_LOCK:
        _BROWSER_RUNTIME_REGISTRY.shutdown(force=stop)


def control_state_path() -> Path:
    return runtime_configuration().path("state.control")


def subagent_state_path() -> Path:
    return runtime_configuration().path("state.subagents")


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


def get_loopx_control_runtime() -> LoopXControlRuntime:
    global _LOOPX_CONTROL_RUNTIME, _LOOPX_CONTROL_KEY
    workspace = tool_workspace_path().resolve()
    database = Path(sqlite_path()).resolve()
    artifacts = artifact_root_path().resolve()
    key = (str(workspace), str(database), str(artifacts))
    with _CONTROL_RUNTIME_LOCK:
        if _LOOPX_CONTROL_RUNTIME is None or _LOOPX_CONTROL_KEY != key:
            runtime_receipt = LoopXRuntimeResolver(PROJECT_ROOT).receipt(
                workspace
            )
            outbox = LoopXOutbox(workspace_root=workspace)
            runtime = LoopXRuntimeStateAdapter(
                workspace_root=workspace,
                install_receipt=runtime_receipt,
            )
            _LOOPX_CONTROL_RUNTIME = LoopXControlRuntime(
                workspace_root=workspace,
                outbox=outbox,
                runtime=runtime,
                dispatcher=LoopXDispatcher(
                    outbox=outbox,
                    single_writer=LoopXSingleWriter(
                        workspace_root=workspace
                    ),
                    runtime=runtime,
                    observability=LoopXBridgeObservability(
                        artifact_store=LocalArtifactStore(artifacts),
                        event_spine=get_runtime_event_spine_bridge(),
                    ),
                ),
            )
            _LOOPX_CONTROL_KEY = key
        return _LOOPX_CONTROL_RUNTIME


def prepare_phase2_loopx_pre_control(
    state: TaskState,
    *,
    causation_id: str,
) -> dict[str, Any]:
    """Commit and execute the shared LoopX control input before topology."""

    existing = state.metadata.get("phase2_loopx_pre_control")
    if isinstance(existing, Mapping):
        replay = dict(existing)
        replay_unsigned = dict(replay)
        replay_digest = str(replay_unsigned.pop("receipt_digest", ""))
        replay_checks = replay.get("checks")
        replay_permission = dict(replay.get("permission_receipt") or {})
        replay_permission_unsigned = dict(replay_permission)
        replay_permission_digest = str(
            replay_permission_unsigned.pop("receipt_digest", "")
        )
        try:
            replay_permission_fresh = datetime.now(UTC) <= datetime.fromisoformat(
                str(replay_permission.get("valid_until") or "").replace(
                    "Z",
                    "+00:00",
                )
            ).astimezone(UTC)
        except (TypeError, ValueError):
            replay_permission_fresh = False
        replay_continuation = dict(replay.get("continuation") or {})
        replay_commit = dict(replay.get("canonical_commit") or {})
        if (
            replay.get("schema")
            == "zyra.phase2-production-loopx-pre-control/v1"
            and replay.get("run_id") == state.run_id
            and replay.get("task_id") == state.task_id
            and replay_digest
            and replay_digest == canonical_digest(replay_unsigned)
            and isinstance(replay_checks, Mapping)
            and bool(replay_checks)
            and all(item is True for item in replay_checks.values())
            and replay_permission_digest
            and replay_permission_digest
            == canonical_digest(replay_permission_unsigned)
            and replay_permission.get("effect") == "allow"
            and "typescript"
            in str(
                replay_permission.get("canonical_owner") or ""
            ).casefold()
            and {"graph.write", "worker.dispatch"}.issubset(
                set(replay_permission.get("allowed_permissions") or ())
            )
            and replay_permission_fresh
            and replay_continuation.get("allowed") is True
            and str(
                dict(replay_commit.get("receipt") or {}).get("status") or ""
            )
            in {"committed", "rebased", "replayed"}
            and replay.get("canonical_commit_digest")
            == canonical_digest(replay_commit)
        ):
            return replay

    permission_receipt = dict(
        _phase2_permission_decision(
            state,
            ("graph.write", "worker.dispatch"),
            SimpleNamespace(event_id=causation_id),
        )
    )
    permission_unsigned = dict(permission_receipt)
    permission_digest = str(
        permission_unsigned.pop("receipt_digest", "")
    )
    permission_allowed = bool(
        permission_digest
        and permission_digest == canonical_digest(permission_unsigned)
        and permission_receipt.get("effect") == "allow"
        and "typescript"
        in str(permission_receipt.get("canonical_owner") or "").casefold()
        and {"graph.write", "worker.dispatch"}.issubset(
            set(permission_receipt.get("allowed_permissions") or ())
        )
    )
    if not permission_allowed:
        raise RuntimeError(
            "canonical permission owner denied LoopX pre-control"
        )
    pool_api = get_worker_pool_api()
    graph_id_value = pool_api.ensure_task_graph(state)
    branch = pool_api.graph_custody.branch(
        graph_id_value,
        branch_id=f"sealed-loopx-pre-control:{state.task_id}",
        actor_id="LoopXControlRuntime",
        causation_id=causation_id,
        idempotency_key=(
            f"sealed:{state.run_id}:{state.task_id}:loopx:pre-control"
        ),
        metadata={
            "sealed_pre_control": True,
            "private_payload_excluded": True,
        },
    )
    goal_id = f"goal_sealed_{state.task_id}"
    branch.set_metadata(
        "loopx_pre_control",
        {
            "schema": "zyra.loopx-pre-control-ref/v1",
            "goal_id": goal_id,
            "continuation_required": True,
            "private_payload_excluded": True,
        },
    )
    committed = pool_api.graph_custody.commit(branch.build())
    if not committed.receipt.committed:
        raise RuntimeError("GraphStateCustody rejected LoopX pre-control commit")
    validation = {
        "validation_passed": True,
        "permission_allowed": permission_allowed,
        "lease_valid": True,
        "budget_allowed": True,
        "validation_receipt_id": committed.receipt.commit_id,
        "permission_receipt_id": permission_receipt.get("decision_id"),
        "lease_receipt_id": "control-plane:no-worker-dispatch",
        "budget_receipt_id": "loopx-private-quota:not-zyra-budget",
    }
    control = get_loopx_control_runtime()
    common = {
        "run_id": state.run_id,
        "task_id": state.task_id,
        "canonical_commit": committed,
        "validation": validation,
        "causation_id": causation_id,
    }
    connected = control.mutate(
        action="connect",
        payload={
            "goal_id": goal_id,
            "todo_id": "todo_sealed_primary",
            "todo_title": "complete the verified sealed delivery",
            "objective": str(state.user_goal or "sealed delivery"),
            "limit_slots": 1,
            "requirement_revision": str(
                state.metadata.get("requirement_revision") or "r1"
            ),
        },
        idempotency_key=(
            f"sealed:{state.run_id}:{state.task_id}:loopx:connect:1"
        ),
        **common,
    )
    claimed = control.mutate(
        action="claim",
        payload={
            "goal_id": goal_id,
            "todo_id": "todo_sealed_primary",
            "claimant": "sealed-controller-a",
        },
        idempotency_key=(
            f"sealed:{state.run_id}:{state.task_id}:loopx:claim:2"
        ),
        **common,
    )
    snapshot = control.snapshot(
        run_id=state.run_id,
        task_id=state.task_id,
        goal_id=goal_id,
    )
    continuation = dict(snapshot.get("continuation") or {})
    checks = {
        "canonical_commit": committed.receipt.committed is True,
        "canonical_permission_owner": (
            "typescript"
            in str(
                permission_receipt.get("canonical_owner") or ""
            ).casefold()
        ),
        "permission_receipt_digest": (
            permission_digest == canonical_digest(permission_unsigned)
        ),
        "permission_allowed": permission_allowed,
        "connected": dict(connected.get("state") or {}).get("connected")
        is True,
        "claim_applied": dict(claimed.get("receipt") or {}).get("status")
        == "applied",
        "continuation_allowed": continuation.get("allowed") is True,
    }
    if not all(checks.values()):
        raise RuntimeError(f"LoopX pre-control did not admit production: {checks}")
    value = {
        "schema": "zyra.phase2-production-loopx-pre-control/v1",
        "run_id": state.run_id,
        "task_id": state.task_id,
        "goal_id": goal_id,
        "canonical_commit_id": committed.receipt.commit_id,
        "canonical_commit": committed.to_dict(),
        "canonical_commit_digest": canonical_digest(committed.to_dict()),
        "validation": validation,
        "permission_receipt": permission_receipt,
        "continuation": continuation,
        "sync_cursor": dict(snapshot.get("sync") or {}).get("cursor"),
        "results": {
            "connect": connected,
            "claim": claimed,
        },
        "checks": checks,
    }
    value["receipt_digest"] = canonical_digest(value)
    state.metadata["phase2_loopx_pre_control"] = dict(value)
    get_store().save_checkpoint(state)
    return value


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
    global _CONTROL_DISPATCHER, _CONTROL_RUNTIME_KEY, _CONTROL_REGISTRY, _CONTROL_SOURCE_COORDINATOR, _STRUCTURED_CONTROL_HUB, _LOOPX_CONTROL_RUNTIME, _LOOPX_CONTROL_KEY
    with _CONTROL_RUNTIME_LOCK:
        _CONTROL_DISPATCHER = None
        _CONTROL_RUNTIME_KEY = None
        _CONTROL_REGISTRY = None
        _CONTROL_SOURCE_COORDINATOR = None
        _STRUCTURED_CONTROL_HUB = None
        _LOOPX_CONTROL_RUNTIME = None
        _LOOPX_CONTROL_KEY = None


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
    canonical_agent_parent_session_id: str = "",
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
        parent_session_id=canonical_agent_parent_session_id or parent_session_id,
    )
    owner_authorization = arguments.get("_canonical_control_authorization")
    public_arguments = {
        key: value
        for key, value in arguments.items()
        if key != "_canonical_control_authorization"
    }
    bounded_arguments = {
        **public_arguments,
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
    if tool_name in {
        "agent_cancel",
        "agent_kill",
        "agent_message",
        "agent_resume",
        "agent_status",
        "agent_list",
    }:
        # Control requests must execute a fresh bounded QueryEngine turn while
        # E03 independently restores its durable SubagentTaskStore snapshot.
        # Reusing the parent's terminal query checkpoint would report a
        # successful zero-turn session without ever reaching the E03 owner.
        constraints["restored_runtime_state"] = {}
        constraints["disable_incremental_checkpoint_restore"] = True
        constraints["permission_transport_queue_enabled"] = False
        constraints["disable_retrieval_context"] = True
        constraints["deferTypescriptAgentBackgroundDrain"] = True
    if canonical_agent_parent_session_id:
        # The bounded QueryEngine request has its own checkpoint/CAS identity,
        # while E03 must restore and mutate the original parent session's
        # canonical SubagentTaskStore partition.
        constraints["typescriptAgentParentSessionId"] = (
            canonical_agent_parent_session_id
        )
    if arguments.get("disable_retrieval_context") is True:
        constraints["disable_retrieval_context"] = True
    if arguments.get("defer_background_drain") is True:
        constraints["deferTypescriptAgentBackgroundDrain"] = True
    if arguments.get("sealed_bounded_read_only_fanout") is True:
        constraints.update(
            {
                "permission_mode": "sealed",
                "permission_interactive": False,
                "permission_headless": True,
                "e02PermissionPolicy": {
                    "version": "zyra.e02-typescript-permission-policy-input.v1",
                    "canonical_owner": "typescript",
                    "mode": "sealed",
                    "mode_revision": 1,
                    "interactive": False,
                    "headless": True,
                    "rules": [
                        {
                            "rule_id": "managed-bounded-read-only-agent-fanout",
                            "effect": "allow",
                            "source": "managed",
                            "tool_pattern": "Agent",
                            "namespace_pattern": "agent",
                            "operation_pattern": "execute",
                            "argument_pattern": (
                                '*"sealed_bounded_read_only_fanout":true*'
                            ),
                            "priority": 10_000,
                            "enabled": True,
                            "max_uses": 1,
                            "scope": {"session_id": parent_session_id},
                            "reason": (
                                "managed sealed policy permits one workspace-isolated "
                                "background fanout whose child tools are restricted to file_read"
                            ),
                            "metadata": {
                                "bounded_tools": ["file_read"],
                                "human_approval_required": False,
                            },
                        }
                    ],
                    "python_policy_fallback": False,
                },
            }
        )
    if isinstance(owner_authorization, Mapping):
        owner = str(owner_authorization.get("owner") or "")
        action = str(owner_authorization.get("action") or "")
        granted = owner_authorization.get("granted") is True
        exact = owner_authorization.get("exact") is True
        one_shot = owner_authorization.get("one_shot") is True
        expected_action = {
            "agent_kill": "kill",
            "agent_message": "steer",
        }.get(tool_name)
        if (
            owner != "SubagentTaskStore"
            or action != expected_action
            or not granted
            or not exact
            or not one_shot
        ):
            raise RuntimeError("invalid canonical subagent control authorization")
        constraints.update(
            {
                "permission_mode": "default",
                "permission_interactive": False,
                "permission_headless": True,
                "e02PermissionPolicy": {
                    "version": "zyra.e02-typescript-permission-policy-input.v1",
                    "canonical_owner": "typescript",
                    "mode": "default",
                    "mode_revision": 1,
                    "interactive": False,
                    "headless": True,
                    "rules": [
                        {
                            "rule_id": (
                                "canonical-subagent-control:"
                                f"{request_id}:{tool_name}"
                            ),
                            "effect": "allow",
                            "source": "managed",
                            "tool_pattern": tool_name,
                            "priority": 10_000,
                            "enabled": True,
                            "max_uses": 1,
                            "reason": (
                                "exact one-shot RuntimeControlDispatcher grant "
                                "bridged to the canonical TypeScript subagent owner"
                            ),
                            "metadata": {
                                "grant_receipt_id": str(
                                    owner_authorization.get("authorization_id") or ""
                                ),
                                "owner": owner,
                                "owner_action": action,
                                "request_id": request_id,
                            },
                        }
                    ],
                    "python_policy_fallback": False,
                },
            }
        )
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
    retrieval_context = (
        None
        if constraints.get("disable_retrieval_context") is True
        else _worker_retrieval_context(
            canonical_store,
            task_id=state.task_id,
            workspace_manager=workspace_manager,
            workspace_access=workspace_access,
        )
    )
    return CodeWorkerRuntime(
        project_root=PROJECT_ROOT,
        workspace_root=worker_workspace_root,
        artifact_root=artifact_root_path(),
        permission_store=get_permission_store(),
        permission_state_path=permission_state_path(),
        tool_registry=default_tool_registry(),
        event_reader=canonical_store.task_events,
        checkpoint_reader=_tool_checkpoint_reader(canonical_store),
        runtime_services={
            "backend_action_dispatch_port": _code_worker_backend_action_dispatch_port(
                state,
                workspace_root=worker_workspace_root,
                route_sources=(constraints,),
            ),
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
            "fault_observation_sink": codeworker_fault_observation_sink(
                canonical_store,
                state,
            ),
            "fault_observation_sink_required": True,
        },
        retrieval_context_runtime=retrieval_context,
    ).run(request)


def _run_typescript_skill_request(
    store: SQLiteStore,
    state: Any,
    *,
    skill_name: str,
    skill_arguments: Mapping[str, Any],
    resources: Sequence[str],
    tool_call_id: str,
    session_id: str,
    session_custody_token: str,
) -> Any:
    """Run SkillTool inside the QueryEngine context required by E02."""

    workspace_manager = get_workspace_manager()
    workspace_access = workspace_manager.acquire_for_worker(
        task_id=state.task_id,
        session_id="",
        worker_id="CodeWorkerRuntime",
    )
    worker_workspace_root = workspace_manager.internal_task_root(workspace_access)
    materialize_bundled_skills(PROJECT_ROOT, worker_workspace_root)
    constraints: dict[str, Any] = {
        "query_turns": [[{
            "tool_name": "skill",
            "tool_call_id": tool_call_id,
            "arguments": {
                "skill": skill_name,
                "arguments": dict(skill_arguments),
                "resources": list(resources),
            },
        }]],
        "session_id": session_id,
        "workspace_ref": workspace_access.to_public_dict(),
        "permission_interactive": True,
        "permission_headless": False,
    }
    if session_custody_token:
        constraints["session_custody_token"] = session_custody_token
    request = WorkerRequest(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=state.root_node_id,
        worker_name="CodeWorkerRuntime",
        request_id=(
            "skill-worker-request-"
            + hashlib.sha256(
                f"{state.task_id}:{session_id}:{tool_call_id}".encode("utf-8")
            ).hexdigest()[:24]
        ),
        constraints=constraints,
        metadata={
            "origin": "typescript-skill-api",
            "canonical_skill_owner": "typescript.SkillCoordinator",
        },
    )
    retrieval_context = _worker_retrieval_context(
        store,
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
        event_reader=store.task_events,
        checkpoint_reader=_tool_checkpoint_reader(store),
        runtime_services={
            "backend_action_dispatch_port": _code_worker_backend_action_dispatch_port(
                state,
                workspace_root=worker_workspace_root,
                route_sources=(constraints,),
            ),
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
            "fault_observation_sink": codeworker_fault_observation_sink(store, state),
            "fault_observation_sink_required": True,
        },
        retrieval_context_runtime=retrieval_context,
    ).run(request)


def _acquire_subagent_physical_dispatch(
    state: Any,
    *,
    task_id: str,
    owner_session_id: str,
    idempotency_key: str,
    physical_location: str = "local",
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
    requested_location = str(physical_location or "local").strip().lower()
    if requested_location not in {"local", "edge"}:
        raise WorkerPoolError(
            WorkerPoolErrorCode.INVALID_ARGUMENT,
            f"unsupported subagent physical location: {requested_location}",
            operation="acquire_subagent_physical_dispatch",
            task_id=task_id,
        )
    preferred_worker_id = "local-code-worker"
    gateway_ref = "local-sandbox-gateway"
    backend_route_id = "local-code-worker"
    location = PhysicalWorkerLocation.LOCAL
    if requested_location == "edge":
        preferred_worker_id, _edge_adapter = ensure_api_edge_worker(pool_api)
        gateway_ref = "api-edge-sandbox-gateway"
        backend_route_id = "api-edge-sandbox-gateway"
        location = PhysicalWorkerLocation.EDGE
    else:
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
            gateway_ref=gateway_ref,
            backend_route_id=backend_route_id,
            required_capabilities=("agent_task",),
            locations=(location,),
            resources=PhysicalResourceVector(process_slots=1, memory_mb=64),
            execution_mode=PhysicalDispatchMode.BACKGROUND,
            edge_only=location is PhysicalWorkerLocation.EDGE,
            preferred_worker_ids=(preferred_worker_id,),
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
    edge_receipt: Mapping[str, Any] | None = None
    if worker.location is PhysicalWorkerLocation.EDGE:
        edge_dispatch = pool_api.integration.execute_edge(
            binding.binding_id,
            fence_token=lease.fence_token,
            payload={
                "operation": "json_transform",
                "input_payload": {
                    "task_id": task_id,
                    "run_id": state.run_id,
                    "owner_session_id": owner_session_id,
                    "logical_owner": "typescript.E03AgentControlCoordinator",
                },
                "artifact_name": f"{task_id}-edge-admission.json",
                "job_id": f"edge-subagent:{task_id}:{binding.attempt_number}",
                "metadata": {"parent_task_id": state.task_id},
            },
        )
        binding = edge_dispatch.binding
        edge_receipt = edge_dispatch.to_dict()
    else:
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
        "worker_location": worker.location.value,
        "backend_id": lease.backend_id,
        "fence_epoch": lease.fence_epoch,
        "manifest_digest": manifest.digest,
        "lease_state": lease.state.value,
        "typescript_dispatch_gate": "typescript.OmpWorkerDispatchRuntime",
        "logical_task_not_duplicated": True,
        "reused": reused,
        "integration_binding_id": binding.binding_id,
        "graph_ref": binding.foreign_refs.graph.to_dict(),
        "edge_gateway_receipt": edge_receipt,
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


_RUNTIME_OWNER_COMPOSITION_LOCK = threading.RLock()
_RUNTIME_OWNER_COMPOSITION: RuntimeOwnerComposition | None = None
_RUNTIME_OWNER_COMPOSITION_KEY: tuple[str, ...] | None = None


def gateway_state_path() -> Path:
    return runtime_configuration().path("state.gateway")


def _session_owner_activation() -> Mapping[str, Any]:
    spine = get_runtime_event_spine_bridge().health()
    details = dict(spine.details or {})
    store = dict(details.get("store") or {})
    ready = (
        bool(spine.ok)
        and details.get("canonicalOwner") == "RuntimeEventSqliteStore"
        and bool(store.get("open"))
        and store.get("path") == str(sqlite_path().resolve())
    )
    return activation_result(
        ready=ready,
        owner="typescript.ClaudeRuntimeCore/DurableSessionRuntime",
        store="typescript.RuntimeEventSqliteStore",
        state_root=str(sqlite_path().resolve()),
        revision=spine.high_watermark,
        details={"event_spine": spine.to_jsonable()},
    )


def _permission_owner_activation() -> Mapping[str, Any]:
    facade = get_permission_api_facade()
    response = facade.health()
    state = facade.control_plane.state_store.read_state()
    ready = (
        response.status == HTTPStatus.OK
        and not facade.control_plane.disabled
        and isinstance(state, Mapping)
    )
    return activation_result(
        ready=ready,
        owner="PermissionJournal/PermissionApprovalRuntime",
        store="PermissionStateStore",
        state_root=str(permission_state_path().resolve()),
        revision=state.get("revision"),
        details={
            "health_operation": response.body.get("operation"),
            "metrics": facade.control_plane.metrics(),
        },
    )


def _memory_owner_activation() -> Mapping[str, Any]:
    runtime = get_memory_curator_runtime()
    response = runtime.execute(
        MemoryCuratorWorkerRequest(operation=MemoryCuratorOperation.HEALTH)
    )
    health = dict(response.data)
    worker = dict(health.get("worker") or {})
    store = dict(worker.get("store") or {})
    ready = (
        response.status == "ok"
        and worker.get("canonical_memory_owner") == "SQLiteStore.memory_records"
        and bool(worker.get("artifact_store_configured"))
        and bool(worker.get("index_runtime_configured"))
        and not bool(store.get("corrupt"))
    )
    return activation_result(
        ready=ready,
        owner="RetrievalIntegrationRuntime/MemoryCommitRuntime",
        store="SQLiteStore.memory_records/SQLiteRetrievalIndex",
        state_root=str(sqlite_path().resolve()),
        revision=store.get("revision"),
        details={"operation": response.operation.value, **health},
    )


def _scheduler_owner_activation() -> Mapping[str, Any]:
    service = get_recovery_runtime_api()
    contract = service.application.contract()
    integrity = service.application.store.integrity_report()
    disabled = _truthy(
        os.environ.get("ZYRA_DISABLE_RECOVERY_RUNTIME"),
        default=False,
    )
    ready = (
        not disabled
        and isinstance(contract, Mapping)
        and isinstance(integrity, Mapping)
        and not bool(integrity.get("corrupt"))
    )
    return activation_result(
        ready=ready,
        owner="RecoveryApplication",
        store="RecoveryPlanStore",
        state_root=str(recovery_runtime_path().resolve()),
        revision=integrity.get("revision"),
        details={"contract": contract, "integrity": integrity},
    )


def _artifact_owner_activation() -> Mapping[str, Any]:
    root = artifact_root_path().resolve()
    root.mkdir(parents=True, exist_ok=True)
    service = artifact_catalog_service()
    probe = root / (
        f".zyra-artifact-owner-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    )
    try:
        with probe.open("xb") as stream:
            stream.write(b"m3-owner-readiness")
        ready = (
            isinstance(service.store, LocalArtifactStore)
            and probe.read_bytes() == b"m3-owner-readiness"
        )
    finally:
        probe.unlink(missing_ok=True)
    return activation_result(
        ready=ready,
        owner="ArtifactCatalogService",
        store="LocalArtifactStore",
        state_root=str(root),
        details={"read_audit_configured": service.audit is _ARTIFACT_READ_AUDIT},
    )


def _worker_owner_activation() -> Mapping[str, Any]:
    api = get_worker_pool_api()
    projection = dict(api.pool.api_projection())
    integrity = dict(api.pool.store.integrity_report())
    ready = (
        projection.get("custody", {}).get("worker_lease") == "WorkerPoolStore"
        and projection.get("custody", {}).get("execution_receipt")
        == "WorkerPoolStore"
        and not bool(integrity.get("corrupt"))
    )
    return activation_result(
        ready=ready,
        owner="WorkerPoolIntegrationRuntime",
        store="WorkerPoolStore",
        state_root=str(worker_pool_path().resolve()),
        revision=projection.get("revision"),
        details={"projection": projection, "integrity": integrity},
    )


def _graph_owner_activation() -> Mapping[str, Any]:
    api = get_worker_pool_api()
    integrity = dict(api.graph_custody.store.integrity_report())
    ready = not bool(integrity.get("corrupt"))
    return activation_result(
        ready=ready,
        owner="DynamicTopologyRuntime/GraphStateCustody",
        store="GraphStateStore",
        state_root=str(graph_state_path().resolve()),
        revision=integrity.get("revision"),
        details=integrity,
    )


def _provider_owner_activation() -> Mapping[str, Any]:
    database = runtime_configuration().path("state.provider")
    health = get_provider_control_client(
        project_root=PROJECT_ROOT,
        database_path=database,
    ).health()
    ready = (
        health.get("schema") == "zyra.provider-control-plane.health/v1"
        and health.get("stateOwner") == "typescript.ProviderControlPlaneStore"
        and health.get("backendFallbackOwned") is False
    )
    return activation_result(
        ready=ready,
        owner="typescript.ProviderControlPlane",
        store="typescript.ProviderControlPlaneStore",
        state_root=str(database.resolve()),
        revision=health.get("revision"),
        details=health,
    )


def _mcp_owner_activation() -> Mapping[str, Any]:
    health = get_mcp_runtime().health()
    runtime = dict(health.get("runtime") or {})
    details = dict(runtime.get("details") or {})
    mcp = dict(details.get("mcp") or {})
    ready = (
        health.get("canonical_owner") == "typescript"
        and runtime.get("canonicalMcpOwner") == "typescript"
        and bool(mcp.get("opened"))
        and not bool(mcp.get("python_live_client_fallback"))
    )
    fallback = bool(
        health.get("python_fallback_active")
        or health.get("fallback_active")
    )
    return activation_result(
        ready=ready,
        owner="typescript.McpRuntimeCoordinator",
        store="typescript.McpRequestJournal",
        state_root=str(mcp_state_path().resolve()),
        revision=runtime.get("capabilityRevision"),
        fallback_active=fallback,
        details=health,
    )


def _gateway_owner_activation() -> Mapping[str, Any]:
    state_store = GatewayStateStore(gateway_state_path())
    snapshot = state_store.snapshot()
    metadata = dict(snapshot.get("metadata") or {})
    ready = (
        metadata.get("canonical_owner") == "SandboxGatewayRuntime"
        and snapshot.get("schema") is not None
    )
    return activation_result(
        ready=ready,
        owner="SandboxGatewayRuntime",
        store="GatewayStateStore",
        state_root=str(gateway_state_path().resolve()),
        revision=snapshot.get("revision"),
        details={
            "schema": snapshot.get("schema"),
            "metadata": metadata,
            "lease_count": len(snapshot.get("leases") or {}),
        },
    )


def _terminal_owner_activation() -> Mapping[str, Any]:
    snapshot = get_terminal_api().registry.snapshot()
    ready = (
        bool(snapshot.get("enabled"))
        and snapshot.get("canonical_owner")
        == "zyra_workers.terminal.TerminalSessionRegistry"
    )
    return activation_result(
        ready=ready,
        owner="TerminalSessionRegistry/BrowserSessionRuntime",
        store="TerminalStateStore/JsonBrowserStateStore",
        state_root=str(terminal_state_path().resolve()),
        revision=len(snapshot.get("sessions") or {}),
        details=snapshot,
    )


def _runtime_owner_activations() -> Mapping[RuntimeDomain, Any]:
    return {
        RuntimeDomain.SESSION_EVENT_PROJECTION: _session_owner_activation,
        RuntimeDomain.PERMISSION: _permission_owner_activation,
        RuntimeDomain.MEMORY_COMPACT: _memory_owner_activation,
        RuntimeDomain.SCHEDULER_RECOVERY: _scheduler_owner_activation,
        RuntimeDomain.ARTIFACT: _artifact_owner_activation,
        RuntimeDomain.WORKER_ROUTE: _worker_owner_activation,
        RuntimeDomain.GRAPH_CHECKPOINT: _graph_owner_activation,
        RuntimeDomain.PROVIDER_CREDENTIAL_FAILOVER: _provider_owner_activation,
        RuntimeDomain.MCP_PLUGIN_REGISTRY: _mcp_owner_activation,
        RuntimeDomain.GATEWAY_LEASE_BUSY: _gateway_owner_activation,
        RuntimeDomain.TERMINAL_BROWSER_SESSION: _terminal_owner_activation,
    }


_RUNTIME_DEFAULT_ENTRY_DOMAINS = {
    "cli.session.runtime": RuntimeDomain.SESSION_EVENT_PROJECTION,
    "web.permission.runtime": RuntimeDomain.PERMISSION,
    "worker.memory.retrieval": RuntimeDomain.MEMORY_COMPACT,
    "worker.scheduler.recovery": RuntimeDomain.SCHEDULER_RECOVERY,
    "api.artifact.catalog": RuntimeDomain.ARTIFACT,
    "worker.pool.dispatch": RuntimeDomain.WORKER_ROUTE,
    "worker.graph.topology": RuntimeDomain.GRAPH_CHECKPOINT,
    "worker.provider.dispatch": RuntimeDomain.PROVIDER_CREDENTIAL_FAILOVER,
    "worker.mcp.coordinator": RuntimeDomain.MCP_PLUGIN_REGISTRY,
    "worker.gateway.command": RuntimeDomain.GATEWAY_LEASE_BUSY,
    "web.interactive.sessions": RuntimeDomain.TERMINAL_BROWSER_SESSION,
}


def _runtime_entry_activation(entry: Any) -> Mapping[str, Any]:
    domain = _RUNTIME_DEFAULT_ENTRY_DOMAINS[entry.entry_id]
    result = dict(_runtime_owner_activations()[domain]())
    result.update(
        {
            "reachable": bool(result.get("ready")),
            "write_path_ready": bool(result.get("ready")),
            "write_symbols": list(entry.write_symbols),
            "fallback_bypass": bool(result.get("fallback_active")),
            "command_or_route": entry.command_or_route,
            "composition_root": "apps/api/zyra_api/main.py",
        }
    )
    return result


def get_runtime_owner_composition() -> RuntimeOwnerComposition:
    global _RUNTIME_OWNER_COMPOSITION, _RUNTIME_OWNER_COMPOSITION_KEY
    key = (
        str(PROJECT_ROOT.resolve()),
        str(sqlite_path().resolve()),
        str(artifact_root_path().resolve()),
        str(permission_state_path().resolve()),
        str(worker_pool_path().resolve()),
        str(graph_state_path().resolve()),
        str(mcp_state_path().resolve()),
        str(gateway_state_path().resolve()),
        str(terminal_state_path().resolve()),
        os.environ.get("ZYRA_RUNTIME_OWNER_ENFORCEMENT_DISABLED", ""),
    )
    with _RUNTIME_OWNER_COMPOSITION_LOCK:
        if _RUNTIME_OWNER_COMPOSITION is None or _RUNTIME_OWNER_COMPOSITION_KEY != key:
            disabled = _truthy(
                os.environ.get("ZYRA_RUNTIME_OWNER_ENFORCEMENT_DISABLED"),
                default=False,
            )
            owner_activations = _runtime_owner_activations()
            entry_activations = {
                entry_id: _runtime_entry_activation
                for entry_id in _RUNTIME_DEFAULT_ENTRY_DOMAINS
            }
            _RUNTIME_OWNER_COMPOSITION = RuntimeOwnerComposition(
                PROJECT_ROOT,
                RuntimeActivationSet(
                    owner=owner_activations,
                    entry=entry_activations,
                ),
                enabled=not disabled,
            )
            _RUNTIME_OWNER_COMPOSITION_KEY = key
        return _RUNTIME_OWNER_COMPOSITION


def reset_runtime_owner_composition() -> None:
    global _RUNTIME_OWNER_COMPOSITION, _RUNTIME_OWNER_COMPOSITION_KEY
    with _RUNTIME_OWNER_COMPOSITION_LOCK:
        _RUNTIME_OWNER_COMPOSITION = None
        _RUNTIME_OWNER_COMPOSITION_KEY = None


def get_api_product_bootstrap(*, auto_start: bool = False) -> ProductBootstrapRuntime:
    def owner_probe() -> Mapping[str, Any]:
        attempts: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        for attempt in range(1, 4):
            result = dict(get_runtime_owner_composition().probe_all())
            attempts.append(
                {
                    "attempt": attempt,
                    "ready": result.get("ready") is True,
                    "blockers": list(result.get("blockers") or ()),
                    "digest": result.get("digest"),
                }
            )
            if result.get("ready") is True:
                break
            if attempt < 3:
                time.sleep(0.15 * attempt)
        result["probe_attempts"] = attempts
        result.setdefault("schema", "zyra.bootstrap-owner-readiness/v1")
        return result

    return get_product_bootstrap(
        PROJECT_ROOT,
        owner_probe=owner_probe,
        auto_start=auto_start,
    )


def reset_api_product_bootstrap(*, shutdown: bool = True) -> None:
    reset_product_bootstrap(shutdown=shutdown)


def runtime_readiness_probes(
    *,
    typed_receipts: TypedReceiptStore,
) -> tuple[dict[str, bool], dict[str, Any]]:
    """Project the 11-domain owner gate onto the legacy readiness contract."""

    legacy = {
        "task_store": False,
        "event_log": False,
        "checkpoint_store": False,
        "artifact_store": False,
        "control_runtime": False,
        "typed_transport": False,
    }
    productization: Mapping[str, Any]
    try:
        bootstrap = get_api_product_bootstrap()
        bootstrap.start()
        productization = bootstrap.readiness()
    except Exception as error:  # noqa: BLE001 - startup must fail closed.
        try:
            productization = get_api_product_bootstrap().readiness()
        except Exception as readiness_error:  # noqa: BLE001
            productization = {
                "schema": "zyra.product-bootstrap-readiness/v1",
                "ready": False,
                "blockers": ["product_bootstrap"],
                "error": (
                    f"{type(error).__name__}: {error}; "
                    f"{type(readiness_error).__name__}: {readiness_error}"
                )[:2048],
                "demo_fallback": False,
                "source_store_fallback": False,
            }
    try:
        canonical = get_runtime_owner_composition().probe_all()
    except Exception as error:  # noqa: BLE001 - readiness must fail closed.
        return legacy, {
            "productization": dict(productization),
            "canonical_runtime_owners": {
                "ready": False,
                "blockers": [item.value for item in RuntimeDomain],
                "error": f"{type(error).__name__}: {error}",
            }
        }

    by_domain = {
        str(item["domain"]): bool(item["ready"])
        for item in canonical["domains"]
    }
    session_ready = by_domain.get(RuntimeDomain.SESSION_EVENT_PROJECTION.value, False)
    control_domains = (
        RuntimeDomain.PERMISSION,
        RuntimeDomain.SCHEDULER_RECOVERY,
        RuntimeDomain.GATEWAY_LEASE_BUSY,
    )
    transport_domains = (
        RuntimeDomain.MCP_PLUGIN_REGISTRY,
        RuntimeDomain.PROVIDER_CREDENTIAL_FAILOVER,
    )
    legacy.update(
        {
            "task_store": session_ready,
            "event_log": session_ready,
            "checkpoint_store": by_domain.get(
                RuntimeDomain.GRAPH_CHECKPOINT.value,
                False,
            ),
            "artifact_store": by_domain.get(RuntimeDomain.ARTIFACT.value, False),
            "control_runtime": all(
                by_domain.get(domain.value, False) for domain in control_domains
            ),
            "typed_transport": (
                isinstance(typed_receipts, TypedReceiptStore)
                and all(
                    by_domain.get(domain.value, False)
                    for domain in transport_domains
                )
            ),
        }
    )
    if productization.get("ready") is not True:
        legacy = {key: False for key in legacy}
    details = {
        "productization": dict(productization),
        "canonical_runtime_owners": canonical,
        "legacy_projection": {
            name: {
                "available": ready,
                "source": "canonical_runtime_owner_composition",
            }
            for name, ready in legacy.items()
        },
    }
    return legacy, details


class _TypeScriptPermissionQueueProjection:
    """Read-only pending projection from the canonical TypeScript owner."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self.session_id = str(
            state.metadata.get("query_session_id") or f"task:{state.task_id}"
        )

    def pending(self) -> tuple[SimpleNamespace, ...]:
        projection = get_mcp_runtime().permission_get(
            view="requests",
            status="",
            limit=1000,
        )
        rows = projection.get("requests") or ()
        pending = []
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            status = str(item.get("status") or "").casefold()
            if status not in {
                "pending",
                "created",
                "delivered",
                "awaiting_response",
            }:
                continue
            task_id = str(item.get("task_id") or item.get("taskId") or "")
            run_id = str(item.get("run_id") or item.get("runId") or "")
            session_id = str(
                item.get("session_id") or item.get("sessionId") or ""
            )
            if task_id and task_id != self.state.task_id:
                continue
            if run_id and run_id != self.state.run_id:
                continue
            if session_id and session_id != self.session_id:
                continue
            pending.append(
                SimpleNamespace(
                    request_id=str(
                        item.get("request_id") or item.get("requestId") or ""
                    ),
                    status=status,
                    revision=int(item.get("revision") or 0),
                    session_id=session_id or self.session_id,
                )
            )
        return tuple(pending)


class _CanonicalFinalVerifierOwner:
    """Independently verify canonical task state, then persist the verdict."""

    def __init__(self, store: SQLiteStore, artifact_store: LocalArtifactStore) -> None:
        self.store = store
        self.artifact_store = artifact_store

    def verify_final_state(
        self,
        state: Any,
        events: Sequence[EventRecord],
    ) -> Mapping[str, Any]:
        scope = dict(state.metadata.get("phase2_final_verifier_scope") or {})
        receipt = dict(state.metadata.get("worker_pool_receipt") or {})
        physical_receipt = dict(
            receipt.get("physical_dispatch_receipt") or {}
        )
        physical_payload = dict(physical_receipt.get("payload") or {})
        physical_signals = dict(
            physical_payload.get("input_signals") or {}
        )
        historical_physical_receipts: list[dict[str, Any]] = []
        invalid_physical_receipt_history = False
        seen_physical_digests: set[str] = set()
        for raw_historical in state.metadata.get(
            "physical_dispatch_receipts"
        ) or ():
            if not isinstance(raw_historical, Mapping):
                invalid_physical_receipt_history = True
                continue
            try:
                historical = PhysicalDispatchReceipt.from_dict(
                    raw_historical
                ).to_dict()
            except (TypeError, ValueError):
                invalid_physical_receipt_history = True
                continue
            historical_digest = str(historical.get("digest") or "")
            if historical_digest in seen_physical_digests:
                continue
            seen_physical_digests.add(historical_digest)
            historical_physical_receipts.append(historical)
        delivery_physical_receipt = next(
            (
                item
                for item in reversed(historical_physical_receipts)
                if dict(item.get("payload") or {})
                .get("input_signals", {})
                .get("operator_adapter_id")
                == "worker.code-worker.typescript-provider-tool-loop"
            ),
            {},
        )
        delivery_physical_payload = dict(
            delivery_physical_receipt.get("payload") or {}
        )
        delivery_physical_signals = dict(
            delivery_physical_payload.get("input_signals") or {}
        )
        delivery_operator_domain_result = dict(
            delivery_physical_signals.get("domain_result") or {}
        )
        operator_execution_body = dict(
            physical_signals.get("operator_execution_body") or {}
        )
        operator_contract_outputs = dict(
            physical_signals.get("contract_outputs") or {}
        )
        operator_domain_artifact = dict(
            physical_signals.get("domain_artifact") or {}
        )
        operator_domain_result = dict(
            physical_signals.get("domain_result") or {}
        )
        memory_mutation_receipt = dict(
            receipt.get("memory_mutation_receipt")
            or (receipt.get("metadata") or {}).get(
                "memory_mutation_receipt"
            )
            or {}
        )
        memory_receipt_unsigned = dict(memory_mutation_receipt)
        memory_receipt_digest = str(
            memory_receipt_unsigned.pop("receipt_digest", "")
        )
        memory_owner_commit_required = (
            physical_signals.get("operator_adapter_id")
            == "worker.local-memory-curator.continuity"
        )
        binding = dict(state.metadata.get("operator_placement_binding") or {})
        binding_unsigned = dict(binding)
        binding_digest = str(binding_unsigned.pop("binding_digest", ""))
        verified_artifacts: list[StableArtifactRef] = []
        invalid_artifact_ids: list[str] = []
        for artifact in state.artifacts:
            try:
                observed = self.artifact_store.verify(artifact)
            except Exception:  # noqa: BLE001 - verifier records a failed verdict.
                invalid_artifact_ids.append(str(artifact.artifact_id or ""))
                continue
            verified_artifacts.append(
                StableArtifactRef(
                    ref_id=str(artifact.artifact_id),
                    uri=str(artifact.uri),
                    digest=str(observed.sha256),
                    media_type=str(
                        artifact.metadata.get("content_type")
                        or "application/octet-stream"
                    ),
                )
            )
        stage_nodes = tuple(
            item
            for item in state.plan_nodes.values()
            if str(item.metadata.get("stage") or "")
        )
        keeper = ConstraintKeeper()
        constraint_results = keeper.check_task_state(
            state,
            node=state.plan_nodes.get(state.root_node_id),
            transition="inspect",
        )
        response_contract = direct_response_contract(state.user_goal)
        final_answer = str(state.metadata.get("final_answer") or "").strip()
        goal_verification = validate_direct_response(
            state.user_goal,
            final_answer,
        )
        workspace_root: Path | None = None
        try:
            workspace_manager = get_workspace_manager()
            verifier_access = workspace_manager.acquire_for_worker(
                task_id=state.task_id,
                session_id=str(
                    state.metadata.get("query_session_id")
                    or f"task:{state.task_id}"
                ),
                worker_id=f"final-verifier:{state.task_id}",
            )
            workspace_root = workspace_manager.internal_task_root(
                verifier_access
            )
        except WorkspaceError:
            workspace_root = None
        delivery_verification = validate_goal_delivery(
            state.user_goal,
            projection=(
                state.metadata.get("delivery_contract")
                if isinstance(state.metadata.get("delivery_contract"), Mapping)
                else None
            ),
            workspace_root=workspace_root,
            workspace_delta=(
                delivery_physical_signals.get("workspace_delta")
                if isinstance(
                    delivery_physical_signals.get("workspace_delta"), Mapping
                )
                else delivery_operator_domain_result.get("workspace_delta")
                if isinstance(
                    delivery_operator_domain_result.get("workspace_delta"),
                    Mapping,
                )
                else None
            ),
            final_response=final_answer,
            provider_evidence=(
                delivery_physical_payload.get("provider_evidence")
                if isinstance(
                    delivery_physical_payload.get("provider_evidence"), Mapping
                )
                else None
            ),
        )
        checks = {
            "requirement_scope_bound": bool(
                scope.get("requirement_revision")
                and isinstance(scope.get("expected_obligation_ids"), list)
            ),
            "stage_graph_completed": bool(
                stage_nodes
                and all(item.status is PlanNodeStatus.COMPLETED for item in stage_nodes)
            ),
            "constraint_keeper_passed": not keeper.has_blocking_failure(
                constraint_results
            ),
            "artifact_owner_verified": bool(verified_artifacts)
            and not invalid_artifact_ids
            and len(verified_artifacts) == len(state.artifacts),
            "physical_outcome_succeeded": bool(
                receipt.get("receipt_id")
                and receipt.get("outcome") == "succeeded"
                and receipt.get("run_id") == state.run_id
                and receipt.get("task_id") == state.task_id
            ),
            "physical_receipt_history_valid": bool(
                historical_physical_receipts
                and not invalid_physical_receipt_history
            ),
            "delivery_evidence_receipt_bound": bool(
                delivery_physical_receipt
                and delivery_physical_signals.get("workload_operation")
                == "phase2-operator-execution"
                and delivery_physical_signals.get("operator_adapter_id")
                == "worker.code-worker.typescript-provider-tool-loop"
                and delivery_physical_payload.get("provider_evidence", {}).get(
                    "task_execution_verified"
                )
                is True
            ),
            "physical_operator_call_bound": bool(
                physical_receipt.get("schema_version")
                == "zyra.physical-dispatch-receipt/v2"
                and physical_signals.get("workload_operation")
                == "phase2-operator-execution"
                and physical_payload.get("placement_decision_id")
                == binding.get("resource_decision_id")
                and physical_payload.get("lease_id")
                == binding.get("lease_id")
                and physical_payload.get("physical_attempt_id")
                == binding.get("attempt_id")
                and physical_signals.get("worker_id")
                == binding.get("worker_id")
                and receipt.get("metadata", {}).get(
                    "physical_dispatch_receipt_digest"
                )
                == physical_receipt.get("digest")
            ),
            "physical_worker_process_bound": bool(
                binding.get("worker_process_identity")
                == physical_signals.get("leased_worker_process_identity")
                == physical_payload.get("physical_identity", {}).get(
                    "failure_boundary_id"
                )
                == physical_payload.get("runtime_evidence", {}).get(
                    "failure_boundary_id"
                )
                and binding.get("worker_endpoint")
                == physical_signals.get("leased_worker_endpoint")
                == physical_payload.get("physical_identity", {}).get(
                    "endpoint"
                )
                == physical_payload.get("runtime_evidence", {}).get(
                    "network_endpoint"
                )
            ),
            "physical_operator_execution_digest_valid": bool(
                operator_execution_body
                and str(
                    physical_signals.get("operator_execution_digest") or ""
                ).removeprefix("sha256:")
                == canonical_digest(operator_execution_body)
            ),
            "physical_operator_contract_fulfilled": bool(
                operator_contract_outputs
                and physical_signals.get("output_contract_fulfilled") is True
                and physical_signals.get("domain_effect_performed") is True
                and sorted(operator_contract_outputs)
                == sorted(operator_execution_body.get("output_contract") or ())
                == sorted(
                    operator_execution_body.get(
                        "fulfilled_output_contract"
                    )
                    or ()
                )
                and str(
                    operator_execution_body.get("contract_outputs_digest")
                    or ""
                ).removeprefix("sha256:")
                == canonical_digest(operator_contract_outputs)
            ),
            "physical_domain_artifact_committed": bool(
                operator_domain_artifact.get("content")
                and str(
                    operator_domain_artifact.get("content_digest") or ""
                ).removeprefix("sha256:")
                == canonical_digest(operator_domain_artifact.get("content"))
                and any(
                    artifact.metadata.get("operator_ref")
                    == physical_signals.get("operator_ref")
                    and artifact.metadata.get(
                        "operator_output_contract_fulfilled"
                    )
                    is True
                    and artifact.metadata.get("domain_output_digest")
                    == str(
                        operator_domain_artifact.get("content_digest") or ""
                    ).removeprefix("sha256:")
                    for artifact in state.artifacts
                )
            ),
            "physical_domain_result_bound": bool(
                operator_domain_result
                and str(
                    operator_execution_body.get("domain_result_digest") or ""
                ).removeprefix("sha256:")
                == canonical_digest(operator_domain_result)
            ),
            "memory_owner_commit_bound": bool(
                not memory_owner_commit_required
                or (
                    memory_mutation_receipt.get("owner") == "MemoryFabric"
                    and memory_mutation_receipt.get("run_id") == state.run_id
                    and memory_mutation_receipt.get("task_id") == state.task_id
                    and memory_mutation_receipt.get("committed") is True
                    and memory_mutation_receipt.get("readback_verified") is True
                    and bool(
                        memory_mutation_receipt.get("committed_record_ids")
                    )
                    and memory_mutation_receipt.get(
                        "physical_dispatch_receipt_digest"
                    )
                    == physical_receipt.get("digest")
                    and memory_mutation_receipt.get(
                        "operator_execution_digest"
                    )
                    == physical_signals.get("operator_execution_digest")
                    and memory_receipt_digest
                    == canonical_digest(memory_receipt_unsigned)
                )
            ),
            "physical_artifacts_exact": bool(
                set(str(item) for item in receipt.get("artifact_refs") or ())
                == {str(item.artifact_id) for item in state.artifacts}
            ),
            "placement_binding_digest_valid": bool(
                binding_digest
                and binding_digest == canonical_digest(binding_unsigned)
            ),
            "physical_event_causality_present": bool(
                set(str(item) for item in receipt.get("event_refs") or ())
                .intersection(
                    str(item.event_id) for item in events if item.event_id
                )
            ),
            "goal_contract_projection_bound": bool(
                goal_contract_matches_projection(
                    state.user_goal,
                    state.metadata.get("goal_contract")
                    if isinstance(state.metadata.get("goal_contract"), Mapping)
                    else None,
                )
            ),
            "goal_contract_satisfied": bool(goal_verification.get("passed")),
            **{
                f"delivery_{name}": bool(passed)
                for name, passed in dict(
                    delivery_verification.get("checks") or {}
                ).items()
            },
            "direct_response_artifact_bound": bool(
                response_contract is None
                or (
                    final_answer
                    and delivery_operator_domain_result.get("kind")
                    == "code_worker_execution"
                    and delivery_physical_signals.get("operator_adapter_id")
                    == "worker.code-worker.typescript-provider-tool-loop"
                    and str(
                        delivery_physical_signals.get("final_text") or ""
                    ).strip()
                    == final_answer
                    and str(
                        delivery_operator_domain_result.get(
                            "final_answer_digest"
                        )
                        or ""
                    ).removeprefix("sha256:")
                    == canonical_digest(final_answer)
                )
            ),
        }
        passed = all(checks.values())
        verified_artifacts.sort(key=lambda item: item.ref_id)
        verified_at = now_iso()
        verifier_ref = "final-verifier://" + state.task_id + "/" + canonical_digest(
            {
                "run_id": state.run_id,
                "task_id": state.task_id,
                "scope": scope,
                "checks": checks,
                "artifacts": [item.to_dict() for item in verified_artifacts],
                "physical_receipt_id": receipt.get("receipt_id"),
            }
        )[:32]
        decision = build_final_verifier_decision(
            task=state,
            requirement_revision=str(scope.get("requirement_revision") or ""),
            expected_obligation_ids=tuple(
                str(item) for item in scope.get("expected_obligation_ids") or ()
            ),
            verified_artifact_refs=tuple(verified_artifacts),
            passed=passed,
            verifier_version="phase2-independent-final-verifier-v2",
            verifier_receipt_ref=verifier_ref,
            verified_at=verified_at,
            fresh_until=(
                datetime.now(UTC) + timedelta(seconds=300)
            ).isoformat().replace("+00:00", "Z"),
            verification_refs=(
                "final_verifier",
                *(f"final-verifier-check:{name}" for name in sorted(checks)),
            ),
        )
        decision.checks = [
            {"condition": name, "passed": value}
            for name, value in sorted(checks.items())
        ]
        self.record_final_verifier_receipt(decision)
        state.decisions.append(decision)
        return {
            "schema": "zyra.production-independent-final-verifier/v2",
            "passed": passed,
            "checks": checks,
            "decision_id": decision.decision_id,
            "verifier_receipt_ref": verifier_ref,
            "invalid_artifact_ids": sorted(invalid_artifact_ids),
            "canonical_owner_bypass": False,
        }

    def record_final_verifier_receipt(self, decision: Any) -> None:
        metadata = dict(decision.metadata)
        receipt_ref = str(metadata.get("verifier_receipt_ref") or "")
        event = EventRecord(
            event_id=(
                "event_final_verifier_owner_"
                + canonical_digest((receipt_ref, metadata))[:24]
            ),
            run_id=decision.run_id,
            task_id=decision.task_id,
            node_id=(
                decision.affected_node_ids[0]
                if decision.affected_node_ids
                else None
            ),
            event_type=EventType.EVALUATION,
            payload={
                "schema": "zyra.final-verifier-owner-receipt/v1",
                "canonical_owner": "SQLiteStore.EventRecord",
                "verifier_receipt_ref": receipt_ref,
                "decision_id": decision.decision_id,
                "decision_metadata": metadata,
            },
        )
        persist_events(self.store, [event])

    def resolve_final_verifier_receipt(
        self,
        receipt_ref: str,
    ) -> Mapping[str, Any] | None:
        # SQLiteStore has no global receipt index; restrict the scan to the
        # task encoded by the canonical verifier URI.
        parts = str(receipt_ref).split("/")
        task_id = parts[-2] if len(parts) >= 2 else ""
        if not task_id:
            return None
        for event in reversed(self.store.task_events(task_id)):
            payload = (
                dict(event.get("payload") or {})
                if isinstance(event, Mapping)
                else dict(event.payload)
            )
            if (
                payload.get("schema")
                == "zyra.final-verifier-owner-receipt/v1"
                and payload.get("verifier_receipt_ref") == receipt_ref
                and isinstance(payload.get("decision_metadata"), Mapping)
            ):
                return dict(payload["decision_metadata"])
        return None


def _scheduler_backend_from_physical_manifest(
    manifest: Any,
) -> WorkerBackendKind:
    aliases = {
        WorkerBackendKind.LOCAL_PROCESS.value: WorkerBackendKind.LOCAL_PROCESS,
        WorkerBackendKind.ISOLATED_PROCESS.value: WorkerBackendKind.ISOLATED_PROCESS,
        WorkerBackendKind.DOCKER_SANDBOX.value: WorkerBackendKind.DOCKER_SANDBOX,
        WorkerBackendKind.CLOUD_MODEL.value: WorkerBackendKind.CLOUD_MODEL,
    }
    declared = tuple(
        dict.fromkeys(
            str(item).strip().casefold()
            for item in getattr(manifest, "backend_kinds", ())
            if str(item).strip()
        )
    )
    if len(declared) != 1 or declared[0] not in aliases:
        raise RuntimeError(
            "dynamic physical manifest requires exactly one supported real "
            f"backend kind: {declared!r}"
        )
    selected = aliases[declared[0]]
    location = str(getattr(getattr(manifest, "location", None), "value", ""))
    allowed = {
        ResourceLocation.LOCAL.value: {
            WorkerBackendKind.LOCAL_PROCESS,
            WorkerBackendKind.DOCKER_SANDBOX,
        },
        ResourceLocation.EDGE.value: {
            WorkerBackendKind.ISOLATED_PROCESS,
            WorkerBackendKind.DOCKER_SANDBOX,
        },
        ResourceLocation.CLOUD.value: {WorkerBackendKind.CLOUD_MODEL},
    }
    if selected not in allowed.get(location, set()):
        raise RuntimeError(
            "dynamic physical manifest backend/location mismatch: "
            f"location={location!r}, backend={declared[0]!r}"
        )
    return selected


def graph_execution_context() -> GraphExecutionContext:
    pool_api = get_worker_pool_api()
    _ensure_phase2_production_workers(pool_api)
    physical_workers = {
        item.worker_id: item
        for item in pool_api.pool.store.list_workers()
        if (
            item.accepting_leases
            and item.metadata.get("phase2_production_worker") is True
        )
    }
    static_manifests = {
        item.worker_id: item for item in WorkerPool().manifests()
    }
    executable: list[WorkerManifest] = []
    for worker_id, worker in sorted(physical_workers.items()):
        static = static_manifests.get(worker_id)
        manifest = pool_api.pool.store.latest_manifest(worker_id)
        if manifest is None:
            continue
        executable.append(
            WorkerManifest(
                worker_id=worker_id,
                display_name=f"Physical worker {worker_id}",
                runtime_worker=(
                    static.runtime_worker
                    if static is not None
                    else (
                        "MemoryCuratorRuntime"
                        if "memory" in worker.worker_kind.casefold()
                        else "CodeWorkerRuntime"
                    )
                ),
                location=ResourceLocation(manifest.location.value),
                backend=_scheduler_backend_from_physical_manifest(manifest),
                capabilities=list(manifest.capabilities),
                tools=list(manifest.tool_ids),
                sandbox="deployment-node",
                gateway="DeploymentNodeRuntime",
                workspace_scope="project-workspace",
                privacy_level=(
                    "sensitive_ok"
                    if manifest.location.value == "local"
                    else "internal_or_project"
                    if "provider-reasoning" in manifest.capabilities
                    else "public_or_masked"
                ),
                max_concurrency=max(
                    1, int(manifest.resource_capacity.process_slots)
                ),
                source_modules={
                    "zyra": [
                        "WorkerPoolFoundationRuntime dynamic physical manifest"
                    ]
                },
                metadata={
                    "dynamic_physical_manifest": True,
                    "process_identity": worker.process_identity,
                    "endpoint": worker.endpoint,
                    "physical_backend_kinds": list(manifest.backend_kinds),
                    "operator": {
                        "health_status": "healthy",
                        "health_source": (
                            "WorkerInstance.accepting_leases"
                        ),
                    },
                },
            )
        )
    executable_manifests = tuple(executable)
    if not executable_manifests:
        raise RuntimeError(
            "ResourceScheduler has no registered physical worker manifest"
        )
    scheduler = ResourceScheduler(WorkerPool(executable_manifests))
    artifact_store = LocalArtifactStore(artifact_root_path())
    final_verifier = _CanonicalFinalVerifierOwner(get_store(), artifact_store)
    topology_policy = Phase2StrongestProductionBridge(
        PROJECT_ROOT,
        worker_pool_api=pool_api,
        memory_fabric=_memory_fabric(get_store()),
        resource_scheduler=scheduler,
        policy_evidence_event_sink=(
            lambda event: persist_events(get_store(), [event])
        ),
        communication_outcome_provider=_phase2_communication_outcomes,
        communication_outcome_recorder=(
            _record_phase2_communication_outcomes
        ),
        permission_decision_provider=_phase2_permission_decision,
        artifact_store=artifact_store,
        delivery_state_probe=_observe_delivery_contract_paths,
        permission_queue_provider=_TypeScriptPermissionQueueProjection,
        recovery_store=get_recovery_runtime_api().application.store,
        final_verifier_owner=final_verifier,
        physical_dispatch_factory=_production_physical_dispatch_port,
        early_exit_enabled=lambda: not _truthy(
            os.environ.get("ZYRA_DISABLE_PHASE2_EARLY_EXIT"),
            default=False,
        ),
    )
    return GraphExecutionContext.from_paths(
        project_root=PROJECT_ROOT,
        workspace_root=tool_workspace_path(),
        artifact_root=artifact_root_path(),
        permission_store_path=permission_store_path(),
        workspace_runtime_resolver=_graph_workspace_runtime_binding,
        topology_policy_trigger=topology_policy,
        resource_scheduler=scheduler,
        execution_placement_validator=(
            topology_policy.validate_execution_placement
        ),
        physical_execution_runner=topology_policy.execute_physical_operator,
        physical_runtime_refresher=(
            lambda: _ensure_phase2_production_workers(pool_api, restart=True)
        ),
        execution_outcome_recorder=topology_policy.record_execution_outcome,
        final_verifier=final_verifier.verify_final_state,
        completion_gate=topology_policy.evaluate_completion,
    )


def _ensure_phase2_production_workers(
    pool_api: WorkerPoolApiService,
    *,
    restart: bool = False,
) -> dict[str, Any]:
    """Bind schedulable workers to truthful deployment-node processes.

    The WorkerPool lease identity and the deployment receipt must describe the
    same process boundary.  The CodeWorker is hosted by the cloud profile
    because that physical process owns network/provider access; the memory
    curator remains local.  Orphaned leases are fenced only after their former
    process identity is absent from every live managed node.
    """

    orchestrator = get_deployment_api().orchestrator
    _sync_configured_provider_environment(orchestrator)
    preferred_provider = _preferred_configured_provider()
    if preferred_provider is None:
        raise RuntimeError(
            "no configured live provider credential is available; expected "
            "ZAI_API_KEY, DEEPSEEK_API_KEY or KIMI_API_KEY"
        )
    provider_environment_names = {
        provider_id: key
        for key, _, provider_id, _model_id in _PROVIDER_ENV_FILES
    }
    required_cloud_credential = provider_environment_names[
        preferred_provider[0]
    ]
    live_nodes: dict[DeploymentProfile, tuple[Any, Mapping[str, Any], Any]] = {}
    live_identities: set[str] = set()
    reconciled_profiles: list[dict[str, Any]] = []
    for profile in (DeploymentProfile.DEVICE, DeploymentProfile.CLOUD):
        policy = orchestrator.catalog.policy(profile)
        prior_record = orchestrator.processes.status(f"profile:{profile.value}")
        # start_node trusts its own READY record and does not probe liveness,
        # so a node killed since the last poll still looks healthy here.  The
        # recovery path must force a replacement, otherwise the following
        # dispatch preflight is the first thing to notice the crash.
        process, client, health = orchestrator.processes.start_node(
            policy,
            restart=restart,
        )
        semantic = dict(client.semantic_readiness())
        credential_ready = bool(
            dict(health.get("credential_presence") or {}).get(
                required_cloud_credential
            )
        )
        if (
            "phase2-operator-execution"
            not in tuple(str(item) for item in semantic.get("operations") or ())
            or semantic.get("runtime_implementation_version")
            != "phase2-operator-execution-v8"
            or (profile is DeploymentProfile.CLOUD and not credential_ready)
        ):
            process, client, health = orchestrator.processes.start_node(
                policy,
                restart=True,
            )
            semantic = dict(client.semantic_readiness())
        if (
            "phase2-operator-execution"
            not in tuple(str(item) for item in semantic.get("operations") or ())
            or semantic.get("runtime_implementation_version")
            != "phase2-operator-execution-v8"
        ):
            raise RuntimeError(
                "the production deployment node does not expose the current "
                f"Phase 2 operator runtime: {profile.value}"
            )
        if profile is DeploymentProfile.CLOUD and not bool(
            dict(health.get("credential_presence") or {}).get(
                required_cloud_credential
            )
        ):
            raise RuntimeError(
                "the production cloud node does not carry the selected "
                f"provider credential reference: {required_cloud_credential}"
            )
        identity = dict(health.get("runtime_identity") or {})
        failure_boundary_id = str(identity.get("failure_boundary_id") or "")
        node_id = str(health.get("node_id") or "")
        generation_id = str(identity.get("generation_id") or "")
        if not failure_boundary_id or not node_id or not generation_id:
            raise RuntimeError(
                f"deployment node physical identity is incomplete: {profile.value}"
            )
        reconciled_profiles.append(
            {
                "profile": profile.value,
                "restart_requested": restart,
                "prior_status": (
                    prior_record.status.value if prior_record else "absent"
                ),
                "prior_pid": prior_record.pid if prior_record else 0,
                "prior_generation_id": (
                    prior_record.generation_id if prior_record else ""
                ),
                "pid": process.pid,
                "status": process.status.value,
                "generation_id": process.generation_id,
                "restart_count": process.restart_count,
                "failure_boundary_id": failure_boundary_id,
            }
        )
        live_nodes[profile] = (process, health, policy)
        live_identities.add(failure_boundary_id)

    static_manifests = {
        item.worker_id: item for item in WorkerPool().manifests()
    }
    worker_profiles = {
        # Keep the provider-backed production worker distinct from the
        # API-owned ``local-code-worker`` used by subagents and local control
        # surfaces.  Reusing that historical id made production startup fence
        # valid background leases before replacing their worker generation.
        "provider-code-worker": DeploymentProfile.CLOUD,
        "local-memory-curator": DeploymentProfile.DEVICE,
    }
    for worker_id, profile in worker_profiles.items():
        logical = static_manifests.get(worker_id)
        capability_manifest = pool_api.pool.store.latest_manifest(worker_id)
        if capability_manifest is None and logical is None:
            continue
        worker_kind = (
            logical.runtime_worker
            if logical is not None
            else pool_api.pool.store.require_worker(worker_id).worker_kind
        )
        capabilities = (
            tuple(logical.capabilities)
            if logical is not None
            else tuple(capability_manifest.capabilities)  # type: ignore[union-attr]
        )
        tools = (
            tuple(logical.tools)
            if logical is not None
            else tuple(capability_manifest.tool_ids)  # type: ignore[union-attr]
        )
        process, health, policy = live_nodes[profile]
        identity = dict(health.get("runtime_identity") or {})
        failure_boundary_id = str(identity.get("failure_boundary_id") or "")
        node_id = str(health.get("node_id") or "")
        generation_id = str(identity.get("generation_id") or "")
        is_code_worker = worker_id == "provider-code-worker"
        physical_location = (
            PhysicalWorkerLocation.CLOUD
            if is_code_worker
            else PhysicalWorkerLocation.LOCAL
        )
        backend_kind = "cloud_model" if is_code_worker else "local_process"
        backend_id = f"phase2-{profile.value}:{worker_id}"
        existing = pool_api.pool.store.get_worker(worker_id)
        exact_existing = bool(
            existing is not None
            and existing.accepting_leases
            and existing.process_identity == failure_boundary_id
            and existing.endpoint == process.endpoint
            and existing.backend_id == backend_id
            and str(existing.metadata.get("deployment_node_id") or "")
            == node_id
            and str(existing.metadata.get("deployment_generation_id") or "")
            == generation_id
        )
        if not exact_existing:
            active = tuple(
                item
                for item in pool_api.pool.store.list_leases(worker_id=worker_id)
                if not item.terminal
            )
            if active:
                old_identity = str(existing.process_identity if existing else "")
                if old_identity in live_identities:
                    raise RuntimeError(
                        "cannot replace a live physical worker registration while "
                        f"leases exist: {worker_id}"
                    )
                for lease in active:
                    pool_api.pool.leases.expire(
                        lease.lease_id,
                        reason=(
                            "orphaned physical worker generation is absent from "
                            "the reconciled deployment process set"
                        ),
                    )
            pool_api.pool.register_physical_worker(
                worker_id=worker_id,
                worker_kind=(
                    "code-worker"
                    if "memory" not in worker_kind.casefold()
                    else "memory-curator-worker"
                ),
                location=physical_location,
                backend=PhysicalBackendCapability(
                    backend_id=backend_id,
                    backend_kind=backend_kind,
                    enabled=True,
                    healthy=True,
                    capabilities=tuple(
                        dict.fromkeys(("agent_task", *capabilities))
                    ),
                    tool_ids=tools,
                    constraints={
                        "gateway_owner": "DeploymentNodeRuntime",
                        "physical_operator_runtime": True,
                    },
                    labels={"dispatch_location": physical_location.value},
                ),
                capabilities=tuple(
                    dict.fromkeys(("agent_task", *capabilities))
                ),
                tool_ids=tools,
                resources=PhysicalResourceVector(
                    cpu_cores=1.0,
                    memory_mb=1024,
                    disk_mb=2048,
                    network_mbps=100,
                    process_slots=4,
                ),
                process_identity=failure_boundary_id,
                endpoint=process.endpoint,
                replace_generation=existing is not None,
                metadata={
                    "phase2_production_worker": True,
                    "deployment_node_id": node_id,
                    "deployment_generation_id": generation_id,
                    "deployment_profile": policy.profile.value,
                    "deployment_pid": int(identity.get("pid") or process.pid),
                    "canonical_runtime_owner": (
                        "TypeScriptQueryEngine"
                        if is_code_worker
                        else "DeploymentNodeRuntime"
                    ),
                    "source_default_api_worker": bool(
                        existing is not None
                        and existing.metadata.get("default_api_worker")
                    ),
                },
            )
        latest = pool_api.pool.store.latest_heartbeat(worker_id)
        pool_api.pool.heartbeat_local_worker(
            worker_id,
            sequence=1 if latest is None else latest.sequence + 1,
            process_uptime_ms=1,
        )
    return {
        "schema": "zyra.production-worker-reconciliation/v1",
        "restart_requested": restart,
        # The dispatch preflight gates on records owned by this exact manager,
        # so recovery evidence must prove both sides observed one instance.
        "process_manager_identity": f"{id(orchestrator.processes):x}",
        "profiles": reconciled_profiles,
        "reconciled_at": now_iso(),
    }


def _production_physical_dispatch_port(
    state: Any,
    binding: Mapping[str, Any],
    operator_task: Mapping[str, Any],
) -> PhysicalDispatchCallPort:
    """Build the real deployment-node port for the selected operator task."""

    location = str(binding.get("selected_location") or "local").casefold()
    permission = dict(
        state.metadata.get("phase2_policy_permission_receipt") or {}
    )
    orchestrator = get_deployment_api().orchestrator
    _sync_configured_provider_environment(orchestrator)
    payload = dict(operator_task)
    # The deployment factory enriches the canonical operator task with the
    # provider route and governed workspace context.  Preserve an explicit
    # commitment to the pre-enrichment task so the production policy can bind
    # the returned digest to both the actual dispatched payload and its
    # orchestrator-owned origin.
    payload["orchestrator_task_digest"] = canonical_digest(operator_task)
    provider_id = ""
    model_id = ""
    execution_budget_ms = 0
    operator_ref = str(payload.get("operator_ref") or "")
    is_model_code_worker = bool(
        str(payload.get("operator_runtime") or "") == "CodeWorkerRuntime"
        and not operator_ref.startswith("worker:local-memory-curator@")
    )
    if is_model_code_worker:
        preferred = _preferred_configured_provider()
        if preferred is None:
            raise RuntimeError(
                "no configured live provider credential is available; expected "
                "ZAI_API_KEY, DEEPSEEK_API_KEY or KIMI_API_KEY"
            )
        provider_id, model_id = preferred
        session_id = f"physical-provider:{state.run_id}:{state.task_id}"
        turn_id = str(
            payload.get("operator_idempotency_key")
            or f"physical-turn:{state.task_id}"
        )
        provider_db = provider_database_path(artifact_root_path())
        route_ref = ProviderRouteBindingRuntime(
            project_root=PROJECT_ROOT,
            database_path=provider_db,
            allow_explicit_sim_bootstrap=False,
        ).bind(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=str(
                (payload.get("physical_worker_binding") or {}).get("node_id")
                or ""
            )
            or None,
            session_id=session_id,
            turn_id=turn_id,
            purpose="phase2-code-worker-task",
            preferred_provider_id=provider_id,
            preferred_model_id=model_id,
            require_tools=True,
            require_streaming=True,
            metadata={
                "runtimeWorker": "CodeWorkerRuntime",
                "physicalTaskExecution": True,
            },
        )
        workspace = get_workspace_manager().config
        payload["provider"] = route_ref.provider_id
        payload["model"] = route_ref.model_id
        payload["code_worker_context"] = {
            "project_root": str(PROJECT_ROOT),
            "artifact_root": str(artifact_root_path()),
            "workspace_manager": {
                "state_root": str(workspace.state_root),
                "data_root": str(workspace.data_root),
                "session_id": str(
                    state.metadata.get("query_session_id")
                    or f"task:{state.task_id}"
                ),
                "local_enabled": workspace.local_enabled,
                "default_backend_id": workspace.default_backend_id,
                "lease_ttl_seconds": workspace.lease_ttl_seconds,
                "reservation_ttl_seconds": workspace.reservation_ttl_seconds,
                "max_receipts": workspace.max_receipts,
            },
            "provider_constraints": route_ref.runtime_constraints(
                database_path=provider_db
            ),
            "model_id": route_ref.model_id,
            "max_turns": _REASONING_MAX_TURNS,
            "model_output_token_limit": 8192,
            # The runtime deadline must expire before the transport deadline so
            # a slow reasoning loop still returns a structured receipt instead
            # of aborting the dispatch connection with an unknown outcome.
            "reasoning_timeout_seconds": _REASONING_RUNTIME_TIMEOUT_SECONDS,
        }
        execution_budget_ms = _REASONING_TRANSPORT_BUDGET_MS
    task = PhysicalDispatchTask(
        run_id=state.run_id,
        task_id=state.task_id,
        payload=payload,
        privacy_class="internal",
        allowed_placements=(location,),
        permission_ref=str(permission.get("decision_id") or ""),
        operation="phase2-operator-execution",
        provider_id=provider_id or "zhipu",
        model_id=model_id or "glm-5.2",
        execution_budget_ms=execution_budget_ms,
    )
    return PhysicalDispatchCallPort(
        task=task,
        catalog=orchestrator.catalog,
        process_manager=orchestrator.processes,
        state_store=orchestrator.store,
        evidence_store=PhysicalDispatchEvidenceStore(
            artifact_root_path() / "physical-dispatch"
        ),
    )


# A live provider reasoning loop runs model -> tool -> observation -> model for
# up to ``_REASONING_MAX_TURNS`` rounds, which routinely exceeds any placement
# latency SLA.  Placement policy caps ``latency_sla_ms`` at 120s because that
# value selects the device/edge/cloud tier, so the execution deadline is budgeted
# separately.  The runtime deadline must stay strictly below the transport
# deadline: a slow loop then fails as a structured receipt rather than aborting
# the dispatch connection and leaving the outcome unknown.
_REASONING_MAX_TURNS = 12
_REASONING_RUNTIME_TIMEOUT_SECONDS = 600.0
_REASONING_TRANSPORT_BUDGET_MS = 780_000

# One physical layer may spend the full 600s reasoning budget plus dispatch and
# recovery overhead, and a task runs several of them.  The validity window has
# to outlast the run it authorizes, not the single layer that mints it.
PHASE2_PERMISSION_RECEIPT_VALIDITY = timedelta(hours=1)

_PROVIDER_ENV_FILES = (
    ("ZAI_API_KEY", ".env.glm.local", "zhipu", "glm-5.2"),
    (
        "DEEPSEEK_API_KEY",
        ".env.deepseek.local",
        "deepseek",
        "deepseek-v4-flash",
    ),
    (
        "KIMI_API_KEY",
        ".env.kimi.local",
        "kimi-platform",
        "kimi-k2.7-code",
    ),
)
_FILE_MANAGED_PROVIDER_ENV: set[str] = set()


def _load_configured_provider_environment() -> tuple[str, ...]:
    """Load only allowlisted provider keys from ignored local env files."""

    if _truthy(
        os.environ.get("ZYRA_DISABLE_LOCAL_PROVIDER_ENV_FILES"),
        default=False,
    ):
        for key in tuple(_FILE_MANAGED_PROVIDER_ENV):
            os.environ.pop(key, None)
            _FILE_MANAGED_PROVIDER_ENV.discard(key)
        return tuple(
            key
            for key, _, _, _ in _PROVIDER_ENV_FILES
            if str(os.environ.get(key) or "").strip()
        )
    configured: list[str] = []
    for key, filename, _, _ in _PROVIDER_ENV_FILES:
        ambient = str(os.environ.get(key) or "").strip()
        path = PROJECT_ROOT / filename
        file_value = (
            _read_allowlisted_env_value(path, key)
            if path.is_file()
            else ""
        )
        if key not in _FILE_MANAGED_PROVIDER_ENV and ambient:
            configured.append(key)
            continue
        if file_value:
            os.environ[key] = file_value
            _FILE_MANAGED_PROVIDER_ENV.add(key)
            configured.append(key)
            continue
        if key in _FILE_MANAGED_PROVIDER_ENV:
            os.environ.pop(key, None)
            _FILE_MANAGED_PROVIDER_ENV.discard(key)
    return tuple(configured)


def _read_allowlisted_env_value(path: Path, expected_name: str) -> str:
    """Read one exact key without importing or expanding an arbitrary env file."""

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        if not separator or name.strip() != expected_name:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        return value.strip()
    return ""


def _preferred_configured_provider() -> tuple[str, str] | None:
    _load_configured_provider_environment()
    for key, _, provider_id, model_id in _PROVIDER_ENV_FILES:
        if str(os.environ.get(key) or "").strip():
            return provider_id, model_id
    return None


def _sync_configured_provider_environment(orchestrator: Any) -> None:
    configured = set(_load_configured_provider_environment())
    for key, _, _, _ in _PROVIDER_ENV_FILES:
        if key not in configured:
            orchestrator.catalog.environment.pop(key, None)
            orchestrator.processes.environment.pop(key, None)
            continue
        value = str(os.environ.get(key) or "")
        orchestrator.catalog.environment[key] = value
        orchestrator.processes.environment[key] = value


def _phase2_communication_outcomes(state: Any) -> tuple[Mapping[str, Any], ...]:
    """Project only canonical event-log communication outcome receipts."""

    events = tuple(get_store().task_events(state.task_id))
    events_by_id = {
        str(
            event.get("event_id")
            if isinstance(event, Mapping)
            else event.event_id
        ): event
        for event in events
    }
    output: list[Mapping[str, Any]] = []
    for event in events:
        event_run_id = str(
            event.get("run_id") if isinstance(event, Mapping) else event.run_id
        )
        event_task_id = str(
            event.get("task_id")
            if isinstance(event, Mapping)
            else event.task_id
        )
        if event_run_id != state.run_id or event_task_id != state.task_id:
            continue
        event_payload = (
            event.get("payload") if isinstance(event, Mapping) else event.payload
        )
        payload = event_payload if isinstance(event_payload, Mapping) else {}
        event_id = str(
            event.get("event_id")
            if isinstance(event, Mapping)
            else event.event_id
        )
        raw = (
            payload.get("communication_outcome")
            if isinstance(payload.get("communication_outcome"), Mapping)
            else payload
        )
        if raw.get("schema_version") != (
            "zyra.agentprune-communication-outcome/v1"
        ):
            continue
        selected = dict(raw)
        # The canonical TypeScript event ingress adds transport identity keys
        # to every legacy payload.  They are outside the owner-signed receipt
        # body and must not change its digest or leak into metric admission.
        for transport_key in ("taskId", "sessionId", "legacyEventType"):
            selected.pop(transport_key, None)
        supplied_digest = str(selected.get("digest") or "")
        unsigned = dict(selected)
        unsigned.pop("digest", None)
        if not supplied_digest or canonical_digest(unsigned) != supplied_digest:
            raise RuntimeError(
                "canonical communication outcome digest is missing or invalid: "
                + event_id
            )
        body_run_id = str(selected.get("run_id") or "")
        body_task_id = str(selected.get("task_id") or "")
        if not body_run_id or not body_task_id:
            # Pre-binding receipts from an older runtime are not admissible
            # to AgentPrune.  They remain in the canonical log, but a fresh
            # scoped communication window must replace them.
            continue
        if body_run_id != event_run_id or body_task_id != event_task_id:
            raise RuntimeError(
                "canonical communication outcome scope mismatch: " + event_id
            )
        completed_at = str(selected.get("completed_at") or "")
        event_created_at = str(
            event.get("created_at")
            if isinstance(event, Mapping)
            else event.created_at
        )
        if not completed_at or completed_at != event_created_at:
            raise RuntimeError(
                "canonical communication outcome time binding mismatch: "
                + event_id
            )
        expected_observation_id = "observation:" + event_id
        if selected.get("observation_id") != expected_observation_id:
            raise RuntimeError(
                "canonical communication outcome identity mismatch: "
                + event_id
            )
        message_id = str(selected.get("message_id") or "")
        if (
            not message_id
            or selected.get("delivery_receipt_ref") != message_id
        ):
            raise RuntimeError(
                "canonical communication delivery receipt binding mismatch: "
                + event_id
            )
        message_event = events_by_id.get(message_id)
        if message_event is None:
            raise RuntimeError(
                "canonical communication delivery receipt is missing: "
                + event_id
            )
        message_run_id = str(
            message_event.get("run_id")
            if isinstance(message_event, Mapping)
            else message_event.run_id
        )
        message_task_id = str(
            message_event.get("task_id")
            if isinstance(message_event, Mapping)
            else message_event.task_id
        )
        message_type = str(
            message_event.get("event_type")
            if isinstance(message_event, Mapping)
            else message_event.event_type
        )
        message_payload_value = (
            message_event.get("payload")
            if isinstance(message_event, Mapping)
            else message_event.payload
        )
        message_payload = (
            dict(message_payload_value)
            if isinstance(message_payload_value, Mapping)
            else {}
        )
        for transport_key in ("taskId", "sessionId", "legacyEventType"):
            message_payload.pop(transport_key, None)
        message_digest = str(message_payload.pop("payload_digest", ""))
        if (
            message_run_id != event_run_id
            or message_task_id != event_task_id
            or message_type != EventType.AGENT_MESSAGE.value
            or message_payload.get("schema")
            != "zyra.phase2-topology-coordination-message/v1"
            or not message_digest
            or canonical_digest(message_payload) != message_digest
            or selected.get("payload_digest") != message_digest
        ):
            raise RuntimeError(
                "canonical communication message receipt is invalid: "
                + event_id
            )
        for field in (
            "run_id",
            "task_id",
            "edge_id",
            "source_node_id",
            "target_node_id",
            "edge_type",
        ):
            if str(selected.get(field) or "") != str(
                message_payload.get(field) or ""
            ):
                raise RuntimeError(
                    "canonical communication outcome/message binding mismatch: "
                    + event_id
                )
        expected_message_id = "event_phase2_message_" + canonical_digest(
            (
                event_run_id,
                str(selected.get("edge_id") or ""),
                str(selected.get("window_id") or "").removeprefix(
                    "topology-route:"
                ),
            )
        )[:24]
        expected_outcome_id = "event_agentprune_outcome_" + canonical_digest(
            (
                "task-scoped-v1",
                event_run_id,
                event_task_id,
                message_id,
                message_digest,
                bool(selected.get("delivered")),
            )
        )[:24]
        if message_id != expected_message_id or event_id != expected_outcome_id:
            raise RuntimeError(
                "canonical communication deterministic identity mismatch: "
                + event_id
            )
        window_id = str(selected.get("window_id") or "")
        window_prefix = "topology-route:"
        window_event_id = (
            window_id[len(window_prefix) :]
            if window_id.startswith(window_prefix)
            else ""
        )
        window_event = events_by_id.get(window_event_id)
        window_payload_value = (
            window_event.get("payload")
            if isinstance(window_event, Mapping)
            else getattr(window_event, "payload", {})
        )
        window_payload = (
            dict(window_payload_value)
            if isinstance(window_payload_value, Mapping)
            else {}
        )
        window_run_id = str(
            window_event.get("run_id")
            if isinstance(window_event, Mapping)
            else getattr(window_event, "run_id", "")
        )
        window_task_id = str(
            window_event.get("task_id")
            if isinstance(window_event, Mapping)
            else getattr(window_event, "task_id", "")
        )
        causal_refs = {
            str(item) for item in selected.get("causal_refs") or () if str(item)
        }
        if (
            window_event is None
            or window_run_id != event_run_id
            or window_task_id != event_task_id
            or window_payload.get("schema")
            != "zyra.phase2-temporal-handoff-receipt/v1"
            or window_payload.get("acknowledged") is not True
            or window_event_id not in causal_refs
        ):
            raise RuntimeError(
                "canonical communication outcome window is invalid: "
                + event_id
            )
        state_delta = message_payload.get("state_delta")
        if (
            not isinstance(state_delta, Mapping)
            or state_delta.get("handoff_ref") != window_event_id
            or state_delta.get("candidate_edge_id")
            != selected.get("edge_id")
            or message_id not in causal_refs
        ):
            raise RuntimeError(
                "canonical communication message causality is invalid: "
                + event_id
            )
        message_created_at = str(
            message_event.get("created_at")
            if isinstance(message_event, Mapping)
            else message_event.created_at
        )
        window_created_at = str(
            window_event.get("created_at")
            if isinstance(window_event, Mapping)
            else window_event.created_at
        )
        try:
            window_time = datetime.fromisoformat(
                window_created_at.replace("Z", "+00:00")
            )
            message_time = datetime.fromisoformat(
                message_created_at.replace("Z", "+00:00")
            )
            outcome_time = datetime.fromisoformat(
                completed_at.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise RuntimeError(
                "canonical communication event time is invalid: " + event_id
            ) from error
        if (
            window_time.tzinfo is None
            or message_time.tzinfo is None
            or outcome_time.tzinfo is None
            or not window_time <= message_time <= outcome_time
        ):
            raise RuntimeError(
                "canonical communication event order is invalid: " + event_id
            )
        output.append(selected)
    return tuple(output)


def _record_phase2_communication_outcomes(
    state: Any,
    candidates: Sequence[Any],
    cause_event: EventRecord | None,
) -> tuple[Mapping[str, Any], ...]:
    """Deliver typed topology coordination messages through the event owner."""

    if (
        cause_event is None
        or cause_event.payload.get("schema")
        != "zyra.phase2-temporal-handoff-receipt/v1"
        or cause_event.payload.get("acknowledged") is not True
    ):
        return ()
    store = get_store()
    persist_events(store, [cause_event])
    existing = _phase2_communication_outcomes(state)
    window_id = "topology-route:" + cause_event.event_id
    existing_edge_ids = {
        str(item.get("edge_id") or "")
        for item in existing
        if str(item.get("window_id") or "") == window_id
    }
    permission = dict(
        state.metadata.get("phase2_policy_permission_receipt") or {}
    )
    permission_ref = str(permission.get("decision_id") or "")
    new_outcomes: list[EventRecord] = []
    for index, candidate in enumerate(candidates):
        source = str(getattr(candidate, "source_node_id", "") or "")
        target = str(getattr(candidate, "target_node_id", "") or "")
        edge_type = str(
            getattr(getattr(candidate, "edge_type", ""), "value", "")
            or getattr(candidate, "edge_type", "")
            or ""
        )
        edge_id = str(getattr(candidate, "edge_id", "") or "")
        if edge_id in existing_edge_ids:
            continue
        message_payload = {
            "schema": "zyra.phase2-topology-coordination-message/v1",
            "run_id": state.run_id,
            "task_id": state.task_id,
            "edge_id": edge_id,
            "source_node_id": source,
            "target_node_id": target,
            "edge_type": edge_type,
            "intent": "topology_coordination",
            "state_delta": {
                "handoff_ref": cause_event.event_id,
                "candidate_edge_id": edge_id,
            },
            "evidence_refs": [cause_event.event_id],
            "artifact_refs": [],
        }
        message_digest = canonical_digest(message_payload)
        message_id = "event_phase2_message_" + canonical_digest(
            (state.run_id, edge_id, cause_event.event_id)
        )[:24]
        message_event = EventRecord(
            event_id=message_id,
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={**message_payload, "payload_digest": message_digest},
        )
        persist_events(store, [message_event])
        stored = {
            str(item.get("event_id") or ""): item
            for item in store.task_events(state.task_id)
            if str(item.get("run_id") or "") == state.run_id
        }.get(message_id)
        stored_payload = (
            dict(stored.get("payload") or {})
            if isinstance(stored, Mapping)
            else {}
        )
        delivered = bool(
            stored_payload.get("payload_digest") == message_digest
            and source
            and target
            and edge_type
        )
        outcome_id = "event_agentprune_outcome_" + canonical_digest(
            (
                "task-scoped-v1",
                state.run_id,
                state.task_id,
                message_id,
                message_digest,
                delivered,
            )
        )[:24]
        message_bytes = len(
            json.dumps(
                message_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        evidence_refs = [cause_event.event_id, message_id]
        outcome_body = {
            "schema_version": "zyra.agentprune-communication-outcome/v1",
            "run_id": state.run_id,
            "task_id": state.task_id,
            "observation_id": "observation:" + outcome_id,
            "window_id": window_id,
            "completed_at": now_iso(),
            "edge_id": edge_id,
            "source_node_id": source,
            "target_node_id": target,
            "edge_type": edge_type,
            "round_index": index,
            "message_id": message_id,
            "payload_digest": message_digest,
            "delivered": delivered,
            "delivery_receipt_ref": message_id,
            "usage_receipt_ref": "",
            "message_bytes": message_bytes,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            # Use the cross-runtime canonical numeric form. JavaScript JSON
            # serializes 0.0 as 0, so signing the integer form keeps the
            # Python owner digest stable after TypeScript event admission.
            "cost_usd": 0,
            "evidence_refs": evidence_refs,
            "utilized_evidence_refs": [],
            "artifact_refs": [],
            "verifier_result": "not_run",
            "failure_count": 0 if delivered else 1,
            "retry_count": 0,
            "permission_result": (
                "allowed"
                if permission.get("effect") == "allow"
                else "denied"
            ),
            "malicious": False,
            "causal_refs": [
                cause_event.event_id,
                message_id,
                *([permission_ref] if permission_ref else []),
            ],
        }
        outcome = {
            **outcome_body,
            "digest": canonical_digest(outcome_body),
        }
        new_outcomes.append(
            EventRecord(
                event_id=outcome_id,
                run_id=state.run_id,
                task_id=state.task_id,
                event_type=EventType.RESOURCE_DECISION,
                created_at=outcome_body["completed_at"],
                payload=outcome,
            )
        )
    if new_outcomes:
        persist_events(store, new_outcomes)
    return _phase2_communication_outcomes(state)


def _phase2_permission_decision(
    state: Any,
    requested: Sequence[str],
    cause_event: EventRecord | None,
) -> Mapping[str, Any]:
    """Resolve strongest-path permissions through the TypeScript owner."""

    request_id = "phase2-permission-" + canonical_digest(
        (state.run_id, state.task_id, tuple(sorted(requested)), (
            cause_event.event_id if cause_event is not None else ""
        ))
    )[:24]
    material = {
        "run_id": state.run_id,
        "task_id": state.task_id,
        "session_id": str(
            state.metadata.get("query_session_id") or f"task:{state.task_id}"
        ),
        "session_revision": 0,
        "worker_request_id": request_id,
        "tool_call_id": request_id,
        "tool_name": "phase2.strongest-control",
        "namespace": "builtin",
        "server_id": "",
        "operation": "phase2.strongest.activate",
        "workspace_root": str(tool_workspace_path().resolve()),
        "arguments": {"requested_permissions": sorted(requested)},
        "metadata": {
            "canonical_permission_owner": "typescript.PermissionCoordinator",
            "python_decision_fallback": False,
            "cause_event_id": (
                cause_event.event_id if cause_event is not None else ""
            ),
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "openWorldHint": False,
                "idempotentHint": True,
            },
        },
        "await_approval_delivery": False,
    }
    port = get_mcp_runtime()
    claimed = dict(port.permission_claim(material))
    response = (
        claimed
        if claimed.get("claimed") is True
        else dict(port.permission_enforce(material))
    )
    decision = (
        dict(response.get("decision") or {})
        if isinstance(response.get("decision"), Mapping)
        else {}
    )
    owner = str(
        response.get("canonical_owner")
        or response.get("canonicalOwner")
        or decision.get("canonical_owner")
        or decision.get("canonicalOwner")
        or ""
    )
    effect = str(
        decision.get("effect") or response.get("effect") or "deny"
    ).casefold()
    receipt = {
        "schema": "zyra.phase2-policy-permission-receipt/v1",
        "run_id": state.run_id,
        "task_id": state.task_id,
        "request_id": str(
            decision.get("request_id")
            or decision.get("requestId")
            or response.get("request_id")
            or request_id
        ),
        "decision_id": str(
            decision.get("decision_id")
            or decision.get("decisionId")
            or response.get("decision_id")
            or request_id
        ),
        "canonical_owner": owner,
        "effect": effect,
        "reason_code": str(
            decision.get("reason_code")
            or decision.get("reasonCode")
            or response.get("reason_code")
            or ""
        ),
        "matched_rule_ids": sorted(
            {
                str(
                    item.get("rule_id")
                    or item.get("ruleId")
                    or ""
                )
                for item in decision.get("matched_rules")
                or decision.get("matchedRules")
                or ()
                if isinstance(item, Mapping)
            }
            - {""}
        ),
        "request_binding": dict(
            decision.get("request_binding")
            or decision.get("requestBinding")
            or {}
        ),
        "allowed_permissions": (
            sorted({str(item) for item in requested})
            if effect == "allow" and "typescript" in owner.casefold()
            else []
        ),
        "cause_event_id": (
            cause_event.event_id if cause_event is not None else ""
        ),
        "owner_response_digest": canonical_digest(response),
        "decided_at": now_iso(),
        # The placement binding is digest-bound to this exact receipt, so the
        # validity window is what bounds how stale a decision may be, not what
        # bounds a task.  Five minutes was shorter than a single live-provider
        # physical layer, so a later layer of a successful long-horizon run
        # arrived with an expired receipt and failed a task nothing had denied.
        # A topology change still mints a new receipt and rebinds placement.
        "valid_until": (
            datetime.now(UTC) + PHASE2_PERMISSION_RECEIPT_VALIDITY
        ).isoformat().replace("+00:00", "Z"),
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    state.metadata["phase2_policy_permission_receipt"] = dict(receipt)
    return receipt


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
        "fault_observation_sink": codeworker_fault_observation_sink(
            get_store(),
            state,
        ),
        "fault_observation_sink_required": True,
    }
    if worker_name == "CodeWorkerRuntime":
        services.update(
            {
                "backend_action_dispatch_port": _code_worker_backend_action_dispatch_port(
                    state,
                    workspace_root=workspace_root,
                ),
                "workspace_isolation_runtime": WorkspaceIsolationRuntime(
                    manager,
                    artifact_store=LocalArtifactStore(artifact_root_path()),
                ),
                "typescript_agent_state_path": str(subagent_state_path()),
            }
        )
    return workspace_root, services


def _code_worker_backend_action_dispatch_port(
    state: Any,
    *,
    workspace_root: str | Path,
    route_sources: Sequence[Mapping[str, Any]] = (),
) -> BackendRegistryActionDispatchPort | None:
    def resolve_route() -> Mapping[str, Any]:
        sources: list[Mapping[str, Any]] = [*route_sources]
        for key in ("provider_route_ref", "provider_route"):
            value = state.metadata.get(key)
            if isinstance(value, Mapping):
                sources.append(value)
        merged: dict[str, Any] = {}
        for source in sources:
            merged.update(dict(source))
        return merged

    port = BackendRegistryActionDispatchPort(
        registry_path=backend_registry_path(artifact_root_path()),
        workspace_root=workspace_root,
        artifact_root=artifact_root_path(),
        route_resolver=resolve_route,
        terminal_dispatch_enabled=lambda: not _task_is_sealed_control(state, {}),
        runtime_worker="CodeWorkerRuntime",
    )
    if port.available_actions():
        return port
    return None


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
    response_contract = direct_response_contract(user_goal)
    if response_contract is not None:
        state.metadata["goal_contract"] = response_contract.to_dict()
        state.metadata["interaction_mode"] = "direct_response"
    state.metadata["delivery_contract"] = goal_delivery_contract(
        user_goal
    ).to_dict()
    event = EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.TASK_CREATED,
        node_id=state.root_node_id,
        payload={"task": to_jsonable(state)},
    )
    return state, event


def _task_execution_failed_event(state: Any, error: BaseException) -> EventRecord:
    code = str(getattr(error, "code", "") or type(error).__name__)
    message = _diagnostic_error_message(error)
    state.status = PlanNodeStatus.BLOCKED
    state.updated_at = now_iso()
    state.metadata["last_execution_error"] = {
        "schema": "zyra.task-execution-error/v1",
        "error": code,
        "message": message,
        "retryable": bool(getattr(error, "retryable", False)),
        "fallback": False,
    }
    return EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        node_id=state.root_node_id,
        event_type=EventType.SYSTEM_NOTICE,
        payload=dict(state.metadata["last_execution_error"]),
    )


def _task_execution_error_response(
    state: Any,
    error: BaseException,
    events: Sequence[EventRecord],
    *,
    error_code: str = "task_execution_failed",
) -> dict[str, Any]:
    """Return a bounded diagnostic envelope; canonical state remains queryable.

    A failed production task can contain thousands of topology, provider and
    worker-pool fields.  Embedding the complete task and event list in a 503
    made the typed client correctly reject the body as oversized, hiding the
    useful server error behind ``response_too_large``.  Persist the full state
    first, then return stable references and the compact failure receipt.
    """

    event_refs = [
        {
            "event_id": str(event.event_id),
            "event_type": str(event.event_type),
        }
        for event in list(events)[-32:]
    ]
    execution_error = state.metadata.get("last_execution_error")
    if not isinstance(execution_error, Mapping):
        execution_error = {
            "schema": "zyra.task-execution-error/v1",
            "error": str(getattr(error, "code", "") or type(error).__name__),
            "message": _diagnostic_error_message(error),
            "retryable": bool(getattr(error, "retryable", False)),
            "fallback": False,
        }
    return {
        "schema": "zyra.task-execution-http-error/v1",
        "error": error_code,
        "message": _diagnostic_error_message(error),
        "task_id": str(state.task_id),
        "run_id": str(state.run_id),
        "task_status": str(state.status),
        "execution_error": dict(execution_error),
        "event_count": len(events),
        "event_refs": event_refs,
        "task_ref": f"/tasks/{state.task_id}",
        "event_stream_ref": f"/tasks/{state.task_id}/events",
        "retryable": bool(getattr(error, "retryable", False)),
        "fallback": False,
    }


def _diagnostic_error_message(error: BaseException) -> str:
    message = str(error).strip()[:4_000] or "task execution failed"
    substitutions = (
        (r"(?i)(Bearer\s+)[^\s,;]+", r"\1<redacted>"),
        (r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted>"),
        (
            r"(?i)\b(api[_-]?key|password|secret|authorization)"
            r"(\s*[:=]\s*)[^\s,;]+",
            r"\1\2<redacted>",
        ),
    )
    for pattern, replacement in substitutions:
        message = re.sub(pattern, replacement, message)
    return message[:1_000]


def persist_events(store: SQLiteStore, events: list[EventRecord]) -> None:
    # The TypeScript spine owns sequence/idempotency and derives the legacy
    # `events` compatibility rows from the committed fact.  The JSONL trajectory is
    # appended only after canonical admission so a failed canonical append can
    # never manufacture a competing fact.
    bridge = get_runtime_event_spine_bridge()
    known_by_task: dict[str, set[str]] = {}
    for event in events:
        known = known_by_task.setdefault(
            event.task_id,
            {
                str(item.get("event_id") or "")
                for item in store.task_events(event.task_id)
            },
        )
        if event.event_id in known:
            continue
        # Legacy EventLog batches were never atomic.  Project each successful
        # canonical commit immediately so a later rejected item cannot leave
        # an already-committed fact missing from the compatibility trajectory.
        bridge.append_legacy_events([event])
        append_jsonl_event(event, event_log_path())
        known.add(event.event_id)


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

    def _prepare_typed_transport(self) -> bool:
        try:
            context = typed_request_context(self.headers)
        except TypedTransportError as error:
            status, body, headers = typed_error_context(error, self.headers)
            self._zyra_typed_response_headers = headers
            self._send_json(status, body, headers=headers)
            return False
        self._zyra_typed_context = context
        self._zyra_typed_response_headers = context.response_headers()
        return True

    def _typed_context(self) -> TypedRequestContext:
        context = getattr(self, "_zyra_typed_context", None)
        if isinstance(context, TypedRequestContext):
            return context
        context = typed_request_context(self.headers)
        self._zyra_typed_context = context
        self._zyra_typed_response_headers = context.response_headers()
        return context

    def _typed_receipts(self) -> TypedReceiptStore:
        store = getattr(self.server, "_zyra_typed_receipt_store", None)
        if isinstance(store, TypedReceiptStore):
            return store
        with _RUNTIME_EVENT_SPINE_LOCK:
            store = getattr(self.server, "_zyra_typed_receipt_store", None)
            if not isinstance(store, TypedReceiptStore):
                store = TypedReceiptStore(sqlite_path())
                setattr(self.server, "_zyra_typed_receipt_store", store)
        return store

    def _begin_typed_receipt(
        self,
        *,
        operation: str,
        path: str,
        payload: Mapping[str, Any],
    ) -> ReceiptReservation | None | bool:
        try:
            start = self._typed_receipts().begin(
                context=self._typed_context(),
                headers=self.headers,
                operation=operation,
                path=path,
                payload=payload,
            )
        except TypedTransportError as error:
            self._send_json(error.status, error.response())
            return False
        if isinstance(start, ReceiptReplay):
            self._send_json(start.status, start.body, headers=start.headers())
            return False
        return start

    def _commit_typed_receipt(
        self,
        reservation: ReceiptReservation | None,
        *,
        status: HTTPStatus,
        body: Mapping[str, Any],
        binding: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, str]] | None:
        try:
            return self._typed_receipts().commit(
                reservation,
                status=status,
                body=body,
                binding=binding,
            )
        except TypedTransportError as error:
            self._send_json(error.status, error.response())
            return None

    def handle_one_request(self) -> None:
        self._zyra_response_started = False
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browser cancellation of SSE/long-poll reads is an expected
            # transport lifecycle event, not a server failure.
            self.close_connection = True
        except RuntimeEventProcessError as error:
            # Canonical event custody is used by most mutating routes.  An
            # unavailable spine must fail closed as a stable HTTP response,
            # never as a dropped socket that hides the owner failure.
            if getattr(self, "_zyra_response_started", False):
                self.close_connection = True
                return
            try:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": error.code,
                        "message": str(error),
                        "fallback": False,
                    },
                )
            except (BrokenPipeError, ConnectionError, OSError):
                self.close_connection = True
        except Exception as error:  # noqa: BLE001 - last-resort HTTP boundary.
            # A route bug or owner failure must still be diagnosable by a
            # client.  Never write a second response after headers started.
            if getattr(self, "_zyra_response_started", False):
                self.close_connection = True
                return
            try:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "schema": "zyra.http-error/v1",
                        "error": "internal_server_error",
                        "message": _diagnostic_error_message(error),
                        "exception_type": type(error).__name__,
                        "retryable": False,
                        "fallback": False,
                    },
                )
            except (BrokenPipeError, ConnectionError, OSError):
                self.close_connection = True

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
                        try:
                            reset_provider_control_client()
                        finally:
                            # The API composition root owns both long-lived MCP
                            # and edge-worker child processes.  ThreadingHTTPServer
                            # shutdown must release them before a cleanroom can
                            # delete its exact-commit workspace on Windows.
                            try:
                                reset_mcp_runtime()
                            finally:
                                try:
                                    reset_terminal_api()
                                finally:
                                    try:
                                        reset_worker_pool_api()
                                    finally:
                                        try:
                                            reset_scenario_runner_api(wait=False)
                                        finally:
                                            try:
                                                reset_experiment_api(wait=False)
                                            finally:
                                                reset_deployment_api()
                                                reset_policy_metric_api()

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
                            console_response = payload.get("console_response")
                            if console_response is not None and not isinstance(console_response, dict):
                                raise ValueError("console_response must be an object")
                            receipt = get_mcp_runtime().permission_respond(
                                request_id,
                                str(payload.get("effect") or ""),
                                responder=authority.actor_id,
                                response_id=str(payload.get("response_id") or payload.get("idempotency_key") or ""),
                                metadata={
                                    "authority_id": authority.authority_id,
                                    "channel": str(authority.channel),
                                    "custody_verified": True,
                                    "console_response": dict(console_response or {}),
                                    "display_responder": str(
                                        payload.get("display_responder") or ""
                                    )[:256],
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
                            "expected_revision": (
                                payload.get("expected_revision")
                                if payload.get("expected_revision") is not None
                                else payload.get("expected_mode_revision")
                            ),
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
        self._zyra_response_started = True
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = _path_parts(parsed.path)
        if (
            len(parts) == 5
            and parts[0] == "tasks"
            and parts[2] == "terminals"
            and parts[4] == "connect"
        ):
            query = _flatten_query(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            try:
                TerminalWebSocketHandler(get_terminal_api().registry).upgrade(
                    self,
                    task_id=parts[1],
                    terminal_id=parts[3],
                    ticket=str(query.get("ticket") or ""),
                    cursor=int(query.get("cursor") or 0),
                    protocol=str(
                        query.get("protocol") or "zyra.terminal.v1"
                    ),
                )
            except (TypeError, ValueError):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "terminal_cursor_invalid",
                        "message": "Terminal WebSocket cursor is invalid.",
                        "fallback": False,
                    },
                )
            except TerminalError as error:
                try:
                    status = HTTPStatus(error.status)
                except ValueError:
                    status = HTTPStatus.BAD_REQUEST
                self._send_json(status, error.response())
            return
        if not self._prepare_typed_transport():
            return
        if parts == ["schema", "policy-contracts"]:
            self._send_json(
                HTTPStatus.OK,
                policy_contract_schema_catalog(),
                headers={"Cache-Control": "public, max-age=300"},
            )
            return
        store = get_store()

        policy_metric_response = get_policy_metric_api(
            PROJECT_ROOT,
            event_api=get_runtime_event_api(),
            artifact_root=artifact_root_path(),
        ).route_get(
            tuple(parts),
            _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
        )
        if policy_metric_response is not None:
            self._send_json(
                policy_metric_response.status,
                policy_metric_response.body,
                headers=dict(policy_metric_response.headers),
            )
            return

        deployment_response = get_deployment_api().route_get(
            tuple(parts),
            _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
        )
        if deployment_response is not None:
            self._send_json(
                deployment_response.status,
                deployment_response.body,
                headers=dict(deployment_response.headers),
            )
            return

        scenario_response = get_scenario_runner_api().route_get(
            tuple(parts),
            _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
        )
        if scenario_response is not None:
            self._send_json(
                scenario_response.status,
                scenario_response.body,
                headers=dict(scenario_response.headers),
            )
            return

        experiment_response = get_experiment_api().route_get(
            tuple(parts),
            _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
        )
        if experiment_response is not None:
            self._send_json(
                experiment_response.status,
                experiment_response.body,
                headers=dict(experiment_response.headers),
            )
            return

        if (
            len(parts) in {3, 4}
            and parts[0] == "tasks"
            and parts[2] == "terminals"
        ):
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            query = _flatten_query(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            try:
                response = (
                    get_terminal_api().list(
                        task_id=state.task_id,
                        include_closed=str(
                            query.get("include_closed") or ""
                        ).casefold()
                        in {"1", "true", "yes", "on"},
                    )
                    if len(parts) == 3
                    else get_terminal_api().get(
                        task_id=state.task_id,
                        terminal_id=parts[3],
                    )
                )
            except TerminalError as error:
                try:
                    status = HTTPStatus(error.status)
                except ValueError:
                    status = HTTPStatus.BAD_REQUEST
                self._send_json(status, error.response())
                return
            self._send_json(
                response.status,
                response.body,
                headers=dict(response.headers),
            )
            return

        hardening_response = get_m1_hardening_api().handle_get(
            tuple(parts),
            _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
        )
        if hardening_response is not None:
            self._send_json(
                hardening_response.status,
                dict(hardening_response.body),
                headers=dict(hardening_response.headers),
            )
            return

        if self._handle_permission_get(parsed=parsed, parts=parts, store=store):
            return

        if (
            len(parts) == 4
            and parts[0] == "tasks"
            and parts[2] == "event-ingress"
        ):
            task_id = parts[1]
            operation = parts[3]
            query = _flatten_query(
                parse_qs(parsed.query, keep_blank_values=True)
            )
            ingress = get_event_ingress_api()
            try:
                if operation == "capabilities":
                    result = ingress.capabilities(task_id, query)
                    self._send_json(
                        result.status,
                        dict(result.body),
                        headers=dict(result.headers),
                    )
                    return
                if operation == "snapshot":
                    result = ingress.snapshot(task_id, query)
                    self._send_json(
                        result.status,
                        dict(result.body),
                        headers=dict(result.headers),
                    )
                    return
                if operation == "delta":
                    result = ingress.delta(task_id, query)
                    self._send_json(
                        result.status,
                        dict(result.body),
                        headers=dict(result.headers),
                    )
                    return
                if operation == "sse":
                    self._send_sse_stream(ingress.sse(task_id, query))
                    return
            except (EventIngressError, RuntimeEventContractError) as error:
                if isinstance(error, EventIngressError):
                    status = error.status
                    body = error.response()
                else:
                    status = HTTPStatus.BAD_REQUEST
                    body = {
                        "schema": "zyra.event-ingress/v1",
                        "ok": False,
                        "error": "event_ingress_contract_error",
                        "message": str(error),
                        "retryable": False,
                        "resyncRequired": False,
                        "canonicalWriteAllowed": False,
                    }
                self._send_json(status, body)
                return
            except RuntimeEventProcessError as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "schema": "zyra.event-ingress/v1",
                        "ok": False,
                        "error": error.code,
                        "message": str(error),
                        "retryable": True,
                        "resyncRequired": False,
                        "canonicalWriteAllowed": False,
                    },
                )
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "schema": "zyra.event-ingress/v1",
                    "ok": False,
                    "error": "event_ingress_operation_not_found",
                    "message": f"Unknown event ingress operation: {operation}",
                    "retryable": False,
                    "resyncRequired": False,
                    "canonicalWriteAllowed": False,
                },
            )
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

        recovery_response = get_recovery_runtime_api(store).route_get(
            tuple(parts),
            _flatten_query(parse_qs(parsed.query, keep_blank_values=True)),
        )
        if recovery_response is not None:
            self._send_json(
                recovery_response.status,
                dict(recovery_response.body),
                headers=dict(recovery_response.headers),
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
            provider_database=runtime_configuration().path("state.provider"),
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
                    "status": "ok",
                    "service": "zyra-api",
                    "phase": "m5-resource-scheduler-fault-recovery",
                    "api_version": API_VERSION,
                    "process_id": os.getpid(),
                    "cli_daemon_generation": (
                        os.environ.get("ZYRA_CLI_DAEMON_GENERATION") or None
                    ),
                    "capabilities": [
                        "typed_transport",
                        "idempotent_task_lifecycle",
                        "request_correlation",
                        "opaque_task_cursor",
                    ],
                    "event_log": str(event_log_path()),
                    "sqlite": str(sqlite_path()),
                    "tool_workspace": str(tool_workspace_path()),
                    "artifact_root": str(artifact_root_path()),
                    "permission_store": str(permission_store_path()),
                    "workspace": get_workspace_manager().health(),
                },
            )
            return

        if parts == ["runtime", "readiness"]:
            owner_readiness, readiness_details = runtime_readiness_probes(
                typed_receipts=self._typed_receipts(),
            )
            payload = runtime_readiness_payload(
                owner_readiness,
                details=readiness_details,
            )
            canonical = readiness_details.get("canonical_runtime_owners")
            canonical_ready = (
                isinstance(canonical, Mapping)
                and canonical.get("ready") is True
            )
            canonical_blockers = (
                list(canonical.get("blockers") or [])
                if isinstance(canonical, Mapping)
                else ["runtime_owner_composition"]
            )
            if not canonical_ready:
                payload["ready"] = False
                payload["status"] = "blocked"
                payload["blockers"] = list(
                    dict.fromkeys(
                        [*payload.get("blockers", []), *canonical_blockers]
                    )
                )
            payload["canonical_owners"] = canonical
            self._send_json(
                HTTPStatus.OK,
                payload,
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

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "command-queue":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            query = _flatten_query(parse_qs(parsed.query, keep_blank_values=True))
            session_id = str(
                query.get("session_id")
                or state.metadata.get("query_session_id")
                or state.metadata.get("session_id")
                or f"task:{state.task_id}"
            )
            include_terminal = str(query.get("include_terminal") or "").lower() in {
                "1",
                "true",
                "yes",
            }
            queue = get_control_dispatcher().prompt_queue
            entries = [
                item.safe_dict()
                for item in queue.queued(
                    session_id=session_id,
                    include_terminal=include_terminal,
                )
                if (
                    str(
                        dict(item.payload.get("request") or {}).get("task_id")
                        or state.task_id
                    )
                    == state.task_id
                )
            ]
            self._send_json(
                HTTPStatus.OK,
                {
                    "schema": "zyra.command-queue/v1",
                    "task_id": state.task_id,
                    "run_id": state.run_id,
                    "session_id": session_id,
                    "sequence": int(queue.snapshot().get("sequence") or 0),
                    "entries": entries,
                    "canonical_owner": "PromptQueueRuntime",
                    "projection_owner": "CanonicalProjectionStore",
                },
                headers={"Cache-Control": "no-store, max-age=0"},
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
            try:
                client = CodeWorkerSidecarClient(PROJECT_ROOT)
                payload = client.runtime_inventory()
                health = client.health()
            except (OSError, RuntimeError, ValueError) as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "ok": False,
                        "error": "code_worker_runtime_unavailable",
                        "message": str(exc),
                        "canonicalOwner": "typescript",
                        "fallbackUsed": False,
                    },
                )
                return
            payload["health"] = health
            payload["processConfiguration"] = health.get("processConfiguration", {})
            payload["apiRoute"] = "/workers/code/inventory"
            payload["defaultRoute"] = True
            payload["fallbackUsed"] = False
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
            try:
                payload = CodeWorkerSidecarClient(PROJECT_ROOT).session_contract()
            except (OSError, RuntimeError, ValueError) as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "ok": False,
                        "error": "code_worker_runtime_unavailable",
                        "message": str(exc),
                        "canonicalOwner": "typescript",
                        "fallbackUsed": False,
                    },
                )
                return
            payload["apiRoute"] = "/workers/code/session-foundation"
            payload["contractSurface"] = "query-session"
            payload["defaultRoute"] = True
            payload["fallbackUsed"] = False
            self._send_json(HTTPStatus.OK, payload)
            return

        if parts == ["workers", "code", "session-integration"]:
            try:
                client = CodeWorkerSidecarClient(PROJECT_ROOT)
                query_contract = client.query_contract()
                session_contract = client.session_contract()
            except (OSError, RuntimeError, ValueError) as exc:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "ok": False,
                        "error": "code_worker_runtime_unavailable",
                        "message": str(exc),
                        "canonicalOwner": "typescript",
                        "fallbackUsed": False,
                    },
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    **query_contract,
                    "apiRoute": "/workers/code/session-integration",
                    "contractSurface": "query-session-integration",
                    "sessionContract": session_contract,
                    "defaultRoute": True,
                    "fallbackUsed": False,
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
            try:
                page = paginate_tasks(
                    store.list_tasks(),
                    parse_qs(parsed.query, keep_blank_values=True),
                )
            except TypedTransportError as error:
                self._send_json(error.status, error.response())
                return
            self._send_json(HTTPStatus.OK, page)
            return

        if parts == ["sessions"]:
            try:
                page = paginate_sessions(
                    store.list_tasks(),
                    parse_qs(parsed.query, keep_blank_values=True),
                )
            except TypedTransportError as error:
                self._send_json(error.status, error.response())
                return
            self._send_json(HTTPStatus.OK, page)
            return

        if len(parts) == 2 and parts[0] == "sessions":
            try:
                body = session_detail(store.list_tasks(), unquote(parts[1]))
            except KeyError:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "session_not_found", "session_id": unquote(parts[1])},
                )
                return
            self._send_json(HTTPStatus.OK, body)
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

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "loopx":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "task_not_found"},
                )
                return
            goal_id = str(
                _flatten_query(
                    parse_qs(parsed.query, keep_blank_values=True)
                ).get("goal_id")
                or task_goal_id(state.task_id)
            )
            try:
                projection = _loopx_task_projection(
                    state,
                    get_loopx_control_runtime().snapshot(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        goal_id=goal_id,
                    ),
                )
            except Exception as error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "schema": "zyra.loopx-control-state/v1",
                        "runtime": {
                            "version": LOOPX_VERSION,
                            "source_commit": LOOPX_SOURCE_COMMIT,
                            "source_tree_commit": LOOPX_SOURCE_TREE_COMMIT,
                            "source_digest": "",
                            "source_kind": "embedded_source",
                            "archive_fallback": False,
                        },
                        "workspace_id": "",
                        "run_id": state.run_id,
                        "task_id": state.task_id,
                        "goal_id": goal_id,
                        "lifecycle": "degraded",
                        "connected": False,
                        "degraded": True,
                        "error": {
                            "code": getattr(
                                error,
                                "code",
                                "loopx_runtime_unavailable",
                            ),
                            "message": str(error),
                            "recovery": "restore_embedded_runtime_and_retry",
                        },
                        "private_state": {
                            "owner": "LoopX",
                            "goal": {"goal_id": goal_id},
                            "todos": [],
                            "claims": [],
                            "quota": {},
                            "history": [],
                        },
                        "canonical_state": {
                            "task_owner": "Zyra orchestration/runtime",
                            "worker_lease_owner": "WorkerLeaseManager",
                            "execution_budget_owner": "ResourceScheduler",
                            "loopx_claim_is_worker_lease": False,
                            "loopx_quota_is_execution_budget": False,
                        },
                        "sync": {
                            "pending": 0,
                            "acked": 0,
                            "dead_letter": 0,
                            "cursor": 0,
                            "workspace_cursor": 0,
                            "records": [],
                        },
                        "continuation": {
                            "allowed": False,
                            "interaction_contract": {},
                        },
                        "last_validated_receipt": {},
                        "last_sync_receipt": {},
                    },
                )
                return
            self._send_json(
                HTTPStatus.OK,
                projection,
                headers={"Cache-Control": "no-store, max-age=0"},
            )
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

        if len(parts) >= 4 and parts[0] == "tasks" and parts[2] == "diff-reviews":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            artifact_id = parts[3]
            artifact = find_task_artifact(state.artifacts, artifact_id)
            if artifact is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": "artifact_not_found",
                        "task_id": state.task_id,
                        "artifact_id": artifact_id,
                    },
                )
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            revision = _optional_query_value(query, "revision") or ""
            try:
                if len(parts) == 4:
                    response = get_diff_review_api().manifest(
                        task_id=state.task_id,
                        run_id=state.run_id,
                        artifact=artifact,
                        revision=revision,
                    )
                elif (
                    len(parts) == 7
                    and parts[4] == "files"
                    and parts[6] == "hunks"
                ):
                    response = get_diff_review_api().page(
                        task_id=state.task_id,
                        run_id=state.run_id,
                        artifact=artifact,
                        file_id=parts[5],
                        revision=revision,
                        page_index=max(
                            0,
                            int(_optional_query_value(query, "page") or 0),
                        ),
                        maximum_bytes=max(
                            1_024,
                            min(
                                8 * 1_024 * 1_024,
                                int(
                                    _optional_query_value(query, "maximum_bytes")
                                    or 8 * 1_024 * 1_024
                                ),
                            ),
                        ),
                        maximum_lines=max(
                            1,
                            min(
                                100_000,
                                int(
                                    _optional_query_value(query, "maximum_lines")
                                    or 100_000
                                ),
                            ),
                        ),
                    )
                elif (
                    len(parts) == 7
                    and parts[4] == "files"
                    and parts[6] == "content"
                ):
                    response = get_diff_review_api().file_content(
                        task_id=state.task_id,
                        run_id=state.run_id,
                        artifact=artifact,
                        file_id=parts[5],
                        revision=revision,
                        version=_optional_query_value(query, "version") or "",
                    )
                else:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "diff_review_route_not_found", "path": parsed.path},
                    )
                    return
            except (DiffReviewApiError, ArtifactApiError) as error:
                status = HTTPStatus(getattr(error, "status", HTTPStatus.BAD_REQUEST))
                self._send_json(status, error.response())
                return
            except (ValueError, WorkspaceError) as error:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "diff_review_request_invalid",
                        "message": str(error),
                        "fallback": False,
                    },
                )
                return
            self._send_json(
                HTTPStatus(response.status),
                dict(response.body),
                headers=dict(response.headers),
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "artifacts":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            try:
                catalog_query = ArtifactCatalogQuery.from_query(
                    task_id=parts[1],
                    query=parse_qs(parsed.query, keep_blank_values=True),
                )
                payload = artifact_catalog_service().catalog(
                    artifacts=state.artifacts,
                    query=catalog_query,
                )
            except ArtifactApiError as error:
                self._send_json(HTTPStatus(error.status), error.response())
                return
            self._send_json(HTTPStatus.OK, payload)
            return

        if len(parts) >= 4 and parts[0] == "tasks" and parts[2] == "artifacts":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            artifact = find_task_artifact(state.artifacts, parts[3])
            if artifact is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": "artifact_not_found",
                        "task_id": parts[1],
                        "artifact_id": parts[3],
                    },
                )
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            service = artifact_catalog_service()
            try:
                read_query = ArtifactReadQuery.from_query(
                    task_id=parts[1],
                    artifact_id=parts[3],
                    query=query,
                )
                if len(parts) == 4:
                    payload = service.metadata(
                        task_id=parts[1],
                        artifact=artifact,
                        expected_revision=read_query.expected_revision,
                    )
                    self._send_json(HTTPStatus.OK, payload)
                    return
                if len(parts) == 5 and parts[4] == "content":
                    payload = service.read(artifact=artifact, query=read_query)
                    self._send_json(HTTPStatus.OK, payload)
                    return
                if len(parts) == 5 and parts[4] == "download":
                    observed, selected, receipt = service.download(
                        artifact=artifact,
                        query=read_query,
                    )
                    self._send_bytes(
                        HTTPStatus.PARTIAL_CONTENT
                        if not selected.complete or selected.offset > 0
                        else HTTPStatus.OK,
                        selected.content,
                        content_type=observed.content_type,
                        headers={
                            "Accept-Ranges": "bytes",
                            "Content-Range":
                                f"bytes {selected.offset}-{max(selected.offset, selected.end_exclusive - 1)}"
                                f"/{selected.total_bytes}",
                            "Content-Disposition":
                                f'attachment; filename="{_safe_download_name(artifact)}"',
                            "X-Zyra-Artifact-Revision": observed.revision,
                            "X-Zyra-Artifact-Sha256": observed.sha256,
                            "X-Zyra-Artifact-Receipt": str(receipt.get("receipt_digest") or ""),
                            "Cache-Control": "private, no-store, max-age=0",
                        },
                    )
                    return
                if len(parts) == 5 and parts[4] == "receipts":
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "schema": "zyra.artifact-read-audit.v1",
                            "task_id": parts[1],
                            "artifact_id": parts[3],
                            "receipts": _ARTIFACT_READ_AUDIT.entries(
                                task_id=parts[1],
                                artifact_id=parts[3],
                            ),
                        },
                    )
                    return
            except ArtifactApiError as error:
                try:
                    status = HTTPStatus(error.status)
                except ValueError:
                    status = HTTPStatus.BAD_REQUEST
                self._send_json(status, error.response())
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": parsed.path})
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
        if not self._prepare_typed_transport():
            return
        parsed = urlparse(self.path)
        parts = _path_parts(parsed.path)
        store = get_store()
        try:
            payload = self._read_json_body()
        except JsonRequestError as error:
            self._send_json(error.status, {"error": error.code, "message": error.message})
            return

        if (
            len(parts) == 4
            and parts[0] == "tasks"
            and parts[2] == "loopx"
            and parts[3] == "commands"
        ):
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "task_not_found"},
                )
                return
            action = str(payload.get("action") or "").strip().lower()
            command_names = {
                "connect": "/loopx-connect",
                "disconnect": "/loopx-disconnect",
                "claim": "/loopx-claim",
                "release": "/loopx-release",
                "interaction_submit": "/loopx-interaction",
                "sync_retry": "/loopx-retry",
            }
            command_name = command_names.get(action)
            if command_name is None:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "schema": "zyra.loopx-control-error/v1",
                        "error": "loopx_action_unsupported",
                        "action": action,
                    },
                )
                return
            reservation = self._begin_typed_receipt(
                operation="task.loopx.command",
                path=parsed.path,
                payload=payload,
            )
            if reservation is False:
                return
            command_payload = {
                **payload,
                "text": command_name,
                "arguments": {
                    **dict(payload.get("arguments") or {}),
                    **{
                        key: value
                        for key, value in payload.items()
                        if key
                        not in {
                            "action",
                            "arguments",
                            "text",
                        }
                    },
                },
            }
            request = _control_command_request_from_text(
                state,
                command_name,
                command_payload,
            )
            if request is None:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "loopx_command_invalid"},
                )
                return
            response = get_control_dispatcher().submit(
                request,
                _control_context_for_task(state, store),
            )
            latest = store.load_task(state.task_id) or state
            result = dict(response.result.data or {})
            projection = result.get("state")
            body = {
                "schema": "zyra.loopx-control-result/v1",
                "ok": response.ok,
                "action": action,
                "control_request": request.to_dict(),
                "command_result": response.to_dict(),
                "receipt": dict(result.get("receipt") or {}),
                "sync_receipt": dict(result.get("receipt") or {}),
                "state": (
                    _loopx_task_projection(latest, projection)
                    if isinstance(projection, Mapping)
                    else _loopx_task_projection(
                        latest,
                        get_loopx_control_runtime().snapshot(
                            run_id=latest.run_id,
                            task_id=latest.task_id,
                        ),
                    )
                ),
            }
            receipt_status = str(
                body["sync_receipt"].get("status") or ""
            )
            body["ok"] = bool(
                response.ok
                and receipt_status
                not in {
                    "claim_conflict",
                    "dead_letter",
                    "sync_degraded",
                }
            )
            status = (
                HTTPStatus.CREATED
                if body["ok"]
                else HTTPStatus.CONFLICT
            )
            committed_response = self._commit_typed_receipt(
                reservation,
                status=status,
                body=body,
                binding={
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                },
            )
            if committed_response is None:
                return
            committed_body, receipt_headers = committed_response
            self._send_json(
                status,
                committed_body,
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    **receipt_headers,
                },
            )
            return

        if parts == ["deployment", "shutdown"]:
            try:
                shutdown_caller_is_local = ipaddress.ip_address(
                    self.client_address[0]
                ).is_loopback
            except ValueError:
                shutdown_caller_is_local = False
            if not shutdown_caller_is_local:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {
                        "schema": "zyra.deployment-error/v1",
                        "error": "deployment_shutdown_remote_forbidden",
                        "message": "Deployment shutdown is restricted to the local supervisor.",
                        "fallback": False,
                    },
                    headers={"Cache-Control": "no-store"},
                )
                return
            self._send_json(
                HTTPStatus.ACCEPTED,
                {
                    "schema": "zyra.deployment-api-shutdown/v1",
                    "accepted": True,
                    "fallback": False,
                },
                headers={"Cache-Control": "no-store"},
            )
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        deployment_response = get_deployment_api().route_post(
            tuple(parts),
            payload,
        )
        if deployment_response is not None:
            self._send_json(
                deployment_response.status,
                deployment_response.body,
                headers=dict(deployment_response.headers),
            )
            return

        scenario_response = get_scenario_runner_api().route_post(
            tuple(parts),
            payload,
            actor_id=self._permission_actor_id(),
        )
        if scenario_response is not None:
            self._send_json(
                scenario_response.status,
                scenario_response.body,
                headers=dict(scenario_response.headers),
            )
            return

        experiment_response = get_experiment_api().route_post(
            tuple(parts),
            payload,
            actor_id=self._permission_actor_id(),
        )
        if experiment_response is not None:
            self._send_json(
                experiment_response.status,
                experiment_response.body,
                headers=dict(experiment_response.headers),
            )
            return

        if (
            len(parts) in {3, 5}
            and parts[0] == "tasks"
            and parts[2] == "terminals"
        ):
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            reservation: ReceiptReservation | None | bool = None
            try:
                if len(parts) == 3:
                    reservation = self._begin_typed_receipt(
                        operation="terminal.create",
                        path=parsed.path,
                        payload=payload,
                    )
                    if reservation is False:
                        return
                    response = get_terminal_api().create(
                        task_id=state.task_id,
                        run_id=state.run_id,
                        payload=payload,
                        actor_id=self._permission_actor_id(),
                    )
                    committed = self._commit_typed_receipt(
                        reservation,
                        status=response.status,
                        body=response.body,
                        binding={
                            "session_id": str(payload.get("session_id") or ""),
                            "run_id": state.run_id,
                            "task_id": state.task_id,
                        },
                    )
                    if committed is None:
                        return
                    body, receipt_headers = committed
                    self._send_json(
                        response.status,
                        body,
                        headers={
                            **dict(response.headers),
                            **receipt_headers,
                        },
                    )
                    return
                operation = parts[4]
                terminal_id = parts[3]
                if operation == "ticket":
                    response = get_terminal_api().ticket(
                        task_id=state.task_id,
                        terminal_id=terminal_id,
                        run_id=state.run_id,
                        payload=payload,
                        origin=str(self.headers.get("Origin") or ""),
                    )
                    self._send_json(
                        response.status,
                        response.body,
                        headers=dict(response.headers),
                    )
                    return
                if operation == "kill":
                    reservation = self._begin_typed_receipt(
                        operation="terminal.kill",
                        path=parsed.path,
                        payload=payload,
                    )
                    if reservation is False:
                        return
                    response = get_terminal_api().kill(
                        task_id=state.task_id,
                        terminal_id=terminal_id,
                        run_id=state.run_id,
                        payload=payload,
                        actor_id=self._permission_actor_id(),
                    )
                    committed = self._commit_typed_receipt(
                        reservation,
                        status=response.status,
                        body=response.body,
                        binding={
                            "session_id": str(payload.get("session_id") or ""),
                            "run_id": state.run_id,
                            "task_id": state.task_id,
                        },
                    )
                    if committed is None:
                        return
                    body, receipt_headers = committed
                    self._send_json(
                        response.status,
                        body,
                        headers={
                            **dict(response.headers),
                            **receipt_headers,
                        },
                    )
                    return
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": "terminal_route_not_found",
                        "path": parsed.path,
                    },
                )
                return
            except TerminalError as error:
                if isinstance(reservation, ReceiptReservation):
                    self._typed_receipts().abandon(reservation)
                try:
                    status = HTTPStatus(error.status)
                except ValueError:
                    status = HTTPStatus.BAD_REQUEST
                self._send_json(status, error.response())
                return

        hardening_response = get_m1_hardening_api().handle_post(
            tuple(parts),
            payload,
            task_loader=lambda task_id: _hardening_task_payload(store, task_id),
            event_loader=lambda task_id: tuple(store.task_events(task_id)),
            base_url=f"http://127.0.0.1:{self.server.server_address[1]}",
        )
        if hardening_response is not None:
            self._send_json(
                hardening_response.status,
                dict(hardening_response.body),
                headers=dict(hardening_response.headers),
            )
            return

        recovery_response = get_recovery_runtime_api(store).route_post(tuple(parts), payload)
        if recovery_response is not None:
            self._send_json(
                recovery_response.status,
                dict(recovery_response.body),
                headers=dict(recovery_response.headers),
            )
            return

        if self._handle_permission_post(parts=parts, payload=payload, store=store):
            return

        if len(parts) >= 5 and parts[0] == "tasks" and parts[2] == "diff-reviews":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            try:
                if (
                    len(parts) == 5
                    and parts[3] != "transactions"
                    and parts[4] in {"comments", "apply"}
                ):
                    artifact = find_task_artifact(state.artifacts, parts[3])
                    if artifact is None:
                        self._send_json(
                            HTTPStatus.NOT_FOUND,
                            {
                                "error": "artifact_not_found",
                                "task_id": state.task_id,
                                "artifact_id": parts[3],
                            },
                        )
                        return
                    response = (
                        get_diff_review_api().review(
                            task_id=state.task_id,
                            run_id=state.run_id,
                            artifact=artifact,
                            payload=payload,
                        )
                        if parts[4] == "comments"
                        else get_diff_review_api().apply(
                            task_id=state.task_id,
                            run_id=state.run_id,
                            artifact=artifact,
                            payload=payload,
                        )
                    )
                elif (
                    len(parts) == 6
                    and parts[3] == "transactions"
                    and parts[5] == "rollback"
                ):
                    response = get_diff_review_api().rollback(
                        task_id=state.task_id,
                        run_id=state.run_id,
                        transaction_id=parts[4],
                        payload=payload,
                    )
                else:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "diff_review_route_not_found", "path": parsed.path},
                    )
                    return
            except (DiffReviewApiError, ArtifactApiError) as error:
                status = HTTPStatus(getattr(error, "status", HTTPStatus.BAD_REQUEST))
                self._send_json(status, error.response())
                return
            except WorkspaceError as error:
                mapped = workspace_error_response(error)
                self._send_json(mapped.status, mapped.body, headers=dict(mapped.headers))
                return
            if response.events:
                persist_events(store, list(response.events))
            response_headers = dict(response.headers)
            receipt_id = str(response.body.get("receipt_id") or "")
            if receipt_id:
                response_headers["X-Zyra-Receipt-Id"] = receipt_id
            if response.body.get("idempotent_replay") is True:
                response_headers["X-Zyra-Receipt-Replayed"] = "true"
            self._send_json(
                HTTPStatus(response.status),
                dict(response.body),
                headers=response_headers,
            )
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
            provider_database=runtime_configuration().path("state.provider"),
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
            receipt_reservation = self._begin_typed_receipt(
                operation="task.create",
                path=parsed.path,
                payload=payload,
            )
            if receipt_reservation is False:
                return
            state, created_event = make_task_created_event(user_goal)
            session_id = str(payload.get("session_id") or f"task:{state.task_id}")
            state.metadata["query_session_id"] = session_id
            requested_mode = str(
                payload.get("competition_mode")
                or payload.get("execution_mode")
                or ""
            ).strip()
            requested_sealed = bool(
                payload.get("sealed")
                or payload.get("sealed_autonomous")
                or payload.get("formal_benchmark")
                or "sealed" in requested_mode.casefold()
            )
            if requested_sealed:
                state.metadata["sealed"] = True
                state.metadata["sealed_autonomous"] = True
                state.metadata["competition_mode"] = "sealed_autonomous"
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
                self._typed_receipts().abandon(receipt_reservation)
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
                if not auto_run:
                    pool_api.acquire_for_task(
                        state,
                        payload=(
                            payload.get("worker_pool")
                            if isinstance(payload.get("worker_pool"), dict)
                            else {}
                        ),
                    )
                # Auto-run graph custody is intentionally deferred to
                # Phase2StrongestProductionBridge.  graph_execution_context()
                # first reconciles the truthful deployment-node identities;
                # materializing the graph here would leave its precompiled
                # nodes without a worker binding and CARD would correctly
                # reject those unowned ARG predecessors.
            except Exception as error:
                failure_event = _task_execution_failed_event(state, error)
                events.append(failure_event)
                try:
                    pool_api.finalize_task(
                        state,
                        success=False,
                        summary="worker pool initialization failed",
                    )
                except Exception:  # noqa: BLE001 - preserve the primary failure.
                    pass
                persist_events(store, events)
                store.save_checkpoint(state)
                response_body = _task_execution_error_response(
                    state,
                    error,
                    events,
                    error_code="worker_pool_initialization_failed",
                )
                committed = self._commit_typed_receipt(
                    receipt_reservation,
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    body=response_body,
                    binding={
                        "session_id": session_id,
                        "run_id": state.run_id,
                        "task_id": state.task_id,
                    },
                )
                if committed is None:
                    return
                response_body, receipt_headers = committed
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    response_body,
                    headers=receipt_headers,
                )
                return
            if auto_run:
                try:
                    if requested_sealed:
                        prepare_phase2_loopx_pre_control(
                            state,
                            causation_id=created_event.event_id,
                        )
                    events.extend(
                        run_task_graph(
                            state,
                            execution_context=graph_execution_context(),
                        )
                    )
                    pool_api.finalize_task(
                        state,
                        success=str(state.status) == "completed",
                        summary=(
                            "default task graph finished with status "
                            f"{state.status}"
                        ),
                    )
                except Exception as error:  # noqa: BLE001 - HTTP boundary must stay structured.
                    failure_event = _task_execution_failed_event(state, error)
                    events.append(failure_event)
                    try:
                        pool_api.finalize_task(
                            state,
                            success=False,
                            summary="task execution failed before completion",
                        )
                    except Exception:  # noqa: BLE001 - preserve the primary failure.
                        pass
                    events.extend(
                        event
                        for event in pool_api.pool.events.project_after(
                            pool_api.pool.store,
                            pool_sequence,
                        )
                        if event.run_id == state.run_id
                        and event.task_id == state.task_id
                    )
                    persist_events(store, events)
                    store.save_checkpoint(state)
                    response_body = _task_execution_error_response(
                        state,
                        error,
                        events,
                    )
                    committed = self._commit_typed_receipt(
                        receipt_reservation,
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                        body=response_body,
                        binding={
                            "session_id": session_id,
                            "run_id": state.run_id,
                            "task_id": state.task_id,
                        },
                    )
                    if committed is None:
                        return
                    response_body, receipt_headers = committed
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        response_body,
                        headers=receipt_headers,
                    )
                    return
            events.extend(
                event
                for event in pool_api.pool.events.project_after(pool_api.pool.store, pool_sequence)
                if event.run_id == state.run_id and event.task_id == state.task_id
            )
            persist_events(store, events)
            store.save_checkpoint(state)
            curator = curate_terminal_task(store, state) if auto_run else None
            response_body = {
                "task": to_jsonable(state),
                "events": [to_jsonable(event) for event in events],
                "memory_curator": curator,
            }
            committed = self._commit_typed_receipt(
                receipt_reservation,
                status=HTTPStatus.CREATED,
                body=response_body,
                binding={
                    "session_id": session_id,
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                },
            )
            if committed is None:
                return
            response_body, receipt_headers = committed
            self._send_json(
                HTTPStatus.CREATED,
                response_body,
                headers=receipt_headers,
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "run":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            receipt_reservation = self._begin_typed_receipt(
                operation="task.resume",
                path=parsed.path,
                payload=payload,
            )
            if receipt_reservation is False:
                return
            pool_api = get_worker_pool_api()
            pool_journal = pool_api.pool.store.journal(limit=10000)
            pool_sequence = pool_journal[-1].sequence if pool_journal else 0
            _fence_pending_task_reservation(
                pool_api,
                state,
                reason="superseded by task resume physical dispatch",
            )
            try:
                if _task_is_sealed_control(state, payload):
                    prepare_phase2_loopx_pre_control(
                        state,
                        causation_id=(
                            f"task-resume:{state.run_id}:{state.task_id}"
                        ),
                    )
                events = run_task_graph(
                    state,
                    execution_context=graph_execution_context(),
                )
                pool_api.finalize_task(
                    state,
                    success=str(state.status) == "completed",
                    summary=f"task run finished with status {state.status}",
                )
            except Exception as error:  # noqa: BLE001 - HTTP boundary must stay structured.
                failure_event = _task_execution_failed_event(state, error)
                events = [failure_event]
                try:
                    pool_api.finalize_task(
                        state,
                        success=False,
                        summary="task resume failed before completion",
                    )
                except Exception:  # noqa: BLE001 - preserve the primary failure.
                    pass
                events.extend(
                    event
                    for event in pool_api.pool.events.project_after(
                        pool_api.pool.store,
                        pool_sequence,
                    )
                    if event.run_id == state.run_id
                    and event.task_id == state.task_id
                )
                persist_events(store, events)
                store.save_checkpoint(state)
                response_body = _task_execution_error_response(
                    state,
                    error,
                    events,
                )
                committed = self._commit_typed_receipt(
                    receipt_reservation,
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    body=response_body,
                    binding={
                        "session_id": str(
                            state.metadata.get("query_session_id") or ""
                        ),
                        "run_id": state.run_id,
                        "task_id": state.task_id,
                    },
                )
                if committed is None:
                    return
                response_body, receipt_headers = committed
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    response_body,
                    headers=receipt_headers,
                )
                return
            events.extend(
                event
                for event in pool_api.pool.events.project_after(pool_api.pool.store, pool_sequence)
                if event.run_id == state.run_id and event.task_id == state.task_id
            )
            persist_events(store, events)
            store.save_checkpoint(state)
            curator = curate_terminal_task(store, state)
            response_body = {
                "task": to_jsonable(state),
                "events": [to_jsonable(event) for event in events],
                "memory_curator": curator,
            }
            committed = self._commit_typed_receipt(
                receipt_reservation,
                status=HTTPStatus.OK,
                body=response_body,
                binding={
                    "session_id": str(state.metadata.get("query_session_id") or ""),
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                },
            )
            if committed is None:
                return
            response_body, receipt_headers = committed
            self._send_json(
                HTTPStatus.OK,
                response_body,
                headers=receipt_headers,
            )
            return

        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "cancel":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            receipt_reservation = self._begin_typed_receipt(
                operation="task.cancel",
                path=parsed.path,
                payload=payload,
            )
            if receipt_reservation is False:
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
            pool_api = get_worker_pool_api()
            pool_cancel = pool_api.integration.control.submit_and_apply(
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
            if pool_cancel.phase.value == "applied":
                pool_api.reconcile_task_graph_binding(
                    state,
                    reason=reason,
                    actor_id="task-control-api",
                    causation_id=f"task-cancel-graph:{pool_cancel.command_id}",
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
            response_body = {
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
            }
            committed = self._commit_typed_receipt(
                receipt_reservation,
                status=HTTPStatus.OK,
                body=response_body,
                binding={
                    "session_id": str(state.metadata.get("query_session_id") or ""),
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                },
            )
            if committed is None:
                return
            response_body, receipt_headers = committed
            self._send_json(
                HTTPStatus.OK,
                response_body,
                headers=receipt_headers,
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
            sealed_bounded_read_only = (
                payload.get("sealed_bounded_read_only") is True
            )
            if sealed_bounded_read_only:
                invalid_children: list[int] = []
                for index, item in enumerate(raw_items):
                    child = item if isinstance(item, Mapping) else {}
                    tools = tuple(str(value) for value in child.get("tools") or ())
                    skills = tuple(str(value) for value in child.get("skills") or ())
                    mcp_servers = tuple(
                        str(value) for value in child.get("mcp_servers") or ()
                    )
                    if (
                        child.get("background") is not True
                        or tools != ("file_read",)
                        or skills
                        or mcp_servers
                        or str(child.get("isolation") or "workspace") != "workspace"
                    ):
                        invalid_children.append(index)
                if invalid_children:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "sealed_bounded_fanout_invalid",
                            "message": (
                                "sealed bounded fanout requires background=true, "
                                "tools=[file_read], no skill/MCP and workspace isolation"
                            ),
                            "invalid_child_indexes": invalid_children,
                        },
                    )
                    return
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
                        physical_location=str(child.get("physical_location") or "local"),
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
                failure_code = getattr(error, "code", "")
                failure_code = getattr(failure_code, "value", failure_code)
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": str(failure_code or "subagent_worker_pool_acquisition_failed"),
                        "message": str(error),
                    },
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
                # One stdio runtime owns one provider-stream supervisor.  It
                # deliberately fences concurrent streams for the same provider,
                # so fanout children are drained serially while their physical
                # WorkerPool attempts remain independently admitted and fenced.
                "maximum_concurrency": 1,
                "idempotency_key": str(payload.get("idempotency_key") or request_id),
                "budget": {"max_children": max(2, int(payload.get("maximum_concurrency") or 4))},
                "disable_retrieval_context": payload.get("disable_retrieval_context") is True,
                "sealed_bounded_read_only_fanout": sealed_bounded_read_only,
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
                failure_code = getattr(error, "code", "")
                failure_code = getattr(failure_code, "value", failure_code)
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": str(failure_code or "typescript_subagent_fanout_execution_failed"),
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
                    physical_location=str(payload.get("physical_location") or "local"),
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
                "defer_background_drain": (
                    payload.get("defer_background_drain") is True
                ),
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
            command_sealed = _task_is_sealed_control(state, payload)
            command_competition_mode = _task_control_competition_mode(
                state,
                payload,
            )
            e02_route = _e02_command_route(text)
            if e02_route is not None:
                command_name, operation = e02_route
                tool_call_id = str(payload.get("tool_call_id") or new_id("e02command"))
                try:
                    command_arguments = {
                        "input": text,
                        "argument_overrides": dict(payload.get("arguments") or {}),
                        "sealed": command_sealed,
                        "competition_mode": command_competition_mode,
                    }
                    execution = get_mcp_runtime().execute(
                        "command",
                        command_arguments,
                        identity={
                            "tool_call_id": tool_call_id,
                            "command_name": command_name,
                            "operation": operation,
                            "permit_id": str(payload.get("permit_id") or ""),
                            "actor_id": str(payload.get("actor_id") or "api-user"),
                            "correlation_id": str(payload.get("correlation_id") or tool_call_id),
                            "sealed": command_sealed,
                            "competition_mode": command_competition_mode,
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
            dispatcher = get_control_dispatcher()
            if dispatcher.disabled:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "error": "control_runtime_unavailable",
                        "message": "RuntimeControlDispatcher is disabled",
                        "request_id": control_request.request_id,
                        "command_id": control_request.command_id,
                        "fallback": False,
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
            response = dispatcher.submit(
                control_request,
                _control_context_for_task(
                    state,
                    store,
                    session_custody_token=extract_bearer_token(
                        self.headers,
                        payload,
                    ),
                ),
            )
            store.save_checkpoint(state)
            status = HTTPStatus.CREATED if response.ok else HTTPStatus.ACCEPTED if response.status.value == "queued" else HTTPStatus.CONFLICT
            descriptor = get_control_command_registry().require(control_request.canonical_name)
            intervention_counted = bool(
                control_request.metadata.get("sealed", False)
                and not response.ok
                and response.error is not None
                and str(
                    getattr(response.error.code, "value", response.error.code)
                )
                == "permission_denied"
            )
            compatibility_result = {
                **response.to_dict(),
                "name": control_request.canonical_name,
                "summary": response.summary,
                "data": response.result.data,
                "intervention_counted": intervention_counted,
                "operator_intervention_attempt_count": int(
                    state.metadata.get("operator_intervention_attempt_count") or 0
                ),
                "human_intervention_count": int(
                    state.metadata.get("human_intervention_count") or 0
                ),
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
                    "intervention_counted": intervention_counted,
                    "operator_intervention_attempt_count": int(
                        state.metadata.get(
                            "operator_intervention_attempt_count"
                        )
                        or 0
                    ),
                    "human_intervention_count": int(
                        state.metadata.get("human_intervention_count") or 0
                    ),
                    "event_only_stateful_fallback": False,
                },
            )
            return

        if (
            len(parts) == 5
            and parts[0] == "tasks"
            and parts[2] == "commands"
            and parts[4] == "cancel"
        ):
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            request_id = parts[3]
            reason = str(
                payload.get("reason")
                or "Cancelled from the Zyra command surface."
            ).strip()
            try:
                response = get_control_dispatcher().cancel(
                    request_id,
                    reason=reason,
                    context=_control_context_for_task(state, store),
                )
            except (KeyError, ValueError) as error:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": "command_cancel_conflict",
                        "message": str(error),
                        "request_id": request_id,
                    },
                )
                return
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.OK,
                {
                    "task": to_jsonable(state),
                    "control_request": response.to_dict(),
                    "command_result": response.to_dict(),
                    "event": None,
                    "receipt_replayed": False,
                },
                headers={"Cache-Control": "no-store, max-age=0"},
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
                        "permit_id": str(payload.get("permit_id") or ""),
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
            try:
                viewer_control = _browser_viewer_control_request(state, payload)
            except (TypeError, ValueError) as error:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "error": "invalid_browser_viewer_control",
                        "message": str(error),
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
            if viewer_control is not None:
                viewer_control["task_id"] = state.task_id
                viewer_control["run_id"] = state.run_id
                sealed_receipts = state.metadata.setdefault(
                    "sealed_browser_viewer_control_receipts",
                    {},
                )
                if not isinstance(sealed_receipts, dict):
                    sealed_receipts = {}
                    state.metadata["sealed_browser_viewer_control_receipts"] = sealed_receipts
                prior_sealed = sealed_receipts.get(viewer_control["command_id"])
                if isinstance(prior_sealed, dict):
                    if (
                        str(prior_sealed.get("idempotency_fingerprint") or "")
                        != str(viewer_control["fingerprint"])
                    ):
                        self._send_json(
                            HTTPStatus.CONFLICT,
                            {
                                "error": "browser_viewer_control_idempotency_conflict",
                                "browser_control_receipt": prior_sealed,
                            },
                            headers={"Cache-Control": "no-store, max-age=0"},
                        )
                        return
                    replayed = dict(prior_sealed)
                    replayed["replayed"] = True
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "ok": False,
                            "sealed": True,
                            "browser_control_receipt": replayed,
                            "intervention_counted": True,
                            "human_intervention_count": int(
                                state.metadata.get("human_intervention_count") or 0
                            ),
                            "receipt_replayed": True,
                        },
                        headers={
                            "Cache-Control": "no-store, max-age=0",
                            "X-Zyra-Receipt-Replayed": "true",
                        },
                    )
                    return
                if bool(viewer_control["sealed"]):
                    attempts = int(
                        state.metadata.get("operator_intervention_attempt_count") or 0
                    ) + 1
                    state.metadata["operator_intervention_attempt_count"] = attempts
                    human_count = int(
                        state.metadata.get("human_intervention_count") or 0
                    )
                    event = EventRecord(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        node_id=state.root_node_id,
                        event_type=EventType.CONTROL_COMMAND,
                        payload={
                            "browser_viewer_control": {
                                **dict(viewer_control),
                                "status": "denied",
                                "error_code": "sealed_browser_viewer_control_denied",
                                "error_message": (
                                    "Sealed autonomous runs are read-only for "
                                    "operator browser controls."
                                ),
                                "authorizes_execution": False,
                                "manual_mutation_applied": False,
                                "approval_wait_entered": False,
                                "intervention_counted": True,
                                "operator_intervention_attempt_count": attempts,
                                "human_intervention_count": human_count,
                                "automatic_recovery_action": "fail_closed",
                                "fallback_allowed": False,
                            }
                        },
                    )
                    persist_events(store, [event])
                    receipt = _browser_viewer_receipt(
                        viewer_control,
                        status="denied",
                        event_ids=(event.event_id,),
                        error_code="sealed_browser_viewer_control_denied",
                        error_message=(
                            "Sealed autonomous runs are read-only for operator "
                            "browser controls."
                        ),
                        intervention_counted=True,
                        human_intervention_count=human_count,
                        manual_mutation_applied=False,
                        approval_wait_entered=False,
                        automatic_recovery_action="fail_closed",
                    )
                    sealed_receipts[viewer_control["command_id"]] = dict(receipt)
                    while len(sealed_receipts) > 128:
                        sealed_receipts.pop(next(iter(sealed_receipts)))
                    state.metadata["last_sealed_browser_viewer_control_denial"] = {
                        "command_id": viewer_control["command_id"],
                        "request_id": viewer_control["request_id"],
                        "action": viewer_control["action"],
                        "event_id": event.event_id,
                        "operator_intervention_attempt_count": attempts,
                        "human_intervention_count": human_count,
                        "manual_mutation_applied": False,
                        "approval_wait_entered": False,
                        "automatic_recovery_action": "fail_closed",
                        "recorded_at": now_iso(),
                    }
                    state.updated_at = event.created_at
                    store.save_checkpoint(state)
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "ok": False,
                            "sealed": True,
                            "event": to_jsonable(event),
                            "event_ids": [event.event_id],
                            "browser_control_receipt": receipt,
                            "intervention_counted": True,
                            "operator_intervention_attempt_count": attempts,
                            "human_intervention_count": human_count,
                            "manual_mutation_applied": False,
                            "approval_wait_entered": False,
                            "automatic_recovery_action": "fail_closed",
                        },
                        headers={
                            "Cache-Control": "no-store, max-age=0",
                            "Pragma": "no-cache",
                            "X-Zyra-Browser-Control-State-Owner": "BrowserWorkerRuntime",
                        },
                    )
                    return
            constraints = payload.get("constraints")
            constraints = dict(constraints) if isinstance(constraints, dict) else {}
            if viewer_control is not None:
                try:
                    constraints.update(
                        _browser_viewer_execution_constraints(viewer_control)
                    )
                except (TypeError, ValueError) as error:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "invalid_browser_viewer_control",
                            "message": str(error),
                        },
                        headers={"Cache-Control": "no-store, max-age=0"},
                    )
                    return
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
                        "browser_control_receipt": {
                            **dict(prior.get("browser_control_receipt") or {}),
                            "replayed": True,
                        }
                        if prior.get("browser_control_receipt")
                        else {},
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
                    gateway_constraints=constraints,
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

            pending_permission_metadata = _browser_pending_permission_metadata(run_result)
            if pending_permission_metadata:
                run_result = replace(
                    run_result,
                    worker_result=replace(
                        run_result.worker_result,
                        metadata={
                            **dict(run_result.worker_result.metadata),
                            **pending_permission_metadata,
                        },
                    ),
                )
            browser_action_pending = bool(pending_permission_metadata)
            viewer_control_receipt: dict[str, Any] = {}
            if viewer_control is not None:
                control_action = str(viewer_control["action"])
                control_status = (
                    "accepted"
                    if browser_action_pending
                    else "observed"
                    if control_action == "inspect" and run_result.worker_result.ok
                    else "applied"
                    if run_result.worker_result.ok
                    else "failed"
                )
                mutation_ids = tuple(
                    str(event.payload.get("mutation_id") or "")
                    for event in run_result.event_records
                    if isinstance(event.payload, Mapping)
                    and event.payload.get("mutation_id")
                )
                viewer_control_receipt = _browser_viewer_receipt(
                    viewer_control,
                    status=control_status,
                    event_ids=tuple(
                        event.event_id for event in run_result.event_records
                    ),
                    mutation_ids=mutation_ids,
                    artifact_ids=tuple(
                        artifact.artifact_id
                        for artifact in run_result.worker_result.artifacts
                    ),
                    worker_request_id=request.request_id,
                    lifecycle_receipt_id=str(
                        run_result.worker_result.metadata.get(
                            "browser_lifecycle_receipt_id",
                            "",
                        )
                    ),
                    permission_request_id=str(
                        run_result.worker_result.metadata.get(
                            "browser_pending_permission_request_id",
                            "",
                        )
                    ),
                    error_code=str(run_result.worker_result.error or ""),
                    error_message=str(
                        run_result.worker_result.metadata.get(
                            "browser_error_message",
                            "",
                        )
                    ),
                    intervention_counted=False,
                    human_intervention_count=int(
                        state.metadata.get("human_intervention_count") or 0
                    ),
                    manual_mutation_applied=bool(
                        run_result.worker_result.ok
                        and control_action != "inspect"
                    ),
                    approval_wait_entered=browser_action_pending,
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
                    "browser_control_receipt": viewer_control_receipt,
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
                    "browser_control_receipt": viewer_control_receipt,
                },
                headers={
                    "Cache-Control": "no-store, max-age=0",
                    "Pragma": "no-cache",
                    "X-Zyra-Permission-State-Owner": "PermissionStateStore",
                    "X-Zyra-Context-State-Owner": "ClaudeContextWindowManager/M1-02D",
                    "X-Zyra-Browser-Control-State-Owner": "BrowserWorkerRuntime",
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
            session_id = str(
                payload.get("session_id")
                or state.metadata.get("query_session_id")
                or f"task:{state.task_id}"
            )
            tool_call_id = str(
                payload.get("tool_call_id")
                or payload.get("tool_use_id")
                or payload.get("invocation_id")
                or new_id("skillcall")
            )
            try:
                run = _run_typescript_skill_request(
                    store,
                    state,
                    skill_name=skill_name,
                    skill_arguments=dict(arguments) if isinstance(arguments, dict) else {},
                    resources=(
                        tuple(resources)
                        if isinstance(resources, list)
                        and all(isinstance(item, str) for item in resources)
                        else ()
                    ),
                    tool_call_id=tool_call_id,
                    session_id=session_id,
                    session_custody_token=extract_bearer_token(self.headers, payload),
                )
            except Exception as error:  # noqa: BLE001 - typed worker failure projection below.
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "error": "skill_worker_failed",
                        "message": "Skill QueryEngine execution failed.",
                        "exception_type": type(error).__name__,
                        "tool_call_id": tool_call_id,
                        "task_id": state.task_id,
                        "run_id": state.run_id,
                        "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                        "python_skill_fallback": False,
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
            persist_events(store, run.event_records)
            _attach_artifacts(state, list(run.worker_result.artifacts))
            _record_code_worker_session_metadata(state, run)
            tool_result: dict[str, Any] | None = None
            for event in run.event_records:
                query_session = event.payload.get("query_session")
                if not isinstance(query_session, Mapping):
                    continue
                candidate = query_session.get("tool_result")
                if (
                    query_session.get("phase") == "tool_call_completed"
                    and query_session.get("tool_name") == "skill"
                    and isinstance(candidate, Mapping)
                ):
                    tool_result = dict(candidate)
                    break
            if not run.worker_result.ok or not tool_result or not tool_result.get("ok"):
                error_code = str(
                    (tool_result or {}).get("error")
                    or run.worker_result.error
                    or "skill_execution_failed"
                )
                if error_code == "permission_suspended":
                    error_code = "permission_approval_required"
                store.save_checkpoint(state)
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "error": error_code,
                        "message": str(
                            (tool_result or {}).get("summary")
                            or run.worker_result.summary
                            or "Skill execution failed."
                        ),
                        "detail": {},
                        "tool_call_id": tool_call_id,
                        "task_id": state.task_id,
                        "run_id": state.run_id,
                        "canonical_entrypoint": "E02CapabilityCoordinator.execute",
                        "python_skill_fallback": False,
                    },
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
                return
            snapshot_hash = str(
                get_mcp_runtime().snapshot(("skill",)).get("snapshotHash") or ""
            )
            execution = {
                "receipt": {
                    "owner": "typescript-skill",
                    "permitId": str(
                        (tool_result.get("metadata") or {}).get("e02_permit_id")
                        or payload.get("permit_id")
                        or ""
                    ),
                    "replayed": (
                        str((tool_result.get("metadata") or {}).get("effect_replay_fenced"))
                        .lower()
                        == "true"
                    ),
                    "result": tool_result,
                },
                "snapshot_hash": snapshot_hash,
            }
            state.metadata["skill_invocation_projection"] = {
                "canonical_owner": "typescript.SkillCoordinator",
                "tool_call_id": tool_call_id,
                "skill": skill_name,
                "snapshot_hash": snapshot_hash,
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
                    "permit_id": str(payload.get("permit_id") or ""),
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
                    "tool_call_id": tool_call_id,
                    "task_id": state.task_id,
                    "run_id": state.run_id,
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
                materialize_bundled_skills(PROJECT_ROOT, worker_workspace_root)
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
                    event_reader=store.task_events,
                    checkpoint_reader=_tool_checkpoint_reader(store),
                    runtime_services={
                        "backend_action_dispatch_port": _code_worker_backend_action_dispatch_port(
                            state,
                            workspace_root=worker_workspace_root,
                            route_sources=(constraints,),
                        ),
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
                        "fault_observation_sink": codeworker_fault_observation_sink(
                            store,
                            state,
                        ),
                        "fault_observation_sink_required": True,
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
        default_max_bytes = (
            8 * 1024 * 1024
            if self.path.split("?", 1)[0].rstrip("/") == "/hardening/m1/integration"
            else 2 * 1024 * 1024
        )
        try:
            max_bytes = max(
                1024,
                int(os.environ.get("ZYRA_MAX_JSON_BODY_BYTES", str(default_max_bytes))),
            )
        except ValueError:
            max_bytes = default_max_bytes
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
        merged_headers = dict(getattr(self, "_zyra_typed_response_headers", {}) or {})
        if not merged_headers:
            try:
                context = typed_request_context(self.headers)
                merged_headers.update(context.response_headers())
            except TypedTransportError as error:
                _, _, fallback_headers = typed_error_context(error, self.headers)
                merged_headers.update(fallback_headers)
        merged_headers.update(dict(headers or {}))
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._zyra_response_started = True
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in merged_headers.items():
            normalized = str(key).strip()
            if not normalized or "\r" in normalized or "\n" in normalized:
                continue
            rendered = str(value)
            if "\r" in rendered or "\n" in rendered:
                continue
            self.send_header(normalized, rendered)
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        *,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        merged_headers = dict(getattr(self, "_zyra_typed_response_headers", {}) or {})
        if not merged_headers:
            try:
                context = typed_request_context(self.headers)
                merged_headers.update(context.response_headers())
            except TypedTransportError as error:
                _, _, fallback_headers = typed_error_context(error, self.headers)
                merged_headers.update(fallback_headers)
        merged_headers.update(dict(headers or {}))
        selected_type = str(content_type or "application/octet-stream").strip().lower()
        if "\r" in selected_type or "\n" in selected_type or "/" not in selected_type:
            selected_type = "application/octet-stream"
        body = bytes(payload)
        self._zyra_response_started = True
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", selected_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in merged_headers.items():
            normalized = str(key).strip()
            rendered = str(value)
            if (
                not normalized
                or "\r" in normalized
                or "\n" in normalized
                or "\r" in rendered
                or "\n" in rendered
            ):
                continue
            self.send_header(normalized, rendered)
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_stream(self, chunks: Any) -> None:
        iterator = iter(chunks)
        first_chunk = next(iterator, None)
        merged_headers = dict(
            getattr(self, "_zyra_typed_response_headers", {}) or {}
        )
        if not merged_headers:
            try:
                context = typed_request_context(self.headers)
                merged_headers.update(context.response_headers())
            except TypedTransportError as error:
                _, _, fallback_headers = typed_error_context(error, self.headers)
                merged_headers.update(fallback_headers)
        merged_headers.update(sse_headers())
        self._zyra_response_started = True
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        for key, value in merged_headers.items():
            normalized = str(key).strip()
            rendered = str(value)
            if (
                not normalized
                or "\r" in normalized
                or "\n" in normalized
                or "\r" in rendered
                or "\n" in rendered
            ):
                continue
            self.send_header(normalized, rendered)
        self.end_headers()
        try:
            if first_chunk is not None:
                chunk = first_chunk
                if not isinstance(chunk, (bytes, bytearray)) or not chunk:
                    pass
                else:
                    self.wfile.write(bytes(chunk))
                    self.wfile.flush()
            for chunk in iterator:
                if not isinstance(chunk, (bytes, bytearray)) or not chunk:
                    continue
                self.wfile.write(bytes(chunk))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        finally:
            # A finite SSE response has no Content-Length.  The embedded
            # server therefore uses EOF as the response delimiter so the
            # browser can deterministically enter cursor-based reconnect.
            self.close_connection = True
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    def _send_cors_headers(self) -> None:
        origin = str(self.headers.get("Origin") or "").strip()
        allowed_origins = set(
            runtime_configuration().get("api.cors_origins", [])
        )
        if origin and origin in allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, Idempotency-Key, Authorization, X-Zyra-Service-Token, "
            "X-Zyra-Api-Version, X-Zyra-Api-Min-Version, X-Request-Id, "
            "X-Correlation-Id, X-Causation-Id, X-Zyra-Client, "
            "X-Zyra-Client-Version, X-Zyra-Operation, X-Zyra-Contract, "
            "X-Zyra-Attempt, X-Zyra-Deadline-Ms",
        )
        self.send_header(
            "Access-Control-Expose-Headers",
            "X-Zyra-Api-Version, X-Zyra-Api-Min-Version, X-Request-Id, "
            "X-Correlation-Id, X-Causation-Id, X-Zyra-Receipt-Id, "
            "X-Zyra-Receipt-Replayed, X-Next-Cursor, X-Zyra-Event-Cursor, "
            "X-Zyra-Event-High-Watermark, X-Zyra-Projection-Cursor, "
            "Retry-After",
        )
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def run(host: str | None = None, port: int | None = None) -> None:
    configuration = runtime_configuration()
    bind_host = host or str(configuration.require("api.host"))
    bind_port = port or int(configuration.require("api.port"))
    bootstrap = get_api_product_bootstrap()
    try:
        receipt = bootstrap.start()
    except BootstrapError as error:
        print(
            json.dumps(
                {
                    "schema": "zyra.api-bootstrap-failure/v1",
                    "code": error.code,
                    "phase": error.phase.value,
                    "retryable": error.retryable,
                    "details": error.details,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise
    server = ThreadingHTTPServer((bind_host, bind_port), ZyraRequestHandler)
    bootstrap.lifecycle.register_resource(
        "api-http-server",
        server,
        close=lambda value: value.server_close(),
        dependencies=("migration-journal",),
    )
    bootstrap.lifecycle.emit(
        "api.server.listening",
        attributes={
            "host": bind_host,
            "port": bind_port,
            "bootstrap_receipt_digest": receipt.digest,
            "event_log": str(event_log_path()),
            "sqlite": str(sqlite_path()),
        },
    )
    print(
        json.dumps(
            {
                "schema": "zyra.api-startup/v1",
                "ready": True,
                "url": f"http://{bind_host}:{bind_port}",
                "process_generation": receipt.process.generation_id,
                "configuration_digest": receipt.configuration_digest,
                "migration_digest": (
                    receipt.migration.digest
                    if receipt.migration is not None
                    else None
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    try:
        server.serve_forever()
    finally:
        bootstrap.shutdown()


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


def _safe_download_name(artifact: ArtifactRef) -> str:
    relative_path = str(artifact.metadata.get("relative_path") or "")
    suffix = Path(relative_path).suffix.lower()
    if not suffix or len(suffix) > 32 or not suffix[1:].replace("_", "").replace("-", "").isalnum():
        suffix = ".bin"
    title = str(artifact.title or artifact.artifact_id or "artifact")
    stem = "".join(
        character
        if character.isalnum() or character in {"-", "_", "."}
        else "-"
        for character in title
    ).strip(".-_")
    if not stem:
        stem = "artifact"
    stem = stem[:128]
    if stem.lower().endswith(suffix):
        return stem
    return f"{stem}{suffix}"


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


def _loopx_task_projection(
    state: Any,
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    value = copy.deepcopy(dict(projection))
    canonical = dict(value.get("canonical_state") or {})
    worker_projection = dict(
        state.metadata.get("worker_pool_projection") or {}
    )
    canonical.update(
        {
            "task": {
                "task_id": state.task_id,
                "run_id": state.run_id,
                "status": str(state.status),
                "owner": "Zyra orchestration/runtime",
            },
            "worker_lease": {
                "lease_id": str(worker_projection.get("lease_id") or ""),
                "status": str(worker_projection.get("status") or "none"),
                "owner": "WorkerLeaseManager",
            },
            "execution_budget": {
                **to_jsonable(state.budget),
                "owner": "ResourceScheduler",
            },
        }
    )
    value["canonical_state"] = canonical
    return value


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
    try:
        priority = QueuePriority(
            str(payload.get("priority") or "next").strip().lower()
        )
        expected_revision = (
            int(payload["expected_session_revision"])
            if payload.get("expected_session_revision") is not None
            else None
        )
    except (TypeError, ValueError):
        return None
    delivery_mode = str(payload.get("delivery_mode") or "enqueue").strip().lower()
    if delivery_mode not in {"enqueue", "steer", "interrupt"}:
        return None
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
        expected_session_revision=expected_revision,
        priority=priority,
        metadata={
            "actor_id": str(payload.get("actor_id") or "api-user"),
            "permission_authority": "retained non-E02 task-control allowlist",
            "e02_command_dispatch": "typescript-only",
            "sealed": _task_is_sealed_control(state, payload),
            "competition_mode": _task_control_competition_mode(state, payload),
            "delivery_mode": delivery_mode,
            "retry_of_request_id": str(payload.get("retry_of_request_id") or ""),
        },
    )


def _control_context_for_task(
    state: Any,
    store: SQLiteStore,
    *,
    session_custody_token: str = "",
) -> RuntimeControlContext:
    def synchronize_canonical_state() -> Any:
        """Refresh the request-scoped object after a canonical owner commits.

        The HTTP compatibility route checkpoints ``state`` after dispatch.
        Recovery and worker-pool owners deliberately reload their own
        transaction-scoped TaskState, so leaving this object stale would
        overwrite their committed mutation at the route boundary.
        """

        latest = store.load_task(state.task_id)
        if latest is None or latest is state:
            return state
        for field_name in (
            "status",
            "updated_at",
            "constraints",
            "plan_nodes",
            "artifacts",
            "decisions",
            "budget",
            "metadata",
        ):
            setattr(
                state,
                field_name,
                copy.deepcopy(getattr(latest, field_name)),
            )
        return state

    def revision(_session_id: str) -> int:
        # Command lifecycle events are audit evidence, not session mutations.
        # Counting requested/validated/started events here made a command
        # invalidate its own expected_session_revision before execution.
        return int(state.metadata.get("session_control_revision") or 0)

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
        if descriptor.handler_id == "subagent.control":
            raw = str(request.arguments.get("raw") or "").strip()
            action = raw.split(maxsplit=1)[0].casefold() if raw else str(
                request.arguments.get("action") or "list"
            ).casefold()
            if action in {"", "list", "ls", "show", "inspect", "status"}:
                return True
        sealed = bool(request.metadata.get("sealed", False))
        mutation_scope = str(getattr(descriptor.mutation_scope, "value", descriptor.mutation_scope))
        if sealed and mutation_scope != "read_only":
            ledger = state.metadata.setdefault("operator_intervention_ledger", [])
            existing_entry = next(
                (
                    item
                    for item in ledger
                    if isinstance(item, Mapping)
                    and str(item.get("request_id") or "") == request.request_id
                ),
                None,
            )
            already_counted = existing_entry is not None
            if not already_counted:
                entry = {
                    "request_id": request.request_id,
                    "command_id": request.command_id,
                    "canonical_name": request.canonical_name,
                    "actor_id": str(request.metadata.get("actor_id") or "api-user"),
                    "competition_mode": str(request.metadata.get("competition_mode") or "sealed_autonomous"),
                    "decision": "denied",
                    "reason": "sealed autonomous policy rejects manual runtime mutation",
                    "human_intervention_count": 0,
                    "manual_command_applied": False,
                    "human_wait_entered": False,
                    "no_human_wait": True,
                    "recovery_phase": "planning",
                    "created_at": now_iso(),
                }
                ledger.append(entry)
                state.metadata["operator_intervention_attempt_count"] = len(ledger)
                state.metadata["human_intervention_count"] = 0
                store.save_checkpoint(state)
                try:
                    worker_api = get_worker_pool_api()
                    graph_id = worker_api.ensure_task_graph(state)
                    result = get_recovery_runtime_api(store).application.recover(
                        {
                            "source": "permission_runtime",
                            "source_kind": "permission_denied",
                            "reason_code": "sealed.manual_control_denied",
                            "summary": (
                                f"Sealed autonomous mode denied "
                                f"{request.canonical_name} from "
                                f"{request.metadata.get('actor_id') or 'api-user'}."
                            ),
                            "refs": {
                                "run_id": state.run_id,
                                "task_id": state.task_id,
                                "request_id": request.request_id,
                                "node_id": str(
                                    request.arguments.get("node_id")
                                    or state.root_node_id
                                ),
                                "graph_id": graph_id,
                            },
                            "permission_effect": "deny",
                            "retryable": False,
                            "terminal": True,
                            "recoverable": True,
                            "causation_id": request.command_id,
                            "correlation_id": request.request_id,
                            "details": {
                                "canonical_name": request.canonical_name,
                                "actor_id": str(
                                    request.metadata.get("actor_id")
                                    or "api-user"
                                ),
                                "manual_command_applied": False,
                                "human_intervention_count": 0,
                            },
                        },
                        source="permission_runtime",
                        context_overrides={
                            "mode": "sealed_autonomous",
                            "metadata": {
                                "explicit_escalation": False,
                                "sealed_control_denial": True,
                                "human_intervention_count": 0,
                            },
                        },
                        apply=True,
                        idempotency_key=(
                            f"sealed-control-denial:{request.request_id}"
                        ),
                    )
                    synchronize_canonical_state()
                    ledger = state.metadata.setdefault(
                        "operator_intervention_ledger",
                        [],
                    )
                    entry = next(
                        item
                        for item in ledger
                        if isinstance(item, Mapping)
                        and str(item.get("request_id") or "")
                        == request.request_id
                    )
                    entry["recovery_phase"] = "applied"
                    entry["recovery"] = result.to_dict()
                    entry["recovery_plan_id"] = result.plan.plan_id
                    entry["recovery_action"] = (
                        result.plan.decision.selected.action.value
                    )
                    entry["no_human_wait"] = True
                except Exception as error:
                    # A sealed command must never become an approval wait or
                    # fall through to the requested mutation.  If the
                    # deterministic replan cannot commit, retain a terminal
                    # fail-closed receipt for timeline/recovery diagnostics.
                    synchronize_canonical_state()
                    ledger = state.metadata.setdefault(
                        "operator_intervention_ledger",
                        [],
                    )
                    entry = next(
                        item
                        for item in ledger
                        if isinstance(item, Mapping)
                        and str(item.get("request_id") or "")
                        == request.request_id
                    )
                    entry["recovery_phase"] = "failed_closed"
                    entry["recovery_error"] = (
                        f"{type(error).__name__}: {error}"
                    )[:2000]
                    entry["no_human_wait"] = True
                    entry["manual_command_applied"] = False
                state.metadata["last_sealed_control_denial"] = dict(entry)
                state.metadata["human_intervention_count"] = 0
                store.save_checkpoint(state)
            return False
        get_permission_control_plane().state_store.snapshot()
        raw = str(request.arguments.get("raw") or "").strip().lower()
        if descriptor.handler_id == "provider.model" and raw in {"", "status", "list", "show"}:
            return True
        if descriptor.permission_action in {"session.rewind", "session.resume"}:
            checkpoint_ref = str(
                request.arguments.get("checkpoint_ref")
                or request.arguments.get("target")
                or (
                    request.arguments.get("raw")
                    if descriptor.permission_action == "session.rewind"
                    else ""
                )
                or ""
            ).strip()
            if not checkpoint_ref:
                return False
            canonical_checkpoint = (
                get_recovery_runtime_api(store)
                .application.store.checkpoint(checkpoint_ref)
            )
            if canonical_checkpoint is None:
                return False
            return (
                canonical_checkpoint.run_id == state.run_id
                and canonical_checkpoint.task_id == state.task_id
                and canonical_checkpoint.session_id == request.session_id
            )
        return descriptor.permission_action in {
            "task.goal",
            "context.compact",
            "session.clear",
            "session.rewind",
            "session.resume",
            "artifact.write",
            "task.change",
            "task.inject",
            "watchdog.control",
            "artifact.export",
            "task.evaluate",
            "worker.kill",
            "recovery.steer",
            "recovery.retry",
            "worker.reassign",
            "subagent.control",
            "loopx.control",
        }

    handlers: dict[str, Any] = {}

    def loopx_snapshot() -> dict[str, Any]:
        return _loopx_task_projection(
            state,
            get_loopx_control_runtime().snapshot(
                run_id=state.run_id,
                task_id=state.task_id,
                goal_id=str(
                    state.metadata.get("loopx_goal_id")
                    or task_goal_id(state.task_id)
                ),
            ),
        )

    def persist_loopx_continuation(
        projection: Mapping[str, Any],
    ) -> None:
        private = dict(projection.get("private_state") or {})
        goal = dict(private.get("goal") or {})
        todos = [
            dict(item)
            for item in private.get("todos") or ()
            if isinstance(item, Mapping)
        ]
        next_todo = next(
            (
                item
                for item in todos
                if not bool(item.get("done"))
                and str(item.get("status") or "open")
                not in {"done", "completed"}
            ),
            {},
        )
        continuation = dict(projection.get("continuation") or {})
        state.metadata["loopx_goal_id"] = str(
            projection.get("goal_id") or task_goal_id(state.task_id)
        )
        state.metadata["loopx_continuation"] = {
            "schema": "zyra.loopx-continuation/v1",
            "enabled": bool(projection.get("connected")),
            "lifecycle": str(projection.get("lifecycle") or "disabled"),
            "goal_id": str(projection.get("goal_id") or ""),
            "objective_ref": str(goal.get("objective_ref") or ""),
            "requirement_revision": str(
                goal.get("requirement_revision") or ""
            ),
            "todo_id": str(next_todo.get("todo_id") or ""),
            "obligation": str(
                next_todo.get("title")
                or next_todo.get("text")
                or ""
            ),
            "continuation_allowed": bool(continuation.get("allowed")),
            "sync_cursor": int(
                dict(projection.get("sync") or {}).get("cursor") or 0
            ),
            "last_validated_receipt": dict(
                projection.get("last_validated_receipt") or {}
            ),
            "owner": "LoopX private control",
            "canonical_mutation_owner": "GraphStateCustody",
        }
        store.save_checkpoint(state)

    def loopx_read(
        request: ControlCommandRequest,
        descriptor: Any,
        _context: Any,
    ) -> ControlResult:
        projection = loopx_snapshot()
        handler_id = str(descriptor.handler_id)
        data: dict[str, Any]
        if handler_id == "loopx.todos":
            data = {
                "schema": projection["schema"],
                "goal_id": projection["goal_id"],
                "todos": projection["private_state"]["todos"],
            }
        elif handler_id == "loopx.todo":
            argv = list(request.arguments.get("argv") or ())
            todo_id = str(
                request.arguments.get("todo_id")
                or (argv[0] if argv else "")
            )
            todo = next(
                (
                    item
                    for item in projection["private_state"]["todos"]
                    if str(item.get("todo_id") or "") == todo_id
                ),
                None,
            )
            if todo is None:
                raise ValueError(f"LoopX todo not found: {todo_id}")
            data = {"goal_id": projection["goal_id"], "todo": todo}
        elif handler_id == "loopx.quota":
            data = {
                "goal_id": projection["goal_id"],
                "loopx_private_quota": projection["private_state"]["quota"],
                "zyra_execution_budget": projection["canonical_state"][
                    "execution_budget"
                ],
                "same_owner": False,
            }
        elif handler_id == "loopx.sync":
            data = {
                "goal_id": projection["goal_id"],
                "lifecycle": projection["lifecycle"],
                "sync": projection["sync"],
                "last_sync_receipt": projection["last_sync_receipt"],
                "error": projection["error"],
            }
        else:
            data = projection
        return ControlResult(
            display_text=(
                f"LoopX {handler_id.removeprefix('loopx.')} "
                f"is {projection['lifecycle']}."
            ),
            data=data,
            metadata={
                "runtime_status": "read_only",
                "state_owner": "LoopXControlRuntime",
            },
        )

    for handler_id in {
        "loopx.status",
        "loopx.todos",
        "loopx.todo",
        "loopx.quota",
        "loopx.sync",
    }:
        handlers[handler_id] = loopx_read

    def loopx_mutation(
        request: ControlCommandRequest,
        descriptor: Any,
        _context: Any,
    ) -> ControlResult:
        action = str(descriptor.handler_id).removeprefix("loopx.")
        arguments = dict(request.arguments)
        argv = list(arguments.get("argv") or ())
        if action in {"claim", "release"}:
            arguments.setdefault(
                "todo_id",
                argv[0] if len(argv) > 0 else "",
            )
            arguments.setdefault(
                "claimant",
                argv[1] if len(argv) > 1 else "",
            )
        elif action == "connect":
            arguments.setdefault(
                "objective",
                str(arguments.get("raw") or state.user_goal),
            )
            arguments.setdefault(
                "objective_ref",
                f"zyra://run/{state.run_id}/task/{state.task_id}/objective",
            )
            arguments.setdefault(
                "requirement_revision",
                str(
                    len(state.metadata.get("requirement_changes") or ())
                    + 1
                ),
            )
        elif action == "interaction":
            action = "interaction_submit"
            arguments.setdefault(
                "continuation_hint",
                str(arguments.get("raw") or ""),
            )
        expected_cursor = arguments.get("expected_cursor")
        if expected_cursor not in {None, ""}:
            actual_cursor = int(
                dict(loopx_snapshot().get("sync") or {}).get("cursor") or 0
            )
            if int(expected_cursor) != actual_cursor:
                raise ValueError(
                    "LoopX sync cursor changed before command execution: "
                    f"expected {expected_cursor}, actual {actual_cursor}"
                )
        if action == "retry":
            sequence = arguments.get("sequence")
            if sequence is None and argv:
                sequence = argv[0]
            result = get_loopx_control_runtime().retry_sync(
                run_id=state.run_id,
                task_id=state.task_id,
                goal_id=str(
                    state.metadata.get("loopx_goal_id")
                    or task_goal_id(state.task_id)
                ),
                sequence=int(sequence) if sequence not in {None, ""} else None,
            )
            projection = _loopx_task_projection(state, result["state"])
            persist_loopx_continuation(projection)
            result["state"] = projection
            return ControlResult(
                display_text="LoopX dead-letter sync retry completed.",
                data=result,
                metadata={
                    "runtime_status": "stateful",
                    "state_owner": "LoopXControlRuntime",
                },
            )

        worker_api = get_worker_pool_api()
        graph_id = worker_api.ensure_task_graph(state)
        effective_idempotency = (
            request.idempotency_key or request.request_id
        )
        branch = worker_api.graph_custody.branch(
            graph_id,
            branch_id=f"loopx-control:{request.request_id}",
            actor_id="LoopXControlRuntime",
            causation_id=request.command_id,
            correlation_id=request.request_id,
            idempotency_key=f"loopx-control:{effective_idempotency}",
            metadata={
                "control_action": action,
                "private_payload_excluded": True,
            },
        )
        branch.set_metadata(
            "loopx_control",
            {
                "schema": "zyra.loopx-canonical-control-ref/v1",
                "action": action,
                "goal_id": str(
                    arguments.get("goal_id")
                    or state.metadata.get("loopx_goal_id")
                    or task_goal_id(state.task_id)
                ),
                "request_id": request.request_id,
                "permission_action": descriptor.permission_action,
                "private_payload_excluded": True,
            },
        )
        committed = worker_api.graph_custody.commit(branch.build())
        if not committed.receipt.committed:
            raise RuntimeError(
                "GraphStateCustody rejected LoopX control mutation"
            )
        validation = {
            "validation_passed": True,
            "permission_allowed": True,
            "lease_valid": True,
            "budget_allowed": True,
            "validation_receipt_id": committed.receipt.commit_id,
            "permission_receipt_id": request.request_id,
            "lease_receipt_id": "control-plane:no-worker-dispatch",
            "budget_receipt_id": "loopx-private-quota:not-zyra-budget",
        }
        result = get_loopx_control_runtime().mutate(
            action=action,
            run_id=state.run_id,
            task_id=state.task_id,
            canonical_commit=committed,
            validation=validation,
            payload=arguments,
            idempotency_key=f"loopx-control:{effective_idempotency}",
            causation_id=request.command_id,
        )
        projection = _loopx_task_projection(state, result["state"])
        persist_loopx_continuation(projection)
        result["state"] = projection
        state.metadata.setdefault("control_mutations", []).append(
            {
                "request_id": request.request_id,
                "command": request.canonical_name,
                "action": action,
                "canonical_commit_id": committed.receipt.commit_id,
                "outbox_sequence": result["sequence"],
                "sync_status": str(
                    dict(result.get("receipt") or {}).get("status") or ""
                ),
                "canonical_owner": "GraphStateCustody",
                "private_state_owner": "LoopX",
            }
        )
        store.save_checkpoint(state)
        return ControlResult(
            display_text=(
                f"LoopX {action} synchronized with "
                f"{result['receipt'].get('status') or 'pending'}."
            ),
            data=result,
            metadata={
                "runtime_status": "stateful",
                "canonical_owner": "GraphStateCustody",
                "private_state_owner": "LoopX",
                "bridge_owner": "LoopXControlRuntime",
            },
        )

    for handler_id in {
        "loopx.connect",
        "loopx.disconnect",
        "loopx.claim",
        "loopx.release",
        "loopx.interaction",
        "loopx.retry",
    }:
        handlers[handler_id] = loopx_mutation

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

    def subagent_tasks(_request: ControlCommandRequest, _descriptor: Any, _context: Any) -> ControlResult:
        snapshot = get_typescript_agent_port().snapshot()
        tasks = [
            item
            for item in snapshot["tasks"]
            if (
                item.get("parent_task_id") == state.task_id
                or state.task_id in tuple(item.get("lineage") or ())
            )
        ]
        task_ids = {str(item.get("task_id") or "") for item in tasks}
        children: dict[str, list[str]] = {state.task_id: []}
        lifecycle: dict[str, int] = {}
        nodes: list[dict[str, Any]] = []
        for item in sorted(
            tasks,
            key=lambda value: (
                len(tuple(value.get("lineage") or ())),
                int(value.get("sequence") or 0),
                str(value.get("task_id") or ""),
            ),
        ):
            task_id = str(item.get("task_id") or "")
            parent_id = str(item.get("parent_task_id") or state.task_id)
            if parent_id not in task_ids and parent_id != state.task_id:
                parent_id = state.task_id
            status = str(item.get("status") or "unknown")
            lifecycle[status] = lifecycle.get(status, 0) + 1
            children.setdefault(parent_id, []).append(task_id)
            children.setdefault(task_id, [])
            nodes.append(
                {
                    **dict(item),
                    "parent_id": parent_id,
                    "depth": max(1, len(tuple(item.get("lineage") or ()))),
                    "terminal": status in {"completed", "failed", "cancelled", "killed"},
                }
            )
        hierarchy = [
            {
                "parent_id": parent_id,
                "child_ids": sorted(child_ids),
            }
            for parent_id, child_ids in sorted(children.items())
        ]
        return ControlResult(
            display_text=f"{len(nodes)} canonical subagent tasks.",
            data={
                "schema": "zyra.subagent-task-projection/v1",
                "task_id": state.task_id,
                "run_id": state.run_id,
                "canonical_logical_owner": snapshot["canonical_logical_owner"],
                "durable_owner": "SubagentTaskStore",
                "registry_revision": snapshot["revision"],
                "tasks": nodes,
                "hierarchy": hierarchy,
                "lifecycle_counts": lifecycle,
                "active_task_ids": [
                    str(item["task_id"])
                    for item in nodes
                    if not bool(item["terminal"])
                ],
                "terminal_task_ids": [
                    str(item["task_id"])
                    for item in nodes
                    if bool(item["terminal"])
                ],
                "fixture_projection": False,
                "python_logical_fallback": False,
            },
            metadata={
                "state_owner": "SubagentTaskStore",
                "canonical_logical_owner": snapshot["canonical_logical_owner"],
                "read_only": True,
            },
        )

    handlers["subagent.tasks"] = subagent_tasks

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
        runtime = get_typescript_agent_port()
        if action == "list":
            snapshot = runtime.snapshot(parent_task_id=state.task_id)
            return {
                "ok": True,
                "summary": f"{len(snapshot['tasks'])} logical subagent tasks.",
                **snapshot,
            }
        try:
            record = runtime.get_task(task_id)
        except KeyError as error:
            raise RuntimeError(f"logical subagent task not found: {task_id}") from error
        if record.parent_task_id != state.task_id:
            raise RuntimeError("logical subagent does not belong to this parent task")
        if record.run_id != state.run_id:
            raise RuntimeError("logical subagent run authority mismatch")
        if action in {"status", "inspect"}:
            return {
                "ok": True,
                "summary": "Logical subagent state.",
                "task": record.safe_dict(),
                "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
                "durable_owner": "SubagentTaskStore",
            }
        if action not in {"kill", "steer"}:
            raise RuntimeError(f"unsupported logical subagent owner action: {action}")
        authorization = arguments.get("authorization")
        if not isinstance(authorization, Mapping):
            raise RuntimeError("mutating subagent control lacks an owner authorization")
        if (
            authorization.get("granted") is not True
            or authorization.get("exact") is not True
            or authorization.get("one_shot") is not True
            or str(authorization.get("owner") or "") != "SubagentTaskStore"
            or str(authorization.get("action") or "") != action
        ):
            raise RuntimeError("mutating subagent control authorization is invalid")
        nonce = str(arguments.get("control_nonce") or "")
        owner_idempotency_key = str(arguments.get("owner_idempotency_key") or "")
        expected_physical_lease_id = str(
            arguments.get("expected_physical_lease_id") or ""
        )
        expected_revision = arguments.get("expected_revision")
        expected_attempt = arguments.get("expected_attempt")
        expected_owner = str(arguments.get("expected_owner") or "")
        expected_parent = str(arguments.get("expected_parent_task_id") or "")
        if not nonce or not owner_idempotency_key or not expected_physical_lease_id:
            raise RuntimeError(
                "subagent control nonce/idempotency/physical lease binding is required"
            )
        control_request_id = f"subagent-control:{record.task_id}:{nonce}"
        transitions = tuple(record.payload.get("transitions") or ())
        same_request = tuple(
            item
            for item in transitions
            if isinstance(item, Mapping)
            and str(item.get("requestId") or "") == control_request_id
        )
        same_idempotency = tuple(
            item
            for item in transitions
            if isinstance(item, Mapping)
            and str(item.get("idempotencyKey") or "") == owner_idempotency_key
        )
        if any(
            str(item.get("idempotencyKey") or "") != owner_idempotency_key
            for item in same_request
        ):
            raise RuntimeError(
                "subagent control nonce was already committed with another idempotency key"
            )
        if any(
            str(item.get("requestId") or "") != control_request_id
            for item in same_idempotency
        ):
            raise RuntimeError(
                "subagent control idempotency key was already committed with another nonce"
            )
        exact_owner_replay = any(
            str(item.get("idempotencyKey") or "") == owner_idempotency_key
            for item in same_request
        )
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise RuntimeError("subagent control expected revision is invalid")
        if record.revision != expected_revision and not exact_owner_replay:
            raise RuntimeError(
                f"subagent revision conflict: expected {expected_revision}, actual {record.revision}"
            )
        if not isinstance(expected_attempt, int) or record.payload.get("attempt") != expected_attempt:
            raise RuntimeError("subagent attempt binding mismatch")
        if expected_owner != "typescript.E03AgentControlCoordinator":
            raise RuntimeError("subagent canonical owner binding mismatch")
        if expected_parent != state.task_id:
            raise RuntimeError("subagent parent binding mismatch")
        physical_dispatch = (
            dict(record.payload.get("physical_dispatch") or {})
            if isinstance(record.payload.get("physical_dispatch"), Mapping)
            else {}
        )
        if (
            str(physical_dispatch.get("lease_id") or "")
            != expected_physical_lease_id
        ):
            raise RuntimeError("subagent physical lease projection binding mismatch")
        pool_api = get_worker_pool_api()
        physical_lease = pool_api.pool.store.get_lease(expected_physical_lease_id)
        physical_binding = pool_api.integration.repository.binding_for_lease(
            expected_physical_lease_id
        )
        if physical_lease is None or physical_binding is None:
            raise RuntimeError("subagent physical lease/binding no longer exists")
        if (
            physical_lease.task_id != record.task_id
            or physical_lease.run_id != state.run_id
            or physical_binding.task_id != record.task_id
            or physical_binding.run_id != state.run_id
            or physical_binding.lease_id != expected_physical_lease_id
            or physical_binding.attempt_id != physical_lease.attempt_id
            or physical_binding.attempt_id
            != str(physical_dispatch.get("attempt_id") or "")
            or physical_binding.binding_id
            != str(physical_dispatch.get("integration_binding_id") or "")
            or physical_binding.attempt_number != expected_attempt
        ):
            raise RuntimeError("subagent physical attempt/binding authority mismatch")
        if (
            (physical_lease.terminal or physical_binding.terminal)
            and not exact_owner_replay
        ):
            raise RuntimeError("subagent physical lease/binding is already terminal")
        tool_name = "agent_kill" if action == "kill" else "agent_message"
        tool_arguments = {
            "task_id": record.task_id,
            "expected_revision": expected_revision,
            "idempotency_key": owner_idempotency_key,
            "control_request_id": control_request_id,
            "control_nonce": nonce,
            "owner_idempotency_key": owner_idempotency_key,
            "expected_attempt": expected_attempt,
            "expected_physical_lease_id": expected_physical_lease_id,
            "_canonical_control_authorization": dict(authorization),
        }
        if action == "kill":
            tool_arguments["reason"] = str(
                arguments.get("reason") or "Killed by an explicit operator subagent control."
            )
        else:
            tool_arguments["message"] = str(arguments.get("instruction") or "")
        run = _run_typescript_agent_request(
            state,
            tool_name=tool_name,
            arguments=tool_arguments,
            request_id=control_request_id,
            session_id=(
                f"{record.parent_session_id}:control:"
                f"{request.request_id}:{nonce}"
            ),
            canonical_agent_parent_session_id=record.parent_session_id,
        )
        persist_events(store, run.event_records)
        updated = runtime.get_task(record.task_id)
        if not run.worker_result.ok:
            runtime_metadata = dict(run.worker_result.metadata or {})
            runtime_event_message = next(
                (
                    str(query_session.get("message") or "")
                    for event in reversed(run.event_records)
                    if isinstance((payload := event.payload), Mapping)
                    and isinstance(
                        (query_session := payload.get("query_session")),
                        Mapping,
                    )
                    and query_session.get("message")
                ),
                "",
            )
            raise RuntimeError(
                str(
                    runtime_event_message
                    or
                    runtime_metadata.get("typescript_runtime_error_message")
                    or run.worker_result.error
                    or f"typescript subagent {action} rejected"
                )
            )
        if action == "kill" and updated.status.value != "killed":
            raise RuntimeError(
                "canonical subagent owner did not commit the killed state: "
                f"status={updated.status.value}, task_error={updated.payload.get('error') or ''}, "
                f"runtime_error={run.worker_result.error or ''}"
            )
        if action == "steer":
            message = str(arguments.get("instruction") or "")
            if not any(
                isinstance(item, Mapping)
                and str(item.get("body") or "") == message
                for item in tuple(updated.payload.get("messages") or ())
            ):
                raise RuntimeError(
                    "canonical subagent owner did not commit the steering message"
                )
        physical_control = None
        if action == "kill":
            physical = get_worker_pool_api().integration.control.submit_and_apply(
                ControlKind.CANCEL,
                claim_owner="subagent-control-command",
                actor_id=str(request.metadata.get("actor_id") or "control-command"),
                reason=str(arguments.get("reason") or "operator subagent kill"),
                idempotency_key=(
                    f"subagent-command-kill:{record.task_id}:{owner_idempotency_key}"
                ),
                task_id=record.task_id,
                run_id=state.run_id,
                lease_id=expected_physical_lease_id,
                binding_id=physical_binding.binding_id,
            )
            physical_control = physical.to_dict()
            if physical.phase.value != "applied":
                raise RuntimeError(
                    physical.error or "canonical physical subagent kill did not apply"
                )
        return {
            "ok": True,
            "summary": (
                f"Subagent {record.task_id} killed by the canonical E03 owner."
                if action == "kill"
                else f"Steering message committed for subagent {record.task_id}."
            ),
            "task": updated.safe_dict(),
            "action": action,
            "revision": updated.revision,
            "replayed": updated.revision == record.revision,
            "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
            "durable_owner": "SubagentTaskStore",
            "python_logical_fallback": False,
            "physical_worker_control": physical_control,
            "receipt": {
                "owner": "SubagentTaskStore",
                "canonical_logical_owner": "typescript.E03AgentControlCoordinator",
                "authorization_id": str(authorization.get("authorization_id") or ""),
                "nonce": nonce,
                "idempotency_key": owner_idempotency_key,
                "expected_revision": expected_revision,
                "committed_revision": updated.revision,
                "attempt": expected_attempt,
                "physical_lease_id": expected_physical_lease_id,
                "physical_attempt_id": physical_binding.attempt_id,
                "physical_binding_id": physical_binding.binding_id,
                "parent_task_id": expected_parent,
                "task_id": record.task_id,
                "action": action,
            },
            "worker_result": to_jsonable(run.worker_result),
            "event_ids": [item.event_id for item in run.event_records],
        }

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
            checkpoint_ref=str(
                state.metadata.get("session_checkpoint_ref")
                or f"sqlite-session:{task_id}:{session_revision}:{epoch}"
            ),
            transcript=tuple(state.metadata.get("main_messages") or ()),
            metadata={"owner": "SQLiteStore", "owner_unit": "M1-S03D-02"},
        )

    def session_mutation(request: Any, before: Any) -> dict[str, Any]:
        if request.action is SessionAction.CLEAR:
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
            after_ref = f"sqlite-session:{state.task_id}:{before.revision + 1}:{before.epoch + 1}"
            state.metadata["session_checkpoint_ref"] = after_ref
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
                "checkpoint_ref": after_ref,
                "result": {"cleared_messages": len(messages), "checkpoint": checkpoint},
                "event_ids": [event.event_id],
                "metadata": {"same_session_new_epoch": True, "state_owner": "SQLiteStore"},
            }
        if request.action not in {SessionAction.REWIND, SessionAction.RESUME}:
            raise RuntimeError(f"session action is not connected to this canonical owner: {request.action.value}")
        checkpoint_ref = str(
            request.arguments.get("checkpoint_ref")
            or request.arguments.get("target")
            or ""
        ).strip()
        if not checkpoint_ref:
            raise RuntimeError(f"{request.action.value} requires an exact recovery checkpoint")
        target_session_id = str(
            request.arguments.get("target_session_id")
            or before.session_id
        ).strip()
        if target_session_id != before.session_id:
            raise RuntimeError("resume target is not owned by the active canonical session")
        recovery_runtime = get_recovery_runtime_api(store)
        canonical_checkpoint = recovery_runtime.application.store.checkpoint(
            checkpoint_ref
        )
        if canonical_checkpoint is None:
            raise RuntimeError(
                f"canonical recovery checkpoint not found: {checkpoint_ref}"
            )
        if (
            canonical_checkpoint.run_id != state.run_id
            or canonical_checkpoint.task_id != state.task_id
            or canonical_checkpoint.session_id != before.session_id
        ):
            raise RuntimeError(
                "canonical recovery checkpoint identity does not match the active session"
            )
        recovery = recovery_runtime.application.resume_checkpoint(
            checkpoint_ref,
            {
                "run_id": state.run_id,
                "task_id": state.task_id,
                "session_id": before.session_id,
                "workflow_signature": canonical_checkpoint.workflow_signature,
                "graph_signature": canonical_checkpoint.graph_signature,
                "topology_signature": canonical_checkpoint.topology_signature,
                "owner_refs": dict(canonical_checkpoint.owner_refs),
                "version_refs": dict(canonical_checkpoint.version_refs),
                "candidate_step_ids": list(request.arguments.get("candidate_step_ids") or ()),
                "compact_first": bool(request.arguments.get("compact_first", False)),
                "rebind_worker": bool(request.arguments.get("rebind_worker", False)),
                "rebind_graph": bool(request.arguments.get("rebind_graph", False)),
                "idempotency_key": request.idempotency_key or request.request_id,
            },
        )
        next_revision = before.revision + 1
        state.metadata["session_control_revision"] = next_revision
        state.metadata["session_checkpoint_ref"] = checkpoint_ref
        state.metadata.setdefault("control_mutations", []).append({
            "request_id": request.request_id,
            "command": request.action.value,
            "checkpoint_ref": checkpoint_ref,
            "recovery_owner": "RecoveryApplication.resume_checkpoint",
        })
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            event_type=EventType.TOPOLOGY_ROUTE,
            payload={
                "schema": "zyra.topology-time-travel/v1",
                "recovery_runtime": {
                    "phase": "checkpoint_resumed",
                    "request_id": request.request_id,
                    "command_id": request.causation_id,
                    "checkpoint_id": checkpoint_ref,
                    "session_id": before.session_id,
                    "action": request.action.value,
                    "receipt": recovery.get("receipt"),
                    "owner_receipts": recovery.get("owner_receipts"),
                    "canonical_owner": "RecoveryApplication.resume_checkpoint",
                },
            },
        )
        persist_events(store, [event])
        store.save_checkpoint(state)
        return {
            "changed": True,
            "session_id": before.session_id,
            "revision": next_revision,
            "checkpoint_ref": checkpoint_ref,
            "result": {
                "rewound_to": checkpoint_ref,
                "checkpoint_ref": checkpoint_ref,
                "recovery": recovery,
            },
            "event_ids": [event.event_id],
            "metadata": {
                "state_owner": "RecoveryApplication.resume_checkpoint",
                "exact_resume": True,
                "same_session": True,
            },
        }

    session_runtime = SessionControlRuntime(
        SessionControlStore(control_state_path() / "sessions" / f"{state.task_id}.json"),
        CallbackSessionOwner(session_snapshot, session_mutation),
    )

    def owner_authorizer(owner: str, action: str, request: Any, arguments: Any) -> Any:
        expected_command = {
            SessionAction.CLEAR.value: "/clear",
            SessionAction.REWIND.value: "/rewind",
            SessionAction.RESUME.value: "/resume",
        }.get(action)
        subagent_grant = (
            owner == "SubagentTaskStore"
            and request.canonical_name == "/agents"
            and action in {"kill", "steer"}
            and not bool(request.metadata.get("sealed", False))
            and bool(str(arguments.get("task_id") or ""))
            and bool(str(arguments.get("control_nonce") or ""))
            and bool(str(arguments.get("owner_idempotency_key") or ""))
            and bool(str(arguments.get("expected_physical_lease_id") or ""))
            and isinstance(arguments.get("expected_revision"), int)
            and isinstance(arguments.get("expected_attempt"), int)
            and str(arguments.get("expected_owner") or "")
            == "typescript.E03AgentControlCoordinator"
            and str(arguments.get("expected_parent_task_id") or "") == state.task_id
        )
        granted = subagent_grant or (
            owner == "CanonicalSessionStore"
            and expected_command == request.canonical_name
            and not bool(request.metadata.get("sealed", False))
        )
        permission_action = (
            f"subagent.{action}"
            if owner == "SubagentTaskStore"
            else f"session.{action}"
        )
        return deterministic_owner_authorization(
            owner=owner,
            action=action,
            request=request,
            actor_id=str(request.metadata.get("actor_id") or "api-user"),
            authority={
                "source": "RuntimeControlDispatcher.permission_authorize",
                "permission_action": permission_action,
                "request_id": request.request_id,
                "target_task_id": str(arguments.get("task_id") or ""),
                "control_nonce": str(arguments.get("control_nonce") or ""),
                "owner_idempotency_key": str(
                    arguments.get("owner_idempotency_key") or ""
                ),
                "expected_revision": arguments.get("expected_revision"),
                "expected_physical_lease_id": str(
                    arguments.get("expected_physical_lease_id") or ""
                ),
            },
            granted=granted,
            reason=(
                (
                    f"explicit {request.canonical_name} uses the canonical "
                    f"{owner} owner for {action}"
                )
                if granted
                else "owner action not granted"
            ),
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
        "session.rewind",
        "session.resume",
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

    def control_owner_snapshot(
        request: ControlCommandRequest,
        *,
        require_worker: bool = False,
        allow_terminal_lease: bool = False,
    ) -> dict[str, Any]:
        arguments = dict(request.arguments)
        if request.task_id != state.task_id or request.run_id != state.run_id:
            raise RuntimeError(
                "timeline control request crossed canonical task/run ownership"
            )
        worker_api = get_worker_pool_api()
        projection = dict(state.metadata.get("worker_pool") or {})
        expected_task_id = str(arguments.get("expected_task_id") or "")
        expected_run_id = str(arguments.get("expected_run_id") or "")
        expected_worker_id = str(arguments.get("expected_worker_id") or "")
        expected_lease_id = str(arguments.get("expected_lease_id") or "")
        expected_attempt_id = str(arguments.get("expected_attempt_id") or "")
        expected_owner_revision = arguments.get("expected_owner_revision")
        if expected_task_id and expected_task_id != state.task_id:
            raise RuntimeError(
                "timeline control expected task is not owned by this request"
            )
        if expected_run_id and expected_run_id != state.run_id:
            raise RuntimeError(
                "timeline control expected run is not owned by this request"
            )
        actual_worker_id = str(projection.get("worker_id") or "")
        actual_lease_id = str(projection.get("lease_id") or "")
        actual_attempt_id = str(projection.get("attempt_id") or "")
        if expected_worker_id and expected_worker_id != actual_worker_id:
            raise RuntimeError(
                "worker owner changed before timeline control execution"
            )
        if expected_lease_id and expected_lease_id != actual_lease_id:
            raise RuntimeError(
                "worker lease changed before timeline control execution"
            )
        if expected_attempt_id and expected_attempt_id != actual_attempt_id:
            raise RuntimeError(
                "worker attempt changed before timeline control execution"
            )
        if expected_owner_revision not in {None, ""}:
            expected = int(expected_owner_revision)
            actual = int(worker_api.pool.store.revision)
            if expected != actual:
                raise RuntimeError(
                    "worker owner revision changed before timeline control "
                    f"execution: expected={expected}, actual={actual}"
                )
        lease = (
            worker_api.pool.store.get_lease(actual_lease_id)
            if actual_lease_id
            else None
        )
        worker = (
            worker_api.pool.store.get_worker(actual_worker_id)
            if actual_worker_id
            else None
        )
        if require_worker and (worker is None or lease is None):
            raise RuntimeError(
                "canonical worker/lease owner is unavailable for timeline control"
            )
        if lease is not None:
            if lease.task_id != state.task_id or lease.run_id != state.run_id:
                raise RuntimeError(
                    "canonical worker lease belongs to another task or run"
                )
            if not allow_terminal_lease and lease.terminal:
                raise RuntimeError(
                    "canonical worker lease is already terminal"
                )
        return {
            "worker_api": worker_api,
            "projection": projection,
            "worker": worker,
            "lease": lease,
            "worker_id": actual_worker_id,
            "lease_id": actual_lease_id,
            "attempt_id": actual_attempt_id,
            "owner_revision": int(worker_api.pool.store.revision),
        }

    def persist_timeline_control_effect(
        request: ControlCommandRequest,
        *,
        action: str,
        owner: str,
        phase: str,
        changed: bool,
        receipt: Mapping[str, Any],
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=str(
                request.arguments.get("node_id")
                or state.root_node_id
            ),
            event_type=EventType.TOPOLOGY_ROUTE,
            payload={
                "schema": "zyra.timeline-recovery-control-effect/v1",
                "phase": phase,
                "command": request.canonical_name,
                "command_id": request.command_id,
                "control_command_id": request.command_id,
                "request_id": request.request_id,
                "correlation_id": request.request_id,
                "causation_id": request.command_id,
                "control_runtime": {
                    "action": action,
                    "phase": phase,
                    "owner": owner,
                    "changed": changed,
                    "receipt": copy.deepcopy(dict(receipt)),
                    "before": copy.deepcopy(dict(before or {})),
                    "after": copy.deepcopy(dict(after or {})),
                    "manual": True,
                    "sealed": False,
                    "state_owner_unchanged": True,
                },
            },
        )
        persist_events(store, [event])
        return event

    def projected_worker_control_events(
        worker_api: Any,
        *,
        after_sequence: int,
    ) -> list[EventRecord]:
        events = [
            event
            for event in worker_api.pool.events.project_after(
                worker_api.pool.store,
                after_sequence,
            )
            if event.task_id == state.task_id and event.run_id == state.run_id
        ]
        if events:
            persist_events(store, events)
        return events

    def kill_control(
        request: ControlCommandRequest,
        _descriptor: Any,
        _context: Any,
    ) -> ControlResult:
        owner = control_owner_snapshot(request, require_worker=True)
        worker_api = owner["worker_api"]
        journal = worker_api.pool.store.journal(limit=10000)
        after_sequence = journal[-1].sequence if journal else 0
        reason = str(
            request.arguments.get("reason")
            or request.arguments.get("raw")
            or "Timeline operator requested a fenced kill."
        ).strip()
        if not reason:
            raise ValueError("kill control requires a reason")
        target = str(request.arguments.get("target") or "task").strip().lower()
        cancel = worker_api.integration.control.submit_and_apply(
            ControlKind.CANCEL,
            claim_owner="timeline-control",
            actor_id=str(request.metadata.get("actor_id") or "timeline-operator"),
            reason=reason,
            idempotency_key=f"{request.idempotency_key}:cancel",
            task_id=state.task_id,
            run_id=state.run_id,
            worker_id=str(owner["worker_id"]),
            attempt_id=str(owner["attempt_id"]),
            lease_id=str(owner["lease_id"]),
        )
        controls = [cancel]
        if target in {"worker", "worker-and-task", "worker_and_task"}:
            stop = worker_api.integration.control.submit_and_apply(
                ControlKind.STOP,
                claim_owner="timeline-control",
                actor_id=str(
                    request.metadata.get("actor_id")
                    or "timeline-operator"
                ),
                reason=reason,
                idempotency_key=f"{request.idempotency_key}:stop",
                task_id=state.task_id,
                run_id=state.run_id,
                worker_id=str(owner["worker_id"]),
            )
            controls.append(stop)
        worker_events = projected_worker_control_events(
            worker_api,
            after_sequence=after_sequence,
        )
        after_projection = {
            "worker": (
                worker_api.pool.store.require_worker(
                    str(owner["worker_id"])
                ).to_dict()
            ),
            "lease": (
                worker_api.pool.store.require_lease(
                    str(owner["lease_id"])
                ).to_dict()
            ),
            "owner_revision": worker_api.pool.store.revision,
        }
        effect = persist_timeline_control_effect(
            request,
            action="kill",
            owner="WorkerControlRuntime",
            phase="applied",
            changed=any(
                bool(item.effect.get("changed", True))
                for item in controls
            ),
            receipt={
                "controls": [item.to_dict() for item in controls],
                "worker_event_ids": [event.event_id for event in worker_events],
            },
            before={
                "worker_pool": owner["projection"],
                "owner_revision": owner["owner_revision"],
            },
            after=after_projection,
        )
        state.metadata.setdefault("control_mutations", []).append(
            {
                "request_id": request.request_id,
                "command": request.canonical_name,
                "action": "kill",
                "control_ids": [item.command_id for item in controls],
                "event_id": effect.event_id,
                "canonical_owner": "WorkerControlRuntime",
            }
        )
        store.save_checkpoint(state)
        return ControlResult(
            display_text="Worker lease fenced by the canonical worker control runtime.",
            data={
                "action": "kill",
                "phase": "applied",
                "controls": [item.to_dict() for item in controls],
                "before": {
                    "worker_pool": owner["projection"],
                    "owner_revision": owner["owner_revision"],
                },
                "after": after_projection,
                "observed_event_ids": [
                    *[event.event_id for event in worker_events],
                    effect.event_id,
                ],
                "control_event": to_jsonable(effect),
            },
            metadata={
                "runtime_status": "stateful",
                "canonical_owner": "WorkerControlRuntime",
                "old_fence_blocks_late_commit": True,
            },
        )

    def recover_from_timeline_control(
        request: ControlCommandRequest,
        *,
        action: str,
        source: str,
        source_kind: str,
        refs: Mapping[str, Any],
        summary: str,
        context_metadata: Mapping[str, Any] | None = None,
    ) -> ControlResult:
        application = get_recovery_runtime_api(store).application
        result = application.recover(
            {
                "source": source,
                "source_kind": source_kind,
                "reason_code": f"timeline.control.{action}",
                "summary": summary,
                "refs": {
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                    **{
                        key: value
                        for key, value in refs.items()
                        if value not in {None, ""}
                    },
                },
                "retryable": action == "retry",
                "terminal": False,
                "recoverable": True,
                "causation_id": request.command_id,
                "correlation_id": request.request_id,
                "details": {
                    "timeline_control": True,
                    "manual_operator": True,
                    "canonical_name": request.canonical_name,
                    "instruction": str(
                        request.arguments.get("instruction")
                        or request.arguments.get("raw")
                        or ""
                    )[:8192],
                },
            },
            source=source,
            context_overrides={
                "mode": "interactive",
                "metadata": {
                    "timeline_control": True,
                    "control_command_id": request.command_id,
                    "operator_actor_id": str(
                        request.metadata.get("actor_id")
                        or "timeline-operator"
                    ),
                    **dict(context_metadata or {}),
                },
            },
            apply=True,
            idempotency_key=request.idempotency_key,
        )
        value = result.to_dict()
        execution = value.get("execution") or {}
        outcome = (
            execution.get("outcome")
            if isinstance(execution, Mapping)
            else {}
        ) or {}
        success = bool(outcome.get("success", False))
        if not success:
            raise RuntimeError(
                f"canonical recovery action {action} did not apply successfully"
            )
        before = dict(
            request.arguments.get("before")
            if isinstance(request.arguments.get("before"), Mapping)
            else {}
        )
        after = (
            get_recovery_runtime_api(store)
            .application.task_view(state.task_id)
        )
        effect = persist_timeline_control_effect(
            request,
            action=action,
            owner="RecoveryApplication",
            phase="applied",
            changed=True,
            receipt=value,
            before=before,
            after={
                "plan_id": result.plan.plan_id,
                "selected_action": result.plan.decision.selected.action.value,
                "task_recovery": dict(
                    (store.load_task(state.task_id) or state).metadata.get(
                        "recovery_runtime"
                    )
                    or {}
                ),
            },
        )
        latest_state = store.load_task(state.task_id) or state
        latest_state.metadata.setdefault("control_mutations", []).append(
            {
                "request_id": request.request_id,
                "command": request.canonical_name,
                "action": action,
                "plan_id": result.plan.plan_id,
                "selected_action": (
                    result.plan.decision.selected.action.value
                ),
                "event_id": effect.event_id,
                "canonical_owner": "RecoveryApplication",
            }
        )
        store.save_checkpoint(latest_state)
        synchronize_canonical_state()
        event_ids = [
            str(item.get("event_id") or "")
            for item in value.get("event_receipts") or ()
            if isinstance(item, Mapping) and item.get("event_id")
        ]
        return ControlResult(
            display_text=(
                f"Recovery control {action} applied through "
                f"{result.plan.decision.selected.action.value}."
            ),
            data={
                "action": action,
                "phase": "applied",
                "recovery": value,
                "task_recovery": dict(
                    latest_state.metadata.get("recovery_runtime") or {}
                ),
                "observed_event_ids": [*event_ids, effect.event_id],
                "control_event": to_jsonable(effect),
            },
            metadata={
                "runtime_status": "stateful",
                "canonical_owner": "RecoveryApplication",
                "policy_owner": "RecoveryDecisionRuntime",
                "selected_action": (
                    result.plan.decision.selected.action.value
                ),
            },
        )

    def steer_control(
        request: ControlCommandRequest,
        _descriptor: Any,
        _context: Any,
    ) -> ControlResult:
        instruction = str(
            request.arguments.get("instruction")
            or request.arguments.get("requirement")
            or request.arguments.get("raw")
            or ""
        ).strip()
        if not instruction:
            raise ValueError("steer control requires an instruction")
        worker_api = get_worker_pool_api()
        graph_id = worker_api.ensure_task_graph(state)
        node_id = str(
            request.arguments.get("node_id") or state.root_node_id
        )
        graph = worker_api.graph_custody.current(graph_id)
        if node_id not in graph.node_map:
            raise RuntimeError(
                "steer target node is not owned by the canonical graph"
            )
        expected_graph_revision = request.arguments.get(
            "expected_graph_revision"
        )
        if expected_graph_revision not in {None, ""}:
            actual = worker_api.topology.version_ref(graph_id).revision
            if int(expected_graph_revision) != int(actual):
                raise RuntimeError(
                    "graph owner revision changed before steer execution"
                )
        return recover_from_timeline_control(
            request,
            action="steer",
            source="control_runtime",
            source_kind="requirement_changed",
            refs={
                "request_id": request.request_id,
                "node_id": node_id,
                "graph_id": graph_id,
            },
            summary=instruction,
            context_metadata={
                "requirement_change_is_fault": False,
                "operator_instruction": instruction,
            },
        )

    def retry_control(
        request: ControlCommandRequest,
        _descriptor: Any,
        _context: Any,
    ) -> ControlResult:
        maximum_attempts_raw = request.arguments.get("maximum_attempts", 1)
        if isinstance(maximum_attempts_raw, bool):
            raise ValueError(
                "retry maximum_attempts must be an integer between 1 and 8"
            )
        try:
            maximum_attempts = int(maximum_attempts_raw)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "retry maximum_attempts must be an integer between 1 and 8"
            ) from error
        if maximum_attempts < 1 or maximum_attempts > 8:
            raise ValueError(
                "retry maximum_attempts must be an integer between 1 and 8"
            )
        if request.arguments.get("bounded_retry") is False:
            raise ValueError("retry must remain bounded")
        control_owner_snapshot(
            request,
            require_worker=False,
            allow_terminal_lease=True,
        )
        node_id = str(
            request.arguments.get("node_id") or state.root_node_id
        )
        reason = str(
            request.arguments.get("reason")
            or request.arguments.get("raw")
            or "Timeline operator requested one bounded retry."
        ).strip()
        return recover_from_timeline_control(
            request,
            action="retry",
            source="tool_runtime",
            source_kind="tool_error",
            refs={
                "request_id": request.request_id,
                "tool_call_id": str(
                    request.arguments.get("tool_call_id")
                    or f"timeline-control:{request.command_id}"
                ),
                "node_id": node_id,
            },
            summary=reason,
            context_metadata={
                "operator_retry": True,
                "maximum_attempts": maximum_attempts,
            },
        )

    def reassign_control(
        request: ControlCommandRequest,
        _descriptor: Any,
        _context: Any,
    ) -> ControlResult:
        owner = control_owner_snapshot(
            request,
            require_worker=True,
            allow_terminal_lease=True,
        )
        worker_api = owner["worker_api"]
        graph_id = worker_api.ensure_task_graph(state)
        reason = str(
            request.arguments.get("reason")
            or request.arguments.get("raw")
            or "Timeline operator requested worker reassignment."
        ).strip()
        return recover_from_timeline_control(
            request,
            action="reassign",
            source="worker_handoff",
            source_kind="worker_lost",
            refs={
                "request_id": request.request_id,
                "worker_id": owner["worker_id"],
                "worker_lease_id": owner["lease_id"],
                "attempt_id": owner["attempt_id"],
                "node_id": str(
                    request.arguments.get("node_id")
                    or state.root_node_id
                ),
                "graph_id": graph_id,
            },
            summary=reason,
            context_metadata={
                "operator_reassign": True,
                "previous_worker_id": owner["worker_id"],
                "previous_lease_id": owner["lease_id"],
            },
        )

    handlers["task.change"] = requirement_change
    handlers["task.inject"] = fault_inject
    handlers["watchdog.control"] = watchdog_control
    handlers["artifact.export"] = legacy_real_mutation
    handlers["task.evaluate"] = legacy_real_mutation
    handlers["recovery.kill"] = kill_control
    handlers["recovery.steer"] = steer_control
    handlers["recovery.retry"] = retry_control
    handlers["recovery.reassign"] = reassign_control

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
        claude_source = claude_code_source_identity()
        browser_source = browser_use_source_identity()
        result["summary"] = "Development runtime health checks."
        result["data"] = {
            "project_root_exists": PROJECT_ROOT.exists(),
            "legacy_source_pools": {
                "status": "retired",
                "availability": "not_applicable",
                "filesystem_required": False,
                "fallback_available": False,
                "sources": [
                    claude_source.to_dict(),
                    browser_source.to_dict(),
                ],
            },
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
