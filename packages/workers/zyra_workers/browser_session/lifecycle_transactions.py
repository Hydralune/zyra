from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from zyra_core import EventRecord, EventType, to_jsonable

from .integration_models import BrowserSessionLease, integration_digest, public_mapping
from .models import BrowserSessionCommand, browser_id, browser_now
from .session_lease import BrowserLeaseLost, BrowserSessionLeaseStore


class BrowserLifecycleAction(StrEnum):
    START = "start"
    ENSURE_STARTED = "ensure-started"
    RECONNECT = "reconnect"
    STOP = "stop"
    CANCEL = "cancel"
    DIAGNOSE = "diagnose"
    LIST = "list"
    STOP_ALL = "stop-all"


class BrowserLifecycleTransactionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LATE = "late"
    CONFLICT = "conflict"


class BrowserLifecycleTransactionError(RuntimeError):
    def __init__(self, code: str, message: str, *, session_id: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.session_id = session_id
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class BrowserLifecycleTransactionRequest:
    action: BrowserLifecycleAction
    run_id: str
    task_id: str
    worker_request_id: str
    request_id: str = field(default_factory=lambda: browser_id("brcontrol"))
    browser_session_id: str = ""
    node_id: str = ""
    expected_generation: int | None = None
    force: bool = False
    reason: str = "requested"
    task_filter: str = ""
    ttl_seconds: float = 60.0
    arguments: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=browser_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", BrowserLifecycleAction(str(self.action)))
        if not self.request_id or not self.worker_request_id:
            raise ValueError("lifecycle transaction request identity is required")
        if self.expected_generation is not None and self.expected_generation < 1:
            raise ValueError("expected lifecycle generation must be positive")
        if self.ttl_seconds <= 0:
            raise ValueError("lifecycle transaction ttl must be positive")
        if self.action in {
            BrowserLifecycleAction.RECONNECT,
            BrowserLifecycleAction.STOP,
            BrowserLifecycleAction.CANCEL,
            BrowserLifecycleAction.DIAGNOSE,
        } and not self.browser_session_id:
            raise ValueError(f"browser_session_id is required for {self.action}")

    @property
    def fingerprint(self) -> str:
        return integration_digest({
            "action": str(self.action),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "browser_session_id": self.browser_session_id,
            "expected_generation": self.expected_generation,
            "force": self.force,
            "reason": self.reason,
            "task_filter": self.task_filter,
            "arguments": public_mapping(self.arguments),
        })

    def public_dict(self) -> dict[str, Any]:
        return {
            "action": str(self.action),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "request_id": self.request_id,
            "fingerprint": self.fingerprint,
            "browser_session_id": self.browser_session_id,
            "node_id": self.node_id,
            "expected_generation": self.expected_generation,
            "force": self.force,
            "reason": self.reason,
            "task_filter": self.task_filter,
            "ttl_seconds": self.ttl_seconds,
            "arguments": public_mapping(self.arguments),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserLifecycleControlReceipt:
    request_id: str
    request_fingerprint: str
    action: BrowserLifecycleAction
    status: BrowserLifecycleTransactionStatus
    run_id: str
    task_id: str
    worker_request_id: str
    browser_session_id: str
    started_at: str
    completed_at: str
    generation_before: int = 0
    generation_after: int = 0
    lease_id: str = ""
    result: Any = None
    error_code: str = ""
    error_message: str = ""
    event_record: EventRecord | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    receipt_id: str = field(default_factory=lambda: browser_id("brcontrolreceipt"))

    @property
    def ok(self) -> bool:
        return self.status == BrowserLifecycleTransactionStatus.SUCCEEDED

    @property
    def terminal(self) -> bool:
        return self.status in {
            BrowserLifecycleTransactionStatus.SUCCEEDED,
            BrowserLifecycleTransactionStatus.FAILED,
            BrowserLifecycleTransactionStatus.CANCELLED,
            BrowserLifecycleTransactionStatus.LATE,
            BrowserLifecycleTransactionStatus.CONFLICT,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "action": str(self.action),
            "status": str(self.status),
            "ok": self.ok,
            "terminal": self.terminal,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "browser_session_id": self.browser_session_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "generation_before": self.generation_before,
            "generation_after": self.generation_after,
            "lease_id": self.lease_id,
            "result": to_jsonable(self.result),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "event_record": to_jsonable(self.event_record) if self.event_record else None,
            "metadata": public_mapping(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BrowserLifecycleControlReceipt":
        raw_event = value.get("event_record")
        event = None
        if isinstance(raw_event, Mapping) and raw_event.get("run_id") and raw_event.get("task_id"):
            try:
                event_type = EventType(str(raw_event.get("event_type") or EventType.BROWSER_SESSION_LIFECYCLE))
            except ValueError:
                event_type = EventType.BROWSER_SESSION_LIFECYCLE
            event = EventRecord(
                run_id=str(raw_event.get("run_id")),
                task_id=str(raw_event.get("task_id")),
                event_type=event_type,
                event_id=str(raw_event.get("event_id") or browser_id("event")),
                node_id=str(raw_event.get("node_id") or "") or None,
                created_at=str(raw_event.get("created_at") or browser_now()),
                payload=dict(raw_event.get("payload") or {}),
            )
        return cls(
            receipt_id=str(value.get("receipt_id") or browser_id("brcontrolreceipt")),
            request_id=str(value.get("request_id") or ""),
            request_fingerprint=str(value.get("request_fingerprint") or ""),
            action=BrowserLifecycleAction(str(value.get("action") or BrowserLifecycleAction.DIAGNOSE)),
            status=BrowserLifecycleTransactionStatus(str(value.get("status") or BrowserLifecycleTransactionStatus.FAILED)),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            worker_request_id=str(value.get("worker_request_id") or ""),
            browser_session_id=str(value.get("browser_session_id") or ""),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            generation_before=int(value.get("generation_before") or 0),
            generation_after=int(value.get("generation_after") or 0),
            lease_id=str(value.get("lease_id") or ""),
            result=value.get("result"),
            error_code=str(value.get("error_code") or ""),
            error_message=str(value.get("error_message") or ""),
            event_record=event,
            metadata=public_mapping(value.get("metadata") if isinstance(value.get("metadata"), Mapping) else {}),
        )


@dataclass(frozen=True, slots=True)
class BrowserLifecycleCancellation:
    browser_session_id: str
    generation: int
    reason: str
    request_id: str
    cancelled_at: str = field(default_factory=browser_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "browser_session_id": self.browser_session_id,
            "generation": self.generation,
            "reason": self.reason,
            "request_id": self.request_id,
            "cancelled_at": self.cancelled_at,
        }


class BrowserLifecycleTransactionRuntime:
    """Serializes lifecycle effects using the existing logical lease store.

    Receipts are an in-process idempotency cache; durable session state remains
    owned by BrowserRuntime and durable lease generations remain owned by
    BrowserSessionLeaseStore.  This runtime never creates another browser or
    canonical fact store.
    """

    def __init__(
        self,
        browser_runtime: Any,
        lease_store: BrowserSessionLeaseStore | None = None,
        *,
        canonical_ports: Any = None,
        disabled: bool = False,
        receipt_limit: int = 4096,
    ) -> None:
        if browser_runtime is None:
            raise ValueError("lifecycle transactions require an existing BrowserRuntime")
        self.browser_runtime = browser_runtime
        self.lease_store = lease_store or BrowserSessionLeaseStore(
            Path(browser_runtime.config.state_root) / "integration"
        )
        self.canonical_ports = canonical_ports
        self.disabled = disabled
        self.receipt_limit = max(128, int(receipt_limit))
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.RLock] = {}
        self._receipts: dict[str, BrowserLifecycleControlReceipt] = {}
        self._receipt_order: list[str] = []
        self._running: dict[str, BrowserLifecycleTransactionRequest] = {}
        self._cancellations: dict[str, BrowserLifecycleCancellation] = {}
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        self._late = 0
        self._conflicts = 0
        self._last_error = ""

    def execute(
        self,
        request: BrowserLifecycleTransactionRequest,
        *,
        session_command: BrowserSessionCommand | None = None,
    ) -> BrowserLifecycleControlReceipt:
        self._ensure_available()
        cached = self.receipt(request.request_id, fingerprint=request.fingerprint)
        if cached is not None:
            return cached
        lock_key = request.browser_session_id or f"task:{request.run_id}:{request.task_id}"
        lock = self._session_lock(lock_key)
        if not lock.acquire(timeout=min(request.ttl_seconds, 30.0)):
            return self._terminal_failure(
                request,
                BrowserLifecycleTransactionStatus.CONFLICT,
                "browser_lifecycle_transaction_busy",
                "browser lifecycle transaction lock is busy",
            )
        started_at = browser_now()
        lease: BrowserSessionLease | None = None
        generation_before = self._current_generation(request.browser_session_id)
        try:
            with self._lock:
                existing = self._running.get(lock_key)
                if existing is not None and existing.request_id != request.request_id:
                    return self._terminal_failure(
                        request,
                        BrowserLifecycleTransactionStatus.CONFLICT,
                        "browser_lifecycle_transaction_conflict",
                        f"lifecycle transaction {existing.request_id} is already running",
                    )
                self._running[lock_key] = request
            if request.expected_generation is not None and generation_before != request.expected_generation:
                return self._terminal_failure(
                    request,
                    BrowserLifecycleTransactionStatus.LATE,
                    "browser_lifecycle_generation_changed",
                    f"expected generation {request.expected_generation}, current generation {generation_before}",
                    generation_before=generation_before,
                )
            if request.browser_session_id:
                lease = self._acquire_control_lease(request)
                lease = self.renew_action_lease(lease, ttl_seconds=request.ttl_seconds)
                generation_before = lease.generation
            cancellation = self._cancellations.get(request.browser_session_id)
            if cancellation and cancellation.generation >= generation_before and request.action != BrowserLifecycleAction.CANCEL:
                return self._terminal_failure(
                    request,
                    BrowserLifecycleTransactionStatus.CANCELLED,
                    "browser_lifecycle_cancelled_before_effect",
                    cancellation.reason,
                    generation_before=generation_before,
                    lease=lease,
                )
            result = self._dispatch(request, session_command=session_command)
            resolved_session_id = self._result_session_id(result) or request.browser_session_id
            generation_after = self._current_generation(resolved_session_id)
            if lease is not None:
                try:
                    current = self.lease_store.assert_fence(lease)
                except BrowserLeaseLost as error:
                    return self._late_receipt(
                        request,
                        started_at=started_at,
                        generation_before=generation_before,
                        generation_after=generation_after,
                        lease=lease,
                        result=result,
                        message=str(error),
                    )
                if current.generation != lease.generation:
                    return self._late_receipt(
                        request,
                        started_at=started_at,
                        generation_before=generation_before,
                        generation_after=current.generation,
                        lease=lease,
                        result=result,
                        message="logical lease generation advanced before lifecycle result committed",
                    )
            cancellation = self._cancellations.get(resolved_session_id)
            if cancellation and cancellation.generation >= generation_before and request.action not in {
                BrowserLifecycleAction.CANCEL,
                BrowserLifecycleAction.STOP,
            }:
                return self._late_receipt(
                    request,
                    started_at=started_at,
                    generation_before=generation_before,
                    generation_after=generation_after,
                    lease=lease,
                    result=result,
                    message=f"lifecycle result arrived after cancellation: {cancellation.reason}",
                    status=BrowserLifecycleTransactionStatus.CANCELLED,
                )
            event = self._event(
                request,
                status=BrowserLifecycleTransactionStatus.SUCCEEDED,
                result=result,
                generation_before=generation_before,
                generation_after=generation_after,
                lease=lease,
            )
            receipt = BrowserLifecycleControlReceipt(
                request_id=request.request_id,
                request_fingerprint=request.fingerprint,
                action=request.action,
                status=BrowserLifecycleTransactionStatus.SUCCEEDED,
                run_id=request.run_id,
                task_id=request.task_id,
                worker_request_id=request.worker_request_id,
                browser_session_id=resolved_session_id,
                started_at=started_at,
                completed_at=browser_now(),
                generation_before=generation_before,
                generation_after=generation_after,
                lease_id=lease.lease_id if lease else "",
                result=result,
                event_record=event,
                metadata={"runtime_id": "zyra-browser-lifecycle-transactions"},
            )
            self._project_event(event)
            self._store_receipt(receipt)
            with self._lock:
                self._completed += 1
                self._last_error = ""
            return receipt
        except Exception as error:
            receipt = self._terminal_failure(
                request,
                BrowserLifecycleTransactionStatus.FAILED,
                type(error).__name__,
                str(error),
                generation_before=generation_before,
                lease=lease,
                started_at=started_at,
            )
            return receipt
        finally:
            if lease is not None:
                try:
                    current = self.lease_store.get_lease(lease.browser_session_id)
                    if current is not None and current.lease_id == lease.lease_id and current.generation == lease.generation:
                        self.lease_store.release(lease, reason=f"control_{request.action}_completed")
                except Exception:
                    self.lease_store.revoke(lease.browser_session_id, reason="control_lease_release_failed")
            with self._lock:
                self._running.pop(lock_key, None)
            lock.release()

    def cancel(
        self,
        browser_session_id: str,
        *,
        request_id: str,
        reason: str = "control_cancelled",
    ) -> BrowserLifecycleCancellation:
        self._ensure_available()
        current = self.lease_store.get_lease(browser_session_id)
        generation = current.generation if current is not None else self._current_generation(browser_session_id)
        own_control_owner = f"browser-control:{request_id}"
        if current is not None and current.owner_id != own_control_owner:
            self.lease_store.revoke(browser_session_id, reason=reason)
            generation = max(generation, current.generation)
        cancellation = BrowserLifecycleCancellation(
            browser_session_id=browser_session_id,
            generation=generation,
            reason=reason,
            request_id=request_id,
        )
        with self._lock:
            self._cancellations[browser_session_id] = cancellation
            self._cancelled += 1
        session_runtime = getattr(self.browser_runtime, "_runtime", None)
        supervisor = getattr(session_runtime, "task_supervisor", None)
        if supervisor is not None:
            supervisor.cancel_session(
                browser_session_id,
                reason=reason,
                advance_generation=True,
            )
        return cancellation

    def acquire_action_lease(
        self,
        *,
        browser_session_id: str,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        owner_id: str,
        ttl_seconds: float,
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserSessionLease:
        self._ensure_available()
        lease = self.lease_store.acquire(
            browser_session_id=browser_session_id,
            run_id=run_id,
            task_id=task_id,
            worker_request_id=worker_request_id,
            owner_id=owner_id,
            ttl_seconds=ttl_seconds,
            metadata={"lease_purpose": "browser_action_plan", **dict(metadata or {})},
        )
        cancellation = self._cancellations.get(browser_session_id)
        if cancellation and cancellation.generation >= lease.generation:
            self.lease_store.revoke(browser_session_id, reason="action_lease_cancelled")
            raise BrowserLeaseLost("browser action lease was cancelled", session_id=browser_session_id)
        return lease

    def assert_active(self, lease: BrowserSessionLease) -> BrowserSessionLease:
        self._ensure_available()
        current = self.lease_store.assert_fence(lease)
        cancellation = self._cancellations.get(lease.browser_session_id)
        if cancellation and cancellation.generation >= current.generation:
            raise BrowserLeaseLost("browser lifecycle cancellation fenced the action lease", session_id=lease.browser_session_id)
        return current

    def renew_action_lease(
        self,
        lease: BrowserSessionLease,
        *,
        ttl_seconds: float | None = None,
    ) -> BrowserSessionLease:
        self._ensure_available()
        current = self.lease_store.renew(lease, ttl_seconds=ttl_seconds)
        cancellation = self._cancellations.get(lease.browser_session_id)
        if cancellation and cancellation.generation >= current.generation:
            self.lease_store.revoke(lease.browser_session_id, reason="lease_renewed_after_cancel")
            raise BrowserLeaseLost("browser lifecycle cancellation fenced lease renewal", session_id=lease.browser_session_id)
        return current

    def release_action_lease(self, lease: BrowserSessionLease, *, reason: str) -> BrowserSessionLease:
        return self.lease_store.release(lease, reason=reason)

    def receipt(self, request_id: str, *, fingerprint: str = "") -> BrowserLifecycleControlReceipt | None:
        with self._lock:
            receipt = self._receipts.get(request_id)
        if receipt is None:
            durable = self.lease_store.control_for_request(request_id, fingerprint)
            if durable is not None:
                receipt = BrowserLifecycleControlReceipt.from_dict(durable)
                with self._lock:
                    self._receipts[request_id] = receipt
                    self._receipt_order.append(request_id)
        if receipt is not None and fingerprint and receipt.request_fingerprint != fingerprint:
            with self._lock:
                self._conflicts += 1
            raise BrowserLifecycleTransactionError(
                "browser_lifecycle_request_conflict",
                "lifecycle request id was retried with a different fingerprint",
                session_id=receipt.browser_session_id,
            )
        return receipt

    def receipts(self, *, browser_session_id: str = "") -> tuple[BrowserLifecycleControlReceipt, ...]:
        with self._lock:
            values = tuple(self._receipts[item] for item in self._receipt_order if item in self._receipts)
        if browser_session_id:
            values = tuple(item for item in values if item.browser_session_id == browser_session_id)
        return values

    def cancellation(self, browser_session_id: str) -> BrowserLifecycleCancellation | None:
        with self._lock:
            return self._cancellations.get(browser_session_id)

    def clear_cancellation(self, browser_session_id: str, *, minimum_generation: int = 0) -> bool:
        with self._lock:
            current = self._cancellations.get(browser_session_id)
            if current is None or current.generation > minimum_generation:
                return False
            self._cancellations.pop(browser_session_id, None)
            return True

    def _dispatch(
        self,
        request: BrowserLifecycleTransactionRequest,
        *,
        session_command: BrowserSessionCommand | None,
    ) -> Any:
        if request.action == BrowserLifecycleAction.START:
            return self.browser_runtime.start(self._require_command(session_command))
        if request.action == BrowserLifecycleAction.ENSURE_STARTED:
            return self.browser_runtime.ensure_started(self._require_command(session_command))
        if request.action == BrowserLifecycleAction.RECONNECT:
            return self.browser_runtime.reconnect(request.browser_session_id)
        if request.action == BrowserLifecycleAction.STOP:
            return self.browser_runtime.stop(
                request.browser_session_id,
                force=request.force,
                reason=request.reason,
            )
        if request.action == BrowserLifecycleAction.CANCEL:
            self.cancel(
                request.browser_session_id,
                request_id=request.request_id,
                reason=request.reason or "control_cancelled",
            )
            return self.browser_runtime.stop(
                request.browser_session_id,
                force=True,
                reason=request.reason or "control_cancelled",
            )
        if request.action == BrowserLifecycleAction.DIAGNOSE:
            return self.browser_runtime.diagnose(request.browser_session_id)
        if request.action == BrowserLifecycleAction.LIST:
            return self.browser_runtime.list_sessions(task_id=request.task_filter or request.task_id)
        if request.action == BrowserLifecycleAction.STOP_ALL:
            return self.browser_runtime.stop_all(
                task_id=request.task_filter or request.task_id,
                force=request.force,
                reason=request.reason,
            )
        raise BrowserLifecycleTransactionError("browser_lifecycle_action_unknown", f"unsupported lifecycle action: {request.action}")

    def _acquire_control_lease(self, request: BrowserLifecycleTransactionRequest) -> BrowserSessionLease:
        current = self.lease_store.get_lease(request.browser_session_id, include_inactive=False)
        destructive = request.action == BrowserLifecycleAction.CANCEL or (
            request.action == BrowserLifecycleAction.STOP and request.force
        )
        if current is not None and destructive:
            self.lease_store.revoke(request.browser_session_id, reason=f"preempted_by_{request.action}")
        return self.lease_store.acquire(
            browser_session_id=request.browser_session_id,
            run_id=request.run_id,
            task_id=request.task_id,
            worker_request_id=request.worker_request_id,
            owner_id=f"browser-control:{request.request_id}",
            ttl_seconds=request.ttl_seconds,
            metadata={
                "lease_purpose": "browser_lifecycle_control",
                "control_action": str(request.action),
                "request_fingerprint": request.fingerprint,
            },
        )

    def _late_receipt(
        self,
        request: BrowserLifecycleTransactionRequest,
        *,
        started_at: str,
        generation_before: int,
        generation_after: int,
        lease: BrowserSessionLease,
        result: Any,
        message: str,
        status: BrowserLifecycleTransactionStatus = BrowserLifecycleTransactionStatus.LATE,
    ) -> BrowserLifecycleControlReceipt:
        receipt = self._build_failure(
            request,
            status=status,
            code="browser_lifecycle_late_generation",
            message=message,
            generation_before=generation_before,
            generation_after=generation_after,
            lease=lease,
            started_at=started_at,
            result=result,
        )
        self._store_receipt(receipt)
        with self._lock:
            self._late += 1
        return receipt

    def _terminal_failure(
        self,
        request: BrowserLifecycleTransactionRequest,
        status: BrowserLifecycleTransactionStatus,
        code: str,
        message: str,
        *,
        generation_before: int = 0,
        generation_after: int = 0,
        lease: BrowserSessionLease | None = None,
        started_at: str | None = None,
    ) -> BrowserLifecycleControlReceipt:
        receipt = self._build_failure(
            request,
            status=status,
            code=code,
            message=message,
            generation_before=generation_before,
            generation_after=generation_after,
            lease=lease,
            started_at=started_at or browser_now(),
        )
        self._store_receipt(receipt)
        with self._lock:
            if status == BrowserLifecycleTransactionStatus.CONFLICT:
                self._conflicts += 1
            elif status == BrowserLifecycleTransactionStatus.CANCELLED:
                self._cancelled += 1
            elif status == BrowserLifecycleTransactionStatus.LATE:
                self._late += 1
            else:
                self._failed += 1
            self._last_error = f"{code}: {message}"
        return receipt

    def _build_failure(
        self,
        request: BrowserLifecycleTransactionRequest,
        *,
        status: BrowserLifecycleTransactionStatus,
        code: str,
        message: str,
        generation_before: int,
        generation_after: int,
        lease: BrowserSessionLease | None,
        started_at: str,
        result: Any = None,
    ) -> BrowserLifecycleControlReceipt:
        event = self._event(
            request,
            status=status,
            result=result,
            generation_before=generation_before,
            generation_after=generation_after,
            lease=lease,
            error_code=code,
            error_message=message,
        )
        self._project_event(event)
        return BrowserLifecycleControlReceipt(
            request_id=request.request_id,
            request_fingerprint=request.fingerprint,
            action=request.action,
            status=status,
            run_id=request.run_id,
            task_id=request.task_id,
            worker_request_id=request.worker_request_id,
            browser_session_id=request.browser_session_id,
            started_at=started_at,
            completed_at=browser_now(),
            generation_before=generation_before,
            generation_after=generation_after,
            lease_id=lease.lease_id if lease else "",
            result=result,
            error_code=code,
            error_message=message,
            event_record=event,
            metadata={"runtime_id": "zyra-browser-lifecycle-transactions"},
        )

    def _event(
        self,
        request: BrowserLifecycleTransactionRequest,
        *,
        status: BrowserLifecycleTransactionStatus,
        result: Any,
        generation_before: int,
        generation_after: int,
        lease: BrowserSessionLease | None,
        error_code: str = "",
        error_message: str = "",
    ) -> EventRecord:
        event_type = (
            EventType.BROWSER_RUNTIME_DIAGNOSTIC
            if request.action in {BrowserLifecycleAction.DIAGNOSE, BrowserLifecycleAction.LIST}
            else EventType.BROWSER_SESSION_LIFECYCLE
        )
        return EventRecord(
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id or None,
            event_type=event_type,
            payload={
                "browser_lifecycle_transaction": {
                    "request": request.public_dict(),
                    "status": str(status),
                    "browser_session_id": request.browser_session_id,
                    "generation_before": generation_before,
                    "generation_after": generation_after,
                    "lease_id": lease.lease_id if lease else "",
                    "result": to_jsonable(result),
                    "error_code": error_code,
                    "error_message": error_message,
                }
            },
        )

    def _project_event(self, event: EventRecord) -> None:
        if self.canonical_ports is None:
            return
        for name in ("project_event", "append_event"):
            method = getattr(self.canonical_ports, name, None)
            if callable(method):
                method(event)
                return
        raise TypeError("browser canonical ports cannot project EventRecord")

    def _store_receipt(self, receipt: BrowserLifecycleControlReceipt) -> None:
        self.lease_store.record_control(receipt.to_dict())
        with self._lock:
            existing = self._receipts.get(receipt.request_id)
            if existing is not None:
                if existing.request_fingerprint != receipt.request_fingerprint:
                    raise BrowserLifecycleTransactionError(
                        "browser_lifecycle_receipt_conflict",
                        "lifecycle receipt fingerprint conflict",
                        session_id=receipt.browser_session_id,
                    )
                return
            self._receipts[receipt.request_id] = receipt
            self._receipt_order.append(receipt.request_id)
            while len(self._receipt_order) > self.receipt_limit:
                stale = self._receipt_order.pop(0)
                self._receipts.pop(stale, None)

    def _session_lock(self, key: str) -> threading.RLock:
        with self._lock:
            return self._session_locks.setdefault(key, threading.RLock())

    def _current_generation(self, browser_session_id: str) -> int:
        if not browser_session_id:
            return 0
        lease = self.lease_store.get_lease(browser_session_id)
        if lease is not None:
            return lease.generation
        try:
            session = self.browser_runtime.get_session(browser_session_id)
            return int(getattr(session, "revision", 0))
        except Exception:
            return 0

    @staticmethod
    def _result_session_id(result: Any) -> str:
        session = getattr(result, "session", None)
        return str(getattr(session, "session_id", "") or "")

    @staticmethod
    def _require_command(command: BrowserSessionCommand | None) -> BrowserSessionCommand:
        if command is None:
            raise BrowserLifecycleTransactionError(
                "browser_session_command_missing",
                "start lifecycle transaction requires BrowserSessionCommand",
            )
        return command

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserLifecycleTransactionError(
                "browser_lifecycle_transactions_disabled",
                "browser lifecycle transaction runtime is disabled",
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runtime_id": "zyra-browser-lifecycle-transactions",
                "owner_unit": "M1-S04A-02",
                "disabled": self.disabled,
                "receipts": len(self._receipts),
                "running": len(self._running),
                "cancellations": len(self._cancellations),
                "completed": self._completed,
                "failed": self._failed,
                "cancelled": self._cancelled,
                "late": self._late,
                "conflicts": self._conflicts,
                "last_error": self._last_error,
                "lease_store": self.lease_store.snapshot(),
                "canonical_store_owner": False,
            }
