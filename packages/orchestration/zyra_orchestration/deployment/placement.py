from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import PlacementDisabled, PlacementRejected
from .models import (
    DeploymentProfile,
    LifecycleStatus,
    NodeObservation,
    PlacementCandidate,
    PlacementDecision,
    ProfilePolicy,
    Sensitivity,
    Workload,
    digest,
    new_id,
    now_iso,
)
from .profiles import PROFILE_POLICY_VERSION, ProfileCatalog
from .state_store import DeploymentStateStore


@dataclass(frozen=True, slots=True)
class PlacementContext:
    observations: Mapping[DeploymentProfile, NodeObservation]
    unavailable_profiles: frozenset[DeploymentProfile] = frozenset()
    excluded_profiles: frozenset[DeploymentProfile] = frozenset()
    measured_latency_ms: Mapping[DeploymentProfile, int] | None = None
    available_memory_mb: Mapping[DeploymentProfile, int] | None = None
    allow_degraded: bool = False
    recovery: bool = False


class PlacementPolicyRuntime:
    def __init__(
        self,
        catalog: ProfileCatalog,
        store: DeploymentStateStore,
        *,
        enabled: bool = True,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.environment = dict(os.environ if environment is None else environment)
        self.enabled = enabled

    def decide(
        self,
        workload: Workload,
        context: PlacementContext,
    ) -> PlacementDecision:
        if not self.enabled or self._truthy(
            self.environment.get("ZYRA_DEPLOYMENT_PLACEMENT_DISABLED")
        ):
            raise PlacementDisabled(
                "deployment_placement_disabled",
                "deployment placement binding is disabled",
                operation="decide",
                details={"workload_id": workload.workload_id},
            )
        self._validate_workload(workload)
        self._validate_observations(context.observations)
        candidates = tuple(
            self._candidate(
                workload,
                profile=profile,
                policy=self.catalog.policy(profile),
                observation=context.observations.get(profile),
                context=context,
            )
            for profile in self.catalog.profiles()
        )
        admitted = [item for item in candidates if item.admitted]
        if not admitted:
            raise PlacementRejected(
                "deployment_no_profile_admitted",
                "no deployment profile can satisfy the workload constraints",
                operation="decide",
                details={
                    "workload_id": workload.workload_id,
                    "candidates": [item.to_dict() for item in candidates],
                },
            )
        selected = sorted(
            admitted,
            key=lambda item: (
                -item.score,
                item.observed_latency_ms,
                self._profile_order(item.profile),
            ),
        )[0]
        policy_material = {
            "version": PROFILE_POLICY_VERSION,
            "catalog_digest": self.catalog.profile_digest,
            "workload_digest": workload.workload_digest,
            "candidates": [item.to_dict() for item in candidates],
            "recovery": context.recovery,
            "allow_degraded": context.allow_degraded,
        }
        decision = PlacementDecision(
            decision_id=new_id("placement"),
            workload_id=workload.workload_id,
            selected_profile=selected.profile,
            candidates=candidates,
            policy_version=PROFILE_POLICY_VERSION,
            policy_digest=digest(policy_material),
            created_at=now_iso(),
        )
        self.store.save_placement(decision)
        return decision

    def _candidate(
        self,
        workload: Workload,
        *,
        profile: DeploymentProfile,
        policy: ProfilePolicy,
        observation: NodeObservation | None,
        context: PlacementContext,
    ) -> PlacementCandidate:
        blockers: list[str] = []
        reasons: list[str] = []
        score = 0
        if profile in context.unavailable_profiles:
            blockers.append("profile_unavailable")
        if profile in context.excluded_profiles:
            blockers.append("profile_excluded")
        if observation is None:
            blockers.append("node_observation_missing")
            observed_latency = policy.latency_budget_ms
            available_memory = 0
            credential_ready = self.catalog.credential_ready(
                profile,
                provider_required=workload.provider_required,
                preferred_provider=workload.preferred_provider,
            )
        else:
            if observation.profile is not profile:
                blockers.append("node_profile_mismatch")
            if observation.pid <= 0:
                blockers.append("node_process_identity_missing")
            if observation.status is LifecycleStatus.BLOCKED:
                blockers.append("node_blocked")
            if (
                observation.status is LifecycleStatus.DEGRADED
                and not context.allow_degraded
            ):
                blockers.append("node_degraded")
            if observation.configuration_digest != policy.configuration_digest:
                blockers.append("node_configuration_drift")
            if observation.heartbeat_sequence <= 0:
                blockers.append("node_heartbeat_missing")
            if observation.network.get("in_process") is True:
                blockers.append("node_same_process_invalid")
            observed_latency = int(
                (context.measured_latency_ms or {}).get(
                    profile,
                    observation.network.get("observed_latency_ms")
                    or policy.latency_budget_ms,
                )
            )
            available_memory = int(
                (context.available_memory_mb or {}).get(
                    profile,
                    max(
                        0,
                        policy.resource.memory_mb
                        - int(float(observation.resource.get("rss_mb") or 0)),
                    ),
                )
            )
            credential_ready = self._credential_ready_from_observation(
                workload,
                policy,
                observation,
            )
            if observation.network.get("network_down") is True:
                blockers.append("node_network_unavailable")
        if workload.sensitivity not in policy.allowed_sensitivity:
            blockers.append("sensitivity_not_allowed")
        if workload.memory_mb > policy.resource.memory_mb:
            blockers.append("memory_exceeds_profile")
        if workload.memory_mb > available_memory:
            blockers.append("memory_currently_unavailable")
        if workload.cpu_units > max(1, policy.resource.cpu_percent // 10):
            blockers.append("cpu_request_exceeds_profile")
        missing = set(workload.required_capabilities) - set(policy.capabilities)
        if missing:
            blockers.extend(f"capability_missing:{item}" for item in sorted(missing))
        if observed_latency > workload.latency_sla_ms:
            blockers.append("latency_sla_exceeded")
        if workload.provider_required:
            if "provider-dispatch" not in policy.capabilities:
                blockers.append("provider_capability_missing")
            if not credential_ready:
                blockers.append("provider_credential_missing")
            if (
                workload.preferred_provider
                and workload.preferred_provider.casefold() not in policy.providers
            ):
                blockers.append("preferred_provider_missing")
        if workload.preferred_model:
            requested_model = workload.preferred_model.casefold()
            if policy.models and requested_model not in policy.models:
                blockers.append("preferred_model_missing")
        if workload.sensitivity is Sensitivity.RESTRICTED:
            if profile is DeploymentProfile.DEVICE:
                score += 1000
                reasons.append("restricted_data_device_only")
            else:
                blockers.append("restricted_data_must_remain_device")
        elif workload.sensitivity is Sensitivity.CONFIDENTIAL:
            if profile is DeploymentProfile.DEVICE:
                score += 500
                reasons.append("confidential_data_locality")
            elif profile is DeploymentProfile.EDGE:
                score += 400
                reasons.append("confidential_data_private_edge")
        if workload.latency_sla_ms <= 100:
            if profile is DeploymentProfile.DEVICE:
                score += 350
                reasons.append("low_latency_device")
            elif profile is DeploymentProfile.EDGE:
                score += 300
                reasons.append("low_latency_edge")
            else:
                score -= 300
        if workload.complexity >= 8:
            if profile is DeploymentProfile.CLOUD:
                score += 600
                reasons.append("high_complexity_cloud_offload")
            elif profile is DeploymentProfile.EDGE:
                score += 150
                reasons.append("high_complexity_edge_degraded")
            else:
                score -= 200
        elif workload.complexity <= 3:
            if profile is DeploymentProfile.DEVICE:
                score += 220
                reasons.append("low_complexity_device_efficiency")
        if workload.provider_required and profile is DeploymentProfile.CLOUD:
            score += 700
            reasons.append("provider_capability_cloud")
        score += max(0, 200 - observed_latency)
        score += min(200, max(0, available_memory // 16))
        if context.recovery:
            score += 50
            reasons.append("recovery_successor_candidate")
        if blockers:
            score = min(score, -1000 - 10 * len(blockers))
        model_split = self._model_split(
            workload,
            profile=profile,
            policy=policy,
            admitted=not blockers,
            context=context,
        )
        return PlacementCandidate(
            profile=profile,
            admitted=not blockers,
            score=score,
            reasons=tuple(reasons),
            blockers=tuple(dict.fromkeys(blockers)),
            model_split=model_split,
            observed_latency_ms=observed_latency,
            available_memory_mb=available_memory,
            credential_ready=credential_ready,
        )

    @staticmethod
    def _model_split(
        workload: Workload,
        *,
        profile: DeploymentProfile,
        policy: ProfilePolicy,
        admitted: bool,
        context: PlacementContext,
    ) -> dict[str, Any]:
        if not admitted:
            return {
                "selected": False,
                "reason": "candidate_not_admitted",
                "local_preprocess": False,
                "remote_reasoning": False,
            }
        if profile is DeploymentProfile.CLOUD:
            return {
                "selected": True,
                "local_preprocess": True,
                "remote_reasoning": workload.complexity >= 6
                or workload.provider_required,
                "provider": workload.preferred_provider
                or (policy.providers[0] if policy.providers else ""),
                "model": workload.preferred_model
                or ("reasoning" if workload.complexity >= 8 else "general"),
                "data_projection": (
                    "redacted-derived-context"
                    if workload.sensitivity is Sensitivity.INTERNAL
                    else "public-context"
                ),
                "checkpoint_handoff": context.recovery,
            }
        if profile is DeploymentProfile.EDGE:
            return {
                "selected": True,
                "local_preprocess": True,
                "remote_reasoning": False,
                "model": "edge-local",
                "data_projection": "private-edge-context",
                "checkpoint_handoff": context.recovery,
            }
        return {
            "selected": True,
            "local_preprocess": True,
            "remote_reasoning": False,
            "model": "device-local",
            "data_projection": "device-only",
            "checkpoint_handoff": context.recovery,
        }

    @staticmethod
    def _credential_ready_from_observation(
        workload: Workload,
        policy: ProfilePolicy,
        observation: NodeObservation,
    ) -> bool:
        if not workload.provider_required:
            return True
        presence = dict(observation.credential_presence)
        if workload.preferred_provider:
            prefix = workload.preferred_provider.casefold()
            candidates = [
                name for name in policy.credential_environment
                if name.casefold().startswith(prefix)
            ]
            return any(presence.get(name, False) for name in candidates)
        return any(presence.values())

    @staticmethod
    def _validate_workload(workload: Workload) -> None:
        if not workload.workload_id or not workload.task_id or not workload.run_id:
            raise PlacementRejected(
                "deployment_workload_identity_invalid",
                "deployment workload identity is incomplete",
                operation="validate_workload",
            )
        if not workload.operation:
            raise PlacementRejected(
                "deployment_workload_operation_missing",
                "deployment workload operation is missing",
                operation="validate_workload",
            )
        if not 0 <= workload.complexity <= 10:
            raise PlacementRejected(
                "deployment_workload_complexity_invalid",
                "deployment workload complexity must be between 0 and 10",
                operation="validate_workload",
            )
        if not 1 <= workload.latency_sla_ms <= 120_000:
            raise PlacementRejected(
                "deployment_workload_sla_invalid",
                "deployment workload latency SLA is outside the supported range",
                operation="validate_workload",
            )
        if workload.cpu_units < 1 or workload.memory_mb < 1:
            raise PlacementRejected(
                "deployment_workload_resource_invalid",
                "deployment workload resource requests must be positive",
                operation="validate_workload",
            )

    @staticmethod
    def _validate_observations(
        observations: Mapping[DeploymentProfile, NodeObservation],
    ) -> None:
        pids = [
            item.pid for item in observations.values()
            if item.pid > 0
        ]
        node_ids = [
            item.node_id for item in observations.values()
            if item.node_id
        ]
        endpoints = [
            item.endpoint for item in observations.values()
            if item.endpoint
        ]
        if len(set(pids)) != len(pids):
            raise PlacementRejected(
                "deployment_profile_process_reused",
                "multiple deployment profiles share a process identity",
                operation="validate_observations",
            )
        if len(set(node_ids)) != len(node_ids):
            raise PlacementRejected(
                "deployment_profile_node_reused",
                "multiple deployment profiles share a node identity",
                operation="validate_observations",
            )
        if len(set(endpoints)) != len(endpoints):
            raise PlacementRejected(
                "deployment_profile_endpoint_reused",
                "multiple deployment profiles share an endpoint",
                operation="validate_observations",
            )

    @staticmethod
    def _profile_order(profile: DeploymentProfile) -> int:
        return {
            DeploymentProfile.DEVICE: 0,
            DeploymentProfile.EDGE: 1,
            DeploymentProfile.CLOUD: 2,
        }[profile]

    @staticmethod
    def _truthy(value: Any) -> bool:
        return value is True or str(value or "").casefold() in {
            "1",
            "true",
            "yes",
            "on",
        }


def default_workloads(task_id: str, run_id: str) -> tuple[Workload, ...]:
    return (
        Workload(
            workload_id=new_id("workload"),
            task_id=task_id,
            run_id=run_id,
            operation="hash-manifest",
            payload={"entries": {"sensitive": "device-bound"}},
            sensitivity=Sensitivity.RESTRICTED,
            complexity=2,
            latency_sla_ms=50,
            cpu_units=1,
            memory_mb=64,
            required_capabilities=("deterministic-transform",),
            idempotency_key=new_id("idempotency"),
        ),
        Workload(
            workload_id=new_id("workload"),
            task_id=task_id,
            run_id=run_id,
            operation="analyze-text",
            payload={"text": "latency bounded edge task"},
            sensitivity=Sensitivity.CONFIDENTIAL,
            complexity=5,
            latency_sla_ms=100,
            cpu_units=2,
            memory_mb=128,
            required_capabilities=("edge-compute",),
            idempotency_key=new_id("idempotency"),
        ),
        Workload(
            workload_id=new_id("workload"),
            task_id=task_id,
            run_id=run_id,
            operation="provider-capability",
            payload={"provider": "openai", "model": "reasoning"},
            sensitivity=Sensitivity.PUBLIC,
            complexity=10,
            latency_sla_ms=1500,
            cpu_units=5,
            memory_mb=512,
            required_capabilities=("provider-dispatch",),
            provider_required=True,
            preferred_provider="openai",
            preferred_model="reasoning",
            idempotency_key=new_id("idempotency"),
        ),
    )


__all__ = [
    "PlacementContext",
    "PlacementPolicyRuntime",
    "default_workloads",
]
