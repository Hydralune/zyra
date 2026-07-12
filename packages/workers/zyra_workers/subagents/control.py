from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Protocol

from zyra_core import new_id, now_iso

from .continuation import ContinuationRequest
from .digests import digest_object


class SubagentControlAction(StrEnum):
    STATUS = "status"
    CANCEL = "cancel"
    MESSAGE = "message"
    BACKGROUND = "background"


class SubagentControlStatus(StrEnum):
    RECEIVED = "received"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class SubagentControlRequest:
    root_task_id: str
    subagent_task_id: str
    action: SubagentControlAction
    arguments: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: new_id("subcontrol"))
    idempotency_key: str = ""
    expected_task_revision: int | None = None
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", copy.deepcopy(dict(self.arguments)))
        if not self.idempotency_key:
            object.__setattr__(self, "idempotency_key", f"subagent-control:{self.request_id}")

    @property
    def body_digest(self) -> str:
        return digest_object({
            "root_task_id": self.root_task_id,
            "subagent_task_id": self.subagent_task_id,
            "action": self.action,
            "arguments": self.arguments,
            "expected_task_revision": self.expected_task_revision,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.subagent-control-request/v1",
            "root_task_id": self.root_task_id,
            "subagent_task_id": self.subagent_task_id,
            "action": self.action.value,
            "arguments": copy.deepcopy(self.arguments),
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "expected_task_revision": self.expected_task_revision,
            "created_at": self.created_at,
            "body_digest": self.body_digest,
        }


@dataclass(frozen=True, slots=True)
class SubagentControlResponse:
    request_id: str
    status: SubagentControlStatus
    action: SubagentControlAction
    subagent_task_id: str
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    revision_before: int | None = None
    revision_after: int | None = None
    finished_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return self.status == SubagentControlStatus.SUCCEEDED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.subagent-control-response/v1",
            "ok": self.ok,
            "request_id": self.request_id,
            "status": self.status.value,
            "action": self.action.value,
            "subagent_task_id": self.subagent_task_id,
            "result": copy.deepcopy(self.result),
            "error": self.error,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SubagentControlResponse":
        return cls(
            request_id=str(raw.get("request_id") or ""),
            status=SubagentControlStatus(str(raw.get("status") or SubagentControlStatus.FAILED.value)),
            action=SubagentControlAction(str(raw.get("action") or SubagentControlAction.STATUS.value)),
            subagent_task_id=str(raw.get("subagent_task_id") or ""),
            result=dict(raw.get("result") or {}),
            error=str(raw.get("error") or ""),
            revision_before=(int(raw["revision_before"]) if raw.get("revision_before") is not None else None),
            revision_after=(int(raw["revision_after"]) if raw.get("revision_after") is not None else None),
            finished_at=str(raw.get("finished_at") or now_iso()),
        )


@dataclass(slots=True)
class SubagentControlRecord:
    request: SubagentControlRequest
    status: SubagentControlStatus = SubagentControlStatus.RECEIVED
    response: SubagentControlResponse | None = None
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "status": self.status.value,
            "response": self.response.to_dict() if self.response else None,
            "updated_at": self.updated_at,
        }


class SubagentControlTarget(Protocol):
    task_store: Any

    def cancel(self, task_id: str, *, reason: str) -> tuple[Any, ...]: ...
    def promote_to_background(self, task_id: str) -> Any: ...
    def send_message(self, request: ContinuationRequest) -> Any: ...


class SubagentControlRuntime:
    """Durable operator controls over Zyra-owned logical subagent state."""

    def __init__(self, target: SubagentControlTarget, state_path: str | Path, *, disabled: bool = False) -> None:
        self.target = target
        self.state_path = Path(state_path).resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.disabled = disabled
        self._lock = RLock()
        self._records: dict[str, SubagentControlRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._history: dict[str, tuple[str, SubagentControlResponse]] = {}
        self._load()

    def execute(self, request: SubagentControlRequest) -> SubagentControlResponse:
        if self.disabled:
            raise RuntimeError("SubagentControlRuntime is disabled")
        with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id:
                existing = self._records[existing_id]
                if existing.request.body_digest != request.body_digest:
                    return self._conflict(request, "subagent control idempotency conflict")
                if existing.response is not None:
                    return copy.deepcopy(existing.response)
            historical = self._history.get(request.idempotency_key)
            if historical is not None:
                body_digest, response = historical
                if body_digest != request.body_digest:
                    return self._conflict(request, "historical subagent control idempotency conflict")
                return copy.deepcopy(response)
            self._records[request.request_id] = SubagentControlRecord(request=request)
            self._idempotency[request.idempotency_key] = request.request_id
            self._persist()
        try:
            record = self.target.task_store.get(request.subagent_task_id)
            if record.parent_task_id != request.root_task_id:
                raise ValueError("subagent control parent identity mismatch")
            before = record.revision
            if request.expected_task_revision is not None and request.expected_task_revision != before:
                raise ValueError(f"subagent task revision conflict: expected {request.expected_task_revision}, actual {before}")
            self._set_status(request.request_id, SubagentControlStatus.RUNNING)
            result = self._apply(request, record)
            after = self.target.task_store.get(request.subagent_task_id).revision
            response = SubagentControlResponse(
                request_id=request.request_id,
                status=SubagentControlStatus.SUCCEEDED,
                action=request.action,
                subagent_task_id=request.subagent_task_id,
                result=result,
                revision_before=before,
                revision_after=after,
            )
        except Exception as error:
            response = SubagentControlResponse(
                request_id=request.request_id,
                status=SubagentControlStatus.FAILED,
                action=request.action,
                subagent_task_id=request.subagent_task_id,
                error=str(error),
            )
        with self._lock:
            current = self._records[request.request_id]
            current.status = response.status
            current.response = response
            current.updated_at = response.finished_at
            self._persist()
        return response

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": "zyra.subagent-control-store/v1",
                "records": [item.to_dict() for item in self._records.values()],
                "historical_idempotency_keys": sorted(self._history),
                "physical_worker_state_owned": False,
            }

    def _apply(self, request: SubagentControlRequest, record: Any) -> dict[str, Any]:
        if request.action == SubagentControlAction.STATUS:
            return {"subagent": record.safe_dict()}
        if request.action == SubagentControlAction.CANCEL:
            changed = self.target.cancel(
                request.subagent_task_id,
                reason=str(request.arguments.get("reason") or "operator_cancelled"),
            )
            return {"cancelled": [item.safe_dict() for item in changed]}
        if request.action == SubagentControlAction.BACKGROUND:
            before_ref = record.execution_ref
            changed = self.target.promote_to_background(request.subagent_task_id)
            if changed.execution_ref != before_ref:
                raise RuntimeError("background promotion changed execution_ref")
            return {"subagent": changed.safe_dict(), "execution_ref_reused": True}
        if request.action == SubagentControlAction.MESSAGE:
            receipt = self.target.send_message(ContinuationRequest(
                task_id=request.subagent_task_id,
                sender_task_id=request.root_task_id,
                message=str(request.arguments.get("message") or ""),
                intent=str(request.arguments.get("intent") or "continue"),
                idempotency_key=request.idempotency_key,
                metadata={"control_request_id": request.request_id},
            ))
            return {"continuation": receipt.to_dict()}
        raise ValueError(f"unsupported subagent control action: {request.action}")

    def _conflict(self, request: SubagentControlRequest, message: str) -> SubagentControlResponse:
        return SubagentControlResponse(
            request_id=request.request_id,
            status=SubagentControlStatus.CONFLICT,
            action=request.action,
            subagent_task_id=request.subagent_task_id,
            error=message,
        )

    def _set_status(self, request_id: str, status: SubagentControlStatus) -> None:
        with self._lock:
            current = self._records[request_id]
            current.status = status
            current.updated_at = now_iso()
            self._persist()

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.state_path)

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in raw.get("records") or ():
            request_raw = item.get("request") if isinstance(item, Mapping) else None
            if isinstance(request_raw, Mapping):
                key = str(request_raw.get("idempotency_key") or "")
                response_raw = item.get("response") if isinstance(item.get("response"), Mapping) else None
                body_digest = str(request_raw.get("body_digest") or "")
                if key and body_digest and response_raw:
                    self._history[key] = (body_digest, SubagentControlResponse.from_dict(response_raw))
