from __future__ import annotations

import html
import asyncio
import json
import os
import re
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from zyra_core import ArtifactKind, ControlCommand, EventRecord, EventType, to_jsonable
from zyra_integrations import browser_use_snapshot
from zyra_runtime import LocalArtifactStore, WorkerRequest, WorkerResult
from zyra_runtime.permission.action_gate import (
    BrowserActionPermissionCustodyError,
    BrowserActionPermissionDisabled,
    BrowserActionPermissionGate,
    BrowserActionPermissionInput,
    browser_permission_setup_failure_events,
)
from zyra_runtime.permission.custody import PermissionSessionCustodyStore

from .browser_actions import BrowserActionRegistry, default_browser_action_registry
from .browser_context import BrowserMessageStateApplication
from .browser_context.task_integration import (
    BrowserContextScope,
    BrowserContextTaskCheckpoint,
    BrowserContextTaskIntegrationRuntime,
)
from .browser_context.integration_audit import BrowserMessageIntegrationAuditRuntime
from .browser_action.application import BrowserActionApplication
from .browser_action.control_runtime import BrowserActionControlCommand
from .browser_observability import BrowserObservabilityApplication
from .browser_session import (
    BrowserRuntime,
    BrowserRuntimeConfig,
    BrowserSessionCommand,
    BrowserSessionControlRuntime,
    BrowserRuntimeRegistry,
    default_browser_runtime_registry,
)
from .browser_session.application import BrowserSessionApplication
from .browser_session.resume_runtime import BrowserSessionResumeRuntime
from .browser_use_runtime import (
    BrowserUseRuntimeHealth,
    browser_use_health_summary,
    browser_use_runtime_metadata,
    configure_browser_use_environment,
    find_browser_executable,
    inspect_browser_use_runtime,
)


@dataclass(frozen=True, slots=True)
class BrowserWorkerRun:
    worker_result: WorkerResult
    event_records: list[EventRecord]
    permission_session_custody_token: str = field(default="", repr=False)
    browser_context_checkpoint: dict[str, Any] = field(default_factory=dict)
    browser_context_projection: dict[str, Any] = field(default_factory=dict)
    browser_observability_projection: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BrowserPageState:
    url: str
    title: str
    text: str
    links: list[dict[str, str]]
    html_chars: int
    text_chars: int


@dataclass(frozen=True, slots=True)
class _BrowserActionBarrierResult:
    allowed: bool
    events: tuple[EventRecord, ...]
    metadata: dict[str, Any]
    summary: str = ""
    error: str | None = None
    output: dict[str, Any] = field(default_factory=dict)


LIVE_BROWSER_USE_TOOL_ACTIONS = {
    "wait",
    "go_back",
    "scroll_page",
    "send_keys",
    "scroll_to_text",
    "evaluate_js",
}

LIVE_BROWSER_USE_ARTIFACT_ACTIONS = {
    "take_screenshot",
    "save_as_pdf",
    "collect_downloads",
}

LIVE_BROWSER_USE_FILE_ACTIONS = {
    "upload_file",
}

LIVE_BROWSER_USE_ONLY_ACTIONS = LIVE_BROWSER_USE_TOOL_ACTIONS | LIVE_BROWSER_USE_ARTIFACT_ACTIONS | LIVE_BROWSER_USE_FILE_ACTIONS


def _browser_context_max_chars(request: WorkerRequest) -> int:
    constraints = request.constraints if isinstance(request.constraints, Mapping) else {}
    budget = constraints.get("browser_context_budget")
    budget = budget if isinstance(budget, Mapping) else {}
    value = budget.get("max_context_bytes") or constraints.get("browser_context_max_chars") or 32000
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = 32000
    return max(4096, min(2_000_000, result))


class BrowserWorkerRuntime:
    """Zyra-owned browser worker with a productized session lifecycle."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        permission_state_path: str | Path | None = None,
        timeout_seconds: int = 15,
        browser_session_runtime: BrowserRuntime | None = None,
        browser_session_application: BrowserSessionApplication | None = None,
        browser_action_application: BrowserActionApplication | None = None,
        browser_runtime_registry: BrowserRuntimeRegistry | None = None,
        browser_session_control: BrowserSessionControlRuntime | None = None,
        browser_session_resume: BrowserSessionResumeRuntime | None = None,
        browser_message_state_application: BrowserMessageStateApplication | None = None,
        browser_context_integration: BrowserContextTaskIntegrationRuntime | None = None,
        browser_message_integration_audit: BrowserMessageIntegrationAuditRuntime | None = None,
        browser_observability_application: BrowserObservabilityApplication | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.artifact_store = LocalArtifactStore(artifact_root)
        self.permission_state_path = (
            Path(permission_state_path).resolve()
            if permission_state_path is not None
            else self.artifact_store.root / ".permission" / "state.json"
        )
        self.timeout_seconds = timeout_seconds
        self.action_registry = default_browser_action_registry(self.project_root)
        self.browser_use_health: BrowserUseRuntimeHealth = inspect_browser_use_runtime(self.project_root)
        browser_config = BrowserRuntimeConfig(
            state_root=self.artifact_store.root / ".browser-session" / "state",
            runtime_root=self.project_root / "tmp" / "browser-session-runtime",
            artifact_root=self.artifact_store.root,
            request_timeout_seconds=float(timeout_seconds),
            connect_timeout_seconds=float(timeout_seconds),
        )
        self.browser_runtime_registry = browser_runtime_registry or default_browser_runtime_registry()
        self.browser_session_runtime = browser_session_runtime or self.browser_runtime_registry.get_or_create(browser_config)
        registry_owns_runtime = browser_session_runtime is None
        if browser_session_runtime is not None and browser_runtime_registry is not None:
            registry_owns_runtime = self.browser_runtime_registry.get_or_create(browser_config) is browser_session_runtime
        self._browser_registry_config = browser_config if registry_owns_runtime else None
        registry_application = (
            self.browser_runtime_registry.application(browser_config)
            if registry_owns_runtime
            else None
        )
        shared_application = getattr(self.browser_session_runtime, "_zyra_browser_application", None)
        self.browser_session_application = (
            browser_session_application
            or registry_application
            or shared_application
            or BrowserSessionApplication(self.browser_session_runtime, artifact_store=self.artifact_store)
        )
        setattr(self.browser_session_runtime, "_zyra_browser_application", self.browser_session_application)
        registry_control = (
            self.browser_runtime_registry.control(browser_config)
            if registry_owns_runtime
            else None
        )
        shared_control = getattr(self.browser_session_runtime, "_zyra_browser_control", None)
        self.browser_session_control = (
            browser_session_control
            or registry_control
            or shared_control
            or BrowserSessionControlRuntime(
                self.browser_session_runtime,
                lease_store=self.browser_session_application.receipt_store,
            )
        )
        setattr(self.browser_session_runtime, "_zyra_browser_control", self.browser_session_control)
        shared_resume = getattr(self.browser_session_runtime, "_zyra_browser_resume", None)
        self.browser_session_resume = browser_session_resume or shared_resume or BrowserSessionResumeRuntime(
            self.browser_session_runtime
        )
        setattr(self.browser_session_runtime, "_zyra_browser_resume", self.browser_session_resume)
        shared_message_state = getattr(self.browser_session_runtime, "_browser_message_state_application", None)
        self.browser_message_state_application = (
            browser_message_state_application
            or shared_message_state
            or BrowserMessageStateApplication(
                self.browser_session_runtime,
                artifact_store=self.artifact_store,
            )
        )
        setattr(
            self.browser_session_runtime,
            "_browser_message_state_application",
            self.browser_message_state_application,
        )
        shared_action_application = getattr(
            self.browser_session_runtime,
            "_browser_action_application",
            None,
        )
        self.browser_action_application = (
            browser_action_application
            or shared_action_application
            or BrowserActionApplication(
                self.browser_session_runtime,
                artifact_store=self.artifact_store,
                message_state_application=self.browser_message_state_application,
            )
        )
        setattr(
            self.browser_session_runtime,
            "_browser_action_application",
            self.browser_action_application,
        )
        self.browser_context_integration = browser_context_integration or BrowserContextTaskIntegrationRuntime()
        self.browser_message_integration_audit = (
            browser_message_integration_audit or BrowserMessageIntegrationAuditRuntime()
        )
        shared_observability = getattr(
            self.browser_session_runtime,
            "_browser_observability_application",
            None,
        )
        self.browser_observability_application = (
            browser_observability_application
            or shared_observability
            or BrowserObservabilityApplication(
                artifact_store=self.artifact_store,
            )
        )
        setattr(
            self.browser_session_runtime,
            "_browser_observability_application",
            self.browser_observability_application,
        )

    def _capture_browser_resume_capsule(self, session_id: str) -> Any:
        if self._browser_registry_config is not None:
            return self.browser_runtime_registry.capture_resume_capsule(
                self._browser_registry_config,
                session_id,
            )
        return self.browser_session_resume.capture(session_id)

    def _resume_browser_session(
        self,
        session_id: str,
        *,
        expected_run_id: str,
        expected_task_id: str,
        worker_request_id: str,
    ) -> Any:
        if self._browser_registry_config is not None:
            return self.browser_runtime_registry.resume(
                self._browser_registry_config,
                session_id,
                expected_run_id=expected_run_id,
                expected_task_id=expected_task_id,
                worker_request_id=worker_request_id,
            )
        return self.browser_session_resume.resume(
            session_id,
            expected_run_id=expected_run_id,
            expected_task_id=expected_task_id,
            worker_request_id=worker_request_id,
        )

    def _browser_start_failure_metadata(
        self,
        request: WorkerRequest,
        command: BrowserSessionCommand,
    ) -> dict[str, Any]:
        try:
            matching = [
                session
                for session in self.browser_session_runtime.list_sessions()
                if session.run_id == request.run_id
                and session.task_id == request.task_id
                and session.canonical_session_id == command.canonical_session_id
            ]
        except Exception:  # noqa: BLE001 - failure projection must preserve the primary error.
            return {}
        if not matching:
            return {}
        session = matching[-1]
        return {
            "browser_session_id": session.session_id,
            "browser_canonical_session_id": session.canonical_session_id,
            "browser_session_status": session.status,
            "browser_session_revision": str(session.revision),
        }

    def run(
        self,
        request: WorkerRequest,
        *,
        browser_context_checkpoint: Mapping[str, Any] | None = None,
    ) -> BrowserWorkerRun:
        if _request_contains_permission_capability_echo(request):
            # Shared permission preflight; this path rejects before backend selection
            # and does not execute a legacy browser action.
            return self._run_legacy_actions(request)
        lifecycle_command = _browser_lifecycle_command_from_request(request)
        if lifecycle_command:
            return self._run_browser_lifecycle(request, lifecycle_command)

        backend = _browser_backend_from_request(request)
        if backend in {"static", "browser-use-live", "browser-use-agent"}:
            return self._run_legacy_actions(request)
        if backend != "zyra-browser-productized":
            return _browser_productized_failure(
                request,
                code="invalid_browser_backend",
                summary=f"BrowserWorker rejected unknown browser backend: {backend}",
            )

        try:
            action_control = BrowserActionControlCommand.from_worker_request(request)
        except Exception as error:  # noqa: BLE001 - typed control parse boundary.
            return _browser_productized_failure(
                request,
                code=str(getattr(error, "code", "browser_action_control_invalid")),
                summary="BrowserWorker rejected an invalid browser action control command.",
                details=str(error),
            )
        if action_control is not None:
            return self._run_browser_action_control(request, action_control)

        snapshot = browser_use_snapshot(self.project_root)
        plan = _browser_plan_from_request(request)
        if not plan:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="BrowserWorker requires a browser_plan.",
                error="missing_browser_plan",
                metadata={
                    **_snapshot_metadata(snapshot),
                    **_action_registry_metadata(self.action_registry),
                    "browser_backend": "zyra-browser-productized",
                    "browser_runtime_vendor_required": "false",
                },
            )
            return BrowserWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(request, worker_result)],
            )
        permission_gate_or_failure = self._permission_gate(
            request,
            snapshot,
            backend="zyra-browser-productized",
        )
        if isinstance(permission_gate_or_failure, BrowserWorkerRun):
            return permission_gate_or_failure
        permission_gate = permission_gate_or_failure
        command = self._browser_session_command(request)
        try:
            started = self.browser_session_runtime.ensure_started(command)
        except Exception as error:  # noqa: BLE001 - runtime boundary returns typed worker failure.
            return _browser_productized_failure(
                request,
                code=type(error).__name__,
                summary="BrowserSessionRuntime failed to prepare a browser session.",
                details=str(error),
                metadata=self._browser_start_failure_metadata(request, command),
            )
        if not started.ok:
            return _browser_productized_failure(
                request,
                code=started.error or "browser_session_start_failed",
                summary="BrowserSessionRuntime could not prepare a browser session.",
                metadata=self._browser_start_failure_metadata(request, command),
            )
        try:
            resume_capsule = self._capture_browser_resume_capsule(started.session.session_id)
        except Exception as error:  # noqa: BLE001 - actions require durable resume state.
            return _browser_productized_failure(
                request,
                code="browser_resume_capsule_capture_failed",
                summary="BrowserWorker could not persist browser session resume state.",
                details=f"{type(error).__name__}: {error}",
            )

        start_payload = to_jsonable(started)
        start_event = _browser_session_event(request, "session_attached", start_payload)
        context_scope = BrowserContextScope(
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=started.session.canonical_session_id,
        )
        try:
            if browser_context_checkpoint:
                context_checkpoint = BrowserContextTaskCheckpoint.from_mapping(
                    browser_context_checkpoint,
                    expected_scope=context_scope,
                )
            else:
                context_checkpoint = BrowserContextTaskCheckpoint.empty(
                    context_scope,
                    max_chars=_browser_context_max_chars(request),
                )
            context_turn_session = self.browser_context_integration.begin_browser_turn(
                context_checkpoint,
                worker_request_id=request.request_id,
                max_chars=_browser_context_max_chars(request),
            )
        except Exception as error:  # noqa: BLE001 - corrupt/foreign context fails before actions.
            return _browser_productized_failure(
                request,
                code=getattr(error, "code", type(error).__name__),
                summary="BrowserWorker refused an invalid browser context checkpoint.",
                details=f"{type(error).__name__}: {error}",
                metadata={
                    "browser_context_scope": json.dumps(context_scope.to_dict(), sort_keys=True),
                    "browser_context_checkpoint_required": "true",
                },
            )
        try:
            action_run = self.browser_action_application.execute_plan(
                request,
                started,
                plan,
                permission_gate,
            )
        except Exception as error:  # noqa: BLE001 - fail closed at the productized action boundary.
            action_run = None
            action_error = f"{type(error).__name__}: {error}"
        else:
            action_error = ""
        action_receipts = tuple(
            getattr(action_run, "receipts", getattr(action_run, "action_receipts", ()))
        ) if action_run is not None else ()
        action_worker_result = getattr(action_run, "worker_result", None)
        action_metadata = dict(getattr(action_worker_result, "metadata", {}) or {})
        action_pending = action_metadata.get("browser_permission_pending") == "true"
        action_partial = action_metadata.get("browser_action_partial") == "true"
        message_turn = None
        message_state_error = ""
        if (
            action_run is not None
            and not action_pending
            and (bool(getattr(action_worker_result, "ok", False)) or action_partial)
        ):
            try:
                message_turn = self.browser_message_state_application.read_state(
                    request=request,
                    session_start=started,
                    action_receipts=action_receipts,
                    context_window=context_turn_session.context_window,
                    source_event_id=start_event.event_id,
                )
            except Exception as error:  # noqa: BLE001 - authoritative read-state fails closed.
                message_state_error = f"{type(error).__name__}: {error}"
                error_details = getattr(error, "details", None)
                if error_details:
                    message_state_error += " | " + json.dumps(
                        to_jsonable(error_details),
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
        stop_payload: dict[str, Any] = {}
        stop_event: EventRecord | None = None
        stop_error = ""
        if not command.keep_alive and not action_pending:
            try:
                stop_control = self.browser_session_control.execute(
                    ControlCommand(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        name="stop",
                        arguments={
                            "browser_session_id": started.session.session_id,
                            "worker_request_id": request.request_id,
                            "reason": "worker_request_completed",
                        },
                        metadata={"node_id": request.node_id},
                    ),
                    session_id=started.session.session_id,
                    task_id=request.task_id,
                    reason="worker_request_completed",
                )
                if not stop_control.ok:
                    raise RuntimeError(stop_control.error or "browser_session_stop_failed")
                stopped = stop_control.result
                stop_payload = to_jsonable(stopped)
                stop_event = stop_control.event_record or _browser_session_event(request, "session_released", stop_payload)
            except Exception as error:  # noqa: BLE001 - cleanup failure is part of worker result.
                stop_error = f"{type(error).__name__}: {error}"

        session_metadata = {
            "browser_backend": "zyra-browser-productized",
            "browser_session_id": started.session.session_id,
            "browser_canonical_session_id": started.session.canonical_session_id,
            "browser_session_status": str(started.session.status),
            "browser_session_revision": str(started.session.revision),
            "browser_session_keep_alive": str(command.keep_alive or action_pending).lower(),
            "browser_session_created": str(started.created).lower(),
            "browser_session_reused": str(started.reused).lower(),
            "browser_resume_capsule_revision": str(resume_capsule.session_revision),
            "browser_resume_capsule_fingerprint": resume_capsule.fingerprint,
            "browser_runtime_vendor_required": "false",
        }
        application_worker_result = getattr(action_run, "worker_result", None)
        application_events = list(
            getattr(action_run, "event_records", getattr(action_run, "events", ()))
        ) if action_run is not None else []
        application_artifacts = list(
            getattr(application_worker_result, "artifacts", getattr(action_run, "artifacts", ()))
        ) if action_run is not None else []
        application_receipts = action_receipts
        committed_context_checkpoint = context_checkpoint
        context_enqueue_event: EventRecord | None = None
        integration_audit = None
        if message_turn is not None:
            committed_context_checkpoint = self.browser_context_integration.commit_browser_turn(
                context_turn_session,
                message_turn,
            )
            context_enqueue_event = self.browser_context_integration.event_for_enqueue(
                committed_context_checkpoint,
                message_turn,
                node_id=request.node_id or "",
            )
            application_events.extend(message_turn.events)
            application_events.append(context_enqueue_event)
            known_artifact_ids = {item.artifact_id for item in application_artifacts}
            application_artifacts.extend(
                item for item in message_turn.artifacts
                if item.artifact_id not in known_artifact_ids
            )
            integration_audit = self.browser_message_integration_audit.audit(
                message_turn,
                committed_context_checkpoint,
                events=(start_event, *application_events),
            )
            application_events.append(
                self.browser_message_integration_audit.event_for_audit(
                    integration_audit,
                    node_id=request.node_id or "",
                )
            )
        failed_receipts = [
            receipt for receipt in application_receipts
            if not bool(getattr(receipt, "ok", False))
        ]
        application_ok = (
            bool(getattr(application_worker_result, "ok", getattr(action_run, "ok", False)))
            and not failed_receipts
            and message_turn is not None
            and message_turn.ok
            and bool(integration_audit and integration_audit.valid)
            if action_run is not None
            else False
        )
        application_error = (
            message_state_error
            or str(getattr(application_worker_result, "error", getattr(action_run, "error", "")) or "")
            if action_run is not None
            else action_error or "browser_application_failed"
        )
        try:
            observability = self.browser_observability_application.observe(
                request=request,
                session_start=started,
                action_run=action_run,
                message_turn=message_turn,
                start_event=start_event,
                application_events=tuple(application_events),
                application_artifacts=tuple(application_artifacts),
                stop_event=stop_event,
                stop_error=stop_error,
                application_ok=application_ok,
                application_error=application_error,
                action_pending=action_pending,
                runtime=self.browser_session_runtime,
            )
        except Exception as error:  # noqa: BLE001 - observability is mandatory and fail-closed.
            observability = None
            observability_error = f"{type(error).__name__}: {error}"
            application_ok = False
            application_error = "browser_observability_failed"
            observability_events = [
                EventRecord(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    event_type=EventType.WORKER_HEALTH,
                    payload={
                        "worker_health_signal": {
                            "schema": "zyra.browser-observability.worker-health-signal.v1",
                            "watchdog": "observability_application",
                            "kind": "history_corruption",
                            "status": "unhealthy",
                            "severity": "critical",
                            "summary": "Browser observability failed on the mandatory main path.",
                            "error": observability_error,
                            "fallback_allowed": False,
                        }
                    },
                ),
                EventRecord(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    node_id=request.node_id,
                    event_type=EventType.AGENT_MESSAGE,
                    payload={
                        "browser_recovery_input": {
                            "schema": "zyra.browser-observability.recovery-input.v1",
                            "reason": "history_failure",
                            "summary": "Mandatory browser observability failed.",
                            "error": observability_error,
                            "worker_request_id": request.request_id,
                            "browser_session_id": started.session.session_id,
                            "planner_owner": "M1-07C",
                            "is_recovery_plan": False,
                            "fallback_allowed": False,
                        }
                    },
                ),
            ]
            observability_projection = {
                "schema": "zyra.browser-observability.worker-projection.v1",
                "scope": {
                    "run_id": request.run_id,
                    "task_id": request.task_id,
                    "node_id": request.node_id or "",
                    "browser_session_id": started.session.session_id,
                    "canonical_session_id": started.session.canonical_session_id,
                    "worker_request_id": request.request_id,
                },
                "error": observability_error,
                "default_route": True,
                "fallback_allowed": False,
                "recovery_handoff": {
                    "planner_owner": "M1-07C",
                    "recovery_planned_emitted": False,
                },
            }
        else:
            observability_error = ""
            observability_events = list(observability.events)
            observability_projection = dict(observability.projection)
            known_artifact_ids = {item.artifact_id for item in application_artifacts}
            application_artifacts.extend(
                item
                for item in observability.artifacts
                if item.artifact_id not in known_artifact_ids
            )
        events = [start_event, *application_events, *observability_events]
        if (
            not application_ok
            and not action_pending
            and not any("browser_recovery_input" in event.payload for event in observability_events)
        ):
            events.append(EventRecord(
                run_id=request.run_id,
                task_id=request.task_id,
                node_id=request.node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "browser_recovery_input": {
                        "schema": "zyra.browser-observability.recovery-input.v1",
                        "reason": "action_failure",
                        "summary": application_error or "browser_action_failed",
                        "browser_session_id": started.session.session_id,
                        "worker_request_id": request.request_id,
                        "failed_receipt_ids": [
                            str(getattr(item, "receipt_id", ""))
                            for item in failed_receipts
                        ],
                        "planner_owner": "M1-07C",
                        "is_recovery_plan": False,
                        "fallback_allowed": False,
                    }
                },
            ))
        if stop_event is not None:
            events.append(stop_event)
        result_events = [
            to_jsonable(event)
            for event in [*application_events, *observability_events]
        ]
        result_events.append({"browser_session": start_payload})
        if stop_payload:
            result_events.append({"browser_session_stop": stop_payload})
        application_metadata = (
            dict(action_run.worker_result.metadata) if action_run is not None else {}
        )
        if message_turn is not None:
            application_metadata.update({
                "browser_dom_capture_id": message_turn.capture_id,
                "browser_selector_revision_id": message_turn.selector_revision_id,
                "browser_context_disclosure_id": message_turn.disclosure.disclosure_id,
                "browser_context_receipt_id": message_turn.next_context.receipt_id,
                "browser_context_tokens": str(message_turn.disclosure.tokens),
                "browser_context_bytes": str(message_turn.disclosure.bytes),
                "browser_context_compression_ratio": str(message_turn.metrics.compression_ratio),
                "browser_memory_candidate_count": str(len(message_turn.memory_candidates)),
                "browser_state_artifact_count": str(len(message_turn.artifacts)),
                "browser_message_turn_status": str(message_turn.status),
                "browser_context_checkpoint_revision": str(committed_context_checkpoint.revision),
                "browser_context_pending_count": str(committed_context_checkpoint.pending_count),
                "browser_context_consumed_count": str(committed_context_checkpoint.consumed_count),
                "browser_message_integration_audit_valid": str(
                    bool(integration_audit and integration_audit.valid)
                ).lower(),
                "browser_message_integration_audit_id": (
                    integration_audit.audit_id if integration_audit else ""
                ),
                "browser_observability_owner_unit": "M1-S04D-01",
                "browser_observability_history_head": str(
                    observability_projection.get("history", {}).get("head_digest", "")
                    if isinstance(observability_projection.get("history"), dict)
                    else ""
                ),
                "browser_observability_signal_count": str(
                    observability_projection.get("health", {}).get("signal_count", 0)
                    if isinstance(observability_projection.get("health"), dict)
                    else 0
                ),
                "browser_observability_recovery_planned_emitted": "false",
            })
        elif message_state_error:
            application_metadata["browser_message_state_error"] = message_state_error
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=application_ok and not stop_error,
            summary=(
                "BrowserWorker completed the productized browser plan."
                if application_ok and not stop_error
                else "BrowserWorker parked the productized browser plan pending permission."
                if action_pending and not stop_error
                else "BrowserWorker stopped on a productized browser session failure."
            ),
            artifacts=application_artifacts,
            events=result_events,
            error=(
                None if application_ok and not stop_error
                else "browser_action_permission_pending" if action_pending and not stop_error
                else application_error or ("browser_session_stop_failed" if stop_error else "browser_action_failed")
            ),
            metadata={
                **application_metadata,
                **session_metadata,
                **permission_gate.metadata(),
                **({"browser_session_stop_error": stop_error} if stop_error else {}),
                **({"browser_observability_error": observability_error} if observability_error else {}),
                "browser_permission_action_execution_count": str(
                    sum(1 for receipt in application_receipts if bool(getattr(receipt, "ok", False)))
                    if action_run is not None else 0
                ),
            },
        )
        return BrowserWorkerRun(
            worker_result=worker_result,
            event_records=events,
            permission_session_custody_token=permission_gate.custody_token,
            browser_context_checkpoint=committed_context_checkpoint.to_dict(),
            browser_context_projection=self.browser_context_integration.public_projection(
                committed_context_checkpoint
            ),
            browser_observability_projection=observability_projection,
        )

    def _run_browser_lifecycle(self, request: WorkerRequest, command_name: str) -> BrowserWorkerRun:
        command = self._browser_session_command(request)
        try:
            normalized = {
                "ensure": "ensure-started", "prepare": "ensure-started", "attach": "ensure-started",
                "diagnostic": "diagnose", "health": "diagnose", "list-sessions": "list",
            }.get(command_name, command_name)
            capsule = None
            control_event = None
            if normalized == "resume":
                result = self._resume_browser_session(
                    _required_browser_session_id(command),
                    expected_run_id=request.run_id,
                    expected_task_id=request.task_id,
                    worker_request_id=request.request_id,
                )
                ok = bool(getattr(result, "ok", False))
            else:
                control_result = self.browser_session_control.execute(
                    ControlCommand(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        name=normalized,
                        arguments={
                            **PermissionSessionCustodyStore.redact_constraints(request.constraints),
                            "browser_session_id": command.browser_session_id,
                            "worker_request_id": request.request_id,
                        },
                        metadata={"node_id": request.node_id},
                    ),
                    session_command=command,
                    session_id=command.browser_session_id,
                    task_id=request.task_id,
                    force=bool(request.constraints.get("force")),
                    reason=str(request.constraints.get("reason") or "requested"),
                )
                result = control_result.result
                ok = control_result.ok
                control_event = control_result.event_record
                if not ok:
                    failure_event = _browser_session_event(
                        request,
                        f"{normalized}_failed",
                        {
                            "ok": False,
                            "error": control_result.error or "browser_lifecycle_failed",
                            "control": to_jsonable(control_result),
                        },
                    )
                    failure_events = [item for item in (control_event, failure_event) if item is not None]
                    return BrowserWorkerRun(
                        worker_result=WorkerResult(
                            request_id=request.request_id,
                            ok=False,
                            summary=f"Browser lifecycle command {normalized} failed.",
                            error=control_result.error or "browser_lifecycle_failed",
                            events=[to_jsonable(item) for item in failure_events],
                            metadata={
                                "browser_backend": "zyra-browser-productized",
                                "browser_lifecycle_command": normalized,
                                **{str(key): str(value) for key, value in control_result.metadata.items()},
                            },
                        ),
                        event_records=failure_events,
                    )
            result_session_id = str(getattr(getattr(result, "session", None), "session_id", "") or command.browser_session_id)
            if ok and normalized in {"start", "ensure-started", "reconnect", "resume"} and result_session_id:
                capsule = self._capture_browser_resume_capsule(result_session_id)
        except Exception as error:  # noqa: BLE001 - lifecycle errors are worker results.
            return _browser_productized_failure(
                request,
                code=type(error).__name__,
                summary=f"Browser lifecycle command {command_name} failed.",
                details=str(error),
            )

        payload = to_jsonable(result)
        ok = bool(payload.get("ok", ok)) if isinstance(payload, dict) else ok
        result_session = payload.get("session", {}) if isinstance(payload, dict) else {}
        if not isinstance(result_session, dict):
            result_session = {}
        event = EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            event_type=(EventType.BROWSER_RUNTIME_DIAGNOSTIC if normalized == "diagnose" else EventType.BROWSER_SESSION_LIFECYCLE),
            payload={"browser_session": {"command": normalized, "result": payload}},
        )
        lifecycle_events = [item for item in (control_event, event) if item is not None]
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=ok,
            summary=f"Browser lifecycle command {normalized} {'completed' if ok else 'failed'}.",
            error=None if ok else str(payload.get("error") or payload.get("error_code") or "browser_lifecycle_failed"),
            events=[to_jsonable(item) for item in lifecycle_events],
            metadata={
                "browser_backend": "zyra-browser-productized",
                "browser_lifecycle_command": normalized,
                "browser_runtime_vendor_required": "false",
                "browser_session_id": str(result_session.get("session_id") or command.browser_session_id),
                "browser_canonical_session_id": str(
                    result_session.get("canonical_session_id") or command.canonical_session_id
                ),
                "browser_session_status": str(result_session.get("status") or ""),
                "browser_session_revision": str(result_session.get("revision") or ""),
                **({
                    "browser_resume_capsule_revision": str(capsule.session_revision),
                    "browser_resume_capsule_fingerprint": capsule.fingerprint,
                } if capsule is not None else {}),
            },
        )
        return BrowserWorkerRun(worker_result=worker_result, event_records=lifecycle_events)

    def _run_browser_action_control(
        self,
        request: WorkerRequest,
        command: BrowserActionControlCommand,
    ) -> BrowserWorkerRun:
        result = self.browser_action_application.apply_control(command)
        payload = result.public_dict()
        metadata = {
            "browser_backend": "zyra-browser-productized",
            "browser_action_control_runtime": "zyra-browser-action-control-runtime",
            "browser_action_control_command_id": command.command_id,
            "browser_action_control_kind": str(command.kind),
            "browser_action_control_status": str(result.status),
            "browser_action_control_replayed": str(result.replayed).lower(),
            "browser_action_control_record_count": str(len(result.records)),
            "browser_action_control_active_count": str(len(result.active_action_ids)),
            "browser_action_fallback_allowed": "false",
            "browser_runtime_vendor_required": "false",
        }
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=result.ok,
            summary=(
                f"Browser action control {command.kind} {result.status}."
                if result.ok
                else "Browser action control was rejected."
            ),
            error=None if result.ok else result.error_code or "browser_action_control_rejected",
            events=[to_jsonable(event) for event in result.events],
            metadata=metadata,
        )
        return BrowserWorkerRun(
            worker_result=worker_result,
            event_records=list(result.events),
            browser_context_projection={"browser_action_control": payload},
        )

    def _browser_session_command(self, request: WorkerRequest) -> BrowserSessionCommand:
        constraints = PermissionSessionCustodyStore.redact_constraints(request.constraints)
        executable = constraints.get("browser_executable") or constraints.get("executable_path")
        headers = constraints.get("browser_headers") or constraints.get("headers") or {}
        if not isinstance(headers, dict):
            headers = {}
        return BrowserSessionCommand(
            run_id=request.run_id,
            task_id=request.task_id,
            worker_request_id=request.request_id,
            canonical_session_id=str(
                constraints.get("canonical_session_id")
                or constraints.get("session_id")
                or f"{request.run_id}:{request.task_id}"
            ),
            node_id=request.node_id or "",
            browser_session_id=str(constraints.get("browser_session_id") or ""),
            workspace_root=self.workspace_root,
            artifact_root=self.artifact_store.root,
            executable_path=Path(str(executable)).expanduser() if executable else None,
            endpoint_url=str(
                constraints.get("cdp_url")
                or constraints.get("browser_endpoint_url")
                or constraints.get("endpoint_url")
                or ""
            ),
            headless=bool(constraints.get("headless", True)),
            keep_alive=bool(constraints.get("keep_alive", False)),
            headers={str(key): str(value) for key, value in headers.items()},
            proxy_url=str(constraints.get("proxy_url") or ""),
            constraints=dict(constraints),
        )

    def _run_legacy_actions(self, request: WorkerRequest) -> BrowserWorkerRun:
        constraints = request.constraints
        presented_custody_tokens = tuple(
            dict.fromkeys(
                str(constraints.get(key) or "")
                for key in (
                    "permission_session_custody_token",
                    "session_custody_token",
                )
                if str(constraints.get(key) or "")
            )
        )
        if PermissionSessionCustodyStore.contains_capability_echo(
            constraints,
            presented_custody_tokens,
        ):
            redacted_request = replace(
                request,
                constraints=PermissionSessionCustodyStore.redact_constraints(constraints),
            )
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="BrowserWorker rejected a custody capability copied into browser task data.",
                error="permission_custody_capability_echo",
                metadata={
                    "browser_backend": "blocked_before_selection",
                    "browser_permission_action_execution_count": "0",
                    "permission_capability_echo_rejected": "true",
                },
            )
            return BrowserWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(redacted_request, worker_result)],
            )

        # Provenance is optional metadata only.  Missing source files are
        # reported by the snapshot but never validated as a runtime gate.
        snapshot = browser_use_snapshot(self.project_root)
        backend = _browser_backend_from_request(request)
        if backend == "browser-use-agent":
            return self._run_browser_use_agent(request, snapshot)

        plan = _browser_plan_from_request(request)
        if not plan:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="BrowserWorker requires a browser_plan.",
                error="missing_browser_plan",
                metadata={**_snapshot_metadata(snapshot), **browser_use_runtime_metadata(self.browser_use_health)},
            )
            return BrowserWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(request, worker_result)],
            )

        validation_issues = self.action_registry.validate_plan(plan)
        if validation_issues:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="BrowserWorker rejected an invalid browser_plan.",
                error="invalid_browser_plan",
                metadata={
                    **_snapshot_metadata(snapshot),
                    **_action_registry_metadata(self.action_registry),
                    **browser_use_runtime_metadata(self.browser_use_health),
                    "browser_backend": backend,
                    "validation_issues": str(len(validation_issues)),
                },
                events=[to_jsonable(issue) for issue in validation_issues],
            )
            return BrowserWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(request, worker_result)],
            )

        if backend != "static":
            if backend == "browser-use-live":
                pass
            else:
                worker_result = WorkerResult(
                    request_id=request.request_id,
                    ok=False,
                    summary=f"BrowserWorker rejected unknown browser backend: {backend}",
                    error="invalid_browser_backend",
                    metadata={
                        **_snapshot_metadata(snapshot),
                        **_action_registry_metadata(self.action_registry),
                        **browser_use_runtime_metadata(self.browser_use_health),
                        "browser_backend": backend,
                    },
                )
                return BrowserWorkerRun(
                    worker_result=worker_result,
                    event_records=[_worker_result_event(request, worker_result)],
                )

        permission_gate_or_failure = self._permission_gate(request, snapshot, backend=backend)
        if isinstance(permission_gate_or_failure, BrowserWorkerRun):
            return permission_gate_or_failure
        permission_gate = permission_gate_or_failure
        if backend == "browser-use-live":
            return self._run_browser_use_live(request, snapshot, plan, permission_gate)

        event_records: list[EventRecord] = []
        artifacts = []
        current_html = ""
        current_url = ""
        current_state: BrowserPageState | None = None
        virtual_inputs: dict[str, str] = {}
        continue_on_error = request.constraints.get("continue_on_error") is True

        for index, step in enumerate(plan, start=1):
            action = str(step.get("action") or step.get("browser_action") or "")
            arguments = step.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            descriptor = self.action_registry.get(action)
            normalized_action = self.action_registry.normalize_action(action)
            action_metadata = _action_metadata(descriptor, normalized_action)

            # Unsupported live-only actions never reach a browser side-effect
            # on the static backend, so preserve the backend diagnostic without
            # manufacturing a permission prompt for an impossible execution.
            if normalized_action not in LIVE_BROWSER_USE_ONLY_ACTIONS:
                if normalized_action == "click_element" and current_state is None:
                    blocked_events = browser_permission_setup_failure_events(
                        request,
                        session_id=permission_gate.session_id,
                        component="static-browser-click-state",
                        reason=(
                            "static click requires an explicitly opened/snapshotted page "
                            "so its exact navigation target is known before permission"
                        ),
                        code="browser_click_state_required",
                    )
                    event_records.extend(blocked_events)
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata={
                                **action_metadata,
                                "permission_effect": "deny",
                                "permission_action_allowed": "false",
                            },
                            ok=False,
                            summary="Static browser click was blocked before target resolution.",
                            output={"action_execution_count": 0},
                            error="browser_click_state_required",
                        )
                    )
                    if not continue_on_error:
                        break
                    continue
                target_url = str(arguments.get("url") or current_url or "")
                if normalized_action == "click_element" and current_state is not None:
                    try:
                        target_url = _click_link_url(current_state, arguments)
                    except (TypeError, ValueError):
                        # Input validation remains the action handler's job; an
                        # unresolved click is conservatively classified from
                        # the current page URL.
                        target_url = current_url
                barrier = self._permission_barrier(
                    gate=permission_gate,
                    request=request,
                    index=index,
                    action=action,
                    normalized_action=normalized_action,
                    arguments=arguments,
                    descriptor=descriptor,
                    backend="static",
                    current_url=current_url,
                    target_url=target_url,
                )
                event_records.extend(barrier.events)
                action_metadata = {**action_metadata, **barrier.metadata}
                if not barrier.allowed:
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=False,
                            summary=barrier.summary,
                            output=barrier.output,
                            error=barrier.error,
                        )
                    )
                    if not continue_on_error:
                        break
                    continue

            try:
                if normalized_action == "open_url":
                    current_url = str(arguments.get("url") or current_url)
                    current_html = self._load_url(current_url, request)
                    current_state = _page_state(current_url, current_html)
                    raw_artifact = self.artifact_store.write_text(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        content=current_html,
                        title=f"Browser raw HTML {index}",
                        kind=ArtifactKind.FILE,
                        extension=".html",
                        producer_node_id=request.node_id,
                    )
                    artifacts.append(raw_artifact)
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=True,
                            summary=f"Opened {current_url}",
                            output=_state_summary(current_state),
                            artifacts=[raw_artifact],
                        )
                    )
                elif normalized_action == "extract_text":
                    if not current_html:
                        current_url = str(arguments.get("url") or current_url)
                        current_html = self._load_url(current_url, request)
                    current_state = _page_state(current_url, current_html)
                    text_artifact = self.artifact_store.write_text(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        content=current_state.text,
                        title=f"Browser extracted text {index}",
                        kind=ArtifactKind.MARKDOWN,
                        extension=".md",
                        producer_node_id=request.node_id,
                    )
                    state_artifact = self.artifact_store.write_text(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        content=json.dumps(_state_summary(current_state), ensure_ascii=False, indent=2),
                        title=f"Browser state {index}",
                        kind=ArtifactKind.STRUCTURED_DATA,
                        extension=".json",
                        producer_node_id=request.node_id,
                    )
                    artifacts.extend([text_artifact, state_artifact])
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=True,
                            summary=f"Extracted text from {current_state.url}",
                            output=_state_summary(current_state),
                            artifacts=[text_artifact, state_artifact],
                        )
                    )
                elif normalized_action == "snapshot_state":
                    if current_state is None:
                        if not current_html:
                            current_url = str(arguments.get("url") or current_url)
                            current_html = self._load_url(current_url, request)
                        current_state = _page_state(current_url, current_html)
                    state_artifact = self.artifact_store.write_text(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        content=json.dumps(_state_summary(current_state), ensure_ascii=False, indent=2),
                        title=f"Browser state {index}",
                        kind=ArtifactKind.STRUCTURED_DATA,
                        extension=".json",
                        producer_node_id=request.node_id,
                    )
                    artifacts.append(state_artifact)
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=True,
                            summary=f"Captured browser state for {current_state.url}",
                            output=_state_summary(current_state),
                            artifacts=[state_artifact],
                        )
                    )
                elif normalized_action == "click_element":
                    if current_state is None:
                        if not current_html:
                            current_url = str(arguments.get("url") or current_url)
                            current_html = self._load_url(current_url, request)
                        current_state = _page_state(current_url, current_html)
                    next_url = _click_link_url(current_state, arguments)
                    current_url = next_url
                    current_html = self._load_url(current_url, request)
                    current_state = _page_state(current_url, current_html)
                    raw_artifact = self.artifact_store.write_text(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        content=current_html,
                        title=f"Browser clicked HTML {index}",
                        kind=ArtifactKind.FILE,
                        extension=".html",
                        producer_node_id=request.node_id,
                    )
                    state_artifact = self.artifact_store.write_text(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        content=json.dumps(_state_summary(current_state), ensure_ascii=False, indent=2),
                        title=f"Browser clicked state {index}",
                        kind=ArtifactKind.STRUCTURED_DATA,
                        extension=".json",
                        producer_node_id=request.node_id,
                    )
                    artifacts.extend([raw_artifact, state_artifact])
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=True,
                            summary=f"Clicked element and navigated to {current_url}",
                            output=_state_summary(current_state),
                            artifacts=[raw_artifact, state_artifact],
                        )
                    )
                elif normalized_action == "input_text":
                    if current_state is None:
                        if not current_html:
                            current_url = str(arguments.get("url") or current_url)
                            current_html = self._load_url(current_url, request)
                        current_state = _page_state(current_url, current_html)
                    input_index = str(arguments.get("index"))
                    text = str(arguments.get("text") or "")
                    if arguments.get("clear", True) is False:
                        text = virtual_inputs.get(input_index, "") + text
                    virtual_inputs[input_index] = text
                    output = {**_state_summary(current_state), "virtual_inputs": dict(virtual_inputs)}
                    state_artifact = self.artifact_store.write_text(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        content=json.dumps(output, ensure_ascii=False, indent=2),
                        title=f"Browser input state {index}",
                        kind=ArtifactKind.STRUCTURED_DATA,
                        extension=".json",
                        producer_node_id=request.node_id,
                    )
                    artifacts.append(state_artifact)
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=True,
                            summary=f"Input text into element {input_index}",
                            output=output,
                            artifacts=[state_artifact],
                        )
                    )
                elif normalized_action == "search_page":
                    if current_state is None:
                        if not current_html:
                            current_url = str(arguments.get("url") or current_url)
                            current_html = self._load_url(current_url, request)
                        current_state = _page_state(current_url, current_html)
                    matches = _search_page_matches(current_state.text, arguments)
                    output = {**_state_summary(current_state), "matches": matches, "match_count": len(matches)}
                    search_artifact = self.artifact_store.write_text(
                        run_id=request.run_id,
                        task_id=request.task_id,
                        content=json.dumps(output, ensure_ascii=False, indent=2),
                        title=f"Browser search page {index}",
                        kind=ArtifactKind.STRUCTURED_DATA,
                        extension=".json",
                        producer_node_id=request.node_id,
                    )
                    artifacts.append(search_artifact)
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=True,
                            summary=f"Found {len(matches)} page match(es)",
                            output=output,
                            artifacts=[search_artifact],
                        )
                    )
                elif normalized_action == "wait":
                    seconds = _safe_wait_seconds(arguments.get("seconds", 3))
                    if current_state is None and current_html:
                        current_state = _page_state(current_url, current_html)
                    output = {
                        **(_state_summary(current_state) if current_state else {"url": current_url, "title": ""}),
                        "wait_seconds": seconds,
                        "browser_backend": "static",
                    }
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=True,
                            summary=f"Static BrowserWorker recorded wait for {seconds} second(s).",
                            output=output,
                        )
                    )
                elif normalized_action in LIVE_BROWSER_USE_ONLY_ACTIONS:
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=False,
                            summary=(
                                f"Browser action {normalized_action} requires browser-use-live backend."
                            ),
                            output={
                                "action": action,
                                "normalized_action": normalized_action,
                                "required_backend": "browser-use-live",
                                "current_backend": "static",
                            },
                            error="browser_action_requires_live_backend",
                        )
                    )
                    if not continue_on_error:
                        break
                else:
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=False,
                            summary=f"Unknown browser action: {action}",
                            output={"action": action},
                            error="unknown_browser_action",
                        )
                    )
                    if not continue_on_error:
                        break
            except Exception as error:  # noqa: BLE001 - browser failures must be traceable events.
                event_records.append(
                    _browser_action_event(
                        request,
                        index,
                        action,
                        action_metadata=action_metadata,
                        ok=False,
                        summary=f"Browser action failed: {action}",
                        output={"action": action, "message": str(error)},
                        error=type(error).__name__,
                    )
                )
                if not continue_on_error:
                    break

        browser_events = _browser_action_events(event_records)
        ok = bool(browser_events) and all(_event_browser_result_ok(event) for event in browser_events)
        action_execution_count = sum(
            event.payload.get("browser_action", {}).get("permission_execution_grant_consumed") == "true"
            for event in browser_events
        )
        trace_artifact = self.artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=_trace_markdown(request, snapshot, event_records, ok, self.browser_use_health),
            title=f"BrowserWorker trace {request.request_id}",
            kind=ArtifactKind.TRACE,
            extension=".md",
            producer_node_id=request.node_id,
        )
        artifacts.append(trace_artifact)
        summary = "BrowserWorker completed browser plan." if ok else "BrowserWorker stopped on a failed browser action."
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=ok,
            summary=summary,
            artifacts=artifacts,
            events=[to_jsonable(event) for event in event_records],
            error=None if ok else "browser_action_failed",
            metadata={
                **_snapshot_metadata(snapshot),
                **_action_registry_metadata(self.action_registry),
                **browser_use_runtime_metadata(self.browser_use_health),
                **permission_gate.metadata(),
                "browser_backend": backend,
                "browser_steps": str(len(browser_events)),
                "browser_permission_action_execution_count": str(action_execution_count),
                "trace_artifact_id": trace_artifact.artifact_id,
            },
        )
        return BrowserWorkerRun(
            worker_result=worker_result,
            event_records=[*event_records, _worker_result_event(request, worker_result)],
            permission_session_custody_token=permission_gate.custody_token,
        )

    def _permission_gate(
        self,
        request: WorkerRequest,
        snapshot: Any,
        *,
        backend: str,
    ) -> BrowserActionPermissionGate | BrowserWorkerRun:
        try:
            return BrowserActionPermissionGate.for_worker_request(
                request,
                workspace_root=self.workspace_root,
                state_path=self.permission_state_path,
            )
        except BrowserActionPermissionDisabled as error:
            return self._permission_setup_failure(
                request,
                snapshot,
                backend=backend,
                component=error.component,
                code=error.code,
                reason=str(error),
            )
        except BrowserActionPermissionCustodyError as error:
            return self._permission_setup_failure(
                request,
                snapshot,
                backend=backend,
                component=error.component,
                code=error.cause_code,
                reason=str(error),
            )
        except Exception as error:  # noqa: BLE001 - browser execution must fail closed.
            return self._permission_setup_failure(
                request,
                snapshot,
                backend=backend,
                component=type(error).__name__,
                code="browser_action_permission_setup_failed",
                reason=str(error),
            )

    def _permission_setup_failure(
        self,
        request: WorkerRequest,
        snapshot: Any,
        *,
        backend: str,
        component: str,
        code: str,
        reason: str,
    ) -> BrowserWorkerRun:
        session_id = str(request.constraints.get("permission_session_id") or "")
        permission_events = list(
            browser_permission_setup_failure_events(
                request,
                session_id=session_id,
                component=component,
                reason=reason,
                code=code,
            )
        )
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=False,
            summary="BrowserWorker stopped because its permission action barrier is unavailable.",
            error="browser_action_permission_unavailable",
            events=[to_jsonable(event) for event in permission_events],
            metadata={
                **_snapshot_metadata(snapshot),
                **_action_registry_metadata(self.action_registry),
                **browser_use_runtime_metadata(self.browser_use_health),
                "browser_backend": backend,
                "browser_permission_gate_runtime_id": "zyra-browser-action-permission-gate",
                "browser_permission_gate_component": component,
                "browser_permission_gate_error_code": code,
                "browser_permission_action_execution_count": "0",
            },
        )
        return BrowserWorkerRun(
            worker_result=worker_result,
            event_records=[*permission_events, _worker_result_event(request, worker_result)],
        )

    def _permission_barrier(
        self,
        *,
        gate: BrowserActionPermissionGate,
        request: WorkerRequest,
        index: int,
        action: str,
        normalized_action: str,
        arguments: dict[str, Any],
        descriptor: Any,
        backend: str,
        current_url: str = "",
        target_url: str = "",
    ) -> _BrowserActionBarrierResult:
        permission_input = BrowserActionPermissionInput(
            step_index=index,
            action=action,
            normalized_action=normalized_action,
            backend=backend,
            arguments=arguments,
            source_action=str(getattr(descriptor, "source_action", "") or ""),
            source_model=str(getattr(descriptor, "source_model", "") or ""),
            required_arguments=tuple(getattr(descriptor, "zyra_required_arguments", ()) or ()),
            optional_arguments=tuple(getattr(descriptor, "zyra_optional_arguments", ()) or ()),
            current_url=current_url,
            target_url=target_url,
        )
        try:
            decision = gate.guard(permission_input)
        except Exception as error:  # noqa: BLE001 - decision failures must block the action.
            events = browser_permission_setup_failure_events(
                request,
                session_id=gate.session_id,
                component=type(error).__name__,
                reason=str(error),
                code="browser_action_permission_guard_failed",
            )
            return _BrowserActionBarrierResult(
                allowed=False,
                events=events,
                metadata={
                    "permission_effect": "deny",
                    "permission_action_allowed": "false",
                    "permission_tool_use_id": "",
                },
                summary="Browser action permission evaluation failed closed.",
                error="browser_action_permission_guard_failed",
                output={"action": normalized_action, "message": str(error)},
            )
        if not decision.allowed:
            return _BrowserActionBarrierResult(
                allowed=False,
                events=decision.events,
                metadata=decision.metadata(),
                summary=(
                    "Browser action requires permission approval."
                    if decision.effect == "ask"
                    else "Browser action was denied by permission policy."
                ),
                error=("permission_required" if decision.effect == "ask" else "permission_denied"),
                output={
                    "permission_decision": decision.guard.decision.to_dict(),
                    "pending_request": decision.guard.pending_request.to_dict()
                    if decision.guard.pending_request is not None
                    else None,
                    "recovery_input": decision.guard.decision.recovery_input.to_dict()
                    if decision.guard.decision.recovery_input is not None
                    else None,
                    "alternatives": list(decision.guard.trace.recovery_alternatives),
                    "action_execution_count": 0,
                },
            )
        consumption = gate.consume(decision, permission_input)
        if not consumption.accepted:
            return _BrowserActionBarrierResult(
                allowed=False,
                events=(*decision.events, *consumption.events),
                metadata=consumption.metadata(),
                summary="Browser action execution grant was rejected at the side-effect boundary.",
                error="permission_action_grant_rejected",
                output={
                    "permission_decision": decision.guard.decision.to_dict(),
                    "reason": consumption.reason,
                    "action_execution_count": 0,
                },
            )
        return _BrowserActionBarrierResult(
            allowed=True,
            events=(*decision.events, *consumption.events),
            metadata=consumption.metadata(),
        )

    def _load_url(self, url: str, request: WorkerRequest) -> str:
        if not url:
            raise ValueError("url is required")
        _validate_allowed_url(url, request.constraints)
        parsed = urlparse(url)
        if parsed.scheme == "file":
            path = Path(urllib.request.url2pathname(unquote(parsed.path)))
            if parsed.netloc:
                path = Path(f"//{parsed.netloc}{urllib.request.url2pathname(unquote(parsed.path))}")
            resolved = path.resolve()
            resolved.relative_to(self.workspace_root)
            return resolved.read_text(encoding="utf-8")
        if parsed.scheme in {"http", "https"}:
            request_obj = urllib.request.Request(url, headers={"User-Agent": "ZyraBrowserWorker/0.1"})
            with urllib.request.urlopen(request_obj, timeout=self.timeout_seconds) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        raise ValueError(f"unsupported URL scheme: {parsed.scheme}")

    def _run_browser_use_live(
        self,
        request: WorkerRequest,
        snapshot: Any,
        plan: list[dict[str, Any]],
        permission_gate: BrowserActionPermissionGate,
    ) -> BrowserWorkerRun:
        if not self.browser_use_health.importable:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="Browser-use live backend is not importable.",
                error="browser_use_runtime_unavailable",
                metadata={
                    **_snapshot_metadata(snapshot),
                    **_action_registry_metadata(self.action_registry),
                    **browser_use_runtime_metadata(self.browser_use_health),
                    **permission_gate.metadata(),
                    "browser_backend": "browser-use-live",
                    "browser_permission_action_execution_count": "0",
                },
            )
            return BrowserWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(request, worker_result)],
                permission_session_custody_token=permission_gate.custody_token,
            )

        executable = find_browser_executable([str(request.constraints.get("browser_executable") or "")])
        if executable is None:
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="Browser-use live backend could not find Chrome or Edge.",
                error="browser_executable_not_found",
                metadata={
                    **_snapshot_metadata(snapshot),
                    **_action_registry_metadata(self.action_registry),
                    **browser_use_runtime_metadata(self.browser_use_health),
                    **permission_gate.metadata(),
                    "browser_backend": "browser-use-live",
                    "browser_permission_action_execution_count": "0",
                },
            )
            return BrowserWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(request, worker_result)],
                permission_session_custody_token=permission_gate.custody_token,
            )

        timeout = _bounded_int(
            request.constraints.get("live_timeout_seconds"),
            default=max(self.timeout_seconds * max(len(plan), 1), 30),
            minimum=10,
            maximum=300,
        )
        try:
            return asyncio.run(
                asyncio.wait_for(
                    self._run_browser_use_live_async(
                        request=request,
                        snapshot=snapshot,
                        plan=plan,
                        executable=executable,
                        permission_gate=permission_gate,
                    ),
                    timeout=timeout,
                )
            )
        except Exception as error:  # noqa: BLE001 - live browser failures must be surfaced as worker results.
            worker_result = WorkerResult(
                request_id=request.request_id,
                ok=False,
                summary="Browser-use live backend failed before producing a complete trace.",
                error=type(error).__name__,
                metadata={
                    **_snapshot_metadata(snapshot),
                    **_action_registry_metadata(self.action_registry),
                    **browser_use_runtime_metadata(self.browser_use_health),
                    **permission_gate.metadata(),
                    "browser_backend": "browser-use-live",
                    "browser_executable": str(executable),
                    "live_error": str(error),
                    "browser_permission_action_execution_count": "0",
                },
            )
            return BrowserWorkerRun(
                worker_result=worker_result,
                event_records=[_worker_result_event(request, worker_result)],
                permission_session_custody_token=permission_gate.custody_token,
            )

    def _run_browser_use_agent(self, request: WorkerRequest, snapshot: Any) -> BrowserWorkerRun:
        task = _browser_use_agent_task_from_request(request)
        base_metadata = {
            **_snapshot_metadata(snapshot),
            **_action_registry_metadata(self.action_registry),
            **browser_use_runtime_metadata(self.browser_use_health),
            "browser_backend": "browser-use-agent",
            "browser_agent_task_chars": str(len(task)),
            "browser_permission_action_execution_count": "0",
            "browser_agent_per_action_gate_integrated": "false",
            "browser_agent_deferred_owner": "M1-04C",
        }
        permission_gate_or_failure = self._permission_gate(request, snapshot, backend="browser-use-agent")
        if isinstance(permission_gate_or_failure, BrowserWorkerRun):
            return permission_gate_or_failure
        permission_gate = permission_gate_or_failure
        blocked, recovery = browser_permission_setup_failure_events(
            request,
            session_id=permission_gate.session_id,
            component="browser-use-agent-action-registry",
            reason=(
                "browser-use Agent cannot execute until each model-selected action "
                "is connected to the Zyra permission gate"
            ),
            code="browser_agent_per_action_gate_unavailable",
        )
        return self._browser_use_agent_failure(
            request,
            snapshot,
            summary="Browser-use Agent backend is fail-closed until per-action permission integration.",
            error="browser_agent_per_action_gate_unavailable",
            metadata={**base_metadata, **permission_gate.metadata()},
            output={
                "task_present": bool(task),
                "action_execution_count": 0,
                "recovery": "use an explicit static/live browser_plan or complete M1-04C",
            },
            prefix_events=(blocked, recovery),
            custody_token=permission_gate.custody_token,
        )

    def _browser_use_agent_failure(
        self,
        request: WorkerRequest,
        snapshot: Any,
        *,
        summary: str,
        error: str,
        metadata: dict[str, str],
        output: dict[str, Any],
        prefix_events: tuple[EventRecord, ...] = (),
        custody_token: str = "",
    ) -> BrowserWorkerRun:
        event = _browser_agent_event(
            request,
            step_index=0,
            task=_browser_use_agent_task_from_request(request),
            ok=False,
            summary=summary,
            output={**output, "browser_backend": "browser-use-agent"},
            error=error,
        )
        event_records = [*prefix_events, event]
        trace_artifact = self.artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=_agent_trace_markdown(request, snapshot, event_records, False, self.browser_use_health),
            title=f"Browser-use Agent trace {request.request_id}",
            kind=ArtifactKind.TRACE,
            extension=".md",
            producer_node_id=request.node_id,
        )
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=False,
            summary=summary,
            artifacts=[trace_artifact],
            events=[to_jsonable(item) for item in event_records],
            error=error,
            metadata={**metadata, "trace_artifact_id": trace_artifact.artifact_id},
        )
        return BrowserWorkerRun(
            worker_result=worker_result,
            event_records=[*event_records, _worker_result_event(request, worker_result)],
            permission_session_custody_token=custody_token,
        )

    async def _run_browser_use_agent_async(
        self,
        *,
        request: WorkerRequest,
        snapshot: Any,
        task: str,
        executable: Path,
        llm: Any,
        llm_metadata: dict[str, str],
        max_steps: int,
        permission_gate: BrowserActionPermissionGate,
        permission_events: tuple[EventRecord, ...],
        permission_metadata: dict[str, Any],
    ) -> BrowserWorkerRun:
        paths = configure_browser_use_environment(self.project_root)
        from browser_use.agent.service import Agent
        from browser_use.browser.session import BrowserSession
        from browser_use.tools.service import Tools

        allowed_domains = request.constraints.get("allowed_domains")
        allowed_domains_arg = [str(item) for item in allowed_domains] if isinstance(allowed_domains, list) else None
        session = BrowserSession(
            executable_path=executable,
            headless=request.constraints.get("headless", True) is not False,
            keep_alive=False,
            user_data_dir=paths.temp_dir / f"browser-use-agent-user-data-dir-{request.request_id}",
            downloads_path=paths.root / "downloads" / request.request_id,
            allowed_domains=allowed_domains_arg,
            enable_default_extensions=False,
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-extensions",
            ],
        )
        available_file_paths = _browser_use_agent_available_files(self.workspace_root, request.constraints)
        initial_actions = _browser_use_agent_initial_actions(request)
        agent = Agent(
            task=task,
            llm=llm,
            browser_session=session,
            tools=Tools(),
            initial_actions=initial_actions,
            available_file_paths=[str(path) for path in available_file_paths],
            use_vision=request.constraints.get("use_vision", True) is not False,
            max_failures=_bounded_int(request.constraints.get("max_failures"), default=3, minimum=1, maximum=20),
            max_actions_per_step=_bounded_int(
                request.constraints.get("max_actions_per_step"),
                default=5,
                minimum=1,
                maximum=20,
            ),
            max_history_items=_optional_int(request.constraints.get("max_history_items")),
            message_compaction=request.constraints.get("message_compaction", True) is not False,
            file_system_path=str(paths.root / "files" / request.request_id),
            source="zyra-browser-worker",
            task_id=request.task_id,
            include_recent_events=True,
            enable_signal_handler=False,
            calculate_cost=request.constraints.get("calculate_cost") is True,
            step_timeout=_bounded_int(request.constraints.get("step_timeout_seconds"), default=120, minimum=10, maximum=600),
        )
        try:
            history = await agent.run(max_steps=max_steps)
        finally:
            await session.close()

        history_payload = history.model_dump()
        history_artifact = self.artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=json.dumps(history_payload, ensure_ascii=False, indent=2, default=str),
            title=f"Browser-use Agent history {request.request_id}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=request.node_id,
        )
        history_events = _browser_use_agent_history_events(request, task, history_payload)
        event_records = [*permission_events, *history_events]
        ok = history.is_done() and not history.has_errors() and history.is_successful() is not False
        trace_artifact = self.artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=_agent_trace_markdown(request, snapshot, event_records, ok, self.browser_use_health),
            title=f"Browser-use Agent trace {request.request_id}",
            kind=ArtifactKind.TRACE,
            extension=".md",
            producer_node_id=request.node_id,
        )
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=ok,
            summary=(
                "BrowserWorker completed browser-use Agent task."
                if ok
                else "BrowserWorker stopped with browser-use Agent errors."
            ),
            artifacts=[history_artifact, trace_artifact],
            events=[to_jsonable(event) for event in event_records],
            error=None if ok else "browser_use_agent_failed",
            metadata={
                **_snapshot_metadata(snapshot),
                **_action_registry_metadata(self.action_registry),
                **browser_use_runtime_metadata(self.browser_use_health),
                **llm_metadata,
                **permission_gate.metadata(),
                **permission_metadata,
                "browser_backend": "browser-use-agent",
                "browser_permission_action_execution_count": "1",
                "browser_agent_steps": str(len(history.history)),
                "browser_agent_done": str(history.is_done()).lower(),
                "browser_agent_successful": str(history.is_successful()).lower(),
                "browser_agent_has_errors": str(history.has_errors()).lower(),
                "browser_agent_max_steps": str(max_steps),
                "browser_agent_available_files": str(len(available_file_paths)),
                "browser_executable": str(executable),
                "history_artifact_id": history_artifact.artifact_id,
                "trace_artifact_id": trace_artifact.artifact_id,
            },
        )
        return BrowserWorkerRun(
            worker_result=worker_result,
            event_records=[*event_records, _worker_result_event(request, worker_result)],
            permission_session_custody_token=permission_gate.custody_token,
        )

    async def _run_browser_use_live_async(
        self,
        *,
        request: WorkerRequest,
        snapshot: Any,
        plan: list[dict[str, Any]],
        executable: Path,
        permission_gate: BrowserActionPermissionGate,
    ) -> BrowserWorkerRun:
        paths = configure_browser_use_environment(self.project_root)
        from browser_use.browser.session import BrowserSession
        from browser_use.filesystem.file_system import FileSystem
        from browser_use.tools.service import Tools

        event_records: list[EventRecord] = []
        artifacts = []
        collected_download_paths: set[Path] = set()
        continue_on_error = request.constraints.get("continue_on_error") is True
        navigation_timeout = (
            _bounded_int(
                request.constraints.get("live_navigation_timeout_seconds"),
                default=30,
                minimum=5,
                maximum=120,
            )
            if request.constraints.get("live_navigation_timeout_seconds") is not None
            else None
        )
        action_timeout = (
            _bounded_int(
                request.constraints.get("live_action_timeout_seconds"),
                default=30,
                minimum=5,
                maximum=120,
            )
            if request.constraints.get("live_action_timeout_seconds") is not None
            else None
        )
        allowed_domains = request.constraints.get("allowed_domains")
        allowed_domains_arg = [str(item) for item in allowed_domains] if isinstance(allowed_domains, list) else None
        downloads_dir = paths.root / "downloads" / request.request_id
        downloads_dir.mkdir(parents=True, exist_ok=True)
        session = BrowserSession(
            executable_path=executable,
            headless=request.constraints.get("headless", True) is not False,
            keep_alive=False,
            user_data_dir=paths.temp_dir / f"browser-use-user-data-dir-{request.request_id}",
            downloads_path=downloads_dir,
            traces_dir=paths.root / "traces" / request.request_id,
            enable_default_extensions=False,
            captcha_solver=False,
            chromium_sandbox=False,
            accept_downloads=True,
            allowed_domains=allowed_domains_arg,
            args=[
                "--disable-background-networking",
                "--disable-component-extensions-with-background-pages",
                "--disable-extensions",
            ],
        )
        tools = Tools()
        browser_use_file_system = FileSystem(paths.root / "files" / request.request_id, create_default_files=False)
        current_url = ""
        session_started = False

        try:
            for index, step in enumerate(plan, start=1):
                action = str(step.get("action") or step.get("browser_action") or "")
                arguments = step.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = {}
                descriptor = self.action_registry.get(action)
                normalized_action = self.action_registry.normalize_action(action)
                action_metadata = {
                    **_action_metadata(descriptor, normalized_action),
                    "browser_backend": "browser-use-live",
                }
                target_url = str(arguments.get("url") or current_url or "")
                barrier = self._permission_barrier(
                    gate=permission_gate,
                    request=request,
                    index=index,
                    action=action,
                    normalized_action=normalized_action,
                    arguments=arguments,
                    descriptor=descriptor,
                    backend="browser-use-live",
                    current_url=current_url,
                    target_url=target_url,
                )
                event_records.extend(barrier.events)
                action_metadata = {**action_metadata, **barrier.metadata}
                if not barrier.allowed:
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=False,
                            summary=barrier.summary,
                            output=barrier.output,
                            error=barrier.error,
                        )
                    )
                    if not continue_on_error:
                        break
                    continue
                if not session_started:
                    # Browser process/profile creation belongs behind the same
                    # side-effect barrier as the first executable action.
                    await session.start()
                    session_started = True
                try:
                    if normalized_action == "open_url":
                        current_url = str(arguments.get("url") or "")
                        _validate_live_url(current_url, request.constraints)
                        await _browser_use_live_navigate(
                            session,
                            current_url,
                            navigation_timeout_seconds=navigation_timeout,
                            action_timeout_seconds=action_timeout,
                        )
                        state, output = await _browser_use_live_state(session)
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live state {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.append(state_artifact)
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=f"Browser-use live opened {state.url}",
                                output=output,
                                artifacts=[state_artifact],
                            )
                        )
                    elif normalized_action == "extract_text":
                        state, output = await _browser_use_live_state(session)
                        text_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=state.text,
                            title=f"Browser-use live extracted text {index}",
                            kind=ArtifactKind.MARKDOWN,
                            extension=".md",
                            producer_node_id=request.node_id,
                        )
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live state {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.extend([text_artifact, state_artifact])
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=f"Browser-use live extracted text from {state.url}",
                                output=output,
                                artifacts=[text_artifact, state_artifact],
                            )
                        )
                    elif normalized_action == "snapshot_state":
                        _state, output = await _browser_use_live_state(session)
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live snapshot {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.append(state_artifact)
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary="Browser-use live captured browser state.",
                                output=output,
                                artifacts=[state_artifact],
                            )
                        )
                    elif normalized_action == "click_element":
                        target_index = await _resolve_browser_use_live_target_index(
                            session,
                            arguments,
                            purpose="click_element",
                        )
                        result = await tools.click(index=target_index, browser_session=session)
                        result_output = _browser_use_action_result_output(result)
                        if result_output.get("error"):
                            raise ValueError(str(result_output["error"]))
                        _state, output = await _browser_use_live_state(session)
                        output["browser_use_action_result"] = result_output
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live click state {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.append(state_artifact)
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=f"Browser-use live clicked element {arguments.get('index')}",
                                output=output,
                                artifacts=[state_artifact],
                            )
                        )
                    elif normalized_action == "input_text":
                        input_action = getattr(tools, "input")
                        target_index = await _resolve_browser_use_live_target_index(
                            session,
                            arguments,
                            purpose="input_text",
                        )
                        result = await input_action(
                            index=target_index,
                            text=str(arguments.get("text") or ""),
                            clear=arguments.get("clear", True) is not False,
                            browser_session=session,
                        )
                        result_output = _browser_use_action_result_output(result)
                        if result_output.get("error"):
                            raise ValueError(str(result_output["error"]))
                        _state, output = await _browser_use_live_state(session)
                        output["browser_use_action_result"] = result_output
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live input state {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.append(state_artifact)
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=f"Browser-use live input text into element {target_index}",
                                output=output,
                                artifacts=[state_artifact],
                            )
                        )
                    elif normalized_action == "upload_file":
                        upload_action = getattr(tools, "upload_file")
                        target_index = await _resolve_browser_use_live_target_index(
                            session,
                            arguments,
                            purpose="upload_file",
                        )
                        upload_path = _resolve_live_upload_path(self.workspace_root, arguments.get("path"))
                        result = await upload_action(
                            index=target_index,
                            path=str(upload_path),
                            browser_session=session,
                            available_file_paths=[str(upload_path)],
                            file_system=browser_use_file_system,
                        )
                        result_output = _browser_use_action_result_output(result)
                        if result_output.get("error"):
                            raise ValueError(str(result_output["error"]))
                        _state, output = await _browser_use_live_state(session)
                        output["browser_use_action_result"] = result_output
                        output["uploaded_file"] = {
                            "path": str(upload_path),
                            "name": upload_path.name,
                            "size_bytes": upload_path.stat().st_size,
                        }
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live upload state {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.append(state_artifact)
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=f"Browser-use live uploaded {upload_path.name} through element {target_index}.",
                                output=output,
                                artifacts=[state_artifact],
                            )
                        )
                    elif normalized_action == "search_page":
                        state, output = await _browser_use_live_state(session)
                        matches = _search_page_matches(state.text, arguments)
                        output["matches"] = matches
                        output["match_count"] = len(matches)
                        search_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live search page {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.append(search_artifact)
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=f"Browser-use live found {len(matches)} page match(es)",
                                output=output,
                                artifacts=[search_artifact],
                            )
                        )
                    elif normalized_action == "take_screenshot":
                        screenshot_format, screenshot_extension = _screenshot_format(arguments.get("format"))
                        screenshot_bytes = await session.take_screenshot(
                            full_page=_truthy(arguments.get("full_page"), default=False),
                            format=screenshot_format,
                            quality=_screenshot_quality(arguments.get("quality"), screenshot_format),
                        )
                        screenshot_artifact = self.artifact_store.write_bytes(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=screenshot_bytes,
                            title=f"Browser-use live screenshot {index}",
                            kind=ArtifactKind.SCREENSHOT,
                            extension=screenshot_extension,
                            producer_node_id=request.node_id,
                            metadata={
                                "browser_backend": "browser-use-live",
                                "screenshot_format": screenshot_format,
                                "full_page": str(_truthy(arguments.get("full_page"), default=False)).lower(),
                            },
                        )
                        _state, output = await _browser_use_live_state(session)
                        output["screenshot_artifact_id"] = screenshot_artifact.artifact_id
                        output["screenshot_size_bytes"] = len(screenshot_bytes)
                        output["screenshot_format"] = screenshot_format
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live screenshot state {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.extend([screenshot_artifact, state_artifact])
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=f"Browser-use live captured screenshot artifact {screenshot_artifact.artifact_id}.",
                                output=output,
                                artifacts=[screenshot_artifact, state_artifact],
                            )
                        )
                    elif normalized_action == "collect_downloads":
                        settle_seconds = _safe_download_settle_seconds(arguments.get("settle_seconds", 1.0))
                        if settle_seconds:
                            await asyncio.sleep(settle_seconds)
                        download_artifacts = []
                        downloaded_files = []
                        for downloaded_path in _browser_use_download_paths(session, downloads_dir):
                            if downloaded_path in collected_download_paths:
                                continue
                            content = downloaded_path.read_bytes()
                            download_artifact = self.artifact_store.write_bytes(
                                run_id=request.run_id,
                                task_id=request.task_id,
                                content=content,
                                title=f"Browser-use live download {index}: {downloaded_path.name}",
                                kind=ArtifactKind.FILE,
                                extension=downloaded_path.suffix or ".bin",
                                producer_node_id=request.node_id,
                                metadata={
                                    "browser_backend": "browser-use-live",
                                    "downloaded_file_name": downloaded_path.name,
                                    "source_download_path": str(downloaded_path),
                                    "download_size_bytes": len(content),
                                },
                            )
                            artifacts.append(download_artifact)
                            download_artifacts.append(download_artifact)
                            collected_download_paths.add(downloaded_path)
                            downloaded_files.append(
                                {
                                    "path": str(downloaded_path),
                                    "name": downloaded_path.name,
                                    "size_bytes": len(content),
                                    "artifact_id": download_artifact.artifact_id,
                                }
                            )
                        _state, output = await _browser_use_live_state(session)
                        output["download_count"] = len(downloaded_files)
                        output["downloaded_files"] = downloaded_files
                        output["download_artifact_ids"] = [artifact.artifact_id for artifact in download_artifacts]
                        output["downloads_dir"] = str(downloads_dir)
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live downloads state {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.append(state_artifact)
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=f"Browser-use live collected {len(downloaded_files)} downloaded file(s).",
                                output=output,
                                artifacts=[*download_artifacts, state_artifact],
                            )
                        )
                    elif normalized_action == "save_as_pdf":
                        pdf_bytes = await _browser_use_live_pdf_bytes(session, arguments)
                        pdf_artifact = self.artifact_store.write_bytes(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=pdf_bytes,
                            title=f"Browser-use live PDF {index}",
                            kind=ArtifactKind.FILE,
                            extension=".pdf",
                            producer_node_id=request.node_id,
                            metadata={
                                "browser_backend": "browser-use-live",
                                "content_type": "application/pdf",
                                "suggested_file_name": str(arguments.get("file_name") or "page.pdf"),
                                "paper_format": str(arguments.get("paper_format") or "Letter"),
                            },
                        )
                        _state, output = await _browser_use_live_state(session)
                        output["pdf_artifact_id"] = pdf_artifact.artifact_id
                        output["pdf_size_bytes"] = len(pdf_bytes)
                        output["pdf_paper_format"] = str(arguments.get("paper_format") or "Letter")
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live PDF state {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.extend([pdf_artifact, state_artifact])
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=f"Browser-use live captured PDF artifact {pdf_artifact.artifact_id}.",
                                output=output,
                                artifacts=[pdf_artifact, state_artifact],
                            )
                        )
                    elif normalized_action in LIVE_BROWSER_USE_TOOL_ACTIONS:
                        result = await _run_browser_use_live_tool_action(
                            tools=tools,
                            session=session,
                            normalized_action=normalized_action,
                            arguments=arguments,
                        )
                        result_output = _browser_use_action_result_output(result)
                        if result_output.get("error"):
                            raise ValueError(str(result_output["error"]))
                        _state, output = await _browser_use_live_state(session)
                        output["browser_use_action_result"] = result_output
                        output["browser_use_tool_action"] = normalized_action
                        state_artifact = self.artifact_store.write_text(
                            run_id=request.run_id,
                            task_id=request.task_id,
                            content=json.dumps(output, ensure_ascii=False, indent=2),
                            title=f"Browser-use live {normalized_action} state {index}",
                            kind=ArtifactKind.STRUCTURED_DATA,
                            extension=".json",
                            producer_node_id=request.node_id,
                        )
                        artifacts.append(state_artifact)
                        event_records.append(
                            _browser_action_event(
                                request,
                                index,
                                action,
                                action_metadata=action_metadata,
                                ok=True,
                                summary=_browser_use_live_tool_summary(normalized_action, result_output),
                                output=output,
                                artifacts=[state_artifact],
                            )
                        )
                    else:
                        raise ValueError(f"Unknown browser action: {action}")
                except Exception as error:  # noqa: BLE001 - per-step live failures must be traceable.
                    event_records.append(
                        _browser_action_event(
                            request,
                            index,
                            action,
                            action_metadata=action_metadata,
                            ok=False,
                            summary=f"Browser-use live action failed: {action}",
                            output={"action": action, "message": str(error), "browser_backend": "browser-use-live"},
                            error=type(error).__name__,
                        )
                    )
                    if not continue_on_error:
                        break
        finally:
            if session_started:
                await session.close()

        browser_events = _browser_action_events(event_records)
        ok = bool(browser_events) and all(_event_browser_result_ok(event) for event in browser_events)
        action_execution_count = sum(
            event.payload.get("browser_action", {}).get("permission_execution_grant_consumed") == "true"
            for event in browser_events
        )
        failed_action = next(
            (
                event.payload.get("browser_result", {})
                for event in browser_events
                if not _event_browser_result_ok(event)
            ),
            {},
        )
        failed_output = failed_action.get("output", {}) if isinstance(failed_action, dict) else {}
        if not isinstance(failed_output, dict):
            failed_output = {}
        trace_artifact = self.artifact_store.write_text(
            run_id=request.run_id,
            task_id=request.task_id,
            content=_trace_markdown(request, snapshot, event_records, ok, self.browser_use_health),
            title=f"Browser-use live trace {request.request_id}",
            kind=ArtifactKind.TRACE,
            extension=".md",
            producer_node_id=request.node_id,
        )
        artifacts.append(trace_artifact)
        worker_result = WorkerResult(
            request_id=request.request_id,
            ok=ok,
            summary=(
                "BrowserWorker completed browser-use live plan."
                if ok
                else "BrowserWorker stopped on a failed browser-use live action."
            ),
            artifacts=artifacts,
            events=[to_jsonable(event) for event in event_records],
            error=None if ok else "browser_use_live_action_failed",
            metadata={
                **_snapshot_metadata(snapshot),
                **_action_registry_metadata(self.action_registry),
                **browser_use_runtime_metadata(self.browser_use_health),
                **permission_gate.metadata(),
                "browser_backend": "browser-use-live",
                "browser_steps": str(len(browser_events)),
                "browser_permission_action_execution_count": str(action_execution_count),
                "browser_executable": str(executable),
                "trace_artifact_id": trace_artifact.artifact_id,
                "live_navigation_timeout_seconds": str(navigation_timeout or ""),
                "live_action_timeout_seconds": str(action_timeout or ""),
                "live_failed_action_index": str(failed_action.get("step_index") or ""),
                "live_failed_action": str(failed_action.get("action") or ""),
                "live_failed_action_error": str(failed_action.get("error") or ""),
                "live_failed_action_message": str(failed_output.get("message") or ""),
            },
        )
        return BrowserWorkerRun(
            worker_result=worker_result,
            event_records=[*event_records, _worker_result_event(request, worker_result)],
            permission_session_custody_token=permission_gate.custody_token,
        )


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[dict[str, str]] = []
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            attrs_dict = {name: value or "" for name, value in attrs}
            href = attrs_dict.get("href")
            if href:
                self.links.append({"href": href, "text": ""})
        if tag in {"p", "br", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = html.unescape(data).strip()
        if not text:
            return
        if self._in_title:
            self.title = text
        self._parts.append(text)
        if self.links and not self.links[-1]["text"]:
            self.links[-1]["text"] = text[:120]

    def text(self) -> str:
        content = " ".join(part.strip() for part in self._parts if part.strip())
        return re.sub(r"\s+", " ", content).strip()


def _browser_plan_from_request(request: WorkerRequest) -> list[dict[str, Any]]:
    plan = request.constraints.get("browser_plan")
    if not isinstance(plan, list):
        return []
    return [dict(item) for item in plan if isinstance(item, dict)]


def _browser_backend_from_request(request: WorkerRequest) -> str:
    raw_backend = request.constraints.get(
        "browser_backend",
        request.constraints.get("backend", "zyra-browser-productized"),
    )
    if request.constraints.get("browser_use_live") is True:
        raw_backend = "browser-use-live"
    backend = str(raw_backend or "zyra-browser-productized").strip().lower().replace("_", "-")
    if backend in {"live", "browser-live", "browser-use"}:
        return "browser-use-live"
    if backend == "browser-use-live":
        return "browser-use-live"
    if backend in {"productized", "zyra-browser", "zyra-browser-productized"}:
        return "zyra-browser-productized"
    if backend in {"agent", "browser-agent", "browser-use-agent"}:
        return "browser-use-agent"
    return backend


def _browser_lifecycle_command_from_request(request: WorkerRequest) -> str:
    value = request.constraints.get("browser_lifecycle_command")
    if value in (None, ""):
        value = request.constraints.get("browser_session_command")
    return str(value or "").strip().lower().replace("_", "-")


def _request_contains_permission_capability_echo(request: WorkerRequest) -> bool:
    tokens = tuple(
        dict.fromkeys(
            str(request.constraints.get(key) or "")
            for key in ("permission_session_custody_token", "session_custody_token")
            if str(request.constraints.get(key) or "")
        )
    )
    return PermissionSessionCustodyStore.contains_capability_echo(request.constraints, tokens)


def _required_browser_session_id(command: BrowserSessionCommand) -> str:
    if not command.browser_session_id:
        raise ValueError("browser_session_id is required for this lifecycle command")
    return command.browser_session_id


def _browser_session_event(
    request: WorkerRequest,
    phase: str,
    result: dict[str, Any],
) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.BROWSER_SESSION_LIFECYCLE,
        payload={"browser_session": {"phase": phase, "result": result}},
    )


def _browser_productized_failure(
    request: WorkerRequest,
    *,
    code: str,
    summary: str,
    details: str = "",
    metadata: dict[str, Any] | None = None,
) -> BrowserWorkerRun:
    failure_metadata = dict(metadata or {})
    event = EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.BROWSER_SESSION_LIFECYCLE,
        payload={
            "browser_session": {
                "phase": "failed",
                "error": code,
                "details": details,
                **failure_metadata,
            }
        },
    )
    worker_result = WorkerResult(
        request_id=request.request_id,
        ok=False,
        summary=summary,
        error=code,
        events=[to_jsonable(event)],
        metadata={
            "browser_backend": "zyra-browser-productized",
            "browser_runtime_vendor_required": "false",
            **({"browser_runtime_error_details": details} if details else {}),
            **failure_metadata,
        },
    )
    return BrowserWorkerRun(worker_result=worker_result, event_records=[event])


def _browser_use_agent_task_from_request(request: WorkerRequest) -> str:
    for key in ("agent_task", "task", "goal"):
        value = request.constraints.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _browser_use_agent_initial_actions(request: WorkerRequest) -> list[dict[str, dict[str, Any]]] | None:
    raw_actions = request.constraints.get("initial_actions")
    if isinstance(raw_actions, list):
        return [dict(item) for item in raw_actions if isinstance(item, dict)]
    start_url = str(request.constraints.get("start_url") or request.constraints.get("url") or "").strip()
    if not start_url:
        return None
    _validate_live_url(start_url, request.constraints)
    return [{"navigate": {"url": start_url, "new_tab": False}}]


def _browser_use_agent_available_files(workspace_root: Path, constraints: dict[str, Any]) -> list[Path]:
    raw_paths = constraints.get("available_file_paths")
    if not isinstance(raw_paths, list):
        return []
    workspace = workspace_root.resolve()
    resolved_paths: list[Path] = []
    for raw_path in raw_paths:
        candidate = Path(str(raw_path)).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        resolved = candidate.resolve()
        if not _path_within(resolved, workspace):
            raise ValueError(f"available_file_paths must stay inside workspace: {raw_path}")
        if not resolved.exists():
            raise ValueError(f"available file path does not exist: {raw_path}")
        if not resolved.is_file():
            raise ValueError(f"available file path is not a file: {raw_path}")
        resolved_paths.append(resolved)
    return resolved_paths


def _create_browser_use_agent_llm(request: WorkerRequest) -> tuple[Any | None, dict[str, str], str | None]:
    constraints = request.constraints
    raw_model = str(
        constraints.get("browser_use_agent_model")
        or constraints.get("llm_model")
        or os.environ.get("BROWSER_USE_LLM_MODEL")
        or os.environ.get("DEFAULT_LLM")
        or ""
    ).strip()
    provider = str(
        constraints.get("browser_use_agent_provider")
        or constraints.get("llm_provider")
        or os.environ.get("BROWSER_USE_LLM_PROVIDER")
        or os.environ.get("MODEL_PROVIDER")
        or _infer_browser_use_agent_provider(raw_model)
    ).strip().lower()
    provider = provider.replace("_", "-")
    custom_key_env = str(constraints.get("browser_use_agent_api_key_env") or "").strip()
    base_url = str(constraints.get("base_url") or constraints.get("llm_base_url") or "").strip() or None
    temperature = _optional_float(constraints.get("temperature"))

    try:
        if provider in {"openai", "openai-compatible"}:
            key_env = custom_key_env or "OPENAI_API_KEY"
            model = raw_model or "gpt-4.1-mini"
            metadata = _browser_use_agent_llm_metadata(provider, model, key_env)
            api_key = os.environ.get(key_env, "").strip()
            if not api_key:
                return None, metadata, f"{key_env} is not set"
            from browser_use.llm.openai.chat import ChatOpenAI

            kwargs: dict[str, Any] = {"model": model, "api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            if temperature is not None:
                kwargs["temperature"] = temperature
            return ChatOpenAI(**kwargs), metadata, None

        if provider == "anthropic":
            key_env = custom_key_env or "ANTHROPIC_API_KEY"
            model = raw_model or "claude-sonnet-4-5"
            metadata = _browser_use_agent_llm_metadata(provider, model, key_env)
            api_key = os.environ.get(key_env, "").strip()
            if not api_key:
                return None, metadata, f"{key_env} is not set"
            from browser_use.llm.anthropic.chat import ChatAnthropic

            kwargs = {"model": model, "api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            if temperature is not None:
                kwargs["temperature"] = temperature
            return ChatAnthropic(**kwargs), metadata, None

        if provider in {"google", "gemini"}:
            key_env = custom_key_env or "GOOGLE_API_KEY"
            model = raw_model or "gemini-2.5-flash"
            metadata = _browser_use_agent_llm_metadata("google", model, key_env)
            api_key = os.environ.get(key_env, "").strip()
            if not api_key:
                return None, metadata, f"{key_env} is not set"
            from browser_use.llm.google.chat import ChatGoogle

            kwargs = {"model": model, "api_key": api_key}
            if temperature is not None:
                kwargs["temperature"] = temperature
            return ChatGoogle(**kwargs), metadata, None

        if provider == "litellm":
            key_env = custom_key_env or "LITELLM_API_KEY"
            model = raw_model
            metadata = _browser_use_agent_llm_metadata(provider, model, key_env)
            if not model:
                return None, metadata, "litellm provider requires browser_use_agent_model"
            api_key = os.environ.get(key_env, "").strip() or None
            from browser_use.llm.litellm.chat import ChatLiteLLM

            kwargs = {"model": model, "api_key": api_key}
            if temperature is not None:
                kwargs["temperature"] = temperature
            return ChatLiteLLM(**kwargs), metadata, None

        if provider in {"browser-use-name", "named"}:
            model = raw_model
            key_env = custom_key_env or ""
            metadata = _browser_use_agent_llm_metadata(provider, model, key_env)
            if not model:
                return None, metadata, "named provider requires browser_use_agent_model"
            from browser_use.llm.models import get_llm_by_name

            return get_llm_by_name(model), metadata, None

        metadata = _browser_use_agent_llm_metadata(provider or "unknown", raw_model, custom_key_env)
        return None, metadata, f"unsupported provider: {provider or 'missing'}"
    except Exception as error:  # noqa: BLE001 - keep LLM setup errors traceable.
        metadata = _browser_use_agent_llm_metadata(provider or "unknown", raw_model, custom_key_env)
        return None, metadata, f"{type(error).__name__}: {error}"


def _infer_browser_use_agent_provider(model: str) -> str:
    model_lower = model.lower()
    if model_lower.startswith("anthropic_") or "claude" in model_lower:
        return "anthropic"
    if model_lower.startswith("google_") or "gemini" in model_lower:
        return "google"
    if model_lower.startswith("litellm_"):
        return "litellm"
    return "openai"


def _browser_use_agent_llm_metadata(provider: str, model: str, key_env: str) -> dict[str, str]:
    return {
        "browser_agent_llm_provider": provider,
        "browser_agent_llm_model": model,
        "browser_agent_llm_key_env": key_env,
        "browser_agent_llm_key_configured": str(bool(key_env and os.environ.get(key_env, "").strip())).lower(),
    }


def _browser_use_agent_history_events(
    request: WorkerRequest,
    task: str,
    history_payload: dict[str, Any],
) -> list[EventRecord]:
    history_items = history_payload.get("history")
    if not isinstance(history_items, list) or not history_items:
        return [
            _browser_agent_event(
                request,
                step_index=0,
                task=task,
                ok=False,
                summary="Browser-use Agent produced no history.",
                output={"history_count": 0, "browser_backend": "browser-use-agent"},
                error="empty_agent_history",
            )
        ]

    events: list[EventRecord] = []
    for index, item in enumerate(history_items, start=1):
        if not isinstance(item, dict):
            continue
        model_output = item.get("model_output") if isinstance(item.get("model_output"), dict) else {}
        results = item.get("result") if isinstance(item.get("result"), list) else []
        state = item.get("state") if isinstance(item.get("state"), dict) else {}
        errors = [str(result.get("error")) for result in results if isinstance(result, dict) and result.get("error")]
        extracted = next(
            (
                str(result.get("extracted_content"))
                for result in results
                if isinstance(result, dict) and result.get("extracted_content")
            ),
            "",
        )
        summary = errors[0] if errors else extracted or str(model_output.get("next_goal") or "Browser-use Agent step completed.")
        events.append(
            _browser_agent_event(
                request,
                step_index=index,
                task=task,
                ok=not errors,
                summary=summary[:500],
                output={
                    "browser_backend": "browser-use-agent",
                    "evaluation_previous_goal": model_output.get("evaluation_previous_goal"),
                    "memory": _clip_text(model_output.get("memory"), 1000),
                    "next_goal": _clip_text(model_output.get("next_goal"), 1000),
                    "actions": model_output.get("action", [])[:5] if isinstance(model_output.get("action"), list) else [],
                    "results": results[:5],
                    "url": state.get("url"),
                    "title": state.get("title"),
                },
                error=errors[0] if errors else None,
            )
        )
    return events


def _clip_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text[:limit]


def _page_state(url: str, html_content: str) -> BrowserPageState:
    extractor = _TextExtractor()
    extractor.feed(html_content)
    text = extractor.text()
    title = extractor.title or _title_from_text(text) or url
    return BrowserPageState(
        url=url,
        title=title,
        text=text,
        links=extractor.links[:50],
        html_chars=len(html_content),
        text_chars=len(text),
    )


def _title_from_text(text: str) -> str:
    return text[:80].strip()


def _state_summary(state: BrowserPageState) -> dict[str, Any]:
    return {
        "url": state.url,
        "title": state.title,
        "text_preview": state.text[:1000],
        "html_chars": state.html_chars,
        "text_chars": state.text_chars,
        "links": state.links[:20],
        "link_count": len(state.links),
    }


def _click_link_url(state: BrowserPageState, arguments: dict[str, Any]) -> str:
    raw_index = arguments.get("index")
    try:
        index = int(raw_index)
    except (TypeError, ValueError) as error:
        raise ValueError("click_element requires an integer index") from error
    if index < 0 or index >= len(state.links):
        raise ValueError(f"click_element index out of range: {index}")
    href = state.links[index].get("href") or ""
    if not href:
        raise ValueError(f"click_element target has no href: {index}")
    return urljoin(state.url, href)


def _search_page_matches(text: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    pattern = str(arguments.get("pattern") or "")
    if not pattern:
        raise ValueError("search_page requires pattern")
    max_results = _bounded_int(arguments.get("max_results"), default=25, minimum=1, maximum=100)
    context_chars = _bounded_int(arguments.get("context_chars"), default=150, minimum=0, maximum=1000)
    case_sensitive = arguments.get("case_sensitive") is True
    use_regex = arguments.get("regex") is True
    flags = 0 if case_sensitive else re.IGNORECASE
    if use_regex:
        regex = re.compile(pattern, flags)
    else:
        regex = re.compile(re.escape(pattern), flags)
    matches = []
    for match in regex.finditer(text):
        if len(matches) >= max_results:
            break
        start = max(match.start() - context_chars, 0)
        end = min(match.end() + context_chars, len(text))
        matches.append(
            {
                "start": match.start(),
                "end": match.end(),
                "match": match.group(0),
                "context": text[start:end],
            }
        )
    return matches


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _validate_allowed_url(url: str, constraints: dict[str, Any]) -> None:
    parsed = urlparse(url)
    allowed_schemes = constraints.get("allowed_schemes")
    if isinstance(allowed_schemes, list) and parsed.scheme not in {str(item) for item in allowed_schemes}:
        raise ValueError(f"url scheme is not allowed: {parsed.scheme}")
    allowed_domains = constraints.get("allowed_domains")
    if parsed.scheme in {"http", "https"} and isinstance(allowed_domains, list) and allowed_domains:
        host = parsed.hostname or ""
        if host not in {str(item) for item in allowed_domains}:
            raise ValueError(f"url domain is not allowed: {host}")


def _validate_live_url(url: str, constraints: dict[str, Any]) -> None:
    _validate_allowed_url(url, constraints)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            f"browser-use-live backend supports http/https URLs; use static backend for scheme: {parsed.scheme}"
        )


async def _browser_use_live_state(session: Any) -> tuple[BrowserPageState, dict[str, Any]]:
    summary = await session.get_browser_state_summary(include_screenshot=False)
    text = await session.get_state_as_text()
    selector_map = getattr(getattr(summary, "dom_state", None), "selector_map", {}) or {}
    tabs = getattr(summary, "tabs", []) or []
    state = BrowserPageState(
        url=str(getattr(summary, "url", "") or ""),
        title=str(getattr(summary, "title", "") or getattr(summary, "url", "") or ""),
        text=text,
        links=[],
        html_chars=0,
        text_chars=len(text),
    )
    output = {
        **_state_summary(state),
        "browser_backend": "browser-use-live",
        "browser_use_selector_count": len(selector_map),
        "browser_use_interactive_elements": _browser_use_interactive_elements(selector_map),
        "browser_use_tab_count": len(tabs),
        "browser_use_tabs": [
            {
                "url": getattr(tab, "url", ""),
                "title": getattr(tab, "title", ""),
                "target_id": str(getattr(tab, "target_id", "")),
            }
            for tab in tabs[:10]
        ],
    }
    return state, output


async def _browser_use_live_navigate(
    session: Any,
    url: str,
    *,
    navigation_timeout_seconds: int | None,
    action_timeout_seconds: int | None,
) -> None:
    if navigation_timeout_seconds is None and action_timeout_seconds is None:
        await session.navigate_to(url)
        return

    from browser_use.browser.events import NavigateToUrlEvent

    navigation_timeout = navigation_timeout_seconds or 8
    event_timeout = max(action_timeout_seconds or 30, navigation_timeout + 5)
    event = session.event_bus.dispatch(
        NavigateToUrlEvent(
            url=url,
            timeout_ms=navigation_timeout * 1000,
            event_timeout=float(event_timeout),
        )
    )
    await event
    await event.event_result(raise_if_any=True, raise_if_none=False)


async def _ensure_browser_use_live_index(session: Any, index: int) -> None:
    summary = await session.get_browser_state_summary(include_screenshot=False)
    selector_map = getattr(getattr(summary, "dom_state", None), "selector_map", {}) or {}
    if index not in {int(item) for item in selector_map.keys()}:
        available = ",".join(str(item) for item in sorted(selector_map.keys(), key=_safe_int))
        raise ValueError(f"browser-use selector index {index} is not available; available indexes: {available}")


async def _resolve_browser_use_live_target_index(session: Any, arguments: dict[str, Any], *, purpose: str) -> int:
    explicit_index = arguments.get("index")
    if explicit_index not in (None, ""):
        target_index = int(explicit_index)
        await _ensure_browser_use_live_index(session, target_index)
        return target_index

    summary = await session.get_browser_state_summary(include_screenshot=False)
    selector_map = getattr(getattr(summary, "dom_state", None), "selector_map", {}) or {}
    element_id = str(arguments.get("id") or arguments.get("element_id") or "").strip()
    selector = str(arguments.get("selector") or arguments.get("css_selector") or "").strip()
    if not element_id and selector.startswith("#") and len(selector) > 1:
        element_id = selector[1:]

    if element_id:
        for index, node in sorted(selector_map.items(), key=lambda item: _safe_int(item[0])):
            attributes = getattr(node, "attributes", {}) or {}
            if str(attributes.get("id") or "") == element_id:
                return int(index)

    available = ",".join(str(item) for item in sorted(selector_map.keys(), key=_safe_int))
    if selector:
        raise ValueError(f"{purpose} could not resolve selector {selector!r}; available indexes: {available}")
    raise ValueError(f"{purpose} requires index, id, or selector; available indexes: {available}")


async def _run_browser_use_live_tool_action(
    *,
    tools: Any,
    session: Any,
    normalized_action: str,
    arguments: dict[str, Any],
) -> Any:
    if normalized_action == "wait":
        return await tools.wait(seconds=_safe_wait_seconds(arguments.get("seconds", 3)))
    if normalized_action == "go_back":
        return await tools.go_back(browser_session=session)
    if normalized_action == "scroll_page":
        target_index = _optional_int(arguments.get("index"))
        if target_index not in (None, 0):
            await _ensure_browser_use_live_index(session, target_index)
        return await tools.scroll(
            down=_truthy(arguments.get("down"), default=True),
            pages=_safe_scroll_pages(arguments.get("pages", 1.0)),
            index=target_index,
            browser_session=session,
        )
    if normalized_action == "send_keys":
        return await tools.send_keys(keys=str(arguments.get("keys") or ""), browser_session=session)
    if normalized_action == "scroll_to_text":
        find_text = getattr(tools, "find_text")
        return await find_text(text=str(arguments.get("text") or ""), browser_session=session)
    if normalized_action == "evaluate_js":
        return await tools.evaluate(code=str(arguments.get("code") or ""), browser_session=session)
    raise ValueError(f"unsupported browser-use live tool action: {normalized_action}")


def _browser_use_live_tool_summary(normalized_action: str, result_output: dict[str, Any]) -> str:
    observed = result_output.get("extracted_content") or result_output.get("long_term_memory")
    if observed:
        return f"Browser-use live {normalized_action} completed: {str(observed)[:180]}"
    return f"Browser-use live {normalized_action} completed."


def _safe_wait_seconds(value: Any) -> int:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        seconds = 3
    return min(max(seconds, 0), 30)


def _safe_scroll_pages(value: Any) -> float:
    try:
        pages = float(value)
    except (TypeError, ValueError):
        pages = 1.0
    return min(max(pages, 0.1), 10.0)


def _screenshot_format(value: Any) -> tuple[str, str]:
    screenshot_format = str(value or "png").strip().lower()
    if screenshot_format not in {"png", "jpeg", "webp"}:
        screenshot_format = "png"
    if screenshot_format == "jpeg":
        return screenshot_format, ".jpg"
    return screenshot_format, f".{screenshot_format}"


def _screenshot_quality(value: Any, screenshot_format: str) -> int | None:
    if screenshot_format == "png":
        return None
    try:
        quality = int(value)
    except (TypeError, ValueError):
        quality = 80
    return min(max(quality, 1), 100)


async def _browser_use_live_pdf_bytes(session: Any, arguments: dict[str, Any]) -> bytes:
    import base64

    paper_width, paper_height = _pdf_paper_size(arguments.get("paper_format"))
    cdp_session = await session.get_or_create_cdp_session(focus=True)
    pdf_params: dict[str, Any] = {
        "printBackground": _truthy(arguments.get("print_background"), default=True),
        "landscape": _truthy(arguments.get("landscape"), default=False),
        "scale": _safe_pdf_scale(arguments.get("scale", 1.0)),
        "paperWidth": paper_width,
        "paperHeight": paper_height,
        "preferCSSPageSize": True,
    }
    if _truthy(arguments.get("display_header_footer"), default=False):
        pdf_params["displayHeaderFooter"] = True
    result = await asyncio.wait_for(
        cdp_session.cdp_client.send.Page.printToPDF(
            params=pdf_params,
            session_id=cdp_session.session_id,
        ),
        timeout=30.0,
    )
    pdf_data = result.get("data")
    if not pdf_data:
        raise ValueError("CDP Page.printToPDF returned no data")
    return base64.b64decode(pdf_data)


def _resolve_live_upload_path(workspace_root: Path, value: Any) -> Path:
    raw_path = str(value or "").strip()
    if not raw_path:
        raise ValueError("upload_file requires a non-empty path")
    raw = Path(raw_path).expanduser()
    candidate = raw if raw.is_absolute() else workspace_root / raw
    resolved = candidate.resolve()
    workspace = workspace_root.resolve()
    if not _path_within(resolved, workspace):
        raise ValueError(f"upload_file path must stay inside workspace: {raw_path}")
    if not resolved.exists():
        raise ValueError(f"upload_file path does not exist: {raw_path}")
    if not resolved.is_file():
        raise ValueError(f"upload_file path is not a file: {raw_path}")
    return resolved


def _browser_use_download_paths(session: Any, downloads_dir: Path) -> list[Path]:
    root = downloads_dir.resolve()
    candidates: list[Path] = []
    for downloaded in getattr(session, "downloaded_files", []) or []:
        candidates.append(Path(str(downloaded)))
    if root.exists():
        candidates.extend(path for path in root.rglob("*") if path.is_file())

    paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not _path_within(resolved, root):
            continue
        if not resolved.is_file() or _is_incomplete_download_path(resolved):
            continue
        seen.add(resolved)
        paths.append(resolved)
    return paths


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_incomplete_download_path(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".crdownload") or path.suffix.lower() in {".tmp", ".download", ".part"}


def _safe_download_settle_seconds(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 1.0
    return min(max(seconds, 0.0), 10.0)


def _safe_pdf_scale(value: Any) -> float:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        scale = 1.0
    return min(max(scale, 0.1), 2.0)


def _pdf_paper_size(value: Any) -> tuple[float, float]:
    sizes = {
        "letter": (8.5, 11.0),
        "legal": (8.5, 14.0),
        "a4": (8.27, 11.69),
        "a3": (11.69, 16.54),
        "tabloid": (11.0, 17.0),
    }
    return sizes.get(str(value or "letter").strip().lower(), sizes["letter"])


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _truthy(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "up"}


def _browser_use_interactive_elements(selector_map: dict[Any, Any]) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for index, node in sorted(selector_map.items(), key=lambda item: _safe_int(item[0])):
        attributes = getattr(node, "attributes", {}) or {}
        elements.append(
            {
                "index": _safe_int(index),
                "tag": getattr(node, "tag_name", ""),
                "xpath": getattr(node, "xpath", ""),
                "text": _browser_use_node_text(node)[:200],
                "attributes": {str(key): str(value) for key, value in list(attributes.items())[:12]},
                "visible": getattr(node, "is_visible", None),
            }
        )
    return elements[:50]


def _browser_use_node_text(node: Any) -> str:
    direct_text = getattr(node, "text", None)
    if isinstance(direct_text, str) and direct_text.strip():
        return re.sub(r"\s+", " ", direct_text).strip()
    parts: list[str] = []
    for child in getattr(node, "children", []) or []:
        text = getattr(child, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
        else:
            nested = _browser_use_node_text(child)
            if nested:
                parts.append(nested)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _browser_use_action_result_output(result: Any) -> dict[str, Any]:
    return {
        "error": getattr(result, "error", None),
        "extracted_content": getattr(result, "extracted_content", None),
        "long_term_memory": getattr(result, "long_term_memory", None),
        "is_done": getattr(result, "is_done", None),
        "success": getattr(result, "success", None),
        "metadata": getattr(result, "metadata", None),
        "attachments": getattr(result, "attachments", None),
    }


def _browser_action_event(
    request: WorkerRequest,
    step_index: int,
    action: str,
    *,
    action_metadata: dict[str, Any],
    ok: bool,
    summary: str,
    output: dict[str, Any],
    artifacts: list[Any] | None = None,
    error: str | None = None,
) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "browser_action": {
                "worker_request_id": request.request_id,
                "step_index": step_index,
                "action": action,
                **action_metadata,
            },
            "browser_result": {
                "ok": ok,
                "summary": summary,
                "output": output,
                "artifacts": [to_jsonable(artifact) for artifact in artifacts or []],
                "error": error,
            },
        },
    )


def _browser_agent_event(
    request: WorkerRequest,
    *,
    step_index: int,
    task: str,
    ok: bool,
    summary: str,
    output: dict[str, Any],
    error: str | None = None,
) -> EventRecord:
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "browser_agent": {
                "worker_request_id": request.request_id,
                "step_index": step_index,
                "backend": "browser-use-agent",
                "task_preview": task[:300],
            },
            "browser_agent_result": {
                "ok": ok,
                "summary": summary,
                "output": output,
                "error": error,
            },
        },
    )


def _worker_result_event(request: WorkerRequest, result: WorkerResult) -> EventRecord:
    request_payload = to_jsonable(request)
    if isinstance(request_payload, dict):
        constraints = request_payload.get("constraints")
        if isinstance(constraints, dict):
            request_payload = {
                **request_payload,
                "constraints": PermissionSessionCustodyStore.redact_constraints(constraints),
            }
    return EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "worker_request": request_payload,
            "worker_result": to_jsonable(result),
        },
    )


def _event_browser_result_ok(event: EventRecord) -> bool:
    result = event.payload.get("browser_result")
    return isinstance(result, dict) and result.get("ok") is True


def _browser_action_events(events: list[EventRecord]) -> list[EventRecord]:
    return [event for event in events if isinstance(event.payload.get("browser_result"), dict)]


def _snapshot_metadata(snapshot: Any) -> dict[str, str]:
    if snapshot is None:
        return {
            "vendor": "browser-use",
            "vendor_root": "",
            "vendor_complete": "not-required",
            "browser_runtime_backend": "zyra-browser-productized",
            "browser_runtime_vendor_required": "false",
        }
    return {
        "vendor": snapshot.name,
        "vendor_root": str(snapshot.root),
        "vendor_complete": str(not snapshot.missing_paths()).lower(),
    }


def _action_registry_metadata(registry: BrowserActionRegistry) -> dict[str, str]:
    description = registry.describe()
    return {
        "action_registry_source": str(description["source"]),
        "action_registry_actions": ",".join(str(action["action"]) for action in description["actions"]),
        "source_registered_actions": str(len(description["source_registered_actions"])),
    }


def _action_metadata(descriptor: Any, normalized_action: str) -> dict[str, Any]:
    if descriptor is None:
        return {"normalized_action": normalized_action}
    return {
        "normalized_action": normalized_action,
        "source_action": descriptor.source_action,
        "source_model": descriptor.source_model,
    }


def _trace_markdown(
    request: WorkerRequest,
    snapshot: Any,
    events: list[EventRecord],
    ok: bool,
    browser_use_health: BrowserUseRuntimeHealth | None = None,
) -> str:
    lines = [
        "# BrowserWorker Trace",
        "",
        f"- request_id: `{request.request_id}`",
        f"- worker_name: `{request.worker_name}`",
        f"- ok: `{str(ok).lower()}`",
        f"- vendor: `{snapshot.name}`",
        f"- vendored_runtime_complete: `{str(not snapshot.missing_paths()).lower()}`",
        f"- browser_use_python_importable: `{browser_use_runtime_metadata(browser_use_health).get('browser_use_python_importable')}`",
        "",
        "## Browser Steps",
        "",
    ]
    for event in events:
        action = event.payload.get("browser_action", {})
        result = event.payload.get("browser_result", {})
        lines.append(
            "- "
            f"{action.get('step_index')}. {action.get('action')}"
            f" -> {action.get('source_action', action.get('normalized_action'))}: {result.get('summary')}"
        )
    lines.extend(["", "## Vendored Runtime Modules", ""])
    for module in snapshot.modules:
        lines.append(f"- {module.name}: {module.target_boundary}")
    lines.append("")
    return "\n".join(lines)


def _agent_trace_markdown(
    request: WorkerRequest,
    snapshot: Any,
    events: list[EventRecord],
    ok: bool,
    browser_use_health: BrowserUseRuntimeHealth | None = None,
) -> str:
    metadata = browser_use_runtime_metadata(browser_use_health)
    lines = [
        "# Browser-use Agent Trace",
        "",
        f"- request_id: `{request.request_id}`",
        f"- worker_name: `{request.worker_name}`",
        f"- ok: `{str(ok).lower()}`",
        f"- vendor: `{snapshot.name}`",
        f"- vendored_runtime_complete: `{str(not snapshot.missing_paths()).lower()}`",
        f"- browser_use_agent_class: `{metadata.get('browser_use_agent_class', '')}`",
        f"- browser_use_agent_history_class: `{metadata.get('browser_use_agent_history_class', '')}`",
        "",
        "## Agent Steps",
        "",
    ]
    for event in events:
        agent = event.payload.get("browser_agent", {})
        result = event.payload.get("browser_agent_result", {})
        lines.append(
            "- "
            f"{agent.get('step_index')}. browser-use-agent"
            f" -> ok={str(result.get('ok')).lower()}: {result.get('summary')}"
        )
    lines.extend(["", "## Vendored Runtime Modules", ""])
    for module in snapshot.modules:
        lines.append(f"- {module.name}: {module.target_boundary}")
    lines.append("")
    return "\n".join(lines)
