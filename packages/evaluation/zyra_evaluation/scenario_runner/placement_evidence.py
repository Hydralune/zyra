from __future__ import annotations

import math
import os
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .canonical import canonicalize, digest, new_identity, utc_now
from .errors import conflict, invalid, unavailable
from .live_models import (
    DomainInput,
    PrivacyClass,
    ProviderObservation,
    TierKind,
    TierObservation,
)


class PlacementOwnerPort(Protocol):
    def acquire_route(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        required_capabilities: Sequence[str],
        allowed_tiers: Sequence[TierKind],
        excluded_route_ids: Sequence[str],
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def migrate_route(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        route: Mapping[str, Any],
        reason: str,
        excluded_route_ids: Sequence[str],
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def execute_tiers(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        route: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]: ...

    def execute_providers(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        route: Mapping[str, Any],
        capability_count: int,
    ) -> Sequence[Mapping[str, Any]]: ...

    def disconnected_degradation(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        route: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class CallbackPlacementOwnerPort:
    def __init__(
        self,
        *,
        acquire_route: Any,
        migrate_route: Any,
        execute_tiers: Any,
        execute_providers: Any,
        disconnected_degradation: Any,
    ) -> None:
        self._acquire_route = acquire_route
        self._migrate_route = migrate_route
        self._execute_tiers = execute_tiers
        self._execute_providers = execute_providers
        self._disconnected_degradation = disconnected_degradation

    def acquire_route(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._acquire_route(**kwargs)

    def migrate_route(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._migrate_route(**kwargs)

    def execute_tiers(self, **kwargs: Any) -> Sequence[Mapping[str, Any]]:
        return self._execute_tiers(**kwargs)

    def execute_providers(self, **kwargs: Any) -> Sequence[Mapping[str, Any]]:
        return self._execute_providers(**kwargs)

    def disconnected_degradation(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._disconnected_degradation(**kwargs)


class UnboundPlacementOwnerPort:
    def _fail(self) -> Mapping[str, Any]:
        raise unavailable(
            "live_placement_owner_unbound",
            "Live scenario is not bound to the scheduler/provider/tier owners.",
            phase="placement",
        )

    def acquire_route(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()

    def migrate_route(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()

    def execute_tiers(self, **_: Any) -> Sequence[Mapping[str, Any]]:
        self._fail()
        return ()

    def execute_providers(self, **_: Any) -> Sequence[Mapping[str, Any]]:
        self._fail()
        return ()

    def disconnected_degradation(self, **_: Any) -> Mapping[str, Any]:
        return self._fail()


@dataclass(frozen=True, slots=True)
class PlacementPolicy:
    policy_id: str
    allowed_tiers: tuple[TierKind, ...]
    denied_tiers: tuple[TierKind, ...]
    maximum_cost_usd: float
    maximum_latency_ms: int
    require_real_tiers: bool
    require_real_providers: bool
    minimum_provider_capabilities: int
    credential_custody_required: bool
    disconnected_degradation_required: bool
    rationale: tuple[str, ...]

    @classmethod
    def for_input(
        cls,
        value: DomainInput,
        *,
        require_real_tiers: bool,
        require_real_providers: bool,
    ) -> "PlacementPolicy":
        sensitive = value.privacy_class in {
            PrivacyClass.CONFIDENTIAL,
            PrivacyClass.RESTRICTED,
        }
        allowed = (
            (TierKind.DEVICE, TierKind.EDGE)
            if sensitive
            else (TierKind.DEVICE, TierKind.EDGE, TierKind.CLOUD)
        )
        denied = (TierKind.CLOUD,) if sensitive else ()
        rationale = [
            f"privacy:{value.privacy_class.value}",
            f"maximum_cost_usd:{value.maximum_cost_usd}",
            f"maximum_latency_ms:{value.maximum_latency_ms}",
        ]
        if sensitive:
            rationale.append("cloud-denied-before-dispatch")
        if require_real_tiers:
            rationale.append("real-device-edge-cloud-attestation-required")
        if require_real_providers:
            rationale.append("two-provider-model-capabilities-required")
        return cls(
            policy_id=f"placement-policy:{value.input_digest[:20]}",
            allowed_tiers=allowed,
            denied_tiers=denied,
            maximum_cost_usd=value.maximum_cost_usd,
            maximum_latency_ms=value.maximum_latency_ms,
            require_real_tiers=require_real_tiers,
            require_real_providers=require_real_providers,
            minimum_provider_capabilities=2 if require_real_providers else 0,
            credential_custody_required=require_real_providers,
            disconnected_degradation_required=require_real_tiers,
            rationale=tuple(rationale),
        )

    @property
    def policy_digest(self) -> str:
        return digest(self.to_dict())

    def permits(self, tier: TierKind) -> bool:
        return tier in self.allowed_tiers and tier not in self.denied_tiers

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.live-placement-policy/v1",
            "policy_id": self.policy_id,
            "allowed_tiers": [item.value for item in self.allowed_tiers],
            "denied_tiers": [item.value for item in self.denied_tiers],
            "maximum_cost_usd": self.maximum_cost_usd,
            "maximum_latency_ms": self.maximum_latency_ms,
            "require_real_tiers": self.require_real_tiers,
            "require_real_providers": self.require_real_providers,
            "minimum_provider_capabilities": self.minimum_provider_capabilities,
            "credential_custody_required": self.credential_custody_required,
            "disconnected_degradation_required": self.disconnected_degradation_required,
            "rationale": list(self.rationale),
        }


@dataclass(frozen=True, slots=True)
class PlacementRun:
    policy: PlacementPolicy
    initial_route: dict[str, Any]
    migrated_routes: tuple[dict[str, Any], ...]
    tiers: tuple[TierObservation, ...]
    providers: tuple[ProviderObservation, ...]
    degradation: dict[str, Any]
    verification: dict[str, Any]
    started_at: str
    completed_at: str
    run_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.live-placement-run/v1",
            "policy": self.policy.to_dict(),
            "initial_route": self.initial_route,
            "migrated_routes": list(self.migrated_routes),
            "tiers": [item.to_dict() for item in self.tiers],
            "providers": [item.to_dict() for item in self.providers],
            "degradation": self.degradation,
            "verification": self.verification,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "run_digest": self.run_digest,
        }


class PlacementEvidenceRuntime:
    def __init__(self, *, owner: PlacementOwnerPort) -> None:
        self.owner = owner

    def execute(
        self,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        require_real_tiers: bool,
        require_real_providers: bool,
    ) -> PlacementRun:
        if _disabled("ZYRA_WORKER_POOL_INTEGRATION_DISABLED"):
            raise unavailable(
                "live_scheduler_disabled",
                "Formal placement has no direct-execution fallback.",
                phase="placement",
            )
        policy = PlacementPolicy.for_input(
            domain_input,
            require_real_tiers=require_real_tiers,
            require_real_providers=require_real_providers,
        )
        started_at = utc_now()
        route = dict(
            self.owner.acquire_route(
                scenario_run_id=scenario_run_id,
                domain_input=domain_input,
                required_capabilities=self._capabilities(domain_input),
                allowed_tiers=policy.allowed_tiers,
                excluded_route_ids=(),
                idempotency_key=f"{scenario_run_id}:initial-route",
            )
        )
        self._require_route(route, policy=policy, previous=None)
        tier_values = (
            tuple(
                self._tier(item)
                for item in self.owner.execute_tiers(
                    scenario_run_id=scenario_run_id,
                    domain_input=domain_input,
                    route=route,
                )
            )
            if policy.require_real_tiers
            else ()
        )
        provider_values = (
            tuple(
                self._provider(item)
                for item in self.owner.execute_providers(
                    scenario_run_id=scenario_run_id,
                    domain_input=domain_input,
                    route=route,
                    capability_count=policy.minimum_provider_capabilities,
                )
            )
            if policy.require_real_providers
            else ()
        )
        degradation = (
            dict(
                self.owner.disconnected_degradation(
                    scenario_run_id=scenario_run_id,
                    domain_input=domain_input,
                    route=route,
                )
            )
            if policy.disconnected_degradation_required
            else {
                "schema": "zyra.live-disconnected-degradation/v1",
                "event_id": new_identity("external-dispatch-excluded"),
                "observed": False,
                "safe": True,
                "relabeled_as_cloud": False,
                "route_before": str(route.get("route_id") or ""),
                "route_after": str(route.get("route_id") or ""),
                "required": False,
                "reason": (
                    "authenticated provider/cloud execution excluded by "
                    "M2-S05-02 user boundary"
                ),
            }
        )
        verification = self.verify(
            policy=policy,
            initial_route=route,
            migrated_routes=(),
            tiers=tier_values,
            providers=provider_values,
            degradation=degradation,
        )
        completed_at = utc_now()
        payload = {
            "policy": policy.to_dict(),
            "initial_route": route,
            "migrated_routes": [],
            "tiers": [item.to_dict() for item in tier_values],
            "providers": [item.to_dict() for item in provider_values],
            "degradation": degradation,
            "verification": verification,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        return PlacementRun(
            policy=policy,
            initial_route=route,
            migrated_routes=(),
            tiers=tier_values,
            providers=provider_values,
            degradation=degradation,
            verification=verification,
            started_at=started_at,
            completed_at=completed_at,
            run_digest=digest(payload),
        )

    def migrate(
        self,
        placement: PlacementRun,
        *,
        scenario_run_id: str,
        domain_input: DomainInput,
        reason: str,
    ) -> PlacementRun:
        previous = (
            placement.migrated_routes[-1]
            if placement.migrated_routes
            else placement.initial_route
        )
        excluded = tuple(
            str(item.get("route_id") or item.get("routeId") or "")
            for item in (
                placement.initial_route,
                *placement.migrated_routes,
            )
            if str(item.get("route_id") or item.get("routeId") or "")
        )
        selected = dict(
            self.owner.migrate_route(
                scenario_run_id=scenario_run_id,
                domain_input=domain_input,
                route=previous,
                reason=reason,
                excluded_route_ids=excluded,
                idempotency_key=(
                    f"{scenario_run_id}:migrate:{len(placement.migrated_routes) + 1}:"
                    f"{digest(reason)[:12]}"
                ),
            )
        )
        self._require_route(
            selected,
            policy=placement.policy,
            previous=previous,
        )
        migrated = (*placement.migrated_routes, selected)
        verification = self.verify(
            policy=placement.policy,
            initial_route=placement.initial_route,
            migrated_routes=migrated,
            tiers=placement.tiers,
            providers=placement.providers,
            degradation=placement.degradation,
        )
        payload = {
            "policy": placement.policy.to_dict(),
            "initial_route": placement.initial_route,
            "migrated_routes": list(migrated),
            "tiers": [item.to_dict() for item in placement.tiers],
            "providers": [item.to_dict() for item in placement.providers],
            "degradation": placement.degradation,
            "verification": verification,
            "started_at": placement.started_at,
            "completed_at": utc_now(),
        }
        return PlacementRun(
            policy=placement.policy,
            initial_route=placement.initial_route,
            migrated_routes=migrated,
            tiers=placement.tiers,
            providers=placement.providers,
            degradation=placement.degradation,
            verification=verification,
            started_at=placement.started_at,
            completed_at=str(payload["completed_at"]),
            run_digest=digest(payload),
        )

    def verify(
        self,
        *,
        policy: PlacementPolicy,
        initial_route: Mapping[str, Any],
        migrated_routes: Sequence[Mapping[str, Any]],
        tiers: Sequence[TierObservation],
        providers: Sequence[ProviderObservation],
        degradation: Mapping[str, Any],
    ) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        routes = (initial_route, *migrated_routes)
        route_ids: list[str] = []
        lease_ids: list[str] = []
        for route in routes:
            try:
                self._require_route(
                    route,
                    policy=policy,
                    previous=None,
                )
            except Exception as error:
                failures.append(
                    {
                        "code": "route_invalid",
                        "message": str(error),
                        "route": canonicalize(route),
                    }
                )
            route_ids.append(
                str(route.get("route_id") or route.get("routeId") or "")
            )
            lease_ids.append(
                str(route.get("lease_id") or route.get("leaseId") or "")
            )
        if len(set(route_ids)) != len(route_ids):
            failures.append(
                {
                    "code": "route_identity_reused",
                    "route_ids": route_ids,
                }
            )
        if len(set(lease_ids)) != len(lease_ids):
            failures.append(
                {
                    "code": "lease_identity_reused",
                    "lease_ids": lease_ids,
                }
            )
        tier_kinds: set[TierKind] = set()
        for item in tiers:
            try:
                item.validate()
            except Exception as error:
                failures.append(
                    {
                        "code": "tier_invalid",
                        "observation_id": item.observation_id,
                        "message": str(error),
                    }
                )
            tier_kinds.add(item.tier)
        if policy.require_real_tiers and set(TierKind) - tier_kinds:
            failures.append(
                {
                    "code": "tier_coverage_missing",
                    "missing": sorted(
                        item.value for item in set(TierKind) - tier_kinds
                    ),
                }
            )
        if TierKind.DEVICE in tier_kinds and TierKind.EDGE in tier_kinds:
            device = next(item for item in tiers if item.tier is TierKind.DEVICE)
            edge = next(item for item in tiers if item.tier is TierKind.EDGE)
            identity_overlap = {
                device.endpoint_id,
                device.runtime_id,
                device.process_id,
                device.isolation_id,
            }.intersection(
                {
                    edge.endpoint_id,
                    edge.runtime_id,
                    edge.process_id,
                    edge.isolation_id,
                }
            )
            if identity_overlap:
                failures.append(
                    {
                        "code": "edge_not_independent",
                        "shared_identity": sorted(identity_overlap),
                    }
                )
        capabilities: set[tuple[str, str]] = set()
        total_cost = 0.0
        latencies: list[int] = []
        for item in providers:
            try:
                item.validate()
            except Exception as error:
                failures.append(
                    {
                        "code": "provider_invalid",
                        "observation_id": item.observation_id,
                        "message": str(error),
                    }
                )
            capabilities.add((item.provider_id, item.model_id))
            total_cost += item.cost_usd
            latencies.append(item.latency_ms)
        if (
            policy.require_real_providers
            and len(capabilities) < policy.minimum_provider_capabilities
        ):
            failures.append(
                {
                    "code": "provider_capability_minimum",
                    "minimum": policy.minimum_provider_capabilities,
                    "observed": sorted(capabilities),
                }
            )
        if total_cost > policy.maximum_cost_usd:
            failures.append(
                {
                    "code": "cost_sla_exceeded",
                    "maximum": policy.maximum_cost_usd,
                    "observed": total_cost,
                }
            )
        if latencies and max(latencies) > policy.maximum_latency_ms:
            failures.append(
                {
                    "code": "latency_sla_exceeded",
                    "maximum": policy.maximum_latency_ms,
                    "observed": max(latencies),
                }
            )
        if policy.disconnected_degradation_required:
            if degradation.get("observed") is not True:
                failures.append({"code": "degradation_not_observed"})
            if degradation.get("safe") is not True:
                failures.append({"code": "degradation_not_safe"})
            if degradation.get("relabeled_as_cloud") is not False:
                failures.append({"code": "degradation_relabels_cloud"})
            if not degradation.get("event_id"):
                failures.append({"code": "degradation_event_missing"})
            if not degradation.get("route_before") or not degradation.get("route_after"):
                failures.append({"code": "degradation_route_missing"})
        receipt = {
            "schema": "zyra.live-placement-verification/v1",
            "valid": not failures,
            "policy_digest": policy.policy_digest,
            "route_count": len(routes),
            "route_ids": route_ids,
            "lease_ids": lease_ids,
            "tier_count": len(tiers),
            "tier_kinds": sorted(item.value for item in tier_kinds),
            "provider_capabilities": sorted(
                f"{provider}/{model}" for provider, model in capabilities
            ),
            "provider_capability_count": len(capabilities),
            "total_cost_usd": round(total_cost, 8),
            "maximum_provider_latency_ms": max(latencies) if latencies else 0,
            "degradation_digest": digest(degradation),
            "failures": failures,
        }
        receipt["receipt_digest"] = digest(receipt)
        if failures:
            raise conflict(
                "live_placement_verification_failed",
                "Live placement/tier/provider evidence failed verification.",
                phase="placement",
                detail=receipt,
            )
        return receipt

    @staticmethod
    def _capabilities(value: DomainInput) -> tuple[str, ...]:
        base = (
            "agent_task",
            "artifact_return",
            "sealed_execution",
        )
        if value.domain.value == "software_delivery":
            return (*base, "code_change", "terminal_execution")
        return (*base, "network_read", "citation_verification")

    @staticmethod
    def _require_route(
        route: Mapping[str, Any],
        *,
        policy: PlacementPolicy,
        previous: Mapping[str, Any] | None,
    ) -> None:
        route_id = str(route.get("route_id") or route.get("routeId") or "")
        lease_id = str(route.get("lease_id") or route.get("leaseId") or "")
        worker_id = str(route.get("worker_id") or route.get("workerId") or "")
        receipt_id = str(route.get("receipt_id") or route.get("receiptId") or "")
        tier_value = str(
            route.get("tier")
            or route.get("location")
            or route.get("worker_location")
            or ""
        )
        missing = [
            key
            for key, value in {
                "route_id": route_id,
                "lease_id": lease_id,
                "worker_id": worker_id,
                "receipt_id": receipt_id,
                "tier": tier_value,
            }.items()
            if not value
        ]
        if missing:
            raise conflict(
                "live_route_identity_missing",
                "Scheduler route lacks canonical route/lease/worker identity.",
                phase="placement",
                detail={"missing": missing, "route": canonicalize(route)},
            )
        try:
            tier = TierKind(tier_value)
        except ValueError as error:
            raise conflict(
                "live_route_tier_invalid",
                "Scheduler route returned an unsupported execution tier.",
                phase="placement",
                detail={"tier": tier_value},
            ) from error
        if not policy.permits(tier):
            raise conflict(
                "live_route_privacy_denied",
                "Scheduler route violates the pre-dispatch privacy policy.",
                phase="placement",
                detail={
                    "tier": tier.value,
                    "allowed": [item.value for item in policy.allowed_tiers],
                },
            )
        if previous is not None:
            previous_route = str(
                previous.get("route_id") or previous.get("routeId") or ""
            )
            previous_lease = str(
                previous.get("lease_id") or previous.get("leaseId") or ""
            )
            if route_id == previous_route or lease_id == previous_lease:
                raise conflict(
                    "live_route_migration_unchanged",
                    "Scheduler migration reused the failed route or lease.",
                    phase="placement",
                    detail={
                        "previous_route": previous_route,
                        "route": route_id,
                        "previous_lease": previous_lease,
                        "lease": lease_id,
                    },
                )
        if route.get("simulated") is True:
            raise conflict(
                "live_route_simulated",
                "Simulated scheduler route cannot satisfy a formal live scenario.",
                phase="placement",
            )

    @staticmethod
    def _tier(value: Mapping[str, Any]) -> TierObservation:
        raw_tier = str(value.get("tier") or "")
        if raw_tier == "local":
            raw_tier = "device"
        return TierObservation(
            observation_id=str(
                value.get("observation_id")
                or value.get("endpoint_id")
                or new_identity("tier")
            ),
            tier=TierKind(raw_tier),
            endpoint=str(value.get("endpoint") or ""),
            endpoint_id=str(value.get("endpoint_id") or ""),
            runtime_id=str(value.get("runtime_id") or ""),
            process_id=str(value.get("process_id") or ""),
            isolation_id=str(value.get("isolation_id") or ""),
            request_id=str(value.get("request_id") or ""),
            route_id=str(value.get("route_id") or ""),
            lease_id=str(value.get("lease_id") or ""),
            artifact_ids=tuple(
                str(item) for item in value.get("artifact_ids") or ()
            ),
            started_at=str(value.get("started_at") or ""),
            completed_at=str(value.get("completed_at") or ""),
            request_digest=str(value.get("request_digest") or ""),
            response_digest=str(value.get("response_digest") or ""),
            handshake_ok=value.get("handshake_ok") is True,
            heartbeat_ok=value.get("heartbeat_ok") is True,
            task_success=value.get("task_success") is True,
            simulated=value.get("simulated") is True,
            loopback=value.get("loopback") is True,
            metadata=dict(value.get("metadata") or {}),
        )

    @staticmethod
    def _provider(value: Mapping[str, Any]) -> ProviderObservation:
        metadata = dict(value.get("metadata") or {})
        started = str(value.get("started_at") or "")
        completed = str(value.get("completed_at") or "")
        latency_ms = int(value.get("latency_ms") or 0)
        if latency_ms <= 0 and started and completed:
            latency_ms = _duration_ms(started, completed)
        return ProviderObservation(
            observation_id=str(
                value.get("observation_id")
                or value.get("attempt_id")
                or new_identity("provider")
            ),
            provider_id=str(value.get("provider_id") or ""),
            model_id=str(value.get("model_id") or ""),
            endpoint=str(value.get("endpoint") or ""),
            request_id=str(value.get("request_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            route_id=str(value.get("route_id") or ""),
            credential_custodian=str(
                value.get("credential_custodian")
                or metadata.get("auth_custodian")
                or ""
            ),
            authenticated=(
                value.get("authenticated") is True
                or metadata.get("authenticated_session") is True
            ),
            response_status=int(value.get("response_status") or 0),
            request_digest=str(value.get("request_digest") or ""),
            response_digest=str(value.get("response_digest") or ""),
            tool_call_ids=tuple(
                str(item) for item in value.get("tool_call_ids") or ()
            ),
            tool_result_ids=tuple(
                str(item) for item in value.get("tool_result_ids") or ()
            ),
            started_at=started,
            completed_at=completed,
            cost_usd=float(
                value.get("cost_usd")
                or metadata.get("cost_usd")
                or 0
            ),
            latency_ms=latency_ms,
            metadata=metadata,
        )


def placement_events(
    placement: PlacementRun,
    *,
    run_id: str,
    task_id: str,
    starting_sequence: int,
    previous_event_id: str,
) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    sequence = starting_sequence
    parent = previous_event_id

    def emit(
        event_type: str,
        effect: str,
        stage: str,
        mutation: Mapping[str, Any],
        *,
        worker_id: str = "",
        provider_id: str = "",
    ) -> None:
        nonlocal sequence, parent
        sequence += 1
        event_id = f"placement-event-{sequence:06d}-{digest(mutation)[:12]}"
        output.append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "run_id": run_id,
                "task_id": task_id,
                "sequence": sequence,
                "causation_id": parent,
                "parent_event_id": parent,
                "created_at": utc_now(),
                "payload": {
                    "semantic_effect": effect,
                    "mutation": canonicalize(mutation),
                    "causation_id": parent,
                },
                "metadata": {
                    "stage": stage,
                    "semantic_effect": effect,
                    "worker_id": worker_id,
                    "provider_id": provider_id,
                },
            }
        )
        parent = event_id

    emit(
        "permission_decision",
        "permission",
        "placement-policy",
        {
            "policy_id": placement.policy.policy_id,
            "policy_digest": placement.policy.policy_digest,
            "allowed_tiers": [item.value for item in placement.policy.allowed_tiers],
            "denied_tiers": [item.value for item in placement.policy.denied_tiers],
            "decision": "allow",
        },
    )
    routes = (placement.initial_route, *placement.migrated_routes)
    for route_index, route in enumerate(routes):
        emit(
            "resource_decision",
            "placement",
            "scheduler",
            {
                "route_index": route_index,
                "route_id": route.get("route_id") or route.get("routeId"),
                "lease_id": route.get("lease_id") or route.get("leaseId"),
                "worker_id": route.get("worker_id") or route.get("workerId"),
                "tier": route.get("tier"),
                "receipt_id": route.get("receipt_id") or route.get("receiptId"),
            },
            worker_id=str(route.get("worker_id") or route.get("workerId") or ""),
            provider_id=str(route.get("provider_id") or ""),
        )
        emit(
            "topology_route",
            "route",
            "scheduler",
            {
                "route_index": route_index,
                "route_id": route.get("route_id") or route.get("routeId"),
                "previous_route_id": (
                    ""
                    if route_index == 0
                    else routes[route_index - 1].get("route_id")
                    or routes[route_index - 1].get("routeId")
                ),
                "reason": route.get("reason") or "initial-placement",
            },
            worker_id=str(route.get("worker_id") or route.get("workerId") or ""),
            provider_id=str(route.get("provider_id") or ""),
        )
    for item in placement.tiers:
        emit(
            "worker_dispatched",
            "placement",
            "tier-execution",
            {
                "tier": item.tier.value,
                "endpoint_id": item.endpoint_id,
                "runtime_id": item.runtime_id,
                "process_id": item.process_id,
                "isolation_id": item.isolation_id,
                "request_id": item.request_id,
                "route_id": item.route_id,
                "lease_id": item.lease_id,
                "artifact_ids": item.artifact_ids,
            },
            worker_id=item.runtime_id,
            provider_id=str(item.metadata.get("provider_id") or ""),
        )
    for item in placement.providers:
        emit(
            "provider_route",
            "route",
            "provider",
            {
                "provider_id": item.provider_id,
                "model_id": item.model_id,
                "request_id": item.request_id,
                "attempt_id": item.attempt_id,
                "route_id": item.route_id,
                "response_digest": item.response_digest,
                "tool_call_ids": item.tool_call_ids,
                "tool_result_ids": item.tool_result_ids,
            },
            worker_id=f"provider:{item.provider_id}",
            provider_id=item.provider_id,
        )
    emit(
        "recovery_applied",
        "recovery",
        "network-degradation",
        placement.degradation,
        worker_id=str(placement.degradation.get("worker_id") or ""),
        provider_id=str(placement.degradation.get("provider_id") or ""),
    )
    emit(
        "verification",
        "verification",
        "placement-verification",
        placement.verification,
    )
    return tuple(output)


def provider_stability(
    observations: Sequence[ProviderObservation],
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[ProviderObservation]] = defaultdict(list)
    for item in observations:
        grouped[(item.provider_id, item.model_id)].append(item)
    capabilities: dict[str, Any] = {}
    for (provider_id, model_id), values in sorted(grouped.items()):
        latencies = [item.latency_ms for item in values]
        costs = [item.cost_usd for item in values]
        capabilities[f"{provider_id}/{model_id}"] = {
            "sample_count": len(values),
            "success_count": sum(200 <= item.response_status < 300 for item in values),
            "request_identity_count": len({item.request_id for item in values}),
            "attempt_identity_count": len({item.attempt_id for item in values}),
            "latency_min_ms": min(latencies) if latencies else 0,
            "latency_max_ms": max(latencies) if latencies else 0,
            "latency_mean_ms": round(statistics.fmean(latencies), 3) if latencies else 0,
            "latency_p95_ms": _percentile(latencies, 0.95),
            "cost_total_usd": round(sum(costs), 8),
            "response_digest_count": len({item.response_digest for item in values}),
        }
    result = {
        "schema": "zyra.live-provider-stability/v1",
        "observation_count": len(observations),
        "capability_count": len(grouped),
        "capabilities": capabilities,
    }
    result["stability_digest"] = digest(result)
    return result


def tier_stability(observations: Sequence[TierObservation]) -> dict[str, Any]:
    grouped: dict[TierKind, list[TierObservation]] = defaultdict(list)
    for item in observations:
        grouped[item.tier].append(item)
    tiers: dict[str, Any] = {}
    for tier, values in grouped.items():
        tiers[tier.value] = {
            "sample_count": len(values),
            "endpoint_count": len({item.endpoint_id for item in values}),
            "runtime_count": len({item.runtime_id for item in values}),
            "process_count": len({item.process_id for item in values}),
            "isolation_count": len({item.isolation_id for item in values}),
            "request_count": len({item.request_id for item in values}),
            "success_count": sum(item.task_success for item in values),
            "simulated_count": sum(item.simulated for item in values),
            "loopback_count": sum(item.loopback for item in values),
        }
    result = {
        "schema": "zyra.live-tier-stability/v1",
        "observation_count": len(observations),
        "tier_count": len(grouped),
        "tiers": tiers,
    }
    result["stability_digest"] = digest(result)
    return result


def _duration_ms(started: str, completed: str) -> int:
    try:
        left = datetime.fromisoformat(started.replace("Z", "+00:00"))
        right = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((right - left).total_seconds() * 1000))


def _percentile(values: Sequence[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return int(ordered[index])


def _disabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


__all__ = [
    "CallbackPlacementOwnerPort",
    "PlacementEvidenceRuntime",
    "PlacementOwnerPort",
    "PlacementPolicy",
    "PlacementRun",
    "UnboundPlacementOwnerPort",
    "placement_events",
    "provider_stability",
    "tier_stability",
]
