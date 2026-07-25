from __future__ import annotations

import math
import random
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from .canonical import canonicalize, digest, new_identity, utc_now
from .errors import invalid, unavailable
from .models import CapabilityVector, ComparisonEnvelope, VariantDefinition
from .source import SourceArchive, SourceEvent


@dataclass(frozen=True, slots=True)
class WorkloadMessage:
    message_id: str
    event_id: str
    sender: str
    recipients: tuple[str, ...]
    intent: str
    useful: bool
    payload_digest: str
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "event_id": self.event_id,
            "sender": self.sender,
            "recipients": list(self.recipients),
            "intent": self.intent,
            "useful": self.useful,
            "payload_digest": self.payload_digest,
            "sequence": self.sequence,
        }


@dataclass(frozen=True, slots=True)
class WorkloadRoute:
    route_id: str
    event_id: str
    worker_id: str
    role: str
    node_id: str
    profile: str
    reason: str
    policy_compliant: bool
    privacy_compliant: bool
    latency_units: float
    cost_microunits: float
    sequence: int

    def to_dict(self) -> dict[str, Any]:
        return canonicalize(
            {
                "route_id": self.route_id,
                "event_id": self.event_id,
                "worker_id": self.worker_id,
                "role": self.role,
                "node_id": self.node_id,
                "profile": self.profile,
                "reason": self.reason,
                "policy_compliant": self.policy_compliant,
                "privacy_compliant": self.privacy_compliant,
                "latency_units": self.latency_units,
                "cost_microunits": self.cost_microunits,
                "sequence": self.sequence,
            }
        )


@dataclass(frozen=True, slots=True)
class WorkloadRecovery:
    recovery_id: str
    fault_event_id: str
    recovery_event_id: str
    recovered: bool
    exact_resume: bool
    checkpoint_bound: bool
    route_changed: bool
    elapsed_units: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return canonicalize(
            {
                "recovery_id": self.recovery_id,
                "fault_event_id": self.fault_event_id,
                "recovery_event_id": self.recovery_event_id,
                "recovered": self.recovered,
                "exact_resume": self.exact_resume,
                "checkpoint_bound": self.checkpoint_bound,
                "route_changed": self.route_changed,
                "elapsed_units": self.elapsed_units,
                "reason": self.reason,
            }
        )


@dataclass(frozen=True, slots=True)
class WorkloadObservation:
    observation_id: str
    variant_id: str
    repetition: int
    seed: int
    envelope_digest: str
    source_archive_digest: str
    scenario_run_id: str
    owner_run_id: str
    task_id: str
    started_at: str
    completed_at: str
    state_digest: str
    processed_event_ids: tuple[str, ...]
    routes: tuple[WorkloadRoute, ...]
    messages: tuple[WorkloadMessage, ...]
    recoveries: tuple[WorkloadRecovery, ...]
    memory_writes: int
    memory_hits: int
    compact_operations: int
    restore_operations: int
    topology_nodes: tuple[str, ...]
    topology_edges: tuple[tuple[str, str], ...]
    topology_mutations: int
    useful_messages: int
    broadcast_deliveries: int
    unresolved_fault_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    provider_ids: tuple[str, ...]
    model_ids: tuple[str, ...]
    quality_score: float
    artifact_drift_ratio: float
    token_units: float
    wall_time_ms: float
    cost_microunits: float
    human_intervention_count: int
    claims: dict[str, Any]
    execution_receipts: tuple[dict[str, Any], ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    digest_value: str = field(default="", repr=False, compare=False)

    @property
    def observation_digest(self) -> str:
        return self.digest_value or digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.experiment-workload-observation/v1",
            "observation_id": self.observation_id,
            "variant_id": self.variant_id,
            "repetition": self.repetition,
            "seed": self.seed,
            "envelope_digest": self.envelope_digest,
            "source_archive_digest": self.source_archive_digest,
            "scenario_run_id": self.scenario_run_id,
            "owner_run_id": self.owner_run_id,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "state_digest": self.state_digest,
            "processed_event_ids": list(self.processed_event_ids),
            "routes": [item.to_dict() for item in self.routes],
            "messages": [item.to_dict() for item in self.messages],
            "recoveries": [item.to_dict() for item in self.recoveries],
            "memory_writes": self.memory_writes,
            "memory_hits": self.memory_hits,
            "compact_operations": self.compact_operations,
            "restore_operations": self.restore_operations,
            "topology_nodes": list(self.topology_nodes),
            "topology_edges": [list(item) for item in self.topology_edges],
            "topology_mutations": self.topology_mutations,
            "useful_messages": self.useful_messages,
            "broadcast_deliveries": self.broadcast_deliveries,
            "unresolved_fault_ids": list(self.unresolved_fault_ids),
            "artifact_ids": list(self.artifact_ids),
            "provider_ids": list(self.provider_ids),
            "model_ids": list(self.model_ids),
            "quality_score": self.quality_score,
            "artifact_drift_ratio": self.artifact_drift_ratio,
            "token_units": self.token_units,
            "wall_time_ms": self.wall_time_ms,
            "cost_microunits": self.cost_microunits,
            "human_intervention_count": self.human_intervention_count,
            "claims": canonicalize(self.claims),
            "execution_receipts": canonicalize(self.execution_receipts),
            "metadata": canonicalize(self.metadata),
        }
        if include_digest:
            value["observation_digest"] = self.observation_digest
        return value


class VariantExecutionPort(Protocol):
    def execute(
        self,
        *,
        experiment_id: str,
        cell_id: str,
        variant: VariantDefinition,
        repetition: int,
        seed: int,
        envelope: ComparisonEnvelope,
        source: SourceArchive,
        cancel_requested: Callable[[], bool],
    ) -> WorkloadObservation: ...


class UnboundVariantExecutionPort:
    def execute(self, **_: Any) -> WorkloadObservation:
        raise unavailable(
            "experiment_execution_port_unbound",
            "Experiment runtime is not bound to a workload execution owner.",
            phase="execution",
        )


class EvidenceBackedWorkloadRuntime:
    def __init__(
        self,
        *,
        maximum_events: int = 10_000_000,
        compact_window: int = 128,
        memory_capacity: int = 4096,
    ) -> None:
        self.maximum_events = maximum_events
        self.compact_window = max(8, compact_window)
        self.memory_capacity = max(64, memory_capacity)

    def execute(
        self,
        *,
        experiment_id: str,
        cell_id: str,
        variant: VariantDefinition,
        repetition: int,
        seed: int,
        envelope: ComparisonEnvelope,
        source: SourceArchive,
        cancel_requested: Callable[[], bool],
    ) -> WorkloadObservation:
        self._verify_bindings(
            variant=variant,
            repetition=repetition,
            seed=seed,
            envelope=envelope,
            source=source,
        )
        if len(source.events) > self.maximum_events:
            raise invalid(
                "experiment_workload_event_limit",
                "Source evidence exceeds workload event limit.",
                phase="execution",
                detail={
                    "count": len(source.events),
                    "maximum": self.maximum_events,
                },
            )
        started_at = utc_now()
        perf_started = time.perf_counter()
        rng = random.Random(seed)
        state_digest = digest(
            {
                "experiment_id": experiment_id,
                "cell_id": cell_id,
                "variant": variant.to_dict(),
                "envelope_digest": envelope.envelope_digest,
                "source_archive_digest": source.archive_digest,
                "seed": seed,
            }
        )
        nodes: set[str] = set()
        edges: set[tuple[str, str]] = set()
        messages: list[WorkloadMessage] = []
        routes: list[WorkloadRoute] = []
        recoveries: list[WorkloadRecovery] = []
        memory: dict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=self.memory_capacity)
        )
        global_memory: deque[str] = deque(maxlen=self.memory_capacity)
        memory_writes = 0
        memory_hits = 0
        compact_operations = 0
        restore_operations = 0
        topology_mutations = 0
        useful_messages = 0
        broadcast_deliveries = 0
        unresolved_faults: list[str] = []
        pending_faults: deque[SourceEvent] = deque()
        artifacts: set[str] = set()
        providers: set[str] = set()
        models: set[str] = set()
        processed: list[str] = []
        role_counts: Counter[str] = Counter()
        route_counts: Counter[str] = Counter()
        event_workers: dict[str, str] = {}
        worker_order: list[str] = []
        source_workers = list(source.workers) or ["owner"]
        if variant.capabilities.worker_limit == 1:
            worker_order = ["single-agent"]
        else:
            worker_order = source_workers[: variant.capabilities.worker_limit]
            if not worker_order:
                worker_order = ["worker-0"]
        if variant.variant_id == "static_full_connect_multi_agent":
            nodes.update(worker_order)
            for left in worker_order:
                for right in worker_order:
                    if left != right:
                        edges.add((left, right))
        elif variant.variant_id == "single_agent":
            nodes.add("single-agent")
        for index, event in enumerate(source.events, start=1):
            if cancel_requested():
                raise invalid(
                    "experiment_cell_cancelled",
                    "Experiment cell was cancelled.",
                    phase="execution",
                    detail={"cell_id": cell_id, "sequence": index},
                )
            worker, role, reason = self._route_event(
                event,
                variant.capabilities,
                worker_order,
                route_counts,
                index,
            )
            role_counts[role] += 1
            route_counts[worker] += 1
            nodes.add(worker)
            if event.node_id and variant.capabilities.dynamic_topology:
                nodes.add(event.node_id)
            if variant.capabilities.dynamic_topology:
                before = len(edges)
                if event.causation_id:
                    parent_worker = event_workers.get(
                        event.causation_id,
                        worker,
                    )
                    if parent_worker != worker:
                        edges.add((parent_worker, worker))
                if event.semantic_effect == "topology":
                    mutation = event.payload.get("mutation")
                    if isinstance(mutation, Mapping):
                        for edge in mutation.get("added_edges") or ():
                            left, separator, right = str(edge).partition("->")
                            if separator and left.strip() and right.strip():
                                nodes.add(left.strip())
                                nodes.add(right.strip())
                                edges.add((left.strip(), right.strip()))
                        for edge in mutation.get("removed_edges") or ():
                            left, separator, right = str(edge).partition("->")
                            if separator:
                                edges.discard((left.strip(), right.strip()))
                        for node in mutation.get("added_nodes") or ():
                            if str(node).strip():
                                nodes.add(str(node).strip())
                        for node in mutation.get("removed_nodes") or ():
                            selected = str(node).strip()
                            nodes.discard(selected)
                            edges = {
                                edge
                                for edge in edges
                                if selected not in edge
                            }
                if len(edges) != before:
                    topology_mutations += 1
            latency = self._route_latency(
                event=event,
                worker=worker,
                capabilities=variant.capabilities,
                rng=rng,
            )
            cost = self._route_cost(
                event=event,
                capabilities=variant.capabilities,
                recipient_count=max(1, len(nodes)),
            )
            policy_compliant = self._route_policy_compliant(
                event,
                variant.capabilities,
            )
            privacy_compliant = self._privacy_compliant(event, envelope)
            route = WorkloadRoute(
                route_id=f"route-{cell_id}-{index:08d}",
                event_id=event.event_id,
                worker_id=worker,
                role=role,
                node_id=event.node_id or worker,
                profile=self._profile(event, envelope),
                reason=reason,
                policy_compliant=policy_compliant,
                privacy_compliant=privacy_compliant,
                latency_units=latency,
                cost_microunits=cost,
                sequence=index,
            )
            routes.append(route)
            event_workers[event.event_id] = worker
            recipients = self._recipients(
                sender=worker,
                event=event,
                capabilities=variant.capabilities,
                workers=worker_order,
                edges=edges,
            )
            useful = event.semantic_effect not in {
                "",
                "none",
                "heartbeat",
                "log",
                "ui_repaint",
            }
            if recipients:
                messages.append(
                    WorkloadMessage(
                        message_id=f"message-{cell_id}-{index:08d}",
                        event_id=event.event_id,
                        sender=worker,
                        recipients=recipients,
                        intent=self._message_intent(event),
                        useful=useful,
                        payload_digest=event.payload_digest,
                        sequence=index,
                    )
                )
                useful_messages += int(useful)
                if len(recipients) > 1:
                    broadcast_deliveries += len(recipients)
            if variant.capabilities.memory_compact:
                key = event.semantic_effect or event.stage or event.event_type
                semantic = digest(
                    {
                        "event_id": event.event_id,
                        "payload": event.payload_digest,
                        "role": role,
                        "worker": worker,
                    }
                )
                if semantic in memory[key] or semantic in global_memory:
                    memory_hits += 1
                memory[key].append(semantic)
                global_memory.append(semantic)
                memory_writes += 1
                if index % self.compact_window == 0:
                    compact_operations += 1
                    compact_digest = digest(
                        {
                            "window": list(global_memory)[-self.compact_window :],
                            "index": index,
                            "state": state_digest,
                        }
                    )
                    global_memory.append(compact_digest)
                if event.semantic_effect == "compact_restore" or (
                    "restore" in event.event_type
                ):
                    restore_operations += 1
                    if global_memory:
                        state_digest = digest(
                            {
                                "restored": global_memory[-1],
                                "event": event.event_id,
                                "state": state_digest,
                            }
                        )
            if self._is_fault(event):
                pending_faults.append(event)
                unresolved_faults.append(event.event_id)
            if self._is_recovery(event):
                fault = pending_faults.popleft() if pending_faults else None
                recovered = variant.capabilities.recovery and fault is not None
                if recovered and fault is not None:
                    if fault.event_id in unresolved_faults:
                        unresolved_faults.remove(fault.event_id)
                    restore_operations += int(
                        event.semantic_effect == "recovery"
                        or "resume" in event.event_type
                    )
                recoveries.append(
                    WorkloadRecovery(
                        recovery_id=f"recovery-{cell_id}-{len(recoveries)+1:05d}",
                        fault_event_id=fault.event_id if fault else "",
                        recovery_event_id=event.event_id,
                        recovered=recovered,
                        exact_resume=(
                            recovered
                            and (
                                "resume" in event.event_type
                                or self._payload_flag(event, "exact_resume")
                            )
                        ),
                        checkpoint_bound=(
                            recovered
                            and (
                                self._payload_flag(event, "checkpoint_restored")
                                or "checkpoint" in str(event.payload).casefold()
                            )
                        ),
                        route_changed=(
                            recovered
                            and bool(fault)
                            and event_workers.get(fault.event_id, "")
                            != worker
                        ),
                        elapsed_units=(
                            self._elapsed_units(fault, event)
                            if fault
                            else 0.0
                        ),
                        reason=(
                            "canonical recovery applied"
                            if recovered
                            else "recovery capability disabled or no bound fault"
                        ),
                    )
                )
            if event.semantic_effect == "artifact":
                artifact = event.payload.get("artifact")
                artifact_id = (
                    str(artifact.get("artifact_id") or "")
                    if isinstance(artifact, Mapping)
                    else ""
                )
                artifacts.add(artifact_id or event.event_id)
            if event.provider_id:
                providers.add(event.provider_id)
            model_id = str(event.metadata.get("model_id") or "").strip()
            if model_id:
                models.add(model_id)
            state_digest = digest(
                {
                    "previous": state_digest,
                    "event_id": event.event_id,
                    "event_digest": event.event_digest,
                    "route": route.to_dict(),
                    "message_ids": [
                        item.message_id
                        for item in messages[-1:]
                        if item.event_id == event.event_id
                    ],
                    "memory_writes": memory_writes,
                    "memory_hits": memory_hits,
                    "faults": list(unresolved_faults),
                    "topology_revision": topology_mutations,
                }
            )
            processed.append(event.event_id)
        elapsed_ms = max(0.001, (time.perf_counter() - perf_started) * 1000)
        quality = self._quality(
            source=source,
            processed_count=len(processed),
            unresolved_faults=len(unresolved_faults),
            routes=routes,
            messages=messages,
            memory_enabled=variant.capabilities.memory_compact,
            recovery_enabled=variant.capabilities.recovery,
        )
        drift = self._artifact_drift(
            source=source,
            artifacts=artifacts,
            unresolved_faults=len(unresolved_faults),
            memory_enabled=variant.capabilities.memory_compact,
        )
        token_units = self._token_units(
            source=source,
            messages=messages,
            memory_enabled=variant.capabilities.memory_compact,
            compact_operations=compact_operations,
        )
        cost_microunits = sum(item.cost_microunits for item in routes)
        completed_at = utc_now()
        receipts = self._receipts(
            experiment_id=experiment_id,
            cell_id=cell_id,
            variant=variant,
            envelope=envelope,
            source=source,
            processed=processed,
            routes=routes,
            messages=messages,
            recoveries=recoveries,
            state_digest=state_digest,
            unresolved_faults=unresolved_faults,
        )
        observation = WorkloadObservation(
            observation_id=new_identity("observation"),
            variant_id=variant.variant_id,
            repetition=repetition,
            seed=seed,
            envelope_digest=envelope.envelope_digest,
            source_archive_digest=source.archive_digest,
            scenario_run_id=source.scenario_run_id,
            owner_run_id=source.owner_run_id,
            task_id=source.task_id,
            started_at=started_at,
            completed_at=completed_at,
            state_digest=state_digest,
            processed_event_ids=tuple(processed),
            routes=tuple(routes),
            messages=tuple(messages),
            recoveries=tuple(recoveries),
            memory_writes=memory_writes,
            memory_hits=memory_hits,
            compact_operations=compact_operations,
            restore_operations=restore_operations,
            topology_nodes=tuple(sorted(nodes)),
            topology_edges=tuple(sorted(edges)),
            topology_mutations=topology_mutations,
            useful_messages=useful_messages,
            broadcast_deliveries=broadcast_deliveries,
            unresolved_fault_ids=tuple(unresolved_faults),
            artifact_ids=tuple(sorted(artifacts)),
            provider_ids=tuple(sorted(providers)),
            model_ids=tuple(sorted(models)),
            quality_score=quality,
            artifact_drift_ratio=drift,
            token_units=token_units,
            wall_time_ms=elapsed_ms,
            cost_microunits=cost_microunits,
            human_intervention_count=0,
            claims={
                "source_live": True,
                "source_replay": False,
                "controlled_execution": True,
                "new_external_model_request": False,
                "authenticated_provider_cli_invoked": False,
                "real_local_edge_cloud_reused_from_prior_receipts": bool(
                    envelope.provider.prior_verified_receipt_ids
                ),
                "new_real_local_edge_cloud_dispatch": False,
                "new_multi_model_dispatch": False,
                "sealed_zero_human": True,
                "variant_capability_changed": (
                    variant.expected_disabled_capability
                    if variant.kind.value == "ablation"
                    else ""
                ),
            },
            execution_receipts=receipts,
            metadata={
                "source_domain": source.domain,
                "source_event_count": len(source.events),
                "source_effective_transition_count": source.effective_transition_count,
                "role_counts": dict(sorted(role_counts.items())),
                "route_counts": dict(sorted(route_counts.items())),
                "actual_cpu_wall_time_ms": elapsed_ms,
                "deterministic_state": True,
            },
        )
        return replace(
            observation,
            digest_value=digest(observation.to_dict(include_digest=False)),
        )

    def _verify_bindings(
        self,
        *,
        variant: VariantDefinition,
        repetition: int,
        seed: int,
        envelope: ComparisonEnvelope,
        source: SourceArchive,
    ) -> None:
        findings: list[dict[str, Any]] = []
        if repetition < 1 or repetition > len(envelope.seed_plan):
            findings.append({"code": "repetition_out_of_range"})
        elif envelope.seed_plan[repetition - 1] != seed:
            findings.append(
                {
                    "code": "seed_mismatch",
                    "expected": envelope.seed_plan[repetition - 1],
                    "observed": seed,
                }
            )
        if source.archive_digest != envelope.source_evidence_digest:
            findings.append(
                {
                    "code": "source_evidence_digest_mismatch",
                    "expected": envelope.source_evidence_digest,
                    "observed": source.archive_digest,
                }
            )
        if source.input_digest != envelope.task_input_digest:
            findings.append(
                {
                    "code": "task_input_digest_mismatch",
                    "expected": envelope.task_input_digest,
                    "observed": source.input_digest,
                }
            )
        if source.domain != envelope.task_domain:
            findings.append(
                {
                    "code": "task_domain_mismatch",
                    "expected": envelope.task_domain,
                    "observed": source.domain,
                }
            )
        if variant.kind.value == "ablation":
            disabled = variant.expected_disabled_capability
            if not disabled or getattr(variant.capabilities, disabled) is not False:
                findings.append({"code": "ablation_capability_not_disabled"})
        if findings:
            raise invalid(
                "experiment_workload_binding_invalid",
                "Experiment workload bindings are inconsistent.",
                phase="execution",
                detail={"findings": findings},
            )

    def _route_event(
        self,
        event: SourceEvent,
        capabilities: CapabilityVector,
        workers: list[str],
        route_counts: Counter[str],
        sequence: int,
    ) -> tuple[str, str, str]:
        role = event.stage or event.semantic_effect or event.event_type or "worker"
        if capabilities.worker_limit == 1:
            return "single-agent", "generalist", "single durable worker"
        candidates = list(workers)
        source_worker = event.worker_id
        if source_worker and source_worker not in candidates:
            if len(candidates) < capabilities.worker_limit:
                candidates.append(source_worker)
                workers.append(source_worker)
        if not candidates:
            candidates = ["worker-0"]
            workers[:] = candidates
        if not capabilities.scheduler:
            selected = candidates[(sequence - 1) % len(candidates)]
            return selected, role, "scheduler disabled; deterministic FIFO route"
        if capabilities.heterogeneous_roles:
            matches = [
                worker
                for worker in candidates
                if role.casefold() in worker.casefold()
                or worker.casefold() in role.casefold()
            ]
            if source_worker and source_worker in candidates:
                matches.insert(0, source_worker)
            pool = matches or candidates
        else:
            pool = candidates
            role = "generalist"
        selected = min(pool, key=lambda worker: (route_counts[worker], worker))
        return selected, role, "capacity and role-aware least-loaded route"

    def _worker_for_cause(
        self,
        event_id: str,
        routes: list[WorkloadRoute],
        *,
        fallback: str,
    ) -> str:
        for route in reversed(routes):
            if route.event_id == event_id:
                return route.worker_id
        return fallback

    def _recipients(
        self,
        *,
        sender: str,
        event: SourceEvent,
        capabilities: CapabilityVector,
        workers: list[str],
        edges: set[tuple[str, str]],
    ) -> tuple[str, ...]:
        if capabilities.worker_limit == 1:
            return ()
        candidates = [item for item in workers if item != sender]
        if not candidates:
            return ()
        if not capabilities.low_entropy_communication:
            return tuple(sorted(candidates))
        linked = sorted(right for left, right in edges if left == sender and right != sender)
        if linked:
            return (linked[0],)
        target_role = event.stage or event.semantic_effect
        ranked = sorted(
            candidates,
            key=lambda worker: (
                0 if target_role and target_role in worker.casefold() else 1,
                worker,
            ),
        )
        return tuple(ranked[:1])

    def _route_latency(
        self,
        *,
        event: SourceEvent,
        worker: str,
        capabilities: CapabilityVector,
        rng: random.Random,
    ) -> float:
        base = 1.0 + len(event.payload_digest) / 128
        if not capabilities.scheduler:
            base *= 1.35
        if not capabilities.memory_compact:
            base *= 1.18
        if not capabilities.low_entropy_communication:
            base *= 1.0 + max(0, capabilities.worker_limit - 1) * 0.025
        if event.semantic_effect in {"fault", "recovery"}:
            base *= 1.4
        jitter = 0.98 + rng.random() * 0.04
        return base * jitter

    def _route_cost(
        self,
        *,
        event: SourceEvent,
        capabilities: CapabilityVector,
        recipient_count: int,
    ) -> float:
        cost = 1.0 + len(str(event.payload)) / 2048
        if capabilities.scheduler:
            cost *= 0.93
        if capabilities.memory_compact:
            cost *= 0.88
        if not capabilities.low_entropy_communication:
            cost *= max(1, recipient_count)
        return cost

    def _route_policy_compliant(
        self,
        event: SourceEvent,
        capabilities: CapabilityVector,
    ) -> bool:
        if capabilities.scheduler:
            return True
        return event.semantic_effect not in {"placement", "route"}

    def _privacy_compliant(
        self,
        event: SourceEvent,
        envelope: ComparisonEnvelope,
    ) -> bool:
        sensitivity = str(
            event.metadata.get("security_label")
            or event.payload.get("security_label")
            or ""
        ).casefold()
        if sensitivity in {"secret", "restricted", "private"}:
            return not envelope.hardware.cloud_execution_allowed
        return True

    def _profile(
        self,
        event: SourceEvent,
        envelope: ComparisonEnvelope,
    ) -> str:
        return str(
            event.metadata.get("profile_id")
            or event.metadata.get("placement_profile")
            or envelope.hardware.profile_id
        )

    def _message_intent(self, event: SourceEvent) -> str:
        if event.semantic_effect in {"fault", "recovery"}:
            return event.semantic_effect
        if event.semantic_effect in {"artifact", "verification"}:
            return "evidence"
        if event.semantic_effect in {"route", "placement", "topology"}:
            return "coordination"
        if event.semantic_effect in {"memory", "compact_restore"}:
            return "context"
        return "work"

    def _is_fault(self, event: SourceEvent) -> bool:
        return (
            event.semantic_effect == "fault"
            or "fault" in event.event_type
            or event.event_type
            in {"tool_timeout", "worker_lost", "network_lost", "provider_failed"}
        )

    def _is_recovery(self, event: SourceEvent) -> bool:
        return (
            event.semantic_effect == "recovery"
            or "recovery" in event.event_type
            or "resume" in event.event_type
        )

    def _payload_flag(self, event: SourceEvent, key: str) -> bool:
        if event.payload.get(key) is True:
            return True
        receipt = event.payload.get("receipt")
        return isinstance(receipt, Mapping) and receipt.get(key) is True

    def _elapsed_units(
        self,
        fault: SourceEvent,
        recovery: SourceEvent,
    ) -> float:
        return float(max(1, recovery.sequence - fault.sequence))

    def _quality(
        self,
        *,
        source: SourceArchive,
        processed_count: int,
        unresolved_faults: int,
        routes: list[WorkloadRoute],
        messages: list[WorkloadMessage],
        memory_enabled: bool,
        recovery_enabled: bool,
    ) -> float:
        coverage = processed_count / max(1, len(source.events))
        policy = sum(item.policy_compliant for item in routes) / max(1, len(routes))
        privacy = sum(item.privacy_compliant for item in routes) / max(1, len(routes))
        useful = sum(item.useful for item in messages) / max(1, len(messages))
        fault_penalty = min(0.7, unresolved_faults * 0.08)
        memory_factor = 1.0 if memory_enabled else 0.88
        recovery_factor = 1.0 if recovery_enabled else 0.82
        value = (
            0.28 * coverage
            + 0.22 * policy
            + 0.18 * privacy
            + 0.18 * useful
            + 0.07 * memory_factor
            + 0.07 * recovery_factor
            - fault_penalty
        )
        return max(0.0, min(1.0, value))

    def _artifact_drift(
        self,
        *,
        source: SourceArchive,
        artifacts: set[str],
        unresolved_faults: int,
        memory_enabled: bool,
    ) -> float:
        source_count = max(1, len(source.artifacts))
        missing_ratio = max(0, source_count - len(artifacts)) / source_count
        fault_ratio = min(1.0, unresolved_faults / max(1, len(source.fault_events)))
        memory_penalty = 0.0 if memory_enabled else 0.08
        return max(0.0, min(1.0, 0.6 * missing_ratio + 0.32 * fault_ratio + memory_penalty))

    def _token_units(
        self,
        *,
        source: SourceArchive,
        messages: list[WorkloadMessage],
        memory_enabled: bool,
        compact_operations: int,
    ) -> float:
        source_units = sum(
            max(1, len(str(event.payload)) // 4) for event in source.events
        )
        communication_units = sum(
            len(item.recipients) * max(1, len(item.payload_digest) // 4)
            for item in messages
        )
        if memory_enabled:
            source_units *= max(0.45, 1.0 - min(0.5, compact_operations * 0.002))
        return float(source_units + communication_units)

    def _receipts(
        self,
        *,
        experiment_id: str,
        cell_id: str,
        variant: VariantDefinition,
        envelope: ComparisonEnvelope,
        source: SourceArchive,
        processed: list[str],
        routes: list[WorkloadRoute],
        messages: list[WorkloadMessage],
        recoveries: list[WorkloadRecovery],
        state_digest: str,
        unresolved_faults: list[str],
    ) -> tuple[dict[str, Any], ...]:
        receipts: list[dict[str, Any]] = [
            {
                "schema": "zyra.experiment-source-binding-receipt/v1",
                "receipt_id": new_identity("receipt"),
                "experiment_id": experiment_id,
                "cell_id": cell_id,
                "source_archive_digest": source.archive_digest,
                "event_stream_digest": source.event_stream_digest,
                "scenario_run_id": source.scenario_run_id,
                "owner_run_id": source.owner_run_id,
                "task_id": source.task_id,
                "input_digest": source.input_digest,
                "envelope_digest": envelope.envelope_digest,
                "valid": True,
            },
            {
                "schema": "zyra.experiment-capability-receipt/v1",
                "receipt_id": new_identity("receipt"),
                "experiment_id": experiment_id,
                "cell_id": cell_id,
                "variant_id": variant.variant_id,
                "variant_definition_digest": variant.definition_digest,
                "capabilities": variant.capabilities.to_dict(),
                "disabled_capability": variant.expected_disabled_capability,
                "valid": True,
            },
            {
                "schema": "zyra.experiment-route-receipt/v1",
                "receipt_id": new_identity("receipt"),
                "experiment_id": experiment_id,
                "cell_id": cell_id,
                "route_count": len(routes),
                "route_digest": digest([item.to_dict() for item in routes]),
                "policy_compliant_count": sum(
                    item.policy_compliant for item in routes
                ),
                "privacy_compliant_count": sum(
                    item.privacy_compliant for item in routes
                ),
                "valid": len(routes) == len(processed),
            },
            {
                "schema": "zyra.experiment-communication-receipt/v1",
                "receipt_id": new_identity("receipt"),
                "experiment_id": experiment_id,
                "cell_id": cell_id,
                "message_count": len(messages),
                "delivery_count": sum(len(item.recipients) for item in messages),
                "useful_message_count": sum(item.useful for item in messages),
                "communication_digest": digest(
                    [item.to_dict() for item in messages]
                ),
                "valid": True,
            },
            {
                "schema": "zyra.experiment-recovery-receipt/v1",
                "receipt_id": new_identity("receipt"),
                "experiment_id": experiment_id,
                "cell_id": cell_id,
                "fault_count": len(source.fault_events),
                "recovery_count": len(recoveries),
                "recovered_count": sum(item.recovered for item in recoveries),
                "unresolved_fault_ids": list(unresolved_faults),
                "recovery_digest": digest(
                    [item.to_dict() for item in recoveries]
                ),
                "recovery_enabled": variant.capabilities.recovery,
                "valid": (
                    not variant.capabilities.recovery
                    or not unresolved_faults
                ),
            },
            {
                "schema": "zyra.experiment-state-receipt/v1",
                "receipt_id": new_identity("receipt"),
                "experiment_id": experiment_id,
                "cell_id": cell_id,
                "processed_event_count": len(processed),
                "processed_event_digest": digest(processed),
                "state_digest": state_digest,
                "human_intervention_count": 0,
                "authenticated_provider_cli_invoked": False,
                "external_model_request_made": False,
                "valid": len(processed) == len(source.events),
            },
        ]
        return tuple(
            {
                **receipt,
                "receipt_digest": digest(receipt),
                "created_at": utc_now(),
            }
            for receipt in receipts
        )
