from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from zyra_core import TaskState

from ..history_store import BrowserHistoryConflict, BrowserHistoryStore
from ..models import HistoryKind, HistoryRecord, ObservationScope, digest_value, utc_now


@dataclass(frozen=True, slots=True)
class BrowserTrajectoryPolicy:
    maximum_records: int = 5_000
    include_payload: bool = True
    require_stable_head: bool = True
    include_artifact_lineage: bool = True

    def __post_init__(self) -> None:
        if self.maximum_records < 1:
            raise ValueError("trajectory record limit must be positive")


@dataclass(frozen=True, slots=True)
class BrowserTrajectoryCursor:
    scope_key: str
    head_digest: str
    after_sequence: int = 0

    def __post_init__(self) -> None:
        if not self.scope_key:
            raise ValueError("trajectory cursor requires scope key")
        if self.after_sequence < 0:
            raise ValueError("trajectory cursor sequence must be non-negative")

    @property
    def token(self) -> str:
        return digest_value(self.to_dict())[7:39]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "head_digest": self.head_digest,
            "after_sequence": self.after_sequence,
        }


@dataclass(frozen=True, slots=True)
class BrowserTrajectorySnapshot:
    scope: ObservationScope
    cursor: BrowserTrajectoryCursor
    events: tuple[Mapping[str, Any], ...]
    record_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    created_at: str = field(default_factory=utc_now)

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.browser-observability.trajectory-snapshot.v1",
            "scope": self.scope.to_dict(),
            "cursor": {**self.cursor.to_dict(), "token": self.cursor.token},
            "events": [dict(item) for item in self.events],
            "record_ids": list(self.record_ids),
            "artifact_ids": list(self.artifact_ids),
            "created_at": self.created_at,
            "record_count": len(self.record_ids),
            "history_owner": "BrowserHistoryStore",
            "trajectory_owner": "MemoryFabric",
        }
        if include_digest:
            value["digest"] = digest_value(value)
        return value


class BrowserTrajectoryProjection:
    """Create immutable MemoryFabric input from a committed history head."""

    def __init__(
        self,
        history_store: BrowserHistoryStore,
        *,
        policy: BrowserTrajectoryPolicy | None = None,
    ) -> None:
        self.history_store = history_store
        self.policy = policy or BrowserTrajectoryPolicy()

    def snapshot(
        self,
        scope: ObservationScope,
        *,
        after_sequence: int = 0,
        expected_head_digest: str = "",
        limit: int | None = None,
    ) -> BrowserTrajectorySnapshot:
        head_before = self.history_store.head(scope)
        head_digest = head_before.content_digest if head_before else ""
        if expected_head_digest and expected_head_digest != head_digest:
            raise BrowserHistoryConflict(
                "trajectory cursor no longer matches the committed history head"
            )
        maximum = min(
            self.policy.maximum_records,
            self.policy.maximum_records if limit is None else max(0, limit),
        )
        records = self.history_store.records(
            scope,
            after_sequence=after_sequence,
            limit=maximum,
        )
        head_after = self.history_store.head(scope)
        after_digest = head_after.content_digest if head_after else ""
        if self.policy.require_stable_head and after_digest != head_digest:
            raise BrowserHistoryConflict(
                "history changed while creating an immutable trajectory snapshot"
            )
        events = tuple(self._event(record, head_digest) for record in records)
        artifacts = tuple(
            dict.fromkeys(
                artifact_id
                for record in records
                for artifact_id in record.artifact_ids
            )
        )
        return BrowserTrajectorySnapshot(
            scope=scope,
            cursor=BrowserTrajectoryCursor(
                scope_key=scope.key,
                head_digest=head_digest,
                after_sequence=(records[-1].sequence if records else after_sequence),
            ),
            events=events,
            record_ids=tuple(item.record_id for item in records),
            artifact_ids=artifacts,
        )

    def replay(
        self,
        state: TaskState,
        scope: ObservationScope,
        memory_fabric: Any,
        *,
        after_sequence: int = 0,
        expected_head_digest: str = "",
        limit: int | None = None,
    ) -> tuple[BrowserTrajectorySnapshot, tuple[Any, ...]]:
        if state.run_id != scope.run_id or state.task_id != scope.task_id:
            raise ValueError("trajectory TaskState does not match browser scope")
        snapshot = self.snapshot(
            scope,
            after_sequence=after_sequence,
            expected_head_digest=expected_head_digest,
            limit=limit,
        )
        frames = tuple(memory_fabric.replay_trajectory(state, snapshot.events))
        for frame, event in zip(frames, snapshot.events, strict=True):
            frame.metadata.update(
                {
                    "source": "BrowserHistoryStore",
                    "history_scope_key": scope.key,
                    "history_head_digest": snapshot.cursor.head_digest,
                    "history_record_id": event["payload"]["history_record_id"],
                    "history_sequence": event["payload"]["history_sequence"],
                }
            )
        return snapshot, frames

    def projection(
        self,
        scope: ObservationScope,
        *,
        after_sequence: int = 0,
        expected_head_digest: str = "",
        limit: int | None = None,
    ) -> dict[str, Any]:
        snapshot = self.snapshot(
            scope,
            after_sequence=after_sequence,
            expected_head_digest=expected_head_digest,
            limit=limit,
        )
        return snapshot.to_dict()

    def _event(
        self,
        record: HistoryRecord,
        head_digest: str,
    ) -> dict[str, Any]:
        event_type = _event_type(record.kind)
        payload: dict[str, Any] = {
            "history_record_id": record.record_id,
            "history_sequence": record.sequence,
            "history_kind": str(record.kind),
            "history_scope_key": record.scope.key,
            "history_head_digest": head_digest,
            "history_record_digest": record.content_digest,
            "tool_call_id": record.tool_call_id,
            "branch_id": record.branch_id,
            "causal_event_ids": list(record.causal_event_ids),
            "artifact_ids": list(record.artifact_ids),
            "status": _status(record),
            "worker": "BrowserWorker",
            "route": "browser",
        }
        if self.policy.include_payload:
            payload["browser_history"] = dict(record.payload)
        return {
            "event_id": f"browser-history:{record.record_id}",
            "run_id": record.scope.run_id,
            "task_id": record.scope.task_id,
            "node_id": record.scope.node_id or None,
            "event_type": event_type,
            "created_at": record.created_at,
            "payload": payload,
        }


def _event_type(kind: HistoryKind) -> str:
    return {
        HistoryKind.SESSION_STARTED: "browser_session_started",
        HistoryKind.ACTION_PLANNED: "browser_action_planned",
        HistoryKind.TOOL_CALL: "tool_started",
        HistoryKind.TOOL_RESULT: "tool_finished",
        HistoryKind.OBSERVATION: "browser_observation",
        HistoryKind.STATE_CAPTURE: "browser_state_captured",
        HistoryKind.ARTIFACT_PUBLISHED: "artifact_written",
        HistoryKind.WATCHDOG_SIGNAL: "worker_health",
        HistoryKind.SESSION_STOPPED: "browser_session_stopped",
        HistoryKind.RECOVERY_INPUT: "browser_recovery_input",
        HistoryKind.TRACE_SPAN: "trace_span",
        HistoryKind.JUDGE_ADVISORY: "judge_advisory",
    }[kind]


def _status(record: HistoryRecord) -> str:
    value = record.payload.get("status")
    if value:
        return str(value)
    if record.kind == HistoryKind.TOOL_RESULT:
        return "completed" if bool(record.payload.get("ok")) else "failed"
    if record.kind == HistoryKind.RECOVERY_INPUT:
        return "handoff"
    return "recorded"
