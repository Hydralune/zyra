from __future__ import annotations

"""Zyra-owned MCP authentication state machine.

This module deliberately owns protocol state instead of delegating it to an
SDK.  Network I/O is expressed through :class:`AuthNetworkProvider`, making
browser, HTTP, keychain, cloud, and test integrations replaceable.  Secrets
remain in ``credentials.py``; state, events, and API snapshots contain only
opaque references and digests.
"""

import hashlib
import json
import os
import secrets as random_secrets
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Generic, Protocol, TypeAlias, TypeVar, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from .credentials import (
    CredentialEnvelope,
    CredentialError,
    CredentialKind,
    CredentialMetadata,
    CredentialNotFound,
    CredentialReference,
    CredentialVault,
    FileCredentialVault,
    MemoryCredentialVault,
    PublicValue,
    SecretValue,
    VaultCredentialProvider,
    is_sensitive_name,
    redact_public_value,
)


EventPayload: TypeAlias = dict[str, PublicValue]
T = TypeVar("T")


class AuthError(RuntimeError):
    """Base authentication failure with a non-secret machine code."""

    code = "auth_error"

    def __init__(self, message: str = "MCP authentication failed", *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = _safe_code(code)


class AuthConfigurationError(AuthError):
    code = "configuration_error"


class AuthNeedsInteraction(AuthError):
    code = "needs_interaction"


class AuthCancelled(AuthError):
    code = "cancelled"


class AuthStepUpRequired(AuthError):
    code = "insufficient_scope"


class AuthNetworkError(AuthError):
    """Provider error carrying structured classification data only.

    ``response_body`` is accepted for classification but never exposed by
    ``str``/``repr`` or snapshots because OAuth servers occasionally echo
    bearer tokens in error bodies.
    """

    def __init__(
        self,
        *,
        code: str = "network_error",
        status: int | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
        response_body: str = "",
    ) -> None:
        super().__init__("MCP authentication provider request failed", code=code)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self._response_body = response_body

    def __repr__(self) -> str:
        return (
            f"AuthNetworkError(code={self.code!r}, status={self.status!r}, "
            f"retryable={self.retryable!r})"
        )


class AuthMode(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    OAUTH = "oauth"
    XAA = "xaa"


class AuthStatus(StrEnum):
    UNKNOWN = "unknown"
    NOT_REQUIRED = "not_required"
    NEEDS_AUTH = "needs_auth"
    AUTHENTICATING = "authenticating"
    AUTHENTICATED = "authenticated"
    REFRESHING = "refreshing"
    STEP_UP_REQUIRED = "step_up_required"
    REVOKED = "revoked"
    FAILED = "failed"


class RefreshErrorKind(StrEnum):
    INVALID_GRANT = "invalid_grant"
    INVALID_CLIENT = "invalid_client"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    TRANSIENT = "transient"
    PROTOCOL = "protocol"
    CONFIGURATION = "configuration"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ProbeDecision(StrEnum):
    AUTHENTICATED = "authenticated"
    NEEDS_AUTH = "needs_auth"
    NOT_REQUIRED = "not_required"
    FAILED = "failed"


def _safe_code(value: Any, default: str = "unknown") -> str:
    text = str(value or default).strip().casefold().replace("-", "_")
    selected = "".join(character for character in text if character.isalnum() or character in "_./")
    return selected[:128] or default


def _timestamp(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_scope(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(value).strip() for value in values if str(value).strip()}))


def _normalize_url(
    value: str,
    *,
    field_name: str,
    require_https: bool = True,
    allow_localhost_http: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthConfigurationError(f"{field_name} is required")
    try:
        parts = urlsplit(value.strip())
    except ValueError as exc:
        raise AuthConfigurationError(f"{field_name} is not a valid URL") from exc
    host = (parts.hostname or "").casefold()
    if not parts.scheme or not host:
        raise AuthConfigurationError(f"{field_name} requires a scheme and host")
    if parts.username or parts.password:
        raise AuthConfigurationError(f"{field_name} must not include user information")
    local = host in {"localhost", "127.0.0.1", "::1"}
    if require_https and parts.scheme.casefold() != "https" and not (allow_localhost_http and local):
        raise AuthConfigurationError(f"{field_name} must use HTTPS")
    if parts.fragment:
        raise AuthConfigurationError(f"{field_name} must not include a fragment")
    netloc = host
    if parts.port is not None:
        netloc = f"[{host}]:{parts.port}" if ":" in host else f"{host}:{parts.port}"
    path = parts.path or ""
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.casefold(), netloc, path, parts.query, ""))


def _urls_match(left: str, right: str) -> bool:
    try:
        return _normalize_url(left, field_name="left URL") == _normalize_url(right, field_name="right URL")
    except AuthConfigurationError:
        return False


def _canonical_config_value(value: Any, *, key: str = "") -> Any:
    """Canonicalize config while preserving secret changes as digests."""

    if isinstance(value, SecretValue):
        return {"secret_digest": value.fingerprint()}
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key in sorted(value, key=lambda item: str(item)):
            name = str(raw_key)
            item = value[raw_key]
            if is_sensitive_name(name):
                if isinstance(item, SecretValue):
                    digest = item.fingerprint()
                elif isinstance(item, (bytes, bytearray, memoryview)):
                    digest = "sha256:" + hashlib.sha256(bytes(item)).hexdigest()
                else:
                    digest = "sha256:" + hashlib.sha256(str(item).encode("utf-8")).hexdigest()
                output[name] = {"secret_digest": digest}
            else:
                output[name] = _canonical_config_value(item, key=name)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_canonical_config_value(item, key=key) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"binary_digest": "sha256:" + hashlib.sha256(bytes(value)).hexdigest()}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise AuthConfigurationError("MCP config contains a non-finite number")
        return value
    if isinstance(value, StrEnum):
        return str(value)
    return str(value)


def compute_config_fingerprint(config: Mapping[str, Any]) -> str:
    """Fingerprint transport, auth, policy, header, and environment config.

    Header/env secret *values* are hashed rather than omitted.  Updating a
    credential under the same server name therefore invalidates needs-auth and
    provider caches without putting the secret into ordinary state.
    """

    canonical = _canonical_config_value(config)
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "mcp-config:sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OAuthMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str = ""
    registration_endpoint: str = ""
    protected_resource: str = ""
    scopes_supported: tuple[str, ...] = ()
    token_endpoint_auth_methods: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _normalize_url(self.issuer, field_name="OAuth issuer"))
        object.__setattr__(
            self,
            "authorization_endpoint",
            _normalize_url(self.authorization_endpoint, field_name="OAuth authorization endpoint"),
        )
        object.__setattr__(self, "token_endpoint", _normalize_url(self.token_endpoint, field_name="OAuth token endpoint"))
        if self.revocation_endpoint:
            object.__setattr__(
                self,
                "revocation_endpoint",
                _normalize_url(self.revocation_endpoint, field_name="OAuth revocation endpoint"),
            )
        if self.registration_endpoint:
            object.__setattr__(
                self,
                "registration_endpoint",
                _normalize_url(self.registration_endpoint, field_name="OAuth registration endpoint"),
            )
        if self.protected_resource:
            object.__setattr__(
                self,
                "protected_resource",
                _normalize_url(self.protected_resource, field_name="OAuth protected resource"),
            )
        object.__setattr__(self, "scopes_supported", _normalize_scope(self.scopes_supported))
        object.__setattr__(self, "token_endpoint_auth_methods", _normalize_scope(self.token_endpoint_auth_methods))

    @property
    def digest(self) -> str:
        raw = json.dumps(self.safe_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def safe_dict(self) -> EventPayload:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "registration_endpoint": self.registration_endpoint,
            "protected_resource": self.protected_resource,
            "scopes_supported": list(self.scopes_supported),
            "token_endpoint_auth_methods": list(self.token_endpoint_auth_methods),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OAuthMetadata":
        return cls(
            issuer=str(value.get("issuer") or ""),
            authorization_endpoint=str(value.get("authorization_endpoint") or value.get("authorizationEndpoint") or ""),
            token_endpoint=str(value.get("token_endpoint") or value.get("tokenEndpoint") or ""),
            revocation_endpoint=str(value.get("revocation_endpoint") or value.get("revocationEndpoint") or ""),
            registration_endpoint=str(value.get("registration_endpoint") or value.get("registrationEndpoint") or ""),
            protected_resource=str(value.get("protected_resource") or value.get("resource") or ""),
            scopes_supported=tuple(value.get("scopes_supported") or value.get("scopesSupported") or ()),
            token_endpoint_auth_methods=tuple(
                value.get("token_endpoint_auth_methods_supported")
                or value.get("tokenEndpointAuthMethodsSupported")
                or ()
            ),
        )


@dataclass(frozen=True, slots=True, repr=False)
class OAuthTokenResponse:
    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    token_type: str = "Bearer"
    expires_in: float | None = None
    expires_at: float | None = None
    scopes: tuple[str, ...] = ()
    resource: str = ""
    issuer: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.access_token, str) or not self.access_token:
            raise AuthError("provider returned no access token", code="invalid_token_response")
        if len(self.access_token) > 4 * 1024 * 1024:
            raise AuthError("provider returned an oversized access token", code="invalid_token_response")
        for name in ("refresh_token", "id_token"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) > 8 * 1024 * 1024:
                raise AuthError(f"provider returned invalid {name}", code="invalid_token_response")
        if self.expires_in is not None and self.expires_in < 0:
            raise AuthError("provider returned negative expires_in", code="invalid_token_response")
        if self.expires_at is not None and self.expires_at < 0:
            raise AuthError("provider returned negative expires_at", code="invalid_token_response")
        object.__setattr__(self, "scopes", _normalize_scope(self.scopes))
        if self.resource:
            object.__setattr__(self, "resource", _normalize_url(self.resource, field_name="token resource"))
        if self.issuer:
            object.__setattr__(self, "issuer", _normalize_url(self.issuer, field_name="token issuer"))

    def __repr__(self) -> str:
        return (
            "OAuthTokenResponse(access_token=<redacted>, refresh_token="
            f"{'<redacted>' if self.refresh_token else '<absent>'}, id_token="
            f"{'<redacted>' if self.id_token else '<absent>'}, token_type={self.token_type!r}, "
            f"expires_in={self.expires_in!r}, expires_at={self.expires_at!r}, scopes={self.scopes!r})"
        )

    def absolute_expiry(self, clock: Callable[[], float]) -> float | None:
        if self.expires_at is not None:
            return float(self.expires_at)
        if self.expires_in is not None:
            return clock() + float(self.expires_in)
        return None

    def safe_dict(self) -> EventPayload:
        return {
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "resource": self.resource,
            "issuer": self.issuer,
            "has_access_token": True,
            "has_refresh_token": bool(self.refresh_token),
            "has_id_token": bool(self.id_token),
            "raw_token_included": False,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OAuthTokenResponse":
        scopes_value = value.get("scopes") or value.get("scope") or ()
        scopes = tuple(scopes_value.split()) if isinstance(scopes_value, str) else tuple(scopes_value)
        return cls(
            access_token=str(value.get("access_token") or value.get("accessToken") or ""),
            refresh_token=str(value.get("refresh_token") or value.get("refreshToken") or ""),
            id_token=str(value.get("id_token") or value.get("idToken") or ""),
            token_type=str(value.get("token_type") or value.get("tokenType") or "Bearer"),
            expires_in=float(value["expires_in"]) if value.get("expires_in") is not None else None,
            expires_at=float(value["expires_at"]) if value.get("expires_at") is not None else None,
            scopes=scopes,
            resource=str(value.get("resource") or ""),
            issuer=str(value.get("issuer") or ""),
        )


@dataclass(frozen=True, slots=True)
class ProbeResult:
    decision: ProbeDecision
    tokens: OAuthTokenResponse | None = None
    metadata: OAuthMetadata | None = None
    reason_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ProbeDecision):
            object.__setattr__(self, "decision", ProbeDecision(str(self.decision)))
        object.__setattr__(self, "reason_code", _safe_code(self.reason_code, "probe_result"))
        if self.decision is ProbeDecision.AUTHENTICATED and self.tokens is None:
            raise AuthError("authenticated probe result requires tokens", code="invalid_probe_result")

    @classmethod
    def from_value(cls, value: "ProbeResult | Mapping[str, Any]") -> "ProbeResult":
        if isinstance(value, cls):
            return value
        decision = ProbeDecision(str(value.get("decision") or ProbeDecision.FAILED))
        raw_tokens = value.get("tokens")
        raw_metadata = value.get("metadata")
        return cls(
            decision=decision,
            tokens=raw_tokens if isinstance(raw_tokens, OAuthTokenResponse) else (
                OAuthTokenResponse.from_mapping(raw_tokens) if isinstance(raw_tokens, Mapping) else None
            ),
            metadata=raw_metadata if isinstance(raw_metadata, OAuthMetadata) else (
                OAuthMetadata.from_mapping(raw_metadata) if isinstance(raw_metadata, Mapping) else None
            ),
            reason_code=str(value.get("reason_code") or value.get("reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class StepUpRequirement:
    scopes: tuple[str, ...]
    resource: str = ""
    reason_code: str = "insufficient_scope"
    requested_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "scopes", _normalize_scope(self.scopes))
        if not self.scopes and not self.resource:
            raise AuthConfigurationError("step-up requires scopes or resource")
        if self.resource:
            object.__setattr__(self, "resource", _normalize_url(self.resource, field_name="step-up resource"))
        object.__setattr__(self, "reason_code", _safe_code(self.reason_code, "insufficient_scope"))

    def safe_dict(self) -> EventPayload:
        return {
            "scopes": list(self.scopes),
            "resource": self.resource,
            "reason_code": self.reason_code,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True, slots=True)
class XaaConfiguration:
    issuer: str
    resource: str
    client_id: str
    audience: str = ""
    token_endpoint: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _normalize_url(self.issuer, field_name="XAA issuer"))
        object.__setattr__(self, "resource", _normalize_url(self.resource, field_name="XAA resource"))
        if self.token_endpoint:
            object.__setattr__(
                self,
                "token_endpoint",
                _normalize_url(self.token_endpoint, field_name="XAA token endpoint"),
            )
        if not isinstance(self.client_id, str) or not self.client_id.strip() or len(self.client_id) > 2048:
            raise AuthConfigurationError("XAA client_id is required")
        if len(self.audience) > 4096:
            raise AuthConfigurationError("XAA audience is too long")

    def validate_discovery(self, *, issuer: str, resource: str, token_endpoint: str = "") -> None:
        selected_issuer = _normalize_url(issuer, field_name="discovered XAA issuer")
        selected_resource = _normalize_url(resource, field_name="discovered XAA resource")
        if selected_issuer != self.issuer:
            raise AuthConfigurationError("XAA issuer mismatch")
        if selected_resource != self.resource:
            raise AuthConfigurationError("XAA resource mismatch")
        if token_endpoint:
            selected_endpoint = _normalize_url(token_endpoint, field_name="discovered XAA token endpoint")
            if self.token_endpoint and selected_endpoint != self.token_endpoint:
                raise AuthConfigurationError("XAA token endpoint mismatch")

    def safe_dict(self) -> EventPayload:
        return {
            "issuer": self.issuer,
            "resource": self.resource,
            "client_id": self.client_id,
            "audience": self.audience,
            "token_endpoint": self.token_endpoint,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class AuthRuntimeConfig:
    server_id: str
    config_fingerprint: str
    mode: AuthMode = AuthMode.OAUTH
    requested_scopes: tuple[str, ...] = ()
    resource: str = ""
    needs_auth_ttl_seconds: float = 900.0
    refresh_skew_seconds: float = 300.0
    xaa: XaaConfiguration | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, str) or not self.server_id.strip() or len(self.server_id) > 512:
            raise AuthConfigurationError("server_id is required")
        if not isinstance(self.config_fingerprint, str) or not self.config_fingerprint.startswith("mcp-config:sha256:"):
            raise AuthConfigurationError("config_fingerprint is invalid")
        if not isinstance(self.mode, AuthMode):
            object.__setattr__(self, "mode", AuthMode(str(self.mode)))
        object.__setattr__(self, "requested_scopes", _normalize_scope(self.requested_scopes))
        if self.resource:
            object.__setattr__(self, "resource", _normalize_url(self.resource, field_name="MCP auth resource"))
        if self.needs_auth_ttl_seconds < 0 or self.refresh_skew_seconds < 0:
            raise AuthConfigurationError("auth TTL and refresh skew must be non-negative")
        if self.mode is AuthMode.XAA and self.xaa is None:
            raise AuthConfigurationError("XAA mode requires XAA configuration")

    @classmethod
    def from_server_config(cls, server_id: str, config: Mapping[str, Any]) -> "AuthRuntimeConfig":
        auth = config.get("auth") if isinstance(config.get("auth"), Mapping) else {}
        raw_mode = auth.get("type") or config.get("auth_type") or config.get("auth") or "oauth"
        if isinstance(raw_mode, Mapping):
            raw_mode = raw_mode.get("type") or "oauth"
        raw_xaa = auth.get("xaa") if isinstance(auth, Mapping) else None
        if raw_xaa is None and isinstance(config.get("xaa"), Mapping):
            raw_xaa = config.get("xaa")
        xaa = None
        if isinstance(raw_xaa, Mapping):
            xaa = XaaConfiguration(
                issuer=str(raw_xaa.get("issuer") or ""),
                resource=str(raw_xaa.get("resource") or config.get("url") or ""),
                client_id=str(raw_xaa.get("client_id") or raw_xaa.get("clientId") or ""),
                audience=str(raw_xaa.get("audience") or ""),
                token_endpoint=str(raw_xaa.get("token_endpoint") or ""),
                enabled=bool(raw_xaa.get("enabled", True)),
            )
        scopes = auth.get("scopes") or auth.get("scope") or () if isinstance(auth, Mapping) else ()
        if isinstance(scopes, str):
            scopes = scopes.split()
        return cls(
            server_id=server_id,
            config_fingerprint=compute_config_fingerprint(config),
            mode=AuthMode(str(raw_mode).casefold()),
            requested_scopes=tuple(scopes),
            resource=str(auth.get("resource") or config.get("url") or "") if isinstance(auth, Mapping) else "",
            needs_auth_ttl_seconds=float(auth.get("needs_auth_ttl_seconds", 900.0)) if isinstance(auth, Mapping) else 900.0,
            refresh_skew_seconds=float(auth.get("refresh_skew_seconds", 300.0)) if isinstance(auth, Mapping) else 300.0,
            xaa=xaa,
        )


@dataclass(frozen=True, slots=True)
class AuthSnapshot:
    server_id: str
    config_fingerprint: str
    mode: AuthMode
    status: AuthStatus = AuthStatus.UNKNOWN
    revision: int = 0
    credential_reference: str = ""
    access_token_digest: str = ""
    refresh_token_digest: str = ""
    expires_at: str = ""
    scopes: tuple[str, ...] = ()
    resource: str = ""
    issuer: str = ""
    needs_auth_cached_until: str = ""
    step_up: StepUpRequirement | None = None
    error_code: str = ""
    updated_at: str = ""
    external_version: str = ""

    def safe_dict(self) -> EventPayload:
        return {
            "server_id": self.server_id,
            "config_fingerprint": self.config_fingerprint,
            "mode": str(self.mode),
            "status": str(self.status),
            "revision": self.revision,
            "credential_reference": self.credential_reference,
            "access_token_digest": self.access_token_digest,
            "refresh_token_digest": self.refresh_token_digest,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "resource": self.resource,
            "issuer": self.issuer,
            "needs_auth_cached_until": self.needs_auth_cached_until,
            "step_up": self.step_up.safe_dict() if self.step_up else None,
            "error_code": self.error_code,
            "updated_at": self.updated_at,
            "external_version": self.external_version,
            "raw_token_included": False,
        }

    def to_mcp_auth_record(self) -> Any:
        """Project into ``models.McpAuthRecord`` without making it mandatory."""

        from .models import McpAuthRecord, McpAuthState

        state_map = {
            AuthStatus.UNKNOWN: McpAuthState.UNKNOWN,
            AuthStatus.NOT_REQUIRED: McpAuthState.NOT_REQUIRED,
            AuthStatus.NEEDS_AUTH: McpAuthState.NEEDS_AUTH,
            AuthStatus.AUTHENTICATING: McpAuthState.AUTHENTICATING,
            AuthStatus.AUTHENTICATED: McpAuthState.AUTHENTICATED,
            AuthStatus.REFRESHING: McpAuthState.REFRESHING,
            AuthStatus.STEP_UP_REQUIRED: McpAuthState.NEEDS_AUTH,
            AuthStatus.REVOKED: McpAuthState.REVOKED,
            AuthStatus.FAILED: McpAuthState.FAILED,
        }
        return McpAuthRecord(
            server_id=self.server_id,
            state=state_map[self.status],
            revision=self.revision,
            credential_reference=self.credential_reference,
            access_token_digest=self.access_token_digest,
            refresh_token_digest=self.refresh_token_digest,
            expires_at=self.expires_at,
            scopes=self.scopes,
            error_code=self.error_code,
            metadata={
                "mode": str(self.mode),
                "resource": self.resource,
                "issuer": self.issuer,
                "step_up_required": str(self.step_up is not None).lower(),
            },
        )


@dataclass(frozen=True, slots=True)
class AuthOutcome:
    status: AuthStatus
    snapshot: AuthSnapshot
    network_called: bool = False
    cache_hit: bool = False
    credential_changed: bool = False
    error_kind: RefreshErrorKind | None = None

    @property
    def authorized(self) -> bool:
        return self.status in {AuthStatus.AUTHENTICATED, AuthStatus.NOT_REQUIRED}

    def safe_dict(self) -> EventPayload:
        return {
            "status": str(self.status),
            "snapshot": self.snapshot.safe_dict(),
            "network_called": self.network_called,
            "cache_hit": self.cache_hit,
            "credential_changed": self.credential_changed,
            "error_kind": str(self.error_kind) if self.error_kind else "",
        }


@dataclass(frozen=True, slots=True)
class RevokeOutcome:
    local_deleted: bool
    attempted_token_types: tuple[str, ...]
    remote_failures: tuple[str, ...]
    snapshot: AuthSnapshot

    @property
    def fully_revoked(self) -> bool:
        return self.local_deleted and not self.remote_failures

    def safe_dict(self) -> EventPayload:
        return {
            "local_deleted": self.local_deleted,
            "attempted_token_types": list(self.attempted_token_types),
            "remote_failures": list(self.remote_failures),
            "snapshot": self.snapshot.safe_dict(),
        }


@dataclass(frozen=True, slots=True)
class NeedsAuthCacheEntry:
    server_id: str
    config_fingerprint: str
    reason_code: str
    marked_at: float
    expires_at: float
    attempts: int = 1

    @property
    def key(self) -> str:
        raw = f"{self.server_id}\0{self.config_fingerprint}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def valid(self, now: float) -> bool:
        return self.expires_at > now

    def safe_dict(self) -> EventPayload:
        return {
            "server_id": self.server_id,
            "config_fingerprint": self.config_fingerprint,
            "reason_code": self.reason_code,
            "marked_at": self.marked_at,
            "expires_at": self.expires_at,
            "attempts": self.attempts,
        }


_CACHE_LOCKS_GUARD = threading.Lock()
_CACHE_LOCKS: dict[str, threading.RLock] = {}


def _cache_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(key, threading.RLock())


class DurableNeedsAuthCache:
    """Restart-safe negative cache keyed by canonical config fingerprint."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], float] = time.time,
        default_ttl_seconds: float = 900.0,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.clock = clock
        self.default_ttl_seconds = max(0.0, float(default_ttl_seconds))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, stat.S_IRWXU)
        except OSError:
            pass
        self._lock = _cache_lock(self.path)

    @staticmethod
    def _key(server_id: str, config_fingerprint: str) -> str:
        return hashlib.sha256(f"{server_id}\0{config_fingerprint}".encode("utf-8")).hexdigest()

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"schema_version": self.SCHEMA_VERSION, "entries": {}}
        except OSError:
            return {"schema_version": self.SCHEMA_VERSION, "entries": {}}
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return {"schema_version": self.SCHEMA_VERSION, "entries": {}}
        if not isinstance(document, dict) or document.get("schema_version") != self.SCHEMA_VERSION:
            return {"schema_version": self.SCHEMA_VERSION, "entries": {}}
        if not isinstance(document.get("entries"), dict):
            document["entries"] = {}
        return document

    def _write(self, document: Mapping[str, Any]) -> None:
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{random_secrets.token_hex(6)}")
        descriptor: int | None = None
        try:
            descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def lookup(self, server_id: str, config_fingerprint: str) -> NeedsAuthCacheEntry | None:
        key = self._key(server_id, config_fingerprint)
        with self._lock:
            document = self._read()
            raw = document["entries"].get(key)
            if not isinstance(raw, dict):
                return None
            try:
                entry = NeedsAuthCacheEntry(
                    server_id=str(raw["server_id"]),
                    config_fingerprint=str(raw["config_fingerprint"]),
                    reason_code=_safe_code(raw.get("reason_code"), "needs_auth"),
                    marked_at=float(raw["marked_at"]),
                    expires_at=float(raw["expires_at"]),
                    attempts=max(1, int(raw.get("attempts", 1))),
                )
            except (KeyError, TypeError, ValueError):
                document["entries"].pop(key, None)
                self._write(document)
                return None
            if entry.server_id != server_id or entry.config_fingerprint != config_fingerprint:
                document["entries"].pop(key, None)
                self._write(document)
                return None
            if not entry.valid(self.clock()):
                document["entries"].pop(key, None)
                self._write(document)
                return None
            return entry

    def mark(
        self,
        server_id: str,
        config_fingerprint: str,
        *,
        reason_code: str,
        ttl_seconds: float | None = None,
    ) -> NeedsAuthCacheEntry:
        now = self.clock()
        ttl = self.default_ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
        key = self._key(server_id, config_fingerprint)
        with self._lock:
            document = self._read()
            previous = document["entries"].get(key)
            attempts = int(previous.get("attempts", 0)) + 1 if isinstance(previous, dict) else 1
            entry = NeedsAuthCacheEntry(
                server_id=server_id,
                config_fingerprint=config_fingerprint,
                reason_code=_safe_code(reason_code, "needs_auth"),
                marked_at=now,
                expires_at=now + ttl,
                attempts=attempts,
            )
            document["entries"][key] = entry.safe_dict()
            self._write(document)
            return entry

    def clear(self, server_id: str, config_fingerprint: str) -> bool:
        key = self._key(server_id, config_fingerprint)
        with self._lock:
            document = self._read()
            existed = key in document["entries"]
            if existed:
                document["entries"].pop(key, None)
                self._write(document)
            return existed

    def purge(self) -> int:
        now = self.clock()
        removed = 0
        with self._lock:
            document = self._read()
            for key, raw in list(document["entries"].items()):
                try:
                    expires = float(raw["expires_at"])
                except (KeyError, TypeError, ValueError):
                    expires = 0
                if expires <= now:
                    document["entries"].pop(key, None)
                    removed += 1
            if removed:
                self._write(document)
        return removed

    def safe_snapshot(self) -> EventPayload:
        now = self.clock()
        with self._lock:
            entries = []
            for raw in self._read()["entries"].values():
                if not isinstance(raw, Mapping):
                    continue
                if float(raw.get("expires_at", 0)) <= now:
                    continue
                entries.append(redact_public_value(raw))
        return {"entries": entries, "raw_token_included": False}


class OAuthMetadataCache:
    """Durable non-secret metadata cache, isolated from token custody."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = _cache_lock(self.path)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {"schema_version": self.SCHEMA_VERSION, "entries": {}}
        if not isinstance(value, dict) or value.get("schema_version") != self.SCHEMA_VERSION:
            return {"schema_version": self.SCHEMA_VERSION, "entries": {}}
        if not isinstance(value.get("entries"), dict):
            value["entries"] = {}
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{random_secrets.token_hex(6)}")
        descriptor: int | None = None
        try:
            descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, self.path)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, config_fingerprint: str) -> OAuthMetadata | None:
        with self._lock:
            raw = self._read()["entries"].get(config_fingerprint)
            if not isinstance(raw, Mapping):
                return None
            try:
                return OAuthMetadata.from_mapping(raw)
            except AuthError:
                return None

    def set(self, config_fingerprint: str, metadata: OAuthMetadata) -> None:
        with self._lock:
            document = self._read()
            document["entries"][config_fingerprint] = metadata.safe_dict()
            self._write(document)

    def clear(self, config_fingerprint: str) -> bool:
        with self._lock:
            document = self._read()
            existed = config_fingerprint in document["entries"]
            if existed:
                document["entries"].pop(config_fingerprint, None)
                self._write(document)
            return existed


@runtime_checkable
class AuthNetworkProvider(Protocol):
    def discover(self, config: AuthRuntimeConfig) -> OAuthMetadata: ...

    def probe(self, config: AuthRuntimeConfig, metadata: OAuthMetadata | None) -> ProbeResult: ...

    def refresh(
        self,
        config: AuthRuntimeConfig,
        metadata: OAuthMetadata,
        refresh_token: SecretValue,
        *,
        scopes: tuple[str, ...],
        resource: str,
    ) -> OAuthTokenResponse: ...

    def revoke(
        self,
        config: AuthRuntimeConfig,
        metadata: OAuthMetadata | None,
        token: SecretValue,
        *,
        token_type_hint: str,
    ) -> None: ...

    def exchange_xaa(
        self,
        config: AuthRuntimeConfig,
        xaa: XaaConfiguration,
        id_token: SecretValue,
    ) -> OAuthTokenResponse: ...


class NullAuthNetworkProvider:
    """Fail-closed provider used when no interactive/network adapter exists."""

    def discover(self, config: AuthRuntimeConfig) -> OAuthMetadata:
        del config
        raise AuthNeedsInteraction("OAuth metadata discovery adapter is unavailable")

    def probe(self, config: AuthRuntimeConfig, metadata: OAuthMetadata | None) -> ProbeResult:
        del config, metadata
        return ProbeResult(ProbeDecision.NEEDS_AUTH, reason_code="provider_unavailable")

    def refresh(
        self,
        config: AuthRuntimeConfig,
        metadata: OAuthMetadata,
        refresh_token: SecretValue,
        *,
        scopes: tuple[str, ...],
        resource: str,
    ) -> OAuthTokenResponse:
        del config, metadata, refresh_token, scopes, resource
        raise AuthNeedsInteraction("OAuth refresh adapter is unavailable")

    def revoke(
        self,
        config: AuthRuntimeConfig,
        metadata: OAuthMetadata | None,
        token: SecretValue,
        *,
        token_type_hint: str,
    ) -> None:
        del config, metadata, token, token_type_hint
        raise AuthNeedsInteraction("OAuth revocation adapter is unavailable")

    def exchange_xaa(
        self,
        config: AuthRuntimeConfig,
        xaa: XaaConfiguration,
        id_token: SecretValue,
    ) -> OAuthTokenResponse:
        del config, xaa, id_token
        raise AuthNeedsInteraction("XAA exchange adapter is unavailable")


class CallbackAuthNetworkProvider(NullAuthNetworkProvider):
    """Composable provider for HTTP, browser, CLI, or in-process adapters."""

    def __init__(
        self,
        *,
        discover: Callable[[AuthRuntimeConfig], OAuthMetadata | Mapping[str, Any]] | None = None,
        probe: Callable[[AuthRuntimeConfig, OAuthMetadata | None], ProbeResult | Mapping[str, Any]] | None = None,
        refresh: Callable[
            [AuthRuntimeConfig, OAuthMetadata, SecretValue, tuple[str, ...], str],
            OAuthTokenResponse | Mapping[str, Any],
        ] | None = None,
        revoke: Callable[[AuthRuntimeConfig, OAuthMetadata | None, SecretValue, str], None] | None = None,
        exchange_xaa: Callable[
            [AuthRuntimeConfig, XaaConfiguration, SecretValue], OAuthTokenResponse | Mapping[str, Any]
        ] | None = None,
    ) -> None:
        self._discover = discover
        self._probe = probe
        self._refresh = refresh
        self._revoke = revoke
        self._exchange_xaa = exchange_xaa

    def discover(self, config: AuthRuntimeConfig) -> OAuthMetadata:
        if self._discover is None:
            return super().discover(config)
        value = self._discover(config)
        return value if isinstance(value, OAuthMetadata) else OAuthMetadata.from_mapping(value)

    def probe(self, config: AuthRuntimeConfig, metadata: OAuthMetadata | None) -> ProbeResult:
        if self._probe is None:
            return super().probe(config, metadata)
        return ProbeResult.from_value(self._probe(config, metadata))

    def refresh(
        self,
        config: AuthRuntimeConfig,
        metadata: OAuthMetadata,
        refresh_token: SecretValue,
        *,
        scopes: tuple[str, ...],
        resource: str,
    ) -> OAuthTokenResponse:
        if self._refresh is None:
            return super().refresh(config, metadata, refresh_token, scopes=scopes, resource=resource)
        value = self._refresh(config, metadata, refresh_token, scopes, resource)
        return value if isinstance(value, OAuthTokenResponse) else OAuthTokenResponse.from_mapping(value)

    def revoke(
        self,
        config: AuthRuntimeConfig,
        metadata: OAuthMetadata | None,
        token: SecretValue,
        *,
        token_type_hint: str,
    ) -> None:
        if self._revoke is None:
            return super().revoke(config, metadata, token, token_type_hint=token_type_hint)
        self._revoke(config, metadata, token, token_type_hint)

    def exchange_xaa(
        self,
        config: AuthRuntimeConfig,
        xaa: XaaConfiguration,
        id_token: SecretValue,
    ) -> OAuthTokenResponse:
        if self._exchange_xaa is None:
            return super().exchange_xaa(config, xaa, id_token)
        value = self._exchange_xaa(config, xaa, id_token)
        return value if isinstance(value, OAuthTokenResponse) else OAuthTokenResponse.from_mapping(value)


@dataclass(slots=True)
class _Flight(Generic[T]):
    condition: threading.Condition
    done: bool = False
    result: T | None = None
    error: BaseException | None = None


class SingleFlight:
    """Thread-safe same-key call coalescing used for probe and refresh."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._flights: dict[str, _Flight[Any]] = {}

    def run(self, key: str, operation: Callable[[], T]) -> T:
        with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight(threading.Condition(self._lock))
                self._flights[key] = flight
                leader = True
            else:
                leader = False
            if not leader:
                while not flight.done:
                    flight.condition.wait()
                if flight.error is not None:
                    raise flight.error
                return flight.result  # type: ignore[return-value]
        try:
            result = operation()
        except BaseException as exc:
            with self._lock:
                flight.error = exc
                flight.done = True
                flight.condition.notify_all()
                self._flights.pop(key, None)
            raise
        with self._lock:
            flight.result = result
            flight.done = True
            flight.condition.notify_all()
            self._flights.pop(key, None)
        return result

    def in_flight(self, key: str | None = None) -> int | bool:
        with self._lock:
            return key in self._flights if key is not None else len(self._flights)


def classify_refresh_error(exc: BaseException) -> RefreshErrorKind:
    if isinstance(exc, AuthCancelled):
        return RefreshErrorKind.CANCELLED
    if isinstance(exc, AuthConfigurationError):
        return RefreshErrorKind.CONFIGURATION
    if isinstance(exc, AuthStepUpRequired):
        return RefreshErrorKind.INSUFFICIENT_SCOPE
    code = _safe_code(getattr(exc, "code", ""), "unknown")
    if code in {"invalid_grant", "expired_refresh_token", "invalid_refresh_token", "token_expired"}:
        return RefreshErrorKind.INVALID_GRANT
    if code in {"invalid_client", "unauthorized_client"}:
        return RefreshErrorKind.INVALID_CLIENT
    if code in {"insufficient_scope", "step_up_required", "forbidden_scope"}:
        return RefreshErrorKind.INSUFFICIENT_SCOPE
    if code in {"invalid_request", "invalid_response", "invalid_token_response", "protocol_error"}:
        return RefreshErrorKind.PROTOCOL
    status = getattr(exc, "status", None)
    retryable = getattr(exc, "retryable", None)
    if retryable is True or status in {408, 425, 429, 500, 502, 503, 504}:
        return RefreshErrorKind.TRANSIENT
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return RefreshErrorKind.TRANSIENT
    return RefreshErrorKind.UNKNOWN


class McpAuthRuntime:
    """Own OAuth/XAA state, credential custody, retries, and safe projection."""

    def __init__(
        self,
        config: AuthRuntimeConfig,
        *,
        credential_vault: CredentialVault | None = None,
        credential_provider: VaultCredentialProvider | None = None,
        network_provider: AuthNetworkProvider | None = None,
        needs_auth_cache: DurableNeedsAuthCache | None = None,
        needs_auth_cache_path: str | os.PathLike[str] | None = None,
        metadata_cache: OAuthMetadataCache | None = None,
        metadata_cache_path: str | os.PathLike[str] | None = None,
        event_sink: Any = None,
        state_store: Any = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.clock = clock
        self.network_provider = network_provider or NullAuthNetworkProvider()
        selected_vault = credential_vault or MemoryCredentialVault(clock=clock)
        self.credentials = credential_provider or VaultCredentialProvider(selected_vault)
        cache_path = Path(needs_auth_cache_path or f".mcp-needs-auth-{hashlib.sha256(config.server_id.encode()).hexdigest()[:12]}.json")
        self.needs_auth_cache = needs_auth_cache or DurableNeedsAuthCache(
            cache_path,
            clock=clock,
            default_ttl_seconds=config.needs_auth_ttl_seconds,
        )
        metadata_path = Path(metadata_cache_path or str(cache_path) + ".metadata")
        self.metadata_cache = metadata_cache or OAuthMetadataCache(metadata_path)
        self.event_sink = event_sink
        self.state_store = state_store
        self._lock = threading.RLock()
        self._singleflight = SingleFlight()
        self._metadata = self.metadata_cache.get(config.config_fingerprint)
        self._known_external_versions: dict[CredentialKind, str] = {}
        self._step_up: StepUpRequirement | None = None
        self._snapshot = AuthSnapshot(
            server_id=config.server_id,
            config_fingerprint=config.config_fingerprint,
            mode=config.mode,
            status=AuthStatus.NOT_REQUIRED if config.mode is AuthMode.NONE else AuthStatus.UNKNOWN,
            revision=0,
            resource=config.resource,
            updated_at=_timestamp(clock),
        )
        self._publish_snapshot("initialized")

    @property
    def snapshot(self) -> AuthSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def metadata(self) -> OAuthMetadata | None:
        with self._lock:
            return self._metadata

    def _reference(self, kind: CredentialKind = CredentialKind.OAUTH) -> CredentialReference:
        return self.credentials.reference(self.config.server_id, self.config.config_fingerprint, kind)

    def _safe_event(self, event_type: str, payload: Mapping[str, Any]) -> EventPayload:
        selected = redact_public_value(payload)
        if not isinstance(selected, dict):
            selected = {"value": selected}
        serialized = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        lowered = serialized.casefold()
        # Structural defense: event shapes may say token *digest* or
        # raw_token_included=false, but never carry token fields themselves.
        forbidden = ('"access_token":', '"refresh_token":', '"id_token":', '"client_secret":')
        if any(marker in lowered for marker in forbidden):
            raise AuthError("unsafe authentication event payload", code="secret_projection_blocked")
        return {
            "event_type": event_type,
            "server_id": self.config.server_id,
            "config_fingerprint": self.config.config_fingerprint,
            "occurred_at": _timestamp(self.clock),
            "payload": selected,
        }

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        event = self._safe_event(event_type, payload)
        sink = self.event_sink
        if sink is None:
            return
        try:
            if callable(sink):
                sink(event)
            elif hasattr(sink, "append_event"):
                try:
                    sink.append_event(event_type, event)
                except TypeError:
                    sink.append_event(event)
            elif hasattr(sink, "publish"):
                sink.publish(event)
        except Exception:
            # Observability cannot make a valid credential unusable.  The
            # runtime state store still receives the latest safe snapshot.
            return

    def _publish_state_store(self, snapshot: AuthSnapshot) -> None:
        store = self.state_store
        if store is None:
            return
        payload = snapshot.safe_dict()
        try:
            if hasattr(store, "update_section"):
                try:
                    store.update_section("auth", self.config.server_id, payload)
                except TypeError:
                    store.update_section("auth", {self.config.server_id: payload})
            elif hasattr(store, "set"):
                store.set(f"mcp.auth.{self.config.server_id}", payload)
            elif hasattr(store, "save"):
                store.save(f"mcp.auth.{self.config.server_id}", payload)
        except Exception:
            return

    def _publish_snapshot(self, reason: str) -> None:
        snapshot = self.snapshot
        self._publish_state_store(snapshot)
        self._emit("mcp.auth.state", {"reason": _safe_code(reason), "snapshot": snapshot.safe_dict()})

    def _transition(
        self,
        status: AuthStatus,
        *,
        error_code: str = "",
        credential: CredentialMetadata | None = None,
        scopes: Iterable[str] | None = None,
        resource: str | None = None,
        issuer: str | None = None,
        cached_until: str | None = None,
        step_up: StepUpRequirement | None | object = ...,
        reason: str = "transition",
    ) -> AuthSnapshot:
        with self._lock:
            previous = self._snapshot
            kwargs: dict[str, Any] = {
                "status": status,
                "revision": previous.revision + 1,
                "error_code": _safe_code(error_code, "") if error_code else "",
                "updated_at": _timestamp(self.clock),
            }
            if credential is not None:
                kwargs.update(
                    credential_reference=credential.reference.opaque_id,
                    expires_at=credential.expires_at,
                    external_version=credential.external_version,
                )
            if scopes is not None:
                kwargs["scopes"] = _normalize_scope(scopes)
            if resource is not None:
                kwargs["resource"] = resource
            if issuer is not None:
                kwargs["issuer"] = issuer
            if cached_until is not None:
                kwargs["needs_auth_cached_until"] = cached_until
            if step_up is not ...:
                kwargs["step_up"] = step_up
                self._step_up = step_up if isinstance(step_up, StepUpRequirement) else None
            self._snapshot = replace(previous, **kwargs)
            snapshot = self._snapshot
        self._publish_state_store(snapshot)
        self._emit(
            "mcp.auth.state_changed",
            {
                "reason": _safe_code(reason),
                "from": str(previous.status),
                "to": str(status),
                "snapshot": snapshot.safe_dict(),
            },
        )
        return snapshot

    def _metadata_or_discover(self) -> OAuthMetadata:
        with self._lock:
            if self._metadata is not None:
                return self._metadata

        def discover() -> OAuthMetadata:
            metadata = self.network_provider.discover(self.config)
            if not isinstance(metadata, OAuthMetadata):
                raise AuthError("provider returned invalid metadata", code="invalid_metadata")
            self.set_metadata(metadata)
            return metadata

        return self._singleflight.run(f"metadata:{self.config.config_fingerprint}", discover)

    def set_metadata(self, metadata: OAuthMetadata) -> None:
        if self.config.resource and metadata.protected_resource and metadata.protected_resource != self.config.resource:
            raise AuthConfigurationError("OAuth protected resource mismatch")
        with self._lock:
            self._metadata = metadata
        self.metadata_cache.set(self.config.config_fingerprint, metadata)
        self._emit("mcp.auth.metadata_updated", {"metadata": metadata.safe_dict(), "digest": metadata.digest})

    def _credential_public_attributes(self, response: OAuthTokenResponse) -> dict[str, PublicValue]:
        return {
            "token_type": response.token_type,
            "scopes": list(response.scopes),
            "resource": response.resource,
            "issuer": response.issuer,
        }

    def _save_tokens(
        self,
        response: OAuthTokenResponse,
        *,
        kind: CredentialKind,
        preserve_refresh_token: str = "",
        preserve_id_token: str = "",
    ) -> CredentialMetadata:
        refresh_token = response.refresh_token or preserve_refresh_token
        id_token = response.id_token or preserve_id_token
        secrets: dict[str, str] = {"access_token": response.access_token}
        if refresh_token:
            secrets["refresh_token"] = refresh_token
        if id_token:
            secrets["id_token"] = id_token
        expiry = response.absolute_expiry(self.clock)
        expires_at = _timestamp(lambda: expiry) if expiry is not None else ""
        metadata = self.credentials.save(
            self.config.server_id,
            self.config.config_fingerprint,
            kind,
            secrets,
            public_attributes=self._credential_public_attributes(response),
            expires_at=expires_at,
        )
        self._known_external_versions[kind] = metadata.external_version
        fingerprints: dict[str, str]
        with self.credentials.load(self.config.server_id, self.config.config_fingerprint, kind) as envelope:
            fingerprints = envelope.secret_fingerprints()
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                access_token_digest=fingerprints.get("access_token", ""),
                refresh_token_digest=fingerprints.get("refresh_token", ""),
            )
        return metadata

    def install_tokens(
        self,
        response: OAuthTokenResponse | Mapping[str, Any],
        *,
        kind: CredentialKind = CredentialKind.OAUTH,
        reason: str = "token_install",
    ) -> AuthOutcome:
        selected = response if isinstance(response, OAuthTokenResponse) else OAuthTokenResponse.from_mapping(response)
        metadata = self._save_tokens(selected, kind=kind)
        self.needs_auth_cache.clear(self.config.server_id, self.config.config_fingerprint)
        snapshot = self._transition(
            AuthStatus.AUTHENTICATED,
            credential=metadata,
            scopes=selected.scopes,
            resource=selected.resource or self.config.resource,
            issuer=selected.issuer or (self._metadata.issuer if self._metadata else ""),
            cached_until="",
            step_up=None,
            reason=reason,
        )
        return AuthOutcome(AuthStatus.AUTHENTICATED, snapshot, credential_changed=True)

    def _mark_needs_auth(self, reason_code: str) -> AuthOutcome:
        entry = self.needs_auth_cache.mark(
            self.config.server_id,
            self.config.config_fingerprint,
            reason_code=reason_code,
            ttl_seconds=self.config.needs_auth_ttl_seconds,
        )
        snapshot = self._transition(
            AuthStatus.NEEDS_AUTH,
            error_code=entry.reason_code,
            cached_until=_timestamp(lambda: entry.expires_at),
            reason="needs_auth",
        )
        return AuthOutcome(AuthStatus.NEEDS_AUTH, snapshot, network_called=True)

    def probe(self, *, force: bool = False) -> AuthOutcome:
        if self.config.mode is AuthMode.NONE:
            snapshot = self._transition(AuthStatus.NOT_REQUIRED, reason="auth_not_required")
            return AuthOutcome(AuthStatus.NOT_REQUIRED, snapshot)
        cached = None if force else self.needs_auth_cache.lookup(
            self.config.server_id, self.config.config_fingerprint
        )
        if cached is not None:
            snapshot = self._transition(
                AuthStatus.NEEDS_AUTH,
                error_code=cached.reason_code,
                cached_until=_timestamp(lambda: cached.expires_at),
                reason="needs_auth_cache_hit",
            )
            return AuthOutcome(AuthStatus.NEEDS_AUTH, snapshot, cache_hit=True)

        def operation() -> AuthOutcome:
            self._transition(AuthStatus.AUTHENTICATING, reason="probe_started")
            metadata = self.metadata
            try:
                result = ProbeResult.from_value(self.network_provider.probe(self.config, metadata))
            except AuthNeedsInteraction as exc:
                return self._mark_needs_auth(exc.code)
            except Exception as exc:
                kind = classify_refresh_error(exc)
                snapshot = self._transition(
                    AuthStatus.FAILED,
                    error_code=getattr(exc, "code", str(kind)),
                    reason="probe_failed",
                )
                return AuthOutcome(AuthStatus.FAILED, snapshot, network_called=True, error_kind=kind)
            if result.metadata is not None:
                self.set_metadata(result.metadata)
            if result.decision is ProbeDecision.AUTHENTICATED:
                outcome = self.install_tokens(result.tokens, reason="probe_authenticated")  # type: ignore[arg-type]
                return replace(outcome, network_called=True)
            if result.decision is ProbeDecision.NOT_REQUIRED:
                self.needs_auth_cache.clear(self.config.server_id, self.config.config_fingerprint)
                snapshot = self._transition(AuthStatus.NOT_REQUIRED, reason="probe_not_required")
                return AuthOutcome(AuthStatus.NOT_REQUIRED, snapshot, network_called=True)
            if result.decision is ProbeDecision.NEEDS_AUTH:
                return self._mark_needs_auth(result.reason_code)
            snapshot = self._transition(
                AuthStatus.FAILED,
                error_code=result.reason_code or "probe_failed",
                reason="probe_failed",
            )
            return AuthOutcome(AuthStatus.FAILED, snapshot, network_called=True)

        return self._singleflight.run(f"probe:{self.config.config_fingerprint}", operation)

    def _load_envelope(self, kind: CredentialKind) -> CredentialEnvelope:
        return self.credentials.load(self.config.server_id, self.config.config_fingerprint, kind)

    def reload_if_external_update(self, kind: CredentialKind = CredentialKind.OAUTH) -> bool:
        current = self.credentials.external_version(self.config.server_id, self.config.config_fingerprint, kind)
        previous = self._known_external_versions.get(kind, "")
        if not current or not previous:
            if current:
                self._known_external_versions[kind] = current
            return False
        if current == previous:
            return False
        with self._load_envelope(kind) as envelope:
            self._known_external_versions[kind] = envelope.metadata.external_version
            fingerprints = envelope.secret_fingerprints()
            attributes = envelope.metadata.public_attributes
            scopes = attributes.get("scopes") if isinstance(attributes.get("scopes"), list) else ()
            with self._lock:
                self._snapshot = replace(
                    self._snapshot,
                    credential_reference=envelope.metadata.reference.opaque_id,
                    access_token_digest=fingerprints.get("access_token", ""),
                    refresh_token_digest=fingerprints.get("refresh_token", ""),
                    expires_at=envelope.metadata.expires_at,
                    scopes=_normalize_scope(scopes),
                    external_version=envelope.metadata.external_version,
                    revision=self._snapshot.revision + 1,
                    updated_at=_timestamp(self.clock),
                )
        self._emit("mcp.auth.external_credentials_reloaded", {"kind": str(kind), "snapshot": self.snapshot.safe_dict()})
        self._publish_state_store(self.snapshot)
        return True

    def refresh(
        self,
        *,
        force: bool = False,
        requested_scopes: Iterable[str] = (),
        resource: str = "",
        kind: CredentialKind = CredentialKind.OAUTH,
    ) -> AuthOutcome:
        selected_scopes = _normalize_scope(requested_scopes) or self.config.requested_scopes
        selected_resource = resource or self.config.resource
        if selected_resource:
            selected_resource = _normalize_url(selected_resource, field_name="refresh resource")

        def operation() -> AuthOutcome:
            try:
                envelope = self._load_envelope(kind)
            except CredentialNotFound:
                return self._mark_needs_auth("missing_credentials")
            with envelope:
                before_version = envelope.metadata.external_version
                expires = envelope.metadata.expired_at
                if not force and expires is not None and expires - self.clock() > self.config.refresh_skew_seconds:
                    snapshot = self._transition(
                        AuthStatus.AUTHENTICATED,
                        credential=envelope.metadata,
                        scopes=envelope.metadata.public_attributes.get("scopes", ()),
                        resource=str(envelope.metadata.public_attributes.get("resource") or selected_resource),
                        issuer=str(envelope.metadata.public_attributes.get("issuer") or ""),
                        reason="refresh_not_needed",
                    )
                    return AuthOutcome(AuthStatus.AUTHENTICATED, snapshot)
                refresh_secret = envelope.secret("refresh_token", required=False)
                if refresh_secret is None or not refresh_secret:
                    return self._mark_needs_auth("missing_refresh_token")
                preserve_refresh = refresh_secret.reveal_text()
                id_secret = envelope.secret("id_token", required=False)
                preserve_id = id_secret.reveal_text() if id_secret else ""
                self._transition(AuthStatus.REFRESHING, credential=envelope.metadata, reason="refresh_started")
                try:
                    metadata = self._metadata_or_discover()
                    response = self.network_provider.refresh(
                        self.config,
                        metadata,
                        refresh_secret,
                        scopes=selected_scopes,
                        resource=selected_resource,
                    )
                    if not isinstance(response, OAuthTokenResponse):
                        response = OAuthTokenResponse.from_mapping(response)  # type: ignore[arg-type]
                except Exception as exc:
                    error_kind = classify_refresh_error(exc)
                    after_version = self.credentials.external_version(
                        self.config.server_id, self.config.config_fingerprint, kind
                    )
                    if after_version and after_version != before_version:
                        self._known_external_versions[kind] = before_version
                        self.reload_if_external_update(kind)
                        snapshot = self._transition(AuthStatus.AUTHENTICATED, reason="refresh_external_winner")
                        return AuthOutcome(
                            AuthStatus.AUTHENTICATED,
                            snapshot,
                            network_called=True,
                            credential_changed=True,
                        )
                    if error_kind is RefreshErrorKind.INSUFFICIENT_SCOPE:
                        requirement = self.require_step_up(
                            selected_scopes or self.config.requested_scopes,
                            selected_resource,
                            reason_code=getattr(exc, "code", "insufficient_scope"),
                        )
                        return AuthOutcome(
                            AuthStatus.STEP_UP_REQUIRED,
                            requirement,
                            network_called=True,
                            error_kind=error_kind,
                        )
                    if error_kind in {RefreshErrorKind.INVALID_GRANT, RefreshErrorKind.INVALID_CLIENT}:
                        self.credentials.delete(self.config.server_id, self.config.config_fingerprint, kind)
                        outcome = self._mark_needs_auth(str(error_kind))
                        return replace(outcome, error_kind=error_kind)
                    snapshot = self._transition(
                        AuthStatus.FAILED,
                        error_code=getattr(exc, "code", str(error_kind)),
                        credential=envelope.metadata,
                        reason="refresh_failed",
                    )
                    return AuthOutcome(
                        AuthStatus.FAILED,
                        snapshot,
                        network_called=True,
                        error_kind=error_kind,
                    )
                finally:
                    refresh_secret.destroy()
                    if id_secret:
                        id_secret.destroy()
                stored = self._save_tokens(
                    response,
                    kind=kind,
                    preserve_refresh_token=preserve_refresh,
                    preserve_id_token=preserve_id,
                )
                self.needs_auth_cache.clear(self.config.server_id, self.config.config_fingerprint)
                snapshot = self._transition(
                    AuthStatus.AUTHENTICATED,
                    credential=stored,
                    scopes=response.scopes or selected_scopes,
                    resource=response.resource or selected_resource,
                    issuer=response.issuer or metadata.issuer,
                    cached_until="",
                    step_up=None,
                    reason="refresh_succeeded",
                )
                return AuthOutcome(
                    AuthStatus.AUTHENTICATED,
                    snapshot,
                    network_called=True,
                    credential_changed=True,
                )

        return self._singleflight.run(f"refresh:{kind}:{self.config.config_fingerprint}", operation)

    def ensure_authorized(self, *, kind: CredentialKind = CredentialKind.OAUTH) -> AuthOutcome:
        if self.config.mode is AuthMode.NONE:
            snapshot = self._transition(AuthStatus.NOT_REQUIRED, reason="auth_not_required")
            return AuthOutcome(AuthStatus.NOT_REQUIRED, snapshot)
        try:
            envelope = self._load_envelope(kind)
        except CredentialNotFound:
            return self.probe()
        with envelope:
            previous_version = self._known_external_versions.get(kind)
            self._known_external_versions[kind] = envelope.metadata.external_version
            external = bool(previous_version and previous_version != envelope.metadata.external_version)
            expiry = envelope.metadata.expired_at
            if expiry is not None and expiry - self.clock() <= self.config.refresh_skew_seconds:
                return self.refresh(kind=kind)
            attributes = envelope.metadata.public_attributes
            fingerprints = envelope.secret_fingerprints()
            with self._lock:
                self._snapshot = replace(
                    self._snapshot,
                    access_token_digest=fingerprints.get("access_token", ""),
                    refresh_token_digest=fingerprints.get("refresh_token", ""),
                )
            snapshot = self._transition(
                AuthStatus.AUTHENTICATED,
                credential=envelope.metadata,
                scopes=attributes.get("scopes", ()),
                resource=str(attributes.get("resource") or self.config.resource),
                issuer=str(attributes.get("issuer") or ""),
                cached_until="",
                reason="credential_loaded",
            )
            if external:
                self._emit("mcp.auth.external_credentials_reloaded", {"kind": str(kind), "snapshot": snapshot.safe_dict()})
            return AuthOutcome(AuthStatus.AUTHENTICATED, snapshot, credential_changed=external)

    def authorization_header(self, *, kind: CredentialKind = CredentialKind.OAUTH) -> str:
        outcome = self.ensure_authorized(kind=kind)
        if not outcome.authorized:
            raise AuthNeedsInteraction("MCP server is not authenticated")
        with self._load_envelope(kind) as envelope:
            token = envelope.reveal_text("access_token")
            token_type = str(envelope.metadata.public_attributes.get("token_type") or "Bearer")
            # This is the only ordinary API that deliberately returns raw
            # access material.  It is for a transport header, never state.
            return f"{token_type} {token}"

    def require_step_up(
        self,
        scopes: Iterable[str],
        resource: str = "",
        *,
        reason_code: str = "insufficient_scope",
    ) -> AuthSnapshot:
        requirement = StepUpRequirement(
            scopes=_normalize_scope(scopes),
            resource=resource,
            reason_code=reason_code,
            requested_at=_timestamp(self.clock),
        )
        return self._transition(
            AuthStatus.STEP_UP_REQUIRED,
            error_code=requirement.reason_code,
            step_up=requirement,
            reason="step_up_required",
        )

    def complete_step_up(self, response: OAuthTokenResponse | Mapping[str, Any]) -> AuthOutcome:
        selected = response if isinstance(response, OAuthTokenResponse) else OAuthTokenResponse.from_mapping(response)
        requirement = self._step_up
        if requirement is None:
            raise AuthConfigurationError("no step-up request is pending")
        missing = set(requirement.scopes) - set(selected.scopes)
        if missing:
            raise AuthStepUpRequired("step-up response did not grant required scopes")
        if requirement.resource and selected.resource and selected.resource != requirement.resource:
            raise AuthConfigurationError("step-up response resource mismatch")
        return self.install_tokens(selected, reason="step_up_completed")

    def revoke(self, *, kind: CredentialKind = CredentialKind.OAUTH) -> RevokeOutcome:
        attempted: list[str] = []
        failures: list[str] = []
        local_deleted = False
        try:
            envelope = self._load_envelope(kind)
        except CredentialNotFound:
            envelope = None
        try:
            if envelope is not None:
                with envelope:
                    # RFC 7009 operational ordering: revoke refresh first so
                    # it cannot mint a replacement access token, then access.
                    for field_name, hint in (
                        ("refresh_token", "refresh_token"),
                        ("access_token", "access_token"),
                    ):
                        token = envelope.secret(field_name, required=False)
                        if token is None or not token:
                            continue
                        attempted.append(hint)
                        try:
                            self.network_provider.revoke(
                                self.config,
                                self.metadata,
                                token,
                                token_type_hint=hint,
                            )
                        except Exception as exc:
                            failures.append(str(classify_refresh_error(exc)))
                        finally:
                            token.destroy()
        finally:
            try:
                local_deleted = self.credentials.delete(
                    self.config.server_id, self.config.config_fingerprint, kind
                )
            except CredentialError:
                local_deleted = False
            self.needs_auth_cache.clear(self.config.server_id, self.config.config_fingerprint)
            self._known_external_versions.pop(kind, None)
        snapshot = self._transition(
            AuthStatus.REVOKED,
            error_code=failures[0] if failures else "",
            cached_until="",
            step_up=None,
            reason="revoked",
        )
        outcome = RevokeOutcome(local_deleted, tuple(attempted), tuple(failures), snapshot)
        self._emit("mcp.auth.revoked", outcome.safe_dict())
        return outcome

    def install_xaa_identity(self, id_token: str, *, expires_at: float | None = None) -> CredentialMetadata:
        if self.config.xaa is None or not self.config.xaa.enabled:
            raise AuthConfigurationError("XAA is not enabled")
        if not id_token:
            raise AuthConfigurationError("XAA identity token is required")
        expiry_text = _timestamp(lambda: expires_at) if expires_at is not None else ""
        metadata = self.credentials.save(
            self.config.server_id,
            self.config.config_fingerprint,
            CredentialKind.XAA,
            {"id_token": id_token},
            public_attributes={
                "issuer": self.config.xaa.issuer,
                "resource": self.config.xaa.resource,
                "client_id": self.config.xaa.client_id,
                "phase": "identity",
            },
            expires_at=expiry_text,
        )
        self._known_external_versions[CredentialKind.XAA] = metadata.external_version
        self._emit(
            "mcp.auth.xaa_identity_stored",
            {"credential_reference": metadata.reference.opaque_id, "expires_at": expiry_text},
        )
        return metadata

    def exchange_xaa(
        self,
        *,
        id_token: str | None = None,
        discovered_issuer: str = "",
        discovered_resource: str = "",
        discovered_token_endpoint: str = "",
    ) -> AuthOutcome:
        xaa = self.config.xaa
        if xaa is None or not xaa.enabled:
            raise AuthConfigurationError("XAA is not enabled")
        if discovered_issuer or discovered_resource or discovered_token_endpoint:
            xaa.validate_discovery(
                issuer=discovered_issuer or xaa.issuer,
                resource=discovered_resource or xaa.resource,
                token_endpoint=discovered_token_endpoint or xaa.token_endpoint,
            )

        def operation() -> AuthOutcome:
            selected_identity: SecretValue
            if id_token is not None:
                selected_identity = SecretValue(id_token)
                preserve_identity = id_token
            else:
                try:
                    envelope = self._load_envelope(CredentialKind.XAA)
                except CredentialNotFound as exc:
                    raise AuthNeedsInteraction("XAA identity token is unavailable") from exc
                with envelope:
                    selected_identity = envelope.secret("id_token")  # type: ignore[assignment]
                    preserve_identity = selected_identity.reveal_text()
            self._transition(AuthStatus.AUTHENTICATING, reason="xaa_exchange_started")
            try:
                response = self.network_provider.exchange_xaa(self.config, xaa, selected_identity)
                if not isinstance(response, OAuthTokenResponse):
                    response = OAuthTokenResponse.from_mapping(response)  # type: ignore[arg-type]
                if response.issuer and response.issuer != xaa.issuer:
                    raise AuthConfigurationError("XAA exchange issuer mismatch")
                if response.resource and response.resource != xaa.resource:
                    raise AuthConfigurationError("XAA exchange resource mismatch")
            except Exception as exc:
                kind = classify_refresh_error(exc)
                snapshot = self._transition(
                    AuthStatus.FAILED,
                    error_code=getattr(exc, "code", str(kind)),
                    reason="xaa_exchange_failed",
                )
                return AuthOutcome(AuthStatus.FAILED, snapshot, network_called=True, error_kind=kind)
            finally:
                selected_identity.destroy()
            stored = self._save_tokens(
                response,
                kind=CredentialKind.XAA,
                preserve_id_token=preserve_identity,
            )
            snapshot = self._transition(
                AuthStatus.AUTHENTICATED,
                credential=stored,
                scopes=response.scopes,
                resource=xaa.resource,
                issuer=xaa.issuer,
                reason="xaa_exchange_succeeded",
            )
            return AuthOutcome(
                AuthStatus.AUTHENTICATED,
                snapshot,
                network_called=True,
                credential_changed=True,
            )

        return self._singleflight.run(f"xaa:{self.config.config_fingerprint}", operation)

    def clear_local(self, *, include_metadata: bool = False) -> bool:
        removed = False
        for kind in (CredentialKind.OAUTH, CredentialKind.XAA, CredentialKind.BEARER, CredentialKind.CLIENT):
            try:
                removed = self.credentials.delete(
                    self.config.server_id, self.config.config_fingerprint, kind
                ) or removed
            except CredentialError:
                continue
        self.needs_auth_cache.clear(self.config.server_id, self.config.config_fingerprint)
        if include_metadata:
            self.metadata_cache.clear(self.config.config_fingerprint)
            with self._lock:
                self._metadata = None
        self._known_external_versions.clear()
        self._transition(AuthStatus.UNKNOWN, cached_until="", step_up=None, reason="local_auth_cleared")
        return removed

    def safe_diagnostics(self) -> EventPayload:
        snapshot = self.snapshot
        return {
            "snapshot": snapshot.safe_dict(),
            "metadata": self.metadata.safe_dict() if self.metadata else None,
            "needs_auth_cache": self.needs_auth_cache.safe_snapshot(),
            "probe_in_flight": bool(self._singleflight.in_flight(f"probe:{self.config.config_fingerprint}")),
            "refresh_in_flight": bool(
                self._singleflight.in_flight(f"refresh:{CredentialKind.OAUTH}:{self.config.config_fingerprint}")
            ),
            "credential_backend": type(self.credentials.vault).__name__,
            "raw_token_included": False,
        }


__all__ = [
    "AuthCancelled",
    "AuthConfigurationError",
    "AuthError",
    "AuthMode",
    "AuthNeedsInteraction",
    "AuthNetworkError",
    "AuthNetworkProvider",
    "AuthOutcome",
    "AuthRuntimeConfig",
    "AuthSnapshot",
    "AuthStatus",
    "AuthStepUpRequired",
    "CallbackAuthNetworkProvider",
    "DurableNeedsAuthCache",
    "McpAuthRuntime",
    "NeedsAuthCacheEntry",
    "NullAuthNetworkProvider",
    "OAuthMetadata",
    "OAuthMetadataCache",
    "OAuthTokenResponse",
    "ProbeDecision",
    "ProbeResult",
    "RefreshErrorKind",
    "RevokeOutcome",
    "SingleFlight",
    "StepUpRequirement",
    "XaaConfiguration",
    "classify_refresh_error",
    "compute_config_fingerprint",
]
