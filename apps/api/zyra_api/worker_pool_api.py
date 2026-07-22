from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Mapping, Sequence

from zyra_core import TaskState, new_id
from zyra_orchestration.graph_custody import (
    DynamicTopologyRuntime,
    GraphDeltaBuilder,
    GraphEdge,
    GraphNode,
    GraphStateCustody,
    NodeExecutionState,
)
from zyra_scheduler.worker_pool import (
    BackendCapability,
    CapabilityRequirement,
    ExecutionOutcome,
    ResourceVector,
    WorkerLocation,
    WorkerPoolError,
    WorkerPoolFoundationRuntime,
    WorkerPoolIntegrationRuntime,
    ControlKind,
)


# The API audit consumes this declaration to prove that the worker-pool owners
# are reachable from the real HTTP dispatcher.  The values are contracts, not
# documentation-only examples: every entry is handled by ``route_get`` or
# ``route_post`` below and covered by the integration tests.
ZYRA_DYNAMIC_API_ROUTES = (
    "GET /worker-pool",
    "GET /worker-pool/workers",
    "GET /worker-pool/workers/{worker_id}",
    "GET /worker-pool/leases",
    "GET /worker-pool/health",
    "GET /worker-pool/journal",
    "GET /worker-pool/graphs/{graph_id}",
    "POST /worker-pool/workers/local/register",
    "POST /worker-pool/workers/{worker_id}/drain",
    "POST /worker-pool/workers/{worker_id}/wake",
    "POST /worker-pool/workers/{worker_id}/stop",
    "POST /worker-pool/workers/{worker_id}/heartbeat",
    "POST /tasks/{task_id}/worker-pool-lease",
    "POST /tasks/{task_id}/worker-pool-cancel",
    "POST /worker-pool/graphs/{graph_id}/mutate",
    "GET /worker-pool/integration",
    "GET /worker-pool/controls",
    "GET /worker-pool/checkpoints/{run_id}",
    "GET /worker-pool/handoff",
    "GET /worker-pool/recovery-handoff",
    "POST /worker-pool/checkpoints/{run_id}",
    "POST /worker-pool/health/sweep",
    "POST /tasks/{task_id}/worker-pool-control",
)


@dataclass(frozen=True, slots=True)
class WorkerPoolApiResponse:
    status: HTTPStatus
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class WorkerPoolApiService:
    def __init__(
        self,
        pool: WorkerPoolFoundationRuntime,
        graph_custody: GraphStateCustody,
        *,
        backend_health: Any | None = None,
        wake_execution: Any | None = None,
    ) -> None:
        self.pool = pool
        self.graph_custody = graph_custody
        self.topology = DynamicTopologyRuntime(graph_custody)
        self.backend_health = backend_health
        self.integration = WorkerPoolIntegrationRuntime(
            pool,
            graph_custody,
            backend_health=backend_health,
            wake_execution=wake_execution,
        )

    def close(self) -> None:
        close = getattr(self.backend_health, "close", None)
        if callable(close):
            close()

    def route_get(
        self,
        parts: Sequence[str],
        query: Mapping[str, str] | None = None,
    ) -> WorkerPoolApiResponse | None:
        query = dict(query or {})
        if list(parts) == ["worker-pool"]:
            return self._ok(self.pool.api_projection())
        if list(parts) == ["worker-pool", "workers"]:
            projection = self.pool.api_projection()
            return self._ok(
                {
                    "revision": projection["revision"],
                    "workers": projection["workers"],
                    "custody": projection["custody"],
                }
            )
        if list(parts) == ["worker-pool", "leases"]:
            task_id = str(query.get("task_id") or "")
            worker_id = str(query.get("worker_id") or "")
            leases = self.pool.store.list_leases(task_id=task_id, worker_id=worker_id)
            return self._ok({"leases": [item.to_dict() for item in leases], "revision": self.pool.store.revision})
        if list(parts) == ["worker-pool", "health"]:
            workers = self.pool.store.list_workers()
            health = [self.pool.heartbeats.assess(item.worker_id).to_dict() for item in workers]
            return self._ok({"health": health, "integrity": self.pool.store.integrity_report()})
        if list(parts) == ["worker-pool", "journal"]:
            after = max(0, int(query.get("after_sequence") or 0))
            limit = max(1, min(5000, int(query.get("limit") or 1000)))
            records = self.pool.store.journal(after_sequence=after, limit=limit)
            return self._ok({"records": [item.to_dict() for item in records]})
        if list(parts) == ["worker-pool", "integration"]:
            return self._ok(self.integration.api_projection(
                run_id=str(query.get("run_id") or ""),
                task_id=str(query.get("task_id") or ""),
            ))
        if list(parts) == ["worker-pool", "handoff"]:
            handoff = self.integration.projection.handoff(
                after_sequence=max(0, int(query.get("after_sequence") or 0)),
                limit=max(1, min(5000, int(query.get("limit") or 500))),
                run_id=str(query.get("run_id") or ""),
                task_id=str(query.get("task_id") or ""),
            )
            return self._ok(handoff.to_dict())
        if list(parts) == ["worker-pool", "recovery-handoff"]:
            handoff = self.integration.recovery_handoff.build(
                run_id=str(query.get("run_id") or ""),
                task_id=str(query.get("task_id") or ""),
            )
            return self._ok(handoff.to_dict())
        if list(parts) == ["worker-pool", "controls"]:
            controls = self.integration.repository.list_controls(
                task_id=str(query.get("task_id") or ""),
                worker_id=str(query.get("worker_id") or ""),
            )
            return self._ok({"controls": [item.to_dict() for item in controls]})
        if len(parts) == 3 and parts[0] == "worker-pool" and parts[1] == "checkpoints":
            checkpoint = self.integration.repository.latest_checkpoint(parts[2])
            if checkpoint is None:
                return self._error(HTTPStatus.NOT_FOUND, "checkpoint_not_found", "worker checkpoint is not available")
            restore = self.integration.checkpoints.restore(checkpoint.checkpoint_id, strict=False)
            return self._ok({"checkpoint": checkpoint.to_dict(), "restore": restore.to_dict()})
        if len(parts) == 3 and parts[0] == "worker-pool" and parts[1] == "workers":
            worker = self.pool.store.get_worker(parts[2])
            if worker is None:
                return self._error(HTTPStatus.NOT_FOUND, "worker_not_found", "worker is not registered")
            manifest = self.pool.store.latest_manifest(worker.worker_id)
            telemetry = self.pool.store.latest_telemetry(worker.worker_id)
            leases = self.pool.store.list_leases(worker_id=worker.worker_id)
            return self._ok(
                {
                    "worker": worker.to_dict(),
                    "manifest": manifest.to_dict() if manifest else None,
                    "telemetry": telemetry.to_dict() if telemetry else None,
                    "health": self.pool.heartbeats.assess(worker.worker_id).to_dict(),
                    "leases": [item.to_dict() for item in leases],
                }
            )
        if len(parts) == 3 and parts[0] == "worker-pool" and parts[1] == "graphs":
            try:
                snapshot = self.graph_custody.current(parts[2])
            except KeyError:
                return self._error(HTTPStatus.NOT_FOUND, "graph_not_found", "dynamic graph is not registered")
            return self._ok(
                {
                    "snapshot": snapshot.to_dict(),
                    "version_ref": self.topology.version_ref(parts[2]).to_dict(),
                    "journal": list(self.graph_custody.store.journal(parts[2])),
                }
            )
        return None

    def route_post(
        self,
        parts: Sequence[str],
        payload: Mapping[str, Any],
        *,
        task_state: TaskState | None = None,
    ) -> WorkerPoolApiResponse | None:
        try:
            if list(parts) == ["worker-pool", "health", "sweep"]:
                report = self.integration.health_bridge.sweep()
                status = HTTPStatus.OK if not report.failures else HTTPStatus.MULTI_STATUS
                return self._response(status, report.to_dict())
            if list(parts) == ["worker-pool", "workers", "local", "register"]:
                registration = self.ensure_default_local_worker(
                    worker_id=str(payload.get("worker_id") or "local-code-worker"),
                    replace_generation=bool(payload.get("replace_generation", False)),
                )
                return self._response(HTTPStatus.CREATED, registration.to_dict())
            if len(parts) == 4 and parts[0] == "worker-pool" and parts[1] == "workers":
                worker_id = parts[2]
                action = parts[3]
                if action == "drain":
                    command = self.integration.control.submit_and_apply(
                        ControlKind.DRAIN,
                        claim_owner="worker-pool-api",
                        actor_id="worker-pool-api",
                        reason=str(payload.get("reason") or "api drain"),
                        idempotency_key=str(payload.get("idempotency_key") or f"api-drain:{worker_id}"),
                        worker_id=worker_id,
                    )
                    worker = self.pool.store.require_worker(worker_id)
                elif action == "wake":
                    command = self.integration.control.submit_and_apply(
                        ControlKind.WAKE,
                        claim_owner="worker-pool-api",
                        actor_id="worker-pool-api",
                        reason=str(payload.get("reason") or "api wake"),
                        idempotency_key=str(payload.get("idempotency_key") or f"api-wake:{worker_id}:{self.pool.store.revision}"),
                        worker_id=worker_id,
                    )
                    worker = self.pool.store.require_worker(worker_id)
                elif action == "stop":
                    command = self.integration.control.submit_and_apply(
                        ControlKind.STOP,
                        claim_owner="worker-pool-api",
                        actor_id="worker-pool-api",
                        reason=str(payload.get("reason") or "api stop"),
                        idempotency_key=str(payload.get("idempotency_key") or f"api-stop:{worker_id}:{self.pool.store.revision}"),
                        worker_id=worker_id,
                    )
                    worker = self.pool.store.require_worker(worker_id)
                elif action == "heartbeat":
                    command = None
                    latest = self.pool.store.latest_heartbeat(worker_id)
                    sequence = 1 if latest is None else latest.sequence + 1
                    self.pool.heartbeat_local_worker(
                        worker_id,
                        sequence=sequence,
                        queue_depth=int(payload.get("queue_depth") or 0),
                        load_average=float(payload.get("load_average") or 0),
                    )
                    worker = self.pool.store.require_worker(worker_id)
                else:
                    return None
                return self._ok({
                    "worker": worker.to_dict(),
                    "control": command.to_dict() if command is not None else None,
                })
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "worker-pool-lease":
                if task_state is None or task_state.task_id != parts[1]:
                    return self._error(HTTPStatus.NOT_FOUND, "task_not_found", "task is not available")
                acquisition = self.acquire_for_task(task_state, payload=payload)
                return self._response(
                    HTTPStatus.CREATED,
                    acquisition.to_dict(include_fence_token=bool(payload.get("include_fence_token", False))),
                )
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "worker-pool-cancel":
                if task_state is None or task_state.task_id != parts[1]:
                    return self._error(HTTPStatus.NOT_FOUND, "task_not_found", "task is not available")
                command = self.integration.control.submit_and_apply(
                    ControlKind.CANCEL,
                    claim_owner="worker-pool-api",
                    actor_id="worker-pool-api",
                    reason=str(payload.get("reason") or "worker pool cancellation requested"),
                    idempotency_key=str(payload.get("idempotency_key") or f"api-cancel:{task_state.task_id}"),
                    task_id=task_state.task_id,
                    run_id=task_state.run_id,
                )
                return self._ok({
                    "control": command.to_dict(),
                    "cancellation": dict(command.effect.get("cancellation") or {}),
                })
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "worker-pool-control":
                if task_state is None or task_state.task_id != parts[1]:
                    return self._error(HTTPStatus.NOT_FOUND, "task_not_found", "task is not available")
                kind = ControlKind(str(payload.get("kind") or "cancel"))
                command = self.integration.control.submit_and_apply(
                    kind,
                    claim_owner="worker-pool-api",
                    actor_id=str(payload.get("actor_id") or "worker-pool-api"),
                    reason=str(payload.get("reason") or f"api {kind.value}"),
                    idempotency_key=str(payload.get("idempotency_key") or f"api-control:{task_state.task_id}:{kind.value}"),
                    task_id=task_state.task_id,
                    run_id=task_state.run_id,
                    worker_id=str(payload.get("worker_id") or ""),
                    lease_id=str(payload.get("lease_id") or ""),
                    binding_id=str(payload.get("binding_id") or ""),
                )
                return self._ok({"control": command.to_dict()})
            if len(parts) == 3 and parts[0] == "worker-pool" and parts[1] == "checkpoints":
                run_id = parts[2]
                graph_ids = tuple(
                    sorted({
                        item.foreign_refs.graph.object_id
                        for item in self.integration.repository.list_bindings(run_id=run_id)
                        if item.foreign_refs.graph.object_id
                    })
                )
                checkpoint = self.integration.checkpoints.create(
                    run_id=run_id,
                    graph_ids=graph_ids,
                    foreign_checkpoint_refs=tuple(payload.get("foreign_checkpoint_refs") or ()),
                    previous_checkpoint_id=str(payload.get("previous_checkpoint_id") or ""),
                    checkpoint_id=str(payload.get("checkpoint_id") or ""),
                )
                return self._response(HTTPStatus.CREATED, {"checkpoint": checkpoint.to_dict()})
            if len(parts) == 4 and parts[0] == "worker-pool" and parts[1] == "graphs" and parts[3] == "mutate":
                return self._mutate_graph(parts[2], payload)
        except WorkerPoolError as error:
            status = HTTPStatus.CONFLICT
            if error.code.value in {"worker_not_found", "attempt_not_found", "lease_not_found"}:
                status = HTTPStatus.NOT_FOUND
            return self._error(status, error.code.value, str(error), detail=error.to_dict())
        except (KeyError, TypeError, ValueError) as error:
            return self._error(HTTPStatus.BAD_REQUEST, "worker_pool_request_invalid", str(error))
        return None

    def ensure_default_local_worker(
        self,
        *,
        worker_id: str = "local-code-worker",
        replace_generation: bool = False,
    ):
        existing = self.pool.store.get_worker(worker_id)
        if existing is not None and existing.accepting_leases:
            manifest = self.pool.store.latest_manifest(worker_id)
            from zyra_scheduler.worker_pool.application import LocalWorkerRegistration

            self._heartbeat_local(worker_id)
            return LocalWorkerRegistration(
                worker=existing,
                manifest=manifest,
                process_identity=existing.process_identity,
            )
        backend = BackendCapability(
            backend_id="local-sandbox-gateway",
            backend_kind="sandbox_gateway",
            enabled=True,
            healthy=True,
            capabilities=("agent_task", "code_execution", "artifact_return", "local_execution"),
            tool_ids=("code", "shell", "read", "write", "search"),
            constraints={"gateway_owner": "SandboxGatewayRuntime", "sealed_capable": True},
            labels={"dispatch_location": "local"},
        )
        registration = self.pool.register_local_worker(
            worker_id=worker_id,
            worker_kind="code-worker",
            backend=backend,
            capabilities=("agent_task", "code_execution", "artifact_return", "local_execution"),
            tool_ids=("code", "shell", "read", "write", "search"),
            resources=ResourceVector(
                cpu_cores=1.0,
                memory_mb=1024,
                disk_mb=2048,
                network_mbps=100,
                process_slots=4,
            ),
            replace_generation=replace_generation,
            metadata={"default_api_worker": True, "gateway_owner": "SandboxGatewayRuntime"},
        )
        self._heartbeat_local(worker_id)
        return registration

    def _heartbeat_local(self, worker_id: str) -> None:
        """Refresh the API-owned local worker before placement decisions.

        The worker record is durable while process liveness is not.  Emitting a
        fresh monotonic heartbeat on every API acquisition prevents a restored
        process from silently routing through a stale registration.
        """

        latest = self.pool.store.latest_heartbeat(worker_id)
        sequence = 1 if latest is None else latest.sequence + 1
        self.pool.heartbeat_local_worker(worker_id, sequence=sequence)

    def ensure_task_graph(self, state: TaskState) -> str:
        graph_id_value = str(state.metadata.get("dynamic_graph_id") or f"graph:{state.task_id}")
        try:
            self.graph_custody.current(graph_id_value)
            return graph_id_value
        except KeyError:
            pass
        snapshot = self.graph_custody.create(
            graph_id_value=graph_id_value,
            run_id=state.run_id,
            metadata={
                "logical_task_id": state.task_id,
                "logical_task_owner": "typescript.AgentTaskRuntime",
                "source_graph_version": str(state.metadata.get("graph_version") or ""),
            },
        )
        builder = GraphDeltaBuilder(
            snapshot,
            branch_id="api-task-bootstrap",
            actor_id="task-api",
            causation_id=f"task-created:{state.task_id}",
            idempotency_key=f"bootstrap:{state.task_id}",
        )
        node_ids = set(state.plan_nodes)
        for node in state.plan_nodes.values():
            builder.add_node(
                GraphNode(
                    node_id=node.node_id,
                    role=str(node.metadata.get("stage") or "task"),
                    capabilities=tuple(node.constraints.required_tools or node.constraints.allowed_tools or ("agent_task",)),
                    dependencies=tuple(item for item in node.depends_on if item in node_ids),
                    state=NodeExecutionState.SUCCEEDED if str(node.status) == "completed" else NodeExecutionState.PLANNED,
                    logical_task_id=state.task_id,
                    workspace_ref=str(state.metadata.get("workspace_ref") or ""),
                    artifact_refs=tuple(item.artifact_id for item in node.artifact_refs),
                    metadata={"plan_node_projection": True, "stage": str(node.metadata.get("stage") or "")},
                )
            )
        for node in state.plan_nodes.values():
            for dependency in node.depends_on:
                if dependency not in node_ids:
                    continue
                builder.add_edge(
                    GraphEdge(
                        edge_id=f"edge:{dependency}:{node.node_id}",
                        source_node_id=dependency,
                        target_node_id=node.node_id,
                        relation="depends_on",
                    )
                )
        result = self.graph_custody.commit(builder.build())
        if not result.receipt.committed:
            raise ValueError("task dynamic graph bootstrap conflicted")
        state.metadata["dynamic_graph_id"] = graph_id_value
        state.metadata["dynamic_graph_ref"] = self.topology.version_ref(graph_id_value).to_dict()
        return graph_id_value

    def acquire_for_task(
        self,
        state: TaskState,
        *,
        payload: Mapping[str, Any] | None = None,
    ):
        options = dict(payload or {})
        self.ensure_default_local_worker()
        graph_id_value = self.ensure_task_graph(state)
        locations = tuple(
            WorkerLocation(str(item))
            for item in options.get("locations") or (WorkerLocation.LOCAL.value,)
        )
        requirement = CapabilityRequirement(
            required=tuple(str(item) for item in options.get("required_capabilities") or ("agent_task",)),
            tool_ids=tuple(str(item) for item in options.get("tool_ids") or ()),
            backend_kinds=tuple(str(item) for item in options.get("backend_kinds") or ()),
            locations=locations,
            resources=ResourceVector.from_dict(
                options.get("resources")
                if isinstance(options.get("resources"), Mapping)
                else {"process_slots": 1, "memory_mb": 64}
            ),
        )
        latest_attempt = self.pool.store.latest_attempt(state.task_id)
        replay_attempt = latest_attempt is not None and not latest_attempt.terminal
        attempt_number = (
            latest_attempt.attempt_number
            if replay_attempt
            else (1 if latest_attempt is None else latest_attempt.attempt_number + 1)
        )
        acquisition = self.pool.acquire_task(
            task_id=state.task_id,
            run_id=state.run_id,
            owner_session_id=str(state.metadata.get("query_session_id") or f"task:{state.task_id}"),
            requirement=requirement,
            preferred_worker_ids=tuple(str(item) for item in options.get("preferred_worker_ids") or ()),
            excluded_worker_ids=tuple(str(item) for item in options.get("excluded_worker_ids") or ()),
            attempt_number=attempt_number,
            ttl_seconds=float(options.get("ttl_seconds") or 3600.0),
            idempotency_key=str(
                options.get("idempotency_key") or f"task-lease:{state.task_id}:{attempt_number}"
            ),
            metadata={"api_task_flow": True, "dynamic_graph_id": graph_id_value},
        )
        execute_node_id = next(
            (
                node.node_id
                for node in state.plan_nodes.values()
                if str(node.metadata.get("stage") or "") == "execute"
            ),
            state.root_node_id,
        )
        bound = self.topology.bind_physical_attempt(
            graph_id_value,
            execute_node_id,
            physical_attempt_ref=acquisition.attempt.attempt_id,
            worker_lease_ref=acquisition.lease.lease_id,
            backend_route_ref=acquisition.lease.backend_id,
            actor_id="worker-pool-api",
            causation_id=acquisition.lease.lease_id,
        )
        state.metadata["worker_pool"] = {
            "attempt_id": acquisition.attempt.attempt_id,
            "attempt_number": acquisition.attempt.attempt_number,
            "lease_id": acquisition.lease.lease_id,
            "worker_id": acquisition.worker.worker_id,
            "backend_id": acquisition.lease.backend_id,
            "graph_ref": self.topology.version_ref(graph_id_value).to_dict(),
            "graph_commit": bound.receipt.to_dict(),
        }
        return acquisition

    def ensure_task_lease(
        self,
        state: TaskState,
        *,
        payload: Mapping[str, Any] | None = None,
    ):
        """Keep a pending task attached to one valid physical attempt.

        A task may be created with ``auto_run=false`` and resumed after a lease
        deadline or API restart.  The expired lease is first made terminal and
        fenced; only then is a successor physical attempt allocated.  The
        logical 03D task identifier remains unchanged.
        """

        projection = state.metadata.get("worker_pool")
        lease = None
        if isinstance(projection, Mapping):
            lease = self.pool.store.get_lease(str(projection.get("lease_id") or ""))
        if lease is not None and not lease.terminal and lease.expired_at():
            self.pool.leases.expire(lease.lease_id, reason="task resumed after lease deadline")
            lease = self.pool.store.require_lease(lease.lease_id)
        if lease is not None and not lease.terminal:
            self.ensure_default_local_worker()
            return None
        return self.acquire_for_task(state, payload=payload)

    def finalize_task(self, state: TaskState, *, success: bool, summary: str) -> Mapping[str, Any] | None:
        projection = state.metadata.get("worker_pool")
        if not isinstance(projection, Mapping):
            return None
        lease = self.pool.store.get_lease(str(projection.get("lease_id") or ""))
        if lease is None or lease.terminal:
            return None
        self.pool.leases.start_attempt(
            lease.lease_id,
            worker_id=lease.worker_id,
            fence_token=lease.fence_token,
            fence_epoch=lease.fence_epoch,
            backend_dispatch_id=f"task-graph:{state.task_id}",
        )
        receipt = self.pool.leases.complete(
            lease.lease_id,
            worker_id=lease.worker_id,
            fence_token=lease.fence_token,
            fence_epoch=lease.fence_epoch,
            outcome=ExecutionOutcome.SUCCEEDED if success else ExecutionOutcome.FAILED,
            summary=summary,
            artifact_refs=tuple(item.artifact_id for item in state.artifacts),
            event_refs=(),
            gateway_receipt_ref=f"task-graph:{state.task_id}",
            metadata={"default_task_graph": True},
        )
        state.metadata["worker_pool_receipt"] = receipt.to_dict()
        return receipt.to_dict()

    def _mutate_graph(self, graph_id_value: str, payload: Mapping[str, Any]) -> WorkerPoolApiResponse:
        operation = str(payload.get("operation") or "")
        actor_id = str(payload.get("actor_id") or "graph-api")
        causation_id = str(payload.get("causation_id") or new_id("graph-cause"))
        if operation == "add_node":
            node = GraphNode.from_dict(dict(payload.get("node") or {}))
            result = self.topology.add_node(graph_id_value, node, actor_id=actor_id, causation_id=causation_id)
        elif operation == "remove_node":
            result = self.topology.remove_node(
                graph_id_value,
                str(payload.get("node_id") or ""),
                actor_id=actor_id,
                causation_id=causation_id,
            )
        elif operation == "replace_node":
            node = GraphNode.from_dict(dict(payload.get("node") or {}))
            result = self.topology.replace_node(graph_id_value, node, actor_id=actor_id, causation_id=causation_id)
        elif operation == "add_edge":
            edge = GraphEdge.from_dict(dict(payload.get("edge") or {}))
            result = self.topology.add_edge(graph_id_value, edge, actor_id=actor_id, causation_id=causation_id)
        elif operation == "remove_edge":
            result = self.topology.remove_edge(
                graph_id_value,
                str(payload.get("edge_id") or ""),
                actor_id=actor_id,
                causation_id=causation_id,
            )
        elif operation == "set_role":
            result = self.topology.set_role(
                graph_id_value,
                str(payload.get("node_id") or ""),
                str(payload.get("role") or ""),
                actor_id=actor_id,
                causation_id=causation_id,
            )
        elif operation == "set_capabilities":
            result = self.topology.set_capabilities(
                graph_id_value,
                str(payload.get("node_id") or ""),
                tuple(str(item) for item in payload.get("capabilities") or ()),
                actor_id=actor_id,
                causation_id=causation_id,
            )
        else:
            return self._error(HTTPStatus.BAD_REQUEST, "graph_operation_invalid", "unsupported graph mutation")
        status = HTTPStatus.OK if result.receipt.committed else HTTPStatus.CONFLICT
        return self._response(status, result.to_dict())

    @staticmethod
    def _headers() -> Mapping[str, str]:
        return {
            "Cache-Control": "no-store, max-age=0",
            "X-Zyra-Worker-State-Owner": "WorkerPoolStore",
            "X-Zyra-Graph-State-Owner": "GraphStateCustody",
            "X-Zyra-Logical-Task-Owner": "typescript.AgentTaskRuntime",
        }

    def _ok(self, body: Mapping[str, Any]) -> WorkerPoolApiResponse:
        return self._response(HTTPStatus.OK, body)

    def _response(self, status: HTTPStatus, body: Mapping[str, Any]) -> WorkerPoolApiResponse:
        return WorkerPoolApiResponse(status=status, body=dict(body), headers=self._headers())

    def _error(
        self,
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> WorkerPoolApiResponse:
        return self._response(status, {"error": code, "message": message, "detail": dict(detail or {})})
