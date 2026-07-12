from __future__ import annotations

"""Durable logical fan-out/fan-in and foreground/background promotion.

This module selectively internalizes the useful TaskTool/AsyncJob semantics
without importing an OMP global registry.  Every group, child binding,
progress update, delivery retry and terminal aggregate is persisted by Zyra.
Physical worker capacity is intentionally absent.
"""

import copy
import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import Condition, Event, RLock, Semaphore, Thread
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso

from .execution_receipts import ExecutionAttemptReceipt, ExecutionPhase, ExecutionReceiptStore
from .models import AgentExecutionMode, SubagentProgress, SubagentSpawnRequest, SubagentTaskStatus
from .typed_yield import TypedYield, TypedYieldStore, YieldAssembly, YieldContract, YieldKind, default_yield_contract


class FanoutError(RuntimeError):
    pass


class FanoutDisabled(FanoutError):
    pass


class FanoutConflict(FanoutError):
    pass


class FanoutNotFound(FanoutError):
    pass


class FanoutCapacityExceeded(FanoutError):
    pass


class FanoutStatus(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    QUEUED = "queued"
    RUNNING = "running"
    DETACHED = "detached"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARKED = "parked"

    @property
    def terminal(self) -> bool:
        return self in {
            FanoutStatus.COMPLETED,
            FanoutStatus.PARTIAL,
            FanoutStatus.FAILED,
            FanoutStatus.CANCELLED,
            FanoutStatus.PARKED,
        }


class FanoutFailurePolicy(StrEnum):
    FAIL_FAST = "fail_fast"
    COLLECT_ALL = "collect_all"
    REQUIRE_ALL = "require_all"
    REQUIRE_ANY = "require_any"


class ChildFanoutStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    YIELDED = "yielded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARKED = "parked"

    @property
    def terminal(self) -> bool:
        return self in {
            ChildFanoutStatus.YIELDED,
            ChildFanoutStatus.FAILED,
            ChildFanoutStatus.CANCELLED,
            ChildFanoutStatus.PARKED,
        }


@dataclass(frozen=True, slots=True)
class FanoutItem:
    item_id: str
    name: str
    agent_type: str
    prompt: str
    requested_tools: tuple[str, ...] = ()
    requested_mcp_servers: tuple[str, ...] = ()
    requested_model: str = ""
    requested_writable_paths: tuple[str, ...] = ()
    requested_readable_paths: tuple[str, ...] = ()
    constraints: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("item_id", "name", "agent_type", "prompt"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")
        object.__setattr__(self, "requested_tools", _unique_strings(self.requested_tools))
        object.__setattr__(self, "requested_mcp_servers", _unique_strings(self.requested_mcp_servers))
        object.__setattr__(self, "requested_writable_paths", _unique_strings(self.requested_writable_paths))
        object.__setattr__(self, "requested_readable_paths", _unique_strings(self.requested_readable_paths))
        object.__setattr__(self, "constraints", _safe_mapping(self.constraints))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "name": self.name,
            "agent_type": self.agent_type,
            "prompt": self.prompt,
            "requested_tools": list(self.requested_tools),
            "requested_mcp_servers": list(self.requested_mcp_servers),
            "requested_model": self.requested_model,
            "requested_writable_paths": list(self.requested_writable_paths),
            "requested_readable_paths": list(self.requested_readable_paths),
            "constraints": _safe_mapping(self.constraints),
            "metadata": _safe_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FanoutItem":
        return cls(
            item_id=str(value.get("item_id") or new_id("fanitem")),
            name=str(value.get("name") or ""),
            agent_type=str(value.get("agent_type") or "general-purpose"),
            prompt=str(value.get("prompt") or ""),
            requested_tools=tuple(str(item) for item in value.get("requested_tools") or ()),
            requested_mcp_servers=tuple(str(item) for item in value.get("requested_mcp_servers") or ()),
            requested_model=str(value.get("requested_model") or ""),
            requested_writable_paths=tuple(str(item) for item in value.get("requested_writable_paths") or ()),
            requested_readable_paths=tuple(str(item) for item in value.get("requested_readable_paths") or ()),
            constraints=_safe_mapping(value.get("constraints")),
            metadata=_safe_mapping(value.get("metadata")),
        )


@dataclass(frozen=True, slots=True)
class FanoutRequest:
    run_id: str
    parent_task_id: str
    parent_session_id: str
    shared_context: str
    items: tuple[FanoutItem, ...]
    idempotency_key: str
    execution_mode: AgentExecutionMode = AgentExecutionMode.FOREGROUND
    failure_policy: FanoutFailurePolicy = FanoutFailurePolicy.REQUIRE_ALL
    maximum_concurrency: int = 4
    promotion_after_ms: int = 0
    yield_contract: YieldContract | None = None
    parent_scope_snapshot_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("run_id", "parent_task_id", "parent_session_id", "idempotency_key"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} is required")
        if not self.items:
            raise ValueError("fan-out requires at least one item")
        if self.maximum_concurrency <= 0:
            raise ValueError("maximum_concurrency must be positive")
        names = [item.name.casefold() for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("fan-out item names must be unique case-insensitively")
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("fan-out item ids must be unique")
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "metadata", _safe_mapping(self.metadata))

    @property
    def request_digest(self) -> str:
        return _digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "run_id": self.run_id,
            "parent_task_id": self.parent_task_id,
            "parent_session_id": self.parent_session_id,
            "shared_context": self.shared_context,
            "items": [item.to_dict() for item in self.items],
            "idempotency_key": self.idempotency_key,
            "execution_mode": self.execution_mode.value,
            "failure_policy": self.failure_policy.value,
            "maximum_concurrency": self.maximum_concurrency,
            "promotion_after_ms": max(0, int(self.promotion_after_ms)),
            "yield_contract": self.yield_contract.to_dict() if self.yield_contract else None,
            "parent_scope_snapshot_id": self.parent_scope_snapshot_id,
            "metadata": _safe_mapping(self.metadata),
        }
        if include_digest:
            value["request_digest"] = _digest(value)
        return value


@dataclass(frozen=True, slots=True)
class FanoutChildRecord:
    item: FanoutItem
    task_id: str
    status: ChildFanoutStatus
    execution_ref: str = ""
    execution_receipt_id: str = ""
    yield_assembly_digest: str = ""
    progress: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    started_at: str = ""
    completed_at: str = ""
    revision: int = 0

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item.to_dict(),
            "task_id": self.task_id,
            "status": self.status.value,
            "execution_ref": self.execution_ref,
            "execution_receipt_id": self.execution_receipt_id,
            "yield_assembly_digest": self.yield_assembly_digest,
            "progress": _safe_mapping(self.progress),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FanoutChildRecord":
        return cls(
            item=FanoutItem.from_dict(_mapping(value.get("item"))),
            task_id=str(value.get("task_id") or ""),
            status=ChildFanoutStatus(str(value.get("status") or ChildFanoutStatus.PENDING.value)),
            execution_ref=str(value.get("execution_ref") or ""),
            execution_receipt_id=str(value.get("execution_receipt_id") or ""),
            yield_assembly_digest=str(value.get("yield_assembly_digest") or ""),
            progress=_safe_mapping(value.get("progress")),
            error_code=str(value.get("error_code") or ""),
            error_message=str(value.get("error_message") or ""),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            revision=max(0, int(value.get("revision") or 0)),
        )


@dataclass(frozen=True, slots=True)
class FanoutDelivery:
    delivery_id: str
    group_id: str
    parent_task_id: str
    payload_digest: str
    payload: Mapping[str, Any]
    attempts: int = 0
    next_attempt_at_ms: int = 0
    delivered: bool = False
    last_error: str = ""
    created_at: str = field(default_factory=now_iso)
    delivered_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "group_id": self.group_id,
            "parent_task_id": self.parent_task_id,
            "payload_digest": self.payload_digest,
            "payload": _safe_mapping(self.payload),
            "attempts": self.attempts,
            "next_attempt_at_ms": self.next_attempt_at_ms,
            "delivered": self.delivered,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FanoutDelivery":
        return cls(
            delivery_id=str(value.get("delivery_id") or new_id("fandelivery")),
            group_id=str(value.get("group_id") or ""),
            parent_task_id=str(value.get("parent_task_id") or ""),
            payload_digest=str(value.get("payload_digest") or ""),
            payload=_safe_mapping(value.get("payload")),
            attempts=max(0, int(value.get("attempts") or 0)),
            next_attempt_at_ms=max(0, int(value.get("next_attempt_at_ms") or 0)),
            delivered=bool(value.get("delivered", False)),
            last_error=str(value.get("last_error") or ""),
            created_at=str(value.get("created_at") or now_iso()),
            delivered_at=str(value.get("delivered_at") or ""),
        )


@dataclass(frozen=True, slots=True)
class FanoutRecord:
    group_id: str
    request: FanoutRequest
    request_digest: str
    status: FanoutStatus
    children: tuple[FanoutChildRecord, ...]
    execution_mode: AgentExecutionMode
    revision: int = 0
    detached_handle: str = ""
    aggregate: Mapping[str, Any] = field(default_factory=dict)
    progress: Mapping[str, Any] = field(default_factory=dict)
    terminal_reason: str = ""
    causation_id: str = ""
    delivery_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    completed_at: str = ""

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    def child(self, task_id: str) -> FanoutChildRecord:
        selected = next((item for item in self.children if item.task_id == task_id), None)
        if selected is None:
            raise FanoutNotFound(task_id)
        return selected

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "request": self.request.to_dict(),
            "request_digest": self.request_digest,
            "status": self.status.value,
            "children": [item.to_dict() for item in self.children],
            "execution_mode": self.execution_mode.value,
            "revision": self.revision,
            "detached_handle": self.detached_handle,
            "aggregate": _safe_mapping(self.aggregate),
            "progress": _safe_mapping(self.progress),
            "terminal_reason": self.terminal_reason,
            "causation_id": self.causation_id,
            "delivery_id": self.delivery_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FanoutRecord":
        request_raw = _mapping(value.get("request"))
        contract_raw = request_raw.get("yield_contract")
        request = FanoutRequest(
            run_id=str(request_raw.get("run_id") or ""),
            parent_task_id=str(request_raw.get("parent_task_id") or ""),
            parent_session_id=str(request_raw.get("parent_session_id") or ""),
            shared_context=str(request_raw.get("shared_context") or ""),
            items=tuple(FanoutItem.from_dict(item) for item in request_raw.get("items") or () if isinstance(item, Mapping)),
            idempotency_key=str(request_raw.get("idempotency_key") or ""),
            execution_mode=AgentExecutionMode(str(request_raw.get("execution_mode") or AgentExecutionMode.FOREGROUND.value)),
            failure_policy=FanoutFailurePolicy(str(request_raw.get("failure_policy") or FanoutFailurePolicy.REQUIRE_ALL.value)),
            maximum_concurrency=max(1, int(request_raw.get("maximum_concurrency") or 1)),
            promotion_after_ms=max(0, int(request_raw.get("promotion_after_ms") or 0)),
            yield_contract=YieldContract.from_dict(contract_raw) if isinstance(contract_raw, Mapping) else None,
            parent_scope_snapshot_id=str(request_raw.get("parent_scope_snapshot_id") or ""),
            metadata=_safe_mapping(request_raw.get("metadata")),
        )
        return cls(
            group_id=str(value.get("group_id") or ""),
            request=request,
            request_digest=str(value.get("request_digest") or request.request_digest),
            status=FanoutStatus(str(value.get("status") or FanoutStatus.CREATED.value)),
            children=tuple(
                FanoutChildRecord.from_dict(item) for item in value.get("children") or () if isinstance(item, Mapping)
            ),
            execution_mode=AgentExecutionMode(str(value.get("execution_mode") or request.execution_mode.value)),
            revision=max(0, int(value.get("revision") or 0)),
            detached_handle=str(value.get("detached_handle") or ""),
            aggregate=_safe_mapping(value.get("aggregate")),
            progress=_safe_mapping(value.get("progress")),
            terminal_reason=str(value.get("terminal_reason") or ""),
            causation_id=str(value.get("causation_id") or ""),
            delivery_id=str(value.get("delivery_id") or ""),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
            completed_at=str(value.get("completed_at") or ""),
        )


class FanoutStore:
    schema = "zyra.subagent-fanout-store/v1"

    def __init__(self, path: str | Path, *, disabled: bool = False) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.disabled = bool(disabled)
        self._lock = RLock()
        self._changed = Condition(self._lock)
        self._records: dict[str, FanoutRecord] = {}
        self._idempotency: dict[str, str] = {}
        self._deliveries: dict[str, FanoutDelivery] = {}
        self._load()

    def create(self, request: FanoutRequest, *, causation_id: str = "") -> FanoutRecord:
        self._require_enabled()
        with self._lock:
            existing_id = self._idempotency.get(request.idempotency_key)
            if existing_id:
                existing = self._records[existing_id]
                if existing.request_digest != request.request_digest:
                    raise FanoutConflict("fan-out idempotency key reused with different request")
                return copy.deepcopy(existing)
            group_id = new_id("fanout")
            children = tuple(
                FanoutChildRecord(
                    item=item,
                    task_id=_stable_child_id(group_id, item.item_id),
                    status=ChildFanoutStatus.PENDING,
                )
                for item in request.items
            )
            record = FanoutRecord(
                group_id=group_id,
                request=copy.deepcopy(request),
                request_digest=request.request_digest,
                status=FanoutStatus.CREATED,
                children=children,
                execution_mode=request.execution_mode,
                causation_id=causation_id,
                progress=_progress(children),
            )
            self._records[group_id] = record
            self._idempotency[request.idempotency_key] = group_id
            self._persist()
            return copy.deepcopy(record)

    def get(self, group_id: str) -> FanoutRecord:
        self._require_enabled()
        with self._lock:
            value = self._records.get(group_id)
            if value is None:
                raise FanoutNotFound(group_id)
            return copy.deepcopy(value)

    def for_idempotency(self, key: str) -> FanoutRecord | None:
        self._require_enabled()
        with self._lock:
            group_id = self._idempotency.get(key)
            return copy.deepcopy(self._records[group_id]) if group_id else None

    def list(self, *, parent_task_id: str | None = None, include_terminal: bool = True) -> tuple[FanoutRecord, ...]:
        self._require_enabled()
        with self._lock:
            return tuple(sorted(
                (
                    copy.deepcopy(item)
                    for item in self._records.values()
                    if (parent_task_id is None or item.request.parent_task_id == parent_task_id)
                    and (include_terminal or not item.terminal)
                ),
                key=lambda item: (item.created_at, item.group_id),
            ))

    def mutate(
        self,
        group_id: str,
        callback: Callable[[FanoutRecord], FanoutRecord],
        *,
        expected_revision: int | None = None,
    ) -> FanoutRecord:
        self._require_enabled()
        with self._lock:
            current = self._records.get(group_id)
            if current is None:
                raise FanoutNotFound(group_id)
            if expected_revision is not None and current.revision != expected_revision:
                raise FanoutConflict(
                    f"fan-out revision conflict: expected {expected_revision}, actual {current.revision}"
                )
            changed = callback(copy.deepcopy(current))
            if not isinstance(changed, FanoutRecord) or changed.group_id != current.group_id:
                raise TypeError("fan-out mutation must return the same FanoutRecord identity")
            changed = replace(
                changed,
                revision=current.revision + 1,
                progress=_progress(changed.children),
                updated_at=now_iso(),
                completed_at=(now_iso() if changed.terminal and not changed.completed_at else changed.completed_at),
            )
            self._records[group_id] = changed
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    def transition(self, group_id: str, status: FanoutStatus, *, reason: str = "") -> FanoutRecord:
        def update(current: FanoutRecord) -> FanoutRecord:
            if current.terminal:
                if current.status is status:
                    return current
                raise FanoutConflict(f"fan-out group is terminal at {current.status.value}")
            allowed = {
                FanoutStatus.CREATED: {FanoutStatus.VALIDATING, FanoutStatus.CANCELLED, FanoutStatus.FAILED},
                FanoutStatus.VALIDATING: {FanoutStatus.QUEUED, FanoutStatus.RUNNING, FanoutStatus.CANCELLED, FanoutStatus.FAILED},
                FanoutStatus.QUEUED: {FanoutStatus.RUNNING, FanoutStatus.DETACHED, FanoutStatus.CANCELLED, FanoutStatus.FAILED},
                FanoutStatus.RUNNING: {FanoutStatus.DETACHED, FanoutStatus.CANCELLING, FanoutStatus.COMPLETED, FanoutStatus.PARTIAL, FanoutStatus.FAILED, FanoutStatus.CANCELLED},
                FanoutStatus.DETACHED: {FanoutStatus.CANCELLING, FanoutStatus.COMPLETED, FanoutStatus.PARTIAL, FanoutStatus.FAILED, FanoutStatus.CANCELLED, FanoutStatus.PARKED},
                FanoutStatus.CANCELLING: {FanoutStatus.CANCELLED, FanoutStatus.PARTIAL, FanoutStatus.FAILED},
            }
            if status is not current.status and status not in allowed.get(current.status, set()):
                raise FanoutConflict(f"invalid fan-out transition {current.status.value}->{status.value}")
            return replace(current, status=status, terminal_reason=reason or current.terminal_reason)

        return self.mutate(group_id, update)

    def update_child(
        self,
        group_id: str,
        task_id: str,
        callback: Callable[[FanoutChildRecord], FanoutChildRecord],
    ) -> FanoutRecord:
        def update(current: FanoutRecord) -> FanoutRecord:
            children = []
            found = False
            for child in current.children:
                if child.task_id != task_id:
                    children.append(child)
                    continue
                found = True
                changed = callback(copy.deepcopy(child))
                if changed.task_id != child.task_id:
                    raise TypeError("fan-out child identity cannot change")
                children.append(replace(changed, revision=child.revision + 1))
            if not found:
                raise FanoutNotFound(task_id)
            return replace(current, children=tuple(children))

        return self.mutate(group_id, update)

    def enqueue_delivery(self, group_id: str, payload: Mapping[str, Any]) -> FanoutDelivery:
        self._require_enabled()
        with self._lock:
            record = self._records.get(group_id)
            if record is None:
                raise FanoutNotFound(group_id)
            digest = _digest(payload)
            if record.delivery_id:
                existing = self._deliveries[record.delivery_id]
                if existing.payload_digest != digest:
                    raise FanoutConflict("fan-out terminal delivery already has different payload")
                return copy.deepcopy(existing)
            delivery = FanoutDelivery(
                delivery_id=new_id("fandelivery"),
                group_id=group_id,
                parent_task_id=record.request.parent_task_id,
                payload_digest=digest,
                payload=_safe_mapping(payload),
                next_attempt_at_ms=_now_ms(),
            )
            self._deliveries[delivery.delivery_id] = delivery
            self._records[group_id] = replace(record, delivery_id=delivery.delivery_id, revision=record.revision + 1)
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(delivery)

    def delivery_succeeded(self, delivery_id: str) -> FanoutDelivery:
        self._require_enabled()
        with self._lock:
            current = self._deliveries.get(delivery_id)
            if current is None:
                raise FanoutNotFound(delivery_id)
            changed = replace(current, delivered=True, delivered_at=now_iso(), last_error="")
            self._deliveries[delivery_id] = changed
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    def delivery_failed(self, delivery_id: str, error: Exception | str) -> FanoutDelivery:
        self._require_enabled()
        with self._lock:
            current = self._deliveries.get(delivery_id)
            if current is None:
                raise FanoutNotFound(delivery_id)
            attempts = current.attempts + 1
            delay = min(30_000, 500 * (2 ** min(max(attempts - 1, 0), 8)) + random.randint(0, 200))
            changed = replace(
                current,
                attempts=attempts,
                next_attempt_at_ms=_now_ms() + delay,
                last_error=str(error),
            )
            self._deliveries[delivery_id] = changed
            self._persist()
            self._changed.notify_all()
            return copy.deepcopy(changed)

    def pending_deliveries(self, *, due_at_ms: int | None = None) -> tuple[FanoutDelivery, ...]:
        self._require_enabled()
        now = _now_ms() if due_at_ms is None else int(due_at_ms)
        with self._lock:
            return tuple(sorted(
                (
                    copy.deepcopy(item)
                    for item in self._deliveries.values()
                    if not item.delivered and item.next_attempt_at_ms <= now
                ),
                key=lambda item: (item.next_attempt_at_ms, item.created_at),
            ))

    def wait_terminal(self, group_id: str, timeout: float | None = None) -> FanoutRecord:
        self._require_enabled()
        with self._changed:
            if group_id not in self._records:
                raise FanoutNotFound(group_id)
            self._changed.wait_for(lambda: self._records[group_id].terminal, timeout=timeout)
            return copy.deepcopy(self._records[group_id])

    def reconcile_after_restart(self) -> tuple[FanoutRecord, ...]:
        reconciled = []
        for record in self.list(include_terminal=False):
            children = tuple(
                replace(
                    item,
                    status=(item.status if item.terminal else ChildFanoutStatus.PARKED),
                    error_code=(item.error_code or ("restart_parked" if not item.terminal else "")),
                    error_message=(item.error_message or ("logical child requires explicit resume after restart" if not item.terminal else "")),
                )
                for item in record.children
            )
            reconciled.append(self.mutate(
                record.group_id,
                lambda current, children=children: replace(
                    current,
                    children=children,
                    status=FanoutStatus.PARKED,
                    terminal_reason="process restarted without a safe physical execution claim; no replay performed",
                ),
            ))
        return tuple(reconciled)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            body = {
                "schema": self.schema,
                "owner": "M1-03D FanoutStore",
                "forbidden_physical_fields": ["worker_id", "lease_id", "capacity", "heartbeat"],
                "records": [item.to_dict() for item in sorted(self._records.values(), key=lambda value: value.group_id)],
                "idempotency": dict(sorted(self._idempotency.items())),
                "deliveries": [item.to_dict() for item in sorted(self._deliveries.values(), key=lambda value: value.delivery_id)],
            }
            return {**body, "checksum": _digest(body)}

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise FanoutError(f"cannot load fan-out store: {error}") from error
        if not isinstance(value, Mapping):
            raise FanoutError("fan-out store root must be an object")
        body = {key: copy.deepcopy(item) for key, item in value.items() if key != "checksum"}
        expected = str(value.get("checksum") or "")
        if expected and expected != _digest(body):
            raise FanoutError("fan-out store checksum mismatch")
        for item in value.get("records") or ():
            if isinstance(item, Mapping):
                record = FanoutRecord.from_dict(item)
                self._records[record.group_id] = record
        raw_idempotency = value.get("idempotency")
        if isinstance(raw_idempotency, Mapping):
            self._idempotency = {str(key): str(item) for key, item in raw_idempotency.items()}
        for item in value.get("deliveries") or ():
            if isinstance(item, Mapping):
                delivery = FanoutDelivery.from_dict(item)
                self._deliveries[delivery.delivery_id] = delivery

    def _persist(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise FanoutDisabled("FanoutStore is disabled")


class SubagentSpawnPort(Protocol):
    def spawn(self, request: SubagentSpawnRequest) -> Any: ...
    def wait(self, task_id: str, timeout: float | None = None) -> Any: ...
    def cancel(self, task_id: str, *, reason: str = "operator_cancelled") -> Any: ...
    def get(self, task_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class FanoutStartResult:
    record: FanoutRecord
    detached: bool
    handle: str = ""

    @property
    def ok(self) -> bool:
        return self.record.status in {FanoutStatus.RUNNING, FanoutStatus.DETACHED, FanoutStatus.COMPLETED, FanoutStatus.PARTIAL}

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "record": self.record.to_dict(), "detached": self.detached, "handle": self.handle}


class LogicalFanoutRuntime:
    def __init__(
        self,
        store: FanoutStore,
        subagents: SubagentSpawnPort,
        yields: TypedYieldStore,
        receipts: ExecutionReceiptStore,
        *,
        event_sink: Callable[[EventRecord], None] | None = None,
        delivery_sink: Callable[[Mapping[str, Any]], None] | None = None,
        spawn_request_factory: Callable[[FanoutRecord, FanoutChildRecord], SubagentSpawnRequest] | None = None,
        typed_yield_extractor: Callable[[Any, FanoutRecord, FanoutChildRecord], TypedYield | None] | None = None,
        maximum_active_groups_per_parent: int = 16,
        disabled: bool = False,
    ) -> None:
        self.store = store
        self.subagents = subagents
        self.yields = yields
        self.receipts = receipts
        self.event_sink = event_sink
        self.delivery_sink = delivery_sink
        self.spawn_request_factory = spawn_request_factory
        self.typed_yield_extractor = typed_yield_extractor or typed_yield_from_lifecycle
        self.maximum_active_groups_per_parent = max(1, int(maximum_active_groups_per_parent))
        self.disabled = bool(disabled)
        self._lock = RLock()
        self._threads: dict[str, Thread] = {}
        self._cancel: dict[str, Event] = {}
        self._promotion: dict[str, Event] = {}
        self._session_semaphores: dict[str, tuple[int, Semaphore]] = {}

    def start(self, request: FanoutRequest, *, causation_id: str = "") -> FanoutStartResult:
        self._require_enabled()
        existing = self.store.for_idempotency(request.idempotency_key)
        if existing:
            return FanoutStartResult(existing, existing.execution_mode is AgentExecutionMode.BACKGROUND, existing.detached_handle)
        active = self.store.list(parent_task_id=request.parent_task_id, include_terminal=False)
        if len(active) >= self.maximum_active_groups_per_parent:
            raise FanoutCapacityExceeded("logical fan-out group limit reached for parent")
        record = self.store.create(request, causation_id=causation_id)
        self.store.transition(record.group_id, FanoutStatus.VALIDATING)
        contract = request.yield_contract or default_yield_contract(record.group_id)
        self.yields.register(contract)
        self._emit_group(record.group_id, "created", {"item_count": len(record.children), "contract": contract.to_dict()})
        if request.execution_mode is AgentExecutionMode.BACKGROUND:
            record = self._detach(record.group_id, reason="requested_background")
            self._launch(record.group_id)
            return FanoutStartResult(record, True, record.detached_handle)
        self.store.transition(record.group_id, FanoutStatus.RUNNING)
        if request.promotion_after_ms > 0:
            self._launch(record.group_id)
            deadline = time.monotonic() + request.promotion_after_ms / 1000.0
            while time.monotonic() < deadline:
                current = self.store.get(record.group_id)
                if current.terminal:
                    return FanoutStartResult(current, False, "")
                time.sleep(0.01)
            promoted = self.promote(record.group_id, reason="foreground_deadline_elapsed")
            return FanoutStartResult(promoted, True, promoted.detached_handle)
        result = self._run(record.group_id)
        return FanoutStartResult(result, False, "")

    def promote(self, group_id: str, *, reason: str = "operator_promoted") -> FanoutRecord:
        self._require_enabled()
        with self._lock:
            thread = self._threads.get(group_id)
        current = self.store.get(group_id)
        if current.terminal:
            return current
        if thread is None:
            self._launch(group_id)
        promoted = self._detach(group_id, reason=reason)
        with self._lock:
            self._promotion.setdefault(group_id, Event()).set()
        return promoted

    def cancel(self, group_id: str, *, reason: str = "parent_cancelled") -> FanoutRecord:
        self._require_enabled()
        current = self.store.get(group_id)
        if current.terminal:
            return current
        try:
            self.store.transition(group_id, FanoutStatus.CANCELLING, reason=reason)
        except FanoutConflict:
            pass
        with self._lock:
            self._cancel.setdefault(group_id, Event()).set()
        for child in self.store.get(group_id).children:
            if child.terminal:
                continue
            try:
                self.subagents.cancel(child.task_id, reason=reason)
            except Exception:
                pass
            self.store.update_child(group_id, child.task_id, lambda item: replace(
                item,
                status=ChildFanoutStatus.CANCELLED,
                error_code="fanout_cancelled",
                error_message=reason,
                completed_at=now_iso(),
            ))
        current = self._finalize(group_id)
        self._emit_group(group_id, "cancelled", {"reason": reason})
        return current

    def wait(self, group_id: str, timeout: float | None = None) -> FanoutRecord:
        with self._lock:
            thread = self._threads.get(group_id)
        if thread:
            thread.join(timeout=timeout)
        return self.store.get(group_id)

    def progress(self, group_id: str) -> Mapping[str, Any]:
        return self.store.get(group_id).progress

    def deliver_pending(self) -> tuple[FanoutDelivery, ...]:
        delivered = []
        for delivery in self.store.pending_deliveries():
            if self.delivery_sink is None:
                continue
            try:
                self.delivery_sink(delivery.payload)
                delivered.append(self.store.delivery_succeeded(delivery.delivery_id))
            except Exception as error:
                self.store.delivery_failed(delivery.delivery_id, error)
        return tuple(delivered)

    def reconcile_after_restart(self) -> tuple[FanoutRecord, ...]:
        self._require_enabled()
        return self.store.reconcile_after_restart()

    def _launch(self, group_id: str) -> None:
        with self._lock:
            existing = self._threads.get(group_id)
            if existing and existing.is_alive():
                return
            self._cancel.setdefault(group_id, Event())
            self._promotion.setdefault(group_id, Event())
            thread = Thread(target=self._run_thread, args=(group_id,), name=f"zyra-fanout-{group_id}", daemon=True)
            self._threads[group_id] = thread
            thread.start()

    def _run_thread(self, group_id: str) -> None:
        try:
            self._run(group_id)
        finally:
            with self._lock:
                self._threads.pop(group_id, None)

    def _run(self, group_id: str) -> FanoutRecord:
        record = self.store.get(group_id)
        if record.status in {FanoutStatus.VALIDATING, FanoutStatus.QUEUED}:
            self.store.transition(group_id, FanoutStatus.RUNNING)
        contract = record.request.yield_contract or default_yield_contract(record.group_id)
        semaphore = self._semaphore(record.request.parent_session_id, record.request.maximum_concurrency)
        child_threads = []
        for child in record.children:
            thread = Thread(
                target=self._run_child,
                args=(group_id, child.task_id, contract, semaphore),
                name=f"zyra-fanout-child-{child.task_id}",
                daemon=True,
            )
            child_threads.append(thread)
            thread.start()
        for thread in child_threads:
            while thread.is_alive():
                thread.join(timeout=0.05)
                if self._cancelled(group_id) and record.request.failure_policy is FanoutFailurePolicy.FAIL_FAST:
                    break
            if self._cancelled(group_id) and record.request.failure_policy is FanoutFailurePolicy.FAIL_FAST:
                break
        result = self._finalize(group_id)
        self._deliver_terminal(result)
        return result

    def _run_child(self, group_id: str, task_id: str, contract: YieldContract, semaphore: Semaphore) -> None:
        record = self.store.get(group_id)
        child = record.child(task_id)
        self.store.update_child(group_id, task_id, lambda item: replace(item, status=ChildFanoutStatus.QUEUED))
        acquired = semaphore.acquire(timeout=max(1.0, float(child.item.constraints.get("queue_timeout_seconds") or 300)))
        if not acquired:
            self._fail_child(group_id, task_id, "fanout_queue_timeout", "logical session semaphore timed out")
            return
        try:
            if self._cancelled(group_id):
                self._cancel_child(group_id, task_id, "fan-out cancelled before child dispatch")
                return
            self.store.update_child(group_id, task_id, lambda item: replace(
                item,
                status=ChildFanoutStatus.RUNNING,
                started_at=now_iso(),
                progress={"phase": "dispatching", "current": 0, "total": 1},
            ))
            self._emit_child(group_id, task_id, "started", {"agent_type": child.item.agent_type})
            if self.spawn_request_factory is None:
                raise FanoutError("fan-out spawn request factory is unavailable")
            spawn_request = self.spawn_request_factory(self.store.get(group_id), self.store.get(group_id).child(task_id))
            spawn_result = self.subagents.spawn(spawn_request)
            current_task = spawn_result.record
            receipt = self.receipts.for_task(task_id)
            execution_ref = str(current_task.execution_ref or (receipt.execution_ref if receipt else ""))
            self.yields.begin(
                task_id=task_id,
                contract=contract,
                execution_ref=execution_ref,
                attempt=max(1, int(current_task.attempt or 1)),
            )
            self.store.update_child(group_id, task_id, lambda item: replace(
                item,
                execution_ref=execution_ref,
                execution_receipt_id=(receipt.receipt_id if receipt else ""),
                progress={"phase": "running", "current": 0, "total": 1},
            ))
            lifecycle = spawn_result.lifecycle
            if spawn_result.background:
                lifecycle = self.subagents.wait(task_id, timeout=float(child.item.constraints.get("wall_timeout_seconds") or 3600))
            explicit = self.typed_yield_extractor(lifecycle, record, child)
            if explicit is None:
                execution_result = getattr(lifecycle, "execution_result", None)
                error_code = str(getattr(execution_result, "error_code", "") or "")
                error_message = str(getattr(execution_result, "error_message", "") or "")
                result_metadata = getattr(execution_result, "metadata", None)
                metadata_keys = sorted(str(key) for key in result_metadata) if isinstance(result_metadata, Mapping) else []
                recovery_signals = tuple(getattr(getattr(lifecycle, "record", None), "recovery_signals", ()) or ())
                recovery_code = str(getattr(recovery_signals[-1], "error_code", "") or "") if recovery_signals else ""
                recovery_reason = str(getattr(recovery_signals[-1], "reason", "") or "") if recovery_signals else ""
                detail = (
                    f" (ok={bool(getattr(execution_result, 'ok', False))}; "
                    f"error={error_code or error_message or recovery_code or 'none'}:{recovery_reason}; "
                    f"metadata_keys={metadata_keys})"
                )
                raise FanoutError(
                    "child completed without explicit typed yield; legacy summary fallback is disabled" + detail
                )
            assembly = self.yields.append(explicit, idempotency_key=f"fanout:{group_id}:{task_id}:{explicit.sequence}")
            assembly = self.yields.require_terminal(task_id)
            if receipt:
                # The actual attempt token remains in SubagentRuntime; receipt
                # integration commits the digest there.  Fan-out only records
                # the immutable receipt id and assembly digest.
                pass
            status = ChildFanoutStatus.YIELDED if assembly.completed else ChildFanoutStatus.FAILED
            self.store.update_child(group_id, task_id, lambda item: replace(
                item,
                status=status,
                yield_assembly_digest=_digest(assembly.to_dict()),
                progress={"phase": "terminal", "current": 1, "total": 1},
                error_code=("" if assembly.completed else "typed_yield_failed"),
                error_message=("" if assembly.completed else (assembly.summary or "child yielded failure")),
                completed_at=now_iso(),
            ))
            self._emit_child(group_id, task_id, "yielded", {"assembly": assembly.to_dict()})
        except Exception as error:
            self._fail_child(group_id, task_id, type(error).__name__, str(error))
            if record.request.failure_policy is FanoutFailurePolicy.FAIL_FAST:
                with self._lock:
                    self._cancel.setdefault(group_id, Event()).set()
        finally:
            semaphore.release()

    def _finalize(self, group_id: str) -> FanoutRecord:
        record = self.store.get(group_id)
        terminal = [item for item in record.children if item.terminal]
        successes = [item for item in terminal if item.status is ChildFanoutStatus.YIELDED]
        failures = [item for item in terminal if item.status in {ChildFanoutStatus.FAILED, ChildFanoutStatus.PARKED}]
        cancelled = [item for item in terminal if item.status is ChildFanoutStatus.CANCELLED]
        if len(terminal) < len(record.children):
            if self._cancelled(group_id):
                status = FanoutStatus.CANCELLED
            else:
                return record
        elif record.request.failure_policy is FanoutFailurePolicy.REQUIRE_ALL:
            status = FanoutStatus.COMPLETED if len(successes) == len(record.children) else FanoutStatus.FAILED
        elif record.request.failure_policy is FanoutFailurePolicy.REQUIRE_ANY:
            status = FanoutStatus.COMPLETED if successes else FanoutStatus.FAILED
        elif record.request.failure_policy is FanoutFailurePolicy.FAIL_FAST:
            status = FanoutStatus.FAILED if failures else (FanoutStatus.CANCELLED if cancelled and not successes else FanoutStatus.COMPLETED)
        else:
            status = FanoutStatus.COMPLETED if not failures and not cancelled else (FanoutStatus.PARTIAL if successes else FanoutStatus.FAILED)
        assemblies = []
        for child in successes:
            try:
                assembly = self.yields.get(child.task_id)
                assemblies.append({"name": child.item.name, "task_id": child.task_id, "yield": assembly.to_dict()})
            except Exception:
                continue
        aggregate = {
            "schema": "zyra.fanout-result/v1",
            "group_id": group_id,
            "policy": record.request.failure_policy.value,
            "success_count": len(successes),
            "failure_count": len(failures),
            "cancelled_count": len(cancelled),
            "results": assemblies,
            "failures": [
                {"name": item.item.name, "task_id": item.task_id, "error_code": item.error_code, "error_message": item.error_message}
                for item in (*failures, *cancelled)
            ],
            "ordered_task_ids": [item.task_id for item in record.children],
        }
        changed = self.store.mutate(group_id, lambda current: replace(
            current,
            status=status,
            aggregate=aggregate,
            terminal_reason=("" if status is FanoutStatus.COMPLETED else f"{len(failures)} failed, {len(cancelled)} cancelled"),
        ))
        self._emit_group(group_id, "completed" if changed.status is FanoutStatus.COMPLETED else "failed", {"aggregate": aggregate})
        return changed

    def _deliver_terminal(self, record: FanoutRecord) -> None:
        if not record.terminal:
            return
        delivery = self.store.enqueue_delivery(record.group_id, record.aggregate)
        if self.delivery_sink is None:
            return
        try:
            self.delivery_sink(delivery.payload)
            self.store.delivery_succeeded(delivery.delivery_id)
        except Exception as error:
            self.store.delivery_failed(delivery.delivery_id, error)

    def _detach(self, group_id: str, *, reason: str) -> FanoutRecord:
        handle = f"fanout://{group_id}"
        current = self.store.get(group_id)
        if current.status is FanoutStatus.VALIDATING:
            self.store.transition(group_id, FanoutStatus.QUEUED)
            current = self.store.get(group_id)
        return self.store.mutate(group_id, lambda item: replace(
            item,
            status=FanoutStatus.DETACHED,
            execution_mode=AgentExecutionMode.BACKGROUND,
            detached_handle=item.detached_handle or handle,
            terminal_reason=reason if item.terminal else item.terminal_reason,
        ))

    def _fail_child(self, group_id: str, task_id: str, code: str, message: str) -> None:
        self.store.update_child(group_id, task_id, lambda item: replace(
            item,
            status=ChildFanoutStatus.FAILED,
            error_code=str(code),
            error_message=str(message),
            completed_at=now_iso(),
            progress={"phase": "failed", "current": 1, "total": 1},
        ))
        self._emit_child(group_id, task_id, "failed", {"error_code": code, "error_message": message})

    def _cancel_child(self, group_id: str, task_id: str, message: str) -> None:
        self.store.update_child(group_id, task_id, lambda item: replace(
            item,
            status=ChildFanoutStatus.CANCELLED,
            error_code="fanout_cancelled",
            error_message=message,
            completed_at=now_iso(),
            progress={"phase": "cancelled", "current": 1, "total": 1},
        ))

    def _semaphore(self, session_id: str, limit: int) -> Semaphore:
        with self._lock:
            current = self._session_semaphores.get(session_id)
            if current is None or current[0] != limit:
                # Existing in-flight calls keep their original semaphore; new
                # groups observe the new logical concurrency ceiling.
                current = (limit, Semaphore(limit))
                self._session_semaphores[session_id] = current
            return current[1]

    def _cancelled(self, group_id: str) -> bool:
        with self._lock:
            return self._cancel.setdefault(group_id, Event()).is_set()

    def _emit_group(self, group_id: str, phase: str, payload: Mapping[str, Any]) -> None:
        record = self.store.get(group_id)
        self._emit(EventRecord(
            run_id=record.request.run_id,
            task_id=record.request.parent_task_id,
            node_id=str(record.request.metadata.get("node_id") or ""),
            event_type=getattr(EventType, "SUBAGENT_PROGRESS", "subagent_progress"),
            payload={
                "schema": "zyra.fanout-event/v1",
                "group_id": group_id,
                "phase": phase,
                "causation_id": record.causation_id,
                "progress": _safe_mapping(record.progress),
                **_safe_mapping(payload),
            },
        ))

    def _emit_child(self, group_id: str, task_id: str, phase: str, payload: Mapping[str, Any]) -> None:
        record = self.store.get(group_id)
        self._emit(EventRecord(
            run_id=record.request.run_id,
            task_id=record.request.parent_task_id,
            node_id=str(record.request.metadata.get("node_id") or ""),
            event_type=getattr(EventType, "SUBAGENT_PROGRESS", "subagent_progress"),
            payload={
                "schema": "zyra.fanout-child-event/v1",
                "group_id": group_id,
                "child_task_id": task_id,
                "phase": phase,
                "causation_id": record.causation_id,
                **_safe_mapping(payload),
            },
        ))

    def _emit(self, event: EventRecord) -> None:
        if self.event_sink is not None:
            self.event_sink(event)

    def _require_enabled(self) -> None:
        if self.disabled:
            raise FanoutDisabled("LogicalFanoutRuntime is disabled")


def typed_yield_from_lifecycle(
    lifecycle: Any,
    _record: FanoutRecord,
    _child: FanoutChildRecord,
) -> TypedYield | None:
    """Read only an explicit child protocol value; never synthesize a summary."""

    execution_result = getattr(lifecycle, "execution_result", None)
    metadata = getattr(execution_result, "metadata", None)
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("typed_yield")
    if not isinstance(raw, Mapping):
        return None
    return TypedYield.from_dict(raw)


def _progress(children: Sequence[FanoutChildRecord]) -> dict[str, Any]:
    counts = {status.value: 0 for status in ChildFanoutStatus}
    for child in children:
        counts[child.status.value] += 1
    terminal = sum(1 for child in children if child.terminal)
    return {
        "total": len(children),
        "terminal": terminal,
        "running": counts[ChildFanoutStatus.RUNNING.value],
        "queued": counts[ChildFanoutStatus.QUEUED.value],
        "succeeded": counts[ChildFanoutStatus.YIELDED.value],
        "failed": counts[ChildFanoutStatus.FAILED.value],
        "cancelled": counts[ChildFanoutStatus.CANCELLED.value],
        "parked": counts[ChildFanoutStatus.PARKED.value],
        "fraction": (terminal / len(children)) if children else 1.0,
        "children": [
            {
                "task_id": child.task_id,
                "name": child.item.name,
                "agent_type": child.item.agent_type,
                "status": child.status.value,
                "progress": _safe_mapping(child.progress),
            }
            for child in children
        ],
    }


def _stable_child_id(group_id: str, item_id: str) -> str:
    value = hashlib.sha256(f"{group_id}:{item_id}".encode("utf-8")).hexdigest()[:24]
    return f"subtask_{value}"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        name = str(key)
        if any(token in name.casefold() for token in ("secret", "token", "password", "authorization", "credential")):
            result[name] = "<redacted>"
        elif isinstance(item, Mapping):
            result[name] = _safe_mapping(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            result[name] = [_safe_value(entry) for entry in item]
        else:
            result[name] = _safe_value(item)
    return result


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return _safe_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_value(item) for item in value]
    method = getattr(value, "safe_dict", None) or getattr(value, "to_dict", None)
    return _safe_value(method()) if callable(method) else str(value)


def _unique_strings(values: Iterable[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    result = []
    for value in values:
        selected = str(value).strip()
        if not selected or selected in seen:
            continue
        seen.add(selected)
        result.append(selected)
    return tuple(result)


def _digest(value: Any) -> str:
    payload = json.dumps(_safe_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "ChildFanoutStatus",
    "FanoutCapacityExceeded",
    "FanoutChildRecord",
    "FanoutConflict",
    "FanoutDelivery",
    "FanoutDisabled",
    "FanoutError",
    "FanoutFailurePolicy",
    "FanoutItem",
    "FanoutNotFound",
    "FanoutRecord",
    "FanoutRequest",
    "FanoutStartResult",
    "FanoutStatus",
    "FanoutStore",
    "LogicalFanoutRuntime",
    "SubagentSpawnPort",
    "typed_yield_from_lifecycle",
]
