from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import BrowserDomCapture, BrowserSelectorMapRevision, SelectorMapIdentity
from .errors import BrowserDomCaptureFailed, BrowserDomCaptureStale
from .state_delta import BrowserDomDelta


class BrowserDomInvalidationReason(StrEnum):
    TARGET_CHANGED = "target_changed"
    TARGET_GENERATION_CHANGED = "target_generation_changed"
    CDP_SESSION_CHANGED = "cdp_session_changed"
    CDP_GENERATION_CHANGED = "cdp_generation_changed"
    DOCUMENT_LOADER_CHANGED = "document_loader_changed"
    SESSION_REVISION_REGRESSED = "session_revision_regressed"
    CAPTURE_OUT_OF_ORDER = "capture_out_of_order"
    DOM_EMPTY = "dom_empty"
    SELECTOR_MAP_EMPTY = "selector_map_empty"
    SELECTOR_CONTINUITY_LOW = "selector_continuity_low"
    CAPTURE_INCOMPLETE = "capture_incomplete"
    EXPLICIT_INVALIDATION = "explicit_invalidation"


@dataclass(frozen=True, slots=True)
class BrowserDomInvalidation:
    browser_session_id: str
    reason: BrowserDomInvalidationReason
    previous_capture_id: str
    capture_id: str
    previous_revision_id: str
    revision_id: str
    details: Mapping[str, Any] = field(default_factory=dict)
    fatal: bool = True
    observed_at_monotonic: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "browser_session_id": self.browser_session_id,
            "reason": str(self.reason),
            "previous_capture_id": self.previous_capture_id,
            "capture_id": self.capture_id,
            "previous_revision_id": self.previous_revision_id,
            "revision_id": self.revision_id,
            "details": dict(self.details),
            "fatal": self.fatal,
            "observed_at_monotonic": self.observed_at_monotonic,
        }


@dataclass(frozen=True, slots=True)
class BrowserDomObservation:
    browser_session_id: str
    capture_id: str
    revision_id: str
    identity: SelectorMapIdentity
    session_revision: int
    state_digest: str
    selector_count: int
    dom_nodes: int
    capture_created_at: str
    accepted: bool
    invalidations: tuple[BrowserDomInvalidation, ...]
    delta_summary: Mapping[str, Any] = field(default_factory=dict)
    observed_at_monotonic: float = field(default_factory=time.monotonic)

    @property
    def current(self) -> bool:
        return self.accepted and not any(item.fatal for item in self.invalidations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "browser_session_id": self.browser_session_id,
            "capture_id": self.capture_id,
            "revision_id": self.revision_id,
            "identity": self.identity.to_dict(),
            "session_revision": self.session_revision,
            "state_digest": self.state_digest,
            "selector_count": self.selector_count,
            "dom_nodes": self.dom_nodes,
            "capture_created_at": self.capture_created_at,
            "accepted": self.accepted,
            "current": self.current,
            "invalidations": [item.to_dict() for item in self.invalidations],
            "delta_summary": dict(self.delta_summary),
            "observed_at_monotonic": self.observed_at_monotonic,
        }


class BrowserDomWatchdog:
    """Zyra-owned validity gate adapted from browser-use DOM watchdog.

    The gate observes authoritative 04A identity plus the 04B capture/revision.
    It never substitutes an empty state and never refreshes selectors itself.
    """

    def __init__(
        self,
        *,
        minimum_selector_continuity: float = 0.15,
        allow_empty_selector_map_for_empty_dom: bool = True,
        disabled: bool = False,
    ) -> None:
        if not 0.0 <= minimum_selector_continuity <= 1.0:
            raise ValueError("minimum selector continuity must be between zero and one")
        self.minimum_selector_continuity = minimum_selector_continuity
        self.allow_empty_selector_map_for_empty_dom = allow_empty_selector_map_for_empty_dom
        self.disabled = disabled
        self._lock = threading.RLock()
        self._observations: dict[str, BrowserDomObservation] = {}
        self._invalidations: dict[str, list[BrowserDomInvalidation]] = {}
        self._accepted = 0
        self._rejected = 0
        self._explicit_invalidations = 0

    def observe(
        self,
        capture: BrowserDomCapture,
        revision: BrowserSelectorMapRevision,
        *,
        delta: BrowserDomDelta | None = None,
    ) -> BrowserDomObservation:
        if self.disabled:
            raise BrowserDomCaptureFailed("browser DOM watchdog is disabled")
        session_id = capture.request.browser_session_id
        identity = SelectorMapIdentity.from_request(capture.request)
        with self._lock:
            previous = self._observations.get(session_id)
            invalidations = list(self._validate(capture, revision, identity, previous, delta))
            invalidations.extend(self._invalidations.pop(session_id, ()))
            accepted = not any(item.fatal for item in invalidations)
            observation = BrowserDomObservation(
                browser_session_id=session_id,
                capture_id=capture.capture_id,
                revision_id=revision.revision_id,
                identity=identity,
                session_revision=capture.request.session_revision,
                state_digest=capture.state_digest,
                selector_count=len(revision.entries),
                dom_nodes=capture.metrics.dom_nodes,
                capture_created_at=capture.created_at,
                accepted=accepted,
                invalidations=tuple(invalidations),
                delta_summary=delta.to_dict(change_limit=12) if delta else {},
            )
            if accepted:
                self._observations[session_id] = observation
                self._accepted += 1
            else:
                self._rejected += 1
        if not observation.current:
            reasons = ", ".join(str(item.reason) for item in observation.invalidations if item.fatal)
            raise BrowserDomCaptureStale(
                f"browser DOM watchdog rejected stale or incomplete state: {reasons}",
                details=observation.to_dict(),
            )
        return observation

    def invalidate(
        self,
        browser_session_id: str,
        *,
        reason: BrowserDomInvalidationReason = BrowserDomInvalidationReason.EXPLICIT_INVALIDATION,
        details: Mapping[str, Any] | None = None,
        fatal: bool = True,
    ) -> BrowserDomInvalidation:
        if not browser_session_id:
            raise ValueError("browser_session_id is required")
        with self._lock:
            previous = self._observations.get(browser_session_id)
            invalidation = BrowserDomInvalidation(
                browser_session_id=browser_session_id,
                reason=reason,
                previous_capture_id=previous.capture_id if previous else "",
                capture_id="",
                previous_revision_id=previous.revision_id if previous else "",
                revision_id="",
                details=dict(details or {}),
                fatal=fatal,
            )
            self._invalidations.setdefault(browser_session_id, []).append(invalidation)
            self._explicit_invalidations += 1
            if fatal:
                self._observations.pop(browser_session_id, None)
            return invalidation

    def current(self, browser_session_id: str) -> BrowserDomObservation:
        with self._lock:
            observation = self._observations.get(browser_session_id)
            pending = tuple(self._invalidations.get(browser_session_id, ()))
        if observation is None:
            raise BrowserDomCaptureStale(
                "browser DOM watchdog has no current observation",
                details={"browser_session_id": browser_session_id},
            )
        if any(item.fatal for item in pending):
            raise BrowserDomCaptureStale(
                "browser DOM observation was explicitly invalidated",
                details={
                    "browser_session_id": browser_session_id,
                    "invalidations": [item.to_dict() for item in pending],
                },
            )
        return observation

    def assert_selector_revision(
        self,
        browser_session_id: str,
        revision: BrowserSelectorMapRevision,
    ) -> BrowserDomObservation:
        observation = self.current(browser_session_id)
        if observation.revision_id != revision.revision_id:
            raise BrowserDomCaptureStale(
                "selector revision is not the watchdog current revision",
                details={
                    "browser_session_id": browser_session_id,
                    "expected_revision_id": observation.revision_id,
                    "actual_revision_id": revision.revision_id,
                },
            )
        if observation.identity.digest != revision.identity.digest:
            raise BrowserDomCaptureStale(
                "selector revision identity does not match current DOM observation",
                details={
                    "expected_identity": observation.identity.to_dict(),
                    "actual_identity": revision.identity.to_dict(),
                },
            )
        return observation

    def release(self, browser_session_id: str) -> None:
        with self._lock:
            self._observations.pop(browser_session_id, None)
            self._invalidations.pop(browser_session_id, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "owner": "BrowserDomWatchdog",
                "owner_unit": "M1-S04B-01",
                "disabled": self.disabled,
                "minimum_selector_continuity": self.minimum_selector_continuity,
                "accepted": self._accepted,
                "rejected": self._rejected,
                "explicit_invalidations": self._explicit_invalidations,
                "current_sessions": {
                    session_id: observation.to_dict()
                    for session_id, observation in self._observations.items()
                },
                "pending_invalidations": {
                    session_id: [item.to_dict() for item in invalidations]
                    for session_id, invalidations in self._invalidations.items()
                },
            }

    def _validate(
        self,
        capture: BrowserDomCapture,
        revision: BrowserSelectorMapRevision,
        identity: SelectorMapIdentity,
        previous: BrowserDomObservation | None,
        delta: BrowserDomDelta | None,
    ) -> tuple[BrowserDomInvalidation, ...]:
        values: list[BrowserDomInvalidation] = []

        def add(
            reason: BrowserDomInvalidationReason,
            details: Mapping[str, Any],
            *,
            fatal: bool = True,
        ) -> None:
            values.append(BrowserDomInvalidation(
                browser_session_id=capture.request.browser_session_id,
                reason=reason,
                previous_capture_id=previous.capture_id if previous else "",
                capture_id=capture.capture_id,
                previous_revision_id=previous.revision_id if previous else "",
                revision_id=revision.revision_id,
                details=dict(details),
                fatal=fatal,
            ))

        if capture.metrics.dom_nodes <= 0:
            add(BrowserDomInvalidationReason.DOM_EMPTY, {"dom_nodes": capture.metrics.dom_nodes})
        if revision.capture_digest != capture.state_digest:
            add(BrowserDomInvalidationReason.CAPTURE_OUT_OF_ORDER, {
                "capture_id": capture.capture_id,
                "revision_capture_id": revision.capture_id,
                "capture_digest": capture.state_digest,
                "revision_capture_digest": revision.capture_digest,
            })
        elif revision.capture_id != capture.capture_id:
            add(BrowserDomInvalidationReason.CAPTURE_OUT_OF_ORDER, {
                "capture_id": capture.capture_id,
                "revision_capture_id": revision.capture_id,
                "capture_digest": capture.state_digest,
                "reason": "unchanged_state_reused_authoritative_revision",
            }, fatal=False)
        if revision.identity.digest != identity.digest:
            add(BrowserDomInvalidationReason.CAPTURE_OUT_OF_ORDER, {
                "capture_identity": identity.to_dict(),
                "revision_identity": revision.identity.to_dict(),
            })
        if not revision.entries and capture.selector_count > 0:
            add(BrowserDomInvalidationReason.SELECTOR_MAP_EMPTY, {
                "capture_selector_count": capture.selector_count,
                "revision_entries": len(revision.entries),
            })
        if not revision.entries and capture.metrics.dom_nodes > 1 and not self.allow_empty_selector_map_for_empty_dom:
            add(BrowserDomInvalidationReason.SELECTOR_MAP_EMPTY, {
                "dom_nodes": capture.metrics.dom_nodes,
                "policy": "empty_selector_map_disallowed",
            })
        if str(capture.completeness) != "complete":
            add(BrowserDomInvalidationReason.CAPTURE_INCOMPLETE, {
                "completeness": str(capture.completeness),
                "warnings": list(capture.warnings),
            }, fatal=False)
        if previous is not None:
            if previous.session_revision > capture.request.session_revision:
                add(BrowserDomInvalidationReason.SESSION_REVISION_REGRESSED, {
                    "previous": previous.session_revision,
                    "current": capture.request.session_revision,
                })
            if previous.identity.target_id != identity.target_id:
                add(BrowserDomInvalidationReason.TARGET_CHANGED, {
                    "previous": previous.identity.target_id,
                    "current": identity.target_id,
                }, fatal=False)
            elif previous.identity.target_generation > identity.target_generation:
                add(BrowserDomInvalidationReason.TARGET_GENERATION_CHANGED, {
                    "previous": previous.identity.target_generation,
                    "current": identity.target_generation,
                })
            if previous.identity.cdp_session_id != identity.cdp_session_id:
                add(BrowserDomInvalidationReason.CDP_SESSION_CHANGED, {
                    "previous": previous.identity.cdp_session_id,
                    "current": identity.cdp_session_id,
                }, fatal=False)
            elif previous.identity.cdp_generation > identity.cdp_generation:
                add(BrowserDomInvalidationReason.CDP_GENERATION_CHANGED, {
                    "previous": previous.identity.cdp_generation,
                    "current": identity.cdp_generation,
                })
            if (
                previous.identity.document_loader_id
                and identity.document_loader_id
                and previous.identity.document_loader_id != identity.document_loader_id
            ):
                add(BrowserDomInvalidationReason.DOCUMENT_LOADER_CHANGED, {
                    "previous": previous.identity.document_loader_id,
                    "current": identity.document_loader_id,
                }, fatal=False)
        if delta is not None and not delta.full_navigation:
            if delta.selector_continuity < self.minimum_selector_continuity and delta.before_nodes:
                add(BrowserDomInvalidationReason.SELECTOR_CONTINUITY_LOW, {
                    "minimum": self.minimum_selector_continuity,
                    "actual": delta.selector_continuity,
                    "before_nodes": delta.before_nodes,
                    "after_nodes": delta.after_nodes,
                })
        return tuple(values)
