from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from zyra_core import EventRecord, EventType

from ...browser_session.errors import BrowserTargetError
from ..models import (
    HealthStatus,
    ObservationScope,
    Severity,
    SignalKind,
    WatchdogName,
    WatchdogSignal,
    digest_value,
    new_observation_id,
    utc_now,
)
from ..security_policy import (
    BrowserSecurityPolicyEngine,
    SecurityDecision,
    SecurityVerdict,
)
from .contracts import (
    BrowserIntegrationOutput,
    EvidenceSource,
    EvidenceTerminalState,
    RuntimeEvidenceEnvelope,
)
from .event_bus import AttachedBrowserEvent, AttachedEventKind


class NavigationIntegrationError(BrowserTargetError):
    code = "browser_navigation_integration_error"


class NavigationEventKind(StrEnum):
    REQUEST = "request"
    REDIRECT = "redirect"
    FRAME_NAVIGATED = "frame_navigated"
    TARGET_CREATED = "target_created"


class NavigationClosePort(Protocol):
    def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        cdp_session_id: str = "",
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class NavigationSecurityPolicy:
    close_denied_redirect_target: bool = True
    close_denied_new_target: bool = True
    fail_closed_on_close_error: bool = True
    require_target_identity: bool = True
    retain_receipts: int = 2_000

    def __post_init__(self) -> None:
        if self.retain_receipts < 1:
            raise ValueError("navigation receipt retention must be positive")


@dataclass(frozen=True, slots=True)
class NavigationObservation:
    scope: ObservationScope
    kind: NavigationEventKind
    url: str
    target_id: str
    source_event_id: str
    redirect_index: int = 0
    previous_url: str = ""
    chain_id: str = ""
    cdp_session_id: str = ""
    browser_event_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("navigation observation requires URL")
        if self.redirect_index < 0:
            raise ValueError("redirect_index must be non-negative")
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_attached_event(
        cls,
        scope: ObservationScope,
        event: AttachedBrowserEvent,
    ) -> "NavigationObservation | None":
        if event.kind != AttachedEventKind.CDP_EVENT:
            return None
        method = str(event.payload.get("method") or "")
        params = _mapping(event.payload.get("params"))
        if method == "Page.frameNavigated":
            frame = _mapping(params.get("frame"))
            return cls(
                scope=scope,
                kind=NavigationEventKind.FRAME_NAVIGATED,
                url=str(frame.get("url") or ""),
                target_id=str(params.get("targetId") or event.payload.get("target_id") or ""),
                source_event_id=event.bus_event_id,
                redirect_index=int(params.get("redirectIndex") or 0),
                previous_url=str(params.get("previousUrl") or ""),
                chain_id=str(frame.get("loaderId") or params.get("chainId") or ""),
                cdp_session_id=str(params.get("_cdp_session_id") or ""),
                browser_event_id=event.receipt_id,
                metadata={"frame_id": frame.get("id"), "method": method},
            )
        if method == "Target.targetCreated":
            info = _mapping(params.get("targetInfo"))
            url = str(info.get("url") or "")
            if not url:
                return None
            return cls(
                scope=scope,
                kind=NavigationEventKind.TARGET_CREATED,
                url=url,
                target_id=str(info.get("targetId") or ""),
                source_event_id=event.bus_event_id,
                chain_id=str(info.get("openerId") or ""),
                browser_event_id=event.receipt_id,
                metadata={"target_type": info.get("type"), "method": method},
            )
        if method == "Fetch.requestPaused":
            request = _mapping(params.get("request"))
            response_status = int(params.get("responseStatusCode") or 0)
            location = ""
            for header in params.get("responseHeaders", ()) if isinstance(params.get("responseHeaders"), Sequence) else ():
                if isinstance(header, Mapping) and str(header.get("name") or "").casefold() == "location":
                    location = str(header.get("value") or "")
                    break
            return cls(
                scope=scope,
                kind=(NavigationEventKind.REDIRECT if 300 <= response_status < 400 else NavigationEventKind.REQUEST),
                url=location or str(request.get("url") or ""),
                target_id=str(params.get("targetId") or ""),
                source_event_id=event.bus_event_id,
                redirect_index=int(params.get("redirectIndex") or 0),
                previous_url=str(request.get("url") or "") if location else "",
                chain_id=str(params.get("requestId") or ""),
                cdp_session_id=str(params.get("_cdp_session_id") or ""),
                browser_event_id=event.receipt_id,
                metadata={"response_status": response_status, "method": method},
            )
        return None


@dataclass(frozen=True, slots=True)
class NavigationSecurityReceipt:
    observation: NavigationObservation
    verdict: SecurityVerdict
    closed: bool
    close_attempted: bool
    close_response: Mapping[str, Any] = field(default_factory=dict)
    receipt_id: str = field(
        default_factory=lambda: new_observation_id("browser-navigation-security")
    )
    error: str = ""
    created_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return self.verdict.allowed and not self.error

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.browser-observability.navigation-security.v1",
            "receipt_id": self.receipt_id,
            "scope": self.observation.scope.to_dict(),
            "observation": {
                "kind": str(self.observation.kind),
                "url_digest": digest_value(self.observation.url),
                "target_id": self.observation.target_id,
                "source_event_id": self.observation.source_event_id,
                "redirect_index": self.observation.redirect_index,
                "previous_url_digest": (
                    digest_value(self.observation.previous_url)
                    if self.observation.previous_url
                    else ""
                ),
                "chain_id": self.observation.chain_id,
                "cdp_session_id": self.observation.cdp_session_id,
                "metadata": dict(self.observation.metadata),
            },
            "verdict": self.verdict.to_dict(),
            "closed": self.closed,
            "close_attempted": self.close_attempted,
            "close_response": dict(self.close_response),
            "error": self.error,
            "created_at": self.created_at,
            "ok": self.ok,
            "policy_owner": "M1-04C/RedirectGuard",
            "close_evidence_owner": "M1-04D",
        }
        if include_digest:
            value["digest"] = digest_value(value)
        return value

    def evidence(self) -> RuntimeEvidenceEnvelope:
        return RuntimeEvidenceEnvelope(
            scope=self.observation.scope,
            source=EvidenceSource.BROWSER_SECURITY,
            event_type=f"navigation_{self.observation.kind}",
            payload=self.to_dict(),
            evidence_id=self.receipt_id,
            source_event_id=self.observation.source_event_id,
            correlation_event_ids=(self.observation.source_event_id,),
            terminal_state=(
                EvidenceTerminalState.COMPLETED
                if self.verdict.allowed
                else EvidenceTerminalState.FAILED
            ),
            outcome_unknown=bool(self.error and not self.closed),
            retryable=False,
            metadata={
                "target_id": self.observation.target_id,
                "closed": self.closed,
                "security_reason": str(self.verdict.reason),
            },
        )


class BrowserNavigationSecurityRuntime:
    """Enforce post-navigation closure for 04C-denied redirect/new targets."""

    def __init__(
        self,
        security_engine: BrowserSecurityPolicyEngine,
        *,
        policy: NavigationSecurityPolicy | None = None,
    ) -> None:
        self.security_engine = security_engine
        self.policy = policy or NavigationSecurityPolicy()
        self._receipts: list[NavigationSecurityReceipt] = []
        self._by_event: dict[str, NavigationSecurityReceipt] = {}

    def consume(
        self,
        scope: ObservationScope,
        events: Sequence[AttachedBrowserEvent],
        close_port: NavigationClosePort,
    ) -> BrowserIntegrationOutput:
        receipts: list[NavigationSecurityReceipt] = []
        for event in events:
            observation = NavigationObservation.from_attached_event(scope, event)
            if observation is None:
                continue
            if observation.source_event_id in self._by_event:
                continue
            receipt = self.evaluate_and_close(observation, close_port)
            self._by_event[observation.source_event_id] = receipt
            self._receipts.append(receipt)
            receipts.append(receipt)
        self._receipts = self._receipts[-self.policy.retain_receipts :]
        signals = tuple(
            self._signal(item)
            for item in receipts
            if not item.verdict.allowed
        )
        return BrowserIntegrationOutput(
            evidence=tuple(item.evidence() for item in receipts),
            signals=signals,
            events=tuple(self._event(item) for item in receipts),
            projection=self.projection(scope=scope),
        )

    def evaluate_and_close(
        self,
        observation: NavigationObservation,
        close_port: NavigationClosePort,
    ) -> NavigationSecurityReceipt:
        verdict = self.security_engine.evaluate(
            observation.url,
            redirect_index=observation.redirect_index,
            previous_url=observation.previous_url,
            chain_id=observation.chain_id,
        )
        should_close = (
            verdict.decision == SecurityDecision.DENY
            and (
                observation.kind == NavigationEventKind.TARGET_CREATED
                and self.policy.close_denied_new_target
                or observation.kind
                in {NavigationEventKind.REDIRECT, NavigationEventKind.FRAME_NAVIGATED}
                and self.policy.close_denied_redirect_target
            )
        )
        if should_close and self.policy.require_target_identity and not observation.target_id:
            error = "denied navigation lacks target identity"
            if self.policy.fail_closed_on_close_error:
                raise NavigationIntegrationError(
                    error,
                    session_id=observation.scope.browser_session_id,
                    operation="Target.closeTarget",
                )
            return NavigationSecurityReceipt(
                observation=observation,
                verdict=verdict,
                closed=False,
                close_attempted=False,
                error=error,
            )
        response: Mapping[str, Any] = {}
        closed = False
        error = ""
        if should_close:
            try:
                response = close_port.send(
                    "Target.closeTarget",
                    {"targetId": observation.target_id},
                )
                closed = bool(response.get("success", not response.get("error")))
                if not closed:
                    error = str(response.get("error") or "Target.closeTarget returned false")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            if error and self.policy.fail_closed_on_close_error:
                raise NavigationIntegrationError(
                    error,
                    session_id=observation.scope.browser_session_id,
                    operation="Target.closeTarget",
                    details={
                        "target_id": observation.target_id,
                        "security_verdict": verdict.to_dict(),
                    },
                )
        receipt = NavigationSecurityReceipt(
            observation=observation,
            verdict=verdict,
            closed=closed,
            close_attempted=should_close,
            close_response=dict(response),
            error=error,
        )
        if observation.chain_id and (
            verdict.decision == SecurityDecision.DENY
            or observation.kind == NavigationEventKind.FRAME_NAVIGATED
        ):
            self.security_engine.release_chain(observation.chain_id)
        return receipt

    def projection(self, *, scope: ObservationScope | None = None) -> dict[str, Any]:
        values = tuple(
            item
            for item in self._receipts
            if scope is None or item.observation.scope == scope
        )
        return {
            "schema": "zyra.browser-observability.navigation-security.v1",
            "receipt_count": len(values),
            "denied": sum(1 for item in values if not item.verdict.allowed),
            "closed_targets": sum(1 for item in values if item.closed),
            "close_failures": sum(1 for item in values if item.error),
            "receipts": [item.to_dict() for item in values[-100:]],
        }

    @staticmethod
    def _signal(receipt: NavigationSecurityReceipt) -> WatchdogSignal:
        return WatchdogSignal(
            scope=receipt.observation.scope,
            watchdog=WatchdogName.SECURITY,
            kind=SignalKind.SECURITY_BLOCK,
            status=HealthStatus.UNHEALTHY,
            severity=Severity.ERROR,
            summary=receipt.verdict.summary,
            sequence=max(1, receipt.observation.redirect_index + 1),
            retryable=False,
            terminal=True,
            evidence_event_ids=(receipt.observation.source_event_id,),
            metadata={
                "navigation_receipt_id": receipt.receipt_id,
                "target_id": receipt.observation.target_id,
                "target_closed": receipt.closed,
                "security_reason": str(receipt.verdict.reason),
                "url_digest": digest_value(receipt.observation.url),
            },
        )

    @staticmethod
    def _event(receipt: NavigationSecurityReceipt) -> EventRecord:
        return EventRecord(
            run_id=receipt.observation.scope.run_id,
            task_id=receipt.observation.scope.task_id,
            node_id=receipt.observation.scope.node_id or None,
            event_type=(
                EventType.WORKER_HEALTH
                if not receipt.verdict.allowed
                else EventType.BROWSER_RUNTIME_DIAGNOSTIC
            ),
            payload={
                "browser_navigation_security": receipt.to_dict(),
                "recovery_planner_owner": "M1-07C",
                "is_recovery_plan": False,
            },
        )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
