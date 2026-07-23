from __future__ import annotations

import hashlib
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity, utc_now
from .integration_contracts import endpoint_identity, parse_timestamp, redact_mapping, stable_digest


class LiveTier(StrEnum):
    LOCAL = "local"
    EDGE = "edge"
    CLOUD = "cloud"


class WireDialect(StrEnum):
    OPENAI_COMPATIBLE = "openai-compatible"
    ANTHROPIC_COMPATIBLE = "anthropic-compatible"


def _endpoint_from_mapping(value: Any) -> str:
    """Recover a credential-free endpoint from its persisted identity."""

    if not isinstance(value, Mapping):
        return str(value or "")
    scheme = str(value.get("scheme") or "").lower()
    host = str(value.get("host") or "").lower()
    if not scheme or not host:
        return ""
    raw_port = value.get("port")
    port = f":{int(raw_port)}" if isinstance(raw_port, int) and raw_port > 0 else ""
    return f"{scheme}://{host}{port}/"


@dataclass(frozen=True, slots=True)
class EndpointAttestation:
    tier: LiveTier
    endpoint: str
    endpoint_id: str
    runtime_id: str
    process_id: str
    host_id: str
    isolation_id: str
    request_id: str
    route_id: str
    lease_id: str
    artifact_ids: tuple[str, ...]
    started_at: str
    completed_at: str
    request_digest: str
    response_digest: str
    handshake_ok: bool
    heartbeat_ok: bool
    task_success: bool
    protocol: str
    transport: str
    simulated: bool = False
    loopback: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EndpointAttestation":
        tier = _enum(LiveTier, value.get("tier"), LiveTier.LOCAL)
        endpoint = _endpoint_from_mapping(value.get("endpoint") or value.get("url"))
        identity = endpoint_identity(endpoint) if endpoint else {}
        artifacts = value.get("artifact_ids") or value.get("artifacts") or ()
        return cls(
            tier=tier,
            endpoint=endpoint,
            endpoint_id=str(value.get("endpoint_id") or ""),
            runtime_id=str(value.get("runtime_id") or value.get("worker_id") or ""),
            process_id=str(value.get("process_id") or value.get("pid") or ""),
            host_id=str(value.get("host_id") or value.get("host") or ""),
            isolation_id=str(value.get("isolation_id") or value.get("sandbox_id") or ""),
            request_id=str(value.get("request_id") or value.get("correlation_id") or ""),
            route_id=str(value.get("route_id") or ""),
            lease_id=str(value.get("lease_id") or ""),
            artifact_ids=tuple(str(item) for item in artifacts if str(item)),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            request_digest=str(value.get("request_digest") or ""),
            response_digest=str(value.get("response_digest") or ""),
            handshake_ok=value.get("handshake_ok") is True,
            heartbeat_ok=value.get("heartbeat_ok") is True,
            task_success=value.get("task_success") is True or value.get("ok") is True,
            protocol=str(value.get("protocol") or ""),
            transport=str(value.get("transport") or ""),
            simulated=value.get("simulated") is True or str(value.get("maturity") or "").lower() in {"simulated", "mock"},
            loopback=value.get("loopback") is True or bool(identity.get("loopback")),
            metadata=dict(value.get("metadata") or {}) if isinstance(value.get("metadata"), Mapping) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "endpoint": endpoint_identity(self.endpoint),
            "endpoint_id": self.endpoint_id,
            "runtime_id": self.runtime_id,
            "process_id": self.process_id,
            "host_id": self.host_id,
            "isolation_id": self.isolation_id,
            "request_id": self.request_id,
            "route_id": self.route_id,
            "lease_id": self.lease_id,
            "artifact_ids": list(self.artifact_ids),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
            "handshake_ok": self.handshake_ok,
            "heartbeat_ok": self.heartbeat_ok,
            "task_success": self.task_success,
            "protocol": self.protocol,
            "transport": self.transport,
            "simulated": self.simulated,
            "loopback": self.loopback,
            "metadata": redact_mapping(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class StreamChunkAttestation:
    sequence: int
    event: str
    content_digest: str
    byte_count: int
    timestamp: str
    tool_call_id: str = ""
    finish_reason: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], index: int) -> "StreamChunkAttestation":
        content = value.get("content") if "content" in value else value.get("delta")
        digest = str(value.get("content_digest") or "")
        if not digest and content is not None:
            digest = stable_digest(content)
        return cls(
            sequence=_integer(value.get("sequence"), index),
            event=str(value.get("event") or value.get("type") or "chunk"),
            content_digest=digest,
            byte_count=_integer(value.get("byte_count"), len(str(content or "").encode("utf-8"))),
            timestamp=str(value.get("timestamp") or value.get("created_at") or ""),
            tool_call_id=str(value.get("tool_call_id") or value.get("tool_use_id") or ""),
            finish_reason=str(value.get("finish_reason") or value.get("stop_reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class ProviderWireAttestation:
    provider_id: str
    model_id: str
    dialect: WireDialect
    endpoint: str
    request_id: str
    attempt_id: str
    route_id: str
    request_method: str
    request_path: str
    request_headers: Mapping[str, Any]
    request_digest: str
    response_status: int
    response_headers: Mapping[str, Any]
    response_digest: str
    chunks: tuple[StreamChunkAttestation, ...]
    tool_call_ids: tuple[str, ...]
    tool_result_ids: tuple[str, ...]
    started_at: str
    completed_at: str
    retry_count: int
    fallback_from: str = ""
    simulated: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProviderWireAttestation":
        chunks_value = value.get("chunks") or value.get("stream_chunks") or ()
        chunks = tuple(
            StreamChunkAttestation.from_mapping(item, index)
            for index, item in enumerate(chunks_value)
            if isinstance(item, Mapping)
        )
        return cls(
            provider_id=str(value.get("provider_id") or value.get("provider") or ""),
            model_id=str(value.get("model_id") or value.get("model") or ""),
            dialect=_enum(WireDialect, value.get("dialect"), WireDialect.OPENAI_COMPATIBLE),
            endpoint=_endpoint_from_mapping(value.get("endpoint") or value.get("url")),
            request_id=str(value.get("request_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            route_id=str(value.get("route_id") or ""),
            request_method=str(value.get("request_method") or "POST").upper(),
            request_path=str(value.get("request_path") or ""),
            request_headers=dict(value.get("request_headers") or {}) if isinstance(value.get("request_headers"), Mapping) else {},
            request_digest=str(value.get("request_digest") or ""),
            response_status=_integer(value.get("response_status"), 0),
            response_headers=dict(value.get("response_headers") or {}) if isinstance(value.get("response_headers"), Mapping) else {},
            response_digest=str(value.get("response_digest") or ""),
            chunks=chunks,
            tool_call_ids=tuple(str(item) for item in value.get("tool_call_ids") or () if str(item)),
            tool_result_ids=tuple(str(item) for item in value.get("tool_result_ids") or () if str(item)),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            retry_count=_integer(value.get("retry_count"), 0),
            fallback_from=str(value.get("fallback_from") or ""),
            simulated=value.get("simulated") is True or str(value.get("maturity") or "").lower() in {"simulated", "mock"},
            metadata=dict(value.get("metadata") or {}) if isinstance(value.get("metadata"), Mapping) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "dialect": self.dialect.value,
            "endpoint": endpoint_identity(self.endpoint),
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "route_id": self.route_id,
            "request_method": self.request_method,
            "request_path": self.request_path,
            "request_headers": _safe_headers(self.request_headers),
            "request_digest": self.request_digest,
            "response_status": self.response_status,
            "response_headers": _safe_headers(self.response_headers),
            "response_digest": self.response_digest,
            "chunks": [
                {
                    "sequence": item.sequence,
                    "event": item.event,
                    "content_digest": item.content_digest,
                    "byte_count": item.byte_count,
                    "timestamp": item.timestamp,
                    "tool_call_id": item.tool_call_id,
                    "finish_reason": item.finish_reason,
                }
                for item in self.chunks
            ],
            "tool_call_ids": list(self.tool_call_ids),
            "tool_result_ids": list(self.tool_result_ids),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "retry_count": self.retry_count,
            "fallback_from": self.fallback_from,
            "simulated": self.simulated,
            "metadata": redact_mapping(self.metadata),
        }


class EndpointEvidenceGate:
    def evaluate(
        self,
        observations: Sequence[EndpointAttestation | Mapping[str, Any]],
        *,
        final_completion: bool,
    ) -> GateResult:
        values = tuple(
            item if isinstance(item, EndpointAttestation) else EndpointAttestation.from_mapping(item)
            for item in observations
        )
        result = GateResult(
            gate_id="m1-live-execution-tiers",
            status=GateStatus.NOT_RUN,
            summary="Real local, independent edge and cloud execution attestation.",
        )
        by_tier: dict[LiveTier, list[EndpointAttestation]] = defaultdict(list)
        for observation in values:
            by_tier[observation.tier].append(observation)
            self._validate_one(observation, result, final_completion=final_completion)
            result.evidence.append(
                EvidencePointer(
                    kind="live_execution_tier",
                    location=observation.endpoint_id or observation.tier.value,
                    summary=f"{observation.tier.value}: {'real success' if observation.task_success else 'incomplete'}",
                    causation_id=observation.request_id,
                    metadata={
                        "runtime_id": observation.runtime_id,
                        "route_id": observation.route_id,
                        "response_digest": observation.response_digest,
                    },
                )
            )
        required = set(LiveTier)
        observed = set(by_tier)
        if final_completion:
            for tier in sorted(required - observed, key=lambda item: item.value):
                result.add(
                    Finding(
                        code="live.tier_missing",
                        severity=Severity.BLOCKER,
                        summary="Final M1 evidence lacks a required real execution tier.",
                        detail=tier.value,
                    )
                )
        self._validate_independence(by_tier, result, final_completion=final_completion)
        result.metrics.update(
            {
                "observation_count": len(values),
                "tier_counts": {tier.value: len(items) for tier, items in sorted(by_tier.items(), key=lambda pair: pair[0].value)},
                "successful_tiers": sorted(
                    tier.value for tier, items in by_tier.items() if any(item.task_success for item in items)
                ),
                "attestations": [item.to_dict() for item in values],
            }
        )
        return result.finish(default_partial=not final_completion and observed != required)

    @staticmethod
    def _validate_one(
        observation: EndpointAttestation,
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        location = observation.endpoint_id or observation.endpoint
        required_identity = {
            "endpoint_id": observation.endpoint_id,
            "runtime_id": observation.runtime_id,
            "process_id": observation.process_id,
            "host_id": observation.host_id,
            "request_id": observation.request_id,
            "route_id": observation.route_id,
            "lease_id": observation.lease_id,
        }
        for name, value in required_identity.items():
            if not value:
                result.add(
                    Finding(
                        code="live.identity_missing",
                        severity=Severity.BLOCKER if final_completion else Severity.ERROR,
                        summary="Execution-tier attestation lacks canonical identity.",
                        detail=name,
                        location=location,
                    )
                )
        if observation.simulated:
            result.add(
                Finding(
                    code="live.simulation_not_real",
                    severity=Severity.BLOCKER,
                    summary="Simulated execution cannot close a real tier gate.",
                    detail=observation.tier.value,
                    location=location,
                )
            )
        if observation.tier in {LiveTier.EDGE, LiveTier.CLOUD} and observation.loopback:
            result.add(
                Finding(
                    code="live.remote_tier_loopback",
                    severity=Severity.BLOCKER,
                    summary="Loopback endpoint cannot prove independent edge/cloud execution.",
                    detail=observation.endpoint,
                    location=location,
                )
            )
        if observation.tier is LiveTier.EDGE and not observation.isolation_id:
            result.add(
                Finding(
                    code="live.edge_isolation_missing",
                    severity=Severity.BLOCKER,
                    summary="Edge execution lacks an independent isolation identity.",
                    location=location,
                )
            )
        if not observation.handshake_ok or not observation.heartbeat_ok:
            result.add(
                Finding(
                    code="live.endpoint_liveness_incomplete",
                    severity=Severity.BLOCKER,
                    summary="Execution tier lacks real handshake or heartbeat evidence.",
                    detail=f"handshake={observation.handshake_ok}; heartbeat={observation.heartbeat_ok}",
                    location=location,
                )
            )
        if not observation.task_success or not observation.artifact_ids:
            result.add(
                Finding(
                    code="live.task_artifact_missing",
                    severity=Severity.BLOCKER,
                    summary="Execution tier did not complete a real task with a canonical artifact.",
                    location=location,
                )
            )
        if not _digest(observation.request_digest) or not _digest(observation.response_digest):
            result.add(
                Finding(
                    code="live.payload_digest_missing",
                    severity=Severity.BLOCKER,
                    summary="Execution tier lacks request/response evidence digests.",
                    location=location,
                )
            )
        started = parse_timestamp(observation.started_at)
        completed = parse_timestamp(observation.completed_at)
        if started is None or completed is None or completed < started:
            result.add(
                Finding(
                    code="live.time_window_invalid",
                    severity=Severity.ERROR,
                    summary="Execution-tier attestation has an invalid time window.",
                    location=location,
                )
            )

    @staticmethod
    def _validate_independence(
        by_tier: Mapping[LiveTier, Sequence[EndpointAttestation]],
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        if not final_completion:
            return
        selected = {
            tier: next((item for item in values if item.task_success), values[0] if values else None)
            for tier, values in by_tier.items()
        }
        local = selected.get(LiveTier.LOCAL)
        edge = selected.get(LiveTier.EDGE)
        cloud = selected.get(LiveTier.CLOUD)
        if local and edge:
            collisions = [
                name
                for name, left, right in (
                    ("endpoint_id", local.endpoint_id, edge.endpoint_id),
                    ("runtime_id", local.runtime_id, edge.runtime_id),
                    ("process_id", local.process_id, edge.process_id),
                    ("isolation_id", local.isolation_id, edge.isolation_id),
                )
                if left and left == right
            ]
            if collisions:
                result.add(
                    Finding(
                        code="live.edge_not_independent",
                        severity=Severity.BLOCKER,
                        summary="Edge evidence reuses local runtime/process identity.",
                        detail=", ".join(collisions),
                    )
                )
        if cloud and edge and cloud.endpoint_id == edge.endpoint_id:
            result.add(
                Finding(
                    code="live.cloud_edge_endpoint_collapsed",
                    severity=Severity.BLOCKER,
                    summary="Cloud and edge attestations resolve to the same endpoint identity.",
                )
            )


class ProviderWireEvidenceGate:
    def evaluate(
        self,
        observations: Sequence[ProviderWireAttestation | Mapping[str, Any]],
        *,
        final_completion: bool,
    ) -> GateResult:
        values = tuple(
            item if isinstance(item, ProviderWireAttestation) else ProviderWireAttestation.from_mapping(item)
            for item in observations
        )
        result = GateResult(
            gate_id="m1-live-provider-wires",
            status=GateStatus.NOT_RUN,
            summary="Two real provider wire dialects with stream and tool-result evidence.",
        )
        dialects: set[WireDialect] = set()
        providers: set[str] = set()
        models: set[str] = set()
        attempts: set[str] = set()
        for observation in values:
            dialects.add(observation.dialect)
            if observation.provider_id:
                providers.add(observation.provider_id)
            if observation.model_id:
                models.add(observation.model_id)
            if observation.attempt_id:
                if observation.attempt_id in attempts:
                    result.add(
                        Finding(
                            code="provider.attempt_reused",
                            severity=Severity.BLOCKER,
                            summary="Provider evidence reuses one attempt identity.",
                            detail=observation.attempt_id,
                        )
                    )
                attempts.add(observation.attempt_id)
            self._validate_one(observation, result, final_completion=final_completion)
            result.evidence.append(
                EvidencePointer(
                    kind="provider_wire",
                    location=observation.provider_id or observation.endpoint,
                    summary=f"{observation.dialect.value}/{observation.model_id}",
                    causation_id=observation.request_id,
                    metadata={
                        "attempt_id": observation.attempt_id,
                        "response_digest": observation.response_digest,
                        "chunk_count": len(observation.chunks),
                    },
                )
            )
        required = set(WireDialect)
        if final_completion:
            for dialect in sorted(required - dialects, key=lambda item: item.value):
                result.add(
                    Finding(
                        code="provider.dialect_missing",
                        severity=Severity.BLOCKER,
                        summary="Final M1 evidence lacks a real required provider wire dialect.",
                        detail=dialect.value,
                    )
                )
            if len(providers) < 2:
                result.add(
                    Finding(
                        code="provider.distinct_provider_missing",
                        severity=Severity.BLOCKER,
                        summary="Final M1 evidence does not use two distinct real providers.",
                        detail=", ".join(sorted(providers)),
                    )
                )
            if len(models) < 2:
                result.add(
                    Finding(
                        code="provider.multi_model_missing",
                        severity=Severity.BLOCKER,
                        summary="Final M1 evidence does not use at least two distinct models.",
                        detail=", ".join(sorted(models)),
                    )
                )
        result.metrics.update(
            {
                "observation_count": len(values),
                "dialects": sorted(item.value for item in dialects),
                "providers": sorted(providers),
                "models": sorted(models),
                "stream_chunk_count": sum(len(item.chunks) for item in values),
                "tool_call_count": sum(len(item.tool_call_ids) for item in values),
                "tool_result_count": sum(len(item.tool_result_ids) for item in values),
                "retry_count": sum(item.retry_count for item in values),
                "attestations": [item.to_dict() for item in values],
            }
        )
        return result.finish(default_partial=not final_completion and dialects != required)

    def _validate_one(
        self,
        observation: ProviderWireAttestation,
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        location = observation.provider_id or observation.endpoint
        identity = endpoint_identity(observation.endpoint) if observation.endpoint else {}
        if observation.simulated:
            result.add(
                Finding(
                    code="provider.simulated_wire",
                    severity=Severity.BLOCKER,
                    summary="Simulated provider traffic cannot close the live wire gate.",
                    location=location,
                )
            )
        if identity.get("loopback"):
            result.add(
                Finding(
                    code="provider.loopback_wire",
                    severity=Severity.BLOCKER,
                    summary="Loopback provider endpoint cannot count as a real cloud wire.",
                    location=location,
                )
            )
        if identity.get("credential_in_url"):
            result.add(
                Finding(
                    code="provider.credential_in_endpoint",
                    severity=Severity.BLOCKER,
                    summary="Provider endpoint embeds credentials and cannot be persisted safely.",
                    location=location,
                )
            )
        for name, value in (
            ("provider_id", observation.provider_id),
            ("model_id", observation.model_id),
            ("request_id", observation.request_id),
            ("attempt_id", observation.attempt_id),
            ("route_id", observation.route_id),
        ):
            if not value:
                result.add(
                    Finding(
                        code="provider.identity_missing",
                        severity=Severity.BLOCKER if final_completion else Severity.ERROR,
                        summary="Provider wire attestation lacks canonical identity.",
                        detail=name,
                        location=location,
                    )
                )
        if observation.request_method != "POST":
            result.add(
                Finding(
                    code="provider.request_method_invalid",
                    severity=Severity.ERROR,
                    summary="Provider generation wire did not use a POST request.",
                    detail=observation.request_method,
                    location=location,
                )
            )
        managed_cli = (
            str(observation.metadata.get("transport_kind") or "")
            == "provider_owned_managed_cli"
        )
        if managed_cli:
            self._validate_managed_cli(observation, result)
        self._validate_dialect_shape(
            observation,
            result,
            managed_cli=managed_cli,
        )
        if observation.response_status < 200 or observation.response_status >= 300:
            result.add(
                Finding(
                    code="provider.response_failed",
                    severity=Severity.BLOCKER,
                    summary="Provider wire did not return a successful real response.",
                    detail=str(observation.response_status),
                    location=location,
                )
            )
        if not _digest(observation.request_digest) or not _digest(observation.response_digest):
            result.add(
                Finding(
                    code="provider.digest_missing",
                    severity=Severity.BLOCKER,
                    summary="Provider wire lacks cryptographic request/response evidence.",
                    location=location,
                )
            )
        self._validate_stream(observation, result)
        if not observation.tool_call_ids or not observation.tool_result_ids:
            result.add(
                Finding(
                    code="provider.tool_roundtrip_missing",
                    severity=Severity.BLOCKER,
                    summary="Provider evidence lacks a real tool-call/tool-result round trip.",
                    location=location,
                )
            )
        unmatched = set(observation.tool_call_ids) - set(observation.tool_result_ids)
        if unmatched:
            result.add(
                Finding(
                    code="provider.tool_result_unmatched",
                    severity=Severity.BLOCKER,
                    summary="Provider tool calls lack matching result identities.",
                    detail=", ".join(sorted(unmatched)),
                    location=location,
                )
            )
        started = parse_timestamp(observation.started_at)
        completed = parse_timestamp(observation.completed_at)
        if started is None or completed is None or completed < started:
            result.add(
                Finding(
                    code="provider.time_window_invalid",
                    severity=Severity.ERROR,
                    summary="Provider attestation time window is invalid.",
                    location=location,
                )
            )

    @staticmethod
    def _validate_managed_cli(
        observation: ProviderWireAttestation,
        result: GateResult,
    ) -> None:
        metadata = observation.metadata
        cli_kind = str(metadata.get("cli_kind") or "")
        expected_cli = (
            "codex"
            if observation.dialect is WireDialect.OPENAI_COMPATIBLE
            else "claude"
        )
        checks = {
            "authenticated_session": metadata.get("authenticated_session") is True,
            "auth_custodian": metadata.get("auth_custodian") == "provider_cli",
            "status_source": metadata.get("status_source") == "provider_cli_success_exit",
            "command_id": bool(str(metadata.get("command_id") or "")),
            "cli_version": bool(str(metadata.get("cli_version") or "")),
            "cli_kind": cli_kind == expected_cli,
        }
        failed = sorted(name for name, passed in checks.items() if not passed)
        if failed:
            result.add(
                Finding(
                    code="provider.managed_cli_custody_invalid",
                    severity=Severity.BLOCKER,
                    summary="Managed provider evidence lacks provider-owned credential and process custody.",
                    detail=", ".join(failed),
                    location=observation.provider_id,
                )
            )
        if metadata.get("direct_wire_headers_observed") is not False:
            result.add(
                Finding(
                    code="provider.managed_cli_header_claim_invalid",
                    severity=Severity.ERROR,
                    summary="Managed CLI evidence must not claim unobserved authorization headers.",
                    location=observation.provider_id,
                )
            )

    @staticmethod
    def _validate_dialect_shape(
        observation: ProviderWireAttestation,
        result: GateResult,
        *,
        managed_cli: bool = False,
    ) -> None:
        path = observation.request_path.lower()
        headers = {str(key).lower(): str(value).lower() for key, value in observation.request_headers.items()}
        if observation.dialect is WireDialect.OPENAI_COMPATIBLE:
            path_ok = any(token in path for token in ("/chat/completions", "/responses"))
            auth_ok = managed_cli or "authorization" in headers
            if not path_ok or not auth_ok:
                result.add(
                    Finding(
                        code="provider.openai_wire_shape_invalid",
                        severity=Severity.BLOCKER,
                        summary="OpenAI-compatible evidence lacks its real request path/auth shape.",
                        detail=f"path_ok={path_ok}; auth_header={auth_ok}",
                        location=observation.provider_id,
                    )
                )
        if observation.dialect is WireDialect.ANTHROPIC_COMPATIBLE:
            path_ok = "/messages" in path
            auth_ok = managed_cli or (
                "x-api-key" in headers and "anthropic-version" in headers
            )
            if not path_ok or not auth_ok:
                result.add(
                    Finding(
                        code="provider.anthropic_wire_shape_invalid",
                        severity=Severity.BLOCKER,
                        summary="Anthropic-compatible evidence lacks its real request path/header shape.",
                        detail=f"path_ok={path_ok}; required_headers={auth_ok}",
                        location=observation.provider_id,
                    )
                )

    @staticmethod
    def _validate_stream(observation: ProviderWireAttestation, result: GateResult) -> None:
        if len(observation.chunks) < 2:
            result.add(
                Finding(
                    code="provider.stream_missing",
                    severity=Severity.BLOCKER,
                    summary="Provider evidence has no multi-chunk stream.",
                    location=observation.provider_id,
                )
            )
            return
        sequences = [item.sequence for item in observation.chunks]
        if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
            result.add(
                Finding(
                    code="provider.stream_sequence_invalid",
                    severity=Severity.BLOCKER,
                    summary="Provider stream chunks are duplicated or out of canonical order.",
                    detail=repr(sequences[:20]),
                    location=observation.provider_id,
                )
            )
        if any(not _digest(item.content_digest) for item in observation.chunks):
            result.add(
                Finding(
                    code="provider.stream_chunk_digest_missing",
                    severity=Severity.ERROR,
                    summary="Provider stream chunk lacks content digest.",
                    location=observation.provider_id,
                )
            )
        if not any(item.finish_reason for item in observation.chunks):
            result.add(
                Finding(
                    code="provider.stream_terminal_missing",
                    severity=Severity.BLOCKER,
                    summary="Provider stream lacks a terminal finish/stop reason.",
                    location=observation.provider_id,
                )
            )


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    endpoint: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 10.0
    maximum_response_bytes: int = 1_048_576


class LiveEndpointProbe:
    def execute(self, request: ProbeRequest) -> Mapping[str, Any]:
        identity = endpoint_identity(request.endpoint)
        if identity.get("credential_in_url"):
            raise ValueError("endpoint URL must not embed credentials")
        if request.timeout_seconds <= 0:
            raise ValueError("endpoint probe timeout must be positive")
        if request.maximum_response_bytes <= 0:
            raise ValueError("maximum response size must be positive")
        body = b""
        if request.payload:
            body = json.dumps(request.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {str(key): str(value) for key, value in request.headers.items()}
        if body:
            headers.setdefault("Content-Type", "application/json")
        wire_request = urllib.request.Request(
            request.endpoint,
            data=body or None,
            headers=headers,
            method=request.method.upper(),
        )
        started_at = utc_now()
        started = time.monotonic()
        status = 0
        response_headers: dict[str, str] = {}
        response_body = b""
        error_code = ""
        error_message = ""
        try:
            with urllib.request.urlopen(wire_request, timeout=request.timeout_seconds) as response:
                status = int(response.status)
                response_headers = {str(key): str(value) for key, value in response.headers.items()}
                response_body = response.read(request.maximum_response_bytes + 1)
                if len(response_body) > request.maximum_response_bytes:
                    raise ValueError("endpoint response exceeds configured evidence bound")
        except urllib.error.HTTPError as error:
            status = int(error.code)
            response_headers = {str(key): str(value) for key, value in error.headers.items()}
            response_body = error.read(request.maximum_response_bytes)
            error_code = "http_error"
            error_message = str(error)
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError) as error:
            error_code = type(error).__name__
            error_message = str(error)
        completed_at = utc_now()
        return {
            "ok": 200 <= status < 300 and not error_code,
            "endpoint": identity,
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "request_method": request.method.upper(),
            "request_headers": _safe_headers(headers),
            "request_digest": hashlib.sha256(body).hexdigest(),
            "response_headers": _safe_headers(response_headers),
            "response_digest": hashlib.sha256(response_body).hexdigest(),
            "response_bytes": len(response_body),
            "error_code": error_code,
            "error_message": error_message,
        }


class LiveEvidenceSuite:
    def __init__(self) -> None:
        self.tiers = EndpointEvidenceGate()
        self.providers = ProviderWireEvidenceGate()

    def evaluate(
        self,
        *,
        tier_observations: Sequence[EndpointAttestation | Mapping[str, Any]],
        provider_observations: Sequence[ProviderWireAttestation | Mapping[str, Any]],
        final_completion: bool,
    ) -> tuple[GateResult, GateResult, GateResult]:
        tier_gate = self.tiers.evaluate(tier_observations, final_completion=final_completion)
        provider_gate = self.providers.evaluate(provider_observations, final_completion=final_completion)
        aggregate = GateResult(
            gate_id="m1-live-evidence",
            status=GateStatus.NOT_RUN,
            summary="M1 real tier and provider wire evidence aggregate.",
        )
        for gate in (tier_gate, provider_gate):
            for finding in gate.findings:
                if finding.severity.failing:
                    aggregate.add(
                        Finding(
                            code=f"live.child.{finding.code}",
                            severity=finding.severity,
                            summary=finding.summary,
                            detail=finding.detail,
                            location=finding.location,
                            metadata=dict(finding.metadata),
                        )
                    )
            aggregate.evidence.extend(gate.evidence)
        aggregate.metrics.update(
            {
                "tier_status": tier_gate.status.value,
                "provider_status": provider_gate.status.value,
                "tier_observation_count": tier_gate.metrics.get("observation_count", 0),
                "provider_observation_count": provider_gate.metrics.get("observation_count", 0),
            }
        )
        return tier_gate, provider_gate, aggregate.finish(default_partial=not final_completion)


def _safe_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    secret = {"authorization", "x-api-key", "api-key", "cookie", "set-cookie", "proxy-authorization"}
    return {
        str(key).lower(): "[REDACTED]" if str(key).lower() in secret else str(value)[:512]
        for key, value in headers.items()
    }


def _digest(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value.lower())) if value else False


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _enum(kind: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return kind(str(value))
    except ValueError:
        return default
