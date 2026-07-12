from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from zyra_core import now_iso

from .schemas import (
    CommandStatus,
    ControlCommandRequest,
    ControlCommandResponse,
    ControlError,
    ControlErrorCode,
    ControlResult,
)


ALLOWED_STATUS_TRANSITIONS: dict[CommandStatus, frozenset[CommandStatus]] = {
    CommandStatus.RECEIVED: frozenset({CommandStatus.VALIDATED, CommandStatus.FAILED, CommandStatus.DENIED, CommandStatus.CONFLICT, CommandStatus.CANCELLED}),
    CommandStatus.VALIDATED: frozenset({CommandStatus.QUEUED, CommandStatus.DISPATCHED, CommandStatus.RUNNING, CommandStatus.FAILED, CommandStatus.DENIED, CommandStatus.CONFLICT, CommandStatus.CANCELLED}),
    CommandStatus.QUEUED: frozenset({CommandStatus.DISPATCHED, CommandStatus.CANCELLED, CommandStatus.FAILED}),
    CommandStatus.DISPATCHED: frozenset({CommandStatus.RUNNING, CommandStatus.CANCELLED, CommandStatus.FAILED}),
    CommandStatus.RUNNING: frozenset({CommandStatus.SUCCEEDED, CommandStatus.FAILED, CommandStatus.DENIED, CommandStatus.CANCELLED, CommandStatus.CONFLICT}),
    CommandStatus.SUCCEEDED: frozenset(),
    CommandStatus.FAILED: frozenset(),
    CommandStatus.DENIED: frozenset(),
    CommandStatus.CANCELLED: frozenset(),
    CommandStatus.CONFLICT: frozenset(),
}


@dataclass(slots=True)
class ControlRequestRecord:
    request: ControlCommandRequest
    status: CommandStatus = CommandStatus.RECEIVED
    revision: int = 0
    queue_id: str = ""
    response: ControlCommandResponse | None = None
    event_ids: list[str] = field(default_factory=list)
    received_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    started_at: str = ""
    finished_at: str = ""
    claim_token: str = field(default="", repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "status": self.status.value,
            "revision": self.revision,
            "queue_id": self.queue_id,
            "response": self.response.to_dict() if self.response else None,
            "event_ids": list(self.event_ids),
            "received_at": self.received_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "claim_token_present": bool(self.claim_token),
            "metadata": copy.deepcopy(self.metadata),
        }

    def private_dict(self) -> dict[str, Any]:
        return {**self.safe_dict(), "claim_token": self.claim_token}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ControlRequestRecord":
        response_raw = raw.get("response") if isinstance(raw.get("response"), Mapping) else None
        return cls(
            request=ControlCommandRequest.from_dict(raw.get("request") if isinstance(raw.get("request"), Mapping) else {}),
            status=_status(raw.get("status")),
            revision=int(raw.get("revision") or 0),
            queue_id=str(raw.get("queue_id") or ""),
            response=ControlCommandResponse.from_dict(response_raw) if response_raw else None,
            event_ids=[str(item) for item in raw.get("event_ids") or ()],
            received_at=str(raw.get("received_at") or now_iso()),
            updated_at=str(raw.get("updated_at") or now_iso()),
            started_at=str(raw.get("started_at") or ""),
            finished_at=str(raw.get("finished_at") or ""),
            claim_token=str(raw.get("claim_token") or ""),
            metadata=dict(raw.get("metadata") or {}),
        )


class ControlRequestStore:
    """Durable command request lifecycle and idempotency owner."""

    def __init__(self, path: str | Path, *, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.disabled = disabled
        self._lock = RLock()
        self._records: dict[str, ControlRequestRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._load()

    def receive(self, request: ControlCommandRequest) -> ControlRequestRecord:
        self._require_enabled()
        with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id:
                existing = self._records[existing_id]
                if existing.request.body_digest != request.body_digest:
                    conflict = ControlCommandResponse(
                        request_id=request.request_id,
                        command_id=request.command_id,
                        status=CommandStatus.CONFLICT,
                        registry_generation=request.registry_generation,
                        error=ControlError(
                            ControlErrorCode.DUPLICATE_CONFLICT,
                            "idempotency key was reused with a different command body",
                            details={"existing_request_id": existing.request.request_id},
                        ),
                    )
                    return ControlRequestRecord(
                        request=request,
                        status=CommandStatus.CONFLICT,
                        response=conflict,
                        metadata={"duplicate_of": existing.request.request_id},
                    )
                return copy.deepcopy(existing)
            if request.request_id in self._records:
                existing = self._records[request.request_id]
                if existing.request.body_digest != request.body_digest:
                    raise ValueError("request_id was reused with different content")
                return copy.deepcopy(existing)
            record = ControlRequestRecord(
                request=request,
                metadata={"body_digest": request.body_digest},
            )
            self._records[request.request_id] = record
            self._idempotency[request.idempotency_key] = request.request_id
            self._persist()
            return copy.deepcopy(record)

    def get(self, request_id: str) -> ControlRequestRecord:
        self._require_enabled()
        with self._lock:
            if request_id not in self._records:
                raise KeyError(request_id)
            return copy.deepcopy(self._records[request_id])

    def list(self, *, status: CommandStatus | None = None, session_id: str | None = None) -> tuple[ControlRequestRecord, ...]:
        self._require_enabled()
        with self._lock:
            result = []
            for record in self._records.values():
                if status is not None and record.status != status:
                    continue
                if session_id is not None and record.request.session_id != session_id:
                    continue
                result.append(copy.deepcopy(record))
            return tuple(sorted(result, key=lambda item: (item.received_at, item.request.request_id)))

    def transition(
        self,
        request_id: str,
        status: CommandStatus,
        *,
        expected_revision: int | None = None,
        queue_id: str | None = None,
        event_id: str = "",
        claim_token: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ControlRequestRecord:
        self._require_enabled()
        with self._lock:
            current = self._records[request_id]
            if expected_revision is not None and current.revision != expected_revision:
                raise ValueError(f"control request revision conflict: expected {expected_revision}, actual {current.revision}")
            if status != current.status and status not in ALLOWED_STATUS_TRANSITIONS[current.status]:
                raise ValueError(f"invalid control request transition {current.status} -> {status}")
            changed = copy.deepcopy(current)
            changed.status = status
            changed.revision += 1
            changed.updated_at = now_iso()
            if status == CommandStatus.RUNNING and not changed.started_at:
                changed.started_at = changed.updated_at
            if status.terminal:
                changed.finished_at = changed.updated_at
            if queue_id is not None:
                changed.queue_id = queue_id
            if event_id and event_id not in changed.event_ids:
                changed.event_ids.append(event_id)
            if claim_token is not None:
                changed.claim_token = claim_token
            if metadata:
                changed.metadata.update(copy.deepcopy(dict(metadata)))
            self._records[request_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def complete(self, request_id: str, response: ControlCommandResponse) -> ControlRequestRecord:
        self._require_enabled()
        with self._lock:
            current = self._records[request_id]
            if current.status.terminal:
                if current.response and current.response.to_dict() == response.to_dict():
                    return copy.deepcopy(current)
                raise ValueError("terminal control request cannot be completed twice with different responses")
            if response.status not in ALLOWED_STATUS_TRANSITIONS[current.status]:
                raise ValueError(f"invalid terminal transition {current.status} -> {response.status}")
            changed = copy.deepcopy(current)
            changed.status = response.status
            changed.response = response
            changed.revision += 1
            changed.updated_at = now_iso()
            changed.finished_at = response.finished_at
            changed.event_ids = list(dict.fromkeys([*changed.event_ids, *response.event_ids]))
            changed.claim_token = ""
            self._records[request_id] = changed
            self._persist()
            return copy.deepcopy(changed)

    def cancel(self, request_id: str, *, reason: str) -> ControlRequestRecord:
        current = self.get(request_id)
        if current.status.terminal:
            return current
        response = ControlCommandResponse(
            request_id=current.request.request_id,
            command_id=current.request.command_id,
            status=CommandStatus.CANCELLED,
            registry_generation=current.request.registry_generation,
            error=ControlError(ControlErrorCode.CANCELLED, reason),
            started_at=current.started_at,
            metadata={"cancel_reason": reason},
        )
        return self.complete(request_id, response)

    def recover_running(self) -> tuple[ControlRequestRecord, ...]:
        """Move crash-interrupted requests back to validated for safe replay."""

        recovered = []
        with self._lock:
            for request_id, current in list(self._records.items()):
                if current.status not in {CommandStatus.DISPATCHED, CommandStatus.RUNNING}:
                    continue
                changed = copy.deepcopy(current)
                changed.status = CommandStatus.VALIDATED
                changed.revision += 1
                changed.updated_at = now_iso()
                changed.claim_token = ""
                changed.metadata["recovered_after_restart"] = True
                self._records[request_id] = changed
                recovered.append(copy.deepcopy(changed))
            if recovered:
                self._persist()
        return tuple(recovered)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "zyra.control-request-store/v1",
                "records": [record.private_dict() for record in sorted(self._records.values(), key=lambda item: item.request.request_id)],
            }

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        records = [ControlRequestRecord.from_dict(item) for item in raw.get("records") or () if isinstance(item, Mapping)]
        self._records = {item.request.request_id: item for item in records}
        self._idempotency = {item.request.idempotency_key: item.request.request_id for item in records}

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise RuntimeError("ControlRequestStore is disabled")


def _status(value: Any) -> CommandStatus:
    try:
        return CommandStatus(str(value))
    except ValueError:
        return CommandStatus.RECEIVED
