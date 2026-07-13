from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from zyra_core import ArtifactRef, EventRecord
from zyra_runtime import LocalArtifactStore, WorkerRequest

from .api_projection import (
    BrowserObservabilityApiProjection,
    BrowserObservabilityQuery,
)
from .artifact_publisher import (
    ArtifactPublication,
    BrowserArtifactPublisher,
    infer_artifact_role,
)
from .crash_detector import BrowserCrashDetector, CrashDetectorPolicy
from .failure_projection import BrowserFailureProjector
from .history_runtime import BrowserHistoryRuntime
from .history_store import BrowserHistoryStore, HistoryStorePolicy
from .health_runtime import BrowserHealthRuntime
from .judge import BrowserTraceJudge
from .models import (
    BrowserObservation,
    HistoryKind,
    HistoryRecord,
    ObservationScope,
    ObservabilityResult,
    TraceSpan,
    WatchdogSignal,
)
from .replay import BrowserHistoryReplay
from .trace_runtime import BrowserTraceRuntime, TracePairingError
from .watchdogs import BrowserWatchdogRegistry, WatchdogPolicy
from .integration import (
    BrowserIntegrationOutput,
    BrowserObservabilityIntegrationRuntime,
)


class BrowserObservabilityDisabled(RuntimeError):
    code = "browser_observability_disabled"


class BrowserObservabilityIntegrationError(RuntimeError):
    code = "browser_observability_integration_error"


class BrowserObservabilityApplication:
    """04D foundation attached to every productized BrowserWorker request."""

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStore,
        state_root: str | Path | None = None,
        disabled: bool = False,
        history_store: BrowserHistoryStore | None = None,
        crash_detector: BrowserCrashDetector | None = None,
        watchdog_registry: BrowserWatchdogRegistry | None = None,
        trace_runtime: BrowserTraceRuntime | None = None,
        artifact_publisher: BrowserArtifactPublisher | None = None,
        failure_projector: BrowserFailureProjector | None = None,
        judge: BrowserTraceJudge | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.state_root = Path(
            state_root
            or artifact_store.root / ".browser-observability"
        ).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.disabled = disabled
        self.history_store = history_store or BrowserHistoryStore(
            self.state_root / "history",
            policy=HistoryStorePolicy(),
        )
        self.history = BrowserHistoryRuntime(self.history_store)
        self.crash_detector = crash_detector or BrowserCrashDetector(
            policy=CrashDetectorPolicy(),
        )
        self.watchdogs = watchdog_registry or BrowserWatchdogRegistry(
            policy=WatchdogPolicy(),
        )
        self.trace = trace_runtime or BrowserTraceRuntime()
        self.artifacts = artifact_publisher or BrowserArtifactPublisher(
            artifact_store,
        )
        self.failures = failure_projector or BrowserFailureProjector()
        self.integration = BrowserObservabilityIntegrationRuntime(
            artifact_store=artifact_store,
            state_root=self.state_root / "integration",
            history=self.history,
            failure_projector=self.failures,
        )
        self.health = BrowserHealthRuntime()
        self.replay = BrowserHistoryReplay(
            self.history_store,
            trace_runtime=self.trace,
        )
        self.judge = judge or BrowserTraceJudge()
        self.api = BrowserObservabilityApiProjection(
            history_store=self.history_store,
            replay=self.replay,
            trace_runtime=self.trace,
            crash_detector=self.crash_detector,
            watchdog_registry=self.watchdogs,
            artifact_publisher=self.artifacts,
            integration_runtime=self.integration,
        )
        self._runs = 0
        self._failures = 0

    def attach(
        self,
        *,
        request: WorkerRequest,
        session_start: Any,
        start_event: EventRecord,
        runtime: Any,
    ) -> dict[str, Any]:
        if self.disabled:
            raise BrowserObservabilityDisabled(
                "browser observability is disabled on the productized main path"
            )
        return self.integration.attach(
            request=request,
            session_start=session_start,
            start_event=start_event,
            runtime=runtime,
        )

    def before_stop(
        self,
        *,
        request: WorkerRequest,
        session_start: Any,
        runtime: Any,
    ) -> BrowserIntegrationOutput:
        if self.disabled:
            raise BrowserObservabilityDisabled(
                "browser observability is disabled on the productized main path"
            )
        return self.integration.before_stop(
            request=request,
            session_start=session_start,
            runtime=runtime,
        )

    def acknowledge_events(
        self,
        projection: Mapping[str, Any],
        *,
        committed_event_ids: Sequence[str],
    ) -> dict[str, Any]:
        commit = projection.get("observation_commit")
        if not isinstance(commit, Mapping):
            return dict(projection)
        updated = self.integration.acknowledge_events(
            commit,
            committed_event_ids=committed_event_ids,
        )
        return {**dict(projection), "observation_commit": updated}

    def acknowledge_checkpoint(
        self,
        projection: Mapping[str, Any],
    ) -> dict[str, Any]:
        commit = projection.get("observation_commit")
        if not isinstance(commit, Mapping):
            return dict(projection)
        updated = self.integration.acknowledge_checkpoint(commit)
        return {**dict(projection), "observation_commit": updated}

    def refresh_commit(
        self,
        projection: Mapping[str, Any],
    ) -> dict[str, Any]:
        commit = projection.get("observation_commit")
        if not isinstance(commit, Mapping):
            return dict(projection)
        updated = self.integration.refresh_commit(commit)
        return {**dict(projection), "observation_commit": updated}

    def observe(
        self,
        *,
        request: WorkerRequest,
        session_start: Any,
        action_run: Any,
        message_turn: Any,
        start_event: EventRecord,
        application_events: Sequence[EventRecord],
        application_artifacts: Sequence[ArtifactRef],
        stop_event: EventRecord | None,
        stop_error: str,
        application_ok: bool,
        application_error: str,
        action_pending: bool,
        runtime: Any = None,
    ) -> ObservabilityResult:
        if self.disabled:
            raise BrowserObservabilityDisabled(
                "browser observability is disabled on the productized main path"
            )
        scope = self._scope(request, session_start)
        records: list[HistoryRecord] = []
        events: list[EventRecord] = []
        artifacts: list[ArtifactRef] = []
        lineage = []
        spans: list[TraceSpan] = []
        signals: list[WatchdogSignal] = []
        recovery_inputs = []
        try:
            self.integration.attach(
                request=request,
                session_start=session_start,
                start_event=start_event,
                runtime=runtime,
                allow_passive=True,
            )
            records.extend(
                self.history_store.records(
                    scope,
                    kinds=(HistoryKind.SESSION_STARTED,),
                )
            )
            receipts = tuple(
                getattr(
                    action_run,
                    "receipts",
                    getattr(action_run, "action_receipts", ()),
                )
            ) if action_run is not None else ()
            if receipts:
                action_history = self.history.action_receipts(
                    scope,
                    receipts,
                    events=application_events,
                )
                records.extend(action_history.records)
            if message_turn is not None:
                state_history = self.history.state_capture(
                    scope,
                    message_turn,
                    events=application_events,
                )
                records.extend(state_history.records)
            causal_event_ids = tuple(
                dict.fromkeys(
                    [
                        start_event.event_id,
                        *(item.event_id for item in application_events),
                    ]
                )
            )
            for artifact in application_artifacts:
                publication = self.artifacts.adopt(
                    scope,
                    artifact,
                    role=infer_artifact_role(artifact),
                    source_event_ids=causal_event_ids,
                    source_record_ids=tuple(item.record_id for item in records),
                )
                artifacts.append(publication.artifact)
                lineage.append(publication.lineage)
                if not publication.idempotent:
                    events.append(publication.event)
                artifact_history = self.history.artifact(publication.lineage)
                records.extend(artifact_history.records)
            integration_output = self.integration.finalize(
                request=request,
                session_start=session_start,
                action_run=action_run,
                application_events=application_events,
                application_artifacts=application_artifacts,
                start_event=start_event,
                runtime=runtime,
                application_ok=application_ok,
                application_error=application_error,
                action_pending=action_pending,
            )
            events.extend(integration_output.events)
            signals.extend(integration_output.signals)
            if integration_output.evidence:
                evidence_history = self.history.runtime_evidence(
                    scope,
                    integration_output.evidence,
                )
                records.extend(evidence_history.records)
            if integration_output.spans:
                span_history = self.history.trace_spans(
                    scope,
                    integration_output.spans,
                )
                records.extend(span_history.records)
            integration_causal_ids = tuple(
                dict.fromkeys(
                    [
                        start_event.event_id,
                        *(item.event_id for item in application_events),
                        *(item.event_id for item in integration_output.events),
                    ]
                )
            )
            for artifact in integration_output.artifacts:
                publication = self.artifacts.adopt(
                    scope,
                    artifact,
                    role=infer_artifact_role(artifact),
                    source_event_ids=integration_causal_ids,
                    source_record_ids=tuple(item.record_id for item in records),
                    metadata={"integration_owner": "M1-S04D-02"},
                )
                artifacts.append(publication.artifact)
                lineage.append(publication.lineage)
                if not publication.idempotent:
                    events.append(publication.event)
                artifact_history = self.history.artifact(publication.lineage)
                records.extend(artifact_history.records)
            observation = self._observation(
                scope,
                request=request,
                session_start=session_start,
                action_run=action_run,
                application_artifacts=application_artifacts,
                application_ok=application_ok,
                application_error=application_error,
                action_pending=action_pending,
                runtime=runtime,
                intentional_stop=stop_event is not None and not stop_error,
            )
            self.crash_detector.attach(
                scope,
                process_id=observation.process_id,
                cdp_connected=observation.cdp_connected,
            )
            if observation.active_request_id:
                self.crash_detector.request_started(
                    scope,
                    observation.active_request_id,
                    at_ms=observation.active_request_started_ms,
                )
            crash_signals = self.crash_detector.observe(observation)
            attached_signals = self.watchdogs.evaluate(observation)
            signals[:] = self._dedupe_signals(
                (
                    *signals,
                    *crash_signals,
                    *attached_signals,
                )
            )
            health_state = self.health.ingest(scope, signals)
            if action_pending:
                failure_error = ""
            else:
                failure_error = application_error if not application_ok else ""
            failed_receipt_ids = tuple(
                str(getattr(item, "receipt_id", "") or "")
                for item in receipts
                if not bool(getattr(item, "ok", False))
                and str(getattr(item, "receipt_id", "") or "")
            )
            failed_tool_ids = tuple(
                str(
                    getattr(item, "tool_call_id", "")
                    or getattr(item, "action_id", "")
                    or ""
                )
                for item in receipts
                if not bool(getattr(item, "ok", False))
                and (
                    getattr(item, "tool_call_id", "")
                    or getattr(item, "action_id", "")
                )
            )
            projection = self.failures.project(
                scope,
                signals,
                action_error=failure_error,
                failed_tool_call_ids=failed_tool_ids,
                failed_receipt_ids=failed_receipt_ids,
                evidence_event_ids=causal_event_ids,
                artifact_ids=tuple(item.artifact_id for item in application_artifacts),
                metadata={
                    "browser_observability_owner": "M1-S04D-01",
                    "action_pending": action_pending,
                    "fallback_allowed": False,
                },
            )
            events.extend(projection.events)
            recovery_inputs.extend(projection.recovery_inputs)
            if signals:
                signal_history = self.history.signals(scope, signals)
                records.extend(signal_history.records)
            if recovery_inputs:
                recovery_history = self.history.recovery_inputs(
                    scope,
                    recovery_inputs,
                )
                records.extend(recovery_history.records)
            if stop_event is not None or stop_error:
                stop_history = self.history.session_stopped(
                    scope,
                    stop_event=stop_event,
                    stop_error=stop_error,
                    intentional=stop_event is not None and not stop_error,
                )
                records.extend(stop_history.records)
                if stop_event is not None and not stop_error:
                    self.crash_detector.intentional_stop(scope)
            durable_records = self.history_store.records(scope)
            try:
                spans.extend(
                    self.trace.spans_from_records(
                        scope,
                        durable_records,
                    )
                )
            except TracePairingError as error:
                raise BrowserObservabilityIntegrationError(str(error)) from error
            trace_projection = self.trace.public_projection(spans)
            trace_publication = self.artifacts.publish_trace(
                scope,
                trace_projection,
                source_event_ids=tuple(
                    dict.fromkeys(
                        [
                            *causal_event_ids,
                            *(item.event_id for item in events),
                        ]
                    )
                ),
                source_record_ids=tuple(item.record_id for item in durable_records),
            )
            artifacts.append(trace_publication.artifact)
            lineage.append(trace_publication.lineage)
            if not trace_publication.idempotent:
                events.append(trace_publication.event)
            trace_artifact_history = self.history.artifact(
                trace_publication.lineage
            )
            records.extend(trace_artifact_history.records)
            manifest = self.history.projection(scope, limit=25)
            manifest_publication = self.artifacts.publish_history_manifest(
                scope,
                manifest,
                source_event_ids=tuple(
                    dict.fromkeys(
                        [
                            *causal_event_ids,
                            *(item.event_id for item in events),
                        ]
                    )
                ),
                source_record_ids=tuple(
                    item.record_id
                    for item in self.history_store.records(scope)
                ),
            )
            artifacts.append(manifest_publication.artifact)
            lineage.append(manifest_publication.lineage)
            if not manifest_publication.idempotent:
                events.append(manifest_publication.event)
            manifest_history = self.history.artifact(
                manifest_publication.lineage
            )
            records.extend(manifest_history.records)
            public = self._projection(
                scope,
                records=records,
                signals=signals,
                spans=spans,
                lineage=lineage,
                recovery_inputs=recovery_inputs,
                health_state=health_state,
                application_ok=application_ok,
                application_error=application_error,
                action_pending=action_pending,
            )
            public["integration"] = {
                **dict(integration_output.projection),
                **self.integration.projection(scope=scope),
            }
            head = self.history_store.head(scope)
            commit_output = integration_output.merge(
                BrowserIntegrationOutput(
                    recovery_inputs=tuple(recovery_inputs),
                    artifacts=tuple(self._dedupe_artifacts(artifacts)),
                    events=tuple(events),
                )
            )
            public["observation_commit"] = self.integration.mark_history_artifacts_committed(
                scope,
                commit_output,
                history_head_digest=head.content_digest if head else "",
            )
            self._runs += 1
            return ObservabilityResult(
                scope=scope,
                records=tuple(records),
                signals=tuple(signals),
                spans=tuple(spans),
                artifacts=tuple(self._dedupe_artifacts(artifacts)),
                artifact_lineage=tuple(lineage),
                events=tuple(events),
                recovery_inputs=tuple(recovery_inputs),
                projection=public,
            )
        except Exception:
            self._failures += 1
            raise

    def query(
        self,
        *,
        task_id: str,
        browser_session_id: str = "",
        worker_request_id: str = "",
        view: str = "summary",
        limit: int = 100,
        after_sequence: int = 0,
    ) -> dict[str, Any]:
        return self.api.query(
            BrowserObservabilityQuery(
                task_id=task_id,
                browser_session_id=browser_session_id,
                worker_request_id=worker_request_id,
                view=view,
                limit=limit,
                after_sequence=after_sequence,
            )
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-observability",
            "owner_unit": "M1-S04D-01",
            "disabled": self.disabled,
            "runs": self._runs,
            "failures": self._failures,
            "state_root": str(self.state_root),
            "watchdogs": self.watchdogs.snapshot(),
            "health": self.health.projection(),
            "integration": self.integration.projection(),
            "default_route": True,
            "fallback_allowed": False,
            "recovery_planner_owner": "M1-07C",
        }

    def _projection(
        self,
        scope: ObservationScope,
        *,
        records: Sequence[HistoryRecord],
        signals: Sequence[WatchdogSignal],
        spans: Sequence[TraceSpan],
        lineage: Sequence[Any],
        recovery_inputs: Sequence[Any],
        health_state: Any,
        application_ok: bool,
        application_error: str,
        action_pending: bool,
    ) -> dict[str, Any]:
        head = self.history_store.head(scope)
        return {
            "schema": "zyra.browser-observability.worker-projection.v1",
            "scope": scope.to_dict(),
            "owner_unit": "M1-S04D-01",
            "history": {
                "record_count": head.sequence if head else 0,
                "head_digest": head.content_digest if head else "",
                "records_written": len(records),
            },
            "health": {
                "signal_count": len(signals),
                "terminal_signal_count": sum(
                    1 for item in signals if item.terminal
                ),
                "signals": [item.to_dict() for item in signals],
                "crash_detector": self.crash_detector.state(scope),
                "aggregate": health_state.to_dict(),
            },
            "trace": self.trace.public_projection(spans, limit=100),
            "artifacts": {
                "lineage_count": len(lineage),
                "lineage": [item.to_dict() for item in lineage],
            },
            "recovery_handoff": {
                "input_count": len(recovery_inputs),
                "inputs": [item.to_dict() for item in recovery_inputs],
                "planner_owner": "M1-07C",
                "recovery_planned_emitted": False,
            },
            "worker": {
                "ok": application_ok,
                "error": application_error,
                "action_pending": action_pending,
            },
            "default_route": True,
            "fallback_allowed": False,
        }

    @staticmethod
    def _scope(
        request: WorkerRequest,
        session_start: Any,
    ) -> ObservationScope:
        session = getattr(session_start, "session", None)
        browser_session_id = str(
            getattr(session, "session_id", "")
            or request.constraints.get("browser_session_id")
            or request.constraints.get("session_id")
            or ""
        )
        canonical_session_id = str(
            getattr(session, "canonical_session_id", "")
            or request.constraints.get("canonical_session_id")
            or ""
        )
        if not browser_session_id:
            raise BrowserObservabilityIntegrationError(
                "browser observability requires the 04A session identity"
            )
        return ObservationScope(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id or "",
            browser_session_id=browser_session_id,
            canonical_session_id=canonical_session_id,
            worker_request_id=request.request_id,
        )

    @staticmethod
    def _observation(
        scope: ObservationScope,
        *,
        request: WorkerRequest,
        session_start: Any,
        action_run: Any,
        application_artifacts: Sequence[ArtifactRef],
        application_ok: bool,
        application_error: str,
        action_pending: bool,
        runtime: Any,
        intentional_stop: bool,
    ) -> BrowserObservation:
        session = getattr(session_start, "session", None)
        process = _first_value(
            runtime,
            session,
            names=(
                "process",
                "browser_process",
                "_process",
                "subprocess",
            ),
        )
        process_id = _integer(
            getattr(process, "pid", None)
            or _first_attribute(
                runtime,
                session,
                names=("process_id", "browser_pid", "pid"),
            )
        )
        process_exit_code = None
        process_running = None
        poll = getattr(process, "poll", None)
        if callable(poll):
            process_exit_code = poll()
            process_running = process_exit_code is None
        cdp_connected_value = _first_attribute(
            session,
            runtime,
            names=(
                "cdp_connected",
                "connected",
                "is_connected",
            ),
        )
        cdp_connected = (
            bool(cdp_connected_value)
            if isinstance(cdp_connected_value, bool)
            else None
        )
        constraints = (
            request.constraints
            if isinstance(request.constraints, Mapping)
            else {}
        )
        screenshot_artifact_id = next(
            (
                item.artifact_id
                for item in application_artifacts
                if str(item.kind) == "screenshot"
                or "screenshot" in item.title.casefold()
            ),
            "",
        )
        receipts = tuple(
            getattr(
                action_run,
                "receipts",
                getattr(action_run, "action_receipts", ()),
            )
        ) if action_run is not None else ()
        download_items = constraints.get("browser_download_observations")
        if not isinstance(download_items, Sequence) or isinstance(
            download_items,
            str | bytes,
        ):
            download_items = ()
        expected_permissions = constraints.get("browser_permissions_expected")
        reported_permissions = constraints.get("browser_permissions_reported")
        target_ids = constraints.get("browser_target_ids")
        popup_ids = constraints.get("browser_popup_target_ids")
        now_ms = int(time.monotonic() * 1000)
        return BrowserObservation(
            scope=scope,
            sequence=max(1, len(receipts) + 1),
            monotonic_ms=now_ms,
            process_id=process_id,
            process_running=process_running,
            process_exit_code=process_exit_code,
            cdp_connected=cdp_connected,
            cdp_disconnect_reason=str(
                constraints.get("browser_cdp_disconnect_reason")
                or ""
            ),
            last_heartbeat_ms=_integer(
                constraints.get("browser_last_heartbeat_ms")
            ),
            active_request_id=(
                request.request_id
                if bool(constraints.get("browser_request_inflight"))
                else ""
            ),
            active_request_started_ms=_integer(
                constraints.get("browser_request_started_ms")
            ),
            current_url=str(
                constraints.get("browser_current_url")
                or getattr(session, "current_url", "")
                or ""
            ),
            target_ids=_strings(target_ids),
            popup_target_ids=_strings(popup_ids),
            permissions_expected=_strings(expected_permissions),
            permissions_reported=_strings(reported_permissions),
            download_items=tuple(
                dict(item)
                for item in download_items
                if isinstance(item, Mapping)
            ),
            storage_dirty=bool(
                constraints.get("browser_storage_dirty")
            ),
            storage_persisted_at=str(
                constraints.get("browser_storage_persisted_at")
                or ""
            ),
            screenshot_artifact_id=screenshot_artifact_id,
            action_terminal=not action_pending,
            action_ok=application_ok,
            action_error=application_error,
            intentional_stop=intentional_stop,
            metadata={
                "about_blank_expected": bool(
                    constraints.get("browser_about_blank_expected")
                ),
                "about_blank_since_ms": _integer(
                    constraints.get("browser_about_blank_since_ms")
                )
                or now_ms,
                "storage_dirty_since_ms": _integer(
                    constraints.get("browser_storage_dirty_since_ms")
                )
                or now_ms,
                "security_verdict": (
                    dict(constraints.get("browser_security_verdict"))
                    if isinstance(
                        constraints.get("browser_security_verdict"),
                        Mapping,
                    )
                    else {}
                ),
                "action_pending": action_pending,
            },
        )

    @staticmethod
    def _dedupe_signals(
        values: Sequence[WatchdogSignal],
    ) -> tuple[WatchdogSignal, ...]:
        output: list[WatchdogSignal] = []
        seen: set[tuple[str, str, str]] = set()
        for item in values:
            key = (
                str(item.watchdog),
                str(item.kind),
                item.summary,
            )
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
        return tuple(output)

    @staticmethod
    def _dedupe_artifacts(
        values: Sequence[ArtifactRef],
    ) -> tuple[ArtifactRef, ...]:
        output: list[ArtifactRef] = []
        seen: set[str] = set()
        for item in values:
            if item.artifact_id in seen:
                continue
            seen.add(item.artifact_id)
            output.append(item)
        return tuple(output)


def _first_value(
    *values: Any,
    names: Sequence[str],
) -> Any:
    for value in values:
        if value is None:
            continue
        for name in names:
            candidate = getattr(value, name, None)
            if candidate is not None:
                return candidate
    return None


def _first_attribute(
    *values: Any,
    names: Sequence[str],
) -> Any:
    for value in values:
        if value is None:
            continue
        for name in names:
            candidate = getattr(value, name, None)
            if callable(candidate):
                try:
                    candidate = candidate()
                except TypeError:
                    continue
            if candidate is not None:
                return candidate
    return None


def _integer(
    value: Any,
) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strings(
    value: Any,
) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(
            item.strip()
            for item in value.split(",")
            if item.strip()
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return tuple(
            str(item)
            for item in value
            if str(item)
        )
    return ()
