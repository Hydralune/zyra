from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zyra_core import ArtifactRef, EventRecord, EventType, to_jsonable
from zyra_runtime import LocalArtifactStore, WorkerRequest

from ..failure_projection import BrowserFailureProjector
from ..history_runtime import BrowserHistoryRuntime
from ..models import (
    HealthStatus,
    HistoryKind,
    ObservationScope,
    Severity,
    SignalKind,
    WatchdogName,
    WatchdogSignal,
    digest_value,
    new_observation_id,
)
from ..security_policy import BrowserSecurityPolicyEngine, SecurityPolicy
from .commit_fence import ObservationCommitFence
from .contracts import (
    BrowserIntegrationOutput,
    CommitPhase,
    EvidenceSource,
    RuntimeEvidenceEnvelope,
)
from .downloads import BrowserDownloadEvidenceRuntime
from .event_bus import (
    AttachedBrowserEvent,
    AttachedEventKind,
    BrowserEventAttachment,
    EventAttachmentPolicy,
)
from .navigation import BrowserNavigationSecurityRuntime
from .runtime_evidence import RuntimeEvidenceMapper, RuntimeEvidenceMapperPolicy
from .screenshots import BrowserScreenshotEvidenceRuntime
from .storage import BrowserStorageIntegrationRuntime
from .trajectory import BrowserTrajectoryProjection


class BrowserActiveIntegrationError(RuntimeError):
    code = "browser_active_observability_integration_error"


@dataclass(frozen=True, slots=True)
class ActiveIntegrationPolicy:
    enabled: bool = True
    require_event_bus: bool = True
    attach_before_action: bool = True
    fail_closed: bool = True
    enable_external_evidence_mapper: bool = True
    capture_storage_when_dirty: bool = True


@dataclass(slots=True)
class _ActiveSession:
    scope: ObservationScope
    runtime: Any
    cdp: Any
    event_attachment: BrowserEventAttachment
    commit: Any
    start_event: EventRecord
    session_start: Any
    profile_id: str = ""
    integration_outputs: list[BrowserIntegrationOutput] = field(default_factory=list)
    finalized: bool = False
    before_stop_completed: bool = False


class BrowserObservabilityIntegrationRuntime:
    """Active 04D bridge over the existing 04A/04C productized runtime."""

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStore,
        state_root: str | Path,
        history: BrowserHistoryRuntime,
        failure_projector: BrowserFailureProjector,
        policy: ActiveIntegrationPolicy | None = None,
        external_mapper_policy: RuntimeEvidenceMapperPolicy | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.history = history
        self.failure_projector = failure_projector
        self.policy = policy or ActiveIntegrationPolicy()
        self.downloads = BrowserDownloadEvidenceRuntime(artifact_store)
        self.screenshots = BrowserScreenshotEvidenceRuntime(artifact_store)
        self.external_mapper = RuntimeEvidenceMapper(
            policy=external_mapper_policy
            or RuntimeEvidenceMapperPolicy(enabled=self.policy.enable_external_evidence_mapper)
        )
        self.commit_fence = ObservationCommitFence(self.state_root / "commits")
        self.trajectory = BrowserTrajectoryProjection(self.history.store)
        self._storage_by_runtime: dict[int, BrowserStorageIntegrationRuntime] = {}
        self._navigation_by_scope: dict[str, BrowserNavigationSecurityRuntime] = {}
        self._active: dict[str, _ActiveSession] = {}
        self._completed = 0
        self._failures = 0
        self._guard = threading.RLock()

    def attach(
        self,
        *,
        request: WorkerRequest,
        session_start: Any,
        start_event: EventRecord,
        runtime: Any,
        allow_passive: bool = False,
    ) -> dict[str, Any]:
        if not self.policy.enabled:
            raise BrowserActiveIntegrationError(
                "active browser observability integration is disabled"
            )
        scope = _scope(request, session_start)
        with self._guard:
            existing = self._active.get(scope.key)
            if existing is not None and not existing.finalized:
                return self._attachment_projection(existing)
            if existing is not None:
                # A permission continuation can intentionally reuse the same
                # worker request and browser session identity.  Its new start
                # event is a new delivery attempt and must not regress the
                # prior attempt's commit fence from events_pending.
                self._active.pop(scope.key, None)
        cdp = self._cdp_required(
            runtime,
            scope.browser_session_id,
            required=not allow_passive,
        )
        event_bus = getattr(cdp, "event_bus", None)
        if event_bus is None and self.policy.require_event_bus and not allow_passive:
            raise BrowserActiveIntegrationError(
                "04A browser event bus is unavailable for active observability"
            )
        receipt_ids: tuple[str, ...] = ()
        commit = self.commit_fence.prepare(
            scope,
            source_event_ids=(start_event.event_id,),
            action_receipt_ids=receipt_ids,
            artifact_ids=(),
            input_metadata={
                "phase": "attach_before_action",
                "browser_session_id": scope.browser_session_id,
            },
        )
        started = self.history.session_started(
            scope,
            session_start,
            source_event=start_event,
        )
        head = self.history.store.head(scope)
        commit = self.commit_fence.advance(
            commit,
            CommitPhase.HISTORY_COMMITTED,
            history_head_digest=head.content_digest if head else "",
        )
        event_bus = event_bus or _PassiveEventBus()
        active = _ActiveSession(
            scope=scope,
            runtime=runtime,
            cdp=cdp,
            event_attachment=BrowserEventAttachment(
                scope,
                event_bus,
                policy=EventAttachmentPolicy(),
                event_sink=lambda event: self._on_attached_event(scope.key, event),
            ),
            commit=commit,
            start_event=start_event,
            session_start=session_start,
            profile_id=_profile_id(session_start),
        )
        active.event_attachment.attach()
        with self._guard:
            self._active[scope.key] = active
        storage = self._storage(runtime)
        if storage is not None and active.profile_id:
            load_output = storage.observe_load(
                scope,
                active.profile_id,
                source_event_ids=(start_event.event_id,),
            )
            active.integration_outputs.append(load_output)
        return {
            **self._attachment_projection(active),
            "session_history_record_ids": [item.record_id for item in started.records],
        }

    def before_stop(
        self,
        *,
        request: WorkerRequest,
        session_start: Any,
        runtime: Any,
    ) -> BrowserIntegrationOutput:
        scope = _scope(request, session_start)
        active = self._active.get(scope.key)
        if active is None:
            return BrowserIntegrationOutput()
        if active.before_stop_completed:
            return _merge(active.integration_outputs)
        constraints = request.constraints if isinstance(request.constraints, Mapping) else {}
        should_capture = bool(constraints.get("browser_capture_storage_state")) or (
            self.policy.capture_storage_when_dirty
            and bool(constraints.get("browser_storage_dirty"))
        )
        if should_capture and active.profile_id:
            storage = self._storage(runtime)
            if storage is not None:
                output = storage.capture(
                    scope,
                    active.profile_id,
                    active.cdp,
                    source_event_ids=tuple(
                        item.bus_event_id for item in active.event_attachment.events()
                    ),
                )
                active.integration_outputs.append(output)
        active.before_stop_completed = True
        return _merge(active.integration_outputs)

    def finalize(
        self,
        *,
        request: WorkerRequest,
        session_start: Any,
        action_run: Any,
        application_events: Sequence[EventRecord],
        application_artifacts: Sequence[ArtifactRef],
        start_event: EventRecord,
        runtime: Any,
        application_ok: bool,
        application_error: str,
        action_pending: bool,
    ) -> BrowserIntegrationOutput:
        scope = _scope(request, session_start)
        active = self._active.get(scope.key)
        if active is None:
            self.attach(
                request=request,
                session_start=session_start,
                start_event=start_event,
                runtime=runtime,
            )
            active = self._active[scope.key]
        if active.finalized:
            return _merge(active.integration_outputs)
        try:
            attached = active.event_attachment.events()
            active.integration_outputs.append(
                BrowserIntegrationOutput(
                    evidence=active.event_attachment.evidence(),
                    signals=self._signals_from_attached(scope, attached),
                    events=active.event_attachment.canonical_events(),
                    projection={
                        "event_attachment": active.event_attachment.receipt().to_dict()
                    },
                )
            )
            guard_snapshot = self._download_guard_snapshot(runtime)
            active.integration_outputs.append(
                self.downloads.observe(
                    scope,
                    action_run=action_run,
                    artifacts=application_artifacts,
                    attached_events=attached,
                    guard_snapshot=guard_snapshot,
                )
            )
            active.integration_outputs.append(
                self.screenshots.inspect(
                    scope,
                    application_artifacts,
                    action_run=action_run,
                    application_events=application_events,
                    action_terminal=not action_pending,
                )
            )
            action_signal = self._action_failure_signal(
                scope,
                action_run=action_run,
                application_events=application_events,
                application_ok=application_ok,
                application_error=application_error,
                action_pending=action_pending,
            )
            if action_signal is not None:
                active.integration_outputs.append(
                    BrowserIntegrationOutput(signals=(action_signal,))
                )
            external = _external_evidence(request, scope)
            if external:
                active.integration_outputs.append(
                    self.external_mapper.map(scope, external)
                )
            active.event_attachment.detach(drain=True)
            output = _merge(active.integration_outputs)
            active.finalized = True
            self._completed += 1
            return output
        except Exception as exc:
            self._failures += 1
            active.commit = self.commit_fence.fail(active.commit, exc)
            if self.policy.fail_closed:
                raise
            return BrowserIntegrationOutput(
                projection={
                    "integration_error": f"{type(exc).__name__}: {exc}",
                    "fallback_allowed": False,
                }
            )

    def mark_history_artifacts_committed(
        self,
        scope: ObservationScope,
        output: BrowserIntegrationOutput,
        *,
        history_head_digest: str,
    ) -> dict[str, Any]:
        active = self._active.get(scope.key)
        if active is None:
            raise BrowserActiveIntegrationError("active observation commit is missing")
        commit = self.commit_fence.advance(
            active.commit,
            CommitPhase.ARTIFACTS_COMMITTED,
            history_head_digest=history_head_digest,
            artifact_ids=tuple(item.artifact_id for item in output.artifacts),
        )
        commit = self.commit_fence.advance(
            commit,
            CommitPhase.EVENTS_PENDING,
            event_ids=tuple(item.event_id for item in output.events),
            recovery_input_ids=tuple(
                item.input_id for item in output.recovery_inputs
            ),
        )
        active.commit = commit
        return commit.to_dict()

    def acknowledge_events(
        self,
        observation_commit: Mapping[str, Any],
        *,
        committed_event_ids: Sequence[str],
    ) -> dict[str, Any]:
        scope, commit_id = _commit_identity(observation_commit)
        receipt = self.commit_fence.acknowledge_events(
            scope,
            commit_id,
            committed_event_ids=committed_event_ids,
        )
        active = self._active.get(scope.key)
        if active is not None and active.commit.commit_id == commit_id:
            active.commit = receipt
        return receipt.to_dict()

    def acknowledge_checkpoint(
        self,
        observation_commit: Mapping[str, Any],
    ) -> dict[str, Any]:
        scope, commit_id = _commit_identity(observation_commit)
        receipt = self.commit_fence.acknowledge_checkpoint(scope, commit_id)
        active = self._active.get(scope.key)
        if active is not None and active.commit.commit_id == commit_id:
            active.commit = receipt
        return receipt.to_dict()

    def refresh_commit(
        self,
        observation_commit: Mapping[str, Any],
    ) -> dict[str, Any]:
        scope, commit_id = _commit_identity(observation_commit)
        receipt = self.commit_fence.get(scope, commit_id)
        if receipt is None:
            raise BrowserActiveIntegrationError(
                f"observation commit {commit_id!r} is unavailable"
            )
        return receipt.to_dict()

    def projection(
        self,
        *,
        task_id: str = "",
        scope: ObservationScope | None = None,
    ) -> dict[str, Any]:
        with self._guard:
            active = tuple(
                item
                for item in self._active.values()
                if (not task_id or item.scope.task_id == task_id)
                and (scope is None or item.scope == scope)
            )
        return {
            "schema": "zyra.browser-observability.active-integration.v1",
            "enabled": self.policy.enabled,
            "active_count": sum(1 for item in active if not item.finalized),
            "completed_count": self._completed,
            "failure_count": self._failures,
            "sessions": [self._attachment_projection(item) for item in active],
            "downloads": self.downloads.projection(scope=scope),
            "screenshots": self.screenshots.projection(scope=scope),
            "external_evidence": self.external_mapper.projection(scope=scope),
            "commits": self.commit_fence.projection(task_id=task_id, scope=scope),
            "default_route": True,
            "fallback_allowed": False,
            "recovery_planner_owner": "M1-07C",
        }

    def _on_attached_event(
        self,
        scope_key: str,
        event: AttachedBrowserEvent,
    ) -> None:
        active = self._active.get(scope_key)
        if active is None or event.stale:
            return
        navigation = self._navigation_by_scope.get(scope_key)
        if navigation is None:
            navigation = BrowserNavigationSecurityRuntime(
                BrowserSecurityPolicyEngine(
                    policy=SecurityPolicy(require_dns_resolution=False),
                    resolver=lambda _host: (),
                )
            )
            self._navigation_by_scope[scope_key] = navigation
        output = navigation.consume(active.scope, (event,), active.cdp)
        if output.evidence or output.signals or output.events:
            active.integration_outputs.append(output)

    def _storage(self, runtime: Any) -> BrowserStorageIntegrationRuntime | None:
        component = _session_component(runtime)
        profile_store = getattr(component, "profile_store", None)
        if profile_store is None:
            return None
        identity = id(profile_store)
        existing = self._storage_by_runtime.get(identity)
        if existing is None:
            existing = BrowserStorageIntegrationRuntime(
                profile_store,
                self.artifact_store,
            )
            self._storage_by_runtime[identity] = existing
        return existing

    @staticmethod
    def _cdp(runtime: Any, browser_session_id: str) -> Any:
        return BrowserObservabilityIntegrationRuntime._cdp_required(
            runtime,
            browser_session_id,
            required=True,
        )

    @staticmethod
    def _cdp_required(
        runtime: Any,
        browser_session_id: str,
        *,
        required: bool,
    ) -> Any:
        method = getattr(runtime, "cdp_runtime", None)
        if callable(method):
            return method(browser_session_id)
        component = _session_component(runtime)
        method = getattr(component, "cdp_runtime", None)
        if callable(method):
            return method(browser_session_id)
        cdp = getattr(component, "_cdp", {}).get(browser_session_id)
        if cdp is None and required:
            raise BrowserActiveIntegrationError("04A CDP runtime is unavailable")
        return cdp

    @staticmethod
    def _download_guard_snapshot(runtime: Any) -> dict[str, Any]:
        action = getattr(runtime, "_browser_action_application", None)
        foundation = getattr(action, "foundation", None)
        executor = getattr(getattr(foundation, "gateway", None), "executor", None)
        download_runtime = getattr(executor, "download_runtime", None)
        snapshot = getattr(download_runtime, "snapshot", None)
        return dict(snapshot()) if callable(snapshot) else {}

    @staticmethod
    def _signals_from_attached(
        scope: ObservationScope,
        events: Sequence[AttachedBrowserEvent],
    ) -> tuple[WatchdogSignal, ...]:
        output: list[WatchdogSignal] = []
        for item in events:
            if item.stale:
                continue
            if item.kind == AttachedEventKind.CDP_CONNECTION_LOST:
                output.append(
                    _runtime_signal(
                        scope,
                        item,
                        kind=SignalKind.CDP_DISCONNECTED,
                        summary="Browser CDP connection was lost during the worker request.",
                        retryable=True,
                        outcome_unknown=True,
                    )
                )
            elif item.kind == AttachedEventKind.CDP_REQUEST_TIMEOUT:
                output.append(
                    _runtime_signal(
                        scope,
                        item,
                        kind=SignalKind.REQUEST_TIMEOUT,
                        summary="Browser CDP request exceeded its deadline.",
                        retryable=True,
                        outcome_unknown=True,
                    )
                )
            elif item.kind in {
                AttachedEventKind.CDP_PROTOCOL_ERROR,
                AttachedEventKind.CDP_HANDLER_ERROR,
            }:
                output.append(
                    _runtime_signal(
                        scope,
                        item,
                        kind=SignalKind.TOOL_FAILED,
                        summary="Browser CDP event processing failed.",
                        retryable=False,
                        outcome_unknown=False,
                    )
                )
        return tuple(output)

    @staticmethod
    def _action_failure_signal(
        scope: ObservationScope,
        *,
        action_run: Any,
        application_events: Sequence[EventRecord],
        application_ok: bool,
        application_error: str,
        action_pending: bool,
    ) -> WatchdogSignal | None:
        if action_pending or application_ok:
            return None
        failed = [
            _mapping(item)
            for item in _action_receipts(action_run)
            if not bool(_mapping(item).get("ok"))
        ]
        retryable = any(bool(item.get("retryable")) for item in failed)
        outcome_unknown = any(bool(item.get("outcome_unknown")) for item in failed)
        return WatchdogSignal(
            scope=scope,
            watchdog=WatchdogName.LOCAL_BROWSER,
            kind=SignalKind.TOOL_FAILED,
            status=HealthStatus.UNHEALTHY,
            severity=Severity.ERROR,
            summary=application_error or "Browser action failed.",
            sequence=max(1, len(failed)),
            retryable=retryable,
            terminal=True,
            evidence_event_ids=tuple(item.event_id for item in application_events),
            artifact_ids=tuple(
                dict.fromkeys(
                    str(artifact_id)
                    for item in failed
                    for artifact_id in item.get("artifact_ids", ())
                )
            ),
            metadata={
                "failed_tool_call_ids": [
                    str(item.get("tool_call_id") or item.get("action_id") or "")
                    for item in failed
                ],
                "failed_receipt_ids": [
                    str(item.get("receipt_id") or "") for item in failed
                ],
                "outcome_unknown": outcome_unknown,
                "action_error": application_error,
            },
        )

    @staticmethod
    def _attachment_projection(active: _ActiveSession) -> dict[str, Any]:
        return {
            "scope": active.scope.to_dict(),
            "event_attachment": active.event_attachment.receipt().to_dict(),
            "commit": active.commit.to_dict(),
            "profile_id": active.profile_id,
            "before_stop_completed": active.before_stop_completed,
            "finalized": active.finalized,
        }


def _runtime_signal(
    scope: ObservationScope,
    event: AttachedBrowserEvent,
    *,
    kind: SignalKind,
    summary: str,
    retryable: bool,
    outcome_unknown: bool,
) -> WatchdogSignal:
    return WatchdogSignal(
        scope=scope,
        watchdog=WatchdogName.LOCAL_BROWSER,
        kind=kind,
        status=HealthStatus.UNHEALTHY,
        severity=Severity.ERROR,
        summary=summary,
        sequence=max(1, event.bus_sequence),
        retryable=retryable,
        terminal=True,
        evidence_event_ids=(event.bus_event_id,),
        metadata={
            "attached_event_receipt_id": event.receipt_id,
            "outcome_unknown": outcome_unknown,
            "bus_generation": event.bus_generation,
        },
    )


def _scope(request: WorkerRequest, session_start: Any) -> ObservationScope:
    session = getattr(session_start, "session", None)
    browser_session_id = str(
        getattr(session, "session_id", "")
        or request.constraints.get("browser_session_id")
        or ""
    )
    if not browser_session_id:
        raise BrowserActiveIntegrationError("04A browser session identity is missing")
    return ObservationScope(
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id or "",
        browser_session_id=browser_session_id,
        canonical_session_id=str(getattr(session, "canonical_session_id", "") or ""),
        worker_request_id=request.request_id,
    )


def _commit_identity(
    observation_commit: Mapping[str, Any],
) -> tuple[ObservationScope, str]:
    scope_value = observation_commit.get("scope")
    if not isinstance(scope_value, Mapping):
        raise BrowserActiveIntegrationError("observation commit scope is missing")
    scope = ObservationScope(
        run_id=str(scope_value.get("run_id") or ""),
        task_id=str(scope_value.get("task_id") or ""),
        node_id=str(scope_value.get("node_id") or ""),
        browser_session_id=str(scope_value.get("browser_session_id") or ""),
        canonical_session_id=str(scope_value.get("canonical_session_id") or ""),
        worker_request_id=str(scope_value.get("worker_request_id") or ""),
    )
    commit_id = str(observation_commit.get("commit_id") or "")
    if not commit_id:
        raise BrowserActiveIntegrationError("observation commit identity is missing")
    return scope, commit_id


def _profile_id(session_start: Any) -> str:
    session = getattr(session_start, "session", None)
    return str(
        getattr(session, "profile_id", "")
        or getattr(session, "browser_profile_id", "")
        or ""
    )


def _session_component(runtime: Any) -> Any:
    method = getattr(runtime, "session_runtime_component", None)
    return method() if callable(method) else getattr(runtime, "_runtime", runtime)


def _action_receipts(action_run: Any) -> tuple[Any, ...]:
    if action_run is None:
        return ()
    return tuple(
        getattr(action_run, "receipts", getattr(action_run, "action_receipts", ()))
    )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    projected = to_jsonable(value)
    return dict(projected) if isinstance(projected, Mapping) else {}


def _external_evidence(
    request: WorkerRequest,
    scope: ObservationScope,
) -> tuple[RuntimeEvidenceEnvelope | Mapping[str, Any], ...]:
    constraints = request.constraints if isinstance(request.constraints, Mapping) else {}
    raw = constraints.get("browser_runtime_evidence") or ()
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    return tuple(item for item in raw if isinstance(item, RuntimeEvidenceEnvelope | Mapping))


def _merge(values: Sequence[BrowserIntegrationOutput]) -> BrowserIntegrationOutput:
    output = BrowserIntegrationOutput()
    return output.merge(*values)


@dataclass(frozen=True, slots=True)
class _PassiveBusSnapshot:
    state: str = "running"
    generation: int = 1


@dataclass(frozen=True, slots=True)
class _PassiveSubscription:
    subscription_id: str


class _PassiveEventBus:
    """Compatibility boundary for direct Application tests, never BrowserWorker."""

    def snapshot(self) -> _PassiveBusSnapshot:
        return _PassiveBusSnapshot()

    def start(self) -> int:
        return 1

    def subscribe(
        self,
        callback: Any,
        *,
        topic_prefix: str = "",
        max_failures: int = 3,
        subscription_id: str = "",
    ) -> _PassiveSubscription:
        del callback, topic_prefix, max_failures
        return _PassiveSubscription(subscription_id or "passive-observability")

    def unsubscribe(self, subscription_id: str) -> bool:
        return bool(subscription_id)
