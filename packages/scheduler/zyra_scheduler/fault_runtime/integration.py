from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import EventRecord, TaskState

from .contracts import CorrelationRefs, FaultInjectionReceipt, FaultInjectionRequest, runtime_id, utc_now
from .browser_integration import (
    BrowserEventSource,
    BrowserWatchdogEventBridge,
    browser_integration_contract,
)
from .handoff_runtime import RecoveryConsumer, SameRunHandoffDispatcher, handoff_dispatch_contract
from .effect_runtime import SignalEffectCoordinator, signal_effect_contract
from .containment_runtime import (
    ActiveFaultContainmentRuntime,
    ContainmentHandler,
    containment_runtime_contract,
)
from .requirement_control import RequirementChangeFaultIsolation, requirement_control_contract
from .observation_port import RuntimeFaultObservationPort, runtime_observation_port_contract
from .source_session import (
    ProcessHandle,
    RuntimeSourceSessionManager,
    SourceKind,
    source_session_contract,
)


class IntegrationAction(StrEnum):
    ATTACH = "attach"
    HEARTBEAT = "heartbeat"
    BEGIN = "begin"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    RECONNECT_FAILED = "reconnect_failed"
    RECONNECT_SUCCEEDED = "reconnect_succeeded"
    STOP = "stop"
    DISABLE = "disable"
    RESTART = "restart"
    TICK = "tick"


@dataclass(frozen=True, slots=True)
class IntegrationResponse:
    ok: bool
    operation: str
    task_id: str
    body: Mapping[str, Any]
    response_id: str = field(default_factory=lambda: runtime_id("fault-integration"))
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.watchdog-fault-integration-response/v1",
            "response_id": self.response_id,
            "ok": self.ok,
            "operation": self.operation,
            "task_id": self.task_id,
            "body": dict(self.body),
            "created_at": self.created_at,
        }


class WatchdogFaultIntegrationRuntime:
    """Default-path integration around the 07B foundation components.

    This runtime is intentionally an orchestrator, not a new state owner. It
    calls the foundation's active sources, deterministic classifier, event
    writer, injection runtime and durable recovery bridge. API, commands and
    workers use the same methods, which prevents a test-only injection path or
    a second observer stack from drifting into production.
    """

    def __init__(
        self,
        application: Any,
        *,
        event_sink: Callable[[EventRecord], None],
        task_state_resolver: Callable[[str], TaskState | None] | None = None,
    ) -> None:
        self.application = application
        self.store = application.store
        self.writer = application.writer
        self.watchdog = application.watchdog
        self.injections = application.injections
        self.recovery = application.recovery
        self.task_state_resolver = task_state_resolver
        self.sources = RuntimeSourceSessionManager(self.watchdog, self.store)
        self.browser_events = BrowserWatchdogEventBridge(self.sources)
        self.handoffs = SameRunHandoffDispatcher(
            self.store,
            self.recovery,
            self.writer,
            task_state_resolver=task_state_resolver,
        )
        self.effects = SignalEffectCoordinator(
            self.store,
            self.writer,
            self.recovery,
            task_state_resolver=task_state_resolver,
        )
        self.containment = ActiveFaultContainmentRuntime(self.store)
        self.requirements = RequirementChangeFaultIsolation(
            self.store,
            self.writer,
            event_sink=event_sink,
        )
        self.observations = RuntimeFaultObservationPort(self, self.store)
        self._guard = threading.RLock()
        self._operation_count = 0
        self._failure_count = 0
        self._last_responses: list[IntegrationResponse] = []
        self.watchdog.add_signal_listener(self._on_fault_signal)

    def _on_fault_signal(self, signal: Any) -> None:
        self.containment.contain(signal)
        task_state = self.task_state_resolver(signal.refs.task_id) if self.task_state_resolver else None
        self.effects.reconcile_signal(signal.signal_id, task_state=task_state)

    def register_recovery_consumer(self, consumer: RecoveryConsumer) -> Mapping[str, Any]:
        return self.handoffs.register(consumer)

    def close(self) -> Mapping[str, Any]:
        self.watchdog.remove_signal_listener(self._on_fault_signal)
        detached = self.browser_events.close()
        return {
            "schema": "zyra.watchdog-fault-integration-close/v1",
            "detached_browser_bridges": list(detached),
            "signal_listener_removed": True,
        }

    def register_containment_target(
        self,
        source_kind: SourceKind | str,
        source_id: str,
        *,
        state: TaskState,
        generation: int,
        handlers: Mapping[str, ContainmentHandler],
        metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return self.containment.register(
            source_kind,
            source_id,
            run_id=state.run_id,
            task_id=state.task_id,
            generation=generation,
            handlers=handlers,
            metadata=metadata,
        )

    def attach_browser_event_source(
        self,
        state: TaskState,
        payload: Mapping[str, Any],
        source: BrowserEventSource,
        *,
        process_handle: ProcessHandle | None = None,
    ) -> Mapping[str, Any]:
        refs = self.refs_from_payload(state, payload, observation_prefix="browser-event-source")
        return self.browser_events.attach(
            refs,
            source,
            generation=self._generation(payload),
            process_handle=process_handle,
            cdp_connected=(bool(payload["cdp_connected"]) if payload.get("cdp_connected") is not None else None),
            metadata=self._metadata(payload),
        ).to_dict()

    def inject(
        self,
        request: FaultInjectionRequest,
        *,
        task_state: TaskState,
        dispatch_to: str = "",
    ) -> FaultInjectionReceipt:
        receipt = self.injections.inject(request, task_state=task_state)
        if receipt.signal is not None:
            self.containment.contain(receipt.signal)
            self.effects.reconcile_signal(receipt.signal.signal_id, task_state=task_state)
        if dispatch_to and receipt.handoff is not None:
            self.handoffs.dispatch_one(dispatch_to, receipt.handoff.handoff_id)
        return receipt

    def apply_requirement_change(
        self,
        state: TaskState,
        event: EventRecord,
    ) -> Mapping[str, Any]:
        return self.requirements.apply(state, event).to_dict()

    def bind_source(
        self,
        state: TaskState,
        payload: Mapping[str, Any],
        *,
        process_handle: ProcessHandle | None = None,
    ) -> IntegrationResponse:
        kind = self._source_kind(payload)
        generation = self._generation(payload)
        refs = self.refs_from_payload(state, payload, observation_prefix=f"{kind.value}-bind")
        metadata = self._metadata(payload)
        if kind is SourceKind.WORKER:
            receipt = self.sources.attach_worker(
                refs,
                generation=generation,
                interval_ms=self._positive_int(payload, "interval_ms", default=1_000),
                grace_intervals=self._positive_int(payload, "grace_intervals", default=3),
                process_handle=process_handle,
                metadata=metadata,
            )
        elif kind is SourceKind.BROWSER:
            receipt = self.sources.attach_browser(
                refs,
                generation=generation,
                process_id=(int(payload["process_id"]) if payload.get("process_id") is not None else None),
                process_handle=process_handle,
                cdp_connected=(bool(payload["cdp_connected"]) if payload.get("cdp_connected") is not None else None),
                metadata=metadata,
            )
        elif kind is SourceKind.PROVIDER:
            receipt = self.sources.attach_provider(
                refs,
                generation=generation,
                metadata=metadata,
            )
        elif kind is SourceKind.MCP:
            receipt = self.sources.attach_mcp(
                refs,
                generation=generation,
                max_reconnect_attempts=self._positive_int(payload, "max_reconnect_attempts", default=4),
                base_backoff_ms=self._positive_int(payload, "base_backoff_ms", default=250),
                process_handle=process_handle,
                metadata=metadata,
            )
        elif kind is SourceKind.TOOL:
            lease = self.sources.arm_tool(
                refs,
                generation=generation,
                deadline_ms=self._positive_int(payload, "deadline_ms"),
                metadata=metadata,
            )
            return self._response(
                "bind:tool",
                state.task_id,
                {
                    "source_kind": kind.value,
                    "tool_call_id": refs.tool_call_id,
                    "generation": generation,
                    "lease_token": lease.token,
                    "deadline_ms": lease.record.deadline_ms,
                    "refs": refs.to_dict(),
                },
            )
        else:
            raise ValueError(f"source kind is not attachable: {kind.value}")
        return self._response(f"bind:{kind.value}", state.task_id, receipt.to_dict())

    def observe_source(
        self,
        state: TaskState,
        payload: Mapping[str, Any],
    ) -> IntegrationResponse:
        kind = self._source_kind(payload)
        action = IntegrationAction(str(payload.get("action") or "").strip().lower())
        generation = self._generation(payload)
        if action is IntegrationAction.TICK:
            return self.tick(task_id=state.task_id, at_ms=self._optional_int(payload, "at_ms"))
        if kind is SourceKind.WORKER:
            body = self._observe_worker(payload, action, generation)
        elif kind is SourceKind.BROWSER:
            body = self._observe_browser(payload, action, generation)
        elif kind is SourceKind.PROVIDER:
            body = self._observe_provider(payload, action, generation)
        elif kind is SourceKind.MCP:
            body = self._observe_mcp(payload, action, generation)
        elif kind is SourceKind.TOOL:
            body = self._observe_tool(payload, action, generation)
        else:
            raise ValueError(f"unsupported source observation kind: {kind.value}")
        return self._response(f"observe:{kind.value}:{action.value}", state.task_id, body)

    def observe_permission(
        self,
        state: TaskState,
        payload: Mapping[str, Any],
    ) -> IntegrationResponse:
        refs = self.refs_from_payload(state, payload, observation_prefix="permission")
        receipt = payload.get("receipt")
        if not isinstance(receipt, Mapping):
            receipt = payload
        observation = self.watchdog.permissions.observe_receipt(refs, receipt)
        return self._response(
            "observe:permission",
            state.task_id,
            {
                "observation": None if observation is None else observation.to_dict(),
                "fault_signal_expected": observation is not None,
                "refs": refs.to_dict(),
            },
        )

    def observe_schema(
        self,
        state: TaskState,
        payload: Mapping[str, Any],
    ) -> IntegrationResponse:
        refs = self.refs_from_payload(state, payload, observation_prefix="schema")
        raw_violations = payload.get("violations") or ()
        if not isinstance(raw_violations, Sequence) or isinstance(raw_violations, str | bytes):
            raise ValueError("schema violations must be an array")
        violations = tuple(dict(item) for item in raw_violations if isinstance(item, Mapping))
        observation = self.watchdog.schemas.observe_validation(
            refs,
            schema_id=str(payload.get("schema_id") or "").strip(),
            valid=bool(payload.get("valid")),
            violations=violations,
        )
        return self._response(
            "observe:schema",
            state.task_id,
            {"observation": None if observation is None else observation.to_dict(), "refs": refs.to_dict()},
        )

    def observe_workspace(
        self,
        state: TaskState,
        payload: Mapping[str, Any],
    ) -> IntegrationResponse:
        refs = self.refs_from_payload(state, payload, observation_prefix="workspace")
        observation = self.watchdog.workspaces.attest(
            refs,
            relative_path=str(payload.get("relative_path") or ""),
            expected_digest=str(payload.get("expected_digest") or ""),
        )
        return self._response(
            "observe:workspace",
            state.task_id,
            {"observation": None if observation is None else observation.to_dict(), "refs": refs.to_dict()},
        )

    def observe_subagent(
        self,
        state: TaskState,
        payload: Mapping[str, Any],
    ) -> IntegrationResponse:
        refs = self.refs_from_payload(state, payload, observation_prefix="subagent")
        receipt = payload.get("receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("subagent observation requires a structured receipt")
        observation = self.watchdog.subagents.observe_terminal_receipt(refs, receipt)
        return self._response(
            "observe:subagent",
            state.task_id,
            {"observation": None if observation is None else observation.to_dict(), "refs": refs.to_dict()},
        )

    def ingest_typescript_event(
        self,
        state: TaskState,
        event: Mapping[str, Any],
    ) -> IntegrationResponse:
        event_run_id = str(event.get("run_id") or "")
        event_task_id = str(event.get("task_id") or "")
        if event_run_id != state.run_id or event_task_id != state.task_id:
            raise ValueError("TypeScript watchdog event is outside task scope")
        result = self.watchdog.ingest_runtime_event(event)
        return self._response(
            "ingest:typescript-watchdog",
            state.task_id,
            {
                **dict(result),
                "source_language": "typescript",
                "source_repo": "oh-my-pi",
                "durable_owner": "python.FaultStateStore",
            },
        )

    def dispatch_handoffs(
        self,
        consumer_id: str,
        *,
        task_id: str,
        limit: int = 20,
        now_ms: int | None = None,
    ) -> IntegrationResponse:
        receipts = self.handoffs.dispatch(
            consumer_id,
            task_id=task_id,
            limit=limit,
            now_ms=now_ms,
        )
        return self._response(
            "dispatch:recovery-handoffs",
            task_id,
            {
                "consumer_id": consumer_id,
                "receipts": [item.to_dict() for item in receipts],
                "acknowledged_count": sum(1 for item in receipts if item.acknowledged),
                "recovery_plan_selected_by_07b": False,
            },
        )

    def tick(self, *, task_id: str = "", at_ms: int | None = None) -> IntegrationResponse:
        result = self.sources.tick(at_ms=at_ms)
        return self._response("tick", task_id, result)

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        with self._guard:
            responses = [
                item.to_dict()
                for item in self._last_responses[-100:]
                if not task_id or item.task_id == task_id
            ]
            operation_count = self._operation_count
            failure_count = self._failure_count
        return {
            "schema": "zyra.watchdog-fault-integration-runtime/v1",
            "task_id": task_id,
            "source_sessions": self.sources.snapshot(task_id=task_id),
            "browser_event_bridge": self.browser_events.snapshot(task_id=task_id),
            "handoff_dispatch": self.handoffs.snapshot(task_id=task_id),
            "signal_effects": self.effects.snapshot(task_id=task_id),
            "active_containment": self.containment.snapshot(task_id=task_id),
            "requirement_control": self.requirements.snapshot(task_id=task_id),
            "runtime_observation_port": self.observations.snapshot(task_id=task_id),
            "operation_count": operation_count,
            "failure_count": failure_count,
            "recent_responses": responses,
            "default_path_components": [
                "RuntimeWatchdog",
                "WatchdogSignalClassifier",
                "FaultInjectionRuntime",
                "SameRunFaultInjector",
                "FaultSignalEventWriter",
                "WatchdogRecoveryBridge",
            ],
            "state_custody": {
                "fault_state": "FaultStateStore",
                "event_history": "canonical task event store",
                "memory": "MemoryFabric",
                "scheduler_health": "BackendRegistryStore",
                "recovery_plan": "M1-S07C",
                "requirement_change": "ConstraintKeeper/TopologyRouter",
            },
        }

    @staticmethod
    def refs_from_payload(
        state: TaskState,
        payload: Mapping[str, Any],
        *,
        observation_prefix: str,
    ) -> CorrelationRefs:
        refs_value = payload.get("refs")
        refs = dict(refs_value) if isinstance(refs_value, Mapping) else {}
        run_id = str(refs.get("run_id") or payload.get("run_id") or state.run_id)
        task_id = str(refs.get("task_id") or payload.get("task_id") or state.task_id)
        if run_id != state.run_id or task_id != state.task_id:
            raise ValueError("fault integration refs are outside task scope")
        return CorrelationRefs(
            run_id=run_id,
            task_id=task_id,
            observation_id=str(
                refs.get("observation_id")
                or payload.get("observation_id")
                or runtime_id(f"{observation_prefix}-observation")
            ),
            session_id=str(refs.get("session_id") or payload.get("session_id") or ""),
            node_id=str(refs.get("node_id") or payload.get("node_id") or ""),
            attempt_id=str(refs.get("attempt_id") or payload.get("attempt_id") or ""),
            tool_call_id=str(refs.get("tool_call_id") or payload.get("tool_call_id") or ""),
            tool_name=str(refs.get("tool_name") or payload.get("tool_name") or ""),
            worker_id=str(refs.get("worker_id") or payload.get("worker_id") or ""),
            backend_id=str(refs.get("backend_id") or payload.get("backend_id") or ""),
            provider_id=str(refs.get("provider_id") or payload.get("provider_id") or ""),
            workspace_id=str(refs.get("workspace_id") or payload.get("workspace_id") or ""),
            browser_session_id=str(
                refs.get("browser_session_id") or payload.get("browser_session_id") or ""
            ),
            mcp_server_id=str(refs.get("mcp_server_id") or payload.get("mcp_server_id") or ""),
            subagent_task_id=str(
                refs.get("subagent_task_id") or payload.get("subagent_task_id") or ""
            ),
            source_state_revision=int(
                refs.get("source_state_revision")
                or payload.get("source_state_revision")
                or 0
            ),
        )

    def _observe_worker(
        self,
        payload: Mapping[str, Any],
        action: IntegrationAction,
        generation: int,
    ) -> Mapping[str, Any]:
        worker_id = self._identity(payload, "worker_id")
        if action is IntegrationAction.HEARTBEAT:
            return self.sources.worker_heartbeat(
                worker_id,
                generation=generation,
                sequence=self._positive_int(payload, "sequence"),
                at_ms=self._optional_int(payload, "at_ms"),
            ).to_dict()
        if action in {IntegrationAction.STOP, IntegrationAction.DISABLE}:
            return self.sources.stop(
                SourceKind.WORKER,
                worker_id,
                generation=generation,
                reason=str(payload.get("reason") or "worker source stopped"),
                disable=action is IntegrationAction.DISABLE,
            ).to_dict()
        raise ValueError(f"unsupported worker observation action: {action.value}")

    def _observe_browser(
        self,
        payload: Mapping[str, Any],
        action: IntegrationAction,
        generation: int,
    ) -> Mapping[str, Any]:
        browser_session_id = self._identity(payload, "browser_session_id")
        if action is IntegrationAction.HEARTBEAT:
            receipt = self.sources.browser_heartbeat(
                browser_session_id,
                generation=generation,
                at_ms=self._optional_int(payload, "at_ms"),
            )
        elif action is IntegrationAction.CONNECTED:
            receipt = self.sources.browser_cdp_connected(
                browser_session_id,
                generation=generation,
                at_ms=self._optional_int(payload, "at_ms"),
            )
        elif action is IntegrationAction.DISCONNECTED:
            receipt = self.sources.browser_cdp_disconnected(
                browser_session_id,
                generation=generation,
                reason_code=self._identity(payload, "reason_code"),
                at_ms=self._optional_int(payload, "at_ms"),
            )
        elif action in {IntegrationAction.STOP, IntegrationAction.DISABLE}:
            receipt = self.sources.stop(
                SourceKind.BROWSER,
                browser_session_id,
                generation=generation,
                reason=str(payload.get("reason") or "browser source stopped"),
                disable=action is IntegrationAction.DISABLE,
            )
        else:
            raise ValueError(f"unsupported browser observation action: {action.value}")
        return receipt.to_dict()

    def _observe_provider(
        self,
        payload: Mapping[str, Any],
        action: IntegrationAction,
        generation: int,
    ) -> Mapping[str, Any]:
        provider_id = self._identity(payload, "provider_id")
        if action is IntegrationAction.BEGIN:
            permit = self.sources.begin_provider_attempt(
                provider_id,
                generation=generation,
                attempt_id=self._identity(payload, "attempt_id"),
                at_ms=self._optional_int(payload, "at_ms"),
            )
            return {
                "provider_id": permit.provider_id,
                "generation": permit.generation,
                "attempt_number": permit.attempt_number,
                "attempt_id": permit.attempt_id,
                "half_open_probe": permit.half_open_probe,
                "issued_at_ms": permit.issued_at_ms,
            }
        attempt_id = self._identity(payload, "attempt_id")
        if action is IntegrationAction.SUCCEEDED:
            return self.sources.provider_succeeded(
                attempt_id,
                at_ms=self._optional_int(payload, "at_ms"),
            ).to_dict()
        if action is IntegrationAction.FAILED:
            outcome = self.sources.provider_failed(
                attempt_id,
                status_code=self._optional_int(payload, "status_code"),
                error_type=str(payload.get("error_type") or ""),
                error_code=str(payload.get("error_code") or "provider_error"),
                retryable=(bool(payload["retryable"]) if payload.get("retryable") is not None else None),
                details=self._metadata(payload),
                at_ms=self._optional_int(payload, "at_ms"),
            )
            return {
                "observation": None if outcome.observation is None else outcome.observation.to_dict(),
                "retryable": outcome.retryable,
                "exhausted": outcome.exhausted,
                "circuit_open": outcome.circuit_open,
                "next_attempt_at_ms": outcome.next_attempt_at_ms,
                "attempt_number": outcome.attempt_number,
            }
        raise ValueError(f"unsupported provider observation action: {action.value}")

    def _observe_mcp(
        self,
        payload: Mapping[str, Any],
        action: IntegrationAction,
        generation: int,
    ) -> Mapping[str, Any]:
        server_id = self._identity(payload, "mcp_server_id")
        if action is IntegrationAction.DISCONNECTED:
            receipt = self.sources.mcp_disconnected(
                server_id,
                generation=generation,
                reason_code=self._identity(payload, "reason_code"),
                at_ms=self._optional_int(payload, "at_ms"),
            )
        elif action is IntegrationAction.RECONNECT_FAILED:
            receipt = self.sources.mcp_reconnect_failed(
                server_id,
                generation=generation,
                at_ms=self._optional_int(payload, "at_ms"),
            )
        elif action is IntegrationAction.RECONNECT_SUCCEEDED:
            receipt = self.sources.mcp_reconnect_succeeded(server_id, generation=generation)
        elif action in {IntegrationAction.STOP, IntegrationAction.DISABLE}:
            receipt = self.sources.stop(
                SourceKind.MCP,
                server_id,
                generation=generation,
                reason=str(payload.get("reason") or "MCP source stopped"),
                disable=action is IntegrationAction.DISABLE,
            )
        else:
            raise ValueError(f"unsupported MCP observation action: {action.value}")
        return receipt.to_dict()

    def _observe_tool(
        self,
        payload: Mapping[str, Any],
        action: IntegrationAction,
        generation: int,
    ) -> Mapping[str, Any]:
        tool_call_id = self._identity(payload, "tool_call_id")
        if action not in {IntegrationAction.SUCCEEDED, IntegrationAction.FAILED, IntegrationAction.STOP}:
            raise ValueError(f"unsupported tool observation action: {action.value}")
        receipt = self.sources.settle_tool(
            tool_call_id,
            generation=generation,
            token=self._identity(payload, "lease_token"),
            result_id=self._identity(payload, "result_id"),
            ok=action is IntegrationAction.SUCCEEDED,
            cancelled=action is IntegrationAction.STOP,
        )
        return {
            "tool_call_id": receipt.tool_call_id,
            "result_id": receipt.result_id,
            "generation": receipt.generation,
            "disposition": receipt.disposition.value,
            "accepted": receipt.accepted,
            "timeout_observation_id": receipt.timeout_observation_id,
            "detail": receipt.detail,
        }

    def _response(self, operation: str, task_id: str, body: Mapping[str, Any]) -> IntegrationResponse:
        task_state = self.task_state_resolver(task_id) if task_id and self.task_state_resolver else None
        containment_receipts = self.containment.reconcile_new_for_task(task_id) if task_id else ()
        effect_receipts = self.effects.reconcile_new_for_task(task_id, task_state=task_state) if task_id else ()
        response = IntegrationResponse(
            ok=True,
            operation=operation,
            task_id=task_id,
            body={
                **dict(body),
                "containment_receipts": [item.to_dict() for item in containment_receipts],
                "signal_effect_receipts": [item.to_dict() for item in effect_receipts],
            },
        )
        with self._guard:
            self._operation_count += 1
            self._last_responses.append(response)
            del self._last_responses[:-200]
        return response

    @staticmethod
    def _source_kind(payload: Mapping[str, Any]) -> SourceKind:
        value = str(payload.get("source_kind") or payload.get("kind") or "").strip().lower()
        try:
            return SourceKind(value)
        except ValueError as error:
            raise ValueError(f"unsupported watchdog source kind: {value}") from error

    @staticmethod
    def _generation(payload: Mapping[str, Any]) -> int:
        generation = int(payload.get("generation") or 0)
        if generation < 0:
            raise ValueError("source generation must be non-negative")
        return generation

    @staticmethod
    def _identity(payload: Mapping[str, Any], name: str) -> str:
        refs = payload.get("refs")
        value = (refs.get(name) if isinstance(refs, Mapping) else None) or payload.get(name)
        selected = str(value or "").strip()
        if not selected:
            raise ValueError(f"fault integration requires structured {name}")
        return selected

    @staticmethod
    def _positive_int(payload: Mapping[str, Any], name: str, *, default: int | None = None) -> int:
        raw = payload.get(name)
        if raw is None and default is not None:
            raw = default
        if raw is None:
            raise ValueError(f"fault integration requires {name}")
        selected = int(raw)
        if selected < 1:
            raise ValueError(f"{name} must be positive")
        return selected

    @staticmethod
    def _optional_int(payload: Mapping[str, Any], name: str) -> int | None:
        if payload.get(name) is None:
            return None
        selected = int(payload[name])
        if selected < 0:
            raise ValueError(f"{name} must be non-negative")
        return selected

    @staticmethod
    def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
        value = payload.get("metadata")
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError("fault integration metadata must be an object")
        return dict(value)


def integration_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.watchdog-fault-integration-contract/v1",
        "source_sessions": source_session_contract(),
        "browser_event_bridge": browser_integration_contract(),
        "handoff_dispatch": handoff_dispatch_contract(),
        "signal_effects": signal_effect_contract(),
        "active_containment": containment_runtime_contract(),
        "requirement_control": requirement_control_contract(),
        "runtime_observation_port": runtime_observation_port_contract(),
        "main_path_components": [
            "RuntimeWatchdog",
            "WatchdogSignalClassifier",
            "FaultInjectionRuntime",
            "SameRunFaultInjector",
            "FaultSignalEventWriter",
            "WatchdogRecoveryBridge",
        ],
        "observed_categories": [
            "tool_deadline",
            "worker_heartbeat",
            "browser_process_or_cdp",
            "provider_transport",
            "mcp_transport",
            "permission_receipt",
            "schema_validation",
            "workspace_attestation",
        ],
        "source_languages": {"browser-use": "python", "oh-my-pi": "typescript"},
        "canonical_classifier_language": "python",
        "recovery_plan_owner": "M1-S07C",
        "requirement_changed_is_fault": False,
    }


__all__ = [
    "IntegrationAction",
    "IntegrationResponse",
    "WatchdogFaultIntegrationRuntime",
    "integration_contract",
]
