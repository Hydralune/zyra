from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from zyra_core import EventRecord, EventType
from zyra_runtime import LocalArtifactStore, WorkerRequest
from zyra_workers.browser_observability import (
    BrowserCrashDetector,
    BrowserObservabilityApplication,
    CrashDetectorPolicy,
    HistoryKind,
    SignalKind,
)


@dataclass
class Receipt:
    receipt_id: str
    action_id: str
    tool_call_id: str
    action: str
    ok: bool
    request_digest: str
    output: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[Any, ...] = ()
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    outcome_unknown: bool = False
    side_effect_count: int = 1


def worker_request(tmp_path: Path) -> WorkerRequest:
    return WorkerRequest(
        run_id="run-main-path",
        task_id="task-main-path",
        node_id="node-main-path",
        worker_name="BrowserWorker",
        request_id="browser-worker-main-path",
        constraints={
            "workspace_root": str(tmp_path / "workspace"),
            "session_id": "canonical-main-path",
            "canonical_session_id": "canonical-main-path",
            "browser_current_url": "https://example.test/result",
            "browser_about_blank_expected": False,
        },
    )


def session_start() -> Any:
    session = SimpleNamespace(
        session_id="browser-session-main-path",
        canonical_session_id="canonical-main-path",
        status="running",
        revision=1,
        runtime_id="runtime-main-path",
        resource_id="resource-main-path",
        cdp_connected=True,
        current_url="https://example.test/result",
    )
    return SimpleNamespace(
        ok=True,
        created=True,
        reused=False,
        session=session,
    )


def test_observability_application_is_durable_and_queryable(
    tmp_path: Path,
) -> None:
    artifact_store = LocalArtifactStore(tmp_path / "artifacts")
    application = BrowserObservabilityApplication(
        artifact_store=artifact_store,
    )
    request = worker_request(tmp_path)
    start = EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.BROWSER_SESSION_LIFECYCLE,
        payload={"browser_session": {"phase": "session_attached"}},
    )
    call_event = EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "browser_action": {
                "tool_call_id": "tool-call-main",
                "action_id": "action-main",
            }
        },
    )
    result_event = EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "browser_result": {
                "tool_call_id": "tool-call-main",
                "action_id": "action-main",
                "ok": True,
            }
        },
    )
    action_run = SimpleNamespace(
        receipts=(
            Receipt(
                receipt_id="receipt-main",
                action_id="action-main",
                tool_call_id="tool-call-main",
                action="click",
                ok=True,
                request_digest="sha256:request",
                output={"url": "https://example.test/result"},
            ),
        )
    )
    message_turn = SimpleNamespace(
        capture_id="capture-main",
        selector_revision_id="selector-main",
        status="ready",
        ok=True,
        artifacts=(),
        next_context=SimpleNamespace(receipt_id="context-main"),
        disclosure=SimpleNamespace(tokens=50, bytes=200),
    )

    result = application.observe(
        request=request,
        session_start=session_start(),
        action_run=action_run,
        message_turn=message_turn,
        start_event=start,
        application_events=(call_event, result_event),
        application_artifacts=(),
        stop_event=None,
        stop_error="",
        application_ok=True,
        application_error="",
        action_pending=False,
        runtime=SimpleNamespace(),
    )
    history = application.query(
        task_id=request.task_id,
        view="history",
    )
    trace = application.query(
        task_id=request.task_id,
        view="trace",
    )
    artifacts = application.query(
        task_id=request.task_id,
        view="artifacts",
    )
    reopened = BrowserObservabilityApplication(
        artifact_store=artifact_store,
    )
    replay = reopened.query(
        task_id=request.task_id,
        view="replay",
    )

    assert result.projection["default_route"] is True
    assert result.projection["fallback_allowed"] is False
    assert result.projection["recovery_handoff"]["recovery_planned_emitted"] is False
    assert len(result.artifacts) == 2
    assert all(
        event.event_type != EventType.RECOVERY_PLANNED
        for event in result.events
    )
    scope_history = history["scopes"][0]
    kinds = {item["kind"] for item in scope_history["records"]}
    assert str(HistoryKind.SESSION_STARTED) in kinds
    assert str(HistoryKind.TOOL_CALL) in kinds
    assert str(HistoryKind.TOOL_RESULT) in kinds
    assert str(HistoryKind.STATE_CAPTURE) in kinds
    assert str(HistoryKind.ARTIFACT_PUBLISHED) in kinds
    assert trace["scopes"][0]["tool_spans"] == 1
    assert artifacts["scopes"][0]["artifact_count"] == 2
    assert replay["scopes"][0]["complete"] is True
    assert replay["scopes"][0]["tool_pair_count"] == 1


def test_failed_action_projects_health_tool_failure_and_07c_input(
    tmp_path: Path,
) -> None:
    application = BrowserObservabilityApplication(
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    request = worker_request(tmp_path)
    start = EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.BROWSER_SESSION_LIFECYCLE,
        payload={"browser_session": {"phase": "session_attached"}},
    )
    action_run = SimpleNamespace(
        receipts=(
            Receipt(
                receipt_id="receipt-failed",
                action_id="action-failed",
                tool_call_id="tool-call-failed",
                action="click",
                ok=False,
                request_digest="sha256:request",
                error_code="target_detached",
                error_message="Target detached",
                retryable=True,
                outcome_unknown=False,
                side_effect_count=0,
            ),
        )
    )
    event = EventRecord(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "browser_result": {
                "tool_call_id": "tool-call-failed",
                "action_id": "action-failed",
                "ok": False,
            }
        },
    )

    result = application.observe(
        request=request,
        session_start=session_start(),
        action_run=action_run,
        message_turn=None,
        start_event=start,
        application_events=(event,),
        application_artifacts=(),
        stop_event=None,
        stop_error="",
        application_ok=False,
        application_error="target_detached",
        action_pending=False,
        runtime=SimpleNamespace(),
    )

    assert result.recovery_inputs
    assert result.recovery_inputs[0].to_dict()["planner_owner"] == "M1-07C"
    assert any(
        "browser_tool_failed" in item.payload
        for item in result.events
    )
    assert any(
        "browser_recovery_input" in item.payload
        for item in result.events
    )
    assert all(
        item.event_type != EventType.RECOVERY_PLANNED
        for item in result.events
    )


def test_real_chrome_process_kill_is_detected(
    tmp_path: Path,
) -> None:
    executable = _find_chrome()
    if executable is None:
        pytest.skip("Chrome/Chromium executable is not installed")
    user_data = tmp_path / "chrome-profile"
    process = subprocess.Popen(
        [
            str(executable),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-port=0",
            f"--user-data-dir={user_data}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    detector = BrowserCrashDetector(
        policy=CrashDetectorPolicy(
            heartbeat_timeout_ms=30_000,
            request_timeout_ms=30_000,
            disconnect_grace_ms=50,
            exit_debounce_ms=0,
        )
    )
    current = application_scope()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
            break
        if process.poll() is not None:
            pytest.skip("Chrome could not start in this environment")
        detector.attach(
            current,
            process_id=process.pid,
            cdp_connected=True,
        )
        process.kill()
        process.wait(timeout=10)

        signals = detector.poll(
            current,
            process_handle=process,
        )

        assert any(
            item.kind == SignalKind.PROCESS_EXITED
            and item.terminal
            for item in signals
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def application_scope() -> Any:
    from zyra_workers.browser_observability import ObservationScope

    return ObservationScope(
        run_id="run-real-chrome",
        task_id="task-real-chrome",
        node_id="node-real-chrome",
        browser_session_id="browser-real-chrome",
        canonical_session_id="canonical-real-chrome",
        worker_request_id="request-real-chrome",
    )


def _find_chrome() -> Path | None:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        shutil.which("chrome") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("msedge") or "",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next(
        (
            Path(value)
            for value in candidates
            if value and Path(value).is_file()
        ),
        None,
    )
