from __future__ import annotations

import socket
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from zyra_core import ArtifactKind, PlanNodeStatus, now_iso
from zyra_orchestration.deployment import (
    DeploymentProcessManager,
    DeploymentProfile,
    DeploymentStateStore,
    ProfileCatalog,
)
from zyra_orchestration.topology_policy.contracts import FrozenDict, StableArtifactRef
from zyra_scheduler import (
    AdaptiveDepthRuntime,
    DeterministicEarlyExitGate,
    OperatorCallResult,
    OperatorCatalog,
    OperatorPlacementLeaseConfig,
    OperatorPlacementLeaseRuntime,
    ResourceLocation,
    ResourceScheduler,
    WorkerBackendKind,
    WorkerManifest,
    WorkerPool,
)
from zyra_scheduler.dispatch_evidence import (
    PhysicalDispatchCallPort,
    PhysicalDispatchEvidenceStore,
    PhysicalDispatchTask,
)
from zyra_scheduler.operator_policy.early_exit import (
    CanonicalExitSnapshotBuilder,
    EarlyExitGateConfig,
    build_final_verifier_decision,
)
from zyra_scheduler.worker_pool import (
    BackendCapability,
    ResourceVector,
    WorkerLocation,
    WorkerPoolFoundationRuntime,
)
from zyra_scheduler.worker_pool.models import AttemptState, LeaseState

from tests.integration.operator_placement_harness import (
    _iso_after,
    catalog,
    EligibilityPort,
    checkpoint,
    continuity_receipt,
    policy_input,
    proposal,
    selector_result,
    task_state,
)
from zyra_runtime import (
    LocalArtifactStore,
    PermissionRequestQueue,
    PermissionStateStore,
)
from zyra_scheduler.recovery_runtime import RecoveryPlanStore


ROOT = Path(__file__).resolve().parents[2]
SECRET = b"p2-physical-dispatch-worker-pool-secret"
_WORKER_BY_LOCATION = {
    "local": "physical-local-worker",
    "edge": "physical-edge-worker",
    "cloud": "physical-cloud-worker",
}
_RESOURCE_LOCATION = {
    "local": ResourceLocation.LOCAL,
    "edge": ResourceLocation.EDGE,
    "cloud": ResourceLocation.CLOUD,
}
_WORKER_LOCATION = {
    "local": WorkerLocation.LOCAL,
    "edge": WorkerLocation.EDGE,
    "cloud": WorkerLocation.CLOUD,
}
_BACKEND_KIND = {
    "local": WorkerBackendKind.LOCAL_PROCESS,
    "edge": WorkerBackendKind.ISOLATED_PROCESS,
    "cloud": WorkerBackendKind.CLOUD_MODEL,
}
_DEPLOYMENT_PROFILE = {
    "local": DeploymentProfile.DEVICE,
    "edge": DeploymentProfile.EDGE,
    "cloud": DeploymentProfile.CLOUD,
}


def free_port_block(count: int = 3) -> int:
    for base in range(49000, 61000, count):
        sockets: list[socket.socket] = []
        try:
            for port in range(base, base + count):
                item = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                item.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                item.bind(("127.0.0.1", port))
                sockets.append(item)
            return base
        except OSError:
            pass
        finally:
            for item in sockets:
                item.close()
    raise RuntimeError("could not reserve a deployment port block")


class CompletingPhysicalCallPort:
    """Test-only canonical task/artifact projection around the real call port."""

    def __init__(
        self,
        *,
        delegate: PhysicalDispatchCallPort,
        task: Any,
        policy: Any,
        artifacts: LocalArtifactStore,
        pool: WorkerPoolFoundationRuntime,
    ) -> None:
        self.delegate = delegate
        self.task = task
        self.policy = policy
        self.artifacts = artifacts
        self.pool = pool

    def prepare(self, context: Any) -> None:
        self.delegate.prepare(context)

    def execute(self, context: Any) -> OperatorCallResult:
        lease = self.pool.store.require_lease(context.lease_id)
        attempt = self.pool.store.require_attempt(context.attempt_id)
        assert lease.state is LeaseState.ACTIVE
        assert attempt.state is AttemptState.RUNNING
        result = self.delegate.execute(context)
        artifact = self.artifacts.write_text(
            run_id=self.task.run_id,
            task_id=self.task.task_id,
            content=(
                f"physical dispatch {context.placement_location} "
                f"attempt={context.attempt_id} receipt="
                f"{result.metadata['physical_dispatch_receipt_digest']}"
            ),
            title="deliverable",
            kind=ArtifactKind.TEXT,
            extension=".txt",
            producer_node_id=self.task.root_node_id,
        )
        if all(
            item.artifact_id != artifact.artifact_id
            for item in self.task.artifacts
        ):
            self.task.artifacts.append(artifact)
        self.task.status = PlanNodeStatus.COMPLETED
        for node in self.task.plan_nodes.values():
            node.status = PlanNodeStatus.COMPLETED
        observed = self.artifacts.verify(artifact)
        projection = StableArtifactRef(
            ref_id=artifact.artifact_id,
            uri=artifact.uri,
            digest=observed.sha256,
            media_type=str(
                artifact.metadata.get("content_type")
                or "application/octet-stream"
            ),
        )
        verifier_ref = "verifier://physical-dispatch/final"
        self.task.decisions.append(
            build_final_verifier_decision(
                task=self.task,
                requirement_revision=self.policy.requirement_revision,
                expected_obligation_ids=self.policy.unresolved_obligations,
                verified_artifact_refs=(projection,),
                passed=True,
                verifier_version="physical-dispatch-verifier-v1",
                verifier_receipt_ref=verifier_ref,
                verified_at=now_iso(),
                fresh_until=_iso_after(300),
            )
        )
        return replace(
            result,
            artifact_refs=(*result.artifact_refs, projection),
            verification_refs=(*result.verification_refs, verifier_ref),
        )


@dataclass(slots=True)
class PhysicalHarness:
    location: str
    task: Any
    policy: Any
    selected_catalog: OperatorCatalog
    selector: Any
    pool: WorkerPoolFoundationRuntime
    deployment_store: DeploymentStateStore
    process_manager: DeploymentProcessManager
    physical_port: PhysicalDispatchCallPort
    call_port: CompletingPhysicalCallPort
    runtime: OperatorPlacementLeaseRuntime
    adaptive: AdaptiveDepthRuntime
    eligibility: EligibilityPort
    clients: dict[str, Any]
    processes: dict[str, Any]

    def execute(self):
        return self.runtime.execute_task(
            task=self.task,
            policy_input=self.policy,
            selector_result=self.selector,
            catalog=self.selected_catalog,
            adaptive_depth=self.adaptive,
            eligibility_port=self.eligibility,
            early_exit_enabled=False,
        )

    def close(self) -> None:
        self.physical_port.close()


def build_physical_harness(
    tmp_path: Path,
    *,
    location: str,
    environment: Mapping[str, str] | None = None,
    privacy_class: str = "internal",
    allowed_placements: tuple[str, ...] = ("local", "edge", "cloud"),
    fallback_locations: tuple[str, ...] = (),
    prefer_selected_location: bool = True,
    logical_manifest_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    condition: str = "normal",
    condition_signals: Mapping[str, Any] | None = None,
) -> PhysicalHarness:
    worker_id = _WORKER_BY_LOCATION[location]
    active_locations = tuple(dict.fromkeys((location, *fallback_locations)))
    if any(item not in _WORKER_BY_LOCATION for item in active_locations):
        raise ValueError("physical harness location is invalid")
    task = task_state()
    task.constraints.allowed_workers = [
        _WORKER_BY_LOCATION[item] for item in active_locations
    ]
    if prefer_selected_location:
        task.metadata["runtime_hints"] = {"preferred_worker": worker_id}
    policy = replace(
        policy_input(task),
        privacy_class=privacy_class,
        allowed_placements=allowed_placements,
    )
    source_catalog = catalog(single_layer=True)
    profile = source_catalog.get("tool:produce-tool")
    assert profile is not None
    profile = replace(
        profile,
        allowed_locations=("local", "edge", "cloud"),
        allowed_privacy_classes=(
            "internal",
            "project",
            "sensitive",
            "public",
            "confidential",
            "restricted",
            "local-only",
        ),
    )
    selected_catalog = OperatorCatalog(
        entries=(profile,),
        source_versions=FrozenDict({"physical_registry": "1"}),
        generation=1,
        built_at=now_iso(),
    )
    selected_proposal = proposal(
        policy,
        selected_catalog,
        single_layer=True,
    )
    selected = selector_result(selected_proposal)
    selected.scheduler_input = selected_proposal.scheduler_input().bind_task(
        run_id=task.run_id,
        task_id=task.task_id,
    )
    logical_manifests = []
    overrides = dict(logical_manifest_overrides or {})
    for selected_location, selected_worker in _WORKER_BY_LOCATION.items():
        manifest = WorkerManifest(
            worker_id=selected_worker,
            display_name=selected_worker,
            runtime_worker=selected_worker,
            location=_RESOURCE_LOCATION[selected_location],
            backend=_BACKEND_KIND[selected_location],
            capabilities=[
                "artifact-production",
                "verification",
                "worker.dispatch",
                "tool.invoke",
                "deterministic-transform",
                *(
                    ["provider-dispatch"]
                    if selected_location == "cloud"
                    else []
                ),
            ],
            tools=["produce-tool"],
            models=(
                ["deepseek-v4-pro"]
                if selected_location == "cloud"
                else ["local-deterministic"]
            ),
            privacy_level=(
                "public_only" if selected_location == "cloud" else "sensitive_ok"
            ),
            max_concurrency=2,
            latency_ms=(
                1
                if selected_location == location
                else {"local": 20, "edge": 30, "cloud": 100}[
                    selected_location
                ]
            ),
            cost_per_1k_tokens=(
                0.00087 if selected_location == "cloud" else 0
            ),
        )
        values = dict(overrides.get(selected_location) or {})
        if values:
            manifest = replace(manifest, **values)
        logical_manifests.append(manifest)
    scheduler = ResourceScheduler(WorkerPool(logical_manifests))
    deployment_root = tmp_path / "deployment"
    environment_map = dict(environment or {})
    profile_catalog = ProfileCatalog.defaults(
        ROOT,
        host="127.0.0.1",
        base_port=free_port_block(),
        environment=environment_map,
    )
    deployment_store = DeploymentStateStore(
        deployment_root / "deployment.sqlite3"
    )
    process_manager = DeploymentProcessManager(
        project_root=ROOT,
        state_root=deployment_root,
        store=deployment_store,
        environment=environment_map,
    )
    clients: dict[str, Any] = {}
    processes: dict[str, Any] = {}
    pool = WorkerPoolFoundationRuntime(
        tmp_path / "worker-pool.sqlite3",
        attestation_secret=SECRET,
        default_lease_ttl_seconds=30,
    )
    for selected_location in active_locations:
        selected_worker = _WORKER_BY_LOCATION[selected_location]
        deployment_profile = _DEPLOYMENT_PROFILE[selected_location]
        process, client, health = process_manager.start_node(
            profile_catalog.policy(deployment_profile)
        )
        clients[selected_location] = client
        processes[selected_location] = process
        runtime_identity = dict(health.get("runtime_identity") or {})
        pool.register_physical_worker(
            worker_id=selected_worker,
            worker_kind="physical-dispatch-worker",
            location=_WORKER_LOCATION[selected_location],
            backend=BackendCapability(
                backend_id=f"physical-{selected_location}",
                backend_kind=_BACKEND_KIND[selected_location].value,
                enabled=True,
                healthy=True,
                capabilities=(
                    "artifact-production",
                    "verification",
                    "deterministic-transform",
                    *(
                        ("provider-dispatch",)
                        if selected_location == "cloud"
                        else ()
                    ),
                ),
                tool_ids=("produce-tool",),
            ),
            capabilities=(
                "artifact-production",
                "verification",
                "deterministic-transform",
                *(
                    ("provider-dispatch",)
                    if selected_location == "cloud"
                    else ()
                ),
            ),
            tool_ids=("produce-tool",),
            resources=ResourceVector(
                cpu_cores=2,
                memory_mb=1024,
                process_slots=2,
            ),
            process_identity=str(
                runtime_identity.get("failure_boundary_id")
                or f"pid-{process.pid}"
            ),
            endpoint=process.endpoint,
            metadata={
                "pid": process.pid,
                "generation_id": process.generation_id,
                "node_id": health.get("node_id"),
                "failure_boundary_id": runtime_identity.get(
                    "failure_boundary_id"
                ),
            },
        )
        pool.heartbeat_local_worker(
            selected_worker,
            sequence=1,
            process_uptime_ms=1,
        )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    permission_queue = PermissionRequestQueue(
        PermissionStateStore(tmp_path / "permission.json"),
        "session-physical-dispatch",
    )
    recovery_store = RecoveryPlanStore(tmp_path / "recovery.sqlite3")
    selected_checkpoint = checkpoint(task, recovery_store)
    continuity = continuity_receipt(policy)
    gate_config = EarlyExitGateConfig.load(
        ROOT / "config" / "phase2" / "maas-early-exit.json"
    )
    eligibility_builder = CanonicalExitSnapshotBuilder(
        config=gate_config,
        artifact_store=artifacts,
        permission_queue=permission_queue,
        side_effect_store=recovery_store,
    )
    physical_port = PhysicalDispatchCallPort(
        task=PhysicalDispatchTask(
            run_id=task.run_id,
            task_id=task.task_id,
            payload=FrozenDict(
                {
                    "kind": "same-minimal-dispatch-task",
                    "input": ["alpha", "beta", "gamma"],
                }
            ),
            privacy_class=privacy_class,
            allowed_placements=allowed_placements,
            permission_ref="permission://physical-dispatch/allowed",
            condition=condition,
            condition_signals=FrozenDict(condition_signals or {}),
        ),
        catalog=profile_catalog,
        process_manager=process_manager,
        state_store=deployment_store,
        evidence_store=PhysicalDispatchEvidenceStore(
            tmp_path / "physical-evidence"
        ),
    )
    call_port = CompletingPhysicalCallPort(
        delegate=physical_port,
        task=task,
        policy=policy,
        artifacts=artifacts,
        pool=pool,
    )
    runtime = OperatorPlacementLeaseRuntime(
        config=OperatorPlacementLeaseConfig.load(
            ROOT / "config" / "phase2" / "operator-placement-lease.json"
        ),
        scheduler=scheduler,
        pool=pool,
        call_port=call_port,
        permission_queue=permission_queue,
        catalog_provider=lambda: selected_catalog,
    )
    return PhysicalHarness(
        location=location,
        task=task,
        policy=policy,
        selected_catalog=selected_catalog,
        selector=selected,
        pool=pool,
        deployment_store=deployment_store,
        process_manager=process_manager,
        physical_port=physical_port,
        call_port=call_port,
        runtime=runtime,
        adaptive=AdaptiveDepthRuntime(
            DeterministicEarlyExitGate(gate_config)
        ),
        eligibility=EligibilityPort(
            builder=eligibility_builder,
            task=task,
            policy=policy,
            proposal=selected_proposal,
            checkpoint=selected_checkpoint,
            continuity=continuity,
        ),
        clients=clients,
        processes=processes,
    )


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


__all__ = [
    "PhysicalHarness",
    "build_physical_harness",
    "free_port_block",
    "read_env_file",
]
