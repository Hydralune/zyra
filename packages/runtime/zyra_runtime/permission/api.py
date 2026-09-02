from __future__ import annotations

"""Structured HTTP/control facade for the permission control plane.

The repository uses a small stdlib HTTP server rather than a framework.  This
module keeps route parsing, custody envelopes, error mapping, and response
redaction out of ``apps/api/main.py`` while remaining transport-neutral.  The
facade returns plain response objects; the app decides how to persist returned
events in the canonical task event store.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
import hmac
from typing import Any

from zyra_core import EventRecord, new_id, now_iso

from .control_plane import (
    PermissionControlAuthority,
    PermissionControlCapability,
    PermissionControlCode,
    PermissionControlDisabled,
    PermissionControlError,
    PermissionControlForbidden,
    PermissionControlIdentityError,
    PermissionControlNotFound,
    PermissionControlPlane,
    PermissionQuery,
    default_api_capabilities,
    project_permission_decision,
    project_permission_request,
    project_permission_rule,
)
from .custody import (
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyError,
    PermissionSessionCustodyReceipt,
)
from .models import (
    PermissionEffect,
    PermissionEvaluationRequest,
    PermissionMode,
    PermissionRequestPhase,
    PermissionRequestStatus,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
    ToolIdentity,
)
from .request_queue import PermissionResolutionCode
from .store import (
    PermissionIdentityMismatch,
    PermissionRequestExpired,
    PermissionRequestTerminal,
    PermissionStateConflict,
    PermissionStateCorrupt,
    PermissionStateDisabled,
)
from .transports import (
    PermissionTransportError,
    PermissionTransportIdentityError,
    PermissionTransportKind,
    PermissionTransportResponseError,
)


PERMISSION_API_SCHEMA = "zyra.permission-api.v1"


class PermissionApiOperation(StrEnum):
    SESSION_OPEN = "session_open"
    SESSION_RESUME = "session_resume"
    REQUEST_QUERY = "request_query"
    REQUEST_GET = "request_get"
    REQUEST_CREATE = "request_create"
    REQUEST_DELIVER = "request_deliver"
    REQUEST_RESOLVE = "request_resolve"
    REQUEST_CANCEL = "request_cancel"
    REQUEST_ABORT = "request_abort"
    REQUEST_EXPIRE = "request_expire"
    REQUEST_RETRY = "request_retry"
    RULE_QUERY = "rule_query"
    RULE_CREATE = "rule_create"
    RULE_REMOVE = "rule_remove"
    MODE_GET = "mode_get"
    MODE_UPDATE = "mode_update"
    DECISION_QUERY = "decision_query"
    HEALTH = "health"


class PermissionApiError(ValueError):
    code = "invalid_permission_api_request"


class PermissionApiAuthenticationError(PermissionApiError):
    code = "permission_api_authentication_failed"


class PermissionApiAuthorizationError(PermissionApiError):
    code = "permission_api_authorization_failed"


class PermissionApiNotFound(PermissionApiError):
    code = "permission_api_not_found"


@dataclass(frozen=True, slots=True, repr=False)
class PermissionCustodyEnvelope:
    session_id: str
    run_id: str
    task_id: str
    workspace_root: str
    custody_id: str
    custody_fingerprint: str
    custody_epoch: int
    created: bool
    verified: bool
    issued_at: str
    bearer_token: str = field(default="", repr=False)

    @classmethod
    def from_receipt(
        cls,
        receipt: PermissionSessionCustodyReceipt,
        *,
        bearer_token: str | None = None,
    ) -> "PermissionCustodyEnvelope":
        return cls(
            session_id=receipt.binding.session_id,
            run_id=receipt.binding.run_id,
            task_id=receipt.binding.task_id,
            workspace_root=receipt.binding.workspace_root,
            custody_id=receipt.custody_id,
            custody_fingerprint=receipt.custody_fingerprint,
            custody_epoch=receipt.epoch,
            created=receipt.created,
            verified=receipt.verified,
            issued_at=receipt.issued_at,
            bearer_token=receipt.token if bearer_token is None else bearer_token,
        )

    @property
    def includes_bearer_token(self) -> bool:
        return bool(self.bearer_token)

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.permission-custody-envelope.v1",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "workspace_root": self.workspace_root,
            "custody_id": self.custody_id,
            "custody_fingerprint": self.custody_fingerprint,
            "custody_epoch": self.custody_epoch,
            "created": self.created,
            "verified": self.verified,
            "issued_at": self.issued_at,
            "bearer_token_included": False,
            "cacheable": False,
        }

    def private_dict(self) -> dict[str, Any]:
        payload = self.public_dict()
        payload.update(
            {
                "bearer_token": self.bearer_token,
                "bearer_token_included": self.includes_bearer_token,
                "presentation": "one_time_if_created",
                "send_via": "response_body_only",
                "must_not_persist": True,
            }
        )
        return payload

    def __repr__(self) -> str:
        return (
            "PermissionCustodyEnvelope("
            f"session_id={self.session_id!r}, custody_id={self.custody_id!r}, "
            f"created={self.created!r}, verified={self.verified!r}, "
            f"bearer_token_included={self.includes_bearer_token!r})"
        )


@dataclass(frozen=True, slots=True)
class PermissionApiResponse:
    status: HTTPStatus
    operation: PermissionApiOperation
    body: dict[str, Any]
    events: tuple[EventRecord, ...] = ()
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status) < 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": int(self.status),
            "operation": str(self.operation),
            "body": dict(self.body),
            "event_ids": [event.event_id for event in self.events],
            "headers": dict(self.headers),
            "ok": self.ok,
        }


class PermissionApiFacade:
    """Converts structured HTTP requests into control-plane operations."""

    def __init__(
        self,
        control_plane: PermissionControlPlane,
        *,
        workspace_root: str | Path,
        service_token: str = "",
        expose_custody_token_in_body: bool = True,
    ) -> None:
        self.control_plane = control_plane
        self.workspace_root = Path(workspace_root).resolve()
        self.service_token = str(service_token or "")
        self.expose_custody_token_in_body = bool(expose_custody_token_in_body)

    def open_session(
        self,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        presented_token: str = "",
        external_session_exists: bool = False,
        allow_binding_handoff: bool = False,
        expected_handoff_fingerprint: str = "",
    ) -> PermissionApiResponse:
        binding = PermissionSessionCustodyBinding(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            workspace_root=str(self.workspace_root),
        )
        receipt = self.control_plane.custody_store.claim(
            binding,
            presented_token=presented_token,
            external_session_exists=external_session_exists,
            allow_binding_handoff=allow_binding_handoff,
            expected_handoff_fingerprint=expected_handoff_fingerprint,
        )
        envelope = PermissionCustodyEnvelope.from_receipt(receipt)
        body = {
            "schema": PERMISSION_API_SCHEMA,
            "ok": True,
            "operation": PermissionApiOperation.SESSION_OPEN.value,
            "session": (
                envelope.private_dict()
                if self.expose_custody_token_in_body and envelope.created
                else envelope.public_dict()
            ),
        }
        return PermissionApiResponse(
            status=HTTPStatus.CREATED if receipt.created else HTTPStatus.OK,
            operation=PermissionApiOperation.SESSION_OPEN,
            body=body,
            headers=_sensitive_headers(),
        )

    def resume_session(
        self,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        custody_token: str,
        actor_id: str,
    ) -> PermissionApiResponse:
        authority = self.authority(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            custody_token=custody_token,
            actor_id=actor_id,
            channel=PermissionTransportKind.API,
        )
        pending = self.control_plane.query(
            authority,
            PermissionQuery(session_id=session_id, pending_only=True, limit=100),
        )
        body = {
            "schema": PERMISSION_API_SCHEMA,
            "ok": True,
            "operation": PermissionApiOperation.SESSION_RESUME.value,
            "session": {
                "session_id": session_id,
                "run_id": run_id,
                "task_id": task_id,
                "workspace_root": str(self.workspace_root),
                "custody_verified": True,
                "bearer_token_included": False,
            },
            "pending": pending.to_dict(),
            "mode": self.control_plane.session_mode(session_id),
            "resume_constraints": {
                "session_id": session_id,
                "permission_state_owner": "PermissionStateStore",
                "requires_custody_token": True,
                "pending_identity_preserved": True,
                "execution_grants_restored": False,
            },
        }
        return PermissionApiResponse(
            status=HTTPStatus.OK,
            operation=PermissionApiOperation.SESSION_RESUME,
            body=body,
            headers=_sensitive_headers(),
        )

    def authority(
        self,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        custody_token: str,
        actor_id: str,
        channel: PermissionTransportKind | str = PermissionTransportKind.API,
        capabilities: Sequence[PermissionControlCapability | str] | None = None,
        principal_id: str = "",
    ) -> PermissionControlAuthority:
        if not custody_token:
            raise PermissionApiAuthenticationError("permission session custody token is required")
        if not actor_id:
            raise PermissionApiAuthenticationError("permission actor id is required")
        binding = PermissionSessionCustodyBinding(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            workspace_root=str(self.workspace_root),
        )
        return self.control_plane.authority_from_custody(
            binding=binding,
            custody_token=custody_token,
            actor_id=actor_id,
            channel=channel,
            capabilities=capabilities or tuple(default_api_capabilities()),
            principal_id=principal_id,
            metadata={"api_facade": "zyra.permission.api"},
        )

    def query_requests(
        self,
        authority: PermissionControlAuthority,
        parameters: Mapping[str, Any],
    ) -> PermissionApiResponse:
        query = _permission_query(parameters, authority)
        page = self.control_plane.query(authority, query)
        return self._response(
            HTTPStatus.OK,
            PermissionApiOperation.REQUEST_QUERY,
            {"requests": page.to_dict()},
        )

    def get_request(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
    ) -> PermissionApiResponse:
        request = self.control_plane.get_request(authority, request_id)
        return self._response(
            HTTPStatus.OK,
            PermissionApiOperation.REQUEST_GET,
            {"request": project_permission_request(request)},
        )

    def create_request(
        self,
        authority: PermissionControlAuthority,
        payload: Mapping[str, Any],
        *,
        service_token: str,
    ) -> PermissionApiResponse:
        self._require_service_token(service_token)
        if PermissionControlCapability.CREATE not in authority.capabilities:
            authority = PermissionControlAuthority(
                actor_id=authority.actor_id,
                channel=PermissionTransportKind.STRUCTURED_IO,
                session_id=authority.session_id,
                run_id=authority.run_id,
                task_id=authority.task_id,
                workspace_root=authority.workspace_root,
                custody_fingerprint=authority.custody_fingerprint,
                capabilities=frozenset({*authority.capabilities, PermissionControlCapability.CREATE}),
                principal_id=authority.principal_id,
                metadata={**authority.metadata, "service_attested": "true"},
                redaction_secrets=authority.redaction_secrets,
            )
        _reject_api_secret_echo(
            payload,
            (*authority.redaction_secrets, service_token),
        )
        evaluation = _evaluation_from_payload(payload, authority)
        result = self.control_plane.create(
            authority,
            evaluation,
            reason_code=str(payload.get("reason_code") or "external_tool.ask"),
            reason=str(payload.get("reason") or "external tool requested permission"),
            ttl_seconds=_bounded_float(payload.get("ttl_seconds"), default=300.0, minimum=1.0, maximum=3600.0),
            metadata={
                "source": "structured_permission_api",
                "service_attested": True,
            },
        )
        return self._control_response(
            HTTPStatus.CREATED,
            PermissionApiOperation.REQUEST_CREATE,
            result,
        )

    def deliver_request(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        payload: Mapping[str, Any],
        *,
        node_id: str | None = None,
    ) -> PermissionApiResponse:
        _reject_api_secret_echo(payload, authority.redaction_secrets)
        channel_text = str(payload.get("channel") or authority.channel.value)
        allowed = {
            PermissionTransportKind.API.value,
            PermissionTransportKind.CLI.value,
            PermissionTransportKind.INTERACTIVE.value,
            PermissionTransportKind.STRUCTURED_IO.value,
        }
        if channel_text not in allowed:
            raise PermissionApiAuthorizationError("public API cannot impersonate privileged permission channel")
        result = self.control_plane.deliver(
            authority,
            request_id,
            channel=PermissionTransportKind(channel_text),
            transport_id=str(payload.get("transport_id") or ""),
            metadata={"api_delivery": True},
            node_id=node_id,
        )
        return self._control_response(
            HTTPStatus.OK,
            PermissionApiOperation.REQUEST_DELIVER,
            result,
        )

    def resolve_request(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        payload: Mapping[str, Any],
        *,
        node_id: str | None = None,
    ) -> PermissionApiResponse:
        _reject_api_secret_echo(payload, authority.redaction_secrets)
        # channel and actor are server-stamped.  Caller-supplied values are
        # rejected instead of silently trusted.
        if "channel" in payload and str(payload.get("channel")) != authority.channel.value:
            raise PermissionApiAuthorizationError("permission response channel is server-owned")
        if "actor_id" in payload and str(payload.get("actor_id")) != authority.actor_id:
            raise PermissionApiAuthorizationError("permission response actor is server-owned")
        normalized_payload = {
            **_payload_without_auth_fields(payload),
            "channel": authority.channel.value,
            "actor_id": authority.actor_id,
        }
        result = self.control_plane.resolve(
            authority,
            request_id,
            normalized_payload,
            require_identity_echo=payload.get("require_identity_echo") is True,
            node_id=node_id,
        )
        status = HTTPStatus.OK if result.ok else _status_for_control_code(result.code)
        return self._control_response(
            status,
            PermissionApiOperation.REQUEST_RESOLVE,
            result,
        )

    def cancel_request(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        payload: Mapping[str, Any],
        *,
        node_id: str | None = None,
    ) -> PermissionApiResponse:
        _reject_api_secret_echo(payload, authority.redaction_secrets)
        result = self.control_plane.cancel(
            authority,
            request_id,
            reason=str(payload.get("reason") or "cancelled by API operator"),
            node_id=node_id,
        )
        return self._control_response(
            HTTPStatus.OK,
            PermissionApiOperation.REQUEST_CANCEL,
            result,
        )

    def abort_request(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        payload: Mapping[str, Any],
        *,
        node_id: str | None = None,
    ) -> PermissionApiResponse:
        _reject_api_secret_echo(payload, authority.redaction_secrets)
        result = self.control_plane.abort(
            authority,
            request_id,
            reason=str(payload.get("reason") or "aborted by runtime control"),
            node_id=node_id,
        )
        return self._control_response(
            HTTPStatus.OK,
            PermissionApiOperation.REQUEST_ABORT,
            result,
        )

    def expire_requests(
        self,
        authority: PermissionControlAuthority,
        *,
        node_id: str | None = None,
    ) -> PermissionApiResponse:
        results = self.control_plane.expire_due(authority, node_id=node_id)
        events = tuple(event for result in results for event in result.events)
        return self._response(
            HTTPStatus.OK,
            PermissionApiOperation.REQUEST_EXPIRE,
            {
                "expired": [result.to_dict() for result in results],
                "expired_count": len(results),
            },
            events=events,
        )

    def prepare_retry(
        self,
        authority: PermissionControlAuthority,
        request_id: str,
        payload: Mapping[str, Any],
        *,
        node_id: str | None = None,
    ) -> PermissionApiResponse:
        _reject_api_secret_echo(payload, authority.redaction_secrets)
        result = self.control_plane.prepare_retry(
            authority,
            request_id,
            ttl_seconds=_bounded_float(payload.get("ttl_seconds"), default=120.0, minimum=1.0, maximum=600.0),
            metadata={"requested_via": "api"},
            node_id=node_id,
        )
        status = HTTPStatus.OK if result.ok else _status_for_control_code(result.code)
        return self._control_response(
            status,
            PermissionApiOperation.REQUEST_RETRY,
            result,
        )

    def query_rules(self, authority: PermissionControlAuthority) -> PermissionApiResponse:
        rules = self.control_plane.list_rules(authority)
        return self._response(
            HTTPStatus.OK,
            PermissionApiOperation.RULE_QUERY,
            {"rules": [project_permission_rule(rule) for rule in rules]},
        )

    def create_rule(
        self,
        authority: PermissionControlAuthority,
        payload: Mapping[str, Any],
        *,
        node_id: str | None = None,
    ) -> PermissionApiResponse:
        _reject_api_secret_echo(payload, authority.redaction_secrets)
        rule = _rule_from_payload(payload, authority)
        result = self.control_plane.add_rule(authority, rule, node_id=node_id)
        return self._control_response(
            HTTPStatus.CREATED,
            PermissionApiOperation.RULE_CREATE,
            result,
        )

    def remove_rule(
        self,
        authority: PermissionControlAuthority,
        rule_id: str,
        *,
        node_id: str | None = None,
    ) -> PermissionApiResponse:
        result = self.control_plane.remove_rule(authority, rule_id, node_id=node_id)
        return self._control_response(
            HTTPStatus.OK,
            PermissionApiOperation.RULE_REMOVE,
            result,
        )

    def get_mode(self, authority: PermissionControlAuthority) -> PermissionApiResponse:
        authority.require(PermissionControlCapability.MODE_READ)
        return self._response(
            HTTPStatus.OK,
            PermissionApiOperation.MODE_GET,
            {
                "mode": self.control_plane.session_mode(authority.session_id),
                "session_id": authority.session_id,
            },
        )

    def update_mode(
        self,
        authority: PermissionControlAuthority,
        payload: Mapping[str, Any],
        *,
        node_id: str | None = None,
    ) -> PermissionApiResponse:
        _reject_api_secret_echo(payload, authority.redaction_secrets)
        result = self.control_plane.update_mode(
            authority,
            str(payload.get("mode") or ""),
            expected_mode_revision=(
                None
                if payload.get("expected_mode_revision") is None
                else int(payload.get("expected_mode_revision"))
            ),
            node_id=node_id,
        )
        return self._control_response(
            HTTPStatus.OK,
            PermissionApiOperation.MODE_UPDATE,
            result,
        )

    def query_decisions(
        self,
        authority: PermissionControlAuthority,
        *,
        limit: int = 100,
    ) -> PermissionApiResponse:
        decisions = self.control_plane.decisions(authority, limit=limit)
        return self._response(
            HTTPStatus.OK,
            PermissionApiOperation.DECISION_QUERY,
            {"decisions": list(decisions)},
        )

    def health(self) -> PermissionApiResponse:
        return self._response(
            HTTPStatus.OK,
            PermissionApiOperation.HEALTH,
            {
                "runtime": "zyra-permission-control-plane",
                "owner_unit": "M1-S03A-02",
                "metrics": self.control_plane.metrics(),
                "state_owner": "PermissionStateStore",
                "legacy_store_is_authority": False,
                "external_process_required": False,
            },
        )

    def _require_service_token(self, presented: str) -> None:
        if not self.service_token:
            raise PermissionApiAuthorizationError("permission service request creation is disabled")
        if not presented or not hmac.compare_digest(self.service_token, presented):
            raise PermissionApiAuthenticationError("permission service token is invalid")

    def _control_response(
        self,
        status: HTTPStatus,
        operation: PermissionApiOperation,
        result: Any,
    ) -> PermissionApiResponse:
        return self._response(
            status,
            operation,
            {"result": result.to_dict()},
            events=result.events,
        )

    def _response(
        self,
        status: HTTPStatus,
        operation: PermissionApiOperation,
        body: Mapping[str, Any],
        *,
        events: Sequence[EventRecord] = (),
    ) -> PermissionApiResponse:
        return PermissionApiResponse(
            status=status,
            operation=operation,
            body={
                "schema": PERMISSION_API_SCHEMA,
                "ok": 200 <= int(status) < 300,
                "operation": operation.value,
                **dict(body),
            },
            events=tuple(events),
            headers=_sensitive_headers(),
        )


def permission_api_error_response(
    operation: PermissionApiOperation,
    error: Exception,
) -> PermissionApiResponse:
    status, code = _map_error(error)
    return PermissionApiResponse(
        status=status,
        operation=operation,
        body={
            "schema": PERMISSION_API_SCHEMA,
            "ok": False,
            "operation": operation.value,
            "error": code,
            "message": _safe_error_message(error),
        },
        headers=_sensitive_headers(),
    )


def extract_bearer_token(headers: Mapping[str, Any], payload: Mapping[str, Any] | None = None) -> str:
    authorization = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    if authorization:
        scheme, separator, value = authorization.partition(" ")
        if separator and scheme.casefold() == "bearer" and value.strip():
            return value.strip()
        raise PermissionApiAuthenticationError("Authorization must use Bearer scheme")
    item = payload or {}
    token = str(item.get("session_custody_token") or item.get("custody_token") or "").strip()
    return token


_AUTH_PAYLOAD_FIELDS = frozenset(
    {
        "authorization",
        "custody_token",
        "permission_session_custody_token",
        "service_token",
        "session_custody_token",
    }
)
_SENSITIVE_PAYLOAD_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "signature",
    "token",
)


def _payload_without_auth_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in payload.items()
        if str(key).casefold().replace("-", "_") not in _AUTH_PAYLOAD_FIELDS
    }


def _reject_api_secret_echo(payload: Mapping[str, Any], secrets: Iterable[str]) -> None:
    """Reject credentials copied into durable permission control fields."""

    protected = tuple(dict.fromkeys(str(item) for item in secrets if str(item)))

    def inspect(item: Any, *, depth: int = 0) -> None:
        if depth > 12:
            raise PermissionApiAuthorizationError(
                "permission request nesting exceeds safety limit"
            )
        if isinstance(item, Mapping):
            for raw_key, child in item.items():
                raw_key_text = str(raw_key)
                if any(secret in raw_key_text for secret in protected):
                    raise PermissionApiAuthorizationError(
                        "permission control data cannot use an authenticated capability as a field"
                    )
                key = raw_key_text.casefold().replace("-", "_")
                if depth == 0 and key in _AUTH_PAYLOAD_FIELDS:
                    continue
                if any(marker in key for marker in _SENSITIVE_PAYLOAD_MARKERS):
                    raise PermissionApiAuthorizationError(
                        "permission control data cannot contain credential-bearing fields"
                    )
                inspect(child, depth=depth + 1)
            return
        if isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                inspect(child, depth=depth + 1)
            return
        if isinstance(item, str) and any(secret in item for secret in protected):
            raise PermissionApiAuthorizationError(
                "permission control data cannot echo an authenticated capability"
            )

    inspect(payload)


def _permission_query(
    parameters: Mapping[str, Any],
    authority: PermissionControlAuthority,
) -> PermissionQuery:
    status = parameters.get("status")
    phases_value = parameters.get("phase", parameters.get("phases", ()))
    if isinstance(phases_value, str):
        phase_items = tuple(item.strip() for item in phases_value.split(",") if item.strip())
    elif isinstance(phases_value, Sequence):
        phase_items = tuple(str(item) for item in phases_value)
    else:
        phase_items = ()
    return PermissionQuery(
        session_id=str(parameters.get("session_id") or authority.session_id),
        task_id=str(parameters.get("task_id") or authority.task_id),
        run_id=str(parameters.get("run_id") or authority.run_id),
        request_id=str(parameters.get("request_id") or ""),
        status=PermissionRequestStatus(str(status)) if status else None,
        phases=tuple(PermissionRequestPhase(item) for item in phase_items),
        tool_name=str(parameters.get("tool_name") or ""),
        namespace=str(parameters.get("namespace") or ""),
        server_id=str(parameters.get("server_id") or ""),
        pending_only=_bool(parameters.get("pending_only"), default=False),
        include_terminal=_bool(parameters.get("include_terminal"), default=True),
        offset=_bounded_int(parameters.get("offset"), default=0, minimum=0, maximum=1_000_000),
        limit=_bounded_int(parameters.get("limit"), default=100, minimum=1, maximum=1000),
    )


def _evaluation_from_payload(
    payload: Mapping[str, Any],
    authority: PermissionControlAuthority,
) -> PermissionEvaluationRequest:
    tool_value = payload.get("tool_identity")
    if not isinstance(tool_value, Mapping):
        tool_value = {
            "namespace": payload.get("namespace") or "builtin",
            "name": payload.get("tool_name"),
            "server_id": payload.get("server_id") or payload.get("server_name") or "",
            "version": payload.get("tool_version") or "",
            "schema_digest": payload.get("tool_schema_digest") or "",
        }
    arguments = payload.get("arguments")
    if not isinstance(arguments, Mapping):
        raise PermissionApiError("permission request creation requires arguments object")
    return PermissionEvaluationRequest(
        run_id=authority.run_id,
        task_id=authority.task_id,
        session_id=authority.session_id,
        worker_request_id=str(payload.get("worker_request_id") or new_id("permission-api-worker")),
        turn_id=str(payload.get("turn_id") or ""),
        node_id=None if payload.get("node_id") is None else str(payload.get("node_id")),
        tool_use_id=str(payload.get("tool_use_id") or payload.get("tool_call_id") or ""),
        tool_identity=ToolIdentity.from_dict(tool_value),
        arguments=dict(arguments),
        operation=str(payload.get("operation") or "execute"),
        mode=PermissionMode(str(payload.get("mode") or "default")),
        workspace_root=authority.workspace_root,
        principal_id=authority.principal_id,
        interactive=True,
        headless=False,
        requires_interaction=True,
        safety_flags=tuple(str(item) for item in payload.get("safety_flags") or ()),
        risk_tags=tuple(str(item) for item in payload.get("risk_tags") or ()),
        attributes=dict(payload.get("attributes") or {}) if isinstance(payload.get("attributes"), Mapping) else {},
        metadata={"source": "permission_api", "service_attested": True},
    )


def _rule_from_payload(
    payload: Mapping[str, Any],
    authority: PermissionControlAuthority,
) -> PermissionRuleRecord:
    effect = PermissionEffect(str(payload.get("effect") or "ask"))
    scope_value = payload.get("scope")
    if isinstance(scope_value, Mapping):
        scope = PermissionScope.from_dict(scope_value)
    else:
        kind = PermissionScopeKind(str(payload.get("scope_kind") or "action"))
        selectors = {
            "session_id": authority.session_id,
            "task_id": authority.task_id,
            "run_id": authority.run_id,
            "workspace_root": authority.workspace_root,
            "principal_id": authority.principal_id,
            "tool_namespace": str(payload.get("namespace_pattern") or payload.get("tool_namespace") or ""),
            "tool_name": str(payload.get("tool_pattern") or payload.get("tool_name") or ""),
            "server_id": str(payload.get("server_pattern") or payload.get("server_id") or ""),
            "argument_digest": str(payload.get("arguments_digest") or ""),
            "request_fingerprint": str(payload.get("request_fingerprint") or ""),
            "path_prefixes": tuple(str(item) for item in payload.get("path_prefixes") or ()),
            "domains": tuple(str(item) for item in payload.get("domains") or ()),
            "expires_at": None if payload.get("scope_expires_at") is None else str(payload.get("scope_expires_at")),
            "metadata": {"created_via": "permission_api"},
        }
        scope = PermissionScope(kind=kind, **selectors)
    return PermissionRuleRecord(
        effect=effect,
        source=PermissionRuleSource.USER,
        scope=scope,
        tool_pattern=str(payload.get("tool_pattern") or "*"),
        namespace_pattern=str(payload.get("namespace_pattern") or "*"),
        server_pattern=str(payload.get("server_pattern") or "*"),
        operation_pattern=str(payload.get("operation_pattern") or "*"),
        argument_pattern=str(payload.get("argument_pattern") or ""),
        reason=str(payload.get("reason") or "created through permission API"),
        priority=_bounded_int(payload.get("priority"), default=0, minimum=-10_000, maximum=10_000),
        expires_at=None if payload.get("expires_at") is None else str(payload.get("expires_at")),
        enabled=_bool(payload.get("enabled"), default=True),
        max_uses=None if payload.get("max_uses") is None else _bounded_int(payload.get("max_uses"), default=1, minimum=1, maximum=1_000_000),
        metadata={"created_via": "permission_api", "actor_id": authority.actor_id},
    )


def _map_error(error: Exception) -> tuple[HTTPStatus, str]:
    if isinstance(error, (PermissionApiAuthenticationError, PermissionSessionCustodyError)):
        return HTTPStatus.UNAUTHORIZED, getattr(error, "code", "permission_authentication_failed")
    if isinstance(
        error,
        (
            PermissionApiAuthorizationError,
            PermissionControlForbidden,
        ),
    ):
        return HTTPStatus.FORBIDDEN, getattr(error, "code", "permission_forbidden")
    if isinstance(error, (PermissionApiNotFound, PermissionControlNotFound, KeyError)):
        return HTTPStatus.NOT_FOUND, getattr(error, "code", "permission_not_found")
    if isinstance(error, (PermissionRequestExpired,)):
        return HTTPStatus.GONE, "permission_request_expired"
    if isinstance(
        error,
        (
            PermissionRequestTerminal,
            PermissionStateConflict,
        ),
    ):
        return HTTPStatus.CONFLICT, getattr(error, "code", "permission_conflict")
    if isinstance(
        error,
        (
            PermissionControlIdentityError,
            PermissionIdentityMismatch,
            PermissionTransportIdentityError,
        ),
    ):
        return HTTPStatus.CONFLICT, getattr(error, "code", "permission_identity_mismatch")
    if isinstance(
        error,
        (
            PermissionControlDisabled,
            PermissionStateDisabled,
        ),
    ):
        return HTTPStatus.SERVICE_UNAVAILABLE, getattr(error, "code", "permission_runtime_disabled")
    if isinstance(error, PermissionTransportResponseError):
        return HTTPStatus.BAD_REQUEST, getattr(error, "code", "invalid_permission_response")
    if isinstance(error, (PermissionTransportError,)):
        return HTTPStatus.SERVICE_UNAVAILABLE, getattr(error, "code", "permission_transport_failed")
    if isinstance(error, (PermissionStateCorrupt,)):
        return HTTPStatus.INTERNAL_SERVER_ERROR, "permission_state_corrupt"
    if isinstance(error, PermissionControlError):
        return _status_for_control_code(error.code), str(error.code)
    return HTTPStatus.BAD_REQUEST, getattr(error, "code", "invalid_permission_request")


def _status_for_control_code(code: PermissionControlCode) -> HTTPStatus:
    return {
        PermissionControlCode.OK: HTTPStatus.OK,
        PermissionControlCode.CREATED: HTTPStatus.CREATED,
        PermissionControlCode.DELIVERED: HTTPStatus.OK,
        PermissionControlCode.RESOLVED: HTTPStatus.OK,
        PermissionControlCode.DENIED: HTTPStatus.CONFLICT,
        PermissionControlCode.EXPIRED: HTTPStatus.GONE,
        PermissionControlCode.CANCELLED: HTTPStatus.CONFLICT,
        PermissionControlCode.ABORTED: HTTPStatus.CONFLICT,
        PermissionControlCode.RETRY_READY: HTTPStatus.OK,
        PermissionControlCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
        PermissionControlCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
        PermissionControlCode.CONFLICT: HTTPStatus.CONFLICT,
        PermissionControlCode.INVALID: HTTPStatus.BAD_REQUEST,
        PermissionControlCode.IDENTITY_MISMATCH: HTTPStatus.CONFLICT,
        PermissionControlCode.DISABLED: HTTPStatus.SERVICE_UNAVAILABLE,
        PermissionControlCode.TRANSPORT_FAILED: HTTPStatus.SERVICE_UNAVAILABLE,
    }[code]


def _safe_error_message(error: Exception) -> str:
    message = str(error)
    lowered = message.casefold()
    if any(marker in lowered for marker in ("token=", "authorization", "cookie", "password", "secret")):
        return "Permission operation failed."
    return message[:1000]


def _sensitive_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Zyra-Permission-State-Owner": "PermissionStateStore",
    }


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() not in {"0", "false", "no", "off", ""}


__all__ = [
    "PERMISSION_API_SCHEMA",
    "PermissionApiAuthenticationError",
    "PermissionApiAuthorizationError",
    "PermissionApiError",
    "PermissionApiFacade",
    "PermissionApiNotFound",
    "PermissionApiOperation",
    "PermissionApiResponse",
    "PermissionCustodyEnvelope",
    "extract_bearer_token",
    "permission_api_error_response",
]
