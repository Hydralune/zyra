from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from zyra_core import PlanNodeStatus, TaskState

from ..graph_custody import GraphStateSnapshot
from .contracts import (
    ContractHeader,
    EnvironmentSnapshot,
    FrozenDict,
    GraphSnapshotRef,
    MechanismEvidenceReadinessReportRef,
    PolicyBudget,
    PolicyContractError,
    PolicyInputSnapshot,
    PolicyNodeSnapshot,
    StableArtifactRef,
    TelemetryObservation,
    canonical_digest,
)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


class EnvironmentSnapshotBuilder:
    """Copies scheduler-owned telemetry into an immutable, attributable snapshot."""

    @staticmethod
    def from_worker_pool_projection(
        projection: Mapping[str, Any],
        *,
        header: ContractHeader,
        freshness_seconds: int = 60,
        confidence: float = 1.0,
        required_categories: Iterable[str] = ("worker",),
        additional_observations: Iterable[TelemetryObservation] = (),
    ) -> EnvironmentSnapshot:
        captured_at = str(projection.get("captured_at") or header.created_at)
        observations = list(additional_observations)
        for worker_value in projection.get("workers") or ():
            worker = _mapping(worker_value)
            telemetry = _mapping(worker.get("telemetry"))
            manifest = _mapping(worker.get("manifest"))
            health_projection = _mapping(worker.get("health"))
            resource_id = str(worker.get("worker_id") or worker.get("id") or "")
            if not resource_id:
                raise PolicyContractError("worker projection is missing worker_id")
            observed_at = str(telemetry.get("observed_at") or captured_at)
            physical_runtime_id = str(
                worker.get("process_identity")
                or worker.get("runtime_id")
                or ""
            )
            missing_fields = []
            if not physical_runtime_id:
                physical_runtime_id = f"unresolved:{resource_id}"
                missing_fields.append("physical_runtime_id")
            location = str(
                worker.get("location")
                or manifest.get("location")
                or manifest.get("backend")
                or "local"
            )
            health = str(
                health_projection.get("status")
                or worker.get("health")
                or ""
            ).lower()
            state = str(worker.get("state") or worker.get("status") or "").lower()
            available = state in {"idle", "busy"} and health in {"healthy", "degraded"}
            healthy = health == "healthy"
            accepting = available and state in {"idle", "busy"}
            capacity_vector = _mapping(telemetry.get("capacity"))
            allocated_vector = _mapping(telemetry.get("allocated"))
            capacity = max(
                0.0,
                float(
                    telemetry.get("capacity_available")
                    or telemetry.get("available_slots")
                    or (
                        float(capacity_vector.get("process_slots") or 0)
                        - float(allocated_vector.get("process_slots") or 0)
                    )
                    or (
                        float(capacity_vector.get("cpu_cores") or 0)
                        - float(allocated_vector.get("cpu_cores") or 0)
                    )
                    or 0
                ),
            )
            if not telemetry:
                missing_fields.append("telemetry")
            if not health_projection:
                missing_fields.append("health")
            constraints = _mapping(manifest.get("constraints"))
            observations.append(
                TelemetryObservation(
                    observation_id=f"obs-{canonical_digest((resource_id, observed_at, telemetry))[:24]}",
                    resource_id=resource_id,
                    category="worker",
                    observed_at=observed_at,
                    fresh_until=_iso(_timestamp(observed_at) + timedelta(seconds=max(1, freshness_seconds))),
                    confidence=confidence,
                    observation_source="ResourceScheduler.worker_pool_api_projection",
                    source_event_id=header.source_event_id,
                    physical_runtime_id=physical_runtime_id,
                    location=location,
                    available=available,
                    healthy=healthy,
                    load=float(
                        telemetry.get("load_average")
                        or telemetry.get("load")
                        or telemetry.get("utilization")
                        or 0
                    ),
                    capacity_available=capacity,
                    lease_available=accepting,
                    recent_failures=int(
                        telemetry.get("recent_failures")
                        or (1 if health in {"lost", "unrecoverable"} else 0)
                    ),
                    latency_p50_ms=float(telemetry.get("latency_p50_ms") or 0),
                    latency_p95_ms=float(telemetry.get("latency_p95_ms") or 0),
                    cost_usd=float(telemetry.get("cost_usd") or 0),
                    privacy_classes=tuple(
                        worker.get("privacy_classes")
                        or manifest.get("privacy_classes")
                        or constraints.get("privacy_classes")
                        or ("internal",)
                    ),
                    allowed_placements=tuple(
                        worker.get("allowed_placements")
                        or manifest.get("allowed_placements")
                        or (location,)
                    ),
                    missing_fields=tuple(missing_fields),
                    attributes=FrozenDict(
                        {
                            "backend": manifest.get("backend") or worker.get("backend") or "",
                            "backend_id": worker.get("backend_id") or "",
                            "capabilities": manifest.get("capabilities") or (),
                            "active_lease_ids": [
                                item.get("lease_id")
                                for item in worker.get("active_leases") or ()
                                if isinstance(item, Mapping)
                            ],
                            "manifest_digest": canonical_digest(manifest),
                            "telemetry_digest": canonical_digest(telemetry),
                        }
                    ),
                )
            )
        categories = tuple(sorted({item.category for item in observations}))
        required = tuple(sorted({str(item) for item in required_categories}))
        missing = tuple(item for item in required if item not in categories)
        return EnvironmentSnapshot(
            header=header,
            observed_at=captured_at,
            observations=tuple(observations),
            required_categories=required,
            missing_categories=missing,
        )


class PolicyInputSnapshotBuilder:
    """Reads existing owners and emits a detached value object; it owns no state."""

    @staticmethod
    def build(
        *,
        task: TaskState,
        graph: GraphStateSnapshot,
        environment: EnvironmentSnapshot,
        header: ContractHeader,
        budget: PolicyBudget,
        readiness_refs: Iterable[MechanismEvidenceReadinessReportRef],
        registry_versions: Mapping[str, Any],
        memory_refs: Iterable[StableArtifactRef] = (),
        phase: str = "phase2",
        requirement_revision: str = "current",
        registered_roles: Iterable[str] = (),
        registered_capabilities: Iterable[str] = (),
        allowed_permissions: Iterable[str] = (),
        allowed_placements: Iterable[str] = (),
        privacy_class: str = "internal",
        last_topology_change_at: str = "",
    ) -> PolicyInputSnapshot:
        if task.run_id != graph.run_id:
            raise PolicyContractError("task and graph snapshots belong to different runs")
        roles = set(registered_roles)
        capabilities = set(registered_capabilities)
        nodes = []
        for node in graph.nodes:
            roles.add(node.role)
            capabilities.update(node.capabilities)
            nodes.append(
                PolicyNodeSnapshot(
                    node_id=node.node_id,
                    role=node.role,
                    capabilities=node.capabilities,
                    dependencies=node.dependencies,
                    state=node.state.value,
                    revision=node.revision,
                    labels=FrozenDict(node.labels),
                    metadata=FrozenDict(node.metadata),
                )
            )
        terminal = {
            PlanNodeStatus.COMPLETED,
            PlanNodeStatus.FAILED,
            PlanNodeStatus.CANCELLED,
            PlanNodeStatus.SUPERSEDED,
        }
        obligations = []
        for node in task.plan_nodes.values():
            if node.status not in terminal:
                obligations.extend(node.constraints.requirements)
                obligations.extend(node.completion_criteria)
        return PolicyInputSnapshot(
            header=header,
            run_id=task.run_id,
            task_id=task.task_id,
            phase=phase,
            requirement_revision=requirement_revision,
            graph=GraphSnapshotRef(
                graph_id=graph.graph_id,
                run_id=graph.run_id,
                revision=graph.revision,
                signature=graph.signature,
                commit_id=graph.commit_id,
            ),
            nodes=tuple(nodes),
            registered_roles=tuple(roles),
            registered_capabilities=tuple(capabilities),
            unresolved_obligations=tuple(obligations),
            registry_versions=FrozenDict(registry_versions),
            environment=environment,
            memory_refs=tuple(memory_refs),
            readiness_refs=tuple(readiness_refs),
            budget=budget,
            allowed_permissions=tuple(allowed_permissions),
            allowed_placements=tuple(allowed_placements),
            privacy_class=privacy_class,
            last_topology_change_at=last_topology_change_at or graph.created_at,
        )
