from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from zyra_integrations.browser_use import BrowserEventBus
from zyra_runtime import LocalArtifactStore
from zyra_workers.browser_action.download_guard import (
    BrowserDownloadGuard,
    DownloadEvent,
    DownloadState,
    RecordingDownloadControlPort,
)
from zyra_workers.browser_action.file_policy import (
    BrowserFilePolicy,
    FilePolicyConfig,
)
from zyra_workers.browser_observability import (
    BrowserHistoryStore,
    HistoryKind,
    ObservationScope,
)
from zyra_workers.browser_observability.history_store import (
    BrowserHistoryConflict,
)
from zyra_workers.browser_observability.integration import (
    AttachmentPhase,
    BrowserEventAttachment,
    BrowserReconnectExhausted,
    BrowserReconnectObserver,
    BrowserTrajectoryProjection,
    CommitPhase,
    EventAttachmentPolicy,
    EvidenceSource,
    EvidenceTerminalState,
    ObservationCommitConflict,
    ObservationCommitFence,
    RuntimeEvidenceEnvelope,
    RuntimeEvidenceMapper,
    RuntimeEvidenceMapperPolicy,
    RuntimeEvidenceMappingError,
)
from zyra_workers.browser_observability.integration.navigation import (
    BrowserNavigationSecurityRuntime,
    NavigationEventKind,
    NavigationObservation,
)
from zyra_workers.browser_observability.security_policy import (
    BrowserSecurityPolicyEngine,
    SecurityPolicy,
)
from zyra_workers.browser_session import (
    BrowserProfileCorrupt,
    BrowserProfileStore,
    BrowserSessionCommand,
)
from zyra_workers.browser_session.screenshot_capture import (
    HighlightFreeScreenshotCapture,
)


def scope() -> ObservationScope:
    return ObservationScope(
        run_id="run-04d-integration",
        task_id="task-04d-integration",
        node_id="node-browser",
        browser_session_id="browser-session-04d",
        canonical_session_id="canonical-session-04d",
        worker_request_id="worker-request-04d",
    )


def test_history_transaction_hides_and_repairs_uncommitted_tail(
    tmp_path: Path,
) -> None:
    store = BrowserHistoryStore(tmp_path / "history")
    current = scope()
    first = store.next_record(
        current,
        HistoryKind.SESSION_STARTED,
        {"name": "attached"},
    )
    committed = store.append_transaction(
        current,
        (first,),
        transaction_id="transaction-attached",
    )
    repeated = store.append_transaction(
        current,
        (first,),
        transaction_id="transaction-attached",
    )
    assert repeated.idempotent
    assert repeated.committed_head_digest == committed.committed_head_digest

    tail = store.next_record(
        current,
        HistoryKind.OBSERVATION,
        {"name": "must-not-be-visible"},
    )
    segment = next((tmp_path / "history").rglob("segment-*.jsonl"))
    with segment.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                tail.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )

    reopened = BrowserHistoryStore(tmp_path / "history")
    assert [item.record_id for item in reopened.records(current)] == [first.record_id]
    replacement = reopened.next_record(
        current,
        HistoryKind.OBSERVATION,
        {"name": "committed-after-repair"},
    )
    repaired = reopened.append_transaction(
        current,
        (replacement,),
        transaction_id="transaction-after-repair",
    )
    assert repaired.records[0].sequence == 2
    assert [item.payload["name"] for item in reopened.records(current)] == [
        "attached",
        "committed-after-repair",
    ]
    assert reopened.audit(current).ok


def test_history_transaction_rejects_identity_reuse_and_stale_head(
    tmp_path: Path,
) -> None:
    store = BrowserHistoryStore(tmp_path / "history")
    current = scope()
    record = store.next_record(current, HistoryKind.OBSERVATION, {"version": 1})
    store.append_transaction(current, (record,), transaction_id="stable-id")
    changed = store.next_record(current, HistoryKind.OBSERVATION, {"version": 2})
    with pytest.raises(BrowserHistoryConflict):
        store.append_transaction(current, (changed,), transaction_id="stable-id")
    with pytest.raises(BrowserHistoryConflict):
        store.append_transaction(
            current,
            (changed,),
            transaction_id="stale-head",
            expected_head_digest="sha256:stale",
        )


def test_commit_fence_requires_complete_event_and_checkpoint_acknowledgement(
    tmp_path: Path,
) -> None:
    fence = ObservationCommitFence(tmp_path / "commits")
    current = scope()
    receipt = fence.prepare(
        current,
        source_event_ids=("source-event",),
        action_receipt_ids=("action-receipt",),
        artifact_ids=("source-artifact",),
    )
    receipt = fence.advance(
        receipt,
        CommitPhase.HISTORY_COMMITTED,
        history_head_digest="sha256:head",
    )
    receipt = fence.advance(
        receipt,
        CommitPhase.ARTIFACTS_COMMITTED,
        artifact_ids=("artifact-one",),
    )
    receipt = fence.advance(
        receipt,
        CommitPhase.EVENTS_PENDING,
        event_ids=("event-one", "event-two"),
    )
    with pytest.raises(ObservationCommitConflict):
        fence.acknowledge_events(
            current,
            receipt.commit_id,
            committed_event_ids=("event-one",),
        )
    with pytest.raises(ObservationCommitConflict):
        fence.acknowledge_checkpoint(current, receipt.commit_id)

    events = fence.acknowledge_events(
        current,
        receipt.commit_id,
        committed_event_ids=("event-two", "event-one", "unrelated"),
    )
    terminal = fence.acknowledge_checkpoint(current, receipt.commit_id)
    assert events.phase == CommitPhase.EVENTS_COMMITTED
    assert terminal.phase == CommitPhase.CHECKPOINT_COMMITTED
    assert terminal.terminal
    assert fence.pending(scope=current) == ()
    assert (
        fence.acknowledge_checkpoint(current, receipt.commit_id).revision
        == terminal.revision
    )
    audit = fence.audit(scope=current)
    assert audit.ok
    assert audit.terminal_receipts == 1


def test_commit_fence_audit_exposes_corrupt_restart_receipt(
    tmp_path: Path,
) -> None:
    fence = ObservationCommitFence(tmp_path / "commits")
    current = scope()
    root = fence.root / current.task_id / current.key
    root.mkdir(parents=True)
    (root / "browser-observation-corrupt.json").write_text(
        "{broken",
        encoding="utf-8",
    )
    audit = fence.audit(scope=current)
    assert not audit.ok
    assert audit.scanned_files == 1
    assert audit.issues[0].code == "commit_receipt_corrupt"
    assert fence.projection(scope=current)["audit"]["ok"] is False


def test_commit_fence_restores_every_delivery_phase_after_restart(
    tmp_path: Path,
) -> None:
    root = tmp_path / "commits"
    current = scope()
    receipt = ObservationCommitFence(root).prepare(
        current,
        source_event_ids=("source-event",),
        action_receipt_ids=("action-receipt",),
        artifact_ids=("source-artifact",),
    )

    def restart(expected: CommitPhase) -> ObservationCommitFence:
        reopened = ObservationCommitFence(root)
        restored = reopened.get(current, receipt.commit_id)
        assert restored is not None
        assert restored.phase == expected
        assert reopened.audit(scope=current).ok
        if expected == CommitPhase.CHECKPOINT_COMMITTED:
            assert reopened.pending(scope=current) == ()
        else:
            assert [item.commit_id for item in reopened.pending(scope=current)] == [
                receipt.commit_id
            ]
        return reopened

    fence = restart(CommitPhase.PREPARED)
    receipt = fence.advance(
        receipt,
        CommitPhase.HISTORY_COMMITTED,
        history_head_digest="sha256:history-head",
    )
    fence = restart(CommitPhase.HISTORY_COMMITTED)
    receipt = fence.advance(
        receipt,
        CommitPhase.ARTIFACTS_COMMITTED,
        artifact_ids=("artifact-one",),
    )
    fence = restart(CommitPhase.ARTIFACTS_COMMITTED)
    receipt = fence.advance(
        receipt,
        CommitPhase.EVENTS_PENDING,
        event_ids=("event-one",),
    )
    fence = restart(CommitPhase.EVENTS_PENDING)
    receipt = fence.acknowledge_events(
        current,
        receipt.commit_id,
        committed_event_ids=("event-one",),
    )
    fence = restart(CommitPhase.EVENTS_COMMITTED)
    receipt = fence.acknowledge_checkpoint(current, receipt.commit_id)
    restart(CommitPhase.CHECKPOINT_COMMITTED)
    assert receipt.revision == 5


def test_event_attachment_observes_existing_bus_and_tears_down() -> None:
    bus = BrowserEventBus()
    bus.start()
    attached = BrowserEventAttachment(scope(), bus)
    try:
        start = attached.attach()
        event = bus.publish(
            "browser.cdp.request_timeout",
            {"request_id": "cdp-request", "method": "Page.navigate"},
        )
        deadline = time.monotonic() + 2
        while not attached.events() and time.monotonic() < deadline:
            time.sleep(0.005)
        values = attached.events()
        stopped = attached.detach(drain=True)
        assert start.phase == AttachmentPhase.ATTACHED
        assert [item.bus_event_id for item in values] == [event.event_id]
        assert values[0].stale is False
        assert stopped.phase == AttachmentPhase.DETACHED
        assert stopped.drained
        assert bus.snapshot().subscriptions == 0
    finally:
        bus.stop()


def test_event_attachment_rejects_stale_generation_after_bus_restart() -> None:
    current = scope()

    class FakeBus:
        def __init__(self) -> None:
            self.callback = None

        def snapshot(self) -> object:
            return SimpleNamespace(state="running", generation=2)

        def subscribe(self, callback: object, **_kwargs: object) -> object:
            self.callback = callback
            return SimpleNamespace(subscription_id="subscription")

        def unsubscribe(self, _subscription_id: str) -> bool:
            return True

    bus = FakeBus()
    attachment = BrowserEventAttachment(current, bus)
    attachment.attach()
    assert bus.callback is not None
    bus.callback(
        SimpleNamespace(
            generation=1,
            topic="browser.cdp.event",
            payload={"method": "Target.targetCreated"},
            event_id="stale-event",
            sequence=1,
            source="fixture",
        )
    )
    receipt = attachment.detach()
    assert receipt.accepted_events == 0
    assert receipt.stale_events == 1
    assert attachment.events()[0].stale is True


def test_reconnect_exhaustion_preserves_attempt_evidence_without_planning() -> None:
    observer = BrowserReconnectObserver(
        policy=EventAttachmentPolicy(
            reconnect_attempts=3,
            reconnect_backoff_seconds=(),
        ),
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(BrowserReconnectExhausted) as raised:
        observer.observe(scope(), lambda: False, reason="websocket-closed")
    receipt = raised.value.receipt
    assert receipt.exhausted
    assert len(receipt.attempts) == 3
    assert all(not item.ok for item in receipt.attempts)
    assert receipt.to_dict()["recovery_planner_owner"] == "M1-07C"


def test_runtime_evidence_mapper_handles_partial_failure_and_worktree_lists() -> None:
    current = scope()
    mapper = RuntimeEvidenceMapper()
    partial = RuntimeEvidenceEnvelope(
        scope=current,
        source=EvidenceSource.PROVIDER_STREAM,
        event_type="content_delta",
        payload={"delta_chars": 10},
        provider_request_id="provider-request",
        partial_sequence=1,
    )
    terminal = RuntimeEvidenceEnvelope(
        scope=current,
        source=EvidenceSource.PROVIDER_STREAM,
        event_type="stream_interrupted",
        payload={"error": "provider disconnected"},
        provider_request_id="provider-request",
        partial_sequence=2,
        terminal_state=EvidenceTerminalState.INTERRUPTED,
        retryable=True,
        outcome_unknown=True,
    )
    output = mapper.map(current, (partial, terminal))
    assert len(output.spans) == 1
    assert len(output.signals) == 1
    assert output.signals[0].metadata["outcome_unknown"] is True
    assert all(event.event_type.value != "recovery_planned" for event in output.events)

    worktree = RuntimeEvidenceEnvelope(
        scope=current,
        source=EvidenceSource.WORKTREE,
        event_type="merge_conflict",
        payload={"conflict_paths": ["a.py", "b.py"], "error": "conflict"},
        worktree_id="worktree-one",
        branch="feature/test",
        base_sha="deadbeef",
        terminal_state=EvidenceTerminalState.CONFLICT,
    )
    conflict = mapper.map(current, (worktree,))
    assert conflict.spans[0].attributes["conflict_paths"] == ["a.py", "b.py"]


def test_runtime_evidence_mapper_disable_does_not_claim_browser_ownership() -> None:
    mapper = RuntimeEvidenceMapper(
        policy=RuntimeEvidenceMapperPolicy(enabled=False)
    )
    projection = mapper.map(scope(), ()).projection["runtime_evidence_mapper"]
    assert projection["enabled"] is False
    assert projection["canonical_browser_path_affected"] is False
    with pytest.raises(RuntimeEvidenceMappingError):
        RuntimeEvidenceMapper().map(
            scope(),
            (
                RuntimeEvidenceEnvelope(
                    scope=scope(),
                    source=EvidenceSource.PROVIDER_STREAM,
                    event_type="content_delta",
                    payload={},
                    provider_request_id="provider-gap",
                    partial_sequence=2,
                ),
            ),
        )


def test_highlight_free_screenshot_restores_page_after_success_and_failure() -> None:
    calls: list[str] = []
    image = b"\x89PNG\r\n\x1a\nfixture"

    def send(method: str, _params: object) -> dict[str, object]:
        calls.append(method)
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(image).decode("ascii")}
        restored = calls.count("Runtime.evaluate") > 1
        return {"result": {"value": {"ok": True, "matched": 2, "hidden": 1, "attributes": 1} if not restored else {"ok": True}}}

    result = HighlightFreeScreenshotCapture().capture(
        send,
        image_format="png",
        capture_beyond_viewport=True,
    )
    assert calls == ["Runtime.evaluate", "Page.captureScreenshot", "Runtime.evaluate"]
    assert result.content == image
    assert result.highlight_removed and result.highlight_restored

    failed_calls: list[str] = []

    def failing(method: str, _params: object) -> dict[str, object]:
        failed_calls.append(method)
        if method == "Page.captureScreenshot":
            raise RuntimeError("capture failed")
        return {"result": {"value": {"ok": True, "matched": 1}}}

    with pytest.raises(RuntimeError, match="capture failed"):
        HighlightFreeScreenshotCapture().capture(
            failing,
            image_format="png",
            capture_beyond_viewport=False,
        )
    assert failed_calls == ["Runtime.evaluate", "Page.captureScreenshot", "Runtime.evaluate"]


def test_profile_storage_state_recovers_primary_from_backup(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command = BrowserSessionCommand(
        run_id="run-profile",
        task_id="task-profile",
        worker_request_id="request-profile",
        canonical_session_id="canonical-profile",
        workspace_root=workspace,
        artifact_root=tmp_path / "artifacts",
        endpoint_url="http://127.0.0.1:9222",
    )
    store = BrowserProfileStore(tmp_path / "runtime", tmp_path / "state")
    profile = store.prepare(command, "session-profile").profile
    store.save_storage_state_with_receipt(
        profile.profile_id,
        {"cookies": [{"name": "one", "value": "1"}], "origins": []},
    )
    second = store.save_storage_state_with_receipt(
        profile.profile_id,
        {"cookies": [{"name": "two", "value": "2"}], "origins": []},
    )
    Path(second.state_path).write_text("{broken", encoding="utf-8")
    restored, receipt = store.load_storage_state_with_receipt(profile.profile_id)
    assert restored["cookies"][0]["name"] == "one"
    assert receipt.source == "backup"
    assert receipt.restored_primary
    assert receipt.temporary_removed

    Path(second.state_path).write_text("{broken", encoding="utf-8")
    (profile.state_dir / "storage-state.backup.json").write_text(
        "{also-broken",
        encoding="utf-8",
    )
    with pytest.raises(BrowserProfileCorrupt):
        store.load_storage_state(profile.profile_id)


def test_download_abort_cancels_native_transfer_and_removes_quarantine(
    tmp_path: Path,
) -> None:
    upload_root = tmp_path / "workspace"
    upload_root.mkdir()
    file_policy = BrowserFilePolicy(
        FilePolicyConfig(
            upload_roots=(str(upload_root),),
            download_root=str(tmp_path / "downloads"),
            artifact_root=str(tmp_path / "artifacts"),
            max_download_bytes=1_024,
        )
    )
    receipt = file_policy.preflight_destination(
        action_id="action-download",
        filename="report.txt",
        expected_bytes=100,
    )
    control = RecordingDownloadControlPort()
    guard = BrowserDownloadGuard(
        file_policy=file_policy,
        control_port=control,
        quarantine_parent=tmp_path / "quarantine",
    )
    lease = guard.arm(
        action_id="action-download",
        receipt=receipt,
        browser_context_id="browser-context",
        consumed_permission_tool_use_id="tool-use-download",
    )
    guard.on_will_begin(
        lease,
        DownloadEvent(
            guid="download-guid",
            state=DownloadState.STARTED,
            suggested_filename="report.txt",
            total_bytes=100,
        ),
    )
    partial = Path(lease.quarantine_root) / "download-guid.crdownload"
    partial.write_bytes(b"partial")
    aborted = guard.abort(lease, reason="worker_stopping")
    assert aborted.cancelled_guids == ("download-guid",)
    assert aborted.control_disarmed
    assert aborted.cleanup_complete
    assert not Path(lease.quarantine_root).exists()
    assert guard.snapshot()["active_progress"] == []
    assert [item["operation"] for item in control.operations] == [
        "arm",
        "cancel",
        "disarm",
    ]


def test_denied_navigation_closes_the_exact_target_without_planning() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class ClosePort:
        def send(
            self,
            method: str,
            params: object = None,
            **_kwargs: object,
        ) -> dict[str, object]:
            calls.append((method, dict(params or {})))
            return {"success": True}

    runtime = BrowserNavigationSecurityRuntime(
        BrowserSecurityPolicyEngine(
            policy=SecurityPolicy(require_dns_resolution=False),
            resolver=lambda _host: (),
        )
    )
    observation = NavigationObservation(
        scope=scope(),
        kind=NavigationEventKind.TARGET_CREATED,
        url="file:///C:/sensitive.txt",
        target_id="forbidden-target",
        source_event_id="target-created-event",
    )
    receipt = runtime.evaluate_and_close(observation, ClosePort())
    assert not receipt.verdict.allowed
    assert receipt.close_attempted and receipt.closed
    assert calls == [("Target.closeTarget", {"targetId": "forbidden-target"})]
    assert receipt.to_dict()["policy_owner"] == "M1-04C/RedirectGuard"


def test_trajectory_projection_uses_committed_head_cursor(
    tmp_path: Path,
) -> None:
    store = BrowserHistoryStore(tmp_path / "history")
    current = scope()
    first = store.next_record(
        current,
        HistoryKind.OBSERVATION,
        {"url": "https://example.test"},
        artifact_ids=("artifact-browser-state",),
    )
    store.append_transaction(current, (first,), transaction_id="trajectory-one")
    projection = BrowserTrajectoryProjection(store)
    snapshot = projection.snapshot(current)
    assert snapshot.cursor.head_digest == first.content_digest
    assert snapshot.artifact_ids == ("artifact-browser-state",)
    assert snapshot.events[0]["payload"]["history_record_id"] == first.record_id

    second = store.next_record(current, HistoryKind.OBSERVATION, {"url": "done"})
    store.append_transaction(current, (second,), transaction_id="trajectory-two")
    with pytest.raises(BrowserHistoryConflict):
        projection.snapshot(
            current,
            expected_head_digest=snapshot.cursor.head_digest,
        )


def test_local_artifact_store_leaves_no_atomic_temporary_file(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    artifact = store.write_text(
        run_id="run-artifact",
        task_id="task-artifact",
        title="manifest",
        content=json.dumps({"records": 2}),
        extension=".json",
    )
    assert Path(artifact.uri).read_text(encoding="utf-8") == '{"records": 2}'
    assert not tuple((tmp_path / "artifacts").rglob(".*.tmp"))
