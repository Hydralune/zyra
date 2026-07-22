from __future__ import annotations

import ipaddress
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

from .contracts import Finding, GateResult, GateStatus, Maturity, Severity


class ExecutionTier:
    LOCAL = "local"
    EDGE = "edge"
    CLOUD = "cloud"


@dataclass(frozen=True, slots=True)
class TierObservation:
    tier: str
    worker_id: str
    endpoint: str
    process_id: str
    runtime_kind: str
    capability_handshake_id: str
    capabilities: tuple[str, ...]
    heartbeat_id: str
    heartbeat_sequence: int
    telemetry_id: str
    gateway_receipt_id: str
    route_id: str
    lease_id: str
    artifact_ids: tuple[str, ...]
    result_id: str
    state_owner: str
    simulated: bool = False
    in_process: bool = False
    connected_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "worker_id": self.worker_id,
            "endpoint": self.endpoint,
            "process_id": self.process_id,
            "runtime_kind": self.runtime_kind,
            "capability_handshake_id": self.capability_handshake_id,
            "capabilities": list(self.capabilities),
            "heartbeat_id": self.heartbeat_id,
            "heartbeat_sequence": self.heartbeat_sequence,
            "telemetry_id": self.telemetry_id,
            "gateway_receipt_id": self.gateway_receipt_id,
            "route_id": self.route_id,
            "lease_id": self.lease_id,
            "artifact_ids": list(self.artifact_ids),
            "result_id": self.result_id,
            "state_owner": self.state_owner,
            "simulated": self.simulated,
            "in_process": self.in_process,
            "connected_at": self.connected_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    provider_id: str
    integration_id: str
    wire_dialect: str
    endpoint: str
    transport_kind: str
    request_id: str
    stream_event_count: int
    tool_use_count: int
    tool_result_count: int
    final_response_id: str
    credential_handle: str
    route_id: str
    state_owner: str
    maturity: Maturity
    loopback: bool = False
    simulated: bool = False
    error_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def completed_wire_loop(self) -> bool:
        return bool(
            self.request_id
            and self.stream_event_count > 0
            and self.final_response_id
            and self.tool_result_count >= self.tool_use_count
            and not self.error_code
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "integration_id": self.integration_id,
            "wire_dialect": self.wire_dialect,
            "endpoint": self.endpoint,
            "transport_kind": self.transport_kind,
            "request_id": self.request_id,
            "stream_event_count": self.stream_event_count,
            "tool_use_count": self.tool_use_count,
            "tool_result_count": self.tool_result_count,
            "final_response_id": self.final_response_id,
            "credential_handle": self.credential_handle,
            "route_id": self.route_id,
            "state_owner": self.state_owner,
            "maturity": self.maturity.value,
            "loopback": self.loopback,
            "simulated": self.simulated,
            "error_code": self.error_code,
            "completed_wire_loop": self.completed_wire_loop,
            "metadata": dict(self.metadata),
        }


class DeploymentObservationExtractor:
    def tiers(self, events: Sequence[Mapping[str, Any]]) -> tuple[TierObservation, ...]:
        aggregates: dict[tuple[str, str], dict[str, Any]] = {}
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
            worker = self._worker_payload(event, payload)
            tier = str(
                worker.get("tier")
                or worker.get("location")
                or worker.get("dispatch_location")
                or metadata.get("tier")
                or ""
            ).lower()
            worker_id = str(worker.get("worker_id") or payload.get("worker_id") or event.get("worker_id") or "")
            if tier not in {ExecutionTier.LOCAL, ExecutionTier.EDGE, ExecutionTier.CLOUD} or not worker_id:
                continue
            key = (tier, worker_id)
            aggregate = aggregates.setdefault(
                key,
                {
                    "tier": tier,
                    "worker_id": worker_id,
                    "capabilities": set(),
                    "artifact_ids": set(),
                    "heartbeat_sequence": 0,
                    "metadata": {},
                },
            )
            self._merge_tier(aggregate, worker, payload, metadata, event)
        return tuple(self._tier_from(item) for item in aggregates.values())

    def providers(self, events: Sequence[Mapping[str, Any]]) -> tuple[ProviderObservation, ...]:
        aggregates: dict[tuple[str, str], dict[str, Any]] = {}
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
            provider = self._provider_payload(event, payload)
            provider_id = str(provider.get("provider_id") or payload.get("provider_id") or "")
            integration_id = str(provider.get("integration_id") or provider.get("backend_id") or payload.get("backend_id") or "")
            event_type = str(event.get("event_type") or event.get("type") or "").lower()
            explicit_provider_payload = any(
                isinstance(payload.get(key), Mapping)
                for key in ("provider", "provider_request", "provider_stream", "model_result")
            )
            provider_semantic = explicit_provider_payload or any(
                marker in event_type for marker in ("provider", "model_request", "model_response", "model_stream")
            )
            if not provider_id and not provider_semantic:
                continue
            if not provider_id and not integration_id:
                continue
            key = (provider_id, integration_id)
            aggregate = aggregates.setdefault(
                key,
                {
                    "provider_id": provider_id,
                    "integration_id": integration_id,
                    "stream_event_count": 0,
                    "tool_use_count": 0,
                    "tool_result_count": 0,
                    "metadata": {},
                },
            )
            self._merge_provider(aggregate, provider, payload, metadata, event)
        return tuple(self._provider_from(item) for item in aggregates.values())

    @staticmethod
    def _worker_payload(event: Mapping[str, Any], payload: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in ("worker", "worker_registration", "worker_heartbeat", "dispatch", "lease", "worker_result"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                return {**payload, **value}
            value = event.get(key)
            if isinstance(value, Mapping):
                return {**payload, **value}
        return payload

    @staticmethod
    def _provider_payload(event: Mapping[str, Any], payload: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in ("provider", "provider_request", "provider_stream", "backend_dispatch", "model_result"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                return {**payload, **value}
            value = event.get(key)
            if isinstance(value, Mapping):
                return {**payload, **value}
        return payload

    def _merge_tier(
        self,
        target: dict[str, Any],
        worker: Mapping[str, Any],
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> None:
        for key, aliases in {
            "endpoint": ("endpoint", "worker_endpoint", "url"),
            "process_id": ("process_id", "pid", "runtime_process_id"),
            "runtime_kind": ("runtime_kind", "worker_kind", "transport_kind"),
            "capability_handshake_id": ("capability_handshake_id", "manifest_id", "handshake_id"),
            "heartbeat_id": ("heartbeat_id", "heartbeat_event_id"),
            "telemetry_id": ("telemetry_id", "span_id", "trace_id"),
            "gateway_receipt_id": ("gateway_receipt_id", "gateway_receipt_ref"),
            "route_id": ("route_id", "backend_route_id", "dispatch_id"),
            "lease_id": ("lease_id", "worker_lease_id"),
            "result_id": ("result_id", "worker_result_id", "receipt_id"),
            "state_owner": ("state_owner", "canonical_owner"),
            "connected_at": ("connected_at", "registered_at"),
        }.items():
            value = self._first(worker, payload, metadata, event, keys=aliases)
            if value not in (None, ""):
                target[key] = str(value)
        capabilities = self._first(worker, payload, metadata, keys=("capabilities", "capability_ids")) or ()
        target["capabilities"].update(str(item) for item in capabilities if str(item))
        artifacts = self._first(worker, payload, metadata, keys=("artifact_ids", "artifact_refs")) or ()
        for item in artifacts:
            if isinstance(item, Mapping):
                value = item.get("artifact_id") or item.get("id") or item.get("uri")
            else:
                value = item
            if value:
                target["artifact_ids"].add(str(value))
        sequence = self._int(self._first(worker, payload, metadata, keys=("heartbeat_sequence", "sequence")))
        target["heartbeat_sequence"] = max(target["heartbeat_sequence"], sequence)
        target["simulated"] = bool(target.get("simulated")) or self._truthy(
            self._first(worker, payload, metadata, keys=("simulated", "simulation", "mock"))
        )
        target["in_process"] = bool(target.get("in_process")) or self._truthy(
            self._first(worker, payload, metadata, keys=("in_process", "same_process"))
        )
        target["metadata"].update(metadata)

    def _merge_provider(
        self,
        target: dict[str, Any],
        provider: Mapping[str, Any],
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> None:
        for key, aliases in {
            "wire_dialect": ("wire_dialect", "protocol", "api_dialect"),
            "endpoint": ("endpoint", "base_url", "url"),
            "transport_kind": ("transport_kind", "transport", "client_kind"),
            "request_id": ("request_id", "provider_request_id"),
            "final_response_id": ("final_response_id", "response_id", "message_id"),
            "credential_handle": ("credential_handle", "credential_ref", "auth_handle"),
            "route_id": ("route_id", "dispatch_id", "backend_route_id"),
            "state_owner": ("state_owner", "canonical_owner"),
            "error_code": ("error_code", "failure_code"),
        }.items():
            value = self._first(provider, payload, metadata, event, keys=aliases)
            if value not in (None, ""):
                target[key] = str(value)
        event_type = str(event.get("event_type") or "").lower()
        if "stream" in event_type or provider.get("stream_event") is not None:
            target["stream_event_count"] += max(1, self._int(provider.get("stream_event_count")))
        target["tool_use_count"] += self._int(provider.get("tool_use_count"))
        target["tool_result_count"] += self._int(provider.get("tool_result_count"))
        if "tool_use" in event_type:
            target["tool_use_count"] += 1
        if "tool_result" in event_type:
            target["tool_result_count"] += 1
        endpoint = str(target.get("endpoint") or "")
        target["loopback"] = bool(target.get("loopback")) or self._truthy(provider.get("loopback")) or self._loopback(endpoint)
        target["simulated"] = bool(target.get("simulated")) or self._truthy(
            self._first(provider, payload, metadata, keys=("simulated", "mock", "fixture"))
        )
        target["maturity"] = str(provider.get("maturity") or metadata.get("maturity") or target.get("maturity") or "")
        target["metadata"].update(metadata)

    @staticmethod
    def _tier_from(value: Mapping[str, Any]) -> TierObservation:
        return TierObservation(
            tier=str(value.get("tier") or ""),
            worker_id=str(value.get("worker_id") or ""),
            endpoint=str(value.get("endpoint") or ""),
            process_id=str(value.get("process_id") or ""),
            runtime_kind=str(value.get("runtime_kind") or ""),
            capability_handshake_id=str(value.get("capability_handshake_id") or ""),
            capabilities=tuple(sorted(value.get("capabilities") or ())),
            heartbeat_id=str(value.get("heartbeat_id") or ""),
            heartbeat_sequence=int(value.get("heartbeat_sequence") or 0),
            telemetry_id=str(value.get("telemetry_id") or ""),
            gateway_receipt_id=str(value.get("gateway_receipt_id") or ""),
            route_id=str(value.get("route_id") or ""),
            lease_id=str(value.get("lease_id") or ""),
            artifact_ids=tuple(sorted(value.get("artifact_ids") or ())),
            result_id=str(value.get("result_id") or ""),
            state_owner=str(value.get("state_owner") or ""),
            simulated=bool(value.get("simulated")),
            in_process=bool(value.get("in_process")),
            connected_at=str(value.get("connected_at") or ""),
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _provider_from(value: Mapping[str, Any]) -> ProviderObservation:
        raw_maturity = str(value.get("maturity") or "").lower()
        try:
            maturity = Maturity(raw_maturity)
        except ValueError:
            maturity = Maturity.SOURCE_INACTIVE
        return ProviderObservation(
            provider_id=str(value.get("provider_id") or ""),
            integration_id=str(value.get("integration_id") or ""),
            wire_dialect=str(value.get("wire_dialect") or ""),
            endpoint=str(value.get("endpoint") or ""),
            transport_kind=str(value.get("transport_kind") or ""),
            request_id=str(value.get("request_id") or ""),
            stream_event_count=int(value.get("stream_event_count") or 0),
            tool_use_count=int(value.get("tool_use_count") or 0),
            tool_result_count=int(value.get("tool_result_count") or 0),
            final_response_id=str(value.get("final_response_id") or ""),
            credential_handle=str(value.get("credential_handle") or ""),
            route_id=str(value.get("route_id") or ""),
            state_owner=str(value.get("state_owner") or ""),
            maturity=maturity,
            loopback=bool(value.get("loopback")),
            simulated=bool(value.get("simulated")),
            error_code=str(value.get("error_code") or ""),
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _first(*sources: Mapping[str, Any], keys: Sequence[str]) -> Any:
        for source in sources:
            for key in keys:
                if key in source and source[key] not in (None, ""):
                    return source[key]
        return None

    @staticmethod
    def _truthy(value: Any) -> bool:
        return value is True or str(value or "").lower() in {"1", "true", "yes", "simulated", "mock"}

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _loopback(endpoint: str) -> bool:
        if not endpoint:
            return False
        parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "localhost.localdomain"}:
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False


class ExecutionTierGate:
    def __init__(self) -> None:
        self.extractor = DeploymentObservationExtractor()

    def evaluate(
        self,
        observations: Sequence[TierObservation] = (),
        *,
        events: Sequence[Mapping[str, Any]] = (),
        final_completion: bool = False,
    ) -> GateResult:
        result = GateResult(
            gate_id="execution-tiers",
            status=GateStatus.NOT_RUN,
            summary="Real local, isolated edge and cloud worker dispatch evidence.",
        )
        observed = tuple(observations) or self.extractor.tiers(events)
        by_tier: dict[str, list[TierObservation]] = defaultdict(list)
        for item in observed:
            by_tier[item.tier].append(item)
            result.findings.extend(self._observation_findings(item))
        missing = {ExecutionTier.LOCAL, ExecutionTier.EDGE, ExecutionTier.CLOUD} - set(by_tier)
        for tier in sorted(missing):
            result.add(
                Finding(
                    code="tiers.required_tier_missing",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="Required execution tier has no real observation.",
                    detail=tier,
                )
            )
        result.findings.extend(self._independence_findings(observed))
        result.metrics.update(
            {
                "observation_count": len(observed),
                "tier_counts": dict(Counter(item.tier for item in observed)),
                "real_tier_count": len({item.tier for item in observed if not item.simulated and not item.in_process}),
                "observations": [item.to_dict() for item in observed],
            }
        )
        if missing and not final_completion:
            result.limitations.append("Real local/edge/cloud release evidence remains open for milestone exit.")
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _observation_findings(item: TierObservation) -> list[Finding]:
        findings: list[Finding] = []
        if item.simulated:
            findings.append(
                Finding(
                    code="tiers.simulated_worker",
                    severity=Severity.BLOCKER,
                    summary="Simulated worker cannot satisfy real tier completion.",
                    detail=item.tier,
                    location=item.worker_id,
                )
            )
        if item.tier in {ExecutionTier.EDGE, ExecutionTier.CLOUD} and item.in_process:
            findings.append(
                Finding(
                    code="tiers.remote_in_process",
                    severity=Severity.BLOCKER,
                    summary="Edge/cloud worker is an in-process object rather than an independent endpoint.",
                    detail=item.tier,
                    location=item.worker_id,
                )
            )
        required = {
            "endpoint": item.endpoint,
            "process_id": item.process_id,
            "capability_handshake_id": item.capability_handshake_id,
            "heartbeat_id": item.heartbeat_id,
            "telemetry_id": item.telemetry_id,
            "gateway_receipt_id": item.gateway_receipt_id,
            "route_id": item.route_id,
            "lease_id": item.lease_id,
            "result_id": item.result_id,
            "state_owner": item.state_owner,
        }
        for name, value in required.items():
            if not value:
                findings.append(
                    Finding(
                        code=f"tiers.{name}_missing",
                        severity=Severity.ERROR,
                        summary=f"Tier observation lacks {name.replace('_', ' ')} evidence.",
                        capability=item.tier,
                        location=item.worker_id,
                    )
                )
        if not item.capabilities:
            findings.append(
                Finding(
                    code="tiers.capabilities_missing",
                    severity=Severity.ERROR,
                    summary="Worker capability handshake exposes no capabilities.",
                    capability=item.tier,
                    location=item.worker_id,
                )
            )
        if item.heartbeat_sequence <= 0:
            findings.append(
                Finding(
                    code="tiers.heartbeat_sequence_invalid",
                    severity=Severity.ERROR,
                    summary="Worker heartbeat sequence is not positive.",
                    capability=item.tier,
                    location=item.worker_id,
                )
            )
        if not item.artifact_ids:
            findings.append(
                Finding(
                    code="tiers.artifact_missing",
                    severity=Severity.ERROR,
                    summary="Tier result has no returned artifact evidence.",
                    capability=item.tier,
                    location=item.worker_id,
                )
            )
        return findings

    @staticmethod
    def _independence_findings(observations: Sequence[TierObservation]) -> list[Finding]:
        findings: list[Finding] = []
        remote = [item for item in observations if item.tier in {ExecutionTier.EDGE, ExecutionTier.CLOUD}]
        endpoints: dict[str, list[TierObservation]] = defaultdict(list)
        process_ids: dict[str, list[TierObservation]] = defaultdict(list)
        for item in remote:
            if item.endpoint:
                endpoints[item.endpoint].append(item)
            if item.process_id:
                process_ids[item.process_id].append(item)
        for endpoint, items in endpoints.items():
            if len({item.tier for item in items}) > 1:
                findings.append(
                    Finding(
                        code="tiers.endpoint_reused",
                        severity=Severity.BLOCKER,
                        summary="Edge and cloud observations reuse the same endpoint.",
                        detail=endpoint,
                    )
                )
        for process_id, items in process_ids.items():
            if len({item.tier for item in items}) > 1:
                findings.append(
                    Finding(
                        code="tiers.process_reused",
                        severity=Severity.BLOCKER,
                        summary="Edge and cloud observations reuse the same process identity.",
                        detail=process_id,
                    )
                )
        return findings


class ProviderControlPlaneGate:
    def __init__(self) -> None:
        self.extractor = DeploymentObservationExtractor()

    def evaluate(
        self,
        observations: Sequence[ProviderObservation] = (),
        *,
        events: Sequence[Mapping[str, Any]] = (),
        final_completion: bool = False,
    ) -> GateResult:
        result = GateResult(
            gate_id="provider-control-plane",
            status=GateStatus.NOT_RUN,
            summary="Two distinct real provider wire dialects with request/stream/tool-result completion.",
        )
        observed = tuple(observations) or self.extractor.providers(events)
        active_real: list[ProviderObservation] = []
        for item in observed:
            result.findings.extend(self._observation_findings(item))
            if (
                item.maturity is Maturity.ACTIVE_REAL
                and item.completed_wire_loop
                and not item.loopback
                and not item.simulated
            ):
                active_real.append(item)
        dialects = {self._normalize_dialect(item.wire_dialect) for item in active_real if item.wire_dialect}
        providers = {item.provider_id for item in active_real if item.provider_id}
        if len(dialects) < 2:
            result.add(
                Finding(
                    code="providers.two_dialects_missing",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="Fewer than two real provider wire dialects completed the full loop.",
                    detail=", ".join(sorted(dialects)),
                )
            )
        if len(providers) < 2:
            result.add(
                Finding(
                    code="providers.two_integrations_missing",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="Fewer than two real provider integrations completed the full loop.",
                    detail=", ".join(sorted(providers)),
                )
            )
        result.metrics.update(
            {
                "observation_count": len(observed),
                "active_real_count": len(active_real),
                "real_provider_count": len(providers),
                "real_wire_dialect_count": len(dialects),
                "wire_dialects": sorted(dialects),
                "providers": sorted(providers),
                "observations": [item.to_dict() for item in observed],
            }
        )
        if (len(dialects) < 2 or len(providers) < 2) and not final_completion:
            result.limitations.append("Two real provider integrations remain an M1 integration/milestone-exit gate.")
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _observation_findings(item: ProviderObservation) -> list[Finding]:
        findings: list[Finding] = []
        if item.maturity is Maturity.ACTIVE_REAL and (item.loopback or item.simulated):
            findings.append(
                Finding(
                    code="providers.invalid_active_real",
                    severity=Severity.BLOCKER,
                    summary="Loopback/simulated provider was promoted to active_real.",
                    location=item.integration_id,
                )
            )
        if item.loopback and item.maturity not in {Maturity.CONFORMANCE_VERIFIED, Maturity.SOURCE_INACTIVE}:
            findings.append(
                Finding(
                    code="providers.loopback_maturity_invalid",
                    severity=Severity.ERROR,
                    summary="Loopback provider evidence can only be conformance_verified or inactive.",
                    location=item.integration_id,
                )
            )
        if item.maturity is Maturity.ACTIVE_REAL and not item.completed_wire_loop:
            findings.append(
                Finding(
                    code="providers.wire_loop_incomplete",
                    severity=Severity.BLOCKER,
                    summary="active_real provider did not complete request/stream/tool result/final response.",
                    detail=item.error_code,
                    location=item.integration_id,
                )
            )
        if item.tool_use_count > item.tool_result_count:
            findings.append(
                Finding(
                    code="providers.tool_result_missing",
                    severity=Severity.ERROR,
                    summary="Provider stream contains unresolved tool uses.",
                    detail=f"tool_use={item.tool_use_count}; tool_result={item.tool_result_count}",
                    location=item.integration_id,
                )
            )
        for name, value in {
            "wire_dialect": item.wire_dialect,
            "transport_kind": item.transport_kind,
            "route_id": item.route_id,
            "state_owner": item.state_owner,
        }.items():
            if not value:
                findings.append(
                    Finding(
                        code=f"providers.{name}_missing",
                        severity=Severity.ERROR,
                        summary=f"Provider observation lacks {name.replace('_', ' ')} evidence.",
                        location=item.integration_id,
                    )
                )
        return findings

    @staticmethod
    def _normalize_dialect(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
        if "anthropic" in normalized or "messages" in normalized:
            return "anthropic_messages"
        if "openai" in normalized or "chat_completion" in normalized or "responses" in normalized:
            return "openai_compatible"
        if "gemini" in normalized or "vertex" in normalized:
            return "google_generative"
        return normalized
