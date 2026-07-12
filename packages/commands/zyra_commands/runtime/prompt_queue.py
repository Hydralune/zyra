from __future__ import annotations

import copy
import json
import os
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import now_iso

from .schemas import (
    CommandOrigin,
    PromptQueueEntry,
    QueueEntryKind,
    QueueEntryStatus,
    QueuePriority,
    RuntimeGuardState,
)


PRIORITY_ORDER = {
    QueuePriority.NOW: 0,
    QueuePriority.NEXT: 1,
    QueuePriority.LATER: 2,
}


class RuntimeConcurrencyGuard:
    """Session-scoped QueryGuard equivalent."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._states: dict[str, RuntimeGuardState] = {}
        self._active_requests: dict[str, str] = {}

    def state(self, session_id: str) -> RuntimeGuardState:
        with self._lock:
            return self._states.get(session_id, RuntimeGuardState.IDLE)

    def reserve(self, session_id: str, request_id: str) -> bool:
        with self._lock:
            if self._states.get(session_id, RuntimeGuardState.IDLE) != RuntimeGuardState.IDLE:
                return False
            self._states[session_id] = RuntimeGuardState.DISPATCHING
            self._active_requests[session_id] = request_id
            return True

    def start(self, session_id: str, request_id: str) -> None:
        with self._lock:
            if self._active_requests.get(session_id) != request_id:
                raise ValueError("runtime guard request identity mismatch")
            self._states[session_id] = RuntimeGuardState.RUNNING

    def interrupt(self, session_id: str, request_id: str) -> None:
        with self._lock:
            if self._active_requests.get(session_id) != request_id:
                raise ValueError("runtime guard request identity mismatch")
            self._states[session_id] = RuntimeGuardState.INTERRUPTING

    def release(self, session_id: str, request_id: str) -> None:
        with self._lock:
            if self._active_requests.get(session_id) not in {None, request_id}:
                raise ValueError("runtime guard cannot release another request")
            self._states[session_id] = RuntimeGuardState.IDLE
            self._active_requests.pop(session_id, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "states": {key: value.value for key, value in self._states.items()},
                "active_requests": dict(self._active_requests),
            }


class PromptQueueRuntime:
    """Durable, target-isolated now/next/later prompt/control queue."""

    def __init__(self, path: str | Path, *, maximum_entries: int = 10_000, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.maximum_entries = maximum_entries
        self.disabled = disabled
        self._lock = RLock()
        self._entries: dict[str, PromptQueueEntry] = {}
        self._idempotency: dict[str, str] = {}
        self._sequence = 0
        self._load()

    def enqueue(self, entry: PromptQueueEntry) -> PromptQueueEntry:
        self._require_enabled()
        with self._lock:
            active_count = sum(1 for item in self._entries.values() if not item.status.terminal)
            if active_count >= self.maximum_entries:
                raise OverflowError("prompt queue is full")
            existing_id = self._idempotency.get(entry.idempotency_key) if entry.idempotency_key else None
            if existing_id:
                existing = self._entries[existing_id]
                if _entry_body(existing) != _entry_body(entry):
                    raise ValueError("prompt queue idempotency conflict")
                return copy.deepcopy(existing)
            self._sequence += 1
            queued = replace(entry, sequence=self._sequence, status=QueueEntryStatus.QUEUED, updated_at=now_iso())
            self._entries[queued.queue_id] = queued
            if queued.idempotency_key:
                self._idempotency[queued.idempotency_key] = queued.queue_id
            self._persist()
            return copy.deepcopy(queued)

    def reserve_next(
        self,
        *,
        session_id: str,
        target_subagent_task_id: str = "",
        priorities: Sequence[QueuePriority] = (QueuePriority.NOW, QueuePriority.NEXT, QueuePriority.LATER),
        claim_seconds: int = 60,
    ) -> PromptQueueEntry | None:
        self._require_enabled()
        with self._lock:
            candidates = [
                item
                for item in self._entries.values()
                if item.status == QueueEntryStatus.QUEUED
                and item.session_id == session_id
                and item.target_subagent_task_id == target_subagent_task_id
                and item.priority in priorities
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda item: (PRIORITY_ORDER[item.priority], item.sequence, item.created_at))
            selected = candidates[0]
            token = secrets.token_urlsafe(24)
            expires = datetime.now(timezone.utc) + timedelta(seconds=max(1, claim_seconds))
            reserved = replace(
                selected,
                status=QueueEntryStatus.RESERVED,
                claim_token=token,
                claim_expires_at=expires.isoformat(),
                updated_at=now_iso(),
            )
            self._entries[selected.queue_id] = reserved
            self._persist()
            return copy.deepcopy(reserved)

    def start(self, queue_id: str, claim_token: str) -> PromptQueueEntry:
        return self._transition_claimed(queue_id, claim_token, QueueEntryStatus.RUNNING)

    def complete(self, queue_id: str, claim_token: str, *, metadata: Mapping[str, Any] | None = None) -> PromptQueueEntry:
        return self._transition_claimed(queue_id, claim_token, QueueEntryStatus.COMPLETED, metadata=metadata)

    def fail(self, queue_id: str, claim_token: str, *, error: str) -> PromptQueueEntry:
        return self._transition_claimed(queue_id, claim_token, QueueEntryStatus.FAILED, metadata={"error": error})

    def cancel(self, *, queue_id: str = "", idempotency_key: str = "") -> tuple[PromptQueueEntry | None, bool]:
        self._require_enabled()
        with self._lock:
            selected_id = queue_id or self._idempotency.get(idempotency_key, "")
            current = self._entries.get(selected_id)
            if current is None:
                return None, False
            if current.status in {QueueEntryStatus.RESERVED, QueueEntryStatus.RUNNING, QueueEntryStatus.COMPLETED, QueueEntryStatus.FAILED}:
                return copy.deepcopy(current), False
            if current.status == QueueEntryStatus.CANCELLED:
                return copy.deepcopy(current), True
            cancelled = replace(current, status=QueueEntryStatus.CANCELLED, updated_at=now_iso(), claim_token="")
            self._entries[selected_id] = cancelled
            self._persist()
            return copy.deepcopy(cancelled), True

    def queued(
        self,
        *,
        session_id: str | None = None,
        target_subagent_task_id: str | None = None,
        include_terminal: bool = False,
    ) -> tuple[PromptQueueEntry, ...]:
        self._require_enabled()
        with self._lock:
            selected = []
            for item in self._entries.values():
                if session_id is not None and item.session_id != session_id:
                    continue
                if target_subagent_task_id is not None and item.target_subagent_task_id != target_subagent_task_id:
                    continue
                if not include_terminal and item.status.terminal:
                    continue
                selected.append(copy.deepcopy(item))
            return tuple(sorted(selected, key=lambda item: (PRIORITY_ORDER[item.priority], item.sequence)))

    def recover_stale_reservations(self, *, now: datetime | None = None) -> tuple[PromptQueueEntry, ...]:
        current_time = now or datetime.now(timezone.utc)
        recovered = []
        with self._lock:
            for queue_id, item in list(self._entries.items()):
                if item.status not in {QueueEntryStatus.RESERVED, QueueEntryStatus.RUNNING} or not item.claim_expires_at:
                    continue
                try:
                    expiry = datetime.fromisoformat(item.claim_expires_at)
                except ValueError:
                    expiry = current_time - timedelta(seconds=1)
                if expiry > current_time:
                    continue
                changed = replace(
                    item,
                    status=QueueEntryStatus.QUEUED,
                    claim_token="",
                    claim_expires_at="",
                    updated_at=now_iso(),
                    metadata={**item.metadata, "recovered_stale_claim": True},
                )
                self._entries[queue_id] = changed
                recovered.append(copy.deepcopy(changed))
            if recovered:
                self._persist()
        return tuple(recovered)

    def merge_plain_prompts(self, entries: Iterable[PromptQueueEntry], *, maximum_chars: int = 16_000) -> PromptQueueEntry | None:
        selected = list(entries)
        if not selected:
            return None
        first = selected[0]
        if any(
            item.kind != QueueEntryKind.PROMPT
            or item.session_id != first.session_id
            or item.target_key != first.target_key
            or item.mode != first.mode
            or item.origin != first.origin
            or item.parse_slash
            for item in selected
        ):
            raise ValueError("only same-target ordinary prompts may be merged")
        texts = [str(item.payload.get("text") or "") for item in selected]
        merged = "\n\n".join(texts)
        if len(merged) > maximum_chars:
            return None
        return replace(
            first,
            payload={"text": merged, "merged_queue_ids": [item.queue_id for item in selected]},
            queue_id="",
            idempotency_key="",
            sequence=0,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "zyra.prompt-queue/v1",
                "sequence": self._sequence,
                "entries": [item.safe_dict() | {"claim_token": item.claim_token} for item in self._entries.values()],
            }

    def _transition_claimed(
        self,
        queue_id: str,
        claim_token: str,
        status: QueueEntryStatus,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> PromptQueueEntry:
        self._require_enabled()
        with self._lock:
            current = self._entries[queue_id]
            if not secrets.compare_digest(current.claim_token, claim_token):
                raise PermissionError("prompt queue claim token mismatch")
            allowed = {
                QueueEntryStatus.RESERVED: {QueueEntryStatus.RUNNING, QueueEntryStatus.FAILED},
                QueueEntryStatus.RUNNING: {QueueEntryStatus.COMPLETED, QueueEntryStatus.FAILED},
            }
            if status not in allowed.get(current.status, set()):
                raise ValueError(f"invalid prompt queue transition {current.status} -> {status}")
            changed = replace(
                current,
                status=status,
                updated_at=now_iso(),
                claim_token="" if status.terminal else current.claim_token,
                claim_expires_at="" if status.terminal else current.claim_expires_at,
                metadata={**current.metadata, **dict(metadata or {})},
            )
            self._entries[queue_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        entries = [PromptQueueEntry.from_dict(item) for item in raw.get("entries") or () if isinstance(item, Mapping)]
        self._entries = {item.queue_id: item for item in entries}
        self._idempotency = {item.idempotency_key: item.queue_id for item in entries if item.idempotency_key}
        self._sequence = max([int(raw.get("sequence") or 0), *(item.sequence for item in entries)], default=0)

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise RuntimeError("PromptQueueRuntime is disabled")


def _entry_body(entry: PromptQueueEntry) -> dict[str, Any]:
    return {
        "session_id": entry.session_id,
        "kind": entry.kind.value,
        "payload": entry.payload,
        "priority": entry.priority.value,
        "origin": entry.origin.value,
        "target_subagent_task_id": entry.target_subagent_task_id,
        "parse_slash": entry.parse_slash,
        "mode": entry.mode,
    }
