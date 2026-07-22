from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_orchestration.graph_custody import GraphStateCustody

from .application import WorkerPoolFoundationRuntime
from .errors import WorkerPoolError, WorkerPoolErrorCode
from .integration_models import AdmissionPhase, ControlKind, ControlPhase, PhysicalDispatchBinding
from .integration_store import WorkerPoolIntegrationRepository
from .models import LeaseState, WorkerLifecycleState, stable_digest, utc_iso


class InvariantSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class InvariantViolation:
    code: str
    severity: InvariantSeverity
    message: str
    owner: str
    task_id: str = ""
    attempt_id: str = ""
    lease_id: str = ""
    worker_id: str = ""
    binding_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "owner": self.owner,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "binding_id": self.binding_id,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkerInvariantReport:
    violations: tuple[InvariantViolation, ...]
    checked_bindings: int
    checked_workers: int
    checked_controls: int
    checked_graphs: int
    captured_at: str = field(default_factory=utc_iso)

    @property
    def ok(self) -> bool:
        return not any(item.severity in {InvariantSeverity.ERROR, InvariantSeverity.FATAL} for item in self.violations)

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result = {
            "ok": self.ok,
            "violations": [item.to_dict() for item in self.violations],
            "checked_bindings": self.checked_bindings,
            "checked_workers": self.checked_workers,
            "checked_controls": self.checked_controls,
            "checked_graphs": self.checked_graphs,
            "captured_at": self.captured_at,
        }
        if include_digest:
            result["digest"] = self.digest
        return result


class WorkerPoolInvariantRuntime:
    """Fail-closed runtime invariants for the integrated physical state chain."""

    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        repository: WorkerPoolIntegrationRepository,
        graph_custody: GraphStateCustody,
    ) -> None:
        self.pool = pool
        self.repository = repository
        self.graph_custody = graph_custody

    def audit(self, *, run_id: str = "", task_id: str = "") -> WorkerInvariantReport:
        bindings = self.repository.list_bindings(run_id=run_id, task_id=task_id)
        workers = self.pool.store.list_workers()
        controls = self.repository.list_controls(task_id=task_id)
        violations: list[InvariantViolation] = []
        seen_attempts: dict[str, str] = {}
        seen_leases: dict[str, str] = {}
        graph_ids: set[str] = set()
        for binding in bindings:
            graph_ids.add(binding.foreign_refs.graph.object_id)
            prior = seen_attempts.setdefault(binding.attempt_id, binding.binding_id)
            if prior != binding.binding_id:
                violations.append(self._violation(
                    "attempt_multiple_bindings",
                    InvariantSeverity.FATAL,
                    "one physical attempt is referenced by multiple integration bindings",
                    "WorkerPoolIntegrationRepository",
                    binding,
                    {"first_binding_id": prior},
                ))
            prior = seen_leases.setdefault(binding.lease_id, binding.binding_id)
            if prior != binding.binding_id:
                violations.append(self._violation(
                    "lease_multiple_bindings",
                    InvariantSeverity.FATAL,
                    "one physical lease is referenced by multiple integration bindings",
                    "WorkerPoolIntegrationRepository",
                    binding,
                    {"first_binding_id": prior},
                ))
            violations.extend(self._binding_violations(binding))
        for worker in workers:
            manifest = self.pool.store.latest_manifest(worker.worker_id)
            if manifest is None:
                violations.append(InvariantViolation(
                    code="worker_manifest_missing",
                    severity=InvariantSeverity.FATAL,
                    message="registered worker has no current capability manifest",
                    owner="WorkerPoolStore",
                    worker_id=worker.worker_id,
                ))
                continue
            if manifest.digest != worker.manifest_digest:
                violations.append(InvariantViolation(
                    code="worker_manifest_stale",
                    severity=InvariantSeverity.FATAL,
                    message="worker instance references a non-current manifest digest",
                    owner="WorkerLifecycleRuntime",
                    worker_id=worker.worker_id,
                ))
            active = self.pool.store.list_leases(
                worker_id=worker.worker_id,
                states=(LeaseState.ACTIVE, LeaseState.DRAINING),
            )
            allocated = self.pool.leases.allocated_resources(worker.worker_id)
            if not manifest.resource_capacity.fits(allocated):
                violations.append(InvariantViolation(
                    code="worker_capacity_overcommitted",
                    severity=InvariantSeverity.ERROR,
                    message="canonical active leases exceed the manifest resource capacity",
                    owner="WorkerLeaseManager",
                    worker_id=worker.worker_id,
                    metadata={
                        "capacity": manifest.resource_capacity.to_dict(),
                        "allocated": allocated.to_dict(),
                        "active_lease_ids": [item.lease_id for item in active],
                    },
                ))
            if worker.state is WorkerLifecycleState.DRAINING and worker.accepting_leases:
                violations.append(InvariantViolation(
                    code="draining_worker_accepts_leases",
                    severity=InvariantSeverity.FATAL,
                    message="draining worker still reports that it accepts leases",
                    owner="WorkerLifecycleRuntime",
                    worker_id=worker.worker_id,
                ))
        violations.extend(self._control_violations(controls))
        for graph_id in sorted(item for item in graph_ids if item):
            try:
                snapshot = self.graph_custody.current(graph_id)
            except KeyError:
                violations.append(InvariantViolation(
                    code="dynamic_graph_missing",
                    severity=InvariantSeverity.FATAL,
                    message="integration binding references a missing dynamic graph",
                    owner="GraphStateCustody",
                    metadata={"graph_id": graph_id},
                ))
                continue
            if snapshot.compute_signature() != snapshot.signature:
                violations.append(InvariantViolation(
                    code="dynamic_graph_signature_invalid",
                    severity=InvariantSeverity.FATAL,
                    message="dynamic graph snapshot signature does not match its immutable payload",
                    owner="GraphStateCustody",
                    metadata={"graph_id": graph_id, "revision": snapshot.revision},
                ))
            for binding in bindings:
                if binding.foreign_refs.graph.object_id != graph_id or binding.terminal:
                    continue
                node_id = str(binding.metadata.get("graph_node_id") or "")
                if not node_id:
                    continue
                node = snapshot.node_map.get(node_id)
                if node is None:
                    violations.append(self._violation(
                        "binding_graph_node_missing",
                        InvariantSeverity.FATAL,
                        "active integration binding references a missing dynamic graph node",
                        "GraphStateCustody",
                        binding,
                        {"graph_id": graph_id, "node_id": node_id},
                    ))
                elif (
                    node.physical_attempt_ref != binding.attempt_id
                    or node.worker_lease_ref != binding.lease_id
                ):
                    violations.append(self._violation(
                        "binding_graph_physical_ref",
                        InvariantSeverity.FATAL,
                        "dynamic graph node points at another physical attempt or lease",
                        "GraphStateCustody",
                        binding,
                        {
                            "graph_id": graph_id,
                            "node_id": node_id,
                            "graph_attempt_ref": node.physical_attempt_ref,
                            "graph_lease_ref": node.worker_lease_ref,
                        },
                    ))
        violations.sort(key=lambda item: (item.severity.value, item.code, item.binding_id, item.worker_id))
        return WorkerInvariantReport(
            violations=tuple(violations),
            checked_bindings=len(bindings),
            checked_workers=len(workers),
            checked_controls=len(controls),
            checked_graphs=len(graph_ids),
        )

    def assert_safe(self, *, run_id: str = "", task_id: str = "") -> WorkerInvariantReport:
        report = self.audit(run_id=run_id, task_id=task_id)
        if not report.ok:
            raise WorkerPoolError(
                WorkerPoolErrorCode.STORE_CORRUPTION,
                "integrated worker state violates a fail-closed runtime invariant",
                operation="assert_worker_integration_invariants",
                task_id=task_id,
                metadata=report.to_dict(),
            )
        return report

    def assert_completion(self, binding: PhysicalDispatchBinding) -> None:
        violations = self._binding_violations(binding)
        blocking = [item for item in violations if item.severity in {InvariantSeverity.ERROR, InvariantSeverity.FATAL}]
        if blocking:
            raise WorkerPoolError(
                WorkerPoolErrorCode.EXECUTION_REJECTED,
                "physical dispatch cannot complete while its custody chain is invalid",
                operation="assert_integrated_completion",
                task_id=binding.task_id,
                attempt_id=binding.attempt_id,
                lease_id=binding.lease_id,
                metadata={"violations": [item.to_dict() for item in blocking]},
            )

    def custody_map(self) -> Mapping[str, Any]:
        return {
            "logical_task": {
                "owner": "typescript.AgentTaskRuntime",
                "integration_access": "read_only_identity_revision_digest",
            },
            "physical_attempt": {
                "owner": "WorkerLeaseManager",
                "store": "WorkerPoolStore.task_attempts",
            },
            "worker_lease": {
                "owner": "WorkerLeaseManager",
                "store": "WorkerPoolStore.worker_leases",
            },
            "integration_binding": {
                "owner": "WorkerPoolIntegrationRepository",
                "store": "WorkerPoolStore.worker_dispatch_bindings",
                "duplicates_attempt_or_lease": False,
            },
            "dynamic_graph": {
                "owner": "GraphStateCustody",
                "integration_access": "read_version_and_commit_typed_mutation",
            },
            "workspace": {
                "owner": "M1-S05A.WorkspaceManager",
                "integration_access": "read_only_ref",
            },
            "gateway": {
                "owner": "M1-S05B.SandboxGatewayRuntime",
                "integration_access": "request_and_receipt_ref",
            },
            "backend_route": {
                "owner": "M1-S05D.BackendRegistry",
                "integration_access": "health_and_route_ref",
            },
            "omp_projection": {
                "owner": "typescript.OmpWorkerDispatchRuntime",
                "durable": False,
                "restore_source": "checksummed WorkerPoolStore lease projection",
            },
            "langgraph_default_path": False,
            "langgraph_production_owner": False,
        }

    def _binding_violations(self, binding: PhysicalDispatchBinding) -> list[InvariantViolation]:
        output: list[InvariantViolation] = []
        lease = self.pool.store.get_lease(binding.lease_id)
        attempt = self.pool.store.get_attempt(binding.attempt_id)
        worker = self.pool.store.get_worker(binding.worker_id)
        manifest = self.pool.store.latest_manifest(binding.worker_id) if worker else None
        if lease is None:
            output.append(self._violation(
                "binding_lease_missing",
                InvariantSeverity.FATAL,
                "integration binding references a missing canonical lease",
                "WorkerPoolStore",
                binding,
            ))
        if attempt is None:
            output.append(self._violation(
                "binding_attempt_missing",
                InvariantSeverity.FATAL,
                "integration binding references a missing canonical attempt",
                "WorkerPoolStore",
                binding,
            ))
        if worker is None:
            output.append(self._violation(
                "binding_worker_missing",
                InvariantSeverity.FATAL,
                "integration binding references a missing worker",
                "WorkerLifecycleRuntime",
                binding,
            ))
        if lease is not None:
            if lease.attempt_id != binding.attempt_id or lease.task_id != binding.task_id:
                output.append(self._violation(
                    "binding_lease_identity",
                    InvariantSeverity.FATAL,
                    "binding task/attempt differs from its canonical lease",
                    "WorkerLeaseManager",
                    binding,
                ))
            if lease.worker_id != binding.worker_id or lease.backend_id != binding.backend_id:
                output.append(self._violation(
                    "binding_route_identity",
                    InvariantSeverity.FATAL,
                    "binding worker/backend differs from its canonical lease",
                    "WorkerLeaseManager",
                    binding,
                ))
            if binding.phase in {
                AdmissionPhase.ADMITTED,
                AdmissionPhase.DISPATCHED,
                AdmissionPhase.PARKED,
                AdmissionPhase.DRAINING,
            } and lease.fence_epoch != binding.fence_epoch:
                output.append(self._violation(
                    "binding_fence_epoch",
                    InvariantSeverity.FATAL,
                    "active binding fence epoch differs from the canonical lease",
                    "WorkerLeaseManager",
                    binding,
                ))
            if binding.phase is AdmissionPhase.LOST and lease.state in {
                LeaseState.ACTIVE,
                LeaseState.DRAINING,
            }:
                output.append(self._violation(
                    "lost_binding_live_lease",
                    InvariantSeverity.FATAL,
                    "lost binding still owns a live canonical lease",
                    "WorkerLeaseManager",
                    binding,
                ))
            if binding.terminal and not lease.terminal:
                output.append(self._violation(
                    "terminal_binding_live_lease",
                    InvariantSeverity.FATAL,
                    "terminal integration binding still owns a live canonical lease",
                    "WorkerLeaseManager",
                    binding,
                    {"lease_state": lease.state.value},
                ))
            if not binding.terminal and binding.phase is not AdmissionPhase.LOST and lease.terminal:
                output.append(self._violation(
                    "active_binding_terminal_lease",
                    InvariantSeverity.FATAL,
                    "active integration binding references a terminal canonical lease",
                    "WorkerLeaseManager",
                    binding,
                    {"lease_state": lease.state.value},
                ))
        if attempt is not None and attempt.lease_id != binding.lease_id:
            output.append(self._violation(
                "binding_attempt_lease",
                InvariantSeverity.FATAL,
                "canonical attempt references another lease",
                "WorkerLeaseManager",
                binding,
            ))
        if (
            worker is not None
            and not binding.terminal
            and worker.generation != binding.worker_generation
        ):
            output.append(self._violation(
                "binding_worker_generation",
                InvariantSeverity.FATAL,
                "active binding belongs to a fenced worker generation",
                "WorkerLifecycleRuntime",
                binding,
                {
                    "binding_generation": binding.worker_generation,
                    "current_generation": worker.generation,
                },
            ))
        if (
            manifest is not None
            and worker is not None
            and not binding.terminal
            and worker.generation == binding.worker_generation
            and manifest.digest != binding.manifest_digest
        ):
            output.append(self._violation(
                "binding_manifest_generation",
                InvariantSeverity.ERROR,
                "binding manifest digest differs from the worker current generation",
                "WorkerLifecycleRuntime",
                binding,
            ))
        if binding.edge_only and worker is not None and worker.location.value != "edge":
            output.append(self._violation(
                "edge_binding_local_fallback",
                InvariantSeverity.FATAL,
                "edge-only binding was admitted onto a non-edge worker",
                "WorkerAdmissionRuntime",
                binding,
            ))
        if binding.foreign_refs.logical_task.owner != "typescript.AgentTaskRuntime":
            output.append(self._violation(
                "logical_task_owner_changed",
                InvariantSeverity.FATAL,
                "integration binding claims a logical task owner other than 03D TypeScript",
                "WorkerAdmissionRuntime",
                binding,
            ))
        return output

    def _control_violations(self, controls: Sequence[Any]) -> list[InvariantViolation]:
        output: list[InvariantViolation] = []
        for command in controls:
            if command.phase is ControlPhase.CLAIMED and not command.claim_owner:
                output.append(InvariantViolation(
                    code="control_claim_owner_missing",
                    severity=InvariantSeverity.ERROR,
                    message="claimed durable control command has no claim owner",
                    owner="WorkerControlRuntime",
                    task_id=command.task_id,
                    lease_id=command.lease_id,
                    worker_id=command.worker_id,
                    binding_id=command.binding_id,
                ))
            if command.kind is ControlKind.CANCEL and command.phase is ControlPhase.APPLIED:
                binding = self.repository.get_binding(command.binding_id) if command.binding_id else self.repository.latest_binding(command.task_id)
                if binding is not None and not binding.terminal:
                    output.append(self._violation(
                        "cancel_control_binding_active",
                        InvariantSeverity.ERROR,
                        "applied cancel command left its physical binding active",
                        "WorkerCancellationRuntime",
                        binding,
                        {"command_id": command.command_id},
                    ))
        return output

    @staticmethod
    def _violation(
        code: str,
        severity: InvariantSeverity,
        message: str,
        owner: str,
        binding: PhysicalDispatchBinding,
        metadata: Mapping[str, Any] | None = None,
    ) -> InvariantViolation:
        return InvariantViolation(
            code=code,
            severity=severity,
            message=message,
            owner=owner,
            task_id=binding.task_id,
            attempt_id=binding.attempt_id,
            lease_id=binding.lease_id,
            worker_id=binding.worker_id,
            binding_id=binding.binding_id,
            metadata=dict(metadata or {}),
        )


__all__ = [
    "InvariantSeverity",
    "InvariantViolation",
    "WorkerInvariantReport",
    "WorkerPoolInvariantRuntime",
]
