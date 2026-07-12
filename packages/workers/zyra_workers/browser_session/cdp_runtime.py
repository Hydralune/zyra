from __future__ import annotations

import queue
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Protocol

from zyra_integrations.browser_use import BrowserEventBus, CdpMessageCodec, RequestIdAllocator

from .errors import BrowserConnectionLost, BrowserProtocolError, BrowserRequestTimeout, BrowserTransportError
from .models import BrowserConnectionStatus, BrowserRequestReceipt, browser_now, stable_digest


class CdpTransportPort(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send(self, payload: str) -> None: ...
    def receive(self, timeout: float | None = None) -> str: ...
    def alive(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class CdpRuntimeSnapshot:
    session_id: str
    status: BrowserConnectionStatus
    generation: int
    pending_requests: int
    completed_requests: int
    timed_out_requests: int
    failed_requests: int
    received_events: int
    late_messages: int
    last_message_at: str
    last_error: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": str(self.status),
            "generation": self.generation,
            "pending_requests": self.pending_requests,
            "completed_requests": self.completed_requests,
            "timed_out_requests": self.timed_out_requests,
            "failed_requests": self.failed_requests,
            "received_events": self.received_events,
            "late_messages": self.late_messages,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class _PendingRequest:
    request_id: int
    method: str
    cdp_session_id: str
    generation: int
    future: Future[dict[str, Any]]
    started_monotonic: float
    timeout_seconds: float
    started_at: str = field(default_factory=browser_now)


class MemoryCdpTransport:
    def __init__(self, responder: Callable[[str], str | None] | None = None) -> None:
        self.responder = responder
        self.incoming: queue.Queue[str] = queue.Queue()
        self.sent: list[str] = []
        self._alive = False

    def open(self) -> None:
        self._alive = True

    def close(self) -> None:
        self._alive = False

    def send(self, payload: str) -> str | None:
        if not self._alive:
            raise BrowserConnectionLost("CDP transport is closed")
        self.sent.append(payload)
        if self.responder is not None:
            response = self.responder(payload)
            if response is not None:
                return response
        return None

    def receive(self, timeout: float | None = None) -> str:
        if not self._alive and self.incoming.empty():
            raise BrowserConnectionLost("CDP transport is closed")
        try:
            return self.incoming.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("CDP transport receive timed out") from error

    def alive(self) -> bool:
        return self._alive

    def inject(self, payload: str) -> None:
        self.incoming.put(payload)


class CdpRequestRuntime:
    def __init__(
        self,
        session_id: str,
        request_timeout_seconds: float,
        event_bus: BrowserEventBus,
        transport_factory: Callable[[], CdpTransportPort] | None = None,
        *,
        disabled: bool = False,
        receipt_limit: int = 2048,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("CDP request timeout must be positive")
        self.session_id = session_id
        self.request_timeout_seconds = request_timeout_seconds
        self.event_bus = event_bus
        self.transport_factory = transport_factory or MemoryCdpTransport
        self.disabled = disabled
        self.receipt_limit = max(64, receipt_limit)
        self.codec = CdpMessageCodec()
        self.allocator = RequestIdAllocator()
        self._transport: CdpTransportPort | None = None
        self._status = BrowserConnectionStatus.NEW
        self._generation = 0
        self._pending: dict[int, _PendingRequest] = {}
        self._receipts: deque[BrowserRequestReceipt] = deque(maxlen=self.receipt_limit)
        self._event_handlers: dict[str, list[Callable[[dict[str, Any]], None]]] = defaultdict(list)
        self._reader: threading.Thread | None = None
        self._monitor: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._completed = 0
        self._timed_out = 0
        self._failed = 0
        self._received_events = 0
        self._late_messages = 0
        self._last_message_at = ""
        self._last_error = ""

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserTransportError("CDP request runtime is disabled", code="browser_cdp_runtime_disabled")

    def connect(self) -> int:
        self._ensure_available()
        with self._lock:
            if self._status == BrowserConnectionStatus.OPEN and self._transport and self._transport.alive():
                return self._generation
            self._status = BrowserConnectionStatus.OPENING
            self._generation += 1
            generation = self._generation
            transport = self.transport_factory()
            self._transport = transport
            self._stop_event = threading.Event()
        try:
            transport.open()
            self._install_transport_callback(transport, generation)
        except Exception as error:
            with self._lock:
                self._status = BrowserConnectionStatus.LOST
                self._last_error = f"{type(error).__name__}: {error}"
            raise BrowserTransportError("failed to open CDP transport", details={"error": str(error)}) from error
        with self._lock:
            self._status = BrowserConnectionStatus.OPEN
            self._reader = threading.Thread(target=self._reader_loop, args=(generation,), daemon=True)
            self._monitor = threading.Thread(target=self._timeout_loop, args=(generation,), daemon=True)
            self._reader.start()
            self._monitor.start()
        self._publish("browser.cdp.connected", {"session_id": self.session_id, "generation": generation})
        return generation

    def close(self, *, reason: str = "requested") -> None:
        with self._lock:
            if self._status in {BrowserConnectionStatus.NEW, BrowserConnectionStatus.CLOSED}:
                self._status = BrowserConnectionStatus.CLOSED
                return
            self._status = BrowserConnectionStatus.CLOSING
            self._stop_event.set()
            transport = self._transport
            generation = self._generation
        if transport is not None:
            try:
                transport.close()
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"
        self._fail_generation(generation, BrowserConnectionLost(f"CDP connection closed: {reason}"))
        with self._lock:
            self._transport = None
            self._status = BrowserConnectionStatus.CLOSED
        self._publish("browser.cdp.closed", {"session_id": self.session_id, "generation": generation, "reason": reason})

    def reconnect(self) -> int:
        old_generation = self._generation
        self.close(reason="reconnect")
        generation = self.connect()
        self._publish(
            "browser.cdp.reconnected",
            {"session_id": self.session_id, "old_generation": old_generation, "generation": generation},
        )
        return generation

    def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        cdp_session_id: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self._ensure_available()
        with self._lock:
            if self._status != BrowserConnectionStatus.OPEN or self._transport is None:
                raise BrowserConnectionLost("CDP connection is not open", session_id=self.session_id, operation=method)
            _allocator_generation, request_id = self.allocator.allocate()
            generation = self._generation
            timeout = timeout_seconds or self.request_timeout_seconds
            future: Future[dict[str, Any]] = Future()
            pending = _PendingRequest(
                request_id=request_id,
                method=method,
                cdp_session_id=cdp_session_id,
                generation=generation,
                future=future,
                started_monotonic=time.monotonic(),
                timeout_seconds=timeout,
            )
            self._pending[request_id] = pending
            transport = self._transport
        try:
            payload = self._encode_request(request_id, method, params or {}, cdp_session_id)
            synchronous_response = transport.send(payload)
            if synchronous_response is not None:
                self.ingest(synchronous_response, generation=generation)
        except Exception as error:
            self._complete_failure(pending, error)
            raise BrowserTransportError(
                f"failed to send CDP method {method}",
                session_id=self.session_id,
                operation=method,
            ) from error
        try:
            result = future.result(timeout=timeout + 0.25)
        except TimeoutError as error:
            self._expire_request(pending)
            raise BrowserRequestTimeout(
                f"CDP method {method} did not respond within {timeout:.3f}s",
                session_id=self.session_id,
                operation=method,
                details={"request_id": request_id, "cdp_session_id": cdp_session_id, "generation": generation},
            ) from error
        if "error" in result:
            error_value = result.get("error")
            raise BrowserProtocolError(
                f"CDP method {method} returned an error: {error_value}",
                session_id=self.session_id,
                operation=method,
                details={"request_id": request_id, "error": error_value},
            )
        return dict(result.get("result") or {})

    def _install_transport_callback(self, transport: CdpTransportPort, generation: int) -> None:
        callback = lambda payload: self.ingest(payload, generation=generation)
        for method_name in ("set_message_handler", "set_on_message", "register_message_handler"):
            method = getattr(transport, method_name, None)
            if callable(method):
                method(callback)
                return
        if hasattr(transport, "on_message"):
            try:
                setattr(transport, "on_message", callback)
            except (AttributeError, TypeError):
                pass

    def ingest(self, payload: Any, *, generation: int | None = None) -> None:
        active_generation = self._generation if generation is None else generation
        if isinstance(payload, Mapping):
            message = dict(payload)
        elif isinstance(payload, bytes):
            message = self._decode(payload.decode("utf-8"))
        elif isinstance(payload, str):
            message = self._decode(payload)
        elif hasattr(payload, "to_dict"):
            message = dict(payload.to_dict())
        else:
            raise BrowserProtocolError("CDP transport callback returned an unsupported message")
        self._last_message_at = browser_now()
        if "id" in message:
            self._handle_response(active_generation, message)
        elif "method" in message:
            self._handle_event(active_generation, message)
        else:
            raise BrowserProtocolError("CDP message has neither response id nor event method")

    def register(self, method: str, handler: Callable[[dict[str, Any]], None]) -> None:
        if not method:
            raise ValueError("CDP event method is required")
        with self._lock:
            if handler not in self._event_handlers[method]:
                self._event_handlers[method].append(handler)

    def unregister(self, method: str, handler: Callable[[dict[str, Any]], None]) -> bool:
        with self._lock:
            handlers = self._event_handlers.get(method, [])
            if handler not in handlers:
                return False
            handlers.remove(handler)
            return True

    def _encode_request(self, request_id: int, method: str, params: Mapping[str, Any], session_id: str) -> str:
        if hasattr(self.codec, "encode_request"):
            return self.codec.encode_request(request_id, method, params, session_id=session_id or None)
        import json
        value: dict[str, Any] = {"id": request_id, "method": method, "params": dict(params)}
        if session_id:
            value["sessionId"] = session_id
        return json.dumps(value, separators=(",", ":"))

    def _decode(self, payload: str) -> dict[str, Any]:
        if hasattr(self.codec, "decode"):
            decoded = self.codec.decode(payload)
            if isinstance(decoded, Mapping):
                return dict(decoded)
            if hasattr(decoded, "to_dict"):
                return dict(decoded.to_dict())
        import json
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise BrowserProtocolError("CDP message is not an object")
        return value

    def _reader_loop(self, generation: int) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                transport = self._transport
                active = generation == self._generation
            if not active or transport is None:
                return
            try:
                payload = transport.receive(timeout=0.25)
            except TimeoutError:
                continue
            except Exception as error:
                self._handle_connection_loss(generation, error)
                return
            if payload is None:
                continue
            try:
                self.ingest(payload, generation=generation)
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"
                self._publish("browser.cdp.protocol_error", {"generation": generation, "error": self._last_error})
                continue

    def _timeout_loop(self, generation: int) -> None:
        while not self._stop_event.wait(0.05):
            now = time.monotonic()
            with self._lock:
                if generation != self._generation:
                    return
                expired = [
                    pending for pending in self._pending.values()
                    if pending.generation == generation and now - pending.started_monotonic >= pending.timeout_seconds
                ]
            for pending in expired:
                self._expire_request(pending)

    def _handle_response(self, generation: int, message: dict[str, Any]) -> None:
        try:
            request_id = int(message["id"])
        except (TypeError, ValueError):
            self._last_error = "invalid CDP response id"
            return
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None or pending.generation != generation:
            self._late_messages += 1
            return
        if not pending.future.done():
            pending.future.set_result(message)
        self._completed += 1
        receipt = BrowserRequestReceipt(
            request_id=request_id,
            method=pending.method,
            session_id=pending.cdp_session_id,
            generation=generation,
            started_at=pending.started_at,
            completed_at=browser_now(),
            ok="error" not in message,
            error=str(message.get("error") or ""),
            result_digest=stable_digest(message.get("result") or {}),
        )
        self._receipts.append(receipt)

    def _handle_event(self, generation: int, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = dict(message.get("params") or {})
        params["_cdp_session_id"] = str(message.get("sessionId") or "")
        params["_generation"] = generation
        with self._lock:
            handlers = tuple(self._event_handlers.get(method, ()))
        self._received_events += 1
        self._publish("browser.cdp.event", {"method": method, "generation": generation, "params": params})
        for handler in handlers:
            try:
                handler(params)
            except Exception as error:
                self._publish(
                    "browser.cdp.handler_error",
                    {"method": method, "error": f"{type(error).__name__}: {error}"},
                )

    def _expire_request(self, pending: _PendingRequest) -> None:
        with self._lock:
            current = self._pending.pop(pending.request_id, None)
        if current is None:
            return
        error = BrowserRequestTimeout(
            f"CDP request {pending.method} timed out",
            session_id=self.session_id,
            operation=pending.method,
        )
        if not pending.future.done():
            pending.future.set_exception(error)
        self._timed_out += 1
        self._receipts.append(
            BrowserRequestReceipt(
                request_id=pending.request_id,
                method=pending.method,
                session_id=pending.cdp_session_id,
                generation=pending.generation,
                started_at=pending.started_at,
                completed_at=browser_now(),
                ok=False,
                error="timeout",
            )
        )
        self._publish(
            "browser.cdp.request_timeout",
            {"request_id": pending.request_id, "method": pending.method, "generation": pending.generation},
        )

    def _complete_failure(self, pending: _PendingRequest, error: BaseException) -> None:
        with self._lock:
            self._pending.pop(pending.request_id, None)
        if not pending.future.done():
            pending.future.set_exception(error)
        self._failed += 1
        self._receipts.append(
            BrowserRequestReceipt(
                request_id=pending.request_id,
                method=pending.method,
                session_id=pending.cdp_session_id,
                generation=pending.generation,
                started_at=pending.started_at,
                completed_at=browser_now(),
                ok=False,
                error=f"{type(error).__name__}: {error}",
            )
        )

    def _handle_connection_loss(self, generation: int, error: BaseException) -> None:
        with self._lock:
            if generation != self._generation or self._status == BrowserConnectionStatus.CLOSING:
                return
            self._status = BrowserConnectionStatus.LOST
            self._last_error = f"{type(error).__name__}: {error}"
        self._fail_generation(generation, BrowserConnectionLost(str(error), session_id=self.session_id))
        self._publish("browser.cdp.connection_lost", {"generation": generation, "error": self._last_error})

    def _fail_generation(self, generation: int, error: BaseException) -> None:
        with self._lock:
            pending = [item for item in self._pending.values() if item.generation == generation]
        for item in pending:
            self._complete_failure(item, error)

    def _publish(self, topic: str, payload: Mapping[str, Any]) -> None:
        try:
            self.event_bus.publish(topic, payload)
        except RuntimeError:
            try:
                self.event_bus.start()
                self.event_bus.publish(topic, payload)
            except RuntimeError:
                pass

    def snapshot(self) -> CdpRuntimeSnapshot:
        with self._lock:
            return CdpRuntimeSnapshot(
                session_id=self.session_id,
                status=self._status,
                generation=self._generation,
                pending_requests=len(self._pending),
                completed_requests=self._completed,
                timed_out_requests=self._timed_out,
                failed_requests=self._failed,
                received_events=self._received_events,
                late_messages=self._late_messages,
                last_message_at=self._last_message_at,
                last_error=self._last_error,
            )

    def receipts(self, *, method: str = "") -> tuple[BrowserRequestReceipt, ...]:
        values = tuple(self._receipts)
        if method:
            values = tuple(item for item in values if item.method == method)
        return values

    def send_batch(
        self,
        requests: list[tuple[str, Mapping[str, Any], str]],
        *,
        stop_on_error: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        results: list[dict[str, Any]] = []
        for method, params, cdp_session_id in requests:
            try:
                result = self.send(method, params, cdp_session_id=cdp_session_id)
            except Exception:
                if stop_on_error:
                    raise
                result = {"_error": True, "method": method}
            results.append(result)
        return tuple(results)

    def cancel_pending(self, *, reason: str = "cancelled", method: str = "") -> int:
        with self._lock:
            pending = [item for item in self._pending.values() if not method or item.method == method]
        error = BrowserTransportError(reason, session_id=self.session_id, code="browser_cdp_request_cancelled")
        for item in pending:
            self._complete_failure(item, error)
        return len(pending)

    def wait_until_idle(self, *, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._pending:
                    return True
            time.sleep(0.01)
        return False

    def stale(self, *, max_silence_seconds: float) -> bool:
        if max_silence_seconds <= 0:
            raise ValueError("maximum silence must be positive")
        with self._lock:
            if self._status != BrowserConnectionStatus.OPEN:
                return True
            pending = bool(self._pending)
            last = self._last_message_at
        if not pending or not last:
            return False
        from datetime import datetime
        normalized = last.replace("Z", "+00:00")
        age = time.time() - datetime.fromisoformat(normalized).timestamp()
        return age >= max_silence_seconds

    def probe(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        return {
            "ok": snapshot.status == BrowserConnectionStatus.OPEN,
            "session_id": self.session_id,
            "generation": snapshot.generation,
            "transport_alive": bool(self._transport and self._transport.alive()),
            "pending_requests": snapshot.pending_requests,
            "last_message_at": snapshot.last_message_at,
            "last_error": snapshot.last_error,
        }

    def enable_page_runtime(self, *, cdp_session_id: str) -> tuple[dict[str, Any], ...]:
        return self.send_batch(
            [
                ("Page.enable", {}, cdp_session_id),
                ("Page.setLifecycleEventsEnabled", {"enabled": True}, cdp_session_id),
                ("Network.enable", {}, cdp_session_id),
                ("Runtime.enable", {}, cdp_session_id),
            ]
        )

    def set_auto_attach(self, *, flatten: bool = True, wait_for_debugger: bool = False) -> dict[str, Any]:
        return self.send(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": wait_for_debugger,
                "flatten": flatten,
            },
        )

    def discover_targets(self) -> dict[str, Any]:
        self.send("Target.setDiscoverTargets", {"discover": True})
        return self.send("Target.getTargets", {})
