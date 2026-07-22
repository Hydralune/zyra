from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .contracts import CorrelationRefs, runtime_id, utc_now
from .source_session import ProcessHandle, RuntimeSourceSessionManager, SourceKind


class BrowserEventSource(Protocol):
    """Minimal event source implemented by BrowserActionRuntime/session ports."""

    def subscribe(self, event_name: str, callback: Callable[[Mapping[str, Any]], None]) -> Any: ...

    def unsubscribe(self, event_name: str, token: Any) -> None: ...


class BrowserBridgePhase(StrEnum):
    DETACHED = "detached"
    ATTACHING = "attaching"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    STOPPED = "stopped"
    DISABLED = "disabled"
    FAILED = "failed"


@dataclass(slots=True)
class BrowserEventSubscription:
    event_name: str
    token: Any
    attached: bool = True
    event_count: int = 0
    last_sequence: int = 0
    last_event_id: str = ""


@dataclass(slots=True)
class BrowserBridgeBinding:
    refs: CorrelationRefs
    generation: int
    phase: BrowserBridgePhase
    bridge_id: str
    source: BrowserEventSource
    subscriptions: dict[str, BrowserEventSubscription]
    process_handle: ProcessHandle | None = None
    revision: int = 1
    attach_count: int = 1
    detach_count: int = 0
    reconnect_count: int = 0
    dropped_stale_events: int = 0
    dropped_disabled_events: int = 0
    forwarded_events: int = 0
    last_error: str = ""
    attached_at: str = field(default_factory=utc_now)
    stopped_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_id": self.bridge_id,
            "refs": self.refs.to_dict(),
            "generation": self.generation,
            "phase": self.phase.value,
            "revision": self.revision,
            "attach_count": self.attach_count,
            "detach_count": self.detach_count,
            "reconnect_count": self.reconnect_count,
            "dropped_stale_events": self.dropped_stale_events,
            "dropped_disabled_events": self.dropped_disabled_events,
            "forwarded_events": self.forwarded_events,
            "last_error": self.last_error,
            "attached_at": self.attached_at,
            "stopped_at": self.stopped_at,
            "process_pid": None if self.process_handle is None else int(self.process_handle.pid),
            "subscriptions": {
                name: {
                    "attached": item.attached,
                    "event_count": item.event_count,
                    "last_sequence": item.last_sequence,
                    "last_event_id": item.last_event_id,
                }
                for name, item in sorted(self.subscriptions.items())
            },
        }


@dataclass(frozen=True, slots=True)
class BrowserBridgeReceipt:
    bridge_id: str
    browser_session_id: str
    generation: int
    action: str
    phase: BrowserBridgePhase
    changed: bool
    forwarded: bool
    details: Mapping[str, Any] = field(default_factory=dict)
    receipt_id: str = field(default_factory=lambda: runtime_id("browser-bridge"))
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-watchdog-event-bridge-receipt/v1",
            "receipt_id": self.receipt_id,
            "bridge_id": self.bridge_id,
            "browser_session_id": self.browser_session_id,
            "generation": self.generation,
            "action": self.action,
            "phase": self.phase.value,
            "changed": self.changed,
            "forwarded": self.forwarded,
            "details": dict(self.details),
            "created_at": self.created_at,
        }


class BrowserWatchdogEventBridge:
    """Active Browser Use-style attach/detach bridge over Zyra 04D evidence.

    Browser Use's relevant mature control flow is explicit callback attachment,
    lifecycle-event handling even while CDP is disconnected, generation-safe
    target listeners, and cancellation/detachment on stop. This class adapts
    those mechanics to the BrowserActionRuntime event port. Actual process/CDP
    evidence remains owned by the existing 04D detector and canonical fault
    state remains in ``FaultStateStore``.
    """

    EVENT_NAMES = (
        "browser.connected",
        "browser.cdp_disconnected",
        "browser.reconnecting",
        "browser.reconnected",
        "browser.heartbeat",
        "browser.process_exited",
        "browser.request_started",
        "browser.request_finished",
        "browser.stopped",
    )

    LIFECYCLE_EVENTS = frozenset(
        {
            "browser.connected",
            "browser.cdp_disconnected",
            "browser.reconnecting",
            "browser.reconnected",
            "browser.process_exited",
            "browser.stopped",
        }
    )

    def __init__(self, sessions: RuntimeSourceSessionManager) -> None:
        self.sessions = sessions
        self._guard = threading.RLock()
        self._bindings: dict[str, BrowserBridgeBinding] = {}
        self._callbacks: dict[str, dict[str, Callable[[Mapping[str, Any]], None]]] = {}
        self._receipts: list[BrowserBridgeReceipt] = []

    def attach(
        self,
        refs: CorrelationRefs,
        source: BrowserEventSource,
        *,
        generation: int,
        process_handle: ProcessHandle | None = None,
        cdp_connected: bool | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserBridgeReceipt:
        if not refs.browser_session_id:
            raise ValueError("browser event bridge requires browser_session_id")
        if generation < 0:
            raise ValueError("browser event bridge generation must be non-negative")
        if not callable(getattr(source, "subscribe", None)) or not callable(getattr(source, "unsubscribe", None)):
            raise TypeError("browser event source must implement subscribe/unsubscribe")
        with self._guard:
            current = self._bindings.get(refs.browser_session_id)
            if current is not None:
                if generation < current.generation:
                    raise RuntimeError("stale browser bridge generation")
                if generation == current.generation:
                    if current.source is not source:
                        raise RuntimeError("browser generation cannot change event source")
                    if current.phase is BrowserBridgePhase.RUNNING:
                        return self._receipt(current, "attach", False, False, {"already_attached": True})
                self._detach_locked(current, reason="replaced by newer generation", disable=False)
            binding = BrowserBridgeBinding(
                refs=refs,
                generation=generation,
                phase=BrowserBridgePhase.ATTACHING,
                bridge_id=runtime_id("browser-watchdog-bridge"),
                source=source,
                subscriptions={},
                process_handle=process_handle,
            )
            self._bindings[refs.browser_session_id] = binding
            self._callbacks[binding.bridge_id] = {}
        source_receipt = self.sessions.attach_browser(
            refs,
            generation=generation,
            process_handle=process_handle,
            cdp_connected=cdp_connected,
            metadata={
                **dict(metadata or {}),
                "browser_event_bridge_id": binding.bridge_id,
                "source_repo": "browser-use",
                "source_revision": "18484f23ac96bb955259a1c54530a7d265dfffdb",
                "migration_mode": "cropped_same_language",
            },
        )
        attached: list[str] = []
        try:
            for event_name in self.EVENT_NAMES:
                callback = self._callback(binding, event_name)
                token = source.subscribe(event_name, callback)
                with self._guard:
                    binding.subscriptions[event_name] = BrowserEventSubscription(event_name, token)
                    self._callbacks[binding.bridge_id][event_name] = callback
                    attached.append(event_name)
            with self._guard:
                binding.phase = BrowserBridgePhase.RUNNING
                binding.revision += 1
        except Exception as error:
            with self._guard:
                binding.phase = BrowserBridgePhase.FAILED
                binding.last_error = f"{type(error).__name__}: {error}"
                binding.revision += 1
                self._detach_subscriptions_locked(binding)
            raise
        return self._receipt(
            binding,
            "attach",
            True,
            False,
            {
                "attached_event_names": attached,
                "source_session_receipt": source_receipt.to_dict(),
                "lifecycle_events_survive_cdp_disconnect": True,
            },
        )

    def detach(
        self,
        browser_session_id: str,
        *,
        generation: int,
        reason: str,
        disable: bool = False,
    ) -> BrowserBridgeReceipt:
        with self._guard:
            binding = self._require_locked(browser_session_id, generation)
            if binding.phase in {BrowserBridgePhase.STOPPED, BrowserBridgePhase.DISABLED}:
                return self._receipt(binding, "disable" if disable else "detach", False, False, {"reason": reason})
            self._detach_locked(binding, reason=reason, disable=disable)
        session_receipt = self.sessions.stop(
            SourceKind.BROWSER,
            browser_session_id,
            generation=generation,
            reason=reason,
            disable=disable,
        )
        return self._receipt(
            binding,
            "disable" if disable else "detach",
            True,
            False,
            {
                "reason": reason,
                "source_session_receipt": session_receipt.to_dict(),
                "capture_path_removed": True,
                "injection_fallback_started": False,
            },
        )

    def dispatch(
        self,
        browser_session_id: str,
        event_name: str,
        event: Mapping[str, Any],
        *,
        generation: int,
    ) -> BrowserBridgeReceipt:
        with self._guard:
            binding = self._bindings.get(browser_session_id)
            if binding is None:
                raise RuntimeError("browser event bridge is not attached")
            if binding.generation != generation:
                binding.dropped_stale_events += 1
                return self._receipt(binding, event_name, False, False, {"stale_generation": True})
            if binding.phase in {BrowserBridgePhase.DISABLED, BrowserBridgePhase.STOPPED}:
                binding.dropped_disabled_events += 1
                return self._receipt(binding, event_name, False, False, {"capture_disabled": True})
        forwarded = self._handle(binding, event_name, event)
        return self._receipt(binding, event_name, forwarded, forwarded, {"event": dict(event)})

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        with self._guard:
            bindings = {
                session_id: binding.to_dict()
                for session_id, binding in sorted(self._bindings.items())
                if not task_id or binding.refs.task_id == task_id
            }
            receipts = [
                item.to_dict()
                for item in self._receipts[-200:]
                if not task_id
                or (binding := self._bindings.get(item.browser_session_id)) is None
                or binding.refs.task_id == task_id
            ]
        return {
            "schema": "zyra.browser-watchdog-event-bridge/v1",
            "bindings": bindings,
            "recent_receipts": receipts,
            "source_repo": "browser-use",
            "source_revision": "18484f23ac96bb955259a1c54530a7d265dfffdb",
            "migration_mode": "cropped_same_language",
            "source_state_owner": "M1-S04D.BrowserCrashDetector",
            "canonical_fault_state_owner": "FaultStateStore",
            "lifecycle_events_survive_cdp_disconnect": True,
            "disabled_capture_falls_back_to_injection": False,
        }

    def close(self) -> tuple[Mapping[str, Any], ...]:
        with self._guard:
            active = [
                (session_id, binding.generation)
                for session_id, binding in self._bindings.items()
                if binding.phase not in {BrowserBridgePhase.STOPPED, BrowserBridgePhase.DISABLED}
            ]
        receipts: list[Mapping[str, Any]] = []
        for session_id, generation in active:
            receipts.append(
                self.detach(
                    session_id,
                    generation=generation,
                    reason="fault integration runtime closed",
                ).to_dict()
            )
        return tuple(receipts)

    def _callback(
        self,
        binding: BrowserBridgeBinding,
        event_name: str,
    ) -> Callable[[Mapping[str, Any]], None]:
        generation = binding.generation
        browser_session_id = binding.refs.browser_session_id

        def callback(event: Mapping[str, Any]) -> None:
            self.dispatch(
                browser_session_id,
                event_name,
                event,
                generation=generation,
            )

        return callback

    def _handle(
        self,
        binding: BrowserBridgeBinding,
        event_name: str,
        event: Mapping[str, Any],
    ) -> bool:
        if event_name not in self.EVENT_NAMES:
            raise ValueError(f"unsupported browser watchdog event: {event_name}")
        sequence = int(event.get("sequence") or 0)
        event_id = str(event.get("event_id") or "")
        with self._guard:
            subscription = binding.subscriptions.get(event_name)
            if subscription is None or not subscription.attached:
                binding.dropped_disabled_events += 1
                return False
            if sequence and sequence <= subscription.last_sequence:
                binding.dropped_stale_events += 1
                return False
            if sequence:
                subscription.last_sequence = sequence
            if event_id and event_id == subscription.last_event_id:
                binding.dropped_stale_events += 1
                return False
            subscription.last_event_id = event_id
            subscription.event_count += 1
        forwarded = False
        session_id = binding.refs.browser_session_id
        generation = binding.generation
        at_ms = int(event["at_ms"]) if event.get("at_ms") is not None else None
        if event_name == "browser.connected":
            self.sessions.browser_cdp_connected(session_id, generation=generation, at_ms=at_ms)
            binding.phase = BrowserBridgePhase.RUNNING
            forwarded = True
        elif event_name == "browser.cdp_disconnected":
            self.sessions.browser_cdp_disconnected(
                session_id,
                generation=generation,
                reason_code=self._reason_code(event, "cdp_transport_closed"),
                at_ms=at_ms,
            )
            binding.phase = BrowserBridgePhase.RECONNECTING
            forwarded = True
        elif event_name == "browser.reconnecting":
            binding.phase = BrowserBridgePhase.RECONNECTING
            binding.reconnect_count += 1
            forwarded = True
        elif event_name == "browser.reconnected":
            self.sessions.browser_cdp_connected(session_id, generation=generation, at_ms=at_ms)
            binding.phase = BrowserBridgePhase.RUNNING
            forwarded = True
        elif event_name == "browser.heartbeat":
            if binding.phase is BrowserBridgePhase.RUNNING:
                self.sessions.browser_heartbeat(session_id, generation=generation, at_ms=at_ms)
                forwarded = True
        elif event_name == "browser.process_exited":
            self.sessions.browser_cdp_disconnected(
                session_id,
                generation=generation,
                reason_code=self._reason_code(event, "browser_process_exited"),
                at_ms=at_ms,
            )
            binding.phase = BrowserBridgePhase.RECONNECTING
            forwarded = True
        elif event_name == "browser.request_started":
            request_id = str(event.get("request_id") or "").strip()
            if not request_id:
                raise ValueError("browser request_started requires request_id")
            self.sessions.watchdog.browser_source.request_started(
                session_id,
                request_id,
                generation=generation,
                at_ms=at_ms,
            )
            forwarded = True
        elif event_name == "browser.request_finished":
            request_id = str(event.get("request_id") or "").strip()
            if not request_id:
                raise ValueError("browser request_finished requires request_id")
            self.sessions.watchdog.browser_source.request_finished(
                session_id,
                request_id,
                generation=generation,
            )
            forwarded = True
        elif event_name == "browser.stopped":
            self.detach(
                session_id,
                generation=generation,
                reason=str(event.get("reason") or "browser stopped"),
            )
            forwarded = True
        with self._guard:
            binding.forwarded_events += int(forwarded)
            binding.revision += 1
        return forwarded

    def _detach_locked(
        self,
        binding: BrowserBridgeBinding,
        *,
        reason: str,
        disable: bool,
    ) -> None:
        binding.phase = BrowserBridgePhase.STOPPING
        binding.revision += 1
        self._detach_subscriptions_locked(binding)
        binding.phase = BrowserBridgePhase.DISABLED if disable else BrowserBridgePhase.STOPPED
        binding.detach_count += 1
        binding.stopped_at = utc_now()
        binding.last_error = reason if disable else ""
        binding.revision += 1

    def _detach_subscriptions_locked(self, binding: BrowserBridgeBinding) -> None:
        for event_name, subscription in tuple(binding.subscriptions.items()):
            if not subscription.attached:
                continue
            try:
                binding.source.unsubscribe(event_name, subscription.token)
            finally:
                subscription.attached = False
        self._callbacks.pop(binding.bridge_id, None)

    def _require_locked(self, browser_session_id: str, generation: int) -> BrowserBridgeBinding:
        binding = self._bindings.get(browser_session_id)
        if binding is None:
            raise RuntimeError("browser event bridge is not attached")
        if binding.generation != generation:
            raise RuntimeError("stale browser bridge generation")
        return binding

    def _receipt(
        self,
        binding: BrowserBridgeBinding,
        action: str,
        changed: bool,
        forwarded: bool,
        details: Mapping[str, Any],
    ) -> BrowserBridgeReceipt:
        receipt = BrowserBridgeReceipt(
            bridge_id=binding.bridge_id,
            browser_session_id=binding.refs.browser_session_id,
            generation=binding.generation,
            action=action,
            phase=binding.phase,
            changed=changed,
            forwarded=forwarded,
            details={
                "run_id": binding.refs.run_id,
                "task_id": binding.refs.task_id,
                "session_id": binding.refs.session_id,
                **dict(details),
            },
        )
        with self._guard:
            self._receipts.append(receipt)
            del self._receipts[:-500]
        return receipt

    @staticmethod
    def _reason_code(event: Mapping[str, Any], fallback: str) -> str:
        value = str(event.get("reason_code") or fallback).strip()
        if not value:
            raise ValueError("browser lifecycle event requires structured reason_code")
        return value


def browser_integration_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.browser-watchdog-event-bridge-contract/v1",
        "source_repo": "browser-use",
        "source_revision": "18484f23ac96bb955259a1c54530a7d265dfffdb",
        "migration_mode": "cropped_same_language",
        "source_mechanisms": [
            "explicit attach_to_session handler registration",
            "lifecycle-event exemption during CDP disconnect",
            "target crash/process evidence forwarding",
            "explicit handler detach and task cleanup",
        ],
        "source_state_owner": "M1-S04D.BrowserCrashDetector",
        "canonical_fault_state_owner": "FaultStateStore",
        "generation_fenced": True,
        "disable_removes_callbacks": True,
        "disable_starts_injection_fallback": False,
    }


__all__ = [
    "BrowserBridgeBinding",
    "BrowserBridgePhase",
    "BrowserBridgeReceipt",
    "BrowserEventSource",
    "BrowserEventSubscription",
    "BrowserWatchdogEventBridge",
    "browser_integration_contract",
]
