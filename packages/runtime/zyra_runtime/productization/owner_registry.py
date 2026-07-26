from __future__ import annotations

import contextlib
import inspect
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from .contracts import (
    OWNER_RECEIPT_SCHEMA,
    REQUIRED_RUNTIME_DOMAINS,
    OwnerAvailability,
    OwnerBinding,
    OwnerLease,
    OwnerProbeResult,
    ProductizationContractError,
    RuntimeDomain,
    canonicalize,
    digest_payload,
    stable_id,
)


DEFAULT_LEASE_TTL_NS = 30_000_000_000
MAX_PROBE_HISTORY = 256
MAX_LEASE_HISTORY = 1_024


class OwnerProbe(Protocol):
    def __call__(self, binding: OwnerBinding) -> OwnerProbeResult | Mapping[str, Any]:
        ...


class Clock(Protocol):
    def __call__(self) -> int:
        ...


@dataclass(slots=True)
class _OwnerSlot:
    binding: OwnerBinding
    probe: OwnerProbe
    generation: int = 1
    enabled: bool = True
    lost: bool = False
    loss_reason: str = ""
    last_result: OwnerProbeResult | None = None
    active_leases: dict[str, OwnerLease] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OwnerRegistryEvent:
    event_id: str
    kind: str
    domain: RuntimeDomain
    generation: int
    owner_token: str
    occurred_at_ns: int
    details: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "domain": self.domain.value,
            "generation": self.generation,
            "owner_token": self.owner_token,
            "occurred_at_ns": self.occurred_at_ns,
            "details": canonicalize(self.details),
        }


class RuntimeOwnerRegistry:
    """Fail-closed registry for already-selected canonical runtime owners.

    The registry coordinates owner availability and generation fences. It does
    not store any domain state, perform a domain mutation, or provide a
    substitute implementation when an owner is unavailable.
    """

    def __init__(
        self,
        *,
        clock: Clock = time.time_ns,
        lease_ttl_ns: int = DEFAULT_LEASE_TTL_NS,
        max_probe_history: int = MAX_PROBE_HISTORY,
        max_lease_history: int = MAX_LEASE_HISTORY,
        enabled: bool = True,
    ) -> None:
        if lease_ttl_ns < 1_000_000:
            raise ProductizationContractError(
                "owner_lease_ttl_invalid",
                "owner lease TTL must be at least one millisecond",
            )
        if max_probe_history < 1 or max_lease_history < 1:
            raise ProductizationContractError(
                "owner_history_limit_invalid",
                "owner registry history limits must be positive",
            )
        self._clock = clock
        self._lease_ttl_ns = lease_ttl_ns
        self._enabled = bool(enabled)
        self._sealed = False
        self._slots: dict[RuntimeDomain, _OwnerSlot] = {}
        self._events: deque[OwnerRegistryEvent] = deque(maxlen=max_probe_history)
        self._lease_history: deque[dict[str, Any]] = deque(maxlen=max_lease_history)
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def sealed(self) -> bool:
        with self._lock:
            return self._sealed

    def register(self, binding: OwnerBinding, probe: OwnerProbe) -> None:
        if not callable(probe):
            raise ProductizationContractError(
                "owner_probe_invalid",
                f"{binding.domain.value} owner probe is not callable",
            )
        with self._lock:
            if self._sealed:
                raise ProductizationContractError(
                    "owner_registry_sealed",
                    "owner registry cannot be changed after seal",
                )
            if binding.domain in self._slots:
                existing = self._slots[binding.domain].binding
                raise ProductizationContractError(
                    "canonical_owner_duplicate",
                    f"{binding.domain.value} already has a canonical owner",
                    details={
                        "existing": existing.owner.to_dict(),
                        "proposed": binding.owner.to_dict(),
                    },
                )
            self._assert_no_cross_domain_owner_collision(binding)
            self._assert_no_fallback_promotion(binding)
            self._slots[binding.domain] = _OwnerSlot(binding=binding, probe=probe)
            self._append_event(
                "owner_registered",
                binding.domain,
                generation=1,
                owner_token=binding.owner_token,
                details={
                    "owner": binding.owner.to_dict(),
                    "store": binding.store.to_dict(),
                    "default_entries": list(binding.default_entries),
                },
            )

    def seal(self, *, require_all_domains: bool = True) -> str:
        with self._lock:
            if require_all_domains:
                missing = sorted(
                    domain.value
                    for domain in REQUIRED_RUNTIME_DOMAINS
                    if domain not in self._slots
                )
                if missing:
                    raise ProductizationContractError(
                        "canonical_owner_domains_missing",
                        "owner registry cannot seal with missing domains",
                        details={"missing": missing},
                    )
            self._sealed = True
            digest = self.configuration_digest()
            for slot in self._slots.values():
                self._append_event(
                    "owner_registry_sealed",
                    slot.binding.domain,
                    generation=slot.generation,
                    owner_token=slot.binding.owner_token,
                    details={"configuration_digest": digest},
                )
            return digest

    def configuration_digest(self) -> str:
        with self._lock:
            return digest_payload(
                {
                    "schema": OWNER_RECEIPT_SCHEMA,
                    "enabled": self._enabled,
                    "sealed": self._sealed,
                    "bindings": [
                        self._slots[domain].binding.to_dict()
                        for domain in sorted(self._slots, key=lambda item: item.value)
                    ],
                }
            )

    def disable(self, reason: str) -> None:
        normalized = str(reason).strip()
        if not normalized:
            raise ProductizationContractError(
                "owner_registry_disable_reason_missing",
                "disabling owner enforcement requires a reason",
            )
        with self._lock:
            self._enabled = False
            for slot in self._slots.values():
                self._revoke_leases(slot, reason="owner registry disabled")
                self._append_event(
                    "owner_registry_disabled",
                    slot.binding.domain,
                    generation=slot.generation,
                    owner_token=slot.binding.owner_token,
                    details={"reason": normalized},
                )

    def enable(self) -> None:
        with self._lock:
            self._enabled = True
            for slot in self._slots.values():
                self._append_event(
                    "owner_registry_enabled",
                    slot.binding.domain,
                    generation=slot.generation,
                    owner_token=slot.binding.owner_token,
                    details={},
                )

    def disable_domain(self, domain: RuntimeDomain, reason: str) -> None:
        normalized = str(reason).strip()
        if not normalized:
            raise ProductizationContractError(
                "owner_disable_reason_missing",
                "disabling a canonical owner requires a reason",
            )
        with self._lock:
            slot = self._require_slot(domain)
            slot.enabled = False
            slot.generation += 1
            slot.loss_reason = normalized
            self._revoke_leases(slot, reason=normalized)
            self._append_event(
                "owner_disabled",
                domain,
                generation=slot.generation,
                owner_token=slot.binding.owner_token,
                details={"reason": normalized},
            )

    def enable_domain(self, domain: RuntimeDomain) -> None:
        with self._lock:
            slot = self._require_slot(domain)
            slot.enabled = True
            slot.lost = False
            slot.loss_reason = ""
            slot.generation += 1
            slot.last_result = None
            self._append_event(
                "owner_enabled",
                domain,
                generation=slot.generation,
                owner_token=slot.binding.owner_token,
                details={},
            )

    def mark_lost(self, domain: RuntimeDomain, reason: str) -> None:
        normalized = str(reason).strip()
        if not normalized:
            raise ProductizationContractError(
                "owner_loss_reason_missing",
                "marking a canonical owner lost requires a reason",
            )
        with self._lock:
            slot = self._require_slot(domain)
            slot.lost = True
            slot.loss_reason = normalized
            slot.generation += 1
            self._revoke_leases(slot, reason=normalized)
            self._append_event(
                "owner_lost",
                domain,
                generation=slot.generation,
                owner_token=slot.binding.owner_token,
                details={"reason": normalized},
            )

    def replace_probe(
        self,
        domain: RuntimeDomain,
        probe: OwnerProbe,
        *,
        reason: str,
    ) -> None:
        if not callable(probe):
            raise ProductizationContractError(
                "owner_probe_invalid",
                f"{domain.value} replacement probe is not callable",
            )
        normalized = str(reason).strip()
        if not normalized:
            raise ProductizationContractError(
                "owner_probe_replace_reason_missing",
                "probe replacement requires a reason",
            )
        with self._lock:
            slot = self._require_slot(domain)
            slot.probe = probe
            slot.generation += 1
            slot.last_result = None
            self._revoke_leases(slot, reason="owner probe replaced")
            self._append_event(
                "owner_probe_replaced",
                domain,
                generation=slot.generation,
                owner_token=slot.binding.owner_token,
                details={"reason": normalized},
            )

    def probe(self, domain: RuntimeDomain) -> OwnerProbeResult:
        with self._lock:
            slot = self._require_slot(domain)
            if not self._enabled:
                return self._synthetic_result(
                    slot,
                    OwnerAvailability.DISABLED,
                    "runtime owner enforcement module is disabled",
                )
            if not slot.enabled:
                return self._synthetic_result(
                    slot,
                    OwnerAvailability.DISABLED,
                    slot.loss_reason or "canonical owner is disabled",
                )
            if slot.lost:
                return self._synthetic_result(
                    slot,
                    OwnerAvailability.LOST,
                    slot.loss_reason or "canonical owner is lost",
                )
            probe = slot.probe
            binding = slot.binding
            generation = slot.generation
        started = self._clock()
        try:
            raw = probe(binding)
            finished = self._clock()
            result = self._coerce_probe_result(
                binding,
                raw,
                generation=generation,
                checked_at_ns=finished,
                latency_ns=max(0, finished - started),
            )
        except Exception as error:  # noqa: BLE001 - owner health must fail closed.
            finished = self._clock()
            result = OwnerProbeResult(
                domain=binding.domain,
                availability=OwnerAvailability.FAILED,
                owner_token=binding.owner_token,
                generation=generation,
                checked_at_ns=finished,
                latency_ns=max(0, finished - started),
                write_path_ready=False,
                checkpoint_ready=False,
                recovery_ready=False,
                fallback_active=False,
                reason=f"{type(error).__name__}: {error}",
                details={"probe_exception": type(error).__name__},
            )
        with self._lock:
            current = self._require_slot(domain)
            if current.generation != generation:
                result = OwnerProbeResult(
                    domain=domain,
                    availability=OwnerAvailability.LOST,
                    owner_token=current.binding.owner_token,
                    generation=current.generation,
                    checked_at_ns=self._clock(),
                    latency_ns=result.latency_ns,
                    write_path_ready=False,
                    checkpoint_ready=False,
                    recovery_ready=False,
                    fallback_active=False,
                    reason="owner generation changed during readiness probe",
                    details={
                        "probed_generation": generation,
                        "current_generation": current.generation,
                    },
                )
            current.last_result = result
            self._append_event(
                "owner_probe_completed",
                domain,
                generation=result.generation,
                owner_token=result.owner_token,
                details={
                    "availability": result.availability.value,
                    "ready": result.ready,
                    "fallback_active": result.fallback_active,
                    "latency_ns": result.latency_ns,
                },
            )
            return result

    def probe_all(self) -> tuple[OwnerProbeResult, ...]:
        with self._lock:
            domains = tuple(sorted(self._slots, key=lambda item: item.value))
        return tuple(self.probe(domain) for domain in domains)

    def require_ready(self, domain: RuntimeDomain) -> OwnerProbeResult:
        result = self.probe(domain)
        if not result.ready:
            raise ProductizationContractError(
                "canonical_owner_unavailable",
                f"{domain.value} canonical owner is not ready: {result.reason}",
                details=result.to_dict(),
            )
        return result

    def acquire(
        self,
        domain: RuntimeDomain,
        *,
        operation: str,
        correlation_id: str,
        ttl_ns: int | None = None,
    ) -> OwnerLease:
        normalized_operation = str(operation).strip()
        normalized_correlation = str(correlation_id).strip()
        if not normalized_operation or not normalized_correlation:
            raise ProductizationContractError(
                "owner_lease_identity_missing",
                "owner lease operation and correlation id are required",
            )
        readiness = self.require_ready(domain)
        now = self._clock()
        lease_ttl = self._lease_ttl_ns if ttl_ns is None else int(ttl_ns)
        if lease_ttl < 1_000_000:
            raise ProductizationContractError(
                "owner_lease_ttl_invalid",
                "owner lease TTL must be at least one millisecond",
            )
        lease = OwnerLease(
            lease_id=stable_id(
                "owner_lease",
                domain.value,
                readiness.owner_token,
                readiness.generation,
                normalized_operation,
                normalized_correlation,
                now,
            ),
            domain=domain,
            owner_token=readiness.owner_token,
            generation=readiness.generation,
            operation=normalized_operation,
            acquired_at_ns=now,
            expires_at_ns=now + lease_ttl,
            correlation_id=normalized_correlation,
        )
        with self._lock:
            slot = self._require_slot(domain)
            if (
                slot.generation != lease.generation
                or slot.binding.owner_token != lease.owner_token
                or not slot.enabled
                or slot.lost
                or not self._enabled
            ):
                raise ProductizationContractError(
                    "owner_lease_stale_on_acquire",
                    f"{domain.value} changed while owner lease was acquired",
                )
            self._prune_expired_leases(slot, now)
            slot.active_leases[lease.lease_id] = lease
            self._append_event(
                "owner_lease_acquired",
                domain,
                generation=lease.generation,
                owner_token=lease.owner_token,
                details={
                    "lease_id": lease.lease_id,
                    "operation": lease.operation,
                    "correlation_id": lease.correlation_id,
                    "expires_at_ns": lease.expires_at_ns,
                },
            )
        return lease

    def validate_lease(self, lease: OwnerLease) -> OwnerBinding:
        now = self._clock()
        with self._lock:
            if not self._enabled:
                raise ProductizationContractError(
                    "owner_registry_disabled",
                    "runtime owner enforcement is disabled",
                )
            slot = self._require_slot(lease.domain)
            self._prune_expired_leases(slot, now)
            recorded = slot.active_leases.get(lease.lease_id)
            if recorded is None:
                raise ProductizationContractError(
                    "owner_lease_unknown",
                    f"owner lease {lease.lease_id} is not active",
                )
            if recorded != lease:
                raise ProductizationContractError(
                    "owner_lease_rebound",
                    f"owner lease {lease.lease_id} was rebound",
                )
            if lease.expires_at_ns <= now:
                slot.active_leases.pop(lease.lease_id, None)
                raise ProductizationContractError(
                    "owner_lease_expired",
                    f"owner lease {lease.lease_id} expired",
                )
            if (
                slot.generation != lease.generation
                or slot.binding.owner_token != lease.owner_token
                or not slot.enabled
                or slot.lost
            ):
                raise ProductizationContractError(
                    "owner_lease_generation_mismatch",
                    f"{lease.domain.value} owner changed after lease acquisition",
                    details={
                        "lease_generation": lease.generation,
                        "owner_generation": slot.generation,
                    },
                )
            return slot.binding

    def release(
        self,
        lease: OwnerLease,
        *,
        outcome: str,
        mutation_receipt_id: str = "",
    ) -> None:
        normalized_outcome = str(outcome).strip()
        if not normalized_outcome:
            raise ProductizationContractError(
                "owner_lease_outcome_missing",
                "owner lease release requires an outcome",
            )
        with self._lock:
            slot = self._require_slot(lease.domain)
            recorded = slot.active_leases.pop(lease.lease_id, None)
            if recorded is None:
                raise ProductizationContractError(
                    "owner_lease_release_unknown",
                    f"owner lease {lease.lease_id} is not active",
                )
            if recorded != lease:
                raise ProductizationContractError(
                    "owner_lease_release_rebound",
                    f"owner lease {lease.lease_id} changed before release",
                )
            released_at = self._clock()
            record = {
                **lease.to_dict(),
                "released_at_ns": released_at,
                "outcome": normalized_outcome,
                "mutation_receipt_id": mutation_receipt_id,
            }
            self._lease_history.append(record)
            self._append_event(
                "owner_lease_released",
                lease.domain,
                generation=lease.generation,
                owner_token=lease.owner_token,
                details={
                    "lease_id": lease.lease_id,
                    "outcome": normalized_outcome,
                    "mutation_receipt_id": mutation_receipt_id,
                },
            )

    @contextlib.contextmanager
    def lease(
        self,
        domain: RuntimeDomain,
        *,
        operation: str,
        correlation_id: str,
        ttl_ns: int | None = None,
    ) -> Iterator[OwnerLease]:
        lease = self.acquire(
            domain,
            operation=operation,
            correlation_id=correlation_id,
            ttl_ns=ttl_ns,
        )
        try:
            yield lease
        except Exception:
            self.release(lease, outcome="failed")
            raise
        else:
            self.release(lease, outcome="completed")

    def assert_lease_owner(
        self,
        lease: OwnerLease,
        *,
        owner_token: str,
        generation: int,
    ) -> None:
        self.validate_lease(lease)
        if lease.owner_token != owner_token or lease.generation != generation:
            raise ProductizationContractError(
                "owner_effect_fence_mismatch",
                "mutation effect does not match the acquired canonical owner",
                details={
                    "lease_owner_token": lease.owner_token,
                    "effect_owner_token": owner_token,
                    "lease_generation": lease.generation,
                    "effect_generation": generation,
                },
            )

    def binding(self, domain: RuntimeDomain) -> OwnerBinding:
        with self._lock:
            return self._require_slot(domain).binding

    def generation(self, domain: RuntimeDomain) -> int:
        with self._lock:
            return self._require_slot(domain).generation

    def bindings(self) -> tuple[OwnerBinding, ...]:
        with self._lock:
            return tuple(
                self._slots[domain].binding
                for domain in sorted(self._slots, key=lambda item: item.value)
            )

    def events(self) -> tuple[OwnerRegistryEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def lease_history(self) -> tuple[Mapping[str, Any], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._lease_history)

    def snapshot(self, *, probe: bool = True) -> dict[str, Any]:
        results = self.probe_all() if probe else self._cached_results()
        with self._lock:
            payload = {
                "schema": OWNER_RECEIPT_SCHEMA,
                "enabled": self._enabled,
                "sealed": self._sealed,
                "configuration_digest": self.configuration_digest(),
                "owners": [item.to_dict() for item in results],
                "active_leases": [
                    lease.to_dict()
                    for domain in sorted(self._slots, key=lambda item: item.value)
                    for lease in sorted(
                        self._slots[domain].active_leases.values(),
                        key=lambda item: item.lease_id,
                    )
                ],
                "events": [item.to_dict() for item in self._events],
            }
            payload["ready"] = (
                self._enabled
                and self._sealed
                and len(results) == len(REQUIRED_RUNTIME_DOMAINS)
                and all(item.ready for item in results)
            )
            payload["digest"] = digest_payload(payload)
            return payload

    def _cached_results(self) -> tuple[OwnerProbeResult, ...]:
        with self._lock:
            result: list[OwnerProbeResult] = []
            for domain in sorted(self._slots, key=lambda item: item.value):
                slot = self._slots[domain]
                if slot.last_result is not None:
                    result.append(slot.last_result)
                else:
                    result.append(
                        self._synthetic_result(
                            slot,
                            OwnerAvailability.UNKNOWN,
                            "owner has not been probed",
                        )
                    )
            return tuple(result)

    def _coerce_probe_result(
        self,
        binding: OwnerBinding,
        raw: OwnerProbeResult | Mapping[str, Any],
        *,
        generation: int,
        checked_at_ns: int,
        latency_ns: int,
    ) -> OwnerProbeResult:
        if isinstance(raw, OwnerProbeResult):
            if raw.domain is not binding.domain:
                raise ProductizationContractError(
                    "owner_probe_domain_mismatch",
                    f"{binding.domain.value} probe returned {raw.domain.value}",
                )
            if raw.owner_token != binding.owner_token:
                raise ProductizationContractError(
                    "owner_probe_token_mismatch",
                    f"{binding.domain.value} probe returned another owner token",
                )
            if raw.generation != generation:
                raise ProductizationContractError(
                    "owner_probe_generation_mismatch",
                    f"{binding.domain.value} probe returned a stale generation",
                )
            return raw
        if not isinstance(raw, Mapping):
            raise ProductizationContractError(
                "owner_probe_result_invalid",
                f"{binding.domain.value} probe returned {type(raw).__name__}",
            )
        availability_value = str(raw.get("availability", "")).strip().casefold()
        if not availability_value:
            availability_value = (
                OwnerAvailability.READY.value
                if bool(raw.get("ready", raw.get("ok", False)))
                else OwnerAvailability.FAILED.value
            )
        try:
            availability = OwnerAvailability(availability_value)
        except ValueError as error:
            raise ProductizationContractError(
                "owner_probe_availability_invalid",
                f"{binding.domain.value} probe returned {availability_value!r}",
            ) from error
        returned_owner = str(
            raw.get("owner_token")
            or raw.get("ownerToken")
            or binding.owner_token
        )
        if returned_owner != binding.owner_token:
            raise ProductizationContractError(
                "owner_probe_token_mismatch",
                f"{binding.domain.value} probe reported a different owner",
                details={
                    "expected": binding.owner_token,
                    "actual": returned_owner,
                },
            )
        fallback_active = bool(
            raw.get("fallback_active")
            or raw.get("fallbackActive")
            or raw.get("fallback_owner")
            or raw.get("fallbackOwner")
        )
        if fallback_active and availability is OwnerAvailability.READY:
            availability = OwnerAvailability.DEGRADED
        return OwnerProbeResult(
            domain=binding.domain,
            availability=availability,
            owner_token=binding.owner_token,
            generation=generation,
            checked_at_ns=checked_at_ns,
            latency_ns=latency_ns,
            write_path_ready=bool(
                raw.get("write_path_ready", raw.get("writePathReady", raw.get("ready", False)))
            ),
            checkpoint_ready=bool(
                raw.get(
                    "checkpoint_ready",
                    raw.get("checkpointReady", raw.get("ready", False)),
                )
            ),
            recovery_ready=bool(
                raw.get("recovery_ready", raw.get("recoveryReady", raw.get("ready", False)))
            ),
            fallback_active=fallback_active,
            reason=str(raw.get("reason") or raw.get("error") or ""),
            details={
                str(key): canonicalize(value)
                for key, value in raw.items()
                if key
                not in {
                    "availability",
                    "ready",
                    "ok",
                    "owner_token",
                    "ownerToken",
                    "write_path_ready",
                    "writePathReady",
                    "checkpoint_ready",
                    "checkpointReady",
                    "recovery_ready",
                    "recoveryReady",
                    "fallback_active",
                    "fallbackActive",
                    "fallback_owner",
                    "fallbackOwner",
                    "reason",
                    "error",
                }
            },
        )

    def _synthetic_result(
        self,
        slot: _OwnerSlot,
        availability: OwnerAvailability,
        reason: str,
    ) -> OwnerProbeResult:
        return OwnerProbeResult(
            domain=slot.binding.domain,
            availability=availability,
            owner_token=slot.binding.owner_token,
            generation=slot.generation,
            checked_at_ns=self._clock(),
            latency_ns=0,
            write_path_ready=False,
            checkpoint_ready=False,
            recovery_ready=False,
            fallback_active=False,
            reason=reason,
            details={},
        )

    def _require_slot(self, domain: RuntimeDomain) -> _OwnerSlot:
        try:
            return self._slots[domain]
        except KeyError as error:
            raise ProductizationContractError(
                "canonical_owner_unregistered",
                f"{domain.value} has no registered canonical owner",
            ) from error

    def _assert_no_cross_domain_owner_collision(self, binding: OwnerBinding) -> None:
        for slot in self._slots.values():
            if slot.binding.owner.identity != binding.owner.identity:
                continue
            raise ProductizationContractError(
                "canonical_owner_cross_domain_collision",
                "one owner symbol cannot silently acquire another state domain",
                details={
                    "existing_domain": slot.binding.domain.value,
                    "proposed_domain": binding.domain.value,
                    "owner": binding.owner.to_dict(),
                },
            )

    @staticmethod
    def _assert_no_fallback_promotion(binding: OwnerBinding) -> None:
        canonical = {
            binding.owner.identity,
            binding.store.identity,
            *(item.identity for item in binding.writers),
        }
        promoted = canonical & {item.identity for item in binding.fallback_refs}
        if promoted:
            raise ProductizationContractError(
                "fallback_promoted_to_owner",
                "fallback reference overlaps a canonical owner/write role",
                details={"references": sorted(promoted)},
            )

    def _append_event(
        self,
        kind: str,
        domain: RuntimeDomain,
        *,
        generation: int,
        owner_token: str,
        details: Mapping[str, Any],
    ) -> None:
        now = self._clock()
        event = OwnerRegistryEvent(
            event_id=stable_id(
                "owner_event",
                kind,
                domain.value,
                generation,
                owner_token,
                now,
                len(self._events),
            ),
            kind=kind,
            domain=domain,
            generation=generation,
            owner_token=owner_token,
            occurred_at_ns=now,
            details=dict(details),
        )
        self._events.append(event)

    def _prune_expired_leases(self, slot: _OwnerSlot, now: int) -> None:
        expired = [
            lease_id
            for lease_id, lease in slot.active_leases.items()
            if lease.expires_at_ns <= now
        ]
        for lease_id in expired:
            lease = slot.active_leases.pop(lease_id)
            self._lease_history.append(
                {
                    **lease.to_dict(),
                    "released_at_ns": now,
                    "outcome": "expired",
                    "mutation_receipt_id": "",
                }
            )
            self._append_event(
                "owner_lease_expired",
                slot.binding.domain,
                generation=slot.generation,
                owner_token=slot.binding.owner_token,
                details={"lease_id": lease_id},
            )

    def _revoke_leases(self, slot: _OwnerSlot, *, reason: str) -> None:
        now = self._clock()
        for lease in tuple(slot.active_leases.values()):
            self._lease_history.append(
                {
                    **lease.to_dict(),
                    "released_at_ns": now,
                    "outcome": "revoked",
                    "reason": reason,
                    "mutation_receipt_id": "",
                }
            )
            self._append_event(
                "owner_lease_revoked",
                slot.binding.domain,
                generation=slot.generation,
                owner_token=slot.binding.owner_token,
                details={"lease_id": lease.lease_id, "reason": reason},
            )
        slot.active_leases.clear()


class CallableOwnerProbe:
    """Normalizes a real owner health callable into registry probe semantics."""

    def __init__(
        self,
        callable_value: Callable[..., Any],
        *,
        write_key: str = "write_path_ready",
        checkpoint_key: str = "checkpoint_ready",
        recovery_key: str = "recovery_ready",
        fallback_keys: Sequence[str] = ("fallback_active", "fallback_owner"),
    ) -> None:
        if not callable(callable_value):
            raise TypeError("callable owner probe requires a callable")
        self.callable_value = callable_value
        self.write_key = write_key
        self.checkpoint_key = checkpoint_key
        self.recovery_key = recovery_key
        self.fallback_keys = tuple(fallback_keys)

    def __call__(self, binding: OwnerBinding) -> Mapping[str, Any]:
        signature = inspect.signature(self.callable_value)
        requires_binding = any(
            parameter.default is inspect.Parameter.empty
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            for parameter in signature.parameters.values()
        )
        raw = (
            self.callable_value(binding)
            if requires_binding
            else self.callable_value()
        )
        if isinstance(raw, bool):
            return {
                "ready": raw,
                "write_path_ready": raw,
                "checkpoint_ready": raw,
                "recovery_ready": raw,
                "probe_kind": "callable_boolean",
            }
        if not isinstance(raw, Mapping):
            to_dict = getattr(raw, "to_dict", None)
            if callable(to_dict):
                raw = to_dict()
        if not isinstance(raw, Mapping):
            raise ProductizationContractError(
                "callable_owner_probe_invalid",
                f"owner callable returned {type(raw).__name__}",
            )
        result = dict(raw)
        overall = bool(result.get("ready", result.get("ok", False)))
        result.setdefault("write_path_ready", bool(result.get(self.write_key, overall)))
        result.setdefault("checkpoint_ready", bool(result.get(self.checkpoint_key, overall)))
        result.setdefault("recovery_ready", bool(result.get(self.recovery_key, overall)))
        result.setdefault(
            "fallback_active",
            any(bool(result.get(key)) for key in self.fallback_keys),
        )
        result["probe_kind"] = "callable_owner_health"
        return result


class FilesystemOwnerProbe:
    """Exercises an owner-controlled root without claiming its stored state."""

    def __init__(
        self,
        root: Path | Callable[[], Path],
        *,
        require_directory: bool = True,
        write_probe: bool = False,
    ) -> None:
        self.root = root
        self.require_directory = require_directory
        self.write_probe = write_probe

    def __call__(self, binding: OwnerBinding) -> Mapping[str, Any]:
        root = self.root() if callable(self.root) else self.root
        resolved = Path(root).expanduser().resolve()
        exists = resolved.is_dir() if self.require_directory else resolved.exists()
        write_ready = exists
        probe_path: Path | None = None
        if exists and self.write_probe:
            probe_path = resolved / f".zyra-owner-probe-{time.time_ns()}"
            try:
                probe_path.write_bytes(binding.owner_token.encode("ascii"))
                write_ready = probe_path.read_bytes() == binding.owner_token.encode("ascii")
            finally:
                if probe_path is not None:
                    probe_path.unlink(missing_ok=True)
        ready = exists and write_ready
        return {
            "ready": ready,
            "write_path_ready": write_ready,
            "checkpoint_ready": exists,
            "recovery_ready": exists,
            "root": str(resolved),
            "probe_kind": "filesystem_owner_root",
        }


class CompositeOwnerProbe:
    """Requires every independent owner-bound probe to pass."""

    def __init__(self, *probes: OwnerProbe) -> None:
        if not probes or not all(callable(item) for item in probes):
            raise TypeError("composite owner probe requires one or more callables")
        self.probes = tuple(probes)

    def __call__(self, binding: OwnerBinding) -> Mapping[str, Any]:
        observations: list[Mapping[str, Any]] = []
        for probe in self.probes:
            raw = probe(binding)
            if isinstance(raw, OwnerProbeResult):
                observations.append(raw.to_dict())
            elif isinstance(raw, Mapping):
                observations.append(dict(raw))
            else:
                raise ProductizationContractError(
                    "composite_owner_probe_invalid",
                    f"composite child returned {type(raw).__name__}",
                )
        ready = all(bool(item.get("ready", item.get("ok", False))) for item in observations)
        return {
            "ready": ready,
            "write_path_ready": all(
                bool(item.get("write_path_ready", item.get("ready", False)))
                for item in observations
            ),
            "checkpoint_ready": all(
                bool(item.get("checkpoint_ready", item.get("ready", False)))
                for item in observations
            ),
            "recovery_ready": all(
                bool(item.get("recovery_ready", item.get("ready", False)))
                for item in observations
            ),
            "fallback_active": any(bool(item.get("fallback_active")) for item in observations),
            "observations": observations,
            "probe_kind": "composite_owner_health",
        }
