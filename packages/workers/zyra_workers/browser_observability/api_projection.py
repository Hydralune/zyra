from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .artifact_publisher import BrowserArtifactPublisher
from .crash_detector import BrowserCrashDetector
from .history_store import BrowserHistoryStore
from .models import ArtifactRole, HistoryKind, ObservationScope
from .replay import BrowserHistoryReplay
from .restart_projection import BrowserRestartProjectionRuntime
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
            "integration",
            "trajectory",
            "commits",
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
        integration_runtime: Any = None,
    ) -> None:
        self.history_store = history_store
        self.replay_runtime = replay
        self.trace_runtime = trace_runtime
        self.crash_detector = crash_detector
        self.watchdog_registry = watchdog_registry
        self.artifact_publisher = artifact_publisher
        self.integration_runtime = integration_runtime
        self.restart_projection = BrowserRestartProjectionRuntime(history_store)

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
        task_artifacts: list[Mapping[str, Any]] = []
        health_counts: dict[str, int] = {}
        for value in scopes:
            scope = _scope_from_mapping(value["scope"])
            rebuilt = self.restart_projection.rebuild(scope)
            task_artifacts.extend(item.to_dict() for item in rebuilt.artifact_lineage)
            for signal in rebuilt.signals:
                status = str(signal.status)
                health_counts[status] = health_counts.get(status, 0) + 1
        return {
            "schema": "zyra.browser-observability.api.v1",
            "view": "summary",
            "task_id": query.task_id,
            "scope_count": len(scopes),
            "scopes": [dict(item) for item in scopes],
            "health": {
                "watchdogs": self.watchdog_registry.snapshot(),
                "crash_detectors": self.crash_detector.snapshots(task_id=query.task_id),
                "durable_signal_statuses": dict(sorted(health_counts.items())),
                "reconstructible_from_history": True,
            },
            "artifacts": {
                "count": len(task_artifacts),
                "screenshots": sum(
                    1 for item in task_artifacts if item.get("role") == str(ArtifactRole.SCREENSHOT)
                ),
                "downloads": sum(
                    1 for item in task_artifacts if item.get("role") == str(ArtifactRole.DOWNLOAD)
                ),
                "traces": sum(
                    1 for item in task_artifacts if item.get("role") == str(ArtifactRole.TRACE)
                ),
                "durable_source": "BrowserHistoryStore",
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
            rebuilt = self.restart_projection.rebuild(scope)
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
            rebuilt = self.restart_projection.rebuild(scope)
            return {
                "scope": scope.to_dict(),
                "crash_detector": self._crash_state(scope),
                "watchdog_records": [item.to_dict() for item in records],
                "aggregate": {
                    "status": str(rebuilt.health_status),
                    "signal_ids": [item.signal_id for item in rebuilt.signals],
                    "recovery_input_ids": [
                        item.input_id for item in rebuilt.recovery_inputs
                    ],
                    "signal_count": len(rebuilt.signals),
                    "recovery_input_count": len(rebuilt.recovery_inputs),
                    "durable_source": "BrowserHistoryStore",
                },
                "reconstructible_from_history": True,
            }
        if query.view in {"downloads", "screenshots", "artifacts"}:
            role = (
                ArtifactRole.DOWNLOAD
                if query.view == "downloads"
                else ArtifactRole.SCREENSHOT
                if query.view == "screenshots"
                else None
            )
            rebuilt = self.restart_projection.rebuild(scope)
            publications = tuple(
                {
                    "artifact_id": item.artifact_id,
                    "role": str(item.role),
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "media_type": item.media_type,
                    "quarantined": item.quarantined,
                    "source_event_ids": list(item.source_event_ids),
                    "source_record_ids": list(item.source_record_ids),
                    "parent_artifact_ids": list(item.parent_artifact_ids),
                    "lineage_receipt_id": item.receipt_id,
                    "metadata": dict(item.metadata),
                }
                for item in rebuilt.artifact_lineage
                if role is None or item.role == role
            )
            return {
                "scope": scope.to_dict(),
                "artifact_count": len(publications),
                "artifacts": list(publications[-query.limit:]),
                "durable_source": "BrowserHistoryStore",
                "raw_filesystem_paths_exposed": False,
            }
        if query.view == "integration":
            if self.integration_runtime is None:
                return {"scope": scope.to_dict(), "available": False}
            return {
                "scope": scope.to_dict(),
                **self.integration_runtime.projection(scope=scope),
            }
        if query.view == "trajectory":
            if self.integration_runtime is None:
                return {"scope": scope.to_dict(), "available": False}
            return self.integration_runtime.trajectory.projection(
                scope,
                after_sequence=query.after_sequence,
                limit=query.limit,
            )
        if query.view == "commits":
            if self.integration_runtime is None:
                return {"scope": scope.to_dict(), "available": False}
            return {
                "scope": scope.to_dict(),
                **self.integration_runtime.commit_fence.projection(scope=scope),
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


def _scope_from_mapping(value: Mapping[str, Any]) -> ObservationScope:
    return ObservationScope(
        run_id=str(value["run_id"]),
        task_id=str(value["task_id"]),
        node_id=str(value.get("node_id") or ""),
        browser_session_id=str(value["browser_session_id"]),
        canonical_session_id=str(value.get("canonical_session_id") or ""),
        worker_request_id=str(value["worker_request_id"]),
    )


def _artifact_lineages(records: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if record.kind != HistoryKind.ARTIFACT_PUBLISHED:
            continue
        lineage = record.payload.get("lineage")
        if not isinstance(lineage, Mapping):
            continue
        receipt_id = str(lineage.get("receipt_id") or record.record_id)
        if receipt_id in seen:
            continue
        seen.add(receipt_id)
        output.append(
            {
                "artifact_id": str(lineage.get("artifact_id") or ""),
                "role": str(lineage.get("role") or ""),
                "sha256": str(lineage.get("sha256") or ""),
                "size_bytes": int(lineage.get("size_bytes") or 0),
                "media_type": str(lineage.get("media_type") or ""),
                "quarantined": bool(lineage.get("quarantined")),
                "source_event_ids": list(lineage.get("source_event_ids") or ()),
                "source_record_ids": list(lineage.get("source_record_ids") or ()),
                "parent_artifact_ids": list(lineage.get("parent_artifact_ids") or ()),
                "lineage_receipt_id": receipt_id,
                "history_record_id": record.record_id,
                "history_sequence": record.sequence,
                "metadata": dict(lineage.get("metadata") or {}),
            }
        )
    return tuple(output)


def _health_aggregate(records: Sequence[Any]) -> dict[str, Any]:
    rank = {"unknown": 0, "healthy": 1, "degraded": 2, "unhealthy": 3, "terminated": 4}
    statuses: list[str] = []
    signal_ids: list[str] = []
    recovery_input_ids: list[str] = []
    for record in records:
        if record.kind == HistoryKind.WATCHDOG_SIGNAL:
            signal = record.payload.get("signal")
            if isinstance(signal, Mapping):
                statuses.append(str(signal.get("status") or "unknown"))
                signal_ids.append(str(signal.get("signal_id") or ""))
        elif record.kind == HistoryKind.RECOVERY_INPUT:
            value = record.payload.get("recovery_input")
            if isinstance(value, Mapping):
                recovery_input_ids.append(str(value.get("input_id") or ""))
    status = max(statuses, key=lambda item: rank.get(item, 0), default="unknown")
    return {
        "status": status,
        "signal_ids": [item for item in signal_ids if item],
        "recovery_input_ids": [item for item in recovery_input_ids if item],
        "signal_count": len(statuses),
        "recovery_input_count": len(recovery_input_ids),
        "durable_source": "BrowserHistoryStore",
    }
