from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol

from .errors import SkillForkUnavailable
from .models import SkillForkRequest, new_id, utc_now


@dataclass(frozen=True, slots=True)
class SkillForkReceipt:
    fork_request_id: str
    task_id: str
    execution_ref: str
    status: str
    accepted_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fork_request_id": self.fork_request_id,
            "task_id": self.task_id,
            "execution_ref": self.execution_ref,
            "status": self.status,
            "accepted_at": self.accepted_at,
        }


class SkillForkPort(Protocol):
    def dispatch(self, request: SkillForkRequest) -> SkillForkReceipt: ...


class UnavailableSkillForkPort:
    def dispatch(self, request: SkillForkRequest) -> SkillForkReceipt:
        raise SkillForkUnavailable(
            "forked skill requires the M1-03D SubagentRuntime port",
            detail={
                "invocation_id": request.invocation_id,
                "agent_type": request.agent_type,
                "version_ref": request.version_ref.immutable_ref,
            },
        )


class DurableForkRequestQueue:
    """Foundation handoff queue; 03D will own execution and child sessions."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._requests: dict[str, SkillForkRequest] = {}
        self._receipts: dict[str, SkillForkReceipt] = {}

    def dispatch(self, request: SkillForkRequest) -> SkillForkReceipt:
        request_id = f"skillfork:{request.invocation_id}"
        with self._lock:
            existing = self._receipts.get(request_id)
            if existing:
                return existing
            self._requests[request_id] = request
            receipt = SkillForkReceipt(
                fork_request_id=request_id,
                task_id=new_id("subagenttask"),
                execution_ref="",
                status="pending_03d_dispatch",
            )
            self._receipts[request_id] = receipt
            return receipt

    def get(self, fork_request_id: str) -> SkillForkRequest:
        with self._lock:
            request = self._requests.get(fork_request_id)
            if request is None:
                raise SkillForkUnavailable("fork request was not found")
            return request

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": 1,
                "requests": {key: value.to_dict() for key, value in self._requests.items()},
                "receipts": {key: value.to_dict() for key, value in self._receipts.items()},
                "owner": "M1-03D SubagentRuntime (handoff only in 03C foundation)",
            }
