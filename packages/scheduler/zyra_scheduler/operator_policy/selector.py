from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from zyra_orchestration.topology_policy.contracts import (
    ContractHeader,
    FrozenDict,
    PolicyInputSnapshot,
    canonical_digest,
)

from .catalog import (
    OperatorCatalog,
    OperatorCatalogError,
    OperatorProfile,
    OperatorType,
)
from .encoder import (
    DeterministicOperatorEncoder,
    OperatorEncoding,
    OperatorSelectionContext,
)


SELECTOR_CONFIG_SCHEMA = "zyra.maas-operator-selector-config/v1"
OPERATOR_PROPOSAL_SCHEMA = "zyra.operator-selection-proposal/v1"
OPERATOR_SCHEDULER_INPUT_SCHEMA = "zyra.operator-scheduler-input/v1"


class OperatorSelectionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _tokens(values: Any) -> tuple[str, ...]:
    if values is None:
        selected: Sequence[Any] = ()
    elif isinstance(values, str):
        selected = (values,)
    elif isinstance(values, Mapping):
        selected = tuple(values.keys())
    else:
        selected = tuple(values)
    return tuple(
        sorted(
            {
                str(item).strip().lower().replace(" ", "_")
                for item in selected
                if str(item).strip()
            }
        )
    )


@dataclass(frozen=True, slots=True)
class OperatorSelectorConfig:
    mechanism_id: str
    mechanism_version: str
    catalog_schema_version: str
    proposal_schema_version: str
    scheduler_input_schema_version: str
    fallback_profile: str
    input_precheck_report_digest: str
    proposal_ttl_seconds: int
    candidate_cap: int
    maximum_breadth: int
    maximum_depth: int
    medium_budget_tokens: int
    high_budget_tokens: int
    medium_budget_time_ms: int
    high_budget_time_ms: int
    cold_start_confidence: float
    established_confidence: float
    cold_start_token_reserve_ratio: float
    score_weights: FrozenDict
    no_policy_training: FrozenDict
    digest: str
    path: str = ""

    @classmethod
    def load(cls, path: Path) -> "OperatorSelectorConfig":
        selected = path.resolve()
        try:
            value = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OperatorSelectionError(
                "maas_config_invalid",
                f"MaAS selector configuration is missing or corrupt: {selected}",
            ) from exc
        if not isinstance(value, Mapping) or value.get("schema") != SELECTOR_CONFIG_SCHEMA:
            raise OperatorSelectionError(
                "maas_config_schema_invalid",
                "unsupported MaAS operator selector configuration",
            )
        no_training = value.get("no_policy_training")
        if not isinstance(no_training, Mapping) or (
            no_training.get("training_allowed") is not False
            or no_training.get("sampling_allowed") is not False
            or no_training.get("pretrained_model_used") is not False
            or no_training.get("datasets") not in ([], ())
            or no_training.get("checkpoints") not in ([], ())
            or no_training.get("mutable_learned_parameters") not in ([], ())
        ):
            raise OperatorSelectionError(
                "maas_policy_training_forbidden",
                "MaAS configuration cannot enable training, sampling, datasets, checkpoints, or learned parameters",
            )
        weights = value.get("score_weights")
        expected_weights = {
            "capability_obligation_coverage",
            "permission",
            "health",
            "cost",
            "latency",
            "verifier_necessity",
            "semantic_match",
            "confidence",
        }
        if (
            not isinstance(weights, Mapping)
            or set(weights) != expected_weights
            or any(float(item) < 0 for item in weights.values())
        ):
            raise OperatorSelectionError(
                "maas_score_weights_invalid",
                "MaAS requires the fixed non-negative eight-term score",
            )
        limits = {
            "proposal_ttl_seconds": int(value.get("proposal_ttl_seconds") or 0),
            "candidate_cap": int(value.get("candidate_cap") or 0),
            "maximum_breadth": int(value.get("maximum_breadth") or 0),
            "maximum_depth": int(value.get("maximum_depth") or 0),
        }
        if any(item < 1 for item in limits.values()):
            raise OperatorSelectionError(
                "maas_selector_limits_invalid",
                "MaAS selector caps and TTL must be positive",
            )
        report_digest = str(value.get("input_precheck_report_digest") or "")
        if len(report_digest) != 64:
            raise OperatorSelectionError(
                "maas_readiness_digest_invalid",
                "MaAS input precheck report digest must be SHA-256",
            )
        return cls(
            mechanism_id=str(value.get("mechanism_id") or ""),
            mechanism_version=str(value.get("mechanism_version") or ""),
            catalog_schema_version=str(value.get("catalog_schema_version") or ""),
            proposal_schema_version=str(value.get("proposal_schema_version") or ""),
            scheduler_input_schema_version=str(
                value.get("scheduler_input_schema_version") or ""
            ),
            fallback_profile=str(value.get("fallback_profile") or ""),
            input_precheck_report_digest=report_digest,
            proposal_ttl_seconds=limits["proposal_ttl_seconds"],
            candidate_cap=limits["candidate_cap"],
            maximum_breadth=limits["maximum_breadth"],
            maximum_depth=limits["maximum_depth"],
            medium_budget_tokens=max(1, int(value.get("medium_budget_tokens") or 1)),
            high_budget_tokens=max(1, int(value.get("high_budget_tokens") or 1)),
            medium_budget_time_ms=max(
                1, int(value.get("medium_budget_time_ms") or 1)
            ),
            high_budget_time_ms=max(
                1, int(value.get("high_budget_time_ms") or 1)
            ),
            cold_start_confidence=min(
                1.0, max(0.0, float(value.get("cold_start_confidence") or 0))
            ),
            established_confidence=min(
                1.0,
                max(0.0, float(value.get("established_confidence") or 0)),
            ),
            cold_start_token_reserve_ratio=min(
                1.0,
                max(
                    0.0,
                    float(value.get("cold_start_token_reserve_ratio") or 0),
                ),
            ),
            score_weights=FrozenDict(
                {str(key): float(item) for key, item in weights.items()}
            ),
            no_policy_training=FrozenDict(no_training),
            digest=canonical_digest(value),
            path=selected.as_posix(),
        )


@dataclass(frozen=True, slots=True)
class OperatorSelectionRequest:
    policy_input: PolicyInputSnapshot
    query: str
    required_capabilities: tuple[str, ...] = ()
    required_input_contract: tuple[str, ...] = ()
    required_output_contract: tuple[str, ...] = ()
    required_verifier_contracts: tuple[str, ...] = ()
    verifier_necessary: bool = False

    def __post_init__(self) -> None:
        if not str(self.query).strip():
            raise OperatorSelectionError(
                "maas_query_missing",
                "operator selection requires a non-empty query",
            )
        for name in (
            "required_capabilities",
            "required_input_contract",
            "required_output_contract",
            "required_verifier_contracts",
        ):
            object.__setattr__(self, name, _tokens(getattr(self, name)))

    @property
    def context(self) -> OperatorSelectionContext:
        roles = tuple(sorted({item.role for item in self.policy_input.nodes}))
        desired = tuple(
            sorted(
                {
                    *self.policy_input.registered_capabilities,
                    *(
                        capability
                        for item in self.policy_input.nodes
                        for capability in item.capabilities
                    ),
                    *self.required_capabilities,
                }
            )
        )
        return OperatorSelectionContext(
            query=self.query,
            phase=self.policy_input.phase,
            requirement_revision=self.policy_input.requirement_revision,
            roles=roles,
            desired_capabilities=desired,
            obligations=self.policy_input.unresolved_obligations,
            privacy_class=self.policy_input.privacy_class,
            allowed_locations=self.policy_input.allowed_placements,
            remaining_tokens=self.policy_input.budget.remaining_tokens,
            remaining_cost_usd=self.policy_input.budget.remaining_cost_usd,
            remaining_time_ms=self.policy_input.budget.remaining_time_ms,
            topology_commit_id=self.policy_input.graph.commit_id,
            topology_signature=self.policy_input.graph.signature,
        )


@dataclass(frozen=True, slots=True)
class OperatorFilterVerdict:
    operator_id: str
    executable: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "executable": self.executable,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class OperatorScoreComponents:
    capability_obligation_coverage: int
    permission: int
    health: int
    cost: int
    latency: int
    verifier_necessity: int
    semantic_match: int
    confidence: int

    def to_dict(self) -> dict[str, int]:
        return {
            name: int(getattr(self, name))
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class OperatorCandidate:
    operator_id: str
    operator_type: str
    version: str
    profile_digest: str
    score: float
    score_components: OperatorScoreComponents
    reasons: tuple[str, ...]
    encoding_digest: str
    cold_start: bool
    confidence: float
    estimated_tokens: int
    estimated_cost_usd: float
    estimated_latency_ms: int

    @property
    def reference(self) -> tuple[str, str]:
        return (self.operator_id, self.version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "operator_type": self.operator_type,
            "version": self.version,
            "profile_digest": self.profile_digest,
            "score": self.score,
            "score_components": self.score_components.to_dict(),
            "reasons": list(self.reasons),
            "encoding_digest": self.encoding_digest,
            "cold_start": self.cold_start,
            "confidence": self.confidence,
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_latency_ms": self.estimated_latency_ms,
        }


@dataclass(frozen=True, slots=True)
class OperatorLayerProposal:
    layer_index: int
    candidates: tuple[OperatorCandidate, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer_index": self.layer_index,
            "candidates": [item.to_dict() for item in self.candidates],
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OperatorSelectionProposal:
    header: ContractHeader
    proposal_id: str
    input_snapshot_digest: str
    requirement_revision: str
    committed_graph_id: str
    committed_graph_revision: int
    committed_graph_signature: str
    committed_graph_commit_id: str
    catalog_version: str
    catalog_digest: str
    context_digest: str
    encoder_profile: str
    encoder_observation_digest: str
    layers: tuple[OperatorLayerProposal, ...]
    alternatives: tuple[OperatorCandidate, ...]
    filter_verdicts: tuple[OperatorFilterVerdict, ...]
    expected_breadth: int
    expected_depth: int
    reasons: tuple[str, ...]
    expires_at: str
    fallback_profile: str
    placement_owner: str = "ResourceScheduler"
    lease_owner: str = "WorkerPoolFoundationRuntime"
    schema_version: str = OPERATOR_PROPOSAL_SCHEMA

    @property
    def candidates(self) -> tuple[OperatorCandidate, ...]:
        return tuple(item for layer in self.layers for item in layer.candidates)

    @property
    def digest(self) -> str:
        return canonical_digest(self.canonical_data())

    def canonical_data(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_kind": "operator_selection_proposal",
            **self.header.to_dict(),
            "payload": {
                "proposal_id": self.proposal_id,
                "input_snapshot_digest": self.input_snapshot_digest,
                "requirement_revision": self.requirement_revision,
                "committed_graph_id": self.committed_graph_id,
                "committed_graph_revision": self.committed_graph_revision,
                "committed_graph_signature": self.committed_graph_signature,
                "committed_graph_commit_id": self.committed_graph_commit_id,
                "catalog_version": self.catalog_version,
                "catalog_digest": self.catalog_digest,
                "context_digest": self.context_digest,
                "encoder_profile": self.encoder_profile,
                "encoder_observation_digest": self.encoder_observation_digest,
                "layers": [item.to_dict() for item in self.layers],
                "alternatives": [item.to_dict() for item in self.alternatives],
                "filter_verdicts": [
                    item.to_dict() for item in self.filter_verdicts
                ],
                "expected_breadth": self.expected_breadth,
                "expected_depth": self.expected_depth,
                "reasons": list(self.reasons),
                "expires_at": self.expires_at,
                "fallback_profile": self.fallback_profile,
                "placement_owner": self.placement_owner,
                "lease_owner": self.lease_owner,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.canonical_data(), "digest": self.digest}

    def scheduler_input(self) -> "OperatorSchedulerInput":
        return OperatorSchedulerInput(
            proposal_id=self.proposal_id,
            proposal_digest=self.digest,
            run_id="",
            task_id="",
            input_snapshot_digest=self.input_snapshot_digest,
            requirement_revision=self.requirement_revision,
            committed_graph_revision=self.committed_graph_revision,
            committed_graph_signature=self.committed_graph_signature,
            committed_graph_commit_id=self.committed_graph_commit_id,
            catalog_version=self.catalog_version,
            catalog_digest=self.catalog_digest,
            candidate_references=tuple(
                item.reference for item in self.candidates
            ),
            expected_breadth=self.expected_breadth,
            expected_depth=self.expected_depth,
            expires_at=self.expires_at,
            placement_owner=self.placement_owner,
            lease_owner=self.lease_owner,
        )


@dataclass(frozen=True, slots=True)
class OperatorSchedulerInput:
    proposal_id: str
    proposal_digest: str
    run_id: str
    task_id: str
    input_snapshot_digest: str
    requirement_revision: str
    committed_graph_revision: int
    committed_graph_signature: str
    committed_graph_commit_id: str
    catalog_version: str
    catalog_digest: str
    candidate_references: tuple[tuple[str, str], ...]
    expected_breadth: int
    expected_depth: int
    expires_at: str
    placement_owner: str
    lease_owner: str
    diagnostic_only: bool = False
    schema_version: str = OPERATOR_SCHEDULER_INPUT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_references",
            tuple((str(item[0]), str(item[1])) for item in self.candidate_references),
        )
        if not self.candidate_references:
            raise OperatorSelectionError(
                "maas_scheduler_candidates_empty",
                "scheduler input requires at least one operator candidate",
            )
        if self.placement_owner != "ResourceScheduler":
            raise OperatorSelectionError(
                "maas_scheduler_owner_invalid",
                "MaAS cannot replace the ResourceScheduler placement owner",
            )

    def bind_task(self, *, run_id: str, task_id: str) -> "OperatorSchedulerInput":
        return OperatorSchedulerInput(
            **{
                **self.to_dict(),
                "run_id": run_id,
                "task_id": task_id,
                "candidate_references": self.candidate_references,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "input_snapshot_digest": self.input_snapshot_digest,
            "requirement_revision": self.requirement_revision,
            "committed_graph_revision": self.committed_graph_revision,
            "committed_graph_signature": self.committed_graph_signature,
            "committed_graph_commit_id": self.committed_graph_commit_id,
            "catalog_version": self.catalog_version,
            "catalog_digest": self.catalog_digest,
            "candidate_references": [
                {"operator_id": item[0], "version": item[1]}
                for item in self.candidate_references
            ],
            "expected_breadth": self.expected_breadth,
            "expected_depth": self.expected_depth,
            "expires_at": self.expires_at,
            "placement_owner": self.placement_owner,
            "lease_owner": self.lease_owner,
            "diagnostic_only": self.diagnostic_only,
        }


@dataclass(frozen=True, slots=True)
class OperatorSelectionResult:
    proposal: OperatorSelectionProposal
    selected: tuple[OperatorCandidate, ...]
    alternatives: tuple[OperatorCandidate, ...]
    filter_verdicts: tuple[OperatorFilterVerdict, ...]


class DeterministicOperatorSelector:
    """Cropped MaAS operator/multilayer control with no sampling or training."""

    def __init__(
        self,
        config: OperatorSelectorConfig,
        *,
        encoder: DeterministicOperatorEncoder | None = None,
    ) -> None:
        self.config = config
        self.encoder = encoder or DeterministicOperatorEncoder()

    def select(
        self,
        *,
        catalog: OperatorCatalog,
        request: OperatorSelectionRequest,
        readiness_report_digest: str,
    ) -> OperatorSelectionResult:
        if catalog.schema_version != self.config.catalog_schema_version:
            raise OperatorSelectionError(
                "maas_catalog_schema_mismatch",
                "operator catalog schema does not match the pinned selector config",
            )
        policy_input = request.policy_input
        if not policy_input.graph.commit_id:
            raise OperatorSelectionError(
                "maas_committed_topology_missing",
                "MaAS selection requires a GraphStateCustody commit id",
            )
        if not request.policy_input.unresolved_obligations:
            raise OperatorSelectionError(
                "maas_obligations_missing",
                "MaAS selection requires active unresolved obligations",
            )
        context = request.context
        verdicts: list[OperatorFilterVerdict] = []
        scored: list[tuple[OperatorCandidate, OperatorProfile, OperatorEncoding]] = []
        for profile in catalog.entries:
            verdict = self._filter(profile, request)
            verdicts.append(verdict)
            if not verdict.executable:
                continue
            encoding = self.encoder.encode(context, profile)
            candidate = self._score(profile, request, encoding)
            scored.append((candidate, profile, encoding))
        if not scored:
            raise OperatorSelectionError(
                "maas_no_executable_operator",
                "all catalog operators were rejected by capability, permission, privacy, health, budget, or contract filters",
            )
        scored.sort(
            key=lambda item: (
                -item[0].score,
                item[0].operator_type,
                item[0].operator_id,
                item[0].version,
            )
        )
        scored = self._first_layer_guard(scored)
        breadth, depth = self._breadth_depth(
            request,
            executable_count=len(scored),
        )
        selected_count = min(len(scored), breadth * depth, self.config.candidate_cap)
        selected_rows = list(scored[:selected_count])
        selected_rows = self._ensure_verifier_candidate(
            selected_rows,
            scored,
            request,
        )
        layers = tuple(
            OperatorLayerProposal(
                layer_index=index + 1,
                candidates=tuple(
                    item[0]
                    for item in selected_rows[index * breadth : (index + 1) * breadth]
                ),
                reason=self._layer_reason(index + 1, request),
            )
            for index in range(depth)
            if selected_rows[index * breadth : (index + 1) * breadth]
        )
        selected = tuple(item for layer in layers for item in layer.candidates)
        selected_refs = {item.reference for item in selected}
        alternatives = tuple(
            item[0] for item in scored if item[0].reference not in selected_refs
        )
        encoding_digest = canonical_digest(
            [item[2].to_dict() for item in scored]
        )
        proposal_seed = canonical_digest(
            {
                "policy_input_digest": policy_input.digest,
                "catalog_digest": catalog.digest,
                "context_digest": context.digest,
                "configuration_digest": self.config.digest,
                "readiness_report_digest": readiness_report_digest,
                "selected": [item.to_dict() for item in selected],
                "breadth": breadth,
                "depth": len(layers),
            }
        )
        proposal_id = f"maas-proposal-{proposal_seed[:24]}"
        header = ContractHeader(
            contract_id=proposal_id,
            created_at=policy_input.header.created_at,
            source_event_id=policy_input.header.source_event_id,
            correlation_id=policy_input.header.correlation_id,
            causation_id=policy_input.graph.commit_id,
            mechanism_id=self.config.mechanism_id,
            mechanism_version=self.config.mechanism_version,
            input_version=policy_input.SCHEMA_VERSION,
            idempotency_key=f"maas:{proposal_seed}",
            configuration_digest=self.config.digest,
        )
        expires_at = (
            _parse_time(header.created_at)
            + timedelta(seconds=self.config.proposal_ttl_seconds)
        ).isoformat().replace("+00:00", "Z")
        proposal = OperatorSelectionProposal(
            header=header,
            proposal_id=proposal_id,
            input_snapshot_digest=policy_input.digest,
            requirement_revision=policy_input.requirement_revision,
            committed_graph_id=policy_input.graph.graph_id,
            committed_graph_revision=policy_input.graph.revision,
            committed_graph_signature=policy_input.graph.signature,
            committed_graph_commit_id=policy_input.graph.commit_id,
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.digest,
            context_digest=context.digest,
            encoder_profile=self.encoder.profile,
            encoder_observation_digest=encoding_digest,
            layers=layers,
            alternatives=alternatives,
            filter_verdicts=tuple(verdicts),
            expected_breadth=max(
                (len(layer.candidates) for layer in layers), default=0
            ),
            expected_depth=len(layers),
            reasons=(
                f"conditioned on committed graph {policy_input.graph.commit_id}",
                f"phase={policy_input.phase}",
                f"requirement_revision={policy_input.requirement_revision}",
                f"unresolved_obligations={len(policy_input.unresolved_obligations)}",
                "stable constrained score and tie-break applied",
                "ResourceScheduler retains placement ownership",
            ),
            expires_at=expires_at,
            fallback_profile=self.config.fallback_profile,
        )
        return OperatorSelectionResult(
            proposal=proposal,
            selected=selected,
            alternatives=alternatives,
            filter_verdicts=tuple(verdicts),
        )

    def validate_proposal(
        self,
        *,
        proposal: OperatorSelectionProposal,
        catalog: OperatorCatalog,
        policy_input: PolicyInputSnapshot,
    ) -> None:
        if proposal.input_snapshot_digest != policy_input.digest:
            raise OperatorSelectionError(
                "maas_input_snapshot_drift",
                "operator proposal belongs to a stale task or requirement revision",
            )
        if (
            proposal.committed_graph_signature != policy_input.graph.signature
            or proposal.committed_graph_revision != policy_input.graph.revision
            or proposal.committed_graph_commit_id != policy_input.graph.commit_id
        ):
            raise OperatorSelectionError(
                "maas_committed_topology_drift",
                "operator proposal belongs to a stale committed topology",
            )
        catalog.validate_references(
            catalog_version=proposal.catalog_version,
            catalog_digest=proposal.catalog_digest,
            references=tuple(item.reference for item in proposal.candidates),
        )

    def _filter(
        self,
        profile: OperatorProfile,
        request: OperatorSelectionRequest,
    ) -> OperatorFilterVerdict:
        reasons: list[str] = []
        policy_input = request.policy_input
        if not profile.enabled or profile.revoked:
            reasons.append("operator disabled or revoked")
        if request.required_capabilities and not set(
            request.required_capabilities
        ).issubset(profile.capabilities):
            reasons.append("missing required capability")
        if not set(profile.required_permissions).issubset(
            policy_input.allowed_permissions
        ):
            reasons.append("required permission not allowed")
        if (
            policy_input.allowed_placements
            and not set(profile.allowed_locations).intersection(
                policy_input.allowed_placements
            )
        ):
            reasons.append("location not allowed")
        if (
            policy_input.privacy_class not in profile.allowed_privacy_classes
            and "*" not in profile.allowed_privacy_classes
        ):
            reasons.append("privacy class not allowed")
        if profile.health_status.lower() not in {"healthy", "degraded"}:
            reasons.append("operator health unavailable")
        if profile.available_capacity < 1:
            reasons.append("operator capacity exhausted")
        if (
            not profile.input_contract
            or not profile.output_contract
            or not profile.verifier_contracts
            or not profile.minimum_evidence_contract
        ):
            reasons.append("operator contract or verifier evidence contract missing")
        if request.required_input_contract and not set(
            request.required_input_contract
        ).issubset(profile.input_contract):
            reasons.append("input contract mismatch")
        if request.required_output_contract and not set(
            request.required_output_contract
        ).issubset(profile.output_contract):
            reasons.append("output contract mismatch")
        if request.required_verifier_contracts and not set(
            request.required_verifier_contracts
        ).issubset(profile.verifier_contracts):
            reasons.append("verifier contract mismatch")
        if profile.estimated_tokens > policy_input.budget.remaining_tokens:
            reasons.append("token budget exceeded")
        if profile.estimated_cost_usd > policy_input.budget.remaining_cost_usd:
            reasons.append("cost budget exceeded")
        if profile.estimated_latency_ms > policy_input.budget.remaining_time_ms:
            reasons.append("latency budget exceeded")
        if profile.cold_start:
            required_reserve = round(
                profile.estimated_tokens
                * (1.0 + self.config.cold_start_token_reserve_ratio)
            )
            if required_reserve > policy_input.budget.remaining_tokens:
                reasons.append("cold-start token reserve unavailable")
        return OperatorFilterVerdict(
            operator_id=profile.operator_id,
            executable=not reasons,
            reasons=tuple(reasons or ("all hard filters passed",)),
        )

    def _score(
        self,
        profile: OperatorProfile,
        request: OperatorSelectionRequest,
        encoding: OperatorEncoding,
    ) -> OperatorCandidate:
        context = request.context
        desired = set(context.desired_capabilities)
        capability_overlap = (
            0
            if not desired
            else round(10_000 * len(desired.intersection(profile.capabilities)) / len(desired))
        )
        obligation_terms = set(encoding.query_terms)
        profile_terms = set(encoding.operator_terms)
        obligation_overlap = (
            0
            if not obligation_terms
            else round(10_000 * len(obligation_terms.intersection(profile_terms)) / len(obligation_terms))
        )
        coverage = max(capability_overlap, obligation_overlap)
        health = 10_000 if profile.health_status.lower() == "healthy" else 5_000
        budget_cost = max(request.policy_input.budget.remaining_cost_usd, 0.000001)
        cost = max(
            0,
            round(10_000 * (1.0 - profile.estimated_cost_usd / budget_cost)),
        )
        budget_time = max(request.policy_input.budget.remaining_time_ms, 1)
        latency = max(
            0,
            round(10_000 * (1.0 - profile.estimated_latency_ms / budget_time)),
        )
        verifier = self._verifier_score(profile, request)
        components = OperatorScoreComponents(
            capability_obligation_coverage=coverage,
            permission=10_000,
            health=health,
            cost=cost,
            latency=latency,
            verifier_necessity=verifier,
            semantic_match=encoding.semantic_score_basis_points,
            confidence=round(profile.confidence * 10_000),
        )
        weighted = sum(
            float(self.config.score_weights[name]) * value
            for name, value in components.to_dict().items()
        )
        weight_total = sum(float(item) for item in self.config.score_weights.values())
        score = round(weighted / max(weight_total, 0.000001), 8)
        reasons = [
            f"{name}={value}"
            for name, value in components.to_dict().items()
        ]
        if profile.cold_start:
            reasons.append(
                "cold-start confidence is conservative and budget reserve was enforced"
            )
        return OperatorCandidate(
            operator_id=profile.operator_id,
            operator_type=profile.operator_type.value,
            version=profile.version,
            profile_digest=profile.digest,
            score=score,
            score_components=components,
            reasons=tuple(reasons),
            encoding_digest=encoding.observation_digest,
            cold_start=profile.cold_start,
            confidence=profile.confidence,
            estimated_tokens=profile.estimated_tokens,
            estimated_cost_usd=profile.estimated_cost_usd,
            estimated_latency_ms=profile.estimated_latency_ms,
        )

    @staticmethod
    def _verifier_score(
        profile: OperatorProfile,
        request: OperatorSelectionRequest,
    ) -> int:
        verifier_terms = {
            "verification",
            "verify",
            "verifier",
            "testing",
            "review",
            "validation",
            "critique",
        }
        matches = bool(
            verifier_terms.intersection(profile.capabilities)
            or any(
                any(term in item for term in verifier_terms)
                for item in profile.verifier_contracts
            )
        )
        necessary = request.verifier_necessary or request.policy_input.phase.lower() in {
            "verify",
            "verification",
            "review",
            "recovery",
        }
        if necessary:
            return 10_000 if matches else 2_000
        return 6_000 if matches else 5_000

    def _breadth_depth(
        self,
        request: OperatorSelectionRequest,
        *,
        executable_count: int,
    ) -> tuple[int, int]:
        budget = request.policy_input.budget
        complexity = (
            len(request.policy_input.unresolved_obligations)
            + len({item.role for item in request.policy_input.nodes})
            + (1 if request.verifier_necessary else 0)
        )
        breadth = 1
        depth = 1
        if (
            budget.remaining_tokens >= self.config.medium_budget_tokens
            and budget.remaining_time_ms >= self.config.medium_budget_time_ms
            and complexity >= 3
        ):
            breadth = 2
            depth = 2
        if (
            budget.remaining_tokens >= self.config.high_budget_tokens
            and budget.remaining_time_ms >= self.config.high_budget_time_ms
            and complexity >= 5
        ):
            breadth = min(self.config.maximum_breadth, 3)
            depth = min(self.config.maximum_depth, 3)
        if request.policy_input.phase.lower() in {"verification", "recovery"}:
            depth = min(self.config.maximum_depth, depth + 1)
        breadth = min(
            breadth,
            max(1, budget.max_fan_out),
            executable_count,
            self.config.maximum_breadth,
        )
        depth = min(
            depth,
            self.config.maximum_depth,
            max(1, (executable_count + breadth - 1) // breadth),
        )
        return breadth, depth

    @staticmethod
    def _first_layer_guard(
        rows: list[tuple[OperatorCandidate, OperatorProfile, OperatorEncoding]],
    ) -> list[tuple[OperatorCandidate, OperatorProfile, OperatorEncoding]]:
        if not rows:
            return rows
        for index, row in enumerate(rows):
            profile = row[1]
            verifier_only = (
                profile.operator_type is OperatorType.MODEL
                and any("verifier" in item for item in profile.verifier_contracts)
                and not set(profile.capabilities).intersection(
                    {"execution", "tool_use", "artifact_production", "coding", "browser"}
                )
            )
            if not verifier_only:
                if index:
                    return [row, *rows[:index], *rows[index + 1 :]]
                return rows
        return rows

    def _ensure_verifier_candidate(
        self,
        selected: list[tuple[OperatorCandidate, OperatorProfile, OperatorEncoding]],
        all_rows: list[tuple[OperatorCandidate, OperatorProfile, OperatorEncoding]],
        request: OperatorSelectionRequest,
    ) -> list[tuple[OperatorCandidate, OperatorProfile, OperatorEncoding]]:
        necessary = request.verifier_necessary or request.policy_input.phase.lower() in {
            "verify",
            "verification",
            "review",
            "recovery",
        }
        if not necessary:
            return selected
        if any(self._verifier_score(item[1], request) == 10_000 for item in selected):
            return selected
        verifier = next(
            (
                item
                for item in all_rows
                if self._verifier_score(item[1], request) == 10_000
            ),
            None,
        )
        if verifier is None:
            raise OperatorSelectionError(
                "maas_required_verifier_missing",
                "the executable catalog has no required verifier operator",
            )
        if selected:
            selected[-1] = verifier
        else:
            selected.append(verifier)
        deduped: list[
            tuple[OperatorCandidate, OperatorProfile, OperatorEncoding]
        ] = []
        seen: set[tuple[str, str]] = set()
        for item in selected:
            if item[0].reference not in seen:
                deduped.append(item)
                seen.add(item[0].reference)
        return deduped

    @staticmethod
    def _layer_reason(index: int, request: OperatorSelectionRequest) -> str:
        if index == 1:
            return "first layer preserves a concrete execution-capable operator"
        if request.verifier_necessary and index > 1:
            return "later layer expands coverage and preserves verifier necessity"
        return "later layer expands obligation coverage within the pinned budget"


def _parse_time(value: str) -> datetime:
    rendered = value.strip()
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    parsed = datetime.fromisoformat(rendered)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
