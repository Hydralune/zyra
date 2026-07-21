from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .errors import WorkerPoolError, WorkerPoolErrorCode
from .models import (
    CapabilityAttestation,
    CapabilityRequirement,
    ResourceVector,
    WorkerCapabilityManifest,
    WorkerLocation,
    stable_digest,
    tokens,
    utc_iso,
)


class BackendCapabilitySource(Protocol):
    def describe_backend(self, backend_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class NativeWorkerCapabilities:
    worker_id: str
    worker_kind: str
    location: WorkerLocation
    capabilities: tuple[str, ...]
    tool_ids: tuple[str, ...]
    resources: ResourceVector
    protocol_version: int = 1
    labels: Mapping[str, str] = field(default_factory=dict)
    constraints: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentToolConstraints:
    allowed_agent_definitions: tuple[str, ...] = ()
    denied_agent_definitions: tuple[str, ...] = ()
    allowed_tool_patterns: tuple[str, ...] = ()
    explicit_tool_allowlist: tuple[str, ...] = ()
    explicit_tool_denylist: tuple[str, ...] = ()
    max_parallel_children: int = 1
    fork_context_allowed: bool = True
    background_allowed: bool = True


@dataclass(frozen=True, slots=True)
class BackendCapability:
    backend_id: str
    backend_kind: str
    enabled: bool
    healthy: bool
    capabilities: tuple[str, ...] = ()
    tool_ids: tuple[str, ...] = ()
    constraints: Mapping[str, Any] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapabilityCompositionReport:
    manifest: WorkerCapabilityManifest
    native_capabilities: tuple[str, ...]
    backend_capabilities: tuple[str, ...]
    removed_tools: tuple[str, ...]
    findings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest.to_dict(),
            "native_capabilities": list(self.native_capabilities),
            "backend_capabilities": list(self.backend_capabilities),
            "removed_tools": list(self.removed_tools),
            "findings": list(self.findings),
        }


class WorkerCapabilityComposer:
    """Composes the effective manifest instead of trusting static worker labels.

    Worker-native declarations, the 05D backend registry projection, and the 03D
    AgentTool/subagent restrictions are intersected.  A backend that is disabled
    or unhealthy never contributes capabilities to the effective manifest.
    """

    def compose(
        self,
        native: NativeWorkerCapabilities,
        *,
        backends: Sequence[BackendCapability],
        agent_constraints: AgentToolConstraints | None = None,
        manifest_revision: int = 1,
    ) -> CapabilityCompositionReport:
        constraints = agent_constraints or AgentToolConstraints()
        usable_backends = tuple(item for item in backends if item.enabled and item.healthy)
        if not usable_backends:
            raise WorkerPoolError(
                WorkerPoolErrorCode.CAPABILITY_MISMATCH,
                "worker has no enabled and healthy backend",
                operation="compose_worker_capability_manifest",
                worker_id=native.worker_id,
            )
        backend_ids = tokens(item.backend_id for item in usable_backends)
        backend_kinds = tokens(item.backend_kind for item in usable_backends)
        backend_capabilities = tokens(
            capability for item in usable_backends for capability in item.capabilities
        )
        capabilities = tokens((*native.capabilities, *backend_capabilities))
        backend_tools = set(tool for item in usable_backends for tool in item.tool_ids)
        effective_tools = set(native.tool_ids)
        findings: list[str] = []
        if backend_tools:
            unavailable = effective_tools - backend_tools
            if unavailable:
                findings.append("native tools absent from enabled backend were removed")
            effective_tools.intersection_update(backend_tools)
        if constraints.explicit_tool_allowlist:
            not_allowed = effective_tools - set(constraints.explicit_tool_allowlist)
            if not_allowed:
                findings.append("tools outside AgentTool allowlist were removed")
            effective_tools.intersection_update(constraints.explicit_tool_allowlist)
        denied = effective_tools.intersection(constraints.explicit_tool_denylist)
        effective_tools.difference_update(constraints.explicit_tool_denylist)
        if denied:
            findings.append("explicitly denied AgentTool entries were removed")
        removed = tokens(set(native.tool_ids) - effective_tools)
        backend_constraints = {
            item.backend_id: dict(item.constraints) for item in usable_backends
        }
        merged_constraints = {
            **dict(native.constraints),
            "backend_constraints": backend_constraints,
            "max_parallel_children": max(1, int(constraints.max_parallel_children)),
            "fork_context_allowed": bool(constraints.fork_context_allowed),
            "background_allowed": bool(constraints.background_allowed),
            "capability_owner": "WorkerCapabilityComposer",
            "backend_owner": "BackendRegistry",
            "logical_agent_owner": "typescript.AgentTaskRuntime",
        }
        labels = dict(native.labels)
        for backend in usable_backends:
            for key, value in backend.labels.items():
                labels.setdefault(key, value)
        manifest = WorkerCapabilityManifest(
            worker_id=native.worker_id,
            worker_kind=native.worker_kind,
            location=native.location,
            backend_ids=backend_ids,
            backend_kinds=backend_kinds,
            capabilities=capabilities,
            tool_ids=tokens(effective_tools),
            resource_capacity=native.resources,
            protocol_version=native.protocol_version,
            manifest_revision=manifest_revision,
            constraints=merged_constraints,
            labels=labels,
            allowed_agent_definitions=tokens(constraints.allowed_agent_definitions),
            denied_agent_definitions=tokens(constraints.denied_agent_definitions),
            allowed_tool_patterns=tokens(constraints.allowed_tool_patterns),
        )
        return CapabilityCompositionReport(
            manifest=manifest,
            native_capabilities=tokens(native.capabilities),
            backend_capabilities=backend_capabilities,
            removed_tools=removed,
            findings=tuple(findings),
        )

    @staticmethod
    def backend_from_mapping(value: Mapping[str, Any]) -> BackendCapability:
        health = str(value.get("health") or value.get("health_status") or "healthy").lower()
        enabled = bool(value.get("enabled", True))
        return BackendCapability(
            backend_id=str(value.get("backend_id") or value.get("id") or ""),
            backend_kind=str(value.get("backend_kind") or value.get("kind") or "unknown"),
            enabled=enabled,
            healthy=health in {"healthy", "ready", "available", "degraded"},
            capabilities=tokens(str(item) for item in value.get("capabilities") or ()),
            tool_ids=tokens(str(item) for item in value.get("tool_ids") or ()),
            constraints=dict(value.get("constraints") or {}),
            labels={str(key): str(item) for key, item in dict(value.get("labels") or {}).items()},
        )


class CapabilityAttestor:
    """Challenge-response attestation shared by local and edge worker protocols."""

    def __init__(self, secret: bytes, *, protocol_version: int = 1) -> None:
        if len(secret) < 24:
            raise ValueError("capability attestation secret must contain at least 24 bytes")
        self._secret = bytes(secret)
        self.protocol_version = max(1, int(protocol_version))

    @staticmethod
    def generate_secret() -> bytes:
        return secrets.token_bytes(32)

    @staticmethod
    def challenge() -> str:
        return secrets.token_hex(24)

    def response(
        self,
        *,
        worker_id: str,
        process_identity: str,
        manifest_digest: str,
        challenge_nonce: str,
        endpoint: str,
        protocol_version: int,
    ) -> str:
        message = "\n".join(
            (
                worker_id,
                process_identity,
                manifest_digest,
                challenge_nonce,
                endpoint,
                str(int(protocol_version)),
            )
        ).encode("utf-8")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def issue(
        self,
        *,
        worker_id: str,
        process_identity: str,
        manifest_digest: str,
        challenge_nonce: str,
        endpoint: str,
        protocol_version: int,
        expires_at: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> CapabilityAttestation:
        response = self.response(
            worker_id=worker_id,
            process_identity=process_identity,
            manifest_digest=manifest_digest,
            challenge_nonce=challenge_nonce,
            endpoint=endpoint,
            protocol_version=protocol_version,
        )
        return CapabilityAttestation(
            worker_id=worker_id,
            manifest_digest=manifest_digest,
            process_identity=process_identity,
            challenge_nonce=challenge_nonce,
            response_digest=response,
            protocol_version=protocol_version,
            endpoint=endpoint,
            expires_at=expires_at,
            metadata=dict(metadata or {}),
        )

    def verify(
        self,
        attestation: CapabilityAttestation,
        manifest: WorkerCapabilityManifest,
        *,
        expected_challenge: str,
        expected_endpoint: str = "",
    ) -> None:
        failures: list[str] = []
        if attestation.worker_id != manifest.worker_id:
            failures.append("worker id mismatch")
        if attestation.manifest_digest != manifest.digest:
            failures.append("manifest digest mismatch")
        if attestation.challenge_nonce != expected_challenge:
            failures.append("challenge nonce mismatch")
        if expected_endpoint and attestation.endpoint != expected_endpoint:
            failures.append("endpoint mismatch")
        if attestation.protocol_version != manifest.protocol_version:
            failures.append("protocol version mismatch")
        if attestation.expired:
            failures.append("attestation expired")
        expected = self.response(
            worker_id=attestation.worker_id,
            process_identity=attestation.process_identity,
            manifest_digest=attestation.manifest_digest,
            challenge_nonce=attestation.challenge_nonce,
            endpoint=attestation.endpoint,
            protocol_version=attestation.protocol_version,
        )
        if not hmac.compare_digest(expected, attestation.response_digest):
            failures.append("challenge response signature mismatch")
        if failures:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTESTATION_REJECTED,
                "; ".join(failures),
                operation="verify_worker_capability_attestation",
                worker_id=attestation.worker_id,
                metadata={"attestation_id": attestation.attestation_id},
            )


def requirement_from_subagent_dispatch(
    request: Any,
    *,
    backend_kinds: Iterable[str] = (),
    locations: Iterable[WorkerLocation] = (),
    resources: ResourceVector | None = None,
) -> CapabilityRequirement:
    """Map a 03D dispatch DTO into physical requirements without copying the task."""

    tool_scope = getattr(request, "tool_scope", None)
    allowed_tools = tuple(getattr(tool_scope, "allowed_tools", ()) or ())
    worker_name = str(getattr(request, "worker_name", "") or "")
    execution_mode = getattr(getattr(request, "execution_mode", None), "value", "")
    required = ["agent_task"]
    if worker_name:
        required.append(f"worker:{worker_name}")
    if execution_mode:
        required.append(f"execution_mode:{execution_mode}")
    return CapabilityRequirement(
        required=tokens(required),
        tool_ids=tokens(str(item) for item in allowed_tools),
        backend_kinds=tokens(backend_kinds),
        locations=tuple(locations),
        resources=resources or ResourceVector(process_slots=1),
    )
