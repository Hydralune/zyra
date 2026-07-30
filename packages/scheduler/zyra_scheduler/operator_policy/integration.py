from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from zyra_core import EventRecord, EventType, PlanNode, TaskState, now_iso
from zyra_orchestration.topology_policy.contracts import (
    ContractHeader,
    FrozenDict,
    PolicyInputSnapshot,
    StableArtifactRef,
    canonical_digest,
)

from ..models import (
    FailureKind,
    FailureSignal,
    ResourceDecision,
    ResourceLocation,
)
from ..recovery import RecoveryPlanner
from ..scheduler import ResourceScheduler
from ..worker_pool.application import WorkerPoolFoundationRuntime
from ..worker_pool.errors import LeaseFenced, WorkerPoolError
from ..worker_pool.models import (
    CapabilityRequirement,
    ExecutionOutcome,
    LeaseState,
    ResourceVector,
    WorkerLocation,
    parse_utc,
)
from .adaptive_depth import (
    AdaptiveDepthResult,
    AdaptiveDepthRuntime,
    EligibilityCapturePort,
    LayerExecutionReceipt,
)
from .catalog import OperatorCatalog, OperatorProfile, OperatorType
from .early_exit import build_operator_execution_decision
from .selector import (
    OperatorCandidate,
    OperatorLayerProposal,
    OperatorScoreComponents,
    OperatorSelectionProposal,
)


PLACEMENT_CONFIG_SCHEMA = "zyra.operator-placement-lease-config/v1"
OPERATOR_CANDIDATE_SET_SCHEMA = "zyra.operator-candidate-set/v1"
OPERATOR_PLACEMENT_DECISION_SCHEMA = "zyra.operator-placement-decision/v1"
OPERATOR_ATTEMPT_RECEIPT_SCHEMA = "zyra.operator-attempt-receipt/v1"
OPERATOR_TASK_RESULT_SCHEMA = "zyra.operator-placement-task-result/v1"


EventSink = Callable[[EventRecord], None]
CatalogProvider = Callable[[], OperatorCatalog]
Clock = Callable[[], str]


class OperatorPlacementError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.metadata = dict(metadata or {})


@dataclass(frozen=True, slots=True)
class OperatorPlacementLeaseConfig:
    mechanism_id: str
    mechanism_version: str
    candidate_set_schema: str
    placement_receipt_schema: str
    attempt_receipt_schema: str
    lease_ttl_seconds: float
    maximum_recovery_attempts: int
    maximum_layer_concurrency: int
    fallback_profile: str
    require_permission_recheck: bool
    require_catalog_recheck: bool
    require_execution_fence: bool
    schema: str = PLACEMENT_CONFIG_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PLACEMENT_CONFIG_SCHEMA:
            raise OperatorPlacementError(
                "operator_placement_config_schema_invalid",
                f"unsupported operator placement config schema: {self.schema}",
            )
        if self.candidate_set_schema != OPERATOR_CANDIDATE_SET_SCHEMA:
            raise OperatorPlacementError(
                "operator_candidate_set_schema_invalid",
                "configured operator candidate-set schema is unsupported",
            )
        if self.placement_receipt_schema != OPERATOR_PLACEMENT_DECISION_SCHEMA:
            raise OperatorPlacementError(
                "operator_placement_receipt_schema_invalid",
                "configured placement receipt schema is unsupported",
            )
        if self.attempt_receipt_schema != OPERATOR_ATTEMPT_RECEIPT_SCHEMA:
            raise OperatorPlacementError(
                "operator_attempt_receipt_schema_invalid",
                "configured attempt receipt schema is unsupported",
            )
        if self.lease_ttl_seconds <= 0:
            raise OperatorPlacementError(
                "operator_lease_ttl_invalid",
                "operator lease TTL must be positive",
            )
        if self.maximum_recovery_attempts < 0:
            raise OperatorPlacementError(
                "operator_recovery_limit_invalid",
                "operator recovery-attempt limit cannot be negative",
            )
        if self.maximum_layer_concurrency < 1:
            raise OperatorPlacementError(
                "operator_concurrency_limit_invalid",
                "operator layer concurrency must be positive",
            )
        if not all(
            (
                self.mechanism_id,
                self.mechanism_version,
                self.fallback_profile,
            )
        ):
            raise OperatorPlacementError(
                "operator_placement_config_identity_invalid",
                "operator placement config requires mechanism and fallback identity",
            )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "mechanism_id": self.mechanism_id,
            "mechanism_version": self.mechanism_version,
            "candidate_set_schema": self.candidate_set_schema,
            "placement_receipt_schema": self.placement_receipt_schema,
            "attempt_receipt_schema": self.attempt_receipt_schema,
            "lease_ttl_seconds": self.lease_ttl_seconds,
            "maximum_recovery_attempts": self.maximum_recovery_attempts,
            "maximum_layer_concurrency": self.maximum_layer_concurrency,
            "fallback_profile": self.fallback_profile,
            "require_permission_recheck": self.require_permission_recheck,
            "require_catalog_recheck": self.require_catalog_recheck,
            "require_execution_fence": self.require_execution_fence,
        }

    @classmethod
    def load(cls, path: str | Path) -> "OperatorPlacementLeaseConfig":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperatorPlacementError(
                "operator_placement_config_unavailable",
                "operator placement config is missing or corrupt",
            ) from exc
        if not isinstance(payload, Mapping):
            raise OperatorPlacementError(
                "operator_placement_config_invalid",
                "operator placement config must be an object",
            )
        return cls(
            mechanism_id=str(payload.get("mechanism_id") or ""),
            mechanism_version=str(payload.get("mechanism_version") or ""),
            candidate_set_schema=str(payload.get("candidate_set_schema") or ""),
            placement_receipt_schema=str(
                payload.get("placement_receipt_schema") or ""
            ),
            attempt_receipt_schema=str(payload.get("attempt_receipt_schema") or ""),
            lease_ttl_seconds=float(payload.get("lease_ttl_seconds") or 0),
            maximum_recovery_attempts=int(
                payload.get("maximum_recovery_attempts") or 0
            ),
            maximum_layer_concurrency=int(
                payload.get("maximum_layer_concurrency") or 0
            ),
            fallback_profile=str(payload.get("fallback_profile") or ""),
            require_permission_recheck=bool(
                payload.get("require_permission_recheck", True)
            ),
            require_catalog_recheck=bool(
                payload.get("require_catalog_recheck", True)
            ),
            require_execution_fence=bool(
                payload.get("require_execution_fence", True)
            ),
            schema=str(payload.get("schema") or ""),
        )


@dataclass(frozen=True, slots=True)
class PlacementOperatorCandidate:
    operator_id: str
    operator_type: str
    version: str
    profile_digest: str
    source_ref: str
    source_registry: str
    layer_index: int
    layer_rank: int
    proposal_score: float
    capabilities: tuple[str, ...]
    input_contract: tuple[str, ...]
    output_contract: tuple[str, ...]
    required_permissions: tuple[str, ...]
    allowed_locations: tuple[str, ...]
    allowed_privacy_classes: tuple[str, ...]
    health_status: str
    available_capacity: int
    verifier_contracts: tuple[str, ...]
    minimum_evidence_contract: tuple[str, ...]
    estimated_tokens: int
    estimated_cost_usd: float
    estimated_latency_ms: int
    cold_start: bool
    confidence: float
    score_components: FrozenDict

    def __post_init__(self) -> None:
        for name in (
            "capabilities",
            "input_contract",
            "output_contract",
            "required_permissions",
            "allowed_locations",
            "allowed_privacy_classes",
            "verifier_contracts",
            "minimum_evidence_contract",
        ):
            object.__setattr__(
                self,
                name,
                tuple(
                    sorted(
                        {
                            str(item).strip().lower()
                            for item in getattr(self, name)
                            if str(item).strip()
                        }
                    )
                ),
            )
        if (
            not self.operator_id
            or not self.version
            or len(self.profile_digest) != 64
            or self.layer_index < 1
            or self.layer_rank < 1
        ):
            raise OperatorPlacementError(
                "operator_candidate_invalid",
                "operator placement candidates require stable identity, digest and layer",
            )
        object.__setattr__(self, "score_components", FrozenDict(self.score_components))

    @property
    def operator_ref(self) -> str:
        return f"{self.operator_id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "operator_ref": self.operator_ref,
            "operator_type": self.operator_type,
            "version": self.version,
            "profile_digest": self.profile_digest,
            "source_ref": self.source_ref,
            "source_registry": self.source_registry,
            "layer_index": self.layer_index,
            "layer_rank": self.layer_rank,
            "proposal_score": self.proposal_score,
            "capabilities": list(self.capabilities),
            "input_contract": list(self.input_contract),
            "output_contract": list(self.output_contract),
            "required_permissions": list(self.required_permissions),
            "allowed_locations": list(self.allowed_locations),
            "allowed_privacy_classes": list(self.allowed_privacy_classes),
            "health_status": self.health_status,
            "available_capacity": self.available_capacity,
            "verifier_contracts": list(self.verifier_contracts),
            "minimum_evidence_contract": list(self.minimum_evidence_contract),
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_latency_ms": self.estimated_latency_ms,
            "cold_start": self.cold_start,
            "confidence": self.confidence,
            "score_components": dict(self.score_components),
        }


@dataclass(frozen=True, slots=True)
class OperatorCandidateSet:
    run_id: str
    task_id: str
    input_snapshot_digest: str
    requirement_revision: str
    committed_graph_revision: int
    committed_graph_signature: str
    committed_graph_commit_id: str
    proposal_id: str
    proposal_digest: str
    catalog_version: str
    catalog_digest: str
    selector_mechanism_version: str
    integration_mechanism_version: str
    mechanism_receipt_ref: str
    expected_breadth: int
    expected_depth: int
    allowed_permissions: tuple[str, ...]
    allowed_placements: tuple[str, ...]
    privacy_class: str
    remaining_tokens: int
    remaining_cost_usd: float
    remaining_time_ms: int
    candidates: tuple[PlacementOperatorCandidate, ...]
    expires_at: str
    fallback_profile: str
    mode: str
    allow_explicit_baseline: bool = True
    placement_owner: str = "ResourceScheduler"
    lease_owner: str = "WorkerPoolFoundationRuntime"
    schema_version: str = OPERATOR_CANDIDATE_SET_SCHEMA

    def __post_init__(self) -> None:
        for name in ("allowed_permissions", "allowed_placements"):
            object.__setattr__(
                self,
                name,
                tuple(
                    sorted(
                        {
                            str(item).strip().lower()
                            for item in getattr(self, name)
                            if str(item).strip()
                        }
                    )
                ),
            )
        object.__setattr__(
            self,
            "candidates",
            tuple(
                sorted(
                    self.candidates,
                    key=lambda item: (
                        item.layer_index,
                        item.layer_rank,
                        item.operator_id,
                        item.version,
                    ),
                )
            ),
        )
        if self.schema_version != OPERATOR_CANDIDATE_SET_SCHEMA:
            raise OperatorPlacementError(
                "operator_candidate_set_schema_invalid",
                "unsupported operator candidate-set schema",
            )
        if self.placement_owner != "ResourceScheduler":
            raise OperatorPlacementError(
                "operator_placement_owner_invalid",
                "operator candidate set cannot replace ResourceScheduler",
            )
        if self.lease_owner != "WorkerPoolFoundationRuntime":
            raise OperatorPlacementError(
                "operator_lease_owner_invalid",
                "operator candidate set cannot replace the worker lease runtime",
            )
        if self.mode not in {"validation", "default"}:
            raise OperatorPlacementError(
                "operator_candidate_set_mode_invalid",
                "only validation/default candidates may affect placement",
            )
        if not self.candidates:
            raise OperatorPlacementError(
                "operator_candidate_set_empty",
                "operator candidate set must contain executable candidates",
            )
        if self.expected_breadth < 1 or self.expected_depth < 1:
            raise OperatorPlacementError(
                "operator_candidate_shape_invalid",
                "operator breadth and depth must be positive",
            )
        digests = (
            self.input_snapshot_digest,
            self.committed_graph_signature,
            self.proposal_digest,
            self.catalog_digest,
            self.mechanism_receipt_ref,
        )
        if any(not _is_digest(item) for item in digests):
            raise OperatorPlacementError(
                "operator_candidate_digest_invalid",
                "operator candidate set contains a non-SHA-256 binding",
            )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "input_snapshot_digest": self.input_snapshot_digest,
            "requirement_revision": self.requirement_revision,
            "committed_graph_revision": self.committed_graph_revision,
            "committed_graph_signature": self.committed_graph_signature,
            "committed_graph_commit_id": self.committed_graph_commit_id,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "catalog_version": self.catalog_version,
            "catalog_digest": self.catalog_digest,
            "selector_mechanism_version": self.selector_mechanism_version,
            "integration_mechanism_version": self.integration_mechanism_version,
            "mechanism_receipt_ref": self.mechanism_receipt_ref,
            "expected_breadth": self.expected_breadth,
            "expected_depth": self.expected_depth,
            "allowed_permissions": list(self.allowed_permissions),
            "allowed_placements": list(self.allowed_placements),
            "privacy_class": self.privacy_class,
            "remaining_tokens": self.remaining_tokens,
            "remaining_cost_usd": self.remaining_cost_usd,
            "remaining_time_ms": self.remaining_time_ms,
            "candidates": [item.to_dict() for item in self.candidates],
            "expires_at": self.expires_at,
            "fallback_profile": self.fallback_profile,
            "mode": self.mode,
            "allow_explicit_baseline": self.allow_explicit_baseline,
            "placement_owner": self.placement_owner,
            "lease_owner": self.lease_owner,
        }
        if include_digest:
            value["candidate_set_digest"] = self.digest
        return value

    def candidate(self, operator_ref: str) -> PlacementOperatorCandidate:
        selected = next(
            (item for item in self.candidates if item.operator_ref == operator_ref),
            None,
        )
        if selected is None:
            raise OperatorPlacementError(
                "operator_candidate_not_scheduled",
                f"scheduler selected an unknown operator: {operator_ref}",
            )
        return selected

    @classmethod
    def build(
        cls,
        *,
        policy_input: PolicyInputSnapshot,
        proposal: OperatorSelectionProposal,
        catalog: OperatorCatalog,
        integration_mechanism_version: str,
        mechanism_receipt_ref: str,
        mode: str,
    ) -> "OperatorCandidateSet":
        if (
            proposal.input_snapshot_digest != policy_input.digest
            or proposal.requirement_revision != policy_input.requirement_revision
            or proposal.committed_graph_revision != policy_input.graph.revision
            or proposal.committed_graph_signature != policy_input.graph.signature
            or proposal.committed_graph_commit_id != policy_input.graph.commit_id
        ):
            raise OperatorPlacementError(
                "operator_candidate_snapshot_stale",
                "operator proposal no longer matches the canonical policy snapshot",
            )
        catalog.validate_references(
            catalog_version=proposal.catalog_version,
            catalog_digest=proposal.catalog_digest,
            references=tuple(item.reference for item in proposal.candidates),
        )
        placements: list[PlacementOperatorCandidate] = []
        for layer in proposal.layers:
            for rank, candidate in enumerate(layer.candidates, start=1):
                profile = catalog.get(candidate.operator_id)
                if (
                    profile is None
                    or profile.version != candidate.version
                    or profile.digest != candidate.profile_digest
                ):
                    raise OperatorPlacementError(
                        "operator_profile_binding_stale",
                        f"operator profile changed: {candidate.operator_id}@{candidate.version}",
                    )
                placements.append(
                    _placement_candidate(
                        candidate,
                        profile,
                        layer_index=layer.layer_index,
                        layer_rank=rank,
                    )
                )
        return cls(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            input_snapshot_digest=policy_input.digest,
            requirement_revision=policy_input.requirement_revision,
            committed_graph_revision=policy_input.graph.revision,
            committed_graph_signature=policy_input.graph.signature,
            committed_graph_commit_id=policy_input.graph.commit_id,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.digest,
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.digest,
            selector_mechanism_version=proposal.header.mechanism_version,
            integration_mechanism_version=integration_mechanism_version,
            mechanism_receipt_ref=mechanism_receipt_ref,
            expected_breadth=proposal.expected_breadth,
            expected_depth=proposal.expected_depth,
            allowed_permissions=policy_input.allowed_permissions,
            allowed_placements=policy_input.allowed_placements,
            privacy_class=policy_input.privacy_class,
            remaining_tokens=policy_input.budget.remaining_tokens,
            remaining_cost_usd=policy_input.budget.remaining_cost_usd,
            remaining_time_ms=policy_input.budget.remaining_time_ms,
            candidates=tuple(placements),
            expires_at=proposal.expires_at,
            fallback_profile=proposal.fallback_profile,
            mode=mode,
        )


@dataclass(frozen=True, slots=True)
class OperatorPlacementDecisionReceipt:
    run_id: str
    task_id: str
    candidate_set_ref: str
    candidate_set_digest: str
    resource_decision_id: str
    selected_manifest_id: str
    selected_worker: str
    selected_location: str
    selected_backend: str
    selected_operator_refs: tuple[str, ...]
    rejected_candidates: tuple[Mapping[str, Any], ...]
    execution_order: tuple[str, ...]
    maximum_concurrency: int
    route_mode: str
    degraded_reason: str
    placement_owner: str
    lease_owner: str
    created_at: str
    decision_id: str
    schema_version: str = OPERATOR_PLACEMENT_DECISION_SCHEMA

    def __post_init__(self) -> None:
        for name in ("selected_operator_refs", "execution_order"):
            object.__setattr__(
                self,
                name,
                tuple(str(item) for item in getattr(self, name) if str(item)),
            )
        object.__setattr__(
            self,
            "rejected_candidates",
            tuple(dict(item) for item in self.rejected_candidates),
        )
        if (
            self.placement_owner != "ResourceScheduler"
            or self.lease_owner != "WorkerPoolFoundationRuntime"
        ):
            raise OperatorPlacementError(
                "operator_placement_owner_invalid",
                "placement and lease ownership cannot move out of canonical runtimes",
            )
        if (
            self.maximum_concurrency < 1
            or set(self.selected_operator_refs) != set(self.execution_order)
            or len(self.execution_order) != len(set(self.execution_order))
        ):
            raise OperatorPlacementError(
                "operator_placement_plan_invalid",
                "operator placement requires a unique complete execution order",
            )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "candidate_set_ref": self.candidate_set_ref,
            "candidate_set_digest": self.candidate_set_digest,
            "resource_decision_id": self.resource_decision_id,
            "selected_manifest_id": self.selected_manifest_id,
            "selected_worker": self.selected_worker,
            "selected_location": self.selected_location,
            "selected_backend": self.selected_backend,
            "selected_operator_refs": list(self.selected_operator_refs),
            "rejected_candidates": [
                dict(item) for item in self.rejected_candidates
            ],
            "execution_order": list(self.execution_order),
            "maximum_concurrency": self.maximum_concurrency,
            "route_mode": self.route_mode,
            "degraded_reason": self.degraded_reason,
            "placement_owner": self.placement_owner,
            "lease_owner": self.lease_owner,
            "created_at": self.created_at,
            "decision_id": self.decision_id,
        }
        if include_digest:
            value["digest"] = self.digest
        return value


@dataclass(frozen=True, slots=True)
class OperatorLeaseExecutionContext:
    run_id: str
    task_id: str
    operator_ref: str
    operator_type: str
    layer_index: int
    placement_decision_id: str
    placement_location: str
    candidate_set_digest: str
    policy_input_digest: str
    graph_signature: str
    catalog_version: str
    catalog_digest: str
    mechanism_version: str
    permission_digest: str
    operator_idempotency_key: str
    attempt_id: str
    lease_id: str
    worker_id: str
    manifest_digest: str
    fence_epoch: int
    fence_token: str
    lease_acquired_at: str
    attempt_started_at: str
    call_started_at: str

    def to_dict(self, *, include_fence_token: bool = False) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "operator_ref": self.operator_ref,
            "operator_type": self.operator_type,
            "layer_index": self.layer_index,
            "placement_decision_id": self.placement_decision_id,
            "placement_location": self.placement_location,
            "candidate_set_digest": self.candidate_set_digest,
            "policy_input_digest": self.policy_input_digest,
            "graph_signature": self.graph_signature,
            "catalog_version": self.catalog_version,
            "catalog_digest": self.catalog_digest,
            "mechanism_version": self.mechanism_version,
            "permission_digest": self.permission_digest,
            "operator_idempotency_key": self.operator_idempotency_key,
            "attempt_id": self.attempt_id,
            "lease_id": self.lease_id,
            "worker_id": self.worker_id,
            "manifest_digest": self.manifest_digest,
            "fence_epoch": self.fence_epoch,
            "fence_token": self.fence_token if include_fence_token else "",
            "fence_token_present": bool(self.fence_token),
            "lease_acquired_at": self.lease_acquired_at,
            "attempt_started_at": self.attempt_started_at,
            "call_started_at": self.call_started_at,
        }


@dataclass(frozen=True, slots=True)
class OperatorCallResult:
    operator_ref: str
    call_ref: str
    artifact_refs: tuple[StableArtifactRef, ...]
    verification_refs: tuple[str, ...]
    actual_tokens: int
    actual_cost_usd: float
    actual_latency_ms: int
    summary: str
    call_finished_at: str
    metadata: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        if not self.operator_ref or not self.call_ref or not self.call_finished_at:
            raise OperatorPlacementError(
                "operator_call_receipt_incomplete",
                "operator call result requires operator, call and finish references",
            )
        if (
            self.actual_tokens < 0
            or self.actual_cost_usd < 0
            or self.actual_latency_ms < 0
        ):
            raise OperatorPlacementError(
                "operator_call_cost_invalid",
                "operator call cost, token and latency values cannot be negative",
            )
        object.__setattr__(
            self,
            "artifact_refs",
            tuple(sorted(self.artifact_refs, key=lambda item: item.ref_id)),
        )
        object.__setattr__(
            self,
            "verification_refs",
            tuple(sorted({str(item) for item in self.verification_refs if str(item)})),
        )
        object.__setattr__(self, "metadata", FrozenDict(self.metadata))


class OperatorCallPort(Protocol):
    """The existing tool/provider/operator owner invoked after the lease gate."""

    def execute(self, context: OperatorLeaseExecutionContext) -> OperatorCallResult: ...


@dataclass(frozen=True, slots=True)
class OperatorAttemptReceipt:
    run_id: str
    task_id: str
    operator_ref: str
    layer_index: int
    placement_decision_id: str
    candidate_set_digest: str
    permission_digest: str
    lease_id: str
    lease_acquired_at: str
    attempt_id: str
    attempt_started_at: str
    worker_id: str
    manifest_digest: str
    call_ref: str
    call_started_at: str
    call_finished_at: str
    completion_receipt_ref: str
    artifact_refs: tuple[str, ...]
    verification_refs: tuple[str, ...]
    actual_tokens: int
    actual_cost_usd: float
    actual_latency_ms: int
    outcome: str
    operator_idempotency_key: str
    recovery_plan_refs: tuple[str, ...] = ()
    physical_dispatch_receipt_ref: str = ""
    physical_dispatch_receipt_digest: str = ""
    replayed: bool = False
    schema_version: str = OPERATOR_ATTEMPT_RECEIPT_SCHEMA

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "operator_ref": self.operator_ref,
            "layer_index": self.layer_index,
            "placement_decision_id": self.placement_decision_id,
            "candidate_set_digest": self.candidate_set_digest,
            "permission_digest": self.permission_digest,
            "lease_id": self.lease_id,
            "lease_acquired_at": self.lease_acquired_at,
            "attempt_id": self.attempt_id,
            "attempt_started_at": self.attempt_started_at,
            "worker_id": self.worker_id,
            "manifest_digest": self.manifest_digest,
            "call_ref": self.call_ref,
            "call_started_at": self.call_started_at,
            "call_finished_at": self.call_finished_at,
            "completion_receipt_ref": self.completion_receipt_ref,
            "artifact_refs": list(self.artifact_refs),
            "verification_refs": list(self.verification_refs),
            "actual_tokens": self.actual_tokens,
            "actual_cost_usd": self.actual_cost_usd,
            "actual_latency_ms": self.actual_latency_ms,
            "outcome": self.outcome,
            "operator_idempotency_key": self.operator_idempotency_key,
            "recovery_plan_refs": list(self.recovery_plan_refs),
            "physical_dispatch_receipt_ref": self.physical_dispatch_receipt_ref,
            "physical_dispatch_receipt_digest": (
                self.physical_dispatch_receipt_digest
            ),
            "replayed": self.replayed,
        }
        if include_digest:
            value["digest"] = self.digest
        return value


@dataclass(frozen=True, slots=True)
class OperatorPlacementTaskResult:
    mode: str
    candidate_set: OperatorCandidateSet | None
    placement: OperatorPlacementDecisionReceipt
    attempt_receipts: tuple[OperatorAttemptReceipt, ...]
    layer_receipts: tuple[LayerExecutionReceipt, ...]
    adaptive_result: AdaptiveDepthResult | None
    recovery_plan_refs: tuple[str, ...]
    events: tuple[EventRecord, ...]
    baseline_profile: str
    schema_version: str = OPERATOR_TASK_RESULT_SCHEMA

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "candidate_set": (
                self.candidate_set.to_dict() if self.candidate_set else None
            ),
            "placement": self.placement.to_dict(),
            "attempt_receipts": [
                item.to_dict() for item in self.attempt_receipts
            ],
            "layer_receipts": [item.to_dict() for item in self.layer_receipts],
            "adaptive_result": (
                self.adaptive_result.to_dict() if self.adaptive_result else None
            ),
            "recovery_plan_refs": list(self.recovery_plan_refs),
            "events": [
                {
                    "event_id": item.event_id,
                    "event_type": item.event_type.value,
                    "run_id": item.run_id,
                    "task_id": item.task_id,
                    "payload": dict(item.payload),
                }
                for item in self.events
            ],
            "baseline_profile": self.baseline_profile,
        }
        if include_digest:
            value["digest"] = self.digest
        return value


class _OperatorExecutionSession:
    def __init__(
        self,
        runtime: "OperatorPlacementLeaseRuntime",
        *,
        task: TaskState,
        node: PlanNode | None,
        policy_input: PolicyInputSnapshot,
        proposal: OperatorSelectionProposal,
        catalog: OperatorCatalog,
        candidate_set: OperatorCandidateSet,
        placement: OperatorPlacementDecisionReceipt,
        resource_decision: ResourceDecision,
    ) -> None:
        self.runtime = runtime
        self.task = task
        self.node = node
        self.policy_input = policy_input
        self.proposal = proposal
        self.catalog = catalog
        self.candidate_set = candidate_set
        self.placement = placement
        self.resource_decision = resource_decision
        self.attempt_receipts: list[OperatorAttemptReceipt] = []
        self.layer_receipts: list[LayerExecutionReceipt] = []
        self.recovery_plan_refs: list[str] = []
        self.events: list[EventRecord] = []

    def execute_layer(
        self,
        *,
        proposal: OperatorSelectionProposal,
        layer: OperatorLayerProposal,
    ) -> LayerExecutionReceipt:
        if proposal.digest != self.proposal.digest:
            raise OperatorPlacementError(
                "operator_execution_proposal_mismatch",
                "execution layer belongs to another operator proposal",
            )
        expected = {
            f"{item.operator_id}@{item.version}" for item in layer.candidates
        }
        selected = set(self.placement.selected_operator_refs)
        if not expected or not expected.issubset(selected):
            raise OperatorPlacementError(
                "operator_execution_not_scheduled",
                "adaptive-depth requested an operator rejected by ResourceScheduler",
                metadata={
                    "requested": sorted(expected),
                    "scheduled": sorted(selected),
                },
            )
        receipts = [
            self._execute_candidate(
                self.candidate_set.candidate(operator_ref),
                layer_index=layer.layer_index,
            )
            for operator_ref in sorted(
                expected,
                key=lambda item: self.placement.execution_order.index(item),
            )
        ]
        owner_seed = canonical_digest([item.digest for item in receipts])
        layer_receipt = LayerExecutionReceipt(
            layer_index=layer.layer_index,
            operator_refs=tuple(item.operator_ref for item in receipts),
            owner_receipt_ref=f"operator-layer-receipt://{owner_seed[:24]}",
            decision_refs=tuple(
                dict.fromkeys(
                    (
                        self.placement.decision_id,
                        *(
                            item
                            for receipt in receipts
                            for item in (
                                receipt.placement_decision_id,
                                receipt.completion_receipt_ref,
                            )
                        ),
                    )
                )
            ),
            artifact_refs=tuple(
                StableArtifactRef(
                    ref_id=artifact_ref,
                    uri=f"artifact-ref://{artifact_ref}",
                    digest=canonical_digest(
                        (artifact_ref, receipt.completion_receipt_ref)
                    ),
                    media_type="application/octet-stream",
                )
                for receipt in receipts
                for artifact_ref in receipt.artifact_refs
            ),
            verification_refs=tuple(
                item for receipt in receipts for item in receipt.verification_refs
            ),
            actual_tokens=sum(item.actual_tokens for item in receipts),
            actual_cost_usd=round(
                sum(item.actual_cost_usd for item in receipts),
                12,
            ),
            actual_latency_ms=sum(item.actual_latency_ms for item in receipts),
        )
        self.layer_receipts.append(layer_receipt)
        return layer_receipt

    def _execute_candidate(
        self,
        candidate: PlacementOperatorCandidate,
        *,
        layer_index: int,
    ) -> OperatorAttemptReceipt:
        idempotency_key = canonical_digest(
            (
                self.task.run_id,
                self.task.task_id,
                self.candidate_set.proposal_digest,
                candidate.operator_ref,
                layer_index,
            )
        )
        prior = self.runtime._prior_attempt(
            self.task.task_id,
            operator_idempotency_key=idempotency_key,
        )
        if prior is not None:
            replayed = OperatorAttemptReceipt(
                **{
                    **prior.to_dict(include_digest=False),
                    "artifact_refs": prior.artifact_refs,
                    "verification_refs": prior.verification_refs,
                    "recovery_plan_refs": prior.recovery_plan_refs,
                    "replayed": True,
                }
            )
            self.attempt_receipts.append(replayed)
            return replayed

        recovery_refs: list[str] = []
        avoided_workers: list[str] = []
        decision = self.resource_decision
        for recovery_index in range(
            self.runtime.config.maximum_recovery_attempts + 1
        ):
            acquisition = None
            call_entered = False
            permission_digest = self.runtime._permission_digest(
                candidate,
                self.candidate_set,
            )
            try:
                acquisition = self.runtime._acquire_exact_lease(
                    task=self.task,
                    candidate=candidate,
                    candidate_set=self.candidate_set,
                    placement=self.placement,
                    decision=decision,
                    layer_index=layer_index,
                    permission_digest=permission_digest,
                    idempotency_key=idempotency_key,
                    excluded_worker_ids=tuple(avoided_workers),
                )
                attempt = self.runtime.pool.leases.start_attempt(
                    acquisition.lease.lease_id,
                    worker_id=acquisition.worker.worker_id,
                    fence_token=acquisition.lease.fence_token,
                    fence_epoch=acquisition.lease.fence_epoch,
                    backend_dispatch_id=(
                        f"operator-call:{candidate.operator_ref}:{acquisition.attempt.attempt_id}"
                    ),
                )
                context = OperatorLeaseExecutionContext(
                    run_id=self.task.run_id,
                    task_id=self.task.task_id,
                    operator_ref=candidate.operator_ref,
                    operator_type=candidate.operator_type,
                    layer_index=layer_index,
                    placement_decision_id=self.placement.decision_id,
                    placement_location=str(
                        acquisition.worker.location.value
                    ),
                    candidate_set_digest=self.candidate_set.digest,
                    policy_input_digest=self.policy_input.digest,
                    graph_signature=self.policy_input.graph.signature,
                    catalog_version=self.candidate_set.catalog_version,
                    catalog_digest=self.candidate_set.catalog_digest,
                    mechanism_version=self.runtime.config.mechanism_version,
                    permission_digest=permission_digest,
                    operator_idempotency_key=idempotency_key,
                    attempt_id=attempt.attempt_id,
                    lease_id=acquisition.lease.lease_id,
                    worker_id=acquisition.worker.worker_id,
                    manifest_digest=acquisition.manifest.digest,
                    fence_epoch=acquisition.lease.fence_epoch,
                    fence_token=acquisition.lease.fence_token,
                    lease_acquired_at=acquisition.lease.acquired_at,
                    attempt_started_at=attempt.started_at,
                    call_started_at="",
                )
                prepare = getattr(self.runtime.call_port, "prepare", None)
                if callable(prepare):
                    prepare(context)
                self.runtime._assert_execution_fresh(
                    context=context,
                    task=self.task,
                    candidate=candidate,
                    candidate_set=self.candidate_set,
                    catalog=self.catalog,
                    permission_digest=permission_digest,
                )
                context = replace(
                    context,
                    call_started_at=self.runtime.clock(),
                )
                call_entered = True
                call_result = self.runtime.call_port.execute(context)
                if call_result.operator_ref != candidate.operator_ref:
                    raise OperatorPlacementError(
                        "operator_call_identity_mismatch",
                        "operator call owner returned a receipt for another operator",
                    )
                completed = self.runtime.pool.leases.complete(
                    acquisition.lease.lease_id,
                    worker_id=acquisition.worker.worker_id,
                    fence_token=acquisition.lease.fence_token,
                    fence_epoch=acquisition.lease.fence_epoch,
                    outcome=ExecutionOutcome.SUCCEEDED,
                    summary=call_result.summary,
                    artifact_refs=tuple(
                        item.ref_id for item in call_result.artifact_refs
                    ),
                    event_refs=(self.placement.decision_id, call_result.call_ref),
                    backend_receipt_ref=call_result.call_ref,
                    metadata={
                        "schema": OPERATOR_ATTEMPT_RECEIPT_SCHEMA,
                        "operator_ref": candidate.operator_ref,
                        "operator_idempotency_key": idempotency_key,
                        "layer_index": layer_index,
                        "placement_decision_id": self.placement.decision_id,
                        "candidate_set_digest": self.candidate_set.digest,
                        "permission_digest": permission_digest,
                        "call_started_at": context.call_started_at,
                        "call_finished_at": call_result.call_finished_at,
                        "verification_refs": list(
                            call_result.verification_refs
                        ),
                        "actual_tokens": call_result.actual_tokens,
                        "actual_cost_usd": call_result.actual_cost_usd,
                        "actual_latency_ms": call_result.actual_latency_ms,
                        "mechanism_version": self.runtime.config.mechanism_version,
                        "physical_dispatch_receipt_ref": str(
                            call_result.metadata.get(
                                "physical_dispatch_receipt_ref"
                            )
                            or ""
                        ),
                        "physical_dispatch_receipt_digest": str(
                            call_result.metadata.get(
                                "physical_dispatch_receipt_digest"
                            )
                            or ""
                        ),
                        "physical_attempt_id": str(
                            call_result.metadata.get("physical_attempt_id")
                            or attempt.attempt_id
                        ),
                        "physical_location": str(
                            call_result.metadata.get("physical_location")
                            or context.placement_location
                        ),
                        "provider_request_id": str(
                            call_result.metadata.get("provider_request_id")
                            or ""
                        ),
                    },
                )
                execution_decision = build_operator_execution_decision(
                    task=self.task,
                    layer_index=layer_index,
                    candidates=tuple(
                        item
                        for item in self.proposal.candidates
                        if f"{item.operator_id}@{item.version}"
                        == candidate.operator_ref
                    ),
                    owner_receipt_ref=completed.receipt_id,
                    actual_tokens=call_result.actual_tokens,
                    actual_cost_usd=call_result.actual_cost_usd,
                    actual_latency_ms=call_result.actual_latency_ms,
                    verification_refs=call_result.verification_refs,
                    created_at=call_result.call_finished_at,
                )
                self.task.decisions.append(execution_decision)
                receipt = OperatorAttemptReceipt(
                    run_id=self.task.run_id,
                    task_id=self.task.task_id,
                    operator_ref=candidate.operator_ref,
                    layer_index=layer_index,
                    placement_decision_id=self.placement.decision_id,
                    candidate_set_digest=self.candidate_set.digest,
                    permission_digest=permission_digest,
                    lease_id=acquisition.lease.lease_id,
                    lease_acquired_at=acquisition.lease.acquired_at,
                    attempt_id=attempt.attempt_id,
                    attempt_started_at=attempt.started_at,
                    worker_id=acquisition.worker.worker_id,
                    manifest_digest=acquisition.manifest.digest,
                    call_ref=call_result.call_ref,
                    call_started_at=context.call_started_at,
                    call_finished_at=call_result.call_finished_at,
                    completion_receipt_ref=completed.receipt_id,
                    artifact_refs=tuple(
                        item.ref_id for item in call_result.artifact_refs
                    ),
                    verification_refs=call_result.verification_refs,
                    actual_tokens=call_result.actual_tokens,
                    actual_cost_usd=call_result.actual_cost_usd,
                    actual_latency_ms=call_result.actual_latency_ms,
                    outcome=completed.outcome.value,
                    operator_idempotency_key=idempotency_key,
                    recovery_plan_refs=tuple(recovery_refs),
                    physical_dispatch_receipt_ref=str(
                        call_result.metadata.get(
                            "physical_dispatch_receipt_ref"
                        )
                        or ""
                    ),
                    physical_dispatch_receipt_digest=str(
                        call_result.metadata.get(
                            "physical_dispatch_receipt_digest"
                        )
                        or ""
                    ),
                )
                self.attempt_receipts.append(receipt)
                event = self.runtime._attempt_event(receipt)
                self.runtime.admit_event(event)
                self.events.append(event)
                return receipt
            except Exception as exc:  # noqa: BLE001 - recovery is owner-routed.
                recovery_exc: Exception = exc
                retry_safe_physical_failure = (
                    isinstance(exc, OperatorPlacementError)
                    and exc.retryable
                    and exc.metadata.get("side_effect_started") is False
                    and bool(exc.metadata.get("physical_attempt_id"))
                )
                if call_entered and not retry_safe_physical_failure:
                    recovery_exc = OperatorPlacementError(
                        "operator_call_outcome_unknown",
                        "operator call entered an external side-effect boundary "
                        "without a canonical successful completion; automatic "
                        "retry is unsafe",
                        metadata={
                            "original_error": getattr(
                                exc,
                                "code",
                                type(exc).__name__,
                            ),
                            "operator_idempotency_key": idempotency_key,
                        },
                    )
                failed_worker = ""
                acquisition_value = acquisition
                if acquisition_value is not None:
                    failed_worker = str(
                        getattr(acquisition_value.worker, "worker_id", "")
                    )
                    self.runtime._close_failed_attempt(
                        acquisition_value,
                        summary=str(recovery_exc),
                    )
                recovery = self.runtime._plan_recovery(
                    task=self.task,
                    node=self.node,
                    exc=recovery_exc,
                    failed_worker=failed_worker,
                    cause_ref=self.placement.decision_id,
                )
                recovery_refs.append(recovery.plan_id)
                self.recovery_plan_refs.append(recovery.plan_id)
                if failed_worker:
                    avoided_workers.append(failed_worker)
                if (
                    recovery_index
                    >= self.runtime.config.maximum_recovery_attempts
                    or not recovery.can_continue
                    or isinstance(recovery_exc, OperatorPlacementError)
                    and not recovery_exc.retryable
                ):
                    raise recovery_exc from (
                        exc if recovery_exc is not exc else None
                    )
                decision = self.runtime.scheduler.decide(
                    self.task,
                    node=self.node,
                    avoid_workers=tuple(avoided_workers),
                    operator_input=self.candidate_set,
                )
                self.runtime.scheduler.attach_decision_to_state(
                    self.task,
                    decision,
                    node=self.node,
                )
                rerouted_placement = self.runtime._placement_receipt(
                    candidate_set=self.candidate_set,
                    decision=decision,
                )
                if candidate.operator_ref not in set(
                    rerouted_placement.selected_operator_refs
                ):
                    raise OperatorPlacementError(
                        "operator_recovery_candidate_unavailable",
                        "ResourceScheduler could not place the failed operator "
                        "during bounded recovery",
                    )
                self.placement = rerouted_placement
                reroute_event = self.runtime._placement_event(
                    rerouted_placement
                )
                self.runtime.admit_event(reroute_event)
                self.events.append(reroute_event)
        raise OperatorPlacementError(
            "operator_execution_recovery_exhausted",
            "operator execution exhausted the bounded recovery budget",
        )


class OperatorPlacementLeaseRuntime:
    """Zyra-owned operator→placement→lease→execution composition root.

    MaAS data ends at ``OperatorCandidateSet``. ResourceScheduler chooses the
    executable operator set and physical placement, WorkerPoolFoundationRuntime
    owns fenced leases/attempts, and the injected call port remains the actual
    tool/provider/operator owner.
    """

    def __init__(
        self,
        *,
        config: OperatorPlacementLeaseConfig,
        scheduler: ResourceScheduler,
        pool: WorkerPoolFoundationRuntime,
        call_port: OperatorCallPort,
        recovery_planner: RecoveryPlanner | None = None,
        permission_queue: Any | None = None,
        catalog_provider: CatalogProvider | None = None,
        admit_event: EventSink | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.scheduler = scheduler
        self.pool = pool
        self.call_port = call_port
        self.recovery_planner = recovery_planner or RecoveryPlanner(scheduler)
        self.permission_queue = permission_queue
        self.catalog_provider = catalog_provider
        self.admit_event = admit_event or (lambda event: None)
        self.clock = clock or now_iso

    @classmethod
    def from_repository(
        cls,
        repository_root: str | Path,
        **kwargs: Any,
    ) -> "OperatorPlacementLeaseRuntime":
        config = OperatorPlacementLeaseConfig.load(
            Path(repository_root)
            / "config"
            / "phase2"
            / "operator-placement-lease.json"
        )
        return cls(config=config, **kwargs)

    def execute_task(
        self,
        *,
        task: TaskState,
        policy_input: PolicyInputSnapshot,
        selector_result: Any,
        catalog: OperatorCatalog,
        adaptive_depth: AdaptiveDepthRuntime | None,
        eligibility_port: EligibilityCapturePort | None,
        node: PlanNode | None = None,
        early_exit_enabled: bool = True,
    ) -> OperatorPlacementTaskResult:
        if (
            policy_input.run_id != task.run_id
            or policy_input.task_id != task.task_id
        ):
            raise OperatorPlacementError(
                "operator_task_snapshot_identity_mismatch",
                "policy input and canonical task identities must match",
            )
        proposal = getattr(selector_result, "proposal", None)
        scheduler_input = getattr(selector_result, "scheduler_input", None)
        if proposal is None or scheduler_input is None:
            return self._execute_baseline(
                task=task,
                policy_input=policy_input,
                node=node,
                reason=str(
                    getattr(selector_result, "degraded_reason", "")
                    or "maas_selector_baseline"
                ),
            )
        if adaptive_depth is None or eligibility_port is None:
            raise OperatorPlacementError(
                "operator_adaptive_depth_owner_missing",
                "candidate execution requires the verifier-gated adaptive-depth owner",
            )
        readiness = getattr(selector_result, "readiness", None)
        readiness_digest = str(
            getattr(readiness, "report_digest", "") or ""
        )
        mode = str(getattr(selector_result, "mode", "") or "")
        candidate_set = OperatorCandidateSet.build(
            policy_input=policy_input,
            proposal=proposal,
            catalog=catalog,
            integration_mechanism_version=self.config.mechanism_version,
            mechanism_receipt_ref=readiness_digest,
            mode=mode,
        )
        decision = self.scheduler.decide(
            task,
            node=node,
            operator_input=candidate_set,
        )
        self.scheduler.attach_decision_to_state(task, decision, node=node)
        placement = self._placement_receipt(
            candidate_set=candidate_set,
            decision=decision,
        )
        placement_event = self._placement_event(placement)
        self.admit_event(placement_event)
        selected = set(placement.selected_operator_refs)
        execution_layers = tuple(
            OperatorLayerProposal(
                layer_index=layer.layer_index,
                candidates=tuple(
                    item
                    for item in layer.candidates
                    if f"{item.operator_id}@{item.version}" in selected
                ),
                reason=(
                    f"{layer.reason}; projected by ResourceScheduler "
                    f"decision {placement.decision_id}"
                ),
            )
            for layer in proposal.layers
            if any(
                f"{item.operator_id}@{item.version}" in selected
                for item in layer.candidates
            )
        )
        if not execution_layers:
            if candidate_set.allow_explicit_baseline:
                return self._execute_baseline(
                    task=task,
                    policy_input=policy_input,
                    node=node,
                    reason=placement.degraded_reason
                    or "operator_candidates_rejected_by_scheduler",
                    prior_event=placement_event,
                )
            raise OperatorPlacementError(
                "operator_scheduler_rejected_all",
                "ResourceScheduler rejected every operator candidate",
            )
        session = _OperatorExecutionSession(
            self,
            task=task,
            node=node,
            policy_input=policy_input,
            proposal=proposal,
            catalog=catalog,
            candidate_set=candidate_set,
            placement=placement,
            resource_decision=decision,
        )
        adaptive_result = adaptive_depth.execute(
            proposal=proposal,
            execution_layers=execution_layers,
            execution_port=session,
            eligibility_port=eligibility_port,
            early_exit_enabled=early_exit_enabled,
        )
        events = (
            placement_event,
            *session.events,
            *adaptive_result.events,
        )
        return OperatorPlacementTaskResult(
            mode=mode,
            candidate_set=candidate_set,
            placement=session.placement,
            attempt_receipts=tuple(session.attempt_receipts),
            layer_receipts=tuple(session.layer_receipts),
            adaptive_result=adaptive_result,
            recovery_plan_refs=tuple(session.recovery_plan_refs),
            events=events,
            baseline_profile="",
        )

    def _execute_baseline(
        self,
        *,
        task: TaskState,
        policy_input: PolicyInputSnapshot,
        node: PlanNode | None,
        reason: str,
        prior_event: EventRecord | None = None,
    ) -> OperatorPlacementTaskResult:
        decision = self.scheduler.decide(task, node=node)
        self.scheduler.attach_decision_to_state(task, decision, node=node)
        physical_workers = self.pool.store.list_workers()
        if not physical_workers:
            raise OperatorPlacementError(
                "baseline_physical_worker_unavailable",
                "baseline scheduler has no registered physical worker",
            )
        physical_worker = next(
            (
                item
                for item in physical_workers
                if item.worker_id == decision.selected_manifest_id
            ),
            physical_workers[0],
        )
        physical_manifest = self.pool.store.latest_manifest(
            physical_worker.worker_id
        )
        if physical_manifest is None:
            raise OperatorPlacementError(
                "baseline_physical_manifest_unavailable",
                "baseline physical worker has no capability manifest",
            )
        decision.metadata["operator_placement"] = {
            "physical_worker_id": physical_worker.worker_id,
        }
        baseline_ref = f"baseline:{physical_worker.worker_id}@1"
        placement = OperatorPlacementDecisionReceipt(
            run_id=task.run_id,
            task_id=task.task_id,
            candidate_set_ref="",
            candidate_set_digest="",
            resource_decision_id=decision.decision_id,
            selected_manifest_id=decision.selected_manifest_id,
            selected_worker=decision.selected_worker,
            selected_location=decision.selected_location.value,
            selected_backend=decision.selected_backend.value,
            selected_operator_refs=(baseline_ref,),
            rejected_candidates=(),
            execution_order=(baseline_ref,),
            maximum_concurrency=1,
            route_mode="baseline",
            degraded_reason=reason,
            placement_owner="ResourceScheduler",
            lease_owner="WorkerPoolFoundationRuntime",
            created_at=decision.created_at,
            decision_id=f"operator-placement-{decision.decision_id}",
        )
        event = self._placement_event(placement)
        self.admit_event(event)
        catalog, proposal, candidate_set = self._baseline_material(
            task=task,
            policy_input=policy_input,
            placement=placement,
            physical_manifest=physical_manifest,
        )
        session = _OperatorExecutionSession(
            self,
            task=task,
            node=node,
            policy_input=policy_input,
            proposal=proposal,
            catalog=catalog,
            candidate_set=candidate_set,
            placement=placement,
            resource_decision=decision,
        )
        layer = proposal.layers[0]
        session.execute_layer(proposal=proposal, layer=layer)
        return OperatorPlacementTaskResult(
            mode="baseline",
            candidate_set=None,
            placement=session.placement,
            attempt_receipts=tuple(session.attempt_receipts),
            layer_receipts=tuple(session.layer_receipts),
            adaptive_result=None,
            recovery_plan_refs=tuple(session.recovery_plan_refs),
            events=tuple(
                item
                for item in (
                    prior_event,
                    event,
                    *session.events,
                )
                if item is not None
            ),
            baseline_profile=self.config.fallback_profile,
        )

    def _baseline_material(
        self,
        *,
        task: TaskState,
        policy_input: PolicyInputSnapshot,
        placement: OperatorPlacementDecisionReceipt,
        physical_manifest: Any,
    ) -> tuple[OperatorCatalog, OperatorSelectionProposal, OperatorCandidateSet]:
        operator_id = f"baseline:{physical_manifest.worker_id}"
        profile = OperatorProfile(
            operator_id=operator_id,
            operator_type=OperatorType.WORKER,
            version="1",
            display_name=f"Phase 1 baseline {physical_manifest.worker_id}",
            description="Explicit Phase 1 ResourceScheduler baseline operator",
            capabilities=physical_manifest.capabilities,
            input_contract=("task", "lease_candidate"),
            output_contract=("worker_result", "artifact_refs"),
            required_permissions=(),
            allowed_locations=(physical_manifest.location.value,),
            allowed_privacy_classes=(
                "internal",
                "masked",
                "project",
                "public",
                "sensitive",
            ),
            estimated_tokens=1,
            estimated_cost_usd=0.0,
            estimated_latency_ms=1,
            health_status="healthy",
            available_capacity=max(
                1, int(physical_manifest.resource_capacity.process_slots or 1)
            ),
            verifier_contracts=("worker_result_verifier",),
            minimum_evidence_contract=("worker_lease", "worker_result"),
            cold_start=False,
            confidence=1.0,
            outcome_count=1,
            source_registry="ResourceSchedulerBaseline",
            source_registry_version="phase1",
            source_ref=physical_manifest.worker_id,
            metadata=FrozenDict({"baseline_profile": self.config.fallback_profile}),
        )
        catalog = OperatorCatalog(
            entries=(profile,),
            source_versions=FrozenDict(
                {"resource_scheduler_baseline": physical_manifest.digest}
            ),
            generation=1,
            built_at=self.clock(),
        )
        score_components = OperatorScoreComponents(
            capability_obligation_coverage=10_000,
            permission=10_000,
            health=10_000,
            cost=10_000,
            latency=10_000,
            verifier_necessity=10_000,
            semantic_match=10_000,
            confidence=10_000,
        )
        candidate = OperatorCandidate(
            operator_id=profile.operator_id,
            operator_type=profile.operator_type.value,
            version=profile.version,
            profile_digest=profile.digest,
            score=10_000.0,
            score_components=score_components,
            reasons=("explicit deterministic Phase 1 baseline",),
            encoding_digest=canonical_digest(("baseline", policy_input.digest)),
            cold_start=False,
            confidence=1.0,
            estimated_tokens=profile.estimated_tokens,
            estimated_cost_usd=profile.estimated_cost_usd,
            estimated_latency_ms=profile.estimated_latency_ms,
        )
        created_at = self.clock()
        header = ContractHeader(
            contract_id=f"baseline-proposal-{placement.resource_decision_id}",
            created_at=created_at,
            source_event_id=f"baseline-event-{placement.resource_decision_id}",
            correlation_id=f"baseline:{task.run_id}",
            causation_id=placement.resource_decision_id,
            mechanism_id="phase1_resource_scheduler_baseline",
            mechanism_version="phase1",
            input_version="v1",
            idempotency_key=(
                f"baseline-proposal:{task.run_id}:{task.task_id}:"
                f"{placement.resource_decision_id}"
            ),
            configuration_digest=self.config.digest,
        )
        proposal = OperatorSelectionProposal(
            header=header,
            proposal_id=header.contract_id,
            input_snapshot_digest=policy_input.digest,
            requirement_revision=policy_input.requirement_revision,
            committed_graph_id=policy_input.graph.graph_id,
            committed_graph_revision=policy_input.graph.revision,
            committed_graph_signature=policy_input.graph.signature,
            committed_graph_commit_id=policy_input.graph.commit_id,
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.digest,
            context_digest=canonical_digest(("baseline", policy_input.digest)),
            encoder_profile="deterministic_baseline",
            encoder_observation_digest=canonical_digest(
                ("baseline-observation", policy_input.digest)
            ),
            layers=(
                OperatorLayerProposal(
                    layer_index=1,
                    candidates=(candidate,),
                    reason="explicit Phase 1 scheduler baseline",
                ),
            ),
            alternatives=(),
            filter_verdicts=(),
            expected_breadth=1,
            expected_depth=1,
            reasons=("selector unavailable or disabled; baseline used explicitly",),
            expires_at=(
                _utc(created_at) + timedelta(seconds=self.config.lease_ttl_seconds)
            ).isoformat().replace("+00:00", "Z"),
            fallback_profile=self.config.fallback_profile,
        )
        candidate_set = OperatorCandidateSet.build(
            policy_input=policy_input,
            proposal=proposal,
            catalog=catalog,
            integration_mechanism_version=self.config.mechanism_version,
            mechanism_receipt_ref=canonical_digest(
                (
                    "baseline",
                    self.config.fallback_profile,
                    placement.resource_decision_id,
                )
            ),
            mode="validation",
        )
        return catalog, proposal, candidate_set

    def _placement_receipt(
        self,
        *,
        candidate_set: OperatorCandidateSet,
        decision: ResourceDecision,
    ) -> OperatorPlacementDecisionReceipt:
        plan = decision.metadata.get("operator_placement")
        if not isinstance(plan, Mapping):
            raise OperatorPlacementError(
                "operator_scheduler_plan_missing",
                "ResourceScheduler did not return an operator placement plan",
            )
        if (
            decision.run_id != candidate_set.run_id
            or decision.task_id != candidate_set.task_id
        ):
            raise OperatorPlacementError(
                "operator_scheduler_identity_mismatch",
                "ResourceScheduler decision belongs to another run or task",
            )
        selected = tuple(str(item) for item in plan.get("selected_operator_refs") or ())
        order = tuple(str(item) for item in plan.get("execution_order") or ())
        if set(selected) != set(order):
            raise OperatorPlacementError(
                "operator_scheduler_order_invalid",
                "scheduler execution order does not cover the selected operator set",
            )
        maximum_concurrency = int(plan.get("maximum_concurrency") or 1)
        if maximum_concurrency > self.config.maximum_layer_concurrency:
            raise OperatorPlacementError(
                "operator_scheduler_concurrency_exceeded",
                "ResourceScheduler plan exceeds the integration concurrency fence",
            )
        return OperatorPlacementDecisionReceipt(
            run_id=decision.run_id,
            task_id=decision.task_id,
            candidate_set_ref=candidate_set.proposal_id,
            candidate_set_digest=candidate_set.digest,
            resource_decision_id=decision.decision_id,
            selected_manifest_id=decision.selected_manifest_id,
            selected_worker=decision.selected_worker,
            selected_location=decision.selected_location.value,
            selected_backend=decision.selected_backend.value,
            selected_operator_refs=selected,
            rejected_candidates=tuple(
                dict(item) for item in plan.get("rejected_candidates") or ()
            ),
            execution_order=order,
            maximum_concurrency=maximum_concurrency,
            route_mode=str(plan.get("route_mode") or "operator_constrained"),
            degraded_reason=str(plan.get("degraded_reason") or ""),
            placement_owner=str(
                plan.get("placement_owner") or "ResourceScheduler"
            ),
            lease_owner=str(
                plan.get("lease_owner") or "WorkerPoolFoundationRuntime"
            ),
            created_at=decision.created_at,
            decision_id=f"operator-placement-{decision.decision_id}",
        )

    def _permission_digest(
        self,
        candidate: PlacementOperatorCandidate,
        candidate_set: OperatorCandidateSet,
    ) -> str:
        missing = sorted(
            set(candidate.required_permissions)
            - set(candidate_set.allowed_permissions)
        )
        pending: list[dict[str, Any]] = []
        if self.permission_queue is not None:
            try:
                pending = [
                    {
                        "request_id": str(getattr(item, "request_id", "")),
                        "session_id": str(getattr(item, "session_id", "")),
                        "run_id": str(getattr(item, "run_id", "")),
                        "task_id": str(getattr(item, "task_id", "")),
                        "revision": int(getattr(item, "revision", 0)),
                    }
                    for item in self.permission_queue.pending()
                    if str(getattr(item, "run_id", ""))
                    == candidate_set.run_id
                    and str(getattr(item, "task_id", ""))
                    == candidate_set.task_id
                ]
            except Exception as exc:  # noqa: BLE001 - uncertainty denies lease.
                raise OperatorPlacementError(
                    "operator_permission_owner_unavailable",
                    "canonical permission owner could not be read",
                ) from exc
        if missing or pending:
            raise OperatorPlacementError(
                "operator_permission_not_settled",
                "operator permission is missing or has pending decisions",
                metadata={"missing": missing, "pending": pending},
            )
        return canonical_digest(
            {
                "owner": "ToolPermissionRuntime",
                "operator_ref": candidate.operator_ref,
                "required_permissions": list(candidate.required_permissions),
                "allowed_permissions": list(candidate_set.allowed_permissions),
                "pending": pending,
            }
        )

    def _acquire_exact_lease(
        self,
        *,
        task: TaskState,
        candidate: PlacementOperatorCandidate,
        candidate_set: OperatorCandidateSet,
        placement: OperatorPlacementDecisionReceipt,
        decision: ResourceDecision,
        layer_index: int,
        permission_digest: str,
        idempotency_key: str,
        excluded_worker_ids: tuple[str, ...],
    ) -> Any:
        physical_worker_id = str(
            (
                decision.metadata.get("operator_placement")
                if isinstance(decision.metadata.get("operator_placement"), Mapping)
                else {}
            ).get("physical_worker_id")
            or decision.selected_manifest_id
        )
        workers = tuple(item.worker_id for item in self.pool.store.list_workers())
        if physical_worker_id not in workers:
            raise OperatorPlacementError(
                "operator_placement_worker_unregistered",
                "ResourceScheduler selected no matching physical worker",
                retryable=True,
                metadata={
                    "selected_worker": physical_worker_id,
                    "registered_workers": list(workers),
                },
            )
        excluded = tuple(
            sorted(
                {
                    *excluded_worker_ids,
                    *(item for item in workers if item != physical_worker_id),
                }
            )
        )
        requirement = _capability_requirement(candidate)
        lease_idempotency_key = canonical_digest(
            (
                idempotency_key,
                decision.decision_id,
                physical_worker_id,
                tuple(sorted(excluded_worker_ids)),
            )
        )
        return self.pool.acquire_task(
            task_id=task.task_id,
            run_id=task.run_id,
            owner_session_id=f"operator-session:{task.run_id}",
            requirement=requirement,
            preferred_worker_ids=(physical_worker_id,),
            excluded_worker_ids=excluded,
            ttl_seconds=self.config.lease_ttl_seconds,
            idempotency_key=f"operator-lease:{lease_idempotency_key}",
            metadata={
                "operator_ref": candidate.operator_ref,
                "operator_type": candidate.operator_type,
                "layer_index": layer_index,
                "placement_decision_id": placement.decision_id,
                "resource_decision_id": placement.resource_decision_id,
                "placement": placement.selected_location,
                "policy_input_digest": candidate_set.input_snapshot_digest,
                "graph_signature": candidate_set.committed_graph_signature,
                "graph_revision": candidate_set.committed_graph_revision,
                "catalog_version": candidate_set.catalog_version,
                "catalog_digest": candidate_set.catalog_digest,
                "profile_digest": candidate.profile_digest,
                "candidate_set_digest": candidate_set.digest,
                "selector_mechanism_version": (
                    candidate_set.selector_mechanism_version
                ),
                "integration_mechanism_version": (
                    candidate_set.integration_mechanism_version
                ),
                "mechanism_receipt_ref": candidate_set.mechanism_receipt_ref,
                "permission_digest": permission_digest,
                "operator_idempotency_key": idempotency_key,
                "execution_before_lease_forbidden": True,
            },
        )

    def _assert_execution_fresh(
        self,
        *,
        context: OperatorLeaseExecutionContext,
        task: TaskState,
        candidate: PlacementOperatorCandidate,
        candidate_set: OperatorCandidateSet,
        catalog: OperatorCatalog,
        permission_digest: str,
    ) -> None:
        if self.config.require_execution_fence:
            lease = self.pool.leases.assert_fence(
                context.lease_id,
                worker_id=context.worker_id,
                fence_token=context.fence_token,
                fence_epoch=context.fence_epoch,
                operation="execute_operator_after_lease",
            )
        else:
            raise OperatorPlacementError(
                "operator_execution_gate_disabled",
                "operator execution cannot run with the lease gate disabled",
            )
        attempt = self.pool.store.require_attempt(context.attempt_id)
        manifest = self.pool.store.latest_manifest(context.worker_id)
        if manifest is None:
            raise OperatorPlacementError(
                "operator_execution_manifest_missing",
                "leased worker manifest disappeared before execution",
                retryable=True,
            )
        if (
            lease.run_id != task.run_id
            or lease.task_id != task.task_id
            or lease.attempt_id != context.attempt_id
            or attempt.run_id != task.run_id
            or attempt.task_id != task.task_id
            or lease.metadata.get("operator_ref") != candidate.operator_ref
            or lease.metadata.get("candidate_set_digest") != candidate_set.digest
            or lease.metadata.get("permission_digest") != permission_digest
            or lease.metadata.get("placement_decision_id")
            != context.placement_decision_id
            or lease.metadata.get("policy_input_digest")
            != context.policy_input_digest
            or lease.metadata.get("catalog_version") != context.catalog_version
            or lease.metadata.get("catalog_digest") != context.catalog_digest
            or lease.metadata.get("integration_mechanism_version")
            != self.config.mechanism_version
            or attempt.started_at != context.attempt_started_at
            or manifest.digest != context.manifest_digest
        ):
            raise OperatorPlacementError(
                "operator_lease_binding_stale",
                "operator lease no longer matches placement, permission or mechanism state",
                retryable=True,
            )
        if task.metadata.get("requirement_revision") not in {
            None,
            "",
            candidate_set.requirement_revision,
        }:
            raise OperatorPlacementError(
                "operator_requirement_revision_stale",
                "task requirement revision changed before operator execution",
            )
        if _utc(self.clock()) >= _utc(candidate_set.expires_at):
            raise OperatorPlacementError(
                "operator_candidate_set_expired",
                "operator candidate set expired before execution",
                retryable=True,
            )
        if self.config.require_permission_recheck:
            current_permission_digest = self._permission_digest(
                candidate,
                candidate_set,
            )
            if current_permission_digest != permission_digest:
                raise OperatorPlacementError(
                    "operator_permission_changed",
                    "permission owner changed after lease acquisition",
                )
        current_catalog = (
            catalog
            if (
                candidate.operator_id.startswith("baseline:")
                and candidate_set.fallback_profile == self.config.fallback_profile
            )
            else (
                self.catalog_provider()
                if self.catalog_provider is not None
                else catalog
            )
        )
        if self.config.require_catalog_recheck:
            if (
                current_catalog.catalog_version != candidate_set.catalog_version
                or current_catalog.digest != candidate_set.catalog_digest
            ):
                raise OperatorPlacementError(
                    "operator_catalog_stale",
                    "operator catalog changed after lease acquisition",
                    retryable=True,
                )
            profile = current_catalog.get(candidate.operator_id)
            if (
                profile is None
                or profile.version != candidate.version
                or profile.digest != candidate.profile_digest
                or not profile.enabled
                or profile.revoked
            ):
                raise OperatorPlacementError(
                    "operator_revoked_before_execution",
                    "operator was revoked or replaced after lease acquisition",
                )
        supported, failures = manifest.supports(
            _capability_requirement(candidate)
        )
        if not supported:
            raise OperatorPlacementError(
                "operator_capacity_or_capability_changed",
                "leased worker no longer satisfies the operator requirement",
                retryable=True,
                metadata={"failures": list(failures)},
            )

    def _prior_attempt(
        self,
        task_id: str,
        *,
        operator_idempotency_key: str,
    ) -> OperatorAttemptReceipt | None:
        for receipt in self.pool.store.receipts_for_task(task_id):
            metadata = dict(receipt.metadata)
            if (
                metadata.get("operator_idempotency_key")
                != operator_idempotency_key
                or metadata.get("schema") != OPERATOR_ATTEMPT_RECEIPT_SCHEMA
            ):
                continue
            lease = self.pool.store.require_lease(receipt.lease_id)
            return OperatorAttemptReceipt(
                run_id=receipt.run_id,
                task_id=receipt.task_id,
                operator_ref=str(metadata.get("operator_ref") or ""),
                layer_index=int(metadata.get("layer_index") or 1),
                placement_decision_id=str(
                    metadata.get("placement_decision_id") or ""
                ),
                candidate_set_digest=str(
                    metadata.get("candidate_set_digest") or ""
                ),
                permission_digest=str(metadata.get("permission_digest") or ""),
                lease_id=receipt.lease_id,
                lease_acquired_at=lease.acquired_at,
                attempt_id=receipt.attempt_id,
                attempt_started_at=receipt.started_at,
                worker_id=receipt.worker_id,
                manifest_digest=str(lease.metadata.get("manifest_digest") or ""),
                call_ref=receipt.backend_receipt_ref,
                call_started_at=str(metadata.get("call_started_at") or ""),
                call_finished_at=str(metadata.get("call_finished_at") or ""),
                completion_receipt_ref=receipt.receipt_id,
                artifact_refs=receipt.artifact_refs,
                verification_refs=tuple(
                    str(item) for item in metadata.get("verification_refs") or ()
                ),
                actual_tokens=int(metadata.get("actual_tokens") or 0),
                actual_cost_usd=float(metadata.get("actual_cost_usd") or 0),
                actual_latency_ms=int(metadata.get("actual_latency_ms") or 0),
                outcome=receipt.outcome.value,
                operator_idempotency_key=operator_idempotency_key,
                physical_dispatch_receipt_ref=str(
                    metadata.get("physical_dispatch_receipt_ref") or ""
                ),
                physical_dispatch_receipt_digest=str(
                    metadata.get("physical_dispatch_receipt_digest") or ""
                ),
                replayed=True,
            )
        return None

    def _close_failed_attempt(self, acquisition: Any, *, summary: str) -> None:
        lease = self.pool.store.get_lease(acquisition.lease.lease_id)
        if lease is None or lease.terminal:
            return
        try:
            self.pool.leases.complete(
                lease.lease_id,
                worker_id=lease.worker_id,
                fence_token=lease.fence_token,
                fence_epoch=lease.fence_epoch,
                outcome=ExecutionOutcome.FAILED,
                summary=summary,
                error_code="operator_execution_failed",
                error_message=summary,
                metadata={"operator_failure": True},
            )
        except (LeaseFenced, WorkerPoolError):
            return

    def _plan_recovery(
        self,
        *,
        task: TaskState,
        node: PlanNode | None,
        exc: Exception,
        failed_worker: str,
        cause_ref: str,
    ) -> Any:
        if isinstance(exc, OperatorPlacementError):
            kind = (
                FailureKind.PERMISSION_DENIED
                if "permission" in exc.code
                else FailureKind.WORKER_UNAVAILABLE
                if exc.retryable
                else FailureKind.VALIDATION_FAILED
            )
            retryable = exc.retryable
        elif isinstance(exc, (LeaseFenced, WorkerPoolError)):
            kind = FailureKind.WORKER_UNAVAILABLE
            retryable = True
        else:
            kind = FailureKind.UNKNOWN
            retryable = True
        signal = FailureSignal(
            run_id=task.run_id,
            task_id=task.task_id,
            node_id=None if node is None else node.node_id,
            kind=kind,
            summary=f"operator placement/lease execution failed: {exc}",
            failed_worker=failed_worker,
            retryable=retryable,
            evidence_event_ids=[cause_ref],
            metadata={
                "failure_owner": "RecoveryPlanner",
                "operator_integration_error": getattr(
                    exc, "code", type(exc).__name__
                ),
            },
        )
        return self.recovery_planner.plan(task, signal, node=node)

    @staticmethod
    def _placement_event(
        receipt: OperatorPlacementDecisionReceipt,
    ) -> EventRecord:
        return EventRecord(
            run_id=receipt.run_id,
            task_id=receipt.task_id,
            event_id=f"event_operator_placement_{receipt.digest[:24]}",
            event_type=EventType.RESOURCE_DECISION,
            payload={
                "schema": OPERATOR_PLACEMENT_DECISION_SCHEMA,
                "operator_placement": receipt.to_dict(),
                "execution_before_lease_forbidden": True,
            },
        )

    @staticmethod
    def _attempt_event(receipt: OperatorAttemptReceipt) -> EventRecord:
        return EventRecord(
            run_id=receipt.run_id,
            task_id=receipt.task_id,
            event_id=f"event_operator_attempt_{receipt.digest[:24]}",
            event_type=EventType.ARTIFACT_WRITTEN,
            payload={
                "schema": OPERATOR_ATTEMPT_RECEIPT_SCHEMA,
                "operator_attempt": receipt.to_dict(),
                "lease_precedes_execution": (
                    _utc(receipt.lease_acquired_at)
                    <= _utc(receipt.attempt_started_at)
                    <= _utc(receipt.call_started_at)
                ),
            },
        )


def _placement_candidate(
    candidate: OperatorCandidate,
    profile: OperatorProfile,
    *,
    layer_index: int,
    layer_rank: int,
) -> PlacementOperatorCandidate:
    return PlacementOperatorCandidate(
        operator_id=candidate.operator_id,
        operator_type=candidate.operator_type,
        version=candidate.version,
        profile_digest=candidate.profile_digest,
        source_ref=profile.source_ref,
        source_registry=profile.source_registry,
        layer_index=layer_index,
        layer_rank=layer_rank,
        proposal_score=candidate.score,
        capabilities=profile.capabilities,
        input_contract=profile.input_contract,
        output_contract=profile.output_contract,
        required_permissions=profile.required_permissions,
        allowed_locations=profile.allowed_locations,
        allowed_privacy_classes=profile.allowed_privacy_classes,
        health_status=profile.health_status,
        available_capacity=profile.available_capacity,
        verifier_contracts=profile.verifier_contracts,
        minimum_evidence_contract=profile.minimum_evidence_contract,
        estimated_tokens=profile.estimated_tokens,
        estimated_cost_usd=profile.estimated_cost_usd,
        estimated_latency_ms=profile.estimated_latency_ms,
        cold_start=profile.cold_start,
        confidence=profile.confidence,
        score_components=FrozenDict(candidate.score_components.to_dict()),
    )


def _capability_requirement(
    candidate: PlacementOperatorCandidate,
) -> CapabilityRequirement:
    locations: list[WorkerLocation] = []
    for value in candidate.allowed_locations:
        try:
            locations.append(WorkerLocation(value))
        except ValueError:
            continue
    tools: tuple[str, ...] = ()
    if candidate.operator_type == OperatorType.TOOL.value:
        tools = (candidate.source_ref or candidate.operator_id.split(":", 1)[-1],)
    return CapabilityRequirement(
        required=candidate.capabilities,
        tool_ids=tools,
        locations=tuple(locations),
        resources=ResourceVector(process_slots=1),
    )


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(
        item in "0123456789abcdef" for item in value.lower()
    )


def _utc(value: str) -> datetime:
    try:
        return parse_utc(value)
    except Exception as exc:  # noqa: BLE001 - contract validation.
        raise OperatorPlacementError(
            "operator_timestamp_invalid",
            f"invalid operator placement timestamp: {value}",
        ) from exc


__all__ = [
    "OPERATOR_ATTEMPT_RECEIPT_SCHEMA",
    "OPERATOR_CANDIDATE_SET_SCHEMA",
    "OPERATOR_PLACEMENT_DECISION_SCHEMA",
    "OPERATOR_TASK_RESULT_SCHEMA",
    "OperatorAttemptReceipt",
    "OperatorCallPort",
    "OperatorCallResult",
    "OperatorCandidateSet",
    "OperatorLeaseExecutionContext",
    "OperatorPlacementDecisionReceipt",
    "OperatorPlacementError",
    "OperatorPlacementLeaseConfig",
    "OperatorPlacementLeaseRuntime",
    "OperatorPlacementTaskResult",
    "PlacementOperatorCandidate",
]
