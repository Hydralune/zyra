from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .memory_index import MemoryIndexRuntime, MemoryIndexSyncResult
from .models import MemoryRecord
from .retrieval_models import IndexCursor, IndexSourceKind, stable_digest


class ChangeDisposition(StrEnum):
    ENQUEUED = "enqueued"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class IndexSourceChange:
    source_kind: IndexSourceKind
    source_id: str
    source_revision: str
    task_id: str
    run_id: str = ""
    session_id: str = ""
    artifact_ids: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    causation_id: str = ""

    @property
    def scope_key(self) -> str:
        return MemoryIndexRuntime.task_scope(self.task_id)

    @property
    def content_digest(self) -> str:
        return stable_digest(
            self.source_kind.value,
            self.source_id,
            self.source_revision,
            self.task_id,
            self.run_id,
            self.session_id,
            self.artifact_ids,
            self.changed_paths,
            self.payload,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "artifact_ids": list(self.artifact_ids),
            "changed_paths": list(self.changed_paths),
            "payload": dict(self.payload),
            "causation_id": self.causation_id,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class IncrementalUpdateReceipt:
    disposition: ChangeDisposition
    change: IndexSourceChange
    sync: MemoryIndexSyncResult | None = None
    previous_revision: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "change": self.change.to_dict(),
            "sync": self.sync.to_dict() if self.sync else None,
            "previous_revision": self.previous_revision,
            "reason": self.reason,
        }


class IncrementalIndexUpdater:
    """Idempotent bridge from canonical change receipts to derived rebuilds.

    The updater never turns an event payload into a competing memory fact.
    Events, artifacts, tool results, browser traces, skill invocations, and
    failures must first be committed/projected by their canonical owners.  The
    updater records a small consumption cursor and requests a task generation.
    """

    SUPPORTED_EVENT_KINDS: Mapping[str, IndexSourceKind] = {
        "artifact": IndexSourceKind.ARTIFACT,
        "artifact_created": IndexSourceKind.ARTIFACT,
        "tool_call": IndexSourceKind.TOOL_TRACE,
        "tool_result": IndexSourceKind.TOOL_TRACE,
        "browser_action": IndexSourceKind.BROWSER_TRACE,
        "browser_result": IndexSourceKind.BROWSER_TRACE,
        "skill_invoked": IndexSourceKind.SKILL,
        "failure_injected": IndexSourceKind.FAILURE,
        "node_failed": IndexSourceKind.FAILURE,
    }

    def __init__(self, runtime: MemoryIndexRuntime) -> None:
        self.runtime = runtime

    def apply(
        self,
        change: IndexSourceChange,
        *,
        canonical_records: Sequence[MemoryRecord] | None = None,
        process: bool = False,
    ) -> IncrementalUpdateReceipt:
        if not change.source_id or not change.source_revision or not change.task_id:
            return IncrementalUpdateReceipt(
                disposition=ChangeDisposition.INVALID,
                change=change,
                reason="source_id, source_revision, and task_id are required",
            )
        previous = self.runtime.index.get_cursor(
            change.source_kind,
            change.source_id,
            change.scope_key,
        )
        if (
            previous is not None
            and previous.source_revision == change.source_revision
            and previous.content_digest == change.content_digest
        ):
            return IncrementalUpdateReceipt(
                disposition=ChangeDisposition.DUPLICATE,
                change=change,
                previous_revision=previous.source_revision,
                reason="source cursor already covers this revision and digest",
            )
        sync = self.runtime.synchronize_task(
            change.task_id,
            records=canonical_records,
            process=process,
            causation_id=change.causation_id or change.source_id,
        )
        self.runtime.index.put_cursor(
            IndexCursor(
                source_kind=change.source_kind,
                source_id=change.source_id,
                source_revision=change.source_revision,
                content_digest=change.content_digest,
                scope_key=change.scope_key,
                generation=sync.generation,
            )
        )
        return IncrementalUpdateReceipt(
            disposition=ChangeDisposition.ENQUEUED if sync.changed else ChangeDisposition.DUPLICATE,
            change=change,
            sync=sync,
            previous_revision=previous.source_revision if previous else "",
            reason="canonical projection changed" if sync.changed else "task generation already current",
        )

    def from_memory_records(
        self,
        task_id: str,
        records: Sequence[MemoryRecord],
        *,
        process: bool = False,
        causation_id: str = "",
    ) -> IncrementalUpdateReceipt:
        revision = self.runtime.source_revision(records)
        change = IndexSourceChange(
            source_kind=IndexSourceKind.MEMORY,
            source_id=task_id,
            source_revision=revision,
            task_id=task_id,
            run_id=records[0].run_id if records else "",
            payload={"record_count": len(records)},
            causation_id=causation_id,
        )
        return self.apply(change, canonical_records=records, process=process)

    def from_event(
        self,
        event: Mapping[str, Any],
        *,
        process: bool = False,
    ) -> IncrementalUpdateReceipt:
        event_type = str(event.get("event_type") or "")
        source_kind = self.SUPPORTED_EVENT_KINDS.get(event_type, IndexSourceKind.EVENT)
        event_id = str(event.get("event_id") or "")
        task_id = str(event.get("task_id") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        revision = str(event.get("created_at") or stable_digest(event))
        artifact_ids = self._artifact_ids(payload)
        change = IndexSourceChange(
            source_kind=source_kind,
            source_id=event_id,
            source_revision=revision,
            task_id=task_id,
            run_id=str(event.get("run_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            artifact_ids=artifact_ids,
            payload={
                "event_type": event_type,
                "payload_digest": stable_digest(payload),
                "canonical_projection_required": True,
            },
            causation_id=str(payload.get("causation_id") or event_id),
        )
        return self.apply(change, process=process)

    def from_artifact(
        self,
        *,
        task_id: str,
        run_id: str,
        artifact_id: str,
        artifact_revision: str,
        causation_id: str = "",
        process: bool = False,
    ) -> IncrementalUpdateReceipt:
        change = IndexSourceChange(
            source_kind=IndexSourceKind.ARTIFACT,
            source_id=artifact_id,
            source_revision=artifact_revision,
            task_id=task_id,
            run_id=run_id,
            artifact_ids=(artifact_id,),
            payload={"canonical_projection_required": True},
            causation_id=causation_id,
        )
        return self.apply(change, process=process)

    def reconcile_events(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        process_final: bool = True,
    ) -> tuple[IncrementalUpdateReceipt, ...]:
        receipts: list[IncrementalUpdateReceipt] = []
        for index, event in enumerate(events):
            receipts.append(
                self.from_event(
                    event,
                    process=process_final and index == len(events) - 1,
                )
            )
        return tuple(receipts)

    @staticmethod
    def _artifact_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
        found: set[str] = set()
        stack: list[Any] = [payload]
        while stack:
            value = stack.pop()
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if str(key) == "artifact_id" and str(item):
                        found.add(str(item))
                    elif str(key) == "artifact_ids" and isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                        found.update(str(entry) for entry in item if str(entry))
                    elif isinstance(item, (Mapping, list, tuple)):
                        stack.append(item)
            elif isinstance(value, (list, tuple)):
                stack.extend(value)
        return tuple(sorted(found))
