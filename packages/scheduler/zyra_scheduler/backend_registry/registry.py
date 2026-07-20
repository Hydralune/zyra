from __future__ import annotations

import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from .models import (
    BackendDefinition,
    BackendDispatchError,
    BackendFailureKind,
    BackendHealthRecord,
    BackendHealthStatus,
    BackendLease,
    BackendLocation,
    BackendRecoveryIntent,
    BackendSelectionRequest,
    checksum,
    new_backend_id,
    now_timestamp,
)
from .store import BackendRegistryStore


class BackendRegistry:
    def __init__(
        self,
        store: BackendRegistryStore,
        *,
        lease_seconds: float = 600.0,
        attempt_limit: int = 3,
        quarantine_seconds: float = 60.0,
        failure_threshold: int = 3,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if attempt_limit <= 0:
            raise ValueError("attempt_limit must be positive")
        self.store = store
        self.lease_seconds = float(lease_seconds)
        self.attempt_limit = int(attempt_limit)
        self.quarantine_seconds = float(quarantine_seconds)
        self.failure_threshold = int(failure_threshold)
        self._lock = threading.RLock()

    def register(
        self,
        definition: BackendDefinition,
        *,
        expected_revision: int | None = None,
    ) -> int:
        normalized = validate_definition(definition)
        with self._lock:
            revision = self.store.put_definition(
                normalized,
                expected_revision=expected_revision,
            )
            health = self.store.get_health(normalized.backend_id)
            if health is None:
                self.store.put_health(
                    BackendHealthRecord(
                        backend_id=normalized.backend_id,
                        status=(
                            BackendHealthStatus.HEALTHY
                            if normalized.enabled
                            else BackendHealthStatus.DISABLED
                        ),
                        revision=1,
                        reason="registered" if normalized.enabled else "definition disabled",
                        last_checked_at=now_timestamp(),
                    )
                )
            elif health.status is BackendHealthStatus.DISABLED and normalized.enabled:
                self.store.put_health(
                    replace(
                        health,
                        status=BackendHealthStatus.HEALTHY,
                        revision=health.revision + 1,
                        reason="definition re-enabled",
                        last_checked_at=now_timestamp(),
                    )
                )
            return revision

    def definition(self, backend_id: str) -> BackendDefinition:
        definition = self.store.get_definition(backend_id)
        if definition is None:
            raise BackendDispatchError(
                BackendFailureKind.BACKEND_UNAVAILABLE,
                f"backend is not registered: {backend_id}",
                retryable=True,
                recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                backend_id=backend_id,
            )
        return definition

    def definitions(self, *, runtime_worker: str = "") -> list[BackendDefinition]:
        return self.store.definitions(runtime_worker=runtime_worker)

    def health(self, backend_id: str) -> BackendHealthRecord:
        self.definition(backend_id)
        health = self.store.get_health(backend_id)
        if health is None:
            raise RuntimeError(f"backend has no health record: {backend_id}")
        if (
            health.status is BackendHealthStatus.QUARANTINED
            and health.quarantine_until is not None
            and health.quarantine_until <= now_timestamp()
        ):
            health = replace(
                health,
                status=BackendHealthStatus.DEGRADED,
                revision=health.revision + 1,
                consecutive_failures=0,
                quarantine_until=None,
                reason="quarantine elapsed; probe required",
                last_checked_at=now_timestamp(),
            )
            self.store.put_health(health)
        return health

    def select(
        self,
        request: BackendSelectionRequest,
        *,
        previous_backend_id: str | None = None,
    ) -> tuple[BackendDefinition, list[str]]:
        validate_selection_request(request)
        excluded = set(request.excluded_backend_ids)
        if previous_backend_id:
            excluded.add(previous_backend_id)
        allowed_locations = set(request.allowed_locations)
        required_capabilities = set(request.required_capabilities)
        candidates: list[tuple[float, BackendDefinition, list[str]]] = []
        for definition in self.store.definitions(runtime_worker=request.runtime_worker):
            if not definition.enabled or definition.backend_id in excluded:
                continue
            if allowed_locations and definition.location not in allowed_locations:
                continue
            if not required_capabilities.issubset(set(definition.capabilities)):
                continue
            health = self.health(definition.backend_id)
            if health.status in {
                BackendHealthStatus.UNAVAILABLE,
                BackendHealthStatus.QUARANTINED,
                BackendHealthStatus.DISABLED,
            }:
                continue
            if health.current_leases >= definition.limits.maximum_concurrency:
                continue
            workspace_failure = validate_workspace(
                Path(request.workspace_root),
                definition,
            )
            if workspace_failure is not None:
                self.record_failure(
                    definition.backend_id,
                    kind=workspace_failure.kind,
                    reason=str(workspace_failure),
                )
                continue
            if self.store.workspace_is_quarantined(
                request.workspace_root,
                backend_id=definition.backend_id,
                worker_id=request.runtime_worker,
                at=now_timestamp(),
            ):
                continue
            reasons: list[str] = []
            score = float(definition.priority) * 100.0
            if request.preferred_backend_id == definition.backend_id:
                score += 10_000.0
                reasons.append("preferred backend")
            if health.status is BackendHealthStatus.HEALTHY:
                score += 500.0
                reasons.append("healthy")
            elif health.status is BackendHealthStatus.DEGRADED:
                score += 100.0
                reasons.append("degraded but eligible")
            score -= health.current_leases * 100.0
            score -= health.latency_milliseconds * max(0.0, definition.latency_weight)
            score -= max(0.0, definition.cost_weight) * 100.0
            if previous_backend_id and definition.backend_id != previous_backend_id:
                reasons.append("backend failover candidate")
            if not reasons:
                reasons.append("eligible backend")
            candidates.append((score, definition, reasons))
        candidates.sort(key=lambda item: (-item[0], item[1].backend_id))
        if not candidates:
            raise BackendDispatchError(
                BackendFailureKind.BACKEND_UNAVAILABLE,
                f"no eligible backend for runtime worker {request.runtime_worker}",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
                provider_route_id=request.provider_route_id or "",
                detail={
                    "runtime_worker": request.runtime_worker,
                    "preferred_backend_id": request.preferred_backend_id,
                    "excluded_backend_ids": sorted(excluded),
                },
            )
        _, selected, reasons = candidates[0]
        return selected, reasons

    def acquire(
        self,
        request: BackendSelectionRequest,
        *,
        previous_lease_id: str | None = None,
    ) -> BackendLease:
        with self._lock:
            previous = self.require_lease(previous_lease_id) if previous_lease_id else None
            selected, reasons = self.select(
                request,
                previous_backend_id=(previous.backend_id if previous else None),
            )
            health = self.health(selected.backend_id)
            acquired_at = now_timestamp()
            body = {
                "lease_id": new_backend_id("backend_lease"),
                "backend_id": selected.backend_id,
                "registry_revision": self.store.revision(),
                "health_revision": health.revision,
                "run_id": request.run_id,
                "task_id": request.task_id,
                "node_id": request.node_id,
                "runtime_worker": request.runtime_worker,
                "workspace_root": str(Path(request.workspace_root).expanduser().resolve()),
                "artifact_root": str(Path(request.artifact_root).expanduser().resolve()),
                "provider_route_id": request.provider_route_id,
                "provider_route_checksum": request.provider_route_checksum,
                "provider_catalog_revision": request.provider_catalog_revision,
                "provider_credential_version": request.provider_credential_version,
                "provider_credential_fingerprint": request.provider_credential_fingerprint,
                "provider_transport_id": request.provider_transport_id,
                "m0_execution_ref": request.m0_execution_ref,
                "physical_worker_lease_ref": None,
                "turn_id": request.turn_id,
                "acquired_at": acquired_at,
                "expires_at": acquired_at + self.lease_seconds,
                "attempt_limit": self.attempt_limit,
                "previous_lease_id": previous_lease_id,
                "reason": "; ".join(reasons),
            }
            lease = BackendLease(**body, checksum=checksum(body))
            self.store.put_lease(lease)
            self.store.put_health(
                replace(
                    health,
                    revision=health.revision + 1,
                    current_leases=health.current_leases + 1,
                    last_checked_at=acquired_at,
                )
            )
            return lease

    def require_lease(self, lease_id: str | None) -> BackendLease:
        if not lease_id:
            raise BackendDispatchError(
                BackendFailureKind.LEASE_CONFLICT,
                "backend lease id is required",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
            )
        lease = self.store.get_lease(lease_id)
        if lease is None:
            raise BackendDispatchError(
                BackendFailureKind.LEASE_CONFLICT,
                f"backend lease not found or already released: {lease_id}",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
                lease_id=lease_id,
            )
        body = lease.to_dict()
        expected = body.pop("checksum")
        if checksum(body) != expected:
            raise BackendDispatchError(
                BackendFailureKind.LEASE_CONFLICT,
                f"backend lease checksum mismatch: {lease_id}",
                retryable=False,
                recovery_intent=BackendRecoveryIntent.NONE,
                lease_id=lease_id,
                backend_id=lease.backend_id,
            )
        if lease.expires_at <= now_timestamp():
            self.release(lease_id, reason="expired")
            raise BackendDispatchError(
                BackendFailureKind.LEASE_EXPIRED,
                f"backend lease expired: {lease_id}",
                retryable=True,
                recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
                lease_id=lease_id,
                backend_id=lease.backend_id,
                provider_route_id=lease.provider_route_id or "",
            )
        return lease

    def release(self, lease_id: str, *, reason: str) -> bool:
        with self._lock:
            lease = self.store.get_lease(lease_id)
            if lease is None:
                return False
            released = self.store.release_lease(
                lease_id,
                reason=reason,
                released_at=now_timestamp(),
            )
            if released:
                health = self.health(lease.backend_id)
                self.store.put_health(
                    replace(
                        health,
                        revision=health.revision + 1,
                        current_leases=max(0, health.current_leases - 1),
                        last_checked_at=now_timestamp(),
                    )
                )
            return released

    def record_success(self, backend_id: str, *, latency_milliseconds: float) -> BackendHealthRecord:
        with self._lock:
            current = self.health(backend_id)
            now = now_timestamp()
            latency = (
                latency_milliseconds
                if current.success_count == 0
                else current.latency_milliseconds * 0.8 + latency_milliseconds * 0.2
            )
            next_record = replace(
                current,
                status=BackendHealthStatus.HEALTHY,
                revision=current.revision + 1,
                consecutive_failures=0,
                success_count=current.success_count + 1,
                latency_milliseconds=max(0.0, latency),
                reason="dispatch succeeded",
                last_checked_at=now,
                last_success_at=now,
                quarantine_until=None,
            )
            self.store.put_health(next_record)
            return next_record

    def record_failure(
        self,
        backend_id: str,
        *,
        kind: BackendFailureKind,
        reason: str,
    ) -> BackendHealthRecord:
        with self._lock:
            current = self.health(backend_id)
            now = now_timestamp()
            failures = current.consecutive_failures + 1
            if failures >= self.failure_threshold:
                status = BackendHealthStatus.QUARANTINED
                quarantine_until = now + self.quarantine_seconds
            elif kind in {
                BackendFailureKind.BACKEND_UNAVAILABLE,
                BackendFailureKind.WORKSPACE_CORRUPT,
            }:
                status = BackendHealthStatus.UNAVAILABLE
                quarantine_until = now + self.quarantine_seconds
            else:
                status = BackendHealthStatus.DEGRADED
                quarantine_until = None
            next_record = replace(
                current,
                status=status,
                revision=current.revision + 1,
                consecutive_failures=failures,
                failure_count=current.failure_count + 1,
                reason=reason[:1_000],
                last_checked_at=now,
                last_failure_at=now,
                quarantine_until=quarantine_until,
                metadata={**dict(current.metadata), "last_failure_kind": kind.value},
            )
            self.store.put_health(next_record)
            return next_record

    def health_snapshot(self) -> list[BackendHealthRecord]:
        return [self.health(item.backend_id) for item in self.store.definitions()]

    def summary(self) -> dict[str, Any]:
        return {
            "schema": "zyra.backend-registry.health/v1",
            "state_owner": "python.BackendRegistryStore",
            "registry_revision": self.store.revision(),
            "provider_state_owned": False,
            "backend_state_owned": True,
            "counts": self.store.counts(),
            "backends": [item.to_dict() for item in self.store.definitions()],
            "health": [item.to_dict() for item in self.health_snapshot()],
        }


def validate_definition(definition: BackendDefinition) -> BackendDefinition:
    if not definition.backend_id.strip() or not definition.runtime_worker.strip():
        raise ValueError("backend_id and runtime_worker are required")
    if definition.limits.maximum_concurrency <= 0:
        raise ValueError("backend maximum_concurrency must be positive")
    if definition.limits.turn_timeout_seconds <= 0:
        raise ValueError("backend turn timeout must be positive")
    if definition.kind.value.endswith("http"):
        if not definition.endpoint:
            raise ValueError("HTTP backend requires endpoint")
        parsed = urlparse(definition.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("backend endpoint must be an absolute HTTP(S) URL")
    if definition.kind.value == "docker" and not definition.docker_image:
        raise ValueError("Docker backend requires docker_image")
    if definition.kind.value == "local_process" and definition.command:
        executable = str(definition.command[0]).strip()
        if not executable:
            raise ValueError("local process command executable is blank")
    return replace(
        definition,
        backend_id=definition.backend_id.strip(),
        display_name=definition.display_name.strip() or definition.backend_id,
        runtime_worker=definition.runtime_worker.strip(),
        capabilities=tuple(sorted(set(str(item).strip() for item in definition.capabilities if str(item).strip()))),
        command=tuple(str(item) for item in definition.command),
        metadata=dict(definition.metadata),
    )


def validate_selection_request(request: BackendSelectionRequest) -> None:
    for name, value in {
        "run_id": request.run_id,
        "task_id": request.task_id,
        "runtime_worker": request.runtime_worker,
        "workspace_root": request.workspace_root,
        "artifact_root": request.artifact_root,
        "turn_id": request.turn_id,
    }.items():
        if not str(value).strip():
            raise ValueError(f"{name} is required")
    # Provider route fields are an opaque immutable projection. BackendRegistry
    # validates presence/digests only and never parses provider/model/defaults.
    opaque_required = {
        "provider_route_id": request.provider_route_id,
        "provider_route_checksum": request.provider_route_checksum,
        "provider_credential_fingerprint": request.provider_credential_fingerprint,
        "provider_transport_id": request.provider_transport_id,
        "m0_execution_ref": request.m0_execution_ref,
    }
    for name, value in opaque_required.items():
        if not str(value or "").strip():
            raise ValueError(f"{name} is required")
    if request.provider_catalog_revision <= 0:
        raise ValueError("provider_catalog_revision must be positive")
    if request.provider_credential_version <= 0:
        raise ValueError("provider_credential_version must be positive")


def validate_workspace(
    workspace_root: Path,
    definition: BackendDefinition,
) -> BackendDispatchError | None:
    policy = definition.workspace_policy
    if policy.artifact_only:
        return None
    root = workspace_root.expanduser().resolve()
    if policy.require_existing and not root.is_dir():
        return BackendDispatchError(
            BackendFailureKind.WORKSPACE_UNAVAILABLE,
            f"workspace does not exist: {root}",
            retryable=True,
            recovery_intent=BackendRecoveryIntent.CHANGE_BACKEND,
            backend_id=definition.backend_id,
        )
    if policy.require_writable and not policy.read_only and root.exists() and not os.access(root, os.W_OK):
        return BackendDispatchError(
            BackendFailureKind.WORKSPACE_CORRUPT,
            f"workspace is not writable: {root}",
            retryable=True,
            recovery_intent=BackendRecoveryIntent.REBUILD_WORKSPACE,
            backend_id=definition.backend_id,
        )
    return None
