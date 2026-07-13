from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from zyra_core import EventRecord, EventType
from zyra_runtime import LocalArtifactStore
from zyra_workers.browser_observability import (
    AboutBlankWatchdog,
    BrowserCrashDetector,
    BrowserFailureProjector,
    BrowserHistoryReplay,
    BrowserHistoryStore,
    BrowserHealthRuntime,
    BrowserObservation,
    BrowserObservabilityApplication,
    BrowserObservabilityDisabled,
    BrowserObservabilitySourceAuditor,
    BrowserScreenshotRuntime,
    BrowserSecurityPolicyEngine,
    BrowserWatchdogRegistry,
    CrashDetectorPolicy,
    HealthStatus,
    HistoryKind,
    ObservationScope,
    PermissionPolicy,
    RecoveryReason,
    SecurityDecision,
    SecurityPolicy,
    Severity,
    SignalKind,
    WatchdogName,
    WatchdogPolicy,
    WatchdogSignal,
)
from zyra_workers.browser_observability.blank_runtime import (
    AboutBlankRuntime,
    BlankPolicy,
    BlankState,
)
from zyra_workers.browser_observability.download_runtime import (
    BrowserDownloadRuntime,
    DownloadPolicy,
    DownloadRisk,
    DownloadState,
)
from zyra_workers.browser_observability.permission_runtime import (
    BrowserPermissionRuntime,
)
from zyra_workers.browser_observability.popup_runtime import (
    BrowserPopupRuntime,
    PopupPolicy,
    PopupState,
)
from zyra_workers.browser_observability.screenshot_runtime import (
    ScreenshotFormat,
)
from zyra_workers.browser_observability.storage_runtime import (
    BrowserStorageStateRuntime,
)


def scope() -> ObservationScope:
    return ObservationScope(
        run_id="run-observe",
        task_id="task-observe",
        node_id="node-observe",
        browser_session_id="browser-session-observe",
        canonical_session_id="canonical-observe",
        worker_request_id="request-observe",
    )


def observation(**overrides: object) -> BrowserObservation:
    values = {
        "scope": scope(),
        "sequence": 1,
        "monotonic_ms": 20_000,
        "process_running": True,
        "cdp_connected": True,
        "current_url": "https://example.test/",
    }
    values.update(overrides)
    return BrowserObservation(**values)


def test_history_store_persists_hash_chain_and_replays_tool_pairs(
    tmp_path: Path,
) -> None:
    store = BrowserHistoryStore(tmp_path / "history")
    current = scope()
    started, _ = store.record(
        current,
        HistoryKind.SESSION_STARTED,
        {"name": "session", "ok": True},
    )
    call, _ = store.record(
        current,
        HistoryKind.TOOL_CALL,
        {
            "name": "click",
            "tool_name": "click",
            "input": {"selector": "#submit"},
        },
        tool_call_id="tool-call-click",
        parent_record_id=started.record_id,
    )
    result, _ = store.record(
        current,
        HistoryKind.TOOL_RESULT,
        {
            "name": "click",
            "tool_name": "click",
            "ok": True,
            "output": {"url": "https://example.test/done"},
        },
        tool_call_id="tool-call-click",
        parent_record_id=call.record_id,
    )

    reopened = BrowserHistoryStore(tmp_path / "history")
    records = reopened.records(current)
    audit = reopened.audit(current)
    replay = BrowserHistoryReplay(reopened).replay(current)

    assert [item.sequence for item in records] == [1, 2, 3]
    assert records[1].previous_digest == records[0].content_digest
    assert records[2].previous_digest == records[1].content_digest
    assert audit.ok
    assert replay.complete
    assert len(replay.tool_pairs) == 1
    assert replay.tool_pairs[0].tool_call_id == "tool-call-click"
    assert replay.tool_pairs[0].ok is True
    assert result.content_digest == replay.head_digest


def test_history_store_detects_tampered_record(
    tmp_path: Path,
) -> None:
    store = BrowserHistoryStore(tmp_path / "history")
    current = scope()
    store.record(
        current,
        HistoryKind.SESSION_STARTED,
        {"name": "session", "ok": True},
    )
    segment = next((tmp_path / "history").rglob("segment-*.jsonl"))
    text = segment.read_text(encoding="utf-8")
    segment.write_text(
        text.replace('"ok":true', '"ok":false'),
        encoding="utf-8",
    )

    audit = store.audit(current)

    assert not audit.ok
    assert any(item.code == "history_content_tampered" for item in audit.issues)


def test_history_store_rejects_sequence_conflict(
    tmp_path: Path,
) -> None:
    store = BrowserHistoryStore(tmp_path / "history")
    current = scope()
    first = store.next_record(
        current,
        HistoryKind.SESSION_STARTED,
        {"name": "session"},
    )
    store.append(first)
    stale = store.next_record(
        current,
        HistoryKind.OBSERVATION,
        {"name": "stale"},
    )
    newer = store.next_record(
        current,
        HistoryKind.OBSERVATION,
        {"name": "newer"},
    )
    store.append(newer)

    with pytest.raises(RuntimeError):
        store.append(stale)


def test_crash_detector_observes_real_process_exit() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    detector = BrowserCrashDetector(
        policy=CrashDetectorPolicy(
            heartbeat_timeout_ms=10_000,
            request_timeout_ms=10_000,
            disconnect_grace_ms=10,
            exit_debounce_ms=0,
        )
    )
    current = scope()
    try:
        detector.attach(
            current,
            process_id=process.pid,
            cdp_connected=True,
        )
        assert detector.poll(current, process_handle=process) == ()
        process.kill()
        process.wait(timeout=5)

        signals = detector.poll(current, process_handle=process)

        assert any(item.kind == SignalKind.PROCESS_EXITED for item in signals)
        signal = next(
            item
            for item in signals
            if item.kind == SignalKind.PROCESS_EXITED
        )
        assert signal.status == HealthStatus.TERMINATED
        assert signal.terminal
        assert signal.metadata["process_id"] == process.pid
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_crash_detector_confirms_cdp_disconnect_after_grace() -> None:
    clock = [1_000]
    detector = BrowserCrashDetector(
        policy=CrashDetectorPolicy(
            heartbeat_timeout_ms=20_000,
            request_timeout_ms=20_000,
            disconnect_grace_ms=100,
            exit_debounce_ms=0,
        ),
        monotonic_ms=lambda: clock[0],
    )
    current = scope()
    detector.attach(current, cdp_connected=True)
    suspected = detector.cdp_disconnected(
        current,
        reason="websocket_close_1006",
        at_ms=1_010,
    )
    assert suspected[0].status == HealthStatus.DEGRADED
    assert not suspected[0].terminal

    signals = detector.poll(current, at_ms=1_111)

    assert any(
        item.kind == SignalKind.CDP_DISCONNECTED
        and item.status == HealthStatus.UNHEALTHY
        and item.terminal
        for item in signals
    )


def test_crash_detector_emits_silent_heartbeat_timeout() -> None:
    detector = BrowserCrashDetector(
        policy=CrashDetectorPolicy(
            heartbeat_timeout_ms=100,
            request_timeout_ms=500,
            disconnect_grace_ms=500,
            exit_debounce_ms=0,
        ),
        monotonic_ms=lambda: 0,
    )
    current = scope()
    detector.attach(current, cdp_connected=True)
    detector.heartbeat(current, at_ms=10)

    signals = detector.poll(current, at_ms=111)

    assert [item.kind for item in signals] == [SignalKind.HEARTBEAT_LATE]
    assert signals[0].metadata["elapsed_ms"] == 101
    assert signals[0].retryable


def test_crash_detector_emits_request_timeout_with_unknown_outcome() -> None:
    detector = BrowserCrashDetector(
        policy=CrashDetectorPolicy(
            heartbeat_timeout_ms=1_000,
            request_timeout_ms=100,
            disconnect_grace_ms=1_000,
            exit_debounce_ms=0,
        ),
        monotonic_ms=lambda: 0,
    )
    current = scope()
    detector.attach(current, cdp_connected=True)
    detector.request_started(current, "cdp-request-1", at_ms=10)

    signals = detector.poll(current, at_ms=111)

    assert [item.kind for item in signals] == [SignalKind.REQUEST_TIMEOUT]
    assert signals[0].metadata["outcome_unknown"] is True
    assert signals[0].terminal


def test_crash_detector_suppresses_intentional_stop() -> None:
    detector = BrowserCrashDetector(
        policy=CrashDetectorPolicy(
            heartbeat_timeout_ms=100,
            request_timeout_ms=100,
            disconnect_grace_ms=0,
            exit_debounce_ms=0,
        ),
        monotonic_ms=lambda: 100,
    )
    current = scope()
    detector.attach(current, process_id=999_999_999, cdp_connected=True)
    detector.intentional_stop(current, at_ms=100)
    detector.cdp_disconnected(current, reason="stop", at_ms=101)

    assert detector.poll(current, at_ms=1_000) == ()


def test_failure_projection_never_claims_recovery_plan() -> None:
    current = scope()
    signal = WatchdogSignal(
        scope=current,
        watchdog=WatchdogName.CRASH_DETECTOR,
        kind=SignalKind.PROCESS_EXITED,
        status=HealthStatus.TERMINATED,
        severity=Severity.CRITICAL,
        summary="Chrome exited.",
        retryable=True,
        terminal=True,
    )

    projected = BrowserFailureProjector().project(
        current,
        (signal,),
        action_error="browser_process_exited",
        failed_tool_call_ids=("tool-call-1",),
        failed_receipt_ids=("receipt-1",),
    )

    assert projected.recovery_inputs[0].reason == RecoveryReason.BROWSER_PROCESS_EXIT
    assert projected.recovery_inputs[0].to_dict()["planner_owner"] == "M1-07C"
    assert all(
        item.event_type != EventType.RECOVERY_PLANNED
        for item in projected.events
    )
    assert any(
        "browser_recovery_input" in item.payload
        for item in projected.events
    )
    assert any(
        item.event_type == EventType.WORKER_HEALTH
        for item in projected.events
    )


def test_health_runtime_aggregates_terminal_signal_and_rebuilds() -> None:
    current = scope()
    degraded = WatchdogSignal(
        scope=current,
        watchdog=WatchdogName.SECURITY,
        kind=SignalKind.SECURITY_DEGRADED,
        status=HealthStatus.DEGRADED,
        severity=Severity.WARNING,
        summary="Cross-origin redirect requires audit.",
        sequence=1,
    )
    terminated = WatchdogSignal(
        scope=current,
        watchdog=WatchdogName.CRASH_DETECTOR,
        kind=SignalKind.PROCESS_EXITED,
        status=HealthStatus.TERMINATED,
        severity=Severity.CRITICAL,
        summary="Chrome exited.",
        sequence=2,
        terminal=True,
        retryable=True,
    )
    runtime = BrowserHealthRuntime()

    first = runtime.ingest(current, (degraded,))
    terminal = runtime.ingest(current, (terminated,))
    rebuilt = runtime.rebuild(current, (terminated, degraded))

    assert first.status == HealthStatus.DEGRADED
    assert terminal.status == HealthStatus.TERMINATED
    assert terminal.terminal_signal_id == terminated.signal_id
    assert terminal.recovery_candidate_signal_ids == (terminated.signal_id,)
    assert rebuilt.status == HealthStatus.TERMINATED
    assert rebuilt.counters[str(SignalKind.PROCESS_EXITED)] == 1
    assert runtime.projection(scope=current)["terminated"] == 1


def test_attached_watchdog_registry_has_exact_maturity_set() -> None:
    registry = BrowserWatchdogRegistry(
        policy=WatchdogPolicy(
            screenshot_required_after_terminal_action=True,
            about_blank_grace_ms=100,
            storage_persist_grace_ms=100,
            maximum_popups=1,
        )
    )
    current = observation(
        process_running=False,
        cdp_connected=False,
        current_url="about:blank",
        popup_target_ids=("popup-1", "popup-2"),
        permissions_expected=("clipboard-read",),
        permissions_reported=(),
        storage_dirty=True,
        screenshot_artifact_id="",
        action_terminal=True,
        action_ok=False,
        action_error="failed",
        metadata={
            "about_blank_expected": False,
            "about_blank_since_ms": 1,
            "storage_dirty_since_ms": 1,
            "security_verdict": {
                "decision": "deny",
                "summary": "blocked",
            },
        },
    )

    signals = registry.evaluate(current)
    snapshot = registry.snapshot()

    assert snapshot["active"] == [
        "local_browser",
        "security",
        "downloads",
        "storage_state",
        "permissions",
        "screenshot",
        "popups",
        "about_blank",
    ]
    assert snapshot["crash_detector"]["active"] is True
    assert snapshot["crash_detector"]["upstream_crash_watchdog_attached"] is False
    kinds = {item.kind for item in signals}
    assert SignalKind.PROCESS_EXITED in kinds
    assert SignalKind.SECURITY_BLOCK in kinds
    assert SignalKind.STORAGE_FAILED in kinds
    assert SignalKind.PERMISSION_DRIFT in kinds
    assert SignalKind.SCREENSHOT_MISSING in kinds
    assert SignalKind.POPUP_QUARANTINED in kinds
    assert SignalKind.ABOUT_BLANK_STALLED in kinds


def test_security_policy_blocks_private_address_and_dns_rebind() -> None:
    addresses = {
        "private.test": ("10.1.2.3",),
        "public.test": ("8.8.8.8",),
    }
    engine = BrowserSecurityPolicyEngine(
        policy=SecurityPolicy(
            require_dns_resolution=True,
            allow_private_network=False,
            pin_dns_for_chain=True,
        ),
        resolver=lambda host: addresses[host],
    )

    denied = engine.evaluate("https://private.test/path")
    allowed = engine.evaluate(
        "https://public.test/start",
        chain_id="chain-1",
    )
    addresses["public.test"] = ("1.1.1.1",)
    rebound = engine.evaluate(
        "https://public.test/next",
        previous_url="https://public.test/start",
        redirect_index=1,
        chain_id="chain-1",
    )

    assert denied.decision == SecurityDecision.DENY
    assert allowed.allowed
    assert rebound.decision == SecurityDecision.DENY
    assert str(rebound.reason) == "dns_rebind"


def test_download_runtime_quarantines_executable_and_prevents_publish(
    tmp_path: Path,
) -> None:
    runtime = BrowserDownloadRuntime(
        tmp_path / "downloads",
        policy=DownloadPolicy(
            max_file_bytes=1_000,
            max_session_bytes=2_000,
        ),
    )
    item = runtime.begin(
        browser_session_id="session",
        suggested_name="../../payload.exe",
        source_url="https://example.test/payload.exe",
        expected_bytes=4,
    )
    runtime.receive(item.download_id, b"MZ00")
    completed = runtime.complete(item.download_id)

    assert completed.state == DownloadState.QUARANTINED
    assert DownloadRisk.EXECUTABLE in completed.risks
    assert DownloadRisk.PATH_TRAVERSAL in completed.risks
    with pytest.raises(RuntimeError):
        runtime.publish(completed.download_id, "artifact-download")


def test_storage_runtime_redacts_secret_values_and_tracks_delta(
    tmp_path: Path,
) -> None:
    runtime = BrowserStorageStateRuntime(tmp_path / "storage")
    first, first_delta = runtime.capture(
        "session",
        {
            "cookies": [
                {
                    "domain": "example.test",
                    "path": "/",
                    "name": "session",
                    "value": "top-secret",
                }
            ],
            "origins": [
                {
                    "origin": "https://example.test",
                    "localStorage": [
                        {"name": "api_token", "value": "token-value"}
                    ],
                }
            ],
        },
    )
    path = runtime.persist(first)
    second, delta = runtime.capture(
        "session",
        {
            "cookies": [],
            "origins": [],
        },
    )

    assert first.redaction_count == 2
    assert "top-secret" not in path.read_text(encoding="utf-8")
    assert first_delta.dirty
    assert delta.removed_cookie_keys
    assert delta.removed_origins
    assert second.revision == 2


def test_permission_runtime_is_bound_to_existing_decision_identity() -> None:
    runtime = BrowserPermissionRuntime(
        policy=PermissionPolicy(max_grant_seconds=60)
    )
    grant = runtime.mirror_grant(
        browser_session_id="session",
        permission="clipboard-read",
        origin="https://example.test",
        source_permission_decision_id="permission-decision",
        source_tool_use_id="tool-use",
        ttl_seconds=30,
    )
    drift = runtime.audit(
        browser_session_id="session",
        origin="https://example.test",
        reported_permissions=(),
    )
    revoked = runtime.revoke_for_origin_change(
        browser_session_id="session",
        new_origin="https://other.test",
    )

    assert grant.active
    assert drift.missing == ("clipboard-read",)
    assert revoked[0].grant_id == grant.grant_id
    assert runtime.projection(browser_session_id="session")["decision_owner"] == "M1-03A"


def test_popup_runtime_quarantines_untrusted_openerless_target() -> None:
    runtime = BrowserPopupRuntime(
        policy=PopupPolicy(
            max_open_popups=2,
            close_untrusted_popups=True,
            allow_openerless=False,
        )
    )

    popup = runtime.observed(
        target_id="popup",
        opener_target_id="",
        url="https://ads.test",
        origin="https://ads.test",
    )

    assert popup.state == PopupState.QUARANTINED
    with pytest.raises(RuntimeError):
        runtime.focused("popup")


def test_about_blank_runtime_distinguishes_expected_grace_and_stall() -> None:
    runtime = AboutBlankRuntime(
        policy=BlankPolicy(
            startup_grace_ms=100,
            navigation_grace_ms=200,
            allow_new_tab_blank_ms=50,
        )
    )
    created = runtime.target_created(
        "target",
        url="about:blank",
        at_ms=100,
    )
    within = runtime.poll("target", at_ms=150)
    stalled = runtime.poll("target", at_ms=201)

    assert created.state == BlankState.EXPECTED
    assert within.state == BlankState.EXPECTED
    assert stalled.state == BlankState.STALLED


def test_screenshot_runtime_validates_png_and_deduplicates() -> None:
    runtime = BrowserScreenshotRuntime()
    png = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0dIHDR"
        + (2).to_bytes(4, "big")
        + (3).to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )

    first = runtime.inspect_bytes(
        png,
        artifact_id="screenshot-1",
        action_id="action-1",
    )
    second = runtime.inspect_bytes(
        png,
        artifact_id="screenshot-2",
        action_id="action-2",
    )

    assert first.format == ScreenshotFormat.PNG
    assert (first.width, first.height) == (2, 3)
    assert second.duplicate_of == "screenshot-1"


def test_observability_disabled_fails_closed(
    tmp_path: Path,
) -> None:
    application = BrowserObservabilityApplication(
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        disabled=True,
    )

    with pytest.raises(BrowserObservabilityDisabled):
        application.observe(
            request=None,
            session_start=None,
            action_run=None,
            message_turn=None,
            start_event=None,
            application_events=(),
            application_artifacts=(),
            stop_event=None,
            stop_error="",
            application_ok=False,
            application_error="disabled",
            action_pending=False,
        )


def test_source_audit_has_one_primary_two_supplements_and_no_root_imports() -> None:
    package_root = (
        Path(__file__).parents[2]
        / "packages"
        / "workers"
        / "zyra_workers"
        / "browser_observability"
    )

    report = BrowserObservabilitySourceAuditor(package_root).audit()

    assert report.ok, report.to_dict()
    primary = {
        item.repository
        for item in report.decisions
        if str(item.role) == "primary_implementation"
    }
    supplementary = {
        item.repository
        for item in report.decisions
        if str(item.role) == "supplementary_implementation"
    }
    assert primary == {"browser-use"}
    assert supplementary == {"OpenHands", "oh-my-pi"}
    crash = next(
        item
        for item in report.decisions
        if any(path.endswith("crash_watchdog.py") for path in item.source_paths)
    )
    assert str(crash.role) == "rejected"
    assert crash.runtime_required is False
