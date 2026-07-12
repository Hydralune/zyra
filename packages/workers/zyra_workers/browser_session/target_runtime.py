from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from zyra_integrations.browser_use import BrowserEndpointDiscovery, BrowserEventBus

from .errors import BrowserFocusError, BrowserTargetDetached, BrowserTargetError
from .models import BrowserCdpSessionRef, BrowserTargetRef, BrowserTargetStatus, browser_now


@dataclass(frozen=True, slots=True)
class TargetRuntimeSnapshot:
    session_id: str
    generation: int
    targets: tuple[BrowserTargetRef, ...]
    cdp_sessions: tuple[BrowserCdpSessionRef, ...]
    active_target_id: str
    recovery_in_progress: bool
    lifecycle_events: int
    attach_count: int
    detach_count: int
    focus_recovery_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "generation": self.generation,
            "targets": [item.to_dict() for item in self.targets],
            "cdp_sessions": [item.to_dict() for item in self.cdp_sessions],
            "active_target_id": self.active_target_id,
            "recovery_in_progress": self.recovery_in_progress,
            "lifecycle_events": self.lifecycle_events,
            "attach_count": self.attach_count,
            "detach_count": self.detach_count,
            "focus_recovery_count": self.focus_recovery_count,
        }


class BrowserTargetRuntime:
    def __init__(
        self,
        session_id: str,
        event_bus: BrowserEventBus,
        discovery_factory: Callable[[], BrowserEndpointDiscovery],
        *,
        disabled: bool = False,
        lifecycle_limit: int = 1024,
    ) -> None:
        self.session_id = session_id
        self.event_bus = event_bus
        self.discovery_factory = discovery_factory
        self.disabled = disabled
        self.lifecycle_limit = max(64, lifecycle_limit)
        self._targets: dict[str, BrowserTargetRef] = {}
        self._sessions: dict[str, BrowserCdpSessionRef] = {}
        self._target_sessions: dict[str, set[str]] = defaultdict(set)
        self._session_targets: dict[str, str] = {}
        self._lifecycle_events: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self.lifecycle_limit)
        )
        self._active_target_id = ""
        self._generation = 0
        self._recovery_in_progress = False
        self._recovery_event = threading.Event()
        self._recovery_event.set()
        self._lock = threading.RLock()
        self._attach_count = 0
        self._detach_count = 0
        self._focus_recovery_count = 0

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserTargetError("browser target runtime is disabled", code="browser_target_runtime_disabled")

    def start(self) -> TargetRuntimeSnapshot:
        self._ensure_available()
        with self._lock:
            self._generation += 1
        self.reconcile()
        self.ensure_valid_focus()
        return self.snapshot()

    def clear(self) -> None:
        with self._lock:
            self._targets.clear()
            self._sessions.clear()
            self._target_sessions.clear()
            self._session_targets.clear()
            self._lifecycle_events.clear()
            self._active_target_id = ""
            self._recovery_in_progress = False
            self._recovery_event.set()

    def reconcile(self, *, recover_focus: bool = True) -> tuple[BrowserTargetRef, ...]:
        self._ensure_available()
        discovery = self.discovery_factory()
        discovered = discovery.list_targets()
        seen: set[str] = set()
        with self._lock:
            for descriptor in discovered:
                if not descriptor.target_id:
                    continue
                seen.add(descriptor.target_id)
                current = self._targets.get(descriptor.target_id)
                target = BrowserTargetRef(
                    target_id=descriptor.target_id,
                    target_type=descriptor.target_type,
                    url=descriptor.url,
                    title=descriptor.title,
                    status=current.status if current else str(BrowserTargetStatus.DISCOVERED),
                    session_ids=current.session_ids if current else (),
                    opener_id=descriptor.opener_id,
                    attached_at=current.attached_at if current else "",
                    updated_at=browser_now(),
                )
                self._targets[target.target_id] = target
            stale = [target_id for target_id in self._targets if target_id not in seen]
        for target_id in stale:
            self.detach_target(
                target_id,
                reason="discovery_reconcile",
                recover_focus=recover_focus,
            )
        self._publish(
            "browser.target.reconciled",
            {"session_id": self.session_id, "target_count": len(seen), "generation": self._generation},
        )
        return self.targets()

    def attach_target(
        self,
        target_id: str,
        cdp_session_id: str,
        *,
        target_type: str = "page",
        url: str = "",
        title: str = "",
    ) -> BrowserTargetRef:
        self._ensure_available()
        if not target_id or not cdp_session_id:
            raise BrowserTargetError("target and CDP session ids are required")
        with self._lock:
            existing_session = self._sessions.get(cdp_session_id)
            if existing_session and existing_session.target_id != target_id:
                raise BrowserTargetError("CDP session is already attached to another target")
            target = self._targets.get(target_id) or BrowserTargetRef(
                target_id=target_id,
                target_type=target_type,
                url=url,
                title=title,
            )
            session_ids = tuple(dict.fromkeys((*target.session_ids, cdp_session_id)))
            target = replace(
                target,
                status=str(BrowserTargetStatus.ATTACHED),
                session_ids=session_ids,
                attached_at=target.attached_at or browser_now(),
                updated_at=browser_now(),
            )
            cdp_session = BrowserCdpSessionRef(
                cdp_session_id=cdp_session_id,
                target_id=target_id,
                generation=self._generation,
            )
            self._targets[target_id] = target
            self._sessions[cdp_session_id] = cdp_session
            self._target_sessions[target_id].add(cdp_session_id)
            self._session_targets[cdp_session_id] = target_id
            self._attach_count += 1
        self._publish("browser.target.attached", {"target": target.to_dict(), "cdp_session_id": cdp_session_id})
        return target

    def detach_session(
        self,
        cdp_session_id: str,
        *,
        reason: str = "detached",
        recover_focus: bool = True,
    ) -> BrowserTargetRef | None:
        self._ensure_available()
        with self._lock:
            session = self._sessions.pop(cdp_session_id, None)
            target_id = self._session_targets.pop(cdp_session_id, "")
            if session is None or not target_id:
                return None
            session_ids = self._target_sessions.get(target_id, set())
            session_ids.discard(cdp_session_id)
            target = self._targets.get(target_id)
            if target is None:
                return None
            target = replace(
                target,
                session_ids=tuple(sorted(session_ids)),
                status=(str(BrowserTargetStatus.ATTACHED) if session_ids else str(BrowserTargetStatus.DETACHED)),
                updated_at=browser_now(),
            )
            self._targets[target_id] = target
            self._detach_count += 1
            recover = not session_ids and target_id == self._active_target_id
            if not session_ids:
                self._target_sessions.pop(target_id, None)
        self._publish(
            "browser.target.session_detached",
            {"target_id": target_id, "cdp_session_id": cdp_session_id, "reason": reason},
        )
        if recover and recover_focus:
            self.recover_focus(crashed_target_id=target_id)
        return target

    def detach_target(
        self,
        target_id: str,
        *,
        reason: str = "detached",
        recover_focus: bool = True,
    ) -> BrowserTargetRef | None:
        self._ensure_available()
        with self._lock:
            target = self._targets.get(target_id)
            if target is None:
                return None
            session_ids = tuple(self._target_sessions.get(target_id, ()))
        for session_id in session_ids:
            self.detach_session(session_id, reason=reason, recover_focus=False)
        with self._lock:
            target = self._targets.pop(target_id, target)
            self._target_sessions.pop(target_id, None)
            focused = target_id == self._active_target_id
            if focused:
                self._active_target_id = ""
        self._publish("browser.target.detached", {"target_id": target_id, "reason": reason})
        if focused and recover_focus:
            self.recover_focus(crashed_target_id=target_id)
        return replace(target, status=str(BrowserTargetStatus.DETACHED), session_ids=(), updated_at=browser_now())

    def focus(self, target_id: str, *, activate: bool = True) -> BrowserTargetRef:
        self._ensure_available()
        with self._lock:
            target = self._targets.get(target_id)
            if target is None or not target.is_page:
                raise BrowserFocusError("focus target must be an existing page target", session_id=self.session_id)
        if activate:
            try:
                self.discovery_factory().activate_target(target_id)
            except Exception as error:
                raise BrowserFocusError(
                    f"failed to activate target {target_id}: {error}",
                    session_id=self.session_id,
                ) from error
        with self._lock:
            self._active_target_id = target_id
        self._publish("browser.focus.changed", {"target_id": target_id, "url": target.url})
        return target

    def ensure_valid_focus(self, *, timeout: float = 3.0) -> BrowserTargetRef:
        self._ensure_available()
        with self._lock:
            target = self._targets.get(self._active_target_id)
            if target is not None and target.is_page:
                return target
            recovering = self._recovery_in_progress
        if recovering:
            self._recovery_event.wait(timeout)
            with self._lock:
                target = self._targets.get(self._active_target_id)
                if target is not None and target.is_page:
                    return target
        return self.recover_focus(crashed_target_id="")

    def recover_focus(self, *, crashed_target_id: str) -> BrowserTargetRef:
        self._ensure_available()
        with self._lock:
            if self._recovery_in_progress:
                event = self._recovery_event
                owner = False
            else:
                self._recovery_in_progress = True
                self._recovery_event = threading.Event()
                event = self._recovery_event
                owner = True
        if not owner:
            if not event.wait(5.0):
                raise BrowserFocusError("timed out waiting for browser focus recovery", session_id=self.session_id)
            with self._lock:
                recovered = self._targets.get(self._active_target_id)
            if recovered is None or not recovered.is_page:
                raise BrowserFocusError("browser focus recovery completed without a page", session_id=self.session_id)
            return recovered
        try:
            self.reconcile(recover_focus=False)
            pages = [target for target in self.targets() if target.is_page and target.target_id != crashed_target_id]
            pages.sort(key=lambda item: (item.updated_at, item.target_id), reverse=True)
            if pages:
                target = self.focus(pages[0].target_id)
            else:
                descriptor = self.discovery_factory().create_blank_target()
                target = BrowserTargetRef(
                    target_id=descriptor.target_id,
                    target_type=descriptor.target_type or "page",
                    url=descriptor.url or "about:blank",
                    title=descriptor.title,
                    status=str(BrowserTargetStatus.DISCOVERED),
                )
                with self._lock:
                    self._targets[target.target_id] = target
                target = self.focus(target.target_id)
                self._publish("browser.target.blank_created", {"target": target.to_dict()})
            self._focus_recovery_count += 1
            return target
        except Exception as error:
            raise BrowserFocusError(
                f"browser focus recovery failed: {error}",
                session_id=self.session_id,
            ) from error
        finally:
            with self._lock:
                self._recovery_in_progress = False
                self._recovery_event.set()

    def append_lifecycle(self, cdp_session_id: str, event: Mapping[str, Any]) -> None:
        with self._lock:
            if cdp_session_id not in self._sessions:
                raise BrowserTargetDetached("cannot append lifecycle event for detached CDP session")
            value = dict(event)
            value.setdefault("received_at", browser_now())
            self._lifecycle_events[cdp_session_id].append(value)

    def wait_for_lifecycle(
        self,
        cdp_session_id: str,
        names: set[str],
        *,
        timeout: float,
        since: float = 0.0,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                events = tuple(self._lifecycle_events.get(cdp_session_id, ()))
            for event in reversed(events):
                name = str(event.get("name") or event.get("event") or "")
                timestamp = float(event.get("timestamp") or 0.0)
                if name in names and timestamp >= since:
                    return dict(event)
            time.sleep(0.01)
        return None

    def target(self, target_id: str) -> BrowserTargetRef | None:
        with self._lock:
            return self._targets.get(target_id)

    def require_target(self, target_id: str) -> BrowserTargetRef:
        target = self.target(target_id)
        if target is None:
            raise BrowserTargetDetached(f"target {target_id} is detached", session_id=self.session_id)
        return target

    def targets(self) -> tuple[BrowserTargetRef, ...]:
        with self._lock:
            return tuple(sorted(self._targets.values(), key=lambda item: (not item.is_page, item.updated_at, item.target_id)))

    def page_targets(self) -> tuple[BrowserTargetRef, ...]:
        return tuple(item for item in self.targets() if item.is_page)

    def cdp_session(self, target_id: str) -> BrowserCdpSessionRef:
        with self._lock:
            session_ids = tuple(self._target_sessions.get(target_id, ()))
            for session_id in session_ids:
                session = self._sessions.get(session_id)
                if session is not None:
                    return session
        raise BrowserTargetDetached(f"target {target_id} has no attached CDP session", session_id=self.session_id)

    @property
    def active_target_id(self) -> str:
        with self._lock:
            return self._active_target_id

    def snapshot(self) -> TargetRuntimeSnapshot:
        with self._lock:
            return TargetRuntimeSnapshot(
                session_id=self.session_id,
                generation=self._generation,
                targets=tuple(self._targets.values()),
                cdp_sessions=tuple(self._sessions.values()),
                active_target_id=self._active_target_id,
                recovery_in_progress=self._recovery_in_progress,
                lifecycle_events=sum(len(value) for value in self._lifecycle_events.values()),
                attach_count=self._attach_count,
                detach_count=self._detach_count,
                focus_recovery_count=self._focus_recovery_count,
            )

    def _publish(self, topic: str, payload: Mapping[str, Any]) -> None:
        try:
            self.event_bus.publish(topic, payload)
        except RuntimeError:
            self.event_bus.start()
            self.event_bus.publish(topic, payload)
