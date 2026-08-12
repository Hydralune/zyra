from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal, Mapping, Sequence


class ProviderProtocol(StrEnum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_MESSAGES = "anthropic_messages"


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
    BLOCKED = "blocked"
    REFRESHING = "refreshing"


class ProviderControlPlanePortError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.detail = dict(detail or {})


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    provider_id: str
    display_name: str
    integration_id: str | None
    status: str
    base_url: str
    protocol: ProviderProtocol
    default_headers: Mapping[str, str] = field(default_factory=dict)
    request_defaults: Mapping[str, Any] = field(default_factory=dict)
    allowed_hosts: Sequence[str] = field(default_factory=tuple)
    tags: Sequence[str] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "displayName": self.display_name,
            "integrationId": self.integration_id,
            "status": self.status,
            "baseUrl": self.base_url,
            "protocol": self.protocol.value,
            "defaultHeaders": dict(self.default_headers),
            "requestDefaults": dict(self.request_defaults),
            "allowedHosts": list(self.allowed_hosts),
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    input: Sequence[str] = ("text",)
    output: Sequence[str] = ("text",)
    tools: bool = True
    streaming: bool = True
    reasoning: bool = False
    structured_output: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {
            "input": list(self.input),
            "output": list(self.output),
            "tools": self.tools,
            "streaming": self.streaming,
            "reasoning": self.reasoning,
            "structuredOutput": self.structured_output,
        }


@dataclass(frozen=True, slots=True)
class ModelDefinition:
    provider_id: str
    model_id: str
    display_name: str
    family: str
    status: str = "active"
    enabled: bool = True
    released_at: int = 0
    context_window: int = 128_000
    maximum_output_tokens: int = 16_384
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    pricing: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    endpoint_path: str | None = None
    protocol: ProviderProtocol | None = None
    request_defaults: Mapping[str, Any] = field(default_factory=dict)
    tags: Sequence[str] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "providerId": self.provider_id,
            "modelId": self.model_id,
            "displayName": self.display_name,
            "family": self.family,
            "status": self.status,
            "enabled": self.enabled,
            "releasedAt": self.released_at,
            "contextWindow": self.context_window,
            "maximumOutputTokens": self.maximum_output_tokens,
            "capabilities": self.capabilities.to_wire(),
            "pricing": [dict(item) for item in self.pricing],
            "endpointPath": self.endpoint_path,
            "protocol": None if self.protocol is None else self.protocol.value,
            "requestDefaults": dict(self.request_defaults),
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class IntegrationDefinition:
    integration_id: str
    display_name: str
    kind: str
    env_names: Sequence[str] = field(default_factory=tuple)
    header_name: str | None = None
    authorization_scheme: str | None = None
    supports_refresh: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "integrationId": self.integration_id,
            "displayName": self.display_name,
            "kind": self.kind,
            "envNames": list(self.env_names),
            "headerName": self.header_name,
            "authorizationScheme": self.authorization_scheme,
            "supportsRefresh": self.supports_refresh,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CredentialRegistration:
    integration_id: str
    provider_id: str
    account_id: str
    secret_ref: str
    fingerprint: str
    credential_id: str | None = None
    priority: int = 0
    allowed_models: Sequence[str] = field(default_factory=tuple)
    scopes: Sequence[str] = field(default_factory=tuple)
    expires_at: int | None = None
    refresh_after: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        value = {
            "integrationId": self.integration_id,
            "providerId": self.provider_id,
            "accountId": self.account_id,
            "secretRef": self.secret_ref,
            "fingerprint": self.fingerprint,
            "priority": self.priority,
            "allowedModels": list(self.allowed_models),
            "scopes": list(self.scopes),
            "expiresAt": self.expires_at,
            "refreshAfter": self.refresh_after,
            "metadata": dict(self.metadata),
        }
        if self.credential_id:
            value["credentialId"] = self.credential_id
        return value


@dataclass(frozen=True, slots=True)
class RouteConstraints:
    provider_ids: Sequence[str] = field(default_factory=tuple)
    model_ids: Sequence[str] = field(default_factory=tuple)
    required_input: Sequence[str] = ("text",)
    required_output: Sequence[str] = ("text",)
    require_tools: bool = False
    require_streaming: bool = True
    minimum_context_window: int = 0
    maximum_input_price_per_million: float | None = None
    maximum_output_price_per_million: float | None = None
    excluded_credential_ids: Sequence[str] = field(default_factory=tuple)
    required_scopes: Sequence[str] = field(default_factory=tuple)

    def to_wire(self) -> dict[str, Any]:
        return {
            "providerIds": list(self.provider_ids),
            "modelIds": list(self.model_ids),
            "requiredInput": list(self.required_input),
            "requiredOutput": list(self.required_output),
            "requireTools": self.require_tools,
            "requireStreaming": self.require_streaming,
            "minimumContextWindow": self.minimum_context_window,
            "maximumInputPricePerMillion": self.maximum_input_price_per_million,
            "maximumOutputPricePerMillion": self.maximum_output_price_per_million,
            "excludedCredentialIds": list(self.excluded_credential_ids),
            "requiredScopes": list(self.required_scopes),
        }


@dataclass(frozen=True, slots=True)
class RouteRequest:
    run_id: str
    task_id: str
    session_id: str
    turn_id: str
    node_id: str | None = None
    purpose: str = "general"
    preferred_provider_id: str | None = None
    preferred_model_id: str | None = None
    route_hint: str | None = None
    constraints: RouteConstraints = field(default_factory=RouteConstraints)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "taskId": self.task_id,
            "nodeId": self.node_id,
            "sessionId": self.session_id,
            "turnId": self.turn_id,
            "purpose": self.purpose,
            "preferredProviderId": self.preferred_provider_id,
            "preferredModelId": self.preferred_model_id,
            "routeHint": self.route_hint,
            "constraints": self.constraints.to_wire(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DispatchMessage:
    role: str
    content: str | Sequence[Mapping[str, Any]]
    name: str | None = None
    tool_call_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        value: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            value["name"] = self.name
        if self.tool_call_id:
            value["toolCallId"] = self.tool_call_id
        return value


@dataclass(frozen=True, slots=True)
class ProviderDispatchRequest:
    dispatch_id: str
    route_id: str
    run_id: str
    task_id: str
    session_id: str
    turn_id: str
    route_fallback_policy: Literal[
        "allow_route_change", "pin_initial_route"
    ]
    messages: Sequence[DispatchMessage]
    node_id: str | None = None
    tools: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    maximum_output_tokens: int = 4_096
    temperature: float | None = None
    stream: bool = True
    timeout_milliseconds: int = 120_000
    chunk_timeout_milliseconds: int = 30_000
    idempotency_key: str = ""
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "dispatchId": self.dispatch_id,
            "routeId": self.route_id,
            "runId": self.run_id,
            "taskId": self.task_id,
            "nodeId": self.node_id,
            "sessionId": self.session_id,
            "turnId": self.turn_id,
            "messages": [item.to_wire() for item in self.messages],
            "routeFallbackPolicy": self.route_fallback_policy,
            "tools": [dict(item) for item in self.tools],
            "maximumOutputTokens": self.maximum_output_tokens,
            "temperature": self.temperature,
            "stream": self.stream,
            "timeoutMilliseconds": self.timeout_milliseconds,
            "chunkTimeoutMilliseconds": self.chunk_timeout_milliseconds,
            "idempotencyKey": self.idempotency_key or self.dispatch_id,
            "extraBody": dict(self.extra_body),
            "metadata": dict(self.metadata),
        }


def safe_dataclass(value: Any) -> dict[str, Any]:
    return asdict(value)
