from __future__ import annotations

import json
import os
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
    ContextSessionRuntime,
    JsonPermissionStore,
    LocalArtifactStore,
    PermissionEffect,
    PermissionOperation,
    PermissionRequestStatus,
    PermissionRule,
    ToolCall,
    ToolExecutionContext,
    ToolExecutor,
    WorkerRequest,
    control_event_from_command,
    default_tool_registry,
    default_worker_descriptors,
    tool_result_event,
)
from zyra_skills import default_skill_registry
from zyra_workers import (
    BrowserWorkerRuntime,
    CodeWorkerRuntime,
    CodeWorkerSidecarClient,
    browser_use_health_summary,
    default_browser_action_registry,
    inspect_browser_use_runtime,
)
from zyra_evaluation import evaluate_task_trace
from zyra_integrations import (
    InternalizationLedgerEntry,
    InternalizationLedgerAuditor,
    event_record_from_audit,
    event_record_from_mutation,
    load_project_ledger,
    load_seed_ledger,
    parse_query as parse_ledger_query,
    project_ledger_path,
    save_project_ledger,
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
            query = parse_ledger_query(_flatten_query(parse_qs(parsed.query)))
            include_audit = _truthy(_optional_query_value(parse_qs(parsed.query), "include_findings"))
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
            self._send_json(HTTPStatus.OK, CodeWorkerSidecarClient(PROJECT_ROOT).runtime_inventory())
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
        payload = self._read_json_body()

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
            mutation = ledger.upsert(entry)
            save_project_ledger(PROJECT_ROOT, ledger)
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
                    "event": event_payload,
                    "ledger_path": str(project_ledger_path(PROJECT_ROOT)),
                },
            )
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
            state = store.load_task(parts[1])
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task_not_found"})
                return
            constraints = payload.get("constraints")
            if not isinstance(constraints, dict):
                constraints = {}
            if "tool_plan" in payload and "tool_plan" not in constraints:
                constraints["tool_plan"] = payload["tool_plan"]
            request = WorkerRequest(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=str(payload.get("node_id") or state.root_node_id),
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
            except Exception as error:  # noqa: BLE001 - API must report worker startup/runtime failures.
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "code_worker_failed", "message": str(error)},
                )
                return

            if run_result.worker_result.artifacts:
                state.artifacts.extend(run_result.worker_result.artifacts)
            state.budget.tool_calls += sum(1 for event in run_result.event_records if "tool_result" in event.payload)
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

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return body if isinstance(body, dict) else {}

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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


def _is_ledger_seed_path(parts: list[str]) -> bool:
    return parts == ["ledger", "seed"] or parts == ["integrations", "ledger", "seed"]


def _is_ledger_entries_path(parts: list[str]) -> bool:
    return parts == ["ledger", "entries"] or parts == ["integrations", "ledger", "entries"]


def _ledger_entry_id_from_path(parts: list[str]) -> str:
    if len(parts) == 2 and parts[0] == "ledger" and parts[1] not in {"audit", "seed", "entries"}:
        return parts[1]
    if len(parts) == 3 and parts[0] == "integrations" and parts[1] == "ledger" and parts[2] not in {"audit", "seed", "entries"}:
        return parts[2]
    return ""


def _flatten_query(query: dict[str, list[str]]) -> dict[str, str]:
    return {key: values[-1] for key, values in query.items() if values}


def _truthy(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "strict"}


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
        result["summary"] = "MCP runtime inventory from CodeWorker sidecar."
        result["data"] = {"mcp_runtime_files": CodeWorkerSidecarClient(PROJECT_ROOT).runtime_inventory()["runtimeBoundaries"]["mcpRuntimeFiles"]}
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
