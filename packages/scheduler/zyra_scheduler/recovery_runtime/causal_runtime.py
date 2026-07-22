from __future__ import annotations

import copy
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import RecoveryPlan, SideEffectState, stable_digest, utc_now
from .store import RecoveryPlanStore


class RecoveryCausalityError(RuntimeError):
    pass


class RecoveryCausalityIncomplete(RecoveryCausalityError):
    pass


class CausalFactKind(StrEnum):
    FAILURE_SPAN = "failure_span"
    OWNER_OBSERVATION = "owner_observation"
    RECOVERY_SIGNAL = "recovery_signal"
    CHECKPOINT = "checkpoint"
    SIDE_EFFECT_FENCE = "side_effect_fence"
    CANDIDATE_SET = "candidate_set"
    RECOVERY_PLAN = "recovery_plan"
    ACTION_RECEIPT = "action_receipt"
    ROUTE_MUTATION = "route_mutation"
    CONTINUATION = "continuation"
    OUTCOME = "outcome"
    MEMORY_UPDATE = "memory_update"
    CANONICAL_EVENT = "canonical_event"


@dataclass(frozen=True, slots=True)
class CausalFact:
    fact_id: str
    kind: CausalFactKind
    run_id: str
    task_id: str
    causation_ids: tuple[str, ...]
    correlation_ids: tuple[str, ...]
    owner: str
    occurred_at: str
    payload: Mapping[str, Any]
    canonical_ref: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return stable_digest({
            "fact_id": self.fact_id,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "causation_ids": list(self.causation_ids),
            "correlation_ids": list(self.correlation_ids),
            "owner": self.owner,
            "payload": dict(self.payload),
            "canonical_ref": self.canonical_ref,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "causation_ids": list(self.causation_ids),
            "correlation_ids": list(self.correlation_ids),
            "owner": self.owner,
            "occurred_at": self.occurred_at,
            "payload": copy.deepcopy(dict(self.payload)),
            "canonical_ref": self.canonical_ref,
            "metadata": copy.deepcopy(dict(self.metadata)),
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class CausalEdge:
    source_fact_id: str
    target_fact_id: str
    relation: str
    explicit: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_fact_id": self.source_fact_id,
            "target_fact_id": self.target_fact_id,
            "relation": self.relation,
            "explicit": self.explicit,
        }


@dataclass(frozen=True, slots=True)
class CausalFinding:
    code: str
    message: str
    blocking: bool
    fact_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
            "fact_ids": list(self.fact_ids),
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class RecoveryCausalTrace:
    trace_id: str
    run_id: str
    task_id: str
    plan_id: str
    facts: tuple[CausalFact, ...]
    edges: tuple[CausalEdge, ...]
    findings: tuple[CausalFinding, ...]
    created_at: str = field(default_factory=utc_now)

    @property
    def complete(self) -> bool:
        return not any(item.blocking for item in self.findings)

    @property
    def fact_kinds(self) -> tuple[CausalFactKind, ...]:
        return tuple(dict.fromkeys(item.kind for item in self.facts))

    def require_complete(self) -> "RecoveryCausalTrace":
        if not self.complete:
            raise RecoveryCausalityIncomplete("; ".join(item.message for item in self.findings if item.blocking))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-causal-trace/v1",
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "facts": [item.to_dict() for item in self.facts],
            "edges": [item.to_dict() for item in self.edges],
            "findings": [item.to_dict() for item in self.findings],
            "fact_kinds": [item.value for item in self.fact_kinds],
            "complete": self.complete,
            "created_at": self.created_at,
        }


class RecoveryCausalTraceRuntime:
    REQUIRED_KINDS = (
        CausalFactKind.RECOVERY_SIGNAL,
        CausalFactKind.RECOVERY_PLAN,
        CausalFactKind.ACTION_RECEIPT,
        CausalFactKind.OUTCOME,
        CausalFactKind.CONTINUATION,
        CausalFactKind.MEMORY_UPDATE,
    )

    def __init__(
        self,
        store: RecoveryPlanStore,
        *,
        external_fact_resolver: Callable[[RecoveryPlan], Sequence[CausalFact | Mapping[str, Any]]] | None = None,
        proof_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
        event_sink: Callable[[RecoveryCausalTrace], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.store = store
        self.external_fact_resolver = external_fact_resolver
        self.proof_resolver = proof_resolver
        self.event_sink = event_sink

    def trace_plan(self, plan_id: str, *, require_complete: bool = False) -> RecoveryCausalTrace:
        plan = self.store.plan(plan_id)
        if plan is None:
            raise RecoveryCausalityError(f"recovery plan not found: {plan_id}")
        facts = list(self._store_facts(plan))
        if self.external_fact_resolver is not None:
            for raw in self.external_fact_resolver(plan):
                fact = raw if isinstance(raw, CausalFact) else self._fact_from_mapping(raw, plan)
                self._assert_scope(plan, fact)
                facts.append(fact)
        facts = self._dedupe(facts)
        edges = self._edges(facts)
        findings = self._findings(plan, facts, edges)
        trace_id = "recoverytrace:" + stable_digest({
            "plan_id": plan.plan_id,
            "facts": [item.digest for item in facts],
            "edges": [item.to_dict() for item in edges],
        })[:40]
        trace = RecoveryCausalTrace(
            trace_id=trace_id,
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            plan_id=plan.plan_id,
            facts=tuple(facts),
            edges=tuple(edges),
            findings=tuple(findings),
        )
        if require_complete:
            trace.require_complete()
        if self.event_sink is not None and trace.complete:
            receipt = dict(self.event_sink(trace) or {})
            if receipt and not str(receipt.get("event_id") or receipt.get("receipt_id") or ""):
                raise RecoveryCausalityError("causal trace event sink returned an unidentifiable receipt")
        return trace

    def task_traces(self, task_id: str, *, require_complete: bool = False) -> tuple[RecoveryCausalTrace, ...]:
        return tuple(
            self.trace_plan(plan.plan_id, require_complete=require_complete)
            for plan in self.store.plans(task_id=task_id, limit=5000)
        )

    def path(
        self,
        trace: RecoveryCausalTrace,
        source_kind: CausalFactKind,
        target_kind: CausalFactKind,
    ) -> tuple[CausalFact, ...]:
        by_id = {item.fact_id: item for item in trace.facts}
        outgoing: defaultdict[str, list[str]] = defaultdict(list)
        for edge in trace.edges:
            outgoing[edge.source_fact_id].append(edge.target_fact_id)
        sources = [item.fact_id for item in trace.facts if item.kind is source_kind]
        targets = {item.fact_id for item in trace.facts if item.kind is target_kind}
        queue = deque((source, (source,)) for source in sources)
        visited: set[str] = set()
        while queue:
            current, route = queue.popleft()
            if current in targets:
                return tuple(by_id[item] for item in route)
            if current in visited:
                continue
            visited.add(current)
            for successor in sorted(outgoing[current]):
                if successor not in route:
                    queue.append((successor, (*route, successor)))
        return ()

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-causal-runtime-contract/v1",
            "required_facts": [item.value for item in self.REQUIRED_KINDS],
            "same_run_task_required": True,
            "failure_to_memory_path_required": True,
            "route_mutation_requires_owner_receipt": True,
            "memory_requires_applied_proof": True,
        }

    def _store_facts(self, plan: RecoveryPlan) -> tuple[CausalFact, ...]:
        signal = plan.signal
        facts: list[CausalFact] = []
        facts.append(self._fact(
            CausalFactKind.RECOVERY_SIGNAL,
            signal.signal_id,
            plan,
            owner="RecoverySignalClassifier",
            causation_ids=(signal.causation_id,) if signal.causation_id else (),
            correlation_ids=(signal.correlation_id,) if signal.correlation_id else (),
            occurred_at=signal.observed_at,
            payload=signal.to_dict(),
            canonical_ref=signal.signal_id,
        ))
        checkpoint = self.store.checkpoint_head(signal.refs.task_id)
        if checkpoint is not None:
            facts.append(self._fact(
                CausalFactKind.CHECKPOINT,
                checkpoint.checkpoint_id,
                plan,
                owner="RecoveryPlanStore",
                causation_ids=(signal.signal_id,),
                correlation_ids=(checkpoint.signature,),
                occurred_at=checkpoint.committed_at or checkpoint.created_at,
                payload={
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "commit_revision": checkpoint.commit_revision,
                    "signature": checkpoint.signature,
                    "graph_signature": checkpoint.graph_signature,
                    "topology_signature": checkpoint.topology_signature,
                    "pending_request_ids": [item.request_id for item in checkpoint.pending_requests],
                    "in_flight_message_ids": [item.message_id for item in checkpoint.in_flight_messages],
                    "side_effect_fence_keys": list(checkpoint.side_effect_fence_keys),
                },
                canonical_ref=checkpoint.checkpoint_id,
            ))
            for fence_key in checkpoint.side_effect_fence_keys:
                fence = self.store.side_effect_fence(fence_key)
                if fence is None:
                    continue
                facts.append(self._fact(
                    CausalFactKind.SIDE_EFFECT_FENCE,
                    fence.fence_key,
                    plan,
                    owner="RecoveryPlanStore",
                    causation_ids=(checkpoint.checkpoint_id,),
                    correlation_ids=(fence.receipt_ref,) if fence.receipt_ref else (),
                    occurred_at=fence.updated_at,
                    payload=fence.to_dict(),
                    canonical_ref=fence.receipt_ref or fence.fence_key,
                ))
        candidate_id = "candidates:" + plan.decision.deterministic_key
        facts.append(self._fact(
            CausalFactKind.CANDIDATE_SET,
            candidate_id,
            plan,
            owner="RecoveryDecisionRuntime",
            causation_ids=(signal.signal_id,),
            correlation_ids=(plan.decision.deterministic_key,),
            occurred_at=plan.created_at,
            payload={
                "selected": plan.decision.selected.to_dict(),
                "candidates": [item.to_dict() for item in plan.decision.candidates],
                "policy_revision": plan.decision.policy_revision,
            },
            canonical_ref=plan.decision.deterministic_key,
        ))
        facts.append(self._fact(
            CausalFactKind.RECOVERY_PLAN,
            plan.plan_id,
            plan,
            owner="RecoveryPlanStore",
            causation_ids=(signal.signal_id, candidate_id),
            correlation_ids=(plan.decision.deterministic_key,),
            occurred_at=plan.created_at,
            payload=plan.to_dict(),
            canonical_ref=plan.plan_id,
        ))
        route_ids: list[str] = []
        for receipt in self.store.action_receipts(plan_id=plan.plan_id):
            facts.append(self._fact(
                CausalFactKind.ACTION_RECEIPT,
                receipt.receipt_id,
                plan,
                owner=receipt.owner,
                causation_ids=(plan.plan_id,),
                correlation_ids=(receipt.request_digest,),
                occurred_at=receipt.created_at,
                payload=receipt.to_dict(),
                canonical_ref=receipt.external_receipt_ref or receipt.receipt_id,
            ))
            if receipt.route_decision is not None:
                route = receipt.route_decision
                route_ids.append(route.route_decision_id)
                facts.append(self._fact(
                    CausalFactKind.ROUTE_MUTATION,
                    route.route_decision_id,
                    plan,
                    owner="LayeredRouteRuntime",
                    causation_ids=(receipt.receipt_id,),
                    correlation_ids=tuple(change.receipt_id for change in route.changes),
                    occurred_at=route.created_at,
                    payload=route.to_dict(),
                    canonical_ref=route.route_decision_id,
                ))
        outcomes = self.store.outcomes(plan_id=plan.plan_id)
        proof_ids: list[str] = []
        for outcome in outcomes:
            facts.append(self._fact(
                CausalFactKind.OUTCOME,
                outcome.outcome_id,
                plan,
                owner="RecoveryActionRuntime",
                causation_ids=tuple(outcome.receipt_ids),
                correlation_ids=tuple(item for item in (outcome.route_decision_id, outcome.checkpoint_id) if item),
                occurred_at=outcome.created_at,
                payload=outcome.to_dict(),
                canonical_ref=outcome.outcome_id,
            ))
            proof_id = str(outcome.metadata.get("applied_proof_id") or "")
            if proof_id:
                proof_ids.append(proof_id)
                proof = dict(self.proof_resolver(proof_id) or {}) if self.proof_resolver else {}
                continuation = dict(proof.get("continuation") or {})
                continuation_id = str(continuation.get("receipt_id") or outcome.metadata.get("continuation_receipt_id") or proof_id)
                facts.append(self._fact(
                    CausalFactKind.CONTINUATION,
                    continuation_id,
                    plan,
                    owner=str(continuation.get("owner") or "ContinuationDispatchRuntime"),
                    causation_ids=(outcome.outcome_id, proof_id),
                    correlation_ids=tuple(outcome.receipt_ids),
                    occurred_at=str(continuation.get("created_at") or outcome.created_at),
                    payload=proof or {
                        "proof_id": proof_id,
                        "continuation_receipt_id": continuation_id,
                        "applied": True,
                    },
                    canonical_ref=continuation_id,
                ))
        for memory in self.store.feedback(task_id=signal.refs.task_id, limit=5000):
            if plan.signal.signal_id not in memory.evidence_refs:
                continue
            facts.append(self._fact(
                CausalFactKind.MEMORY_UPDATE,
                memory.record_id,
                plan,
                owner="RecoveryPlanStore+MemoryFabric",
                causation_ids=tuple(item for item in memory.evidence_refs if item in {
                    *(outcome.outcome_id for outcome in outcomes),
                    *route_ids,
                    *proof_ids,
                }),
                correlation_ids=tuple(memory.evidence_refs),
                occurred_at=memory.created_at,
                payload=memory.to_dict(),
                canonical_ref=memory.record_id,
            ))
        return tuple(facts)

    def _edges(self, facts: Sequence[CausalFact]) -> tuple[CausalEdge, ...]:
        by_id = {item.fact_id: item for item in facts}
        by_correlation: defaultdict[str, list[CausalFact]] = defaultdict(list)
        for fact in facts:
            for correlation in fact.correlation_ids:
                by_correlation[correlation].append(fact)
        edges: dict[tuple[str, str, str], CausalEdge] = {}
        for target in facts:
            for cause in target.causation_ids:
                if cause in by_id and cause != target.fact_id:
                    edge = CausalEdge(cause, target.fact_id, "causes", True)
                    edges[(edge.source_fact_id, edge.target_fact_id, edge.relation)] = edge
            for correlation in target.correlation_ids:
                for source in by_correlation[correlation]:
                    if source.fact_id == target.fact_id or source.occurred_at > target.occurred_at:
                        continue
                    edge = CausalEdge(source.fact_id, target.fact_id, "correlates", False)
                    edges[(edge.source_fact_id, edge.target_fact_id, edge.relation)] = edge
        ordered_kinds = (
            CausalFactKind.FAILURE_SPAN,
            CausalFactKind.OWNER_OBSERVATION,
            CausalFactKind.RECOVERY_SIGNAL,
            CausalFactKind.CHECKPOINT,
            CausalFactKind.SIDE_EFFECT_FENCE,
            CausalFactKind.CANDIDATE_SET,
            CausalFactKind.RECOVERY_PLAN,
            CausalFactKind.ACTION_RECEIPT,
            CausalFactKind.ROUTE_MUTATION,
            CausalFactKind.OUTCOME,
            CausalFactKind.CONTINUATION,
            CausalFactKind.MEMORY_UPDATE,
            CausalFactKind.CANONICAL_EVENT,
        )
        rank = {kind: index for index, kind in enumerate(ordered_kinds)}
        sorted_facts = sorted(facts, key=lambda item: (rank[item.kind], item.occurred_at, item.fact_id))
        for left, right in zip(sorted_facts, sorted_facts[1:]):
            if rank[left.kind] < rank[right.kind]:
                edge = CausalEdge(left.fact_id, right.fact_id, "precedes", False)
                edges.setdefault((edge.source_fact_id, edge.target_fact_id, edge.relation), edge)
        return tuple(sorted(edges.values(), key=lambda item: (item.source_fact_id, item.target_fact_id, item.relation)))

    def _findings(
        self,
        plan: RecoveryPlan,
        facts: Sequence[CausalFact],
        edges: Sequence[CausalEdge],
    ) -> list[CausalFinding]:
        findings: list[CausalFinding] = []
        by_kind: defaultdict[CausalFactKind, list[CausalFact]] = defaultdict(list)
        for fact in facts:
            by_kind[fact.kind].append(fact)
            if fact.run_id != plan.signal.refs.run_id or fact.task_id != plan.signal.refs.task_id:
                findings.append(CausalFinding(
                    code="cross_scope_fact",
                    message=f"causal fact {fact.fact_id} crosses run/task custody",
                    blocking=True,
                    fact_ids=(fact.fact_id,),
                ))
        for kind in self.REQUIRED_KINDS:
            if not by_kind[kind]:
                findings.append(CausalFinding(
                    code="required_fact_missing",
                    message=f"recovery causal trace lacks {kind.value}",
                    blocking=True,
                    metadata={"kind": kind.value},
                ))
        for route in by_kind[CausalFactKind.ROUTE_MUTATION]:
            changes = route.payload.get("changes") or ()
            if not any(isinstance(item, Mapping) and item.get("applied") and item.get("receipt_id") for item in changes):
                findings.append(CausalFinding(
                    code="route_owner_receipt_missing",
                    message="route mutation lacks an applied canonical owner receipt",
                    blocking=True,
                    fact_ids=(route.fact_id,),
                ))
        for memory in by_kind[CausalFactKind.MEMORY_UPDATE]:
            references = set(memory.payload.get("evidence_refs") or ())
            proof_refs = {item.fact_id for item in by_kind[CausalFactKind.CONTINUATION]}
            proof_refs.update(
                str(item.payload.get("proof_id") or "") for item in by_kind[CausalFactKind.CONTINUATION]
            )
            if not (references & proof_refs):
                findings.append(CausalFinding(
                    code="memory_before_applied_proof",
                    message="routing memory is not linked to an applied continuation proof",
                    blocking=True,
                    fact_ids=(memory.fact_id,),
                ))
        committed_fences = [
            item for item in by_kind[CausalFactKind.SIDE_EFFECT_FENCE]
            if str(item.payload.get("state") or "") == SideEffectState.COMMITTED.value
        ]
        for fence in committed_fences:
            repeated = [
                item for item in by_kind[CausalFactKind.SIDE_EFFECT_FENCE]
                if item.fact_id == fence.fact_id and item.canonical_ref != fence.canonical_ref
            ]
            if repeated:
                findings.append(CausalFinding(
                    code="side_effect_replayed",
                    message="committed side-effect fence has conflicting receipt identities",
                    blocking=True,
                    fact_ids=tuple(item.fact_id for item in (fence, *repeated)),
                ))
        if by_kind[CausalFactKind.RECOVERY_SIGNAL] and by_kind[CausalFactKind.MEMORY_UPDATE]:
            trace = RecoveryCausalTrace("temp", plan.signal.refs.run_id, plan.signal.refs.task_id, plan.plan_id, tuple(facts), tuple(edges), ())
            if not self.path(trace, CausalFactKind.RECOVERY_SIGNAL, CausalFactKind.MEMORY_UPDATE):
                findings.append(CausalFinding(
                    code="failure_to_memory_path_missing",
                    message="no directed causal path links the recovery signal to routing memory",
                    blocking=True,
                ))
        return findings

    def _fact_from_mapping(self, value: Mapping[str, Any], plan: RecoveryPlan) -> CausalFact:
        return CausalFact(
            fact_id=str(value["fact_id"]),
            kind=CausalFactKind(str(value["kind"])),
            run_id=str(value.get("run_id") or plan.signal.refs.run_id),
            task_id=str(value.get("task_id") or plan.signal.refs.task_id),
            causation_ids=tuple(value.get("causation_ids") or ()),
            correlation_ids=tuple(value.get("correlation_ids") or ()),
            owner=str(value.get("owner") or "external-owner"),
            occurred_at=str(value.get("occurred_at") or utc_now()),
            payload=copy.deepcopy(dict(value.get("payload") or {})),
            canonical_ref=str(value.get("canonical_ref") or ""),
            metadata=copy.deepcopy(dict(value.get("metadata") or {})),
        )

    @staticmethod
    def _fact(
        kind: CausalFactKind,
        fact_id: str,
        plan: RecoveryPlan,
        *,
        owner: str,
        causation_ids: Sequence[str],
        correlation_ids: Sequence[str],
        occurred_at: str,
        payload: Mapping[str, Any],
        canonical_ref: str,
    ) -> CausalFact:
        return CausalFact(
            fact_id=str(fact_id),
            kind=kind,
            run_id=plan.signal.refs.run_id,
            task_id=plan.signal.refs.task_id,
            causation_ids=tuple(str(item) for item in causation_ids if str(item)),
            correlation_ids=tuple(str(item) for item in correlation_ids if str(item)),
            owner=owner,
            occurred_at=occurred_at or utc_now(),
            payload=copy.deepcopy(dict(payload)),
            canonical_ref=canonical_ref,
        )

    @staticmethod
    def _assert_scope(plan: RecoveryPlan, fact: CausalFact) -> None:
        if fact.run_id != plan.signal.refs.run_id or fact.task_id != plan.signal.refs.task_id:
            raise RecoveryCausalityError("external causal fact crosses recovery plan scope")

    @staticmethod
    def _dedupe(facts: Sequence[CausalFact]) -> list[CausalFact]:
        result: dict[str, CausalFact] = {}
        for fact in facts:
            existing = result.get(fact.fact_id)
            if existing is not None and existing.digest != fact.digest:
                raise RecoveryCausalityError(f"causal fact identity reused with different content: {fact.fact_id}")
            result[fact.fact_id] = fact
        return sorted(result.values(), key=lambda item: (item.occurred_at, item.kind.value, item.fact_id))


def causal_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-causal-runtime-surface/v1",
        "same_run_chain": [
            "failure span",
            "checkpoint",
            "side-effect fence",
            "candidate routes",
            "applied owner receipt",
            "continuation",
            "routing memory",
        ],
        "event_only_route": False,
        "memory_without_applied_proof": False,
    }


__all__ = [
    "CausalEdge",
    "CausalFact",
    "CausalFactKind",
    "CausalFinding",
    "RecoveryCausalTrace",
    "RecoveryCausalTraceRuntime",
    "RecoveryCausalityError",
    "RecoveryCausalityIncomplete",
    "causal_runtime_contract",
]
