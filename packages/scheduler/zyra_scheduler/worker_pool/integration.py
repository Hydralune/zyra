from __future__ import annotations

import copy
import os
from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, Sequence

from zyra_orchestration.graph_custody import (
    DynamicTopologyRuntime,
    GraphEdge,
    GraphNode,
    GraphStateCustody,
    NodeExecutionState,
)

from .admission import AdmissionPorts, WorkerAdmissionRuntime
from .application import WorkerPoolFoundationRuntime
from .checkpoint import ExactWorkerCheckpointRuntime, StartupIntegrationRecovery
from .control import ProjectionControlPort, WakeExecutionPort, WorkerControlRuntime
from .errors import WorkerPoolError, WorkerPoolErrorCode
from .execution_gate import WorkerExecutionGateRuntime
from .integration_models import (
    AdmissionPhase,
    AdmissionPolicy,
    AdmissionResult,
    DispatchAdmissionRequest,
    ForeignStateRef,
    IntegrationOutcome,
    PhysicalDispatchBinding,
    ProgressReceipt,
    RecoveryDisposition,
    RecoveryEvidence,
    TypedYieldReceipt,
    YieldKind,
)
from .integration_store import WorkerPoolIntegrationRepository
from .invariants import WorkerPoolInvariantRuntime
from .health_bridge import WorkerHealthBridgeRuntime, WorkerRouteHealthSink
from .models import (
    AttemptState,
    CapabilityRequirement,
    ExecutionOutcome,
    LeaseState,
    ResourceVector,
    WorkerLocation,
    new_pool_id,
    stable_digest,
    utc_iso,
)
from .renewal import LeaseRenewalRuntime, RenewalBackendHealthPort
from .projection import WorkerPoolProjectionRuntime
from .recovery_handoff import WorkerRecoveryHandoffRuntime
from .scheduler_bridge import WorkerPoolSchedulerBridge


class EdgeExecutionPort(Protocol):
    def execute(
        self,
        binding: PhysicalDispatchBinding,
        payload: Mapping[str, Any],
        *,
        fence_token: str,
    ) -> Mapping[str, Any]: ...
    def cancel(self, binding: PhysicalDispatchBinding, *, reason: str) -> bool: ...


class EventSinkPort(Protocol):
    def append(self, event: Mapping[str, Any]) -> str: ...


@dataclass(frozen=True, slots=True)
class DispatchStartReceipt:
    binding: PhysicalDispatchBinding
    backend_dispatch_id: str
    attempt_state: str
    graph_ref: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "backend_dispatch_id": self.backend_dispatch_id,
            "attempt_state": self.attempt_state,
            "graph_ref": copy.deepcopy(dict(self.graph_ref)),
        }


@dataclass(frozen=True, slots=True)
class EdgeDispatchReceipt:
    binding: PhysicalDispatchBinding
    accepted: bool
    gateway_action_ref: str
    artifact_refs: tuple[str, ...]
    telemetry: Mapping[str, Any]
    result: Mapping[str, Any]
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "accepted": self.accepted,
            "gateway_action_ref": self.gateway_action_ref,
            "artifact_refs": list(self.artifact_refs),
            "telemetry": copy.deepcopy(dict(self.telemetry)),
            "result": copy.deepcopy(dict(self.result)),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class FailoverReceipt:
    failed_binding: PhysicalDispatchBinding
    successor: AdmissionResult
    recovery: RecoveryEvidence
    graph_ref: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "failed_binding": self.failed_binding.to_dict(),
            "successor": self.successor.to_dict(),
            "recovery": self.recovery.to_dict(),
            "graph_ref": copy.deepcopy(dict(self.graph_ref)),
        }


class WorkerPoolIntegrationRuntime:
    """07A integration owner connecting task, route, worker and graph custody."""

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        graph_custody: GraphStateCustody,
        *,
        policy: AdmissionPolicy | None = None,
        admission_ports: AdmissionPorts | None = None,
        projection_control: ProjectionControlPort | None = None,
        backend_health: RenewalBackendHealthPort | WorkerRouteHealthSink | None = None,
        edge_execution: EdgeExecutionPort | None = None,
        event_sink: EventSinkPort | None = None,
        wake_execution: WakeExecutionPort | None = None,
    ) -> None:
        self.pool = pool
        self.graph_custody = graph_custody
        self.topology = DynamicTopologyRuntime(graph_custody)
        self.repository = WorkerPoolIntegrationRepository(pool.store)
        self.policy = policy or AdmissionPolicy()
        if admission_ports is None:
            admission_ports = AdmissionPorts(backend_health=backend_health)
        self.admission = WorkerAdmissionRuntime(
            pool,
            self.repository,
            policy=self.policy,
            ports=admission_ports,
        )
        self.renewal = LeaseRenewalRuntime(
            pool,
            self.repository,
            policy=self.policy,
            backend_health=backend_health,
        )
        self.control = WorkerControlRuntime(
            pool,
            self.repository,
            projection_control=projection_control,
            execution_cancellation=edge_execution,
            wake_execution=wake_execution,
        )
        self.execution_gate = WorkerExecutionGateRuntime(pool, self.repository)
        self.checkpoints = ExactWorkerCheckpointRuntime(pool, self.repository, graph_custody)
        self.invariants = WorkerPoolInvariantRuntime(pool, self.repository, graph_custody)
        self.projection = WorkerPoolProjectionRuntime(pool, self.repository, graph_custody)
        self.recovery_handoff = WorkerRecoveryHandoffRuntime(pool, self.repository)
        self.health_bridge = WorkerHealthBridgeRuntime(
            pool,
            self.repository,
            backend_health=(
                backend_health
                if backend_health is not None and hasattr(backend_health, "record_worker_health")
                else None
            ),
        )
        self.scheduler = WorkerPoolSchedulerBridge(self)
        self.edge_execution = edge_execution
        self.event_sink = event_sink

    def admit(
        self,
        request: DispatchAdmissionRequest,
        *,
        graph_node_id: str = "",
    ) -> AdmissionResult:
        node_id = graph_node_id or str(request.metadata.get("graph_node_id") or "")
        graph_id = request.foreign_refs.graph.object_id
        if node_id:
            graph = self.graph_custody.current(graph_id)
            if node_id not in graph.node_map:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.STORE_CONFLICT,
                    "physical admission cannot bind an unknown dynamic graph node",
                    operation="bind_integration_graph_attempt",
                    task_id=request.task_id,
                    metadata={"graph_id": graph_id, "node_id": node_id},
                )
        result = self.admission.admit(request)
        binding = result.binding
        if not node_id:
            return result
        graph = self.graph_custody.current(graph_id)
        if node_id not in graph.node_map:
            self._cancel_graph_binding(binding, reason="dynamic graph node disappeared during admission")
            raise WorkerPoolError(
                WorkerPoolErrorCode.STORE_CONFLICT,
                "dynamic graph node disappeared while physical admission committed",
                operation="bind_integration_graph_attempt",
                task_id=request.task_id,
                attempt_id=binding.attempt_id,
                lease_id=binding.lease_id,
                metadata={"graph_id": graph_id, "node_id": node_id},
            )
        existing_node = graph.node_map[node_id]
        if result.reused:
            if (
                existing_node.physical_attempt_ref == binding.attempt_id
                and existing_node.worker_lease_ref == binding.lease_id
                and existing_node.backend_route_ref
                == request.foreign_refs.backend_route.object_id
            ):
                graph_ref = self.topology.version_ref(graph_id)
                if (
                    binding.foreign_refs.graph.revision == graph_ref.revision
                    and binding.foreign_refs.graph.digest == graph_ref.signature
                    and str(binding.metadata.get("graph_node_id") or "") == node_id
                ):
                    return result
                refs = replace(
                    binding.foreign_refs,
                    graph=ForeignStateRef(
                        owner="GraphStateCustody",
                        kind="dynamic_graph",
                        object_id=graph_ref.graph_id,
                        revision=graph_ref.revision,
                        digest=graph_ref.signature,
                    ),
                )
                repaired = self.repository.update_binding(
                    binding.advance(
                        foreign_refs=refs,
                        metadata={
                            **dict(binding.metadata),
                            "graph_node_id": node_id,
                            "graph_replay_repaired": True,
                        },
                    ),
                    expected_version=binding.version,
                    operation="dispatch_graph_replay_repaired",
                    payload={"graph_ref": graph_ref.to_dict(), "node_id": node_id},
                )
                return AdmissionResult(
                    binding=repaired,
                    candidates=result.candidates,
                    reused=True,
                )
            if existing_node.physical_attempt_ref or existing_node.worker_lease_ref:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.STORE_CONFLICT,
                    "idempotent admission replay found a graph node bound to another attempt",
                    operation="bind_integration_graph_attempt",
                    task_id=request.task_id,
                    attempt_id=binding.attempt_id,
                    lease_id=binding.lease_id,
                    metadata={
                        "graph_id": graph_id,
                        "node_id": node_id,
                        "graph_attempt_ref": existing_node.physical_attempt_ref,
                        "graph_lease_ref": existing_node.worker_lease_ref,
                    },
                )
        commit = self.topology.bind_physical_attempt(
            graph_id,
            node_id,
            physical_attempt_ref=binding.attempt_id,
            worker_lease_ref=binding.lease_id,
            backend_route_ref=request.foreign_refs.backend_route.object_id,
            actor_id="worker-pool-integration",
            causation_id=binding.lease_id,
        )
        if not commit.receipt.committed:
            self._cancel_graph_binding(
                binding,
                reason="dynamic graph physical binding conflicted",
                payload={"graph_commit": commit.receipt.to_dict()},
            )
            raise WorkerPoolError(
                WorkerPoolErrorCode.STORE_CONFLICT,
                "dynamic graph rejected the physical attempt binding",
                operation="bind_integration_graph_attempt",
                task_id=request.task_id,
                attempt_id=binding.attempt_id,
                lease_id=binding.lease_id,
                metadata={"graph_commit": commit.receipt.to_dict()},
            )
        graph_ref = self.topology.version_ref(graph_id)
        refs = replace(
            binding.foreign_refs,
            graph=ForeignStateRef(
                owner="GraphStateCustody",
                kind="dynamic_graph",
                object_id=graph_ref.graph_id,
                revision=graph_ref.revision,
                digest=graph_ref.signature,
            ),
        )
        updated = binding.advance(
            foreign_refs=refs,
            metadata={
                **dict(binding.metadata),
                "graph_node_id": node_id,
                "graph_commit": commit.receipt.to_dict(),
            },
        )
        persisted = self.repository.update_binding(
            updated,
            expected_version=binding.version,
            operation="dispatch_graph_bound",
            payload={"graph_ref": graph_ref.to_dict(), "node_id": node_id},
        )
        return AdmissionResult(binding=persisted, candidates=result.candidates, reused=result.reused)

    def _cancel_graph_binding(
        self,
        binding: PhysicalDispatchBinding,
        *,
        reason: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        lease = self.pool.store.get_lease(binding.lease_id)
        if lease is not None and not lease.terminal:
            self.pool.leases.cancel(binding.lease_id, reason=reason)
        current = self.repository.get_binding(binding.binding_id)
        if current is not None and not current.terminal:
            self.repository.update_binding(
                current.advance(AdmissionPhase.CANCELLED),
                expected_version=current.version,
                operation="dispatch_graph_binding_failed",
                payload={"reason": reason, **dict(payload or {})},
            )

    def add_runtime_node_for_requirement(
        self,
        *,
        graph_id: str,
        logical_task_id: str,
        role: str,
        capabilities: Sequence[str],
        dependencies: Sequence[str] = (),
        connect_from: Sequence[str] = (),
        workspace_ref: str = "",
        actor_id: str = "worker-pool-integration",
        causation_id: str = "requirement-change",
        node_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Create a runtime node outside the graph's original compiled set."""

        if os.getenv("ZYRA_DYNAMIC_GRAPH_COMMIT_DISABLED") == "1":
            raise WorkerPoolError(
                WorkerPoolErrorCode.EXECUTION_REJECTED,
                "dynamic graph commit runtime is disabled",
                operation="add_runtime_worker_node",
                task_id=logical_task_id,
            )
        selected_node_id = node_id or new_pool_id("runtime-node")
        node = GraphNode(
            node_id=selected_node_id,
            role=role,
            capabilities=tuple(capabilities),
            dependencies=tuple(dependencies),
            state=NodeExecutionState.PLANNED,
            logical_task_id=logical_task_id,
            workspace_ref=workspace_ref,
            metadata={
                **dict(metadata or {}),
                "created_during_run": True,
                "precompiled": False,
                "creation_reason": causation_id,
            },
        )
        node_commit = self.topology.add_node(
            graph_id,
            node,
            actor_id=actor_id,
            causation_id=causation_id,
        )
        if not node_commit.receipt.committed:
            return {"node": node.to_dict(), "node_commit": node_commit.to_dict(), "edge_commits": []}
        edges: list[Mapping[str, Any]] = []
        for source_id in sorted(set(connect_from)):
            edge = GraphEdge(
                edge_id=f"runtime-edge:{source_id}:{selected_node_id}",
                source_node_id=source_id,
                target_node_id=selected_node_id,
                relation="runtime_requirement",
                metadata={"created_during_run": True, "precompiled": False},
            )
            result = self.topology.add_edge(
                graph_id,
                edge,
                actor_id=actor_id,
                causation_id=causation_id,
            )
            edges.append(result.to_dict())
            if not result.receipt.committed:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.STORE_CONFLICT,
                    "runtime node committed but its dynamic edge conflicted",
                    operation="add_runtime_worker_node",
                    task_id=logical_task_id,
                    metadata={"node_commit": node_commit.to_dict(), "edge_commit": result.to_dict()},
                )
        return {
            "node": node.to_dict(),
            "node_commit": node_commit.to_dict(),
            "edge_commits": edges,
            "version_ref": self.topology.version_ref(graph_id).to_dict(),
        }

    def start(
        self,
        binding_id: str,
        *,
        fence_token: str,
        backend_dispatch_id: str,
    ) -> DispatchStartReceipt:
        binding = self._require_binding(binding_id)
        if binding.phase in {AdmissionPhase.DISPATCHED, AdmissionPhase.DRAINING}:
            attempt = self.pool.store.require_attempt(binding.attempt_id)
            if attempt.state is not AttemptState.RUNNING:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                    "dispatch binding reports started but canonical attempt is not running",
                    operation="start_integrated_dispatch",
                    task_id=binding.task_id,
                    attempt_id=binding.attempt_id,
                    lease_id=binding.lease_id,
                )
            return DispatchStartReceipt(
                binding=binding,
                backend_dispatch_id=attempt.backend_dispatch_id,
                attempt_state=attempt.state.value,
                graph_ref=binding.foreign_refs.graph.to_dict(),
            )
        if binding.phase is AdmissionPhase.PARKED:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                "parked dispatch must be revived before it can start",
                operation="start_integrated_dispatch",
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                lease_id=binding.lease_id,
            )
        if binding.phase is not AdmissionPhase.ADMITTED:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                f"dispatch cannot start from {binding.phase.value}",
                operation="start_integrated_dispatch",
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                lease_id=binding.lease_id,
            )
        attempt = self.pool.leases.start_attempt(
            binding.lease_id,
            worker_id=binding.worker_id,
            fence_token=fence_token,
            fence_epoch=binding.fence_epoch,
            backend_dispatch_id=backend_dispatch_id,
        )
        target = AdmissionPhase.DRAINING if self.pool.store.require_lease(binding.lease_id).state is LeaseState.DRAINING else AdmissionPhase.DISPATCHED
        updated = binding.advance(target, backend_dispatch_id=backend_dispatch_id)
        persisted = self.repository.update_binding(
            updated,
            expected_version=binding.version,
            operation="dispatch_started",
            payload={"backend_dispatch_id": backend_dispatch_id, "attempt_state": attempt.state.value},
        )
        self._emit(
            "worker_dispatch_started",
            persisted,
            {"backend_dispatch_id": backend_dispatch_id},
        )
        return DispatchStartReceipt(
            binding=persisted,
            backend_dispatch_id=backend_dispatch_id,
            attempt_state=attempt.state.value,
            graph_ref=persisted.foreign_refs.graph.to_dict(),
        )

    def progress(
        self,
        binding_id: str,
        *,
        fence_token: str,
        sequence: int,
        payload: Mapping[str, Any],
        renew_ttl_seconds: float | None = None,
        force_renewal: bool = False,
    ) -> ProgressReceipt:
        binding = self._require_binding(binding_id)
        if binding.terminal or binding.phase not in {
            AdmissionPhase.DISPATCHED,
            AdmissionPhase.PARKED,
            AdmissionPhase.DRAINING,
        }:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_CONFLICT,
                "progress requires a running, parked, or draining physical dispatch",
                operation="record_integrated_progress",
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                lease_id=binding.lease_id,
            )
        if sequence <= binding.progress_sequence:
            selected_digest = stable_digest(payload)
            if sequence == binding.progress_sequence and selected_digest == binding.progress_digest:
                return ProgressReceipt(binding=binding, renewal=None, accepted=False, reason="idempotent progress replay")
            raise WorkerPoolError(
                WorkerPoolErrorCode.STORE_CONFLICT,
                "progress sequence is not monotonic",
                operation="record_integrated_progress",
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                lease_id=binding.lease_id,
                metadata={"current": binding.progress_sequence, "requested": sequence},
            )
        self.pool.leases.assert_fence(
            binding.lease_id,
            worker_id=binding.worker_id,
            fence_token=fence_token,
            fence_epoch=binding.fence_epoch,
            operation="record_integrated_progress",
        )
        renewal = self.renewal.renew_if_due(
            binding,
            fence_token=fence_token,
            progress_sequence=sequence,
            ttl_seconds=renew_ttl_seconds,
            force=force_renewal,
        )
        updated = binding.advance(
            progress_sequence=sequence,
            progress_digest=stable_digest(payload),
            metadata={
                **dict(binding.metadata),
                "last_progress": copy.deepcopy(dict(payload)),
                "last_renewal_id": renewal.renewal_id,
                "last_renewal_disposition": renewal.disposition.value,
            },
        )
        persisted = self.repository.update_binding(
            updated,
            expected_version=binding.version,
            operation="dispatch_progressed",
            payload={
                "progress_sequence": sequence,
                "progress_digest": updated.progress_digest,
                "renewal_id": renewal.renewal_id,
                "renewal_disposition": renewal.disposition.value,
            },
        )
        self._emit(
            "worker_dispatch_progress",
            persisted,
            {"sequence": sequence, "renewal": renewal.to_dict()},
        )
        return ProgressReceipt(binding=persisted, renewal=renewal, accepted=True, reason="progress committed")

    def yield_once(
        self,
        binding_id: str,
        *,
        fence_token: str,
        kind: YieldKind,
        summary: str,
        payload: Mapping[str, Any],
        artifact_refs: Sequence[str] = (),
        event_refs: Sequence[str] = (),
    ) -> TypedYieldReceipt:
        binding = self._require_binding(binding_id)
        existing = self.repository.typed_yield_for_binding(binding.binding_id)
        candidate_payload = {
            "kind": kind.value,
            "summary": summary,
            "payload": copy.deepcopy(dict(payload)),
            "artifact_refs": sorted(set(artifact_refs)),
            "event_refs": sorted(set(event_refs)),
        }
        if existing is not None:
            comparable = {
                "kind": existing.kind.value,
                "summary": existing.summary,
                "payload": dict(existing.payload),
                "artifact_refs": list(existing.artifact_refs),
                "event_refs": list(existing.event_refs),
            }
            if stable_digest(comparable) != stable_digest(candidate_payload):
                raise WorkerPoolError(
                    WorkerPoolErrorCode.IDEMPOTENCY_CONFLICT,
                    "physical attempt attempted a second typed yield",
                    operation="commit_integrated_typed_yield",
                    task_id=binding.task_id,
                    attempt_id=binding.attempt_id,
                    lease_id=binding.lease_id,
                )
            return existing
        self.pool.leases.assert_fence(
            binding.lease_id,
            worker_id=binding.worker_id,
            fence_token=fence_token,
            fence_epoch=binding.fence_epoch,
            operation="commit_integrated_typed_yield",
        )
        receipt = TypedYieldReceipt(
            yield_id="yield:" + stable_digest(
                {"binding_id": binding.binding_id, **candidate_payload}
            )[:32],
            task_id=binding.task_id,
            run_id=binding.run_id,
            binding_id=binding.binding_id,
            attempt_id=binding.attempt_id,
            lease_id=binding.lease_id,
            kind=kind,
            sequence=1,
            summary=summary,
            payload=copy.deepcopy(dict(payload)),
            artifact_refs=tuple(artifact_refs),
            event_refs=tuple(event_refs),
        )
        updated = binding.advance(typed_yield_id=receipt.yield_id)
        persisted = self.repository.append_typed_yield(
            receipt,
            binding=updated,
            expected_binding_version=binding.version,
        )
        self._emit("worker_typed_yield", updated, persisted.to_dict())
        return persisted

    def complete(
        self,
        binding_id: str,
        *,
        fence_token: str,
        outcome: ExecutionOutcome,
        summary: str,
        artifact_refs: Sequence[str] = (),
        event_refs: Sequence[str] = (),
        backend_receipt_ref: str = "",
        gateway_receipt_ref: str = "",
        error_code: str = "",
        error_message: str = "",
        result_payload: Mapping[str, Any] | None = None,
    ) -> IntegrationOutcome:
        binding = self._require_binding(binding_id)
        if binding.terminal:
            receipts = self.pool.store.receipts_for_task(binding.task_id)
            receipt = next((item for item in receipts if item.attempt_id == binding.attempt_id), None)
            if receipt is None:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.STORE_CORRUPTION,
                    "terminal dispatch binding has no execution receipt",
                    operation="complete_integrated_dispatch",
                    task_id=binding.task_id,
                    attempt_id=binding.attempt_id,
                )
            return IntegrationOutcome(
                binding=binding,
                outcome=receipt.outcome,
                execution_receipt=receipt.to_dict(),
                typed_yield=self.repository.typed_yield_for_binding(binding.binding_id),
            )
        self.invariants.assert_completion(binding)
        typed_yield = self.repository.typed_yield_for_binding(binding.binding_id)
        if typed_yield is None:
            yield_kind = {
                ExecutionOutcome.SUCCEEDED: YieldKind.RESULT,
                ExecutionOutcome.CANCELLED: YieldKind.CANCELLED,
            }.get(outcome, YieldKind.FAILED)
            typed_yield = self.yield_once(
                binding.binding_id,
                fence_token=fence_token,
                kind=yield_kind,
                summary=summary,
                payload=dict(result_payload or {"outcome": outcome.value}),
                artifact_refs=artifact_refs,
                event_refs=event_refs,
            )
            binding = self._require_binding(binding.binding_id)
        receipt = self.pool.leases.complete(
            binding.lease_id,
            worker_id=binding.worker_id,
            fence_token=fence_token,
            fence_epoch=binding.fence_epoch,
            outcome=outcome,
            summary=summary,
            artifact_refs=artifact_refs,
            event_refs=event_refs,
            backend_receipt_ref=backend_receipt_ref,
            gateway_receipt_ref=gateway_receipt_ref,
            error_code=error_code,
            error_message=error_message,
            metadata={
                "integration_binding_id": binding.binding_id,
                "typed_yield_id": typed_yield.yield_id,
                "foreign_refs": binding.foreign_refs.to_dict(),
                **copy.deepcopy(dict(result_payload or {})),
            },
        )
        phase = {
            ExecutionOutcome.SUCCEEDED: AdmissionPhase.SUCCEEDED,
            ExecutionOutcome.CANCELLED: AdmissionPhase.CANCELLED,
            ExecutionOutcome.FENCED: AdmissionPhase.SUPERSEDED,
        }.get(outcome, AdmissionPhase.FAILED)
        current = self._require_binding(binding.binding_id)
        updated = current.advance(
            phase,
            metadata={
                **dict(current.metadata),
                "execution_receipt_id": receipt.receipt_id,
                "execution_outcome": outcome.value,
            },
        )
        persisted = self.repository.update_binding(
            updated,
            expected_version=current.version,
            operation=f"dispatch_{phase.value}",
            payload={"receipt_id": receipt.receipt_id, "outcome": outcome.value},
        )
        self._emit("worker_dispatch_terminal", persisted, receipt.to_dict())
        return IntegrationOutcome(
            binding=persisted,
            outcome=outcome,
            execution_receipt=receipt.to_dict(),
            typed_yield=typed_yield,
        )

    def execute_edge(
        self,
        binding_id: str,
        *,
        fence_token: str,
        payload: Mapping[str, Any],
    ) -> EdgeDispatchReceipt:
        binding = self._require_binding(binding_id)
        worker = self.pool.store.require_worker(binding.worker_id)
        if not binding.edge_only or worker.location is not WorkerLocation.EDGE:
            raise WorkerPoolError(
                WorkerPoolErrorCode.CAPABILITY_MISMATCH,
                "edge execution requires an edge-only binding on an edge worker",
                operation="execute_integrated_edge_dispatch",
                task_id=binding.task_id,
                worker_id=binding.worker_id,
                lease_id=binding.lease_id,
            )
        if os.getenv("ZYRA_EDGE_POOL_DISABLED") == "1" or self.edge_execution is None:
            raise WorkerPoolError(
                WorkerPoolErrorCode.EDGE_CONNECTOR_DISABLED,
                "edge-only dispatch cannot use a local fallback when its connector is unavailable",
                operation="execute_integrated_edge_dispatch",
                task_id=binding.task_id,
                worker_id=binding.worker_id,
                lease_id=binding.lease_id,
            )
        if binding.phase is AdmissionPhase.ADMITTED:
            self.start(
                binding.binding_id,
                fence_token=fence_token,
                backend_dispatch_id=f"edge:{binding.attempt_id}",
            )
            binding = self._require_binding(binding.binding_id)
        result = copy.deepcopy(dict(self.edge_execution.execute(binding, payload, fence_token=fence_token)))
        artifact_refs = tuple(str(item) for item in result.get("artifact_refs") or ())
        telemetry = dict(result.get("telemetry") or {})
        gateway_action_ref = str(result.get("gateway_action_ref") or "")
        accepted = bool(result.get("accepted", True))
        if not gateway_action_ref:
            raise WorkerPoolError(
                WorkerPoolErrorCode.EDGE_PROTOCOL_ERROR,
                "edge result did not include a sandbox gateway action receipt",
                operation="execute_integrated_edge_dispatch",
                task_id=binding.task_id,
                worker_id=binding.worker_id,
                lease_id=binding.lease_id,
            )
        if not artifact_refs:
            raise WorkerPoolError(
                WorkerPoolErrorCode.EDGE_PROTOCOL_ERROR,
                "edge result did not include an artifact or result reference",
                operation="execute_integrated_edge_dispatch",
                task_id=binding.task_id,
                worker_id=binding.worker_id,
                lease_id=binding.lease_id,
            )
        self.progress(
            binding.binding_id,
            fence_token=fence_token,
            sequence=binding.progress_sequence + 1,
            payload={"edge_telemetry": telemetry, "gateway_action_ref": gateway_action_ref},
        )
        return EdgeDispatchReceipt(
            binding=self._require_binding(binding.binding_id),
            accepted=accepted,
            gateway_action_ref=gateway_action_ref,
            artifact_refs=artifact_refs,
            telemetry=telemetry,
            result=result,
            error=str(result.get("error") or ""),
        )

    def failover(
        self,
        binding_id: str,
        *,
        successor_request: DispatchAdmissionRequest,
        failure_reason: str,
        graph_node_id: str = "",
    ) -> FailoverReceipt:
        failed = self._require_binding(binding_id)
        if failed.phase is AdmissionPhase.SUPERSEDED:
            successor_attempt_id = str(failed.metadata.get("successor_attempt_id") or "")
            successor_binding = self.repository.binding_for_attempt(successor_attempt_id)
            recovery = next(
                (
                    item
                    for item in self.repository.list_recovery(task_id=failed.task_id)
                    if item.attempt_id == failed.attempt_id
                    and item.successor_attempt_id == successor_attempt_id
                ),
                None,
            )
            if successor_binding is None or recovery is None:
                raise WorkerPoolError(
                    WorkerPoolErrorCode.STORE_CORRUPTION,
                    "superseded dispatch is missing its successor or recovery evidence",
                    operation="failover_integrated_dispatch",
                    task_id=failed.task_id,
                    attempt_id=failed.attempt_id,
                    lease_id=failed.lease_id,
                )
            return FailoverReceipt(
                failed_binding=failed,
                successor=AdmissionResult(
                    binding=successor_binding,
                    candidates=(),
                    reused=True,
                ),
                recovery=recovery,
                graph_ref=successor_binding.foreign_refs.graph.to_dict(),
            )
        lease = self.pool.store.require_lease(failed.lease_id)
        if not lease.terminal:
            self.pool.leases.expire(lease.lease_id, reason=failure_reason)
        current = self._require_binding(binding_id)
        if not current.terminal:
            current = self.repository.update_binding(
                current.advance(AdmissionPhase.LOST),
                expected_version=current.version,
                operation="dispatch_lost",
                payload={"reason": failure_reason},
            )
        excluded = tuple(sorted(set(successor_request.excluded_worker_ids).union({failed.worker_id})))
        latest_attempt = self.pool.store.latest_attempt(failed.task_id)
        if latest_attempt is None:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_NOT_FOUND,
                "lost dispatch no longer has a canonical attempt",
                operation="failover_integrated_dispatch",
                task_id=failed.task_id,
            )
        successor_request = replace(
            successor_request,
            task_id=failed.task_id,
            run_id=failed.run_id,
            owner_session_id=failed.owner_session_id,
            attempt_number=latest_attempt.attempt_number + 1,
            excluded_worker_ids=excluded,
            recovery_reason=failure_reason,
            idempotency_key=f"{successor_request.idempotency_key}:after:{failed.attempt_id}",
            metadata={
                **dict(successor_request.metadata),
                "recovery_parent_attempt_id": failed.attempt_id,
                "failed_worker_id": failed.worker_id,
            },
        )
        successor = self.admit(successor_request, graph_node_id=graph_node_id)
        superseded = self.repository.update_binding(
            current.advance(
                AdmissionPhase.SUPERSEDED,
                metadata={
                    **dict(current.metadata),
                    "successor_attempt_id": successor.binding.attempt_id,
                    "successor_lease_id": successor.binding.lease_id,
                },
            ),
            expected_version=current.version,
            operation="dispatch_superseded",
            payload={
                "successor_attempt_id": successor.binding.attempt_id,
                "successor_lease_id": successor.binding.lease_id,
            },
        )
        evidence = RecoveryEvidence(
            evidence_id="recovery:" + stable_digest(
                {
                    "failed_attempt": failed.attempt_id,
                    "successor_attempt": successor.binding.attempt_id,
                    "reason": failure_reason,
                }
            )[:32],
            task_id=failed.task_id,
            run_id=failed.run_id,
            attempt_id=failed.attempt_id,
            lease_id=failed.lease_id,
            worker_id=failed.worker_id,
            disposition=RecoveryDisposition.REASSIGN,
            reason=failure_reason,
            signal_kind="worker_lost",
            successor_attempt_id=successor.binding.attempt_id,
            successor_lease_id=successor.binding.lease_id,
            graph_ref=successor.binding.foreign_refs.graph.to_dict(),
            route_ref=successor.binding.foreign_refs.backend_route.to_dict(),
            metadata={
                "old_fence_epoch": failed.fence_epoch,
                "canonical_old_lease_state": self.pool.store.require_lease(failed.lease_id).state.value,
                "old_attempt_cannot_commit": True,
                "local_fallback_used": False,
            },
        )
        recovery = self.repository.append_recovery(evidence)
        return FailoverReceipt(
            failed_binding=superseded,
            successor=successor,
            recovery=recovery,
            graph_ref=successor.binding.foreign_refs.graph.to_dict(),
        )

    def reconcile(self, *, run_id: str = "") -> Mapping[str, Any]:
        repaired: list[str] = []
        conflicts: list[str] = []
        bindings = self.repository.list_bindings(run_id=run_id) if run_id else self.repository.list_bindings()
        for binding in bindings:
            lease = self.pool.store.get_lease(binding.lease_id)
            attempt = self.pool.store.get_attempt(binding.attempt_id)
            if lease is None or attempt is None:
                conflicts.append(f"{binding.binding_id}: missing canonical lease or attempt")
                continue
            if binding.terminal:
                continue
            receipts = self.pool.store.receipts_for_task(binding.task_id)
            receipt = next((item for item in receipts if item.attempt_id == binding.attempt_id), None)
            target: AdmissionPhase | None = None
            if receipt is not None:
                target = {
                    ExecutionOutcome.SUCCEEDED: AdmissionPhase.SUCCEEDED,
                    ExecutionOutcome.CANCELLED: AdmissionPhase.CANCELLED,
                    ExecutionOutcome.FENCED: AdmissionPhase.SUPERSEDED,
                }.get(receipt.outcome, AdmissionPhase.FAILED)
            elif lease.state is LeaseState.EXPIRED or attempt.state is AttemptState.LOST:
                target = AdmissionPhase.LOST
            elif lease.state is LeaseState.CANCELLED or attempt.state is AttemptState.CANCELLED:
                target = AdmissionPhase.CANCELLED
            elif lease.state is LeaseState.FENCED or attempt.state is AttemptState.SUPERSEDED:
                target = AdmissionPhase.SUPERSEDED
            elif lease.state is LeaseState.DRAINING and binding.phase is not AdmissionPhase.DRAINING:
                target = AdmissionPhase.DRAINING
            elif attempt.state is AttemptState.RUNNING and binding.phase is AdmissionPhase.ADMITTED:
                target = AdmissionPhase.DISPATCHED
            if target is not None and target is not binding.phase:
                updated = binding.advance(
                    target,
                    metadata={**dict(binding.metadata), "restart_reconciled": True},
                )
                self.repository.update_binding(
                    updated,
                    expected_version=binding.version,
                    operation="dispatch_restart_reconciled",
                    payload={"canonical_lease_state": lease.state.value, "attempt_state": attempt.state.value},
                )
                repaired.append(binding.binding_id)
        return {
            "repaired_binding_ids": repaired,
            "conflicts": conflicts,
            "integrity": self.repository.integrity_report(),
            "invariants": self.invariants.audit(run_id=run_id).to_dict(),
            "process_local_registry_used": False,
        }

    def startup_recover(self, *, run_id: str = "") -> StartupIntegrationRecovery:
        self.reconcile(run_id=run_id)
        return self.checkpoints.startup_recover(run_id=run_id)

    def api_projection(self, *, run_id: str = "", task_id: str = "") -> Mapping[str, Any]:
        bindings = self.repository.list_bindings(run_id=run_id, task_id=task_id)
        controls = self.repository.list_controls(task_id=task_id)
        recovery = self.repository.list_recovery(task_id=task_id)
        graph_ids = sorted(
            {
                item.foreign_refs.graph.object_id
                for item in bindings
                if item.foreign_refs.graph.object_id
            }
        )
        graphs: list[Mapping[str, Any]] = []
        for graph_id in graph_ids:
            try:
                graphs.append(self.topology.version_ref(graph_id).to_dict())
            except KeyError:
                graphs.append({"graph_id": graph_id, "missing": True})
        return {
            "bindings": [item.to_dict() for item in bindings],
            "controls": [item.to_dict() for item in controls],
            "renewals": {
                item.binding_id: [record.to_dict() for record in self.repository.renewals_for_lease(item.lease_id)]
                for item in bindings
            },
            "typed_yields": [
                receipt.to_dict()
                for item in bindings
                if (receipt := self.repository.typed_yield_for_binding(item.binding_id)) is not None
            ],
            "recovery": [item.to_dict() for item in recovery],
            "recovery_handoff": self.recovery_handoff.build(
                run_id=run_id,
                task_id=task_id,
            ).to_dict(),
            "health_bridge": self.health_bridge.projection(),
            "capacity": self.admission.capacity.projection(run_id=run_id),
            "graphs": graphs,
            "integrity": self.repository.integrity_report(),
            "custody": {
                "logical_task": "typescript.AgentTaskRuntime",
                "physical_attempt_lease": "WorkerPoolStore/WorkerLeaseManager",
                "admission_binding": "WorkerPoolIntegrationRepository in canonical pool database",
                "dynamic_graph": "GraphStateCustody",
                "backend_route": "M1-S05D.BackendRegistry",
                "workspace": "M1-S05A.WorkspaceManager",
                "gateway": "M1-S05B.SandboxGatewayRuntime",
                "typescript_projection": "OmpWorkerDispatchRuntime process-local only",
            },
        }

    def _require_binding(self, binding_id: str) -> PhysicalDispatchBinding:
        binding = self.repository.get_binding(binding_id)
        if binding is None:
            raise WorkerPoolError(
                WorkerPoolErrorCode.ATTEMPT_NOT_FOUND,
                "integrated physical dispatch binding was not found",
                operation="require_dispatch_binding",
                metadata={"binding_id": binding_id},
            )
        return binding

    def _emit(self, kind: str, binding: PhysicalDispatchBinding, payload: Mapping[str, Any]) -> str:
        event = {
            "kind": kind,
            "run_id": binding.run_id,
            "task_id": binding.task_id,
            "attempt_id": binding.attempt_id,
            "lease_id": binding.lease_id,
            "worker_id": binding.worker_id,
            "binding_id": binding.binding_id,
            "causation_id": binding.lease_id,
            "correlation_id": binding.owner_session_id,
            "payload": copy.deepcopy(dict(payload)),
            "observed_at": utc_iso(),
        }
        if self.event_sink is not None:
            return str(self.event_sink.append(event))
        return stable_digest(event)


def capability_requirement_from_dict(value: Mapping[str, Any]) -> CapabilityRequirement:
    return CapabilityRequirement(
        required=tuple(str(item) for item in value.get("required") or ()),
        forbidden=tuple(str(item) for item in value.get("forbidden") or ()),
        tool_ids=tuple(str(item) for item in value.get("tool_ids") or ()),
        backend_kinds=tuple(str(item) for item in value.get("backend_kinds") or ()),
        locations=tuple(WorkerLocation(str(item)) for item in value.get("locations") or ()),
        min_protocol_version=int(value.get("min_protocol_version") or 1),
        resources=ResourceVector.from_dict(
            value.get("resources") if isinstance(value.get("resources"), Mapping) else {}
        ),
        labels={str(key): str(item) for key, item in dict(value.get("labels") or {}).items()},
    )


__all__ = [
    "DispatchStartReceipt",
    "EdgeDispatchReceipt",
    "EdgeExecutionPort",
    "EventSinkPort",
    "FailoverReceipt",
    "WorkerPoolIntegrationRuntime",
    "capability_requirement_from_dict",
]
