from __future__ import annotations

import json
import os
import threading
import sys
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
]
for package_path in PACKAGE_PATHS:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, create_task_state, to_jsonable
from zyra_core.event_log import append_event as append_jsonl_event
from zyra_memory import CompactPolicy, MemoryFabric, SQLiteStore
from zyra_orchestration import GraphExecutionContext, cancel_task_graph, ensure_default_graph, run_task_graph
from zyra_symbolic import apply_failure_injection, apply_requirement_change
from zyra_scheduler import (
    ResourceScheduler,
    RuntimeWatchdog,
    WorkerPool,
    source_to_target_ledger,
)
from zyra_commands import default_command_registry, parse_slash_command
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
    PermissionOperation,
    PermissionRequestStatus,
    PermissionRule,
    QueryInputProcessor,
    QuerySessionIntegrationRuntime,
    SessionAcceptanceRuntime,
    SessionApiProjectionBuilder,
    SessionFoundationAuditor,
    SessionLifecycleRuntime,
    SessionLineageRuntime,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
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
from zyra_skills import default_skill_registry
from zyra_workers import (
    BrowserWorkerRuntime,
    CodeWorkerRuntime,
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
    configured = Path(os.environ.get("ZYRA_TOOL_WORKSPACE", "tmp/workspace"))
    if configured.is_absolute():
        return configured
    return PROJECT_ROOT / configured


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

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = _path_parts(parsed.path)
        store = get_store()

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
            self._send_json(
                HTTPStatus.OK,
                {"commands": [to_jsonable(command) for command in default_command_registry().list()]},
            )
            return

        if parts == ["skills"]:
            self._send_json(
                HTTPStatus.OK,
                {"skills": [to_jsonable(skill) for skill in default_skill_registry().list()]},
            )
            return

        if parts == ["tools"]:
            self._send_json(
                HTTPStatus.OK,
                {"tools": [to_jsonable(tool) for tool in default_tool_registry().list()]},
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
            self._send_json(HTTPStatus.OK, browser_use_health_summary(inspect_browser_use_runtime(PROJECT_ROOT)))
            return

        if parts == ["permissions"]:
            permission_store = get_permission_store()
            self._send_json(
                HTTPStatus.OK,
                {
                    "rules": [to_jsonable(rule) for rule in permission_store.list_rules()],
                    "requests": [to_jsonable(request) for request in permission_store.list_requests()],
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

        if parts == ["tasks"]:
            user_goal = str(payload.get("goal") or "Unspecified long-horizon task")
            auto_run = payload.get("auto_run", True) is not False
            state, created_event = make_task_created_event(user_goal)
            events = [created_event, *ensure_default_graph(state)]
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
            persist_events(store, events)
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.OK,
                {"task": to_jsonable(state), "events": [to_jsonable(event) for event in events]},
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
            parsed_command = parse_slash_command(
                text,
                run_id=state.run_id,
                task_id=state.task_id,
            )
            if parsed_command is None:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "unknown_or_invalid_command", "text": text},
                )
                return

            event = control_event_from_command(
                parsed_command.control_command,
                node_id=state.root_node_id,
            )
            applied_events = _apply_control_event_to_state(state, event)
            persist_events(store, [event, *applied_events])
            command_result = _command_result_for_event(state, event, store)
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "task": to_jsonable(state),
                    "command": to_jsonable(parsed_command.control_command),
                    "command_result": command_result,
                    "event": to_jsonable(event),
                    "events": [to_jsonable(item) for item in [event, *applied_events]],
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
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            skill_name = str(payload.get("skill_name") or payload.get("skill") or "").strip()
            skill = default_skill_registry().get(skill_name)
            if skill is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "skill_not_found", "skill_name": skill_name},
                )
                return
            arguments = payload.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            event = _skill_invocation_event(
                state,
                skill,
                arguments=arguments,
                node_id=str(payload.get("node_id") or state.root_node_id),
            )
            _apply_skill_invocation_to_state(state, event)
            persist_events(store, [event])
            store.save_checkpoint(state)
            self._send_json(
                HTTPStatus.CREATED,
                {
                    "task": to_jsonable(state),
                    "skill": to_jsonable(skill),
                    "event": to_jsonable(event),
                    "skill_result": {
                        "ok": True,
                        "summary": f"Skill {skill.name} invocation recorded for {skill.preferred_runtime}.",
                        "runtime_status": "recorded",
                        "data": event.payload["skill_invocation"],
                    },
                },
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
            call = ToolCall(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=str(payload.get("node_id") or state.root_node_id),
                tool_name=tool_name,
                arguments=arguments,
            )
            context = ToolExecutionContext.for_workspace(
                workspace_root=tool_workspace_path(),
                artifact_root=artifact_root_path(),
                permission_store=get_permission_store(),
                event_reader=store.task_events,
                checkpoint_reader=lambda task_id: _checkpoint_json(store, task_id),
            )
            result = ToolExecutor(context).execute(call)
            event = tool_result_event(call, result)
            if result.artifacts:
                state.artifacts.extend(result.artifacts)
            state.budget.tool_calls += 1
            state.updated_at = event.created_at
            persist_events(store, [event])
            store.save_checkpoint(state)
            status = HTTPStatus.CREATED if result.ok else HTTPStatus.CONFLICT
            self._send_json(
                status,
                {
                    "task": to_jsonable(state),
                    "tool_call": to_jsonable(call),
                    "tool_result": to_jsonable(result),
                    "event": to_jsonable(event),
                },
            )
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "workers" and parts[3] == "code":
            self._execute_code_worker_post(store, parts[1], payload)
            return

        if len(parts) == 4 and parts[0] == "tasks" and parts[2] == "workers" and parts[3] == "browser":
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            constraints = payload.get("constraints")
            if not isinstance(constraints, dict):
                constraints = {}
            if "browser_plan" in payload and "browser_plan" not in constraints:
                constraints["browser_plan"] = payload["browser_plan"]
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=str(payload.get("node_id") or state.root_node_id),
                worker_name="BrowserWorker",
                constraints=constraints,
            )
            try:
                run_result = BrowserWorkerRuntime(
                    project_root=PROJECT_ROOT,
                    workspace_root=tool_workspace_path(),
                    artifact_root=artifact_root_path(),
                ).run(request)
            except Exception as error:  # noqa: BLE001 - API must report browser worker startup/runtime failures.
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "browser_worker_failed", "message": str(error)},
                )
                return

            if run_result.worker_result.artifacts:
                state.artifacts.extend(run_result.worker_result.artifacts)
            state.budget.tool_calls += sum(1 for event in run_result.event_records if "browser_result" in event.payload)
            if run_result.event_records:
                state.updated_at = run_result.event_records[-1].created_at
            persist_events(store, run_result.event_records)
            store.save_checkpoint(state)
            status = HTTPStatus.CREATED if run_result.worker_result.ok else HTTPStatus.CONFLICT
            self._send_json(
                status,
                {
                    "task": to_jsonable(state),
                    "worker_request": to_jsonable(request),
                    "worker_result": to_jsonable(run_result.worker_result),
                    "events": [to_jsonable(event) for event in run_result.event_records],
                },
            )
            return

        if parts == ["permissions", "rules"]:
            try:
                rule = PermissionRule(
                    operation=PermissionOperation(str(payload.get("operation") or PermissionOperation.SHELL)),
                    pattern=str(payload.get("pattern") or ""),
                    effect=PermissionEffect(str(payload.get("effect") or PermissionEffect.ASK)),
                    reason=str(payload.get("reason") or ""),
                )
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_permission_rule", "message": str(error)})
                return
            if not rule.pattern:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_pattern"})
                return
            stored_rule = get_permission_store().add_rule(rule)
            self._send_json(HTTPStatus.CREATED, {"rule": to_jsonable(stored_rule)})
            return

        if len(parts) == 4 and parts[0] == "permissions" and parts[1] == "requests" and parts[3] == "resolve":
            try:
                status = PermissionRequestStatus(str(payload.get("status") or ""))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_permission_request_status"})
                return
            resolved = get_permission_store().resolve_request(
                parts[2],
                status,
                create_rule=payload.get("create_rule") is True,
            )
            if resolved is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "permission_request_not_found"})
                return
            self._send_json(HTTPStatus.OK, {"request": to_jsonable(resolved)})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": parsed.path})

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
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=node_id,
                worker_name="CodeWorkerRuntime",
                constraints=constraints,
            )
            try:
                run_result = CodeWorkerRuntime(
                    project_root=PROJECT_ROOT,
                    workspace_root=tool_workspace_path(),
                    artifact_root=artifact_root_path(),
                    permission_store=get_permission_store(),
                ).run(request)
            except Exception:  # noqa: BLE001 - keep internal exception details out of API responses.
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "code_worker_failed", "message": "CodeWorker execution failed."},
                )
                return

            _attach_artifacts(state, list(run_result.worker_result.artifacts))
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
            projected_events = [*run_result.event_records, projection_event]
            state.metadata["last_code_worker_api_projection"] = codeworker_api_projection.to_dict()
            response_payload = {
                "task": to_jsonable(state),
                "worker_request": to_jsonable(request),
                "worker_result": to_jsonable(run_result.worker_result),
                "codeworker_session": codeworker_api_projection.to_dict(),
                "compact_state": codeworker_api_projection.compact_state.to_dict(),
                "tool_trace": codeworker_api_projection.tool_trace.to_dict(),
                "events": [to_jsonable(event) for event in projected_events],
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
            persisted_events = [*projected_events, contract_event]
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
                if run_result.worker_result.ok and route_contract.ok
                else HTTPStatus.CONFLICT
            )
            self._send_json(status, response_payload)

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

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key, Authorization")
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
        result["summary"] = "Registered tools."
        result["data"] = {"tools": [to_jsonable(tool) for tool in default_tool_registry().list()]}
    elif name == "/permissions":
        permission_store = get_permission_store()
        result["summary"] = "Permission rules and requests."
        result["data"] = {
            "rules": [to_jsonable(rule) for rule in permission_store.list_rules()],
            "requests": [to_jsonable(request) for request in permission_store.list_requests()],
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
    elif name == "/mcp":
        contracts = build_productized_claude_runtime_contracts(project_root=PROJECT_ROOT)
        integration = build_claude_productization_integration_report(
            project_root=PROJECT_ROOT,
            runtime_contracts=contracts,
        )
        mcp_batches = [
            batch.to_dict()
            for batch in integration.crosswalk.batches
            if "M1-03B" in batch.downstream_slices or "mcp" in str(batch.batch).lower()
        ]
        mcp_contracts = [
            contract.to_dict()
            for contract in integration.crosswalk.downstream_contracts
            if contract.owner_slice == "M1-03B" or "mcp" in contract.contract_id.lower()
        ]
        result["summary"] = "MCP runtime handoff contract from Zyra source graph crosswalk."
        result["data"] = {
            "runtime_status": "downstream_handoff",
            "owner_slice": "M1-03B",
            "source_repo": integration.crosswalk.source_repo,
            "source_graph_contract_id": integration.crosswalk.contract_id,
            "source_graph_ok": integration.ok,
            "requires_node_sidecar": False,
            "sidecar_contracts_used": False,
            "contracts": mcp_contracts,
            "source_batches": mcp_batches,
        }
    elif name == "/skills":
        result["summary"] = "Registered skills and recent skill invocations."
        result["data"] = {
            "skills": [to_jsonable(skill) for skill in default_skill_registry().list()],
            "skill_invocations": list(state.metadata.get("skill_invocations", [])),
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


def _skill_invocation_event(
    state: Any,
    skill: Any,
    *,
    arguments: dict[str, Any],
    node_id: str,
) -> EventRecord:
    return EventRecord(
        run_id=state.run_id,
        task_id=state.task_id,
        event_type=EventType.SKILL_INVOKED,
        node_id=node_id,
        payload={
            "skill_invocation": {
                "skill_name": skill.name,
                "purpose": skill.purpose,
                "source": skill.source,
                "preferred_runtime": skill.preferred_runtime,
                "allowed_tools": list(skill.allowed_tools),
                "vendor_paths": list(skill.vendor_paths),
                "arguments": arguments,
                "status": "recorded",
            }
        },
    )


def _apply_skill_invocation_to_state(state: Any, event: EventRecord) -> None:
    invocation = event.payload.get("skill_invocation")
    if isinstance(invocation, dict):
        state.metadata.setdefault("skill_invocations", []).append(
            {
                "event_id": event.event_id,
                "node_id": event.node_id,
                "skill_name": invocation.get("skill_name", ""),
                "preferred_runtime": invocation.get("preferred_runtime", ""),
                "allowed_tools": invocation.get("allowed_tools", []),
                "status": invocation.get("status", "recorded"),
                "created_at": event.created_at,
            }
        )
    state.updated_at = event.created_at


if __name__ == "__main__":
    run()
