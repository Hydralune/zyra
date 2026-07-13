from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .artifact_publisher import BrowserArtifactPublisher
from .crash_detector import BrowserCrashDetector
from .history_store import BrowserHistoryStore
from .models import ArtifactRole, HistoryKind, ObservationScope
from .replay import BrowserHistoryReplay
from .trace_runtime import BrowserTraceRuntime, TracePairingError
from .watchdogs import BrowserWatchdogRegistry


@dataclass(frozen=True, slots=True)
class BrowserObservabilityQuery:
    task_id: str
    browser_session_id: str = ""
    worker_request_id: str = ""
    view: str = "summary"
    limit: int = 100
    after_sequence: int = 0

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ValueError("observability query requires task_id")
        if self.limit < 0 or self.limit > 5_000:
            raise ValueError("observability query limit must be between 0 and 5000")
        if self.after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if self.view not in {
            "summary",
            "history",
            "trace",
            "health",
            "downloads",
            "screenshots",
            "artifacts",
            "replay",
        }:
            raise ValueError(f"unsupported browser observability view {self.view!r}")


class BrowserObservabilityApiProjection:
    def __init__(
        self,
        *,
        history_store: BrowserHistoryStore,
        replay: BrowserHistoryReplay,
        trace_runtime: BrowserTraceRuntime,
        crash_detector: BrowserCrashDetector,
        watchdog_registry: BrowserWatchdogRegistry,
        artifact_publisher: BrowserArtifactPublisher,
    ) -> None:
        self.history_store = history_store
        self.replay_runtime = replay
        self.trace_runtime = trace_runtime
        self.crash_detector = crash_detector
        self.watchdog_registry = watchdog_registry
        self.artifact_publisher = artifact_publisher

    def query(
        self,
        query: BrowserObservabilityQuery,
    ) -> dict[str, Any]:
        scopes = self.history_store.list_scopes(task_id=query.task_id)
        selected = [
            item
            for item in scopes
            if (
                not query.browser_session_id
                or str(item["scope"].get("browser_session_id") or "")
                == query.browser_session_id
            )
            and (
                not query.worker_request_id
                or str(item["scope"].get("worker_request_id") or "")
                == query.worker_request_id
            )
        ]
        if query.view == "summary":
            return self._summary(query, selected)
        projections = [
            self._scope_view(
                ObservationScope(
                    run_id=str(item["scope"]["run_id"]),
                    task_id=str(item["scope"]["task_id"]),
                    node_id=str(item["scope"].get("node_id") or ""),
                    browser_session_id=str(item["scope"]["browser_session_id"]),
                    canonical_session_id=str(
                        item["scope"].get("canonical_session_id")
                        or ""
                    ),
                    worker_request_id=str(item["scope"]["worker_request_id"]),
                ),
                query,
            )
            for item in selected
        ]
        return {
            "schema": "zyra.browser-observability.api.v1",
            "view": query.view,
            "task_id": query.task_id,
            "scope_count": len(projections),
            "scopes": projections,
        }

    def _summary(
        self,
        query: BrowserObservabilityQuery,
        scopes: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        publications = self.artifact_publisher.publications()
        task_publications = tuple(
            item
            for item in publications
            if item.lineage.scope.task_id == query.task_id
        )
        return {
            "schema": "zyra.browser-observability.api.v1",
            "view": "summary",
            "task_id": query.task_id,
            "scope_count": len(scopes),
            "scopes": [dict(item) for item in scopes],
            "health": {
                "watchdogs": self.watchdog_registry.snapshot(),
                "crash_detectors": self.crash_detector.snapshots(
                    task_id=query.task_id
                ),
            },
            "artifacts": {
                "count": len(task_publications),
                "screenshots": sum(
                    1
                    for item in task_publications
                    if item.lineage.role == ArtifactRole.SCREENSHOT
                ),
                "downloads": sum(
                    1
                    for item in task_publications
                    if item.lineage.role == ArtifactRole.DOWNLOAD
                ),
                "traces": sum(
                    1
                    for item in task_publications
                    if item.lineage.role == ArtifactRole.TRACE
                ),
            },
            "owners": {
                "history": "M1-S04D-01",
                "canonical_task": "SQLite/TaskState",
                "canonical_events": "EventLog",
                "artifact_store": "LocalArtifactStore",
                "recovery_planner": "M1-07C",
            },
        }

    def _scope_view(
        self,
        scope: ObservationScope,
        query: BrowserObservabilityQuery,
    ) -> dict[str, Any]:
        if query.view == "history":
            records = self.history_store.records(
                scope,
                after_sequence=query.after_sequence,
                limit=query.limit,
            )
            return {
                "scope": scope.to_dict(),
                "records": [item.to_dict() for item in records],
                "head": (
                    self.history_store.head(scope).to_dict()
                    if self.history_store.head(scope)
                    else None
                ),
            }
        if query.view == "trace":
            records = self.history_store.records(scope)
            try:
                spans = self.trace_runtime.spans_from_records(scope, records)
                error = ""
            except TracePairingError as exc:
                spans = ()
                error = str(exc)
            return {
                "scope": scope.to_dict(),
                **self.trace_runtime.public_projection(
                    spans,
                    limit=query.limit,
                ),
                "error": error,
            }
        if query.view == "health":
            records = self.history_store.records(
                scope,
                kinds=(HistoryKind.WATCHDOG_SIGNAL, HistoryKind.RECOVERY_INPUT),
                limit=query.limit,
            )
            return {
                "scope": scope.to_dict(),
                "crash_detector": self._crash_state(scope),
                "watchdog_records": [item.to_dict() for item in records],
            }
        if query.view in {"downloads", "screenshots", "artifacts"}:
            role = (
                ArtifactRole.DOWNLOAD
                if query.view == "downloads"
                else ArtifactRole.SCREENSHOT
                if query.view == "screenshots"
                else None
            )
            publications = self.artifact_publisher.publications(
                scope=scope,
                role=role,
            )
            return {
                "scope": scope.to_dict(),
                "artifact_count": len(publications),
                "artifacts": [
                    {
                        "artifact_id": item.artifact.artifact_id,
                        "kind": str(item.artifact.kind),
                        "uri": item.artifact.uri,
                        "title": item.artifact.title,
                        "lineage": item.lineage.to_dict(),
                        "event_id": item.event.event_id,
                    }
                    for item in publications[-query.limit:]
                ],
            }
        projection = self.replay_runtime.replay(scope)
        return projection.to_dict()

    def _crash_state(
        self,
        scope: ObservationScope,
    ) -> Mapping[str, Any]:
        try:
            return self.crash_detector.state(scope)
        except RuntimeError:
            return {
                "scope": scope.to_dict(),
                "phase": "not_attached",
            }
