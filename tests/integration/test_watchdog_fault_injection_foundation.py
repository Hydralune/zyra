from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
from typing import Any
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from zyra_core import EventType, create_task_state
from zyra_scheduler.fault_runtime import (
    ContinuationMode,
    CorrelationRefs,
    FaultInjectionRequest,
    FaultKind,
    FaultPressureMonitor,
    FaultQuery,
    FaultRuntimeApplication,
    FaultRuntimeApiService,
    FaultRuntimeDiagnostics,
    FaultRuntimeQueryService,
    FaultStateStore,
    InjectionKind,
    InjectionPhase,
    ObservationCategory,
    ObservationProvenance,
    ObserverMaturity,
    PressureLevel,
    ProviderRetryPolicy,
    SignalOrigin,
    StructuredObservation,
    TerminalResultDisposition,
    WatchdogPollingCoordinator,
    WatchdogSignalClassifier,
    parse_injection_command,
)


def _refs(state: Any, **values: Any) -> CorrelationRefs:
    return CorrelationRefs(
        run_id=state.run_id,
        task_id=state.task_id,
        observation_id=str(values.pop("observation_id", "observation-test")),
        **values,
    )


def _application(tmp_path: Path):
    state = create_task_state("Exercise watchdog and fault injection in one run.")
    events = []
    application = FaultRuntimeApplication(
        tmp_path / "fault-runtime.sqlite3",
        event_sink=events.append,
        task_state_resolver=lambda task_id: state if task_id == state.task_id else None,
    )
    return state, events, application


def _request(state: Any, kind: InjectionKind, idempotency_key: str) -> FaultInjectionRequest:
    target_values = {
        InjectionKind.WORKER_LOST: {"worker_id": "worker-local-1", "backend_id": "local-terminal"},
        InjectionKind.TOOL_TIMEOUT: {"tool_call_id": "tool-call-1", "tool_name": "shell"},
        InjectionKind.BROWSER_CRASH: {"browser_session_id": "browser-session-1"},
        InjectionKind.MODEL_FAILURE: {"provider_id": "provider-cloud-1"},
        InjectionKind.WORKSPACE_CORRUPT: {"workspace_id": "workspace-task-1"},
    }[kind]
    return FaultInjectionRequest(
        run_id=state.run_id,
        task_id=state.task_id,
        kind=kind,
        target=_refs(state, observation_id=f"target-{kind.value}", **target_values),
        requested_by="pytest",
        idempotency_key=idempotency_key,
        continuation=ContinuationMode.RECOVERY_HANDOFF,
        parameters={"scenario": f"test-{kind.value}"},
    )


def test_close_does_not_reopen_a_released_missing_store(tmp_path: Path) -> None:
    _, _, application = _application(tmp_path)
    application.watchdog.polling.stop()
    application.store.release_connection()
    for suffix in ("", "-shm", "-wal"):
        Path(f"{application.store.path}{suffix}").unlink(missing_ok=True)

    application.close()

    assert not application.store.path.exists()


def test_five_same_run_injections_write_events_and_recovery_handoffs(tmp_path: Path) -> None:
    state, events, application = _application(tmp_path)
    try:
        receipts = [
            application.inject(_request(state, kind, f"idem-{kind.value}"), task_state=state)
            for kind in InjectionKind
        ]
        assert all(receipt.ok for receipt in receipts)
        assert all(receipt.phase.value == "handed_off" for receipt in receipts)
        assert {receipt.signal.kind for receipt in receipts if receipt.signal} == {
            FaultKind.WORKER_UNAVAILABLE,
            FaultKind.TOOL_TIMEOUT,
            FaultKind.BROWSER_CRASH,
            FaultKind.MODEL_FAILURE,
            FaultKind.WORKSPACE_CORRUPT,
        }
        assert len(events) == len(InjectionKind)
        assert all(event.event_type is EventType.FAILURE_INJECTED for event in events)
        assert all(event.payload["signal"]["origin"] == "injection" for event in events)
        assert len(application.store.handoffs(task_id=state.task_id)) == len(InjectionKind)
        assert len(state.metadata["fault_injection"]) == len(InjectionKind)
        assert all(not value["external_resource_mutated"] for value in state.metadata["fault_injection"].values())
    finally:
        application.close()


def test_injection_idempotency_restores_terminal_receipt(tmp_path: Path) -> None:
    state, events, application = _application(tmp_path)
    try:
        first = application.inject(_request(state, InjectionKind.TOOL_TIMEOUT, "same-idem"), task_state=state)
        duplicate_request = _request(state, InjectionKind.TOOL_TIMEOUT, "same-idem")
        duplicate = application.inject(duplicate_request, task_state=state)
        assert first.ok and duplicate.ok
        assert duplicate.duplicate is True
        assert duplicate.request.injection_id == first.request.injection_id
        assert duplicate.signal.signal_id == first.signal.signal_id
        assert len(events) == 1
        assert duplicate.transitions[-1].phase.value == "handed_off"
    finally:
        application.close()


def test_recovery_handoff_delivery_is_lease_fenced_and_restart_durable(tmp_path: Path) -> None:
    state, _events, application = _application(tmp_path)
    store_path = tmp_path / "fault-runtime.sqlite3"
    try:
        receipt = application.inject(
            _request(state, InjectionKind.WORKER_LOST, "handoff-delivery-idem"),
            task_state=state,
        )
        assert receipt.handoff is not None
        first = application.recovery.claim(
            consumer="recovery-planner-a",
            now_ms=100,
            lease_ms=20,
            task_id=state.task_id,
        )
        assert len(first) == 1
        assert first[0]["handoff"]["handoff_id"] == receipt.handoff.handoff_id
        assert application.recovery.claim(
            consumer="recovery-planner-b",
            now_ms=119,
            lease_ms=20,
            task_id=state.task_id,
        ) == ()
        reclaimed = application.recovery.claim(
            consumer="recovery-planner-b",
            now_ms=120,
            lease_ms=20,
            task_id=state.task_id,
        )
        assert len(reclaimed) == 1
        assert reclaimed[0]["attempt"] == 2
        with pytest.raises(Exception, match="ownership"):
            application.recovery.acknowledge(
                receipt.handoff.handoff_id,
                consumer="recovery-planner-a",
                lease_token=first[0]["lease_token"],
            )
        acknowledged = application.recovery.acknowledge(
            receipt.handoff.handoff_id,
            consumer="recovery-planner-b",
            lease_token=reclaimed[0]["lease_token"],
            receipt={"recovery_plan_id": "recovery-plan-1"},
        )
        assert acknowledged["status"] == "acknowledged"
    finally:
        application.close()

    restored = FaultStateStore(store_path)
    try:
        deliveries = restored.handoff_deliveries(task_id=state.task_id)
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "acknowledged"
        assert deliveries[0]["attempts"] == 2
        assert deliveries[0]["receipt"]["recovery_plan_id"] == "recovery-plan-1"
    finally:
        restored.close()


def test_restart_reconciliation_fails_pre_observation_and_resumes_projected_injection(
    tmp_path: Path,
) -> None:
    state = create_task_state("Reconcile crash-interrupted same-run injections.")
    store_path = tmp_path / "fault-runtime.sqlite3"
    pre_observation = _request(state, InjectionKind.WORKER_LOST, "reconcile-before-observation")
    store = FaultStateStore(store_path)
    try:
        _stored, requested, _duplicate = store.begin_injection(pre_observation)
        store.transition_injection(
            pre_observation.injection_id,
            InjectionPhase.ARMED,
            expected_revision=requested.revision,
            reason="test simulates process loss after arming",
        )
    finally:
        store.close()

    initial_events: list[Any] = []
    first = FaultRuntimeApplication(
        store_path,
        event_sink=initial_events.append,
        task_state_resolver=lambda task_id: state if task_id == state.task_id else None,
    )
    try:
        failed = first.store.injection(pre_observation.injection_id)
        assert failed is not None
        assert failed[1][-1].phase is InjectionPhase.FAILED
        assert first.reconciliation_report[0]["external_resource_replayed"] is False

        projected_request = _request(
            state,
            InjectionKind.MODEL_FAILURE,
            "reconcile-after-projection",
        )
        _stored, latest, _duplicate = first.store.begin_injection(projected_request)
        latest = first.store.transition_injection(
            projected_request.injection_id,
            InjectionPhase.ARMED,
            expected_revision=latest.revision,
            reason="test arms projected restart scenario",
        )
        observation = first.injections.injector.build(projected_request)
        latest = first.store.transition_injection(
            projected_request.injection_id,
            InjectionPhase.TRIGGERED,
            expected_revision=latest.revision,
            reason="test persists the injected boundary",
        )
        signal = first.injections.injector.trigger(observation)
        latest = first.store.transition_injection(
            projected_request.injection_id,
            InjectionPhase.OBSERVED,
            expected_revision=latest.revision,
            reason="test persists the classified signal",
            signal_id=signal.signal_id,
        )
        projection = first.writer.write(signal, task_state=state)
        assert projection.canonical_event_written
        first.store.transition_injection(
            projected_request.injection_id,
            InjectionPhase.PROJECTED,
            expected_revision=latest.revision,
            reason="test simulates process loss before continuation handoff",
            signal_id=signal.signal_id,
            event_id=projection.event_id,
        )
    finally:
        first.close()

    restart_events: list[Any] = []
    restarted = FaultRuntimeApplication(
        store_path,
        event_sink=restart_events.append,
        task_state_resolver=lambda task_id: state if task_id == state.task_id else None,
    )
    try:
        restored = restarted.store.injection(projected_request.injection_id)
        assert restored is not None
        assert restored[1][-1].phase is InjectionPhase.HANDED_OFF
        report = next(
            item
            for item in restarted.reconciliation_report
            if item["injection_id"] == projected_request.injection_id
        )
        assert report["status"] == "resumed"
        assert report["signal_id"] == signal.signal_id
        assert restart_events == []
        assert len(restarted.store.handoffs(task_id=state.task_id)) == 1
    finally:
        restarted.close()


def test_observer_runtime_epoch_reattaches_persisted_running_callbacks(tmp_path: Path) -> None:
    state = create_task_state("Reattach observer callbacks after an ungraceful process stop.")
    store_path = tmp_path / "fault-runtime.sqlite3"
    first = FaultRuntimeApplication(
        store_path,
        event_sink=lambda _event: None,
        task_state_resolver=lambda task_id: state if task_id == state.task_id else None,
    )
    first_epoch = first.watchdog.lifecycle.process_epoch
    assert first.store.require_observer("browser-crash").lifecycle.value == "running"
    first.store.close()

    events: list[Any] = []
    restarted = FaultRuntimeApplication(
        store_path,
        event_sink=events.append,
        task_state_resolver=lambda task_id: state if task_id == state.task_id else None,
    )
    try:
        assert restarted.watchdog.lifecycle.process_epoch != first_epoch
        startup = restarted.watchdog.lifecycle.snapshot()["startup_report"]
        browser = next(item for item in startup if item["observer_id"] == "browser-crash")
        assert browser["action"] == "reattached_stale_running"
        runtime = restarted.watchdog.registry.runtime_snapshot("browser-crash")
        assert runtime["attached"] is True
        assert runtime["running"] is True

        observation = restarted.watchdog.browser.observe_04d_signal({
            "signal_id": "browser-after-runtime-restart",
            "kind": "process_exited",
            "status": "terminated",
            "summary": "Browser process exited after runtime callback restoration.",
            "sequence": 1,
            "retryable": True,
            "terminal": True,
            "scope": {
                "run_id": state.run_id,
                "task_id": state.task_id,
                "browser_session_id": "browser-restart-session",
                "worker_request_id": "browser-restart-attempt",
            },
            "metadata": {"exit_code": 17},
        })
        assert observation is not None
        assert restarted.store.signals(task_id=state.task_id)[0].kind is FaultKind.BROWSER_CRASH
        assert len(events) == 1
    finally:
        restarted.close()


def test_observer_runtime_supervisor_backoff_and_heartbeat_fences(tmp_path: Path) -> None:
    state, _events, application = _application(tmp_path)
    try:
        failure = application.watchdog.lifecycle.report_failure(
            "provider-response",
            error="provider callback stream closed",
            at_ms=10,
        )
        assert failure["outcome"] == "restart_scheduled"
        assert failure["next_restart_ms"] == 260
        assert application.store.require_observer("provider-response").lifecycle.value == "failed"
        assert application.watchdog.lifecycle.restart_due(at_ms=259) == ()
        restarted = application.watchdog.lifecycle.restart_due(at_ms=260)
        assert restarted[0]["outcome"] == "restarted"
        generation = restarted[0]["generation"]
        assert application.watchdog.lifecycle.source_heartbeat(
            "provider-response",
            generation=generation - 1,
            sequence=99,
            at_ms=300,
        ) is False
        assert application.watchdog.lifecycle.source_heartbeat(
            "provider-response",
            generation=generation,
            sequence=1,
            at_ms=300,
        ) is True
        assert application.watchdog.lifecycle.source_heartbeat(
            "provider-response",
            generation=generation,
            sequence=1,
            at_ms=301,
        ) is False
        stale = application.watchdog.lifecycle.sweep_stale(at_ms=60_301)
        assert stale[0]["outcome"] == "restart_scheduled"
        snapshot = application.watchdog.lifecycle.snapshot()["observers"]["provider-response"]
        assert snapshot["stale_heartbeats"] == 2
        assert snapshot["failure_count"] == 1
    finally:
        application.close()


def test_durable_source_revision_cursor_rejects_restart_regression(tmp_path: Path) -> None:
    state = create_task_state("Fence source revisions across observer process epochs.")
    store_path = tmp_path / "fault-runtime.sqlite3"
    refs = _refs(
        state,
        observation_id="provider-source-cursor",
        provider_id="provider-cursor-1",
    )
    first = FaultRuntimeApplication(
        store_path,
        event_sink=lambda _event: None,
        task_state_resolver=lambda task_id: state if task_id == state.task_id else None,
    )
    try:
        assert first.watchdog.providers.observe_response(
            refs,
            ok=False,
            status_code=503,
            error_code="provider_error",
            retryable=True,
        ) is not None
        assert first.store.observation_cursors(
            task_id=state.task_id,
            observer_id="provider-response",
        )[0]["source_state_revision"] == 1
    finally:
        first.close()

    restarted = FaultRuntimeApplication(
        store_path,
        event_sink=lambda _event: None,
        task_state_resolver=lambda task_id: state if task_id == state.task_id else None,
    )
    try:
        with pytest.raises(Exception, match="revision was reused"):
            restarted.watchdog.providers.observe_response(
                refs,
                ok=False,
                status_code=500,
                error_code="provider_error",
                retryable=True,
            )
        accepted = restarted.watchdog.providers.observe_response(
            refs,
            ok=False,
            status_code=502,
            error_code="provider_error",
            retryable=True,
        )
        assert accepted is not None
        cursor = restarted.store.observation_cursors(
            task_id=state.task_id,
            observer_id="provider-response",
        )[0]
        assert cursor["source_state_revision"] == 2
        assert len(restarted.store.signals(task_id=state.task_id)) == 2
    finally:
        restarted.close()


def test_disabling_real_browser_observer_does_not_disable_injection(tmp_path: Path) -> None:
    state, events, application = _application(tmp_path)
    try:
        disabled = application.watchdog.disable_observer("browser-crash", reason="disconnect test")
        assert disabled["lifecycle"] == "disabled"
        source_signal = {
            "signal_id": "04d-browser-signal-1",
            "kind": "browser_process_exited",
            "status": "failed",
            "summary": "browser process exited",
            "sequence": 1,
            "scope": {
                "run_id": state.run_id,
                "task_id": state.task_id,
                "browser_session_id": "browser-session-1",
                "worker_request_id": "attempt-1",
            },
            "metadata": {"exit_code": 1},
        }
        application.watchdog.browser.observe_04d_signal(source_signal)
        assert not application.store.signals(task_id=state.task_id)

        receipt = application.inject(
            _request(state, InjectionKind.BROWSER_CRASH, "browser-injection-after-disable"),
            task_state=state,
        )
        assert receipt.ok
        assert receipt.signal.origin is SignalOrigin.INJECTION
        assert receipt.signal.provenance.observer_id == "same-run-fault-injector"
        assert len(events) == 1
        injection_state = application.store.require_observer("same-run-fault-injector")
        assert injection_state.lifecycle.value == "running"
    finally:
        application.close()


def test_real_tool_provider_and_browser_sources_project_canonical_events(tmp_path: Path) -> None:
    state, events, application = _application(tmp_path)
    try:
        tool_refs = _refs(
            state,
            observation_id="tool-deadline-source",
            tool_call_id="tool-real-1",
            tool_name="shell",
        )
        record = application.watchdog.tool_deadlines.begin(tool_refs, deadline_ms=10)
        tool_observations = application.watchdog.tool_deadlines.poll(at_ms=record.started_ms + 11)
        assert len(tool_observations) == 1

        provider = application.watchdog.providers.observe_response(
            _refs(state, observation_id="provider-source", provider_id="provider-real-1"),
            ok=False,
            status_code=503,
            error_type="ServiceUnavailable",
            error_code="provider_error",
            retryable=True,
        )
        assert provider is not None

        browser = application.watchdog.browser.observe_04d_signal({
            "signal_id": "04d-browser-real-1",
            "kind": "browser_process_exited",
            "status": "failed",
            "summary": "Browser process exited unexpectedly.",
            "sequence": 1,
            "retryable": True,
            "terminal": True,
            "scope": {
                "run_id": state.run_id,
                "task_id": state.task_id,
                "browser_session_id": "browser-real-1",
                "worker_request_id": "browser-attempt-1",
                "node_id": state.root_node_id,
            },
            "metadata": {"exit_code": 9},
        })
        assert browser is not None
        kinds = {signal.kind for signal in application.store.signals(task_id=state.task_id)}
        assert {FaultKind.TOOL_TIMEOUT, FaultKind.MODEL_FAILURE, FaultKind.BROWSER_CRASH} <= kinds
        assert len(events) == 3
        assert all(event.event_type is EventType.WORKER_HEALTH for event in events)
        assert all(event.payload["signal"]["origin"] == "observer" for event in events)
        assert len(application.store.handoffs(task_id=state.task_id)) == 3
    finally:
        application.close()


@dataclass
class _Process:
    pid: int = 42
    exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code


def test_attached_04d_browser_source_polls_real_handle_and_generation_fences(tmp_path: Path) -> None:
    state, events, application = _application(tmp_path)
    try:
        process = _Process(pid=314, exit_code=None)
        refs = _refs(
            state,
            observation_id="browser-source-binding",
            node_id=state.root_node_id,
            session_id="session-browser-source",
            attempt_id="browser-worker-request-1",
            browser_session_id="browser-source-session-1",
        )
        binding = application.watchdog.browser_source.attach(
            refs,
            generation=2,
            process_handle=process,
            cdp_connected=True,
        )
        assert application.watchdog.browser_source.poll(
            refs.browser_session_id,
            generation=2,
        ) == ()
        last_alive_ms = application.watchdog.browser_source.detector.state(binding.scope)[
            "process_last_alive_ms"
        ]
        process.exit_code = 9
        signals = application.watchdog.browser_source.poll(
            refs.browser_session_id,
            generation=2,
            at_ms=last_alive_ms + 251,
        )
        assert any(item.kind.value == "process_exited" and item.terminal for item in signals)
        canonical = application.store.signals(task_id=state.task_id)
        assert canonical[-1].kind is FaultKind.BROWSER_CRASH
        assert canonical[-1].refs.browser_session_id == refs.browser_session_id
        assert binding.canonical_observation_count == 1
        assert events[-1].payload["signal"]["provenance"]["source_repo"] == "browser-use"
        with pytest.raises(RuntimeError, match="stale browser source generation"):
            application.watchdog.browser_source.poll(refs.browser_session_id, generation=1)
        assert application.watchdog.browser_source.snapshot()[
            "upstream_browser_use_crash_watchdog_attached"
        ] is False
    finally:
        application.close()


def test_provider_attempt_backoff_circuit_and_terminal_classification(tmp_path: Path) -> None:
    state, _events, application = _application(tmp_path)
    try:
        refs = _refs(
            state,
            observation_id="provider-supervision-binding",
            provider_id="provider-supervised-1",
        )
        application.watchdog.provider_attempts.bind(
            refs,
            generation=4,
            policy=ProviderRetryPolicy(
                max_attempts=2,
                base_backoff_ms=10,
                max_backoff_ms=10,
                circuit_failure_threshold=2,
                circuit_reset_ms=50,
            ),
        )
        first = application.watchdog.provider_attempts.begin_attempt(
            refs.provider_id,
            generation=4,
            attempt_id="provider-attempt-1",
            at_ms=0,
        )
        first_failure = application.watchdog.provider_attempts.failed(
            first,
            status_code=503,
            error_code="provider_error",
            error_type="ServiceUnavailable",
            at_ms=0,
        )
        assert first_failure.retryable is True
        assert first_failure.next_attempt_at_ms == 10
        with pytest.raises(RuntimeError, match="backoff"):
            application.watchdog.provider_attempts.begin_attempt(
                refs.provider_id,
                generation=4,
                attempt_id="provider-attempt-too-early",
                at_ms=9,
            )
        second = application.watchdog.provider_attempts.begin_attempt(
            refs.provider_id,
            generation=4,
            attempt_id="provider-attempt-2",
            at_ms=10,
        )
        exhausted = application.watchdog.provider_attempts.failed(
            second,
            status_code=503,
            error_code="provider_error",
            at_ms=10,
        )
        assert exhausted.exhausted is True
        assert exhausted.circuit_open is True
        assert exhausted.observation is not None
        signals = application.store.signals(task_id=state.task_id)
        assert signals[0].kind is FaultKind.MODEL_FAILURE
        assert signals[0].terminal is True
        snapshot = application.watchdog.provider_attempts.snapshot()["states"]
        assert snapshot[refs.provider_id]["phase"] == "open"
        assert snapshot[refs.provider_id]["rejected_attempts"] == 1
    finally:
        application.close()


def test_tool_deadline_fences_late_and_duplicate_terminal_results(tmp_path: Path) -> None:
    state, _events, application = _application(tmp_path)
    try:
        first_refs = _refs(
            state,
            observation_id="tool-deadline-lease-1",
            tool_call_id="tool-deadline-fenced-1",
            tool_name="shell",
        )
        first = application.watchdog.tool_execution.arm(
            first_refs,
            generation=1,
            deadline_ms=10,
        )
        assert application.watchdog.tool_execution.poll(
            at_ms=first.record.started_ms + 11
        )
        late = application.watchdog.tool_execution.accept_result(
            first_refs.tool_call_id,
            generation=1,
            token=first.token,
            result_id="tool-result-late-1",
            ok=True,
        )
        assert late.accepted is False
        assert late.disposition is TerminalResultDisposition.LATE_AFTER_TIMEOUT
        assert first.record.status == "timed_out"

        second_refs = _refs(
            state,
            observation_id="tool-deadline-lease-2",
            tool_call_id="tool-deadline-fenced-2",
            tool_name="read",
        )
        second = application.watchdog.tool_execution.arm(
            second_refs,
            generation=3,
            deadline_ms=1_000,
        )
        accepted = application.watchdog.tool_execution.accept_result(
            second_refs.tool_call_id,
            generation=3,
            token=second.token,
            result_id="tool-result-accepted-1",
            ok=True,
        )
        duplicate = application.watchdog.tool_execution.accept_result(
            second_refs.tool_call_id,
            generation=3,
            token=second.token,
            result_id="tool-result-duplicate-1",
            ok=True,
        )
        assert accepted.accepted is True
        assert duplicate.disposition is TerminalResultDisposition.DUPLICATE
        snapshot = application.watchdog.tool_execution.snapshot()
        assert snapshot["late_results"] == 1
        assert snapshot["duplicate_results"] == 1
        assert snapshot["late_result_can_mutate_terminal_state"] is False
    finally:
        application.close()


def test_process_heartbeat_mcp_and_workspace_observers_have_real_state_effects(tmp_path: Path) -> None:
    state, events, application = _application(tmp_path)
    try:
        process = _Process()
        application.watchdog.processes.bind(
            "worker-process-1",
            _refs(state, observation_id="process-source", worker_id="worker-real-1"),
            process,
            generation=1,
        )
        assert application.watchdog.processes.poll() == ()
        process.exit_code = 17
        assert len(application.watchdog.processes.poll()) == 1

        heartbeat = application.watchdog.worker_heartbeats.bind(
            _refs(state, observation_id="heartbeat-source", worker_id="worker-real-2"),
            generation=3,
            interval_ms=10,
            grace_intervals=2,
        )
        assert len(application.watchdog.worker_heartbeats.sweep(at_ms=heartbeat.last_heartbeat_ms + 21)) == 1
        assert application.watchdog.worker_heartbeats.heartbeat(
            "worker-real-2", generation=2, sequence=99
        ) is False

        mcp = application.watchdog.mcp_transports.connected(
            _refs(state, observation_id="mcp-source", mcp_server_id="mcp-real-1"),
            generation=1,
            max_reconnect_attempts=2,
            base_backoff_ms=10,
        )
        assert application.watchdog.mcp_transports.disconnected(
            "mcp-real-1", generation=1, reason_code="pipe_closed"
        ) is not None
        application.watchdog.mcp_transports.reconnect_failed(
            "mcp-real-1", generation=1, at_ms=mcp.disconnected_at_ms or 0
        )

        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        target = workspace_root / "state.txt"
        target.write_text("current", encoding="utf-8")
        application.watchdog.workspaces.register_workspace("workspace-real-1", workspace_root)
        application.watchdog.enable_observer("workspace-integrity")
        workspace_observation = application.watchdog.workspaces.attest(
            _refs(state, observation_id="workspace-source", workspace_id="workspace-real-1"),
            relative_path="state.txt",
            expected_digest="sha256:" + "0" * 64,
        )
        assert workspace_observation is not None

        subagent_observation = application.watchdog.subagents.observe_terminal_receipt(
            _refs(
                state,
                observation_id="subagent-source",
                subagent_task_id="subagent-logical-1",
                worker_id="worker-real-3",
            ),
            {
                "status": "failed",
                "revision": 7,
                "attempt_number": 2,
                "failure_code": "child_runtime_failed",
                "checkpoint_id": "checkpoint-child-1",
                "retryable": True,
            },
        )
        assert subagent_observation is not None

        kinds = {signal.kind for signal in application.store.signals(task_id=state.task_id)}
        assert {
            FaultKind.WORKER_UNAVAILABLE,
            FaultKind.MCP_DISCONNECTED,
            FaultKind.WORKSPACE_CORRUPT,
            FaultKind.SUBAGENT_FAILED,
        } <= kinds
        assert events
    finally:
        application.close()


def test_requirement_change_is_never_classified_as_fault() -> None:
    state = create_task_state("Change a requirement without creating a fault.")
    observation = StructuredObservation(
        category=ObservationCategory.REQUIREMENT_CHANGE,
        code="requirement_changed",
        refs=_refs(state),
        provenance=ObservationProvenance(
            observer_id="requirement-control",
            source_repo="zyra",
            source_revision="M1-S03D",
            observation_point="control.requirement-change",
            maturity=ObserverMaturity.ACTIVE_REAL,
        ),
        summary="BrowserWorker failed timeout worker-123 text must not be parsed.",
    )
    result = WatchdogSignalClassifier().classify(observation)
    assert result.ignored is True
    assert result.signal is None
    assert "not a fault" in result.reason


def test_classifier_rejects_missing_critical_ref_even_when_summary_contains_identity() -> None:
    state = create_task_state("Do not infer critical identities from prose.")
    observation = StructuredObservation(
        category=ObservationCategory.PROVIDER,
        code="provider_error",
        refs=_refs(state),
        provenance=ObservationProvenance(
            observer_id="provider-test",
            source_repo="oh-my-pi",
            source_revision="test",
            observation_point="provider.response",
            maturity=ObserverMaturity.ACTIVE_REAL,
        ),
        summary="provider-real-1 failed",
    )
    with pytest.raises(Exception) as raised:
        WatchdogSignalClassifier().classify(observation)
    assert "provider_id" in str(raised.value)


def test_typescript_runtime_event_ingress_uses_structured_refs(tmp_path: Path) -> None:
    state, events, application = _application(tmp_path)
    try:
        event = {
            "phase": "tool_failure_signal",
            "sequence": 1,
            "runtime_id": "zyra-typescript-claude-runtime",
            "signal_id": "ts-source-signal-1",
            "observed_code": "tool_timeout",
            "provenance": {"source_repo": "oh-my-pi", "source_revision": "c6b83c"},
            "observation": {
                "observation_id": "ts-observation-1",
                "category": "tool",
                "code": "tool_timeout",
                "status": "timed_out",
                "summary": "tool timed out",
                "error_type": "ToolTimeoutError",
                "retryable_hint": True,
                "terminal_hint": True,
                "elapsed_ms": 101,
                "deadline_ms": 100,
                "refs": {
                    "run_id": state.run_id,
                    "task_id": state.task_id,
                    "observation_id": "ts-observation-1",
                    "tool_call_id": "ts-tool-call-1",
                    "tool_name": "shell",
                    "source_state_revision": 1,
                },
                "details": {"terminal_result_guard": True},
            },
        }
        receipt = application.watchdog.ingest_runtime_event(event)
        assert receipt["accepted"] is True
        assert receipt["canonical_signal_id"]
        signal = application.store.signal(receipt["canonical_signal_id"])
        assert signal.kind is FaultKind.TOOL_TIMEOUT
        assert signal.refs.tool_call_id == "ts-tool-call-1"
        assert signal.provenance.observer_id == "typescript-runtime-ingress"
        assert len(events) == 1
    finally:
        application.close()


def test_query_diagnostics_pressure_and_restore_are_durable(tmp_path: Path) -> None:
    state, _events, application = _application(tmp_path)
    path = tmp_path / "fault-runtime.sqlite3"
    try:
        for index in range(3):
            request = _request(state, InjectionKind.MODEL_FAILURE, f"provider-idem-{index}")
            assert application.inject(request, task_state=state).ok
        query = FaultRuntimeQueryService(application.store)
        injected = query.signals(FaultQuery(task_id=state.task_id, origins=(SignalOrigin.INJECTION,)))
        assert len(injected) == 3
        assert query.task_summary(state.task_id)["counts"]["signals"] == 3
        assert query.timeline(task_id=state.task_id)
        assert query.recovery_queue(task_id=state.task_id)
        assert query.run_matrix(task_id=state.task_id)["run_count"] == 1
        assert FaultRuntimeDiagnostics(application.store).inspect(task_id=state.task_id).ok
        monitor = FaultPressureMonitor()
        decisions = [monitor.record(signal, at_ms=index) for index, signal in enumerate(injected)]
        assert all(decision.level is PressureLevel.NORMAL for decision in decisions)
        assert monitor.snapshot(task_id=state.task_id)["injection_masks_observer_pressure"] is False
    finally:
        application.close()

    restored = FaultStateStore(path)
    try:
        assert len(restored.signals(task_id=state.task_id)) == 3
        assert len(restored.injections(task_id=state.task_id)) == 3
        assert len(restored.handoffs(task_id=state.task_id)) == 3
    finally:
        restored.close()


def test_polling_coordinator_is_explicit_and_failure_bounded() -> None:
    now = [0]
    calls = [0]

    def failing() -> dict[str, Any]:
        calls[0] += 1
        raise RuntimeError("poll failure")

    coordinator = WatchdogPollingCoordinator(monotonic_ms=lambda: now[0])
    coordinator.register("failing", failing, interval_ms=10, max_failures=2)
    assert coordinator.snapshot()["thread_running"] is False
    first = coordinator.run_due(at_ms=0)[0]
    assert first["status"] == "backoff"
    now[0] = first["next_due_ms"]
    second = coordinator.run_due(at_ms=now[0])[0]
    assert second["status"] == "failed"
    assert calls[0] == 2


def test_injection_command_parser_requires_named_critical_refs() -> None:
    value = parse_injection_command(
        "tool_timeout tool_call_id=call-1 tool_name=shell deadline_ms=10 continuation=recovery_handoff"
    )
    assert value["kind"] == "tool_timeout"
    assert value["target"]["tool_call_id"] == "call-1"
    assert value["parameters"]["deadline_ms"] == 10
    with pytest.raises(Exception):
        parse_injection_command("tool_timeout call-1")


def test_fault_api_service_changes_same_task_state(tmp_path: Path) -> None:
    state, events, application = _application(tmp_path)
    try:
        service = FaultRuntimeApiService(application)
        response = service.route_post(
            ("tasks", state.task_id, "faults", "inject"),
            {
                "kind": "worker_lost",
                "target": {"worker_id": "worker-api-1"},
                "continuation": "recovery_handoff",
                "idempotency_key": "api-idem-1",
            },
            task_state=state,
        )
        assert response.status.value == 201
        assert response.body["same_run"] is True
        assert response.body["signal"]["refs"]["worker_id"] == "worker-api-1"
        view = service.route_get(("tasks", state.task_id, "faults"), task_state=state)
        assert view.status.value == 200
        assert view.body["fault_state"]["counts"]["signals"] == 1
        assert len(events) == 1
    finally:
        application.close()


def test_http_fault_route_is_dynamically_reachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZYRA_SQLITE_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("ZYRA_EVENT_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("ZYRA_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ZYRA_FAULT_RUNTIME_STORE", str(tmp_path / "fault-runtime.sqlite3"))
    from apps.api.zyra_api import main as api

    api.reset_api_product_bootstrap()
    api.reset_runtime_owner_composition()
    api.reset_fault_runtime_api()
    api.reset_recovery_runtime_api()
    ZyraRequestHandler = api.ZyraRequestHandler

    server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        created = post("/tasks", {"goal": "HTTP same-run fault injection", "auto_run": False})
        task_id = created["task"]["task_id"]
        injected = post(
            f"/tasks/{task_id}/faults/inject",
            {
                "kind": "model_failure",
                "target": {"provider_id": "provider-http-1"},
                "continuation": "recovery_handoff",
                "idempotency_key": "http-fault-idem-1",
            },
        )
        with urllib.request.urlopen(base + f"/tasks/{task_id}/faults", timeout=30) as response:
            fault_view = json.loads(response.read().decode("utf-8"))
        assert injected["ok"] is True
        assert injected["signal"]["origin"] == "injection"
        assert injected["handoff"]["metadata"]["consumer"] == "M1-S07C.RecoveryPlanner"
        assert fault_view["fault_state"]["counts"]["signals"] == 1
        assert fault_view["api"]["observer_disable_is_independent_from_injection"] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        api.reset_api_product_bootstrap()
        api.reset_runtime_owner_composition()
        api.reset_fault_runtime_api()
        api.reset_recovery_runtime_api()
