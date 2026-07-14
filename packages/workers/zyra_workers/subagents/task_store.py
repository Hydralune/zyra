from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable, Mapping

from zyra_core import now_iso

from .digests import digest_object
from .errors import (
    SubagentDisabled,
    SubagentRevisionConflict,
    SubagentTaskNotFound,
    SubagentTransitionRejected,
)
from .models import (
    RecoverySignal,
    StructuredHandoff,
    StructuredSubagentMessage,
    SubagentTaskRecord,
    SubagentTaskStatus,
    UsageLedger,
)


ALLOWED_TRANSITIONS: dict[SubagentTaskStatus, frozenset[SubagentTaskStatus]] = {
    SubagentTaskStatus.CREATED: frozenset({SubagentTaskStatus.VALIDATING, SubagentTaskStatus.CANCELLED, SubagentTaskStatus.FAILED}),
    SubagentTaskStatus.VALIDATING: frozenset({SubagentTaskStatus.READY, SubagentTaskStatus.CANCELLED, SubagentTaskStatus.FAILED}),
    SubagentTaskStatus.READY: frozenset({SubagentTaskStatus.DISPATCHED, SubagentTaskStatus.CANCELLED, SubagentTaskStatus.FAILED}),
    SubagentTaskStatus.DISPATCHED: frozenset({SubagentTaskStatus.RUNNING, SubagentTaskStatus.CANCELLED, SubagentTaskStatus.FAILED}),
    SubagentTaskStatus.RUNNING: frozenset({SubagentTaskStatus.WAITING, SubagentTaskStatus.COMPLETED, SubagentTaskStatus.FAILED, SubagentTaskStatus.CANCELLED, SubagentTaskStatus.KILLED, SubagentTaskStatus.CLEANUP_FAILED}),
    SubagentTaskStatus.WAITING: frozenset({SubagentTaskStatus.RESUMING, SubagentTaskStatus.COMPLETED, SubagentTaskStatus.FAILED, SubagentTaskStatus.CANCELLED, SubagentTaskStatus.CLEANUP_FAILED}),
    SubagentTaskStatus.RESUMING: frozenset({SubagentTaskStatus.DISPATCHED, SubagentTaskStatus.RUNNING, SubagentTaskStatus.FAILED, SubagentTaskStatus.CANCELLED}),
    SubagentTaskStatus.COMPLETED: frozenset({SubagentTaskStatus.RESUMING, SubagentTaskStatus.CLEANUP_FAILED}),
    SubagentTaskStatus.FAILED: frozenset({SubagentTaskStatus.RESUMING, SubagentTaskStatus.CLEANUP_FAILED}),
    SubagentTaskStatus.CANCELLED: frozenset({SubagentTaskStatus.CLEANUP_FAILED}),
    SubagentTaskStatus.KILLED: frozenset({SubagentTaskStatus.RESUMING, SubagentTaskStatus.CLEANUP_FAILED}),
    SubagentTaskStatus.CLEANUP_FAILED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TaskMutationReceipt:
    task_id: str
    revision_before: int
    revision_after: int
    status_before: SubagentTaskStatus
    status_after: SubagentTaskStatus
    mutation: str
    digest: str
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "status_before": self.status_before.value,
            "status_after": self.status_after.value,
            "mutation": self.mutation,
            "digest": self.digest,
            "created_at": self.created_at,
        }


class SubagentTaskStore:
    """Sole durable owner of logical SubagentTask aggregates.

    Records intentionally exclude worker_id, lease_id, capacity, heartbeat and
    physical workspace lifecycle state. Those are downstream projections owned
    by 07A and 05A.
    """

    def __init__(self, path: str | Path, *, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.disabled = disabled
        self._lock = RLock()
        self._tasks: dict[str, SubagentTaskRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._load()

    def create(self, record: SubagentTaskRecord, *, idempotency_key: str) -> SubagentTaskRecord:
        self._require_enabled()
        with self._lock:
            existing_id = self._idempotency.get(idempotency_key)
            if existing_id:
                existing = self._tasks[existing_id]
                if existing.prompt_digest != record.prompt_digest or existing.agent_type != record.agent_type:
                    raise SubagentRevisionConflict(existing.task_id, existing.revision, existing.revision + 1)
                return copy.deepcopy(existing)
            if record.task_id in self._tasks:
                raise SubagentRevisionConflict(record.task_id, 0, self._tasks[record.task_id].revision)
            record.metadata = {**record.metadata, "idempotency_key": idempotency_key}
            self._tasks[record.task_id] = copy.deepcopy(record)
            self._idempotency[idempotency_key] = record.task_id
            self._persist()
            return copy.deepcopy(record)

    def get(self, task_id: str) -> SubagentTaskRecord:
        self._require_enabled()
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise SubagentTaskNotFound(task_id)
            return copy.deepcopy(record)

    def refresh(self) -> None:
        """Reload durable state written by another runtime process/port."""

        self._require_enabled()
        with self._lock:
            self._load()

    def list(
        self,
        *,
        run_id: str | None = None,
        parent_task_id: str | None = None,
        include_terminal: bool = True,
    ) -> tuple[SubagentTaskRecord, ...]:
        self._require_enabled()
        with self._lock:
            selected = []
            for record in self._tasks.values():
                if run_id is not None and record.run_id != run_id:
                    continue
                if parent_task_id is not None and record.parent_task_id != parent_task_id:
                    continue
                if not include_terminal and record.status.terminal:
                    continue
                selected.append(copy.deepcopy(record))
            return tuple(sorted(selected, key=lambda item: (item.created_at, item.task_id)))

    def transition(
        self,
        task_id: str,
        status: SubagentTaskStatus,
        *,
        expected_revision: int | None = None,
        mutation: str = "transition",
        update: Callable[[SubagentTaskRecord], None] | None = None,
    ) -> tuple[SubagentTaskRecord, TaskMutationReceipt]:
        self._require_enabled()
        with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise SubagentTaskNotFound(task_id)
            if expected_revision is not None and current.revision != expected_revision:
                raise SubagentRevisionConflict(task_id, expected_revision, current.revision)
            if status != current.status and status not in ALLOWED_TRANSITIONS[current.status]:
                raise SubagentTransitionRejected(task_id, current.status.value, status.value)
            changed = copy.deepcopy(current)
            before = current.status
            changed.status = status
            changed.revision += 1
            changed.updated_at = now_iso()
            if status.terminal:
                changed.completed_at = changed.updated_at
            if update:
                update(changed)
            self._tasks[task_id] = changed
            self._persist()
            receipt = TaskMutationReceipt(
                task_id=task_id,
                revision_before=current.revision,
                revision_after=changed.revision,
                status_before=before,
                status_after=status,
                mutation=mutation,
                digest=digest_object(changed.safe_dict()),
            )
            return copy.deepcopy(changed), receipt

    def mutate(
        self,
        task_id: str,
        mutation: str,
        callback: Callable[[SubagentTaskRecord], None],
        *,
        expected_revision: int | None = None,
    ) -> tuple[SubagentTaskRecord, TaskMutationReceipt]:
        current = self.get(task_id)
        return self.transition(
            task_id,
            current.status,
            expected_revision=expected_revision,
            mutation=mutation,
            update=callback,
        )

    def attach_dispatch(
        self,
        task_id: str,
        *,
        dispatch_request: Mapping[str, Any],
        execution_ref: str,
        expected_revision: int,
    ) -> tuple[SubagentTaskRecord, TaskMutationReceipt]:
        def update(record: SubagentTaskRecord) -> None:
            record.dispatch_request = copy.deepcopy(dict(dispatch_request))
            record.execution_ref = execution_ref
            record.attempt += 1

        return self.transition(
            task_id,
            SubagentTaskStatus.DISPATCHED,
            expected_revision=expected_revision,
            mutation="dispatch_attached",
            update=update,
        )

    def append_message(
        self,
        task_id: str,
        message: StructuredSubagentMessage,
        *,
        maximum_pending: int = 64,
    ) -> SubagentTaskRecord:
        def update(record: SubagentTaskRecord) -> None:
            if len(record.pending_messages) >= maximum_pending:
                raise ValueError("subagent pending message queue is full")
            if any(item.message_id == message.message_id for item in record.pending_messages):
                return
            record.pending_messages.append(copy.deepcopy(message))

        record, _ = self.mutate(task_id, "message_queued", update)
        return record

    def drain_messages(self, task_id: str) -> tuple[StructuredSubagentMessage, ...]:
        drained: list[StructuredSubagentMessage] = []

        def update(record: SubagentTaskRecord) -> None:
            drained.extend(copy.deepcopy(record.pending_messages))
            record.pending_messages.clear()

        self.mutate(task_id, "messages_drained", update)
        return tuple(drained)

    def attach_handoff(
        self,
        task_id: str,
        handoff: StructuredHandoff,
        *,
        status: SubagentTaskStatus,
        expected_revision: int | None = None,
    ) -> SubagentTaskRecord:
        def update(record: SubagentTaskRecord) -> None:
            record.handoff = copy.deepcopy(handoff)
            record.usage = handoff.usage
            if handoff.recovery_signal:
                record.recovery_signals.append(copy.deepcopy(handoff.recovery_signal))

        record, _ = self.transition(
            task_id,
            status,
            expected_revision=expected_revision,
            mutation="handoff_attached",
            update=update,
        )
        return record

    def append_recovery_signal(self, task_id: str, signal: RecoverySignal) -> SubagentTaskRecord:
        def update(record: SubagentTaskRecord) -> None:
            if not any(item.signal_id == signal.signal_id for item in record.recovery_signals):
                record.recovery_signals.append(copy.deepcopy(signal))

        record, _ = self.mutate(task_id, "recovery_signal_appended", update)
        return record

    def add_child(self, parent_task_id: str, child_task_id: str) -> SubagentTaskRecord | None:
        if parent_task_id not in self._tasks:
            return None

        def update(record: SubagentTaskRecord) -> None:
            if child_task_id not in record.child_task_ids:
                record.child_task_ids.append(child_task_id)

        record, _ = self.mutate(parent_task_id, "child_added", update)
        return record

    def descendants(self, task_id: str) -> tuple[SubagentTaskRecord, ...]:
        self._require_enabled()
        result: list[SubagentTaskRecord] = []
        seen: set[str] = set()
        queue = [task_id]
        while queue:
            parent = queue.pop(0)
            for record in self.list():
                if record.parent_task_id != parent or record.task_id in seen:
                    continue
                seen.add(record.task_id)
                result.append(record)
                queue.append(record.task_id)
        return tuple(result)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            tasks = [record.safe_dict() for record in sorted(self._tasks.values(), key=lambda item: item.task_id)]
            payload = {
                "schema": "zyra.subagent-task-store/v1",
                "owner": "M1-03D SubagentTaskStore",
                "forbidden_physical_fields": ["worker_id", "lease_id", "capacity", "heartbeat"],
                "tasks": tasks,
            }
            return {**payload, "digest": digest_object(payload)}

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        tasks = raw.get("tasks") if isinstance(raw, Mapping) else None
        if isinstance(tasks, list):
            records = [SubagentTaskRecord.from_dict(item) for item in tasks if isinstance(item, Mapping)]
        elif isinstance(tasks, Mapping):
            records = [SubagentTaskRecord.from_dict(item) for item in tasks.values() if isinstance(item, Mapping)]
        else:
            records = []
        self._tasks = {item.task_id: item for item in records}
        self._idempotency = {
            str(item.metadata.get("idempotency_key")): item.task_id
            for item in records
            if item.metadata.get("idempotency_key")
        }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.snapshot()
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise SubagentDisabled("AgentTaskLifecycleRuntime")
