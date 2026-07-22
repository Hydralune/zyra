from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping

from zyra_orchestration.graph_custody import DynamicTopologyRuntime, GraphStateCustody

from .application import WorkerPoolFoundationRuntime
from .integration_store import WorkerPoolIntegrationRepository
from .models import PoolJournalRecord, stable_digest, utc_iso


@dataclass(frozen=True, slots=True)
class ProjectionCursor:
    journal_sequence: int
    pool_revision: int
    graph_revisions: Mapping[str, int]
    issued_at: str = field(default_factory=utc_iso)
    checksum: str = ""

    def __post_init__(self) -> None:
        if self.journal_sequence < 0 or self.pool_revision < 0:
            raise ValueError("projection cursor revisions cannot be negative")
        normalized = {str(key): int(value) for key, value in self.graph_revisions.items()}
        object.__setattr__(self, "graph_revisions", dict(sorted(normalized.items())))
        expected = stable_digest(self.to_dict(include_checksum=False))
        if self.checksum and self.checksum != expected:
            raise ValueError("worker-pool projection cursor checksum mismatch")
        object.__setattr__(self, "checksum", expected)

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        result = {
            "journal_sequence": self.journal_sequence,
            "pool_revision": self.pool_revision,
            "graph_revisions": dict(self.graph_revisions),
            "issued_at": self.issued_at,
        }
        if include_checksum:
            result["checksum"] = self.checksum
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProjectionCursor":
        return cls(
            journal_sequence=int(value.get("journal_sequence") or 0),
            pool_revision=int(value.get("pool_revision") or 0),
            graph_revisions={
                str(key): int(item)
                for key, item in dict(value.get("graph_revisions") or {}).items()
            },
            issued_at=str(value.get("issued_at") or utc_iso()),
            checksum=str(value.get("checksum") or ""),
        )


@dataclass(frozen=True, slots=True)
class PoolProjectionEvent:
    sequence: int
    event_id: str
    kind: str
    aggregate_type: str
    aggregate_id: str
    task_id: str
    run_id: str
    causation_id: str
    correlation_id: str
    payload: Mapping[str, Any]
    created_at: str

    @classmethod
    def from_journal(cls, record: PoolJournalRecord) -> "PoolProjectionEvent":
        return cls(
            sequence=record.sequence,
            event_id=record.journal_id,
            kind=record.operation,
            aggregate_type=record.aggregate_type,
            aggregate_id=record.aggregate_id,
            task_id=record.task_id,
            run_id=record.run_id,
            causation_id=record.causation_id,
            correlation_id=record.correlation_id,
            payload=copy.deepcopy(dict(record.payload)),
            created_at=record.created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "kind": self.kind,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "payload": copy.deepcopy(dict(self.payload)),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class WorkerPoolHandoff:
    cursor: ProjectionCursor
    workers: tuple[Mapping[str, Any], ...]
    bindings: tuple[Mapping[str, Any], ...]
    controls: tuple[Mapping[str, Any], ...]
    recovery: tuple[Mapping[str, Any], ...]
    graphs: tuple[Mapping[str, Any], ...]
    events: tuple[PoolProjectionEvent, ...]
    has_more: bool
    custody: Mapping[str, Any]
    captured_at: str = field(default_factory=utc_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursor": self.cursor.to_dict(),
            "workers": [copy.deepcopy(dict(item)) for item in self.workers],
            "bindings": [copy.deepcopy(dict(item)) for item in self.bindings],
            "controls": [copy.deepcopy(dict(item)) for item in self.controls],
            "recovery": [copy.deepcopy(dict(item)) for item in self.recovery],
            "graphs": [copy.deepcopy(dict(item)) for item in self.graphs],
            "events": [item.to_dict() for item in self.events],
            "has_more": self.has_more,
            "custody": copy.deepcopy(dict(self.custody)),
            "captured_at": self.captured_at,
        }


class WorkerPoolProjectionRuntime:
    """Cursor-safe worker state handoff for M2 without a second reducer store."""

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
        graph_custody: GraphStateCustody,
    ) -> None:
        self.pool = pool
        self.repository = repository
        self.graph_custody = graph_custody
        self.topology = DynamicTopologyRuntime(graph_custody)

    def handoff(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 500,
        run_id: str = "",
        task_id: str = "",
    ) -> WorkerPoolHandoff:
        selected_limit = max(1, min(5000, int(limit)))
        records = self.pool.store.journal(
            after_sequence=max(0, int(after_sequence)),
            task_id=task_id,
            run_id=run_id,
            limit=selected_limit + 1,
        )
        has_more = len(records) > selected_limit
        records = records[:selected_limit]
        events = tuple(PoolProjectionEvent.from_journal(item) for item in records)
        workers = self.pool.api_projection().get("workers") or ()
        bindings = self.repository.list_bindings(run_id=run_id, task_id=task_id)
        controls = self.repository.list_controls(task_id=task_id)
        recovery = self.repository.list_recovery(task_id=task_id)
        graph_ids = sorted({item.foreign_refs.graph.object_id for item in bindings if item.foreign_refs.graph.object_id})
        graphs: list[Mapping[str, Any]] = []
        graph_revisions: dict[str, int] = {}
        for graph_id in graph_ids:
            try:
                reference = self.topology.version_ref(graph_id)
                graphs.append(reference.to_dict())
                graph_revisions[graph_id] = reference.revision
            except KeyError:
                graphs.append({"graph_id": graph_id, "missing": True})
                graph_revisions[graph_id] = -1
        last_sequence = events[-1].sequence if events else max(0, int(after_sequence))
        cursor = ProjectionCursor(
            journal_sequence=last_sequence,
            pool_revision=self.pool.store.revision,
            graph_revisions=graph_revisions,
        )
        return WorkerPoolHandoff(
            cursor=cursor,
            workers=tuple(copy.deepcopy(dict(item)) for item in workers),
            bindings=tuple(item.to_dict() for item in bindings),
            controls=tuple(item.to_dict() for item in controls),
            recovery=tuple(item.to_dict() for item in recovery),
            graphs=tuple(graphs),
            events=events,
            has_more=has_more,
            custody={
                "projection_only": True,
                "canonical_state_owner": "python.WorkerPoolStore",
                "canonical_worker_owner": "WorkerPoolStore",
                "canonical_graph_owner": "GraphStateCustody",
                "logical_task_owner": "typescript.AgentTaskRuntime",
                "cursor_source": "WorkerPoolStore.worker_pool_journal",
            },
        )

    def resume(
        self,
        cursor_value: Mapping[str, Any],
        *,
        limit: int = 500,
        run_id: str = "",
        task_id: str = "",
    ) -> WorkerPoolHandoff:
        if not str(cursor_value.get("checksum") or ""):
            raise ValueError("projection resume requires a checksummed cursor")
        cursor = ProjectionCursor.from_dict(cursor_value)
        if cursor.pool_revision > self.pool.store.revision:
            raise ValueError("projection cursor belongs to a future worker-pool revision")
        for graph_id, revision in cursor.graph_revisions.items():
            if revision < 0:
                continue
            current = self.topology.version_ref(graph_id)
            if current.revision < revision:
                raise ValueError(f"projection cursor graph revision is in the future: {graph_id}")
        return self.handoff(
            after_sequence=cursor.journal_sequence,
            limit=limit,
            run_id=run_id,
            task_id=task_id,
        )

    def causal_chain(self, task_id: str, *, limit: int = 5000) -> tuple[PoolProjectionEvent, ...]:
        records = self.pool.store.journal(task_id=task_id, limit=limit)
        ordered = sorted(records, key=lambda item: item.sequence)
        seen: set[str] = set()
        output: list[PoolProjectionEvent] = []
        for record in ordered:
            if record.journal_id in seen:
                continue
            seen.add(record.journal_id)
            output.append(PoolProjectionEvent.from_journal(record))
        return tuple(output)


__all__ = [
    "PoolProjectionEvent",
    "ProjectionCursor",
    "WorkerPoolHandoff",
    "WorkerPoolProjectionRuntime",
]
