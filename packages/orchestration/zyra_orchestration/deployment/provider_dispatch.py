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


DEEPSEEK_PROVIDER_ID = "deepseek"
DEEPSEEK_MODEL_ID = "deepseek-v4-pro"
DEEPSEEK_INTEGRATION_ID = "deepseek-bearer"
DEEPSEEK_CREDENTIAL_ID = "deepseek-physical-dispatch"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_CACHED_INPUT_USD_PER_MILLION = 0.003625
DEEPSEEK_INPUT_USD_PER_MILLION = 0.435
DEEPSEEK_OUTPUT_USD_PER_MILLION = 0.87
DEEPSEEK_PRICING_SOURCE = (
    "https://api-docs.deepseek.com/quick_start/pricing/"
    "?article_id=article_1779470751466_8"
)


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
            "cost_source": "provider_usage_x_versioned_catalog_pricing",
            "pricing_source_ref": DEEPSEEK_PRICING_SOURCE,
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
        selected_provider = provider_id.strip().casefold() or DEEPSEEK_PROVIDER_ID
        selected_model = model_id.strip() or DEEPSEEK_MODEL_ID
        if (
            selected_provider != DEEPSEEK_PROVIDER_ID
            or selected_model != DEEPSEEK_MODEL_ID
        ):
            raise DispatchRejected(
                "node_provider_profile_unsupported",
                "physical dispatch currently requires the frozen DeepSeek live profile",
                operation="provider_dispatch",
                profile="cloud",
                details={
                    "provider": selected_provider,
                    "model": selected_model,
                },
            )
        secret = str(self.environment.get(DEEPSEEK_API_KEY_ENV) or "").strip()
        if not secret:
            raise DispatchRejected(
                "node_provider_credential_missing",
                "physical cloud dispatch requires a real provider credential",
                operation="provider_dispatch",
                profile="cloud",
            )
        request_id = f"physical-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}"
        database_path = self.state_root / "provider.sqlite3"
        try:
            with ProviderControlPlaneClient(
                project_root=self.project_root,
                database_path=database_path,
                request_timeout_seconds=120.0,
            ) as client:
                credential = self._install_deepseek(client, secret)
                route = client.routing.acquire(
                    RouteRequest(
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        session_id=f"physical-session:{task_id}",
                        turn_id=request_id,
                        purpose="verify",
                        preferred_provider_id=DEEPSEEK_PROVIDER_ID,
                        preferred_model_id=DEEPSEEK_MODEL_ID,
                        route_hint=f"{DEEPSEEK_PROVIDER_ID}/{DEEPSEEK_MODEL_ID}",
                        constraints=RouteConstraints(
                            provider_ids=(DEEPSEEK_PROVIDER_ID,),
                            model_ids=(DEEPSEEK_MODEL_ID,),
                            required_input=("text",),
                            required_output=("text",),
                            require_tools=False,
                            require_streaming=True,
                            minimum_context_window=1_000,
                            maximum_input_price_per_million=1.0,
                            maximum_output_price_per_million=1.0,
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
                        maximum_output_tokens=32,
                        temperature=0,
                        stream=True,
                        timeout_milliseconds=90_000,
                        chunk_timeout_milliseconds=45_000,
                        idempotency_key=idempotency_key,
                        extra_body={"thinking": {"type": "disabled"}},
                        metadata={
                            "purpose": "p2-physical-dispatch-marker",
                            "payload_digest": payload_digest,
                        },
                    )
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
        cost_usd = round(
            (
                (prompt_tokens - cached_prompt_tokens)
                * DEEPSEEK_INPUT_USD_PER_MILLION
                + cached_prompt_tokens
                * DEEPSEEK_CACHED_INPUT_USD_PER_MILLION
                + completion_tokens * DEEPSEEK_OUTPUT_USD_PER_MILLION
            )
            / 1_000_000,
            10,
        )
        return LiveProviderDispatchEvidence(
            provider_id=str(result.get("providerId") or ""),
            model_id=str(result.get("modelId") or ""),
            request_id=str(result.get("dispatchId") or request_id),
            provider_attempt_id=str(attempt.get("attemptId") or ""),
            route_id=str(result.get("routeId") or route.get("routeId") or ""),
            protocol=str(result.get("protocol") or ""),
            endpoint_host="api.deepseek.com",
            endpoint_path="/chat/completions",
            http_status=http_status,
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
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
    def _install_deepseek(
        client: ProviderControlPlaneClient,
        secret: str,
    ) -> dict[str, Any]:
        client.integrations.upsert(
            IntegrationDefinition(
                integration_id=DEEPSEEK_INTEGRATION_ID,
                display_name="DeepSeek API bearer credential",
                kind="bearer",
                env_names=(DEEPSEEK_API_KEY_ENV,),
                authorization_scheme="Bearer",
                supports_refresh=False,
                metadata={"secret_custody": "environment-reference-only"},
            )
        )
        client.catalog.upsert_provider(
            ProviderDefinition(
                provider_id=DEEPSEEK_PROVIDER_ID,
                display_name="DeepSeek",
                integration_id=DEEPSEEK_INTEGRATION_ID,
                status="active",
                base_url="https://api.deepseek.com",
                protocol=ProviderProtocol.OPENAI_CHAT,
                allowed_hosts=("api.deepseek.com",),
                tags=(
                    "cloud",
                    "openai-compatible",
                    "real-provider",
                    "physical-dispatch",
                ),
                metadata={"profile_revision": "2026-07-27"},
            )
        )
        client.catalog.upsert_model(
            ModelDefinition(
                provider_id=DEEPSEEK_PROVIDER_ID,
                model_id=DEEPSEEK_MODEL_ID,
                display_name="DeepSeek V4 Pro",
                family="deepseek-v4",
                released_at=1_776_988_800_000,
                context_window=1_000_000,
                maximum_output_tokens=384_000,
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
                        "inputPerMillion": DEEPSEEK_INPUT_USD_PER_MILLION,
                        "outputPerMillion": DEEPSEEK_OUTPUT_USD_PER_MILLION,
                        "cachedInputPerMillion": (
                            DEEPSEEK_CACHED_INPUT_USD_PER_MILLION
                        ),
                        "currency": "USD",
                    },
                ),
                endpoint_path="/chat/completions",
                protocol=ProviderProtocol.OPENAI_CHAT,
                request_defaults={"thinking": {"type": "disabled"}},
                tags=("non-thinking-default", "physical-dispatch"),
                metadata={"pricing_checked_at": "2026-07-27"},
            )
        )
        fingerprint = "sha256:" + hashlib.sha256(secret.encode()).hexdigest()[:16]
        existing = next(
            (
                item
                for item in client.credentials.list(provider_id=DEEPSEEK_PROVIDER_ID)
                if item.get("credentialId") == DEEPSEEK_CREDENTIAL_ID
            ),
            None,
        )
        if existing is not None:
            if (
                existing.get("secretRef") != f"env://{DEEPSEEK_API_KEY_ENV}"
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
                credential_id=DEEPSEEK_CREDENTIAL_ID,
                integration_id=DEEPSEEK_INTEGRATION_ID,
                provider_id=DEEPSEEK_PROVIDER_ID,
                account_id="physical-dispatch",
                secret_ref=f"env://{DEEPSEEK_API_KEY_ENV}",
                fingerprint=fingerprint,
                priority=100,
                allowed_models=(DEEPSEEK_MODEL_ID,),
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
    "LiveProviderDispatchEvidence",
    "LiveProviderDispatchRuntime",
]
