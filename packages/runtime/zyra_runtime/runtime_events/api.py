"""HTTP-facing query and projection facade for runtime events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .integration import RuntimeEventSpineBridge
from .models import JsonValue, RuntimeEventContractError, RuntimeEventQuery, coerce_json


@dataclass(frozen=True, slots=True)
class RuntimeEventApiResult:
    status: int
    body: Mapping[str, JsonValue]
    headers: Mapping[str, str]


class RuntimeEventApiFacade:
    def __init__(self, bridge: RuntimeEventSpineBridge) -> None:
        self.bridge = bridge

    def list_events(self, params: Mapping[str, Any]) -> RuntimeEventApiResult:
        query = RuntimeEventQuery.from_params(params)
        page = self.bridge.query(query)
        return RuntimeEventApiResult(
            status=200,
            body=page.to_jsonable(),
            headers=self._cursor_headers(page.next_sequence, page.high_watermark),
        )

    def list_task_events(self, task_id: str, params: Mapping[str, Any]) -> RuntimeEventApiResult:
        task_id = task_id.strip()
        if not task_id:
            raise RuntimeEventContractError("task_id must not be empty")
        mutable = dict(params)
        mutable.setdefault("task_id", task_id)
        query = RuntimeEventQuery.from_params(mutable)
        page = self.bridge.query(query)
        return RuntimeEventApiResult(
            status=200,
            body={"taskId": task_id, **page.to_jsonable()},
            headers=self._cursor_headers(page.next_sequence, page.high_watermark),
        )

    def get_event(self, event_id: str) -> RuntimeEventApiResult:
        event = self.bridge.get_event(event_id)
        if event is None:
            return RuntimeEventApiResult(
                status=404,
                body={"error": "runtime_event_not_found", "eventId": event_id},
                headers={},
            )
        return RuntimeEventApiResult(status=200, body=event.to_jsonable(), headers={})

    def get_task_projection(self, task_id: str) -> RuntimeEventApiResult:
        task_id = task_id.strip()
        if not task_id:
            raise RuntimeEventContractError("task_id must not be empty")
        view = self.bridge.get_task_view(task_id)
        if view is None:
            return RuntimeEventApiResult(
                status=404,
                body={"error": "runtime_projection_not_found", "taskId": task_id},
                headers={},
            )
        return RuntimeEventApiResult(
            status=200,
            body={
                "schema": "zyra.runtime-task-projection-api/v1",
                # Preserve the public projection kind while making the new
                # durable projector's ownership explicit in a separate field.
                "projection": "task",
                "projectionOwner": "runtime-state-projector",
                "key": task_id,
                "state": dict(view),
                "canonicalWriteAllowed": False,
            },
            headers={"X-Zyra-Projection-Cursor": str(view.get("lastGlobalSequence", 0))},
        )

    def get_task_history(self, task_id: str, params: Mapping[str, Any]) -> RuntimeEventApiResult:
        after = int(params.get("after_sequence", params.get("afterSequence", 0)) or 0)
        max_events_raw = params.get("limit", params.get("max_events", 1000))
        max_events = min(max(int(max_events_raw), 1), 5000)
        view = self.bridge.get_task_view(task_id)
        if view is None:
            return RuntimeEventApiResult(
                status=404,
                body={"error": "runtime_projection_not_found", "taskId": task_id},
                headers={},
            )
        aggregate_id = str(view.get("aggregateId") or "")
        history = self.bridge.get_projected_history(
            aggregate_id,
            after_global_sequence=after,
            limit=min(max_events, 1000),
        )
        cursor = history.get("cursor", {})
        cursor_mapping = cursor if isinstance(cursor, Mapping) else {}
        return RuntimeEventApiResult(
            status=200,
            body=dict(history),
            headers=self._cursor_headers(
                int(cursor_mapping.get("globalSequence", 0) or 0),
                int(history.get("highWatermark", 0) or 0),
            ),
        )

    def get_task_projection_stream(
        self,
        task_id: str,
        params: Mapping[str, Any],
    ) -> RuntimeEventApiResult:
        view = self.bridge.get_task_view(task_id)
        if view is None:
            return RuntimeEventApiResult(
                status=404,
                body={"error": "runtime_projection_not_found", "taskId": task_id},
                headers={},
            )
        after = int(params.get("after_global_sequence", params.get("afterGlobalSequence", 0)) or 0)
        generation = int(params.get("generation", 1) or 1)
        limit = min(max(int(params.get("limit", 200) or 200), 1), 1000)
        frames = self.bridge.get_projection_stream(
            str(view.get("aggregateId") or ""),
            cursor={
                "aggregateId": str(view.get("aggregateId") or ""),
                "globalSequence": after,
                "projectionSequence": int(view.get("lastAggregateSequence", -1) or -1),
                "generation": generation,
            },
            limit=limit,
        )
        last_cursor = frames[-1].get("cursor", {}) if frames else {}
        last_cursor_mapping = last_cursor if isinstance(last_cursor, Mapping) else {}
        return RuntimeEventApiResult(
            status=200,
            body={
                "schema": "zyra.runtime-projection-stream-api/v1",
                "taskId": task_id,
                "frames": [dict(item) for item in frames],
                "canonicalWriteAllowed": False,
            },
            headers={
                "X-Zyra-Projection-Cursor": str(last_cursor_mapping.get("globalSequence", after)),
                "Cache-Control": "no-store",
            },
        )

    def read_artifact(self, artifact_id: str, params: Mapping[str, Any]) -> RuntimeEventApiResult:
        expected_digest = params.get("expected_digest", params.get("expectedDigest"))
        length_raw = params.get("length")
        result = self.bridge.read_artifact(
            artifact_id,
            expected_digest=str(expected_digest) if expected_digest else None,
            offset=int(params.get("offset", 0) or 0),
            length=int(length_raw) if length_raw is not None else None,
            encoding=str(params.get("encoding", "base64") or "base64"),
        )
        return RuntimeEventApiResult(
            status=200,
            body=dict(result),
            headers={"Cache-Control": "private, no-store"},
        )

    def reconciliation(self, params: Mapping[str, Any]) -> RuntimeEventApiResult:
        allowed = {
            "aggregateId": params.get("aggregate_id", params.get("aggregateId")),
            "repairDeliveries": str(params.get("repair_deliveries", "true")).lower() in {"1", "true", "yes", "on"},
            "repairProjection": str(params.get("repair_projection", "true")).lower() in {"1", "true", "yes", "on"},
            "rebuildProjection": str(params.get("rebuild_projection", "false")).lower() in {"1", "true", "yes", "on"},
        }
        result = self.bridge.reconcile({key: value for key, value in allowed.items() if value is not None})
        status = 200 if bool(result.get("equivalentAfterRepair", False)) else 409
        return RuntimeEventApiResult(status=status, body=dict(result), headers={"Cache-Control": "no-store"})

    def health(self) -> RuntimeEventApiResult:
        health = self.bridge.health()
        status = 200 if health.ok else 503
        return RuntimeEventApiResult(status=status, body=health.to_jsonable(), headers={})

    def metrics(self) -> RuntimeEventApiResult:
        metrics = self.bridge.metrics()
        return RuntimeEventApiResult(
            status=200,
            body={key: coerce_json(item) for key, item in metrics.items()},
            headers={},
        )

    def baselines(self) -> RuntimeEventApiResult:
        comparison = self.bridge.baselines()
        return RuntimeEventApiResult(
            status=200,
            body={key: coerce_json(item) for key, item in comparison.items()},
            headers={"Cache-Control": "no-store"},
        )

    def causal_chain(self, event_id: str, *, max_depth: int = 256) -> RuntimeEventApiResult:
        event = self.bridge.get_event(event_id)
        if event is None:
            return RuntimeEventApiResult(
                status=404,
                body={"error": "runtime_event_not_found", "eventId": event_id},
                headers={},
            )
        chain = []
        current = event
        seen: set[str] = set()
        while current is not None and len(chain) < max_depth and current.event_id not in seen:
            seen.add(current.event_id)
            chain.append(current)
            current = self.bridge.get_event(current.causation_id) if current.causation_id else None
        chain.reverse()
        return RuntimeEventApiResult(
            status=200,
            body={
                "eventId": event_id,
                "chain": [event.to_jsonable() for event in chain],
                "length": len(chain),
            },
            headers={},
        )

    def _cursor_headers(self, cursor: int, high_watermark: int) -> Mapping[str, str]:
        return {
            "X-Zyra-Event-Cursor": str(cursor),
            "X-Zyra-Event-High-Watermark": str(high_watermark),
            "Cache-Control": "no-store",
        }
