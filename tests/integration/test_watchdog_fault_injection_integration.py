from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from zyra_core import EventRecord, EventType, create_task_state
from zyra_memory import MemoryFabric, SQLiteStore
from zyra_scheduler.fault_runtime import (
    ContinuationMode,
    CorrelationRefs,
    FaultInjectionRequest,
    FaultKind,
    FaultRuntimeApplication,
    FaultRuntimeApiService,
    InjectionKind,
    RuntimeObservationControlRequired,
    RuntimeObservationRejected,
    SignalOrigin,
    SourceKind,
)
from zyra_scheduler.worker_pool import BackendRegistryHealthAdapter
from zyra_commands.runtime import WatchdogControlCommandRuntime


def _refs(state: Any, **values: Any) -> CorrelationRefs:
    return CorrelationRefs(
        run_id=state.run_id,
        task_id=state.task_id,
        observation_id=str(values.pop("observation_id", "integration-observation")),
        **values,
    )


@dataclass
class _IntegratedRuntime:
    state: Any
    canonical: SQLiteStore
    events: list[EventRecord]
    application: FaultRuntimeApplication
    backend_path: Path

    def close(self) -> None:
        self.application.close()


def _integrated_runtime(tmp_path: Path) -> _IntegratedRuntime:
    state = create_task_state("Exercise active watchdog integration and same-run recovery delivery.")
    canonical = SQLiteStore(tmp_path / "canonical.sqlite3")
    canonical.initialize()
    canonical.save_checkpoint(state)
    events: list[EventRecord] = []

    def event_sink(event: EventRecord) -> None:
        events.append(event)
        canonical.append_event(event)

    backend_path = tmp_path / "backend.sqlite3"
    application = FaultRuntimeApplication(
        tmp_path / "fault-runtime.sqlite3",
        event_sink=event_sink,
        memory=MemoryFabric(canonical),
        scheduler_health=BackendRegistryHealthAdapter(backend_path),
        task_state_resolver=lambda task_id: state if task_id == state.task_id else None,
        workspace_roots={"workspace-integration": tmp_path / "workspace"},
    )
    return _IntegratedRuntime(state, canonical, events, application, backend_path)


def _event(
    runtime: _IntegratedRuntime,
    *,
    event_id: str,
    event_type: str,
    producer: str,
    sequence: int,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "producer": producer,
        "run_id": runtime.state.run_id,
        "task_id": runtime.state.task_id,
        "sequence": sequence,
        "payload": dict(payload),
    }


@dataclass
class _RecoveryConsumer:
    consumer_id: str = "m1-07c-test-consumer"
    deliveries: list[dict[str, Any]] = field(default_factory=list)

    def accept_fault_handoff(
        self,
        handoff: Any,
        *,
        task_state: Any,
        causal_context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert task_state.run_id == handoff.run_id
        assert causal_context["event_written"] is True
        assert causal_context["event_id"]
        receipt = {
            "handoff_id": handoff.handoff_id,
            "signal_id": handoff.signal_id,
            "recovery_plan_id": f"plan-for-{handoff.signal_id}",
            "selected_by": "M1-S07C",
        }
        self.deliveries.append(receipt)
        return receipt


class _BrowserEventSource:
    def __init__(self) -> None:
        self.callbacks: dict[str, dict[str, Callable[[Mapping[str, Any]], None]]] = {}
        self.counter = 0
        self.unsubscribe_count = 0

    def subscribe(self, event_name: str, callback: Callable[[Mapping[str, Any]], None]) -> str:
        self.counter += 1
        token = f"browser-subscription-{self.counter}"
        self.callbacks.setdefault(event_name, {})[token] = callback
        return token

    def unsubscribe(self, event_name: str, token: Any) -> None:
        self.callbacks.get(event_name, {}).pop(str(token), None)
        self.unsubscribe_count += 1

    def emit(self, event_name: str, value: Mapping[str, Any]) -> None:
        for callback in tuple(self.callbacks.get(event_name, {}).values()):
            callback(value)


@dataclass
class _ProcessHandle:
    process: subprocess.Popen[str]

    @property
    def pid(self) -> int:
        return self.process.pid

    def poll(self) -> int | None:
        return self.process.poll()


def test_worker_heartbeat_changes_event_memory_scheduler_containment_and_07c_delivery(
    tmp_path: Path,
) -> None:
    runtime = _integrated_runtime(tmp_path)
    state = runtime.state
    containment: list[dict[str, Any]] = []
    consumer = _RecoveryConsumer()
    try:
        attached = runtime.application.integration.observations.ingest(
            state,
            _event(
                runtime,
                event_id="worker-attached-1",
                event_type="worker.attached",
                producer="worker-runtime",
                sequence=1,
                payload={
                    "worker_id": "worker-live-1",
                    "backend_id": "local-code-worker",
                    "generation": 3,
                    "interval_ms": 10,
                    "grace_intervals": 2,
                    "session_id": "session-live-1",
                    "attempt_id": "attempt-live-1",
                },
            ),
        )
        assert attached.status.value == "accepted"
        runtime.application.integration.register_containment_target(
            SourceKind.WORKER,
            "worker-live-1",
            state=state,
            generation=3,
            handlers={
                "revoke_worker_lease": lambda signal, context: containment.append(
                    {"signal": signal.signal_id, **dict(context)}
                )
                or {
                    "applied": True,
                    "signal_id": signal.signal_id,
                    "action": "revoke_worker_lease",
                    "lease_revoked": True,
                    "recovery_plan_selected": False,
                }
            },
        )
        binding = runtime.application.integration.sources.snapshot(task_id=state.task_id)["bindings"][
            "worker:worker-live-1"
        ]
        tick = runtime.application.integration.observations.ingest(
            state,
            _event(
                runtime,
                event_id="watchdog-tick-worker-1",
                event_type="watchdog.tick",
                producer="watchdog-clock",
                sequence=1,
                payload={"at_ms": binding["last_heartbeat_ms"] + 100},
            ),
        )
        new_ids = tick.result["body"]["new_signal_ids"]
        assert len(new_ids) == 1
        signal = runtime.application.store.signal(new_ids[0])
        assert signal is not None
        assert signal.kind is FaultKind.WORKER_UNAVAILABLE
        assert signal.origin is SignalOrigin.OBSERVER
        assert signal.refs.worker_id == "worker-live-1"
        assert signal.refs.backend_id == "local-code-worker"
        assert len(containment) == 1
        assert containment[0]["action"] == "revoke_worker_lease"

        projection = runtime.application.store.projection_receipt(signal.signal_id)
        assert projection is not None and projection.ok
        assert projection.memory_record_ids
        assert projection.scheduler_changed is True
        assert projection.scheduler_receipt["route_id"] == "local-code-worker"
        assert runtime.canonical.task_memory_records(state.task_id)
        assert runtime.events[-1].payload["signal"]["signal_id"] == signal.signal_id

        runtime.application.integration.register_recovery_consumer(consumer)
        dispatched = runtime.application.integration.dispatch_handoffs(
            consumer.consumer_id,
            task_id=state.task_id,
            now_ms=100,
        )
        assert dispatched.body["acknowledged_count"] == 1, dispatched.body
        delivery = runtime.application.store.handoff_deliveries(task_id=state.task_id)[0]
        assert delivery["status"] == "acknowledged"
        assert delivery["receipt"]["event_id"] == projection.event_id
        assert consumer.deliveries[0]["signal_id"] == signal.signal_id
    finally:
        runtime.close()


def test_tool_deadline_aborts_registered_surface_and_fences_late_result(tmp_path: Path) -> None:
    runtime = _integrated_runtime(tmp_path)
    state = runtime.state
    aborted: list[str] = []
    try:
        started_event = _event(
            runtime,
            event_id="tool-started-1",
            event_type="tool.started",
            producer="code-worker",
            sequence=1,
            payload={
                "tool_call_id": "tool-live-1",
                "tool_name": "shell",
                "generation": 4,
                "deadline_ms": 5,
                "session_id": "session-tool-1",
                "attempt_id": "attempt-tool-1",
            },
        )
        started = runtime.application.integration.observations.ingest(state, started_event)
        duplicate = runtime.application.integration.observations.ingest(state, started_event)
        assert duplicate.status.value == "duplicate"
        assert duplicate.changed is False
        lease_token = str(started.result["body"]["lease_token"])
        runtime.application.integration.register_containment_target(
            SourceKind.TOOL,
            "tool-live-1",
            state=state,
            generation=4,
            handlers={
                "abort_tool_call": lambda signal, context: aborted.append(signal.signal_id)
                or {
                    "applied": True,
                    "signal_id": signal.signal_id,
                    "action": context["action"],
                    "abort_dispatched": True,
                }
            },
        )
        deadline = runtime.application.watchdog.tool_execution.snapshot()["leases"]["tool-live-1"]
        tick = runtime.application.integration.tick(
            task_id=state.task_id,
            at_ms=deadline["started_ms"] + deadline["deadline_ms"] + 1,
        )
        assert tick.body["new_fault_kinds"] == ["tool_timeout"]
        assert len(aborted) == 1
        late = runtime.application.integration.observations.ingest(
            state,
            _event(
                runtime,
                event_id="tool-late-result-1",
                event_type="tool.succeeded",
                producer="code-worker",
                sequence=2,
                payload={
                    "tool_call_id": "tool-live-1",
                    "generation": 4,
                    "lease_token": lease_token,
                    "result_id": "late-result-1",
                },
            ),
        )
        assert late.result["body"]["accepted"] is False
        assert late.result["body"]["disposition"] == "late_after_timeout"
        with pytest.raises(RuntimeObservationRejected, match="different payload"):
            runtime.application.integration.observations.ingest(
                state,
                {**started_event, "payload": {**started_event["payload"], "deadline_ms": 50}},
            )
    finally:
        runtime.close()


def test_browser_use_style_event_attach_disable_removes_capture_without_injection_fallback(
    tmp_path: Path,
) -> None:
    runtime = _integrated_runtime(tmp_path)
    source = _BrowserEventSource()
    state = runtime.state
    try:
        attached = runtime.application.integration.attach_browser_event_source(
            state,
            {
                "browser_session_id": "browser-live-1",
                "session_id": "session-browser-1",
                "attempt_id": "browser-attempt-1",
                "generation": 2,
                "cdp_connected": True,
            },
            source,
        )
        assert attached["phase"] == "running"
        assert len(source.callbacks) == 9
        source.emit(
            "browser.cdp_disconnected",
            {
                "event_id": "browser-cdp-down-1",
                "sequence": 1,
                "reason_code": "cdp_pipe_closed",
            },
        )
        signals = runtime.application.store.signals(task_id=state.task_id)
        assert len(signals) == 1
        assert signals[0].kind is FaultKind.BROWSER_DISCONNECT
        assert signals[0].provenance.source_repo == "browser-use"
        before_injections = len(runtime.application.store.injections(task_id=state.task_id))
        detached = runtime.application.integration.browser_events.detach(
            "browser-live-1",
            generation=2,
            reason="operator disabled browser observation",
            disable=True,
        )
        assert detached.details["capture_path_removed"] is True
        assert source.unsubscribe_count == 9
        assert all(not callbacks for callbacks in source.callbacks.values())
        source.emit(
            "browser.process_exited",
            {"event_id": "browser-process-exit-after-disable", "sequence": 2, "exit_code": 9},
        )
        assert len(runtime.application.store.signals(task_id=state.task_id)) == 1
        assert len(runtime.application.store.injections(task_id=state.task_id)) == before_injections
        snapshot = runtime.application.integration.browser_events.snapshot(task_id=state.task_id)
        assert snapshot["disabled_capture_falls_back_to_injection"] is False
    finally:
        runtime.close()


def test_real_process_exit_and_mcp_disconnect_feed_active_observer_signals(tmp_path: Path) -> None:
    runtime = _integrated_runtime(tmp_path)
    state = runtime.state
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        text=True,
    )
    try:
        handle = _ProcessHandle(child)
        runtime.application.integration.bind_source(
            state,
            {
                "source_kind": "worker",
                "worker_id": "worker-process-real-1",
                "backend_id": "local-code-worker",
                "generation": 1,
                "interval_ms": 10_000,
                "grace_intervals": 3,
            },
            process_handle=handle,
        )
        child.terminate()
        child.wait(timeout=10)
        tick = runtime.application.integration.tick(task_id=state.task_id)
        assert "worker_unavailable" in tick.body["new_fault_kinds"]
        process_signal = next(
            item
            for item in runtime.application.store.signals(task_id=state.task_id)
            if item.refs.worker_id == "worker-process-real-1"
        )
        assert process_signal.observed_code == "worker_lost"
        assert process_signal.details["observation_details"]["exit_code"] == child.returncode

        runtime.application.integration.bind_source(
            state,
            {
                "source_kind": "mcp",
                "mcp_server_id": "mcp-live-1",
                "generation": 1,
                "max_reconnect_attempts": 2,
                "base_backoff_ms": 1,
            },
        )
        disconnected = runtime.application.integration.observe_source(
            state,
            {
                "source_kind": "mcp",
                "action": "disconnected",
                "mcp_server_id": "mcp-live-1",
                "generation": 1,
                "reason_code": "stdio_pipe_closed",
            },
        )
        assert disconnected.body["details"]["transport_phase"] in {"disconnected", "backoff"}
        kinds = {item.kind for item in runtime.application.store.signals(task_id=state.task_id)}
        assert FaultKind.WORKER_UNAVAILABLE in kinds
        assert FaultKind.MCP_DISCONNECTED in kinds
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        runtime.close()


def test_requirement_change_routes_to_replan_without_fault_memory_or_health_mutation(
    tmp_path: Path,
) -> None:
    runtime = _integrated_runtime(tmp_path)
    state = runtime.state
    try:
        before_fault = runtime.application.store.snapshot(task_id=state.task_id)["counts"]
        before_memory = list(runtime.canonical.task_memory_records(state.task_id))
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            event_type=EventType.REQUIREMENT_CHANGE,
            payload={
                "raw": "BrowserWorker timeout failure text is a new verification requirement, not a fault.",
                "intent": "requirement_change",
            },
        )
        receipt = runtime.application.integration.apply_requirement_change(state, event)
        after_fault = runtime.application.store.snapshot(task_id=state.task_id)["counts"]
        assert receipt["unchanged_fault_state"] is True
        assert receipt["replan_node_id"]
        assert receipt["decision_id"]
        assert before_fault == after_fault
        assert runtime.canonical.task_memory_records(state.task_id) == before_memory
        assert not runtime.application.store.signals(task_id=state.task_id)
        with pytest.raises(RuntimeObservationControlRequired, match="control event"):
            runtime.application.integration.observations.ingest(
                state,
                _event(
                    runtime,
                    event_id="requirement-observation-rejected-1",
                    event_type="requirement.changed",
                    producer="control-runtime",
                    sequence=1,
                    payload={"raw": "timeout wording is not fault truth"},
                ),
            )
    finally:
        runtime.close()


def test_permission_schema_workspace_provider_and_mcp_failures_share_structured_ingress(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "state.json").write_text('{"revision":2}', encoding="utf-8")
    runtime = _integrated_runtime(tmp_path)
    state = runtime.state
    port = runtime.application.integration.observations
    try:
        runtime.application.watchdog.enable_observer("workspace-integrity")
        receipts = [
            port.ingest(
                state,
                _event(
                    runtime,
                    event_id="permission-denied-live-1",
                    event_type="permission.settled",
                    producer="permission-runtime",
                    sequence=1,
                    payload={
                        "tool_call_id": "tool-permission-1",
                        "tool_name": "shell",
                        "receipt": {
                            "decision": "deny",
                            "rule_id": "sealed-deny-1",
                            "policy_revision": 4,
                            "reason_code": "unknown_command",
                        },
                    },
                ),
            ),
            port.ingest(
                state,
                _event(
                    runtime,
                    event_id="schema-invalid-live-1",
                    event_type="schema.validated",
                    producer="schema-runtime",
                    sequence=1,
                    payload={
                        "schema_id": "tool-result/v2",
                        "valid": False,
                        "violations": [{"path": "$.result", "code": "required"}],
                        "tool_call_id": "tool-schema-1",
                        "tool_name": "structured-output",
                    },
                ),
            ),
            port.ingest(
                state,
                _event(
                    runtime,
                    event_id="workspace-corrupt-live-1",
                    event_type="workspace.attested",
                    producer="workspace-runtime",
                    sequence=1,
                    payload={
                        "workspace_id": "workspace-integration",
                        "relative_path": "state.json",
                        "expected_digest": "sha256:" + "0" * 64,
                    },
                ),
            ),
        ]
        assert all(item.status.value == "accepted" for item in receipts)

        port.ingest(
            state,
            _event(
                runtime,
                event_id="provider-attached-live-2",
                event_type="provider.attached",
                producer="provider-runtime",
                sequence=1,
                payload={"provider_id": "provider-live-2", "generation": 5},
            ),
        )
        port.ingest(
            state,
            _event(
                runtime,
                event_id="provider-attempt-live-2",
                event_type="provider.attempt_started",
                producer="provider-runtime",
                sequence=2,
                payload={
                    "provider_id": "provider-live-2",
                    "generation": 5,
                    "attempt_id": "provider-attempt-live-2",
                },
            ),
        )
        port.ingest(
            state,
            _event(
                runtime,
                event_id="provider-failed-live-2",
                event_type="provider.failed",
                producer="provider-runtime",
                sequence=3,
                payload={
                    "provider_id": "provider-live-2",
                    "generation": 5,
                    "attempt_id": "provider-attempt-live-2",
                    "error_code": "api_retry_exhausted",
                    "error_type": "ProviderApiError",
                    "status_code": 503,
                    "retryable": False,
                },
            ),
        )
        port.ingest(
            state,
            _event(
                runtime,
                event_id="mcp-attached-auth-1",
                event_type="mcp.attached",
                producer="mcp-runtime",
                sequence=1,
                payload={"mcp_server_id": "mcp-auth-live-1", "generation": 2},
            ),
        )
        port.ingest(
            state,
            _event(
                runtime,
                event_id="mcp-auth-failed-1",
                event_type="mcp.disconnected",
                producer="mcp-runtime",
                sequence=2,
                payload={
                    "mcp_server_id": "mcp-auth-live-1",
                    "generation": 2,
                    "reason_code": "authentication_failed",
                },
            ),
        )

        signals = runtime.application.store.signals(task_id=state.task_id, limit=100)
        kinds = {item.kind for item in signals}
        assert {
            FaultKind.PERMISSION_DENIED,
            FaultKind.SCHEMA_FAILURE,
            FaultKind.WORKSPACE_CORRUPT,
            FaultKind.MODEL_FAILURE,
            FaultKind.MCP_DISCONNECTED,
        } <= kinds
        mcp_signal = next(item for item in signals if item.kind is FaultKind.MCP_DISCONNECTED)
        assert mcp_signal.details["observation_details"]["reason_code"] == "authentication_failed"
        assert len(runtime.events) == 5
        assert all(
            runtime.application.store.projection_receipt(item.signal_id).memory_record_ids
            for item in signals
        )
        assert runtime.application.integration.snapshot(task_id=state.task_id)["state_custody"][
            "recovery_plan"
        ] == "M1-S07C"
    finally:
        runtime.close()


def test_http_observation_and_command_share_single_durable_fault_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZYRA_SQLITE_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("ZYRA_EVENT_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("ZYRA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ZYRA_FAULT_RUNTIME_STORE", str(tmp_path / "fault-runtime.sqlite3"))
    from apps.api.zyra_api.main import ZyraRequestHandler, reset_fault_runtime_api

    reset_fault_runtime_api()
    server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            base + path,
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        created = post("/tasks", {"goal": "Observe and inject through one fault runtime", "auto_run": False})
        task_id = created["task"]["task_id"]
        run_id = created["task"]["run_id"]
        bound = post(
            f"/tasks/{task_id}/faults/observations",
            {
                "event_id": "http-provider-attached-1",
                "event_type": "provider.attached",
                "producer": "http-provider-runtime",
                "run_id": run_id,
                "task_id": task_id,
                "sequence": 1,
                "payload": {"provider_id": "provider-http-live-1", "generation": 1},
            },
        )
        started = post(
            f"/tasks/{task_id}/faults/observations",
            {
                "event_id": "http-provider-attempt-1",
                "event_type": "provider.attempt_started",
                "producer": "http-provider-runtime",
                "run_id": run_id,
                "task_id": task_id,
                "sequence": 2,
                "payload": {
                    "provider_id": "provider-http-live-1",
                    "attempt_id": "http-provider-attempt-1",
                    "generation": 1,
                },
            },
        )
        failed = post(
            f"/tasks/{task_id}/faults/observations",
            {
                "event_id": "http-provider-failed-1",
                "event_type": "provider.failed",
                "producer": "http-provider-runtime",
                "run_id": run_id,
                "task_id": task_id,
                "sequence": 3,
                "payload": {
                    "provider_id": "provider-http-live-1",
                    "attempt_id": "http-provider-attempt-1",
                    "generation": 1,
                    "error_code": "provider_transport_closed",
                    "status_code": 503,
                },
            },
        )
        injected = post(
            f"/tasks/{task_id}/faults/inject",
            {
                "kind": "worker_lost",
                "target": {"worker_id": "worker-http-injected-1"},
                "continuation": ContinuationMode.RECOVERY_HANDOFF.value,
                "idempotency_key": "http-observation-shared-runtime",
            },
        )
        with urllib.request.urlopen(base + f"/tasks/{task_id}/faults", timeout=30) as response:
            snapshot = json.loads(response.read().decode("utf-8"))
        assert bound["status"] == "accepted"
        assert started["status"] == "accepted"
        assert failed["status"] == "accepted"
        assert injected["ok"] is True
        assert snapshot["fault_state"]["counts"]["signals"] == 2
        assert snapshot["integration"]["runtime_observation_port"]["counts"]["accepted"] == 3
        assert snapshot["integration"]["source_sessions"]["bindings"][
            "provider:provider-http-live-1"
        ]["failure_count"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        reset_fault_runtime_api()


def test_watchdog_control_command_uses_same_injection_and_observation_state(tmp_path: Path) -> None:
    runtime = _integrated_runtime(tmp_path)
    service = FaultRuntimeApiService(runtime.application)
    command = WatchdogControlCommandRuntime(service, runtime.state, actor_id="command-test")
    try:
        injected = command.dispatch(
            "/inject",
            {
                "raw": (
                    "tool_timeout tool_call_id=command-tool-1 tool_name=shell "
                    "deadline_ms=10 continuation=recovery_handoff"
                )
            },
            idempotency_key="command-injection-idem-1",
        )
        assert injected.ok is True
        assert injected.data["same_run"] is True
        assert "canonical_event_store" in injected.mutations
        assert "MemoryFabric" in injected.mutations
        observed = command.dispatch(
            "/watchdog",
            {
                "action": "runtime-event",
                "payload": _event(
                    runtime,
                    event_id="command-provider-attached-1",
                    event_type="provider.attached",
                    producer="command-provider-runtime",
                    sequence=1,
                    payload={"provider_id": "provider-command-1", "generation": 1},
                ),
            },
            idempotency_key="command-runtime-event-idem-1",
        )
        assert observed.ok is True
        assert observed.data["status"] == "accepted"
        status = command.dispatch(
            "/watchdog",
            {"action": "status"},
            idempotency_key="command-status-idem-1",
        )
        assert status.data["fault_state"]["counts"]["signals"] == 1
        assert "provider:provider-command-1" in status.data["integration"]["source_sessions"]["bindings"]
    finally:
        runtime.close()
