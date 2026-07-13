from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .models import digest_value, stable_id
from .network_policy import BrowserNetworkPolicy, NetworkPolicyError, NetworkReceipt, canonicalize_url


class RedirectGuardError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class InterceptionDisposition(StrEnum):
    CONTINUE = "continue"
    FAIL = "fail"


class RequestResourceType(StrEnum):
    DOCUMENT = "Document"
    XHR = "XHR"
    FETCH = "Fetch"
    SCRIPT = "Script"
    STYLESHEET = "Stylesheet"
    IMAGE = "Image"
    FONT = "Font"
    MEDIA = "Media"
    OTHER = "Other"


@dataclass(frozen=True, slots=True)
class InterceptedRequest:
    interception_id: str
    network_request_id: str
    frame_id: str
    target_id: str
    url: str
    method: str
    resource_type: RequestResourceType
    is_navigation: bool
    redirect_from_request_id: str = ""
    redirect_status: int = 0
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.interception_id or not self.url or not self.method:
            raise ValueError("intercepted browser request identity is incomplete")
        object.__setattr__(self, "headers", dict(self.headers))

    def public_dict(self) -> dict[str, Any]:
        return {
            "interception_id": self.interception_id,
            "network_request_id": self.network_request_id,
            "frame_id": self.frame_id,
            "target_id": self.target_id,
            "url": self.url,
            "method": self.method,
            "resource_type": str(self.resource_type),
            "is_navigation": self.is_navigation,
            "redirect_from_request_id": self.redirect_from_request_id,
            "redirect_status": self.redirect_status,
            "header_names": sorted(self.headers),
        }


@dataclass(frozen=True, slots=True)
class InterceptionDecision:
    decision_id: str
    disposition: InterceptionDisposition
    request: InterceptedRequest
    receipt: NetworkReceipt | None
    reason: str
    error_reason: str = "BlockedByClient"
    parent_receipt_id: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "disposition": str(self.disposition),
            "request": self.request.public_dict(),
            "receipt": self.receipt.public_dict() if self.receipt else None,
            "reason": self.reason,
            "error_reason": self.error_reason if self.disposition == InterceptionDisposition.FAIL else "",
            "parent_receipt_id": self.parent_receipt_id,
        }


class FetchInterceptionPort(Protocol):
    def continue_request(self, interception_id: str) -> None: ...

    def fail_request(self, interception_id: str, error_reason: str) -> None: ...


@dataclass(slots=True)
class RecordingFetchInterceptionPort:
    continued: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def continue_request(self, interception_id: str) -> None:
        self.continued.append(interception_id)

    def fail_request(self, interception_id: str, error_reason: str) -> None:
        self.failed.append((interception_id, error_reason))


class BrowserRedirectGuard:
    """Pre-network request interception for redirects and new target egress.

    Browser-use's post-navigation watchdog informed this boundary.  Zyra moves
    the decision to Fetch.requestPaused so a denied redirect hop is failed
    before Chrome sends it.  This guard owns only one in-flight action chain;
    canonical session/target state remains in 04A.
    """

    def __init__(
        self,
        policy: BrowserNetworkPolicy,
        port: FetchInterceptionPort,
        *,
        action_id: str,
        initial_receipt: NetworkReceipt,
        allow_subresources_same_origin: bool = True,
        block_cross_origin_subresources: bool = False,
        disabled: bool = False,
    ) -> None:
        if initial_receipt.action_id != action_id:
            raise ValueError("redirect guard initial receipt belongs to another action")
        self.policy = policy
        self.port = port
        self.action_id = action_id
        self.initial_receipt = initial_receipt
        self.allow_subresources_same_origin = allow_subresources_same_origin
        self.block_cross_origin_subresources = block_cross_origin_subresources
        self.disabled = disabled
        self._receipts_by_request: dict[str, NetworkReceipt] = {}
        self._decisions: list[InterceptionDecision] = []
        self._lock = threading.RLock()

    def handle(self, request: InterceptedRequest) -> InterceptionDecision:
        if self.disabled:
            raise RedirectGuardError("redirect_guard_disabled", "browser redirect guard is disabled")
        with self._lock:
            if any(item.request.interception_id == request.interception_id for item in self._decisions):
                raise RedirectGuardError("interception_replayed", "browser request interception was already decided")
            parent = self._parent_receipt(request)
            try:
                receipt = self._admit(request, parent)
            except NetworkPolicyError as exc:
                decision = InterceptionDecision(
                    decision_id=stable_id("brintercept", self.action_id, request.interception_id, "fail", exc.code),
                    disposition=InterceptionDisposition.FAIL,
                    request=request,
                    receipt=None,
                    reason=f"{exc.code}: {exc}",
                    parent_receipt_id=parent.receipt_id if parent else "",
                )
                self.port.fail_request(request.interception_id, decision.error_reason)
            else:
                decision = InterceptionDecision(
                    decision_id=stable_id("brintercept", self.action_id, request.interception_id, "continue", receipt.receipt_id),
                    disposition=InterceptionDisposition.CONTINUE,
                    request=request,
                    receipt=receipt,
                    reason="destination admitted before network dispatch",
                    parent_receipt_id=parent.receipt_id if parent else "",
                )
                self.port.continue_request(request.interception_id)
                if request.network_request_id:
                    self._receipts_by_request[request.network_request_id] = receipt
            self._decisions.append(decision)
            return decision

    def decisions(self) -> tuple[InterceptionDecision, ...]:
        with self._lock:
            return tuple(self._decisions)

    @property
    def forbidden_request_count(self) -> int:
        with self._lock:
            return sum(item.disposition == InterceptionDisposition.FAIL for item in self._decisions)

    @property
    def continued_request_count(self) -> int:
        with self._lock:
            return sum(item.disposition == InterceptionDisposition.CONTINUE for item in self._decisions)

    def chain_digest(self) -> str:
        return digest_value([item.public_dict() for item in self.decisions()])

    def _parent_receipt(self, request: InterceptedRequest) -> NetworkReceipt | None:
        if request.redirect_from_request_id:
            parent = self._receipts_by_request.get(request.redirect_from_request_id)
            if parent is None:
                raise RedirectGuardError(
                    "redirect_parent_missing",
                    "redirect request does not have an admitted parent receipt",
                )
            return parent
        return self._receipts_by_request.get(request.network_request_id) or self.initial_receipt

    def _admit(self, request: InterceptedRequest, parent: NetworkReceipt | None) -> NetworkReceipt:
        target = canonicalize_url(request.url)
        if request.is_navigation:
            if request.redirect_from_request_id or request.redirect_status:
                if parent is None:
                    raise NetworkPolicyError("redirect_parent_missing", "redirect has no approved parent")
                return self.policy.redirect(parent, target.url)
            if target.url == self.initial_receipt.canonical_url.url:
                return self.policy.revalidate(self.initial_receipt)
            if parent is not None and request.frame_id:
                return self.policy.new_target(parent, target.url)
            return self.policy.preflight(action_id=self.action_id, raw_url=target.url)
        if parent is None:
            raise NetworkPolicyError("subresource_parent_missing", "subresource request has no approved document receipt")
        if target.origin == parent.canonical_url.origin and self.allow_subresources_same_origin:
            return self.policy.preflight(action_id=self.action_id, raw_url=target.url, creator_origin=parent.canonical_url.origin)
        if self.block_cross_origin_subresources:
            raise NetworkPolicyError("cross_origin_subresource_denied", "cross-origin subresource request is prohibited")
        return self.policy.preflight(action_id=self.action_id, raw_url=target.url, creator_origin=parent.canonical_url.origin)


def intercepted_request_from_cdp(payload: Mapping[str, Any], *, target_id: str = "") -> InterceptedRequest:
    request = payload.get("request", {})
    if not isinstance(request, Mapping):
        raise RedirectGuardError("invalid_interception_payload", "Fetch.requestPaused payload has no request object")
    resource_raw = str(payload.get("resourceType") or "Other")
    try:
        resource_type = RequestResourceType(resource_raw)
    except ValueError:
        resource_type = RequestResourceType.OTHER
    response_status = int(payload.get("responseStatusCode") or 0)
    return InterceptedRequest(
        interception_id=str(payload.get("requestId") or ""),
        network_request_id=str(payload.get("networkId") or payload.get("requestId") or ""),
        frame_id=str(payload.get("frameId") or ""),
        target_id=target_id,
        url=str(request.get("url") or ""),
        method=str(request.get("method") or "GET"),
        resource_type=resource_type,
        is_navigation=bool(payload.get("frameId")) and resource_type == RequestResourceType.DOCUMENT,
        redirect_from_request_id=str(payload.get("redirectedRequestId") or ""),
        redirect_status=response_status,
        headers={str(key): str(value) for key, value in dict(request.get("headers") or {}).items()},
    )
