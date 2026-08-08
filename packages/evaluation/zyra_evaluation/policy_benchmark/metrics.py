from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from zyra_orchestration.topology_policy import (
    PhysicalDispatchReceipt,
    parse_policy_contract,
)
from zyra_scheduler import PhysicalDispatchReceiptValidator

from .contracts import canonical_digest
from .metric_specs import (
    EmptySampleSemantics,
    FailedTaskSemantics,
    PHASE2_METRIC_SPECS,
)


PHASE2_METRIC_VALUE_SCHEMA = "zyra.phase2-metric-value/v1"

COMMUNICATION_RECEIPTS = "communication_observations"
TOPOLOGY_PROPOSALS = "topology_proposals"
POLICY_DECISIONS = "policy_decisions"
POLICY_OUTCOMES = "policy_outcomes"
READINESS_REPORTS = "readiness_reports"
CONTINUITY_RECEIPTS = "continuity_receipts"
SYMBOLIC_BUNDLES = "symbolic_bundles"
EARLY_EXIT_RECEIPTS = "early_exit_receipts"
ADAPTIVE_DEPTH_RECEIPTS = "adaptive_depth_receipts"
PHYSICAL_DISPATCH_RECEIPTS = "physical_dispatch_receipts"

RECEIPT_KINDS = (
    COMMUNICATION_RECEIPTS,
    TOPOLOGY_PROPOSALS,
    POLICY_DECISIONS,
    POLICY_OUTCOMES,
    READINESS_REPORTS,
    CONTINUITY_RECEIPTS,
    SYMBOLIC_BUNDLES,
    EARLY_EXIT_RECEIPTS,
    ADAPTIVE_DEPTH_RECEIPTS,
    PHYSICAL_DISPATCH_RECEIPTS,
)

_RECEIPT_SCHEMAS = {
    COMMUNICATION_RECEIPTS: "zyra.agentprune-communication-outcome/v1",
    TOPOLOGY_PROPOSALS: "zyra.topology-proposal-artifact/v1",
    POLICY_DECISIONS: "zyra.policy-decision-receipt/v1",
    POLICY_OUTCOMES: "zyra.policy-outcome/v1",
    READINESS_REPORTS: "zyra.mechanism-evidence-readiness-report/v1",
    CONTINUITY_RECEIPTS: "zyra.memory-continuity-receipt/v1",
    SYMBOLIC_BUNDLES: "zyra.neuro-symbolic-evidence-bundle/v1",
    EARLY_EXIT_RECEIPTS: "zyra.early-exit-decision-receipt/v1",
    ADAPTIVE_DEPTH_RECEIPTS: "zyra.adaptive-depth-cost-receipt/v1",
    PHYSICAL_DISPATCH_RECEIPTS: "zyra.physical-dispatch-receipt/v2",
}


class Phase2MetricError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MetricStatus(StrEnum):
    OBSERVED = "observed"
    DEGRADED = "degraded"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class ReceiptResolution:
    kind: str
    available: bool
    receipts: tuple[Any, ...] = ()
    reason: str = ""


class CanonicalReceiptResolver(Protocol):
    def resolve(self, kind: str) -> ReceiptResolution: ...


class InMemoryCanonicalReceiptResolver:
    """Read-only adapter over already admitted canonical receipt objects."""

    def __init__(
        self,
        receipts: Mapping[str, Sequence[Any]] | None = None,
        *,
        disconnected: Iterable[str] = (),
    ) -> None:
        unknown = set(receipts or {}) - set(RECEIPT_KINDS)
        if unknown:
            raise Phase2MetricError(
                "metric_receipt_kind_unknown",
                f"unknown canonical receipt kinds: {sorted(unknown)}",
            )
        self._receipts = {
            kind: tuple(values) for kind, values in (receipts or {}).items()
        }
        self._disconnected = frozenset(str(item) for item in disconnected)

    def resolve(self, kind: str) -> ReceiptResolution:
        if kind not in RECEIPT_KINDS:
            return ReceiptResolution(
                kind=kind,
                available=False,
                reason="unknown_receipt_kind",
            )
        if kind in self._disconnected:
            return ReceiptResolution(
                kind=kind,
                available=False,
                reason="receipt_resolver_disconnected",
            )
        return ReceiptResolution(
            kind=kind,
            available=True,
            receipts=self._receipts.get(kind, ()),
        )


@dataclass(frozen=True, slots=True)
class RunMetricInput:
    run_id: str
    task_id: str
    scenario_id: str
    mechanism_profile: str
    receipt_resolver: CanonicalReceiptResolver
    task_succeeded: bool
    effective_transition_count: int

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "scenario_id", "mechanism_profile"):
            if not str(getattr(self, name) or "").strip():
                raise Phase2MetricError(
                    "metric_run_identity_missing",
                    f"{name} is required",
                )
        if int(self.effective_transition_count) < 0:
            raise Phase2MetricError(
                "metric_transition_count_invalid",
                "effective_transition_count cannot be negative",
            )


@dataclass(frozen=True, slots=True)
class MetricValue:
    metric_id: str
    value: float | None
    numerator: float
    denominator: float
    sample_count: int
    failed_sample_count: int
    status: MetricStatus
    eligible_for_optimization: bool
    reasons: tuple[str, ...]
    source_refs: tuple[str, ...]
    schema_version: str = PHASE2_METRIC_VALUE_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "metric_id": self.metric_id,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "sample_count": self.sample_count,
            "failed_sample_count": self.failed_sample_count,
            "status": self.status.value,
            "eligible_for_optimization": self.eligible_for_optimization,
            "reasons": list(self.reasons),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True, slots=True)
class RunMetricResult:
    run_id: str
    task_id: str
    scenario_id: str
    mechanism_profile: str
    task_succeeded: bool
    metrics: Mapping[str, MetricValue]
    lineage: Mapping[str, tuple[str, ...]]
    evidence_transition_count: int
    source_admission: Mapping[str, Any]

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        body = {
            "schema_version": "zyra.phase2-run-metric-result/v1",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "scenario_id": self.scenario_id,
            "mechanism_profile": self.mechanism_profile,
            "task_succeeded": self.task_succeeded,
            "metrics": {
                key: self.metrics[key].to_dict() for key in sorted(self.metrics)
            },
            "lineage": {
                key: list(self.lineage[key]) for key in sorted(self.lineage)
            },
            "evidence_transition_count": self.evidence_transition_count,
            "source_admission": dict(self.source_admission),
        }
        return {**body, "digest": self.digest} if include_digest else body


def _parse_time(value: Any) -> datetime:
    rendered = str(value or "").strip()
    if not rendered:
        raise Phase2MetricError(
            "metric_receipt_timestamp_missing",
            "canonical receipt timestamp is required",
        )
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise Phase2MetricError(
            "metric_receipt_timestamp_invalid",
            f"invalid canonical receipt timestamp: {value}",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _document(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
    elif hasattr(value, "to_dict"):
        result = dict(value.to_dict())
    else:
        raise Phase2MetricError(
            "metric_receipt_not_canonical",
            f"{type(value).__name__} has no canonical mapping",
        )
    supplied = str(result.get("digest") or "")
    if not supplied:
        raise Phase2MetricError(
            "metric_receipt_digest_missing",
            "canonical metric input requires an owner-issued digest",
        )
    body = {key: item for key, item in result.items() if key != "digest"}
    if canonical_digest(body) != supplied:
        raise Phase2MetricError(
            "metric_receipt_digest_mismatch",
            "canonical receipt digest does not match its content",
        )
    return result


def _admit_document(kind: str, value: Any) -> dict[str, Any]:
    if kind == READINESS_REPORTS:
        if not isinstance(value, Mapping):
            raise Phase2MetricError(
                "metric_receipt_not_canonical",
                "readiness report must be a canonical mapping",
            )
        result = dict(value)
        if result.get("schema") != _RECEIPT_SCHEMAS[kind]:
            raise Phase2MetricError(
                "metric_receipt_schema_invalid",
                "readiness report schema is unsupported",
            )
        supplied = str(result.get("report_digest") or "")
        body = dict(result)
        body.pop("report_digest", None)
        if not supplied or canonical_digest(body) != supplied:
            raise Phase2MetricError(
                "metric_receipt_digest_mismatch",
                "readiness report digest does not match its content",
            )
        return {**result, "digest": supplied}
    document = _document(value)
    schema = str(document.get("schema_version") or document.get("schema") or "")
    if schema != _RECEIPT_SCHEMAS[kind]:
        raise Phase2MetricError(
            "metric_receipt_schema_invalid",
            f"{kind} requires {_RECEIPT_SCHEMAS[kind]}, observed {schema or 'missing'}",
        )
    if kind in {
        TOPOLOGY_PROPOSALS,
        POLICY_DECISIONS,
        POLICY_OUTCOMES,
        CONTINUITY_RECEIPTS,
        SYMBOLIC_BUNDLES,
        PHYSICAL_DISPATCH_RECEIPTS,
    }:
        try:
            parsed = parse_policy_contract(document)
        except Exception as error:
            raise Phase2MetricError(
                "metric_receipt_contract_invalid",
                f"{kind} failed canonical contract admission: {error}",
            ) from error
        if kind == PHYSICAL_DISPATCH_RECEIPTS:
            receipt = PhysicalDispatchReceipt.from_dict(parsed.to_dict())
            validation = PhysicalDispatchReceiptValidator().validate(receipt)
            if not validation.real_gate_closed:
                raise Phase2MetricError(
                    "metric_physical_dispatch_gate_open",
                    "physical dispatch failed the production v2 real gate: "
                    + ",".join(validation.blockers),
                )
    return document


def _payload(document: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = document.get("payload")
    return nested if isinstance(nested, Mapping) else document


def _header(document: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = document.get("header")
    if isinstance(nested, Mapping):
        return nested
    return document


def _receipt_digest(document: Mapping[str, Any]) -> str:
    return str(document.get("digest") or canonical_digest(document))


def _identity(kind: str, document: Mapping[str, Any]) -> str:
    payload = _payload(document)
    header = _header(document)
    # One adaptive-depth proposal is evaluated after each executed layer.  The
    # cost snapshot therefore evolves while the proposal id remains constant;
    # the terminal early-exit decision is the canonical identity of each
    # snapshot.  Treating proposal_id as the receipt identity incorrectly
    # reports those legitimate successive observations as an idempotency
    # conflict.
    if kind == ADAPTIVE_DEPTH_RECEIPTS:
        candidates = (
            payload.get("decision_ref"),
            payload.get("proposal_id"),
        )
    else:
        candidates = (
            header.get("idempotency_key"),
            payload.get("observation_id"),
            payload.get("proposal_id"),
            payload.get("decision_id"),
            payload.get("placement_decision_id"),
            payload.get("physical_attempt_id"),
            payload.get("snapshot_id"),
            payload.get("contract_id"),
            document.get("report_digest"),
        )
    selected = next((str(item) for item in candidates if str(item or "").strip()), "")
    return f"{kind}:{selected or _receipt_digest(document)}"


def _deduplicate(kind: str, values: Sequence[Any]) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    unique: dict[str, tuple[str, dict[str, Any]]] = {}
    for value in values:
        document = _admit_document(kind, value)
        identity = _identity(kind, document)
        digest = _receipt_digest(document)
        previous = unique.get(identity)
        if previous is not None:
            if previous[0] != digest:
                raise Phase2MetricError(
                    "metric_idempotency_conflict",
                    f"{identity} resolves to inconsistent canonical digests",
                )
            continue
        unique[identity] = (digest, document)
    ordered = tuple(unique[key][1] for key in sorted(unique))
    refs = tuple(unique[key][0] for key in sorted(unique))
    return ordered, refs


def _status_from_empty(semantics: EmptySampleSemantics) -> MetricStatus:
    return {
        EmptySampleSemantics.NOT_APPLICABLE: MetricStatus.NOT_APPLICABLE,
        EmptySampleSemantics.DEGRADED: MetricStatus.DEGRADED,
        EmptySampleSemantics.FAILED: MetricStatus.FAILED,
    }[semantics]


def _metric(
    metric_id: str,
    numerator: float,
    denominator: float,
    sample_count: int,
    refs: Sequence[str],
    *,
    task_succeeded: bool,
    available: bool = True,
    value: float | None = None,
    reasons: Sequence[str] = (),
) -> MetricValue:
    spec = PHASE2_METRIC_SPECS[metric_id]
    failed_count = 0 if task_succeeded else 1
    rendered_reasons = list(reasons)
    if not available:
        status = _status_from_empty(spec.missing_semantics)
        rendered_reasons.append("canonical_receipt_resolver_unavailable")
        result_value = None
    elif sample_count < spec.minimum_sample_size or denominator == 0:
        status = _status_from_empty(spec.empty_semantics)
        rendered_reasons.append(
            "minimum_sample_not_met"
            if sample_count < spec.minimum_sample_size
            else "zero_denominator"
        )
        result_value = None
    else:
        status = MetricStatus.OBSERVED
        result_value = (
            float(value)
            if value is not None
            else float(numerator) / float(denominator)
        )
    eligible = status is MetricStatus.OBSERVED
    if (
        not task_succeeded
        and spec.failed_task_semantics
        is FailedTaskSemantics.EXCLUDE_FROM_OPTIMIZATION
    ):
        eligible = False
        status = MetricStatus.FAILED
        rendered_reasons.append("failed_task_excluded_from_optimization")
    elif (
        not task_succeeded
        and spec.failed_task_semantics is FailedTaskSemantics.COUNT_AS_FAILURE
    ):
        eligible = False
        status = MetricStatus.FAILED
        rendered_reasons.append("failed_task_counted_as_failure")
    return MetricValue(
        metric_id=metric_id,
        value=result_value,
        numerator=float(numerator),
        denominator=float(denominator),
        sample_count=int(sample_count),
        failed_sample_count=failed_count,
        status=status,
        eligible_for_optimization=eligible,
        reasons=tuple(sorted(set(rendered_reasons))),
        source_refs=tuple(sorted(set(refs))),
    )


def _entropy(counts: Iterable[int]) -> float:
    values = tuple(int(item) for item in counts if int(item) > 0)
    total = sum(values)
    if total <= 0:
        return 0.0
    return -sum(
        (count / total) * math.log2(count / total)
        for count in values
    )


def _communication_metrics(
    documents: Sequence[Mapping[str, Any]],
    refs: Sequence[str],
    *,
    available: bool,
    task_succeeded: bool,
    effective_transition_count: int,
) -> dict[str, MetricValue]:
    delivered = [
        item for item in documents if bool(_payload(item).get("delivered"))
    ]
    nodes = {
        str(_payload(item).get(key) or "")
        for item in delivered
        for key in ("source_node_id", "target_node_id")
        if str(_payload(item).get(key) or "")
    }
    pairs = Counter(
        (
            str(_payload(item).get("source_node_id") or ""),
            str(_payload(item).get("target_node_id") or ""),
        )
        for item in delivered
    )
    max_pairs = len(nodes) * max(0, len(nodes) - 1)
    entropy_denominator = math.log2(max_pairs) if max_pairs > 1 else 0.0
    normalized_entropy = (
        _entropy(pairs.values()) / entropy_denominator
        if entropy_denominator > 0
        else None
    )
    by_sender: dict[str, Counter[str]] = defaultdict(Counter)
    for (source, target), count in pairs.items():
        by_sender[source][target] += count
    total_messages = len(delivered)
    sender_entropy = 0.0
    sender_entropy_bound = 0.0
    for targets in by_sender.values():
        sender_total = sum(targets.values())
        weight = sender_total / max(total_messages, 1)
        sender_entropy += weight * _entropy(targets.values())
        recipient_bound = max(0, len(nodes) - 1)
        if recipient_bound > 1:
            sender_entropy_bound += weight * math.log2(recipient_bound)
    sender_conditioned = (
        sender_entropy / sender_entropy_bound
        if sender_entropy_bound > 0
        else (0.0 if total_messages > 0 and len(nodes) > 1 else None)
    )
    payloads = Counter(
        str(_payload(item).get("payload_digest") or "") for item in delivered
    )
    duplicate_count = sum(
        1
        for item in delivered
        if payloads[str(_payload(item).get("payload_digest") or "")] > 1
        or bool(_payload(item).get("redundant_with_message_id"))
    )
    evidence = {
        str(ref)
        for item in delivered
        for ref in (_payload(item).get("evidence_refs") or ())
    }
    utilized = {
        str(ref)
        for item in delivered
        for ref in (_payload(item).get("utilized_evidence_refs") or ())
    }
    useful = sum(
        1
        for item in delivered
        if (
            bool(_payload(item).get("utilized_evidence_refs"))
            or (
                bool(_payload(item).get("artifact_refs"))
                and _payload(item).get("verifier_result") == "passed"
            )
        )
    )
    spatial = sum(
        1 for item in delivered if _payload(item).get("edge_type") == "spatial"
    )
    temporal = sum(
        1 for item in delivered if _payload(item).get("edge_type") == "temporal"
    )
    delivered_bytes = sum(
        int(_payload(item).get("message_bytes") or 0) for item in delivered
    )
    delivered_tokens = sum(
        int(_payload(item).get("total_tokens") or 0)
        or int(_payload(item).get("prompt_tokens") or 0)
        + int(_payload(item).get("completion_tokens") or 0)
        for item in delivered
    )
    delivered_cost = sum(
        float(_payload(item).get("cost_usd") or 0.0) for item in delivered
    )
    count = len(delivered)
    common = {
        "sample_count": count,
        "refs": refs,
        "available": available,
        "task_succeeded": task_succeeded,
    }
    return {
        "communication.normalized_entropy": _metric(
            "communication.normalized_entropy",
            normalized_entropy or 0.0,
            1.0 if normalized_entropy is not None else 0.0,
            count,
            refs,
            available=available,
            task_succeeded=task_succeeded,
            value=normalized_entropy,
        ),
        "communication.sender_conditioned_recipient_entropy": _metric(
            "communication.sender_conditioned_recipient_entropy",
            sender_conditioned or 0.0,
            1.0 if sender_conditioned is not None else 0.0,
            count,
            refs,
            available=available,
            task_succeeded=task_succeeded,
            value=sender_conditioned,
        ),
        "communication.spatial_delivery_count": _metric(
            "communication.spatial_delivery_count", spatial, 1, count, refs, available=available, task_succeeded=task_succeeded, value=spatial
        ),
        "communication.temporal_delivery_count": _metric(
            "communication.temporal_delivery_count", temporal, 1, count, refs, available=available, task_succeeded=task_succeeded, value=temporal
        ),
        "communication.delivered_bytes": _metric(
            "communication.delivered_bytes", delivered_bytes, 1, count, refs, available=available, task_succeeded=task_succeeded, value=delivered_bytes
        ),
        "communication.delivered_tokens": _metric(
            "communication.delivered_tokens", delivered_tokens, 1, count, refs, available=available, task_succeeded=task_succeeded, value=delivered_tokens
        ),
        "communication.delivered_cost_usd": _metric(
            "communication.delivered_cost_usd", delivered_cost, 1, count, refs, available=available, task_succeeded=task_succeeded, value=delivered_cost
        ),
        "communication.duplicate_semantic_payload_ratio": _metric(
            "communication.duplicate_semantic_payload_ratio", duplicate_count, count, count, refs, available=available, task_succeeded=task_succeeded
        ),
        "communication.evidence_utilization_ratio": _metric(
            "communication.evidence_utilization_ratio", len(utilized), len(evidence), count, refs, available=available, task_succeeded=task_succeeded
        ),
        "communication.useful_message_ratio": _metric(
            "communication.useful_message_ratio", useful, count, count, refs, available=available, task_succeeded=task_succeeded
        ),
        "communication.cost_per_effective_transition": _metric(
            "communication.cost_per_effective_transition", delivered_cost, effective_transition_count, count, refs, available=available, task_succeeded=task_succeeded
        ),
    }


def _topology_metrics(
    proposals: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
    refs: Sequence[str],
    *,
    available: bool,
    task_succeeded: bool,
) -> dict[str, MetricValue]:
    by_proposal = {
        str(_payload(item).get("proposal_id") or ""): item for item in proposals
    }
    proposal_digest = {
        str(_payload(item).get("proposal_id") or ""): _receipt_digest(item)
        for item in proposals
    }
    latencies: list[float] = []
    dispositions: Counter[str] = Counter()
    operation_history: dict[str, list[str]] = defaultdict(list)
    operation_count = 0
    graph_entities: set[str] = set()
    decision_overheads: list[float] = []
    decision_ids: set[str] = set()
    accepted_decision_ids: set[str] = set()
    for item in decisions:
        payload = _payload(item)
        proposal_id = str(payload.get("proposal_id") or "")
        proposal = by_proposal.get(proposal_id)
        if proposal is None:
            raise Phase2MetricError(
                "metric_proposal_reference_missing",
                f"decision references unknown proposal {proposal_id}",
            )
        if str(payload.get("proposal_digest") or "") != proposal_digest[proposal_id]:
            raise Phase2MetricError(
                "metric_proposal_digest_inconsistent",
                f"decision has inconsistent proposal digest for {proposal_id}",
            )
        created = _parse_time(_header(item).get("created_at"))
        expires = _parse_time(_payload(proposal).get("expires_at"))
        if created > expires:
            raise Phase2MetricError(
                "metric_receipt_stale",
                f"decision for {proposal_id} was created after proposal expiry",
            )
        disposition = str(payload.get("disposition") or "")
        dispositions[disposition] += 1
        decision_id = str(payload.get("decision_id") or "")
        decision_ids.add(decision_id)
        if disposition in {"accept", "project", "rebase", "replay"}:
            accepted_decision_ids.add(decision_id)
        commit = payload.get("graph_commit")
        if isinstance(commit, Mapping) and commit:
            latency = (
                created - _parse_time(_header(proposal).get("created_at"))
            ).total_seconds() * 1000.0
            latencies.append(max(0.0, latency))
        for operation in payload.get("projected_operations") or ():
            if not isinstance(operation, Mapping):
                continue
            kind = str(operation.get("kind") or "")
            entity = str(operation.get("entity_id") or "")
            if entity:
                graph_entities.add(entity)
                operation_history[entity].append(kind)
                operation_count += 1
    outcome_decision_ids: set[str] = set()
    for item in outcomes:
        outcome_payload = _payload(item)
        outcome_decision = str(outcome_payload.get("decision_ref") or "")
        outcome_proposal = str(outcome_payload.get("proposal_ref") or "")
        if outcome_decision not in decision_ids or outcome_proposal not in by_proposal:
            raise Phase2MetricError(
                "metric_policy_outcome_reference_missing",
                "policy outcome does not bind to a known proposal and decision",
            )
        outcome_decision_ids.add(outcome_decision)
        metrics = outcome_payload.get("metrics")
        if isinstance(metrics, Mapping):
            raw = metrics.get("mechanism_decision_overhead_ms")
            if raw is not None:
                decision_overheads.append(float(raw))
    missing_outcomes = accepted_decision_ids - outcome_decision_ids
    if missing_outcomes:
        raise Phase2MetricError(
            "metric_policy_outcome_reference_missing",
            f"accepted decisions have no canonical outcome: {sorted(missing_outcomes)}",
        )
    inverses = {
        ("add_node", "remove_node"),
        ("remove_node", "add_node"),
        ("add_edge", "remove_edge"),
        ("remove_edge", "add_edge"),
        ("add_role", "remove_role"),
        ("remove_role", "add_role"),
        ("add_capability", "remove_capability"),
        ("remove_capability", "add_capability"),
    }
    oscillations = sum(
        1
        for history in operation_history.values()
        for pair in zip(history, history[1:])
        if pair in inverses
    )
    total = len(decisions)
    degraded = sum(
        dispositions[key] for key in ("conflict", "diagnostic_only", "replay")
    )
    churn_denominator = len(graph_entities) * max(total, 1)
    return {
        "topology.adaptation_latency_ms": _metric(
            "topology.adaptation_latency_ms", sum(latencies), len(latencies), len(latencies), refs, available=available, task_succeeded=task_succeeded
        ),
        "topology.normalized_churn": _metric(
            "topology.normalized_churn", operation_count, churn_denominator, total, refs, available=available, task_succeeded=task_succeeded
        ),
        "topology.oscillation_count": _metric(
            "topology.oscillation_count", oscillations, 1, total, refs, available=available, task_succeeded=task_succeeded, value=oscillations
        ),
        "topology.proposal_accept_ratio": _metric(
            "topology.proposal_accept_ratio", dispositions["accept"], total, total, refs, available=available, task_succeeded=task_succeeded
        ),
        "topology.proposal_reject_ratio": _metric(
            "topology.proposal_reject_ratio", dispositions["reject"], total, total, refs, available=available, task_succeeded=task_succeeded
        ),
        "topology.proposal_project_ratio": _metric(
            "topology.proposal_project_ratio", dispositions["project"] + dispositions["rebase"], total, total, refs, available=available, task_succeeded=task_succeeded
        ),
        "topology.proposal_degraded_ratio": _metric(
            "topology.proposal_degraded_ratio", degraded, total, total, refs, available=available, task_succeeded=task_succeeded
        ),
        "topology.mechanism_decision_overhead_ms": _metric(
            "topology.mechanism_decision_overhead_ms", sum(decision_overheads), len(decision_overheads), len(decision_overheads), refs, available=available, task_succeeded=task_succeeded
        ),
    }


def _flatten_readiness(documents: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    mechanisms: list[Mapping[str, Any]] = []
    audits: list[Mapping[str, Any]] = []
    for document in documents:
        nested = document.get("mechanisms")
        if isinstance(nested, Mapping):
            mechanisms.extend(
                item for item in nested.values() if isinstance(item, Mapping)
            )
        elif document.get("mechanism_id"):
            mechanisms.append(document)
        audit = document.get("no_policy_training_audit") or document.get(
            "no_policy_update_audit"
        )
        if isinstance(audit, Mapping):
            audits.append(audit)
    return mechanisms, audits


def _coverage_ratio(value: Any) -> tuple[float, float]:
    if not isinstance(value, Mapping):
        return 0.0, 0.0
    required = tuple(value.get("required") or ())
    observed = set(value.get("observed") or ())
    return float(len(observed.intersection(required))), float(len(required))


def _readiness_metrics(
    documents: Sequence[Mapping[str, Any]],
    refs: Sequence[str],
    *,
    available: bool,
    task_succeeded: bool,
) -> dict[str, MetricValue]:
    mechanisms, audits = _flatten_readiness(documents)
    stage_score = {
        "input_precheck": 1.0 / 3.0,
        "implementation_validated": 2.0 / 3.0,
        "activation_ready": 1.0,
    }
    status_score = {
        "unavailable": 0.0,
        "evidence_only": 0.5,
        "deterministic_ready": 1.0,
    }
    stages = [stage_score.get(str(item.get("readiness_stage") or ""), 0.0) for item in mechanisms]
    statuses = [status_score.get(str(item.get("status") or ""), 0.0) for item in mechanisms]
    fields = [
        field
        for item in mechanisms
        for field in (item.get("field_coverage") or ())
        if isinstance(field, Mapping)
    ]
    required_fields = [item for item in fields if bool(item.get("required"))]
    optional_fields = [item for item in fields if not bool(item.get("required"))]
    required_coverage = sum(float(item.get("coverage_ratio") or 0.0) for item in required_fields)
    optional_coverage = sum(float(item.get("coverage_ratio") or 0.0) for item in optional_fields)
    freshness = sum(float(item.get("freshness_ratio") or 0.0) for item in required_fields)
    confidence = sum(float(item.get("confidence_ratio") or 0.0) for item in required_fields)
    missing = sum(float(item.get("missing_count") or 0.0) for item in required_fields)
    field_samples = sum(float(item.get("sample_count") or 0.0) for item in required_fields)
    scenario_num = scenario_den = failure_num = failure_den = 0.0
    causal_num = causal_den = replay_num = replay_den = 0.0
    modes = Counter()
    for item in mechanisms:
        num, den = _coverage_ratio(item.get("scenario_coverage"))
        scenario_num += num
        scenario_den += den
        num, den = _coverage_ratio(item.get("failure_path_coverage"))
        failure_num += num
        failure_den += den
        causal = item.get("causal_links")
        if isinstance(causal, Mapping):
            required_links = tuple(causal.get("required") or ())
            causal_den += len(required_links)
            causal_num += float(causal.get("completeness_ratio") or 0.0) * len(required_links)
        replay = item.get("deterministic_input_snapshot_replay")
        if isinstance(replay, Mapping):
            replay_den += 1
            replay_num += int(bool(replay.get("passed") or replay.get("match")))
        mode = str(item.get("actual_mode") or item.get("mode") or "")
        if mode:
            modes[mode] += 1
    count = len(mechanisms)
    mode_total = sum(modes.values())
    audit_pass = sum(int(bool(item.get("passed"))) for item in audits)
    metric_args = {
        "available": available,
        "task_succeeded": task_succeeded,
    }
    return {
        "readiness.stage_score": _metric("readiness.stage_score", sum(stages), len(stages), count, refs, **metric_args),
        "readiness.status_score": _metric("readiness.status_score", sum(statuses), len(statuses), count, refs, **metric_args),
        "readiness.required_input_coverage": _metric("readiness.required_input_coverage", required_coverage, len(required_fields), len(required_fields), refs, **metric_args),
        "readiness.optional_input_coverage": _metric("readiness.optional_input_coverage", optional_coverage, len(optional_fields), len(optional_fields), refs, **metric_args),
        "readiness.freshness": _metric("readiness.freshness", freshness, len(required_fields), len(required_fields), refs, **metric_args),
        "readiness.confidence": _metric("readiness.confidence", confidence, len(required_fields), len(required_fields), refs, **metric_args),
        "readiness.missingness": _metric("readiness.missingness", missing, field_samples, len(required_fields), refs, **metric_args),
        "readiness.scenario_coverage": _metric("readiness.scenario_coverage", scenario_num, scenario_den, count, refs, **metric_args),
        "readiness.failure_path_coverage": _metric("readiness.failure_path_coverage", failure_num, failure_den, count, refs, **metric_args),
        "readiness.causal_link_completeness": _metric("readiness.causal_link_completeness", causal_num, causal_den, count, refs, **metric_args),
        "readiness.deterministic_replay_match": _metric("readiness.deterministic_replay_match", replay_num, replay_den, count, refs, **metric_args),
        "readiness.actual_default_mode_ratio": _metric("readiness.actual_default_mode_ratio", modes["default"], mode_total, mode_total, refs, **metric_args),
        "readiness.diagnostic_mode_ratio": _metric("readiness.diagnostic_mode_ratio", modes["diagnostic"], mode_total, mode_total, refs, **metric_args),
        "readiness.baseline_mode_ratio": _metric("readiness.baseline_mode_ratio", modes["baseline"], mode_total, mode_total, refs, **metric_args),
        "readiness.no_policy_audit_pass": _metric("readiness.no_policy_audit_pass", audit_pass, len(audits), len(audits), refs, **metric_args),
    }


def _continuity_metrics(
    documents: Sequence[Mapping[str, Any]],
    refs: Sequence[str],
    *,
    available: bool,
    task_succeeded: bool,
) -> dict[str, MetricValue]:
    fact_total = fact_present = fact_used = fact_present_and_used = provenance = 0
    obligation_total = obligation_retained = stale = duplicates = correct = 0
    for document in documents:
        payload = _payload(document)
        facts = payload.get("critical_fact_results")
        facts = facts if isinstance(facts, Mapping) else {}
        provenance_ids = {
            str(item.get("ref_id") or "")
            for item in (payload.get("provenance_refs") or ())
            if isinstance(item, Mapping)
        }
        for fact_id, raw in facts.items():
            value = raw if isinstance(raw, Mapping) else {}
            fact_total += 1
            fact_present += int(value.get("present_after") is True)
            used = (
                value.get("consumed") is True
                and bool(value.get("usage_event_refs"))
            )
            fact_used += int(used)
            fact_present_and_used += int(
                value.get("present_after") is True and used
            )
            provenance += int(
                str(fact_id) in provenance_ids and bool(value.get("provenance_ref"))
            )
        obligations = payload.get("obligation_results")
        obligations = obligations if isinstance(obligations, Mapping) else {}
        consumed = tuple(obligations.get("consumed_ids") or ())
        missing = tuple(obligations.get("missing_consumption") or ())
        obligation_total += len(consumed) + len(missing)
        if obligations.get("retained") is True:
            obligation_retained += len(consumed)
        stale += int(obligations.get("stale_requirement_execution") is True)
        duplicates += len(obligations.get("duplicate_work_artifact_ids") or ())
        correct += int(
            payload.get("continuity_result") == "passed"
            and bool(payload.get("downstream_decision_ref"))
            and fact_total > 0
            and fact_used > 0
        )
    count = len(documents)
    args = {"available": available, "task_succeeded": task_succeeded}
    return {
        "continuity.critical_fact_recall": _metric("continuity.critical_fact_recall", fact_present_and_used, fact_total, count, refs, **args, reasons=("downstream_usage_required",) if fact_present > fact_used else ()),
        "continuity.unresolved_obligation_retention": _metric("continuity.unresolved_obligation_retention", obligation_retained, obligation_total, count, refs, **args),
        "continuity.provenance_coverage": _metric("continuity.provenance_coverage", provenance, fact_total, count, refs, **args),
        "continuity.stale_requirement_execution_count": _metric("continuity.stale_requirement_execution_count", stale, 1, count, refs, **args, value=stale),
        "continuity.duplicate_completed_work_count": _metric("continuity.duplicate_completed_work_count", duplicates, 1, count, refs, **args, value=duplicates),
        "continuity.first_decision_correctness": _metric("continuity.first_decision_correctness", correct, count, count, refs, **args),
    }


def _symbolic_metrics(
    documents: Sequence[Mapping[str, Any]],
    refs: Sequence[str],
    *,
    available: bool,
    task_succeeded: bool,
) -> dict[str, MetricValue]:
    results = Counter()
    unsafe = bypass = 0
    for item in documents:
        commit = _payload(item).get("commit_or_no_commit")
        commit = commit if isinstance(commit, Mapping) else {}
        disposition = str(commit.get("decision_disposition") or commit.get("result") or "")
        if disposition in {"reject", "rejected", "conflict", "forced_baseline"}:
            results["reject"] += 1
        elif disposition in {"project", "rebase", "repaired"}:
            results["project"] += 1
        elif disposition in {"accept", "accepted"}:
            results["accept"] += 1
        unsafe += int(bool(commit.get("unsafe_commit")))
        bypass += int(bool(commit.get("projector_bypass_production_reachable")))
    count = len(documents)
    args = {"available": available, "task_succeeded": task_succeeded}
    return {
        "symbolic.adversarial_reject_ratio": _metric("symbolic.adversarial_reject_ratio", results["reject"], count, count, refs, **args),
        "symbolic.adversarial_project_ratio": _metric("symbolic.adversarial_project_ratio", results["project"], count, count, refs, **args),
        "symbolic.adversarial_accept_ratio": _metric("symbolic.adversarial_accept_ratio", results["accept"], count, count, refs, **args),
        "symbolic.unsafe_commit_count": _metric("symbolic.unsafe_commit_count", unsafe, 1, count, refs, **args, value=unsafe),
        "symbolic.projector_bypass_reachable": _metric("symbolic.projector_bypass_reachable", bypass, count, count, refs, **args),
    }


def _operator_metrics(
    exits: Sequence[Mapping[str, Any]],
    depths: Sequence[Mapping[str, Any]],
    refs: Sequence[str],
    *,
    available_exit: bool,
    available_depth: bool,
    task_succeeded: bool,
) -> dict[str, MetricValue]:
    exited = [
        _payload(item)
        for item in exits
        if _payload(item).get("decision") == "exit"
        and _payload(item).get("posterior_result") in {"true_exit", "false_exit"}
    ]
    true_exit = sum(item.get("posterior_result") == "true_exit" for item in exited)
    false_exit = sum(item.get("posterior_result") == "false_exit" for item in exited)
    depth_payloads = [_payload(item) for item in depths]
    executed_depth = sum(int(item.get("executed_depth") or 0) for item in depth_payloads)
    executed_operators = sum(
        int(item.get("executed_operator_count") or 0) for item in depth_payloads
    )
    return {
        "operator.early_exit_true_positive_ratio": _metric("operator.early_exit_true_positive_ratio", true_exit, len(exited), len(exited), refs, available=available_exit, task_succeeded=task_succeeded),
        "operator.early_exit_false_positive_ratio": _metric("operator.early_exit_false_positive_ratio", false_exit, len(exited), len(exited), refs, available=available_exit, task_succeeded=task_succeeded),
        "operator.executed_breadth": _metric("operator.executed_breadth", executed_operators, executed_depth, len(depth_payloads), refs, available=available_depth, task_succeeded=task_succeeded),
        "operator.executed_depth": _metric("operator.executed_depth", executed_depth, len(depth_payloads), len(depth_payloads), refs, available=available_depth, task_succeeded=task_succeeded),
    }


def _dispatch_metrics(
    documents: Sequence[Mapping[str, Any]],
    refs: Sequence[str],
    *,
    available: bool,
    task_succeeded: bool,
) -> dict[str, MetricValue]:
    real: list[Mapping[str, Any]] = []
    simulated_count = 0
    for item in documents:
        payload = _payload(item)
        if bool(payload.get("simulated")) or bool(payload.get("semantic_only")):
            simulated_count += 1
            continue
        real.append(payload)
    by_location: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    complete: Counter[str] = Counter()
    provider_complete = privacy_violations = link_present = 0
    ordered: list[tuple[str, str]] = []
    for payload in real:
        identity = payload.get("physical_identity")
        identity = identity if isinstance(identity, Mapping) else {}
        location = str(
            identity.get("location")
            or (payload.get("input_signals") or {}).get("selected_location")
            or ""
        ).casefold()
        by_location[location].append(payload)
        links = (
            payload.get("placement_decision_id"),
            payload.get("lease_id"),
            payload.get("physical_attempt_id"),
            payload.get("call_receipt"),
            payload.get("artifact_ref"),
            payload.get("verifier_ref"),
        )
        link_present += sum(bool(item) for item in links)
        if all(links):
            complete[location] += 1
        allowed = set(payload.get("allowed_placements") or ())
        if location not in allowed:
            privacy_violations += 1
        privacy = payload.get("privacy_evidence")
        if isinstance(privacy, Mapping) and privacy.get("placement_allowed") is False:
            privacy_violations += 1
        provider = payload.get("provider_evidence")
        if location == "cloud" and isinstance(provider, Mapping):
            provider_complete += int(
                bool(provider.get("provider_id"))
                and bool(provider.get("model_id"))
                and bool(provider.get("request_id"))
                and bool(provider.get("provider_attempt_id"))
            )
        ordered.append(
            (
                str(_header(payload).get("created_at") or payload.get("physical_attempt_id") or ""),
                location,
            )
        )
    ordered.sort()
    reroutes = sum(
        previous[1] != current[1]
        for previous, current in zip(ordered, ordered[1:])
    )
    args = {"available": available, "task_succeeded": task_succeeded}
    result: dict[str, MetricValue] = {}
    for location in ("local", "edge", "cloud"):
        metric_id = f"dispatch.{location}_real_receipt_completeness"
        result[metric_id] = _metric(
            metric_id,
            complete[location],
            len(by_location[location]),
            len(by_location[location]),
            refs,
            **args,
            reasons=(f"simulated_receipts_excluded:{simulated_count}",) if simulated_count else (),
        )
    result.update(
        {
            "dispatch.physical_reroute_count": _metric("dispatch.physical_reroute_count", reroutes, 1, len(real), refs, **args, value=reroutes),
            "dispatch.privacy_placement_violation_count": _metric("dispatch.privacy_placement_violation_count", privacy_violations, 1, len(real), refs, **args, value=privacy_violations),
            "dispatch.provider_receipt_coverage": _metric("dispatch.provider_receipt_coverage", provider_complete, len(by_location["cloud"]), len(by_location["cloud"]), refs, **args),
            "dispatch.causal_chain_completeness": _metric("dispatch.causal_chain_completeness", link_present, len(real) * 6, len(real), refs, **args),
        }
    )
    return result


class Phase2MetricEngine:
    """Deterministic, read-only computation over canonical receipt resolvers."""

    def evaluate_run(self, value: RunMetricInput) -> RunMetricResult:
        canonical_transition_count = getattr(
            value.receipt_resolver,
            "canonical_transition_count",
            None,
        )
        if callable(canonical_transition_count):
            observed_transition_count = int(canonical_transition_count())
            if observed_transition_count != value.effective_transition_count:
                raise Phase2MetricError(
                    "metric_transition_count_not_canonical",
                    "runtime transition count differs from the canonical event spine",
                )
        resolved: dict[str, ReceiptResolution] = {
            kind: value.receipt_resolver.resolve(kind) for kind in RECEIPT_KINDS
        }
        documents: dict[str, tuple[dict[str, Any], ...]] = {}
        refs: dict[str, tuple[str, ...]] = {}
        for kind, resolution in resolved.items():
            if not resolution.available:
                documents[kind] = ()
                refs[kind] = ()
                continue
            documents[kind], refs[kind] = _deduplicate(
                kind, resolution.receipts
            )
        metrics: dict[str, MetricValue] = {}
        metrics.update(
            _communication_metrics(
                documents[COMMUNICATION_RECEIPTS],
                refs[COMMUNICATION_RECEIPTS],
                available=resolved[COMMUNICATION_RECEIPTS].available,
                task_succeeded=value.task_succeeded,
                effective_transition_count=value.effective_transition_count,
            )
        )
        topology_available = all(
            resolved[kind].available
            for kind in (TOPOLOGY_PROPOSALS, POLICY_DECISIONS, POLICY_OUTCOMES)
        )
        topology_refs = (
            refs[TOPOLOGY_PROPOSALS]
            + refs[POLICY_DECISIONS]
            + refs[POLICY_OUTCOMES]
        )
        metrics.update(
            _topology_metrics(
                documents[TOPOLOGY_PROPOSALS],
                documents[POLICY_DECISIONS],
                documents[POLICY_OUTCOMES],
                topology_refs,
                available=topology_available,
                task_succeeded=value.task_succeeded,
            )
        )
        metrics.update(
            _readiness_metrics(
                documents[READINESS_REPORTS],
                refs[READINESS_REPORTS],
                available=resolved[READINESS_REPORTS].available,
                task_succeeded=value.task_succeeded,
            )
        )
        metrics.update(
            _continuity_metrics(
                documents[CONTINUITY_RECEIPTS],
                refs[CONTINUITY_RECEIPTS],
                available=resolved[CONTINUITY_RECEIPTS].available,
                task_succeeded=value.task_succeeded,
            )
        )
        metrics.update(
            _symbolic_metrics(
                documents[SYMBOLIC_BUNDLES],
                refs[SYMBOLIC_BUNDLES],
                available=resolved[SYMBOLIC_BUNDLES].available,
                task_succeeded=value.task_succeeded,
            )
        )
        operator_refs = refs[EARLY_EXIT_RECEIPTS] + refs[ADAPTIVE_DEPTH_RECEIPTS]
        metrics.update(
            _operator_metrics(
                documents[EARLY_EXIT_RECEIPTS],
                documents[ADAPTIVE_DEPTH_RECEIPTS],
                operator_refs,
                available_exit=resolved[EARLY_EXIT_RECEIPTS].available,
                available_depth=resolved[ADAPTIVE_DEPTH_RECEIPTS].available,
                task_succeeded=value.task_succeeded,
            )
        )
        metrics.update(
            _dispatch_metrics(
                documents[PHYSICAL_DISPATCH_RECEIPTS],
                refs[PHYSICAL_DISPATCH_RECEIPTS],
                available=resolved[PHYSICAL_DISPATCH_RECEIPTS].available,
                task_succeeded=value.task_succeeded,
            )
        )
        policy_refs = refs[POLICY_DECISIONS] + refs[POLICY_OUTCOMES]
        metrics["evidence.canonical_transition_count"] = _metric(
            "evidence.canonical_transition_count",
            value.effective_transition_count,
            1,
            value.effective_transition_count,
            policy_refs,
            available=(
                resolved[POLICY_DECISIONS].available
                and resolved[POLICY_OUTCOMES].available
            ),
            task_succeeded=value.task_succeeded,
            value=value.effective_transition_count,
            reasons=("evidence_volume_only",),
        )
        missing = set(PHASE2_METRIC_SPECS) - set(metrics)
        if missing:
            raise Phase2MetricError(
                "metric_registry_implementation_incomplete",
                f"metric engine did not calculate: {sorted(missing)}",
            )
        lineage = {
            kind: refs[kind] for kind in RECEIPT_KINDS
        }
        admission_context = getattr(
            value.receipt_resolver,
            "admission_context",
            None,
        )
        source_admission: dict[str, Any] = {}
        if callable(admission_context):
            admission_body = {
                **dict(admission_context()),
                "run_id": value.run_id,
                "task_id": value.task_id,
                "lineage_digest": canonical_digest(
                    {
                        kind: list(lineage[kind])
                        for kind in sorted(lineage)
                    }
                ),
                "canonical_transition_count": value.effective_transition_count,
            }
            source_admission = {
                **admission_body,
                "digest": canonical_digest(admission_body),
            }
        return RunMetricResult(
            run_id=value.run_id,
            task_id=value.task_id,
            scenario_id=value.scenario_id,
            mechanism_profile=value.mechanism_profile,
            task_succeeded=value.task_succeeded,
            metrics=metrics,
            lineage=lineage,
            evidence_transition_count=value.effective_transition_count,
            source_admission=source_admission,
        )


__all__ = [
    "ADAPTIVE_DEPTH_RECEIPTS",
    "COMMUNICATION_RECEIPTS",
    "CONTINUITY_RECEIPTS",
    "CanonicalReceiptResolver",
    "EARLY_EXIT_RECEIPTS",
    "InMemoryCanonicalReceiptResolver",
    "MetricStatus",
    "MetricValue",
    "PHYSICAL_DISPATCH_RECEIPTS",
    "POLICY_DECISIONS",
    "POLICY_OUTCOMES",
    "Phase2MetricEngine",
    "Phase2MetricError",
    "READINESS_REPORTS",
    "RECEIPT_KINDS",
    "ReceiptResolution",
    "RunMetricInput",
    "RunMetricResult",
    "SYMBOLIC_BUNDLES",
    "TOPOLOGY_PROPOSALS",
]
