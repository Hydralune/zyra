from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from zyra_runtime.provider_control_plane import (
    CredentialRegistration,
    DispatchMessage,
    IntegrationDefinition,
    ModelCapabilities,
    ModelDefinition,
    ProviderControlPlaneClient,
    ProviderControlPlanePortError,
    ProviderDefinition,
    ProviderDispatchRequest,
    ProviderProtocol,
    RouteConstraints,
    RouteRequest,
)

from .errors import DispatchRejected
from .models import digest


@dataclass(frozen=True, slots=True)
class LiveProviderProfile:
    provider_id: str
    model_id: str
    integration_id: str
    credential_id: str
    api_key_env: str
    provider_display_name: str
    model_display_name: str
    family: str
    base_url: str
    endpoint_host: str
    endpoint_path: str
    released_at_ms: int
    context_window: int
    maximum_output_tokens: int
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    pricing_currency: str
    pricing_source: str
    normalized_input_usd_per_million: float | None = None
    normalized_cached_input_usd_per_million: float | None = None
    normalized_output_usd_per_million: float | None = None
    normalized_pricing_source: str | None = None
    model_version: str | None = None


ZHIPU_PROVIDER_ID = "zhipu"
GLM_52_MODEL_ID = "glm-5.2"
ZAI_API_KEY_ENV = "ZAI_API_KEY"
KIMI_PROVIDER_ID = "kimi-platform"
KIMI_MODEL_ID = "kimi-k2.7-code"
KIMI_API_KEY_ENV = "KIMI_API_KEY"
DEEPSEEK_PROVIDER_ID = "deepseek"
DEEPSEEK_MODEL_ID = "deepseek-v4-flash"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"

PROVIDER_PRIORITY = (
    (ZHIPU_PROVIDER_ID, GLM_52_MODEL_ID),
    (DEEPSEEK_PROVIDER_ID, DEEPSEEK_MODEL_ID),
    (KIMI_PROVIDER_ID, KIMI_MODEL_ID),
)

_LIVE_PROFILES = {
    (ZHIPU_PROVIDER_ID, GLM_52_MODEL_ID): LiveProviderProfile(
        provider_id=ZHIPU_PROVIDER_ID,
        model_id=GLM_52_MODEL_ID,
        integration_id="zhipu-bearer",
        credential_id="zhipu-physical-dispatch",
        api_key_env=ZAI_API_KEY_ENV,
        provider_display_name="Zhipu AI",
        model_display_name="GLM-5.2",
        family="glm-5.2",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        endpoint_host="open.bigmodel.cn",
        endpoint_path="/chat/completions",
        released_at_ms=1_781_568_000_000,
        context_window=1_000_000,
        maximum_output_tokens=131_072,
        input_per_million=8,
        cached_input_per_million=2,
        output_per_million=28,
        pricing_currency="CNY",
        pricing_source="https://bigmodel.cn/pricing",
        normalized_input_usd_per_million=1.4,
        normalized_cached_input_usd_per_million=0.26,
        normalized_output_usd_per_million=4.4,
        normalized_pricing_source="https://docs.z.ai/guides/overview/pricing",
    ),
    (KIMI_PROVIDER_ID, KIMI_MODEL_ID): LiveProviderProfile(
        provider_id=KIMI_PROVIDER_ID,
        model_id=KIMI_MODEL_ID,
        integration_id="kimi-platform-bearer",
        credential_id="kimi-platform-physical-dispatch",
        api_key_env=KIMI_API_KEY_ENV,
        provider_display_name="Kimi Open Platform",
        model_display_name="Kimi K2.7 Code",
        family="kimi-k2.7-code",
        base_url="https://api.moonshot.cn/v1",
        endpoint_host="api.moonshot.cn",
        endpoint_path="/chat/completions",
        released_at_ms=1_781_222_400_000,
        context_window=262_144,
        maximum_output_tokens=131_072,
        input_per_million=6.5,
        cached_input_per_million=1.3,
        output_per_million=27,
        pricing_currency="CNY",
        pricing_source="https://platform.kimi.com/",
        # Admission uses a conservative upper bound instead of silently
        # treating non-USD usage as free: one CNY is charged as one USD.
        normalized_input_usd_per_million=6.5,
        normalized_cached_input_usd_per_million=1.3,
        normalized_output_usd_per_million=27,
        normalized_pricing_source="zyra://pricing/conservative-cny-as-usd-upper-bound",
    ),
    (DEEPSEEK_PROVIDER_ID, DEEPSEEK_MODEL_ID): LiveProviderProfile(
        provider_id=DEEPSEEK_PROVIDER_ID,
        model_id=DEEPSEEK_MODEL_ID,
        integration_id="deepseek-bearer",
        credential_id="deepseek-physical-dispatch",
        api_key_env=DEEPSEEK_API_KEY_ENV,
        provider_display_name="DeepSeek",
        model_display_name="DeepSeek V4 Flash 0731",
        family="deepseek-v4",
        base_url="https://api.deepseek.com",
        endpoint_host="api.deepseek.com",
        endpoint_path="/chat/completions",
        released_at_ms=1_785_456_000_000,
        context_window=1_000_000,
        maximum_output_tokens=384_000,
        input_per_million=0.14,
        cached_input_per_million=0.0028,
        output_per_million=0.28,
        pricing_currency="USD",
        pricing_source=(
            "https://api-docs.deepseek.com/quick_start/pricing/"
            "?article_id=article_1779470751466_8"
        ),
        normalized_input_usd_per_million=0.14,
        normalized_cached_input_usd_per_million=0.0028,
        normalized_output_usd_per_million=0.28,
        normalized_pricing_source=(
            "https://api-docs.deepseek.com/quick_start/pricing/"
            "?article_id=article_1779470751466_8"
        ),
        model_version="DeepSeek-V4-Flash-0731",
    ),
}


@dataclass(frozen=True, slots=True)
class LiveProviderDispatchEvidence:
    provider_id: str
    model_id: str
    request_id: str
    provider_attempt_id: str
    route_id: str
    protocol: str
    endpoint_host: str
    endpoint_path: str
    http_status: int
    prompt_tokens: int
    cached_prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    cost_amount: float
    cost_currency: str
    pricing_source_ref: str
    normalized_pricing_source_ref: str | None
    latency_ms: int
    request_digest: str
    response_digest: str
    payload_digest: str
    output_digest: str
    credential_ref: str
    credential_fingerprint: str
    started_at_ms: int
    completed_at_ms: int
    marker_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.live-provider-dispatch-evidence/v1",
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "request_id": self.request_id,
            "provider_attempt_id": self.provider_attempt_id,
            "route_id": self.route_id,
            "protocol": self.protocol,
            "endpoint_host": self.endpoint_host,
            "endpoint_path": self.endpoint_path,
            "http_status": self.http_status,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "cached_prompt_tokens": self.cached_prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            },
            "cost_usd": self.cost_usd,
            "cost_amount": self.cost_amount,
            "cost_currency": self.cost_currency,
            "cost_source": "provider_usage_x_versioned_catalog_pricing",
            "pricing_source_ref": self.pricing_source_ref,
            "normalized_pricing_source_ref": self.normalized_pricing_source_ref,
            "latency_ms": self.latency_ms,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
            "payload_digest": self.payload_digest,
            "output_digest": self.output_digest,
            "credential_ref": self.credential_ref,
            "credential_fingerprint": self.credential_fingerprint,
            "credential_material_persisted": False,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "marker_verified": self.marker_verified,
            "live": True,
            "external_model_request": True,
            "simulated": False,
            "semantic_only": False,
        }


class LiveProviderDispatchRuntime:
    """Thin deployment bridge to the existing TypeScript provider owner.

    The bridge installs only versioned catalog records and an ``env://`` secret
    reference. The TypeScript ProviderControlPlane remains the route, request,
    credential and transport state owner.
    """

    def __init__(
        self,
        *,
        project_root: str | Path,
        state_root: str | Path,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.environment = dict(os.environ if environment is None else environment)

    def dispatch_marker(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str,
        marker: str,
        provider_id: str,
        model_id: str,
        idempotency_key: str,
        payload_digest: str,
    ) -> LiveProviderDispatchEvidence:
        selected_provider = provider_id.strip().casefold() or ZHIPU_PROVIDER_ID
        selected_model = model_id.strip() or (
            GLM_52_MODEL_ID
            if selected_provider == ZHIPU_PROVIDER_ID
            else ""
        )
        requested_key = (selected_provider, selected_model)
        requested_profile = _LIVE_PROFILES.get(requested_key)
        if requested_profile is None:
            raise DispatchRejected(
                "node_provider_profile_unsupported",
                "physical dispatch requires a registered live provider profile",
                operation="provider_dispatch",
                profile="cloud",
                details={
                    "provider": selected_provider,
                    "model": selected_model,
                    "supported_priority": [
                        f"{provider}/{model}"
                        for provider, model in PROVIDER_PRIORITY
                    ],
                },
            )
        priority_offset = PROVIDER_PRIORITY.index(requested_key)
        candidates = [
            (
                _LIVE_PROFILES[key],
                str(
                    self.environment.get(_LIVE_PROFILES[key].api_key_env)
                    or ""
                ).strip(),
            )
            for key in PROVIDER_PRIORITY[priority_offset:]
        ]
        available = [
            (candidate, secret)
            for candidate, secret in candidates
            if secret
        ]
        if not available:
            raise DispatchRejected(
                "node_provider_credential_missing",
                (
                    "physical cloud dispatch requires a credential for the "
                    "requested provider or a lower-priority fallback"
                ),
                operation="provider_dispatch",
                profile="cloud",
                details={
                    "requested_provider": requested_profile.provider_id,
                    "requested_model": requested_profile.model_id,
                    "credential_priority": [
                        candidate.api_key_env
                        for candidate, _secret in candidates
                    ],
                },
            )
        preferred_profile = available[0][0]
        request_id = f"physical-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}"
        database_path = self.state_root / "provider.sqlite3"
        try:
            with ProviderControlPlaneClient(
                project_root=self.project_root,
                database_path=database_path,
                request_timeout_seconds=120.0,
            ) as client:
                for candidate, secret in available:
                    self._install_profile(client, candidate, secret)
                route = client.routing.acquire(
                    RouteRequest(
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        session_id=f"physical-session:{task_id}",
                        turn_id=request_id,
                        purpose="verify",
                        preferred_provider_id=preferred_profile.provider_id,
                        preferred_model_id=preferred_profile.model_id,
                        route_hint=(
                            f"{preferred_profile.provider_id}/"
                            f"{preferred_profile.model_id}"
                        ),
                        constraints=RouteConstraints(
                            provider_ids=tuple(
                                candidate.provider_id
                                for candidate, _secret in available
                            ),
                            model_ids=tuple(
                                candidate.model_id
                                for candidate, _secret in available
                            ),
                            required_input=("text",),
                            required_output=("text",),
                            require_tools=False,
                            require_streaming=True,
                            minimum_context_window=1_000,
                            maximum_input_price_per_million=None,
                            maximum_output_price_per_million=None,
                            required_scopes=("chat.completions",),
                        ),
                        metadata={
                            "evidence_class": "p2-physical-dispatch",
                            "payload_digest": payload_digest,
                            "secret_material_present": False,
                        },
                    )
                )
                result = client.dispatch(
                    ProviderDispatchRequest(
                        dispatch_id=request_id,
                        route_id=str(route.get("routeId") or ""),
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        session_id=f"physical-session:{task_id}",
                        turn_id=request_id,
                        messages=(
                            DispatchMessage(
                                role="user",
                                content=(
                                    "Return exactly the marker below and no other text.\n"
                                    f"{marker}"
                                ),
                            ),
                        ),
                        maximum_output_tokens=64,
                        temperature=None,
                        stream=True,
                        timeout_milliseconds=90_000,
                        chunk_timeout_milliseconds=45_000,
                        idempotency_key=idempotency_key,
                        metadata={
                            "purpose": "p2-physical-dispatch-marker",
                            "payload_digest": payload_digest,
                        },
                    )
                )
                profile = _LIVE_PROFILES.get(
                    (
                        str(result.get("providerId") or ""),
                        str(result.get("modelId") or ""),
                    )
                )
                if profile is None:
                    raise DispatchRejected(
                        "node_provider_result_profile_unknown",
                        "provider control plane returned an unregistered profile",
                        operation="provider_dispatch",
                        profile="cloud",
                    )
                route = client.routing.get(str(result.get("routeId") or ""))
                credential = client.credentials.get(
                    str(route.get("credentialId") or "")
                )
        except ProviderControlPlanePortError as error:
            raise DispatchRejected(
                "node_provider_request_failed",
                "the canonical provider control plane rejected the live request",
                operation="provider_dispatch",
                profile="cloud",
                retryable=True,
                details={"provider_error": error.code},
            ) from error

        attempts = [
            item for item in result.get("attempts") or () if isinstance(item, Mapping)
        ]
        attempt = attempts[-1] if attempts else {}
        http_status = int(attempt.get("httpStatus") or 0)
        response_digest = str(attempt.get("responseDigest") or "")
        if (
            not attempts
            or http_status < 200
            or http_status >= 300
            or not response_digest
        ):
            raise DispatchRejected(
                "node_provider_request_unverified",
                "the live provider request has no successful HTTP receipt",
                operation="provider_dispatch",
                profile="cloud",
                retryable=True,
                details={"http_status": http_status},
            )
        output = str(result.get("text") or "").strip()
        marker_verified = output == marker
        if not marker_verified:
            raise DispatchRejected(
                "node_provider_marker_mismatch",
                "the live provider output failed deterministic verification",
                operation="provider_dispatch",
                profile="cloud",
            )
        usage = result.get("usage")
        usage_map = usage if isinstance(usage, Mapping) else {}
        prompt_tokens = max(0, int(usage_map.get("prompt_tokens") or 0))
        completion_tokens = max(0, int(usage_map.get("completion_tokens") or 0))
        prompt_details = usage_map.get("prompt_tokens_details")
        prompt_details_map = (
            prompt_details if isinstance(prompt_details, Mapping) else {}
        )
        cached_prompt_tokens = max(
            0,
            min(
                prompt_tokens,
                int(
                    prompt_details_map.get("cached_tokens")
                    or usage_map.get("prompt_cache_hit_tokens")
                    or 0
                ),
            ),
        )
        total_tokens = max(
            prompt_tokens + completion_tokens,
            int(usage_map.get("total_tokens") or 0),
        )
        if total_tokens < 1:
            raise DispatchRejected(
                "node_provider_usage_missing",
                "the live provider request has no token usage receipt",
                operation="provider_dispatch",
                profile="cloud",
            )
        started_at_ms = int(attempt.get("startedAt") or 0)
        completed_at_ms = int(
            attempt.get("completedAt") or result.get("completedAt") or 0
        )
        latency_ms = max(0, completed_at_ms - started_at_ms)
        cost_amount = round(
            (
                (prompt_tokens - cached_prompt_tokens)
                * profile.input_per_million
                + cached_prompt_tokens
                * profile.cached_input_per_million
                + completion_tokens * profile.output_per_million
            )
            / 1_000_000,
            10,
        )
        if (
            profile.normalized_input_usd_per_million is not None
            and profile.normalized_cached_input_usd_per_million is not None
            and profile.normalized_output_usd_per_million is not None
        ):
            cost_usd = round(
                (
                    (prompt_tokens - cached_prompt_tokens)
                    * profile.normalized_input_usd_per_million
                    + cached_prompt_tokens
                    * profile.normalized_cached_input_usd_per_million
                    + completion_tokens
                    * profile.normalized_output_usd_per_million
                )
                / 1_000_000,
                10,
            )
        else:
            cost_usd = 0.0
        return LiveProviderDispatchEvidence(
            provider_id=str(result.get("providerId") or ""),
            model_id=str(result.get("modelId") or ""),
            request_id=str(result.get("dispatchId") or request_id),
            provider_attempt_id=str(attempt.get("attemptId") or ""),
            route_id=str(result.get("routeId") or route.get("routeId") or ""),
            protocol=str(result.get("protocol") or ""),
            endpoint_host=profile.endpoint_host,
            endpoint_path=profile.endpoint_path,
            http_status=http_status,
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            cost_amount=cost_amount,
            cost_currency=profile.pricing_currency,
            pricing_source_ref=profile.pricing_source,
            normalized_pricing_source_ref=profile.normalized_pricing_source,
            latency_ms=latency_ms,
            request_digest=str(attempt.get("requestDigest") or ""),
            response_digest=response_digest,
            payload_digest=payload_digest,
            output_digest=digest(output),
            credential_ref=str(credential.get("secretRef") or ""),
            credential_fingerprint=str(credential.get("fingerprint") or ""),
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            marker_verified=marker_verified,
        )

    @staticmethod
    def _install_profile(
        client: ProviderControlPlaneClient,
        profile: LiveProviderProfile,
        secret: str,
    ) -> dict[str, Any]:
        client.integrations.upsert(
            IntegrationDefinition(
                integration_id=profile.integration_id,
                display_name=f"{profile.provider_display_name} API bearer credential",
                kind="bearer",
                env_names=(profile.api_key_env,),
                authorization_scheme="Bearer",
                supports_refresh=False,
                metadata={"secret_custody": "environment-reference-only"},
            )
        )
        client.catalog.upsert_provider(
            ProviderDefinition(
                provider_id=profile.provider_id,
                display_name=profile.provider_display_name,
                integration_id=profile.integration_id,
                status="active",
                base_url=profile.base_url,
                protocol=ProviderProtocol.OPENAI_CHAT,
                allowed_hosts=(profile.endpoint_host,),
                tags=(
                    "cloud",
                    "openai-compatible",
                    "real-provider",
                    "physical-dispatch",
                ),
                metadata={
                    "profile_revision": "2026-07-31",
                    "routing_priority": (
                        len(PROVIDER_PRIORITY)
                        - PROVIDER_PRIORITY.index(
                            (profile.provider_id, profile.model_id)
                        )
                    )
                    * 100,
                },
            )
        )
        client.catalog.upsert_model(
            ModelDefinition(
                provider_id=profile.provider_id,
                model_id=profile.model_id,
                display_name=profile.model_display_name,
                family=profile.family,
                released_at=profile.released_at_ms,
                context_window=profile.context_window,
                maximum_output_tokens=profile.maximum_output_tokens,
                capabilities=ModelCapabilities(
                    input=("text",),
                    output=("text", "tool"),
                    tools=True,
                    streaming=True,
                    reasoning=True,
                    structured_output=True,
                ),
                pricing=(
                    {
                        "inputPerMillion": profile.input_per_million,
                        "outputPerMillion": profile.output_per_million,
                        "cachedInputPerMillion": profile.cached_input_per_million,
                        "currency": profile.pricing_currency,
                    },
                ),
                endpoint_path=profile.endpoint_path,
                protocol=ProviderProtocol.OPENAI_CHAT,
                request_defaults=(
                    {
                        "thinking": {"type": "enabled"},
                        "reasoning_effort": "max",
                    }
                    if profile.provider_id == ZHIPU_PROVIDER_ID
                    else {
                        "thinking": {
                            "type": (
                                "enabled"
                                if profile.provider_id == KIMI_PROVIDER_ID
                                else "disabled"
                            )
                        }
                    }
                ),
                tags=("physical-dispatch",),
                metadata={
                    "pricing_checked_at": "2026-07-31",
                    "pricing_reference": profile.pricing_source,
                    **(
                        {"model_version": profile.model_version}
                        if profile.model_version is not None
                        else {}
                    ),
                },
            )
        )
        fingerprint = "sha256:" + hashlib.sha256(secret.encode()).hexdigest()[:16]
        existing = next(
            (
                item
                for item in client.credentials.list(provider_id=profile.provider_id)
                if item.get("credentialId") == profile.credential_id
            ),
            None,
        )
        if existing is not None:
            if (
                existing.get("secretRef") != f"env://{profile.api_key_env}"
                or existing.get("fingerprint") != fingerprint
            ):
                raise DispatchRejected(
                    "node_provider_credential_state_conflict",
                    "provider credential reference changed within the physical runtime",
                    operation="provider_dispatch",
                    profile="cloud",
                )
            return dict(existing)
        return client.credentials.register(
            CredentialRegistration(
                credential_id=profile.credential_id,
                integration_id=profile.integration_id,
                provider_id=profile.provider_id,
                account_id="physical-dispatch",
                secret_ref=f"env://{profile.api_key_env}",
                fingerprint=fingerprint,
                priority=100,
                allowed_models=(profile.model_id,),
                scopes=("chat.completions",),
                metadata={
                    "purpose": "p2-physical-dispatch",
                    "secret_material_persisted": False,
                },
            )
        )


__all__ = [
    "DEEPSEEK_API_KEY_ENV",
    "DEEPSEEK_MODEL_ID",
    "DEEPSEEK_PROVIDER_ID",
    "GLM_52_MODEL_ID",
    "KIMI_API_KEY_ENV",
    "KIMI_MODEL_ID",
    "KIMI_PROVIDER_ID",
    "LiveProviderDispatchEvidence",
    "LiveProviderProfile",
    "LiveProviderDispatchRuntime",
    "PROVIDER_PRIORITY",
    "ZAI_API_KEY_ENV",
    "ZHIPU_PROVIDER_ID",
]
