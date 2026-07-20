"""HTTP-facing query and projection facade for runtime events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .integration import RuntimeEventSpineBridge
from .models import JsonValue, RuntimeEventContractError, RuntimeEventQuery, coerce_json
from .openhands_fold import EventHistoryFold


@dataclass(frozen=True, slots=True)
class RuntimeEventApiResult:
    status: int
    body: Mapping[str, JsonValue]
    headers: Mapping[str, str]


class RuntimeEventApiFacade:
    def __init__(self, bridge: RuntimeEventSpineBridge) -> None:
        self.bridge = bridge
        self.history = EventHistoryFold(bridge)

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
        for projection_name in ("session", "task", "runtime"):
            snapshot = self.bridge.get_projection(projection_name, task_id)
            if snapshot is not None:
                return RuntimeEventApiResult(
                    status=200,
                    body=snapshot.to_jsonable(),
                    headers={"X-Zyra-Projection-Cursor": str(snapshot.cursor)},
                )
        history = self.history.fold_session(task_id, max_events=1000)
        if history.cursor.event_count == 0:
            return RuntimeEventApiResult(
                status=404,
                body={"error": "runtime_projection_not_found", "taskId": task_id},
                headers={},
            )
        return RuntimeEventApiResult(
            status=200,
            body={"projection": "openhands-history", "key": task_id, "state": history.to_jsonable()},
            headers={"X-Zyra-Projection-Cursor": str(history.cursor.global_sequence)},
        )

    def get_task_history(self, task_id: str, params: Mapping[str, Any]) -> RuntimeEventApiResult:
        after = int(params.get("after_sequence", params.get("afterSequence", 0)) or 0)
        max_events_raw = params.get("limit", params.get("max_events", 1000))
        max_events = min(max(int(max_events_raw), 1), 5000)
        include_hidden_raw = params.get("include_hidden", params.get("includeHidden", False))
        include_hidden = (
            str(include_hidden_raw).lower() in {"1", "true", "yes", "on"}
            if isinstance(include_hidden_raw, str)
            else bool(include_hidden_raw)
        )
        history = self.history.fold_session(
            task_id,
            after_sequence=after,
            include_hidden=include_hidden,
            max_events=max_events,
        )
        return RuntimeEventApiResult(
            status=200,
            body=history.to_jsonable(),
            headers=self._cursor_headers(history.cursor.global_sequence, history.cursor.high_watermark),
        )

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
        chain = self.history.causal_chain(event_id, max_depth=max_depth)
        if not chain:
            return RuntimeEventApiResult(
                status=404,
                body={"error": "runtime_event_not_found", "eventId": event_id},
                headers={},
            )
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
